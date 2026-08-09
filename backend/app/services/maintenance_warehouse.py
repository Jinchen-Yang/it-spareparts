"""Preview/apply/search/resolve workflow for maintenance warehouse documents."""

from __future__ import annotations

import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import hmac
import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import bindparam, column, false, func, or_, select, table, text
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart, PartAlias
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceSiteIssue
from app.models.system import SysUser
from app.models.maintenance_warehouse import (
    MaintenanceWarehouseAmbiguity,
    MaintenanceWarehouseAuditEvent,
    MaintenanceWarehouseDocument,
    MaintenanceWarehouseDocumentLine,
    MaintenanceWarehouseDocumentLink,
    MaintenanceWarehouseImportBatch,
)
from app.security import FULL_SCOPE_ROLES, UserContext
from app.services.maintenance_warehouse_adapters import (
    ParsedWarehouseWorkbook,
    WarehouseAmbiguityFact,
    parse_warehouse_workbook,
)


class MaintenanceWarehouseError(ValueError):
    pass


class MaintenanceWarehouseConflict(MaintenanceWarehouseError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, label))


def _safe_filename(filename: str) -> str:
    value = (filename or "warehouse.xlsx").replace("\\", "/").rsplit("/", 1)[-1]
    return (value.strip() or "warehouse.xlsx")[:256]


def _plan(parsed: ParsedWarehouseWorkbook) -> dict:
    ambiguity_counts = dict(sorted(Counter(item.code for item in parsed.ambiguities).items()))
    return {
        "source_file_hash": parsed.source_file_hash,
        "adapter_key": parsed.adapter_key,
        "adapter_version": parsed.adapter_version,
        "version_state": parsed.version_state,
        "header_signature": parsed.header_signature,
        "header_pairs": [
            {
                "position": pair.position,
                "internal_code": pair.internal_code,
                "business_label": pair.business_label,
            }
            for pair in parsed.header_pairs
        ],
        "header_diff": parsed.header_diff,
        "data_row_count": parsed.data_row_count,
        "document_count": len(parsed.documents),
        "line_count": sum(len(item.lines) for item in parsed.documents),
        "adapter_ambiguity_counts": ambiguity_counts,
    }


def _import_id(parsed: ParsedWarehouseWorkbook) -> str:
    return _uuid(
        f"maintenance-warehouse-import:{parsed.source_file_hash}:{parsed.adapter_version}"
    )


def _preview_token(parsed: ParsedWarehouseWorkbook, hmac_key: bytes) -> str:
    if len(hmac_key) < 16:
        raise MaintenanceWarehouseError("服务端预览签名密钥配置无效")
    digest = hmac.new(hmac_key, _canonical(_plan(parsed)), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def preview_import(content: bytes, *, filename: str, hmac_key: bytes) -> dict:
    """Pure preview: parsing and signing only, with no Session and no writes."""

    parsed = parse_warehouse_workbook(content)
    plan = _plan(parsed)
    return {
        "import_id": _import_id(parsed),
        "preview_token": _preview_token(parsed, hmac_key),
        "filename": _safe_filename(filename),
        **plan,
        "can_apply": parsed.version_state == "known",
    }


def _advisory_key(identity: str) -> int:
    raw = hashlib.sha256(identity.encode("utf-8")).digest()[:8]
    return int.from_bytes(raw, byteorder="big", signed=True)


def _lock_business_identities(db: Session, parsed: ParsedWarehouseWorkbook) -> None:
    """Serialize different files that carry the same immutable business ID."""

    identities = sorted({
        f"maintenance-warehouse:{document.document_type}:{document.source_document_id}"
        for document in parsed.documents
    })
    if not identities:
        identities = [
            f"maintenance-warehouse:{parsed.adapter_version}:{parsed.source_file_hash}"
        ]
    for identity in identities:
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_key(identity)},
        )


def _link_id(
    document_id: str,
    line_id: str | None,
    kind: str,
    target_type: str,
    target_id: str,
    *,
    version: int = 1,
) -> str:
    return _uuid(
        f"maintenance-warehouse-link:{document_id}:{line_id or '-'}:{kind}:"
        f"{target_type}:{target_id}:v{version}"
    )


def _document_id(document_type: str, source_document_id: str) -> str:
    return _uuid(f"maintenance-warehouse-document:{document_type}:{source_document_id}")


def _line_id(document_type: str, source_document_id: str, source_line_id: str) -> str:
    return _uuid(
        f"maintenance-warehouse-line:{document_type}:{source_document_id}:{source_line_id}"
    )


def _candidate(target_type: str, target_id: str, label: str | None = None) -> dict:
    item = {"target_type": target_type, "target_id": str(target_id)}
    if label:
        item["label"] = label[:256]
    return item


def _chunks(values: set[str], size: int = 1_000):
    ordered = sorted(values)
    for offset in range(0, len(ordered), size):
        yield ordered[offset:offset + size]


def _maintenance_order_candidate_map(
    db: Session, stable_refs: set[str]
) -> dict[str, list[dict]]:
    output = {value: [] for value in stable_refs}
    for chunk in _chunks(stable_refs):
        rows = db.scalars(
            select(FMaintenanceOrder)
            .where(or_(
                FMaintenanceOrder.raw_order_id.in_(chunk),
                FMaintenanceOrder.order_no.in_(chunk),
            ))
            .order_by(FMaintenanceOrder.id)
        ).all()
        for row in rows:
            candidate = _candidate("maintenance_order", row.raw_order_id, row.order_no)
            for value in {row.raw_order_id, row.order_no} & stable_refs:
                output[value].append(candidate)
    return output


def _part_candidate_map(db: Session, pns: set[str]) -> dict[str, list[dict]]:
    output: dict[str, dict[int, dict]] = {value: {} for value in pns}
    for chunk in _chunks(pns):
        direct = db.scalars(
            select(DimPart).where(DimPart.pn_std.in_(chunk), DimPart.status == "active")
        ).all()
        for part in direct:
            output[part.pn_std][part.id] = _candidate("dim_part", str(part.id), part.pn_std)
        aliases = db.execute(
            select(PartAlias, DimPart)
            .join(DimPart, DimPart.id == PartAlias.part_id)
            .where(
                PartAlias.pn_raw.in_(chunk),
                PartAlias.status == "active",
                DimPart.status == "active",
            )
        ).all()
        for alias, part in aliases:
            output[alias.pn_raw][part.id] = _candidate("dim_part", str(part.id), part.pn_std)
    return {
        pn: [candidates[key] for key in sorted(candidates)][:3]
        for pn, candidates in output.items()
    }


