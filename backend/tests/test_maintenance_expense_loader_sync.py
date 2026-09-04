"""Raw BXD import -> canonical attribution -> workbook/card read model."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app import config
from app.etl import loader, mapping
from app.etl.transform import TransformResult
from app.models.maintenance import FProjectExpense
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import (
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectWorkbookState,
)
from app.models.system import SysImportBatch
from app.services import maintenance_boss_board


def _batch(db) -> int:
    batch = SysImportBatch(
        filename=f"expense-{uuid.uuid4()}.xlsx",
        file_type="expense",
        file_hash=uuid.uuid4().hex,
        status="success",
    )
    db.add(batch)
    db.flush()
    return batch.id


def _expense(raw_id: str, *, bxd_no: str, amount: str) -> dict:
    amount_ex = Decimal(amount)
    return {
        "raw_line_id": raw_id,
        "bxd_no": bxd_no,
        "line_no": 1,
        "data_status": config.MAINT_EXPENSE_ACTIVE_STATUS,
        "expense_date": date(2026, 8, 1),
        "person": "报销测试员",
        "expense_type": "维保费用",
        "fee_category": "备件",
        "reason": "现场备件报销",
        "linked_sales_order_no": "XSDD-EXP-SYNC",
        "amount": amount_ex,
        "amount_ex_tax": amount_ex,
        "amount_inc_tax": (amount_ex * Decimal("1.13")).quantize(Decimal("0.01")),
        "tax_basis": "ex",
        "tax_rate_used": Decimal("0.13"),
    }


def _result(*lines: dict) -> TransformResult:
    # 模拟的是「权威合同页」＝带页级锚的单合同项目工作簿报销页（D-09：修复模式的
    # 删除侧只在这种形态下武装；无锚逐行表不作废）。
    return TransformResult(
        file_type=mapping.EXPENSE,
        lines=list(lines),
        rows_total=len(lines),
        expense_anchors=["XSDD-EXP-SYNC"],
    )


def _seed_project(db) -> MaintenanceProject:
    project = MaintenanceProject(
        project_id=str(uuid.uuid4()),
        project_code=f"EXP-{uuid.uuid4().hex[:8]}",
        display_name="报销同步项目",
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    db.add(MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()),
        project_id=project.project_id,
        contract_id=f"EXP-CONTRACT-{uuid.uuid4().hex[:8]}",
        contract_no="XSDD-EXP-SYNC",
        amount_inc_tax=Decimal("10000.00"),
        included_in_total=True,
        status_mapping_state="mapped",
        status_mapping_version="test",
        effective_from=date(2026, 1, 1),
        source="ledger",
        version=1,
    ))
    db.commit()
    return project


def _revision(db, project_id: str) -> int:
    db.expire_all()
    return db.scalar(select(MaintenanceProjectWorkbookState.revision).where(
        MaintenanceProjectWorkbookState.project_id == project_id
    ))


def test_expense_upsert_refreshes_raw_attribution_card_and_revision_atomically(db):
    project = _seed_project(db)
    raw_a = f"EXP-A-{uuid.uuid4()}"
    raw_b = f"EXP-B-{uuid.uuid4()}"

    first = loader.load(
        db,
        _result(
            _expense(raw_a, bxd_no="BXD-EXP-A", amount="100"),
            _expense(raw_b, bxd_no="BXD-EXP-B", amount="50"),
        ),
        _batch(db),
        date(2026, 8, 1),
        mode="skip",
        operated_by="expense-sync-test",
    )
    db.commit()
    assert first["expense_attributions_synced"] == 2
    assert first["workbook_projects_bumped"] == 1
    first_revision = _revision(db, project.project_id)

    # A changes 100 -> 200 and B disappears from the authoritative contract
    # sheet.  Raw B is retained as a void lineage row; both canonical rows are
    # synchronized before the transaction commits.
    second = loader.load(
        db,
        _result(_expense(raw_a, bxd_no="BXD-EXP-A", amount="200")),
        _batch(db),
        date(2026, 8, 1),
        mode="upsert",
        operated_by="expense-sync-test",
    )
    db.commit()
    db.expire_all()

    raw = db.scalar(select(FProjectExpense).where(
        FProjectExpense.raw_line_id == raw_a
    ))
    attr_a = db.get(MaintenanceProjectExpenseAttribution, f"bxd:{raw_a}")
    attr_b = db.get(MaintenanceProjectExpenseAttribution, f"bxd:{raw_b}")
    contract_id = db.scalar(select(
        MaintenanceProjectContract.project_contract_id
    ).where(
        MaintenanceProjectContract.project_id == project.project_id,
    ))
    assert raw.amount_ex_tax == Decimal("200.00")
    assert attr_a.amount_ex_tax == Decimal("200.00")
    assert attr_a.amount_inc_tax == Decimal("226.00")
    assert attr_a.raw_expense_line_id == raw_a
    assert attr_a.project_contract_id == contract_id
    assert attr_a.ownership_mapping_state == "mapped"
    assert attr_b.raw_expense_line_id == raw_b
    assert attr_b.project_contract_id == contract_id
    assert attr_b.normalized_status == "void"
    assert second["expense_rows_voided"] == 1
    assert second["expense_attributions_synced"] == 2
    assert second["workbook_projects_bumped"] == 1
    assert _revision(db, project.project_id) == first_revision + 1

    expense_costs, _site_costs = (
        maintenance_boss_board._card_expense_and_requisition_costs(
            db, [project.project_id]
        )
    )
    assert expense_costs[project.project_id] == Decimal("226.00")

    # Same authoritative snapshot is a true no-op: no attribution version or
    # workbook revision churn, and the card remains immediately consistent.
    version_after_change = attr_a.version
    revision_after_change = _revision(db, project.project_id)
    third = loader.load(
        db,
        _result(_expense(raw_a, bxd_no="BXD-EXP-A", amount="200")),
        _batch(db),
        date(2026, 8, 1),
        mode="upsert",
        operated_by="expense-sync-test",
    )
    db.commit()
    db.expire_all()
    assert third["expense_attributions_synced"] == 0
    assert third["workbook_projects_bumped"] == 0
    assert _revision(db, project.project_id) == revision_after_change
    assert db.get(
        MaintenanceProjectExpenseAttribution, f"bxd:{raw_a}"
    ).version == version_after_change
    expense_costs, _site_costs = (
        maintenance_boss_board._card_expense_and_requisition_costs(
            db, [project.project_id]
        )
    )
    assert expense_costs[project.project_id] == Decimal("226.00")
