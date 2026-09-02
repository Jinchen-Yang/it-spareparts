"""XSDD is the canonical maintenance-project identity (#56)."""

import io
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.etl import loader
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAlias,
    MaintenanceProjectAuditLog,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
    MaintenanceProjectXsdd,
)
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectWorkbookOperation,
    MaintenanceProjectWorkbookState,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch, SysUser
from app.security import UserContext
from app.services import maintenance_project_operations as operations
from app.services import maintenance_boss_board
from app.services import maintenance_ledger
from app.services import maintenance_project_identity
from app.services import maintenance_source_assignments as assignments
from tests import factories as f


def _admin() -> UserContext:
    return UserContext(user_id="xsdd-admin", role="admin", is_authenticated=True)


def _project(db, project_id: str, code: str, name: str) -> MaintenanceProject:
    row = MaintenanceProject(
        project_id=project_id,
        project_code=code,
        display_name=name,
        lifecycle_status="ongoing",
        period_from=date(2026, 1, 1),
        period_to=date(2026, 12, 31),
        is_active=True,
        version=1,
    )
    db.add(row)
    db.commit()
    return row


def _load_same_xsdd_different_names(db) -> list[FMaintenanceOrder]:
    batch = SysImportBatch(
        filename="xsdd-identity.xlsx",
        file_type="maintenance",
        file_hash="xsdd-identity".ljust(64, "0"),
        status="success",
    )
    db.add(batch)
    db.flush()
    heads = {
        "WBDD-XSDD-1": f.maintenance_head(
            "WBDD-XSDD-1",
            project="腾讯TCE 2025维保项目",
            sales_order="XSDD-20251017-0036",
        ),
        "WBDD-XSDD-2": f.maintenance_head(
            "WBDD-XSDD-2",
            project="腾讯整体维保项目-预交付",
            sales_order="20251017-0036",
        ),
    }
    lines = [
        f.maintenance_line(raw_id, f"{raw_id}-L1", "PN-XSDD-1")
        for raw_id in heads
    ]
    loader.load(
        db,
        f.maintenance_result(heads, lines),
        batch.id,
        date(2026, 8, 28),
        mode="upsert",
    )
    db.commit()
    return list(db.scalars(select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id.in_(sorted(heads))
    )))


def _upsert_wbdd_xsdd(
    db,
    *,
    raw_order_id: str,
    sales_order: str | None,
    batch_key: str,
) -> None:
    batch = SysImportBatch(
        filename=f"{batch_key}.xlsx",
        file_type="maintenance",
        file_hash=batch_key.ljust(64, "0")[:64],
        status="success",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.maintenance_result(
            {
                raw_order_id: f.maintenance_head(
                    raw_order_id,
                    project="WBDD XSDD guard project",
                    sales_order=sales_order,
                )
            },
            [
                f.maintenance_line(
                    raw_order_id,
                    f"{raw_order_id}-L1",
                    "PN-XSDD-GUARD",
                )
            ],
        ),
        batch.id,
        date(2026, 8, 31),
        mode="upsert",
    )


