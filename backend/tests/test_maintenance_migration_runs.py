import hashlib
import hmac
from datetime import date
from decimal import Decimal

import pytest

from app.business_time import business_today
from app.models.dimensions import DimPart
from app.models.maintenance_migration import (
    MaintenanceMigrationRun,
    MaintenanceProjectCutoverPlan,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectWorkbookState,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.services import maintenance_migration_runs as runs
from app.services import maintenance_consumption_cost
from app.services import maintenance_migration_source


_SIGNING_KEY = b"synthetic-migration-signing-key-v1"
_SIGNING_KEY_ID = "synthetic-v1"


def _seed_project(
    db,
    project_id="migration-run-project",
    *,
    expense_ready_through: date | None = None,
):
    db.add_all(
        [
            MaintenanceProject(
                project_id=project_id,
                project_code=project_id.upper(),
                display_name="迁移运行合成项目",
                lifecycle_status="ongoing",
            ),
            DimPart(id=21001, pn_std="PN-MIGRATION-RUN"),
            MaintenanceProjectWorkbookState(
                project_id=project_id,
                revision=0,
                data_version=f"{project_id}-version-0",
                expense_ready_through=(
                    expense_ready_through or business_today().replace(day=1)
                ),
            ),
        ]
    )
    db.add(
        MaintenanceSiteIssue(
            issue_id=f"{project_id}-sales-estimate",
            project_id=project_id,
            issue_no="LY-SALES-ESTIMATE",
            issue_date=date(2026, 8, 2),
            raw_status="已确认",
            status_mapping_state="mapped",
            normalized_status="confirmed",
            status_mapping_version="test-v1",
            source="direct_api",
        )
    )
    db.flush()
    db.add(
        MaintenanceSiteIssueLine(
            issue_line_id=f"{project_id}-sales-estimate-line",
            issue_id=f"{project_id}-sales-estimate",
            line_no=1,
            part_id=21001,
            pn="PN-MIGRATION-RUN",
            quantity=Decimal("1"),
            unit_cost=Decimal("20.00"),
            cost_amount=Decimal("20.00"),
            unit_cost_ex_tax=Decimal("20.00"),
            unit_cost_inc_tax=Decimal("22.60"),
            cost_amount_ex_tax=Decimal("20.00"),
            cost_amount_inc_tax=Decimal("22.60"),
            cost_source="manual",
            manual_unit_cost=Decimal("20.00"),
            manual_unit_cost_inc_tax=Decimal("22.60"),
            manual_evidence="合成迁移测试人工成本证据",
            reference_side="manual",
            reference_sample_ids=[],
            reference_sample_count=0,
            reference_samples=[],
            algorithm_version=maintenance_consumption_cost.ALGORITHM_VERSION,
        )
    )
    db.commit()


def _spec(project_id="migration-run-project"):
    baseline = {
        "amount_ex_tax": "100.00",
        "amount_inc_tax": "113.00",
        "evidence_hash": "a" * 64,
        "coverage_from": "2025-01-01",
        "coverage_through": "2026-07-31",
        "scope": "site_issue_parts_only",
        "excludes_expenses": True,
        "source_artifact_locator": f"artifact://migration/{project_id}/history.xlsx",
        "source_row_count": 10,
    }
    baseline["aggregation_fingerprint"] = (
        runs.controls.historical_baseline_aggregation_fingerprint(baseline)
    )
    return {
        "project_id": project_id,
        "cutover_date": "2026-08-01",
        "warehouse_ready_through": business_today().isoformat(),
        "historical_mode": "approved_cost_baseline",
        "historical_baseline": baseline,
        "opening_balances": [
            {
                "balance_key": f"{project_id}:21001",
                "pn": "PN-MIGRATION-RUN",
                "quantity": "10",
                "evidence_hash": "b" * 64,
            }
        ],
    }


def _warehouse_loader(_db, project_id, _cutover_date, _warehouse_ready_through):
    return (
        [
            {
                "movement_id": "shipment-document-1:shipment-line-1",
                "document_id": "shipment-document-1",
                "line_id": "shipment-line-1",
                "document_no": "FH-MIGRATION-1",
                "document_date": "2026-08-02",
                "movement_type": "delivery",
                "source": "maintenance_warehouse_v1",
                "source_document_type": "shipment",
                "source_status": "confirmed",
                "formal_available": False,
                "project_id": project_id,
                "part_id": 21001,
                "balance_key": f"{project_id}:21001",
                "pn": "PN-MIGRATION-RUN",
                "quantity": "3",
            }
        ],
        True,
    )


def _legacy_loader(_db, project_id, as_of):
    evidence = {
        "cost_lines": [
            {
                "source_order_id": f"{project_id}-legacy-order",
                "source_line_id": f"{project_id}-legacy-line",
                "order_no": "WBDD-MIGRATION-LEGACY",
                "order_date": "2026-07-31",
                "pn": "PN-MIGRATION-RUN",
                "sn": None,
                "demand_quantity": "1",
                "return_quantity": "0",
                "effective_quantity": "1",
                "unit_cost_ex_tax": "120.00",
                "unit_cost_inc_tax": "135.60",
                "cost_tax_basis": "ex",
                "cost_amount_ex_tax": "120.00",
                "cost_amount_inc_tax": "135.60",
            }
        ],
        "expenses": [],
        "source_coverage": {
            "legacy_truth_version": "test-v1",
            "business_as_of": as_of.isoformat(),
        },
    }
    return {
        **evidence,
        "source_hash": runs.controls.canonical_hash(evidence),
        "source_ready": True,
        "blockers": [],
    }


def _preview(db, *, key="migration-preview-key", loader=_warehouse_loader):
    result = runs.create_preview_run(
        db,
        idempotency_key=key,
        projects=[_spec()],
        reason="建立合成 dry-run",
        operated_by="creator-user",
        warehouse_loader=loader,
        legacy_loader=_legacy_loader,
    )
    db.commit()
    return result


def _project_signoffs(preview):
    return [
        {
            "project_id": plan["project_id"],
            "expected_plan_version": plan["version"],
            "expected_truth_comparison_hash": plan["truth_comparison"][
                "truth_comparison_hash"
            ],
            "reason": f"逐项核对项目 {plan['project_id']}",
            "historical_baseline": None
            if plan["historical_baseline"] is None
            else {
                "baseline_id": plan["historical_baseline"]["baseline_id"],
                "expected_version": plan["historical_baseline"]["version"],
            },
            "opening_balances": [
                {
                    "opening_balance_id": row["opening_balance_id"],
                    "expected_version": row["version"],
                }
                for row in plan["opening_balances"]
            ],
        }
        for plan in preview["plans"]
    ]


def _reconcile(db, preview, *, key="migration-reconcile-key", loader=_warehouse_loader):
    result = runs.reconcile_run(
        db,
        run_id=preview["run_id"],
        expected_version=preview["version"],
        operation_key=key,
        reason="已逐项核对成本基线与库存期初",
        operated_by="reconciler-user",
        project_signoffs=_project_signoffs(preview),
        warehouse_loader=loader,
        legacy_loader=_legacy_loader,
    )
    db.commit()
    return result


def test_full_workflow_is_hash_bound_idempotent_and_never_activates_production(db):
    _seed_project(db)
    preview = _preview(db)

    assert preview["status"] == "previewed"
    assert preview["version"] == 1
    assert preview["preview"]["approval_blocker_count"] == 2
    # Candidate values stay visible, but an unreviewed baseline does not enter cost.
    assert preview["plans"][0]["cost"]["total_ex_tax"] == "20.00"
    preview_truth = preview["plans"][0]["truth_comparison"]
    assert preview_truth["after_candidate_values_applied"] is True
    assert preview_truth["before"]["total_ex_tax"] == "120.00"
    assert preview_truth["after"]["total_ex_tax"] == "120.00"
    assert preview_truth["delta"]["total_ex_tax"] == "0.00"
    assert preview["plans"][0]["cost"]["sales_estimate_cost_ex_tax"] == "0.00"
    assert preview["plans"][0]["cost"]["sales_estimate_lines"] == 0
    assert preview["plans"][0]["cost"]["cost_progress_includes_sales_estimate"] is False
    assert preview["preview"]["projects"][0]["inventory"][0]["closing_quantity"] == "7"
    assert preview["production_activation_included"] is False

    replay = runs.create_preview_run(
        db,
        idempotency_key="migration-preview-key",
        projects=[_spec()],
        reason="建立合成 dry-run",
        operated_by="creator-user",
        warehouse_loader=_warehouse_loader,
    )
    assert replay["run_id"] == preview["run_id"]
    assert len(replay["events"]) == 1

    reconciled = _reconcile(db, preview)
    assert reconciled["status"] == "reconciled"
    assert reconciled["version"] == 2
    assert reconciled["preview"]["approval_blocker_count"] == 0
    assert reconciled["plans"][0]["cost"]["total_ex_tax"] == "120.00"
    assert (
        reconciled["plans"][0]["truth_comparison"]["truth_comparison_hash"]
        == preview_truth["truth_comparison_hash"]
    )
    assert reconciled["plans"][0]["historical_baseline"]["approval_state"] == "approved"
    assert reconciled["plans"][0]["opening_balances"][0]["approval_state"] == "approved"
    assert all(
        row["status"] == "resolved" for row in reconciled["plans"][0]["discrepancies"]
    )

    reconcile_replay = runs.reconcile_run(
        db,
        run_id=preview["run_id"],
        expected_version=1,
        operation_key="migration-reconcile-key",
        reason="已逐项核对成本基线与库存期初",
        operated_by="reconciler-user",
        project_signoffs=_project_signoffs(preview),
        warehouse_loader=_warehouse_loader,
    )
    assert reconcile_replay["version"] == 2
    assert len(reconcile_replay["events"]) == 2

    approved = runs.approve_run(
        db,
        run_id=preview["run_id"],
        expected_version=reconciled["version"],
        supplied_fingerprint=reconciled["preview"]["input_fingerprint"],
        operation_key="migration-approve-key",
        reason="独立审批合成 manifest",
        operated_by="approver-user",
        signing_key=_SIGNING_KEY,
        signing_key_id=_SIGNING_KEY_ID,
        warehouse_loader=_warehouse_loader,
        legacy_loader=_legacy_loader,
    )
    db.commit()

    assert approved["status"] == "approved"
    assert approved["version"] == 3
    assert approved["manifest"]["production_activation_included"] is False
    assert approved["manifest"]["manifest_hash"] == approved["manifest_hash"]
    assert approved["manifest"]["signing_key_id"] == _SIGNING_KEY_ID
    expected_signature = hmac.new(
        _SIGNING_KEY,
        approved["manifest_hash"].encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    assert approved["manifest"]["manifest_signature"] == expected_signature
    signed_manifest = runs.get_signed_manifest(
        db,
        run_id=preview["run_id"],
        verification_keys={_SIGNING_KEY_ID: _SIGNING_KEY},
        warehouse_loader=_warehouse_loader,
        legacy_loader=_legacy_loader,
    )
    assert signed_manifest["projects"][0]["cost"]["sales_estimate_lines"] == 0
    assert (
        signed_manifest["projects"][0]["cost"]["cost_progress_label"]
        == "priced_cost_without_sales_estimate"
    )
    assert runs.verify_signed_manifest(
        signed_manifest,
        verification_keys={_SIGNING_KEY_ID: _SIGNING_KEY},
        expected_run_id=signed_manifest["run_id"],
        expected_rule_version=runs.controls.RULE_VERSION,
        expected_source_snapshot_hash=signed_manifest["source_snapshot_hash"],
        expected_input_fingerprint=signed_manifest["input_fingerprint"],
    )
    replayed_unsigned = {
        key: value
        for key, value in signed_manifest.items()
        if key not in {"manifest_hash", "manifest_signature"}
    }
    replayed_unsigned["run_id"] = "another-validly-signed-run"
    replayed_hash = runs.controls.canonical_hash(replayed_unsigned)
    replayed_manifest = {
        **replayed_unsigned,
        "manifest_hash": replayed_hash,
        "manifest_signature": hmac.new(
            _SIGNING_KEY, replayed_hash.encode("ascii"), hashlib.sha256
        ).hexdigest(),
    }
    assert not runs.verify_signed_manifest(
        replayed_manifest,
        verification_keys={_SIGNING_KEY_ID: _SIGNING_KEY},
        expected_run_id=signed_manifest["run_id"],
        expected_rule_version=runs.controls.RULE_VERSION,
        expected_source_snapshot_hash=signed_manifest["source_snapshot_hash"],
        expected_input_fingerprint=signed_manifest["input_fingerprint"],
    )
    assert not runs.verify_signed_manifest(
        {**signed_manifest, "rule_version": "tampered"},
        verification_keys={_SIGNING_KEY_ID: _SIGNING_KEY},
        expected_run_id=signed_manifest["run_id"],
        expected_rule_version=runs.controls.RULE_VERSION,
        expected_source_snapshot_hash=signed_manifest["source_snapshot_hash"],
        expected_input_fingerprint=signed_manifest["input_fingerprint"],
    )
    assert not runs.verify_signed_manifest(
        {**signed_manifest, "manifest_version": "unknown-manifest"},
        verification_keys={_SIGNING_KEY_ID: _SIGNING_KEY},
        expected_run_id=signed_manifest["run_id"],
        expected_rule_version=runs.controls.RULE_VERSION,
        expected_source_snapshot_hash=signed_manifest["source_snapshot_hash"],
        expected_input_fingerprint=signed_manifest["input_fingerprint"],
    )
    assert [event["action"] for event in approved["events"]] == [
        "preview",
        "reconcile",
        "approve",
    ]

    approve_replay = runs.approve_run(
        db,
        run_id=preview["run_id"],
        expected_version=2,
        supplied_fingerprint=reconciled["preview"]["input_fingerprint"],
        operation_key="migration-approve-key",
        reason="独立审批合成 manifest",
        operated_by="approver-user",
        signing_key=_SIGNING_KEY,
        signing_key_id=_SIGNING_KEY_ID,
        warehouse_loader=_warehouse_loader,
    )
    assert approve_replay["version"] == 3
    assert len(approve_replay["events"]) == 3


def test_run_freezes_one_as_of_across_midnight_and_all_projects(db, monkeypatch):
    _seed_project(db, expense_ready_through=date(2026, 8, 1))
    second_project_id = "migration-run-project-2"
    db.add_all(
        [
            MaintenanceProject(
                project_id=second_project_id,
                project_code=second_project_id.upper(),
                display_name="第二个迁移合成项目",
                lifecycle_status="ongoing",
            ),
            MaintenanceProjectWorkbookState(
                project_id=second_project_id,
                revision=0,
                data_version=f"{second_project_id}-version-0",
                expense_ready_through=date(2026, 8, 1),
            ),
        ]
    )
    db.commit()
    calls = 0

    def frozen_today():
        nonlocal calls
        calls += 1
        return date(2026, 8, 10)

    monkeypatch.setattr(runs, "business_today", frozen_today)
    monkeypatch.setattr(
        maintenance_migration_source,
        "business_today",
        lambda: (_ for _ in ()).throw(
            AssertionError("显式 as_of 时不得重新读取当前业务日")
        ),
    )
    specs = [_spec(), _spec(second_project_id)]
    for spec in specs:
        spec["warehouse_ready_through"] = "2026-08-10"
    preview = runs.create_preview_run(
        db,
        idempotency_key="frozen-as-of-preview",
        projects=specs,
        reason="验证跨午夜冻结业务截止日",
        operated_by="creator-user",
        warehouse_loader=_warehouse_loader,
        legacy_loader=_legacy_loader,
    )
    db.commit()

    assert calls == 1
    assert preview["as_of"] == "2026-08-10"
    assert {row["as_of"] for row in preview["preview"]["projects"]} == {"2026-08-10"}

    monkeypatch.setattr(runs, "business_today", lambda: date(2026, 8, 11))
    replay = runs.create_preview_run(
        db,
        idempotency_key="frozen-as-of-preview",
        projects=specs,
        reason="验证跨午夜冻结业务截止日",
        operated_by="creator-user",
        warehouse_loader=_warehouse_loader,
        legacy_loader=_legacy_loader,
    )
    assert replay["source_snapshot_hash"] == preview["source_snapshot_hash"]
    reconciled = runs.reconcile_run(
        db,
        run_id=preview["run_id"],
        expected_version=preview["version"],
        operation_key="frozen-as-of-reconcile",
        reason="跨午夜逐项目对账",
        operated_by="reconciler-user",
        project_signoffs=_project_signoffs(preview),
        warehouse_loader=_warehouse_loader,
        legacy_loader=_legacy_loader,
    )
    approved = runs.approve_run(
        db,
        run_id=preview["run_id"],
        expected_version=reconciled["version"],
        supplied_fingerprint=reconciled["preview"]["input_fingerprint"],
        operation_key="frozen-as-of-approve",
        reason="跨午夜独立审批",
        operated_by="approver-user",
        signing_key=_SIGNING_KEY,
        signing_key_id=_SIGNING_KEY_ID,
        warehouse_loader=_warehouse_loader,
        legacy_loader=_legacy_loader,
    )
    db.commit()

    assert approved["as_of"] == "2026-08-10"
    manifest = runs.get_signed_manifest(
        db,
        run_id=preview["run_id"],
        verification_keys={_SIGNING_KEY_ID: _SIGNING_KEY},
        warehouse_loader=_warehouse_loader,
        legacy_loader=_legacy_loader,
    )
    assert {row["as_of"] for row in manifest["projects"]} == {"2026-08-10"}


def test_idempotency_key_reuse_with_changed_reason_or_operator_is_rejected(db):
    _seed_project(db)
    _preview(db)

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="不同迁移清单"):
        runs.create_preview_run(
            db,
            idempotency_key="migration-preview-key",
            projects=[_spec()],
            reason="不同理由",
            operated_by="another-user",
            warehouse_loader=_warehouse_loader,
        )


def test_concurrent_operation_lock_fails_fast_instead_of_waiting_unbounded():
    statements = []

    class BusySession:
        def scalar(self, statement, params):
            statements.append((str(statement), params))
            return False

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="正在处理中"):
        runs._advisory_lock(BusySession(), "preview:busy-operation")

    assert "pg_try_advisory_xact_lock" in statements[0][0]


