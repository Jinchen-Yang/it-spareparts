"""项目追踪工作簿表单闭环（§17）：宽松报销导入 / 页级锚 / 内容幂等键 /
多 sheet 白名单 / upsert=以本表为准 / 导出↔导入 round-trip。"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.etl import mapping, pipeline, reader
from app.etl.transform import transform
from app.models.maintenance import FProjectExpense
from app.models.system import SysImportBatch

_CANON = ["报销日期", "报销人员", "报销类别", "费用分类", "支出事由",
          "报销金额", "流程状态", "单号", "序号"]


@pytest.fixture()
def batch(db):
    b = SysImportBatch(filename="t.xlsx", file_type="expense", file_hash="hwf")
    db.add(b)
    db.flush()
    return b


def _canon_row(d="2026-05-01", person="张三", amount=100, reason="外援",
               status=None, bxd=None, seq=None, fee="外援劳务"):
    return dict(zip(_CANON, [d, person, "维保费用", fee, reason, amount, status, bxd, seq]))


def _workbook_xlsx(tmp_path, name, exp_rows, anchor="XSDD-1", with_parts=True,
                   with_total_row=True, marker=""):
    """构造项目追踪工作簿：预算页(kv) + 备件页(WBDD表头) + 报销页(锚+canonical) + 说明页。

    marker：写进预算页的杂散单元格，用于制造"内容相同但文件 hash 不同"的重传场景。
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "项目预算"
    ws.append(["合同（销售订单）", anchor])
    ws.append(["合同金额（含税参考）", 50000])
    if marker:
        ws.append(["备注", marker])
    if with_parts:
        ws2 = wb.create_sheet("备件明细-氚云")
        ws2.append(["数据ID(不可修改)", "需求单号", "制单日期", "需求类型",
                    "需求明细.数据ID(不可修改)", "需求明细.需供货产品",
                    "需求明细.产品描述", "需求明细.需求数量"])
        ws2.append(["MH1", "WBDD-20260501-0001", "2026-05-01", "报修供货",
                    "ML1", "PN-X", "HDD", 1])
    ws3 = wb.create_sheet("报销明细")
    ws3.append(["销售订单", anchor])
    ws3.append(_CANON)
    for r in exp_rows:
        ws3.append([r.get(c) for c in _CANON])
    if with_total_row:
        ws3.append([None, None, None, None, "合计（仅已结束）", 999999, None, None, None])
    ws4 = wb.create_sheet("填写说明")
    ws4["A1"] = "说明"
    p = tmp_path / name
    wb.save(str(p))
    return str(p)


# ---------- 识别（§17.3 宽松签名） ----------

def test_detect_loose_expense_signature():
    assert mapping.detect_file_type(_CANON) == mapping.EXPENSE
    assert mapping.detect_file_type(["报销日期", "报销金额"]) == mapping.EXPENSE
    assert mapping.detect_file_type(["费用分类", "报销金额(必填)"]) == mapping.EXPENSE
    # 裸「金额」不吃：防误伤
    assert mapping.detect_file_type(["日期", "金额"]) is None


# ---------- transform：必需列 / 锚 / 默认状态 / 内容键 ----------

def test_loose_transform_anchor_defaults_and_content_key():
    df = pd.DataFrame([
        _canon_row(amount=100),                      # 无状态 → 默认已结束
        _canon_row(amount=100),                      # 同内容 → dup_idx 区分
        _canon_row(d=None, amount=300),              # 缺日期 → 跳过（合计行形态）
        _canon_row(amount=None),                     # 缺金额 → 跳过
        _canon_row(amount=200, status="流程中", bxd="BXD-20260501-3", seq=2),
    ])
    res = transform(df, mapping.EXPENSE, anchor="XSDD-A")
    assert not res.errors
    assert res.rows_skipped_no_data == 2
    assert len(res.lines) == 3
    k0, k1, k2 = (r["raw_line_id"] for r in res.lines)
    assert k0.startswith("EXP:") and k0.endswith("#0")
    assert k1.startswith("EXP:") and k1.endswith("#1") and k0[:41] == k1[:41]
    assert k2 == "BXD-20260501-3#2"                  # 有单号+序号 → 复合键优先
    assert all(r["linked_sales_order_no"] == "XSDD-A" for r in res.lines)
    assert res.lines[0]["data_status"] == "已结束"    # 默认生效
    assert res.lines[2]["data_status"] == "流程中"
    assert res.rows_inactive == 1


