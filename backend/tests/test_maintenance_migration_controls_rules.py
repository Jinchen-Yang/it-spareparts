from copy import deepcopy
from decimal import Decimal

import pytest

from app.services import maintenance_consumption_cost
from app.services import maintenance_migration_controls as controls


def _manual_cost_evidence():
    return {
        "cost_source": "manual",
        "price_basis": "ex_tax",
        "manual_unit_cost": "10.00",
        "manual_unit_cost_inc_tax": "11.30",
        "manual_evidence": "合成规则测试人工确认",
        "unit_cost_ex_tax": "10.00",
        "unit_cost_inc_tax": "11.30",
        "tax_rate_used": "0.13",
        "reference_side": "manual",
        "reference_sample_ids": [],
        "reference_sample_count": 0,
        "reference_samples": [],
        "reference_window_from": None,
        "reference_window_to": None,
        "algorithm_version": maintenance_consumption_cost.ALGORITHM_VERSION,
    }


def _sales_cost_evidence():
    return {
        "cost_source": "sales_window",
        "manual_evidence": None,
        "unit_cost_ex_tax": "10.00",
        "unit_cost_inc_tax": "11.30",
        "reference_side": "sales",
        "reference_sample_ids": ["sales:101"],
        "reference_sample_count": 1,
        "reference_samples": [
            {
                "sample_id": "sales:101",
                "document_no": "XS-101",
                "document_date": "2026-08-05",
                "distance_days": 3,
                "quantity": "2",
                "unit_price_raw": "11.30",
                "unit_price_ex_tax": "10.00",
                "tax_conversion": "divide_1.13",
            }
        ],
        "reference_window_from": "2026-07-26",
        "reference_window_to": "2026-08-09",
    }


def _historical_baseline(**overrides):
    baseline = {
        "amount_ex_tax": "100.00",
        "amount_inc_tax": "113.00",
        "evidence_hash": "a" * 64,
        "coverage_from": "2025-01-01",
        "coverage_through": "2026-07-31",
        "scope": "site_issue_parts_only",
        "excludes_expenses": True,
        "source_artifact_locator": "artifact://migration/project-1/history.xlsx",
        "source_row_count": 10,
    }
    baseline.update(overrides)
    baseline["aggregation_fingerprint"] = (
        controls.historical_baseline_aggregation_fingerprint(baseline)
    )
    baseline["approved"] = True
    return baseline


def _bind_current_cost_resolution(row):
    resolution = {
        field: deepcopy(row.get(field))
        for field in controls.SITE_ISSUE_COST_RESOLUTION_FIELDS
    }
    row["current_cost_resolution"] = resolution
    row["stored_cost_resolution_hash"] = controls.canonical_hash(resolution)
    row["current_cost_resolution_hash"] = controls.canonical_hash(resolution)
    row["cost_resolution_matches_current"] = True


def _bind_payload_cost_resolutions(payload):
    for section in ("historical_site_issues", "post_cutover_site_issues"):
        for row in payload.get(section) or []:
            _bind_current_cost_resolution(row)
    return payload


def _preview(payload):
    return controls.build_project_preview(_bind_payload_cost_resolutions(payload))