def _warehouse_document_candidate_map(
    db: Session, stable_refs: set[str]
) -> dict[str, list[dict]]:
    output = {value: [] for value in stable_refs}
    for chunk in _chunks(stable_refs):
        rows = db.scalars(
            select(MaintenanceWarehouseDocument)
            .where(or_(
                MaintenanceWarehouseDocument.source_document_id.in_(chunk),
                MaintenanceWarehouseDocument.document_no.in_(chunk),
            ))
            .order_by(
                MaintenanceWarehouseDocument.created_at,
                MaintenanceWarehouseDocument.document_id,
            )
        ).all()
        for row in rows:
            candidate = _candidate("warehouse_document", row.document_id, row.document_no)
            for value in {row.source_document_id, row.document_no} & stable_refs:
                output[str(value)].append(candidate)
    return output


def _table_has_columns(db: Session, table_name: str, columns: set[str]) -> bool:
    """Check optional sibling-branch contracts without importing absent models."""

    if db.scalar(text("SELECT to_regclass(:name)"), {"name": table_name}) is None:
        return False
    found = set(db.scalars(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :name"
        ),
        {"name": table_name},
    ))
    return columns <= found


def _project_candidate_map(
    db: Session,
    *,
    maintenance_candidates: dict[str, list[dict]],
) -> tuple[dict[str, list[dict]], bool]:
    """Consume #201's single active WBDD assignment; never recreate it."""

    output = {reference: [] for reference in maintenance_candidates}
    required = {"source_order_id", "project_id", "is_active"}
    if not _table_has_columns(db, "maintenance_source_order_assignment", required):
        return output, False
    source_ids = {
        candidate["target_id"]
        for candidates in maintenance_candidates.values()
        for candidate in candidates
        if candidate.get("target_type") == "maintenance_order"
    }
    if not source_ids:
        return output, True
    statement = text(
        "SELECT assignment.source_order_id, assignment.project_id, project.project_code "
        "FROM maintenance_source_order_assignment AS assignment "
        "JOIN maintenance_project AS project "
        "  ON project.project_id = assignment.project_id "
        "WHERE assignment.is_active IS TRUE "
        "  AND project.is_active IS TRUE "
        "  AND assignment.source_order_id IN :source_ids "
        "ORDER BY assignment.source_order_id, assignment.project_id"
    ).bindparams(bindparam("source_ids", expanding=True))
    assigned: dict[str, list[dict]] = {}
    for source_order_id, project_id, project_code in db.execute(
        statement, {"source_ids": sorted(source_ids)}
    ):
        assigned.setdefault(str(source_order_id), []).append(
            _candidate("maintenance_project", str(project_id), project_code)
        )
    for reference, candidates in maintenance_candidates.items():
        if len(candidates) != 1:
            continue
        source_order_id = str(candidates[0]["target_id"])
        output[reference] = assigned.get(source_order_id, [])
    return output, True


def _site_issue_candidate_map(
    db: Session,
    *,
    source_order_ids: set[str],
    source_line_ids: set[str],
) -> tuple[dict[tuple[str, str], list[dict]], bool]:
    """Consume explicit site issue line source IDs from #207 after integration."""

    output: dict[tuple[str, str], list[dict]] = {}
    required_line = {"source_order_id", "source_line_id", "serial_number", "issue_id"}
    required_issue = {"issue_id", "issue_no", "project_id", "normalized_status"}
    if not (
        _table_has_columns(db, "maintenance_site_issue_line", required_line)
        and _table_has_columns(db, "maintenance_site_issue", required_issue)
    ):
        return output, False
    if not source_order_ids or not source_line_ids:
        return output, True
    statement = text(
        "SELECT line.source_order_id, line.source_line_id, line.serial_number, "
        "       issue.issue_id, issue.issue_no, issue.project_id "
        "FROM maintenance_site_issue_line AS line "
        "JOIN maintenance_site_issue AS issue ON issue.issue_id = line.issue_id "
        "WHERE line.source_order_id IN :source_order_ids "
        "  AND line.source_line_id IN :source_line_ids "
        "  AND issue.normalized_status IN ('confirmed', 'corrected') "
        "ORDER BY line.source_order_id, line.source_line_id, issue.issue_id"
    ).bindparams(
        bindparam("source_order_ids", expanding=True),
        bindparam("source_line_ids", expanding=True),
    )
    for source_order_id, source_line_id, serial_number, issue_id, issue_no, project_id in db.execute(
        statement,
        {
            "source_order_ids": sorted(source_order_ids),
            "source_line_ids": sorted(source_line_ids),
        },
    ):
        output.setdefault((str(source_order_id), str(source_line_id)), []).append({
            **_candidate("maintenance_site_issue", str(issue_id), issue_no),
            "project_id": str(project_id),
            "serial_number": str(serial_number) if serial_number is not None else None,
        })
    return output, True


def _bad_return_candidate_map(
    db: Session,
    references: set[str],
) -> tuple[dict[str, list[dict]], bool]:
    """Match only #208's explicit warehouse/inbound reference columns."""

    output = {reference: [] for reference in references}
    required = {
        "return_id", "return_no", "project_id", "status",
        "warehouse_reference", "inbound_reference",
    }
    if not _table_has_columns(db, "maintenance_bad_return", required):
        return output, False
    if not references:
        return output, True
    statement = text(
        "SELECT return_id, return_no, project_id, warehouse_reference, inbound_reference "
        "FROM maintenance_bad_return "
        "WHERE status <> 'void' AND ("
        "  warehouse_reference IN :references OR inbound_reference IN :references"
        ") ORDER BY return_id"
    ).bindparams(bindparam("references", expanding=True))
    for return_id, return_no, project_id, warehouse_reference, inbound_reference in db.execute(
        statement, {"references": sorted(references)}
    ):
        candidate = {
            **_candidate("maintenance_bad_return", str(return_id), return_no),
            "project_id": str(project_id),
        }
        for value in {warehouse_reference, inbound_reference} & references:
            output[str(value)].append(candidate)
    return output, True


def _ambiguity_fact(
    *,
    code: str,
    field_code: str | None = None,
    source_row: int | None = None,
    document_source_id: str | None = None,
    line_source_id: str | None = None,
    value_hash: str | None = None,
    candidates: list[dict] | tuple[dict, ...] = (),
) -> WarehouseAmbiguityFact:
    return WarehouseAmbiguityFact(
        code=code,
        field_code=field_code,
        source_row=source_row,
        document_source_id=document_source_id,
        line_source_id=line_source_id,
        value_hash=value_hash,
        candidate_refs=tuple(candidates),
    )