def _ledger_bytes(*, order_no: str, project_name: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "维保项目清单"
    sheet.append([
        "订单编号", "订单日期", "销售人员", "业务类型", "项目名称",
        "维保起始日期", "维保终止日期", "CMO", "项目经理", "订单金额",
        "已收尾款", "待收尾款", "验收材料", "验收材料是否完成及上传附件",
        "巡检时间", "巡检是否完成及上传附件",
    ])
    sheet.append([
        order_no, "2026-08-28", "测试销售", "整体维保", project_name,
        "2026-01-01", "2026-12-31", "测试CMO", "测试经理", 100, 0, 100,
        "", "", "", "",
    ])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _seed_mergeable_contract_conflict(db, *, suffix: str):
    first = _project(db, f"merge-a-{suffix}", f"MERGE-A-{suffix}", "腾讯TCE名称")
    second = _project(db, f"merge-b-{suffix}", f"MERGE-B-{suffix}", "腾讯预交付名称")
    xsdd = f"20251017-{suffix}"
    db.execute(text(
        "ALTER TABLE maintenance_project_contract DISABLE TRIGGER "
        "trg_maintenance_contract_claim_xsdd"
    ))
    try:
        current = MaintenanceProjectContract(
            project_contract_id=f"merge-contract-current-{suffix}",
            project_id=first.project_id,
            contract_id=f"xsdd-XSDD-{xsdd}",
            contract_no=f"XSDD-{xsdd}",
            contract_amount=Decimal("1575471.70"),
            amount_inc_tax=Decimal("1780283.02"),
            contract_status="正常",
            status_mapping_state="mapped",
            status_mapping_version="merge-test-v1",
            included_in_total=True,
            effective_from=date(2025, 10, 16),
            effective_to=None,
            source="sales_fallback",
            version=1,
        )
        historical = MaintenanceProjectContract(
            project_contract_id=f"merge-contract-history-{suffix}",
            project_id=second.project_id,
            contract_id=f"xsdd-XSDD-{xsdd}",
            contract_no=f"XSDD-{xsdd}",
            contract_amount=Decimal("1575471.70"),
            amount_inc_tax=Decimal("1670000.00"),
            contract_status="正常",
            status_mapping_state="mapped",
            status_mapping_version="merge-test-v1",
            included_in_total=True,
            effective_from=date(2025, 10, 16),
            effective_to=None,
            source="sales_fallback",
            version=2,
        )
        db.add_all([current, historical])
        db.flush()
    finally:
        db.execute(text(
            "ALTER TABLE maintenance_project_contract ENABLE TRIGGER "
            "trg_maintenance_contract_claim_xsdd"
        ))
    survivor = MaintenanceCollectionSnapshot(
        collection_id=f"merge-collection-current-{suffix}",
        project_id=first.project_id,
        project_contract_id=current.project_contract_id,
        report_month=date(2026, 6, 1),
        cumulative_amount=Decimal("82325.40"),
        status="confirmed",
        receipt_reference="SKD-20260630-0007",
        remark=None,
        source="direct_api",
        import_batch_id=None,
        version=1,
    )
    duplicate = MaintenanceCollectionSnapshot(
        collection_id=f"merge-collection-history-{suffix}",
        project_id=second.project_id,
        project_contract_id=historical.project_contract_id,
        report_month=date(2026, 6, 1),
        cumulative_amount=Decimal("82325.40"),
        status="confirmed",
        receipt_reference="SKD-20260630-0007",
        remark=None,
        source="direct_api",
        import_batch_id=None,
        version=9,
    )
    db.add_all([survivor, duplicate])
    db.commit()
    return first, second, current, historical, survivor, duplicate, xsdd


def test_auto_assign_groups_same_xsdd_and_retains_names_as_aliases(db):
    project = _project(
        db,
        "xsdd-auto-contract-owner",
        "XSDD-AUTO-CONTRACT",
        "腾讯销售合同项目",
    )
    operations.create_contract(
        db,
        project_id=project.project_id,
        contract_id="xsdd-auto-contract-owner-contract",
        contract_no="XSDD-20251017-0036",
        contract_amount=Decimal("100.00"),
        contract_status="正常",
        status_mapping_state="mapped",
        status_mapping_version="xsdd-test-v1",
        included_in_total=True,
        effective_from=date(2025, 10, 16),
        effective_to=None,
        source="test",
        reason="销售合同先建立 XSDD owner",
        operated_by="xsdd-admin",
    )
    db.commit()
    orders = _load_same_xsdd_different_names(db)

    result = assignments.auto_assign_unassigned(
        db,
        operated_by="xsdd-admin",
        user_ctx=_admin(),
    )
    db.commit()

    owner_ids = set(db.scalars(select(MaintenanceSourceOrderAssignment.project_id).where(
        MaintenanceSourceOrderAssignment.source_order_id.in_(
            [order.raw_order_id for order in orders]
        ),
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )))
    assert len(owner_ids) == 1
    project_id = next(iter(owner_ids))
    assert project_id == project.project_id
    assert result["created_projects"] == 0
    assert result["assigned_orders"] == 2
    mapping = db.get(MaintenanceProjectXsdd, "20251017-0036")
    assert mapping is not None and mapping.project_id == project_id
    aliases = set(db.scalars(select(MaintenanceProjectAlias.alias_name).where(
        MaintenanceProjectAlias.project_id == project_id
    )))
    assert aliases >= {
        "腾讯TCE 2025维保项目",
        "腾讯整体维保项目-预交付",
    }


def test_cross_project_contract_claim_fails_closed(db):
    first = _project(db, "xsdd-project-a", "XSDD-A", "XSDD项目甲")
    second = _project(db, "xsdd-project-b", "XSDD-B", "XSDD项目乙")
    created = operations.create_contract(
        db,
        project_id=first.project_id,
        contract_id="xsdd-XSDD-20260828-0001",
        contract_no="XSDD-20260828-0001",
        contract_amount=Decimal("106.00"),
        contract_status="正常",
        status_mapping_state="mapped",
        status_mapping_version="xsdd-test-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        source="test",
        reason="验证 XSDD 单项目归属",
        operated_by="xsdd-admin",
    )
    assert created is not None
    db.commit()

    with pytest.raises(operations.MaintenanceOperationConflict, match="不能再次拆分"):
        operations.create_contract(
            db,
            project_id=second.project_id,
            contract_id="xsdd-20260828-0001",
            contract_no="20260828-0001",
            contract_amount=Decimal("106.00"),
            contract_status="正常",
            status_mapping_state="mapped",
            status_mapping_version="xsdd-test-v1",
            included_in_total=True,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            source="test",
            reason="验证跨项目拆分被拒绝",
            operated_by="xsdd-admin",
        )
    db.rollback()


def test_wbdd_upsert_rejects_xsdd_owned_by_another_project(db):
    project_a = _project(db, "xsdd-wbdd-project-a", "XSDD-WBDD-A", "WBDD项目甲")
    project_b = _project(db, "xsdd-wbdd-project-b", "XSDD-WBDD-B", "WBDD项目乙")
    raw_order_id = "WBDD-XSDD-GUARD-001"
    claimed_xsdd = "XSDD-20991231-0001"

    _upsert_wbdd_xsdd(
        db,
        raw_order_id=raw_order_id,
        sales_order=None,
        batch_key="xsdd-wbdd-guard-initial",
    )
    assignments.assign_source_orders(
        db,
        project_id=project_a.project_id,
        items=[{"source_order_id": raw_order_id}],
        reason="建立项目甲的 active assignment",
        operated_by="xsdd-admin",
        user_ctx=_admin(),
    )
    db.commit()
    operations.create_contract(
        db,
        project_id=project_b.project_id,
        contract_id="xsdd-wbdd-project-b-contract",
        contract_no=claimed_xsdd,
        contract_amount=Decimal("100.00"),
        contract_status="正常",
        status_mapping_state="mapped",
        status_mapping_version="xsdd-test-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        source="test",
        reason="建立项目乙的既有 XSDD 归属",
        operated_by="xsdd-admin",
    )
    db.commit()

    with pytest.raises(IntegrityError):
        _upsert_wbdd_xsdd(
            db,
            raw_order_id=raw_order_id,
            sales_order=claimed_xsdd,
            batch_key="xsdd-wbdd-guard-conflict",
        )
    db.rollback()

    order = db.scalar(select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id == raw_order_id
    ))
    assert order is not None and order.linked_sales_order_no is None
    assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    ))
    assert assignment is not None and assignment.project_id == project_a.project_id
    mapping = db.get(MaintenanceProjectXsdd, "20991231-0001")
    assert mapping is not None and mapping.project_id == project_b.project_id


