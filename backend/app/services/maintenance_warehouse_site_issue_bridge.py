"""Deterministic warehouse-shipment projection for site-issue candidates.

Warehouse documents remain immutable facts.  This module maintains only the
replaceable delivery-candidate projection consumed by the site-issue workflow;
it never creates inventory movements or consumption facts.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssueDeliverySource,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.maintenance_warehouse import (
    MaintenanceWarehouseAmbiguity,
    MaintenanceWarehouseDocument,
    MaintenanceWarehouseDocumentLine,
    MaintenanceWarehouseDocumentLink,
)
from app.services.query_filters import active_beta_maintenance_orders


WAREHOUSE_DELIVERY_ADAPTER = "warehouse_shipment_v1"
SUPPORTED_DELIVERY_ADAPTERS = {
    "synthetic_delivery_v1",
    WAREHOUSE_DELIVERY_ADAPTER,
}
_RELEVANT_AMBIGUITY_TYPES = {
    "unknown_version",
    "missing_document_id",
    "missing_line_id",
    "missing_stable_link",
    "multiple_candidates",
    "field_conflict",
    "unknown_enum",
    "integration_blocker",
}
_INTEGRATION_LOCK_KEY = 8_209_207_201


def _table_available(db: Session) -> bool:
    return db.scalar(
        text("SELECT to_regclass('maintenance_warehouse_document')")
    ) is not None


def _mapping_version(parts: list[object]) -> str:
    payload = json.dumps(
        parts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def synchronize_delivery_sources(
    db: Session,
    *,
    document_ids: set[str] | None = None,
    source_order_ids: set[str] | None = None,
    delivery_line_ids: set[str] | None = None,
) -> dict[str, int]:
    """Refresh affected delivery candidates from exact active warehouse links.

    Every eligibility edge is explicit: confirmed shipment, stable WBDD link,
    current #201 project assignment matching the project link, active part link,
    positive quantity/date, and no unresolved mapping ambiguity.  Anything else
    deactivates the projection and therefore fails site-issue confirmation.
    """

    if not _table_available(db):
        return {"created": 0, "updated": 0, "deactivated": 0, "eligible": 0}

    scoped_call = (
        document_ids is not None
        or source_order_ids is not None
        or delivery_line_ids is not None
    )
    # An explicitly empty scope is a no-op.  Treating it as "all rows" would
    # let a caller that simply had no affected documents deactivate the whole
    # production projection.
    if scoped_call and not any(
        (document_ids or set(), source_order_ids or set(), delivery_line_ids or set())
    ):
        return {"created": 0, "updated": 0, "deactivated": 0, "eligible": 0}
    # The transaction-scoped lock linearizes assignment/import/tombstone and
    # confirmation refreshes.  A confirmation that wins the lock commits
    # before a later reassignment; otherwise it observes the later projection.
    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _INTEGRATION_LOCK_KEY},
    )

    scoped_document_ids = set(document_ids or set())
    if source_order_ids:
        scoped_document_ids.update(
            db.scalars(
                select(MaintenanceWarehouseDocumentLink.document_id).where(
                    MaintenanceWarehouseDocumentLink.line_id.is_(None),
                    MaintenanceWarehouseDocumentLink.link_kind
                    == "maintenance_order",
                    MaintenanceWarehouseDocumentLink.target_type
                    == "maintenance_order",
                    MaintenanceWarehouseDocumentLink.status == "active",
                    MaintenanceWarehouseDocumentLink.target_id.in_(
                        sorted(source_order_ids)
                    ),
                )
            )
        )
    if delivery_line_ids:
        scoped_document_ids.update(
            db.scalars(
                select(MaintenanceWarehouseDocumentLine.document_id).where(
                    MaintenanceWarehouseDocumentLine.line_id.in_(
                        sorted(delivery_line_ids)
                    )
                )
            )
        )

    document_statement = select(MaintenanceWarehouseDocument).where(
        MaintenanceWarehouseDocument.document_type == "shipment"
    )
    if (
        document_ids is not None
        or source_order_ids is not None
        or delivery_line_ids is not None
    ):
        if not scoped_document_ids:
            existing_statement = select(MaintenanceSiteIssueDeliverySource).where(
                MaintenanceSiteIssueDeliverySource.adapter_key
                == WAREHOUSE_DELIVERY_ADAPTER
            )
            empty_scope_filters = []
            if source_order_ids:
                empty_scope_filters.append(
                    MaintenanceSiteIssueDeliverySource.source_order_id.in_(
                        sorted(source_order_ids)
                    )
                )
            if delivery_line_ids:
                empty_scope_filters.append(
                    MaintenanceSiteIssueDeliverySource.delivery_line_id.in_(
                        sorted(delivery_line_ids)
                    )
                )
            if not empty_scope_filters:
                return {
                    "created": 0,
                    "updated": 0,
                    "deactivated": 0,
                    "eligible": 0,
                }
            existing_statement = existing_statement.where(or_(*empty_scope_filters))
            deactivated = 0
            for source in db.scalars(existing_statement):
                if source.is_active:
                    source.is_active = False
                    deactivated += 1
            db.flush()
            return {
                "created": 0,
                "updated": 0,
                "deactivated": deactivated,
                "eligible": 0,
            }
        document_statement = document_statement.where(
            MaintenanceWarehouseDocument.document_id.in_(sorted(scoped_document_ids))
        )
    documents = list(db.scalars(document_statement))
    document_by_id = {row.document_id: row for row in documents}
    affected_document_ids = set(document_by_id)
    lines = list(
        db.scalars(
            select(MaintenanceWarehouseDocumentLine)
            .where(
                MaintenanceWarehouseDocumentLine.document_id.in_(
                    sorted(affected_document_ids)
                )
            )
            .order_by(
                MaintenanceWarehouseDocumentLine.document_id,
                MaintenanceWarehouseDocumentLine.line_id,
            )
        )
    ) if affected_document_ids else []
    affected_line_ids = {line.line_id for line in lines}
    links = list(
        db.scalars(
            select(MaintenanceWarehouseDocumentLink).where(
                MaintenanceWarehouseDocumentLink.document_id.in_(
                    sorted(affected_document_ids)
                ),
                MaintenanceWarehouseDocumentLink.status == "active",
            )
        )
    ) if affected_document_ids else []
    links_by_document: dict[str, list[MaintenanceWarehouseDocumentLink]] = {}
    for link in links:
        links_by_document.setdefault(link.document_id, []).append(link)

    open_ambiguities = list(
        db.scalars(
            select(MaintenanceWarehouseAmbiguity).where(
                MaintenanceWarehouseAmbiguity.document_id.in_(
                    sorted(affected_document_ids)
                ),
                MaintenanceWarehouseAmbiguity.status == "open",
                MaintenanceWarehouseAmbiguity.ambiguity_type.in_(
                    sorted(_RELEVANT_AMBIGUITY_TYPES)
                ),
            )
        )
    ) if affected_document_ids else []
    ambiguities_by_document: dict[str, list[MaintenanceWarehouseAmbiguity]] = {}
    for ambiguity in open_ambiguities:
        if ambiguity.document_id:
            ambiguities_by_document.setdefault(ambiguity.document_id, []).append(
                ambiguity
            )

    order_ids = {
        link.target_id
        for link in links
        if link.line_id is None
        and link.link_kind == "maintenance_order"
        and link.target_type == "maintenance_order"
    }
    effective_order_ids = set(
        db.scalars(
            active_beta_maintenance_orders(
                select(FMaintenanceOrder.raw_order_id).where(
                    FMaintenanceOrder.raw_order_id.in_(sorted(order_ids))
                ),
                FMaintenanceOrder,
            )
        )
    ) if order_ids else set()
    assignments_by_order: dict[str, list[MaintenanceSourceOrderAssignment]] = {}
    if order_ids:
        for row in db.scalars(
            select(MaintenanceSourceOrderAssignment)
            .where(
                MaintenanceSourceOrderAssignment.source_order_id.in_(
                    sorted(order_ids)
                ),
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            )
            .order_by(
                MaintenanceSourceOrderAssignment.source_order_id,
                MaintenanceSourceOrderAssignment.assignment_id,
            )
        ):
            assignments_by_order.setdefault(row.source_order_id, []).append(row)
    assignments = {
        source_order_id: rows[0]
        for source_order_id, rows in assignments_by_order.items()
        if len(rows) == 1
    }
    project_ids = {row.project_id for row in assignments.values()}
    active_project_ids = set(
        db.scalars(
            select(MaintenanceProject.project_id).where(
                MaintenanceProject.project_id.in_(sorted(project_ids)),
                MaintenanceProject.is_active.is_(True),
            )
        )
    ) if project_ids else set()
    part_ids: set[int] = set()
    for link in links:
        if (
            link.line_id is not None
            and link.link_kind == "part"
            and link.target_type == "dim_part"
        ):
            try:
                part_ids.add(int(link.target_id))
            except (TypeError, ValueError):
                continue
    parts = {
        part.id: part
        for part in db.scalars(
            select(DimPart).where(
                DimPart.id.in_(sorted(part_ids)),
                DimPart.status == "active",
            )
        )
    } if part_ids else {}

    candidates: dict[str, dict] = {}
    for line in lines:
        document = document_by_id[line.document_id]
        document_links = links_by_document.get(document.document_id, [])
        order_links = [
            link
            for link in document_links
            if link.line_id is None
            and link.link_kind == "maintenance_order"
            and link.target_type == "maintenance_order"
        ]
        project_links = [
            link
            for link in document_links
            if link.line_id is None
            and link.link_kind == "project"
            and link.target_type == "maintenance_project"
        ]
        part_links = [
            link
            for link in document_links
            if link.line_id == line.line_id
            and link.link_kind == "part"
            and link.target_type == "dim_part"
        ]
        relevant_ambiguity = any(
            ambiguity.line_id is None or ambiguity.line_id == line.line_id
            for ambiguity in ambiguities_by_document.get(document.document_id, [])
        )
        if (
            document.normalized_status != "confirmed"
            or document.document_date is None
            or line.quantity is None
            or Decimal(line.quantity) <= 0
            or len(order_links) != 1
            or len(project_links) != 1
            or len(part_links) != 1
            or relevant_ambiguity
        ):
            continue
        order_id = order_links[0].target_id
        assignment = assignments.get(order_id)
        if (
            order_id not in effective_order_ids
            or assignment is None
            or assignment.project_id not in active_project_ids
            or project_links[0].target_id != assignment.project_id
        ):
            continue
        try:
            part_id = int(part_links[0].target_id)
        except (TypeError, ValueError):
            continue
        part = parts.get(part_id)
        if part is None or not str(part.pn_std or "").strip():
            continue
        candidates[line.line_id] = {
            "project_id": assignment.project_id,
            "source_order_id": order_id,
            "source_line_id": line.source_line_id,
            "delivery_no": document.document_no,
            "delivery_date": document.document_date,
            "part_id": part.id,
            "pn": part.pn_std,
            "serial_number": line.sn,
            "delivered_quantity": Decimal(line.quantity),
            "mapping_version": _mapping_version(
                [
                    WAREHOUSE_DELIVERY_ADAPTER,
                    document.document_id,
                    document.raw_fingerprint,
                    line.line_id,
                    line.raw_fingerprint,
                    order_links[0].link_id,
                    order_links[0].version,
                    project_links[0].link_id,
                    project_links[0].version,
                    part_links[0].link_id,
                    part_links[0].version,
                    assignment.assignment_id,
                    assignment.version,
                ]
            ),
        }

    existing_statement = select(MaintenanceSiteIssueDeliverySource).where(
        MaintenanceSiteIssueDeliverySource.adapter_key == WAREHOUSE_DELIVERY_ADAPTER
    )
    existing_scopes = []
    if affected_line_ids:
        existing_scopes.append(
            MaintenanceSiteIssueDeliverySource.delivery_line_id.in_(
                sorted(affected_line_ids)
            )
        )
    if source_order_ids:
        existing_scopes.append(
            MaintenanceSiteIssueDeliverySource.source_order_id.in_(
                sorted(source_order_ids)
            )
        )
    if scoped_call and not existing_scopes:
        return {"created": 0, "updated": 0, "deactivated": 0, "eligible": 0}
    if existing_scopes:
        existing_statement = existing_statement.where(or_(*existing_scopes))
    existing = {
        row.delivery_line_id: row
        for row in db.scalars(existing_statement.with_for_update())
    }
    created = 0
    updated = 0
    deactivated = 0
    for delivery_line_id, row in existing.items():
        candidate = candidates.get(delivery_line_id)
        if candidate is None:
            if row.is_active:
                row.is_active = False
                deactivated += 1
            continue
        changed = any(
            getattr(row, field) != value for field, value in candidate.items()
        ) or row.mapping_state != "ready" or not row.is_active
        if changed:
            for field, value in candidate.items():
                setattr(row, field, value)
            row.mapping_state = "ready"
            row.is_active = True
            updated += 1
    for delivery_line_id, candidate in candidates.items():
        if delivery_line_id in existing:
            continue
        identity_collision = db.get(
            MaintenanceSiteIssueDeliverySource,
            delivery_line_id,
        )
        if identity_collision is not None:
            # A line id already owned by another adapter is never overwritten.
            continue
        db.add(
            MaintenanceSiteIssueDeliverySource(
                delivery_line_id=delivery_line_id,
                adapter_key=WAREHOUSE_DELIVERY_ADAPTER,
                linked_purchase_line_id=None,
                mapping_state="ready",
                is_active=True,
                **candidate,
            )
        )
        created += 1
    db.flush()
    return {
        "created": created,
        "updated": updated,
        "deactivated": deactivated,
        "eligible": len(candidates),
    }
