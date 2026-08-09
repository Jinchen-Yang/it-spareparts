"""Fail-closed bridge from #209 warehouse facts into #210 cutover movements.

The sibling warehouse models are intentionally consumed through an optional SQL
contract.  An isolated #210 branch therefore remains unavailable, while a merged
tree can only become ready when #201/#208/#209 tables and every stable link are
present.  No project name, date proximity, PN text, or quantity inference is used.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


class MaintenanceMigrationWarehouseError(ValueError):
    """Canonical warehouse facts cannot satisfy the migration cutover contract."""


_WAREHOUSE_MOVEMENT_MAP = {
    "shipment": "delivery",
    "receipt": "available_receipt",
    "return": "return_registration",
}
_MAX_WAREHOUSE_DOCUMENTS_PER_PROJECT = 50_000
_MAX_WAREHOUSE_LINES_PER_PROJECT = 200_000
_MAX_WAREHOUSE_LINKS_PER_PROJECT = 1_000_000
_REQUIRED_CONTRACTS = {
    "maintenance_warehouse_import_batch": {
        "import_id",
        "source_file_hash",
        "adapter_version",
        "header_signature",
        "version_state",
        "status",
    },
    "maintenance_warehouse_document": {
        "document_id",
        "document_type",
        "document_no",
        "document_date",
        "normalized_status",
        "first_import_id",
    },
    "maintenance_warehouse_document_line": {
        "line_id",
        "document_id",
        "pn",
        "sn",
        "quantity",
    },
    "maintenance_warehouse_document_link": {
        "link_id",
        "document_id",
        "line_id",
        "link_kind",
        "target_type",
        "target_id",
        "status",
        "version",
    },
    "maintenance_warehouse_ambiguity": {
        "document_id",
        "line_id",
        "candidates_json",
        "status",
    },
    "maintenance_source_order_assignment": {
        "assignment_id",
        "source_order_id",
        "project_id",
        "is_active",
        "version",
    },
    "maintenance_project": {"project_id", "is_active"},
    "dim_part": {"id", "pn_std", "status"},
    "maintenance_bad_return": {"return_id", "project_id", "status", "version"},
}


def _required_text(value: Any, label: str, *, max_length: int = 128) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > max_length:
        raise MaintenanceMigrationWarehouseError(f"{label}无效")
    return clean


def _table_has_columns(db: Session, table_name: str, columns: set[str]) -> bool:
    if db.scalar(text("SELECT to_regclass(:name)"), {"name": table_name}) is None:
        return False
    found = set(
        db.scalars(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :name"
            ),
            {"name": table_name},
        )
    )
    return columns <= found


def _contracts_available(db: Session) -> bool:
    return all(
        _table_has_columns(db, table_name, columns)
        for table_name, columns in _REQUIRED_CONTRACTS.items()
    )


def _lock_warehouse_snapshot(db: Session) -> None:
    """Keep #209 documents, links, ambiguities and targets stable to commit.

    #209 has immutable fact rows but link resolution and import append new rows.
    PostgreSQL SHARE locks allow concurrent readers and block those writes while a
    dry-run is rebuilt/signed, closing the READ COMMITTED mixed-snapshot window.
    """

    db.execute(
        text(
            "LOCK TABLE maintenance_warehouse_import_batch, "
            "maintenance_warehouse_document, maintenance_warehouse_document_line, "
            "maintenance_warehouse_document_link, maintenance_warehouse_ambiguity, "
            "maintenance_source_order_assignment, maintenance_project, dim_part, "
            "maintenance_bad_return IN SHARE MODE"
        )
    )


def validate_cutover_inventory_movements(
    movements: Sequence[Mapping[str, Any]],
    *,
    cutover_date: date,
    project_id: str | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Validate the canonical movement identity, mapping and cutover boundary."""

    expected_project = (
        _required_text(project_id, "项目稳定编号", max_length=36)
        if project_id is not None
        else None
    )
    validated: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for row in movements:
        document_id = _required_text(row.get("document_id"), "仓库单据稳定编号")
        line_id = _required_text(row.get("line_id"), "仓库明细稳定编号")
        movement_id = _required_text(row.get("movement_id"), "仓库流水稳定编号")
        if movement_id != f"{document_id}:{line_id}":
            raise MaintenanceMigrationWarehouseError(
                "仓库流水稳定编号必须由 document_id:line_id 生成"
            )
        if movement_id in seen:
            raise MaintenanceMigrationWarehouseError("仓库流水稳定编号重复")
        seen.add(movement_id)

        row_project_id = _required_text(
            row.get("project_id"), "仓库流水项目稳定编号", max_length=36
        )
        if expected_project is not None and row_project_id != expected_project:
            raise MaintenanceMigrationWarehouseError("仓库流水归属了其他项目")
        try:
            part_id = int(row.get("part_id"))
        except (TypeError, ValueError) as exc:
            raise MaintenanceMigrationWarehouseError("仓库流水 part_id 无效") from exc
        if part_id <= 0:
            raise MaintenanceMigrationWarehouseError("仓库流水 part_id 无效")
        expected_balance_key = f"{row_project_id}:{part_id}"
        if str(row.get("balance_key") or "") != expected_balance_key:
            raise MaintenanceMigrationWarehouseError(
                "仓库流水 balance_key 必须为 project_id:part_id"
            )

        source = str(row.get("source") or "")
        source_type = str(row.get("source_document_type") or "")
        source_status = str(row.get("source_status") or "")
        movement_type = str(row.get("movement_type") or "")
        expected_type = _WAREHOUSE_MOVEMENT_MAP.get(source_type)
        if (
            source != "maintenance_warehouse_v1"
            or source_status != "confirmed"
            or expected_type != movement_type
        ):
            raise MaintenanceMigrationWarehouseError(
                "仓库流水仅接受 confirmed canonical warehouse 映射"
            )
        expected_formal_available = source_type == "receipt"
        if row.get("formal_available") is not expected_formal_available:
            raise MaintenanceMigrationWarehouseError(
                "正式可用标记与仓库单据类型不一致"
            )

        raw_date = row.get("document_date")
        try:
            document_date = (
                raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
            )
        except (TypeError, ValueError) as exc:
            raise MaintenanceMigrationWarehouseError(
                f"仓库流水 {movement_id} 缺少有效单据日期"
            ) from exc
        if document_date < cutover_date:
            raise MaintenanceMigrationWarehouseError(
                f"仓库流水 {movement_id} 的单据日期早于切换日"
            )
        validated.append(row)
    return tuple(validated)