def test_wbdd_upsert_cannot_claim_unowned_xsdd_for_assigned_project(db):
    project_a = _project(
        db,
        "xsdd-wbdd-project-unowned-a",
        "XSDD-WBDD-UNOWNED-A",
        "WBDD未认领项目甲",
    )
    raw_order_id = "WBDD-XSDD-GUARD-UNOWNED-001"
    new_xsdd = "XSDD-20991231-0002"

    _upsert_wbdd_xsdd(
        db,
        raw_order_id=raw_order_id,
        sales_order=None,
        batch_key="xsdd-wbdd-guard-unowned-initial",
    )
    assignments.assign_source_orders(
        db,
        project_id=project_a.project_id,
        items=[{"source_order_id": raw_order_id}],
        reason="建立新 XSDD 写入前的 active assignment",
        operated_by="xsdd-admin",
        user_ctx=_admin(),
    )
    db.commit()

    with pytest.raises(IntegrityError):
        _upsert_wbdd_xsdd(
            db,
            raw_order_id=raw_order_id,
            sales_order=new_xsdd,
            batch_key="xsdd-wbdd-guard-unowned-update",
        )
    db.rollback()

    order = db.scalar(select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id == raw_order_id
    ))
    assert order is not None and order.linked_sales_order_no is None
    mapping = db.get(MaintenanceProjectXsdd, "20991231-0002")
    assert mapping is None


def test_assignment_requires_matching_contract_owner_even_if_map_exists(db):
    project = _project(db, "xsdd-map-only-project", "XSDD-MAP-ONLY", "仅映射项目")
    raw_order_id = "WBDD-XSDD-MAP-ONLY"
    xsdd = "XSDD-20991231-0003"
    _upsert_wbdd_xsdd(
        db,
        raw_order_id=raw_order_id,
        sales_order=xsdd,
        batch_key="xsdd-map-only-order",
    )
    db.add(MaintenanceProjectXsdd(
        xsdd_norm="20991231-0003",
        project_id=project.project_id,
        source="test-map-only",
    ))
    db.commit()

    db.add(MaintenanceSourceOrderAssignment(
        assignment_id="xsdd-map-only-assignment",
        source_order_id=raw_order_id,
        project_id=project.project_id,
        is_active=True,
        version=1,
        created_by="xsdd-admin",
    ))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    assert db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == raw_order_id
    )) is None
    mapping = db.get(MaintenanceProjectXsdd, "20991231-0003")
    assert mapping is not None and mapping.project_id == project.project_id


def test_assignment_allows_matching_contract_owner(db):
    project = _project(db, "xsdd-contract-owner", "XSDD-CONTRACT", "合同 owner 项目")
    raw_order_id = "WBDD-XSDD-CONTRACT-OWNER"
    xsdd = "XSDD-20991231-0004"
    _upsert_wbdd_xsdd(
        db,
        raw_order_id=raw_order_id,
        sales_order=xsdd,
        batch_key="xsdd-contract-owner-order",
    )
    operations.create_contract(
        db,
        project_id=project.project_id,
        contract_id="xsdd-contract-owner-contract",
        contract_no=xsdd,
        contract_amount=Decimal("100.00"),
        contract_status="正常",
        status_mapping_state="mapped",
        status_mapping_version="xsdd-test-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        source="test",
        reason="建立合同 owner",
        operated_by="xsdd-admin",
    )
    db.commit()

    assignments.assign_source_orders(
        db,
        project_id=project.project_id,
        items=[{"source_order_id": raw_order_id}],
        reason="匹配合同 owner 后挂靠",
        operated_by="xsdd-admin",
        user_ctx=_admin(),
    )
    db.commit()
    assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    ))
    assert assignment is not None and assignment.project_id == project.project_id


