"""通用号池降本分析：可靠标杆(去重订单≥2的最低加权均价)、双端品牌溢价、
两级节省(理论上限/供应层面上限，均待核实)、客户跨品牌集中度。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import permissions, security
from app.api import dashboard as dashboard_api
from app.db import get_db
from app.main import app
from app.etl import loader
from app.models.dimensions import DimPart
from app.models.inventory import PartSubstitute
from app.models.system import SysImportBatch
from app.services import pool, profit
from app import config
from tests import factories as f

AS_OF = date(2026, 6, 1)


def _ctx(**over):
    perms = permissions._full()
    perms.update(over)
    return security.UserContext(user_id="u", role="custom", permissions=perms, is_authenticated=True)


def _part(db, pn, brand):
    p = DimPart(pn_std=pn, brand=brand)
    db.add(p); db.flush()
    return p.id


def _edge(db, a, b):
    lo, hi = (a, b) if a < b else (b, a)
    db.add(PartSubstitute(part_id_a=lo, part_id_b=hi, status="active",
                          direction="both", substitute_type="same_spec"))


@pytest.fixture()
def seeded(db):
    x = _part(db, "PN-X", "BrandX")   # 性价比标杆：ex 100，2 张不同采购单→可靠
    y = _part(db, "PN-Y", "BrandY")   # 品牌溢价：ex 150（+50%）
    z = _part(db, "PN-Z", "BrandZ")   # ex 120（+20%，恰达阈值）
    _edge(db, x, y); _edge(db, y, z)
    db.flush()
    pool.rebuild(db)

    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hpool")
    db.add(b); db.flush()
    # PN-X 分两张单采购（去重订单=2→标杆可靠；同单两行不算稳定，复审 P1-6）
    porders = {
        "P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
        "P2": f.purchase_head("P2", on=date(2026, 1, 10), is_tax_inclusive=True),
    }
    plines = [
        f.purchase_line("P1", "PLX1", "PN-X", qty="5", price="113"),   # ex 100
        f.purchase_line("P2", "PLX2", "PN-X", qty="5", price="113"),   # 第二张单
        f.purchase_line("P1", "PLY", "PN-Y", qty="5", price="169.5"),  # ex 150
        f.purchase_line("P1", "PLZ", "PN-Z", qty="5", price="135.6"),  # ex 120
    ]
    loader.load(db, f.purchase_result(porders, plines), b.id, date(2026, 6, 1))
    sorders = {"S1": f.sales_head("S1", on=date(2026, 2, 1))}
    slines = [
        f.sales_line("S1", "SLY", "PN-Y", qty="10", price="300"),   # 买贵的品牌 10 个
        f.sales_line("S1", "SLZ", "PN-Z", qty="5", price="300"),
        f.sales_line("S1", "SLX", "PN-X", qty="2", price="300"),
    ]
    loader.load(db, f.sales_result(sorders, slines), b.id, date(2026, 6, 1))
    db.commit()
    profit.recompute(db)
    gid = db.execute(select(pool.PartPoolMember.group_id)
                     .where(pool.PartPoolMember.part_id == x)).scalar()
    return {"gid": gid, "x": x, "y": y, "z": z}


def test_benchmark_reliable_needs_distinct_orders(db, seeded):
    d = pool.analyze(db, seeded["gid"], as_of=AS_OF)
    assert d["benchmark"]["cost_part_id"] == seeded["x"]
    assert d["benchmark"]["cost_ex_tax"] == 100.0
    assert d["benchmark"]["low_confidence"] is False   # PN-X 2 张不同采购单
    assert d["benchmark"]["supply_ok"] is True


def test_dual_end_brand_premium(db, seeded):
    d = pool.analyze(db, seeded["gid"], as_of=AS_OF)
    by = {m["part_id"]: m for m in d["members"]}
    y = by[seeded["y"]]
    assert y["purchase_price"]["wavg"] == 150.0
    assert y["purchase_premium_pct"] == 0.5
    assert y["brand_premium_purchase"] is True
    z = by[seeded["z"]]
    assert z["purchase_premium_pct"] == 0.2
    assert z["brand_premium_purchase"] is True
    x = by[seeded["x"]]
    assert x["brand_premium_purchase"] is False


def test_savings_no_executable_only_pending(db, seeded):
    """复审 P0-3：绝无"可执行"金额；只有理论上限 + 供应层面上限，每条待核实。"""
    d = pool.analyze(db, seeded["gid"], as_of=AS_OF)
    s = d["savings"]
    assert s["theoretical_max"] == 600.0             # Y:500 + Z:100
    assert s["supply_available_upper"] == 600.0      # 标杆供应可得
    assert s["executable"] is None                   # 无可执行金额
    assert all(o["verification_status"] == "待核实" for o in s["opportunities"])
    assert "executable" not in s["opportunities"][0]  # 不再有布尔"可执行"
    assert s["opportunities"][0]["from_part_id"] == seeded["y"]


def test_customer_cross_brand(db, seeded):
    d = pool.analyze(db, seeded["gid"], as_of=AS_OF)
    cb = d["customer_cross_brand"]
    assert cb["restricted"] is False
    assert cb["multi_brand_customers"] == 1
    assert cb["customers"][0]["brand_count"] == 3


def test_supply_gate_rejects_single_order_benchmark(db):
    """复审二轮 P1-5：标杆只有 1 张采购单（单样本）→ 供应不可得，不计入 supply_available_upper。
    理论节省仍在（口径不变），但供应层面上限=0，机会条目标 supply_available=False。"""
    x = _part(db, "PN-SX", "BX"); y = _part(db, "PN-SY", "BY")
    _edge(db, x, y); db.flush(); pool.rebuild(db)
    b = SysImportBatch(filename="s.xlsx", file_type="purchase", file_hash="hsupply1")
    db.add(b); db.flush()
    # 标杆 PN-SX 只 1 张采购单 → orders=1 < POOL_SUPPLY_MIN_ORDERS(2) → 供应不稳
    porders = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True)}
    plines = [f.purchase_line("P1", "PLX", "PN-SX", qty="5", price="113"),    # ex 100
              f.purchase_line("P1", "PLY", "PN-SY", qty="5", price="169.5")]  # ex 150
    loader.load(db, f.purchase_result(porders, plines), b.id, date(2026, 6, 1))
    sorders = {"S1": f.sales_head("S1", on=date(2026, 2, 1))}
    slines = [f.sales_line("S1", "SLY", "PN-SY", qty="10", price="300")]
    loader.load(db, f.sales_result(sorders, slines), b.id, date(2026, 6, 1))
    db.commit(); profit.recompute(db)
    gid = db.execute(select(pool.PartPoolMember.group_id)
                     .where(pool.PartPoolMember.part_id == x)).scalar()
    d = pool.analyze(db, gid, as_of=AS_OF)
    assert d["benchmark"]["supply_ok"] is False            # 单样本 → 供应不稳
    assert d["savings"]["theoretical_max"] > 0             # 理论上仍有节省
    assert d["savings"]["supply_available_upper"] == 0.0   # 但供应层面上限为 0
    opp = d["savings"]["opportunities"][0]
    assert opp["supply_available"] is False and opp["block_reason"]


def test_supply_window_not_truncated_by_page_range(db):
    """复审三轮 P1：页面选近30天时，60/90天前的有效采购仍应满足365天供应证据。
    采购统计（标杆价+供应）用近一年窗口，不被页面窗口截断；销量仍按页面窗口。"""
    x = _part(db, "PN-WX", "BX"); y = _part(db, "PN-WY", "BY")
    _edge(db, x, y); db.flush(); pool.rebuild(db)
    b = SysImportBatch(filename="w.xlsx", file_type="purchase", file_hash="hwin1")
    db.add(b); db.flush()
    porders = {   # 标杆 X 两张单在 60/90 天前（页面近30天窗口之外，但在一年内）
        "P1": f.purchase_head("P1", on=date(2026, 4, 2), is_tax_inclusive=True),
        "P2": f.purchase_head("P2", on=date(2026, 3, 3), is_tax_inclusive=True),
    }
    plines = [f.purchase_line("P1", "LX1", "PN-WX", qty="5", price="113"),   # ex100
              f.purchase_line("P2", "LX2", "PN-WX", qty="5", price="113"),
              f.purchase_line("P1", "LY", "PN-WY", qty="5", price="169.5")]  # ex150 溢价
    loader.load(db, f.purchase_result(porders, plines), b.id, date(2026, 6, 1))
    sorders = {"S1": f.sales_head("S1", on=date(2026, 5, 15))}   # 销量落在页面窗口内
    slines = [f.sales_line("S1", "SLY", "PN-WY", qty="10", price="300")]
    loader.load(db, f.sales_result(sorders, slines), b.id, date(2026, 6, 1))
    db.commit(); profit.recompute(db)
    gid = db.execute(select(pool.PartPoolMember.group_id)
                     .where(pool.PartPoolMember.part_id == x)).scalar()
    # 页面近30天：date_from=2026-05-02 —— 两张采购单都在窗口外
    d = pool.analyze(db, gid, date_from=date(2026, 5, 2), date_to=date(2026, 6, 1), as_of=AS_OF)
    assert d["benchmark"]["cost_ex_tax"] == 100.0        # 标杆价来自窗口外采购，仍算得出
    assert d["benchmark"]["supply_ok"] is True            # 60/90天前×2单 → 一年内供应稳定
    assert d["savings"]["supply_available_upper"] > 0     # 供应可得 → 有上限
    assert d["supply_window"] == {
        "date_from": "2025-06-01", "date_to": "2026-06-01", "as_of": "2026-06-01"
    }

    historical = pool.analyze(
        db, gid, date_from=date(2025, 12, 1), date_to=date(2025, 12, 31), as_of=AS_OF
    )
    assert historical["benchmark"]["cost_ex_tax"] == 100.0
    assert historical["benchmark"]["supply_ok"] is True
    assert historical["demand"]["total_qty"] == 0.0
    assert historical["supply_window"] == d["supply_window"]


def test_list_pools_savings_global_ranking(db):
    """复审二轮 P1-4：小成员但高节省的池，sort=savings 必须排在大成员低节省池之前
    （旧实现先按成员数分页、只在页内排节省 → 高节省小池藏后页）。"""
    # 池A：3 成员、同价（无溢价）→ 节省≈0
    a1 = _part(db, "PN-A1", "B1"); a2 = _part(db, "PN-A2", "B2"); a3 = _part(db, "PN-A3", "B3")
    _edge(db, a1, a2); _edge(db, a2, a3)
    # 池B：2 成员、大溢价 + 有销量 → 高节省
    b1 = _part(db, "PN-B1", "B4"); b2 = _part(db, "PN-B2", "B5")
    _edge(db, b1, b2)
    db.flush(); pool.rebuild(db)
    bat = SysImportBatch(filename="r.xlsx", file_type="purchase", file_hash="hrank1")
    db.add(bat); db.flush()
    po = {"PA": f.purchase_head("PA", on=date(2026, 1, 5), is_tax_inclusive=True)}
    pl = [f.purchase_line("PA", "LA1", "PN-A1", qty="5", price="113"),   # 池A 全 ex100
          f.purchase_line("PA", "LA2", "PN-A2", qty="5", price="113"),
          f.purchase_line("PA", "LA3", "PN-A3", qty="5", price="113"),
          f.purchase_line("PA", "LB1", "PN-B1", qty="5", price="113"),   # 池B 标杆 ex100
          f.purchase_line("PA", "LB2", "PN-B2", qty="5", price="226")]   # 池B 溢价 ex200
    loader.load(db, f.purchase_result(po, pl), bat.id, date(2026, 6, 1))
    so = {"SB": f.sales_head("SB", on=date(2026, 2, 1))}
    sl = [f.sales_line("SB", "SB2", "PN-B2", qty="20", price="300")]     # 溢价件卖 20 → 高节省
    loader.load(db, f.sales_result(so, sl), bat.id, date(2026, 6, 1))
    db.commit(); profit.recompute(db)
    gid_a = db.execute(select(pool.PartPoolMember.group_id)
                       .where(pool.PartPoolMember.part_id == a1)).scalar()
    gid_b = db.execute(select(pool.PartPoolMember.group_id)
                       .where(pool.PartPoolMember.part_id == b1)).scalar()

    by_savings = pool.list_pools(db, as_of=AS_OF, sort="savings", page=1, page_size=1)
    assert by_savings["sort"] == "savings"
    assert by_savings["items"][0]["group_id"] == gid_b   # 高节省小池排全局第一
    by_members = pool.list_pools(db, as_of=AS_OF, sort="member_count", page=1, page_size=1)
    assert by_members["items"][0]["group_id"] == gid_a   # 成员多的池排第一（对照）


def test_list_pools_pagination_and_cap(db, monkeypatch):
    """复审三轮 P1/Standards：savings 排序下服务端分页可翻到任意页（>100 池同理）；
    超 POOL_RANK_ANALYZE_CAP 时退回成员数排序并置 ranking_capped=True。"""
    for i in range(5):   # 5 个池，各 2 成员 1 边
        a = _part(db, f"PN-P{i}A", f"B{i}A"); b2 = _part(db, f"PN-P{i}B", f"B{i}B")
        _edge(db, a, b2)
    db.flush(); pool.rebuild(db)
    r1 = pool.list_pools(db, as_of=AS_OF, sort="savings", page=1, page_size=2)
    r2 = pool.list_pools(db, as_of=AS_OF, sort="savings", page=2, page_size=2)
    r3 = pool.list_pools(db, as_of=AS_OF, sort="savings", page=3, page_size=2)
    assert r1["total"] == 5 and len(r1["items"]) == 2
    assert len(r3["items"]) == 1                          # 第 3 页可访问（末池）
    seen = {it["group_id"] for r in (r1, r2, r3) for it in r["items"]}
    assert len(seen) == 5                                 # 5 个池全部可达、不重不漏
    assert r1["ranking_capped"] is False
    # 超 cap → 退回成员数排序并标注
    monkeypatch.setattr(config, "POOL_RANK_ANALYZE_CAP", 2)
    rc = pool.list_pools(db, as_of=AS_OF, sort="savings", page=1, page_size=2)
    assert rc["ranking_capped"] is True and rc["sort"] == "member_count"


def test_pools_endpoint_restricts_savings_ranking_and_masks_values(db):
    """端点级：节省序与成员序不同，成本受限账号必须拿到成员序。"""
    a1 = _part(db, "PN-EA1", "BE1"); a2 = _part(db, "PN-EA2", "BE2"); a3 = _part(db, "PN-EA3", "BE3")
    _edge(db, a1, a2); _edge(db, a2, a3)
    b1 = _part(db, "PN-EB1", "BE4"); b2 = _part(db, "PN-EB2", "BE5")
    _edge(db, b1, b2)
    db.flush(); pool.rebuild(db)
    batch = SysImportBatch(filename="endpoint-rank.xlsx", file_type="purchase", file_hash="hrank-endpoint")
    db.add(batch); db.flush()
    purchase = {"PE": f.purchase_head("PE", on=date(2026, 1, 5), is_tax_inclusive=True)}
    purchase_lines = [
        f.purchase_line("PE", "LE1", "PN-EB1", qty="5", price="113"),
        f.purchase_line("PE", "LE2", "PN-EB2", qty="5", price="226"),
    ]
    loader.load(db, f.purchase_result(purchase, purchase_lines), batch.id, date(2026, 6, 1))
    sales = {"SE": f.sales_head("SE", on=date(2026, 2, 1))}
    loader.load(db, f.sales_result(
        sales, [f.sales_line("SE", "SLE2", "PN-EB2", qty="20", price="300")]
    ), batch.id, date(2026, 6, 1))
    db.commit(); profit.recompute(db)
    gid_a = db.execute(select(pool.PartPoolMember.group_id).where(pool.PartPoolMember.part_id == a1)).scalar()
    gid_b = db.execute(select(pool.PartPoolMember.group_id).where(pool.PartPoolMember.part_id == b1)).scalar()

    unrestricted = _ctx()
    restricted = _ctx(data_purchase_cost=False)
    original = dict(app.dependency_overrides)
    try:
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[dashboard_api.get_current_user_context] = lambda: unrestricted
        app.dependency_overrides[dashboard_api.current_role] = lambda: "boss"
        client = TestClient(app)
        full = client.get("/api/dashboard/pools", params={"sort": "savings", "page_size": 10})
        assert full.status_code == 200
        assert full.json()["effective_sort"] == "savings"
        assert full.json()["items"][0]["group_id"] == gid_b

        app.dependency_overrides[dashboard_api.get_current_user_context] = lambda: restricted
        limited = client.get("/api/dashboard/pools", params={"sort": "savings", "page_size": 10})
        assert limited.status_code == 200
        body = limited.json()
        assert body["ranking_restricted"] is True
        assert body["effective_sort"] == "member_count"
        assert body["items"][0]["group_id"] == gid_a
        assert body["items"][0]["group_id"] != gid_b
        assert all(item["theoretical_saving"] is None for item in body["items"])
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)
