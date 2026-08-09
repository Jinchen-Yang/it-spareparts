"""Atomic warehouse preview/apply behavior with synthetic business values only."""

from __future__ import annotations

from datetime import date
import io

from openpyxl import Workbook
import pytest
from sqlalchemy import func, select

from app import config
from app.models.dimensions import DimPart
from app.models.inventory import Inventory
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_warehouse import (
    MaintenanceWarehouseAmbiguity,
    MaintenanceWarehouseAuditEvent,
    MaintenanceWarehouseDocument,
    MaintenanceWarehouseDocumentLine,
    MaintenanceWarehouseDocumentLink,
    MaintenanceWarehouseImportBatch,
)
from app.security import UserContext
from app.models.system import SysImportBatch
from app.services import maintenance_warehouse
from app.services.maintenance_warehouse_adapters import parse_warehouse_workbook


SHIPMENT_PREFIX = "D107407Fvxu6voev32rlg4pkdu6nvdc83"
HMAC_KEY = b"synthetic-preview-signing-key-for-tests"


def _workbook(
    *,
    wbdd: str = "SYN-WBDD-001",
    pn: str = "SYN-PN-001",
    quantity: int = 2,
) -> bytes:
    headers = [
        ("ObjectId", "数据ID(不可修改)"),
        ("SeqNo", "出库单号(必填)"),
        ("F0000001", "出库日期(必填)"),
        ("F0000032", "出库类别(必填)"),
        ("F0000061", "出库备件/整机(必填)"),
        ("Status", "数据状态"),
        ("F0000151", "维保需求单(备件)(必填)"),
        (f"{SHIPMENT_PREFIX}.ObjectId", "备件明细.数据ID(不可修改)"),
        (f"{SHIPMENT_PREFIX}.F0000031", "备件明细.备件PN(必填)"),
        (f"{SHIPMENT_PREFIX}.F0000044", "备件明细.备件SN号(必填)"),
        (f"{SHIPMENT_PREFIX}.F0000011", "备件明细.出库数量"),
    ]
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([code for code, _label in headers])
    sheet.append([label for _code, label in headers])
    sheet.append([
        "SYN-DOC-001", "SYN-SHIP-001", "2026-08-01", "维保", "备件", "已完成",
        wbdd, "SYN-LINE-001", pn, "SYN-SN-001", quantity,
    ])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


@pytest.fixture(autouse=True)
def _approve_synthetic_shipment_contract(monkeypatch):
    parsed = parse_warehouse_workbook(_workbook())
    contract = tuple(
        (pair.internal_code, pair.business_label) for pair in parsed.header_pairs
    )
    monkeypatch.setattr(
        config,
        "MAINTENANCE_WAREHOUSE_APPROVED_HEADER_CONTRACTS",
        {"shipment_v1": (contract,)},
    )


def _seed_stable_targets(db, *, duplicate_order_no: bool = False) -> None:
    batch = SysImportBatch(
        filename="synthetic-source.xlsx",
        file_type="maintenance",
        file_hash="synthetic-source-hash",
        status="success",
    )
    db.add(batch)
    db.flush()
    db.add(DimPart(
        pn_std="SYN-PN-001",
        status="active",
        master_source="import",
        locked_fields=[],
    ))
    db.add(FMaintenanceOrder(
        raw_order_id="SYN-WBDD-001",
        order_no="SYN-WBDD-001" if not duplicate_order_no else "SYN-DUPLICATE-WBDD",
        order_date=date(2026, 8, 1),
        import_batch_id=batch.id,
    ))
    if duplicate_order_no:
        db.add(FMaintenanceOrder(
            raw_order_id="SYN-WBDD-002",
            order_no="SYN-DUPLICATE-WBDD",
            order_date=date(2026, 8, 1),
            import_batch_id=batch.id,
        ))
    db.commit()


def _counts(db) -> dict[str, int]:
    models = {
        "batches": MaintenanceWarehouseImportBatch,
        "documents": MaintenanceWarehouseDocument,
        "lines": MaintenanceWarehouseDocumentLine,
        "links": MaintenanceWarehouseDocumentLink,
        "ambiguities": MaintenanceWarehouseAmbiguity,
        "audits": MaintenanceWarehouseAuditEvent,
        "inventory": Inventory,
        "maintenance_lines": FMaintenanceLine,
    }
    return {
        key: db.scalar(select(func.count()).select_from(model)) or 0
        for key, model in models.items()
    }