def test_last_matching_contract_cannot_orphan_active_wbdd(db):
    project = _project(db, "xsdd-contract-guard", "XSDD-CONTRACT-GUARD", "合同删除守卫")
    xsdd = "XSDD-20991231-0005"
    contracts = []
    for suffix in ("a", "b"):
        payload = operations.create_contract(
            db,
            project_id=project.project_id,
            contract_id=f"xsdd-contract-guard-{suffix}",
            contract_no=xsdd,
            contract_amount=Decimal("100.00"),
            contract_status="正常",
            status_mapping_state="mapped",
            status_mapping_version="xsdd-test-v1",
            included_in_total=True,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            source="test",
            reason="建立可替代合同 owner",
            operated_by="xsdd-admin",
        )
        contracts.append(payload["project_contract_id"])
    _upsert_wbdd_xsdd(
        db,
        raw_order_id="WBDD-XSDD-CONTRACT-GUARD",
        sales_order=xsdd,
        batch_key="xsdd-contract-guard-order",
    )
    assignments.assign_source_orders(
        db,
        project_id=project.project_id,
        items=[{"source_order_id": "WBDD-XSDD-CONTRACT-GUARD"}],
        reason="建立 active WBDD evidence",
        operated_by="xsdd-admin",
        user_ctx=_admin(),
    )
    db.commit()

    first = db.get(MaintenanceProjectContract, contracts[0])
    db.delete(first)
    db.commit()
    assert db.get(MaintenanceProjectContract, contracts[0]) is None

    last = db.get(MaintenanceProjectContract, contracts[1])
    last.contract_no = "XSDD-20991231-0999"
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    last = db.get(MaintenanceProjectContract, contracts[1])
    db.delete(last)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    assert db.get(MaintenanceProjectContract, contracts[1]) is not None


def test_xsdd_map_delete_requires_no_contract_or_active_wbdd_evidence(db):
    project = _project(db, "xsdd-map-guard", "XSDD-MAP-GUARD", "映射删除守卫")
    other_project = _project(
        db,
        "xsdd-map-guard-other",
        "XSDD-MAP-GUARD-OTHER",
        "映射移动守卫",
    )
    operations.create_contract(
        db,
        project_id=project.project_id,
        contract_id="xsdd-map-guard-contract",
        contract_no="XSDD-20991231-0006",
        contract_amount=Decimal("100.00"),
        contract_status="正常",
        status_mapping_state="mapped",
        status_mapping_version="xsdd-test-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        source="test",
        reason="建立映射证据",
        operated_by="xsdd-admin",
    )
    db.commit()
    mapped = db.get(MaintenanceProjectXsdd, "20991231-0006")
    mapped.project_id = other_project.project_id
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    mapped = db.get(MaintenanceProjectXsdd, "20991231-0006")
    db.delete(mapped)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    empty = MaintenanceProjectXsdd(
        xsdd_norm="20991231-0007",
        project_id=project.project_id,
        source="test-no-evidence",
    )
    db.add(empty)
    db.commit()
    db.delete(empty)
    db.commit()
    assert db.get(MaintenanceProjectXsdd, "20991231-0007") is None


def test_one_project_may_own_multiple_xsdds(db):
    project = _project(db, "xsdd-project-multi", "XSDD-MULTI", "多合同项目")
    for contract_no in ("XSDD-20260828-0101", "XSDD-20260828-0102"):
        assert operations.create_contract(
            db,
            project_id=project.project_id,
            contract_id=f"xsdd-{contract_no}",
            contract_no=contract_no,
            contract_amount=Decimal("100.00"),
            contract_status="正常",
            status_mapping_state="mapped",
            status_mapping_version="xsdd-test-v1",
            included_in_total=True,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            source="test",
            reason="验证一个项目允许多个 XSDD",
            operated_by="xsdd-admin",
        ) is not None
    db.commit()
    assert set(db.scalars(select(MaintenanceProjectXsdd.xsdd_norm).where(
        MaintenanceProjectXsdd.project_id == project.project_id
    ))) == {"20260828-0101", "20260828-0102"}


def test_board_returns_aliases_and_searches_by_old_name(db):
    project = _project(db, "xsdd-project-alias", "XSDD-ALIAS", "腾讯TCE主名称")
    maintenance_project_identity.record_alias(
        db,
        project_id=project.project_id,
        alias_name="腾讯整体维保项目-预交付",
        source="source_order",
    )
    db.commit()

    result = maintenance_boss_board.projects(
        db,
        user_ctx=_admin(),
        q_text="整体维保项目",
        page=1,
        page_size=20,
    )
    row = next(item for item in result["rows"] if item["project_id"] == project.project_id)
    assert row["display_name"] == "腾讯TCE主名称"
    assert row["aliases"] == ["腾讯整体维保项目-预交付"]
    assert row["peer_names"] == []


def test_board_returns_proven_same_xsdd_names_as_peers(db):
    project = _project(db, "xsdd-project-peer", "XSDD-PEER", "腾讯TCE主名称")
    maintenance_project_identity.record_alias(
        db,
        project_id=project.project_id,
        alias_name="腾讯整体维保项目-预交付",
        source="xsdd_container_merge",
    )
    db.commit()

    result = maintenance_boss_board.projects(
        db,
        user_ctx=_admin(),
        page=1,
        page_size=20,
    )
    row = next(item for item in result["rows"] if item["project_id"] == project.project_id)
    assert row["aliases"] == ["腾讯整体维保项目-预交付"]
    assert row["peer_names"] == ["腾讯整体维保项目-预交付"]