def _project(**overrides):
    payload = {
        "project_id": "project-1",
        "cutover_date": "2026-08-01",
        "as_of": "2026-08-09",
        "historical_mode": "approved_cost_baseline",
        "historical_baseline": _historical_baseline(),
        "post_cutover_site_issues": [
            {
                "issue_line_id": "issue-line-1",
                "issue_date": "2026-08-02",
                "workflow_status": "confirmed",
                "stable_identity": True,
                "quantity": "2",
                "cost_amount_ex_tax": "20.00",
                "cost_amount_inc_tax": "22.60",
                **_manual_cost_evidence(),
            }
        ],
        "approved_expenses": [
            {
                "expense_id": "expense-1",
                "expense_date": "2026-08-03",
                "normalized_status": "approved",
                "amount_ex_tax": "10.00",
                "amount_inc_tax": "11.30",
            }
        ],
        "legacy_cost_lines": [
            {
                "source_order_id": "legacy-order-1",
                "source_line_id": "legacy-line-1",
                "order_no": "WBDD-LEGACY-1",
                "order_date": "2026-07-31",
                "pn": "PN-1",
                "sn": None,
                "demand_quantity": "2",
                "return_quantity": "0",
                "effective_quantity": "2",
                "unit_cost_ex_tax": "60.00",
                "unit_cost_inc_tax": "67.80",
                "cost_tax_basis": "ex",
                "cost_amount_ex_tax": "120.00",
                "cost_amount_inc_tax": "135.60",
            }
        ],
        "legacy_expenses": [
            {
                "expense_id": "legacy-expense-1",
                "expense_ref": "BXD-LEGACY-1",
                "expense_date": "2026-08-03",
                "normalized_status": "approved",
                "tax_basis": "default_ex",
                "amount_ex_tax": "10.00",
                "amount_inc_tax": "11.30",
            }
        ],
        "opening_balances": [
            {
                "balance_key": "project-1:1",
                "part_id": 1,
                "quantity": "10",
                "evidence_hash": "b" * 64,
                "approved": True,
            }
        ],
        "inventory_movements": [
            {
                "movement_id": "shipment-document-1:shipment-line-1",
                "document_id": "shipment-document-1",
                "line_id": "shipment-line-1",
                "document_date": "2026-08-02",
                "movement_type": "delivery",
                "source": "maintenance_warehouse_v1",
                "source_document_type": "shipment",
                "source_status": "confirmed",
                "formal_available": False,
                "project_id": "project-1",
                "part_id": 1,
                "quantity": "3",
                "balance_key": "project-1:1",
            },
            {
                "movement_id": "receipt-document-1:receipt-line-1",
                "document_id": "receipt-document-1",
                "line_id": "receipt-line-1",
                "document_date": "2026-08-02",
                "movement_type": "available_receipt",
                "source": "maintenance_warehouse_v1",
                "source_document_type": "receipt",
                "source_status": "confirmed",
                "formal_available": True,
                "project_id": "project-1",
                "part_id": 1,
                "quantity": "2",
                "balance_key": "project-1:1",
            },
            {
                "movement_id": "site-issue-1:site-issue-line-1",
                "document_id": "site-issue-1",
                "line_id": "site-issue-line-1",
                "document_date": "2026-08-02",
                "movement_type": "site_issue",
                "source": "site_issue_v2",
                "source_document_type": "site_issue",
                "source_status": "confirmed",
                "project_id": "project-1",
                "part_id": 1,
                "quantity": "2",
                "balance_key": "project-1:1",
            },
            {
                "movement_id": "return-document-1:return-line-1",
                "document_id": "return-document-1",
                "line_id": "return-line-1",
                "document_date": "2026-08-02",
                "movement_type": "return_registration",
                "source": "maintenance_warehouse_v1",
                "source_document_type": "return",
                "source_status": "confirmed",
                "formal_available": False,
                "project_id": "project-1",
                "part_id": 1,
                "quantity": "1",
                "balance_key": "project-1:1",
            },
        ],
        "return_offsets": [{"return_id": "return-1", "amount_ex_tax": "999.00"}],
        "source_snapshot_hash": "c" * 64,
        "source_coverage": {"legacy_source_hash": "d" * 64},
    }
    payload.update(overrides)
    return _bind_payload_cost_resolutions(payload)


def test_preview_uses_only_baseline_post_cutover_consumption_and_approved_expenses():
    preview = _preview(_project())

    assert preview["cost"] == {
        "historical_baseline_ex_tax": "100.00",
        "historical_baseline_inc_tax": "113.00",
        "post_cutover_consumption_ex_tax": "20.00",
        "post_cutover_consumption_inc_tax": "22.60",
        "approved_expense_ex_tax": "10.00",
        "approved_expense_inc_tax": "11.30",
        "sales_estimate_cost_ex_tax": "0.00",
        "sales_estimate_cost_inc_tax": "0.00",
        "sales_estimate_lines": 0,
        "cost_progress_includes_sales_estimate": False,
        "cost_progress_label": "priced_cost_without_sales_estimate",
        "total_ex_tax": "130.00",
        "total_inc_tax": "146.90",
    }
    assert preview["ignored_return_offset_count"] == 1
    assert preview["approval_blockers"] == []


