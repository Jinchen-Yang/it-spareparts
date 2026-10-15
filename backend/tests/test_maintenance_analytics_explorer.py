"""Cross-view facts, attribution, precision and permissions for the explorer."""
from datetime import date, datetime, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import event

from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder, MaintenanceManualCostOverride
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_analytics as legacy
from app.services import maintenance_analytics_explorer as explorer
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _line, _part, _project
from tests.test_maintenance_analytics_filters import _assign, _set_order
from tests.test_maintenance_return_metrics import _issue, _receipt

URL = "/api/maintenance/analytics/explorer"


def explore(db, **kwargs):
    return explorer.explorer(db, **{"range_": "all", "can_cost": True, **kwargs})


def demand(db, project, part, qty="1", cost=None, **kwargs):
    return _line(db, project, part, order_no=f"WBDD-{uuid.uuid4().hex}",
                 order_date=kwargs.pop("order_date", date(2026, 9, 10)), qty=Decimal(qty),
                 cost_inc=Decimal(cost) if cost is not None else None, **kwargs)


def test_pn_overview_independent_full_rankings_share_one_snapshot(db):
    project = _project(db, "EX-OVERVIEW")
    costly, popular = _part(db, "EX-COSTLY"), _part(db, "EX-POPULAR")
    demand(db, project, costly, "1", "123456789012.34")
    demand(db, project, popular, "9.001", "0")
    db.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        result = explore(db, metric="effective", include_pn_overview=True,
                         top_n=1, page_size=1, page=2)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert len(statements) == 1
    overview = result["pn_overview"]
    assert overview["cost_rows"][0]["key"] == f"part:{costly.id}"
    assert overview["cost_rows"][0]["value"] == "123456789012.34"
    assert overview["effective_rows"][0]["key"] == f"part:{popular.id}"
    assert overview["effective_rows"][0]["value"] == "9.001"
    assert result["rows"][0]["part_id"] == costly.id
    assert result["summary"]["effective_qty"] == "10.001"
    other_page = explore(db, metric="issued", include_pn_overview=True,
                         top_n=1, page_size=100, focus="part:missing")
    assert other_page["pn_overview"] == overview
    assert "pn_overview" not in explore(db)


def test_pn_overview_restricted_cost_cannot_change_quantity_ranking(db):
    project = _project(db, "EX-OVERVIEW-PERMS")
    a, b = _part(db, "EX-OVERVIEW-A"), _part(db, "EX-OVERVIEW-B")
    first = demand(db, project, a, "1", "1")
    second = demand(db, project, b, "1", "999")
    _set_order(db, first, end_customer="OVERVIEW-SECRET")
    db.commit()
    result = explore(db, metric="effective", include_pn_overview=True,
                     can_cost=False, can_customer=False)
    overview = result["pn_overview"]
    assert overview["cost_visibility"] == "restricted"
    assert overview["cost_rows"] is None
    assert [r["key"] for r in overview["effective_rows"]] == [f"part:{a.id}", f"part:{b.id}"]
    assert all(r["cost_inc"] is None and r["cost_state"] == "restricted" for r in overview["effective_rows"])
    assert "OVERVIEW-SECRET" not in str(result)
    first.cost_amount_inc_tax, second.cost_amount_inc_tax = second.cost_amount_inc_tax, first.cost_amount_inc_tax
    first.cost_amount, second.cost_amount = second.cost_amount, first.cost_amount
    db.commit()
    changed = explore(db, metric="effective", include_pn_overview=True,
                      can_cost=False, can_customer=False)
    assert changed["pn_overview"] == overview


