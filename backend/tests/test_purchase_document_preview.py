"""Document hover must use the same visible facts as a drill, including its exact scope."""

import json

from sqlalchemy import update
from app.models import FPurchaseLine

from tests.test_purchase_analytics_api import boss, ds  # noqa: F401 shared seeded purchase flow
from tests.boss_board_helpers import client_for
from tests.test_accounts_departed_hide import _departed_account

URL = "/api/purchases/analytics/document-preview"


def test_chart_sample_is_the_latest_visible_matching_line(ds, db, boss):
    endpoint = "/api/purchases/analytics/chart-sample"
    params = {"scope": json.dumps({"buyer": "采购员甲", "pn": "PN-DQ"}), "date_to": "2026-03-31"}
    ds.p1.description = "目录中的电器备件"
    db.execute(update(FPurchaseLine).where(FPurchaseLine.raw_line_id == "PA-L1")
               .values(unit="件", description=""))
    db.commit()
    response = boss.get(endpoint, params=params)
    assert response.status_code == 200, response.text
    sample = response.json()["sample"]
    assert (sample["order_no"], sample["order_date"]) == (ds.poa.order_no, ds.poa.order_date.isoformat())
    assert (sample["pn_std"], sample["description"], sample["category"]) == ("PN-DQ", "目录中的电器备件", "电器件")
    assert (sample["qty"], sample["unit"]) == (1, "件")
    assert boss.get(endpoint, params={**params, "category": "机械件"}).json() == {"sample": None}
    assert boss.get(endpoint, params={"scope": json.dumps({"orderId": str(ds.pod.id)})}).json() == {"sample": None}
    _departed_account(db, salesperson="销售小王")
    assert boss.get(endpoint, params={"scope": json.dumps({"orderId": str(ds.poa.id)})}).json() == {"sample": None}
    restricted = client_for(db, username="sample-no-vendor", role="boss", overrides={"data_supplier": False})
    assert restricted.get(endpoint, params={"scope": '{"supplier":"1"}'}).status_code == 403


def test_preview_on_a_sale_only_returns_matching_purchase_goods(ds, db, boss):
    db.execute(update(FPurchaseLine).where(FPurchaseLine.raw_line_id == "PA-L1").values(unit="件"))
    db.commit()
    scope = json.dumps({"pn": "PN-DQ", "type": "销售订单"})
    params = {
        "sales_no": "XS-001",
        "scope": scope,
        "date_from": "2026-03-01",
        "date_to": "2026-03-31",
    }
    r = boss.get(URL, params=params)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["orders"] == 1
    assert [(p["pn"], p["qty"]) for p in result["products"]] == [("PN-DQ", 1)]
    assert result["categories"] == [{"category": "电器件", "unit": "件", "qty": 1}]
    assert not result["more_products"]
    unknown = boss.get(URL, params={**params, "scope": '{"pn":"PN-FL"}'}).json()
    assert unknown["categories"][0]["qty"] is None
    params["scope"] = json.dumps({"linkedSalesOrderNo": "different"})
    assert boss.get(URL, params=params).json()["products"] == []


def test_purchase_preview_scope_and_field_permissions(ds, db):
    client = client_for(
        db, username="preview-boss", role="boss", overrides={"data_supplier": False}
    )
    assert (
        client.get(
            URL, params={"order_id": ds.poa.id, "scope": '{"supplier":"1"}'}
        ).status_code
        == 403
    )
    assert (
        client.get(
            URL, params={"order_id": ds.poa.id, "scope": '{"unknown":"1"}'}
        ).status_code
        == 422
    )
    assert (
        client.get(
            URL, params={"order_id": ds.poa.id, "sales_no": "XS-001"}
        ).status_code
        == 422
    )
    r = client.get(URL, params={"order_id": ds.poa.id, "category": "机械件"})
    assert r.status_code == 200
    assert r.json()["orders"] == 0


def test_product_candidate_search_is_bounded_to_matching_orders_and_preserves_unknown_units(
    ds, boss
):
    url = "/api/purchases/analytics/product-options"
    result = boss.get(
        url, params={"axis": "pn", "scope": '{"type":"销售订单"}', "q": "dq"}
    )
    assert result.status_code == 200, result.text
    assert [item["key"] for item in result.json()["items"]] == ["PN-DQ"]
    assert boss.get(
        url, params={"axis": "unit", "q": "", "scope": '{"pn":"PN-DQ"}'}
    ).json()["items"] == [{"key": None, "label": None}]
    assert not boss.get(url, params={"axis": "pn", "date_to": "2000-01-01"}).json()[
        "items"
    ]