def test_command_replay_is_bound_to_the_original_named_operator(db):
    _seed_project(db)
    preview = _preview(db)
    reconciled = _reconcile(db, preview)

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="操作人"):
        runs.reconcile_run(
            db,
            run_id=preview["run_id"],
            expected_version=preview["version"],
            operation_key="migration-reconcile-key",
            reason="已逐项核对成本基线与库存期初",
            operated_by="different-reconciler",
            project_signoffs=_project_signoffs(preview),
            warehouse_loader=_warehouse_loader,
        )

    approved = runs.approve_run(
        db,
        run_id=preview["run_id"],
        expected_version=reconciled["version"],
        supplied_fingerprint=reconciled["preview"]["input_fingerprint"],
        operation_key="actor-bound-approve",
        reason="独立审批并绑定操作人",
        operated_by="approver-user",
        signing_key=_SIGNING_KEY,
        signing_key_id=_SIGNING_KEY_ID,
        warehouse_loader=_warehouse_loader,
        legacy_loader=_legacy_loader,
    )
    db.commit()
    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="操作人"):
        runs.approve_run(
            db,
            run_id=preview["run_id"],
            expected_version=reconciled["version"],
            supplied_fingerprint=reconciled["preview"]["input_fingerprint"],
            operation_key="actor-bound-approve",
            reason="独立审批并绑定操作人",
            operated_by="different-approver",
            signing_key=_SIGNING_KEY,
            signing_key_id=_SIGNING_KEY_ID,
            warehouse_loader=_warehouse_loader,
        )
    assert approved["approved_by"] == "approver-user"


