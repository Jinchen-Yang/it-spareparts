"""看板 v2（验收 9）：嵌套派生字段递归脱敏系统性扫描。

两个受限维度分别穷举全部 v2 面板：
- cost-blind（data_purchase_cost=False + data_profit=False）：任何"成本/毛利金额/采购
  溢价/池采购均价/与均价差额"语义键都必须 null；reference_status 仍可见（任务要求）。
- governance-blind（data_pool_price_governance=False）：任何"约束价/越线计数/与约束价
  差额"语义键都必须 null，且状态降级为池均价口径（不出现 manual 字样状态）。
不得只保护顶层键——扫描递归进 parts/members/订单板块。"""
from datetime import date
from decimal import Decimal

import pytest

from app import permissions, security
from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import dashboard, pool, pool_catalog, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


def _ctx(**over):
    perms = permissions._full()
    perms.update(over)
    return security.UserContext(user_id="u", role="custom", permissions=perms,
                                is_authenticated=True)


@pytest.fixture()
def seeded(db):
    """带约束价与双侧越线的池——所有敏感派生键都有非空值可泄漏，扫描才有效。"""
    a = DimPart(pn_std="VM-A", brand="BA"); bp = DimPart(pn_std="VM-B", brand="BB")
    db.add_all([a, bp]); db.flush()
    created = pool_catalog.create_pool(db, name="池-VM", member_part_ids=[a.id, bp.id],
                                       operated_by="t")
    gid = created["group_id"]
    pool_catalog.set_price_policy(db, group_id=gid, version=1,
                                  purchase_value=Decimal("150"), purchase_basis="ex_tax",
                                  sales_value=Decimal("180"), sales_basis="ex_tax",
                                  operated_by="t")
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hvm")
    db.add(b); db.flush()
    po = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
          "P2": f.purchase_head("P2", on=date(2026, 1, 10), is_tax_inclusive=True)}
    pl = [f.purchase_line("P1", "L1", "VM-A", qty="10", price="113"),
          f.purchase_line("P2", "L2", "VM-A", qty="10", price="226")]     # ex200 越上限
    loader.load(db, f.purchase_result(po, pl), b.id, AS_OF)
    so = {"S1": f.sales_head("S1", on=date(2026, 2, 1)),
          "S2": f.sales_head("S2", on=date(2026, 2, 10))}
    sl = [f.sales_line("S1", "SL1", "VM-A", qty="2", price="452"),
          f.sales_line("S2", "SL2", "VM-A", qty="1", price="113")]        # ex100 破下限
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)
    return gid


def _panels(db, gid, ctx):
    """全部 v2 面板（含新块），统一过 apply_field_visibility。"""
    av = lambda d: security.apply_field_visibility(d, ctx)  # noqa: E731
    return {
        "sales_orders": av(dashboard.sales_orders(db, status="全部", as_of=AS_OF, user_ctx=ctx)),
        "purchase_orders": av(dashboard.purchase_orders(db, status="全部", as_of=AS_OF, user_ctx=ctx)),
        "ranking": av(dashboard.part_ranking(db, None, None, sort="revenue",
                                             as_of=AS_OF, user_ctx=ctx)),
        "pools_new_sort": av(pool.list_pools(db, as_of=AS_OF, sort="sales_total", user_ctx=ctx)),
        "pools_legacy": av(pool.list_pools(db, as_of=AS_OF, sort="member_count", user_ctx=ctx)),
        "pool_detail": av(pool.analyze(db, gid, as_of=AS_OF, user_ctx=ctx, with_v2=True)),
    }


def _leaky(obj, hints, exempt=(), path=""):
    """递归找出命中 hints、不在 exempt、值非 None 的标量键路径。"""
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if (k not in exempt and any(h in kl for h in hints)
                    and v is not None and not isinstance(v, (dict, list))):
                bad.append(f"{path}.{k}={v!r}")
            bad += _leaky(v, hints, exempt, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad += _leaky(v, hints, exempt, f"{path}[{i}]")
    return bad


# 成本语义键（金额/判定）。max_purchase_price/min_sale_price 是治理口径（§12 全员公开、
# 由 governance 开关另测），在 cost 扫描中豁免；reference_status 是任务明确要求可见的状态。
_COST_HINTS = ("gross_profit", "premium_purchase", "purchase_price", "benchmark", "saving",
               "purchase_ex_tax", "total_ex_tax", "cost_ex_tax", "unit_price_ex_tax",
               "purchase_metrics", "pool_avg_purchase", "pool_avg_delta",
               "manual_limit_delta", "total_amount")
_COST_EXEMPT = ("max_purchase_price", "min_sale_price")

# 治理语义键（约束价/越线/与约束差额）
_GOV_HINTS = ("ceiling", "floor", "violation", "manual_limit",
              "max_purchase_price", "min_sale_price",
              "purchase_input_value", "sales_input_value")


def test_cost_blind_no_cost_key_leaks_across_v2_panels(db, seeded):
    ctx = _ctx(data_purchase_cost=False, data_profit=False)
    leaks = {n: _leaky(d, _COST_HINTS, _COST_EXEMPT) for n, d in _panels(db, seeded, ctx).items()}
    leaks = {n: v for n, v in leaks.items() if v}
    assert not leaks, f"成本语义键泄漏：{leaks}"


def test_cost_blind_still_sees_reference_status(db, seeded):
    """任务要求：无采购成本/利润权限时异常状态仍可见。"""
    ctx = _ctx(data_purchase_cost=False, data_profit=False)
    d = security.apply_field_visibility(
        dashboard.purchase_orders(db, status="全部", as_of=AS_OF, user_ctx=ctx), ctx)
    statuses = {p["reference_status"] for i in d["items"] for p in i["parts"]}
    assert "above_manual_max" in statuses          # 异常状态没有被顺带遮掉


def test_governance_blind_no_constraint_leaks_across_v2_panels(db, seeded):
    ctx = _ctx(data_pool_price_governance=False)
    panels = _panels(db, seeded, ctx)
    leaks = {n: _leaky(d, _GOV_HINTS) for n, d in panels.items()}
    leaks = {n: v for n, v in leaks.items() if v}
    assert not leaks, f"治理语义键泄漏：{leaks}"
    # 状态本身也不得携带约束关系（服务层降级，非键名 mask 可覆盖）
    for name in ("sales_orders", "purchase_orders"):
        for i in panels[name]["items"]:
            for p in i["parts"]:
                assert "manual" not in (p["reference_status"] or ""), (
                    f"{name} 状态泄漏约束关系: {p['reference_status']}")


def test_sales_aggregates_stay_visible_for_cost_blind(db, seeded):
    """不过度失能：cost-blind 仍能看销售聚合（营收/池销售均价/销售指标均价）。"""
    ctx = _ctx(data_purchase_cost=False, data_profit=False)
    panels = _panels(db, seeded, ctx)
    s = panels["sales_orders"]["items"][0]
    assert s["total_revenue"] is not None
    assert all(p["pool_avg_sale_price"] is not None for p in s["parts"])
    pool_item = panels["pools_new_sort"]["items"][0]
    assert pool_item["sales_metrics"]["weighted_avg_unit_price"] is not None
    assert pool_item["purchase_metrics"] is None   # 采购指标容器整块遮