@pytest.mark.parametrize("return_qty", ["0", "3"])
def test_pn_overview_null_demand_qty_is_unknown_not_zero_or_negative(db, return_qty):
    project = _project(db, "EX-NULL-QTY")
    unknown, mixed, zero, negative = [_part(db, f"EX-NULL-QTY-{name}")
                                      for name in ("UNKNOWN", "MIXED", "ZERO", "NEGATIVE")]
    for part in (unknown, mixed):
        line = demand(db, project, part, "1", "10", return_qty=Decimal(return_qty))
        line.qty = None
    demand(db, project, mixed, "5", "20")
    demand(db, project, zero, "0", "0")
    demand(db, project, negative, "1", "1", return_qty=Decimal("2"))
    # Other fact streams must not turn an unknown demand quantity into zero.
    _issue(db, project, unknown, "2")
    _receipt(db, project, unknown, "1")
    db.commit()

    result = explore(db, metric="effective", include_pn_overview=True)
    overview = result["pn_overview"]
    rows = {row["key"]: row for row in overview["effective_rows"]}
    for part in (unknown, mixed):
        assert rows[f"part:{part.id}"]["value"] is None
        assert rows[f"part:{part.id}"]["effective_qty"] is None
    assert [row["key"] for row in overview["effective_rows"][:2]] == [f"part:{zero.id}", f"part:{negative.id}"]
    assert Decimal(rows[f"part:{zero.id}"]["value"]) == 0
    assert rows[f"part:{negative.id}"]["value"] == "-1.000"
    assert result["summary"]["effective_qty"] is None
    assert result["meta"]["unknown_count"] == 2
    assert overview["has_negative"] is True
    # A quantity gap must not erase separately known cost evidence.
    costs = {row["key"]: row for row in overview["cost_rows"]}
    assert costs[f"part:{unknown.id}"]["value"] == "10.00"
    assert costs[f"part:{mixed.id}"]["value"] == "30.00"


def test_pn_overview_unknown_quantity_does_not_create_negative_or_shares(db):
    project = _project(db, "EX-UNKNOWN-QTY-SHARES")
    unknown, known = _part(db, "EX-UNKNOWN-QTY"), _part(db, "EX-KNOWN-QTY")
    line = demand(db, project, unknown, return_qty=Decimal("3"))
    line.qty = None
    demand(db, project, known, "5")
    db.commit()

    result = explore(db, metric="effective", include_pn_overview=True, can_cost=False)
    rows = result["pn_overview"]["effective_rows"]
    assert [(row["key"], row["value"]) for row in rows] == [(f"part:{known.id}", "5.000"), (f"part:{unknown.id}", None)]
    assert result["pn_overview"]["has_negative"] is False
    assert result["meta"]["has_negative"] is False
    assert result["meta"]["unknown_count"] == 1
    assert result["summary"]["effective_qty"] is None
    assert result["summary"]["top_share_pct"] is None
    assert all(row["share_pct"] is None and row["cumulative_share_pct"] is None for row in rows)


@pytest.mark.parametrize("status", ["已取消", "草稿", "进行中", "", None])
def test_pn_overview_excludes_demands_from_non_effective_source_heads(db, status):
    project = _project(db, "EX-HEAD-STATUS")
    valid, excluded = _part(db, "EX-VALID-HEAD"), _part(db, "EX-NON-EFFECTIVE-HEAD")
    demand(db, project, valid, "2", "6")
    line = demand(db, project, excluded, "100", "1000")
    _set_order(db, line, data_status=status)
    db.commit()

    result = explore(db, metric="effective", include_pn_overview=True)
    for ranking in ("cost_rows", "effective_rows"):
        assert [row["key"] for row in result["pn_overview"][ranking]] == [f"part:{valid.id}"]
    assert result["summary"]["effective_qty"] == "2.000"
    assert result["summary"]["cost_inc"] == "6.00"
    assert result["summary"]["order_count"] == 1


def test_pn_overview_cancelled_source_counts_again_when_current_status_is_effective(db):
    project = _project(db, "EX-REVERSIBLE-CANCEL")
    part = _part(db, "EX-RESTORED-HEAD")
    line = demand(db, project, part, "4", "40")
    order = _set_order(db, line, data_status="已取消")
    db.commit()
    cancelled = explore(db, metric="effective", include_pn_overview=True)
    assert cancelled["pn_overview"]["cost_rows"] == []
    assert cancelled["pn_overview"]["effective_rows"] == []

    order.data_status = "已生效"
    db.commit()
    restored = explore(db, metric="effective", include_pn_overview=True)
    for ranking in ("cost_rows", "effective_rows"):
        assert [row["key"] for row in restored["pn_overview"][ranking]] == [f"part:{part.id}"]
    assert restored["summary"]["effective_qty"] == "4.000"
    assert restored["summary"]["cost_inc"] == "40.00"
    assert restored["summary"]["order_count"] == 1


