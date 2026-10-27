"""售前客户与供应商候选的权限、行级范围和搜索契约。"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config, permissions
from app.auth import _make_token
from app.main import app
from app.models.dimensions import DimCustomer, DimSupplier
from app.models.presales import PresalesInquiry
from app.models.sales import FSalesOrder
from app.models.system import SysImportBatch, SysUser
from app.security import apply_field_visibility
from app.services.presales import candidates, common


def _client(user):
    token, _ = _make_token(user.role, user.username, user.display_name, authn="sys_user")
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + token
    return client


@pytest.fixture()
def choices(db):
    users = {}

    def add_user(name, role, salesperson_name=None, **changes):
        graph = permissions.effective(role, None)
        graph.update(changes)
        row = SysUser(username=f"candidate_{name}", role=role, display_name=name,
                      salesperson_name=salesperson_name, password_hash="测试不使用口令",
                      template_perms=graph, perm_overrides={})
        db.add(row)
        users[name] = row

    add_user("seller", "sales", "销售甲")
    add_user("other", "sales", "销售乙")
    add_user("unnamed", "sales")
    add_user("unrestricted", "sales", own_customers_only=False)
    add_user("buyer", "purchaser")
    add_user("quote_only", "purchaser", action_presales_offer=False)
    add_user("offer_only", "purchaser", action_presales_quote=False)
    add_user("no_actions", "purchaser", action_presales_quote=False, action_presales_offer=False)
    add_user("no_supplier", "purchaser", data_supplier=False)
    add_user("no_customer", "sales", data_customer=False)
    add_user("no_page", "sales", page_presales=False)
    add_user("no_inquire", "sales", action_presales_inquire=False)
    add_user("boss", "boss")
    customers = [
        DimCustomer(name_raw="客户-A 原始", name_normalized="Alpha 标准客户"),
        DimCustomer(name_raw="客户-B 私有询价"),
        DimCustomer(name_raw="客户-C 他人"),
        DimCustomer(name_raw="客户-D 空销售"),
        DimCustomer(name_raw="客户 100%_档案"),
    ]
    suppliers = [
        DimSupplier(name_raw="Supplier Raw Alpha", name_normalized="供应商 标准"),
        DimSupplier(name_raw="普通供应商"),
        DimSupplier(name_raw="供应商 100%_档案"),
    ]
    batch = SysImportBatch(filename="候选测试.xlsx", file_type="sales", file_hash="c" * 64)
    db.add_all([*customers, *suppliers, batch])
    db.flush()
    for index, (customer, salesperson) in enumerate((
        (customers[0], "销售甲"), (customers[2], "销售乙"),
        (customers[3], None), (customers[3], ""),
    )):
        db.add(FSalesOrder(raw_order_id=f"candidate-{index}", order_no=f"C-{index}",
                           customer_id=customer.id, salesperson=salesperson,
                           import_batch_id=batch.id))
    for index, (customer, owner) in enumerate((
        (customers[0], users["seller"]), (customers[1], users["seller"]),
        (customers[2], users["other"]), (customers[1], users["unnamed"]),
    )):
        db.add(PresalesInquiry(kind="sales_inquiry", inquiry_no=f"CAND{index}",
                               customer_id=customer.id, created_by=owner.id))
    db.commit()
    return SimpleNamespace(**users, customers=customers, suppliers=suppliers)


def test_customer_candidates_are_scoped_to_sales_history_or_own_inquiry(db, choices):
    response = _client(choices.seller).get("/api/presales/customers")
    assert response.status_code == 200, response.text
    assert response.json() == [
        {"id": choices.customers[0].id, "name": "Alpha 标准客户"},
        {"id": choices.customers[1].id, "name": "客户-B 私有询价"},
    ]
    other = _client(choices.other).get("/api/presales/customers").json()
    assert other == [{"id": choices.customers[2].id, "name": "客户-C 他人"}]
    assert _client(choices.seller).get("/api/presales/customers", params={"q": "他人"}).json() == []


def test_customer_scope_without_sales_name_uses_only_own_inquiry(db, choices):
    response = _client(choices.unnamed).get("/api/presales/customers")
    assert response.status_code == 200, response.text
    assert response.json() == [{"id": choices.customers[1].id, "name": "客户-B 私有询价"}]


@pytest.mark.parametrize("viewer", ["unrestricted", "boss"])
def test_customer_candidates_without_scope_can_search_all_dimensions(db, choices, viewer):
    response = _client(getattr(choices, viewer)).get("/api/presales/customers")
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()} == {row.id for row in choices.customers}


@pytest.mark.parametrize("viewer", ["buyer", "no_page", "no_inquire", "no_customer"])
def test_customer_candidates_require_page_inquire_and_customer_permissions(db, choices, viewer):
    response = _client(getattr(choices, viewer)).get("/api/presales/customers")
    assert response.status_code == 403, response.text


def test_customer_search_matches_raw_and_normalized_and_limits_after_scope(db, choices):
    client = _client(choices.seller)
    expected = [{"id": choices.customers[0].id, "name": "Alpha 标准客户"}]
    for query in ("原始", "aLPHa", " 标准 "):
        response = client.get("/api/presales/customers", params={"q": query, "limit": 1})
        assert response.status_code == 200 and response.json() == expected, response.text
    assert len(client.get("/api/presales/customers", params={"limit": 1}).json()) == 1
    literal = _client(choices.unrestricted).get("/api/presales/customers", params={"q": "%_"})
    assert literal.json() == [{"id": choices.customers[4].id, "name": "客户 100%_档案"}]


@pytest.mark.parametrize("viewer", ["buyer", "quote_only", "offer_only"])
def test_supplier_candidates_allow_either_quote_or_offer_permission(db, choices, viewer):
    response = _client(getattr(choices, viewer)).get("/api/presales/suppliers")
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()} == {row.id for row in choices.suppliers}
    assert all(set(row) == {"id", "name"} for row in response.json())


@pytest.mark.parametrize("viewer", ["seller", "no_actions", "no_supplier", "no_page"])
def test_supplier_candidates_deny_missing_action_page_or_data_permissions(db, choices, viewer):
    response = _client(getattr(choices, viewer)).get("/api/presales/suppliers")
    assert response.status_code == 403, response.text


def test_supplier_search_matches_raw_normalized_and_literal_wildcards(db, choices):
    client = _client(choices.buyer)
    expected = [{"id": choices.suppliers[0].id, "name": "供应商 标准"}]
    for query in ("raw alpha", " 标准 "):
        response = client.get("/api/presales/suppliers", params={"q": query, "limit": 1})
        assert response.status_code == 200 and response.json() == expected, response.text
    literal = client.get("/api/presales/suppliers", params={"q": "%_"})
    assert literal.json() == [{"id": choices.suppliers[2].id, "name": "供应商 100%_档案"}]


def test_supplier_data_gate_shares_field_visibility_rbac_switch(db, choices, monkeypatch):
    ctx = common.resolve_actor(db, choices.no_supplier.id)[1]
    assert apply_field_visibility({"supplier_id": choices.suppliers[0].id}, ctx)["supplier_id"] is None
    monkeypatch.setattr(config, "ENABLE_RBAC", False)
    assert apply_field_visibility({"supplier_id": choices.suppliers[0].id}, ctx)["supplier_id"] is not None
    assert {row["id"] for row in candidates.suppliers(db, ctx)} == {row.id for row in choices.suppliers}


def test_customer_data_gate_shares_field_visibility_rbac_switch(db, choices, monkeypatch):
    ctx = common.resolve_actor(db, choices.no_customer.id)[1]
    assert apply_field_visibility({"customer_id": choices.customers[0].id}, ctx)["customer_id"] is None
    monkeypatch.setattr(config, "ENABLE_RBAC", False)
    assert apply_field_visibility({"customer_id": choices.customers[0].id}, ctx)["customer_id"] is not None
    assert {row["id"] for row in candidates.customers(db, ctx)} == {row.id for row in choices.customers}


@pytest.mark.parametrize("path", ["customers", "suppliers"])
def test_candidate_endpoints_require_real_login_and_validate_query_bounds(db, choices, path):
    assert TestClient(app).get(f"/api/presales/{path}").status_code == 401
    user = choices.seller if path == "customers" else choices.buyer
    client = _client(user)
    for params in ({"limit": 0}, {"limit": 101}, {"q": "长" * 257}):
        response = client.get(f"/api/presales/{path}", params=params)
        assert response.status_code == 422, response.text