def _ambiguity_model(
    item: WarehouseAmbiguityFact,
    *,
    import_id: str,
    document_ids: dict[str, str],
    line_ids: dict[tuple[str, str], str],
) -> MaintenanceWarehouseAmbiguity:
    document_id = document_ids.get(item.document_source_id or "")
    line_id = line_ids.get((item.document_source_id or "", item.line_source_id or ""))
    candidates = sorted(
        [dict(candidate) for candidate in item.candidate_refs],
        key=lambda value: (value.get("target_type", ""), value.get("target_id", "")),
    )
    fingerprint = _hash({
        "type": item.code,
        "field": item.field_code,
        "row": item.source_row,
        "document": item.document_source_id,
        "line": item.line_source_id,
        "value_hash": item.value_hash,
        "candidates": candidates,
    })
    return MaintenanceWarehouseAmbiguity(
        ambiguity_id=_uuid(f"maintenance-warehouse-ambiguity:{import_id}:{fingerprint}"),
        import_id=import_id,
        document_id=document_id,
        line_id=line_id,
        ambiguity_type=item.code,
        field_code=item.field_code,
        source_row=item.source_row,
        value_hash=item.value_hash,
        candidates_json=candidates,
        fingerprint=fingerprint,
        status="open",
        version=1,
    )