def test_pn_overview_api_opt_in_validation_and_scope(db):
    mine, hidden = _project(db, "EX-OV-API-MINE"), _project(db, "EX-OV-API-HIDDEN")
    mine_pn, hidden_pn = _part(db, "EX-OV-API-PN"), _part(db, "EX-HIDDEN-PN")
    demand(db, mine, mine_pn, "0.001", "0")
    demand(db, hidden, hidden_pn, "999", "999")
    db.commit()
    client = client_for(db, username="overview-scoped", overrides={
        "page_maintenance": True, "data_purchase_cost": False, "data_customer": False,
        "own_maintenance_projects_only": True,
    })
    params = {"range": "all", "metric": "effective", "include_pn_overview": True}
    empty = client.get(URL, params=params)
    assert empty.status_code == 200, empty.text
    assert empty.json()["pn_overview"]["effective_rows"] == []
    _assign(db, mine, "overview-scoped")
    response = client.get(URL, params={**params, "project": f"{mine.project_id},{hidden.project_id}"})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    overview = response.json()["pn_overview"]
    assert overview["cost_rows"] is None
    assert [r["key"] for r in overview["effective_rows"]] == [f"part:{mine_pn.id}"]
    assert overview["effective_rows"][0]["can_open_project"] is False
    assert "EX-HIDDEN-PN" not in response.text
    default = client.get(URL, params={"range": "all", "metric": "effective"}).json()
    disabled = client.get(URL, params={**params, "include_pn_overview": False}).json()
    assert "pn_overview" not in default
    for payload in (default, disabled):
        payload["meta"].pop("as_of")
    assert disabled == default
    for extra in ({"dimension": "project"}, {"dimension": "customer"}, {"metric": "cost"},
                  {"customer": "secret"}, {"top_n": 0}, {"top_n": 31}, {"include_pn_overview": "invalid"}):
        assert client.get(URL, params={**params, **extra}).status_code == 422
    cost_project = _project(db, "EX-OV-API-COST")
    cost_pn = _part(db, "EX-OV-API-COST-PN")
    demand(db, cost_project, cost_pn, "0.001", "0")
    db.commit()
    cost_client = client_for(db, username="overview-cost-scoped", overrides={
        "page_maintenance": True, "data_purchase_cost": True,
        "own_maintenance_projects_only": True,
    })
    _assign(db, cost_project, "overview-cost-scoped")
    visible = cost_client.get(URL, params=params)
    assert visible.status_code == 200, visible.text
    cost_rows = visible.json()["pn_overview"]["cost_rows"]
    assert len(cost_rows) == 1 and cost_rows[0]["key"] == f"part:{cost_pn.id}"
    assert isinstance(cost_rows[0]["value"], str)
    assert Decimal(cost_rows[0]["value"]) == 0
    assert cost_rows[0]["can_open_project"] is False
    assert "EX-HIDDEN-PN" not in visible.text


def test_one_statement_keeps_independent_facts_and_chart_totals(db):
    project = _project(db, "EX-UNION")
    part = _part(db, "EX-UNION-PN")
    demand(db, project, part, "2", "12.00")
    demand(db, project, part, "3", "18.00")
    for qty in ("2", "4"):
        _issue(db, project, part, qty)
    for qty in ("1", "2", "3"):
        _receipt(db, project, part, qty)
    db.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        result = explore(db)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["key"] == f"part:{part.id}"
    assert Decimal(row["issued_qty"]) == 6
    assert Decimal(row["effective_qty"]) == 5
    assert Decimal(row["cost_inc"]) == 30
    assert Decimal(result["summary"]["receipt_qty"]) == 6
    assert row["order_count"] == 2
    assert result["focus"]["rows"][0]["project_id"] == project.project_id
    assert Decimal(result["matrix"]["cells"][0]["value"]) == 6