def test_creator_cannot_reconcile_own_dry_run(db):
    _seed_project(db)
    preview = _preview(db)

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="独立"):
        runs.reconcile_run(
            db,
            run_id=preview["run_id"],
            expected_version=preview["version"],
            operation_key="creator-self-reconcile",
            reason="创建人试图兼任对账人",
            operated_by="creator-user",
            project_signoffs=_project_signoffs(preview),
            warehouse_loader=_warehouse_loader,
        )

    db.rollback()
    stored = db.get(MaintenanceMigrationRun, preview["run_id"])
    assert stored.status == "previewed"
    assert stored.reconciled_by is None


def test_reconcile_rejects_stale_truth_comparison_signoff(db):
    _seed_project(db)
    preview = _preview(db)
    signoffs = _project_signoffs(preview)
    signoffs[0]["expected_truth_comparison_hash"] = "f" * 64

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="新旧口径差异"):
        runs.reconcile_run(
            db,
            run_id=preview["run_id"],
            expected_version=preview["version"],
            operation_key="stale-truth-comparison-signoff",
            reason="拒绝过期的新旧口径差异签字",
            operated_by="reconciler-user",
            project_signoffs=signoffs,
            warehouse_loader=_warehouse_loader,
            legacy_loader=_legacy_loader,
        )

    db.rollback()
    stored = db.get(MaintenanceMigrationRun, preview["run_id"])
    assert stored.status == "previewed"
    assert stored.reconciled_by is None


