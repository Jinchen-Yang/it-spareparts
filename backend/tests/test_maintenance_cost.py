"""维保出库成本引擎 + 导入识别/转换测试（docs/维保出库成本核算-开发方案.md §14）。

取价瀑布五层各一例、含税优先、追溯上限、占位 PN 排除、退货冲抵、起算日作用域、
幂等与 upsert 不冲成本、文件识别与列名容差、transform 基础路径。
"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import select

from app.etl import loader, mapping
from app.etl.transform import transform
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.system import SysImportBatch
from app.services import maintenance_cost
from tests import factories as f


@pytest.fixture()
def batch(db):
    b = SysImportBatch(filename="t.xlsx", file_type="maintenance", file_hash="hm1")
    db.add(b)
    db.flush()
    return b


def _load_purchases(db, b, orders, lines):
    loader.load(db, f.purchase_result(orders, lines), b.id, date(2026, 6, 1))


def _load_maintenance(db, b, orders, lines, mode="skip"):
    loader.load(db, f.maintenance_result(orders, lines), b.id, date(2026, 6, 1), mode=mode)


def _line(db, raw_line_id) -> FMaintenanceLine:
    return db.execute(select(FMaintenanceLine)
                      .where(FMaintenanceLine.raw_line_id == raw_line_id)).scalar_one()


# ---------- 瀑布五层 ----------

def test_direct_hit_weighted(db, batch):
    """A0 直配：同 WBDD 同 PN 多行按数量加权；记来源采购单号。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", order_no="CGDD-1", on=date(2026, 3, 5),
                              source_type="维保需求", linked_maintenance_order_no="WBDD-1"),
    }, [
        f.purchase_line("P1", "PL1", "PN-A", qty="1", price="100"),
        f.purchase_line("P1", "PL2", "PN-A", qty="3", price="200"),
    ])
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", order_no="WBDD-1",
                                                           on=date(2026, 3, 10))},
                      [f.maintenance_line("M1", "ML1", "PN-A", qty="2")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["direct"] == 1
    ln = _line(db, "ML1")
    assert ln.cost_source == "direct"
    assert ln.unit_cost == Decimal("175.00")          # (100+600)/4
    assert ln.cost_amount == Decimal("350.00")
    assert ln.trace_months == 0
    assert ln.linked_purchase_order_no == "CGDD-1"
    assert ln.cost_tax_basis == "ex"                   # is_tax_inclusive=None → ex


def test_month_avg(db, batch):
    """A2 当月加权：无直配/±7天窗口命中时取同 part 当月均价（跨采购单加权）。

    v2：两笔采购都放在出库日 ±7 天窗口之外（13/15 天），确保不落 window 层。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", on=date(2026, 3, 2)),
        "P2": f.purchase_head("P2", on=date(2026, 3, 30)),
    }, [
        f.purchase_line("P1", "PL1", "PN-B", qty="1", price="90"),
        f.purchase_line("P2", "PL2", "PN-B", qty="1", price="110"),
    ])
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", on=date(2026, 3, 15))},
                      [f.maintenance_line("M1", "ML1", "PN-B", qty="1")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["month_avg"] == 1
    ln = _line(db, "ML1")
    assert ln.cost_source == "month_avg"
    assert ln.unit_cost == Decimal("100.00")
    assert ln.price_month == "2026-03"
    assert ln.trace_months == 0


def test_trace_avg_and_cap(db, batch):
    """B 追溯：缺当月往前找最近采购月（记月数）；超 3 个月不追、落销售参考层。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", on=date(2026, 1, 10)),   # PN-C：3 月出库 → 追溯 2 个月
        "P2": f.purchase_head("P2", on=date(2025, 10, 10)),  # PN-D：5 个月前 → 超上限
    }, [
        f.purchase_line("P1", "PL1", "PN-C", qty="1", price="50"),
        f.purchase_line("P2", "PL2", "PN-D", qty="1", price="70"),
    ])
    sb = SysImportBatch(filename="s.xlsx", file_type="sales", file_hash="hs1")
    db.add(sb)
    db.flush()
    loader.load(db, f.sales_result(
        {"S1": f.sales_head("S1", on=date(2026, 3, 5), business_type="备件销售")},
        [f.sales_line("S1", "SL1", "PN-D", qty="1", price="88")]), sb.id, date(2026, 6, 1))
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", on=date(2026, 3, 15))}, [
        f.maintenance_line("M1", "ML1", "PN-C", qty="1"),
        f.maintenance_line("M1", "ML2", "PN-D", qty="1"),
    ])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["trace_avg"] == 1 and stats["sales_ref"] == 1
    c = _line(db, "ML1")
    assert (c.cost_source, c.trace_months, c.price_month) == ("trace_avg", 2, "2026-01")
    assert c.unit_cost == Decimal("50.00")
    d = _line(db, "ML2")
    assert d.cost_source == "sales_ref"               # 采购超追溯上限 → 没有采购有销售
    assert d.unit_cost == Decimal("88.00")
    assert d.cost_tax_basis == "ex"                    # sales_head 默认无税率 → ex