def apply_import(
    db: Session,
    content: bytes,
    *,
    filename: str,
    import_id: str,
    preview_token: str,
    reason: str,
    operated_by: str,
    hmac_key: bytes,
) -> dict:
    """Atomically materialize immutable facts; caller owns commit/rollback."""

    reason = (reason or "").strip()
    if not reason or len(reason) > 1000:
        raise MaintenanceWarehouseError("导入理由无效")
    operated_by = (operated_by or "").strip()
    if not operated_by or len(operated_by) > 64:
        raise MaintenanceWarehouseError("实名操作人无效")
    parsed = parse_warehouse_workbook(content)
    expected_id = _import_id(parsed)
    expected_token = _preview_token(parsed, hmac_key)
    if import_id != expected_id or not hmac.compare_digest(preview_token or "", expected_token):
        raise MaintenanceWarehouseConflict("文件与预览签名不一致，请重新预览")
    if parsed.version_state != "known":
        raise MaintenanceWarehouseConflict(
            "模板完整双表头尚未获批准，只允许零写入预览"
        )

    _lock_business_identities(db, parsed)
    existing_batch = db.scalar(
        select(MaintenanceWarehouseImportBatch).where(
            MaintenanceWarehouseImportBatch.source_file_hash == parsed.source_file_hash,
            MaintenanceWarehouseImportBatch.adapter_version == parsed.adapter_version,
        )
    )
    if existing_batch is not None:
        return {
            **dict(existing_batch.result_json),
            "import_id": existing_batch.import_id,
            "idempotent_replay": True,
            "writes": {"documents": 0, "lines": 0, "links": 0, "ambiguities": 0, "audits": 0},
        }

    import_id = expected_id
    document_ids: dict[str, str] = {}
    line_ids: dict[tuple[str, str], str] = {}
    new_documents: list[MaintenanceWarehouseDocument] = []
    new_lines: list[MaintenanceWarehouseDocumentLine] = []
    new_links: list[MaintenanceWarehouseDocumentLink] = []
    ambiguity_facts: list[WarehouseAmbiguityFact] = list(parsed.ambiguities)
    planned_link_slots: set[tuple[str, str | None, str]] = set()

    existing_documents: dict[tuple[str, str], MaintenanceWarehouseDocument] = {}
    document_types = {document.document_type for document in parsed.documents}
    document_sources = {document.source_document_id for document in parsed.documents}
    for chunk in _chunks(document_sources):
        rows = db.scalars(
            select(MaintenanceWarehouseDocument).where(
                MaintenanceWarehouseDocument.document_type.in_(document_types),
                MaintenanceWarehouseDocument.source_document_id.in_(chunk),
            )
        ).all()
        existing_documents.update({
            (row.document_type, row.source_document_id): row for row in rows
        })

    for document in parsed.documents:
        deterministic_document_id = _document_id(document.document_type, document.source_document_id)
        existing_document = existing_documents.get(
            (document.document_type, document.source_document_id)
        )
        if existing_document is not None:
            document_ids[document.source_document_id] = existing_document.document_id
            if existing_document.raw_fingerprint != document.raw_fingerprint:
                ambiguity_facts.append(_ambiguity_fact(
                    code="field_conflict", field_code="document_header",
                    document_source_id=document.source_document_id,
                ))
        else:
            document_ids[document.source_document_id] = deterministic_document_id
            new_documents.append(MaintenanceWarehouseDocument(
                document_id=deterministic_document_id,
                document_type=document.document_type,
                source_document_id=document.source_document_id,
                document_no=document.document_no,
                document_date=document.document_date,
                raw_status=document.raw_status,
                normalized_status=document.normalized_status,
                raw_fields_json=document.raw_fields,
                raw_fingerprint=document.raw_fingerprint,
                first_import_id=import_id,
            ))

    existing_lines: dict[tuple[str, str], MaintenanceWarehouseDocumentLine] = {}
    existing_document_ids = {
        row.document_id for row in existing_documents.values()
    }
    for chunk in _chunks(existing_document_ids):
        rows = db.scalars(
            select(MaintenanceWarehouseDocumentLine).where(
                MaintenanceWarehouseDocumentLine.document_id.in_(chunk)
            )
        ).all()
        existing_lines.update({
            (row.document_id, row.source_line_id): row for row in rows
        })

    for document in parsed.documents:
        for line in document.lines:
            deterministic_line_id = _line_id(
                document.document_type, document.source_document_id, line.source_line_id
            )
            doc_id = document_ids[document.source_document_id]
            existing_line = existing_lines.get((doc_id, line.source_line_id))
            if existing_line is not None:
                line_ids[(document.source_document_id, line.source_line_id)] = existing_line.line_id
                if existing_line.raw_fingerprint != line.raw_fingerprint:
                    ambiguity_facts.append(_ambiguity_fact(
                        code="field_conflict", field_code="document_line",
                        source_row=line.source_row,
                        document_source_id=document.source_document_id,
                        line_source_id=line.source_line_id,
                    ))
            else:
                line_ids[(document.source_document_id, line.source_line_id)] = deterministic_line_id
                new_lines.append(MaintenanceWarehouseDocumentLine(
                    line_id=deterministic_line_id,
                    document_id=doc_id,
                    source_line_id=line.source_line_id,
                    line_no=line.line_no,
                    pn=line.pn,
                    sn=line.sn,
                    self_code=line.self_code,
                    quantity=line.quantity,
                    raw_fields_json=line.raw_fields,
                    raw_fingerprint=line.raw_fingerprint,
                    first_import_id=import_id,
                ))

    # A stable identity whose payload changed is evidence of a conflict, not a
    # new relationship source.  Preserve the original immutable fact and make
    # the entire conflicted document/line fail closed until a person reviews
    # the ambiguity.  In particular, never let a changed PN append a second
    # automatic part link to the old stable line.
    conflicted_documents = {
        fact.document_source_id
        for fact in ambiguity_facts
        if fact.code == "field_conflict"
        and fact.field_code == "document_header"
        and fact.document_source_id
    }
    conflicted_lines = {
        (fact.document_source_id, fact.line_source_id)
        for fact in ambiguity_facts
        if fact.code == "field_conflict"
        and fact.field_code == "document_line"
        and fact.document_source_id
        and fact.line_source_id
    }

    existing_active_links: dict[
        tuple[str, str | None, str], MaintenanceWarehouseDocumentLink
    ] = {}
    for chunk in _chunks(set(document_ids.values())):
        rows = db.scalars(
            select(MaintenanceWarehouseDocumentLink).where(
                MaintenanceWarehouseDocumentLink.document_id.in_(chunk),
                MaintenanceWarehouseDocumentLink.status == "active",
            )
        ).all()
        existing_active_links.update({
            (row.document_id, row.line_id, row.link_kind): row for row in rows
        })

    maintenance_refs = {
        value for document in parsed.documents
        if (value := document.stable_refs.get("maintenance_order"))
    }
    upstream_refs = {
        value for document in parsed.documents
        if (value := document.stable_refs.get("upstream_document"))
    }
    part_refs = {
        line.pn for document in parsed.documents for line in document.lines if line.pn
    }
    maintenance_candidates = _maintenance_order_candidate_map(db, maintenance_refs)
    project_candidates, project_bridge_available = _project_candidate_map(
        db, maintenance_candidates=maintenance_candidates
    )
    upstream_candidates_from_db = _warehouse_document_candidate_map(db, upstream_refs)
    part_candidates = _part_candidate_map(db, part_refs)
    stable_order_ids = {
        str(candidate["target_id"])
        for candidates in maintenance_candidates.values()
        for candidate in candidates
        if candidate.get("target_type") == "maintenance_order"
    }
    stable_line_ids = {
        line.source_line_id
        for document in parsed.documents
        for line in document.lines
    }
    site_issue_candidates, site_issue_bridge_available = _site_issue_candidate_map(
        db,
        source_order_ids=stable_order_ids,
        source_line_ids=stable_line_ids,
    )
    warehouse_identity_refs = {
        str(value)
        for document in parsed.documents
        for value in (document.source_document_id, document.document_no)
        if value
    }
    bad_return_candidates, bad_return_bridge_available = _bad_return_candidate_map(
        db, warehouse_identity_refs
    )

    # Upstream/downstream documents can coexist in one uploaded workbook.  Add
    # those deterministic, stable-ID candidates to the DB candidates; never
    # fall back to dates, PN, display names, or row proximity.
    planned_document_candidates: dict[str, list[dict]] = {}
    for document in parsed.documents:
        candidate = _candidate(
            "warehouse_document",
            document_ids[document.source_document_id],
            document.document_no,
        )
        for stable_ref in {document.source_document_id, document.document_no} - {None, ""}:
            planned_document_candidates.setdefault(str(stable_ref), []).append(candidate)

    def plan_link(
        *, document_source_id: str, line_source_id: str | None, link_kind: str,
        stable_key_kind: str, stable_value: str, candidates: list[dict], field_code: str,
    ) -> None:
        document_id = document_ids[document_source_id]
        line_id = (
            line_ids.get((document_source_id, line_source_id)) if line_source_id else None
        )
        if not candidates:
            ambiguity_facts.append(_ambiguity_fact(
                code="missing_stable_link", field_code=field_code,
                document_source_id=document_source_id, line_source_id=line_source_id,
                value_hash=_hash(stable_value),
            ))
            return
        if len(candidates) != 1:
            ambiguity_facts.append(_ambiguity_fact(
                code="multiple_candidates", field_code=field_code,
                document_source_id=document_source_id, line_source_id=line_source_id,
                value_hash=_hash(stable_value), candidates=candidates,
            ))
            return
        target = candidates[0]
        target_type = target["target_type"]
        target_id = str(target["target_id"])
        slot = (document_id, line_id, link_kind)
        existing_link = existing_active_links.get(slot)
        if existing_link is not None:
            if (
                existing_link.target_type == target_type
                and existing_link.target_id == target_id
            ):
                return
            ambiguity_facts.append(_ambiguity_fact(
                code="field_conflict",
                field_code=field_code,
                document_source_id=document_source_id,
                line_source_id=line_source_id,
                value_hash=_hash(stable_value),
                candidates=[
                    _candidate(
                        existing_link.target_type,
                        existing_link.target_id,
                        "当前有效关联",
                    ),
                    target,
                ],
            ))
            return
        if slot in planned_link_slots:
            return
        planned_link_slots.add(slot)
        new_links.append(MaintenanceWarehouseDocumentLink(
            link_id=_link_id(document_id, line_id, link_kind, target_type, target_id),
            document_id=document_id,
            line_id=line_id,
            link_kind=link_kind,
            target_type=target_type,
            target_id=target_id,
            stable_key_kind=stable_key_kind,
            stable_key_hash=_hash(stable_value),
            source="automatic",
            status="active",
            supersedes_link_id=None,
            version=1,
            reason="系统按稳定键精确关联",
            operated_by=operated_by,
        ))

    for document in parsed.documents:
        if document.source_document_id in conflicted_documents:
            continue
        maintenance_ref = document.stable_refs.get("maintenance_order")
        if maintenance_ref:
            plan_link(
                document_source_id=document.source_document_id,
                line_source_id=None,
                link_kind="maintenance_order",
                stable_key_kind="wbdd_id_or_no",
                stable_value=maintenance_ref,
                candidates=maintenance_candidates.get(maintenance_ref, []),
                field_code="maintenance_order",
            )
            if project_bridge_available:
                plan_link(
                    document_source_id=document.source_document_id,
                    line_source_id=None,
                    link_kind="project",
                    stable_key_kind="active_source_order_assignment",
                    stable_value=maintenance_ref,
                    candidates=project_candidates.get(maintenance_ref, []),
                    field_code="project",
                )
            else:
                ambiguity_facts.append(_ambiguity_fact(
                    code="integration_blocker",
                    field_code="project_assignment_contract",
                    document_source_id=document.source_document_id,
                    value_hash=_hash(maintenance_ref),
                ))
        else:
            ambiguity_facts.append(_ambiguity_fact(
                code="missing_stable_link", field_code="maintenance_order",
                document_source_id=document.source_document_id,
            ))
        exact_project_candidates = (
            project_candidates.get(maintenance_ref, []) if maintenance_ref else []
        )
        exact_order_candidates = (
            maintenance_candidates.get(maintenance_ref, []) if maintenance_ref else []
        )
        project_id = (
            str(exact_project_candidates[0]["target_id"])
            if len(exact_project_candidates) == 1
            else None
        )
        source_order_id = (
            str(exact_order_candidates[0]["target_id"])
            if len(exact_order_candidates) == 1
            else None
        )
        upstream_ref = document.stable_refs.get("upstream_document")
        if upstream_ref:
            upstream_candidates = list(upstream_candidates_from_db.get(upstream_ref, []))
            upstream_candidates.extend(planned_document_candidates.get(upstream_ref, []))
            current_id = document_ids[document.source_document_id]
            upstream_candidates = list({
                (candidate["target_type"], candidate["target_id"]): candidate
                for candidate in upstream_candidates
                if candidate["target_id"] != current_id
            }.values())
            plan_link(
                document_source_id=document.source_document_id,
                line_source_id=None,
                link_kind="warehouse_document",
                stable_key_kind="warehouse_document_id_or_no",
                stable_value=upstream_ref,
                candidates=upstream_candidates,
                field_code="upstream_document",
            )
        for line in document.lines:
            if (document.source_document_id, line.source_line_id) in conflicted_lines:
                continue
            if line.pn:
                plan_link(
                    document_source_id=document.source_document_id,
                    line_source_id=line.source_line_id,
                    link_kind="part",
                    stable_key_kind="pn_exact",
                    stable_value=line.pn,
                    candidates=part_candidates.get(line.pn, []),
                    field_code="pn",
                )
            else:
                ambiguity_facts.append(_ambiguity_fact(
                    code="missing_stable_link", field_code="pn",
                    source_row=line.source_row,
                    document_source_id=document.source_document_id,
                    line_source_id=line.source_line_id,
                ))
            if document.document_type == "shipment":
                if not site_issue_bridge_available:
                    ambiguity_facts.append(_ambiguity_fact(
                        code="integration_blocker",
                        field_code="site_issue_source_contract",
                        source_row=line.source_row,
                        document_source_id=document.source_document_id,
                        line_source_id=line.source_line_id,
                    ))
                elif not source_order_id or not project_id:
                    ambiguity_facts.append(_ambiguity_fact(
                        code="integration_blocker",
                        field_code="site_issue_project_bridge",
                        source_row=line.source_row,
                        document_source_id=document.source_document_id,
                        line_source_id=line.source_line_id,
                    ))
                elif not line.sn:
                    ambiguity_facts.append(_ambiguity_fact(
                        code="missing_stable_link",
                        field_code="sn",
                        source_row=line.source_row,
                        document_source_id=document.source_document_id,
                        line_source_id=line.source_line_id,
                    ))
                else:
                    candidates = [
                        candidate
                        for candidate in site_issue_candidates.get(
                            (source_order_id, line.source_line_id), []
                        )
                        if candidate.get("project_id") == project_id
                        and candidate.get("serial_number") == line.sn
                    ]
                    plan_link(
                        document_source_id=document.source_document_id,
                        line_source_id=line.source_line_id,
                        link_kind="site_issue",
                        stable_key_kind="source_order_line_sn_exact",
                        stable_value=f"{source_order_id}:{line.source_line_id}:{line.sn}",
                        candidates=candidates,
                        field_code="site_issue",
                    )

        if document.document_type in {"return", "receipt"}:
            if not bad_return_bridge_available:
                ambiguity_facts.append(_ambiguity_fact(
                    code="integration_blocker",
                    field_code="bad_return_contract",
                    document_source_id=document.source_document_id,
                ))
            elif project_id:
                candidates_by_identity: dict[tuple[str, str], dict] = {}
                for reference in {
                    document.source_document_id,
                    document.document_no,
                } - {None, ""}:
                    for candidate in bad_return_candidates.get(str(reference), []):
                        if candidate.get("project_id") == project_id:
                            candidates_by_identity[
                                (candidate["target_type"], candidate["target_id"])
                            ] = candidate
                plan_link(
                    document_source_id=document.source_document_id,
                    line_source_id=None,
                    link_kind="bad_return",
                    stable_key_kind="explicit_warehouse_reference",
                    stable_value="|".join(sorted({
                        document.source_document_id,
                        document.document_no or "",
                    } - {""})),
                    candidates=list(candidates_by_identity.values()),
                    field_code="bad_return",
                )

    ambiguity_models: list[MaintenanceWarehouseAmbiguity] = []
    fingerprints: set[str] = set()
    for fact in ambiguity_facts:
        model = _ambiguity_model(
            fact, import_id=import_id, document_ids=document_ids, line_ids=line_ids
        )
        if model.fingerprint in fingerprints:
            continue
        fingerprints.add(model.fingerprint)
        ambiguity_models.append(model)

    result = {
        "import_id": import_id,
        "adapter_key": parsed.adapter_key,
        "adapter_version": parsed.adapter_version,
        "version_state": parsed.version_state,
        "source_file_hash": parsed.source_file_hash,
        "header_signature": parsed.header_signature,
        "header_diff": parsed.header_diff,
        "document_count": len(parsed.documents),
        "line_count": sum(len(document.lines) for document in parsed.documents),
        "ambiguity_count": len(ambiguity_models),
        "new_document_count": len(new_documents),
        "new_line_count": len(new_lines),
        "new_link_count": len(new_links),
        "idempotent_replay": False,
    }
    batch = MaintenanceWarehouseImportBatch(
        import_id=import_id,
        source_file_hash=parsed.source_file_hash,
        source_filename=_safe_filename(filename),
        adapter_key=parsed.adapter_key,
        adapter_version=parsed.adapter_version,
        version_state=parsed.version_state,
        header_signature=parsed.header_signature,
        header_pairs_json=[
            {
                "position": pair.position,
                "internal_code": pair.internal_code,
                "business_label": pair.business_label,
            }
            for pair in parsed.header_pairs
        ],
        status="applied",
        document_count=result["document_count"],
        line_count=result["line_count"],
        ambiguity_count=result["ambiguity_count"],
        result_json=result,
        reason=reason,
        applied_by=operated_by,
    )
    audit = MaintenanceWarehouseAuditEvent(
        event_id=_uuid(f"maintenance-warehouse-audit:import:{import_id}"),
        import_id=import_id,
        ambiguity_id=None,
        action="import_applied",
        before_json=None,
        after_json=result,
        reason=reason,
        operated_by=operated_by,
    )
    # No ORM relationships are declared on these deliberately small fact models.
    # Flush in FK order while remaining inside the caller's single transaction;
    # any later failure still rolls the complete apply back atomically.
    db.add(batch)
    db.flush([batch])
    db.add_all(new_documents)
    if new_documents:
        db.flush(new_documents)
    db.add_all(new_lines)
    if new_lines:
        db.flush(new_lines)
    db.add_all(new_links)
    db.add_all(ambiguity_models)
    if new_links or ambiguity_models:
        db.flush([*new_links, *ambiguity_models])
    db.add(audit)
    db.flush([audit])
    return {
        **result,
        "writes": {
            "documents": len(new_documents),
            "lines": len(new_lines),
            "links": len(new_links),
            "ambiguities": len(ambiguity_models),
            "audits": 1,
        },
    }