def test_reconcile_recomputes_candidate_applied_truth_before_accepting_signoff(db):
    _seed_project(db)
    preview = _preview(db)
    plan = db.get(MaintenanceProjectCutoverPlan, preview["plans"][0]["plan_id"])
    plan.truth_comparison_hash = "f" * 64
    db.commit()
    signoffs = _project_signoffs(preview)
    signoffs[0]["expected_truth_comparison_hash"] = "f" * 64

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="候选应用后"):
        runs.reconcile_run(
            db,
            run_id=preview["run_id"],
            expected_version=preview["version"],
            operation_key="forged-candidate-truth-signoff",
            reason="拒绝未绑定候选应用后真值的签字",
            operated_by="reconciler-user",
            project_signoffs=signoffs,
            warehouse_loader=_warehouse_loader,
            legacy_loader=_legacy_loader,
        )

    db.rollback()
    stored = db.get(MaintenanceMigrationRun, preview["run_id"])
    assert stored.status == "previewed"
    assert stored.reconciled_by is None


def test_rule_version_change_invalidates_unreconciled_run(db, monkeypatch):
    _seed_project(db)
    preview = _preview(db)
    monkeypatch.setattr(runs.controls, "RULE_VERSION", "maintenance-cutover-v2")

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="规则版本"):
        _reconcile(db, preview)