def test_historical_baseline_cross_month_boundary_is_machine_verified():
    payload = _project()
    payload["historical_baseline"] = _historical_baseline(
        coverage_from="2025-12-15",
        coverage_through="2026-07-31",
    )

    preview = _preview(payload)

    assert preview["cost"]["historical_baseline_ex_tax"] == "100.00"
    assert "historical_baseline_contract_invalid" not in {
        row["code"] for row in preview["approval_blockers"]
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"coverage_from": "2026-08-01", "coverage_through": "2026-07-31"},
        {"coverage_through": "2026-08-01"},
        {"scope": "all_project_costs"},
        {"excludes_expenses": False},
        {"amount_inc_tax": "999.00"},
    ],
)
def test_historical_baseline_invalid_scope_boundary_or_tax_blocks(changes):
    payload = _project()
    payload["historical_baseline"] = _historical_baseline(**changes)

    preview = _preview(payload)

    assert preview["cost"]["historical_baseline_ex_tax"] == "0.00"
    assert "historical_baseline_contract_invalid" in {
        row["code"] for row in preview["approval_blockers"]
    }


def test_historical_baseline_forged_aggregation_fingerprint_blocks():
    payload = _project()
    payload["historical_baseline"]["aggregation_fingerprint"] = "f" * 64

    preview = _preview(payload)

    assert "historical_baseline_contract_invalid" in {
        row["code"] for row in preview["approval_blockers"]
    }


def test_preview_never_treats_demand_or_return_registration_as_consumption_or_inventory():
    payload = _project()
    payload["maintenance_demands"] = [
        {"demand_line_id": "wbdd-1", "quantity": "999", "amount": "99999"}
    ]
    preview = _preview(payload)

    assert "maintenance_demands" not in preview
    assert preview["cost"]["post_cutover_consumption_ex_tax"] == "20.00"
    assert preview["inventory"][0]["closing_quantity"] == "9"
    assert preview["inventory"][0]["ignored_site_issue_quantity"] == "2"
    assert preview["inventory"][0]["ignored_return_registration_quantity"] == "1"


def test_truth_comparison_exposes_before_after_delta_and_document_evidence():
    preview = _preview(_project())

    assert preview["truth_comparison"]["before"] == {
        "parts_cost_ex_tax": "120.00",
        "parts_cost_inc_tax": "135.60",
        "approved_expense_ex_tax": "10.00",
        "approved_expense_inc_tax": "11.30",
        "total_ex_tax": "130.00",
        "total_inc_tax": "146.90",
    }
    assert preview["truth_comparison"]["after"] == preview["truth_comparison"]["before"]
    assert set(preview["truth_comparison"]["delta"].values()) == {"0.00"}
    assert len(preview["truth_comparison"]["truth_comparison_hash"]) == 64
    legacy = preview["evidence"]["legacy_cost_lines"][0]
    assert legacy["order_no"] == "WBDD-LEGACY-1"
    assert legacy["pn"] == "PN-1"
    assert (
        preview["evidence"]["truth_quantity_differences"][0]["before_quantity"] == "2"
    )


def test_legacy_demand_change_updates_source_fingerprint_and_truth_hash_only_before():
    first_payload = _project()
    first = _preview(first_payload)
    changed_payload = _project()
    legacy = changed_payload["legacy_cost_lines"][0]
    legacy.update(
        {
            "demand_quantity": "3",
            "effective_quantity": "3",
            "cost_amount_ex_tax": "180.00",
            "cost_amount_inc_tax": "203.40",
        }
    )
    changed = _preview(changed_payload)

    assert changed["truth_comparison"]["before"]["parts_cost_ex_tax"] == "180.00"
    assert changed["truth_comparison"]["after"]["parts_cost_ex_tax"] == "120.00"
    assert changed["truth_comparison"]["delta"]["parts_cost_ex_tax"] == "-60.00"
    assert changed["project_input_fingerprint"] != first["project_input_fingerprint"]
    assert (
        changed["truth_comparison"]["truth_comparison_hash"]
        != first["truth_comparison"]["truth_comparison_hash"]
    )
    assert changed["cost"]["post_cutover_consumption_ex_tax"] == "20.00"


def test_invalid_legacy_quantity_blocks_without_aborting_difference_preview():
    payload = _project()
    payload["legacy_cost_lines"][0]["effective_quantity"] = None

    preview = _preview(payload)

    assert preview["can_approve"] is False
    assert "legacy_cost_fact_invalid" in {
        blocker["code"] for blocker in preview["approval_blockers"]
    }
    assert preview["truth_comparison"]["before"]["parts_cost_ex_tax"] == "0.00"


