"""XSDD is the canonical maintenance-project identity (#56)."""

import io
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.etl import loader
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAlias,
    MaintenanceProjectContract,
    MaintenanceProjectXsdd,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch
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


def test_auto_assign_groups_same_xsdd_and_retains_names_as_aliases(db):
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
    assert result["created_projects"] == 1
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
