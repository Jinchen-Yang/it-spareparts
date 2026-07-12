"""复审 P0-1：老板看板/池分析的新派生成本键必须过字段脱敏——
自定义角色 page_boss_board=True 但 data_purchase_cost=False 时，采购成本相关字段一律遮成 null。"""
from datetime import date

import pytest
from sqlalchemy import select

from app import permissions, security
from app.etl import loader
from app.models.dimensions import DimPart
from app.models.inventory import PartSubstitute
from app.models.system import SysImportBatch
from app.services import dashboard, pool, pool_catalog, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


def _ctx(**over):
    """自定义权限：默认全开，再按 over 关某些 data_*。"""
    perms = permissions._full()
    perms.update(over)
    return security.UserContext(user_id="u", role="custom", permissions=perms, is_authenticated=True)


@pytest.fixture()
def seeded(db):
    x = DimPart(pn_std="PN-X", brand="BX"); y = DimPart(pn_std="PN-Y", brand="BY")
    db.add_all([x, y]); db.flush()
    db.add(PartSubstitute(part_id_a=min(x.id, y.id), part_id_b=max(x.id, y.id),
                          status="active", direction="both", substitute_type="same_spec"))
    db.flush()
    # 人工池是唯一真值（Slice 1）：经 pool_catalog 建池，不再自动重算
    pool_catalog.create_pool(db, name="池-XY", member_part_ids=[x.id, y.id], operated_by="t")
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hmask")
    db.add(b); db.flush()
    po = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
          "P2": f.purchase_head("P2", on=date(2026, 1, 9), is_tax_inclusive=True)}
    pl = [f.purchase_line("P1", "PLX1", "PN-X", qty="5", price="113"),
          f.purchase_line("P2", "PLX2", "PN-X", qty="5", price="113"),
          f.purchase_line("P1", "PLY", "PN-Y", qty="5", price="226")]
    loader.load(db, f.purchase_result(po, pl), b.id, date(2026, 6, 1))
    so = {"S1": f.sales_head("S1", on=date(2026, 2, 1))}
    sl = [f.sales_line("S1", "SLY", "PN-Y", qty="10", price="400"),
          f.sales_line("S1", "SLX", "PN-X", qty="2", price="400")]
    loader.load(db, f.sales_result(so, sl), b.id, date(2026, 6, 1))
    db.commit(); profit.recompute(db)
    gid = db.execute(select(pool.PartPoolMember.group_id)).scalar()
    return gid


def test_kpi_masks_purchase_and_profit(db, seeded):
    ctx = _ctx(data_purchase_cost=False, data_profit=False)
    k = security.apply_field_visibility(dashboard.kpi(db, None, None, as_of=AS_OF, user_ctx=ctx), ctx)
    assert k["purchase_ex_tax"] is None        # 采购成本遮
    assert k["gross_profit"] is None           # 毛利遮（data_profit=False）
    # 销售额不是成本，仍可见
    assert k["sales_ex_tax"] is not None


def test_ranking_masks_purchase_price(db, seeded):
    ctx = _ctx(data_purchase_cost=False)
    r = security.apply_field_visibility(
        dashboard.part_ranking(db, None, None, as_of=AS_OF, user_ctx=ctx), ctx)
    assert r["profit_restricted"] is False     # 只关采购成本、未关利润 → 分类仍给
    for row in r["profitable"] + r["loss"]:
        assert row["purchase_price"] is None   # 采购价统计容器遮
        assert row["revenue"] is not None      # 营收仍可见


def test_ranking_profit_restricted_hides_classification(db, seeded):
    """复审三轮 P0-1：data_profit=false 时，连"哪些型号赚/亏、各几个"都不能返回
    （字段 mask 只置空金额，型号落在哪个榜 + 榜内计数本身泄漏利润结论）。"""
    ctx = _ctx(data_profit=False)
    r = security.apply_field_visibility(
        dashboard.part_ranking(db, None, None, as_of=AS_OF, user_ctx=ctx), ctx)
    assert r["profit_restricted"] is True
    assert r["profitable"] == [] and r["loss"] == []      # 无型号归属
    assert r["counts"]["profitable"] is None              # 无赚/亏计数
    assert r["counts"]["loss"] is None
    # 非利润的数据质量计数仍可给（不泄漏盈亏归属）
    assert r["counts"]["total_parts"] is not None