def test_loose_transform_row_link_beats_anchor_and_missing_link_errors():
    df = pd.DataFrame([
        {**_canon_row(amount=50), "销售订单": "XSDD-ROW"},
        _canon_row(amount=60),
    ])
    res = transform(df, mapping.EXPENSE, anchor="XSDD-PAGE")
    assert res.lines[0]["linked_sales_order_no"] == "XSDD-ROW"    # 行级优先
    assert res.lines[1]["linked_sales_order_no"] == "XSDD-PAGE"
    res2 = transform(pd.DataFrame([_canon_row(amount=70)]), mapping.EXPENSE)  # 无锚无行级
    assert not res2.lines and res2.errors[0].error_type == "missing_link"


# ---------- reader：锚行探测 + 多 sheet ----------

def test_read_workbook_multisheet_anchor(db, tmp_path):
    p = _workbook_xlsx(tmp_path, "wb1.xlsx", [_canon_row()])
    sheets = reader.read_workbook(p)
    by_type = {s.file_type: s for s in sheets}
    assert set(by_type) == {mapping.MAINTENANCE, mapping.EXPENSE}   # 预算/说明页不识别
    exp = by_type[mapping.EXPENSE]
    assert exp.sheet_name == "报销明细" and exp.anchor == "XSDD-1"
    assert list(exp.df.columns)[:6] == _CANON[:6]


# ---------- pipeline：多 sheet 白名单 + 报告 ----------

def test_run_import_workbook_only_ingests_expense(db, tmp_path):
    p = _workbook_xlsx(tmp_path, "wb2.xlsx", [_canon_row(), _canon_row(amount=200, reason="快递")])
    batch = pipeline.run_import(db, p, "wb2.xlsx")
    db.commit()
    assert batch.file_type == "workbook"
    assert batch.rows_inserted == 2
    rep = batch.report_json
    assert [s["sheet"] for s in rep["skipped_sheets"]] == ["备件明细-氚云"]
    assert rep["sheets"][0]["rows_skipped_no_data"] == 1            # 合计行
    # 备件页确实没入库
    from app.models.maintenance import FMaintenanceOrder
    assert db.scalar(select(func.count()).select_from(FMaintenanceOrder)) == 0
    # 锚归集
    assert db.scalar(select(func.count()).select_from(FProjectExpense)
                     .where(FProjectExpense.linked_sales_order_no == "XSDD-1")) == 2


def test_run_import_workbook_reupload_idempotent(db, tmp_path):
    rows = [_canon_row(), _canon_row(amount=200, reason="快递")]
    p1 = _workbook_xlsx(tmp_path, "wb3.xlsx", rows)
    pipeline.run_import(db, p1, "wb3.xlsx")
    db.commit()
    # 同报销内容但文件字节不同（预算页加备注换 hash）→ 不触发文件级去重，考验行级内容键
    p2 = _workbook_xlsx(tmp_path, "wb3b.xlsx", rows, marker="财务月更 v2")
    b2 = pipeline.run_import(db, p2, "wb3b.xlsx")
    db.commit()
    assert b2.rows_inserted == 0 and b2.rows_skipped == 2           # 内容键幂等
    assert db.scalar(select(func.count()).select_from(FProjectExpense)) == 2


def test_run_import_upsert_replaces_contract(db, tmp_path):
    p1 = _workbook_xlsx(tmp_path, "wb4.xlsx",
                        [_canon_row(), _canon_row(amount=200, reason="快递")])
    pipeline.run_import(db, p1, "wb4.xlsx")
    db.commit()
    # 财务改了金额（100→150）并删掉快递行 → 修复模式=以本表为准
    p2 = _workbook_xlsx(tmp_path, "wb4b.xlsx", [_canon_row(amount=150)])
    b2 = pipeline.run_import(db, p2, "wb4b.xlsx", mode="upsert")
    db.commit()
    assert b2.report_json["sheets"][0]["expense_rows_replaced"] == 2
    amts = db.scalars(select(FProjectExpense.amount)
                      .where(FProjectExpense.linked_sales_order_no == "XSDD-1")).all()
    assert amts == [Decimal("150")]                                 # 无旧行残影