@pytest.mark.parametrize("overview", [False, True])
def test_decimal_cost_missing_zero_partial_and_negative_net_are_distinct(db, overview):
    project = _project(db, "EX-DECIMAL")
    parts = [_part(db, f"EX-DECIMAL-{n}") for n in range(4)]
    manual = demand(db, project, parts[0], "0.5")
    db.add(MaintenanceManualCostOverride(line_id=manual.id, unit_cost_ex_tax=Decimal("0.01"),
                                       unit_cost_inc_tax=Decimal("0.01"), active=True, updated_by="test"))
    demand(db, project, parts[0], "1")
    demand(db, project, parts[1], "1", "0")
    demand(db, project, parts[2], "1")
    demand(db, project, parts[3], "1", return_qty=Decimal("2"))
    _issue(db, project, parts[0], "0.001")
    db.commit()
    result = explore(db, metric="cost", include_pn_overview=overview)
    by_id = {row["part_id"]: row for row in result["rows"]}
    assert Decimal(by_id[parts[0].id]["cost_inc"]) == Decimal("0.005")
    assert by_id[parts[0].id]["cost_state"] == "partial"
    assert by_id[parts[0].id]["missing_lines"] == 1
    assert Decimal(by_id[parts[0].id]["issued_qty"]) == Decimal("0.001")
    assert by_id[parts[1].id]["cost_inc"] is not None
    assert Decimal(by_id[parts[1].id]["cost_inc"]) == 0
    assert by_id[parts[1].id]["cost_state"] == "known"
    assert by_id[parts[2].id]["cost_inc"] is None
    assert by_id[parts[2].id]["cost_state"] == "unknown"
    assert Decimal(result["summary"]["cost_inc"]) == Decimal("0.005")
    assert result["summary"]["top_share_pct"] == "100.0"
    if overview:
        assert result["pn_overview"]["cost_rows"] == [
            {**row, "can_open_project": False} for row in result["rows"]
        ]
        assert result["pn_overview"]["has_negative"] is True
        quantities = result["pn_overview"]["effective_rows"]
        assert all(row["share_pct"] is None for row in quantities)
        assert quantities[-1]["value"] == "-1.000"
    negative = explore(db, metric="effective")
    assert negative["meta"]["has_negative"] is True
    assert all(row["share_pct"] is None for row in negative["rows"])
    assert negative["rows"][-1]["value"] == "-1.000"


def test_distinct_order_counts_are_not_added_between_pns(db):
    project = _project(db, "EX-ORDERS")
    first, second = _part(db, "EX-ORD-A"), _part(db, "EX-ORD-B")
    line = demand(db, project, first)
    sibling = FMaintenanceLine(raw_line_id=f"rl-{uuid.uuid4()}", order_id=line.order_id, line_no=2,
                              part_id=second.id, pn_std=second.pn_std, pn_raw=second.pn_std,
                              qty=Decimal(1), return_qty=Decimal(0), is_active=True, import_batch_id=line.import_batch_id)
    db.add(sibling)
    db.commit()
    result = explore(db, metric="orders")
    assert result["summary"]["order_count"] == 1
    assert [row["order_count"] for row in result["rows"]] == [1, 1]
    assert result["meta"]["additive"] is False
    assert all(row["share_pct"] is None for row in result["rows"])
    projects = explore(db, dimension="project", metric="orders")
    assert projects["rows"][0]["order_count"] == 1


@pytest.mark.parametrize("overview", [False, True])
def test_identity_rename_and_ambiguous_raw_pn_do_not_merge(db, overview):
    project = _project(db, "EX-IDENTITY")
    first, second = _part(db, "EX-case"), _part(db, "EX-CASE")
    demand(db, project, first, "2", "20")
    demand(db, project, second, "3", "30")
    _issue(db, project, first, "1")
    _issue(db, project, second, "2")
    _receipt(db, project, None, "4", pn=" ex-case ")
    db.commit()
    result = explore(db, include_pn_overview=overview)
    if overview:
        assert {row["key"] for row in result["pn_overview"]["effective_rows"]} == {
            f"part:{first.id}", f"part:{second.id}"
        }
    assert {row["key"] for row in result["rows"]} == {f"part:{first.id}", f"part:{second.id}"}
    assert Decimal(result["summary"]["receipt_qty"]) == 4
    assert all(Decimal(row["receipt_qty"]) == 0 for row in result["rows"])
    first.pn_std = "EX-RENAMED"
    db.commit()
    renamed = explore(db, q="EX-RENAMED")
    assert len(renamed["rows"]) == 1
    assert renamed["rows"][0]["part_id"] == first.id
    assert Decimal(renamed["rows"][0]["effective_qty"]) == 2