def test_legacy_inc_tax_basis_is_recomputed_in_its_original_direction():
    payload = _project()
    payload["legacy_cost_lines"][0].update(
        {
            "unit_cost_ex_tax": "88.50",
            "unit_cost_inc_tax": "100.00",
            "cost_tax_basis": "inc",
            "cost_amount_ex_tax": "177.00",
            "cost_amount_inc_tax": "200.00",
        }
    )
    payload["legacy_expenses"][0].update(
        {
            "tax_basis": "inc",
            "amount_ex_tax": "88.50",
            "amount_inc_tax": "100.00",
        }
    )

    preview = _preview(payload)

    assert "legacy_expense_fact_invalid" not in {
        blocker["code"] for blocker in preview["approval_blockers"]
    }
    assert "legacy_cost_fact_invalid" not in {
        blocker["code"] for blocker in preview["approval_blockers"]
    }
    assert preview["truth_comparison"]["before"]["parts_cost_ex_tax"] == "177.00"
    assert preview["truth_comparison"]["before"]["parts_cost_inc_tax"] == "200.00"
    assert preview["truth_comparison"]["before"]["approved_expense_ex_tax"] == "88.50"
    assert preview["truth_comparison"]["before"]["approved_expense_inc_tax"] == "100.00"


@pytest.mark.parametrize("status", ["draft", "void", "unknown"])
def test_only_confirmed_or_corrected_site_issues_enter_cost(status):
    payload = _project()
    payload["post_cutover_site_issues"][0]["workflow_status"] = status

    preview = _preview(payload)

    assert preview["cost"]["post_cutover_consumption_ex_tax"] == "0.00"
    codes = {row["code"] for row in preview["approval_blockers"]}
    if status == "void":
        assert "unapproved_site_issue" not in codes
    else:
        assert "unapproved_site_issue" in codes


def test_corrected_site_issue_with_stable_identity_enters_cost():
    payload = _project()
    payload["post_cutover_site_issues"][0]["workflow_status"] = "corrected"

    preview = _preview(payload)

    assert preview["cost"]["post_cutover_consumption_ex_tax"] == "20.00"
    assert "unapproved_site_issue" not in {
        row["code"] for row in preview["approval_blockers"]
    }


def test_sales_window_requires_reproducible_samples_and_exact_window():
    payload = _project()
    payload["post_cutover_site_issues"][0].update(
        {
            "cost_source": "sales_window",
            "manual_evidence": None,
            "reference_side": None,
            "reference_sample_ids": [],
            "reference_sample_count": 0,
            "reference_samples": [],
            "reference_window_from": None,
            "reference_window_to": None,
        }
    )

    preview = _preview(payload)

    assert preview["cost"]["post_cutover_consumption_ex_tax"] == "0.00"
    assert "site_issue_invalid_cost_evidence" in {
        row["code"] for row in preview["approval_blockers"]
    }
    assert preview["can_approve"] is False


def test_sales_window_estimate_is_aggregated_and_explicitly_labeled():
    payload = _project()
    payload["post_cutover_site_issues"][0].update(_sales_cost_evidence())

    preview = _preview(payload)

    assert preview["cost"]["sales_estimate_cost_ex_tax"] == "20.00"
    assert preview["cost"]["sales_estimate_cost_inc_tax"] == "22.60"
    assert preview["cost"]["sales_estimate_lines"] == 1
    assert preview["cost"]["cost_progress_includes_sales_estimate"] is True
    assert (
        preview["cost"]["cost_progress_label"] == "priced_cost_including_sales_estimate"
    )
    assert preview["can_approve"] is True


def test_cost_evidence_recomputes_sample_unit_weighted_unit_and_issue_amount():
    payload = _project()
    row = payload["post_cutover_site_issues"][0]
    row.update(_sales_cost_evidence())
    row.update(
        {
            "quantity": "1",
            "unit_cost_ex_tax": "999.00",
            "unit_cost_inc_tax": "1128.87",
            "cost_amount_ex_tax": "999.00",
            "cost_amount_inc_tax": "1128.87",
        }
    )

    preview = _preview(payload)

    assert preview["cost"]["post_cutover_consumption_ex_tax"] == "0.00"
    assert preview["can_approve"] is False
    assert "site_issue_invalid_cost_evidence" in {
        blocker["code"] for blocker in preview["approval_blockers"]
    }


def test_cost_evidence_rejects_stale_algorithm_version():
    payload = _project()
    payload["post_cutover_site_issues"][0]["algorithm_version"] = "stale-v0"

    preview = _preview(payload)

    assert preview["can_approve"] is False
    assert "site_issue_invalid_cost_evidence" in {
        blocker["code"] for blocker in preview["approval_blockers"]
    }


