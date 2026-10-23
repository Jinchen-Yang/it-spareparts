"""D-0026: real PDP -> API -> PostgreSQL, including every sales read surface."""
import csv
import io
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.auth import hash_password
from app.config import get_settings
from app.main import app
from app.models.authorization import SysAuthorizationGrant, SysCustomerAssignment
from app.models.sales import FSalesOrder
from app.models.system import SysUser
from tests.authz_pdp_helpers import cerbos_url
from tests.test_sales_dashboard_d44 import seeded  # shared synthetic facts, no production data

PREFIX = "/api/sales/analytics"
FIELDS = ["data_supplier", "data_customer", "data_purchase_cost", "data_profit",
          "data_pool_price_governance", "data_reconciliation"]
PASSWORD = "synthetic-authz-2026-only"


@pytest.fixture(autouse=True)
def new_authority(monkeypatch, cerbos_url):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    monkeypatch.setattr(get_settings(), "authz_sales_analysis_enabled", True)
    monkeypatch.setattr(get_settings(), "cerbos_url", cerbos_url)


def _client(db, *, customer_ids=(), scope="assigned_customers", fields=None,
            actions=("read", "export"), role="maintenance_manager", username="service-sales"):
    # Old permission map deliberately denies the page and every field. New
    # authority must work without changing role or reusing stale token flags.
    user = SysUser(username=username, role=role, password_hash=hash_password(PASSWORD),
                   is_active=True, permissions={"page_sales_analysis": False})
    db.add(user)
    db.flush()
    grant = SysAuthorizationGrant(user_id=user.id, resource="sales_analysis",
                                  actions=list(actions), scope=scope,
                                  field_permissions=list(FIELDS if fields is None else fields),
                                  version=1)
    db.add(grant)
    db.add_all(SysCustomerAssignment(user_id=user.id, customer_id=cid) for cid in customer_ids)
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = "Bearer " + login.json()["token"]
    return client, user, grant


