"""看板 v2（验收 10 及价格参考语义）：reference_status 判定 + 约束价为空不误标 +
治理权限关闭时的状态降级（纯函数级 + 端点级）。"""
from datetime import date
from decimal import Decimal

import pytest

from app import permissions, security
from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import dashboard, pool_catalog, profit
from app.services.pool_metrics import price_reference
from tests import factories as f

AS_OF = date(2026, 6, 1)


def _ctx(**over):
    perms = permissions._full()
    perms.update(over)
    return security.UserContext(user_id="u", role="custom", permissions=perms,
                                is_authenticated=True)


# ---------------------------------------------------------------- 纯函数级

def test_purchase_status_ladder():
    # 超人工上限（优先级最高）
    r = price_reference("purchase", 200.0, True, 150.0, 150.0, False)
    assert r["reference_status"] == "above_manual_max"
    assert r["manual_limit_delta"] == 50.0 and r["manual_limit_delta_pct"] == 0.3333
    assert r["pool_avg_delta"] == 50.0
    # 未越线但高于池均价
    r = price_reference("purchase", 140.0, True, 120.0, 150.0, False)
    assert r["reference_status"] == "above_pool_average"
    # 有约束且不劣于池均价
    r = price_reference("purchase", 100.0, True, 120.0, 150.0, False)
    assert r["reference_status"] == "within_limit"
    # 等于约束价不算越线（§13）
    r = price_reference("purchase", 150.0, True, 200.0, 150.0, False)
    assert r["reference_status"] == "within_limit"
    assert r["manual_limit_delta"] == 0.0


def test_sales_status_ladder():
    r = price_reference("sales", 100.0, True, 300.0, 180.0, False)
    assert r["reference_status"] == "below_manual_min"
    assert r["manual_limit_delta"] == -80.0
    r = price_reference("sales", 200.0, True, 300.0, 180.0, False)
    assert r["reference_status"] == "below_pool_average"
    r = price_reference("sales", 400.0, True, 300.0, 180.0, False)
    assert r["reference_status"] == "within_limit"
    # 等于下限不算越线
    r = price_reference("sales", 180.0, True, 100.0, 180.0, False)
    assert r["reference_status"] == "within_limit"


def test_no_pool_and_no_price():
    assert price_reference("purchase", 100.0, False, None, None, False)["reference_status"] == "no_pool"
    assert price_reference("purchase", None, True, 100.0, 100.0, False)["reference_status"] == "no_price"
    r = price_reference("sales", 0.0, True, 100.0, 100.0, False)
    assert r["reference_status"] == "no_price"
    # ¥0 赠送行不是价格信号：不得输出"低于池均价 100%"的差额（审计 P2）
    assert r["pool_avg_delta"] is None and r["pool_avg_delta_pct"] is None
    assert r["manual_limit_delta"] is None


def test_raw_value_judged_not_rounded_display():
    """判定用未税原值：limit=100.88、原值 114/1.13≈100.885 —— 舍入显示值等于约束价，
    但原值严格大于 → 必须 above_manual_max，与池级 SQL 计数一致（审计 P2 分厘边界）。"""
    raw = 114 / 1.13   # 100.88495575...
    r = price_reference("purchase", raw, True, None, 100.88, False)
    assert r["reference_status"] == "above_manual_max"
    assert r["manual_limit_delta"] == 0.0     # 差额输出仍按 2 位舍入


def test_null_constraint_never_flags_violation():
    """验收 10：人工约束价为空 → 不标越线、不标 within_limit（没有 limit 可 within）。"""
    r = price_reference("purchase", 999.0, True, None, None, False)
    assert r["reference_status"] == "no_manual_limit"
    assert r["manual_limit_delta"] is None and r["manual_limit_delta_pct"] is None
    # 无约束但高于池均价 → 池均价口径照常提示
    r = price_reference("purchase", 200.0, True, 150.0, None, False)
    assert r["reference_status"] == "above_pool_average"
    r = price_reference("sales", 200.0, True, 300.0, None, False)
    assert r["reference_status"] == "below_pool_average"
    r = price_reference("sales", 400.0, True, 300.0, None, False)
    assert r["reference_status"] == "no_manual_limit"


