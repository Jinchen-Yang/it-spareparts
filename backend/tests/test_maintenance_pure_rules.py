"""不触碰数据库的排序和回款状态规则回归。"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.services.maintenance_boss_board import sort_project_ids_by_cost_ratio
from app.services.maintenance_collection_reminders import derive_collection_payment_state


def test_cost_ratio_sort_puts_unknown_last_and_is_stable():
    bundles = {
        "p1": {"state": "ready", "value": {"known_amount": Decimal("80")}},
        "p2": {"state": "ready", "value": {"known_amount": Decimal("100")}},
        "p3": {"state": "ready", "value": {"known_amount": Decimal("10")}},
        "p4": {"state": "unknown", "value": {"known_amount": None}},
    }
    contracts = {key: {"amount_inc_tax": Decimal("100")} for key in bundles}
    assert sort_project_ids_by_cost_ratio(
        ["p1", "p2", "p3", "p4"], cost_bundles=bundles, contracts=contracts
    ) == ["p2", "p1", "p3", "p4"]


def test_collection_payment_state_distinguishes_not_reported_partial_and_paid():
    milestone = SimpleNamespace(
        planned_date=date(2026, 8, 1), planned_amount=Decimal("40"),
        completeness_state="complete", date_precision="day",
    )
    snapshot = SimpleNamespace(cumulative_amount=Decimal("80"))
    assert derive_collection_payment_state(
        milestone,
        cumulative_planned=Decimal("100"),
        previous_cumulative_planned=Decimal("60"),
        latest_confirmed_snapshot=snapshot,
        as_of=date(2026, 8, 18),
    ) == "partial"
    assert derive_collection_payment_state(
        milestone,
        cumulative_planned=Decimal("100"),
        previous_cumulative_planned=Decimal("60"),
        latest_confirmed_snapshot=None,
        as_of=date(2026, 8, 18),
    ) == "not_reported"
    assert derive_collection_payment_state(
        milestone,
        cumulative_planned=Decimal("100"),
        previous_cumulative_planned=Decimal("60"),
        latest_confirmed_snapshot=SimpleNamespace(cumulative_amount=Decimal("100")),
        as_of=date(2026, 8, 18),
    ) == "paid"
