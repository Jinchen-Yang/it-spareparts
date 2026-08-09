"""Exact warehouse-shipment projection into site-issue candidates."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app import config
from app.models.dimensions import DimPart
from app.models.inventory import Inventory
from app.models.maintenance import (
    FMaintenanceOrder,
    MaintenanceDemandDeleteIntent,
    MaintenanceDemandTombstone,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueDeliverySource,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.maintenance_warehouse import (
    MaintenanceWarehouseAmbiguity,
    MaintenanceWarehouseDocument,
    MaintenanceWarehouseDocumentLine,
    MaintenanceWarehouseDocumentLink,
    MaintenanceWarehouseImportBatch,
)
from app.models.system import SysImportBatch
from app.services import maintenance_project_operations as operations
from app.services import maintenance_warehouse_site_issue_bridge as bridge


def _uuid(index: int) -> str:
    return f"00000000-0000-0000-0000-{index:012d}"


def _seed_exact_shipment(db) -> dict:
    source_batch = SysImportBatch(
        filename="synthetic-warehouse-bridge-source.xlsx",
        file_type="maintenance",
        file_hash="synthetic-warehouse-bridge-source",
        status="success",
    )
    db.add(source_batch)
    db.flush()
    part = DimPart(
        pn_std="SYN-BRIDGE-PN-001",
        status="active",
        master_source="import",
        locked_fields=[],
    )
    project = MaintenanceProject(
        project_id=_uuid(101),
        project_code="SYN-BRIDGE-PROJECT-001",
        display_name="合成仓储领用桥项目",
        lifecycle_status="ongoing",
    )
    order = FMaintenanceOrder(
        raw_order_id="SYN-BRIDGE-WBDD-001",
        order_no="SYN-BRIDGE-WBDD-001",
        order_date=date(2026, 8, 1),
        data_status=config.ACTIVE_STATUS,
        import_batch_id=source_batch.id,
    )
    db.add_all([part, project, order])
    db.flush()
    assignment = MaintenanceSourceOrderAssignment(
        assignment_id=_uuid(102),
        source_order_id=order.raw_order_id,
        project_id=project.project_id,
        is_active=True,
        version=1,
        created_by="synthetic-admin",
    )
    warehouse_batch = MaintenanceWarehouseImportBatch(
        import_id=_uuid(103),
        source_file_hash="1" * 64,
        source_filename="synthetic-warehouse-bridge.xlsx",
        adapter_key="maintenance_warehouse_workbook",
        adapter_version="shipment_v1",
        version_state="known",
        header_signature="2" * 64,
        header_pairs_json=[],
        status="applied",
        document_count=1,
        line_count=1,
        ambiguity_count=0,
        result_json={},
        reason="合成仓储领用桥测试",
        applied_by="synthetic-admin",
    )
    db.add_all([assignment, warehouse_batch])
    db.flush()
    document = MaintenanceWarehouseDocument(
        document_id=_uuid(104),
        document_type="shipment",
        source_document_id="SYN-BRIDGE-SHIP-OBJECT-001",
        document_no="SYN-BRIDGE-SHIP-001",
        document_date=date(2026, 8, 2),
        raw_status="已完成",
        normalized_status="confirmed",
        raw_fields_json={},
        raw_fingerprint="3" * 64,
        first_import_id=warehouse_batch.import_id,
    )
    db.add(document)
    db.flush()
    line = MaintenanceWarehouseDocumentLine(
        line_id=_uuid(105),
        document_id=document.document_id,
        source_line_id="SYN-BRIDGE-SHIP-LINE-001",
        line_no=1,
        pn=part.pn_std,
        sn="SYN-BRIDGE-SN-001",
        self_code=None,
        quantity=Decimal("3"),
        raw_fields_json={},
        raw_fingerprint="4" * 64,
        first_import_id=warehouse_batch.import_id,
    )
    db.add(line)
    db.flush()
    links = [
        MaintenanceWarehouseDocumentLink(
            link_id=_uuid(106),
            document_id=document.document_id,
            line_id=None,
            link_kind="maintenance_order",
            target_type="maintenance_order",
            target_id=order.raw_order_id,
            stable_key_kind="wbdd_id_or_no",
            stable_key_hash="5" * 64,
            source="automatic",
            status="active",
            supersedes_link_id=None,
            version=1,
            reason="合成稳定关联",
            operated_by="synthetic-admin",
        ),
        MaintenanceWarehouseDocumentLink(
            link_id=_uuid(107),
            document_id=document.document_id,
            line_id=None,
            link_kind="project",
            target_type="maintenance_project",
            target_id=project.project_id,
            stable_key_kind="active_source_order_assignment",
            stable_key_hash="6" * 64,
            source="automatic",
            status="active",
            supersedes_link_id=None,
            version=1,
            reason="合成稳定关联",
            operated_by="synthetic-admin",
        ),
        MaintenanceWarehouseDocumentLink(
            link_id=_uuid(108),
            document_id=document.document_id,
            line_id=line.line_id,
            link_kind="part",
            target_type="dim_part",
            target_id=str(part.id),
            stable_key_kind="pn_exact",
            stable_key_hash="7" * 64,
            source="automatic",
            status="active",
            supersedes_link_id=None,
            version=1,
            reason="合成稳定关联",
            operated_by="synthetic-admin",
        ),
    ]
    db.add_all(links)
    db.commit()
    return {
        "part": part,
        "project": project,
        "order": order,
        "assignment": assignment,
        "batch": warehouse_batch,
        "document": document,
        "line": line,
    }


def test_exact_confirmed_shipment_materializes_without_inventory_write(db):
    facts = _seed_exact_shipment(db)
    inventory_before = int(
        db.scalar(select(func.count()).select_from(Inventory)) or 0
    )

    result = bridge.synchronize_delivery_sources(
        db,
        document_ids={facts["document"].document_id},
    )
    db.commit()

    assert result == {"created": 1, "updated": 0, "deactivated": 0, "eligible": 1}
    source = db.get(MaintenanceSiteIssueDeliverySource, facts["line"].line_id)
    assert source is not None
    assert source.adapter_key == bridge.WAREHOUSE_DELIVERY_ADAPTER
    assert source.project_id == facts["project"].project_id
    assert source.source_order_id == facts["order"].raw_order_id
    assert source.source_line_id == facts["line"].source_line_id
    assert source.part_id == facts["part"].id
    assert source.pn == facts["part"].pn_std
    assert source.serial_number == facts["line"].sn
    assert source.delivered_quantity == Decimal("3.000")
    assert source.mapping_state == "ready"
    assert source.is_active is True
    assert len(source.mapping_version) == 64
    assert int(db.scalar(select(func.count()).select_from(Inventory)) or 0) == (
        inventory_before
    )


def test_exact_warehouse_candidate_can_confirm_under_production_gate(
    db,
    monkeypatch,
):
    facts = _seed_exact_shipment(db)
    bridge.synchronize_delivery_sources(
        db,
        document_ids={facts["document"].document_id},
    )
    db.commit()
    inventory_before = int(
        db.scalar(select(func.count()).select_from(Inventory)) or 0
    )
    draft = operations.create_site_issue_draft(
        db,
        project_id=facts["project"].project_id,
        idempotency_key="synthetic-warehouse-production-draft",
        issue_date=date(2026, 8, 3),
        receiver="合成接收人",
        issued_by="合成发出人",
        site_location="合成现场",
        lines=[{"delivery_line_id": facts["line"].line_id, "quantity": "1"}],
        reason="验证正式仓库来源生产确认",
        operated_by="synthetic-admin",
    )
    db.commit()
    assert draft is not None
    monkeypatch.setattr(
        operations,
        "_site_issue_is_production_blocked",
        lambda: True,
    )

    preview = operations.preview_site_issue(
        db,
        issue_id=draft["issue_id"],
        project_id=facts["project"].project_id,
        version=draft["version"],
    )
    assert preview is not None and preview["can_confirm"] is True
    confirmed = operations.confirm_site_issue(
        db,
        issue_id=draft["issue_id"],
        project_id=facts["project"].project_id,
        version=draft["version"],
        idempotency_key="synthetic-warehouse-production-confirm",
        reason="确认正式仓库现场领用事实",
        operated_by="synthetic-admin",
    )
    db.commit()

    assert confirmed is not None
    assert confirmed["normalized_status"] == "confirmed"
    assert confirmed["inventory_effect"] == "none"
    assert db.get(MaintenanceSiteIssue, draft["issue_id"]).normalized_status == (
        "confirmed"
    )
    assert int(db.scalar(select(func.count()).select_from(Inventory)) or 0) == (
        inventory_before
    )


def test_open_mapping_ambiguity_deactivates_candidate_and_empty_scope_is_noop(db):
    facts = _seed_exact_shipment(db)
    bridge.synchronize_delivery_sources(
        db,
        document_ids={facts["document"].document_id},
    )
    db.commit()
    source = db.get(MaintenanceSiteIssueDeliverySource, facts["line"].line_id)
    assert source is not None and source.is_active is True

    no_op = bridge.synchronize_delivery_sources(db, document_ids=set())
    db.commit()
    assert no_op == {"created": 0, "updated": 0, "deactivated": 0, "eligible": 0}
    assert db.get(MaintenanceSiteIssueDeliverySource, source.delivery_line_id).is_active

    ambiguity = MaintenanceWarehouseAmbiguity(
        ambiguity_id=_uuid(109),
        import_id=facts["batch"].import_id,
        document_id=facts["document"].document_id,
        line_id=facts["line"].line_id,
        ambiguity_type="field_conflict",
        field_code="document_line",
        source_row=3,
        value_hash="8" * 64,
        candidates_json=[],
        evidence_json={"reason": "synthetic"},
        fingerprint="9" * 64,
        status="open",
        version=1,
    )
    db.add(ambiguity)
    db.commit()

    blocked = bridge.synchronize_delivery_sources(
        db,
        delivery_line_ids={facts["line"].line_id},
    )
    db.commit()
    assert blocked == {"created": 0, "updated": 0, "deactivated": 1, "eligible": 0}
    assert not db.get(
        MaintenanceSiteIssueDeliverySource,
        source.delivery_line_id,
    ).is_active


def test_tombstoned_wbdd_deactivates_existing_candidate(db):
    facts = _seed_exact_shipment(db)
    bridge.synchronize_delivery_sources(
        db,
        document_ids={facts["document"].document_id},
    )
    db.commit()
    now = datetime.now(UTC)
    intent = MaintenanceDemandDeleteIntent(
        intent_id=_uuid(110),
        idempotency_key="synthetic-warehouse-bridge-delete-intent",
        request_digest="a" * 64,
        selection_digest="b" * 64,
        status="executed",
        reason="合成逻辑删除",
        operated_by="synthetic-admin",
        header_count=1,
        line_count=0,
        created_at=now - timedelta(seconds=10),
        not_before=now - timedelta(seconds=3),
        expires_at=now + timedelta(minutes=10),
        executed_at=now,
        terminal_at=now,
        result_json={"status": "executed"},
    )
    tombstone = MaintenanceDemandTombstone(
        source_order_id=facts["order"].raw_order_id,
        delete_intent_id=intent.intent_id,
        version_digest="c" * 64,
        deleted_by="synthetic-admin",
        delete_reason="合成逻辑删除",
        deleted_at=now,
        version=1,
    )
    db.add_all([intent, tombstone])
    db.commit()

    result = bridge.synchronize_delivery_sources(
        db,
        source_order_ids={facts["order"].raw_order_id},
    )
    db.commit()
    assert result["deactivated"] == 1
    assert not db.get(
        MaintenanceSiteIssueDeliverySource,
        facts["line"].line_id,
    ).is_active


def test_project_assignment_mismatch_fails_closed(db):
    facts = _seed_exact_shipment(db)
    bridge.synchronize_delivery_sources(
        db,
        document_ids={facts["document"].document_id},
    )
    db.commit()
    second_project = MaintenanceProject(
        project_id=_uuid(111),
        project_code="SYN-BRIDGE-PROJECT-002",
        display_name="合成改派项目",
        lifecycle_status="ongoing",
    )
    db.add(second_project)
    db.flush()
    old_assignment = db.get(
        MaintenanceSourceOrderAssignment,
        facts["assignment"].assignment_id,
    )
    now = datetime.now(UTC)
    old_assignment.is_active = False
    old_assignment.version += 1
    old_assignment.archived_by = "synthetic-admin"
    old_assignment.archived_at = now
    db.flush()
    db.add(
        MaintenanceSourceOrderAssignment(
            assignment_id=_uuid(112),
            source_order_id=facts["order"].raw_order_id,
            project_id=second_project.project_id,
            is_active=True,
            version=1,
            created_by="synthetic-admin",
        )
    )
    db.commit()

    result = bridge.synchronize_delivery_sources(
        db,
        source_order_ids={facts["order"].raw_order_id},
    )
    db.commit()
    assert result["deactivated"] == 1
    assert not db.get(
        MaintenanceSiteIssueDeliverySource,
        facts["line"].line_id,
    ).is_active