def test_none_no_cost_flag(db, batch):
    """D 无成本：采购/销售皆无 → none + no_cost 标记。"""
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", on=date(2026, 3, 1))},
                      [f.maintenance_line("M1", "ML1", "PN-NONE", qty="1")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["none"] == 1
    ln = _line(db, "ML1")
    assert ln.cost_source == "none"
    assert ln.unit_cost is None and ln.cost_amount is None
    assert "no_cost" in ln.anomaly_flags


# ---------- 口径细节 ----------

def test_tax_preference_inc_first(db, batch):
    """Q4：同一取价日含税/不含税并存 → 优先含税并标注；仅不含税 → ex。

    v2：日期近优先于税口径（window 层），税偏好只在同一取价日内取舍 → 两单同日验证。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", on=date(2026, 3, 2), is_tax_inclusive=True,
                              tax_rate=Decimal("0.13")),
        "P2": f.purchase_head("P2", on=date(2026, 3, 2)),
    }, [
        f.purchase_line("P1", "PL1", "PN-E", qty="1", price="113"),
        f.purchase_line("P2", "PL2", "PN-E", qty="1", price="100"),
        f.purchase_line("P2", "PL3", "PN-F", qty="1", price="60"),
    ])
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", on=date(2026, 3, 9))}, [
        f.maintenance_line("M1", "ML1", "PN-E", qty="1"),
        f.maintenance_line("M1", "ML2", "PN-F", qty="1"),
    ])
    db.commit()
    maintenance_cost.recompute(db)
    e = _line(db, "ML1")
    assert (e.unit_cost, e.cost_tax_basis) == (Decimal("113.00"), "inc")
    fl = _line(db, "ML2")
    assert (fl.unit_cost, fl.cost_tax_basis) == (Decimal("60.00"), "ex")


def test_placeholder_pn_excluded_from_pool(db, batch):
    """「一批备件」打包占位不进价格池（2,333 万实测教训）。"""
    _load_purchases(db, batch,
                    {"P1": f.purchase_head("P1", on=date(2026, 3, 2))},
                    [f.purchase_line("P1", "PL1", "一批备件", qty="1", price="999999")])
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", on=date(2026, 3, 9))},
                      [f.maintenance_line("M1", "ML1", "一批备件", qty="1")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["none"] == 1
    assert _line(db, "ML1").unit_cost is None


def test_return_qty_offsets_cost(db, batch):
    """退货冲抵：cost_amount=(qty-return_qty)×单价，标 has_return；退超发按 0 计。"""
    _load_purchases(db, batch, {"P1": f.purchase_head("P1", on=date(2026, 3, 2))},
                    [f.purchase_line("P1", "PL1", "PN-G", qty="1", price="100")])
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", on=date(2026, 3, 9))}, [
        f.maintenance_line("M1", "ML1", "PN-G", qty="5", return_qty="2"),
        f.maintenance_line("M1", "ML2", "PN-G", qty="1", return_qty="3"),
    ])
    db.commit()
    maintenance_cost.recompute(db)
    a = _line(db, "ML1")
    assert a.cost_amount == Decimal("300.00") and "has_return" in a.anomaly_flags
    b = _line(db, "ML2")
    assert b.cost_amount == Decimal("0.00")


def test_start_date_scope(db, batch):
    """起算日（2024-01-01）前的出库不计价：cost_source 恒 NULL，区别于 none。"""
    _load_purchases(db, batch, {"P1": f.purchase_head("P1", on=date(2023, 12, 5))},
                    [f.purchase_line("P1", "PL1", "PN-H", qty="1", price="100")])
    _load_maintenance(db, batch,
                      {"M1": f.maintenance_head("M1", on=date(2023, 12, 20))},
                      [f.maintenance_line("M1", "ML1", "PN-H", qty="1")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["out_of_scope"] == 1 and stats["lines_in_scope"] == 0
    ln = _line(db, "ML1")
    assert ln.cost_source is None and ln.unit_cost is None


def test_recompute_idempotent_and_upsert_preserves_cost(db, batch):
    """重复 recompute 结果稳定；维保文件 upsert 重导不冲成本字段（白名单排除）。"""
    orders = {"M1": f.maintenance_head("M1", order_no="WBDD-9", on=date(2026, 3, 9))}
    lines = [f.maintenance_line("M1", "ML1", "PN-I", qty="2")]
    _load_purchases(db, batch, {"P1": f.purchase_head("P1", on=date(2026, 3, 2))},
                    [f.purchase_line("P1", "PL1", "PN-I", qty="1", price="42")])
    _load_maintenance(db, batch, orders, lines)
    db.commit()
    s1 = maintenance_cost.recompute(db)
    first = (_line(db, "ML1").unit_cost, _line(db, "ML1").cost_source)
    # upsert 重导同一批明细：成本回填字段必须保留
    _load_maintenance(db, batch, orders, lines, mode="upsert")
    db.commit()
    ln = _line(db, "ML1")
    assert (ln.unit_cost, ln.cost_source) == first
    s2 = maintenance_cost.recompute(db)
    assert s1 == s2
    assert (_line(db, "ML1").unit_cost, _line(db, "ML1").cost_source) == first


# ---------- 项目聚合 ----------

def test_projects_aggregate_shared_contract(db, batch):
    """项目聚合：税口径分列小计、来源分布、共用合同标记（Q5）。"""
    sb = SysImportBatch(filename="s.xlsx", file_type="sales", file_hash="hs2")
    db.add(sb)
    db.flush()
    loader.load(db, f.sales_result(
        {"S1": {**f.sales_head("S1", order_no="XSDD-1", on=date(2026, 1, 5),
                               business_type="整体维保"),
                "amount_ex_tax": Decimal("1000"), "tax_rate": Decimal("0.06")}},
        [f.sales_line("S1", "SL1", "整体维保专用", qty="1", price="1060")]),
        sb.id, date(2026, 6, 1))
    _load_purchases(db, batch, {"P1": f.purchase_head("P1", on=date(2026, 3, 2))},
                    [f.purchase_line("P1", "PL1", "PN-J", qty="1", price="100")])
    _load_maintenance(db, batch, {
        "M1": f.maintenance_head("M1", on=date(2026, 3, 9), project="项目甲",
                                 sales_order="XSDD-1"),
        "M2": f.maintenance_head("M2", on=date(2026, 3, 12), project="项目乙",
                                 sales_order="XSDD-1"),
    }, [
        f.maintenance_line("M1", "ML1", "PN-J", qty="2"),
        f.maintenance_line("M2", "ML2", "PN-J", qty="1"),
        f.maintenance_line("M2", "ML3", "PN-NOPRICE", qty="1"),
    ])
    db.commit()
    maintenance_cost.recompute(db)
    data = maintenance_cost.projects_aggregate(db, lifecycle="all")
    rows = {r["project"]: r for r in data["rows"]}
    assert rows["项目甲"]["cost_ex"] == 200.0 and rows["项目甲"]["cost_inc"] == 0.0
    assert rows["项目甲"]["coverage_pct"] == 100.0
    assert rows["项目乙"]["by_source"]["none"] == 1
    assert rows["项目乙"]["coverage_pct"] == 50.0
    # 同一 XSDD 挂两个项目 → 共用标记；合同额=1000×1.06
    assert rows["项目甲"]["contract_shared"] and rows["项目乙"]["contract_shared"]
    assert rows["项目甲"]["contract_amount"] == 1060.0


# ---------- 导入识别 / 转换 ----------

_MAINT_COLS = [
    "数据ID(不可修改)", "需求单号", "制单日期", "销售订单", "项目名", "客户名称",
    "需求类型", "业务类型", "销售人员", "出库仓库(必填)", "维保起始日期", "维保终止日期",
    "数据状态", "需求明细.数据ID(不可修改)", "需求明细.序号", "需求明细.需供货产品",
    "需求明细.产品描述", "需求明细.需求数量", "需求明细.退货数量", "需求明细.发货SN",
]


def test_detect_maintenance_and_no_regression():
    assert mapping.detect_file_type(_MAINT_COLS) == "maintenance"
    # 列名容差：注解后缀变体经 canonicalize 后仍可识别
    variants = [c.replace("(必填)", "") for c in _MAINT_COLS]
    assert mapping.detect_file_type(mapping.canonicalize_columns(variants)) == "maintenance"
    # 原有类型零回归
    assert mapping.detect_file_type(["采购单号(必填)", "明细.产品名称(必填)"]) == "purchase"
    assert mapping.detect_file_type(["订单编号(必填)", "业务类型#"]) == "sales"
    assert mapping.detect_file_type(["产品库存ID", "库存数量"]) == "inventory"


def test_transform_maintenance_basics():
    """转换：头/行字段落位、项目名剥「预交付-」、草稿单空 PN 走软错误。"""
    rows = [
        {"数据ID(不可修改)": "RID1", "需求单号": "WBDD-1", "制单日期": "2026-03-01",
         "销售订单": "XSDD-1", "项目名": "预交付-某项目20260101", "客户名称": "客户A",
         "需求类型": "报修供货", "业务类型": "备件维保", "销售人员": "张三",
         "出库仓库(必填)": "北京成品仓", "维保起始日期": None, "维保终止日期": None,
         "数据状态": "已生效",
         "需求明细.数据ID(不可修改)": "LID1", "需求明细.序号": 1,
         "需求明细.需供货产品": "PN-1", "需求明细.产品描述": "硬盘",
         "需求明细.需求数量": 3, "需求明细.退货数量": 1, "需求明细.发货SN": "SN1"},
        {"数据ID(不可修改)": "RID2", "需求单号": "WBDD-2", "制单日期": "2026-03-02",
         "销售订单": None, "项目名": None, "客户名称": None,
         "需求类型": None, "业务类型": None, "销售人员": None,
         "出库仓库(必填)": None, "维保起始日期": None, "维保终止日期": None,
         "数据状态": "草稿",
         "需求明细.数据ID(不可修改)": "LID2", "需求明细.序号": 1,
         "需求明细.需供货产品": None, "需求明细.产品描述": None,
         "需求明细.需求数量": None, "需求明细.退货数量": None, "需求明细.发货SN": None},
    ]
    res = transform(pd.DataFrame(rows), mapping.MAINTENANCE)
    assert res.rows_total == 2 and len(res.lines) == 1
    head = res.orders["RID1"]
    assert head["project_std"] == "某项目20260101"      # 剥前缀
    assert head["project_raw"] == "预交付-某项目20260101"
    ln = res.lines[0]
    assert (ln["qty"], ln["return_qty"]) == (Decimal("3"), Decimal("1"))
    # 草稿单空 PN → 软错误（不计入 rows_error 口径由 loader 判定）
    assert res.errors and res.errors[0].error_type == "empty_pn_inactive"


def test_loader_report_counts(db, batch):
    """loader 报告：维保头/行入库计数与幂等 skip。"""
    orders = {"M1": f.maintenance_head("M1", on=date(2026, 3, 9))}
    lines = [f.maintenance_line("M1", "ML1", "PN-K", qty="1")]
    r1 = loader.load(db, f.maintenance_result(orders, lines), batch.id, date(2026, 6, 1))
    assert (r1["orders_inserted"], r1["fact_rows_inserted"]) == (1, 1)
    r2 = loader.load(db, f.maintenance_result(orders, lines), batch.id, date(2026, 6, 1))
    assert (r2["fact_rows_inserted"], r2["fact_rows_skipped"]) == (0, 1)
    n = db.execute(select(FMaintenanceOrder)).scalars().all()
    assert len(n) == 1
