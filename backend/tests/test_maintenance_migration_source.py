from datetime import date
from decimal import Decimal

from app.models.dimensions import DimPart
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectExpenseAttribution,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.services.maintenance_migration_controls import build_project_preview
from app.services.maintenance_migration_source import build_project_source_payload


def _seed_project_facts(db):
    db.add(
        MaintenanceProject(
            project_id="migration-source-project",
            project_code="MIGRATION-SOURCE",
            display_name="迁移来源合成项目",
            lifecycle_status="ongoing",
        )
    )
    part = DimPart(pn_std="PN-MIGRATION-SOURCE")
    db.add(part)
    db.flush()
    db.add_all(
        [
            MaintenanceSiteIssue(
                issue_id="migration-source-history",
                project_id="migration-source-project",
                issue_no="LY-HISTORY",
                issue_date=date(2026, 7, 31),
                raw_status="已确认",
                status_mapping_state="mapped",
                normalized_status="confirmed",
                status_mapping_version="test-v1",
                source="legacy",
            ),
            MaintenanceSiteIssue(
                issue_id="migration-source-current",
                project_id="migration-source-project",
                issue_no="LY-CURRENT",
                issue_date=date(2026, 8, 2),
                raw_status="已确认",
                status_mapping_state="mapped",
                normalized_status="confirmed",
                status_mapping_version="test-v1",
                source="direct_api",
            ),
        ]
    )
    db.flush()
    db.add_all(
        [
            MaintenanceSiteIssueLine(
                issue_line_id="migration-source-history-line",
                issue_id="migration-source-history",
                line_no=1,
                part_id=part.id,
                pn=part.pn_std,
                quantity=Decimal("1"),
                unit_cost=Decimal("10"),
                cost_amount=Decimal("10"),
                unit_cost_ex_tax=Decimal("10"),
                unit_cost_inc_tax=Decimal("11.30"),
                cost_amount_ex_tax=Decimal("10"),
                cost_amount_inc_tax=Decimal("11.30"),
                cost_source="manual",
                manual_unit_cost=Decimal("10"),
                manual_unit_cost_inc_tax=Decimal("11.30"),
                manual_evidence="合成迁移测试",
                algorithm_version="migration-test-v1",
            ),
            MaintenanceSiteIssueLine(
                issue_line_id="migration-source-current-line",
                issue_id="migration-source-current",
                line_no=1,
                part_id=part.id,
                pn=part.pn_std,
                quantity=Decimal("2"),
                unit_cost=Decimal("20"),
                cost_amount=Decimal("40"),
                unit_cost_ex_tax=Decimal("20"),
                unit_cost_inc_tax=Decimal("22.60"),
                cost_amount_ex_tax=Decimal("40"),
                cost_amount_inc_tax=Decimal("45.20"),
                cost_source="manual",
                manual_unit_cost=Decimal("20"),
                manual_unit_cost_inc_tax=Decimal("22.60"),
                manual_evidence="合成迁移测试",
                algorithm_version="migration-test-v1",
            ),
            MaintenanceProjectExpenseAttribution(
                expense_id="migration-source-expense",
                project_id="migration-source-project",
                expense_ref="BX-MIGRATION-SOURCE",
                expense_date=date(2026, 8, 3),
                amount_ex_tax=Decimal("5"),
                amount_inc_tax=Decimal("5.65"),
                raw_status="已审批",
                status_mapping_state="mapped",
                normalized_status="approved",
                status_mapping_version="test-v1",
            ),
        ]
    )
    db.commit()


def _build(db, *, historical_mode="approved_cost_baseline", warehouse_ready=True):
    return build_project_source_payload(
        db,
        project_id="migration-source-project",
        cutover_date=date(2026, 8, 1),
        historical_mode=historical_mode,
        historical_baseline={
            "amount_ex_tax": "100",
            "amount_inc_tax": "113",
            "evidence_hash": "a" * 64,
            "approved": True,
        },
        opening_balances=[
            {
                "balance_key": "project:part",
                "pn": "PN-MIGRATION-SOURCE",
                "quantity": "10",
                "evidence_hash": "b" * 64,
                "approved": True,
            }
        ],
        inventory_movements=[
            {
                "movement_id": "shipment-line",
                "document_date": "2026-08-02",
                "movement_type": "delivery",
                "balance_key": "project:part",
                "pn": "PN-MIGRATION-SOURCE",
                "quantity": "3",
            }
        ],
        warehouse_source_ready=warehouse_ready,
    )


def test_server_snapshot_uses_database_cost_facts_and_never_demand_rows(db):
    _seed_project_facts(db)

    payload = _build(db)
    preview = build_project_preview(payload)

    assert preview["cost"]["historical_baseline_ex_tax"] == "100.00"
    assert preview["cost"]["post_cutover_consumption_ex_tax"] == "40.00"
    assert preview["cost"]["approved_expense_ex_tax"] == "5.00"
    assert preview["inventory"][0]["closing_quantity"] == "7"
    assert "maintenance_demands" not in payload
    assert payload["historical_site_issues"][0]["issue_line_id"] == (
        "migration-source-history-line"
    )


def test_legacy_historical_issue_is_not_claimed_as_stable_identity(db):
    _seed_project_facts(db)

    payload = _build(db, historical_mode="stable_site_issues")
    payload["historical_baseline"] = None
    preview = build_project_preview(payload)

    assert "historical_issue_missing_identity" in {
        row["code"] for row in preview["approval_blockers"]
    }


def test_missing_canonical_warehouse_source_is_an_explicit_approval_blocker(db):
    _seed_project_facts(db)

    preview = build_project_preview(_build(db, warehouse_ready=False))

    assert "warehouse_source_not_ready" in {
        row["code"] for row in preview["approval_blockers"]
    }
    assert preview["evidence"]["source_coverage"]["warehouse_source_ready"] is False


def test_source_hash_changes_when_an_operational_fact_changes(db):
    _seed_project_facts(db)
    first = _build(db)["source_snapshot_hash"]

    expense = db.get(MaintenanceProjectExpenseAttribution, "migration-source-expense")
    expense.normalized_status = "void"
    db.commit()
    changed = _build(db)["source_snapshot_hash"]

    assert first != changed


def test_named_approval_state_does_not_masquerade_as_source_data_change(db):
    _seed_project_facts(db)
    pending = _build(db)
    pending["historical_baseline"]["approved"] = False
    pending["opening_balances"][0]["approved"] = False

    rebuilt_pending = build_project_source_payload(
        db,
        project_id="migration-source-project",
        cutover_date=date(2026, 8, 1),
        historical_mode="approved_cost_baseline",
        historical_baseline=pending["historical_baseline"],
        opening_balances=pending["opening_balances"],
        inventory_movements=pending["inventory_movements"],
        warehouse_source_ready=True,
    )
    approved = _build(db)

    assert rebuilt_pending["source_snapshot_hash"] == approved["source_snapshot_hash"]
    assert (
        build_project_preview(rebuilt_pending)["project_input_fingerprint"]
        != build_project_preview(approved)["project_input_fingerprint"]
    )
