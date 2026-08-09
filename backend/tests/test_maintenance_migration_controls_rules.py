from decimal import Decimal

import pytest

from app.services import maintenance_migration_controls as controls


def _project(**overrides):
    payload = {
        "project_id": "project-1",
        "cutover_date": "2026-08-01",
        "historical_mode": "approved_cost_baseline",
        "historical_baseline": {
            "amount_ex_tax": "100.00",
            "amount_inc_tax": "113.00",
            "evidence_hash": "a" * 64,
            "approved": True,
        },
        "post_cutover_site_issues": [
            {
                "issue_line_id": "issue-line-1",
                "issue_date": "2026-08-02",
                "workflow_status": "confirmed",
                "cost_amount_ex_tax": "20.00",
                "cost_amount_inc_tax": "22.60",
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
        "opening_balances": [
            {
                "balance_key": "warehouse-1:part-1",
                "quantity": "10",
                "evidence_hash": "b" * 64,
                "approved": True,
            }
        ],
        "inventory_movements": [
            {
                "movement_id": "delivery-1",
                "movement_type": "delivery",
                "quantity": "3",
                "balance_key": "warehouse-1:part-1",
            },
            {
                "movement_id": "receipt-1",
                "movement_type": "available_receipt",
                "quantity": "2",
                "balance_key": "warehouse-1:part-1",
            },
            {
                "movement_id": "site-issue-1",
                "movement_type": "site_issue",
                "quantity": "2",
                "balance_key": "warehouse-1:part-1",
            },
            {
                "movement_id": "return-registration-1",
                "movement_type": "return_registration",
                "quantity": "1",
                "balance_key": "warehouse-1:part-1",
            },
        ],
        "return_offsets": [{"return_id": "return-1", "amount_ex_tax": "999.00"}],
        "source_snapshot_hash": "c" * 64,
    }
    payload.update(overrides)
    return payload


def test_preview_uses_only_baseline_post_cutover_consumption_and_approved_expenses():
    preview = controls.build_project_preview(_project())

    assert preview["cost"] == {
        "historical_baseline_ex_tax": "100.00",
        "historical_baseline_inc_tax": "113.00",
        "post_cutover_consumption_ex_tax": "20.00",
        "post_cutover_consumption_inc_tax": "22.60",
        "approved_expense_ex_tax": "10.00",
        "approved_expense_inc_tax": "11.30",
        "total_ex_tax": "130.00",
        "total_inc_tax": "146.90",
    }
    assert preview["ignored_return_offset_count"] == 1
    assert preview["approval_blockers"] == []


def test_preview_never_treats_demand_or_return_registration_as_consumption_or_inventory():
    payload = _project()
    payload["maintenance_demands"] = [
        {"demand_line_id": "wbdd-1", "quantity": "999", "amount": "99999"}
    ]
    preview = controls.build_project_preview(payload)

    assert "maintenance_demands" not in preview
    assert preview["cost"]["post_cutover_consumption_ex_tax"] == "20.00"
    assert preview["inventory"][0]["closing_quantity"] == "9"
    assert preview["inventory"][0]["ignored_site_issue_quantity"] == "2"
    assert preview["inventory"][0]["ignored_return_registration_quantity"] == "1"


@pytest.mark.parametrize("status", ["draft", "void", "unknown"])
def test_only_confirmed_or_corrected_site_issues_enter_cost(status):
    payload = _project()
    payload["post_cutover_site_issues"][0]["workflow_status"] = status

    preview = controls.build_project_preview(payload)

    assert preview["cost"]["post_cutover_consumption_ex_tax"] == "0.00"
    codes = {row["code"] for row in preview["approval_blockers"]}
    if status == "void":
        assert "unapproved_site_issue" not in codes
    else:
        assert "unapproved_site_issue" in codes


@pytest.mark.parametrize("status", ["rejected", "void"])
def test_explicitly_excluded_expenses_do_not_block_cutover(status):
    payload = _project()
    payload["approved_expenses"][0]["normalized_status"] = status

    preview = controls.build_project_preview(payload)

    assert preview["cost"]["approved_expense_ex_tax"] == "0.00"
    assert "expense_not_approved" not in {
        row["code"] for row in preview["approval_blockers"]
    }


def test_missing_site_issue_cost_is_not_silently_coerced_to_zero():
    payload = _project()
    payload["post_cutover_site_issues"][0]["cost_amount_ex_tax"] = None

    preview = controls.build_project_preview(payload)

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
            }
        ],
    )

    with pytest.raises(controls.MigrationControlError, match="不能同时"):
        controls.build_project_preview(payload)


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
            }
        ],
    )

    preview = controls.build_project_preview(payload)

    codes = {row["code"] for row in preview["approval_blockers"]}
    assert {
        "historical_issue_missing_identity",
        "historical_issue_date_overlap",
    } <= codes
    assert preview["cost"]["historical_baseline_ex_tax"] == "0.00"


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
        with pytest.raises(controls.MigrationControlError):
            controls.build_project_preview(payload)


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
    payload["post_cutover_site_issues"][0]["cost_amount_ex_tax"] = Decimal("0.20")
    payload["approved_expenses"][0]["amount_ex_tax"] = Decimal("0.30")

    preview = controls.build_project_preview(payload)

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

    preview = controls.build_project_preview(payload)

    row = preview["evidence"]["post_cutover_site_issues"][0]
    assert row["issue_no"] == "LY-001"
    assert row["pn"] == "PN-001"
    assert "customer_secret" not in row