def test_governance_restricted_degrades_to_pool_only():
    """data_pool_price_governance 关闭：涉约束价状态降级为池均价口径，约束差额一律 None——
    多行"可见价格×越线布尔"可二分逼出约束价原值。"""
    r = price_reference("purchase", 200.0, True, 150.0, 150.0, True)
    assert r["reference_status"] == "above_pool_average"          # 不再出现 above_manual_max
    assert r["manual_limit_delta"] is None and r["manual_limit_delta_pct"] is None
    r = price_reference("purchase", 100.0, True, 150.0, 150.0, True)
    assert r["reference_status"] == "within_pool_average"
    r = price_reference("purchase", 100.0, True, None, 150.0, True)
    assert r["reference_status"] == "no_pool_average"
    r = price_reference("sales", 100.0, True, 300.0, 180.0, True)
    assert r["reference_status"] == "below_pool_average"


# ---------------------------------------------------------------- 端点级

@pytest.fixture()
def seeded(db):
    a = DimPart(pn_std="PN-A", brand="BA"); bp = DimPart(pn_std="PN-B", brand="BB")
    db.add_all([a, bp]); db.flush()
    created = pool_catalog.create_pool(db, name="池-AB", member_part_ids=[a.id, bp.id],
                                       operated_by="t")
    gid = created["group_id"]
    pool_catalog.set_price_policy(db, group_id=gid, version=1,
                                  purchase_value=Decimal("150"), purchase_basis="ex_tax",
                                  sales_value=Decimal("180"), sales_basis="ex_tax",
                                  operated_by="t")
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="href")
    db.add(b); db.flush()
    po = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
          "P2": f.purchase_head("P2", on=date(2026, 1, 10), is_tax_inclusive=True)}
    pl = [f.purchase_line("P1", "PL1", "PN-A", qty="10", price="113"),    # ex100 within
          f.purchase_line("P2", "PL2", "PN-A", qty="10", price="226")]    # ex200 超上限
    loader.load(db, f.purchase_result(po, pl), b.id, AS_OF)
    so = {"S1": f.sales_head("S1", on=date(2026, 2, 1)),
          "S2": f.sales_head("S2", on=date(2026, 2, 10))}
    sl = [f.sales_line("S1", "SL1", "PN-A", qty="2", price="452"),        # ex400 within
          f.sales_line("S2", "SL2", "PN-A", qty="1", price="113")]        # ex100 破下限
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)
    return gid


def test_purchase_order_lines_reference(db, seeded):
    d = dashboard.purchase_orders(db, status="全部", as_of=AS_OF)
    by = {i["order_no"]: i["parts"][0] for i in d["items"]}
    # 池窗口加权均价 = (10*100+10*200)/20 = 150
    assert by["P1"]["pool_avg_purchase_price"] == 150.0
    assert by["P1"]["max_purchase_price"] == 150.0
    assert by["P1"]["reference_status"] == "within_limit"          # ex100 ≤150 且 ≤池均
    assert by["P2"]["reference_status"] == "above_manual_max"      # ex200 >150
    assert by["P2"]["manual_limit_delta"] == 50.0
    assert by["P2"]["pool_avg_delta"] == 50.0
    assert by["P2"]["pool_avg_delta_pct"] == 0.3333


def test_sales_order_lines_reference(db, seeded):
    d = dashboard.sales_orders(db, status="全部", as_of=AS_OF)
    by = {i["order_no"]: i["parts"][0] for i in d["items"]}
    # 池窗口销售加权均价 = (800+100)/3 = 300
    assert by["S1"]["pool_avg_sale_price"] == 300.0
    assert by["S1"]["min_sale_price"] == 180.0
    assert by["S1"]["reference_status"] == "within_limit"
    assert by["S2"]["reference_status"] == "below_manual_min"
    assert by["S2"]["manual_limit_delta"] == -80.0