def test_reconcile_rejects_omitted_candidate_without_approving_anything(db):
    _seed_project(db)
    preview = _preview(db)
    signoffs = _project_signoffs(preview)
    signoffs[0]["opening_balances"] = []

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="候选清单不完整"):
        runs.reconcile_run(
            db,
            run_id=preview["run_id"],
            expected_version=preview["version"],
            operation_key="omitted-candidate-reconcile",
            reason="故意漏选库存期初",
            operated_by="reconciler-user",
            project_signoffs=signoffs,
            warehouse_loader=_warehouse_loader,
        )

    unchanged = runs.get_run_detail(db, run_id=preview["run_id"])
    assert unchanged["status"] == "previewed"
    assert unchanged["plans"][0]["historical_baseline"]["approval_state"] == "pending"
    assert unchanged["plans"][0]["opening_balances"][0]["approval_state"] == "pending"


def test_run_detail_summarizes_evidence_and_evidence_rows_are_paginated(db):
    _seed_project(db)
    preview = _preview(db)

    project_preview = preview["preview"]["projects"][0]
    assert "evidence" not in project_preview
    assert project_preview["evidence_summary"]["inventory_movements"] == 1

    evidence = runs.get_project_evidence(
        db,
        run_id=preview["run_id"],
        project_id="migration-run-project",
        section="inventory_movements",
        page=1,
        page_size=1,
    )

    assert evidence["total"] == 1
    assert evidence["items"][0]["document_no"] == "FH-MIGRATION-1"
    assert evidence["items"][0]["sn"] is None
    assert evidence["source_snapshot_hash"] == project_preview["source_snapshot_hash"]