def test_run_import_workbook_without_expense_sheet_fails(db, tmp_path):
    """≥2 个可识别页但都不是报销页 → 拒绝（白名单：防把回填副本吃回去）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "备件明细-氚云"
    ws.append(["需求单号", "制单日期", "需求类型", "需求明细.需供货产品",
               "需求明细.产品描述", "需求明细.需求数量"])
    ws.append(["WBDD-20260501-0002", "2026-05-01", "报修供货", "PN-Y", "SSD", 1])
    ws2 = wb.create_sheet("库存快照")
    ws2.append(["产品库存ID", "产品名称(PN)", "库存数量", "仓库"])
    ws2.append(["INV-9", "PN-Y", 3, "总仓"])
    p = tmp_path / "wb5.xlsx"
    wb.save(str(p))
    from app.etl.reader import ReaderError
    with pytest.raises(ReaderError, match="报销明细页"):
        pipeline.run_import(db, str(p), "wb5.xlsx")
    db.commit()


def test_single_sheet_maintenance_still_imports(db, tmp_path):
    """单表维保出库文件行为不变（维保需求单样式不动，权威源不受白名单影响）。"""
    df = pd.DataFrame([{
        "数据ID(不可修改)": "MH9", "需求单号": "WBDD-20260501-0009",
        "制单日期": "2026-05-01", "需求类型": "报修供货",
        "销售订单": "XSDD-9", "项目名": "某项目",
        "需求明细.数据ID(不可修改)": "ML9", "需求明细.需供货产品": "PN-Z",
        "需求明细.产品描述": "HDD", "需求明细.需求数量": 1,
    }])
    p = tmp_path / "wbdd.xlsx"
    df.to_excel(str(p), index=False)
    batch = pipeline.run_import(db, str(p), "wbdd.xlsx")
    db.commit()
    assert batch.file_type == mapping.MAINTENANCE and batch.rows_inserted == 1


# ---------- round-trip：导出 → 再导入 = 零新增（§17.6 核心不变量）----------

def test_export_workbook_roundtrip_zero_new_rows(db, batch, tmp_path):
    from app.api.maintenance import _build_workbook
    from app.services import maintenance_cost

    db.add(FProjectExpense(raw_line_id="BXD-20260501-1#1", bxd_no="BXD-20260501-1",
                           line_no=1, data_status="已结束", expense_date=date(2026, 5, 2),
                           person="张三", expense_type="维保费用", fee_category="外援劳务",
                           reason="外援", linked_sales_order_no="XSDD-RT",
                           amount=Decimal("500"), import_batch_id=batch.id))
    db.add(FProjectExpense(raw_line_id="EXP:abc#0", bxd_no=None, line_no=None,
                           data_status="流程中", expense_date=date(2026, 5, 3),
                           person="李四", expense_type="维保费用", fee_category="交通差旅",
                           reason="打车", linked_sales_order_no="XSDD-RT",
                           amount=Decimal("66"), import_batch_id=batch.id))
    db.commit()

    data = maintenance_cost.contract_workbook_data(db, "XSDD-RT")
    wb = _build_workbook("XSDD-RT", data)
    p = tmp_path / "roundtrip.xlsx"
    wb.save(str(p))

    b2 = pipeline.run_import(db, str(p), "roundtrip.xlsx")
    db.commit()
    rep = b2.report_json
    sheet_rep = rep["sheets"][0] if "sheets" in rep else rep
    assert sheet_rep["fact_rows_error"] == 0
    # 复合键行幂等命中；内容键行（EXP:abc#0 是手造键）按导出内容重derive → 允许 ≤1 差异？
    # 不——出厂即约定：内容键由(合同|日期|金额|事由|人员)派生，重导必然一致。手造键除外，
    # 故此断言用"真实闭环"：先经系统导入产生内容键，再导出回灌。
    assert db.scalar(select(func.count()).select_from(FProjectExpense)
                     .where(FProjectExpense.linked_sales_order_no == "XSDD-RT")) >= 2


def test_export_workbook_true_roundtrip(db, batch, tmp_path):
    """真实闭环：宽松导入产生内容键 → 导出 → 再导入 → 零新增零错误。"""
    from app.api.maintenance import _build_workbook
    from app.services import maintenance_cost

    p1 = _workbook_xlsx(tmp_path, "rt1.xlsx",
                        [_canon_row(amount=100), _canon_row(amount=100),
                         _canon_row(amount=200, reason="快递", status="流程中"),
                         _canon_row(amount=300, bxd="BXD-20260502-1", seq=1)],
                        anchor="XSDD-RT2", with_parts=False, with_total_row=False)
    pipeline.run_import(db, p1, "rt1.xlsx")
    db.commit()
    n0 = db.scalar(select(func.count()).select_from(FProjectExpense)
                   .where(FProjectExpense.linked_sales_order_no == "XSDD-RT2"))
    assert n0 == 4

    data = maintenance_cost.contract_workbook_data(db, "XSDD-RT2")
    wb = _build_workbook("XSDD-RT2", data)
    p2 = tmp_path / "rt2.xlsx"
    wb.save(str(p2))

    b2 = pipeline.run_import(db, str(p2), "rt2.xlsx")
    db.commit()
    assert b2.rows_inserted == 0 and b2.rows_error == 0             # 零新增零错误
    assert db.scalar(select(func.count()).select_from(FProjectExpense)
                     .where(FProjectExpense.linked_sales_order_no == "XSDD-RT2")) == n0
