import json
from datetime import date, datetime

import pytest

from tests.boss_board_helpers import client_for

BASE = "/api/inventory/analytics"


@pytest.fixture
def warehouse(db):
    from app.models.warehouse_analytics import WarehouseAnalyticsDocument as Document
    from app.models.warehouse_analytics import WarehouseAnalyticsLine as Line

    def doc(source, key, when, kind, person, **fields):
        row = Document(
            source=source,
            source_id=key,
            source_modified_at=datetime(2026, 11, 19),
            number=key,
            business_date=date.fromisoformat(when),
            kind=kind,
            operator=person,
            status=fields.pop("status", "已生效"),
            **fields,
        )
        db.add(row)
        db.flush()
        return row.id

    first = doc(
        "return",
        "A",
        "2026-11-15",
        "销售退货",
        "张",
        center="东",
        whole_pn="SERVER",
        whole_description="服务器",
        whole_qty=2,
        whole_unit="台",
        needs_test=True,
        tested=True,
        tester="测试甲;测试乙",
        tested_date=date(2026, 11, 19),
    )
    second = doc(
        "inbound",
        "B",
        "2026-11-16",
        "采购",
        "张",
        center="西",
        whole_pn="SERVER",
        whole_qty=3,
        whole_unit="台",
        needs_test=True,
        tested=False,
    )
    third = doc("return", "C", "2026-11-17", "销售退货", None, needs_test=True, tested=None)
    doc("direct", "D", "2026-11-16", "直采退货", "张", needs_test=False)
    doc("return", "V", "2026-11-15", "销售退货", "张", status="已取消")
    for i, (did, pn, qty, unit, wh) in enumerate(
        [
            (first, "PN-A", 1, "件", "库甲"),
            (first, "PN-B", 2, "件", "库甲"),
            (first, "PN-A", 9, "套", None),
            (second, "PN-A", 4, "件", "库甲"),
            (third, None, 3, None, "库乙"),
        ]
    ):
        db.add(
            Line(
                document_id=did,
                source_id=f"L{i}",
                pn=pn,
                description="描述" + (pn or ""),
                quantity=qty,
                unit=unit,
                warehouse=wh,
            )
        )
    db.commit()
    return client_for(db, username="warehouse-analytics-boss", role="boss"), first


def test_cube_full_scope_margins_and_time(warehouse):
    c, first = warehouse
    data = c.get(
        BASE + "/cube", params={"row": "operator", "col": "pn", "metric": "documents", "col_limit": 1}
    ).json()
    assert data["columns"][0]["key"] == "PN-A"
    assert data["total"] == 3  # three actual line-bearing documents
    assert next(r["value"] for r in data["rows"] if r["key"] == "张") == 2
    assert next(cell["value"] for cell in data["cells"] if cell["row_key"] == "张") == 2
    assert c.get(BASE + "/cube", params={"row": "pn", "metric": "quantity"}).status_code == 422
    qty = c.get(BASE + "/cube", params={"row": "pn", "metric": "quantity", "unit": "件"}).json()
    assert qty["total"] == 7 and qty["rows"][0]["value"] == 5
    weekly = c.get(BASE + "/cube", params={"row": "week", "metric": "documents"}).json()
    assert [(r["key"], r["value"]) for r in weekly["rows"]] == [("2026-11-09", 1), ("2026-11-16", 3)]
    scope = json.dumps({"warehouse": None, "week": "2026-11-09"})
    docs = c.get(BASE + "/documents", params={"scope": scope, "date_from": "2026-11-15"}).json()
    assert docs["total"] == 1 and docs["items"][0]["id"] == first
    assert (docs["items"][0]["matched_lines"], docs["items"][0]["total_lines"]) == (1, 3)
    assert [r["unit"] for r in docs["items"][0]["sample"]] == ["套"]
    assert c.get(BASE + f"/documents/{first}/lines", params={"scope": scope}).json()["total"] == 1
    assert (
        c.get(BASE + "/documents", params={"scope": scope, "date_from": "2026-11-16"}).json()["total"]
        == 0
    )