def test_linked_filters_do_not_widen_or_apply_demand_dates_to_actual_facts(db):
    project = _project(db, "EX-SOURCE")
    first, other = _part(db, "EX-SOURCE-A"), _part(db, "EX-SOURCE-B")
    line = demand(db, project, first, "2", "20", order_date=date(2025, 1, 1))
    _set_order(db, line, end_customer="客户%100", salesperson="旧销售", warehouse=" 广州仓 ", demand_type="报修供货")
    source = db.get(FMaintenanceOrder, line.order_id).raw_order_id
    _issue(db, project, first, "3", source_order_id=source)
    _issue(db, project, other, "10", source_order_id=source)
    _issue(db, project, first, "100")
    _receipt(db, project, None, "2", pn=first.pn_std, source_order_id=source)
    project.salesperson = "现销售"
    project.salesperson_override_active = True
    db.commit()
    result = explore(db, range_="custom", date_from=date(2026, 9, 1), date_to=date(2026, 9, 30),
                     customer="%100", salesperson="现销售", warehouses=[" 广州仓 "],
                     demand_types=["报修供货"], cost_sources=["linked"])
    assert len(result["rows"]) == 1
    assert Decimal(result["summary"]["issued_qty"]) == 3
    assert Decimal(result["summary"]["effective_qty"]) == 0
    assert Decimal(result["summary"]["receipt_qty"]) == 2
    assert result["rows"][0]["part_id"] == first.id
    assert not explore(db, customer="X100")["rows"]


def test_unknown_customer_is_not_inferred_from_project_and_override_clear_wins(db):
    project = _project(db, "EX-CUSTOMER")
    part = _part(db, "EX-CUSTOMER-PN")
    line = demand(db, project, part)
    order = _set_order(db, line, end_customer="客户甲", salesperson="历史销售")
    _issue(db, project, part, "2", source_order_id=order.raw_order_id)
    _issue(db, project, part, "3")
    project.salesperson_override_active = True
    project.salesperson = None
    db.commit()
    result = explore(db, dimension="customer")
    assert {row["key"]: Decimal(row["value"]) for row in result["rows"]} == {"客户甲": Decimal(2), "__unknown__": Decimal(3)}
    sales = explore(db, dimension="salesperson")
    assert len(sales["rows"]) == 1
    assert sales["rows"][0]["key"] == "__unknown__"


def test_shanghai_bounds_and_null_dates_are_consistent(db):
    project = _project(db, "EX-DATE")
    part = _part(db, "EX-DATE-PN")
    _issue(db, project, part, "2", issue_date=date(2026, 9, 10))
    _receipt(db, project, part, "1", occurred_at=datetime(2026, 9, 9, 16, tzinfo=timezone.utc))
    _receipt(db, project, part, "2", occurred_at=datetime(2026, 9, 10, 15, 59, 59, tzinfo=timezone.utc))
    _receipt(db, project, part, "4", occurred_at=datetime(2026, 9, 10, 16, tzinfo=timezone.utc))
    _receipt(db, project, part, "8", occurred_at=None)
    db.commit()
    result = explore(db, range_="custom", date_from=date(2026, 9, 10), date_to=date(2026, 9, 10))
    assert Decimal(result["summary"]["receipt_qty"]) == 3
    assert Decimal(explore(db)["summary"]["receipt_qty"]) == 15
    with pytest.raises(legacy.AnalyticsValidationError):
        explore(db, range_="custom", date_from=date(2026, 9, 11), date_to=date(2026, 9, 10))


def test_topn_full_denominator_and_focus_pagination_do_not_truncate(db):
    part = _part(db, "EX-PAGES-PN")
    for n in range(5):
        project = _project(db, f"EX-PAGES-{n}")
        _issue(db, project, part, str(n + 1))
    second = _part(db, "EX-PAGES-OTHER")
    _issue(db, project, second, "5")
    db.commit()
    result = explore(db, top_n=1, page_size=1, page=2, focus_page=2, focus_page_size=2)
    assert len(result["rows"]) == 1 and result["rows"][0]["part_id"] == second.id
    assert result["chart_rows"][0]["part_id"] == part.id
    assert result["chart_rows"][0]["share_pct"] == "75.0"
    assert result["focus"]["total"] == 5
    assert len(result["focus"]["rows"]) == 2
    assert result["focus"]["page"] == 2
    stale = explore(db, focus="part:987654321", page=900)
    assert stale["focus"]["row"] is None
    assert stale["focus"]["rows"] == []
    assert stale["page"] == 1


