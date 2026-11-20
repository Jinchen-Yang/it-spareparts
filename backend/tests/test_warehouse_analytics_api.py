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
    _, document_id = warehouse
    paths = (
        "/coverage",
        "/cube",
        "/rankings",
        "/documents",
        f"/documents/{document_id}/lines",
        "/testing",
        "/equipment",
    )
    for role, granted, expected in (
        ("readonly", True, 200),
        ("readonly", False, 403),
        ("sales", True, 200),
        ("purchaser", False, 403),
        ("boss", False, 403),
        ("admin", True, 200),
        ("admin", False, 403),
    ):
        c = client_for(
            db,
            username=f"warehouse-{role}-{granted}",
            role=role,
            overrides={"page_inventory_analytics": granted},
        )
        for path in paths:
            assert c.get(BASE + path).status_code == expected, (path, role, granted)


def test_inventory_reports_use_warehouse_grant_and_preserve_known_zero(warehouse, db):
    from app.models.warehouse_analytics import WarehouseAnalyticsDocument as Document
    from sqlalchemy import update

    boss, _ = warehouse
    restricted = client_for(
        db, username="warehouse-limited-boss", role="boss", overrides={"own_customers_only": True}
    )
    assert restricted.get(BASE + "/documents").status_code == 200
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


def test_unidentified_and_shared_tokens_cannot_use_inventory_grant(warehouse, db):
    from app.auth import _make_token, verify_token_db
    from app.config import get_settings
    from app.main import app
    from fastapi.testclient import TestClient

    page = "page_inventory_analytics"
    client = TestClient(app)
    shared = client.post(
        "/api/auth/login", json={"username": "admin", "password": get_settings().admin_password}
    )
    assert shared.status_code == 200
    assert shared.json()["permissions"][page] is False
    assert (
        client.get(
            BASE + "/coverage", headers={"Authorization": f"Bearer {shared.json()['token']}"}
        ).status_code
        == 403
    )
    for sub, authn, fallback in (
        ("warehouse-analytics-boss", None, False),
        ("warehouse-analytics-boss", "shared", False),
        ("warehouse-analytics-boss", "sys_user", True),
        ("warehouse-unknown", "sys_user", False),
    ):
        token, _ = _make_token("admin", sub, None, fallback=fallback, perms={page: True}, authn=authn)
        assert verify_token_db(token, db)["perms"][page] is False
        response = client.get(BASE + "/coverage", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

    # A named old token without the newly introduced key uses the verified account.
    token, _ = _make_token("boss", "warehouse-analytics-boss", None, perms={}, authn="sys_user")
    assert verify_token_db(token, db)["perms"][page] is True
    # A pre-change token may still carry the old unconditional admin grant.
    client_for(db, username="warehouse-denied-admin", role="admin", overrides={page: False})
    token, _ = _make_token(
        "admin", "warehouse-denied-admin", None, perms={page: True}, authn="sys_user"
    )
    assert (
        client.get(BASE + "/coverage", headers={"Authorization": f"Bearer {token}"}).status_code == 403
    )


def test_legacy_account_inventory_baseline_and_admin_creation(db):
    from app.models.system import SysUser
    from sqlalchemy import select

    page = "page_inventory_analytics"
    operator = client_for(db, username="warehouse-manager", role="admin")
    create = operator.post(
        "/api/accounts",
        json={
            "role": "admin",
            "username": "created-without-warehouse",
            "password": "synthetic-warehouse-admin",
            "overrides": {page: False},
        },
    )
    assert create.status_code == 201 and create.json()["permissions"][page] is False
    user = db.scalar(select(SysUser).where(SysUser.username == "created-without-warehouse"))
    user.template_perms = None
    user.permissions = {page: True, "page_maintenance_beta": False}
    user.perm_overrides = None
    db.commit()
    accounts = operator.get("/api/accounts").json()
    old = next(a for a in accounts if a["username"] == user.username)
    assert old["permissions"][page] is old["template_perms"][page] is True
    unaffected = {k: v for k, v in old["permissions"].items() if k != page}
    version = user.token_version
    for overrides, expected in (({page: False}, False), ({}, True)):
        response = operator.put(f"/api/accounts/{user.username}", json={"overrides": overrides})
        assert response.status_code == 200
        graph = response.json()
        assert graph["template_perms"][page] is True and graph["permissions"][page] is expected
        assert {k: v for k, v in graph["permissions"].items() if k != page} == unaffected
        db.refresh(user)
        version += 1
        assert user.token_version == version
    # Even an administrator with incomplete historical data must be granted the
    # missing inventory bit. The editor must show exactly the same closed base.
    user.template_perms = {"page_maintenance_beta": False}
    user.permissions = {page: True}
    user.perm_overrides = None
    db.commit()
    incomplete = next(
        acc for acc in operator.get("/api/accounts").json() if acc["username"] == user.username
    )
    assert incomplete["template_perms"][page] is incomplete["permissions"][page] is False
    repaired = operator.put(f"/api/accounts/{user.username}", json={"overrides": {page: True}})
    assert repaired.status_code == 200 and repaired.json()["permissions"][page] is True


def test_inventory_always_checks_the_named_grant_with_legacy_rbac_switch(warehouse, db, monkeypatch):
    from app import config
    from app.config import get_settings
    from app.main import app
    from fastapi.testclient import TestClient

    allowed, document_id = warehouse
    closed = client_for(
        db,
        username="warehouse-legacy-switch-denied",
        role="admin",
        overrides={"page_inventory_analytics": False},
    )
    shared = TestClient(app)
    login = shared.post(
        "/api/auth/login", json={"username": "admin", "password": get_settings().admin_password}
    )
    assert login.status_code == 200
    shared.headers["Authorization"] = f"Bearer {login.json()['token']}"
    monkeypatch.setattr(config, "ENABLE_RBAC", False)
    for path in (
        "/coverage",
        "/cube",
        "/rankings",
        "/documents",
        f"/documents/{document_id}/lines",
        "/equipment",
        "/testing",
    ):
        for client, status in ((allowed, 200), (closed, 403), (shared, 403)):
            assert client.get(BASE + path).status_code == status, path