def test_preview_is_zero_write_and_apply_replay_is_zero_change(db):
    _seed_stable_targets(db)
    content = _workbook()
    before = _counts(db)

    preview = maintenance_warehouse.preview_import(
        content, filename="synthetic-shipment.xlsx", hmac_key=HMAC_KEY
    )

    assert _counts(db) == before
    assert preview["adapter_version"] == "shipment_v1"
    assert preview["version_state"] == "known"
    assert preview["can_apply"] is True

    applied = maintenance_warehouse.apply_import(
        db,
        content,
        filename="synthetic-shipment.xlsx",
        import_id=preview["import_id"],
        preview_token=preview["preview_token"],
        reason="合成测试：首次导入",
        operated_by="synthetic-admin",
        hmac_key=HMAC_KEY,
    )
    db.commit()
    after_first = _counts(db)
    assert applied["writes"] == {
        "documents": 1,
        "lines": 1,
        "links": 2,
        "ambiguities": 2,
        "audits": 1,
    }
    assert after_first["inventory"] == before["inventory"]
    assert after_first["maintenance_lines"] == before["maintenance_lines"]
    assert set(db.scalars(select(MaintenanceWarehouseDocumentLink.link_kind))) == {
        "maintenance_order",
        "part",
    }
    assert set(db.scalars(
        select(MaintenanceWarehouseAmbiguity.field_code).where(
            MaintenanceWarehouseAmbiguity.ambiguity_type == "integration_blocker"
        )
    )) == {"project_assignment_contract", "site_issue_source_contract"}

    replay = maintenance_warehouse.apply_import(
        db,
        content,
        filename="renamed-synthetic-shipment.xlsx",
        import_id=preview["import_id"],
        preview_token=preview["preview_token"],
        reason="合成测试：幂等重放",
        operated_by="synthetic-admin",
        hmac_key=HMAC_KEY,
    )
    db.commit()
    assert replay["idempotent_replay"] is True
    assert set(replay["writes"].values()) == {0}
    assert _counts(db) == after_first


def test_unapproved_full_header_is_preview_only_and_cannot_apply(db, monkeypatch):
    monkeypatch.setattr(
        config,
        "MAINTENANCE_WAREHOUSE_APPROVED_HEADER_CONTRACTS",
        {},
    )
    content = _workbook()
    before = _counts(db)
    preview = maintenance_warehouse.preview_import(
        content, filename="synthetic-unapproved.xlsx", hmac_key=HMAC_KEY
    )

    assert preview["version_state"] == "unknown_version"
    assert preview["can_apply"] is False
    assert preview["header_diff"]["state"] == "approved_baseline_unavailable"
    with pytest.raises(
        maintenance_warehouse.MaintenanceWarehouseConflict,
        match="只允许零写入预览",
    ):
        maintenance_warehouse.apply_import(
            db,
            content,
            filename="synthetic-unapproved.xlsx",
            import_id=preview["import_id"],
            preview_token=preview["preview_token"],
            reason="合成未批准模板必须失败关闭",
            operated_by="synthetic-admin",
            hmac_key=HMAC_KEY,
        )
    assert _counts(db) == before


def test_database_lock_key_is_stable_business_identity_not_file_hash():
    left = parse_warehouse_workbook(_workbook(quantity=1))
    right = parse_warehouse_workbook(_workbook(quantity=9))
    assert left.source_file_hash != right.source_file_hash

    class RecordingSession:
        def __init__(self):
            self.keys: list[int] = []

        def execute(self, _statement, parameters):
            self.keys.append(parameters["key"])

    left_session = RecordingSession()
    right_session = RecordingSession()
    maintenance_warehouse._lock_business_identities(left_session, left)  # noqa: SLF001
    maintenance_warehouse._lock_business_identities(right_session, right)  # noqa: SLF001

    assert left_session.keys == right_session.keys


def test_same_stable_line_with_changed_payload_is_conflict_not_mutation(db):
    _seed_stable_targets(db)
    first_content = _workbook(quantity=1)
    first_preview = maintenance_warehouse.preview_import(
        first_content, filename="synthetic-a.xlsx", hmac_key=HMAC_KEY
    )
    maintenance_warehouse.apply_import(
        db, first_content, filename="synthetic-a.xlsx",
        import_id=first_preview["import_id"], preview_token=first_preview["preview_token"],
        reason="合成首次导入", operated_by="synthetic-admin", hmac_key=HMAC_KEY,
    )
    db.commit()
    original = db.scalar(select(MaintenanceWarehouseDocumentLine))
    original_fingerprint = original.raw_fingerprint
    links_before = db.scalar(select(func.count()).select_from(MaintenanceWarehouseDocumentLink))
    db.add(DimPart(
        pn_std="SYN-PN-CHANGED",
        status="active",
        master_source="import",
        locked_fields=[],
    ))
    db.commit()

    changed_content = _workbook(pn="SYN-PN-CHANGED", quantity=9)
    changed_preview = maintenance_warehouse.preview_import(
        changed_content, filename="synthetic-b.xlsx", hmac_key=HMAC_KEY
    )
    changed = maintenance_warehouse.apply_import(
        db, changed_content, filename="synthetic-b.xlsx",
        import_id=changed_preview["import_id"], preview_token=changed_preview["preview_token"],
        reason="合成冲突验证", operated_by="synthetic-admin", hmac_key=HMAC_KEY,
    )
    db.commit()

    assert changed["new_document_count"] == 0
    assert changed["new_line_count"] == 0
    assert changed["new_link_count"] == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceWarehouseDocumentLine)) == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceWarehouseDocumentLink)) == links_before
    assert db.scalar(select(MaintenanceWarehouseDocumentLine)).raw_fingerprint == original_fingerprint
    types = set(db.scalars(
        select(MaintenanceWarehouseAmbiguity.ambiguity_type).where(
            MaintenanceWarehouseAmbiguity.import_id == changed["import_id"]
        )
    ))
    assert "field_conflict" in types