def _get(client, suffix, **params):
    response = client.get(PREFIX + suffix, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_all_orders_of_assigned_customer_across_salespeople_and_dates(db, seeded):
    a1, a2, a3, a4, a5, a6 = seeded["a"]
    client, _, _ = _client(db, customer_ids=[a3.customer_id])
    snapshot = client.get("/api/authz/me")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["sales_analysis"]["read"] is True
    data = _get(client, "/orders", sort="order_date", order="asc", page_size=1)
    assert data["total"] == 2  # pagination is applied after authorization
    assert [item["order_no"] for item in data["items"]] == ["SO-A3"]
    second = _get(client, "/orders", sort="order_date", order="asc", page=2, page_size=1)
    assert [item["order_no"] for item in second["items"]] == ["SO-A6"]
    assert data["items"][0]["salesperson"] != second["items"][0]["salesperson"]
    for order in (a3, a6):
        assert _get(client, f"/orders/{order.id}")["order_no"] == order.order_no
    for order in (a1, a2, a4, a5):
        assert client.get(f"{PREFIX}/orders/{order.id}").status_code == 404
    # A user filter may narrow, never enlarge the grant's customer scope.
    assert _get(client, "/orders", customer_name="A公司")["total"] == 0
    assert _get(client, "/orders", salesperson_name="李四")["total"] == 1
    assert _get(client, "/orders", customer_name="__empty__")["total"] == 0


def test_candidates_summaries_line_groups_trends_and_exports_share_scope(db, seeded):
    customer_id = seeded["a"][2].customer_id
    client, _, _ = _client(db, customer_ids=[customer_id])
    options = _get(client, "/options")
    assert options["customers"] == [{"value": "B公司", "orders": 2}]
    assert sum(row["orders"] for row in options["business_types"]) == 2
    assert {row["value"] for row in options["salespeople"]} == {"张三", "李四"}
    assert options["categories"] == [{"value": "内存", "orders": 1}]
    kpi = _get(client, "/summary")["kpi"]
    assert kpi["orders"] == 2
    assert kpi["amount_ex_tax_total"] == 10000
    assert kpi["revenue_total"] == 7000
    for dim in ("customer", "salesperson", "business_type"):
        groups = _get(client, "/groups", group_by=dim, top=0)["groups"]
        assert sum(row["orders"] for row in groups) == 2
        assert sum(row["amount_ex_tax"] or 0 for row in groups) == 10000
    for dim in ("category", "brand", "pn"):
        groups = _get(client, "/groups", group_by=dim, top=0)["groups"]
        assert sum(row["orders"] for row in groups) == 1
        assert sum(row["revenue"] or 0 for row in groups) == 7000
    trend = _get(client, "/trend", date_from="2026-01-01", date_to="2026-03-31")
    assert sum(row["orders"] for row in trend["buckets"]) == 2
    assert sum(row["amount_ex_tax"] or 0 for row in trend["buckets"]) == 10000
    assert _get(client, "/amount-histogram")["total"] == 2
    response = client.get(PREFIX + "/orders/export")
    assert response.status_code == 200, response.text
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert {row["订单号"] for row in rows} == {"SO-A3", "SO-A6"}
    assert {row["客户"] for row in rows} == {"B公司"}


def test_historical_lifecycle_cannot_use_unassigned_same_name_customer(db, seeded):
    a1, a2, *_ = seeded["a"]
    # Same normalized name, distinct customer IDs. The other customer's earlier
    # order must not turn this assigned customer into a returning customer.
    client, _, _ = _client(db, customer_ids=[a2.customer_id])
    kpi = _get(client, "/summary", date_from="2026-01-11", date_to="2026-01-31")["kpi"]
    assert (kpi["new_customers"], kpi["returning_customers"]) == (1, 0)
    assert _get(client, "/orders", customer_name="A公司")["total"] == 1
    db.add(FSalesOrder(raw_order_id="scope-previous", order_no="scope-previous",
                       order_date=date(2025, 12, 11), customer_id=a2.customer_id,
                       data_status="已生效", import_batch_id=a2.import_batch_id))
    db.commit()
    comparison = _get(client, "/summary", date_from="2026-01-01", date_to="2026-01-31", compare=True)
    assert comparison["compare"]["kpi"]["orders"] == 1


def test_empty_assignment_has_empty_scope_on_all_surfaces(db, seeded):
    client, _, _ = _client(db)
    snapshot = client.get("/api/authz/me").json()["sales_analysis"]
    assert snapshot["read"] is True and snapshot["customer_count"] == 0
    assert _get(client, "/orders")["total"] == 0
    assert _get(client, "/summary")["kpi"]["orders"] == 0
    assert _get(client, "/options")["business_types"] == []
    assert _get(client, "/options")["customers"] == []
    assert _get(client, "/groups", group_by="customer")["groups"] == []
    assert _get(client, "/amount-histogram")["total"] == 0
    assert client.get(f"{PREFIX}/orders/{seeded['a'][0].id}").status_code == 404


def test_explicit_all_scope_includes_unmatched_customers_without_role_shortcut(db, seeded):
    client, _, _ = _client(db, scope="all", role="readonly")
    assert _get(client, "/orders")["total"] == 6


def test_field_plan_overrides_admin_and_legacy_rbac_bypass(db, seeded, monkeypatch):
    client, _, _ = _client(db, scope="all", role="admin", fields=[])
    monkeypatch.setattr(config, "ENABLE_RBAC", False)
    data = _get(client, "/orders", sort="customer")
    assert data["sort"] == "order_date"
    assert all(row["customer"] is None and row["amount_ex_tax"] is None for row in data["items"])
    assert _get(client, "/options")["customers"] == []
    for path, params in (("/orders", {"customer_name": "A公司"}),
                         ("/groups", {"group_by": "customer"}),
                         ("/orders", {"amount_min": "1"}), ("/amount-histogram", {})):
        assert client.get(PREFIX + path, params=params).status_code == 403
    detail = _get(client, f"/orders/{seeded['a'][0].id}")
    assert detail["reconciliation_history"] == []
    assert all(row["unit_price"] is None and row["qty"] is None for row in detail["lines"])
    response = client.get(PREFIX + "/orders/export")
    header = next(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert "客户" not in header and "毛利" not in header and "不含税金额" not in header


def test_export_is_separate_capability_and_pdp_failure_never_falls_back(db, seeded, monkeypatch):
    client, _, _ = _client(db, scope="all", role="admin", actions=("read",))
    assert _get(client, "/orders")["total"] == 6
    assert client.get(PREFIX + "/orders/export").status_code == 403
    monkeypatch.setattr(get_settings(), "cerbos_url", "http://127.0.0.1:1")
    for path in ("/orders", "/options", "/summary", f"/orders/{seeded['a'][0].id}"):
        response = client.get(PREFIX + path)
        assert response.status_code == 503, response.text


def test_new_assignment_replaces_scope_and_revocation_invalidates_old_token(db, seeded):
    client, user, grant = _client(db, customer_ids=[seeded["a"][2].customer_id])
    admin, _, _ = _client(db, username="authz-admin", role="admin", scope="all")
    payload = {"expected_version": 1, "actions": ["read"], "scope": "assigned_customers",
               "field_permissions": FIELDS, "customer_ids": [seeded["a"][0].customer_id]}
    saved = admin.put("/api/authz/users/service-sales", json=payload)
    assert saved.status_code == 200, saved.text
    assert client.get(PREFIX + "/orders").status_code in (401, 403)
    login = client.post("/api/auth/login", json={"username": user.username, "password": PASSWORD})
    client.headers["Authorization"] = "Bearer " + login.json()["token"]
    assert [row["order_no"] for row in _get(client, "/orders")["items"]] == ["SO-A1"]
    assert client.get(PREFIX + "/orders/export").status_code == 403


@pytest.mark.parametrize("path,params", [
    ("/options", {}), ("/summary", {}), ("/orders", {}),
    ("/groups", {"group_by": "customer"}), ("/trend", {}),
    ("/amount-histogram", {}), ("/orders/export", {}), ("/orders/1", {}),
])
def test_every_migrated_entry_rejects_revoked_grant_including_admin(db, path, params):
    client, _, _ = _client(db, scope="all", role="admin", actions=())
    assert client.get(PREFIX + path, params=params).status_code == 403


def test_sales_endpoint_inventory_requires_an_explicit_coverage_update():
    # New endpoints must join the authorization acceptance matrix, rather than
    # quietly inheriting a navigation-only gate and skipping SQL scope tests.
    actual = {(method, route.path) for route in app.routes
              if getattr(route, "path", "").startswith(PREFIX + "/")
              for method in getattr(route, "methods", [])}
    expected = {("GET", PREFIX + suffix) for suffix in (
        "/options", "/summary", "/orders", "/groups", "/trend",
        "/amount-histogram", "/orders/export", "/orders/{order_id}",
    )}
    assert actual == expected