def test_future_issue_and_expense_are_blocked_and_excluded_from_cost():
    payload = _project()
    payload["post_cutover_site_issues"][0]["issue_date"] = "2099-01-02"
    payload["approved_expenses"][0]["expense_date"] = "2099-01-03"

    preview = _preview(payload)

    assert preview["as_of"] == "2026-08-09"
    assert preview["cost"]["post_cutover_consumption_ex_tax"] == "0.00"
    assert preview["cost"]["approved_expense_ex_tax"] == "0.00"
    assert preview["cost"]["total_ex_tax"] == "100.00"
    assert all(
        not row["comparison_key"].startswith("site_issue:")
        for row in preview["evidence"]["truth_quantity_differences"]
    )
    assert preview["can_approve"] is False
    assert {"site_issue_after_as_of", "expense_after_as_of"}.issubset(
        {blocker["code"] for blocker in preview["approval_blockers"]}
    )


def test_approved_expense_tax_mismatch_is_blocked_and_excluded():
    payload = _project()
    payload["approved_expenses"][0]["amount_inc_tax"] = "99.99"

    preview = _preview(payload)

    assert preview["cost"]["approved_expense_ex_tax"] == "0.00"
    assert preview["cost"]["approved_expense_inc_tax"] == "0.00"
    assert "expense_tax_mismatch" in {
        blocker["code"] for blocker in preview["approval_blockers"]
    }


@pytest.mark.parametrize("status", ["rejected", "void"])
def test_explicitly_excluded_expenses_do_not_block_cutover(status):
    payload = _project()
    payload["approved_expenses"][0]["normalized_status"] = status

    preview = _preview(payload)

    assert preview["cost"]["approved_expense_ex_tax"] == "0.00"
    assert "expense_not_approved" not in {
        row["code"] for row in preview["approval_blockers"]
    }


def test_missing_site_issue_cost_is_not_silently_coerced_to_zero():
    payload = _project()
    payload["post_cutover_site_issues"][0].update(
        {
            "cost_amount_ex_tax": None,
            "cost_amount_inc_tax": None,
            "cost_source": None,
        }
    )

    preview = _preview(payload)

    assert preview["cost"]["post_cutover_consumption_ex_tax"] == "0.00"
    assert "missing_site_issue_cost" in {
        row["code"] for row in preview["approval_blockers"]
    }


def test_baseline_and_historical_issue_modes_are_mutually_exclusive():
    payload = _project(
        historical_mode="stable_site_issues",
        historical_site_issues=[
            {
                "issue_line_id": "historical-1",
                "issue_date": "2026-07-31",
                "workflow_status": "confirmed",
                "cost_amount_ex_tax": "50.00",
                "cost_amount_inc_tax": "56.50",
                "stable_identity": True,
                **_manual_cost_evidence(),
            }
        ],
    )

    with pytest.raises(controls.MigrationControlError, match="不能同时"):
        _preview(payload)


def test_baseline_mode_rejects_reliable_historical_issues_without_hiding_evidence():
    payload = _project(
        historical_site_issues=[
            {
                "issue_line_id": "historical-reliable-1",
                "issue_date": "2026-07-31",
                "workflow_status": "confirmed",
                "cost_amount_ex_tax": "50.00",
                "cost_amount_inc_tax": "56.50",
                "stable_identity": True,
                **_manual_cost_evidence(),
            }
        ]
    )

    with pytest.raises(controls.MigrationControlError, match="可靠历史领用"):
        _preview(payload)


def test_unreliable_historical_rows_remain_visible_when_baseline_is_required():
    payload = _project(
        historical_site_issues=[
            {
                "issue_line_id": "historical-legacy-1",
                "issue_date": "2026-07-31",
                "workflow_status": "confirmed",
                "cost_amount_ex_tax": "50.00",
                "cost_amount_inc_tax": "56.50",
                "stable_identity": False,
                **_manual_cost_evidence(),
            }
        ]
    )

    preview = _preview(payload)

    assert preview["cost"]["historical_baseline_ex_tax"] == "100.00"
    evidence = preview["evidence"]["historical_site_issues"][0]
    assert evidence["issue_line_id"] == "historical-legacy-1"
    assert evidence["workflow_status"] == "confirmed"
    assert evidence["stable_identity"] is False
    assert evidence["cost_amount_ex_tax"] == "50.00"
    assert evidence["sn"] is None


