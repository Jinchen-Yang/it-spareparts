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
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.system import SysImportBatch
from app.services import maintenance_migration_source as migration_source
from app.services import maintenance_consumption_cost
from app.services.maintenance_migration_controls import build_project_preview
from app.services import maintenance_migration_controls as controls
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
                reference_side="manual",
                algorithm_version=maintenance_consumption_cost.ALGORITHM_VERSION,
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
                reference_side="manual",
                algorithm_version=maintenance_consumption_cost.ALGORITHM_VERSION,
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


def _build(
    db,
    *,
    historical_mode="approved_cost_baseline",
    warehouse_ready=True,
    warehouse_ready_through=business_today(),
    warehouse_ambiguities=(),
    as_of=None,
    legacy_business_as_of=None,
):
    frozen_as_of = as_of or migration_source.business_today()
    baseline = {
        "amount_ex_tax": "100",
        "amount_inc_tax": "113",
        "evidence_hash": "a" * 64,
        "coverage_from": "2025-01-01",
        "coverage_through": "2026-07-31",
        "scope": "site_issue_parts_only",
        "excludes_expenses": True,
        "source_artifact_locator": "artifact://migration/source-project/history.xlsx",
        "source_row_count": 10,
    }
    baseline["aggregation_fingerprint"] = (
        controls.historical_baseline_aggregation_fingerprint(baseline)
    )
    baseline["approved"] = True
    legacy_evidence = {
        "cost_lines": [
            {
                "source_order_id": "legacy-source-order",
                "source_line_id": "legacy-source-line",
                "order_no": "WBDD-LEGACY-SOURCE",
                "order_date": "2026-07-31",
                "pn": "PN-MIGRATION-SOURCE",
                "sn": None,
                "demand_quantity": "1",
                "return_quantity": "0",
                "effective_quantity": "1",
                "unit_cost_ex_tax": "140.00",
                "unit_cost_inc_tax": "158.20",
                "cost_tax_basis": "ex",
                "cost_amount_ex_tax": "140.00",
                "cost_amount_inc_tax": "158.20",
            }
        ],
        "expenses": [
            {
                "expense_id": "legacy-source-expense",
                "expense_ref": "BXD-LEGACY-SOURCE",
                "expense_date": "2026-08-03",
                "normalized_status": "approved",
                "tax_basis": "default_ex",
                "amount_ex_tax": "5.00",
                "amount_inc_tax": "5.65",
            }
        ],
        "source_coverage": {
            "legacy_truth_version": "test-v1",
            "business_as_of": (legacy_business_as_of or frozen_as_of).isoformat(),
        },
    }
    legacy_truth = {
        **legacy_evidence,
        "source_hash": controls.canonical_hash(legacy_evidence),
        "source_ready": True,
        "blockers": [],
    }
    return build_project_source_payload(
        db,
        project_id="migration-source-project",
        cutover_date=date(2026, 8, 1),
        historical_mode=historical_mode,
        historical_baseline=baseline,
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
        warehouse_ambiguities=warehouse_ambiguities,
        warehouse_source_ready=warehouse_ready,
        warehouse_ready_through=warehouse_ready_through,
        as_of=frozen_as_of,
        legacy_truth=legacy_truth,
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
    evidence = payload["post_cutover_site_issues"][0]
    assert evidence["unit_cost_ex_tax"] == "20.00"
    assert evidence["unit_cost_inc_tax"] == "22.60"
    assert evidence["manual_unit_cost"] == "20.00"
    assert evidence["tax_rate_used"] == "0.13"


def test_snapshot_binds_one_shanghai_business_date(monkeypatch, db):
    _seed_project_facts(db)
    fixed_as_of = date(2026, 8, 9)
    calls = 0

    def fixed_business_today():
        nonlocal calls
        calls += 1
        return fixed_as_of

    monkeypatch.setattr(migration_source, "business_today", fixed_business_today)

    payload = _build(db, warehouse_ready_through=fixed_as_of)

    assert calls == 1
    assert payload["as_of"] == "2026-08-09"
    assert payload["source_coverage"]["business_as_of"] == "2026-08-09"
    assert build_project_preview(payload)["as_of"] == "2026-08-09"


def test_future_database_facts_are_excluded_from_the_frozen_snapshot(db):
    _seed_project_facts(db)
    issue = db.get(MaintenanceSiteIssue, "migration-source-current")
    expense = db.get(MaintenanceProjectExpenseAttribution, "migration-source-expense")
    issue.issue_date = date(2099, 1, 2)
    expense.expense_date = date(2099, 1, 3)
    db.commit()

    payload = _build(db)
    preview = build_project_preview(payload)

    assert preview["cost"]["post_cutover_consumption_ex_tax"] == "0.00"
    assert preview["cost"]["approved_expense_ex_tax"] == "0.00"
    assert payload["post_cutover_site_issues"] == []
    assert payload["approved_expenses"] == []


def test_legacy_truth_must_use_the_same_frozen_as_of(db):
    _seed_project_facts(db)

    with pytest.raises(MaintenanceMigrationSourceError, match="旧口径.*截止日"):
        _build(
            db,
            as_of=date(2026, 8, 5),
            warehouse_ready_through=date(2026, 8, 5),
            legacy_business_as_of=date(2026, 8, 4),
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


def test_zero_warehouse_rows_need_a_signed_completeness_watermark(db):
    _seed_project_facts(db)

    payload = _build(db, warehouse_ready_through=None)
    preview = build_project_preview(payload)

    assert payload["source_coverage"]["warehouse_ready_through"] is None
    assert "warehouse_readiness_missing" in {
        row["code"] for row in preview["approval_blockers"]
    }


def test_open_warehouse_ambiguity_is_hash_bound_and_explainable(db):
    _seed_project_facts(db)
    clean = _build(db)
    payload = _build(
        db,
        warehouse_ready=False,
        warehouse_ambiguities=[
            {
                "ambiguity_id": "ambiguity-1",
                "import_id": "import-1",
                "document_id": "document-1",
                "line_id": "line-1",
                "document_no": "FH-001",
                "document_date": "2026-08-02",
                "ambiguity_type": "missing_stable_link",
                "field_code": "maintenance_order",
                "source_row": 3,
                "value_hash": "d" * 64,
                "candidates": [],
                "fingerprint": "e" * 64,
                "status": "open",
                "version": 1,
                "scope": "global_unresolved",
                "scope_project_ids": [],
                "scope_reason": "candidate_does_not_prove_unique_active_project",
            }
        ],
    )
    preview = build_project_preview(payload)

    assert clean["source_snapshot_hash"] != payload["source_snapshot_hash"]
    assert payload["source_coverage"]["warehouse_ambiguity_count"] == 1
    assert preview["evidence"]["warehouse_ambiguities"][0]["ambiguity_id"] == (
        "ambiguity-1"
    )
    blockers = {(row["code"], row["entity_id"]) for row in preview["approval_blockers"]}
    assert ("warehouse_ambiguity_open", "ambiguity-1") in blockers
    assert ("warehouse_source_not_ready", None) not in blockers


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
    line.cost_source = "purchase_window"
    line.reference_side = "purchase"
    line.reference_sample_ids = ["purchase:101"]
    line.reference_sample_count = 1
    line.reference_samples = [
        {
            "sample_id": "purchase:101",
            "document_no": "CG-101",
            "document_date": "2026-08-01",
            "distance_days": 1,
            "quantity": "2",
            "unit_price_raw": "20",
            "unit_price_ex_tax": "20",
            "tax_conversion": "none",
            "untrusted_note": "must-not-enter-manifest",
        }
    ]
    line.reference_window_from = date(2026, 7, 26)
    line.reference_window_to = date(2026, 8, 9)
    db.commit()

    changed = _build(db)
    evidence = changed["post_cutover_site_issues"][0]

    assert first["source_snapshot_hash"] != changed["source_snapshot_hash"]
    assert evidence["cost_evidence_kind"] == "purchase_evidence"
    assert evidence["cost_is_estimate"] is False
    assert evidence["reference_sample_count"] == 1
    assert evidence["reference_samples"][0]["sample_id"] == "purchase:101"
    assert "untrusted_note" not in evidence["reference_samples"][0]


def test_current_purchase_fact_recomputes_waterfall_and_blocks_stale_cached_cost(db):
    _seed_project_facts(db)
    first = _build(db)
    db.commit()

    batch = SysImportBatch(
        filename="migration-current-cost.xlsx",
        file_type="purchase",
        file_hash="migration-current-cost-hash",
    )
    db.add(batch)
    db.flush()
    order = FPurchaseOrder(
        raw_order_id="migration-current-purchase-order",
        order_no="CG-MIGRATION-CURRENT",
        order_date=date(2026, 8, 2),
        is_tax_inclusive=False,
        data_status="已生效",
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    purchase_line = FPurchaseLine(
        raw_line_id="migration-current-purchase-line",
        order_id=order.id,
        line_no=1,
        part_id=21003,
        pn_std="PN-MIGRATION-SOURCE",
        qty=Decimal("2"),
        unit_price=Decimal("30.00"),
        import_batch_id=batch.id,
    )
    db.add(purchase_line)
    db.commit()

    changed = _build(db)
    source_row = changed["post_cutover_site_issues"][0]
    preview = build_project_preview(changed)
    persisted_line = db.get(MaintenanceSiteIssueLine, "migration-source-current-line")

    assert first["source_snapshot_hash"] != changed["source_snapshot_hash"]
    assert source_row["cost_resolution_matches_current"] is False
    assert source_row["current_cost_resolution"]["cost_source"] == "purchase_window"
    assert source_row["current_cost_resolution"]["reference_sample_ids"] == [
        f"purchase:{purchase_line.id}"
    ]
    assert "site_issue_cost_resolution_stale" in {
        blocker["code"] for blocker in preview["approval_blockers"]
    }
    assert preview["cost"]["post_cutover_consumption_ex_tax"] == "0.00"
    assert persisted_line.cost_source == "manual"
    assert persisted_line.unit_cost_ex_tax == Decimal("20.00")


def test_cost_waterfall_excludes_purchase_facts_after_frozen_as_of(db):
    _seed_project_facts(db)
    frozen_as_of = date(2026, 8, 5)
    batch = SysImportBatch(
        filename="migration-future-cost.xlsx",
        file_type="purchase",
        file_hash="migration-future-cost-hash",
    )
    db.add(batch)
    db.flush()
    order = FPurchaseOrder(
        raw_order_id="migration-future-purchase-order",
        order_no="CG-MIGRATION-FUTURE",
        order_date=date(2026, 8, 8),
        is_tax_inclusive=False,
        data_status="已生效",
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    db.add(
        FPurchaseLine(
            raw_line_id="migration-future-purchase-line",
            order_id=order.id,
            line_no=1,
            part_id=21003,
            pn_std="PN-MIGRATION-SOURCE",
            qty=Decimal("2"),
            unit_price=Decimal("30.00"),
            import_batch_id=batch.id,
        )
    )
    db.commit()

    payload = _build(
        db,
        as_of=frozen_as_of,
        warehouse_ready_through=frozen_as_of,
    )
    source_row = payload["post_cutover_site_issues"][0]

    assert source_row["current_cost_resolution"]["cost_source"] == "manual"
    assert source_row["current_cost_resolution"]["reference_sample_ids"] == []
    assert source_row["cost_resolution_matches_current"] is True


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
        warehouse_ready_through=business_today(),
        legacy_truth={
            "cost_lines": pending["legacy_cost_lines"],
            "expenses": pending["legacy_expenses"],
            "source_coverage": pending["source_coverage"]["legacy_source_coverage"],
            "source_hash": pending["source_coverage"]["legacy_source_hash"],
            "source_ready": True,
            "blockers": [],
        },
    )
    approved = _build(db)

    assert rebuilt_pending["source_snapshot_hash"] == approved["source_snapshot_hash"]
    assert (
        build_project_preview(rebuilt_pending)["project_input_fingerprint"]
        != build_project_preview(approved)["project_input_fingerprint"]
    )
