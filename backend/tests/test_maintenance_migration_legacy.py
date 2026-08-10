"""Read-only legacy WBDD/BXD truth used by maintenance cutover comparison."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app import config
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder, FProjectExpense
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.models.system import SysImportBatch
from app.services.maintenance_migration_legacy import load_project_legacy_truth


@pytest.fixture()
def assignment_table_guard(db):
    db.rollback()
    db.execute(text("DROP TABLE IF EXISTS maintenance_source_order_assignment"))
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.execute(text("DROP TABLE IF EXISTS maintenance_source_order_assignment"))
        db.commit()


def _seed_project_contract(
    db,
    *,
    project_id: str,
    contract_no: str,
    effective_from: date = date(2025, 1, 1),
    effective_to: date | None = None,
) -> None:
    db.add(
        MaintenanceProject(
            project_id=project_id,
            project_code=project_id.upper(),
            display_name="旧口径读取合成项目",
            lifecycle_status="ongoing",
        )
    )
    db.flush()
    db.add(
        MaintenanceProjectContract(
            project_contract_id=f"{project_id}-contract",
            project_id=project_id,
            contract_id=f"{project_id}-contract-id",
            contract_no=contract_no,
            contract_amount=Decimal("1000.00"),
            contract_status="已生效",
            status_mapping_state="mapped",
            status_mapping_version="test-v1",
            included_in_total=True,
            effective_from=effective_from,
            effective_to=effective_to,
            source="synthetic-test",
        )
    )


def test_missing_stable_assignment_contract_fails_closed(assignment_table_guard):
    db = assignment_table_guard
    _seed_project_contract(
        db,
        project_id="legacy-missing-assignment",
        contract_no="XSDD-LEGACY-MISSING",
    )
    db.commit()

    result = load_project_legacy_truth(
        db,
        "legacy-missing-assignment",
        date(2026, 8, 10),
    )

    assert result["source_ready"] is False
    assert result["cost_lines"] == []
    assert {blocker["code"] for blocker in result["blockers"]} >= {
        "legacy_assignment_contract_missing"
    }


def test_legacy_truth_recomputes_old_formula_excludes_future_and_is_read_only(
    assignment_table_guard,
):
    db = assignment_table_guard
    db.execute(
        text(
            """
            CREATE TABLE maintenance_source_order_assignment (
                assignment_id VARCHAR(36) PRIMARY KEY,
                source_order_id VARCHAR(64) NOT NULL,
                project_id VARCHAR(36) NOT NULL,
                is_active BOOLEAN NOT NULL,
                version INTEGER NOT NULL
            )
            """
        )
    )
    _seed_project_contract(
        db,
        project_id="legacy-project",
        contract_no="XSDD-LEGACY-1",
    )
    batch = SysImportBatch(
        filename="synthetic-legacy.xlsx",
        file_type="maintenance",
        file_hash="1" * 64,
        status="success",
    )
    part = DimPart(id=21100, pn_std="PN-LEGACY-1")
    db.add_all([batch, part])
    db.flush()

    current_order = FMaintenanceOrder(
        raw_order_id="legacy-order-current",
        order_no="WBDD-LEGACY-CURRENT",
        order_date=date(2026, 7, 31),
        data_status=config.ACTIVE_STATUS,
        import_batch_id=batch.id,
    )
    future_order = FMaintenanceOrder(
        raw_order_id="legacy-order-future",
        order_no="WBDD-LEGACY-FUTURE",
        order_date=date(2026, 8, 11),
        data_status=config.ACTIVE_STATUS,
        import_batch_id=batch.id,
    )
    db.add_all([current_order, future_order])
    db.flush()
    current_line = FMaintenanceLine(
        raw_line_id="legacy-line-current",
        order_id=current_order.id,
        line_no=1,
        part_id=part.id,
        pn_std=part.pn_std,
        qty=Decimal("5"),
        return_qty=Decimal("2"),
        unit_cost_ex_tax=Decimal("88.50"),
        unit_cost_inc_tax=Decimal("100.00"),
        cost_tax_basis="inc",
        cost_amount_ex_tax=Decimal("265.50"),
        cost_amount_inc_tax=Decimal("300.00"),
        import_batch_id=batch.id,
    )
    future_line = FMaintenanceLine(
        raw_line_id="legacy-line-future",
        order_id=future_order.id,
        line_no=1,
        part_id=part.id,
        pn_std=part.pn_std,
        qty=Decimal("9"),
        return_qty=Decimal("0"),
        unit_cost_ex_tax=Decimal("10.00"),
        unit_cost_inc_tax=Decimal("11.30"),
        cost_tax_basis="ex",
        cost_amount_ex_tax=Decimal("90.00"),
        cost_amount_inc_tax=Decimal("101.70"),
        import_batch_id=batch.id,
    )
    current_expense = FProjectExpense(
        raw_line_id="legacy-expense-current",
        bxd_no="BXD-LEGACY-CURRENT",
        line_no=1,
        data_status=config.MAINT_EXPENSE_ACTIVE_STATUS,
        expense_date=date(2026, 7, 30),
        linked_sales_order_no="XSDD-LEGACY-1",
        amount=Decimal("100.00"),
        amount_ex_tax=Decimal("88.50"),
        amount_inc_tax=Decimal("100.00"),
        tax_basis="inc",
        import_batch_id=batch.id,
    )
    future_expense = FProjectExpense(
        raw_line_id="legacy-expense-future",
        bxd_no="BXD-LEGACY-FUTURE",
        line_no=2,
        data_status=config.MAINT_EXPENSE_ACTIVE_STATUS,
        expense_date=date(2026, 8, 11),
        linked_sales_order_no="XSDD-LEGACY-1",
        amount=Decimal("80.00"),
        amount_ex_tax=Decimal("80.00"),
        amount_inc_tax=Decimal("90.40"),
        tax_basis="default_ex",
        import_batch_id=batch.id,
    )
    db.add_all([current_line, future_line, current_expense, future_expense])
    db.execute(
        text(
            """
            INSERT INTO maintenance_source_order_assignment
                (assignment_id, source_order_id, project_id, is_active, version)
            VALUES
                ('assignment-current', 'legacy-order-current',
                 'legacy-project', TRUE, 3),
                ('assignment-future', 'legacy-order-future',
                 'legacy-project', TRUE, 2)
            """
        )
    )
    db.commit()

    result = load_project_legacy_truth(
        db,
        "legacy-project",
        date(2026, 8, 10),
    )

    assert result["source_ready"] is True
    assert result["blockers"] == []
    assert result["source_coverage"]["business_as_of"] == "2026-08-10"
    assert result["source_coverage"]["cost_line_count"] == 1
    assert result["source_coverage"]["expense_line_count"] == 1
    assert [row["source_line_id"] for row in result["cost_lines"]] == [
        "legacy-line-current"
    ]
    assert result["cost_lines"][0]["effective_quantity"] == "3.000"
    assert result["cost_lines"][0]["cost_tax_basis"] == "inc"
    assert result["cost_lines"][0]["cost_amount_ex_tax"] == "265.50"
    assert result["cost_lines"][0]["cost_amount_inc_tax"] == "300.00"
    assert [row["expense_id"] for row in result["expenses"]] == [
        "legacy-expense-current"
    ]
    assert result["expenses"][0]["tax_basis"] == "inc"
    assert result["expenses"][0]["project_contract_id"] == "legacy-project-contract"
    assert result["expenses"][0]["contract_relation_version"] == 1
    assert result["expenses"][0]["amount_ex_tax"] == "88.50"
    assert result["expenses"][0]["amount_inc_tax"] == "100.00"
    assert len(result["source_hash"]) == 64

    db.refresh(current_line)
    db.refresh(current_expense)
    assert current_line.qty == Decimal("5.000")
    assert current_line.return_qty == Decimal("2.000")
    assert current_line.cost_amount_ex_tax == Decimal("265.50")
    assert current_expense.amount_ex_tax == Decimal("88.50")

    db.commit()
    _seed_project_contract(
        db,
        project_id="legacy-other-project",
        contract_no="XSDD-LEGACY-1",
    )
    db.execute(
        text(
            """
            INSERT INTO maintenance_source_order_assignment
                (assignment_id, source_order_id, project_id, is_active, version)
            VALUES
                ('assignment-conflict', 'legacy-order-current',
                 'legacy-other-project', TRUE, 1)
            """
        )
    )
    db.commit()

    ambiguous = load_project_legacy_truth(
        db,
        "legacy-project",
        date(2026, 8, 10),
    )
    assert ambiguous["source_ready"] is False
    assert ambiguous["cost_lines"] == []
    assert ambiguous["expenses"] == []
    assert {
        blocker["entity_id"]
        for blocker in ambiguous["blockers"]
        if blocker["code"] == "legacy_assignment_ambiguous"
    } == {"legacy-order-current"}
    assert {
        blocker["entity_id"]
        for blocker in ambiguous["blockers"]
        if blocker["code"] == "legacy_expense_contract_ambiguous"
    } == {"XSDD-LEGACY-1"}


def test_legacy_expense_uses_contract_owner_at_expense_date(assignment_table_guard):
    db = assignment_table_guard
    db.execute(
        text(
            """
            CREATE TABLE maintenance_source_order_assignment (
                assignment_id VARCHAR(36) PRIMARY KEY,
                source_order_id VARCHAR(64) NOT NULL,
                project_id VARCHAR(36) NOT NULL,
                is_active BOOLEAN NOT NULL,
                version INTEGER NOT NULL
            )
            """
        )
    )
    _seed_project_contract(
        db,
        project_id="legacy-old-owner",
        contract_no="XSDD-LEGACY-HANDOFF",
        effective_to=date(2026, 7, 1),
    )
    _seed_project_contract(
        db,
        project_id="legacy-new-owner",
        contract_no="XSDD-LEGACY-HANDOFF",
        effective_from=date(2026, 7, 1),
    )
    batch = SysImportBatch(
        filename="synthetic-legacy-handoff.xlsx",
        file_type="expense",
        file_hash="2" * 64,
        status="success",
    )
    db.add(batch)
    db.flush()
    db.add(
        FProjectExpense(
            raw_line_id="legacy-expense-before-handoff",
            bxd_no="BXD-LEGACY-HANDOFF",
            line_no=1,
            data_status=config.MAINT_EXPENSE_ACTIVE_STATUS,
            expense_date=date(2026, 6, 15),
            linked_sales_order_no="XSDD-LEGACY-HANDOFF",
            amount=Decimal("100.00"),
            amount_ex_tax=Decimal("100.00"),
            amount_inc_tax=Decimal("113.00"),
            tax_basis="default_ex",
            import_batch_id=batch.id,
        )
    )
    db.commit()

    old_owner = load_project_legacy_truth(db, "legacy-old-owner", date(2026, 8, 10))
    new_owner = load_project_legacy_truth(db, "legacy-new-owner", date(2026, 8, 10))

    assert old_owner["source_ready"] is True
    assert [row["expense_id"] for row in old_owner["expenses"]] == [
        "legacy-expense-before-handoff"
    ]
    assert new_owner["source_ready"] is True
    assert new_owner["expenses"] == []


def test_unassigned_approved_expense_is_a_global_fail_closed_blocker(
    assignment_table_guard,
):
    db = assignment_table_guard
    db.execute(
        text(
            """
            CREATE TABLE maintenance_source_order_assignment (
                assignment_id VARCHAR(36) PRIMARY KEY,
                source_order_id VARCHAR(64) NOT NULL,
                project_id VARCHAR(36) NOT NULL,
                is_active BOOLEAN NOT NULL,
                version INTEGER NOT NULL
            )
            """
        )
    )
    _seed_project_contract(
        db,
        project_id="legacy-expense-scope",
        contract_no="XSDD-LEGACY-KNOWN",
    )
    batch = SysImportBatch(
        filename="synthetic-legacy-unassigned.xlsx",
        file_type="expense",
        file_hash="3" * 64,
        status="success",
    )
    db.add(batch)
    db.flush()
    db.add(
        FProjectExpense(
            raw_line_id="legacy-expense-unassigned",
            bxd_no="BXD-LEGACY-UNASSIGNED",
            line_no=1,
            data_status=config.MAINT_EXPENSE_ACTIVE_STATUS,
            expense_date=date(2026, 6, 15),
            linked_sales_order_no="XSDD-LEGACY-UNKNOWN",
            amount=Decimal("50.00"),
            amount_ex_tax=Decimal("50.00"),
            amount_inc_tax=Decimal("56.50"),
            tax_basis="default_ex",
            import_batch_id=batch.id,
        )
    )
    db.commit()

    result = load_project_legacy_truth(
        db,
        "legacy-expense-scope",
        date(2026, 8, 10),
    )

    assert result["source_ready"] is False
    assert result["expenses"] == []
    assert {
        blocker.get("entity_id")
        for blocker in result["blockers"]
        if blocker["code"] == "legacy_expense_contract_scope_missing"
    } == {"legacy-expense-unassigned"}


def test_unassigned_active_wbdd_is_a_global_fail_closed_blocker(
    assignment_table_guard,
):
    db = assignment_table_guard
    db.execute(
        text(
            """
            CREATE TABLE maintenance_source_order_assignment (
                assignment_id VARCHAR(36) PRIMARY KEY,
                source_order_id VARCHAR(64) NOT NULL,
                project_id VARCHAR(36) NOT NULL,
                is_active BOOLEAN NOT NULL,
                version INTEGER NOT NULL
            )
            """
        )
    )
    _seed_project_contract(
        db,
        project_id="legacy-wbdd-scope",
        contract_no="XSDD-LEGACY-WBDD-KNOWN",
    )
    batch = SysImportBatch(
        filename="synthetic-legacy-wbdd-unassigned.xlsx",
        file_type="maintenance",
        file_hash="4" * 64,
        status="success",
    )
    db.add(batch)
    db.flush()
    db.add(
        FMaintenanceOrder(
            raw_order_id="legacy-order-unassigned",
            order_no="WBDD-LEGACY-UNASSIGNED",
            order_date=date(2026, 6, 15),
            data_status=config.ACTIVE_STATUS,
            import_batch_id=batch.id,
        )
    )
    db.commit()

    result = load_project_legacy_truth(
        db,
        "legacy-wbdd-scope",
        date(2026, 8, 10),
    )

    assert result["source_ready"] is False
    assert result["cost_lines"] == []
    assert {
        blocker.get("entity_id")
        for blocker in result["blockers"]
        if blocker["code"] == "legacy_assignment_missing"
    } == {"legacy-order-unassigned"}