def test_project_scope_intersection_and_unassigned_business_are_separate(db):
    mine, other = _project(db, "EX-MINE"), _project(db, "EX-OTHER")
    mine_part, other_part, unassigned_part = (_part(db, x) for x in ("EX-MINE-PN", "EX-OTHER-PN", "EX-UNASSIGNED-PN"))
    demand(db, mine, mine_part)
    demand(db, other, other_part)
    orphan = demand(db, other, unassigned_part)
    order = db.get(FMaintenanceOrder, orphan.order_id)
    assignment = db.query(MaintenanceSourceOrderAssignment).filter_by(source_order_id=order.raw_order_id).one()
    assignment.is_active = False
    assignment.version += 1
    assignment.archived_by = "test"
    assignment.archived_at = datetime.now(timezone.utc)
    _issue(db, mine, mine_part, "2")
    _issue(db, other, other_part, "4")
    db.commit()
    result = explore(db, allowed_project_ids={mine.project_id}, project_ids=[mine.project_id, other.project_id])
    assert result["total"] == 1
    assert "EX-OTHER" not in str(result)
    denied = explore(db, allowed_project_ids=set(), project_ids=[other.project_id])
    assert denied["rows"] == denied["matrix"]["cells"] == denied["focus"]["rows"] == []
    assert Decimal(denied["summary"]["issued_qty"]) == 0
    business = explore(db, metric="effective", dimension="business")
    assert {row["key"] for row in business["rows"]} == {"unassigned", "unlabeled"}


def test_cost_and_customer_permissions_cover_all_response_surfaces(db):
    project = _project(db, "EX-PERMS")
    part = _part(db, "EX-PERMS-PN")
    line = demand(db, project, part, "1", "123456.78")
    _set_order(db, line, end_customer="SECRET-CUSTOMER")
    _issue(db, project, part, "2")
    db.commit()
    result = explore(db, can_cost=False, can_customer=False)
    assert "123456.78" not in str(result)
    assert "SECRET-CUSTOMER" not in str(result)
    assert result["summary"]["cost_state"] == "restricted"
    assert result["rows"][0]["cost_inc"] is None
    for kwargs in ({"metric": "cost", "can_cost": False},
                   {"dimension": "customer", "can_customer": False},
                   {"customer": "SECRET", "can_customer": False}):
        with pytest.raises(legacy.AnalyticsValidationError):
            explore(db, **kwargs)


def test_api_guards_customer_cost_and_project_scope(db):
    project = _project(db, "EX-API")
    part = _part(db, "EX-API-PN")
    demand(db, project, part, "1", "123")
    _issue(db, project, part, "3")
    db.commit()
    client = client_for(db, username="explorer-scoped", overrides={
        "page_maintenance": True, "data_purchase_cost": False, "data_customer": False,
        "own_maintenance_projects_only": True,
    })
    empty = client.get(URL, params={"range": "all", "project": project.project_id})
    assert empty.status_code == 200, empty.text
    assert empty.json()["total"] == 0
    _assign(db, project, "explorer-scoped")
    response = client.get(URL, params={"range": "all"})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["total"] == 1
    for params in ({"metric": "cost"}, {"dimension": "customer"}, {"customer": "secret"}, {"top_n": 31}):
        assert client.get(URL, params=params).status_code == 422
    for path in ("pn-ranking", "spend-trend"):
        assert client.get(f"/api/maintenance/analytics/{path}", params={"customer": "secret", "sort": "qty"}).status_code == 422
    assert client.get("/api/maintenance/analytics/pn-ranking", params={"sort": "cost_share"}).status_code == 422


def test_legacy_cost_share_and_cost_tiebreak_are_closed(db):
    project = _project(db, "EX-LEGACY")
    cheap, expensive = _part(db, "EX-A-CHEAP"), _part(db, "EX-B-EXPENSIVE")
    first = demand(db, project, cheap, "1", "1")
    second = demand(db, project, expensive, "1", "900")
    db.commit()
    result = legacy.pn_ranking(db, range_="all", sort="effective_qty", can_cost=False)
    assert all(row["cost_share_pct"] is None for row in result["rows"])
    initial_order = [row["part_id"] for row in result["rows"]]
    first.cost_amount_inc_tax, second.cost_amount_inc_tax = second.cost_amount_inc_tax, first.cost_amount_inc_tax
    first.cost_amount, second.cost_amount = second.cost_amount, first.cost_amount
    db.commit()
    changed = legacy.pn_ranking(db, range_="all", sort="effective_qty", can_cost=False)
    assert [row["part_id"] for row in changed["rows"]] == initial_order
    for sort in ("cost_inc", "cost_ex", "cost_share"):
        with pytest.raises(legacy.AnalyticsValidationError):
            legacy.pn_ranking(db, range_="all", sort=sort, can_cost=False)


