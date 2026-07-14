"""老板看板订单接口的安全/精确定位/日期边界回归。

本文件专门守住三个不能靠前端补救的后端契约：
- 受限销售连“某过滤条件下有几单”都不可见，且全程零查询；
- order_no 是全等定位，q 仍是模糊搜索；
- 默认有效单视图始终排除未来日期，只有显式“全部”保留诊断视图。
"""
from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import permissions, security
from app.api import dashboard as dashboard_api
from app.db import engine, get_db
from app.etl import loader
from app.main import app
from app.models.system import SysImportBatch
from app.services import dashboard, profit
from tests import factories as f


def _ctx(*, scoped: bool = False) -> security.UserContext:
    perms = permissions._full()
    perms["own_customers_only"] = scoped
    return security.UserContext(
        user_id="sales-limited" if scoped else "boss",
        role="custom" if scoped else "boss",
        salesperson_name="测试销售",
        permissions=perms,
        is_authenticated=True,
    )


def _customer_blind_ctx() -> security.UserContext:
    perms = permissions._full()
    perms["data_customer"] = False
    perms["own_customers_only"] = False
    return security.UserContext(
        user_id="customer-blind",
        role="custom",
        salesperson_name=None,
        permissions=perms,
        is_authenticated=True,
    )


@contextmanager
def _query_count():
    counter = {"n": 0}

    def before(*_args):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", before)


@pytest.fixture()
def seeded(db):
    """22 张前缀相似单 + 销采各 1 张未来单。"""
    today = date.today()
    batch = SysImportBatch(filename="order-safety.xlsx", file_type="purchase",
                           file_hash="h-order-safety")
    db.add(batch)
    db.flush()

    sales_heads = {}
    sales_lines = []
    purchase_heads = {}
    purchase_lines = []
    for i in range(22):
        raw_s, no_s = f"SR{i}", f"SO-PREFIX-TARGET-{i:02d}"
        sales_heads[raw_s] = f.sales_head(raw_s, order_no=no_s, on=today - timedelta(days=10))
        sales_lines.append(f.sales_line(raw_s, f"SL{i}", "PN-SAFE", qty="1", price="113"))

        raw_p, no_p = f"PR{i}", f"PO-PREFIX-TARGET-{i:02d}"
        purchase_heads[raw_p] = f.purchase_head(
            raw_p, order_no=no_p, on=today - timedelta(days=9), is_tax_inclusive=True)
        purchase_lines.append(f.purchase_line(raw_p, f"PL{i}", "PN-SAFE", qty="1", price="113"))

    sales_heads["SF"] = f.sales_head(
        "SF", order_no="SO-FUTURE", on=today + timedelta(days=30))
    sales_lines.append(f.sales_line("SF", "SLF", "PN-SAFE", qty="1", price="113"))
    purchase_heads["PF"] = f.purchase_head(
        "PF", order_no="PO-FUTURE", on=today + timedelta(days=30), is_tax_inclusive=True)
    purchase_lines.append(f.purchase_line("PF", "PLF", "PN-SAFE", qty="1", price="113"))

    loader.load(db, f.purchase_result(purchase_heads, purchase_lines), batch.id, today)
    loader.load(db, f.sales_result(sales_heads, sales_lines), batch.id, today)
    db.commit()
    profit.recompute(db)
    return today


def _client(db, ctx):
    original = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[dashboard_api.get_current_user_context] = lambda: ctx
    app.dependency_overrides[dashboard_api.current_role] = lambda: ctx.role
    return TestClient(app), original


def _restore_overrides(original):
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original)


def test_scoped_sales_short_circuits_before_all_queries_and_has_stable_shape(db, seeded):
    ctx = _ctx(scoped=True)
    variants = [
        {},
        {"customer": "真实客户"},
        {"salesperson": "真实销售"},
        {"order_no": "SO-PREFIX-TARGET-07"},
        {"q": "PN-SAFE", "sort": "gross_profit", "order": "asc"},
    ]
    outputs = []
    with _query_count() as count:
        for kwargs in variants:
            outputs.append(dashboard.sales_orders(db, user_ctx=ctx, **kwargs))
    assert count["n"] == 0, "权限短路必须发生在任何过滤/计数 SQL 之前"
    assert all(out == outputs[0] for out in outputs)
    assert outputs[0] == {
        "contract_version": 2,
        "total": None,
        "page": 1,
        "page_size": 50,
        "as_of": seeded.isoformat(),
        "effective_sort": None,
        "ranking_restricted": True,
        "profit_restricted": True,
        "parts_restricted": True,
        "orders_restricted": True,
        "manual_reference_restricted": False,
        "items": [],
    }


def test_scoped_sales_api_cannot_guess_customer_salesperson_order_or_totals(db, seeded):
    client, original = _client(db, _ctx(scoped=True))
    try:
        paths = [
            "/api/dashboard/sales",
            "/api/dashboard/sales?customer=真实客户",
            "/api/dashboard/sales?salesperson=真实销售",
            "/api/dashboard/sales?order_no=SO-PREFIX-TARGET-07",
            "/api/dashboard/sales?order_no=不存在的单",
            "/api/dashboard/sales?sort=gross_profit&order=asc",
        ]
        bodies = []
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200
            bodies.append(response.json())
        assert all(body == bodies[0] for body in bodies)
        assert bodies[0]["orders_restricted"] is True
        assert bodies[0]["total"] is None and bodies[0]["items"] == []
        assert bodies[0]["contract_version"] == 2
    finally:
        _restore_overrides(original)


