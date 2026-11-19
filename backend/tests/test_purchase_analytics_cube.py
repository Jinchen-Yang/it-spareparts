import json

from app.services import purchase_analytics
from app.models import FPurchaseLine
from app.models.dimensions import DimSupplier
from tests.boss_board_helpers import client_for
from tests.test_accounts_departed_hide import _departed_account
from tests.test_purchase_analytics_api import boss as boss, ds as ds
from tests.test_purchase_analytics_equivalence import _capture_selects


BASE = "/api/purchases/analytics/"


def read(client, endpoint="cube", **params):
    reply = client.get(BASE + endpoint, params=params)
    assert reply.status_code == 200, reply.text
    return reply.json()


def test_paged_matrix_and_exact_drill(ds, boss):
    head = read(boss, row="type", column="buyer", metric="orders")
    assert head["basis_mode"] == "order"
    assert head["total"]["orders"] == 6
    assert head["total"]["lines"] == 7
    assert head["total"]["amount_paid"] == 4826
    line = read(boss, row="pn", column="buyer", metric="amount", page_size=1, column_size=2)
    assert line["total_rows"] == 3 and line["total_columns"] == 4
    assert len(line["rows"]) == 1 and len(line["columns"]) == 2
    assert line["total"]["value"] == 6190
    assert line["rows"][0]["key"] == "PN-DQ"
    for cell in line["cells"]:
        scope = json.dumps(cell["scope"])
        listed = read(boss, "orders", scope=scope)
        assert listed["total"] == cell["orders"]
        assert sum(o["amount_paid"] for o in listed["items"]) == cell["value"]
        for order in listed["items"]:
            assert all(p["pn_std"] == cell["row_key"] for p in order["products"])
            detail = read(boss, "orders/" + str(order["order_id"]), scope=scope)
            assert detail["category_matched"]["amount_paid"] == order["amount_paid"]
    assert read(boss, row="pn", page_size=1, page=99)["total_rows"] == 3


def test_period_union_and_missing_amount(ds, boss):
    reply = read(
        boss,
        row="buyer",
        compare="true",
        date_from="2026-04-01",
        date_to="2026-04-30",
        sort="absolute",
    )
    assert reply["previous_from"] == "2026-03-02"
    prev_only = next(r for r in reply["rows"] if r["key"] == "采购员甲")
    assert prev_only["previous_value"] == 1130 and prev_only["value"] == 0
    unknown = next(r for r in reply["rows"] if r["key"] is None)
    assert unknown["previous_value"] is None and unknown["change"] is None
    assert reply["rows"][0]["key"] == "采购员乙"
    assert reply["rows"][0]["change_rate"] is None


def test_identity_relationship_concentration_and_permissions(ds, boss, db):
    a = DimSupplier(name_raw="来源一", name_normalized="同名")
    b = DimSupplier(name_raw="来源二", name_normalized="同名")
    db.add_all([a, b])
    db.flush()
    ds.poa.supplier_id = a.id
    ds.poc.supplier_id = b.id
    db.query(FPurchaseLine).update({FPurchaseLine.unit: "件"})
    db.commit()
    groups = read(boss, row="supplier", metric="orders")
    assert {r["key"] for r in groups["rows"]} == {str(a.id), str(b.id), None}
    assert read(boss, "shared-pns", supplier_id=a.id)["items"][0]["pn"] == "PN-DQ"
    result = read(boss, "concentration")
    pn = next(r for r in result["items"] if r["pn"] == "PN-DQ")
    assert pn["total_qty"] == 3 and pn["leading_qty"] == 1
    assert abs(pn["share"] - 1 / 3) < 1e-9
    nocost = client_for(
        db, username="cube-nocost", role="boss", overrides={"data_purchase_cost": False}
    )
    assert nocost.get(BASE + "cube", params={"row": "buyer", "metric": "amount"}).status_code == 403
    assert read(nocost, row="buyer", metric="orders")["total"]["amount_paid"] is None
    restricted = client_for(
        db, username="cube-novendor", role="boss", overrides={"data_supplier": False}
    )
    assert (
        restricted.get(BASE + "cube", params={"row": "supplier", "metric": "orders"}).status_code
        == 403
    )
    assert (
        restricted.get(
            BASE + "orders", params={"scope": json.dumps({"supplier": str(a.id)})}
        ).status_code
        == 403
    )
    assert read(restricted, row="buyer", metric="orders")["total"]["supplier_count"] is None
    assert boss.get(BASE + "cube", params={"row": "buyer", "scope": "[]"}).status_code == 422


def test_departed_documents_hide_only_identity_surface(ds, boss, db):
    _departed_account(db, salesperson="销售小王")
    linked = read(boss, row="buyer", column="linkedSalesOrderNo", metric="orders")
    assert linked["total"]["orders"] == 6
    assert sum(group["orders"] for group in linked["rows"]) == 6
    assert "XS-001" not in [g["key"] for g in linked["columns"]]
    assert "XS-001" not in [g["column_key"] for g in linked["cells"]]
    numbers = read(boss, row="orderId", metric="orders")
    assert str(ds.poa.id) not in [g["key"] for g in numbers["rows"]]
    assert numbers["total"]["orders"] == 6
    assert boss.get(BASE + "orders/" + str(ds.poa.id)).status_code == 404


def test_absent_period_avg_and_incomparable_units(ds, boss):
    date_range = dict(compare="true", date_from="2026-04-01", date_to="2026-04-30")
    rows = read(boss, row="buyer", metric="avgOrder", **date_range)["rows"]
    missing = next(row for row in rows if row["key"] == "采购员甲")
    assert missing["value"] is None and missing["change"] is None and missing["change_rate"] is None
    units = read(boss, row="unit", metric="qty", **date_range)
    assert units["total"]["value"] is None and units["previous_total"]["value"] is None
    assert all(row["value"] is None and row["previous_value"] is None for row in units["rows"])


def test_part_identity_can_refine_pn_drill(ds, boss):
    rows = read(boss, "orders", scope=json.dumps({"partId": str(ds.p1.id), "pn": "PN-DQ"}))
    assert rows["total"] == 3
    assert all(p["part_id"] == ds.p1.id for o in rows["items"] for p in o["products"])
    assert (
        read(boss, "orders", scope=json.dumps({"partId": str(ds.p2.id), "pn": "PN-DQ"}))["total"]
        == 0
    )
    assert boss.get(BASE + "cube", params={"row": "partId", "metric": "orders"}).status_code == 422


def test_header_cell_pages_before_loading_current_batch_products(ds):
    statements = _capture_selects(
        lambda: purchase_analytics.orders(
            ds.db, scope={"buyer": "采购员甲", "type": "销售订单"}, page=2, page_size=1
        )
    )
    main = [sql for sql, _ in statements if "count(*)" in sql or "LIMIT" in sql]
    assert len(main) == 2
    assert all("f_purchase_line" not in sql and "f_purchase_order" in sql for sql in main)