@pytest.mark.parametrize("document_date", [None, "2026-07-31"])
def test_inventory_movements_without_post_cutover_date_are_blocked_and_not_counted(
    document_date,
):
    payload = _project()
    payload["inventory_movements"] = [
        {
            "movement_id": "overlap-document:overlap-line",
            "document_id": "overlap-document",
            "line_id": "overlap-line",
            "document_date": document_date,
            "movement_type": "delivery",
            "source": "maintenance_warehouse_v1",
            "source_document_type": "shipment",
            "source_status": "confirmed",
            "formal_available": False,
            "project_id": "project-1",
            "part_id": 1,
            "quantity": "3",
            "balance_key": "project-1:1",
        }
    ]

    preview = _preview(payload)

    assert preview["inventory"][0]["closing_quantity"] == "10"
    assert preview["can_approve"] is False
    assert "inventory_movement_date_overlap" in {
        row["code"] for row in preview["approval_blockers"]
    }


def test_stable_historical_issue_mode_requires_identity_and_pre_cutover_date():
    payload = _project(
        historical_mode="stable_site_issues",
        historical_baseline=None,
        historical_site_issues=[
            {
                "issue_line_id": "historical-1",
                "issue_date": "2026-08-01",
                "workflow_status": "confirmed",
                "cost_amount_ex_tax": "50.00",
                "cost_amount_inc_tax": "56.50",
                "stable_identity": False,
                **_manual_cost_evidence(),
            }
        ],
    )

    preview = _preview(payload)

    codes = {row["code"] for row in preview["approval_blockers"]}
    assert {
        "historical_issue_missing_identity",
        "historical_issue_date_overlap",
    } <= codes
    assert preview["cost"]["historical_baseline_ex_tax"] == "0.00"


def test_stable_historical_mode_with_only_void_rows_requires_a_baseline():
    payload = _project(
        historical_mode="stable_site_issues",
        historical_baseline=None,
        historical_site_issues=[
            {
                "issue_line_id": "historical-void-1",
                "issue_date": "2026-07-31",
                "workflow_status": "void",
                "cost_amount_ex_tax": "50.00",
                "cost_amount_inc_tax": "56.50",
                "stable_identity": True,
                **_manual_cost_evidence(),
            }
        ],
    )

    preview = _preview(payload)

    assert preview["cost"]["historical_baseline_ex_tax"] == "0.00"
    assert "missing_historical_site_issues" in {
        row["code"] for row in preview["approval_blockers"]
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("movement_id", "invented-id", "document_id:line_id"),
        ("project_id", "another-project", "项目或配件"),
        ("part_id", 2, "项目或配件"),
        ("balance_key", "project-1:2", "项目或配件"),
        ("source", "legacy_warehouse", "来源契约"),
        ("source_status", "pending", "状态或库存变动映射"),
        ("source_document_type", "receipt", "状态或库存变动映射"),
    ],
)
def test_inventory_movement_identity_and_source_mapping_fail_closed(
    field, value, message
):
    payload = _project()
    payload["inventory_movements"][0][field] = value

    with pytest.raises(controls.MigrationControlError, match=message):
        _preview(payload)


def test_receipt_must_be_explicitly_formal_available():
    payload = _project()
    payload["inventory_movements"][1]["formal_available"] = False

    with pytest.raises(controls.MigrationControlError, match="正式可用标记"):
        _preview(payload)


def test_opening_part_id_must_be_numeric_and_match_the_stable_key():
    payload = _project()
    payload["opening_balances"][0]["part_id"] = "not-an-id"

    with pytest.raises(controls.MigrationControlError, match="part_id 无效"):
        _preview(payload)


def test_snapshot_fingerprint_is_deterministic_and_changes_with_any_input():
    first = controls.build_migration_preview(
        rule_version=controls.RULE_VERSION,
        projects=[_project()],
    )
    second = controls.build_migration_preview(
        rule_version=controls.RULE_VERSION,
        projects=[_project()],
    )
    changed_payload = _project()
    changed_payload["opening_balances"][0]["quantity"] = "11"
    changed = controls.build_migration_preview(
        rule_version=controls.RULE_VERSION,
        projects=[changed_payload],
    )

    assert first == second
    assert len(first["input_fingerprint"]) == 64
    assert first["input_fingerprint"] != changed["input_fingerprint"]