def test_pool_masks_cost_benchmark_savings(db, seeded):
    ctx = _ctx(data_purchase_cost=False)
    d = security.apply_field_visibility(pool.analyze(db, seeded, as_of=AS_OF, user_ctx=ctx), ctx)
    assert d["benchmark"] is None              # 含成本标杆 → 整块遮
    assert d["savings"] is None                # 含节省额 → 整块遮
    for m in d["members"]:
        assert m["purchase_price"] is None     # 成员采购成本遮
        assert m["purchase_premium_pct"] is None
    assert d["demand"] is not None             # 需求量非成本，可见


def test_boss_sees_everything(db, seeded):
    """老板 data_purchase_cost=True → 完全不遮。"""
    ctx = _ctx()
    d = security.apply_field_visibility(pool.analyze(db, seeded, as_of=AS_OF, user_ctx=ctx), ctx)
    assert d["benchmark"] is not None and d["savings"] is not None


# ── 复审二轮 P0：订单拉通端点的 total_ 前缀派生键此前漏登记 → 端点级脱敏回归 ──

def test_purchase_orders_masks_total_ex_tax(db, seeded):
    """采购订单一单一行的 total_ex_tax 是采购额，data_purchase_cost=False 必须遮。"""
    ctx = _ctx(data_purchase_cost=False)
    d = security.apply_field_visibility(
        dashboard.purchase_orders(db, as_of=AS_OF, status="全部", user_ctx=ctx), ctx)
    assert d["items"], "seeded 应有采购订单"
    for row in d["items"]:
        assert row["total_ex_tax"] is None       # 采购额遮
        assert row["total_qty"] is not None       # 数量非成本，可见
        assert row["order_no"] is not None


def test_sales_orders_masks_total_gross_profit(db, seeded):
    """销售订单一单一行的 total_gross_profit 是毛利，data_profit=False 必须遮；营收仍可见。"""
    ctx = _ctx(data_profit=False)
    d = security.apply_field_visibility(
        dashboard.sales_orders(db, as_of=AS_OF, status="全部", user_ctx=ctx), ctx)
    assert d["items"], "seeded 应有销售订单"
    for row in d["items"]:
        assert row["total_gross_profit"] is None  # 毛利遮
        assert row["total_revenue"] is not None    # 营收非成本，可见


def test_order_sort_by_hidden_field_falls_back_to_date(db):
    """复审三轮同类扩展：利润被脱敏的角色按 gross_profit 排序时，行序本身泄漏盈亏排名 →
    后端退回按日期排序。构造"日期序≠毛利序"的两单来区分。"""
    b = SysImportBatch(filename="s.xlsx", file_type="purchase", file_hash="hsortleak")
    db.add(b); db.flush()
    db.add_all([DimPart(pn_std="PN-SX", brand="BX"), DimPart(pn_std="PN-SY", brand="BY")])
    db.flush()
    po = {"P1": f.purchase_head("P1", on=date(2026, 1, 1), is_tax_inclusive=True)}
    pl = [f.purchase_line("P1", "LX", "PN-SX", qty="10", price="113"),   # ex100
          f.purchase_line("P1", "LY", "PN-SY", qty="10", price="226")]   # ex200
    loader.load(db, f.purchase_result(po, pl), b.id, date(2026, 6, 1))
    so = {"OA": f.sales_head("OA", order_no="OA", on=date(2026, 1, 10)),   # 早日期、高毛利
          "OB": f.sales_head("OB", order_no="OB", on=date(2026, 2, 10))}   # 晚日期、低毛利
    sl = [f.sales_line("OA", "SA", "PN-SX", qty="1", price="400"),   # 毛利高
          f.sales_line("OB", "SB", "PN-SY", qty="1", price="226")]   # 毛利≈0
    loader.load(db, f.sales_result(so, sl), b.id, date(2026, 6, 1))
    db.commit(); profit.recompute(db)

    def order(ctx, sort):
        return [i["order_no"] for i in dashboard.sales_orders(
            db, status="全部", sort=sort, order="desc", as_of=AS_OF, user_ctx=ctx)["items"]][:2]

    full = _ctx()
    assert order(full, "gross_profit") == ["OA", "OB"]   # 毛利降序：高毛利在前
    assert order(full, "order_date") == ["OB", "OA"]      # 日期降序：晚单在前
    # 无利润权限请求按毛利排序 → 退回日期序（≠毛利序），不泄漏盈亏排名
    limited = dashboard.sales_orders(
        db, status="全部", sort="gross_profit", order="desc", as_of=AS_OF,
        user_ctx=_ctx(data_profit=False),
    )
    assert [i["order_no"] for i in limited["items"]] == ["OB", "OA"]
    assert limited["effective_sort"] == "order_date"
    assert limited["ranking_restricted"] is True
    assert limited["profit_restricted"] is True
    masked = security.apply_field_visibility(limited, _ctx(data_profit=False))
    assert all(i["total_gross_profit"] is None for i in masked["items"])


