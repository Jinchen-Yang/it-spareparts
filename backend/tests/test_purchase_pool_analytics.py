import json
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from app import config
from app.business_time import business_today
from app.models import DimSupplier
from app.services import pool_catalog
from tests.boss_board_helpers import client_for
from tests.test_pool_analysis_api import _dq_issue
from tests.test_purchase_analytics_api import _batch, _line, _part, _po

URL = "/api/purchases/analytics/pool"


@pytest.fixture
def inventory(db):
    batch, old = _batch(db, "current"), _batch(db, "stale")
    a, b, c = [_part(db, name, "内存") for name in ("MEM-A", "MEM-B", "MEM-Z")]
    for p in (a, b, c):
        p.unit = "件"
    a.description = "带%号"
    source = DimSupplier(name_raw="供应甲", name_normalized="供应甲")
    db.add(source)
    db.flush()
    gid = pool_catalog.create_pool(
        db, name="可比池", member_part_ids=[a.id, b.id, c.id], operated_by="test"
    )["group_id"]
    pool_catalog.set_price_policy(
        db,
        group_id=gid,
        version=1,
        purchase_value=D(150),
        purchase_basis="ex_tax",
        operated_by="test",
    )

    def order(name, when, inclusive=False):
        o = _po(db, name, batch, on=when, source_type="销售订单", is_inc=inclusive)
        o.supplier_id = source.id
        return o

    def line(name, o, p, qty, unit, price, run=batch):
        l = _line(db, name, o, p, run, line_no=1, amount=D(qty) * D(price), qty=qty, price=price)
        l.unit = unit
        return l

    o = order("O-1", date(2025, 3, 1), True)
    line("A-1", o, a, "2", "件", "113")
    line("A-2", o, a, "1", "件", "226")
    line("A-3", o, a, "10", "套", "11300")
    line("A-4", o, a, "1", None, "113000")
    line("A-old", o, a, "10", "件", "99999", run=old)
    b_order = order("O-2", date(2025, 3, 1))
    line("B-1", b_order, b, "1", "件", "200")
    error = line("B-error", b_order, b, "10", "件", "8000")
    _dq_issue(db, side="purchase", line=error, status="confirmed_source_error", rule_code="mock")
    line("A-future", order("future", business_today() + timedelta(days=1)), a, "3", "件", "99999")
    db.commit()
    return a.id, b.id, gid


def test_prices_are_weighted_within_unit_and_scope(db, inventory):
    a, b, gid = inventory
    client = client_for(db, username="pool-stats", role="admin")

    def get(path, **params):
        r = client.get(URL + path, params=params)
        assert r.status_code == 200, r.text
        return r.json()

    records = get("/parts")["items"]
    by = {(r["part_id"], r["unit"]): r for r in records}
    row = by[a, "件"]
    assert (row["lines"], row["orders"], row["quantity"]) == (3, 2, 6)
    assert row["purchase_metrics"]["price"] == pytest.approx(400 / 3)
    assert row["purchase_metrics"]["pool_price"] == 150
    assert by[a, None]["quantity"] is None
    assert by[a, None]["purchase_metrics"]["price"] is None
    assert by[a, "套"]["purchase_metrics"]["ceiling"] is None
    assert by[b, "件"]["quantity"] == 11
    assert by[b, "件"]["purchase_metrics"]["price_qty"] == 1
    for focus in ["above_cap", "above_pool"]:
        rank = get("/parts", focus=focus, q="MEM-B")["items"]
        assert len(rank) == 1 and rank[0]["part_id"] == b
        assert rank[0]["purchase_metrics"]["delta_pool"] == pytest.approx(1 / 3)
    assert all(r["part_id"] == a for r in get("/parts", q="%")["items"])
    assert get("/parts", page_size=1)["has_more"]
    assert (
        get("/parts", scope='{"pn":"MEM-B"}')["items"][0]["purchase_metrics"]["pool_price"] == 200
    )
    sources = get("/sources", part_id=a, unit="件")["items"]
    assert len(sources) == 1 and sources[0]["purchase_metrics"]["price_min"] == 100
    assert sources[0]["purchase_metrics"]["price_max"] == 200
    members = get("/members", pool_id=gid, unit="件")["items"]
    assert len(members) == 3
    absent = next(r for r in members if r["pn"] == "MEM-Z")
    assert absent["orders"] == 0 and absent["purchase_metrics"]["price"] is None


