"""Versioned, fail-closed provenance contracts for immutable Agent artifacts.

The model and HTTP payloads never receive or construct :class:`TrustedEvidence`.
Only deterministic server adapters mint it, and every evidence object is bound to the
exact renderer input by a canonical SHA-256 fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from io import BytesIO
from typing import Any, Iterable

from app import config, permissions
from app.services import agent_integrity
from openpyxl import Workbook


ACCESS_SCHEMA_VERSION = "artifact-access/v2"
ROW_PREDICATE_VERSION = "row-access/v1"
SERVER_QUERY_PROOF_VERSION = "server-query/v1"
INTERNAL_TEST_PROOF_VERSION = "internal-test-source/v1"
ARTIFACT_SNAPSHOT_PROOF_VERSION = "artifact-snapshot/v1"
ARTIFACT_PREDICATE_VERSION = "artifact-delegation/v1"
SOURCE_SNAPSHOT_SCHEMA_VERSION = "artifact-source-snapshot/v1"
SOURCE_SNAPSHOT_PURPOSE = "agent.source-snapshot"
SOURCE_UNION_PROOF_VERSION = "source-union/v1"
SOURCE_SET_PREDICATE_VERSION = "source-condition-set/v1"
IDENTITY_CLASSIFIER_VERSION = "identity-template-classifier/v1"
_IDENTITY_PROFILES = {
    "pn-replenishment-request/v1": {
        "申请": ("PN", "数量", "备注"),
    },
}
_CORE_TIMESTAMP = re.compile(
    rb"(<dcterms:(?:created|modified)[^>]*>).*?(</dcterms:(?:created|modified)>)"
)

_EVIDENCE_SEAL = object()
_SENSITIVITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
MAX_SOURCE_SNAPSHOTS = 32
MAX_SOURCE_SCOPE_BYTES = 256 * 1024
MAX_SCOPE_ITEMS = 64
MAX_SCOPE_STRING = 128
_RESOURCE_PERMISSION = {
    key.removeprefix("page_"): key for key in permissions.PAGE_KEYS
}
_FIELD_PERMISSION = {
    group: key
    for key, groups in permissions.DATA_GROUPS.items()
    for group in groups
}


class ProvenanceError(ValueError):
    """The evidence is unknown, mutable, inconsistent, or cannot be evaluated."""


@dataclass(frozen=True, slots=True)
class TrustedEvidence:
    """Opaque object capability minted only by a deterministic server adapter."""

    owner_sub: str
    content_fingerprint: str
    source_envelopes_json: tuple[str, ...]
    _seal: object


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ProvenanceError("非有限数值不能进入来源指纹")
        return {"$float": repr(value)}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ProvenanceError("来源指纹对象键必须是字符串")
        return {
            key: _canonical_value(value[key])
            for key in sorted(value)
        }
    raise ProvenanceError(f"来源指纹不支持类型 {type(value).__name__}")


def content_fingerprint(renderer: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"renderer": renderer, "payload": _canonical_value(payload)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _unclassified_upload_scope() -> dict[str, Any]:
    return {
        "schema_version": ACCESS_SCHEMA_VERSION,
        "policy": "owner_only",
        "classification": "business_content",
        "proof_version": "upload-unclassified/v1",
        "containment_status": "unclassified",
        "required_permissions": [],
        "contained_resources": [],
        "contained_fields": [],
        "sensitivity": "critical",
        "row_subject": None,
        "predicate_version": "unclassified/v1",
        "condition": {"op": "unknown"},
        "source_access_snapshots": [],
        "template_proof": None,
    }


@lru_cache(maxsize=len(_IDENTITY_PROFILES))
def _canonical_template_members(profile_id: str) -> dict[str, bytes]:
    profile = _IDENTITY_PROFILES[profile_id]
    workbook = Workbook()
    first = True
    for sheet_name, headers in profile.items():
        sheet = workbook.active if first else workbook.create_sheet()
        first = False
        sheet.title = sheet_name
        sheet.append(list(headers))
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    with zipfile.ZipFile(BytesIO(buffer.getvalue())) as archive:
        return {
            name: _CORE_TIMESTAMP.sub(rb"\1<TIMESTAMP>\2", archive.read(name))
            for name in archive.namelist()
        }


def _identity_profile(content: bytes) -> str | None:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            if (
                len(infos) > 32
                or len({item.filename for item in infos}) != len(infos)
                or sum(item.file_size for item in infos) > 8 * 1024 * 1024
                or any(item.file_size > 2 * 1024 * 1024 for item in infos)
            ):
                return None
            actual = {
                item.filename: _CORE_TIMESTAMP.sub(
                    rb"\1<TIMESTAMP>\2", archive.read(item.filename)
                )
                for item in infos
            }
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return None
    for profile_id in _IDENTITY_PROFILES:
        if actual == _canonical_template_members(profile_id):
            return profile_id
    return None


def classify_upload_access_scope(content: bytes, ext: str) -> dict[str, Any]:
    """Pre-model deterministic template classifier; never upgrades generated output."""
    if ext != "xlsx":
        return _unclassified_upload_scope()
    profile_id = _identity_profile(content)
    if profile_id is None:
        return _unclassified_upload_scope()
    profile = _IDENTITY_PROFILES[profile_id]
    return {
        "schema_version": ACCESS_SCHEMA_VERSION,
        "policy": "owner_only",
        "classification": "identity_only",
        "proof_version": IDENTITY_CLASSIFIER_VERSION,
        "containment_status": "classified",
        "required_permissions": [],
        "contained_resources": [],
        "contained_fields": [],
        "sensitivity": "low",
        "row_subject": None,
        "predicate_version": "identity-top/v1",
        "condition": {"op": "top"},
        "source_access_snapshots": [],
        "template_proof": {
            "classifier_version": IDENTITY_CLASSIFIER_VERSION,
            "profile_id": profile_id,
            "template_sha256": hashlib.sha256(content).hexdigest(),
            "sheet_headers": [
                {"sheet": sheet, "headers": list(headers)}
                for sheet, headers in profile.items()
            ],
            "safe_style_profile": "default-style-only/v1",
            "pre_model": True,
        },
    }


def _identity_scope_matches(scope: Any, source_sha256: str) -> bool:
    """Require the complete server-classifier record, not a caller assertion."""
    if not isinstance(scope, dict):
        return False
    proof = scope.get("template_proof")
    if not isinstance(proof, dict):
        return False
    profile_id = proof.get("profile_id")
    profile = _IDENTITY_PROFILES.get(profile_id)
    if profile is None:
        return False
    expected_proof = {
        "classifier_version": IDENTITY_CLASSIFIER_VERSION,
        "profile_id": profile_id,
        "template_sha256": source_sha256,
        "sheet_headers": [
            {"sheet": sheet, "headers": list(headers)}
            for sheet, headers in profile.items()
        ],
        "safe_style_profile": "default-style-only/v1",
        "pre_model": True,
    }
    return scope == {
        "schema_version": ACCESS_SCHEMA_VERSION,
        "policy": "owner_only",
        "classification": "identity_only",
        "proof_version": IDENTITY_CLASSIFIER_VERSION,
        "containment_status": "classified",
        "required_permissions": [],
        "contained_resources": [],
        "contained_fields": [],
        "sensitivity": "low",
        "row_subject": None,
        "predicate_version": "identity-top/v1",
        "condition": {"op": "top"},
        "source_access_snapshots": [],
        "template_proof": expected_proof,
    }


def report_fingerprint(
    *,
    title: str | None,
    headers: list[str],
    rows: list[list],
    output_name: str | None,
    money_cols: list[int] | None,
) -> str:
    return content_fingerprint(
        "write_report/v1",
        {
            "title": title,
            "headers": headers,
            "rows": rows,
            "output_name": output_name,
            "money_cols": money_cols or [],
        },
    )


def excel_edit_fingerprint(
    *,
    base_file_id: str | None,
    sheet: str | None,
    cells: list[dict],
    output_name: str | None,
) -> str:
    return content_fingerprint(
        "write_excel/v1",
        {
            "base_file_id": base_file_id,
            "sheet": sheet,
            "cells": cells,
            "output_name": output_name,
        },
    )


def _positive_permissions(
    resources: Iterable[str], fields: Iterable[str]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    def checked(values: Iterable[str], label: str) -> set[str]:
        if isinstance(values, (str, bytes, dict)):
            raise ProvenanceError(f"{label} 必须是字符串数组")
        try:
            items = list(values)
        except TypeError as exc:
            raise ProvenanceError(f"{label} 必须是字符串数组") from exc
        if (
            len(items) > MAX_SCOPE_ITEMS
            or not all(
                isinstance(item, str)
                and 0 < len(item) <= MAX_SCOPE_STRING
                for item in items
            )
        ):
            raise ProvenanceError(f"{label} 字段类型或数量无效")
        return set(items)

    resource_set = checked(resources, "contained_resources")
    field_set = checked(fields, "contained_fields")
    if any(item not in _RESOURCE_PERMISSION for item in resource_set):
        raise ProvenanceError("制品包含未知业务资源")
    if any(item not in _FIELD_PERMISSION for item in field_set):
        raise ProvenanceError("制品包含未知字段组")
    required = {
        *(_RESOURCE_PERMISSION[item] for item in resource_set),
        *(_FIELD_PERMISSION[item] for item in field_set),
    }
    return tuple(sorted(resource_set)), tuple(sorted(field_set)), tuple(sorted(required))


def derive_sensitivity(required_permissions: Iterable[str]) -> str:
    levels = [
        permissions.PERMISSION_META.get(key, {}).get("sensitivity")
        for key in required_permissions
    ]
    known = [level for level in levels if level in _SENSITIVITY_RANK]
    return max(known, key=_SENSITIVITY_RANK.__getitem__) if known else "high"


def validate_source_artifact_ids(source_ids: Any) -> list[str]:
    """Reject source fanout/duplicates before authorization, DB reads or sealing work."""
    if not isinstance(source_ids, list) or not source_ids:
        raise ProvenanceError("派生制品至少需要一个来源 Artifact")
    if len(source_ids) > MAX_SOURCE_SNAPSHOTS:
        raise ProvenanceError("派生制品来源超过 fanout 预算")
    checked: list[str] = []
    for source_id in source_ids:
        try:
            canonical = str(uuid.UUID(str(source_id)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ProvenanceError("来源 Artifact 标识无效") from exc
        if not isinstance(source_id, str) or canonical != source_id:
            raise ProvenanceError("来源 Artifact 标识无效")
        checked.append(canonical)
    if len(checked) != len(set(checked)):
        raise ProvenanceError("同层不能重复引用同一来源 Artifact")
    return checked


def mint_server_evidence(
    *,
    owner_sub: str,
    content_fingerprint_value: str,
    contained_resources: Iterable[str],
    contained_fields: Iterable[str],
    row_subject: str | None,
    own_customers_only: bool,
) -> TrustedEvidence:
    """Test-only root evidence until #224 provides independent Query Evidence.

    Production callers must bind a separately issued source envelope with
    :func:`bind_source_envelopes`; renderer rows are not a source of truth.
    """
    from app.config import get_settings

    if get_settings().environment == "prod":
        raise ProvenanceError("生产环境必须使用独立 Query Evidence 来源")
    subject = str(owner_sub or "").strip()
    if not subject:
        raise ProvenanceError("来源证明缺少实名 owner")
    if len(content_fingerprint_value) != 64:
        raise ProvenanceError("来源内容指纹无效")
    resources, fields, required = _positive_permissions(
        contained_resources, contained_fields
    )
    if own_customers_only:
        if not row_subject:
            raise ProvenanceError("本人客户来源证明缺少行级主体")
        condition_op = "row_subject_or_all"
    else:
        row_subject = None
        condition_op = "all_rows"
    condition: dict[str, Any] = {"op": condition_op}
    if condition_op == "row_subject_or_all":
        condition["subject"] = row_subject
    source_ref = str(uuid.uuid4())
    payload = {
        "source_ref": source_ref,
        "source_kind": "internal_test",
        "source_artifact_id": None,
        # Deliberately independent from renderer/output binding. This placeholder can
        # never be verified in production and is not Query Evidence.
        "source_sha256": hashlib.sha256(source_ref.encode("ascii")).hexdigest(),
        "owner_sub": subject,
        "required_positive_keys": list(required),
        "contained_resources": list(resources),
        "contained_fields": list(fields),
        "sensitivity": derive_sensitivity(required),
        "row_subject": row_subject,
        "predicate_version": ROW_PREDICATE_VERSION,
        "condition": condition,
        "classification": "business_content",
        "proof_version": INTERNAL_TEST_PROOF_VERSION,
    }
    try:
        envelope = agent_integrity.seal(
            payload,
            purpose=SOURCE_SNAPSHOT_PURPOSE,
            payload_schema_version=SOURCE_SNAPSHOT_SCHEMA_VERSION,
            keyring=agent_integrity.configured_keyring(),
        )
    except agent_integrity.IntegrityError as exc:
        raise ProvenanceError("来源证明无法使用统一完整性 Envelope 封存") from exc
    return TrustedEvidence(
        owner_sub=subject,
        content_fingerprint=content_fingerprint_value,
        source_envelopes_json=(json.dumps(envelope, ensure_ascii=False),),
        _seal=_EVIDENCE_SEAL,
    )


def bind_source_envelopes(
    *,
    owner_sub: str,
    content_fingerprint_value: str,
    source_envelopes: list[dict[str, Any]],
) -> TrustedEvidence:
    """Bind independently issued source evidence to one exact renderer input."""
    subject = str(owner_sub or "").strip()
    if not subject or len(content_fingerprint_value) != 64:
        raise ProvenanceError("来源证明缺少 owner 或渲染指纹")
    scope = _aggregate_envelopes(source_envelopes)
    payloads = [_verify_snapshot(envelope) for envelope in source_envelopes]
    if any(payload["owner_sub"] != subject for payload in payloads):
        raise ProvenanceError("独立来源证明 owner 不一致")
    if scope["classification"] == "unclassified":
        raise ProvenanceError("独立来源证明未分类")
    return TrustedEvidence(
        owner_sub=subject,
        content_fingerprint=content_fingerprint_value,
        source_envelopes_json=tuple(
            json.dumps(envelope, ensure_ascii=False) for envelope in source_envelopes
        ),
        _seal=_EVIDENCE_SEAL,
    )


def mint_artifact_evidence(
    *,
    owner_sub: str,
    content_fingerprint_value: str,
    source_metas: list[dict[str, Any]],
) -> TrustedEvidence:
    """Seal immutable Artifact references supplied by a trusted transform adapter."""
    subject = str(owner_sub or "").strip()
    if not subject or not isinstance(source_metas, list) or not source_metas:
        raise ProvenanceError("派生制品缺少实名 owner 或来源 Artifact")
    if len(content_fingerprint_value) != 64:
        raise ProvenanceError("派生制品内容指纹无效")
    validate_source_artifact_ids([
        meta.get("file_id") if isinstance(meta, dict) else None
        for meta in source_metas
    ])
    envelopes: list[str] = []
    for meta in source_metas:
        scope = meta.get("access_scope")
        artifact_id = meta.get("file_id")
        sha256 = meta.get("sha256")
        if (
            meta.get("status") != "ready"
            or meta.get("owner_sub") != subject
            or not isinstance(artifact_id, str)
            or not isinstance(sha256, str)
        ):
            raise ProvenanceError("来源 Artifact 状态、owner 或 hash 无效")
        identity_only = _identity_scope_matches(scope, sha256)
        if identity_only:
            required_permissions: list[str] = []
            contained_resources: list[str] = []
            contained_fields: list[str] = []
            sensitivity = "low"
            row_subject = None
            predicate_version = "identity-top/v1"
            condition = {"op": "top"}
            classification = "identity_only"
            proof_version = IDENTITY_CLASSIFIER_VERSION
        else:
            if (
                isinstance(scope, dict)
                and scope.get("classification") == "identity_only"
            ):
                raise ProvenanceError("identity_only 模板证明无效")
            if not isinstance(scope, dict) or scope.get("policy") != "provenance_guarded":
                raise ProvenanceError("来源 Artifact containment 未分类")
            try:
                if _aggregate_envelopes(scope["source_access_snapshots"]) != scope:
                    raise ProvenanceError("来源 Artifact scope 不可验证")
            except (KeyError, TypeError, ProvenanceError) as exc:
                raise ProvenanceError("来源 Artifact scope 不可验证") from exc
            required_permissions = list(scope["required_permissions"])
            contained_resources = list(scope["contained_resources"])
            contained_fields = list(scope["contained_fields"])
            sensitivity = scope["sensitivity"]
            row_subject = None
            predicate_version = ARTIFACT_PREDICATE_VERSION
            condition = {"op": "source_artifact_live_scope"}
            classification = scope["classification"]
            proof_version = ARTIFACT_SNAPSHOT_PROOF_VERSION
        payload = {
            "source_ref": str(uuid.uuid4()),
            "source_kind": "artifact",
            "source_artifact_id": artifact_id,
            "source_sha256": sha256,
            "owner_sub": subject,
            "required_positive_keys": required_permissions,
            "contained_resources": contained_resources,
            "contained_fields": contained_fields,
            "sensitivity": sensitivity,
            "row_subject": row_subject,
            "predicate_version": predicate_version,
            "condition": condition,
            "classification": classification,
            "proof_version": proof_version,
        }
        try:
            envelope = agent_integrity.seal(
                payload,
                purpose=SOURCE_SNAPSHOT_PURPOSE,
                payload_schema_version=SOURCE_SNAPSHOT_SCHEMA_VERSION,
                keyring=agent_integrity.configured_keyring(),
            )
        except agent_integrity.IntegrityError as exc:
            raise ProvenanceError("来源 Artifact 快照无法封存") from exc
        envelopes.append(json.dumps(envelope, ensure_ascii=False))
    return TrustedEvidence(
        owner_sub=subject,
        content_fingerprint=content_fingerprint_value,
        source_envelopes_json=tuple(envelopes),
        _seal=_EVIDENCE_SEAL,
    )


_SNAPSHOT_KEYS = {
    "source_ref",
    "source_kind",
    "source_artifact_id",
    "source_sha256",
    "owner_sub",
    "required_positive_keys",
    "contained_resources",
    "contained_fields",
    "sensitivity",
    "row_subject",
    "predicate_version",
    "condition",
    "classification",
    "proof_version",
}


def _verify_snapshot(envelope: Any) -> dict[str, Any]:
    try:
        payload = agent_integrity.verify(
            envelope,
            allowed_purposes={SOURCE_SNAPSHOT_PURPOSE},
            allowed_payload_schemas={SOURCE_SNAPSHOT_SCHEMA_VERSION},
            keyring=agent_integrity.configured_keyring(),
        )
    except agent_integrity.IntegrityError as exc:
        raise ProvenanceError("来源快照完整性验证失败") from exc
    if set(payload) != _SNAPSHOT_KEYS:
        raise ProvenanceError("来源快照 schema 不完整")
    if (
        not isinstance(payload["source_ref"], str)
        or not 0 < len(payload["source_ref"]) <= MAX_SCOPE_STRING
        or payload["source_kind"] not in {"server_query", "artifact", "internal_test"}
        or not isinstance(payload["owner_sub"], str)
        or not 0 < len(payload["owner_sub"]) <= 64
        or not isinstance(payload["source_sha256"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", payload["source_sha256"])
        or payload["sensitivity"] not in _SENSITIVITY_RANK
        or payload["classification"] not in {"business_content", "identity_only"}
        or not isinstance(payload["proof_version"], str)
        or not 0 < len(payload["proof_version"]) <= MAX_SCOPE_STRING
    ):
        raise ProvenanceError("来源快照字段无效")
    if payload["source_kind"] == "artifact":
        try:
            if str(uuid.UUID(str(payload["source_artifact_id"]))) != payload["source_artifact_id"]:
                raise ValueError
        except (ValueError, AttributeError, TypeError) as exc:
            raise ProvenanceError("来源 Artifact 标识无效") from exc
    elif payload["source_artifact_id"] is not None:
        raise ProvenanceError("非 Artifact 来源不能伪造 Artifact 标识")
    resources, fields, required = _positive_permissions(
        payload["contained_resources"], payload["contained_fields"]
    )
    positive = payload["required_positive_keys"]
    if (
        not isinstance(positive, list)
        or len(positive) > MAX_SCOPE_ITEMS * 2
        or not all(
            isinstance(key, str) and 0 < len(key) <= MAX_SCOPE_STRING
            for key in positive
        )
    ):
        raise ProvenanceError("来源快照 positive key 类型或数量无效")
    if (
        payload["contained_resources"] != list(resources)
        or payload["contained_fields"] != list(fields)
        or payload["required_positive_keys"] != list(required)
    ):
        raise ProvenanceError("来源快照 positive key 或 containment 不一致")
    expected_sensitivity = (
        "low" if payload["classification"] == "identity_only"
        else derive_sensitivity(required)
    )
    if payload["sensitivity"] != expected_sensitivity:
        raise ProvenanceError("来源快照 sensitivity 被降低或不一致")
    condition = payload["condition"]
    if not isinstance(condition, dict):
        raise ProvenanceError("来源快照 predicate condition 无效")
    if payload["classification"] == "identity_only":
        if (
            payload["source_kind"] != "artifact"
            or resources
            or fields
            or required
            or payload["predicate_version"] != "identity-top/v1"
            or condition != {"op": "top"}
            or payload["row_subject"] is not None
            or payload["proof_version"] != IDENTITY_CLASSIFIER_VERSION
        ):
            raise ProvenanceError("identity_only 来源不能贡献业务 scope")
    elif payload["source_kind"] == "artifact":
        if (
            payload["predicate_version"] != ARTIFACT_PREDICATE_VERSION
            or condition != {"op": "source_artifact_live_scope"}
            or payload["row_subject"] is not None
            or payload["proof_version"] != ARTIFACT_SNAPSHOT_PROOF_VERSION
        ):
            raise ProvenanceError("来源 Artifact predicate 或 proof 版本未知")
    else:
        if payload["predicate_version"] != ROW_PREDICATE_VERSION:
            raise ProvenanceError("来源 predicate 版本未知")
        row_subject = payload["row_subject"]
        condition = payload["condition"]
        if condition == {"op": "all_rows"}:
            if row_subject is not None:
                raise ProvenanceError("全量来源不能绑定 row subject")
        elif (
            isinstance(condition, dict)
            and set(condition) == {"op", "subject"}
            and condition.get("op") == "row_subject_or_all"
        ):
            condition_subject = condition.get("subject")
            if (
                not isinstance(condition_subject, str)
                or not 0 < len(condition_subject) <= MAX_SCOPE_STRING
                or row_subject != condition_subject
                or _canonical_subject(condition_subject) != condition_subject
            ):
                raise ProvenanceError("来源 row subject condition 不一致")
        else:
            raise ProvenanceError("来源 predicate condition 未注册")
    if (
        payload["source_kind"] == "server_query"
        and payload["proof_version"] != SERVER_QUERY_PROOF_VERSION
    ):
        raise ProvenanceError("Query Evidence proof 版本未知")
    if payload["source_kind"] == "internal_test":
        from app.config import get_settings

        if (
            get_settings().environment == "prod"
            or payload["proof_version"] != INTERNAL_TEST_PROOF_VERSION
        ):
            raise ProvenanceError("测试来源不能在生产环境授权")
    return payload


def _aggregate_envelopes(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(envelopes, list) or not envelopes:
        raise ProvenanceError("生成制品至少需要一个独立来源快照")
    if len(envelopes) > MAX_SOURCE_SNAPSHOTS:
        raise ProvenanceError("生成制品来源快照超过 fanout 预算")
    # This is only an early reject over untrusted structure; authenticated schema
    # validation below remains authoritative.  It prevents duplicate Artifact IDs from
    # forcing repeated HMAC/schema work before an outcome that can never be accepted.
    early_artifact_ids: list[str] = []
    for envelope in envelopes:
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        source_id = payload.get("source_artifact_id") if isinstance(payload, dict) else None
        if isinstance(source_id, str):
            early_artifact_ids.append(source_id)
    if len(early_artifact_ids) != len(set(early_artifact_ids)):
        raise ProvenanceError("同层不能重复引用同一来源 Artifact")
    total_bytes = 0
    for envelope in envelopes:
        try:
            total_bytes += len(agent_integrity.canonicalize(envelope))
        except agent_integrity.IntegrityError as exc:
            raise ProvenanceError("生成制品来源快照编码无效") from exc
        if total_bytes > MAX_SOURCE_SCOPE_BYTES:
            # Stop at the first over-budget envelope; never canonicalize the rest of
            # the fanout after the aggregate outcome is already a deterministic deny.
            raise ProvenanceError("生成制品来源快照超过字节预算")
    payloads = [_verify_snapshot(envelope) for envelope in envelopes]
    source_refs = [payload["source_ref"] for payload in payloads]
    if len(source_refs) != len(set(source_refs)):
        raise ProvenanceError("生成制品包含重复来源快照")
    artifact_ids = [
        payload["source_artifact_id"]
        for payload in payloads
        if payload["source_kind"] == "artifact"
    ]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ProvenanceError("同层不能重复引用同一来源 Artifact")
    owners = {payload["owner_sub"] for payload in payloads}
    if len(owners) != 1:
        raise ProvenanceError("生成制品来源 owner 不一致")
    resources = sorted({
        item for payload in payloads for item in payload["contained_resources"]
    })
    fields = sorted({
        item for payload in payloads for item in payload["contained_fields"]
    })
    required = sorted({
        item for payload in payloads for item in payload["required_positive_keys"]
    })
    sensitivity = max(
        (payload["sensitivity"] for payload in payloads),
        key=sensitivity_rank,
    )
    return {
        "schema_version": ACCESS_SCHEMA_VERSION,
        "policy": "provenance_guarded",
        "classification": (
            "business_content"
            if any(payload["classification"] == "business_content" for payload in payloads)
            else "identity_only"
        ),
        "proof_version": SOURCE_UNION_PROOF_VERSION,
        "required_permissions": required,
        "contained_resources": resources,
        "contained_fields": fields,
        "sensitivity": sensitivity,
        "row_subject": None,
        "predicate_version": SOURCE_SET_PREDICATE_VERSION,
        "condition": {"op": "all_sources"},
        "source_access_snapshots": envelopes,
    }


def consume_evidence(
    evidence: Any,
    *,
    owner_sub: str,
    expected_fingerprint: str,
) -> dict[str, Any]:
    if (
        not isinstance(evidence, TrustedEvidence)
        or evidence._seal is not _EVIDENCE_SEAL
        or evidence.owner_sub != owner_sub
        or evidence.content_fingerprint != expected_fingerprint
    ):
        raise ProvenanceError("来源证明与 owner 或输出内容不匹配")
    try:
        envelopes = [json.loads(item) for item in evidence.source_envelopes_json]
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProvenanceError("来源证明 Envelope 编码无效") from exc
    scope = _aggregate_envelopes(envelopes)
    if any(_verify_snapshot(item)["owner_sub"] != owner_sub for item in envelopes):
        raise ProvenanceError("来源证明 owner 不一致")
    return scope


def _canonical_subject(value: Any) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    return normalized or None


def _condition_holds(payload: dict[str, Any], user_ctx: Any, current: dict[str, bool]) -> bool:
    if payload["classification"] == "identity_only":
        return payload["condition"] == {"op": "top"}
    if payload["source_kind"] == "artifact":
        return payload["condition"] == {"op": "source_artifact_live_scope"}
    condition = payload["condition"]
    current_own = bool(current.get("own_customers_only", False))
    if condition == {"op": "all_rows"}:
        return not current_own and payload["row_subject"] is None
    if condition.get("op") == "row_subject_or_all" and set(condition) == {"op", "subject"}:
        subject = condition.get("subject")
        if not isinstance(subject, str) or not subject or payload["row_subject"] != subject:
            return False
        return not current_own or _canonical_subject(user_ctx.salesperson_name) == subject
    return False


def current_scope_covers(
    scope: dict[str, Any],
    user_ctx: Any,
    *,
    source_artifact_authorizer: Any | None = None,
) -> bool:
    """Evaluate a validated v2 aggregate scope against the current account facts."""
    if scope.get("schema_version") != ACCESS_SCHEMA_VERSION:
        return False
    try:
        envelopes = scope.get("source_access_snapshots")
        if not isinstance(envelopes, list):
            return False
        recomputed = _aggregate_envelopes(envelopes)
    except ProvenanceError:
        return False
    if recomputed != scope:
        return False
    payloads = []
    try:
        payloads = [_verify_snapshot(envelope) for envelope in envelopes]
    except ProvenanceError:
        return False
    owner_sub = str(getattr(user_ctx, "user_id", None) or "").strip()
    if not owner_sub or any(payload["owner_sub"] != owner_sub for payload in payloads):
        return False
    current = permissions.runtime_safe(
        permissions.effective(user_ctx.role, None)
        if user_ctx.permissions is None
        else user_ctx.permissions
    )
    if any(not current.get(key, False) for key in scope["required_permissions"]):
        return False
    for payload in payloads:
        if not _condition_holds(payload, user_ctx, current):
            return False
        if payload["source_kind"] == "artifact":
            if source_artifact_authorizer is None or not source_artifact_authorizer(payload):
                return False
    return True


def sensitivity_rank(level: str) -> int:
    return _SENSITIVITY_RANK.get(level, _SENSITIVITY_RANK["critical"])


def source_artifact_ids(scope: dict[str, Any]) -> list[str]:
    """Return ordered Artifact contributors only after every snapshot verifies."""
    envelopes = scope.get("source_access_snapshots")
    if not isinstance(envelopes, list):
        raise ProvenanceError("制品缺少来源快照")
    payloads = [_verify_snapshot(envelope) for envelope in envelopes]
    return [
        payload["source_artifact_id"]
        for payload in payloads
        if payload["source_kind"] == "artifact"
    ]


def artifact_snapshot_matches_scope(
    payload: dict[str, Any],
    source_scope: dict[str, Any],
    *,
    source_sha256: str,
) -> bool:
    """Prevent a validly signed snapshot from underdeclaring its live source scope."""
    if payload.get("source_sha256") != source_sha256:
        return False
    if payload.get("classification") == "identity_only":
        return _identity_scope_matches(source_scope, source_sha256)
    if source_scope.get("policy") != "provenance_guarded":
        return False
    try:
        if _aggregate_envelopes(source_scope["source_access_snapshots"]) != source_scope:
            return False
    except (KeyError, TypeError, ProvenanceError):
        return False
    return (
        payload.get("classification") == source_scope.get("classification")
        and payload.get("required_positive_keys") == source_scope.get("required_permissions")
        and payload.get("contained_resources") == source_scope.get("contained_resources")
        and payload.get("contained_fields") == source_scope.get("contained_fields")
        and payload.get("sensitivity") == source_scope.get("sensitivity")
        and payload.get("predicate_version") == ARTIFACT_PREDICATE_VERSION
        and payload.get("condition") == {"op": "source_artifact_live_scope"}
        and payload.get("proof_version") == ARTIFACT_SNAPSHOT_PROOF_VERSION
    )


def known_field_groups() -> frozenset[str]:
    return frozenset(config.FIELD_GROUPS)