def test_final_approver_must_be_independent(db):
    _seed_project(db)
    preview = _preview(db)
    reconciled = _reconcile(db, preview)

    with pytest.raises(
        runs.MaintenanceMigrationRunConflict, match="最终审批人必须独立"
    ):
        runs.approve_run(
            db,
            run_id=preview["run_id"],
            expected_version=reconciled["version"],
            supplied_fingerprint=reconciled["preview"]["input_fingerprint"],
            operation_key="non-independent-approval",
            reason="不能自审",
            operated_by="reconciler-user",
            signing_key=_SIGNING_KEY,
            signing_key_id=_SIGNING_KEY_ID,
            warehouse_loader=_warehouse_loader,
        )


def test_source_change_after_reconciliation_invalidates_approval(db):
    _seed_project(db)
    preview = _preview(db)
    reconciled = _reconcile(db, preview)

    project = db.get(MaintenanceProject, "migration-run-project")
    project.version += 1
    db.commit()

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="来源事实已变化"):
        runs.approve_run(
            db,
            run_id=preview["run_id"],
            expected_version=reconciled["version"],
            supplied_fingerprint=reconciled["preview"]["input_fingerprint"],
            operation_key="stale-source-approval",
            reason="来源变化后不能审批",
            operated_by="approver-user",
            signing_key=_SIGNING_KEY,
            signing_key_id=_SIGNING_KEY_ID,
            warehouse_loader=_warehouse_loader,
        )