def test_xsdd_normalization_matches_database_ascii_whitespace_contract():
    assert maintenance_project_identity.normalize_xsdd(
        " XSDD-20260828-0201\t"
    ) == "20260828-0201"
    # Python's default ``\s`` would remove NBSP while PostgreSQL ``\s`` does
    # not.  Both identity paths deliberately reject this malformed value.
    assert maintenance_project_identity.normalize_xsdd(
        "XSDD-20260828-0201\N{NO-BREAK SPACE}"
    ) == ""


def test_ledger_apply_rejects_cross_project_xsdd_without_partial_write(db):
    existing = _project(db, "xsdd-ledger-owner", "OWNER", "既有项目")
    operations.create_contract(
        db,
        project_id=existing.project_id,
        contract_id="xsdd-ledger-owner-contract",
        contract_no="XSDD-20260828-0202",
        contract_amount=Decimal("100.00"),
        contract_status="正常",
        status_mapping_state="mapped",
        status_mapping_version="xsdd-test-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        source="test",
        reason="建立既有 XSDD 归属",
        operated_by="xsdd-admin",
    )
    db.commit()

    payload = maintenance_ledger.parse_ledger_workbook(
        # Reuse the ledger test's real parser through a minimal local workbook
        # assembled here to keep the conflict test at the service boundary.
        _ledger_bytes(
            order_no="XSDD-20260828-0202",
            project_name="台账试图新建的另一项目",
        ),
        "xsdd-conflict.xlsx",
    )
    batch_id = maintenance_ledger.store_preview(
        db,
        payload,
        "xsdd-admin",
        idempotency_key="xsdd-ledger-conflict-0202",
    )
    with pytest.raises(maintenance_ledger.LedgerBatchError, match="不能再次拆分"):
        maintenance_ledger.apply_batch(db, batch_id, "xsdd-admin")
    db.rollback()

    assert db.scalar(select(MaintenanceProject.project_id).where(
        MaintenanceProject.display_name == "台账试图新建的另一项目"
    )) is None
    assert db.scalar(select(MaintenanceProjectContract.project_contract_id).where(
        MaintenanceProjectContract.project_id != existing.project_id,
        MaintenanceProjectContract.contract_no == "XSDD-20260828-0202",
    )) is None


def test_preview_marks_only_full_business_duplicates_for_deletion(db):
    """同额不够；完整可见业务字段一致才进入 exact duplicate 删除计划。"""

    first = _project(db, "xsdd-preview-a", "PREVIEW-A", "腾讯TCE名称")
    second = _project(db, "xsdd-preview-b", "PREVIEW-B", "腾讯预交付名称")
    db.execute(text(
        "ALTER TABLE maintenance_project_contract DISABLE TRIGGER "
        "trg_maintenance_contract_claim_xsdd"
    ))
    try:
        contracts = [
            MaintenanceProjectContract(
                project_contract_id="xsdd-preview-contract-a",
                project_id=first.project_id,
                contract_id="xsdd-XSDD-20251017-0036",
                contract_no="XSDD-20251017-0036",
                contract_amount=Decimal("1575471.70"),
                amount_inc_tax=Decimal("1780283.02"),
                contract_status="正常",
                status_mapping_state="mapped",
                status_mapping_version="workbook-v2-xsdd",
                included_in_total=True,
                effective_from=date(2025, 10, 16),
                effective_to=None,
                source="sales_fallback",
                version=1,
            ),
            MaintenanceProjectContract(
                project_contract_id="xsdd-preview-contract-b",
                project_id=second.project_id,
                contract_id="xsdd-XSDD-20251017-0036",
                contract_no="XSDD-20251017-0036",
                contract_amount=Decimal("1575471.70"),
                amount_inc_tax=Decimal("1670000.00"),
                contract_status="正常",
                status_mapping_state="mapped",
                status_mapping_version="workbook-v2-xsdd",
                included_in_total=True,
                effective_from=date(2025, 10, 16),
                effective_to=None,
                source="sales_fallback",
                version=3,
            ),
        ]
        db.add_all(contracts)
        db.flush()
    finally:
        db.execute(text(
            "ALTER TABLE maintenance_project_contract ENABLE TRIGGER "
            "trg_maintenance_contract_claim_xsdd"
        ))
    db.add_all([
        MaintenanceCollectionSnapshot(
            collection_id="xsdd-preview-collection-a",
            project_id=first.project_id,
            project_contract_id=contracts[0].project_contract_id,
            report_month=date(2026, 6, 1),
            cumulative_amount=Decimal("82325.40"),
            status="confirmed",
            receipt_reference="SKD-20260630-0007",
            remark=None,
            source="workbook",
            import_batch_id="preview-batch-a",
            version=1,
        ),
        MaintenanceCollectionSnapshot(
            collection_id="xsdd-preview-collection-b",
            project_id=second.project_id,
            project_contract_id=contracts[1].project_contract_id,
            report_month=date(2026, 6, 1),
            cumulative_amount=Decimal("82325.40"),
            status="confirmed",
            receipt_reference="SKD-20260630-0007",
            remark=None,
            source="direct_api",
            import_batch_id=None,
            version=7,
        ),
    ])
    db.add(MaintenanceProjectWorkbookOperation(
        project_id=second.project_id,
        export_id=None,
        file_sha256="a" * 64,
        operation_key="xsdd-preview-collection-operation",
        payload_hash="b" * 64,
        operation_type="collection_create",
        entity_id="xsdd-preview-collection-b",
        operated_by="xsdd-admin",
    ))
    db.commit()

    preview = maintenance_project_identity.preview_historical_conflicts(db)
    conflict = next(
        row for row in preview["conflicts"]
        if row["xsdd_norm"] == "20251017-0036"
    )
    # 合同含税额不同，因此两条都保留。
    assert conflict["exact_duplicate_candidates"]["contracts"] == []
    # 回款可见业务字段完全一致；source/import_batch/version 是技术 provenance。
    receipt_clusters = conflict["exact_duplicate_candidates"]["collections"]
    assert len(receipt_clusters) == 1
    assert receipt_clusters[0]["survivor_id"] == "xsdd-preview-collection-a"
    assert receipt_clusters[0]["duplicate_ids"] == [
        "xsdd-preview-collection-b"
    ]
    assert conflict["canonical_project_id"] == first.project_id

    result = maintenance_project_identity.apply_exact_collection_dedupe(
        db,
        xsdd="XSDD-20251017-0036",
        operated_by="xsdd-admin",
    )
    db.commit()
    assert result["deleted_collection_ids"] == [
        "xsdd-preview-collection-b"
    ]
    assert result["repointed_operation_count"] == 1
    assert db.get(
        MaintenanceCollectionSnapshot,
        "xsdd-preview-collection-b",
    ) is None
    assert db.get(
        MaintenanceCollectionSnapshot,
        "xsdd-preview-collection-a",
    ) is not None
    operation = db.scalar(select(MaintenanceProjectWorkbookOperation).where(
        MaintenanceProjectWorkbookOperation.operation_key
        == "xsdd-preview-collection-operation"
    ))
    assert operation.entity_id == "xsdd-preview-collection-a"