def test_customer_blind_api_rejects_customer_filter_oracle(db, seeded):
    """非销售受限账号也不能靠 total 差异枚举被脱敏的客户名。"""
    client, original = _client(db, _customer_blind_ctx())
    try:
        baseline = client.get("/api/dashboard/sales")
        matching = client.get("/api/dashboard/sales", params={"customer": "真实客户"})
        missing = client.get("/api/dashboard/sales", params={"customer": "绝对不存在客户"})
        assert baseline.status_code == 200
        assert matching.status_code == missing.status_code == 403
        assert matching.json() == missing.json() == {
            "detail": "当前账号无客户信息权限，不能按客户筛选",
        }
    finally:
        _restore_overrides(original)


@pytest.mark.parametrize("path,action,order_no,scoped", [
    ("/api/dashboard/sales", "sales", "SO-PREFIX-TARGET-07", True),
    ("/api/dashboard/purchase-orders", "purchase_orders", "PO-PREFIX-TARGET-07", False),
])
def test_exact_order_access_log_records_order_no_without_weakening_restriction(
        db, seeded, monkeypatch, path, action, order_no, scoped):
    """订单弹窗改走 order_no 精确查找后，访问日志必须能追溯具体单号。

    销售受限时仍记录访问目标，但响应继续是稳定的受限结构，不暴露
    该单是否存在。"""
    calls = []
    monkeypatch.setattr(
        dashboard_api, "record_access_log",
        lambda ctx, got_action, resource, filters: calls.append(
            (got_action, resource, filters)),
    )
    client, original = _client(db, _ctx(scoped=scoped))
    try:
        response = client.get(path, params={"order_no": order_no})
        assert response.status_code == 200
        assert calls == [(action, "dashboard", {
            "q": None, "order_no": order_no, "status": None,
            "sort": "order_date", "part_id": None, "pool_group_id": None,
        })]
        if scoped:
            body = response.json()
            assert body["orders_restricted"] is True
            assert body["total"] is None and body["items"] == []
    finally:
        _restore_overrides(original)


def test_exact_order_no_beats_20_plus_prefix_matches_in_both_services(db, seeded):
    # q 保留模糊语义；精确 order_no 在 page_size=1 时仍只返回指定单。
    assert dashboard.sales_orders(db, q="SO-PREFIX-TARGET", page_size=1)["total"] == 22
    sales = dashboard.sales_orders(db, order_no="SO-PREFIX-TARGET-07", page_size=1)
    assert sales["total"] == 1
    assert [row["order_no"] for row in sales["items"]] == ["SO-PREFIX-TARGET-07"]

    assert dashboard.purchase_orders(db, q="PO-PREFIX-TARGET", page_size=1)["total"] == 22
    purchases = dashboard.purchase_orders(db, order_no="PO-PREFIX-TARGET-07", page_size=1)
    assert purchases["total"] == 1
    assert [row["order_no"] for row in purchases["items"]] == ["PO-PREFIX-TARGET-07"]
    assert sales["contract_version"] == purchases["contract_version"] == 2


def test_exact_order_no_is_wired_through_both_api_endpoints(db, seeded):
    client, original = _client(db, _ctx())
    try:
        sales = client.get("/api/dashboard/sales", params={"order_no": "SO-PREFIX-TARGET-13",
                                                            "page_size": 1})
        purchases = client.get("/api/dashboard/purchase-orders",
                               params={"order_no": "PO-PREFIX-TARGET-13", "page_size": 1})
        assert sales.status_code == purchases.status_code == 200
        assert [x["order_no"] for x in sales.json()["items"]] == ["SO-PREFIX-TARGET-13"]
        assert [x["order_no"] for x in purchases.json()["items"]] == ["PO-PREFIX-TARGET-13"]
        assert sales.json()["contract_version"] == purchases.json()["contract_version"] == 2
    finally:
        _restore_overrides(original)


@pytest.mark.parametrize("service,future_no", [
    (dashboard.sales_orders, "SO-FUTURE"),
    (dashboard.purchase_orders, "PO-FUTURE"),
])
def test_default_and_specific_status_exclude_future_even_with_future_date_to(
        db, seeded, service, future_no):
    future_upper = seeded + timedelta(days=60)
    default = service(db, date_to=future_upper)
    explicit_active = service(db, status="已生效", date_to=future_upper)
    diagnostic = service(db, status="全部", date_to=future_upper)
    assert future_no not in {x["order_no"] for x in default["items"]}
    assert future_no not in {x["order_no"] for x in explicit_active["items"]}
    assert future_no in {x["order_no"] for x in diagnostic["items"]}


def test_api_default_excludes_future_but_explicit_all_keeps_diagnostic_view(db, seeded):
    client, original = _client(db, _ctx())
    try:
        date_to = (seeded + timedelta(days=60)).isoformat()
        for path, future_no in (("/api/dashboard/sales", "SO-FUTURE"),
                                ("/api/dashboard/purchase-orders", "PO-FUTURE")):
            default = client.get(path, params={"date_to": date_to, "page_size": 200}).json()
            diagnostic = client.get(path, params={"date_to": date_to, "status": "全部",
                                                   "page_size": 200}).json()
            assert future_no not in {x["order_no"] for x in default["items"]}
            assert future_no in {x["order_no"] for x in diagnostic["items"]}
    finally:
        _restore_overrides(original)