def test_source_change_after_approval_invalidates_manifest_download(db):
    _seed_project(db)
    preview = _preview(db, key="stale-manifest-preview")
    reconciled = _reconcile(db, preview, key="stale-manifest-reconcile")
    runs.approve_run(
        db,
        run_id=preview["run_id"],
        expected_version=reconciled["version"],
        supplied_fingerprint=reconciled["preview"]["input_fingerprint"],
        operation_key="stale-manifest-approve",
        reason="独立审批后验证来源变化",
        operated_by="approver-user",
        signing_key=_SIGNING_KEY,
        signing_key_id=_SIGNING_KEY_ID,
        warehouse_loader=_warehouse_loader,
        legacy_loader=_legacy_loader,
    )
    db.commit()

    project = db.get(MaintenanceProject, "migration-run-project")
    project.version += 1
    db.commit()

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="来源事实已变化"):
        runs.get_signed_manifest(
            db,
            run_id=preview["run_id"],
            verification_keys={_SIGNING_KEY_ID: _SIGNING_KEY},
            warehouse_loader=_warehouse_loader,
            legacy_loader=_legacy_loader,
        )


def test_manifest_download_rechecks_persisted_signature(db):
    _seed_project(db)
    preview = _preview(db, key="tampered-manifest-preview")
    reconciled = _reconcile(db, preview, key="tampered-manifest-reconcile")
    runs.approve_run(
        db,
        run_id=preview["run_id"],
        expected_version=reconciled["version"],
        supplied_fingerprint=reconciled["preview"]["input_fingerprint"],
        operation_key="tampered-manifest-approve",
        reason="独立审批后验证签名",
        operated_by="approver-user",
        signing_key=_SIGNING_KEY,
        signing_key_id=_SIGNING_KEY_ID,
        warehouse_loader=_warehouse_loader,
        legacy_loader=_legacy_loader,
    )
    db.commit()

    run = db.get(MaintenanceMigrationRun, preview["run_id"])
    run.manifest_json = {
        **run.manifest_json,
        "manifest_signature": "0" * 64,
    }
    db.commit()

    with pytest.raises(
        runs.MaintenanceMigrationRunConflict, match="签名或绑定事实无效"
    ):
        runs.get_signed_manifest(
            db,
            run_id=preview["run_id"],
            verification_keys={_SIGNING_KEY_ID: _SIGNING_KEY},
            warehouse_loader=_warehouse_loader,
            legacy_loader=_legacy_loader,
        )


def test_manifest_download_binds_persisted_hash_key_and_approval_chain(db):
    _seed_project(db)
    preview = _preview(db, key="persisted-binding-preview")
    reconciled = _reconcile(db, preview, key="persisted-binding-reconcile")
    runs.approve_run(
        db,
        run_id=preview["run_id"],
        expected_version=reconciled["version"],
        supplied_fingerprint=reconciled["preview"]["input_fingerprint"],
        operation_key="persisted-binding-approve",
        reason="独立审批后验证持久化绑定",
        operated_by="approver-user",
        signing_key=_SIGNING_KEY,
        signing_key_id=_SIGNING_KEY_ID,
        warehouse_loader=_warehouse_loader,
        legacy_loader=_legacy_loader,
    )
    db.commit()

    run = db.get(MaintenanceMigrationRun, preview["run_id"])
    run.manifest_hash = "f" * 64
    db.commit()

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="持久化审批事实"):
        runs.get_signed_manifest(
            db,
            run_id=preview["run_id"],
            verification_keys={_SIGNING_KEY_ID: _SIGNING_KEY},
            warehouse_loader=_warehouse_loader,
            legacy_loader=_legacy_loader,
        )