def _document_scope_condition(db: Session, user_ctx: UserContext):
    """#205-compatible project row scope; missing assignment contract means none."""

    if user_ctx.role in FULL_SCOPE_ROLES:
        return None
    if not user_ctx.is_authenticated or not user_ctx.user_id:
        return false()
    required = {"project_id", "responsibility_type", "user_id", "archived_at"}
    if not _table_has_columns(db, "maintenance_project_user_assignment", required):
        return false()
    assignment = table(
        "maintenance_project_user_assignment",
        column("project_id"),
        column("responsibility_type"),
        column("user_id"),
        column("archived_at"),
    )
    return select(MaintenanceWarehouseDocumentLink.link_id).select_from(
        MaintenanceWarehouseDocumentLink.__table__
        .join(
            assignment,
            assignment.c.project_id == MaintenanceWarehouseDocumentLink.target_id,
        )
        .join(SysUser.__table__, SysUser.id == assignment.c.user_id)
    ).where(
        MaintenanceWarehouseDocumentLink.document_id
        == MaintenanceWarehouseDocument.document_id,
        MaintenanceWarehouseDocumentLink.link_kind == "project",
        MaintenanceWarehouseDocumentLink.target_type == "maintenance_project",
        MaintenanceWarehouseDocumentLink.status == "active",
        assignment.c.responsibility_type == "primary_manager",
        assignment.c.archived_at.is_(None),
        SysUser.username == user_ctx.user_id,
        SysUser.is_active.is_(True),
    ).exists()


