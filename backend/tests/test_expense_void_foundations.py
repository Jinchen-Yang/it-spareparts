"""钉死修复模式删除侧的隐形地基（2026-09-04 对抗核验指出：这些此前没有任何测试保护）。

两条配置、一条常量、一处消费关系。任一处松动，都会让「预演说作废 0 行、实际作废
整个合同域」成为可能，而没有测试会先叫。
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from app.etl import expense_void, mapping, pipeline
from app.etl.reader import ReaderError
from app.etl.transform import ErrorRec, TransformResult, transform


# ---------- 地基 1：报销页不前向填充 ----------

def test_expense_sheet_is_never_forward_filled():
    """`FFILL_COLS[EXPENSE] == []` 是「空的销售订单单元格不会被前向填充」的唯一保证。
    改成非空 ⇒ 扫描器看到空、transform 看到上一行的合同号 ⇒ missing_link 消失
    ⇒ 删除侧抑制失效 ⇒ 整个合同域被作废。"""
    assert mapping.FFILL_COLS[mapping.EXPENSE] == []
    assert mapping.EXPENSE not in mapping.VALUE_ALIASES


def test_blank_xsdd_cell_under_a_filled_one_stays_blank_at_row_level():
    """行为钉：同一张表里，上一行有合同号、本行单元格为空且本行自带日期
    （因此不是延续行、不走 gvh 单头继承）⇒ 本行必须是 missing_link，而不是继承。"""
    df = pd.DataFrame([
        {"报销日期": "2026-05-01", "报销人员": "甲", "支出事由": "项目差旅",
         "报销金额": 100, "销售订单": "XSDD-A"},
        {"报销日期": "2026-05-02", "报销人员": "乙", "支出事由": "办公用品",
         "报销金额": 50},                                   # 自带日期，不是延续行
    ])
    res = transform(df, mapping.EXPENSE)
    assert [ln["linked_sales_order_no"] for ln in res.lines] == ["XSDD-A"]
    assert [e.error_type for e in res.errors] == ["missing_link"]


# ---------- 地基 2：门禁只放行 missing_link，且真的从 expense_void 取 ----------

def test_only_missing_link_is_non_blocking():
    assert expense_void.NON_BLOCKING_ERROR_TYPES == frozenset({"missing_link"})


def test_pipeline_gate_consumes_expense_void_not_a_private_copy(db, tmp_path, monkeypatch):
    """变异：把 duplicate_key 临时塞进放行集合，门禁必须跟着放行——证明 pipeline
    没有自己再抄一份类型集。"""
    from openpyxl import Workbook

    cols = ["报销日期", "报销人员", "支出事由", "报销金额", "单号", "序号", "销售订单"]
    wb = Workbook(); ws = wb.active; ws.title = "Sheet1"; ws.append(cols)
    ws.append(["2026-05-01", "甲", "a", 100, "B1", 1, "XSDD-A"])
    ws.append(["2026-05-01", "甲", "b", 200, "B1", 1, "XSDD-A"])   # duplicate_key
    p = tmp_path / "dup.xlsx"; wb.save(str(p))

    with pytest.raises(ReaderError, match="修复模式"):
        pipeline.run_import(db, str(p), "dup.xlsx", mode="upsert")
    db.rollback()

    monkeypatch.setattr(expense_void, "NON_BLOCKING_ERROR_TYPES",
                        frozenset({"missing_link", "duplicate_key"}))
    batch = pipeline.run_import(db, str(p), "dup2.xlsx", mode="upsert")
    db.commit()
    assert batch.status == "success"


# ---------- 判定函数本身（纯函数，无库） ----------

def _result(lines, errors=(), anchors=("X",)):
    """默认带页级锚（项目工作簿报销页形态）；anchors=(None,) 表示无锚逐行表。"""
    r = TransformResult(file_type=mapping.EXPENSE)
    r.lines = list(lines)
    r.errors = list(errors)
    r.expense_anchors = list(anchors)
    return r


def _line(raw_id, xsdd):
    return {"raw_line_id": raw_id, "linked_sales_order_no": xsdd}


def _existing(**status_by_id):
    return {k: SimpleNamespace(data_status=v) for k, v in status_by_id.items()}


def test_classify_skip_mode_never_voids():
    inputs = expense_void.plan_inputs(_result([_line("a", "X")]), mode="skip")
    d = expense_void.classify(_existing(a="已结束", b="已结束"), inputs)
    assert d.void_ids == () and d.protected_ids == () and d.suppressed_reason is None


def test_classify_single_contract_voids_missing_and_skips_already_void():
    inputs = expense_void.plan_inputs(_result([_line("a", "X")]), mode="upsert")
    d = expense_void.classify(_existing(a="已结束", b="已结束", c="已作废"), inputs)
    assert d.void_ids == ("b",)
    assert d.already_void_ids == ("c",)
    assert d.protected_ids == ()
    assert d.suppressed_reason is None


def test_classify_dropped_row_suppresses_everything():
    err = ErrorRec(1, "missing_link", "x", {})
    inputs = expense_void.plan_inputs(_result([_line("a", "X")], [err]), mode="upsert")
    d = expense_void.classify(_existing(a="已结束", b="已结束"), inputs)
    assert d.void_ids == ()
    assert d.protected_ids == ("b",)
    assert d.suppressed_reason == expense_void.SUPPRESS_DROPPED


def test_classify_multi_contract_suppresses_everything():
    inputs = expense_void.plan_inputs(
        _result([_line("a", "X"), _line("c", "Y")]), mode="upsert")
    d = expense_void.classify(_existing(a="已结束", b="已结束", c="已结束", d="已结束"),
                              inputs)
    assert d.void_ids == ()
    assert set(d.protected_ids) == {"b", "d"}
    assert d.suppressed_reason == expense_void.SUPPRESS_MULTI_CONTRACT


def test_dropped_takes_precedence_over_multi_contract_in_reason():
    err = ErrorRec(1, "missing_link", "x", {})
    inputs = expense_void.plan_inputs(
        _result([_line("a", "X"), _line("c", "Y")], [err]), mode="upsert")
    assert inputs.suppressed_reason == expense_void.SUPPRESS_DROPPED


def test_scope_contracts_empty_unless_upsert_with_contracts():
    assert expense_void.scope_contracts(
        expense_void.plan_inputs(_result([_line("a", "X")]), mode="skip")) == []
    assert expense_void.scope_contracts(
        expense_void.plan_inputs(_result([]), mode="upsert")) == []
    # 单合同 + 锚 ⇒ 扩宽到该合同；两个合同 ⇒ 抑制在查库前已知 ⇒ 不扩宽
    assert expense_void.scope_contracts(
        expense_void.plan_inputs(_result([_line("a", "X")]), mode="upsert")) == ["X"]
    assert expense_void.scope_contracts(
        expense_void.plan_inputs(_result([_line("a", "X"), _line("b", "Y")]),
                                 mode="upsert")) == []


def test_classify_unanchored_single_contract_is_suppressed():
    """无锚逐行表即使只有一个合同也不作废：否则把多合同导出按合同拆成多份
    单合同表分次上传就能绕过多合同抑制（对抗核验实跑证实）。"""
    inputs = expense_void.plan_inputs(_result([_line("a", "X")], anchors=(None,)),
                                      mode="upsert")
    d = expense_void.classify(_existing(a="已结束", b="已结束"), inputs)
    assert d.void_ids == ()
    assert d.protected_ids == ("b",)
    assert d.suppressed_reason == expense_void.SUPPRESS_UNANCHORED


def test_two_sheets_with_different_anchors_count_as_multi_contract():
    inputs = expense_void.plan_inputs(
        _result([_line("a", "X"), _line("b", "Y")], anchors=("X", "Y")), mode="upsert")
    assert inputs.anchored is True
    assert inputs.suppressed_reason == expense_void.SUPPRESS_MULTI_CONTRACT


def test_scope_is_not_widened_when_suppressed():
    """抑制在查库前已知 ⇒ 不按合同扩宽 scope（不锁、不同步那 790 个合同的旧行）。"""
    inputs = expense_void.plan_inputs(_result([_line("a", "X")], anchors=(None,)),
                                      mode="upsert")
    assert inputs.suppressed_reason is not None
    assert expense_void.scope_contracts(inputs) == []


def test_parse_int_overflow_is_none_not_a_crash():
    """序号 = "inf" / "1e999"：int(float(...)) 抛 OverflowError，此前会让整个多文件
    预检 500，导入路径也会炸。"""
    from app.etl import cleaner
    assert cleaner.parse_int("inf") is None
    assert cleaner.parse_int("1e999") is None
    assert cleaner.parse_int("3.0") == 3
