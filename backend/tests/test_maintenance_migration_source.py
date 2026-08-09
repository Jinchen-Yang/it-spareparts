from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.business_time import business_today
from app.models.dimensions import DimPart
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectWorkbookState,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.services import maintenance_migration_source as migration_source
from app.services.maintenance_migration_controls import build_project_preview
from app.services.maintenance_migration_source import (
    MaintenanceMigrationSourceError,
    build_project_source_payload,
)


def _seed_project_facts(db):
    db.add(
        MaintenanceProject(
            project_id="migration-source-project",
            project_code="MIGRATION-SOURCE",
            display_name="迁移来源合成项目",
            lifecycle_status="ongoing",
        )
    )
    part = DimPart(id=21003, pn_std="PN-MIGRATION-SOURCE")
    db.add(part)
    db.flush()
    db.add(
        MaintenanceProjectWorkbookState(
            project_id="migration-source-project",
            revision=0,
            data_version="migration-source-version-0",
            expense_ready_through=business_today().replace(day=1),
        )
    )
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
                "balance_key": "migration-source-project:21003",
                "pn": "PN-MIGRATION-SOURCE",
                "quantity": "10",
                "evidence_hash": "b" * 64,
                "approved": True,
            }
        ],
        inventory_movements=[
            {
                "movement_id": "shipment-document:shipment-line",
                "document_id": "shipment-document",
                "line_id": "shipment-line",
                "document_date": "2026-08-02",
                "movement_type": "delivery",
                "source": "maintenance_warehouse_v1",
                "source_document_type": "shipment",
                "source_status": "confirmed",
                "formal_available": False,
                "project_id": "migration-source-project",
                "part_id": 21003,
                "balance_key": "migration-source-project:21003",
                "pn": "PN-MIGRATION-SOURCE",
                "quantity": "3",
            }
        ],
        warehouse_source_ready=warehouse_ready,
    )


def test_multi_project_snapshot_locks_every_project_before_shared_linkage(monkeypatch):
    events = []
    monkeypatch.setattr(
        migration_source,
        "lock_project_source_snapshot",
        lambda _db, *, project_id: events.append(("project", project_id)),
    )
    monkeypatch.setattr(
        migration_source,
        "lock_optional_linkage_snapshot",
        lambda _db: events.append(("linkage", None)),
    )

    migration_source.lock_project_source_snapshots(
        object(), project_ids=["project-b", "project-a", "project-b"]
    )

    assert events == [
        ("project", "project-a"),
        ("project", "project-b"),
        ("linkage", None),
    ]


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


def test_cost_evidence_and_samples_are_visible_and_hash_bound(db):
    _seed_project_facts(db)
    first = _build(db)

    line = db.get(MaintenanceSiteIssueLine, "migration-source-current-line")
    line.reference_sample_ids = ["purchase-line-101"]
    line.reference_sample_count = 1
    line.reference_samples = [
        {
            "sample_id": "purchase-line-101",
            "document_no": "CG-101",
            "document_date": "2026-08-01",
            "distance_days": 1,
            "quantity": "2",
            "unit_price_raw": "20",
            "unit_price_ex_tax": "20",
            "tax_conversion": "already_ex_tax",
            "untrusted_note": "must-not-enter-manifest",
        }
    ]
    line.reference_window_from = date(2026, 7, 26)
    line.reference_window_to = date(2026, 8, 9)
    db.commit()

    changed = _build(db)
    evidence = changed["post_cutover_site_issues"][0]

    assert first["source_snapshot_hash"] != changed["source_snapshot_hash"]
    assert evidence["cost_evidence_kind"] == "manual_confirmed"
    assert evidence["cost_is_estimate"] is False
    assert evidence["reference_sample_count"] == 1
    assert evidence["reference_samples"][0]["sample_id"] == "purchase-line-101"
    assert "untrusted_note" not in evidence["reference_samples"][0]


def test_missing_expense_completeness_watermark_blocks_approval(db):
    _seed_project_facts(db)
    state = db.get(MaintenanceProjectWorkbookState, "migration-source-project")
    state.expense_ready_through = None
    db.commit()

    payload = _build(db)
    preview = build_project_preview(payload)

    assert payload["source_coverage"]["expense_ready_through"] is None
    assert "expense_readiness_missing" in {
        row["code"] for row in preview["approval_blockers"]
    }


def test_stale_expense_completeness_watermark_blocks_approval(db):
    _seed_project_facts(db)
    state = db.get(MaintenanceProjectWorkbookState, "migration-source-project")
    current_month = business_today().replace(day=1)
    state.expense_ready_through = (current_month - timedelta(days=1)).replace(day=1)
    db.commit()

    preview = build_project_preview(_build(db))

    assert "expense_readiness_missing" in {
        row["code"] for row in preview["approval_blockers"]
    }


def test_future_expense_completeness_watermark_blocks_approval(db):
    _seed_project_facts(db)
    state = db.get(MaintenanceProjectWorkbookState, "migration-source-project")
    current_month = business_today().replace(day=1)
    state.expense_ready_through = (current_month + timedelta(days=32)).replace(day=1)
    db.commit()

    preview = build_project_preview(_build(db))

    assert "expense_readiness_invalid" in {
        row["code"] for row in preview["approval_blockers"]
    }


def test_archived_project_and_stale_part_identity_fail_closed(db):
    _seed_project_facts(db)
    project = db.get(MaintenanceProject, "migration-source-project")
    project.is_active = False
    db.commit()

    with pytest.raises(MaintenanceMigrationSourceError, match="已归档"):
        _build(db)

    project.is_active = True
    part = db.get(DimPart, 21003)
    part.pn_std = "PN-MIGRATION-SOURCE-RENAMED"
    db.commit()

    with pytest.raises(MaintenanceMigrationSourceError, match="PN.*不一致"):
        _build(db)


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