def test_transferred_source_does_not_leak_customer_or_match_filters(db):
    visible, hidden = _project(db, "EX-OLD-PROJECT"), _project(db, "EX-NEW-PROJECT")
    part = _part(db, "EX-TRANSFERRED-PN")
    line = demand(db, visible, part)
    order = _set_order(db, line, end_customer="TRANSFER-SECRET", salesperson="TRANSFER-SALES")
    _issue(db, visible, part, "7", source_order_id=order.raw_order_id)
    assignment = db.query(MaintenanceSourceOrderAssignment).filter_by(source_order_id=order.raw_order_id).one()
    assignment.is_active = False
    assignment.version += 1
    assignment.archived_by = "test"
    assignment.archived_at = datetime.now(timezone.utc)
    db.flush()
    db.add(MaintenanceSourceOrderAssignment(assignment_id=str(uuid.uuid4()), project_id=hidden.project_id,
                                          source_order_id=order.raw_order_id, is_active=True, created_by="test"))
    db.commit()
    result = explore(db, allowed_project_ids={visible.project_id}, dimension="customer")
    assert result["rows"][0]["key"] == "__unknown__"
    assert Decimal(result["summary"]["issued_qty"]) == 7
    assert "TRANSFER-SECRET" not in str(result)
    sales = explore(db, allowed_project_ids={visible.project_id}, dimension="salesperson")
    assert sales["rows"][0]["key"] == "__unknown__"
    assert "TRANSFER-SALES" not in str(sales)
    assert not explore(db, customer="TRANSFER-SECRET", allowed_project_ids={visible.project_id})["rows"]
    # Also reject cross-project source associations even for an all-scope user.
    assert not explore(db, customer="TRANSFER-SECRET")["rows"]


def test_cumulative_percent_is_computed_once_and_hidden_negative_remains_flagged(db):
    project = _project(db, "EX-CUMULATIVE")
    parts = [_part(db, f"EX-CUMULATIVE-{n}") for n in range(6)]
    for part in parts:
        demand(db, project, part, "1", "1")
    # Negative demand offset by positive quantity in the same SQL group.
    demand(db, project, parts[0], "1", return_qty=Decimal("2"))
    demand(db, project, parts[0], "2")
    db.commit()
    costs = explore(db, metric="cost")
    assert costs["rows"][-1]["cumulative_share_pct"] == "100.0"
    assert sum(Decimal(row["share_pct"]) for row in costs["rows"]) == Decimal("100.2")
    assert costs["summary"]["top_share_pct"] == "100.0"
    effective = explore(db, metric="effective")
    assert all(Decimal(row["value"]) > 0 for row in effective["rows"])
    assert effective["meta"]["has_negative"] is True
    assert all(row["share_pct"] is None and row["cumulative_share_pct"] is None for row in effective["rows"])
    assert all(row["share_pct"] is None for row in effective["focus"]["rows"])


def test_void_lines_orders_and_unconfirmed_issues_are_excluded(db):
    project = _project(db, "EX-VOID")
    part = _part(db, "EX-VOID-PN")
    demand(db, project, part, "2", "2")
    demand(db, project, part, "100", "100", active=False)
    deleted_order = demand(db, project, part, "100", "100")
    _set_order(db, deleted_order, data_status="已作废")
    _issue(db, project, part, "3")
    for status in ("draft", "unknown", "void"):
        _issue(db, project, part, "100", status=status)
    _issue(db, project, part, "100", active=False)
    _receipt(db, project, part, "100", status="voided")
    db.commit()
    result = explore(db)
    assert Decimal(result["summary"]["issued_qty"]) == 3
    assert Decimal(result["summary"]["effective_qty"]) == 2
    assert Decimal(result["summary"]["cost_inc"]) == 2
    assert Decimal(result["summary"]["receipt_qty"]) == 0
