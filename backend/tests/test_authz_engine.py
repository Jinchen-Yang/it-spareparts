from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from cerbos.sdk.model import Resource, ResourceList
from sqlalchemy import Integer, column, func, select, table, text

from app.authz import engine
from app.authz.catalog import SALES_FIELD_PERMISSIONS
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def configured_engine(monkeypatch, cerbos_url):
    monkeypatch.setattr(engine, "get_settings", lambda: SimpleNamespace(
        cerbos_url=cerbos_url, cerbos_timeout_seconds=2.0,
    ))


@pytest.fixture
def orders(db):
    db.execute(text("CREATE TEMP TABLE authz_engine_orders (id integer, customer_id bigint, amount integer) ON COMMIT DROP"))
    db.execute(text("INSERT INTO authz_engine_orders VALUES (1, 11, 100), (2, 22, 200), (3, NULL, 300)"))
    return table("authz_engine_orders", column("id", Integer), column("customer_id", Integer), column("amount", Integer))


@pytest.mark.parametrize("scope,customers,expected,total", [
    ("assigned_customers", [11], [1], 100),
    ("assigned_customers", [11, 22], [1, 2], 300),
    ("assigned_customers", [], [], 0),
    ("all", [], [1, 2, 3], 600),
])
def test_real_plan_matches_checks_and_pg_aggregate(db, orders, configured_engine, scope, customers, expected, total):
    grant = engine.SalesGrant(("read", "export"), scope, ("data_customer",), 1)
    decision = engine.check_sales(7, grant, customers)
    assert decision.allowed_actions == {"read", "export"}
    assert decision.field_permissions == {"data_customer"}
    plan = engine.plan_sales(7, grant, customers, "read", orders, orders.c.customer_id)
    actual = list(db.scalars(select(orders.c.id).where(plan.predicate).order_by(orders.c.id)))
    assert actual == expected
    assert db.scalar(select(func.coalesce(func.sum(orders.c.amount), 0)).where(plan.predicate)) == total
    with engine._client() as client:
        principal = engine._principal(7, grant, customers)
        checked = []
        for ident, customer in [(1, "11"), (2, "22"), (3, None)]:
            if client.is_allowed("read", principal, Resource(
                id=str(ident), kind="sales_analysis", attr={"module_probe": False, "customer_id": customer},
            )):
                checked.append(ident)
        assert checked == actual


def test_pdp_denial_does_not_fall_back_to_grant(configured_engine, orders, monkeypatch):
    grant = engine.SalesGrant(("read",), "all", tuple(sorted(SALES_FIELD_PERMISSIONS)), 1)
    assert engine.check_sales(1, grant, []).allowed_actions == {"read"}
    with pytest.raises(engine.AuthorizationDenied):
        engine.plan_sales(1, grant, [], "export", orders, orders.c.customer_id)
    revoked = replace(grant, actions=())
    assert not engine.check_sales(1, revoked, []).allowed_actions
    with pytest.raises(engine.AuthorizationDenied):
        engine.plan_sales(1, revoked, [], "read", orders, orders.c.customer_id)


@pytest.mark.parametrize("changes", [
    {"actions": ("delete",)}, {"actions": ("export",)}, {"scope": "self"},
    {"field_permissions": ("data_unknown",)}, {"field_permissions": ("data_profit",)}, {"field_permissions": ("data_reconciliation",)},
    {"version": 0},
])
def test_unknown_grant_vocabulary_is_rejected(changes):
    base = {"actions": ("read",), "scope": "all", "field_permissions": (), "version": 1}
    with pytest.raises(ValueError):
        engine.SalesGrant(**(base | changes))


def test_unknown_action_never_contacts_pdp(monkeypatch):
    monkeypatch.setattr(engine, "_client", lambda: pytest.fail("must reject before contacting PDP"))
    with pytest.raises(engine.AuthorizationDenied):
        engine.plan_sales(1, engine.SalesGrant(("read",), "all", (), 1), [], "delete", None, None)


def test_network_error_and_adapter_error_are_unavailable(configured_engine, orders, monkeypatch):
    grant = engine.SalesGrant(("read",), "all", (), 1)
    def bad_adapter(*args, **kwargs):
        raise ValueError("unsupported plan")
    monkeypatch.setattr(engine, "get_query", bad_adapter)
    with pytest.raises(engine.AuthorizationUnavailable):
        engine.plan_sales(1, grant, [], "read", orders, orders.c.customer_id)
    def offline():
        raise httpx.ConnectError("PDP unavailable")
    monkeypatch.setattr(engine, "_client", offline)
    with pytest.raises(engine.AuthorizationUnavailable):
        engine.check_sales(1, grant, [])


def test_malformed_field_output_is_not_treated_as_allow(configured_engine, monkeypatch):
    grant = engine.SalesGrant(("read",), "all", ("data_customer",), 1)
    with engine._client() as client:
        principal = engine._principal(1, grant, [])
        real = client.check_resources(principal, ResourceList().add(
            Resource(id="sales-analysis-module", kind="sales_analysis", attr={"module_probe": True}), {"read"},
        ))
        real.results[0].outputs[0].val["field_permissions"] = ["data_unknown"]
        monkeypatch.setattr(client, "check_resources", lambda *args, **kwargs: real)
        with pytest.raises(ValueError):
            engine._check(client, principal, grant, ("read",))


def test_generic_registered_resource_plan_matches_sales_wrapper(db, orders, configured_engine):
    grant = engine.Grant(("read",), "assigned_customers", ("data_customer",), 1, resource="sales_analysis")
    decision = engine.check_access(7, grant, {"customer_ids": ["11"]}, ("read",))
    assert decision.allowed_actions == {"read"}
    plan = engine.plan_access(7, grant, {"customer_ids": ["11"]}, "read", engine.sales_bindings(orders, orders.c.customer_id))
    assert list(db.scalars(select(orders.c.id).where(plan.predicate))) == [1]


def test_unregistered_resource_cannot_enter_generic_engine():
    with pytest.raises(ValueError, match="资源未注册"):
        engine.Grant(("read",), "all", (), 1, resource="inventory")


def test_query_bindings_reject_unregistered_attribute():
    with pytest.raises(ValueError, match="查询属性"):
        engine.QueryBindings("sales_analysis", None, {"request.resource.attr.unregistered": None})


def test_grant_cannot_be_overwritten_by_module_attributes(monkeypatch):
    monkeypatch.setattr(engine, "_client", lambda: pytest.fail("invalid principal reached PDP"))
    with pytest.raises(engine.AuthorizationUnavailable):
        engine.check_access(1, engine.SalesGrant(("read",), "all", (), 1), {"grant": {}}, ("read",))