def test_status_visible_but_amounts_masked_for_cost_blind(db, seeded):
    """任务要求：无采购成本/利润权限时异常状态仍可见，但受限金额必须 null，
    且差额一并 null（不能"池均价+差额"反推行价）。"""
    ctx = _ctx(data_purchase_cost=False, data_profit=False)
    d = security.apply_field_visibility(
        dashboard.purchase_orders(db, status="全部", as_of=AS_OF, user_ctx=ctx), ctx)
    by = {i["order_no"]: i["parts"][0] for i in d["items"]}
    assert by["P2"]["reference_status"] == "above_manual_max"      # 状态可见
    assert by["P2"]["unit_price_ex_tax"] is None                    # 金额遮
    assert by["P2"]["amount"] is None
    assert by["P2"]["pool_avg_purchase_price"] is None
    assert by["P2"]["pool_avg_delta"] is None and by["P2"]["pool_avg_delta_pct"] is None
    assert by["P2"]["manual_limit_delta"] is None and by["P2"]["manual_limit_delta_pct"] is None
    # 约束价本身随 data_pool_price_governance（此处未关）仍可见——治理口径 §12 全员公开
    assert by["P2"]["max_purchase_price"] == 150.0
    # 销售侧：行价随 purchase_cost 同名先例遮，池销售均价为公开聚合
    ds = security.apply_field_visibility(
        dashboard.sales_orders(db, status="全部", as_of=AS_OF, user_ctx=ctx), ctx)
    sby = {i["order_no"]: i["parts"][0] for i in ds["items"]}
    assert sby["S2"]["unit_price_ex_tax"] is None
    assert sby["S2"]["pool_avg_sale_price"] == 300.0
    assert sby["S2"]["pool_avg_delta"] is None                      # 差额遮：否则均价+差额反推行价
    assert sby["S2"]["reference_status"] == "below_manual_min"


def test_in_stats_scope_flag(db, seeded):
    """展示行全打标签，但取消单/未来单/¥0 行标 in_stats_scope=False——
    页面红标数与池 violation_count 的对账钩子（审计 P2：两个"越线"呈现不再哑口不一致）。"""
    b = SysImportBatch(filename="t2.xlsx", file_type="sales", file_hash="hscope")
    db.add(b); db.flush()
    so = {"SC": f.sales_head("SC", on=date(2026, 2, 15), data_status="已取消"),
          "SF": f.sales_head("SF", on=date(2026, 12, 1))}
    sl = [f.sales_line("SC", "SLC", "PN-A", qty="1", price="113"),
          f.sales_line("SF", "SLZ", "PN-A", qty="1", price="0")]
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)
    d = dashboard.sales_orders(db, status="全部", as_of=AS_OF)
    by = {i["order_no"]: i["parts"][0] for i in d["items"]}
    assert by["S1"]["in_stats_scope"] is True
    assert by["SC"]["in_stats_scope"] is False        # 取消单
    assert by["SC"]["reference_status"] == "below_manual_min"   # 标签照打，但不进计数口径
    assert by["SF"]["in_stats_scope"] is False        # 未来 + ¥0
    assert by["SF"]["reference_status"] == "no_price"


def test_governance_blind_endpoint_degrades(db, seeded):
    """治理关闭：约束价/越线差额/涉约束状态全部不可见（含服务层降级 + 键名脱敏双防线）。"""
    ctx = _ctx(data_pool_price_governance=False)
    d = security.apply_field_visibility(
        dashboard.purchase_orders(db, status="全部", as_of=AS_OF, user_ctx=ctx), ctx)
    assert d["manual_reference_restricted"] is True
    by = {i["order_no"]: i["parts"][0] for i in d["items"]}
    assert by["P2"]["max_purchase_price"] is None
    assert by["P2"]["manual_limit_delta"] is None
    assert by["P2"]["reference_status"] == "above_pool_average"     # 降级，不暴露约束关系
    assert by["P1"]["reference_status"] == "within_pool_average"
