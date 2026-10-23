"""Workbook signatures using the keyring selected at call time.

No database access or business mutation is performed here. Global cost tokens
still accept loaded model objects, and configuration is intentionally not cached.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation

from app.config import get_settings
from app.models.maintenance import FMaintenanceLine, MaintenanceManualCostOverride
from app.services.maintenance_workbook_plans import WorkbookError
from app.services.maintenance_workbook_protocol import (
    _GLOBAL_COST_TOKEN_DOMAIN, _V2_ROW_BASE_DOMAIN, V2_META_SIGNATURE_ALGORITHM,
)
from app.services.maintenance_workbook_values import (
    _global_cost_decimal, _v2_hash_value, _chunk_meta_value, _v2_metadata_signature,
)


def _global_cost_base_token(
    *,
    line: FMaintenanceLine,
    override: MaintenanceManualCostOverride | None,
    display_unit_cost_ex_tax: Decimal | None,
    display_unit_cost_inc_tax: Decimal | None,
    visible_reason: str | None,
) -> str:
    """Signed base snapshot used to reject stale concurrent global uploads."""

    payload = {
        "line_id": int(line.id),
        "override_version": override.version if override is not None else None,
        "override_active": override.active if override is not None else None,
        "unit_cost_ex_tax": _global_cost_decimal(display_unit_cost_ex_tax),
        "unit_cost_inc_tax": _global_cost_decimal(display_unit_cost_inc_tax),
        "reason": visible_reason or None,
    }
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    key_id, signing_key = get_settings().maintenance_manifest_signing_material()
    signature = hmac.new(
        signing_key,
        _GLOBAL_COST_TOKEN_DOMAIN + payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return json.dumps(
        {"key_id": key_id, "payload": payload, "signature": signature},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_global_cost_base_token(value, *, line_id: int, row_no: int) -> dict:
    try:
        envelope = json.loads(str(value))
        key_id = str(envelope["key_id"])
        payload = envelope["payload"]
        signature = str(envelope["signature"])
        if not isinstance(payload, dict) or int(payload["line_id"]) != line_id:
            raise ValueError("line mismatch")
        payload_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        verification_key = (
            get_settings().maintenance_manifest_verification_keys().get(key_id)
        )
        if verification_key is None:
            raise ValueError("unknown key")
        expected = hmac.new(
            verification_key,
            _GLOBAL_COST_TOKEN_DOMAIN + payload_bytes,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("bad signature")
        raw_version = payload.get("override_version")
        version = None if raw_version is None else int(raw_version)
        if version is not None and version < 1:
            raise ValueError("bad version")
        raw_active = payload.get("override_active")
        if raw_active is not None and not isinstance(raw_active, bool):
            raise ValueError("bad active state")

        def _money(key: str) -> Decimal | None:
            raw = payload.get(key)
            return None if raw is None else Decimal(str(raw)).quantize(Decimal("0.01"))

        return {
            "override_version": version,
            "override_active": raw_active,
            "unit_cost_ex_tax": _money("unit_cost_ex_tax"),
            "unit_cost_inc_tax": _money("unit_cost_inc_tax"),
            "reason": str(payload["reason"]) if payload.get("reason") is not None else None,
        }
    except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError) as exc:
        raise WorkbookError(
            "invalid_concurrency_token",
            f"第 {row_no} 行并发校验信息无效，请重新下载全局备件表",
        ) from exc


def _v2_row_base_token(
    *, sheet: str, entity_id, values: dict[str, object]
) -> str:
    """行级签名基线（2.7.0）：该行可编辑字段的导出值快照 + HMAC。

    与全局表 ``_global_cost_base_token`` 同一模式（域分隔 + 规范 JSON），
    供上传侧三路合并区分「用户改的」与「文件里带出来的导出旧值」。
    """
    payload = {
        "sheet": sheet,
        "entity": str(entity_id),
        "base": {
            str(name): _v2_hash_value(values.get(name)) for name in sorted(values)
        },
    }
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    key_id, signing_key = get_settings().maintenance_manifest_signing_material()
    signature = hmac.new(
        signing_key, _V2_ROW_BASE_DOMAIN + payload_bytes, hashlib.sha256
    ).hexdigest()
    return json.dumps(
        {"key_id": key_id, "payload": payload, "signature": signature},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _parse_v2_row_base_token(
    value, *, sheet: str, entity_id, row_no: int
) -> dict[str, str]:
    try:
        envelope = json.loads(str(value))
        key_id = str(envelope["key_id"])
        payload = envelope["payload"]
        signature = str(envelope["signature"])
        if (
            not isinstance(payload, dict)
            or payload.get("sheet") != sheet
            or str(payload.get("entity")) != str(entity_id)
        ):
            raise ValueError("row mismatch")
        payload_bytes = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        verification_key = (
            get_settings().maintenance_manifest_verification_keys().get(key_id)
        )
        if verification_key is None:
            raise ValueError("unknown key")
        expected = hmac.new(
            verification_key, _V2_ROW_BASE_DOMAIN + payload_bytes, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("bad signature")
        base = payload.get("base")
        if not isinstance(base, dict):
            raise ValueError("bad base")
        return {str(k): ("" if v is None else str(v)) for k, v in base.items()}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkbookError(
            "invalid_concurrency_token",
            f"第 {row_no} 行基线令牌无效或与实体不匹配，请重新下载当前项目总表（2.7.0）",
        ) from exc


def _v2_sign_meta_rows(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    key_id, signing_key = get_settings().maintenance_manifest_signing_material()
    flat_rows: list[tuple[str, str]] = []
    for key, value in rows:
        flat_rows.extend(_chunk_meta_value(str(key), str(value)))
    signed_rows = [
        *flat_rows,
        ("metadata_hmac_algorithm", V2_META_SIGNATURE_ALGORITHM),
        ("metadata_hmac_key_id", key_id),
    ]
    # 签名覆盖「逻辑值（未分块）+ 算法/键ID」，与读取端合并后的全键一致。
    # 教训：3cf4f53 曾误签 dict(rows)（缺 algorithm/key_id 两键）导致全量必炸，
    # 由 test_v271 回归测试守住。
    metadata = dict(rows) | {
        "metadata_hmac_algorithm": V2_META_SIGNATURE_ALGORITHM,
        "metadata_hmac_key_id": key_id,
    }
    signed_rows.append(("metadata_hmac", _v2_metadata_signature(metadata, signing_key)))
    return signed_rows