def test_reviewed_merge_preserves_contracts_and_archives_source_container(db):
    (
        canonical,
        source,
        current,
        historical,
        survivor,
        duplicate,
        xsdd,
    ) = _seed_mergeable_contract_conflict(db, suffix="9101")
    preview = maintenance_project_identity.preview_historical_conflicts(db)
    conflict = next(
        row for row in preview["conflicts"] if row["xsdd_norm"] == xsdd
    )
    assert conflict["canonical_project_id"] == canonical.project_id
    assert conflict["requires_human_decision"] is False

    result = maintenance_project_identity.apply_historical_project_merge(
        db,
        xsdd=xsdd,
        expected_manifest_hash=conflict["manifest_hash"],
        expected_canonical_project_id=canonical.project_id,
        expected_member_project_ids=[canonical.project_id, source.project_id],
        operated_by="xsdd-admin",
        contract_resolution={
            "current_project_contract_id": current.project_contract_id,
            "archive_contracts": [{
                "project_contract_id": historical.project_contract_id,
                "effective_to": "2026-09-01",
            }],
            "collection_contract_repoints": [{
                "collection_id": survivor.collection_id,
                "target_project_contract_id": current.project_contract_id,
            }],
        },
    )
    db.commit()

    assert result["canonical_project_id"] == canonical.project_id
    assert result["source_project_ids"] == [source.project_id]
    assert result["deleted_exact_collections"][0]["collection_id"] == duplicate.collection_id
    source_after = db.get(MaintenanceProject, source.project_id)
    assert source_after is not None and source_after.is_active is False
    contracts = list(db.scalars(
        select(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_id == canonical.project_id)
        .order_by(MaintenanceProjectContract.amount_inc_tax)
    ))
    assert len(contracts) == 2
    current_after = db.get(MaintenanceProjectContract, current.project_contract_id)
    historical_after = db.get(
        MaintenanceProjectContract,
        historical.project_contract_id,
    )
    assert current_after.contract_no == f"XSDD-{xsdd}"
    assert current_after.amount_inc_tax == Decimal("1780283.02")
    assert current_after.included_in_total is True
    assert current_after.effective_to is None
    assert historical_after.contract_no == f"XSDD-{xsdd}"
    assert historical_after.amount_inc_tax == Decimal("1670000.00")
    assert historical_after.included_in_total is False
    assert historical_after.effective_to == date(2026, 9, 1)
    assert historical_after.contract_id != current_after.contract_id
    assert db.get(MaintenanceCollectionSnapshot, duplicate.collection_id) is None
    survivor_after = db.get(MaintenanceCollectionSnapshot, survivor.collection_id)
    assert survivor_after.project_id == canonical.project_id
    assert survivor_after.project_contract_id == current.project_contract_id
    assert db.get(MaintenanceProjectXsdd, xsdd).project_id == canonical.project_id
    aliases = set(db.scalars(select(MaintenanceProjectAlias.alias_name).where(
        MaintenanceProjectAlias.project_id == canonical.project_id
    )))
    assert aliases >= {canonical.display_name, source.display_name}
    audit = db.scalar(
        select(MaintenanceProjectAuditLog)
        .where(MaintenanceProjectAuditLog.action == "xsdd_container_merge")
        .order_by(MaintenanceProjectAuditLog.id.desc())
    )
    assert audit is not None
    assert audit.after_json["manifest_hash"] == conflict["manifest_hash"]


