"""维保 v2（§16）：window 取价层/置信度、盈亏看板状态灯、BXD 报销导入、工作簿单据级回填。"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import select

from app.etl import loader, mapping
from app.etl.transform import transform
from app.models.maintenance import (
    FMaintenanceLine,
    FProjectExpense,
    MaintenanceContractWorkbookState,
)
from app.models.system import SysImportBatch
from app.services import maintenance_cost, maintenance_workbook_renderer
from tests import factories as f


@pytest.fixture()
def batch(db):
    b = SysImportBatch(
        filename="t.xlsx",
        file_type="maintenance",
        file_hash="hv2",
        status="success",
    )
    db.add(b)
    db.flush()
    return b


def _load_purchases(db, b, orders, lines):
    loader.load(db, f.purchase_result(orders, lines), b.id, date(2026, 6, 1))


def _load_maintenance(db, b, orders, lines):
    loader.load(db, f.maintenance_result(orders, lines), b.id, date(2026, 6, 1))


def _load_sales(db, b, orders, lines):
    loader.load(db, f.sales_result(orders, lines), b.id, date(2026, 6, 1))


def _line(db, raw_line_id) -> FMaintenanceLine:
    return db.execute(select(FMaintenanceLine)
                      .where(FMaintenanceLine.raw_line_id == raw_line_id)).scalar_one()


# ---------- window 层（§16.1）----------

def test_window_nearest_wins_and_boundary(db, batch):
    """出库 3/15：采购 3/19(4天) 比 3/10(5天) 近 → 取 3/19；3/23(8天) 超 ±7 界不参与。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", on=date(2026, 3, 10)),
        "P2": f.purchase_head("P2", on=date(2026, 3, 19)),
        "P3": f.purchase_head("P3", on=date(2026, 3, 23)),
    }, [
        f.purchase_line("P1", "PL1", "PN-W", qty="1", price="120"),
        f.purchase_line("P2", "PL2", "PN-W", qty="1", price="200"),
        f.purchase_line("P3", "PL3", "PN-W", qty="1", price="999"),
    ])
    _load_maintenance(db, batch,
                      {"M1": f.maintenance_head("M1", order_no="WBDD-W1", on=date(2026, 3, 15))},
                      [f.maintenance_line("M1", "MLW1", "PN-W", qty="1")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["window"] == 1
    ln = _line(db, "MLW1")
    assert ln.cost_source == "window"
    assert ln.unit_cost == Decimal("200.00")
    assert ln.price_distance_days == 4
    assert ln.confidence == "high"
    assert ln.price_month == "2026-03"


def test_window_tie_prefers_earlier_same_day_weighted(db, batch):
    """同距（±3 天）取更早一侧；同日多笔加权：(1×100+3×200)/4 = 175。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", on=date(2026, 3, 12)),
        "P2": f.purchase_head("P2", on=date(2026, 3, 12)),
        "P3": f.purchase_head("P3", on=date(2026, 3, 18)),
    }, [
        f.purchase_line("P1", "PL1", "PN-T", qty="1", price="100"),
        f.purchase_line("P2", "PL2", "PN-T", qty="3", price="200"),
        f.purchase_line("P3", "PL3", "PN-T", qty="1", price="500"),
    ])
    _load_maintenance(db, batch,
                      {"M1": f.maintenance_head("M1", order_no="WBDD-T1", on=date(2026, 3, 15))},
                      [f.maintenance_line("M1", "MLT1", "PN-T", qty="1")])
    db.commit()
    maintenance_cost.recompute(db)
    ln = _line(db, "MLT1")
    assert ln.cost_source == "window"
    assert ln.unit_cost == Decimal("175.00")
    assert ln.price_distance_days == 3


def test_outside_window_month_avg_and_confidence_ladder(db, batch):
    """窗口外同月 → month_avg(medium)；跨月 → purchase_history(low)。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", on=date(2026, 3, 1)),    # 出库 3/20 距 19 天：窗口外、同月
        "P2": f.purchase_head("P2", on=date(2026, 2, 10)),   # PN-Y：出库 3/20 → 跨月追溯
    }, [
        f.purchase_line("P1", "PL1", "PN-X", qty="1", price="100"),
        f.purchase_line("P2", "PL2", "PN-Y", qty="1", price="80"),
    ])
    _load_maintenance(db, batch,
                      {"M1": f.maintenance_head("M1", order_no="WBDD-X1", on=date(2026, 3, 20))},
                      [f.maintenance_line("M1", "MLX1", "PN-X", qty="1"),
                       f.maintenance_line("M1", "MLX2", "PN-Y", qty="1")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["month_avg"] == 1 and stats["purchase_history"] == 1
    assert _line(db, "MLX1").confidence == "medium"
    ln_y = _line(db, "MLX2")
    assert ln_y.cost_source == "purchase_history"
    assert ln_y.confidence == "low" and ln_y.price_distance_days is None


def test_direct_wbdd_key_case_insensitive(db, batch):
    """§16.5#2：采购关联单号小写/带空格 vs 维保单号大写 → 仍命中 direct。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", order_no="CGDD-U1", on=date(2026, 3, 5),
                              source_type="维保需求",
                              linked_maintenance_order_no="wbdd-u1 "),
    }, [f.purchase_line("P1", "PL1", "PN-U", qty="1", price="300")])
    _load_maintenance(db, batch,
                      {"M1": f.maintenance_head("M1", order_no="WBDD-U1", on=date(2026, 3, 10))},
                      [f.maintenance_line("M1", "MLU1", "PN-U", qty="1")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["direct"] == 1
    assert _line(db, "MLU1").cost_source == "direct"


# ---------- 盈亏看板（§16.2）----------

def _one_contract(db, batch, tag: str, budget: str, cost: str):
    """一张合同：销售单(预算) + 直配采购(精确控制成本) + 一行出库。"""
    _load_sales(db, batch, {f"S-{tag}": f.sales_head(
        f"S-{tag}", order_no=f"XS-{tag}", amount_ex_tax=Decimal(budget))},
        [f.sales_line(f"S-{tag}", f"SL-{tag}", f"PN-{tag}", qty="1", price="1")])
    _load_purchases(db, batch, {
        f"P-{tag}": f.purchase_head(f"P-{tag}", on=date(2026, 3, 5), source_type="维保需求",
                                    linked_maintenance_order_no=f"WBDD-{tag}"),
    }, [f.purchase_line(f"P-{tag}", f"PL-{tag}", f"PN-{tag}", qty="1", price=cost)])
    _load_maintenance(db, batch, {
        f"M-{tag}": f.maintenance_head(f"M-{tag}", order_no=f"WBDD-{tag}",
                                       on=date(2026, 3, 10), sales_order=f"XS-{tag}"),
    }, [f.maintenance_line(f"M-{tag}", f"ML-{tag}", f"PN-{tag}", qty="1")])


def test_board_fails_closed_without_complete_expense_watermark(db, batch):
    """费用全量水位未建立时，预算灯和无预算结论都不得绕过费用门禁。"""
    _one_contract(db, batch, "G", budget="1000", cost="100")     # 剩余 90% → green
    _one_contract(db, batch, "B", budget="1000", cost="800")     # 剩余恰 20% → yellow（≤ 含边界）
    _one_contract(db, batch, "R", budget="1000", cost="1200")    # 超支 → red
    # 无合同关联 → no_budget
    _load_maintenance(db, batch, {"M-N": f.maintenance_head("M-N", order_no="WBDD-N",
                                                            on=date(2026, 3, 10), sales_order=None)},
                      [f.maintenance_line("M-N", "ML-N", "PN-G", qty="1")])
    db.commit()
    maintenance_cost.recompute(db)
    b = maintenance_cost.board(db, lifecycle="all")
    st = {r["contract"]: r["status"] for r in b["rows"]}
    assert set(st.values()) == {"expense_data_unavailable"}
    assert all(
        row["expense_data_available"] is False
        and row["spent_expense"] is None
        and row["spent"] is None
        and row["remaining"] is None
        for row in b["rows"]
    )
    assert b["status_counts"] == {
        "red": 0, "yellow": 0, "green": 0, "no_budget": 0,
    }
    unavailable = maintenance_cost.board(
        db,
        status="expense_data_unavailable",
        lifecycle="all",
    )["rows"]
    assert len(unavailable) == 4
    assert maintenance_cost.board(
        db,
        status="yellow",
        lifecycle="all",
    )["rows"] == []


def test_board_does_not_publish_partial_expenses_as_complete_spend(db, batch):
    """已有零散生效费用仍不等于建立全量水位，完整支出必须保持空值。"""
    _one_contract(db, batch, "E", budget="1000", cost="100")
    db.add(FProjectExpense(raw_line_id="EXP-1", bxd_no="BXD-20260301-1", line_no=1,
                           data_status="已结束", expense_date=date(2026, 3, 8),
                           fee_category="差旅", linked_sales_order_no="XS-E",
                           amount=Decimal("950"),
                           amount_ex_tax=Decimal("950"),
                           amount_inc_tax=Decimal("1073.50"),
                           import_batch_id=batch.id))
    db.add(FProjectExpense(raw_line_id="EXP-2", bxd_no="BXD-20260301-2", line_no=1,
                           data_status="流程中", expense_date=date(2026, 3, 9),
                           fee_category="差旅", linked_sales_order_no="XS-E",
                           amount=Decimal("9999"),
                           amount_ex_tax=Decimal("9999"),
                           amount_inc_tax=Decimal("11298.87"),
                           import_batch_id=batch.id))
    db.commit()
    maintenance_cost.recompute(db)
    row = next(
        r for r in maintenance_cost.board(db, lifecycle="all")["rows"]
        if r["contract"] == "XS-E"
    )
    assert row["expense_data_available"] is False
    assert row["spent_expense"] is None
    assert row["spent"] is None
    assert row["remaining"] is None
    assert row["status"] == "expense_data_unavailable"


# ---------- BXD 报销导入（§16.3）----------

_EXP_COLS_NATIVE = ["数据ID(不可修改)", "数据标题", "流程状态", "报销人员", "报销类别",
                    "支出事由", "维保销售订单", "报销日期",
                    "报销明细.数据ID(不可修改)", "报销明细.序号", "报销明细.费用分类",
                    "报销明细.报销金额"]


def test_expense_detect_transform_idempotent(db, batch):
    assert mapping.detect_file_type(_EXP_COLS_NATIVE) == mapping.EXPENSE
    df = pd.DataFrame([{
        "数据ID(不可修改)": "H1", "数据标题": "BXD-20260301-1 张三", "流程状态": "已结束",
        "报销人员": "张三", "报销类别": "差旅费", "支出事由": "维保驻场",
        "维保销售订单": "XS-1", "报销日期": "2026-03-05",
        "报销明细.数据ID(不可修改)": "E1", "报销明细.序号": 1,
        "报销明细.费用分类": "交通差旅", "报销明细.报销金额": 500,
    }])
    res = transform(df, mapping.EXPENSE)
    assert not res.errors and len(res.lines) == 1
    r = res.lines[0]
    assert r["bxd_no"] == "BXD-20260301-1" and r["amount"] == Decimal("500")
    assert r["linked_sales_order_no"] == "XS-1"

    loader.load(db, res, batch.id, date(2026, 6, 1))
    again = loader.load(db, transform(df, mapping.EXPENSE), batch.id, date(2026, 6, 1))
    db.commit()
    assert again["fact_rows_inserted"] == 0        # 幂等：重复导入不重复入库
    assert db.scalar(select(FProjectExpense.amount)
                     .where(FProjectExpense.raw_line_id == "E1")) == Decimal("500")


def test_expense_composite_key_and_column_drift(db, batch):
    """工作簿版：无数据ID列 → BXD单号#序号 复合键；费用分类/销售订单列名漂移回退互补。"""
    df = pd.DataFrame([{
        "数据标题": "BXD-20260401-7 李四", "流程状态": "已结束", "报销人员": "李四",
        "报销类别": "劳务", "支出事由": "外援", "销售订单": "XS-2",
        "报销日期": "2026-04-02", "序号": 1, "费用分类": "外援劳务", "报销金额": 300,
    }])
    res = transform(df, mapping.EXPENSE)
    assert not res.errors and len(res.lines) == 1
    r = res.lines[0]
    # v1.7.0（§17.4）：复合键带合同域后缀——单号可为手填自由文本，跨合同同名不得互撞
    assert r["raw_line_id"].startswith("BXD-20260401-7#1@")
    assert r["fee_category"] == "外援劳务" and r["linked_sales_order_no"] == "XS-2"
    loader.load(db, res, batch.id, date(2026, 6, 1))
    db.commit()
    assert db.scalar(select(FProjectExpense.bxd_no)
                     .where(FProjectExpense.raw_line_id.like("BXD-20260401-7#1@%"))) == "BXD-20260401-7"


# ---------- 工作簿导出（§16.4）----------

def test_workbook_doc_level_backfill(db, batch):
    """产品成本=单据级总成本（Σ行成本），月度汇总含备件消耗与生效费用分类。"""
    _load_sales(db, batch, {"S-W": f.sales_head("S-W", order_no="XS-W",
                                                amount_ex_tax=Decimal("5000"))},
                [f.sales_line("S-W", "SL-W", "PN-WB", qty="1", price="1")])
    _load_purchases(db, batch, {
        "P-W": f.purchase_head("P-W", on=date(2026, 3, 5), source_type="维保需求",
                               linked_maintenance_order_no="WBDD-WB"),
    }, [f.purchase_line("P-W", "PL-W1", "PN-WB", qty="1", price="100"),
        f.purchase_line("P-W", "PL-W2", "PN-WB2", qty="1", price="200")])
    _load_maintenance(db, batch, {
        "M-W": f.maintenance_head("M-W", order_no="WBDD-WB", on=date(2026, 3, 10),
                                  sales_order="XS-W"),
    }, [f.maintenance_line("M-W", "ML-W1", "PN-WB", qty="1"),
        f.maintenance_line("M-W", "ML-W2", "PN-WB2", qty="1")])
    db.add(FProjectExpense(raw_line_id="EXP-W", bxd_no="BXD-20260301-9", line_no=1,
                           data_status="已结束", expense_date=date(2026, 3, 20),
                           fee_category="驻场工程师", linked_sales_order_no="XS-W",
                           amount=Decimal("400"),
                           amount_ex_tax=Decimal("400"),
                           amount_inc_tax=Decimal("452"),
                           import_batch_id=batch.id))
    db.add(FProjectExpense(raw_line_id="EXP-W-PART-NAMED",
                           bxd_no="BXD-20260301-10", line_no=1,
                               data_status="已结束", expense_date=date(2026, 3, 21),
                               fee_category="备件消耗", linked_sales_order_no="XS-W",
                               amount=Decimal("50"),
                               amount_ex_tax=Decimal("50"),
                               amount_inc_tax=Decimal("56.50"),
                               import_batch_id=batch.id))
    db.add(FProjectExpense(raw_line_id="EXP-W-STATIC-NAMED",
                           bxd_no="BXD-20260301-11", line_no=1,
                               data_status="已结束", expense_date=date(2026, 3, 22),
                               fee_category="月份", linked_sales_order_no="XS-W",
                               amount=Decimal("10"),
                               amount_ex_tax=Decimal("10"),
                               amount_inc_tax=Decimal("11.30"),
                               import_batch_id=batch.id))
    db.commit()
    maintenance_cost.recompute(db)
    data = maintenance_cost.contract_workbook_data(db, "XS-W")
    assert data["doc_total"]["WBDD-WB"] == Decimal("300.00")
    assert "monthly" not in data
    assert data["monthly_parts"]["2026-03"] == Decimal("300.00")
    assert data["monthly_expenses"]["2026-03"] == {
        "备件消耗": Decimal("50"),
        "月份": Decimal("10"),
        "驻场工程师": Decimal("400"),
    }
    assert data["budget"] == Decimal("5650.00")

    # 模板渲染：三页齐全、标题含合同号、表头深色白字、产品成本只填单据首行且高亮、金额千分位
    from app.api.maintenance import _build_workbook
    wb = _build_workbook("XS-W", data)
    assert wb.sheetnames == ["项目预算", "备件明细-氚云", "报销明细", "填写说明"]
    ws = wb["项目预算"]
    assert "XS-W" in ws["A1"].value
    monthly_header_row = next(
        row
        for row in range(1, ws.max_row + 1)
        if ws.cell(row, 1).value == "月份"
    )
    monthly_headings = [
        cell.value
        for cell in ws[monthly_header_row]
        if cell.value is not None
    ]
    monthly_values = [
        cell.value
        for cell in ws[monthly_header_row + 1]
    ][:len(monthly_headings)]
    assert dict(zip(monthly_headings, monthly_values, strict=True)) == {
        "月份": "2026-03",
        "已知备件成本参考（混合原值·兼容）": 300,
        "当前已导入报销（非全量）·备件消耗": 50,
        "当前已导入报销（非全量）·月份": 10,
        "当前已导入报销（非全量）·驻场工程师": 400,
        "当月已知合计（费用非全量）": 760,
    }
    ws2 = wb["备件明细-氚云"]
    assert ws2.cell(1, 1).font.color.rgb.endswith("FFFFFF")           # 表头白字
    assert ws2.cell(1, 1).fill.fgColor.rgb.endswith("35506B")         # 深色表头
    assert ws2.freeze_panes == "A2"
    assert ws2.cell(2, 13).value == 300.0                             # 单据级总成本填首行
    assert ws2.cell(2, 13).font.bold                                  # 且高亮加粗
    assert ws2.cell(3, 13).value is None                              # 第二行不重复填
    assert ws2.cell(2, 18).number_format == "#,##0.00"                # 金额千分位
    ws3 = wb["报销明细"]
    # §17.3 canonical：第 1 行归集锚（销售订单|XSDD），第 2 行表头，金额在第 6 列
    assert ws3.cell(1, 1).value == "销售订单" and ws3.cell(1, 2).value == "XS-W"
    assert ws3.cell(2, 1).value == "报销日期" and ws3.cell(2, 6).value == "报销金额"
    assert ws3.cell(3, 6).number_format == "#,##0.00"


def test_date_scoped_workbook_keeps_period_facts_but_blocks_budget_decision(
    db,
    batch,
):
    """区间支出不能与整合同预算比较；全量导出仍保留原预算决策。"""
    contract = "XS-SCOPED-BUDGET"
    _load_sales(
        db,
        batch,
        {
            "S-SCOPED": f.sales_head(
                "S-SCOPED",
                order_no=contract,
                amount_ex_tax=Decimal("884.96"),
            ),
        },
        [f.sales_line("S-SCOPED", "SL-SCOPED", "PN-SCOPED", qty="1")],
    )
    for suffix, order_date, cost in (
        ("OLD", date(2026, 3, 10), "800"),
        ("SELECTED", date(2026, 4, 10), "100"),
    ):
        order_no = f"WBDD-SCOPED-{suffix}"
        _load_purchases(
            db,
            batch,
            {
                f"P-SCOPED-{suffix}": f.purchase_head(
                    f"P-SCOPED-{suffix}",
                    on=order_date,
                    source_type="维保需求",
                    linked_maintenance_order_no=order_no,
                ),
            },
            [
                f.purchase_line(
                    f"P-SCOPED-{suffix}",
                    f"PL-SCOPED-{suffix}",
                    f"PN-SCOPED-{suffix}",
                    qty="1",
                    price=cost,
                ),
            ],
        )
        _load_maintenance(
            db,
            batch,
            {
                f"M-SCOPED-{suffix}": f.maintenance_head(
                    f"M-SCOPED-{suffix}",
                    order_no=order_no,
                    on=order_date,
                    sales_order=contract,
                ),
            },
            [
                f.maintenance_line(
                    f"M-SCOPED-{suffix}",
                    f"ML-SCOPED-{suffix}",
                    f"PN-SCOPED-{suffix}",
                    qty="1",
                ),
            ],
        )
    db.add(MaintenanceContractWorkbookState(
        contract_no=contract,
        expense_snapshot_complete=True,
    ))
    db.commit()
    maintenance_cost.recompute(db)

    full = maintenance_cost.contract_workbook_data(db, contract)
    scoped = maintenance_cost.contract_workbook_data(
        db,
        contract,
        date_from=date(2026, 4, 1),
        date_to=date(2026, 4, 30),
    )

    assert full["date_filtered"] is False
    assert full["budget"] == Decimal("1000.00")
    assert full["decision"] == {
        "decision_status": "yellow",
        "known_spend_total": Decimal("900.00"),
        "remaining": Decimal("100.00"),
        "remaining_pct": Decimal("10.00"),
    }
    assert scoped["date_filtered"] is True
    assert scoped["cost_summary"]["known_cost_total"] == Decimal("100.00")
    assert scoped["decision"] == {
        "decision_status": "filtered_scope",
        "known_spend_total": Decimal("100.00"),
        "remaining": None,
        "remaining_pct": None,
    }

    full_workbook = maintenance_workbook_renderer.render_contract_workbook(
        contract,
        full,
        lambda value: value,
    )
    scoped_workbook = maintenance_workbook_renderer.render_contract_workbook(
        contract,
        scoped,
        lambda value: value,
    )
    try:
        def value_after(sheet, label):
            label_cell = next(
                cell
                for row_cells in sheet.iter_rows()
                for cell in row_cells
                if cell.value == label
            )
            return sheet.cell(
                row=label_cell.row,
                column=label_cell.column + 1,
            ).value

        full_sheet = full_workbook["项目预算"]
        assert value_after(
            full_sheet,
            "完整项目支出参考（备件+报销）",
        ) == 900
        assert value_after(full_sheet, "剩余预算") == 100

        scoped_sheet = scoped_workbook["项目预算"]
        assert "日期筛选下不计算合同预算余量/红黄绿" in scoped_sheet["A2"].value
        assert value_after(
            scoped_sheet,
            "所选期间支出参考（备件+报销）",
        ) == 100
        assert value_after(scoped_sheet, "剩余预算") == "—"
        assert value_after(
            scoped_sheet,
            "预算消耗参考状态",
        ) == "日期筛选下不计算合同预算余量/红黄绿"
        assert all(
            cell.value != "完整项目支出参考（备件+报销）"
            for row_cells in scoped_sheet.iter_rows()
            for cell in row_cells
        )
    finally:
        full_workbook.close()
        scoped_workbook.close()
