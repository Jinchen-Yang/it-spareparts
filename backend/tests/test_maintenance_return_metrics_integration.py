"""Actual return facts remain independent of demand amounts and historical PN text."""
from datetime import UTC, date, datetime
from decimal import Decimal

from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.services.maintenance_analytics import pn_ranking
from app.services.maintenance_boss_board import _card_receipt_rates
from tests.test_maintenance_analytics import _line, _part, _project
from tests.test_maintenance_return_metrics import _issue, _receipt
from tests.boss_board_helpers import client_for


def test_same_window_actual_issue_denominator_in_both_views(db):
    project = _project(db, "SAME-WINDOW")
    part = _part(db, "SAME-WINDOW-PN")
    _line(db, project, part, order_no="SYN-WINDOW-DEMAND", order_date=date(2026, 1, 1), qty=100)
    _issue(db, project, part, 10, issue_date=date(2026, 9, 2), no_return=True)
    _issue(db, project, part, 90, issue_date=date(2026, 8, 31))
    _receipt(db, project, part, 4, occurred_at=datetime(2026, 9, 2, tzinfo=UTC))
    _receipt(db, project, part, 50, occurred_at=datetime(2026, 10, 1, tzinfo=UTC))
    scope = dict(date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    result = pn_ranking(db, range_="custom", project_ids=[project.project_id], can_cost=True, **scope)
    row = result["rows"][0]
    assert row["qty"] == 0  # The demand occurred outside the selected period.
    assert row["issued_qty"] == 10
    assert row["receipt_qty"] == 4
    assert row["receipt_return_rate_pct"] == 40
    assert row["bad_return_rate_pct"] == 40
    card = _card_receipt_rates(db, [project.project_id], **scope)[project.project_id]
    assert card["rate_pct"] == 40
    assert card["issued_qty"] == "10.000"


def test_canonical_pn_search_retains_existing_demand_and_cost(db):
    project = _project(db, "RENAMED")
    part = _part(db, "OLD-IDENTITY-PN")
    _line(db, project, part, order_no="SYN-OLD", order_date=date(2026, 9, 1),
          qty=100, cost_inc=Decimal("1000"))
    part.pn_std = "NEW-IDENTITY-PN"
    db.flush()
    _issue(db, project, part, 10)
    _receipt(db, project, part, 4)
    result = pn_ranking(db, range_="all", q="NEW-IDENTITY", can_cost=True)
    assert result["total"] == 1
    row = result["rows"][0]
    assert row["part_id"] == part.id
    assert row["qty"] == 100
    assert row["cost_inc"]["value"] == "1000.00"
    assert row["receipt_return_rate_pct"] == 40


def test_cost_filter_cannot_reintroduce_nonmatching_pn_as_facts_only(db):
    project = _project(db, "FILTER-PN")
    wanted = _part(db, "FILTER-WANTED")
    other = _part(db, "FILTER-OTHER")
    demand = _line(db, project, wanted, order_no="SYN-MIXED", order_date=date(2026, 9, 1),
                   qty=100, cost_inc=Decimal("1000"))
    demand.cost_source = "manual"
    order = db.get(FMaintenanceOrder, demand.order_id)
    db.add(FMaintenanceLine(raw_line_id="syn-other-line", order_id=order.id, line_no=2,
                           part_id=other.id, pn_std=other.pn_std, pn_raw=other.pn_std,
                           qty=100, is_active=True, cost_source="direct", cost_tax_basis="inc",
                           cost_amount=2000, cost_amount_inc_tax=2000, cost_amount_ex_tax=1770,
                           import_batch_id=order.import_batch_id))
    db.flush()
    for part in (wanted, other):
        _issue(db, project, part, 10, source_order_id=order.raw_order_id)
        _receipt(db, project, part, 4, source_order_id=order.raw_order_id)
    result = pn_ranking(db, range_="all", cost_sources=["manual"], can_cost=True)
    assert result["total"] == 1
    assert result["rows"][0]["part_id"] == wanted.id
    assert result["rows"][0]["receipt_return_rate_pct"] == 40
    assert result["rows"][0]["cost_inc"]["value"] == "1000.00"


def test_all_condition_metrics_sort_before_pagination_and_keep_missing_basis_last(db):
    project = _project(db, "SORT-ACTUAL")
    rows = [
        ("RATE-150", 2, 3, "成品"),
        ("RATE-50", 8, 4, "废品"),
        ("RATE-ZERO", 9, 0, None),
        ("RATE-UNKNOWN", 0, 100, "坏品"),
    ]
    for pn, issued, returned, condition in rows:
        part = _part(db, pn)
        if issued:
            _issue(db, project, part, issued)
        if returned:
            _receipt(db, project, part, returned, condition=condition)
    scope = dict(range_="all", project_ids=[project.project_id], can_cost=False)
    result = pn_ranking(db, sort="receipt_rate", page_size=2, **scope)
    assert [row["pn"] for row in result["rows"]] == ["RATE-150", "RATE-50"]
    assert [row["receipt_return_rate_pct"] for row in result["rows"]] == [150, 50]
    assert result["summary"]["total_receipt_qty"] == "107.000"
    assert result["summary"]["total_issued_qty"] == "19.000"
    second = pn_ranking(db, sort="receipt_rate", page=2, page_size=2, **scope)
    assert [row["receipt_return_rate_pct"] for row in second["rows"]] == [0, None]
    assert pn_ranking(db, sort="receipt_qty", **scope)["rows"][0]["pn"] == "RATE-UNKNOWN"
    assert pn_ranking(db, sort="issued_qty", **scope)["rows"][0]["pn"] == "RATE-ZERO"
    # Old API sort remains a bad-condition breakdown; UI migrates old URL keys.
    assert pn_ranking(db, sort="bad_qty", **scope)["rows"][0]["pn"] == "RATE-UNKNOWN"


def test_api_accepts_actual_metric_sorts_without_cost_permission(db):
    project = _project(db, "API-ACTUAL")
    part = _part(db, "API-ACTUAL-PN")
    _issue(db, project, part, 10)
    _receipt(db, project, part, 4, condition="成品")
    db.commit()
    client = client_for(db, username="actual-metric-reader", overrides={
        "page_maintenance": True, "data_purchase_cost": False,
    })
    for sort in ("issued_qty", "receipt_qty", "receipt_rate"):
        response = client.get("/api/maintenance/analytics/pn-ranking", params={
            "range": "custom", "date_from": "2026-09-01", "date_to": "2026-09-30",
            "project": project.project_id, "sort": sort,
        })
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        row = response.json()["rows"][0]
        assert Decimal(row["issued_qty"]) == 10
        assert Decimal(row["receipt_qty"]) == 4
        assert row["receipt_return_rate_pct"] == 40
        assert row["cost_inc"]["state"] == "restricted"