def test_same_stable_document_with_changed_header_never_adds_new_automatic_link(db):
    _seed_stable_targets(db)
    first_content = _workbook()
    first_preview = maintenance_warehouse.preview_import(
        first_content, filename="synthetic-header-a.xlsx", hmac_key=HMAC_KEY
    )
    maintenance_warehouse.apply_import(
        db, first_content, filename="synthetic-header-a.xlsx",
        import_id=first_preview["import_id"], preview_token=first_preview["preview_token"],
        reason="合成首次导入", operated_by="synthetic-admin", hmac_key=HMAC_KEY,
    )
    db.commit()
    source_batch_id = db.scalar(select(SysImportBatch.id).where(
        SysImportBatch.filename == "synthetic-source.xlsx"
    ))
    db.add(FMaintenanceOrder(
        raw_order_id="SYN-WBDD-CHANGED",
        order_no="SYN-WBDD-CHANGED",
        order_date=date(2026, 8, 2),
        import_batch_id=source_batch_id,
    ))
    db.commit()
    links_before = db.scalar(select(func.count()).select_from(MaintenanceWarehouseDocumentLink))

    changed_content = _workbook(wbdd="SYN-WBDD-CHANGED")
    changed_preview = maintenance_warehouse.preview_import(
        changed_content, filename="synthetic-header-b.xlsx", hmac_key=HMAC_KEY
    )
    result = maintenance_warehouse.apply_import(
        db, changed_content, filename="synthetic-header-b.xlsx",
        import_id=changed_preview["import_id"], preview_token=changed_preview["preview_token"],
        reason="合成表头冲突验证", operated_by="synthetic-admin", hmac_key=HMAC_KEY,
    )
    db.commit()

    assert result["new_link_count"] == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceWarehouseDocumentLink)) == links_before
    assert db.scalar(select(func.count()).select_from(MaintenanceWarehouseAmbiguity).where(
        MaintenanceWarehouseAmbiguity.import_id == result["import_id"],
        MaintenanceWarehouseAmbiguity.ambiguity_type == "field_conflict",
    )) >= 1


def test_multiple_exact_wbdd_candidates_are_ambiguous_not_auto_linked(db):
    _seed_stable_targets(db, duplicate_order_no=True)
    content = _workbook(wbdd="SYN-DUPLICATE-WBDD")
    preview = maintenance_warehouse.preview_import(
        content, filename="synthetic-duplicate.xlsx", hmac_key=HMAC_KEY
    )
    result = maintenance_warehouse.apply_import(
        db, content, filename="synthetic-duplicate.xlsx",
        import_id=preview["import_id"], preview_token=preview["preview_token"],
        reason="合成多候选验证", operated_by="synthetic-admin", hmac_key=HMAC_KEY,
    )
    db.commit()

    assert result["new_link_count"] == 1  # PN only; WBDD must remain unresolved.
    ambiguity = db.scalar(select(MaintenanceWarehouseAmbiguity).where(
        MaintenanceWarehouseAmbiguity.import_id == result["import_id"],
        MaintenanceWarehouseAmbiguity.ambiguity_type == "multiple_candidates",
    ))
    assert ambiguity is not None
    assert len(ambiguity.candidates_json) == 2

    source_batch_id = db.scalar(select(SysImportBatch.id).where(
        SysImportBatch.filename == "synthetic-source.xlsx"
    ))
    db.add(FMaintenanceOrder(
        raw_order_id="SYN-UNRELATED-WBDD",
        order_no="SYN-UNRELATED-WBDD",
        order_date=date(2026, 8, 3),
        import_batch_id=source_batch_id,
    ))
    db.commit()
    with pytest.raises(
        maintenance_warehouse.MaintenanceWarehouseError,
        match="候选集合",
    ):
        maintenance_warehouse.resolve_ambiguity(
            db,
            ambiguity_id=ambiguity.ambiguity_id,
            version=ambiguity.version,
            reason="合成验证：不得选择无关稳定目标",
            operated_by="synthetic-admin",
            decision="link",
            link_kind="maintenance_order",
            target_type="maintenance_order",
            target_id="SYN-UNRELATED-WBDD",
            user_ctx=UserContext(
                user_id="synthetic-admin",
                role="admin",
                is_authenticated=True,
            ),
        )
    db.rollback()