def test_merge_uses_real_immutable_generations_and_preserves_permissions(db):
    (
        canonical,
        source,
        current,
        historical,
        survivor,
        _duplicate,
        xsdd,
    ) = _seed_mergeable_contract_conflict(db, suffix="9103")

    # Seed a legacy split without disabling either immutable-history trigger.
    # The order receives its XSDD only after the historical generations exist;
    # the merge itself must then pass the real XSDD claim trigger when it
    # creates the canonical generation.
    raw_order_id = "WBDD-MERGE-9103"
    _upsert_wbdd_xsdd(
        db,
        raw_order_id=raw_order_id,
        sales_order=None,
        batch_key="merge-generations-9103",
    )
    active_order_assignment = MaintenanceSourceOrderAssignment(
        assignment_id="merge-order-active-9103",
        source_order_id=raw_order_id,
        project_id=source.project_id,
        is_active=True,
        version=1,
        created_by="legacy-import",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    archived_order_assignment = MaintenanceSourceOrderAssignment(
        assignment_id="merge-order-archived-9103",
        source_order_id=raw_order_id,
        project_id=source.project_id,
        is_active=False,
        version=3,
        created_by="legacy-import",
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        archived_by="legacy-admin",
        archived_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
    )
    db.add_all([active_order_assignment, archived_order_assignment])
    db.flush()
    order = db.scalar(select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id == raw_order_id
    ))
    # Only this guard is paused to manufacture the legacy split.  The merge
    # below executes with the order/assignment XSDD guards and both immutable
    # history triggers enabled.
    db.execute(text(
        "ALTER TABLE f_maintenance_order DISABLE TRIGGER "
        "trg_maintenance_order_claim_xsdd"
    ))
    try:
        order.linked_sales_order_no = f"XSDD-{xsdd}"
        db.flush()
    finally:
        db.execute(text(
            "ALTER TABLE f_maintenance_order ENABLE TRIGGER "
            "trg_maintenance_order_claim_xsdd"
        ))
    db.add(MaintenanceProjectXsdd(
        xsdd_norm=xsdd,
        project_id=canonical.project_id,
        source="reviewed_legacy_owner",
    ))

    users = [
        SysUser(
            id=91031,
            username="merge-user-primary-9103",
            password_hash="not-a-login-secret",
            role="maintenance_manager",
            is_active=True,
        ),
        SysUser(
            id=91032,
            username="merge-user-viewer-9103",
            password_hash="not-a-login-secret",
            role="readonly",
            is_active=True,
        ),
    ]
    db.add_all(users)
    db.flush()
    source_user_assignments = [
        MaintenanceProjectUserAssignment(
            assignment_id="merge-user-primary-assignment-9103",
            project_id=source.project_id,
            responsibility_type="primary_manager",
            user_id=users[0].id,
            source_manager_text="原负责人",
            version=1,
            assigned_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            assigned_by="legacy-admin",
            assignment_reason="历史负责人",
        ),
        MaintenanceProjectUserAssignment(
            assignment_id="merge-user-viewer-primary-9103",
            project_id=source.project_id,
            responsibility_type="viewer",
            user_id=users[0].id,
            source_manager_text="原负责人兼查看人",
            version=1,
            assigned_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            assigned_by="legacy-admin",
            assignment_reason="历史查看权限",
        ),
        MaintenanceProjectUserAssignment(
            assignment_id="merge-user-viewer-9103",
            project_id=source.project_id,
            responsibility_type="viewer",
            user_id=users[1].id,
            source_manager_text="原查看人",
            version=1,
            assigned_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            assigned_by="legacy-admin",
            assignment_reason="历史查看权限",
        ),
    ]
    db.add_all(source_user_assignments)
    source_state = operations.get_or_create_workbook_state(
        db,
        project_id=source.project_id,
    )
    source_state.revision = 7
    source_state.data_version = "source-state-before-merge"
    provenance_audit = MaintenanceProjectAuditLog(
        project_id=source.project_id,
        entity_type="project",
        entity_id=source.project_id,
        action="legacy_provenance",
        before_json=None,
        after_json={"marker": "must-stay-on-source"},
        reason="验证历史审计保留在 source 容器",
        operated_by="legacy-admin",
    )
    db.add(provenance_audit)
    db.commit()
    provenance_audit_id = provenance_audit.id

    preview = maintenance_project_identity.preview_historical_conflicts(db)
    conflict = next(
        row for row in preview["conflicts"] if row["xsdd_norm"] == xsdd
    )
    source_user_assignment_ids = sorted(
        row.assignment_id for row in source_user_assignments
    )
    result = maintenance_project_identity.apply_historical_project_merge(
        db,
        xsdd=xsdd,
        expected_manifest_hash=conflict["manifest_hash"],
        expected_canonical_project_id=canonical.project_id,
        expected_member_project_ids=[canonical.project_id, source.project_id],
        operated_by="xsdd-admin",
        contract_resolution={
            "current_project_contract_id": current.project_contract_id,
            "archive_contracts": [{
                "project_contract_id": historical.project_contract_id,
                "effective_to": "2026-09-01",
            }],
            "collection_contract_repoints": [{
                "collection_id": survivor.collection_id,
                "target_project_contract_id": current.project_contract_id,
            }],
        },
        user_assignment_resolution={
            "keep_assignment_ids": [],
            "archive_assignment_ids": source_user_assignment_ids,
            "create_on_canonical": [
                {
                    "source_assignment_id": row.assignment_id,
                    "responsibility_type": row.responsibility_type,
                    "user_id": row.user_id,
                }
                for row in source_user_assignments
            ],
        },
    )
    db.commit()

    active_after = db.get(
        MaintenanceSourceOrderAssignment,
        active_order_assignment.assignment_id,
    )
    archived_after = db.get(
        MaintenanceSourceOrderAssignment,
        archived_order_assignment.assignment_id,
    )
    assert active_after.project_id == source.project_id
    assert active_after.is_active is False
    assert active_after.version == 2
    assert active_after.archived_by == "xsdd-admin"
    assert archived_after.project_id == source.project_id
    assert archived_after.is_active is False
    assert archived_after.version == 3
    canonical_order_generations = list(db.scalars(
        select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.source_order_id == raw_order_id,
            MaintenanceSourceOrderAssignment.project_id == canonical.project_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
    ))
    assert len(canonical_order_generations) == 1
    assert canonical_order_generations[0].version == 1
    assert canonical_order_generations[0].assignment_id != active_after.assignment_id

    archived_user_rows = list(db.scalars(
        select(MaintenanceProjectUserAssignment)
        .where(MaintenanceProjectUserAssignment.assignment_id.in_(
            source_user_assignment_ids
        ))
        .order_by(MaintenanceProjectUserAssignment.assignment_id)
    ))
    assert all(row.project_id == source.project_id for row in archived_user_rows)
    assert all(row.archived_at is not None for row in archived_user_rows)
    assert all(row.version == 2 for row in archived_user_rows)
    canonical_users = list(db.scalars(
        select(MaintenanceProjectUserAssignment).where(
            MaintenanceProjectUserAssignment.project_id == canonical.project_id,
            MaintenanceProjectUserAssignment.archived_at.is_(None),
        )
    ))
    assert {
        (row.responsibility_type, row.user_id) for row in canonical_users
    } == {
        ("primary_manager", users[0].id),
        ("viewer", users[0].id),
        ("viewer", users[1].id),
    }
    assert all(row.version == 1 for row in canonical_users)
    assert not ({row.assignment_id for row in canonical_users} & set(
        source_user_assignment_ids
    ))

    assert db.get(MaintenanceProjectAuditLog, provenance_audit_id).project_id == source.project_id
    assert db.get(
        MaintenanceProjectWorkbookState,
        source.project_id,
    ).revision == 7
    assert db.get(
        MaintenanceProjectWorkbookState,
        canonical.project_id,
    ).revision == 1
    assert db.get(MaintenanceProjectXsdd, xsdd).project_id == canonical.project_id
    assert result["source_order_assignment_resolution"][
        "created_canonical_generations"
    ][0]["source_assignment_id"] == active_order_assignment.assignment_id