def _can_access_document(
    db: Session,
    *,
    document_id: str,
    user_ctx: UserContext,
) -> bool:
    scope = _document_scope_condition(db, user_ctx)
    if scope is None:
        return True
    return bool(db.scalar(
        select(MaintenanceWarehouseDocument.document_id).where(
            MaintenanceWarehouseDocument.document_id == document_id,
            scope,
        )
    ))


def _batch_evidence(batch: MaintenanceWarehouseImportBatch | None) -> dict | None:
    if batch is None:
        return None
    return {
        "import_id": batch.import_id,
        "filename": batch.source_filename,
        "source_file_hash": batch.source_file_hash,
        "adapter_key": batch.adapter_key,
        "adapter_version": batch.adapter_version,
        "version_state": batch.version_state,
        "header_signature": batch.header_signature,
        "header_pairs": batch.header_pairs_json,
        "header_diff": (batch.result_json or {}).get("header_diff"),
        "applied_by": batch.applied_by,
        "applied_at": batch.applied_at.isoformat(),
    }


def _link_evidence(link: MaintenanceWarehouseDocumentLink) -> dict:
    return {
        "link_id": link.link_id,
        "line_id": link.line_id,
        "link_kind": link.link_kind,
        "target_type": link.target_type,
        "target_id": link.target_id,
        "stable_key_kind": link.stable_key_kind,
        "stable_key_hash": link.stable_key_hash,
        "source": link.source,
        "status": link.status,
        "supersedes_link_id": link.supersedes_link_id,
        "version": link.version,
        "reason": link.reason,
        "operated_by": link.operated_by,
        "created_at": link.created_at.isoformat(),
    }


def _audit_evidence(event: MaintenanceWarehouseAuditEvent) -> dict:
    return {
        "event_id": event.event_id,
        "action": event.action,
        "before": event.before_json,
        "after": event.after_json,
        "reason": event.reason,
        "operated_by": event.operated_by,
        "occurred_at": event.occurred_at.isoformat(),
    }


