"""人员全域排行榜的公共接口契约。"""

import json

from app.models import FPurchaseLine
from app.models.dimensions import DimSupplier

from tests.boss_board_helpers import client_for
from tests.test_purchase_analytics_api import boss as boss  # noqa: PLC0414 -- pytest fixture
from tests.test_purchase_analytics_api import ds as ds  # noqa: PLC0414 -- pytest fixture

PATH = "/api/purchases/analytics/buyer-ranks"


def ranks(client, **params):
    response = client.get(PATH, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_sorting_preserves_order_grain_and_all_other_measures(ds, db, boss):
    supplier = DimSupplier(name_raw="厂商甲", name_normalized="厂商甲")
    db.add(supplier)
    db.flush()
    ds.poa.supplier_id = ds.pof.supplier_id = supplier.id
    db.query(FPurchaseLine).update({FPurchaseLine.unit: "件"})
    db.query(FPurchaseLine).filter_by(raw_line_id="PA-L2").update({FPurchaseLine.unit: "套"})
    db.commit()

    base = ranks(boss, quantity_unit="件")
    assert base["total"] == len(base["items"]) == 4
    by_name = {row["key"]: row for row in base["items"]}
    assert by_name["采购员甲"] == {
        "key": "采购员甲",
        "label": "采购员甲",
        "rank": 1,
        "scope": {"buyer": "采购员甲"},
        "lines": 3,
        "orders": 2,
        "amount": 2260,
        "pn_count": 2,
        "supplier_count": 1,
        "quantity": 2,
    }
    assert by_name["采购员乙"]["lines"] == 1
    assert by_name["采购员乙"]["orders"] == 2  # 单头当前批次无行，仍计单数。
    assert by_name["采购员乙"]["amount"] == 1560
    assert by_name[None]["amount"] is None
    assert by_name[None]["scope"] == {"buyer": None}

    for metric, field in [
        ("orders", "orders"),
        ("amount", "amount"),
        ("lines", "lines"),
        ("pns", "pn_count"),
        ("suppliers", "supplier_count"),
        ("qty", "quantity"),
    ]:
        rows = ranks(boss, quantity_unit="件", metric=metric)["items"]
        assert {r["key"]: {**r, "rank": by_name[r["key"]]["rank"]} for r in rows} == by_name
        values = [row[field] for row in rows if row[field] is not None]
        assert values == sorted(values, reverse=True)
        assert all(row[field] is None for row in rows[len(values) :])
        for row in rows:
            assert row["rank"] == (values.index(row[field]) + 1 if row[field] is not None else None)
    assert ranks(boss, metric="qty", quantity_unit="套")["items"][0] == {
        **by_name["采购员甲"],
        "quantity": 1,
    }
    assert all(
        row["quantity"] is None
        for row in ranks(boss, quantity_unit="套", metric="qty")["items"][1:]
    )
    assert all(row["quantity"] is None for row in ranks(boss)["items"])
    first = ranks(boss, page_size=2)["items"]
    second = ranks(boss, page=2, page_size=2)["items"]
    assert [r["key"] for r in first + second] == [r["key"] for r in base["items"]]
    assert ranks(boss, page=2, page_size=1)["items"][0]["rank"] == 1
    assert ranks(boss, page=3, page_size=1)["items"][0]["rank"] == 3
    assert ranks(boss, page=10, page_size=2) == {"total": 4, "items": []}


def test_line_scope_and_filter_use_matching_facts_for_every_sort(ds, db, boss):
    db.query(FPurchaseLine).filter_by(raw_line_id="PA-L1").update({FPurchaseLine.unit: " 件 "})
    db.commit()
    for params in [
        {"scope": json.dumps({"unit": "件"})},
        {"scope": json.dumps({"pn": "PN-DQ"}), "quantity_unit": "件"},
        {"category": ["电器件"], "quantity_unit": "件"},
    ]:
        rows = ranks(boss, purchaser=["采购员甲"], **params)["items"]
        assert len(rows) == 1
        row = rows[0]
        assert (
            row["quantity"],
            row["orders"],
            row["lines"],
            row["amount"],
            row["pn_count"],
        ) == (1, 1, 1, 500, 1)
        assert row["scope"]["_line"] == "1"
        assert row["scope"]["buyer"] == "采购员甲"
    assert (
        ranks(boss, purchaser=["采购员甲"], category=["电器件"], basis="ex")["items"][0]["amount"]
        == 442.48
    )
    assert ranks(boss, source_type=["指定采购"], basis="inc")["items"][0]["amount"] == 2260
    assert ranks(boss, date_from="2100-01-01") == {"items": [], "total": 0}


def test_permissions_and_missing_units(ds, db, boss):
    for metric, right, field in [
        ("amount", "data_purchase_cost", "amount"),
        ("suppliers", "data_supplier", "supplier_count"),
    ]:
        client = client_for(
            db, username="buyer-rank-" + metric, role="boss", overrides={right: False}
        )
        assert all(row[field] is None for row in ranks(client)["items"])
        assert client.get(PATH, params={"metric": metric}).status_code == 403
    denied = client_for(
        db,
        username="buyer-rank-staff",
        role="purchaser",
        overrides={"page_purchase_analytics": True},
    )
    assert denied.get(PATH).status_code == 403
    assert boss.get(PATH, params={"metric": "qty"}).status_code == 422
    assert (
        boss.get(
            PATH,
            params={
                "metric": "qty",
                "quantity_unit": "套",
                "scope": json.dumps({"unit": "件"}),
            },
        ).status_code
        == 422
    )