def _candidate_document_ids(db: Session, *, project_id: str) -> list[str]:
    rows = db.scalars(
        text(
            "SELECT DISTINCT document.document_id "
            "FROM maintenance_warehouse_document AS document "
            "LEFT JOIN maintenance_warehouse_document_link AS project_link "
            "  ON project_link.document_id = document.document_id "
            " AND project_link.line_id IS NULL "
            " AND project_link.link_kind = 'project' "
            " AND project_link.target_type = 'maintenance_project' "
            " AND project_link.status = 'active' "
            "LEFT JOIN maintenance_warehouse_document_link AS order_link "
            "  ON order_link.document_id = document.document_id "
            " AND order_link.line_id IS NULL "
            " AND order_link.link_kind = 'maintenance_order' "
            " AND order_link.target_type = 'maintenance_order' "
            " AND order_link.status = 'active' "
            "LEFT JOIN maintenance_source_order_assignment AS assignment "
            "  ON assignment.source_order_id = order_link.target_id "
            " AND assignment.is_active IS TRUE "
            "WHERE project_link.target_id = :project_id "
            "   OR assignment.project_id = :project_id "
            "ORDER BY document.document_id LIMIT :candidate_limit"
        ),
        {
            "project_id": project_id,
            "candidate_limit": _MAX_WAREHOUSE_DOCUMENTS_PER_PROJECT + 1,
        },
    ).all()
    ambiguity_rows = db.scalars(
        text(
            "SELECT DISTINCT ambiguity.document_id "
            "FROM maintenance_warehouse_ambiguity AS ambiguity "
            "CROSS JOIN LATERAL jsonb_array_elements(ambiguity.candidates_json) "
            "  AS candidate "
            "LEFT JOIN maintenance_source_order_assignment AS assignment "
            "  ON candidate->>'target_type' = 'maintenance_order' "
            " AND assignment.source_order_id = candidate->>'target_id' "
            " AND assignment.is_active IS TRUE "
            "WHERE ambiguity.status = 'open' "
            "  AND ambiguity.document_id IS NOT NULL "
            "  AND ((candidate->>'target_type' = 'maintenance_project' "
            "        AND candidate->>'target_id' = :project_id) "
            "    OR assignment.project_id = :project_id) "
            "ORDER BY ambiguity.document_id LIMIT :candidate_limit"
        ),
        {
            "project_id": project_id,
            "candidate_limit": _MAX_WAREHOUSE_DOCUMENTS_PER_PROJECT + 1,
        },
    ).all()
    return sorted({str(value) for value in [*rows, *ambiguity_rows]})