def search_documents(
    db: Session, *, q: str | None, document_type: str | None,
    page: int, page_size: int, user_ctx: UserContext,
) -> dict:
    filters = []
    scope = _document_scope_condition(db, user_ctx)
    if scope is not None:
        filters.append(scope)
    if q:
        term = q.strip()
        filters.append(or_(
            MaintenanceWarehouseDocument.document_no.ilike(f"%{term}%"),
            MaintenanceWarehouseDocument.source_document_id.ilike(f"%{term}%"),
            select(MaintenanceWarehouseDocumentLine.line_id).where(
                MaintenanceWarehouseDocumentLine.document_id
                == MaintenanceWarehouseDocument.document_id,
                or_(
                    MaintenanceWarehouseDocumentLine.pn.ilike(f"%{term}%"),
                    MaintenanceWarehouseDocumentLine.sn.ilike(f"%{term}%"),
                    MaintenanceWarehouseDocumentLine.self_code.ilike(f"%{term}%"),
                    MaintenanceWarehouseDocumentLine.source_line_id.ilike(f"%{term}%"),
                ),
            ).exists(),
        ))
    if document_type:
        if document_type not in {"shipment", "return", "receipt"}:
            raise MaintenanceWarehouseError("单据类型无效")
        filters.append(MaintenanceWarehouseDocument.document_type == document_type)
    total = db.scalar(
        select(func.count()).select_from(MaintenanceWarehouseDocument).where(*filters)
    ) or 0
    line_count_query = (
        select(func.count())
        .select_from(MaintenanceWarehouseDocumentLine)
        .where(
            MaintenanceWarehouseDocumentLine.document_id
            == MaintenanceWarehouseDocument.document_id
        )
        .correlate(MaintenanceWarehouseDocument)
        .scalar_subquery()
    )
    open_count_query = (
        select(func.count())
        .select_from(MaintenanceWarehouseAmbiguity)
        .where(
            MaintenanceWarehouseAmbiguity.document_id
            == MaintenanceWarehouseDocument.document_id,
            MaintenanceWarehouseAmbiguity.status == "open",
        )
        .correlate(MaintenanceWarehouseDocument)
        .scalar_subquery()
    )
    rows = db.execute(
        select(
            MaintenanceWarehouseDocument,
            line_count_query.label("line_count"),
            open_count_query.label("open_ambiguity_count"),
        )
        .where(*filters)
        .order_by(
            MaintenanceWarehouseDocument.document_date.desc().nullslast(),
            MaintenanceWarehouseDocument.created_at.desc(),
            MaintenanceWarehouseDocument.document_id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    document_ids = [row.document_id for row, _line_count, _open_count in rows]
    import_ids = [row.first_import_id for row, _line_count, _open_count in rows]
    batches = {
        batch.import_id: batch
        for batch in db.scalars(
            select(MaintenanceWarehouseImportBatch).where(
                MaintenanceWarehouseImportBatch.import_id.in_(import_ids)
            )
        )
    } if import_ids else {}
    links_by_document: dict[str, list[MaintenanceWarehouseDocumentLink]] = {}
    if document_ids:
        for link in db.scalars(
            select(MaintenanceWarehouseDocumentLink)
            .where(MaintenanceWarehouseDocumentLink.document_id.in_(document_ids))
            .order_by(
                MaintenanceWarehouseDocumentLink.document_id,
                MaintenanceWarehouseDocumentLink.link_kind,
                MaintenanceWarehouseDocumentLink.created_at,
                MaintenanceWarehouseDocumentLink.link_id,
            )
        ):
            links_by_document.setdefault(link.document_id, []).append(link)
    items = []
    for row, line_count, open_count in rows:
        items.append({
            "document_id": row.document_id,
            "document_type": row.document_type,
            "source_document_id": row.source_document_id,
            "document_no": row.document_no,
            "document_date": row.document_date.isoformat() if row.document_date else None,
            "raw_status": row.raw_status,
            "normalized_status": row.normalized_status,
            "line_count": int(line_count or 0),
            "open_ambiguity_count": int(open_count or 0),
            "batch": _batch_evidence(batches.get(row.first_import_id)),
            "links": [
                _link_evidence(link)
                for link in links_by_document.get(row.document_id, [])
            ],
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def search_ambiguities(
    db: Session, *, q: str | None, status: str | None,
    ambiguity_type: str | None, page: int, page_size: int,
    user_ctx: UserContext,
) -> dict:
    filters = []
    scope = _document_scope_condition(db, user_ctx)
    if scope is not None:
        filters.extend([
            MaintenanceWarehouseAmbiguity.document_id.is_not(None),
            scope,
        ])
    if status:
        if status not in {"open", "resolved"}:
            raise MaintenanceWarehouseError("歧义状态无效")
        filters.append(MaintenanceWarehouseAmbiguity.status == status)
    if ambiguity_type:
        filters.append(MaintenanceWarehouseAmbiguity.ambiguity_type == ambiguity_type)
    if q:
        term = q.strip()
        filters.append(or_(
            MaintenanceWarehouseDocument.document_no.ilike(f"%{term}%"),
            MaintenanceWarehouseDocument.source_document_id.ilike(f"%{term}%"),
            select(MaintenanceWarehouseDocumentLine.line_id).where(
                MaintenanceWarehouseDocumentLine.document_id
                == MaintenanceWarehouseDocument.document_id,
                or_(
                    MaintenanceWarehouseDocumentLine.pn.ilike(f"%{term}%"),
                    MaintenanceWarehouseDocumentLine.sn.ilike(f"%{term}%"),
                    MaintenanceWarehouseDocumentLine.self_code.ilike(f"%{term}%"),
                    MaintenanceWarehouseDocumentLine.source_line_id.ilike(f"%{term}%"),
                ),
            ).exists(),
        ))
    joined = select(MaintenanceWarehouseAmbiguity, MaintenanceWarehouseDocument).outerjoin(
        MaintenanceWarehouseDocument,
        MaintenanceWarehouseDocument.document_id == MaintenanceWarehouseAmbiguity.document_id,
    ).where(*filters)
    total = db.scalar(
        select(func.count()).select_from(joined.subquery())
    ) or 0
    rows = db.execute(
        joined.order_by(
            MaintenanceWarehouseAmbiguity.status,
            MaintenanceWarehouseAmbiguity.created_at.desc(),
            MaintenanceWarehouseAmbiguity.ambiguity_id,
        ).offset((page - 1) * page_size).limit(page_size)
    ).all()
    ambiguity_ids = [ambiguity.ambiguity_id for ambiguity, _document in rows]
    import_ids = [ambiguity.import_id for ambiguity, _document in rows]
    document_ids = [
        ambiguity.document_id
        for ambiguity, _document in rows
        if ambiguity.document_id is not None
    ]
    batches = {
        batch.import_id: batch
        for batch in db.scalars(
            select(MaintenanceWarehouseImportBatch).where(
                MaintenanceWarehouseImportBatch.import_id.in_(import_ids)
            )
        )
    } if import_ids else {}
    links_by_document: dict[str, list[MaintenanceWarehouseDocumentLink]] = {}
    if document_ids:
        for link in db.scalars(
            select(MaintenanceWarehouseDocumentLink)
            .where(MaintenanceWarehouseDocumentLink.document_id.in_(document_ids))
            .order_by(
                MaintenanceWarehouseDocumentLink.document_id,
                MaintenanceWarehouseDocumentLink.link_kind,
                MaintenanceWarehouseDocumentLink.created_at,
                MaintenanceWarehouseDocumentLink.link_id,
            )
        ):
            links_by_document.setdefault(link.document_id, []).append(link)
    audits_by_ambiguity: dict[str, list[MaintenanceWarehouseAuditEvent]] = {}
    if ambiguity_ids:
        for event in db.scalars(
            select(MaintenanceWarehouseAuditEvent)
            .where(MaintenanceWarehouseAuditEvent.ambiguity_id.in_(ambiguity_ids))
            .order_by(
                MaintenanceWarehouseAuditEvent.occurred_at,
                MaintenanceWarehouseAuditEvent.event_id,
            )
        ):
            if event.ambiguity_id:
                audits_by_ambiguity.setdefault(event.ambiguity_id, []).append(event)
    items = []
    for ambiguity, document in rows:
        items.append({
            "ambiguity_id": ambiguity.ambiguity_id,
            "import_id": ambiguity.import_id,
            "ambiguity_type": ambiguity.ambiguity_type,
            "field_code": ambiguity.field_code,
            "source_row": ambiguity.source_row,
            "value_hash": ambiguity.value_hash,
            "status": ambiguity.status,
            "version": ambiguity.version,
            "candidates": ambiguity.candidates_json,
            "resolution": ambiguity.resolution_json,
            "resolution_reason": ambiguity.resolution_reason,
            "resolved_by": ambiguity.resolved_by,
            "resolved_at": (
                ambiguity.resolved_at.isoformat() if ambiguity.resolved_at else None
            ),
            "batch": _batch_evidence(batches.get(ambiguity.import_id)),
            "links": [
                _link_evidence(link)
                for link in links_by_document.get(ambiguity.document_id or "", [])
            ],
            "history": [
                _audit_evidence(event)
                for event in audits_by_ambiguity.get(ambiguity.ambiguity_id, [])
            ],
            "document": None if document is None else {
                "document_id": document.document_id,
                "document_type": document.document_type,
                "document_no": document.document_no,
                "source_document_id": document.source_document_id,
            },
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


_LINK_TARGET_MATRIX = {
    "maintenance_order": "maintenance_order",
    "project": "maintenance_project",
    "site_issue": "maintenance_site_issue",
    "bad_return": "maintenance_bad_return",
    "part": "dim_part",
    "warehouse_document": "warehouse_document",
}

_FIELD_LINK_KIND = {
    "maintenance_order": "maintenance_order",
    "project": "project",
    "site_issue": "site_issue",
    "bad_return": "bad_return",
    "pn": "part",
    "upstream_document": "warehouse_document",
}


def _target_exists(db: Session, target_type: str, target_id: str) -> bool:
    if target_type == "maintenance_order":
        return db.scalar(select(FMaintenanceOrder.raw_order_id).where(
            FMaintenanceOrder.raw_order_id == target_id
        )) is not None
    if target_type == "maintenance_project":
        return db.scalar(select(MaintenanceProject.project_id).where(
            MaintenanceProject.project_id == target_id
        )) is not None
    if target_type == "maintenance_site_issue":
        return db.scalar(select(MaintenanceSiteIssue.issue_id).where(
            MaintenanceSiteIssue.issue_id == target_id
        )) is not None
    if target_type == "maintenance_bad_return":
        if not _table_has_columns(
            db, "maintenance_bad_return", {"return_id"}
        ):
            return False
        return db.scalar(
            text(
                "SELECT return_id FROM maintenance_bad_return "
                "WHERE return_id = :target_id"
            ),
            {"target_id": target_id},
        ) is not None
    if target_type == "dim_part":
        try:
            part_id = int(target_id)
        except ValueError:
            return False
        return db.scalar(select(DimPart.id).where(DimPart.id == part_id)) is not None
    if target_type == "warehouse_document":
        return db.scalar(select(MaintenanceWarehouseDocument.document_id).where(
            MaintenanceWarehouseDocument.document_id == target_id
        )) is not None
    return False


def resolve_ambiguity(
    db: Session,
    *,
    ambiguity_id: str,
    version: int,
    reason: str,
    operated_by: str,
    decision: str,
    link_kind: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    user_ctx: UserContext,
) -> dict:
    reason = (reason or "").strip()
    if not reason or len(reason) > 1000:
        raise MaintenanceWarehouseError("裁决理由无效")
    if decision not in {"acknowledge", "link"}:
        raise MaintenanceWarehouseError("裁决类型无效")
    operated_by = (operated_by or "").strip()
    if not operated_by or len(operated_by) > 64:
        raise MaintenanceWarehouseError("实名操作人无效")
    ambiguity = db.scalar(
        select(MaintenanceWarehouseAmbiguity)
        .where(MaintenanceWarehouseAmbiguity.ambiguity_id == ambiguity_id)
        .with_for_update()
    )
    if ambiguity is None:
        raise MaintenanceWarehouseError("关联歧义不存在")
    if ambiguity.status != "open" or ambiguity.version != version:
        raise MaintenanceWarehouseConflict("歧义已被其他人处理，请刷新")
    if (
        ambiguity.document_id is None
        and user_ctx.role not in FULL_SCOPE_ROLES
    ) or (
        ambiguity.document_id is not None
        and not _can_access_document(
            db, document_id=ambiguity.document_id, user_ctx=user_ctx
        )
    ):
        raise MaintenanceWarehouseError("无权访问该仓库单据歧义")
    before = {
        "status": ambiguity.status,
        "version": ambiguity.version,
        "ambiguity_type": ambiguity.ambiguity_type,
        "candidates": ambiguity.candidates_json,
    }
    resolution: dict = {"decision": decision}
    new_link: MaintenanceWarehouseDocumentLink | None = None
    if decision == "link":
        if not link_kind or not target_type or not target_id:
            raise MaintenanceWarehouseError("关联裁决缺少稳定目标")
        if _LINK_TARGET_MATRIX.get(link_kind) != target_type:
            raise MaintenanceWarehouseError("关联类型与目标类型不匹配")
        if ambiguity.ambiguity_type not in {"multiple_candidates", "field_conflict"}:
            raise MaintenanceWarehouseError("该歧义类型不允许建立关联")
        if _FIELD_LINK_KIND.get(ambiguity.field_code or "") != link_kind:
            raise MaintenanceWarehouseError("裁决关联与歧义字段不匹配")
        candidate_targets = {
            (str(candidate.get("target_type") or ""), str(candidate.get("target_id") or ""))
            for candidate in ambiguity.candidates_json
            if isinstance(candidate, dict)
        }
        if (target_type, target_id) not in candidate_targets:
            raise MaintenanceWarehouseError("稳定目标不属于该歧义的候选集合")
        if ambiguity.document_id is None:
            raise MaintenanceWarehouseError("该歧义没有可关联的单据事实")
        if not _target_exists(db, target_type, target_id):
            raise MaintenanceWarehouseError("稳定关联目标不存在")
        resolution.update({
            "link_kind": link_kind,
            "target_type": target_type,
            "target_id": target_id,
        })
        active_link = db.scalar(
            select(MaintenanceWarehouseDocumentLink)
            .where(
                MaintenanceWarehouseDocumentLink.document_id == ambiguity.document_id,
                MaintenanceWarehouseDocumentLink.line_id.is_(ambiguity.line_id)
                if ambiguity.line_id is None
                else MaintenanceWarehouseDocumentLink.line_id == ambiguity.line_id,
                MaintenanceWarehouseDocumentLink.link_kind == link_kind,
                MaintenanceWarehouseDocumentLink.status == "active",
            )
            .with_for_update()
        )
        if (
            active_link is not None
            and active_link.target_type == target_type
            and active_link.target_id == target_id
        ):
            resolution["link_action"] = "retained"
            resolution["active_link_id"] = active_link.link_id
        else:
            next_version = 1 if active_link is None else active_link.version + 1
            link_id = _link_id(
                ambiguity.document_id,
                ambiguity.line_id,
                link_kind,
                target_type,
                target_id,
                version=next_version,
            )
            if active_link is not None:
                before["active_link"] = {
                    "link_id": active_link.link_id,
                    "target_type": active_link.target_type,
                    "target_id": active_link.target_id,
                    "version": active_link.version,
                }
                active_link.status = "superseded"
                active_link.version += 1
                db.flush([active_link])
            new_link = MaintenanceWarehouseDocumentLink(
                link_id=link_id,
                document_id=ambiguity.document_id,
                line_id=ambiguity.line_id,
                link_kind=link_kind,
                target_type=target_type,
                target_id=target_id,
                stable_key_kind="manual_stable_id",
                stable_key_hash=_hash(target_id),
                source="manual",
                status="active",
                supersedes_link_id=(
                    active_link.link_id if active_link is not None else None
                ),
                version=next_version,
                reason=reason,
                operated_by=operated_by,
            )
            db.add(new_link)
            resolution["link_action"] = (
                "corrected" if active_link is not None else "created"
            )
            resolution["active_link_id"] = link_id
    now = datetime.now(timezone.utc)
    ambiguity.status = "resolved"
    ambiguity.version += 1
    ambiguity.resolution_json = resolution
    ambiguity.resolution_reason = reason
    ambiguity.resolved_by = operated_by
    ambiguity.resolved_at = now
    after = {"status": "resolved", "version": ambiguity.version, "resolution": resolution}
    db.add(MaintenanceWarehouseAuditEvent(
        event_id=_uuid(
            f"maintenance-warehouse-audit:resolve:{ambiguity_id}:{ambiguity.version}"
        ),
        import_id=ambiguity.import_id,
        ambiguity_id=ambiguity.ambiguity_id,
        action="ambiguity_resolved",
        before_json=before,
        after_json=after,
        reason=reason,
        operated_by=operated_by,
    ))
    db.flush()
    return {
        "ambiguity_id": ambiguity.ambiguity_id,
        "status": ambiguity.status,
        "version": ambiguity.version,
        "resolution": resolution,
        "link_created": new_link is not None,
    }