def test_purchase_order_sort_response_explains_cost_restriction(db, seeded):
    limited = dashboard.purchase_orders(
        db, status="全部", sort="amount", order="desc", as_of=AS_OF,
        user_ctx=_ctx(data_purchase_cost=False),
    )
    assert limited["effective_sort"] == "order_date"
    assert limited["ranking_restricted"] is True
    assert limited["cost_restricted"] is True


def test_pool_masks_brand_premium_purchase(db, seeded):
    """池成员 brand_premium_purchase（采购溢价判定）反推采购成本比较，必须遮。"""
    ctx = _ctx(data_purchase_cost=False)
    d = security.apply_field_visibility(pool.analyze(db, seeded, as_of=AS_OF, user_ctx=ctx), ctx)
    for m in d["members"]:
        assert m["brand_premium_purchase"] is None


# ── 系统性护栏：穷举所有面板端点，凡键名暗示"成本/毛利金额/采购溢价"者，cost-blind 角色一律 null ──
# 复审两次都在"新加端点又漏登记同类键"上翻车，本测试按语义扫全部端点键，杜绝下次再漏。
# 命中词刻意收窄到"金额/判定"语义，绕开营收(revenue_costed)、覆盖率(cost_coverage)、
# 计数(no_cost/with_cost/profitable)、方法名(cost_method)这些含 cost/profit 子串但非成本值的键。
_COST_HINT = ("gross_profit", "premium_purchase", "purchase_price", "benchmark", "saving",
              "purchase_ex_tax", "total_ex_tax", "cost_ex_tax", "unit_price_ex_tax")


def _leaky_keys(obj, path=""):
    """递归找出"键名暗示成本金额/毛利/采购溢价"且值非 None 的标量路径。"""
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if (any(h in kl for h in _COST_HINT)
                    and v is not None and not isinstance(v, (dict, list))):
                bad.append(f"{path}.{k}={v!r}")
            bad += _leaky_keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad += _leaky_keys(v, f"{path}[{i}]")
    return bad


def test_no_cost_key_leaks_across_all_panels(db, seeded):
    """cost-blind（采购成本+毛利全关）角色跑遍所有看板/池端点，任何成本语义键都不得漏。"""
    ctx = _ctx(data_purchase_cost=False, data_profit=False)
    av = lambda d: security.apply_field_visibility(d, ctx)  # noqa: E731
    panels = {
        "kpi": av(dashboard.kpi(db, None, None, as_of=AS_OF, user_ctx=ctx)),
        "ranking": av(dashboard.part_ranking(db, None, None, as_of=AS_OF, user_ctx=ctx)),
        "sales_orders": av(dashboard.sales_orders(db, as_of=AS_OF, status="全部", user_ctx=ctx)),
        "purchase_orders": av(dashboard.purchase_orders(db, as_of=AS_OF, status="全部", user_ctx=ctx)),
        "pool": av(pool.analyze(db, seeded, as_of=AS_OF, user_ctx=ctx)),
        "pools": av(pool.list_pools(db, as_of=AS_OF)),
    }
    leaks = {name: _leaky_keys(d) for name, d in panels.items()}
    leaks = {n: v for n, v in leaks.items() if v}
    assert not leaks, f"成本语义键泄漏：{leaks}"