def _rows_for_documents(
    db: Session, *, document_ids: list[str]
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    ids = bindparam("document_ids", expanding=True)
    documents = list(
        db.execute(
            text(
                "SELECT document.document_id, document.document_type, "
                "document.document_no, document.document_date, "
                "document.normalized_status, document.first_import_id, "
                "batch.source_file_hash, batch.adapter_version, "
                "batch.header_signature, batch.version_state, batch.status AS batch_status "
                "FROM maintenance_warehouse_document AS document "
                "JOIN maintenance_warehouse_import_batch AS batch "
                "  ON batch.import_id = document.first_import_id "
                "WHERE document.document_id IN :document_ids "
                "ORDER BY document.document_id LIMIT :document_limit"
            ).bindparams(ids),
            {
                "document_ids": document_ids,
                "document_limit": _MAX_WAREHOUSE_DOCUMENTS_PER_PROJECT + 1,
            },
        ).mappings()
    )
    lines = list(
        db.execute(
            text(
                "SELECT line_id, document_id, pn, sn, quantity "
                "FROM maintenance_warehouse_document_line "
                "WHERE document_id IN :document_ids "
                "ORDER BY document_id, line_id LIMIT :line_limit"
            ).bindparams(bindparam("document_ids", expanding=True)),
            {
                "document_ids": document_ids,
                "line_limit": _MAX_WAREHOUSE_LINES_PER_PROJECT + 1,
            },
        ).mappings()
    )
    links = list(
        db.execute(
            text(
                "SELECT link_id, document_id, line_id, link_kind, target_type, "
                "target_id, status, version "
                "FROM maintenance_warehouse_document_link "
                "WHERE document_id IN :document_ids AND status = 'active' "
                "ORDER BY document_id, line_id NULLS FIRST, link_kind, link_id "
                "LIMIT :link_limit"
            ).bindparams(bindparam("document_ids", expanding=True)),
            {
                "document_ids": document_ids,
                "link_limit": _MAX_WAREHOUSE_LINKS_PER_PROJECT + 1,
            },
        ).mappings()
    )
    return documents, lines, links


def load_project_inventory_movements(
    db: Session, project_id: str, cutover_date: date
) -> tuple[Sequence[Mapping[str, Any]], bool]:
    """Load only exact, current, confirmed #209 facts for one active project."""

    clean_project_id = _required_text(project_id, "项目稳定编号", max_length=36)
    if not _contracts_available(db):
        return (), False
    _lock_warehouse_snapshot(db)
    active_project = db.scalar(
        text(
            "SELECT project_id FROM maintenance_project "
            "WHERE project_id = :project_id AND is_active IS TRUE"
        ),
        {"project_id": clean_project_id},
    )
    if active_project is None:
        return (), False

    document_ids = _candidate_document_ids(db, project_id=clean_project_id)
    if len(document_ids) > _MAX_WAREHOUSE_DOCUMENTS_PER_PROJECT:
        return (), False
    if not document_ids:
        return (), True
    documents, lines, links = _rows_for_documents(db, document_ids=document_ids)
    if (
        len(documents) > _MAX_WAREHOUSE_DOCUMENTS_PER_PROJECT
        or len(lines) > _MAX_WAREHOUSE_LINES_PER_PROJECT
        or len(links) > _MAX_WAREHOUSE_LINKS_PER_PROJECT
    ):
        return (), False
    lines_by_document: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    links_by_document: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    links_by_line: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in lines:
        lines_by_document[str(row["document_id"])].append(row)
    for row in links:
        links_by_document[str(row["document_id"])].append(row)
        if row["line_id"] is not None:
            links_by_line[str(row["line_id"])].append(row)

    order_ids = {
        str(row["target_id"])
        for row in links
        if row["line_id"] is None
        and row["link_kind"] == "maintenance_order"
        and row["target_type"] == "maintenance_order"
    }
    assignments: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    if order_ids:
        assignment_statement = text(
            "SELECT assignment_id, source_order_id, project_id, version "
            "FROM maintenance_source_order_assignment "
            "WHERE is_active IS TRUE AND source_order_id IN :source_order_ids "
            "ORDER BY source_order_id, assignment_id"
        ).bindparams(bindparam("source_order_ids", expanding=True))
        for row in db.execute(
            assignment_statement, {"source_order_ids": sorted(order_ids)}
        ).mappings():
            assignments[str(row["source_order_id"])].append(row)

    part_ids: set[int] = set()
    bad_return_ids: set[str] = set()
    for row in links:
        if row["link_kind"] == "part" and row["target_type"] == "dim_part":
            try:
                part_ids.add(int(row["target_id"]))
            except (InvalidOperation, TypeError, ValueError):
                return (), False
        if row["link_kind"] == "bad_return" and row["target_type"] == "maintenance_bad_return":
            bad_return_ids.add(str(row["target_id"]))

    active_parts: dict[int, str] = {}
    if part_ids:
        part_statement = text(
            "SELECT id, pn_std FROM dim_part "
            "WHERE status = 'active' AND id IN :part_ids ORDER BY id"
        ).bindparams(bindparam("part_ids", expanding=True))
        active_parts = {
            int(row.id): str(row.pn_std)
            for row in db.execute(part_statement, {"part_ids": sorted(part_ids)})
        }
    bad_returns: dict[str, Mapping[str, Any]] = {}
    if bad_return_ids:
        return_statement = text(
            "SELECT return_id, project_id, status, version "
            "FROM maintenance_bad_return "
            "WHERE return_id IN :return_ids ORDER BY return_id"
        ).bindparams(bindparam("return_ids", expanding=True))
        bad_returns = {
            str(row["return_id"]): row
            for row in db.execute(
                return_statement, {"return_ids": sorted(bad_return_ids)}
            ).mappings()
        }

    open_ambiguities = set(
        db.scalars(
            text(
                "SELECT DISTINCT document_id FROM maintenance_warehouse_ambiguity "
                "WHERE status = 'open' AND document_id IN :document_ids"
            ).bindparams(bindparam("document_ids", expanding=True)),
            {"document_ids": document_ids},
        )
    )
    movements: list[dict[str, Any]] = []
    for document in documents:
        document_id = str(document["document_id"])
        document_date = document["document_date"]
        status = str(document["normalized_status"])
        if status == "void":
            continue
        if document_date is None:
            return (), False
        if document_date < cutover_date:
            continue
        if document_id in open_ambiguities or status != "confirmed":
            return (), False
        if document["version_state"] != "known" or document["batch_status"] != "applied":
            return (), False

        document_links = links_by_document[document_id]
        order_links = [
            row
            for row in document_links
            if row["line_id"] is None
            and row["link_kind"] == "maintenance_order"
            and row["target_type"] == "maintenance_order"
        ]
        project_links = [
            row
            for row in document_links
            if row["line_id"] is None
            and row["link_kind"] == "project"
            and row["target_type"] == "maintenance_project"
        ]
        if len(order_links) != 1 or len(project_links) != 1:
            return (), False
        order_id = str(order_links[0]["target_id"])
        assignment_rows = assignments.get(order_id, [])
        if (
            len(assignment_rows) != 1
            or str(assignment_rows[0]["project_id"]) != clean_project_id
            or str(project_links[0]["target_id"]) != clean_project_id
        ):
            return (), False
        assignment = assignment_rows[0]

        document_type = str(document["document_type"])
        movement_type = _WAREHOUSE_MOVEMENT_MAP.get(document_type)
        if movement_type is None:
            return (), False
        bad_return_link: Mapping[str, Any] | None = None
        bad_return: Mapping[str, Any] | None = None
        if document_type in {"receipt", "return"}:
            bad_return_links = [
                row
                for row in document_links
                if row["line_id"] is None
                and row["link_kind"] == "bad_return"
                and row["target_type"] == "maintenance_bad_return"
            ]
            if len(bad_return_links) != 1:
                return (), False
            bad_return_link = bad_return_links[0]
            bad_return = bad_returns.get(str(bad_return_link["target_id"]))
            if (
                bad_return is None
                or str(bad_return["project_id"]) != clean_project_id
                or str(bad_return["status"]) == "void"
                or (
                    document_type == "receipt"
                    and str(bad_return["status"]) != "warehouse_confirmed"
                )
            ):
                return (), False

        document_lines = lines_by_document.get(document_id, [])
        if not document_lines:
            return (), False
        for line in document_lines:
            line_id = str(line["line_id"])
            part_links = [
                row
                for row in links_by_line.get(line_id, [])
                if row["link_kind"] == "part" and row["target_type"] == "dim_part"
            ]
            if len(part_links) != 1 or line["quantity"] is None:
                return (), False
            try:
                part_id = int(part_links[0]["target_id"])
                quantity = Decimal(line["quantity"])
            except (InvalidOperation, TypeError, ValueError):
                return (), False
            if part_id not in active_parts or not quantity.is_finite() or quantity < 0:
                return (), False
            movements.append(
                {
                    "movement_id": f"{document_id}:{line_id}",
                    "document_id": document_id,
                    "line_id": line_id,
                    "document_no": _required_text(
                        document["document_no"], "仓库单据号"
                    ),
                    "document_date": document_date.isoformat(),
                    "movement_type": movement_type,
                    "source": "maintenance_warehouse_v1",
                    "source_document_type": document_type,
                    "source_status": status,
                    "formal_available": document_type == "receipt",
                    "project_id": clean_project_id,
                    "part_id": part_id,
                    "balance_key": f"{clean_project_id}:{part_id}",
                    "pn": active_parts[part_id],
                    "source_pn": line["pn"],
                    "sn": line["sn"],
                    "quantity": format(quantity, "f"),
                    "source_order_id": order_id,
                    "source_assignment_id": str(assignment["assignment_id"]),
                    "source_assignment_version": int(assignment["version"]),
                    "project_link_id": str(project_links[0]["link_id"]),
                    "project_link_version": int(project_links[0]["version"]),
                    "part_link_id": str(part_links[0]["link_id"]),
                    "part_link_version": int(part_links[0]["version"]),
                    "bad_return_id": (
                        str(bad_return_link["target_id"]) if bad_return_link else None
                    ),
                    "bad_return_status": (
                        str(bad_return["status"]) if bad_return_link else None
                    ),
                    "bad_return_version": (
                        int(bad_return["version"]) if bad_return_link else None
                    ),
                    "warehouse_import_id": str(document["first_import_id"]),
                    "warehouse_source_file_hash": str(document["source_file_hash"]),
                    "warehouse_adapter_version": str(document["adapter_version"]),
                    "warehouse_header_signature": str(document["header_signature"]),
                }
            )
    ordered = sorted(movements, key=lambda row: str(row["movement_id"]))
    return validate_cutover_inventory_movements(
        ordered,
        cutover_date=cutover_date,
        project_id=clean_project_id,
    ), True
