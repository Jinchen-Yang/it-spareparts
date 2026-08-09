"""Preview/apply/search/resolve workflow for maintenance warehouse documents."""

from __future__ import annotations

import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import hmac
import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart, PartAlias
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceSiteIssue
from app.models.maintenance_warehouse import (
    MaintenanceWarehouseAmbiguity,
    MaintenanceWarehouseAuditEvent,
    MaintenanceWarehouseDocument,
    MaintenanceWarehouseDocumentLine,
    MaintenanceWarehouseDocumentLink,
    MaintenanceWarehouseImportBatch,
)
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
        "can_apply": True,
    }


def _advisory_key(parsed: ParsedWarehouseWorkbook) -> int:
    raw = bytes.fromhex(parsed.source_file_hash[:16])
    return int.from_bytes(raw, byteorder="big", signed=True)


def _link_id(document_id: str, line_id: str | None, kind: str, target_type: str, target_id: str) -> str:
    return _uuid(
        f"maintenance-warehouse-link:{document_id}:{line_id or '-'}:{kind}:{target_type}:{target_id}"
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

    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _advisory_key(parsed)})
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
    planned_link_keys: set[tuple[str, str | None, str, str, str]] = set()

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

    existing_link_keys: set[tuple[str, str | None, str, str, str]] = set()
    for chunk in _chunks(set(document_ids.values())):
        rows = db.execute(
            select(
                MaintenanceWarehouseDocumentLink.document_id,
                MaintenanceWarehouseDocumentLink.line_id,
                MaintenanceWarehouseDocumentLink.link_kind,
                MaintenanceWarehouseDocumentLink.target_type,
                MaintenanceWarehouseDocumentLink.target_id,
            ).where(MaintenanceWarehouseDocumentLink.document_id.in_(chunk))
        ).all()
        existing_link_keys.update(tuple(row) for row in rows)

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
    upstream_candidates_from_db = _warehouse_document_candidate_map(db, upstream_refs)
    part_candidates = _part_candidate_map(db, part_refs)

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
        key = (document_id, line_id, link_kind, target_type, target_id)
        if key in existing_link_keys or key in planned_link_keys:
            return
        planned_link_keys.add(key)
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
        else:
            ambiguity_facts.append(_ambiguity_fact(
                code="missing_stable_link", field_code="maintenance_order",
                document_source_id=document.source_document_id,
            ))
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


def search_documents(
    db: Session, *, q: str | None, document_type: str | None,
    page: int, page_size: int,
) -> dict:
    filters = []
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
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def search_ambiguities(
    db: Session, *, q: str | None, status: str | None,
    ambiguity_type: str | None, page: int, page_size: int,
) -> dict:
    filters = []
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
    items = []
    for ambiguity, document in rows:
        items.append({
            "ambiguity_id": ambiguity.ambiguity_id,
            "import_id": ambiguity.import_id,
            "ambiguity_type": ambiguity.ambiguity_type,
            "field_code": ambiguity.field_code,
            "source_row": ambiguity.source_row,
            "status": ambiguity.status,
            "version": ambiguity.version,
            "candidates": ambiguity.candidates_json,
            "resolution": ambiguity.resolution_json,
            "resolution_reason": ambiguity.resolution_reason,
            "resolved_by": ambiguity.resolved_by,
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
    "part": "dim_part",
    "warehouse_document": "warehouse_document",
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
) -> dict:
    reason = (reason or "").strip()
    if not reason or len(reason) > 1000:
        raise MaintenanceWarehouseError("裁决理由无效")
    if decision not in {"acknowledge", "link"}:
        raise MaintenanceWarehouseError("裁决类型无效")
    ambiguity = db.scalar(
        select(MaintenanceWarehouseAmbiguity)
        .where(MaintenanceWarehouseAmbiguity.ambiguity_id == ambiguity_id)
        .with_for_update()
    )
    if ambiguity is None:
        raise MaintenanceWarehouseError("关联歧义不存在")
    if ambiguity.status != "open" or ambiguity.version != version:
        raise MaintenanceWarehouseConflict("歧义已被其他人处理，请刷新")
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
        if ambiguity.document_id is None:
            raise MaintenanceWarehouseError("该歧义没有可关联的单据事实")
        if not _target_exists(db, target_type, target_id):
            raise MaintenanceWarehouseError("稳定关联目标不存在")
        resolution.update({
            "link_kind": link_kind,
            "target_type": target_type,
            "target_id": target_id,
        })
        link_id = _link_id(
            ambiguity.document_id, ambiguity.line_id, link_kind, target_type, target_id
        )
        existing = db.get(MaintenanceWarehouseDocumentLink, link_id)
        if existing is None:
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
                version=1,
                reason=reason,
                operated_by=operated_by,
            )
            db.add(new_link)
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