def test_unavailable_warehouse_source_cannot_be_approved(db):
    _seed_project(db)
    preview = _preview(
        db,
        key="warehouse-unavailable-preview",
        loader=runs.unavailable_warehouse_loader,
    )
    assert "warehouse_source_not_ready" in {
        row["code"] for row in preview["preview"]["projects"][0]["approval_blockers"]
    }
    reconciled = _reconcile(
        db,
        preview,
        key="warehouse-unavailable-reconcile",
        loader=runs.unavailable_warehouse_loader,
    )
    assert reconciled["preview"]["can_approve"] is False

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="仍有未解决差异"):
        runs.approve_run(
            db,
            run_id=preview["run_id"],
            expected_version=reconciled["version"],
            supplied_fingerprint=reconciled["preview"]["input_fingerprint"],
            operation_key="warehouse-unavailable-approve",
            reason="仓库来源未接入时拒绝",
            operated_by="approver-user",
            signing_key=_SIGNING_KEY,
            signing_key_id=_SIGNING_KEY_ID,
            warehouse_loader=runs.unavailable_warehouse_loader,
            legacy_loader=_legacy_loader,
        )


def test_warehouse_adapter_rejects_pre_cutover_movement_before_preview(db):
    _seed_project(db)

    def overlapping_loader(_db, project_id, _cutover_date, _warehouse_ready_through):
        return (
            [
                {
                    "movement_id": "pre-cutover-document:pre-cutover-line",
                    "document_id": "pre-cutover-document",
                    "line_id": "pre-cutover-line",
                    "document_date": "2026-07-31",
                    "movement_type": "delivery",
                    "source": "maintenance_warehouse_v1",
                    "source_document_type": "shipment",
                    "source_status": "confirmed",
                    "formal_available": False,
                    "project_id": project_id,
                    "part_id": 21001,
                    "balance_key": f"{project_id}:21001",
                    "quantity": "1",
                }
            ],
            True,
        )

    with pytest.raises(runs.MaintenanceMigrationRunError, match="早于切换日"):
        runs.create_preview_run(
            db,
            idempotency_key="pre-cutover-adapter-preview",
            projects=[_spec()],
            reason="适配器必须拒绝切换日前流水",
            operated_by="creator-user",
            warehouse_loader=overlapping_loader,
        )


def test_run_level_fact_budget_rejects_cross_project_materialization(db, monkeypatch):
    frozen_as_of = business_today()
    specs = [
        {**spec, "as_of": frozen_as_of.isoformat()}
        for spec in runs._normalize_specs([_spec()])
    ]
    monkeypatch.setattr(runs, "MAX_MIGRATION_FACT_ROWS", 0)
    monkeypatch.setattr(
        runs, "lock_project_source_snapshots", lambda _db, *, project_ids: None
    )
    monkeypatch.setattr(
        runs,
        "_load_warehouse",
        lambda *_args, **_kwargs: ((), True, ()),
    )
    monkeypatch.setattr(
        runs,
        "build_project_source_payload",
        lambda *_args, **_kwargs: {
            "historical_site_issues": [],
            "post_cutover_site_issues": [],
            "approved_expenses": [{"expense_id": "one-row"}],
            "opening_balances": [],
            "inventory_movements": [],
        },
    )

    with pytest.raises(runs.MaintenanceMigrationRunError, match="事实总行数"):
        runs._source_payloads_from_specs(
            db,
            specs=specs,
            loader=runs.unavailable_warehouse_loader,
            legacy_loader=_legacy_loader,
            as_of=frozen_as_of,
        )


def test_search_and_optimistic_version_fail_closed(db):
    _seed_project(db)
    preview = _preview(db)

    result = runs.search_runs(db, statuses=["previewed"], page=1, page_size=20)
    assert result["total"] == 1
    assert result["items"][0]["run_id"] == preview["run_id"]

    with pytest.raises(runs.MaintenanceMigrationRunConflict, match="版本已变化"):
        runs.reconcile_run(
            db,
            run_id=preview["run_id"],
            expected_version=99,
            operation_key="wrong-version-reconcile",
            reason="错误版本必须失败",
            operated_by="reconciler-user",
            project_signoffs=_project_signoffs(preview),
            warehouse_loader=_warehouse_loader,
        )