def test_omitted_evidence_unit_means_only_missing_unit_and_matches_drills(db, inventory):
    """The UI omits unit=null on the wire; absence must remain IS NULL, never all units."""
    a, b, gid = inventory
    client = client_for(db, username="pool-missing-unit", role="admin")

    def get(path, **params):
        response = client.get(URL + path, params=params)
        assert response.status_code == 200, response.text
        return response.json()["items"]

    selected = next(item for item in get("/parts") if item["part_id"] == a and item["unit"] is None)
    assert (selected["orders"], selected["lines"], selected["quantity"]) == (1, 1, None)
    sources = get("/sources", part_id=a)
    assert len(sources) == 1
    source = sources[0]
    assert (source["orders"], source["lines"], source["quantity"]) == (1, 1, None)
    assert source["purchase_metrics"]["price"] is None
    known = get("/sources", part_id=a, unit="件")[0]
    assert (known["orders"], known["lines"], known["quantity"]) == (2, 3, 6)
    assert get("/sources", part_id=a, scope='{"unit":null}') == sources

    members = get("/members", pool_id=gid)
    by_part = {member["part_id"]: member for member in members}
    assert by_part[a]["orders"] == 1 and by_part[a]["quantity"] is None
    assert by_part[b]["orders"] == 0 and by_part[b]["quantity"] is None
    assert all(member["purchase_metrics"]["price"] is None for member in members)
    assert get("/members", pool_id=gid, scope='{"unit":null}') == members

    for part_id, expected, supplier in [(a, source["orders"], source["supplier_id"]), (a, by_part[a]["orders"], None), (b, by_part[b]["orders"], None)]:
        scope = {"partId": str(part_id), "unit": None, "_line": "1"}
        if supplier is not None:
            scope["supplier"] = str(supplier)
        response = client.get("/api/purchases/analytics/orders", params={"scope": json.dumps(scope)})
        assert response.status_code == 200, response.text
        assert response.json()["total"] == expected
        assert all(product["unit"] is None for order in response.json()["items"] for product in order["products"])


@pytest.mark.parametrize(
    "perm", ["data_purchase_cost", "data_pool_price_governance", "data_supplier"]
)
def test_pool_derived_permissions(db, inventory, monkeypatch, perm):
    a, _, _ = inventory
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    overrides = {
        "page_purchase_analytics": True,
        "data_pool_price_governance": True,
        "data_purchase_cost": True,
        "data_supplier": True,
        perm: False,
    }
    client = client_for(db, username="pool-hidden", role="boss", overrides=overrides)
    r = client.get(URL + "/parts")
    assert r.status_code == 200, r.text
    rows = r.json()["items"]
    assert rows
    if perm == "data_supplier":
        assert all(r["suppliers"] is None for r in rows)
        assert client.get(URL + "/sources", params={"part_id": a}).status_code == 403
        assert client.get(URL + "/parts", params={"focus": "single"}).status_code == 403
    else:
        assert all(r["purchase_metrics"] is None for r in rows)
        assert client.get(URL + "/parts", params={"focus": "above_pool"}).status_code == 403
        assert client.get(URL + "/parts", params={"focus": "above_cap"}).status_code == 403


def test_role_and_page_guard(db, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    client = client_for(
        db, username="pool-denied", role="readonly", overrides={"page_purchase_analytics": True}
    )
    assert client.get(URL + "/parts").status_code == 403


def test_pool_scope_follows_merged_identity_without_collapsing_source_pn(db, inventory):
    a, _, gid = inventory
    alias = _part(db, "旧 PN", "内存")
    alias.merged_into_id = a
    alias.status = "merged"
    alias.unit = "件"
    batch = _batch(db, "merge-batch")
    o = _po(db, "MERGE", batch, on=date(2025, 3, 2), source_type="销售订单")
    l = _line(db, "merge-line", o, alias, batch, line_no=1, qty="1", price="100", amount=D(100))
    l.unit = "件"
    db.commit()
    client = client_for(db, username="merged-stats", role="admin")
    scoped = client.get(URL + "/parts", params={"scope": '{"pool":"%s","unit":"件"}' % gid})
    assert scoped.status_code == 200, scoped.text
    rows = scoped.json()["items"]
    assert {r["pn"] for r in rows} == {"MEM-A", "MEM-B", "旧 PN"}
    assert all(r["pool_id"] == gid and r["purchase_metrics"]["pool_price"] == 140 for r in rows)
    peers = client.get(URL + "/members", params={"pool_id": gid, "unit": "件"}).json()["items"]
    historical = next(r for r in peers if r["part_id"] == alias.id)
    assert historical["orders"] == 1 and historical["purchase_metrics"]["price"] == 100