def test_candidate_bound_manual_correction_keeps_one_active_link_and_full_history(db):
    _seed_stable_targets(db)
    content = _workbook()
    preview = maintenance_warehouse.preview_import(
        content, filename="synthetic-correction.xlsx", hmac_key=HMAC_KEY
    )
    result = maintenance_warehouse.apply_import(
        db,
        content,
        filename="synthetic-correction.xlsx",
        import_id=preview["import_id"],
        preview_token=preview["preview_token"],
        reason="合成首次稳定关联",
        operated_by="synthetic-admin",
        hmac_key=HMAC_KEY,
    )
    db.commit()
    document = db.scalar(select(MaintenanceWarehouseDocument))
    original = db.scalar(select(MaintenanceWarehouseDocumentLink).where(
        MaintenanceWarehouseDocumentLink.link_kind == "maintenance_order",
        MaintenanceWarehouseDocumentLink.status == "active",
    ))
    source_batch_id = db.scalar(select(SysImportBatch.id).where(
        SysImportBatch.filename == "synthetic-source.xlsx"
    ))
    db.add(FMaintenanceOrder(
        raw_order_id="SYN-WBDD-CORRECTED",
        order_no="SYN-WBDD-CORRECTED",
        order_date=date(2026, 8, 2),
        import_batch_id=source_batch_id,
    ))
    ambiguity = MaintenanceWarehouseAmbiguity(
        ambiguity_id="00000000-0000-0000-0000-000000000219",
        import_id=result["import_id"],
        document_id=document.document_id,
        line_id=None,
        ambiguity_type="field_conflict",
        field_code="maintenance_order",
        source_row=3,
        value_hash="a" * 64,
        candidates_json=[
            {
                "target_type": "maintenance_order",
                "target_id": "SYN-WBDD-001",
                "label": "当前有效关联",
            },
            {
                "target_type": "maintenance_order",
                "target_id": "SYN-WBDD-CORRECTED",
                "label": "合成人工核实目标",
            },
        ],
        fingerprint="b" * 64,
        status="open",
        version=1,
    )
    db.add(ambiguity)
    db.commit()

    resolved = maintenance_warehouse.resolve_ambiguity(
        db,
        ambiguity_id=ambiguity.ambiguity_id,
        version=1,
        reason="合成核实：更正到候选集合内的稳定 WBDD",
        operated_by="synthetic-admin",
        decision="link",
        link_kind="maintenance_order",
        target_type="maintenance_order",
        target_id="SYN-WBDD-CORRECTED",
        user_ctx=UserContext(
            user_id="synthetic-admin", role="admin", is_authenticated=True
        ),
    )
    db.commit()

    links = list(db.scalars(
        select(MaintenanceWarehouseDocumentLink)
        .where(MaintenanceWarehouseDocumentLink.link_kind == "maintenance_order")
        .order_by(MaintenanceWarehouseDocumentLink.created_at)
    ))
    assert resolved["resolution"]["link_action"] == "corrected"
    assert len(links) == 2
    assert [link.status for link in links].count("active") == 1
    active = next(link for link in links if link.status == "active")
    superseded = next(link for link in links if link.status == "superseded")
    assert active.target_id == "SYN-WBDD-CORRECTED"
    assert active.supersedes_link_id == original.link_id
    assert superseded.link_id == original.link_id
    audit = db.scalar(select(MaintenanceWarehouseAuditEvent).where(
        MaintenanceWarehouseAuditEvent.ambiguity_id == ambiguity.ambiguity_id
    ))
    assert audit.before_json["active_link"]["target_id"] == "SYN-WBDD-001"
    assert audit.after_json["resolution"]["target_id"] == "SYN-WBDD-CORRECTED"


def test_apply_failure_rolls_back_every_planned_table(db, monkeypatch):
    _seed_stable_targets(db)
    content = _workbook()
    preview = maintenance_warehouse.preview_import(
        content, filename="synthetic-atomic.xlsx", hmac_key=HMAC_KEY
    )
    before = _counts(db)
    original_flush = db.flush
    calls = 0

    def fail_after_batch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic atomic failure")
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(db, "flush", fail_after_batch)
    with pytest.raises(RuntimeError, match="synthetic atomic failure"):
        maintenance_warehouse.apply_import(
            db, content, filename="synthetic-atomic.xlsx",
            import_id=preview["import_id"], preview_token=preview["preview_token"],
            reason="合成原子回滚验证", operated_by="synthetic-admin", hmac_key=HMAC_KEY,
        )
    db.rollback()
    assert _counts(db) == before