def test_wrong_rule_version_and_duplicate_projects_fail_closed():
    with pytest.raises(controls.MigrationControlError, match="规则版本"):
        controls.build_migration_preview(rule_version="stale", projects=[_project()])
    with pytest.raises(controls.MigrationControlError, match="项目重复"):
        controls.build_migration_preview(
            rule_version=controls.RULE_VERSION,
            projects=[_project(), _project()],
        )


def test_decimal_input_must_be_finite_non_negative_and_bounded():
    for bad in ("NaN", "Infinity", "-1", "1000000000000"):
        payload = _project()
        payload["historical_baseline"]["amount_ex_tax"] = bad
        preview = _preview(payload)
        assert "historical_baseline_contract_invalid" in {
            row["code"] for row in preview["approval_blockers"]
        }


def test_aggregate_money_overflow_is_rejected_before_persistence():
    payload = _project()
    payload["historical_baseline"] = _historical_baseline(
        amount_ex_tax="600000000000.00",
        amount_inc_tax="678000000000.00",
    )
    payload["post_cutover_site_issues"][0].update(
        {
            "quantity": "1",
            "manual_unit_cost": "600000000000.00",
            "manual_unit_cost_inc_tax": "678000000000.00",
            "unit_cost_ex_tax": "600000000000.00",
            "unit_cost_inc_tax": "678000000000.00",
            "cost_tax_basis": "ex",
            "cost_amount_ex_tax": "600000000000.00",
            "cost_amount_inc_tax": "678000000000.00",
        }
    )

    with pytest.raises(controls.MigrationControlError, match="汇总超出"):
        _preview(payload)


def test_approval_gate_requires_no_blockers_and_exact_current_fingerprint():
    preview = controls.build_migration_preview(
        rule_version=controls.RULE_VERSION,
        projects=[_project()],
    )
    assert (
        controls.validate_approval(
            preview,
            supplied_fingerprint=preview["input_fingerprint"],
            current_fingerprint=preview["input_fingerprint"],
        )
        is None
    )

    with pytest.raises(controls.MigrationControlError, match="输入已经变化"):
        controls.validate_approval(
            preview,
            supplied_fingerprint=preview["input_fingerprint"],
            current_fingerprint="d" * 64,
        )

    blocked = controls.build_migration_preview(
        rule_version=controls.RULE_VERSION,
        projects=[_project(historical_baseline=None)],
    )
    with pytest.raises(controls.MigrationControlError, match="仍有未解决差异"):
        controls.validate_approval(
            blocked,
            supplied_fingerprint=blocked["input_fingerprint"],
            current_fingerprint=blocked["input_fingerprint"],
        )


def test_money_rendering_is_exact_decimal_not_float():
    payload = _project()
    payload["historical_baseline"]["amount_ex_tax"] = Decimal("0.10")
    payload["historical_baseline"]["amount_inc_tax"] = Decimal("0.11")
    payload["historical_baseline"]["aggregation_fingerprint"] = (
        controls.historical_baseline_aggregation_fingerprint(
            payload["historical_baseline"]
        )
    )
    payload["post_cutover_site_issues"][0].update(
        {
            "manual_unit_cost": Decimal("0.10"),
            "manual_unit_cost_inc_tax": Decimal("0.11"),
            "unit_cost_ex_tax": Decimal("0.10"),
            "unit_cost_inc_tax": Decimal("0.11"),
            "cost_amount_ex_tax": Decimal("0.20"),
            "cost_amount_inc_tax": Decimal("0.22"),
        }
    )
    payload["approved_expenses"][0].update(
        {
            "amount_ex_tax": Decimal("0.30"),
            "amount_inc_tax": Decimal("0.34"),
        }
    )

    preview = _preview(payload)

    assert preview["cost"]["total_ex_tax"] == "0.60"


def test_preview_evidence_is_whitelisted_and_deterministically_ordered():
    payload = _project()
    payload["post_cutover_site_issues"][0].update(
        {
            "issue_no": "LY-001",
            "pn": "PN-001",
            "quantity": "2",
            "customer_secret": "must-not-leak",
        }
    )

    preview = _preview(payload)

    row = preview["evidence"]["post_cutover_site_issues"][0]
    assert row["issue_no"] == "LY-001"
    assert row["pn"] == "PN-001"
    assert "customer_secret" not in row