def test_rankings_roles_equipment_testing_and_coverage(warehouse):
    c, first = warehouse

    def read(path, **params):
        resp = c.get(BASE + path, params=params)
        assert resp.status_code == 200, resp.text
        return resp.json()

    sources = {r["source"]: r for r in read("/coverage")["sources"]}
    assert sources["outbound"]["available"] is False
    assert sources["outbound"]["documents"] is None and sources["outbound"]["lines"] is None
    assert (sources["return"]["documents"], sources["return"]["lines"]) == (2, 4)
    pick = read("/rankings", by="operator", operator_role="pick")
    attributed = next(r for r in pick["items"] if r["key"] == "张")
    assert (attributed["documents"], attributed["active_days"], attributed["rank"]) == (2, 2, 1)
    assert next(r for r in pick["items"] if r["key"] is None)["rank"] is None
    assert read("/rankings", by="operator", operator_role="direct")["items"][0]["documents"] == 1
    pn = read("/rankings", by="pn", metric="lines")
    assert [r["key"] for r in pn["items"]] == ["PN-A", "PN-B"]
    assert pn["top10_line_share"] == 1 and pn["repeat_pns"] == 1
    equipment = read("/equipment", pn="PN-A")
    assert [(r["whole_qty"], r["documents"], r["matched_lines"]) for r in equipment["items"]] == [
        (5, 2, 3)
    ]
    tests = read("/testing")
    assert [tests[k] for k in ("required", "completed", "pending", "unknown", "attributed")] == [
        3,
        1,
        1,
        1,
        1,
    ]
    signatures = read("/testing", by="tester")["items"]
    assert [(x["key"], x["attributed"]) for x in signatures if x["key"] is not None] == [
        ("测试甲;测试乙", 1)
    ]
    assert (
        read("/documents", testing="attributed", scope=json.dumps({"tester": "测试甲;测试乙"}))["total"]
        == 1
    )
    assert read("/documents", testing="unknown", scope=json.dumps({"tester": None}))["total"] == 1
    assert c.get(BASE + f"/documents/{first}/lines", params={"source": "direct"}).status_code == 404
    assert read("/documents", q="PN-B")["items"][0]["sample"][0]["pn"] == "PN-B"
    head_only = read("/documents", source="direct")["items"][0]
    assert head_only["matched_lines"] == 0 and head_only["sample"] == []
    assert read(f"/documents/{head_only['id']}/lines") == {"items": [], "total": 0}
    assert read("/cube", row="week", row_limit=1)["rows"][0]["key"] == "2026-11-16"
    assert c.get(BASE + "/documents", params={"page": 1000001}).status_code == 422
    assert (
        c.get(BASE + "/cube", params={"row": "warehouse", "scope": '{"not_a_field":1}'}).status_code
        == 422
    )
    assert (
        c.get(
            BASE + "/documents", params={"date_from": "2026-12-01", "date_to": "2026-11-01"}
        ).status_code
        == 422
    )


def test_visibility_is_enforced_on_all_routes(warehouse, db):
    c = client_for(
        db,
        username="warehouse-scoped-user",
        role="readonly",
        overrides={"page_inventory_analytics": True},
    )
    for path in (
        "/coverage",
        "/cube",
        "/rankings",
        "/documents",
        "/documents/1/lines",
        "/testing",
        "/equipment",
    ):
        assert c.get(BASE + path).status_code == 403


def test_scope_restrictions_and_known_zero(warehouse, db):
    from app.models.warehouse_analytics import WarehouseAnalyticsDocument as Document
    from sqlalchemy import update

    boss, _ = warehouse
    restricted = client_for(
        db, username="warehouse-limited-boss", role="boss", overrides={"own_customers_only": True}
    )
    assert restricted.get(BASE + "/documents").status_code == 403
    denied = client_for(
        db, username="warehouse-page-denied", role="boss", overrides={"page_inventory_analytics": False}
    )
    assert denied.get(BASE + "/coverage").status_code == 403
    db.execute(update(Document).where(Document.source == "inbound").values(operator="甲"))
    db.execute(update(Document).where(Document.source == "direct").values(status="已取消"))
    db.commit()
    sources = {row["source"]: row for row in boss.get(BASE + "/coverage").json()["sources"]}
    assert sources["direct"]["available"] is True
    assert sources["direct"]["documents"] == 0 and sources["direct"]["lines"] == 0
    assert sources["outbound"]["available"] is False and sources["outbound"]["documents"] is None
    pages = [
        boss.get(BASE + "/rankings", params={"by": "operator", "page": page, "page_size": 1}).json()
        for page in (1, 2, 3)
    ]
    assert {p["items"][0]["key"] for p in pages[:2]} == {"甲", "张"}
    assert [p["items"][0]["rank"] for p in pages] == [1, 1, None]


def test_line_dimension_margins_drill_only_into_documents_with_lines(warehouse, db):
    from app.models.warehouse_analytics import WarehouseAnalyticsDocument as Document

    client, _ = warehouse
    db.add(Document(source="return", source_id="without-lines", number="empty-source-doc",
                    status="已生效", business_date=date(2026, 11, 16),
                    center="东", source_modified_at=datetime(2026, 11, 19)))
    db.commit()
    filters = {"center": "东", "source": "return"}
    cube = client.get(BASE + "/cube", params={**filters, "row": "center", "col": "pn"}).json()
    normal = client.get(BASE + "/documents", params=filters).json()
    matches = client.get(BASE + "/documents", params={**filters, "require_lines": True}).json()
    assert normal["total"] == cube["total"] + 1
    assert matches["total"] == cube["rows"][0]["value"] == cube["total"]
    assert all(item["matched_lines"] for item in matches["items"])
