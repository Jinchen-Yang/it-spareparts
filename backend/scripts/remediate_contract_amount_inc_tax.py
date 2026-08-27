#!/usr/bin/env python3
"""Conservatively repair contract ``amount_inc_tax`` values after f7a3d2c8e6b1.

Why this is a manifest-driven tool
----------------------------------
The already-applied migration did not record a before-image or provenance for
each value it filled.  Current rows may also have been changed later by a
ledger import or a person.  It is therefore impossible to identify every f7a
write safely from the current value alone.

This command is dry-run by default.  Execution requires all of the following:

* ``--execute``;
* an active, named ``sys_user`` supplied through ``--operator``;
* ``--confirm-manifest-sha256`` copied from the dry-run output;
* an exact expected current amount and contract version for every row;
* live, exact ledger evidence or an active sales row with an explicit tax rate,
  otherwise an explicit transition to the incomplete (NULL) state.

No 13% fallback is used to choose a repaired value.  The source-export mode
does reproduce the old migration's 13% branch solely to prove its exact
after-image and affected set.  Every execute is one SERIALIZABLE transaction,
locks all target rows before the first update, performs a SQL compare-and-swap,
writes the canonical project audit, and appends an immutable remediation
receipt.  Successful output contains an inverse rollback manifest; rollback is
itself guarded, audited, and append-only.

Production runbook
------------------
First restore the pre-f7 database backup into an isolated database left at
``d5c1f8a3b7e2``.  Point this command at that restore and export the exact old
write set with ``--export-f7-source-snapshot --backup-file BACKUP``.  Extract
the returned ``source_snapshot`` object into an immutable JSON file.  Against
current production, run ``--build-manifest --source-snapshot SOURCE --reason
TICKET`` and extract its returned ``manifest`` object.  The builder requires
the complete source set and partitions it into preserved, authoritative
corrections, and values that must return to incomplete/NULL.

Upgrade production through ``a9c4e7b2d6f1``.  Run ``--manifest repair.json
--source-snapshot source.json`` and archive its JSON output.  A second reviewer
must compare the source backup SHA, affected-set SHA, partition counts, changes,
and manifest fingerprint.  Only then run the same command with ``--execute
--operator NAME --confirm-manifest-sha256 HASH`` and both JSON files.  A
rollback remains valid only while every row still has its reported post-apply
value and version.
Exit code 3 means the commit or receipt delivery outcome needs reconciliation:
run ``--reconcile-manifest-sha256 HASH`` against the primary database before
any retry; it returns the stored run and regenerates the exact rollback
manifest.  Never interpret an acknowledgement loss as a rollback.

Manifest examples
-----------------
Authoritative ledger amount::

    {
      "schema_version": 1,
      "mode": "apply",
      "reason": "ticket INC-123 verified against signed ledger",
      "rows": [{
        "project_contract_id": "...",
        "expected_version": 3,
        "expected_current_amount_inc_tax": "113.00",
        "evidence": {
          "kind": "ledger_amount_inc_tax",
          "batch_id": "...",
          "row_id": "...",
          "expected_amount_inc_tax": "106.00"
        }
      }]
    }

Explicit sales tax::

    "evidence": {
      "kind": "sales_explicit_tax",
      "raw_order_id": "...",
      "expected_amount_ex_tax": "100.00",
      "expected_tax_rate": "0.0600"
    }

No unambiguous authority (restores NULL, which existing readers mark
incomplete)::

    "evidence": {
      "kind": "no_authoritative_evidence",
      "note": "ledger missing; active sales evidence absent or conflicting"
    }
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import config


SCHEMA_VERSION = 1
F7_SOURCE_SCHEMA_VERSION = 1
F7_REVISION = "f7a3d2c8e6b1"
F7_PREDECESSOR_REVISION = "d5c1f8a3b7e2"
F7_ALGORITHM_SPEC = (
    "f7a3d2c8e6b1:v2:candidate-contract-no-from-null-inc-nonnull-ex;"
    "latest-sales-by-id-updates-all-null-inc-siblings-by-coalesce-ex;"
    "null-tax-zero;remaining-nonnull-ex-tax-0.13;round-half-up-cent"
)
F7_ALGORITHM_SHA256 = hashlib.sha256(
    F7_ALGORITHM_SPEC.encode("utf-8")
).hexdigest()
MAX_ROWS = 5_000
MAX_MONEY = Decimal("1000000000000")
CENT = Decimal("0.01")
ACTIVE_SALES_STATUS = config.ACTIVE_STATUS

_TOP_FIELDS = {
    "schema_version",
    "mode",
    "source_run_id",
    "reason",
    "rows",
    "preserved_rows",
    "f7_source",
    "partition",
}
_ROW_FIELDS = {
    "project_contract_id",
    "expected_version",
    "expected_current_amount_inc_tax",
    "evidence",
}
_APPLY_ROW_FIELDS = _ROW_FIELDS | {"f7_source_row_sha256"}
_F7_SOURCE_FIELDS = {
    "schema_version",
    "f7_revision",
    "predecessor_revision",
    "backup_sha256",
    "algorithm_sha256",
    "affected_count",
    "affected_set_sha256",
    "rows",
}
_F7_SOURCE_ROW_FIELDS = {
    "project_contract_id",
    "project_id",
    "contract_no",
    "pre_f7_version",
    "pre_f7_contract_amount",
    "pre_f7_amount_inc_tax",
    "f7_write_kind",
    "f7_tax_rate",
    "f7_sales_row_id",
    "f7_sales_amount_ex_tax",
    "f7_written_amount_inc_tax",
    "row_sha256",
}
_F7_SOURCE_MANIFEST_FIELDS = {
    "snapshot_sha256",
    "backup_sha256",
    "algorithm_sha256",
    "affected_count",
    "affected_set_sha256",
}
_PARTITION_FIELDS = {
    "affected_count",
    "preserved_count",
    "authoritative_corrected_count",
    "cleared_count",
    "changed_count",
    "preserved_set_sha256",
    "changed_set_sha256",
}
_EVIDENCE_FIELDS = {
    "ledger_amount_inc_tax": {
        "kind", "batch_id", "row_id", "expected_amount_inc_tax",
    },
    "sales_explicit_tax": {
        "kind", "raw_order_id", "expected_amount_ex_tax", "expected_tax_rate",
    },
    "no_authoritative_evidence": {"kind", "note"},
    "rollback_receipt": {"kind", "source_run_id", "source_entry_sha256"},
}


class RemediationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CommitOutcomeUnknown(RemediationError):
    """The commit acknowledgement was lost; database state must be queried."""

    def __init__(self, *, run_id: str, manifest_hash: str):
        super().__init__(
            "commit_outcome_unknown",
            "提交结果不确定；禁止盲目重试，必须按 manifest_sha256 查询执行账本",
        )
        self.run_id = run_id
        self.manifest_hash = manifest_hash


class _SafeArgumentError(Exception):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _SafeArgumentError(message)


@dataclass(frozen=True)
class PlannedChange:
    project_contract_id: str
    project_id: str
    contract_no: str
    expected_version: int
    before_amount_inc_tax: Decimal | None
    after_amount_inc_tax: Decimal | None
    target_state: str
    evidence_kind: str
    evidence_ref: str
    evidence_snapshot: dict[str, Any]

    @property
    def after_version(self) -> int:
        return self.expected_version + 1


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RemediationError("duplicate_json_key", f"JSON 字段重复：{key}")
        result[key] = value
    return result


def _required_text(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RemediationError("invalid_manifest", f"{label}必须是非空字符串")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise RemediationError("invalid_manifest", f"{label}超过 {maximum} 字符")
    return cleaned


def _money(value: Any, *, label: str, nullable: bool = False) -> Decimal | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise RemediationError(
            "invalid_manifest",
            f"{label}必须用字符串精确表示（例如 \"106.00\"），不能使用 JSON 浮点数",
        )
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, ValueError) as exc:
        raise RemediationError("invalid_manifest", f"{label}不是合法金额") from exc
    if not parsed.is_finite() or parsed < 0 or parsed >= MAX_MONEY:
        raise RemediationError("invalid_manifest", f"{label}超出合法范围")
    rounded = parsed.quantize(CENT, rounding=ROUND_HALF_UP)
    if parsed != rounded:
        raise RemediationError("invalid_manifest", f"{label}最多保留两位小数")
    return rounded


def _rate(value: Any, *, label: str) -> Decimal:
    if not isinstance(value, str):
        raise RemediationError(
            "invalid_manifest",
            f"{label}必须用字符串精确表示（例如 \"0.0600\"）",
        )
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, ValueError) as exc:
        raise RemediationError("invalid_manifest", f"{label}不是合法税率") from exc
    if not parsed.is_finite() or parsed < 0 or parsed >= 1:
        raise RemediationError("invalid_manifest", f"{label}必须在 [0, 1) 内")
    if parsed.as_tuple().exponent < -4:
        raise RemediationError("invalid_manifest", f"{label}最多保留四位小数")
    return parsed


def _money_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(CENT), ".2f")


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_text(value: Any, *, label: str) -> str:
    cleaned = _required_text(value, label=label, maximum=64).lower()
    if len(cleaned) != 64 or any(ch not in "0123456789abcdef" for ch in cleaned):
        raise RemediationError("invalid_manifest", f"{label}必须是 64 位小写 SHA-256")
    return cleaned


def _identity_set_sha256(rows: list[dict[str, Any]]) -> str:
    return _canonical_sha256([
        {
            "project_contract_id": row["project_contract_id"],
            "f7_source_row_sha256": row["f7_source_row_sha256"],
        }
        for row in sorted(rows, key=lambda item: item["project_contract_id"])
    ])


def _source_identity_set_sha256(rows: list[dict[str, Any]]) -> str:
    return _canonical_sha256([
        {
            "project_contract_id": row["project_contract_id"],
            "row_sha256": row["row_sha256"],
        }
        for row in sorted(rows, key=lambda item: item["project_contract_id"])
    ])


def _normalize_f7_source_row(raw: Any, *, row_no: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RemediationError(
            "invalid_source_snapshot", f"source snapshot 第 {row_no} 行必须是对象")
    unknown = set(raw) - _F7_SOURCE_ROW_FIELDS
    required = _F7_SOURCE_ROW_FIELDS - {"row_sha256"}
    missing = required - set(raw)
    if unknown or missing:
        raise RemediationError(
            "invalid_source_snapshot",
            f"source snapshot 第 {row_no} 行字段不匹配；"
            f"缺少={sorted(missing)}，未知={sorted(unknown)}",
        )
    contract_id = _required_text(
        raw["project_contract_id"],
        label=f"source snapshot 第 {row_no} 行 project_contract_id",
        maximum=36,
    )
    project_id = _required_text(
        raw["project_id"],
        label=f"source snapshot 第 {row_no} 行 project_id",
        maximum=36,
    )
    contract_no = _required_text(
        raw["contract_no"],
        label=f"source snapshot 第 {row_no} 行 contract_no",
        maximum=64,
    )
    version = raw["pre_f7_version"]
    if (isinstance(version, bool) or not isinstance(version, int) or version < 1):
        raise RemediationError(
            "invalid_source_snapshot",
            f"source snapshot 第 {row_no} 行 pre_f7_version 必须为正整数",
        )
    contract_amount = _money(
        raw["pre_f7_contract_amount"],
        label=f"source snapshot 第 {row_no} 行 pre_f7_contract_amount",
        nullable=True,
    )
    if raw["pre_f7_amount_inc_tax"] is not None:
        raise RemediationError(
            "invalid_source_snapshot",
            f"source snapshot 第 {row_no} 行不是 f7 可写入的 NULL 含税额",
        )
    write_kind = raw["f7_write_kind"]
    if write_kind not in {"latest_sales_row", "default_13_percent"}:
        raise RemediationError(
            "invalid_source_snapshot",
            f"source snapshot 第 {row_no} 行 f7_write_kind 无效",
        )
    rate = _rate(
        raw["f7_tax_rate"],
        label=f"source snapshot 第 {row_no} 行 f7_tax_rate",
    )
    sales_row_id = raw["f7_sales_row_id"]
    sales_amount = _money(
        raw["f7_sales_amount_ex_tax"],
        label=f"source snapshot 第 {row_no} 行 f7_sales_amount_ex_tax",
        nullable=True,
    )
    if write_kind == "latest_sales_row":
        if (isinstance(sales_row_id, bool)
                or not isinstance(sales_row_id, int) or sales_row_id < 1):
            raise RemediationError(
                "invalid_source_snapshot",
                f"source snapshot 第 {row_no} 行缺少 latest sales row id",
            )
        amount_base = contract_amount if contract_amount is not None else sales_amount
        if amount_base is None:
            raise RemediationError(
                "invalid_source_snapshot",
                f"source snapshot 第 {row_no} 行无法复算旧 f7 coalesce 金额",
            )
    elif (sales_row_id is not None or sales_amount is not None
            or rate != Decimal("0.13") or contract_amount is None):
        raise RemediationError(
            "invalid_source_snapshot",
            f"source snapshot 第 {row_no} 行 13% fallback 证据不一致",
        )
    else:
        amount_base = contract_amount
    written = _money(
        raw["f7_written_amount_inc_tax"],
        label=f"source snapshot 第 {row_no} 行 f7_written_amount_inc_tax",
    )
    expected_written = (amount_base * (Decimal("1") + rate)).quantize(
        CENT, rounding=ROUND_HALF_UP)
    if written != expected_written:
        raise RemediationError(
            "invalid_source_snapshot",
            f"source snapshot 第 {row_no} 行 f7 写入结果不可复算",
        )
    normalized = {
        "project_contract_id": contract_id,
        "project_id": project_id,
        "contract_no": contract_no,
        "pre_f7_version": version,
        "pre_f7_contract_amount": _money_text(contract_amount),
        "pre_f7_amount_inc_tax": None,
        "f7_write_kind": write_kind,
        "f7_tax_rate": format(rate, "f"),
        "f7_sales_row_id": sales_row_id,
        "f7_sales_amount_ex_tax": _money_text(sales_amount),
        "f7_written_amount_inc_tax": _money_text(written),
    }
    row_hash = _canonical_sha256(normalized)
    if "row_sha256" in raw:
        supplied = _sha256_text(
            raw["row_sha256"],
            label=f"source snapshot 第 {row_no} 行 row_sha256",
        )
        if supplied != row_hash:
            raise RemediationError(
                "source_row_hash_mismatch",
                f"source snapshot 第 {row_no} 行内容与 row_sha256 不一致",
            )
    normalized["row_sha256"] = row_hash
    return normalized


def make_f7_source_snapshot(
    *,
    rows: list[dict[str, Any]],
    backup_sha256: str,
) -> dict[str, Any]:
    """Create the canonical, hash-bound pre-f7 source artifact."""

    normalized_rows = [
        _normalize_f7_source_row(row, row_no=index)
        for index, row in enumerate(rows, start=1)
    ]
    normalized_rows.sort(key=lambda item: item["project_contract_id"])
    if not normalized_rows or len(normalized_rows) > MAX_ROWS:
        raise RemediationError(
            "invalid_source_snapshot",
            f"source snapshot rows 数量必须在 1 到 {MAX_ROWS} 之间",
        )
    identities = [row["project_contract_id"] for row in normalized_rows]
    if len(identities) != len(set(identities)):
        raise RemediationError("invalid_source_snapshot", "source snapshot 合同关系重复")
    return {
        "schema_version": F7_SOURCE_SCHEMA_VERSION,
        "f7_revision": F7_REVISION,
        "predecessor_revision": F7_PREDECESSOR_REVISION,
        "backup_sha256": _sha256_text(
            backup_sha256, label="source snapshot backup_sha256"),
        "algorithm_sha256": F7_ALGORITHM_SHA256,
        "affected_count": len(normalized_rows),
        "affected_set_sha256": _source_identity_set_sha256(normalized_rows),
        "rows": normalized_rows,
    }


def normalize_f7_source_snapshot(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RemediationError("invalid_source_snapshot", "source snapshot 顶层必须是对象")
    unknown = set(raw) - _F7_SOURCE_FIELDS
    missing = _F7_SOURCE_FIELDS - set(raw)
    if unknown or missing:
        raise RemediationError(
            "invalid_source_snapshot",
            f"source snapshot 顶层字段不匹配；缺少={sorted(missing)}，"
            f"未知={sorted(unknown)}",
        )
    if raw["schema_version"] != F7_SOURCE_SCHEMA_VERSION:
        raise RemediationError("invalid_source_snapshot", "source snapshot schema_version 无效")
    if (raw["f7_revision"] != F7_REVISION
            or raw["predecessor_revision"] != F7_PREDECESSOR_REVISION
            or raw["algorithm_sha256"] != F7_ALGORITHM_SHA256):
        raise RemediationError(
            "invalid_source_snapshot", "source snapshot 迁移版本或算法指纹不匹配")
    canonical = make_f7_source_snapshot(
        rows=raw["rows"],
        backup_sha256=raw["backup_sha256"],
    )
    if (raw["affected_count"] != canonical["affected_count"]
            or _sha256_text(
                raw["affected_set_sha256"],
                label="source snapshot affected_set_sha256",
            ) != canonical["affected_set_sha256"]):
        raise RemediationError(
            "source_partition_mismatch",
            "source snapshot 声明的受影响行数或集合指纹与实际 rows 不一致",
        )
    return canonical


def load_f7_source_snapshot(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_duplicate_safe_object,
        )
    except RemediationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RemediationError(
            "invalid_source_snapshot", "无法安全读取 source snapshot JSON") from exc
    return normalize_f7_source_snapshot(raw)


def f7_source_snapshot_sha256(snapshot: dict[str, Any]) -> str:
    return _canonical_sha256(normalize_f7_source_snapshot(snapshot))


def _normalized_evidence(raw: Any, *, mode: str, row_no: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RemediationError("invalid_manifest", f"第 {row_no} 行 evidence 必须是对象")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in _EVIDENCE_FIELDS:
        raise RemediationError("invalid_manifest", f"第 {row_no} 行 evidence.kind 无效")
    unknown = set(raw) - _EVIDENCE_FIELDS[kind]
    missing = _EVIDENCE_FIELDS[kind] - set(raw)
    if unknown or missing:
        raise RemediationError(
            "invalid_manifest",
            f"第 {row_no} 行 evidence 字段不匹配；缺少={sorted(missing)}，未知={sorted(unknown)}",
        )
    if mode == "rollback" and kind != "rollback_receipt":
        raise RemediationError("invalid_manifest", "rollback manifest 只接受 rollback_receipt")
    if mode == "apply" and kind == "rollback_receipt":
        raise RemediationError("invalid_manifest", "apply manifest 不能使用 rollback_receipt")

    if kind == "ledger_amount_inc_tax":
        return {
            "kind": kind,
            "batch_id": _required_text(raw["batch_id"], label="batch_id", maximum=36),
            "row_id": _required_text(raw["row_id"], label="row_id", maximum=36),
            "expected_amount_inc_tax": _money_text(_money(
                raw["expected_amount_inc_tax"],
                label="expected_amount_inc_tax",
            )),
        }
    if kind == "sales_explicit_tax":
        return {
            "kind": kind,
            "raw_order_id": _required_text(
                raw["raw_order_id"], label="raw_order_id", maximum=64),
            "expected_amount_ex_tax": _money_text(_money(
                raw["expected_amount_ex_tax"], label="expected_amount_ex_tax")),
            "expected_tax_rate": format(
                _rate(raw["expected_tax_rate"], label="expected_tax_rate"), "f"),
        }
    if kind == "no_authoritative_evidence":
        return {
            "kind": kind,
            "note": _required_text(raw["note"], label="note", maximum=1000),
        }
    return {
        "kind": kind,
        "source_run_id": _required_text(
            raw["source_run_id"], label="source_run_id", maximum=36),
        "source_entry_sha256": _required_text(
            raw["source_entry_sha256"],
            label="source_entry_sha256",
            maximum=64,
        ).lower(),
    }


def _normalize_manifest_rows(
    raw_rows: Any,
    *,
    mode: str,
    label: str,
    identities: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(raw_rows, list):
        raise RemediationError("invalid_manifest", f"{label} 必须是数组")
    expected_fields = _APPLY_ROW_FIELDS if mode == "apply" else _ROW_FIELDS
    normalized_rows: list[dict[str, Any]] = []
    for row_no, row in enumerate(raw_rows, start=1):
        if not isinstance(row, dict):
            raise RemediationError(
                "invalid_manifest", f"{label} 第 {row_no} 行必须是对象")
        unknown_row = set(row) - expected_fields
        missing_row = expected_fields - set(row)
        if unknown_row or missing_row:
            raise RemediationError(
                "invalid_manifest",
                f"{label} 第 {row_no} 行字段不匹配；"
                f"缺少={sorted(missing_row)}，未知={sorted(unknown_row)}",
            )
        contract_id = _required_text(
            row["project_contract_id"],
            label=f"{label} 第 {row_no} 行 project_contract_id",
            maximum=36,
        )
        if contract_id in identities:
            raise RemediationError("duplicate_contract", f"合同关系重复：{contract_id}")
        identities.add(contract_id)
        expected_version = row["expected_version"]
        if (isinstance(expected_version, bool)
                or not isinstance(expected_version, int)
                or expected_version < 1):
            raise RemediationError(
                "invalid_manifest",
                f"{label} 第 {row_no} 行 expected_version 必须为正整数",
            )
        expected_amount = _money(
            row["expected_current_amount_inc_tax"],
            label=f"{label} 第 {row_no} 行 expected_current_amount_inc_tax",
            nullable=True,
        )
        normalized_row = {
            "project_contract_id": contract_id,
            "expected_version": expected_version,
            "expected_current_amount_inc_tax": _money_text(expected_amount),
            "evidence": _normalized_evidence(
                row["evidence"], mode=mode, row_no=row_no),
        }
        if mode == "apply":
            normalized_row["f7_source_row_sha256"] = _sha256_text(
                row["f7_source_row_sha256"],
                label=f"{label} 第 {row_no} 行 f7_source_row_sha256",
            )
        normalized_rows.append(normalized_row)
    normalized_rows.sort(key=lambda item: item["project_contract_id"])
    return normalized_rows


def _normalize_f7_manifest_source(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RemediationError("invalid_manifest", "f7_source 必须是对象")
    unknown = set(raw) - _F7_SOURCE_MANIFEST_FIELDS
    missing = _F7_SOURCE_MANIFEST_FIELDS - set(raw)
    if unknown or missing:
        raise RemediationError(
            "invalid_manifest",
            f"f7_source 字段不匹配；缺少={sorted(missing)}，未知={sorted(unknown)}",
        )
    affected_count = raw["affected_count"]
    if (isinstance(affected_count, bool) or not isinstance(affected_count, int)
            or not 1 <= affected_count <= MAX_ROWS):
        raise RemediationError("invalid_manifest", "f7_source.affected_count 无效")
    algorithm_hash = _sha256_text(
        raw["algorithm_sha256"], label="f7_source.algorithm_sha256")
    if algorithm_hash != F7_ALGORITHM_SHA256:
        raise RemediationError("invalid_manifest", "f7_source 算法指纹不匹配")
    return {
        "snapshot_sha256": _sha256_text(
            raw["snapshot_sha256"], label="f7_source.snapshot_sha256"),
        "backup_sha256": _sha256_text(
            raw["backup_sha256"], label="f7_source.backup_sha256"),
        "algorithm_sha256": algorithm_hash,
        "affected_count": affected_count,
        "affected_set_sha256": _sha256_text(
            raw["affected_set_sha256"], label="f7_source.affected_set_sha256"),
    }


def _normalize_partition(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RemediationError("invalid_manifest", "partition 必须是对象")
    unknown = set(raw) - _PARTITION_FIELDS
    missing = _PARTITION_FIELDS - set(raw)
    if unknown or missing:
        raise RemediationError(
            "invalid_manifest",
            f"partition 字段不匹配；缺少={sorted(missing)}，未知={sorted(unknown)}",
        )
    counts: dict[str, int] = {}
    for key in (
        "affected_count",
        "preserved_count",
        "authoritative_corrected_count",
        "cleared_count",
        "changed_count",
    ):
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RemediationError("invalid_manifest", f"partition.{key} 无效")
        counts[key] = value
    return {
        **counts,
        "preserved_set_sha256": _sha256_text(
            raw["preserved_set_sha256"], label="partition.preserved_set_sha256"),
        "changed_set_sha256": _sha256_text(
            raw["changed_set_sha256"], label="partition.changed_set_sha256"),
    }


def normalize_manifest(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RemediationError("invalid_manifest", "manifest 顶层必须是对象")
    unknown = set(raw) - _TOP_FIELDS
    if unknown:
        raise RemediationError("invalid_manifest", f"manifest 含未知字段：{sorted(unknown)}")
    schema_version = raw.get("schema_version")
    if (isinstance(schema_version, bool)
            or schema_version != SCHEMA_VERSION):
        raise RemediationError("invalid_manifest", "schema_version 必须为 1")
    mode = raw.get("mode")
    if mode not in {"apply", "rollback"}:
        raise RemediationError("invalid_manifest", "mode 必须为 apply 或 rollback")
    source_run_id = raw.get("source_run_id")
    if mode == "rollback":
        source_run_id = _required_text(
            source_run_id, label="source_run_id", maximum=36)
        forbidden = {"preserved_rows", "f7_source", "partition"} & set(raw)
        if forbidden:
            raise RemediationError(
                "invalid_manifest", f"rollback manifest 不能设置：{sorted(forbidden)}")
    elif source_run_id is not None:
        raise RemediationError("invalid_manifest", "apply manifest 不能设置 source_run_id")
    reason = _required_text(raw.get("reason"), label="reason", maximum=1000)
    rows = raw.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise RemediationError(
            "invalid_manifest", f"rows 数量必须在 1 到 {MAX_ROWS} 之间")

    identities: set[str] = set()
    normalized_rows = _normalize_manifest_rows(
        rows,
        mode=mode,
        label="rows",
        identities=identities,
    )
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "reason": reason,
        "rows": normalized_rows,
    }
    if mode == "apply":
        if not {"preserved_rows", "f7_source", "partition"}.issubset(raw):
            raise RemediationError(
                "missing_f7_provenance",
                "apply manifest 必须包含 source snapshot 与完整 preserved/changed 分区",
            )
        normalized_preserved = _normalize_manifest_rows(
            raw["preserved_rows"],
            mode=mode,
            label="preserved_rows",
            identities=identities,
        )
        if any(row["evidence"]["kind"] not in {
            "ledger_amount_inc_tax", "sales_explicit_tax",
        } for row in normalized_preserved):
            raise RemediationError(
                "invalid_manifest",
                "preserved_rows 必须逐行绑定明确台账或销售税率证据",
            )
        normalized["preserved_rows"] = normalized_preserved
        normalized["f7_source"] = _normalize_f7_manifest_source(raw["f7_source"])
        normalized["partition"] = _normalize_partition(raw["partition"])
    if source_run_id is not None:
        normalized["source_run_id"] = source_run_id
    return normalized


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_duplicate_safe_object,
        )
    except RemediationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RemediationError("invalid_manifest", "无法安全读取 manifest JSON") from exc
    return normalize_manifest(raw)


def manifest_sha256(manifest: dict[str, Any]) -> str:
    return _canonical_sha256(manifest)


def _lock_clause(lock: bool) -> str:
    return " FOR SHARE" if lock else ""


def _latest_ledger_evidence(
    db: Session,
    *,
    contract_no: str,
    lock: bool,
) -> dict[str, Any]:
    batch = db.execute(text(
        "SELECT b.batch_id, b.file_hash, b.applied_at "
        "FROM maintenance_ledger_import_batch b "
        "JOIN maintenance_ledger_contract_row r ON r.batch_id = b.batch_id "
        "WHERE b.status = 'applied' AND r.order_no = :contract_no "
        "ORDER BY b.applied_at DESC, b.uploaded_at DESC, b.batch_id DESC "
        "LIMIT 1" + _lock_clause(lock)
    ), {"contract_no": contract_no}).mappings().one_or_none()
    if batch is None:
        return {
            "batch_id": None,
            "file_hash": None,
            "values": [],
            "rows": [],
            "unresolved_row_ids": [],
        }
    rows = list(db.execute(text(
        "SELECT r.row_id, r.amount_inc_tax "
        "FROM maintenance_ledger_contract_row r "
        "WHERE r.batch_id = :batch_id AND r.order_no = :contract_no "
        "ORDER BY r.row_id" + _lock_clause(lock)
    ), {
        "batch_id": batch["batch_id"],
        "contract_no": contract_no,
    }).mappings())
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        amount = None
        if row["amount_inc_tax"] is not None:
            raw_amount = Decimal(row["amount_inc_tax"])
            if (raw_amount.is_finite() and raw_amount >= 0
                    and raw_amount < MAX_MONEY):
                amount = raw_amount.quantize(CENT)
        normalized_rows.append({
            "row_id": str(row["row_id"]),
            "amount_inc_tax": amount,
        })
    values = sorted({
        row["amount_inc_tax"]
        for row in normalized_rows if row["amount_inc_tax"] is not None
    })
    return {
        "batch_id": str(batch["batch_id"]),
        "file_hash": str(batch["file_hash"]),
        "values": values,
        "rows": normalized_rows,
        "unresolved_row_ids": [
            row["row_id"] for row in normalized_rows
            if row["amount_inc_tax"] is None
        ],
    }


def _active_sales_evidence(
    db: Session,
    *,
    contract_no: str,
    lock: bool,
) -> dict[str, Any]:
    rows = list(db.execute(text(
        "SELECT o.raw_order_id, o.amount_ex_tax, o.tax_rate, "
        "o.import_batch_id, b.file_hash, b.status AS import_status "
        "FROM f_sales_order o "
        "JOIN sys_import_batch b ON b.id = o.import_batch_id "
        "WHERE o.order_no = :contract_no AND o.data_status = :active_status "
        "ORDER BY o.id" + _lock_clause(lock)
    ), {
        "contract_no": contract_no,
        "active_status": ACTIVE_SALES_STATUS,
    }).mappings())
    candidates: list[dict[str, Any]] = []
    unresolved_rows: list[str] = []
    for row in rows:
        if (row["import_status"] != "success"
                or row["amount_ex_tax"] is None or row["tax_rate"] is None):
            unresolved_rows.append(str(row["raw_order_id"]))
            continue
        amount_ex = Decimal(row["amount_ex_tax"])
        rate = Decimal(row["tax_rate"])
        if (not amount_ex.is_finite() or amount_ex < 0 or amount_ex >= MAX_MONEY
                or not rate.is_finite() or rate < 0 or rate >= 1):
            unresolved_rows.append(str(row["raw_order_id"]))
            continue
        target = (amount_ex * (Decimal("1") + rate)).quantize(
            CENT, rounding=ROUND_HALF_UP)
        if target >= MAX_MONEY:
            unresolved_rows.append(str(row["raw_order_id"]))
            continue
        candidates.append({
            "raw_order_id": str(row["raw_order_id"]),
            "amount_ex_tax": amount_ex.quantize(CENT),
            "tax_rate": rate,
            "amount_inc_tax": target,
            "import_batch_id": int(row["import_batch_id"]),
            "file_hash": str(row["file_hash"]),
        })
    values = sorted({item["amount_inc_tax"] for item in candidates})
    return {
        "values": values,
        "amount_ex_tax_values": sorted({
            item["amount_ex_tax"] for item in candidates
        }),
        "tax_rate_values": sorted({item["tax_rate"] for item in candidates}),
        "rows": candidates,
        "unresolved_rows": unresolved_rows,
    }


def _resolve_ledger(
    db: Session,
    *,
    contract_no: str,
    evidence: dict[str, Any],
    lock: bool,
) -> tuple[Decimal, str, dict[str, Any]]:
    latest = _latest_ledger_evidence(db, contract_no=contract_no, lock=lock)
    if latest["batch_id"] != evidence["batch_id"]:
        raise RemediationError(
            "stale_ledger_evidence",
            f"合同 {contract_no} 的 manifest 不是最新已应用台账批次",
        )
    selected = next(
        (row for row in latest["rows"] if row["row_id"] == evidence["row_id"]),
        None,
    )
    if selected is None:
        raise RemediationError(
            "stale_ledger_evidence",
            f"合同 {contract_no} 的台账证据行不属于最新已应用批次",
        )
    expected = Decimal(evidence["expected_amount_inc_tax"])
    if (selected["amount_inc_tax"] != expected
            or latest["unresolved_row_ids"]
            or latest["values"] != [expected]):
        raise RemediationError(
            "ambiguous_ledger_evidence",
            f"合同 {contract_no} 的最新台账行金额为空、变化或互相冲突",
        )
    snapshot = {
        "kind": "ledger_amount_inc_tax",
        "batch_id": latest["batch_id"],
        "row_id": evidence["row_id"],
        "file_hash": latest["file_hash"],
        "amount_inc_tax": _money_text(expected),
    }
    return expected, f"ledger:{latest['batch_id']}:{evidence['row_id']}", snapshot


def _resolve_sales(
    db: Session,
    *,
    contract_no: str,
    contract_amount: Decimal | None,
    evidence: dict[str, Any],
    lock: bool,
) -> tuple[Decimal, str, dict[str, Any]]:
    latest_ledger = _latest_ledger_evidence(db, contract_no=contract_no, lock=lock)
    if latest_ledger["values"]:
        raise RemediationError(
            "ledger_evidence_exists",
            f"合同 {contract_no} 已有最新已应用台账金额，不能降级使用销售推导值",
        )
    sales = _active_sales_evidence(db, contract_no=contract_no, lock=lock)
    selected = next(
        (row for row in sales["rows"]
         if row["raw_order_id"] == evidence["raw_order_id"]),
        None,
    )
    if selected is None:
        raise RemediationError(
            "stale_sales_evidence",
            f"合同 {contract_no} 的销售证据不存在、未生效或税率不明确",
        )
    expected_ex = Decimal(evidence["expected_amount_ex_tax"])
    expected_rate = Decimal(evidence["expected_tax_rate"])
    if contract_amount != expected_ex:
        raise RemediationError(
            "contract_sales_amount_conflict",
            f"合同 {contract_no} 的 canonical 未税额与销售证据不一致",
        )
    if (selected["amount_ex_tax"] != expected_ex
            or selected["tax_rate"] != expected_rate):
        raise RemediationError(
            "stale_sales_evidence",
            f"合同 {contract_no} 的销售未税额或明确税率已变化",
        )
    target = (expected_ex * (Decimal("1") + expected_rate)).quantize(
        CENT, rounding=ROUND_HALF_UP)
    if (sales["unresolved_rows"]
            or sales["amount_ex_tax_values"] != [expected_ex]
            or sales["tax_rate_values"] != [expected_rate]
            or sales["values"] != [target]):
        raise RemediationError(
            "ambiguous_sales_evidence",
            f"合同 {contract_no} 存在缺失或互相冲突的生效销售金额/税率事实",
        )
    snapshot = {
        "kind": "sales_explicit_tax",
        "raw_order_id": evidence["raw_order_id"],
        "amount_ex_tax": _money_text(expected_ex),
        "tax_rate": format(expected_rate, "f"),
        "amount_inc_tax": _money_text(target),
        "import_batch_id": selected["import_batch_id"],
        "file_hash": selected["file_hash"],
        "active_resolved_row_count": len(sales["rows"]),
        "active_unresolved_row_count": len(sales["unresolved_rows"]),
    }
    return target, f"sales:{evidence['raw_order_id']}", snapshot


def _resolve_incomplete(
    db: Session,
    *,
    contract_no: str,
    contract_amount: Decimal | None,
    evidence: dict[str, Any],
    lock: bool,
) -> tuple[None, str, dict[str, Any]]:
    ledger = _latest_ledger_evidence(db, contract_no=contract_no, lock=lock)
    sales = _active_sales_evidence(db, contract_no=contract_no, lock=lock)
    # A single unambiguous latest-ledger value is authoritative.  Only when no
    # ledger amount exists do we accept one unambiguous active sales derivation.
    if len(ledger["values"]) == 1 and not ledger["unresolved_row_ids"]:
        raise RemediationError(
            "authoritative_evidence_exists",
            f"合同 {contract_no} 有唯一最新台账含税额，不能标为 incomplete",
        )
    if (not ledger["values"] and len(sales["values"]) == 1
            and not sales["unresolved_rows"]
            and contract_amount is not None
            and sales["amount_ex_tax_values"] == [contract_amount]
            and len(sales["tax_rate_values"]) == 1):
        raise RemediationError(
            "authoritative_evidence_exists",
            f"合同 {contract_no} 有唯一明确税率销售证据，不能标为 incomplete",
        )
    source_summary = {
        "latest_ledger_batch_id": ledger["batch_id"],
        "ledger_distinct_amounts": [_money_text(v) for v in ledger["values"]],
        "ledger_unresolved_rows": ledger["unresolved_row_ids"],
        "sales_distinct_amounts": [_money_text(v) for v in sales["values"]],
        "canonical_contract_amount_ex_tax": _money_text(contract_amount),
        "sales_distinct_amounts_ex_tax": [
            _money_text(v) for v in sales["amount_ex_tax_values"]
        ],
        "sales_distinct_tax_rates": [
            format(v, "f") for v in sales["tax_rate_values"]
        ],
        "sales_evidence_rows": [row["raw_order_id"] for row in sales["rows"]],
        "sales_unresolved_rows": sales["unresolved_rows"],
    }
    ref_hash = _canonical_sha256({
        "contract_no": contract_no,
        "note": evidence["note"],
        "sources": source_summary,
    })[:20]
    snapshot = {
        "kind": "no_authoritative_evidence",
        "note": evidence["note"],
        **source_summary,
        "completeness_state": "incomplete",
    }
    return None, f"incomplete:{ref_hash}", snapshot


def _f7_manifest_source(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_sha256": f7_source_snapshot_sha256(snapshot),
        "backup_sha256": snapshot["backup_sha256"],
        "algorithm_sha256": snapshot["algorithm_sha256"],
        "affected_count": snapshot["affected_count"],
        "affected_set_sha256": snapshot["affected_set_sha256"],
    }


def _partition_payload(
    *,
    changed_rows: list[dict[str, Any]],
    preserved_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    corrected = sum(
        row["evidence"]["kind"] in {
            "ledger_amount_inc_tax", "sales_explicit_tax",
        }
        for row in changed_rows
    )
    cleared = sum(
        row["evidence"]["kind"] == "no_authoritative_evidence"
        for row in changed_rows
    )
    return {
        "affected_count": len(changed_rows) + len(preserved_rows),
        "preserved_count": len(preserved_rows),
        "authoritative_corrected_count": corrected,
        "cleared_count": cleared,
        "changed_count": len(changed_rows),
        "preserved_set_sha256": _identity_set_sha256(preserved_rows),
        "changed_set_sha256": _identity_set_sha256(changed_rows),
    }


def bind_apply_manifest_to_f7_source(
    manifest: dict[str, Any],
    *,
    source_snapshot: dict[str, Any],
    preserved_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bind every apply disposition to one immutable pre-f7 source row."""

    snapshot = normalize_f7_source_snapshot(source_snapshot)
    source_by_id = {
        row["project_contract_id"]: row for row in snapshot["rows"]
    }
    raw_changed = manifest.get("rows")
    if not isinstance(raw_changed, list):
        raise RemediationError("invalid_manifest", "rows 必须是数组")
    raw_preserved = preserved_rows if preserved_rows is not None else []

    def attach(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        attached: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise RemediationError("invalid_manifest", "manifest 行必须是对象")
            contract_id = row.get("project_contract_id")
            source = source_by_id.get(contract_id)
            if source is None:
                raise RemediationError(
                    "row_not_in_f7_source",
                    f"合同 {contract_id} 不在 pre-f7 精确写入集中",
                )
            attached.append({
                **row,
                "f7_source_row_sha256": source["row_sha256"],
            })
        return attached

    changed = attach(raw_changed)
    preserved = attach(raw_preserved)
    bound = {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply",
        "reason": manifest.get("reason"),
        "rows": changed,
        "preserved_rows": preserved,
        "f7_source": _f7_manifest_source(snapshot),
        "partition": _partition_payload(
            changed_rows=changed,
            preserved_rows=preserved,
        ),
    }
    normalized = normalize_manifest(bound)
    _validate_f7_binding(normalized, source_snapshot=snapshot)
    return normalized


def _validate_f7_binding(
    manifest: dict[str, Any],
    *,
    source_snapshot: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if manifest["mode"] != "apply":
        return {}
    if source_snapshot is None:
        raise RemediationError(
            "source_snapshot_required",
            "apply dry-run/execute 必须同时提供生成 manifest 的 pre-f7 source snapshot",
        )
    snapshot = normalize_f7_source_snapshot(source_snapshot)
    if manifest["f7_source"] != _f7_manifest_source(snapshot):
        raise RemediationError(
            "source_snapshot_mismatch",
            "manifest 绑定的备份/source snapshot/受影响集合指纹不匹配",
        )
    source_by_id = {
        row["project_contract_id"]: row for row in snapshot["rows"]
    }
    combined = manifest["rows"] + manifest["preserved_rows"]
    if {row["project_contract_id"] for row in combined} != set(source_by_id):
        raise RemediationError(
            "source_partition_mismatch",
            "manifest preserved + changed 未完整且仅覆盖 pre-f7 精确写入集",
        )
    for row in combined:
        source = source_by_id[row["project_contract_id"]]
        if row["f7_source_row_sha256"] != source["row_sha256"]:
            raise RemediationError(
                "source_row_hash_mismatch",
                f"合同 {row['project_contract_id']} 的 pre-f7 行证据已被替换",
            )
    for row in manifest["rows"]:
        source = source_by_id[row["project_contract_id"]]
        # f7 itself did not bump the optimistic-lock version.  A changed value
        # or version means somebody touched the row after f7; never clear it
        # using a manifest generated from an older observation.
        if (row["expected_version"] != source["pre_f7_version"]
                or row["expected_current_amount_inc_tax"]
                != source["f7_written_amount_inc_tax"]):
            raise RemediationError(
                "post_f7_manual_change",
                f"合同 {row['project_contract_id']} 已偏离 f7 精确写入后像，禁止自动修复",
            )
    expected_partition = _partition_payload(
        changed_rows=manifest["rows"],
        preserved_rows=manifest["preserved_rows"],
    )
    if (manifest["partition"] != expected_partition
            or expected_partition["affected_count"] != snapshot["affected_count"]):
        raise RemediationError(
            "source_partition_mismatch",
            "manifest preserved/corrected/cleared 分区计数或集合指纹不一致",
        )
    return source_by_id


def _authoritative_manifest_evidence(
    db: Session,
    *,
    contract_no: str,
    contract_amount: Decimal | None,
) -> tuple[dict[str, Any], Decimal] | None:
    ledger = _latest_ledger_evidence(db, contract_no=contract_no, lock=False)
    if len(ledger["values"]) == 1 and not ledger["unresolved_row_ids"]:
        target = ledger["values"][0]
        selected = next(
            row for row in ledger["rows"] if row["amount_inc_tax"] == target)
        evidence = {
            "kind": "ledger_amount_inc_tax",
            "batch_id": ledger["batch_id"],
            "row_id": selected["row_id"],
            "expected_amount_inc_tax": _money_text(target),
        }
        _resolve_ledger(
            db,
            contract_no=contract_no,
            evidence=evidence,
            lock=False,
        )
        return evidence, target
    if ledger["values"]:
        return None
    sales = _active_sales_evidence(db, contract_no=contract_no, lock=False)
    if (not sales["unresolved_rows"] and len(sales["values"]) == 1
            and contract_amount is not None
            and sales["amount_ex_tax_values"] == [contract_amount]
            and len(sales["tax_rate_values"]) == 1):
        target = sales["values"][0]
        selected = next(
            row for row in sales["rows"] if row["amount_inc_tax"] == target)
        evidence = {
            "kind": "sales_explicit_tax",
            "raw_order_id": selected["raw_order_id"],
            "expected_amount_ex_tax": _money_text(contract_amount),
            "expected_tax_rate": format(selected["tax_rate"], "f"),
        }
        _resolve_sales(
            db,
            contract_no=contract_no,
            contract_amount=contract_amount,
            evidence=evidence,
            lock=False,
        )
        return evidence, target
    return None


def export_f7_source_snapshot(
    db: Session,
    *,
    backup_sha256: str,
) -> dict[str, Any]:
    """Reproduce the exact old f7 UPDATE set on a restored pre-f7 database."""

    revision = db.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != F7_PREDECESSOR_REVISION:
        raise RemediationError(
            "wrong_source_revision",
            f"source DB 必须停在 {F7_PREDECESSOR_REVISION}，当前为 {revision}",
        )
    rows = list(db.execute(text(
        "WITH candidate_contract_no AS ("
        "  SELECT DISTINCT contract_no FROM maintenance_project_contract "
        "  WHERE amount_inc_tax IS NULL AND contract_amount IS NOT NULL"
        "), latest_sales AS ("
        "  SELECT DISTINCT ON (o.order_no) o.order_no, o.id, "
        "         o.amount_ex_tax, o.tax_rate "
        "  FROM f_sales_order o "
        "  JOIN candidate_contract_no n ON n.contract_no = o.order_no "
        "  ORDER BY o.order_no, o.id DESC"
        ") "
        "SELECT c.project_contract_id, c.project_id, c.contract_no, "
        "c.version, c.contract_amount, s.id AS sales_row_id, "
        "s.amount_ex_tax AS sales_amount_ex_tax, s.tax_rate "
        "FROM maintenance_project_contract c "
        "LEFT JOIN latest_sales s ON s.order_no = c.contract_no "
        "WHERE c.amount_inc_tax IS NULL AND ("
        "  (s.id IS NOT NULL AND coalesce(c.contract_amount, s.amount_ex_tax) "
        "   IS NOT NULL) OR "
        "  (s.id IS NULL AND c.contract_amount IS NOT NULL)"
        ") ORDER BY c.project_contract_id"
    )).mappings())
    source_rows: list[dict[str, Any]] = []
    for row in rows:
        if row["sales_row_id"] is None:
            write_kind = "default_13_percent"
            rate = Decimal("0.13")
        else:
            write_kind = "latest_sales_row"
            rate = Decimal(row["tax_rate"] or 0)
        contract_amount = (
            Decimal(row["contract_amount"]).quantize(CENT)
            if row["contract_amount"] is not None else None
        )
        sales_amount = (
            Decimal(row["sales_amount_ex_tax"]).quantize(CENT)
            if row["sales_amount_ex_tax"] is not None else None
        )
        amount_base = (
            contract_amount if contract_amount is not None else sales_amount)
        if amount_base is None:  # SQL predicate above is the frozen safety proof.
            raise RemediationError(
                "invalid_source_snapshot", "旧 f7 命中行缺少可复算的金额基数")
        source_rows.append({
            "project_contract_id": str(row["project_contract_id"]),
            "project_id": str(row["project_id"]),
            "contract_no": str(row["contract_no"]),
            "pre_f7_version": int(row["version"]),
            "pre_f7_contract_amount": _money_text(contract_amount),
            "pre_f7_amount_inc_tax": None,
            "f7_write_kind": write_kind,
            "f7_tax_rate": format(rate, "f"),
            "f7_sales_row_id": (
                int(row["sales_row_id"]) if row["sales_row_id"] is not None else None
            ),
            "f7_sales_amount_ex_tax": _money_text(sales_amount),
            "f7_written_amount_inc_tax": _money_text(
                (amount_base * (Decimal("1") + rate)).quantize(
                    CENT, rounding=ROUND_HALF_UP)),
        })
    return make_f7_source_snapshot(
        rows=source_rows,
        backup_sha256=backup_sha256,
    )


def build_apply_manifest(
    db: Session,
    *,
    source_snapshot: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Partition every f7-affected row using one current read-only snapshot."""

    snapshot = normalize_f7_source_snapshot(source_snapshot)
    reason = _required_text(reason, label="reason", maximum=1000)
    changed: list[dict[str, Any]] = []
    preserved: list[dict[str, Any]] = []
    for source in snapshot["rows"]:
        contract = db.execute(text(
            "SELECT project_id, contract_no, contract_amount, amount_inc_tax, version "
            "FROM maintenance_project_contract "
            "WHERE project_contract_id = :contract_id"
        ), {"contract_id": source["project_contract_id"]}).mappings().one_or_none()
        if contract is None:
            raise RemediationError(
                "post_f7_identity_changed",
                f"合同 {source['project_contract_id']} 已不存在",
            )
        contract_amount = (
            Decimal(contract["contract_amount"]).quantize(CENT)
            if contract["contract_amount"] is not None else None
        )
        if (str(contract["project_id"]) != source["project_id"]
                or str(contract["contract_no"]) != source["contract_no"]
                or _money_text(contract_amount) != source["pre_f7_contract_amount"]):
            raise RemediationError(
                "post_f7_identity_changed",
                f"合同 {source['project_contract_id']} 的项目/编号/未税额已变化",
            )
        current = (
            Decimal(contract["amount_inc_tax"]).quantize(CENT)
            if contract["amount_inc_tax"] is not None else None
        )
        authority = _authoritative_manifest_evidence(
            db,
            contract_no=source["contract_no"],
            contract_amount=contract_amount,
        )
        base = {
            "project_contract_id": source["project_contract_id"],
            "expected_version": int(contract["version"]),
            "expected_current_amount_inc_tax": _money_text(current),
        }
        if authority is not None and authority[1] == current:
            preserved.append({**base, "evidence": authority[0]})
            continue
        if (int(contract["version"]) != source["pre_f7_version"]
                or _money_text(current) != source["f7_written_amount_inc_tax"]):
            raise RemediationError(
                "post_f7_manual_change",
                f"合同 {source['project_contract_id']} 已有人工作业且无等值权威证据；"
                "禁止自动置 NULL",
            )
        evidence = authority[0] if authority is not None else {
            "kind": "no_authoritative_evidence",
            "note": "pre-f7 备份证明原值为 NULL；当前无唯一权威台账或明确税率销售事实",
        }
        changed.append({**base, "evidence": evidence})
    return bind_apply_manifest_to_f7_source(
        {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "reason": reason,
            "rows": changed,
        },
        source_snapshot=snapshot,
        preserved_rows=preserved,
    )


def _source_entry_sha256(row: Any) -> str:
    return _canonical_sha256({
        "run_id": str(row["run_id"]),
        "project_contract_id": str(row["project_contract_id"]),
        "before_version": int(row["before_version"]),
        "after_version": int(row["after_version"]),
        "before_amount_inc_tax": _money_text(row["before_amount_inc_tax"]),
        "after_amount_inc_tax": _money_text(row["after_amount_inc_tax"]),
        "target_state": str(row["target_state"]),
        "evidence_kind": str(row["evidence_kind"]),
        "evidence_ref": str(row["evidence_ref"]),
        "evidence_snapshot": row["evidence_snapshot"],
    })


def _resolve_rollback(
    db: Session,
    *,
    contract_id: str,
    expected_version: int,
    expected_amount: Decimal | None,
    source_run_id: str,
    evidence: dict[str, Any],
    lock: bool,
) -> tuple[Decimal | None, str, dict[str, Any]]:
    if evidence["source_run_id"] != source_run_id:
        raise RemediationError("invalid_rollback_receipt", "rollback source_run_id 不一致")
    row = db.execute(text(
        "SELECT run_id, project_contract_id, before_version, after_version, "
        "before_amount_inc_tax, after_amount_inc_tax, target_state, "
        "evidence_kind, evidence_ref, evidence_snapshot "
        "FROM maintenance_contract_amount_remediation_entry "
        "WHERE run_id = :run_id AND project_contract_id = :contract_id" +
        _lock_clause(lock)
    ), {
        "run_id": source_run_id,
        "contract_id": contract_id,
    }).mappings().one_or_none()
    if row is None:
        raise RemediationError("invalid_rollback_receipt", "找不到原 remediation entry")
    if (_source_entry_sha256(row) != evidence["source_entry_sha256"]
            or int(row["after_version"]) != expected_version
            or row["after_amount_inc_tax"] != expected_amount):
        raise RemediationError(
            "invalid_rollback_receipt",
            "rollback receipt 与不可变原记录不一致",
        )
    target = row["before_amount_inc_tax"]
    snapshot = {
        "kind": "rollback_receipt",
        "source_run_id": source_run_id,
        "source_entry_sha256": evidence["source_entry_sha256"],
        "restored_amount_inc_tax": _money_text(target),
    }
    return target, f"rollback:{source_run_id}", snapshot


def _workbook_data_version(project_id: str, revision: int) -> str:
    """Match the canonical project-workbook concurrency token exactly."""

    return hashlib.sha256(f"{project_id}:{revision}".encode("utf-8")).hexdigest()


def _target_contract_projects(
    db: Session,
    *,
    manifest: dict[str, Any],
) -> dict[str, list[str]]:
    by_project: dict[str, list[str]] = {}
    for item in manifest["rows"]:
        contract_id = item["project_contract_id"]
        project_id = db.scalar(text(
            "SELECT project_id FROM maintenance_project_contract "
            "WHERE project_contract_id = :contract_id"
        ), {"contract_id": contract_id})
        if project_id is None:
            raise RemediationError(
                "contract_not_found", f"合同关系不存在：{contract_id}")
        by_project.setdefault(str(project_id), []).append(contract_id)
    return by_project


def _validate_workbook_state(
    *,
    project_id: str,
    revision: int,
    data_version: str,
) -> None:
    expected = _workbook_data_version(project_id, revision)
    if data_version != expected:
        raise RemediationError(
            "workbook_state_corrupt",
            f"项目 {project_id} 的 workbook data_version 与 revision 不一致",
        )


def _read_workbook_revisions(
    db: Session,
    *,
    plans: list[PlannedChange],
) -> dict[str, int]:
    revisions: dict[str, int] = {}
    for project_id in sorted({plan.project_id for plan in plans}):
        project = db.execute(text(
            "SELECT is_active FROM maintenance_project "
            "WHERE project_id = :project_id"
        ), {"project_id": project_id}).mappings().one_or_none()
        if project is None or not project["is_active"]:
            raise RemediationError(
                "project_not_active", f"项目 {project_id} 不存在或已归档")
        state = db.execute(text(
            "SELECT revision, data_version "
            "FROM maintenance_project_workbook_state "
            "WHERE project_id = :project_id"
        ), {"project_id": project_id}).mappings().one_or_none()
        if state is None:
            revisions[project_id] = 0
            continue
        revision = int(state["revision"])
        _validate_workbook_state(
            project_id=project_id,
            revision=revision,
            data_version=str(state["data_version"]),
        )
        revisions[project_id] = revision
    return revisions


def _lock_ledger_evidence_envelope(
    db: Session,
    *,
    contract_nos: list[str] | set[str] | tuple[str, ...],
) -> dict[str, list[str]]:
    """Lock ledger evidence before any canonical workbook-state lock.

    Existing matching batches and immutable raw rows are locked in stable
    order first.  The shared per-contract advisory identities then close the
    phantom window: an apply for a batch committed after the envelope scan can
    own its new batch row, but it must wait here before touching workbook
    state.  Conversely, an apply already holding an existing batch completes
    before this function can take any advisory or workbook lock.
    """

    identities = sorted({
        str(contract_no).strip()
        for contract_no in contract_nos
        if contract_no is not None and str(contract_no).strip()
    })
    if not identities:
        return {"contract_nos": [], "batch_ids": [], "row_ids": []}
    params = {"contract_nos": identities}
    batch_ids = [str(value) for value in db.scalars(text(
        "SELECT b.batch_id FROM maintenance_ledger_import_batch b "
        "WHERE EXISTS ("
        "  SELECT 1 FROM maintenance_ledger_contract_row r "
        "  WHERE r.batch_id = b.batch_id "
        "    AND r.order_no = ANY(CAST(:contract_nos AS TEXT[]))"
        ") ORDER BY b.batch_id FOR SHARE OF b"
    ), params)]
    row_ids = [str(value) for value in db.scalars(text(
        "SELECT r.row_id FROM maintenance_ledger_contract_row r "
        "WHERE r.order_no = ANY(CAST(:contract_nos AS TEXT[])) "
        "ORDER BY r.batch_id, r.row_id FOR SHARE OF r"
    ), params)]

    # Import lazily so source-snapshot export and default dry-run do not load
    # the workbook parser or acquire an advisory lock.
    from app.services import maintenance_ledger

    locked_identities = maintenance_ledger._lock_contract_evidence_identities(
        db,
        identities,
    )
    return {
        "contract_nos": locked_identities,
        "batch_ids": batch_ids,
        "row_ids": row_ids,
    }


def _lock_write_protocol(
    db: Session,
    *,
    manifest: dict[str, Any],
) -> dict[str, int]:
    """Take the canonical workbook-state -> project -> contract lock order."""

    by_project = _target_contract_projects(db, manifest=manifest)
    revisions: dict[str, int] = {}
    for project_id in sorted(by_project):
        db.execute(text(
            "INSERT INTO maintenance_project_workbook_state "
            "(project_id, revision, data_version) VALUES "
            "(:project_id, 0, :data_version) "
            "ON CONFLICT (project_id) DO NOTHING"
        ), {
            "project_id": project_id,
            "data_version": _workbook_data_version(project_id, 0),
        })
        state = db.execute(text(
            "SELECT revision, data_version "
            "FROM maintenance_project_workbook_state "
            "WHERE project_id = :project_id FOR UPDATE"
        ), {"project_id": project_id}).mappings().one()
        revision = int(state["revision"])
        _validate_workbook_state(
            project_id=project_id,
            revision=revision,
            data_version=str(state["data_version"]),
        )
        project = db.execute(text(
            "SELECT is_active FROM maintenance_project "
            "WHERE project_id = :project_id FOR UPDATE"
        ), {"project_id": project_id}).mappings().one_or_none()
        if project is None or not project["is_active"]:
            raise RemediationError(
                "project_not_active", f"项目 {project_id} 不存在或已归档")
        for contract_id in sorted(by_project[project_id]):
            locked_project_id = db.scalar(text(
                "SELECT project_id FROM maintenance_project_contract "
                "WHERE project_contract_id = :contract_id FOR UPDATE"
            ), {"contract_id": contract_id})
            if str(locked_project_id or "") != project_id:
                raise RemediationError(
                    "stale_contract",
                    f"合同 {contract_id} 已删除或项目归属已变化",
                )
        revisions[project_id] = revision
    return revisions


def _bump_workbook_revisions(
    db: Session,
    *,
    revisions: dict[str, int],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for project_id in sorted(revisions):
        before = revisions[project_id]
        after = before + 1
        data_version = _workbook_data_version(project_id, after)
        updated = db.execute(text(
            "UPDATE maintenance_project_workbook_state "
            "SET revision = :after, data_version = :data_version, "
            "updated_at = now() "
            "WHERE project_id = :project_id AND revision = :before"
        ), {
            "project_id": project_id,
            "before": before,
            "after": after,
            "data_version": data_version,
        })
        if updated.rowcount != 1:
            raise RemediationError(
                "workbook_compare_and_swap_failed",
                f"项目 {project_id} 的 workbook revision 在写入前发生变化",
            )
        changes.append({
            "project_id": project_id,
            "before_revision": before,
            "after_revision": after,
            "after_data_version": data_version,
        })
    return changes


def _attach_workbook_protocol(
    plans: list[PlannedChange],
    *,
    revisions: dict[str, int],
) -> list[PlannedChange]:
    enriched: list[PlannedChange] = []
    for plan in plans:
        before = revisions[plan.project_id]
        enriched.append(replace(
            plan,
            evidence_snapshot={
                **plan.evidence_snapshot,
                "workbook_write_protocol": {
                    "lock_order": (
                        "workbook_state->project->contract"
                        if plan.evidence_kind == "rollback_receipt"
                        else "ledger_batch->ledger_row->contract_advisory->"
                        "workbook_state->project->contract"
                    ),
                    "before_revision": before,
                    "after_revision": before + 1,
                    "after_data_version": _workbook_data_version(
                        plan.project_id, before + 1),
                },
            },
        ))
    return enriched


def _plan(
    db: Session,
    *,
    manifest: dict[str, Any],
    lock: bool,
    f7_source_by_id: dict[str, dict[str, Any]] | None = None,
) -> list[PlannedChange]:
    plans: list[PlannedChange] = []
    for item in manifest["rows"]:
        contract_id = item["project_contract_id"]
        contract = db.execute(text(
            "SELECT project_contract_id, project_id, contract_no, "
            "contract_amount, amount_inc_tax, version "
            "FROM maintenance_project_contract "
            "WHERE project_contract_id = :contract_id" +
            (" FOR UPDATE" if lock else "")
        ), {"contract_id": contract_id}).mappings().one_or_none()
        if contract is None:
            raise RemediationError("contract_not_found", f"合同关系不存在：{contract_id}")
        expected_amount = (
            Decimal(item["expected_current_amount_inc_tax"])
            if item["expected_current_amount_inc_tax"] is not None else None
        )
        current_amount = (
            Decimal(contract["amount_inc_tax"]).quantize(CENT)
            if contract["amount_inc_tax"] is not None else None
        )
        contract_amount = (
            Decimal(contract["contract_amount"]).quantize(CENT)
            if contract["contract_amount"] is not None else None
        )
        source = f7_source_by_id.get(contract_id) if f7_source_by_id else None
        if source is not None and (
            str(contract["project_id"]) != source["project_id"]
            or str(contract["contract_no"]) != source["contract_no"]
            or _money_text(contract_amount) != source["pre_f7_contract_amount"]
        ):
            raise RemediationError(
                "post_f7_identity_changed",
                f"合同 {contract_id} 的项目/编号/未税额已偏离 pre-f7 证据",
            )
        if int(contract["version"]) != item["expected_version"]:
            raise RemediationError(
                "stale_contract",
                f"合同 {contract_id} 版本不符：expected={item['expected_version']}，"
                f"current={contract['version']}",
            )
        if current_amount != expected_amount:
            raise RemediationError(
                "stale_contract",
                f"合同 {contract_id} 当前含税额与 manifest expected-current 不符",
            )
        evidence = item["evidence"]
        kind = evidence["kind"]
        if kind == "ledger_amount_inc_tax":
            target, evidence_ref, snapshot = _resolve_ledger(
                db,
                contract_no=str(contract["contract_no"]),
                evidence=evidence,
                lock=lock,
            )
            state = "authoritative"
        elif kind == "sales_explicit_tax":
            target, evidence_ref, snapshot = _resolve_sales(
                db,
                contract_no=str(contract["contract_no"]),
                contract_amount=contract_amount,
                evidence=evidence,
                lock=lock,
            )
            state = "authoritative"
        elif kind == "no_authoritative_evidence":
            target, evidence_ref, snapshot = _resolve_incomplete(
                db,
                contract_no=str(contract["contract_no"]),
                contract_amount=contract_amount,
                evidence=evidence,
                lock=lock,
            )
            state = "incomplete"
        else:
            target, evidence_ref, snapshot = _resolve_rollback(
                db,
                contract_id=contract_id,
                expected_version=item["expected_version"],
                expected_amount=expected_amount,
                source_run_id=manifest["source_run_id"],
                evidence=evidence,
                lock=lock,
            )
            # Rollback restores a before-image; non-NULL does not prove that
            # historical value was authoritative (it may be the original f7a
            # guess).  Keep that semantic distinct in the immutable receipt.
            state = "restored"
        if target == current_amount:
            raise RemediationError(
                "no_change",
                f"合同 {contract_id} 目标值与当前值相同；请从 manifest 删除该行",
            )
        plans.append(PlannedChange(
            project_contract_id=contract_id,
            project_id=str(contract["project_id"]),
            contract_no=str(contract["contract_no"]),
            expected_version=item["expected_version"],
            before_amount_inc_tax=current_amount,
            after_amount_inc_tax=target,
            target_state=state,
            evidence_kind=kind,
            evidence_ref=evidence_ref,
            evidence_snapshot={
                **snapshot,
                **({
                    "f7_source": {
                        "snapshot_sha256": manifest["f7_source"]["snapshot_sha256"],
                        "backup_sha256": manifest["f7_source"]["backup_sha256"],
                        "row_sha256": source["row_sha256"],
                        "pre_f7_amount_inc_tax": None,
                        "f7_written_amount_inc_tax": source[
                            "f7_written_amount_inc_tax"],
                    },
                } if source is not None else {}),
            },
        ))
    return plans


def _verify_preserved(
    db: Session,
    *,
    manifest: dict[str, Any],
    lock: bool,
    f7_source_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for item in manifest["preserved_rows"]:
        contract_id = item["project_contract_id"]
        source = f7_source_by_id[contract_id]
        contract = db.execute(text(
            "SELECT project_id, contract_no, contract_amount, amount_inc_tax, version "
            "FROM maintenance_project_contract "
            "WHERE project_contract_id = :contract_id" +
            (" FOR UPDATE" if lock else "")
        ), {"contract_id": contract_id}).mappings().one_or_none()
        if contract is None:
            raise RemediationError("contract_not_found", f"合同关系不存在：{contract_id}")
        current = (
            Decimal(contract["amount_inc_tax"]).quantize(CENT)
            if contract["amount_inc_tax"] is not None else None
        )
        contract_amount = (
            Decimal(contract["contract_amount"]).quantize(CENT)
            if contract["contract_amount"] is not None else None
        )
        expected = (
            Decimal(item["expected_current_amount_inc_tax"])
            if item["expected_current_amount_inc_tax"] is not None else None
        )
        if (str(contract["project_id"]) != source["project_id"]
                or str(contract["contract_no"]) != source["contract_no"]
                or _money_text(contract_amount) != source["pre_f7_contract_amount"]):
            raise RemediationError(
                "post_f7_identity_changed",
                f"合同 {contract_id} 的项目/编号/未税额已偏离 pre-f7 证据",
            )
        if int(contract["version"]) != item["expected_version"] or current != expected:
            raise RemediationError(
                "stale_contract", f"保留合同 {contract_id} 已在 manifest 后变化")
        evidence = item["evidence"]
        if evidence["kind"] == "ledger_amount_inc_tax":
            target, evidence_ref, _snapshot = _resolve_ledger(
                db,
                contract_no=str(contract["contract_no"]),
                evidence=evidence,
                lock=lock,
            )
        else:
            target, evidence_ref, _snapshot = _resolve_sales(
                db,
                contract_no=str(contract["contract_no"]),
                contract_amount=contract_amount,
                evidence=evidence,
                lock=lock,
            )
        if target != current:
            raise RemediationError(
                "preservation_evidence_changed",
                f"合同 {contract_id} 的保留值已不再等于实时权威证据",
            )
        verified.append({
            "project_contract_id": contract_id,
            "project_id": str(contract["project_id"]),
            "contract_no": str(contract["contract_no"]),
            "amount_inc_tax": _money_text(current),
            "version": int(contract["version"]),
            "evidence_kind": evidence["kind"],
            "evidence_ref": evidence_ref,
            "f7_source_row_sha256": source["row_sha256"],
        })
    return verified


def _change_payload(plan: PlannedChange) -> dict[str, Any]:
    return {
        "project_contract_id": plan.project_contract_id,
        "project_id": plan.project_id,
        "contract_no": plan.contract_no,
        "before_amount_inc_tax": _money_text(plan.before_amount_inc_tax),
        "after_amount_inc_tax": _money_text(plan.after_amount_inc_tax),
        "before_version": plan.expected_version,
        "after_version": plan.after_version,
        "target_state": plan.target_state,
        "evidence_kind": plan.evidence_kind,
        "evidence_ref": plan.evidence_ref,
    }


def _entry_digest_from_plan(run_id: str, plan: PlannedChange) -> str:
    return _canonical_sha256({
        "run_id": run_id,
        "project_contract_id": plan.project_contract_id,
        "before_version": plan.expected_version,
        "after_version": plan.after_version,
        "before_amount_inc_tax": _money_text(plan.before_amount_inc_tax),
        "after_amount_inc_tax": _money_text(plan.after_amount_inc_tax),
        "target_state": plan.target_state,
        "evidence_kind": plan.evidence_kind,
        "evidence_ref": plan.evidence_ref,
        "evidence_snapshot": plan.evidence_snapshot,
    })


def _rollback_manifest(
    *,
    run_id: str,
    plans: list[PlannedChange],
) -> dict[str, Any]:
    return normalize_manifest({
        "schema_version": SCHEMA_VERSION,
        "mode": "rollback",
        "source_run_id": run_id,
        "reason": f"回滚合同含税额 remediation run {run_id}",
        "rows": [{
            "project_contract_id": plan.project_contract_id,
            "expected_version": plan.after_version,
            "expected_current_amount_inc_tax": _money_text(
                plan.after_amount_inc_tax),
            "evidence": {
                "kind": "rollback_receipt",
                "source_run_id": run_id,
                "source_entry_sha256": _entry_digest_from_plan(run_id, plan),
            },
        } for plan in plans],
    })


def reconcile_manifest_run(
    db: Session,
    *,
    manifest_hash: str,
) -> dict[str, Any]:
    """Recover the immutable receipt after commit/output acknowledgement loss."""

    manifest_hash = _sha256_text(
        manifest_hash, label="reconcile manifest_sha256")
    run = db.execute(text(
        "SELECT run_id, manifest_sha256, mode, source_run_id, reason, operated_by, "
        "database_principal, row_count, source_snapshot_sha256, "
        "source_backup_sha256, source_algorithm_sha256, "
        "f7_affected_set_sha256, preserved_set_sha256, changed_set_sha256, "
        "f7_affected_count, preserved_count, "
        "authoritative_corrected_count, cleared_count, created_at "
        "FROM maintenance_contract_amount_remediation_run "
        "WHERE manifest_sha256 = :manifest_hash"
    ), {"manifest_hash": manifest_hash}).mappings().one_or_none()
    if run is None:
        return {
            "status": "not_found",
            "manifest_sha256": manifest_hash,
            "safe_to_retry_after_primary_db_confirmation": True,
        }
    entries = list(db.execute(text(
        "SELECT run_id, project_contract_id, project_id, contract_no, "
        "before_version, after_version, before_amount_inc_tax, "
        "after_amount_inc_tax, target_state, evidence_kind, evidence_ref, "
        "evidence_snapshot FROM maintenance_contract_amount_remediation_entry "
        "WHERE run_id = :run_id ORDER BY project_contract_id"
    ), {"run_id": run["run_id"]}).mappings())
    if len(entries) != int(run["row_count"]):
        raise RemediationError(
            "audit_receipt_corrupt",
            "remediation run.row_count 与 append-only entry 数量不一致",
        )
    rollback = normalize_manifest({
        "schema_version": SCHEMA_VERSION,
        "mode": "rollback",
        "source_run_id": str(run["run_id"]),
        "reason": f"回滚合同含税额 remediation run {run['run_id']}",
        "rows": [{
            "project_contract_id": str(entry["project_contract_id"]),
            "expected_version": int(entry["after_version"]),
            "expected_current_amount_inc_tax": _money_text(
                entry["after_amount_inc_tax"]),
            "evidence": {
                "kind": "rollback_receipt",
                "source_run_id": str(run["run_id"]),
                "source_entry_sha256": _source_entry_sha256(entry),
            },
        } for entry in entries],
    })
    return {
        "status": "applied",
        "run_id": str(run["run_id"]),
        "manifest_sha256": str(run["manifest_sha256"]),
        "mode": str(run["mode"]),
        "operated_by": str(run["operated_by"]),
        "database_principal": str(run["database_principal"]),
        "row_count": int(run["row_count"]),
        "source_snapshot_sha256": run["source_snapshot_sha256"],
        "source_backup_sha256": run["source_backup_sha256"],
        "source_algorithm_sha256": run["source_algorithm_sha256"],
        "f7_affected_set_sha256": run["f7_affected_set_sha256"],
        "partition": ({
            "affected_count": int(run["f7_affected_count"]),
            "preserved_count": int(run["preserved_count"]),
            "authoritative_corrected_count": int(
                run["authoritative_corrected_count"]),
            "cleared_count": int(run["cleared_count"]),
            "preserved_set_sha256": run["preserved_set_sha256"],
            "changed_set_sha256": run["changed_set_sha256"],
        } if run["mode"] == "apply" else None),
        "created_at": run["created_at"].isoformat(),
        "rollback_manifest": rollback,
        "rollback_manifest_sha256": manifest_sha256(rollback),
    }


def _active_operator(db: Session, operator: str) -> bool:
    # Hold a share lock through commit so an account cannot be disabled between
    # authorization and the audited write.
    return db.scalar(text(
        "SELECT 1 FROM sys_user "
        "WHERE username = :operator AND is_active IS TRUE "
        "AND deleted_at IS NULL LIMIT 1 FOR SHARE"
    ), {"operator": operator}) is not None


def _apply(
    db: Session,
    *,
    manifest: dict[str, Any],
    manifest_hash: str,
    operator: str,
    plans: list[PlannedChange],
    workbook_revisions: dict[str, int],
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    if db.scalar(text(
        "SELECT EXISTS (SELECT 1 "
        "FROM maintenance_contract_amount_remediation_run "
        "WHERE manifest_sha256 = :manifest_hash)"
    ), {"manifest_hash": manifest_hash}):
        raise RemediationError("manifest_already_applied", "该 manifest 已成功执行")
    run_id = str(uuid4())
    source = manifest.get("f7_source")
    partition = manifest.get("partition")
    db.execute(text(
        "INSERT INTO maintenance_contract_amount_remediation_run "
        "(run_id, manifest_sha256, mode, source_run_id, reason, operated_by, "
        "database_principal, row_count, source_snapshot_sha256, "
        "source_backup_sha256, source_algorithm_sha256, "
        "f7_affected_set_sha256, preserved_set_sha256, changed_set_sha256, "
        "f7_affected_count, preserved_count, authoritative_corrected_count, "
        "cleared_count) VALUES "
        "(:run_id, :manifest_hash, :mode, :source_run_id, :reason, "
        ":operator, current_user, :row_count, :source_snapshot_sha256, "
        ":source_backup_sha256, :source_algorithm_sha256, "
        ":f7_affected_set_sha256, :preserved_set_sha256, :changed_set_sha256, "
        ":f7_affected_count, :preserved_count, :authoritative_corrected_count, "
        ":cleared_count)"
    ), {
        "run_id": run_id,
        "manifest_hash": manifest_hash,
        "mode": manifest["mode"],
        "source_run_id": manifest.get("source_run_id"),
        "reason": manifest["reason"],
        "operator": operator,
        "row_count": len(plans),
        "source_snapshot_sha256": (
            source["snapshot_sha256"] if source is not None else None),
        "source_backup_sha256": (
            source["backup_sha256"] if source is not None else None),
        "source_algorithm_sha256": (
            source["algorithm_sha256"] if source is not None else None),
        "f7_affected_set_sha256": (
            source["affected_set_sha256"] if source is not None else None),
        "preserved_set_sha256": (
            partition["preserved_set_sha256"] if partition is not None else None),
        "changed_set_sha256": (
            partition["changed_set_sha256"] if partition is not None else None),
        "f7_affected_count": (
            partition["affected_count"] if partition is not None else None),
        "preserved_count": (
            partition["preserved_count"] if partition is not None else None),
        "authoritative_corrected_count": (
            partition["authoritative_corrected_count"]
            if partition is not None else None
        ),
        "cleared_count": (
            partition["cleared_count"] if partition is not None else None),
    })
    audit_action = (
        "rollback_contract_amount"
        if manifest["mode"] == "rollback"
        else "remediate_contract_amount"
    )
    audit_reason = f"{manifest['reason']} [remediation_run={run_id}]"
    for plan in plans:
        updated = db.execute(text(
            "UPDATE maintenance_project_contract "
            "SET amount_inc_tax = CAST(:after_amount AS NUMERIC(14, 2)), "
            "version = version + 1, updated_at = now() "
            "WHERE project_contract_id = :contract_id "
            "AND version = :expected_version "
            "AND amount_inc_tax IS NOT DISTINCT FROM "
            "CAST(:expected_amount AS NUMERIC(14, 2))"
        ), {
            "after_amount": plan.after_amount_inc_tax,
            "contract_id": plan.project_contract_id,
            "expected_version": plan.expected_version,
            "expected_amount": plan.before_amount_inc_tax,
        })
        if updated.rowcount != 1:
            raise RemediationError(
                "compare_and_swap_failed",
                f"合同 {plan.project_contract_id} 在写入前发生变化；整批回滚",
            )
        db.execute(text(
            "INSERT INTO maintenance_contract_amount_remediation_entry "
            "(run_id, project_contract_id, project_id, contract_no, "
            "expected_version, before_version, after_version, "
            "before_amount_inc_tax, after_amount_inc_tax, target_state, "
            "evidence_kind, evidence_ref, evidence_snapshot) VALUES "
            "(:run_id, :contract_id, :project_id, :contract_no, "
            ":expected_version, :before_version, :after_version, "
            ":before_amount, :after_amount, :target_state, :evidence_kind, "
            ":evidence_ref, CAST(:evidence_snapshot AS JSONB))"
        ), {
            "run_id": run_id,
            "contract_id": plan.project_contract_id,
            "project_id": plan.project_id,
            "contract_no": plan.contract_no,
            "expected_version": plan.expected_version,
            "before_version": plan.expected_version,
            "after_version": plan.after_version,
            "before_amount": plan.before_amount_inc_tax,
            "after_amount": plan.after_amount_inc_tax,
            "target_state": plan.target_state,
            "evidence_kind": plan.evidence_kind,
            "evidence_ref": plan.evidence_ref,
            "evidence_snapshot": json.dumps(
                plan.evidence_snapshot, ensure_ascii=False, sort_keys=True),
        })
        before_json = {
            "amount_inc_tax": _money_text(plan.before_amount_inc_tax),
            "version": plan.expected_version,
        }
        after_json = {
            "amount_inc_tax": _money_text(plan.after_amount_inc_tax),
            "version": plan.after_version,
            "amount_state": plan.target_state,
            "evidence_kind": plan.evidence_kind,
            "evidence_ref": plan.evidence_ref,
            "remediation_run_id": run_id,
            "workbook_revision_before": workbook_revisions[plan.project_id],
            "workbook_revision_after": workbook_revisions[plan.project_id] + 1,
            "workbook_data_version_after": _workbook_data_version(
                plan.project_id,
                workbook_revisions[plan.project_id] + 1,
            ),
        }
        db.execute(text(
            "INSERT INTO maintenance_project_audit_log "
            "(project_id, entity_type, entity_id, action, before_json, "
            "after_json, reason, operated_by) VALUES "
            "(:project_id, 'project_contract', :contract_id, :action, "
            "CAST(:before_json AS JSONB), CAST(:after_json AS JSONB), "
            ":reason, :operator)"
        ), {
            "project_id": plan.project_id,
            "contract_id": plan.project_contract_id,
            "action": audit_action,
            "before_json": json.dumps(before_json, ensure_ascii=False),
            "after_json": json.dumps(after_json, ensure_ascii=False),
            "reason": audit_reason,
            "operator": operator,
        })
    workbook_changes = _bump_workbook_revisions(
        db,
        revisions=workbook_revisions,
    )
    rollback = _rollback_manifest(run_id=run_id, plans=plans)
    return run_id, rollback, workbook_changes


def run_remediation(
    db: Session,
    *,
    manifest: dict[str, Any],
    execute: bool = False,
    operator: str | None = None,
    confirm_manifest_sha256: str | None = None,
    source_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_manifest(manifest)
    f7_source_by_id = _validate_f7_binding(
        normalized,
        source_snapshot=source_snapshot,
    )
    digest = manifest_sha256(normalized)
    run_id: str | None = None
    try:
        if execute:
            operator = _required_text(operator, label="operator", maximum=64)
            if confirm_manifest_sha256 != digest:
                raise RemediationError(
                    "manifest_confirmation_mismatch",
                    "--confirm-manifest-sha256 必须精确等于本 manifest 的 dry-run 指纹",
                )
            db.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            if not _active_operator(db, operator):
                raise RemediationError(
                    "operator_not_active",
                    "execute 必须使用有效的实名系统账号",
                )
            lock_manifest = normalized
            if normalized["mode"] == "apply":
                lock_manifest = {
                    **normalized,
                    "rows": normalized["rows"] + normalized["preserved_rows"],
                }
                _lock_ledger_evidence_envelope(
                    db,
                    contract_nos={
                        source["contract_no"]
                        for source in f7_source_by_id.values()
                    },
                )
            all_workbook_revisions = _lock_write_protocol(
                db,
                manifest=lock_manifest,
            )
        else:
            db.execute(text("SET TRANSACTION READ ONLY"))
        plans = _plan(
            db,
            manifest=normalized,
            lock=execute,
            f7_source_by_id=f7_source_by_id,
        )
        preserved = (
            _verify_preserved(
                db,
                manifest=normalized,
                lock=execute,
                f7_source_by_id=f7_source_by_id,
            )
            if normalized["mode"] == "apply" else []
        )
        changed_project_ids = {plan.project_id for plan in plans}
        if execute:
            workbook_revisions = {
                project_id: revision
                for project_id, revision in all_workbook_revisions.items()
                if project_id in changed_project_ids
            }
        if not execute:
            workbook_revisions = _read_workbook_revisions(db, plans=plans)
        plans = _attach_workbook_protocol(
            plans,
            revisions=workbook_revisions,
        )
        predicted_workbook_changes = [{
            "project_id": project_id,
            "before_revision": revision,
            "after_revision": revision + 1,
            "after_data_version": _workbook_data_version(
                project_id, revision + 1),
        } for project_id, revision in sorted(workbook_revisions.items())]
        result: dict[str, Any] = {
            "status": "ready" if not execute else "applied",
            "dry_run": not execute,
            "manifest_sha256": digest,
            "mode": normalized["mode"],
            "row_count": len(plans),
            "changes": [_change_payload(plan) for plan in plans],
            "preserved_rows": preserved,
            "workbook_revision_changes": predicted_workbook_changes,
        }
        if normalized["mode"] == "apply":
            result["f7_source"] = normalized["f7_source"]
            result["partition"] = normalized["partition"]
        if not execute:
            db.rollback()
            return result
        run_id, rollback, workbook_changes = _apply(
            db,
            manifest=normalized,
            manifest_hash=digest,
            operator=operator,
            plans=plans,
            workbook_revisions=workbook_revisions,
        )
        if run_id is None:  # defensive: commit must always be reconcilable
            raise RemediationError("internal_error", "未生成可对账的 remediation run_id")
        rollback_hash = manifest_sha256(rollback)
        result["run_id"] = run_id
        result["rollback_manifest"] = rollback
        result["rollback_manifest_sha256"] = rollback_hash
        result["workbook_revision_changes"] = workbook_changes
    except Exception:
        # Cleanup failure must never mask the original validation/CAS error.
        try:
            if db is not None:
                db.rollback()
        except Exception:
            pass
        raise
    try:
        db.commit()
    except Exception as exc:
        # COMMIT can succeed server-side while its acknowledgement is lost.
        # Calling rollback or claiming zero writes would be misleading.  The
        # immutable run row is the sole source of truth for reconciliation.
        raise CommitOutcomeUnknown(
            run_id=run_id,
            manifest_hash=digest,
        ) from exc
    return result


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="安全修复 f7a 后合同含税额（默认 dry-run）",
        allow_abbrev=False,
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--source-snapshot",
        type=Path,
        help="由 pre-f7 备份恢复库导出的 source snapshot；apply 必填",
    )
    parser.add_argument(
        "--export-f7-source-snapshot",
        action="store_true",
        help="在停于 d5c1f8a3b7e2 的备份恢复库上复算 f7 精确写入集",
    )
    parser.add_argument(
        "--backup-file",
        type=Path,
        help="导出 source snapshot 时用于现场计算 SHA-256 的原始备份文件",
    )
    parser.add_argument(
        "--build-manifest",
        action="store_true",
        help="按当前只读快照把完整 f7 集合分为 preserved/corrected/cleared",
    )
    parser.add_argument(
        "--reason",
        help="build-manifest 必填：工单与双人复核原因",
    )
    parser.add_argument(
        "--reconcile-manifest-sha256",
        help="commit/output ACK 丢失后只读查询 run 并重建 rollback receipt",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="显式执行；省略时只读 dry-run",
    )
    parser.add_argument(
        "--operator",
        help="execute 必填：有效实名 sys_user.username",
    )
    parser.add_argument(
        "--confirm-manifest-sha256",
        help="execute 必填：复制同一 manifest 的 dry-run 指纹",
    )
    return parser


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RemediationError("invalid_backup_file", "无法读取备份文件计算 SHA-256") from exc
    return digest.hexdigest()


def _error_payload(*, execute: bool, code: str) -> dict[str, Any]:
    return {
        "status": "error",
        "dry_run": not execute,
        "code": code,
        "applied_rows": 0,
    }


def main(argv: list[str] | None = None) -> int:
    execute = False
    result: dict[str, Any] | None = None
    try:
        args = _parser().parse_args(argv)
        execute = bool(args.execute)
        generation_modes = sum((
            bool(args.export_f7_source_snapshot),
            bool(args.build_manifest),
            bool(args.reconcile_manifest_sha256),
        ))
        if generation_modes > 1 or (generation_modes and execute):
            raise RemediationError(
                "invalid_arguments", "source 导出/manifest 生成不能与 execute 混用")
        if args.export_f7_source_snapshot:
            if args.backup_file is None or args.manifest is not None:
                raise RemediationError(
                    "invalid_arguments",
                    "export-f7-source-snapshot 只接受 --backup-file",
                )
        elif args.build_manifest:
            if args.source_snapshot is None or not args.reason or args.manifest is not None:
                raise RemediationError(
                    "invalid_arguments",
                    "build-manifest 必须提供 --source-snapshot 与 --reason",
                )
        elif args.reconcile_manifest_sha256:
            if args.manifest is not None or args.source_snapshot is not None:
                raise RemediationError(
                    "invalid_arguments", "reconcile 只接受 manifest SHA-256")
        elif args.manifest is None:
            raise RemediationError("invalid_arguments", "dry-run/execute 必须提供 --manifest")
        if execute and (not args.operator or not args.confirm_manifest_sha256):
            raise RemediationError(
                "execution_confirmation_required",
                "execute 必须同时提供 --operator 和 --confirm-manifest-sha256",
            )
        from app.db import SessionLocal

        with SessionLocal() as db:
            if args.export_f7_source_snapshot:
                db.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                snapshot = export_f7_source_snapshot(
                    db,
                    backup_sha256=_file_sha256(args.backup_file),
                )
                result = {
                    "status": "source_snapshot_ready",
                    "source_snapshot_sha256": f7_source_snapshot_sha256(snapshot),
                    "source_snapshot": snapshot,
                }
                db.rollback()
            elif args.build_manifest:
                snapshot = load_f7_source_snapshot(args.source_snapshot)
                db.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                built = build_apply_manifest(
                    db,
                    source_snapshot=snapshot,
                    reason=args.reason,
                )
                result = {
                    "status": "manifest_ready",
                    "manifest_sha256": manifest_sha256(built),
                    "partition": built["partition"],
                    "manifest": built,
                }
                db.rollback()
            elif args.reconcile_manifest_sha256:
                db.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                result = reconcile_manifest_run(
                    db,
                    manifest_hash=args.reconcile_manifest_sha256,
                )
                db.rollback()
            else:
                manifest = load_manifest(args.manifest)
                source_snapshot = (
                    load_f7_source_snapshot(args.source_snapshot)
                    if args.source_snapshot is not None else None
                )
                result = run_remediation(
                    db,
                    manifest=manifest,
                    execute=execute,
                    operator=args.operator,
                    confirm_manifest_sha256=args.confirm_manifest_sha256,
                    source_snapshot=source_snapshot,
                )
    except _SafeArgumentError:
        payload = _error_payload(execute=False, code="invalid_arguments")
    except CommitOutcomeUnknown as exc:
        payload = {
            "status": "commit_outcome_unknown",
            "dry_run": False,
            "code": exc.code,
            "applied_rows": None,
            "manifest_sha256": exc.manifest_hash,
            "run_id": exc.run_id,
            "required_action": (
                "run --reconcile-manifest-sha256 on the primary database "
                "before any retry"
            ),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        print(
            "contract amount remediation commit outcome unknown; "
            "verify the immutable run ledger before retry",
            file=sys.stderr,
        )
        return 3
    except RemediationError as exc:
        payload = _error_payload(execute=execute, code=exc.code)
    except Exception:
        if result is not None and execute:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            print(
                "contract amount remediation committed; "
                "post-commit session cleanup failed",
                file=sys.stderr,
            )
            return 3
        payload = _error_payload(execute=execute, code="internal_error")
    else:
        try:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        except Exception:
            if execute and result is not None:
                print(
                    "contract amount remediation committed but receipt output failed; "
                    "query the run ledger before any retry",
                    file=sys.stderr,
                )
                return 3
            print(
                "contract amount remediation dry-run output failed; no writes committed",
                file=sys.stderr,
            )
            return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    print(
        "contract amount remediation failed before commit; transaction rolled back",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