def test_merge_rejects_stale_manifest_before_any_write(db):
    canonical, source, current, historical, survivor, _duplicate, xsdd = (
        _seed_mergeable_contract_conflict(db, suffix="9102")
    )
    preview = maintenance_project_identity.preview_historical_conflicts(db)
    conflict = next(
        row for row in preview["conflicts"] if row["xsdd_norm"] == xsdd
    )
    source.display_name = "并发修改后的名称"
    source.version += 1
    db.commit()

    with pytest.raises(
        maintenance_project_identity.XsddProjectMergeConflict,
        match="manifest 已漂移",
    ):
        maintenance_project_identity.apply_historical_project_merge(
            db,
            xsdd=xsdd,
            expected_manifest_hash=conflict["manifest_hash"],
            expected_canonical_project_id=canonical.project_id,
            expected_member_project_ids=[canonical.project_id, source.project_id],
            operated_by="xsdd-admin",
            contract_resolution={
                "current_project_contract_id": current.project_contract_id,
                "archive_contracts": [{
                    "project_contract_id": historical.project_contract_id,
                    "effective_to": "2026-09-01",
                }],
                "collection_contract_repoints": [{
                    "collection_id": survivor.collection_id,
                    "target_project_contract_id": current.project_contract_id,
                }],
            },
        )
    db.rollback()

    assert db.get(MaintenanceProject, source.project_id).is_active is True
    assert db.get(MaintenanceProjectContract, historical.project_contract_id).project_id == source.project_id
    assert db.get(MaintenanceProjectXsdd, xsdd) is None
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectAuditLog).where(
        MaintenanceProjectAuditLog.action == "xsdd_container_merge"
    )) == 0
