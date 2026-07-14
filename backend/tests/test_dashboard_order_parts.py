"""看板 v2（验收 1/2/3）：订单嵌套 parts 完整返回、分页按订单计、查询数不随订单数增长。"""
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event

from app import permissions, security
from app.db import engine
from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import dashboard, pool_catalog, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


def _ctx(**over):
    perms = permissions._full()
    perms.update(over)
    return security.UserContext(user_id="u", role="custom", permissions=perms,
                                is_authenticated=True)


@contextmanager
def count_queries():
    """统计底层 cursor 执行次数（N+1 证据用）。"""
    counter = {"n": 0}

    def _before(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _before)


@pytest.fixture()
def seeded(db):
    """PN-A/PN-B 成池（采购上限 ex150 / 销售下限 ex180），PN-C 无池。
    采购：P1 A×10@ex100、P2 A×10@ex200（越上限）。
    销售：S1 = A×2@ex400 + C×1@ex100（多 PN 单）；S2 = A×1@ex100（越下限）。"""
    a = DimPart(pn_std="PN-A", brand="BA")
    bpart = DimPart(pn_std="PN-B", brand="BB")
    c = DimPart(pn_std="PN-C", brand="BC")
    db.add_all([a, bpart, c]); db.flush()
    created = pool_catalog.create_pool(db, name="池-AB", member_part_ids=[a.id, bpart.id],
                                       operated_by="t")
    gid = created["group_id"]
    pool_catalog.set_price_policy(db, group_id=gid, version=1,
                                  purchase_value=Decimal("150"), purchase_basis="ex_tax",
                                  sales_value=Decimal("180"), sales_basis="ex_tax",
                                  operated_by="t")
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hparts")
    db.add(b); db.flush()
    po = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
          "P2": f.purchase_head("P2", on=date(2026, 1, 10), is_tax_inclusive=True)}
    pl = [f.purchase_line("P1", "PL1", "PN-A", qty="10", price="113"),
          f.purchase_line("P2", "PL2", "PN-A", qty="10", price="226")]
    loader.load(db, f.purchase_result(po, pl), b.id, AS_OF)
    so = {"S1": f.sales_head("S1", on=date(2026, 2, 1)),
          "S2": f.sales_head("S2", on=date(2026, 2, 10))}
    sl = [f.sales_line("S1", "SL1", "PN-A", qty="2", price="452"),
          f.sales_line("S1", "SL2", "PN-C", qty="1", price="113"),
          f.sales_line("S2", "SL3", "PN-A", qty="1", price="113")]
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)
    return gid


def test_multi_pn_order_returns_full_parts(db, seeded):
    """验收 1：一个订单多个 PN → parts 完整列表 + 概要字段。"""
    d = dashboard.sales_orders(db, status="全部", as_of=AS_OF)
    by = {i["order_no"]: i for i in d["items"]}
    s1 = by["S1"]
    assert s1["pn_count"] == 2 and len(s1["parts"]) == 2
    assert {p["pn_std"] for p in s1["parts"]} == {"PN-A", "PN-C"}
    assert set(s1["pn_preview"]) == {"PN-A", "PN-C"}
    assert s1["total_quantity"] == 3.0
    assert s1["total_amount"] == s1["total_revenue"] == 900.0   # 2*400 + 1*100
    assert s1["occurred_date"] == "2026-02-01"
    pa = next(p for p in s1["parts"] if p["pn_std"] == "PN-A")
    assert pa["part_id"] and pa["brand"] == "BA"
    assert pa["quantity"] == 2.0 and pa["unit_price_ex_tax"] == 400.0 and pa["amount"] == 800.0
    assert pa["pool_group_id"] == seeded and pa["pool_name"] == "池-AB"
    pc = next(p for p in s1["parts"] if p["pn_std"] == "PN-C")
    assert pc["pool_group_id"] is None and pc["reference_status"] == "no_pool"


def test_purchase_order_parts(db, seeded):
    d = dashboard.purchase_orders(db, status="全部", as_of=AS_OF)
    by = {i["order_no"]: i for i in d["items"]}
    p1 = by["P1"]
    assert p1["pn_count"] == 1 and len(p1["parts"]) == 1
    part = p1["parts"][0]
    assert part["unit_price_ex_tax"] == 100.0 and part["amount"] == 1000.0
    assert part["pool_group_id"] == seeded
    assert p1["total_amount"] == p1["total_ex_tax"] == 1000.0


def test_pagination_counts_orders_not_lines(db, seeded):
    """验收 2：total 按订单数（S1 有 2 行仍算 1 单）。"""
    d = dashboard.sales_orders(db, status="全部", as_of=AS_OF, page=1, page_size=1)
    assert d["total"] == 2
    assert len(d["items"]) == 1
    assert len(d["items"][0]["parts"]) >= 1   # 分页后 parts 只装配当页


def test_query_count_constant_as_orders_grow(db, seeded):
    """验收 3：查询数不随订单数线性增长（2 单 vs 20+ 单严格相等）。"""
    with count_queries() as c1:
        dashboard.sales_orders(db, status="全部", as_of=AS_OF, page_size=200)
    n_small = c1["n"]
    b = SysImportBatch(filename="t2.xlsx", file_type="sales", file_hash="hgrow")
    db.add(b); db.flush()
    so = {f"G{i}": f.sales_head(f"G{i}", on=date(2026, 3, 1)) for i in range(20)}
    sl = [f.sales_line(f"G{i}", f"GL{i}", "PN-A", qty="1", price="226") for i in range(20)]
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)
    with count_queries() as c2:
        d = dashboard.sales_orders(db, status="全部", as_of=AS_OF, page_size=200)
    assert d["total"] == 22
    assert c2["n"] == n_small, f"订单增多后查询数变化：{n_small} → {c2['n']}（疑似 N+1）"


def test_purchase_query_count_constant(db, seeded):
    with count_queries() as c1:
        dashboard.purchase_orders(db, status="全部", as_of=AS_OF, page_size=200)
    n_small = c1["n"]
    b = SysImportBatch(filename="t3.xlsx", file_type="purchase", file_hash="hgrow2")
    db.add(b); db.flush()
    po = {f"GP{i}": f.purchase_head(f"GP{i}", on=date(2026, 3, 2), is_tax_inclusive=True)
          for i in range(20)}
    pl = [f.purchase_line(f"GP{i}", f"GPL{i}", "PN-A", qty="1", price="113") for i in range(20)]
    loader.load(db, f.purchase_result(po, pl), b.id, AS_OF)
    db.commit()
    with count_queries() as c2:
        d = dashboard.purchase_orders(db, status="全部", as_of=AS_OF, page_size=200)
    assert d["total"] == 22
    assert c2["n"] == n_small, f"订单增多后查询数变化：{n_small} → {c2['n']}（疑似 N+1）"


def test_scoped_sales_gets_no_parts(db, seeded):
    """受限销售（own_customers_only）：逐单成交明细整段不可见 → parts 空 + 旗标；
    订单头的 客户/销售员（"某单卖给谁、谁卖的"逐单归属）同样置空（审计 P1）。"""
    ctx = _ctx(own_customers_only=True)
    d = dashboard.sales_orders(db, status="全部", as_of=AS_OF, user_ctx=ctx)
    assert d["parts_restricted"] is True
    for it in d["items"]:
        assert it["parts"] == [] and it["pn_preview"] == []
        assert it["customer"] is None and it["salesperson"] is None
