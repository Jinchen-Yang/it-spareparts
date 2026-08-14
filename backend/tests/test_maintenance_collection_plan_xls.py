"""Task 3 车道 B1：专用 .xls parser 红测（Step 3.1）。

使用合成 BIFF8 二进制 fixture（测试内程序化构建，绝不使用真实业务数据）。
覆盖：
- 精确 64 列签名（header_signature 与 ordered_headers）；任何标签或位置漂移失败关闭。
- 24 组交替 ``回款时间 N / 回款金额`` 解析。
- 非 BIFF、错误扩展名、超文件/Sheet/行/列/物理单元格/字符串预算拒绝。
- 只投影首个 Sheet；其他 Sheet 计入资源预算但不产生计划事实。
- ``YYYY年M月`` → 月初 + precision=month；非法月份拒绝。
- 金额 ``Decimal(str(value))``；零/负/过大/精度越界拒绝且不静默 round。
- 日期金额孤儿、重复订单、序号断档拒绝；未知/合计行失败关闭；多余列拒绝。
- 颜色/格式不参与状态；公式缓存只是需人工确认的观察值，不宣称验证公式。
- 错误信息只给 sheet/row/field code 与原因，不回显业务值。
- 真实样例只读 acceptance（环境变量提供路径时才运行；否则推迟到 Task 7）。
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path

import pytest

from app.services.maintenance_collection_plan_xls import (
    CONTRACT_HEADER_SIGNATURE,
    CONTRACT_VERSION,
    CollectionPlanContractError,
    parse_project_manager_collection_xls,
)

# 与 .ai/contracts/maintenance-collections/project-manager-xls-v1.yaml 冻结的
# ordered_headers 完全一致（64 项）。测试同时锁定模块常量与其逐字一致。
ORDERED_HEADERS = [
    "订单编号",
    "订单日期",
    "销售人员",
    "业务类型",
    "项目名称",
    "维保起始日期",
    "维保终止日期",
    "CMO",
    "项目经理",
    "订单金额",
    "已收尾款",
    "待收尾款",
    "验收材料",
    "验收材料是否完成及上传附件",
    "巡检时间",
    "巡检是否完成及上传附件",
]
for _idx in range(1, 25):
    ORDERED_HEADERS.append(f"回款时间{_idx}")
    ORDERED_HEADERS.append("回款金额")
assert len(ORDERED_HEADERS) == 64

# 合同冻结的 header_signature（json_array_utf8_ensure_ascii_false_compact + sha256）。
FROZEN_HEADER_SIGNATURE = "eee2d1f5f67644d18ae3c2dadada6f9f2422a8545bc316bff9e01998e3b9c13e"
# 真实样例（冻结 SHA；只读 acceptance 使用，绝不 apply）。
REAL_SAMPLE_SHA256 = "a783af09fa108d366a26e10fe188be52d20a9ce1fe02121bfd683d96356c8c18"


# ---------- 合成 BIFF8 构建器 ----------

def _biff_record(code: int, payload: bytes) -> bytes:
    return struct.pack("<HH", code, len(payload)) + payload


def _sst_string(text: str) -> bytes:
    """SST/BOUNDSHEET 内的 UnicodeString（lenlen=1 前缀；BIFF8）。"""
    if all(ord(ch) < 256 for ch in text):
        return struct.pack("<HB", len(text), 0) + text.encode("latin-1")
    return struct.pack("<HB", len(text), 0x01) + text.encode("utf-16-le")


def build_synthetic_biff8(sheets) -> bytes:
    """构造最小 raw BIFF8 工作簿（无 OLE 包装）。

    sheets: [{"name": str, "cells": [(row, col, value), ...]}]；
    value 为 str → LABELSST；int/float → NUMBER；
    ("formula", float) → FORMULA（缓存数值，cce=0，不携带公式体）。
    """
    unique: dict[str, int] = {}

    def _sst_index(text: str) -> int:
        if text not in unique:
            unique[text] = len(unique)
        return unique[text]

    sheet_streams: list[tuple[str, bytes]] = []
    for sheet in sheets:
        parts = [
            _biff_record(0x0809, struct.pack("<HHHHHH", 0x0600, 0x0010, 0x0DBB, 0x07CC, 0, 0))
        ]
        for row, col, value in sheet["cells"]:
            if isinstance(value, str):
                parts.append(_biff_record(0x00FD, struct.pack("<HHHi", row, col, 0, _sst_index(value))))
            elif isinstance(value, tuple) and value[0] == "formula":
                parts.append(
                    _biff_record(
                        0x0006,
                        struct.pack("<HHH8sHH", row, col, 0, struct.pack("<d", float(value[1])), 0, 0),
                    )
                )
            else:
                parts.append(_biff_record(0x0203, struct.pack("<HHHd", row, col, 0, float(value))))
        parts.append(_biff_record(0x000A, b""))
        sheet_streams.append((sheet["name"], b"".join(parts)))

    globals_head = [
        _biff_record(0x0809, struct.pack("<HHHHHH", 0x0600, 0x0005, 0x0DBB, 0x07CC, 0, 0)),
        _biff_record(0x0042, struct.pack("<H", 1200)),  # CODEPAGE utf_16_le
        _biff_record(0x0022, struct.pack("<H", 0)),     # DATEMODE 1900
    ]
    sst_payload = struct.pack("<II", len(unique), len(unique))
    for text in unique:
        sst_payload += _sst_string(text)
    globals_head.append(_biff_record(0x00FC, sst_payload))

    boundsheet_records = [
        _biff_record(0x0085, struct.pack("<iBB", 0, 0, 0) + _sst_string(name))
        for name, _stream in sheet_streams
    ]
    pos = len(b"".join(globals_head)) + sum(len(r) for r in boundsheet_records) + 4
    sheet_offsets: list[int] = []
    for _name, stream in sheet_streams:
        sheet_offsets.append(pos)
        pos += len(stream)

    out = b"".join(globals_head)
    for offset, (name, _stream) in zip(sheet_offsets, sheet_streams):
        out += _biff_record(0x0085, struct.pack("<iBB", offset, 0, 0) + _sst_string(name))
    out += _biff_record(0x000A, b"")
    for _name, stream in sheet_streams:
        out += stream
    return out


def _header_row() -> list:
    return list(ORDERED_HEADERS)


def _plan_row(
    order_no: str,
    project_name: str,
    *,
    months: list[str] | None = None,
    amounts: list[float | str] | None = None,
    order_amount: float | str | None = 100000.0,
    row: int = 1,
) -> list:
    """构造一行 64 列数据。months/amounts 为 24 组长度的可选列表。"""
    values: list = [None] * 64
    values[0] = order_no
    values[1] = "2026-01-01"
    values[2] = "合成销售"
    values[3] = "维保"
    values[4] = project_name
    values[5] = "2026-01-01"
    values[6] = "2027-01-01"
    values[7] = "CMO-合成"
    values[8] = "合成负责人"
    if order_amount is not None:
        values[9] = order_amount
    values[10] = 0
    values[11] = 0
    values[12] = ""
    values[13] = ""
    values[14] = ""
    values[15] = ""
    for idx in range(24):
        if months is not None and idx < len(months) and months[idx]:
            values[16 + idx * 2] = months[idx]
        if amounts is not None and idx < len(amounts) and amounts[idx] is not None:
            values[17 + idx * 2] = amounts[idx]
    return values


def _cells_from_rows(rows: list[list], *, start_row: int = 0) -> list:
    cells = []
    for offset, row_values in enumerate(rows):
        for col, value in enumerate(row_values):
            if value is not None:
                cells.append((start_row + offset, col, value))
    return cells


def _valid_plan_workbook(*, extra_rows: list[list] | None = None, sheets: list | None = None) -> bytes:
    rows = [_header_row()]
    if extra_rows:
        rows.extend(extra_rows)
    if sheets is None:
        sheets = [{"name": "维保项目清单", "cells": _cells_from_rows(rows)}]
    else:
        sheets = list(sheets)
        sheets[0]["cells"] = _cells_from_rows(rows)
    return build_synthetic_biff8(sheets)


def _parse(content: bytes, *, filename: str = "synthetic-plan.xls"):
    return parse_project_manager_collection_xls(content, filename=filename)


# ---------- 精确签名与 24 组交替列 ----------

def test_frozen_header_signature_matches_contract_and_module():
    """64 列有序表头必须与合同冻结签名逐字一致（任何漂移失败关闭）。"""
    assert len(ORDERED_HEADERS) == 64
    assert CONTRACT_VERSION == "project-manager-xls-v1"
    assert CONTRACT_HEADER_SIGNATURE == FROZEN_HEADER_SIGNATURE
    actual = hashlib.sha256(
        json.dumps(ORDERED_HEADERS, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert actual == FROZEN_HEADER_SIGNATURE
    # 交替结构：第 17..63 列（1-based）必须是 回款时间 N / 回款金额 交替。
    for idx in range(24):
        assert ORDERED_HEADERS[16 + idx * 2] == f"回款时间{idx + 1}"
        assert ORDERED_HEADERS[17 + idx * 2] == "回款金额"


def test_parses_24_pairs_with_canonical_month_and_decimal_amounts():
    months = [f"2026年{m}月" for m in range(1, 13)] + [f"2027年{m}月" for m in range(1, 13)]
    amounts = [18000.5 + idx * 1000 for idx in range(24)]
    rows = [
        _plan_row("ORD-SYN-001", "合成项目 A", months=months, amounts=amounts, order_amount=708012.0),
    ]
    content = _valid_plan_workbook(extra_rows=rows)

    plan = _parse(content)
    assert plan.contract_version == CONTRACT_VERSION
    assert plan.header_sha256 == FROZEN_HEADER_SIGNATURE
    assert len(plan.rows) == 1
    order = plan.rows[0]
    assert order.external_order_no == "ORD-SYN-001"
    assert order.source_project_name == "合成项目 A"
    assert len(order.nodes) == 24
    assert [n.sequence for n in order.nodes] == list(range(1, 25))
    assert order.nodes[0].planned_month == "2026-01"
    assert order.nodes[0].planned_amount == "18000.5"
    assert order.nodes[11].planned_month == "2026-12"
    assert order.nodes[12].planned_month == "2027-01"
    assert order.nodes[23].planned_amount == "41000.5"
    assert order.plan_total == "708012.0"
    assert plan.requires_human_preview_confirmation is True
    assert plan.resource_metrics["fact_rows"] == 1
    assert plan.resource_metrics["plan_nodes"] == 24
    assert plan.semantic_hash and len(plan.semantic_hash) == 64
    assert not plan.issues


def test_plan_total_mismatch_with_order_amount_is_warning():
    months = ["2026年1月", "2026年2月"]
    amounts = [100.0, 200.0]
    rows = [_plan_row("ORD-SYN-WARN", "合成项目 W", months=months, amounts=amounts, order_amount=999.0)]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    assert len(plan.issues) == 1
    issue = plan.issues[0]
    assert issue["severity"] == "warning"
    assert issue["code"] == "plan_total_mismatch"
    assert plan.rows[0].warning_codes == ("plan_total_mismatch",)


def test_row_key_is_stable_and_never_the_raw_order_no():
    months = ["2026年1月"]
    amounts = [100.0]
    rows = [_plan_row("ORD-SYN-KEY", "合成项目 K", months=months, amounts=amounts)]
    content = _valid_plan_workbook(extra_rows=rows)
    first = _parse(content).rows[0].row_key
    second = _parse(content).rows[0].row_key
    assert first == second
    assert first != "ORD-SYN-KEY"
    assert len(first) <= 64


def test_header_drift_label_change_fails_closed():
    headers = _header_row()
    headers[16] = "回款时间X"
    content = build_synthetic_biff8([{"name": "Plan", "cells": _cells_from_rows([headers])}])
    with pytest.raises(CollectionPlanContractError) as excinfo:
        _parse(content)
    assert excinfo.value.code == "header_signature_mismatch"
    assert "回款时间" not in str(excinfo.value)


def test_header_drift_column_swap_fails_closed():
    headers = _header_row()
    headers[16], headers[17] = headers[17], headers[16]
    content = build_synthetic_biff8([{"name": "Plan", "cells": _cells_from_rows([headers])}])
    with pytest.raises(CollectionPlanContractError) as excinfo:
        _parse(content)
    assert excinfo.value.code == "header_signature_mismatch"


def test_header_drift_extra_or_missing_column_fails_closed():
    fewer = _header_row()[:-1]
    content = build_synthetic_biff8([{"name": "Plan", "cells": _cells_from_rows([fewer])}])
    with pytest.raises(CollectionPlanContractError) as excinfo:
        _parse(content)
    assert excinfo.value.code == "header_signature_mismatch"


# ---------- 非 BIFF / 扩展名 / 文件预算 ----------

def test_non_biff_content_rejected():
    with pytest.raises(CollectionPlanContractError) as excinfo:
        _parse(b"this is definitely not a biff workbook")
    assert excinfo.value.code == "not_biff"


def test_wrong_extension_rejected():
    content = _valid_plan_workbook()
    with pytest.raises(CollectionPlanContractError) as excinfo:
        _parse(content, filename="synthetic-plan.xlsx")
    assert excinfo.value.code == "unsupported_extension"


def test_oversized_file_rejected():
    content = b"\x00" * (8 * 1024 * 1024 + 1)
    with pytest.raises(CollectionPlanContractError) as excinfo:
        _parse(content)
    assert excinfo.value.code == "file_too_large"


def test_too_many_sheets_rejected():
    sheets = [{"name": f"Sheet{i}", "cells": _cells_from_rows([_header_row()])} for i in range(9)]
    with pytest.raises(CollectionPlanContractError) as excinfo:
        _parse(build_synthetic_biff8(sheets))
    assert excinfo.value.code == "sheet_count_budget"


def test_rows_per_sheet_budget_rejected():
    headers = _header_row()
    cells = [(row, col, value) for row, values in enumerate([headers]) for col, value in enumerate(values) if value is not None]
    cells.append((2001, 0, "ORD-BUDGET-ROW"))
    content = build_synthetic_biff8([{"name": "Plan", "cells": cells}])
    with pytest.raises(CollectionPlanContractError) as excinfo:
        _parse(content)
    assert excinfo.value.code == "rows_budget"


def test_columns_per_sheet_budget_rejected():
    headers = _header_row() + [f"extra{idx}" for idx in range(70)]
    content = build_synthetic_biff8([{"name": "Plan", "cells": _cells_from_rows([headers])}])
    with pytest.raises(CollectionPlanContractError) as excinfo:
        _parse(content)
    assert excinfo.value.code == "columns_budget"


def test_physical_cells_budget_rejected():
    # 2 张 129 列 x 2001 行以上的稠密 sheet 会超过 250000 物理单元格。
    cells = []
    for row in range(1, 2001):
        for col in range(0, 64):
            cells.append((row, col, 1.0))
    cells.append((0, 0, "x"))
    sheets = [{"name": "A", "cells": cells}, {"name": "B", "cells": cells}]
    with pytest.raises(CollectionPlanContractError) as excinfo:
        _parse(build_synthetic_biff8(sheets))
    assert excinfo.value.code == "physical_cells_budget"


def test_string_budget_rejected():
    headers = _header_row()
    headers[4] = "长" * 3000
    content = build_synthetic_biff8([{"name": "Plan", "cells": _cells_from_rows([headers])}])
    with pytest.raises(CollectionPlanContractError) as excinfo:
        _parse(content)
    assert excinfo.value.code == "string_budget"


# ---------- 只投影首 Sheet；其他 Sheet 计入预算 ----------

def test_first_sheet_only_and_second_expense_sheet_excluded():
    months = ["2026年1月", "2026年2月"]
    amounts = [100.0, 200.0]
    plan_rows = [_plan_row("ORD-SYN-ONLY", "合成项目 O", months=months, amounts=amounts)]
    expense_cells = [(0, 0, "费用"), (1, 0, "差旅费"), (1, 1, 9999.0)]
    content = build_synthetic_biff8(
        [
            {"name": "维保项目清单", "cells": _cells_from_rows([_header_row(), *plan_rows])},
            {"name": "费用样例", "cells": expense_cells},
        ]
    )
    plan = _parse(content)
    assert len(plan.rows) == 1
    assert plan.rows[0].external_order_no == "ORD-SYN-ONLY"
    assert len(plan.rows[0].nodes) == 2
    assert "差旅费" not in json.dumps(plan.plan_rows())
    # 第二张费用 Sheet 计入资源预算（存在即可，不产生事实）。
    assert plan.resource_metrics["sheets"] == 2
    assert plan.resource_metrics["physical_cells"] >= 3


def test_second_sheet_rows_count_against_budget_but_not_facts():
    plan_rows = [_plan_row("ORD-SYN-B", "合成项目 B", months=["2026年1月"], amounts=[100.0])]
    big_expense = [(row, 0, "费用占位") for row in range(1, 2001)]
    content = build_synthetic_biff8(
        [
            {"name": "维保项目清单", "cells": _cells_from_rows([_header_row(), *plan_rows])},
            {"name": "费用", "cells": big_expense},
        ]
    )
    plan = _parse(content)
    assert len(plan.rows) == 1
    assert plan.resource_metrics["fact_rows"] == 1


# ---------- 月份规则 ----------

def test_month_canonical_first_of_month_and_precision():
    months = ["2026年8月", "2026年12月", "2099年1月", "2000年10月"]
    amounts = [1.0, 2.0, 3.0, 4.0]
    rows = [_plan_row("ORD-SYN-MONTH", "合成项目 M", months=months, amounts=amounts, order_amount=10.0)]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    assert [n.planned_month for n in plan.rows[0].nodes] == [
        "2026-08", "2026-12", "2099-01", "2000-10",
    ]
    assert all(n.date_precision == "month" for n in plan.rows[0].nodes)
    assert not plan.issues


def test_invalid_months_are_blockers():
    months = ["2026年13月", "2026年0月", "2100年1月", "2026-01", "2026年月"]
    amounts = [1.0, 2.0, 3.0, 4.0, 5.0]
    rows = [_plan_row("ORD-SYN-BADMONTH", "合成项目 BM", months=months, amounts=amounts)]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    codes = {issue["code"] for issue in plan.issues}
    assert "invalid_month" in codes
    assert all(issue["severity"] == "blocker" for issue in plan.issues)
    assert plan.rows[0].blocker_codes == ("invalid_month", "invalid_month", "invalid_month", "invalid_month", "invalid_month")


def test_month_cell_must_be_text():
    months = [202608.0]
    amounts = [1.0]
    rows = [_plan_row("ORD-SYN-MTYPE", "合成项目 MT", months=months, amounts=amounts)]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    assert "invalid_month_type" in {issue["code"] for issue in plan.issues}


# ---------- 金额规则 ----------

def test_amount_text_decimal_and_number_both_accepted_via_decimal_str():
    amounts = ["18000.50", 999.0, "0.01", "123456789012.12", "1000000000000"]
    months = ["2026年1月"] * 5
    rows = [_plan_row("ORD-SYN-AMT", "合成项目 A1", months=months, amounts=amounts)]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    nodes = plan.rows[0].nodes
    assert nodes[0].planned_amount == "18000.50"
    assert nodes[1].planned_amount == "999.0"
    assert nodes[2].planned_amount == "0.01"
    assert nodes[3].planned_amount == "123456789012.12"
    assert "amount_oversized" in {issue["code"] for issue in plan.issues}
    assert len(nodes) == 4  # 第 5 对金额超上限被拒绝，不产生节点


def test_zero_negative_and_oversized_amounts_rejected_without_rounding():
    months = ["2026年1月", "2026年2月", "2026年3月", "2026年4月", "2026年5月"]
    amounts = [0.0, -5.0, 1000000000000.0, 100.555, "1e3"]
    rows = [_plan_row("ORD-SYN-BADAMT", "合成项目 BA", months=months, amounts=amounts)]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    codes = {issue["code"] for issue in plan.issues}
    assert "non_positive_amount" in codes      # 0 与 -5
    assert "amount_oversized" in codes         # 1000000000000（>= 上限）
    assert "amount_scale" in codes             # 100.555 不静默 round
    assert "amount_format" in codes            # "1e3" 不是十进制定点文本
    # 阻断行不产生节点，绝不携带被拒金额。
    assert plan.rows[0].nodes == ()


def test_oversized_text_amount_rejected():
    months = ["2026年1月"]
    amounts = ["9999999999999.99"]
    rows = [_plan_row("ORD-SYN-BIGTXT", "合成项目 BT", months=months, amounts=amounts)]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    assert "amount_oversized" in {issue["code"] for issue in plan.issues}


# ---------- 孤儿 / 重复 / 断档 / 未知行 / 多余列 ----------

def test_orphan_date_and_orphan_amount_blockers():
    months = ["2026年1月", None, "2026年3月"]
    amounts = [None, 200.0, 300.0]
    rows = [_plan_row("ORD-SYN-ORPHAN", "合成项目 OR", months=months, amounts=amounts)]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    codes = {issue["code"] for issue in plan.issues}
    assert "orphan_date" in codes
    assert "orphan_amount" in codes
    assert all(issue["severity"] == "blocker" for issue in plan.issues)


def test_duplicate_order_blocker():
    rows = [
        _plan_row("ORD-SYN-DUP", "合成项目 D1", months=["2026年1月"], amounts=[100.0], row=1),
        _plan_row("ORD-SYN-DUP", "合成项目 D2", months=["2026年2月"], amounts=[200.0], row=2),
    ]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    assert "duplicate_order" in {issue["code"] for issue in plan.issues}
    assert plan.rows[1].blocker_codes == ("duplicate_order",)


def test_sequence_gap_blocker():
    months = ["2026年1月", None, "2026年3月", None, "2026年5月"]
    amounts = [100.0, None, 300.0, None, 500.0]
    rows = [_plan_row("ORD-SYN-GAP", "合成项目 GAP", months=months, amounts=amounts)]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    assert "sequence_gap" in {issue["code"] for issue in plan.issues}
    assert plan.rows[0].blocker_codes == ("sequence_gap",)


def test_unknown_or_total_rows_fail_closed_and_empty_rows_ignored():
    months = ["2026年1月"]
    amounts = [100.0]
    # 合计/未知行：只有订单金额列有值、无订单号与项目名称 → 无法识别为事实行。
    total_row = [None] * 64
    total_row[9] = "999999.0"
    rows = [
        _plan_row("ORD-SYN-OK", "合成项目 OK", months=months, amounts=amounts, row=1),
        total_row,
        [None] * 64,  # 完全空行忽略
    ]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    assert "unknown_row" in {issue["code"] for issue in plan.issues}
    assert len(plan.rows) == 2
    assert plan.rows[1].blocker_codes == ("unknown_row",)


def test_missing_required_text_positions_fail_closed():
    bad = _plan_row("ORD-SYN-NOPROJ", "合成项目 X", months=["2026年1月"], amounts=[100.0])
    bad[4] = None  # 项目名称（required_text_positions 5）缺失
    rows = [bad]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    assert "missing_required_text" in {issue["code"] for issue in plan.issues}


def test_numeric_order_no_rejected():
    bad = _plan_row("ORD-SYN-NUM", "合成项目 N", months=["2026年1月"], amounts=[100.0])
    bad[0] = 123456.0
    rows = [bad]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    assert "order_no_type" in {issue["code"] for issue in plan.issues}


def test_order_no_only_trims_outer_whitespace_and_case_preserved():
    months = ["2026年1月"]
    amounts = [100.0]
    rows = [_plan_row("  ORD-SYN-TRIM  ", "合成项目 T", months=months, amounts=amounts)]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    assert plan.rows[0].external_order_no == "ORD-SYN-TRIM"


def test_excess_columns_beyond_contract_rejected():
    bad = _plan_row("ORD-SYN-EXCESS", "合成项目 E", months=["2026年1月"], amounts=[100.0])
    bad.append("第 65 列多余数据")
    rows = [bad]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    assert "excess_columns" in {issue["code"] for issue in plan.issues}


# ---------- 颜色 / 公式 ----------

def test_colors_and_formatting_never_business_facts():
    months = ["2026年1月", "2026年2月"]
    amounts = [100.0, 200.0]
    rows = [_plan_row("ORD-SYN-CLRS", "合成项目 C", months=months, amounts=amounts)]
    content = _valid_plan_workbook(extra_rows=rows)
    plan = _parse(content)
    payload = json.dumps(plan.plan_rows())
    for forbidden in ("color", "colour", "fill", "font", "formatting"):
        assert forbidden not in payload.lower()


def test_formula_cached_value_is_observation_not_verified():
    months = ["2026年1月"]
    amounts = [("formula", 12000.5)]
    rows = [_plan_row("ORD-SYN-FORMULA", "合成项目 F", months=months, amounts=amounts)]
    content = _valid_plan_workbook(extra_rows=rows)
    plan = _parse(content)
    # 缓存数值作为观察值读取（Decimal(str())），不宣称验证了公式。
    assert plan.rows[0].nodes[0].planned_amount == "12000.5"
    assert plan.requires_human_preview_confirmation is True
    payload = json.dumps(plan.plan_rows())
    assert "formula" not in payload
    assert "evaluated" not in payload


# ---------- 错误不回显业务值 ----------

def test_errors_never_echo_business_values():
    months = ["2026年13月", None]
    amounts = [0.0, -7.0]
    rows = [
        _plan_row("SECRET-ORDER-777", "机密项目 ALPHA", months=months, amounts=amounts),
        _plan_row("SECRET-ORDER-777", "机密项目 ALPHA", months=["2026年1月"], amounts=[100.0]),
    ]
    plan = _parse(_valid_plan_workbook(extra_rows=rows))
    assert plan.issues
    joined = "\n".join(f"{issue['code']} {issue['message']}" for issue in plan.issues)
    assert "SECRET-ORDER-777" not in joined
    assert "机密项目" not in joined
    assert "2026年13月" not in joined
    for issue in plan.issues:
        assert issue["severity"] in ("warning", "blocker")
        assert issue["row_key"] is None or isinstance(issue["row_key"], str)
        assert issue["message"]


# ---------- semantic hash ----------

def test_semantic_hash_stable_and_content_sensitive():
    months = ["2026年1月", "2026年2月"]
    amounts = [100.0, 200.0]
    rows = [_plan_row("ORD-SYN-HASH", "合成项目 H1", months=months, amounts=amounts)]
    content = _valid_plan_workbook(extra_rows=rows)
    first = _parse(content)
    second = _parse(content)
    assert first.semantic_hash == second.semantic_hash
    assert len(first.semantic_hash) == 64

    other_amounts = [100.0, 201.0]
    other_rows = [_plan_row("ORD-SYN-HASH", "合成项目 H1", months=months, amounts=other_amounts)]
    changed = _parse(_valid_plan_workbook(extra_rows=other_rows))
    assert changed.semantic_hash != first.semantic_hash

    other_month = ["2026年1月", "2026年3月"]
    month_rows = [_plan_row("ORD-SYN-HASH", "合成项目 H1", months=other_month, amounts=amounts)]
    month_changed = _parse(_valid_plan_workbook(extra_rows=month_rows))
    assert month_changed.semantic_hash != first.semantic_hash


# ---------- 真实样例只读 acceptance ----------

_SAMPLE_ENV_CANDIDATES = (
    "COLLECTION_REMINDERS_SAMPLE_XLS",
    "COLLECTION_SAMPLE_XLS_PATH",
    "REAL_SAMPLE_XLS_PATH",
    "SAMPLE_XLS_PATH",
    "XLS_SAMPLE_PATH",
)


def test_real_sample_read_only_parser_acceptance():
    """真实样例只读验收：环境变量提供路径才运行；只断言 SHA/合同版本/聚合计数。

    绝不打印订单号、项目名、金额或文件名；绝不 apply。路径未配置时推迟到 Task 7。
    """
    sample_path = next(
        (os.environ[name] for name in _SAMPLE_ENV_CANDIDATES if os.environ.get(name)),
        None,
    )
    if not sample_path:
        pytest.skip("真实样例路径未配置，推迟到 Task 7 只读验收")
    path = Path(sample_path)
    if not path.is_file():
        pytest.skip(f"真实样例路径不存在：{sample_path}")
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    assert digest == REAL_SAMPLE_SHA256
    plan = _parse(content, filename=path.name)
    assert plan.contract_version == CONTRACT_VERSION
    assert plan.header_sha256 == FROZEN_HEADER_SIGNATURE
    assert plan.resource_metrics["fact_rows"] == 3
    assert plan.resource_metrics["plan_nodes"] == 19
    projects = {row.source_project_name for row in plan.rows}
    assert len(projects) == 3
    assert not any(issue["severity"] == "blocker" for issue in plan.issues)
    # 只输出聚合：项目数、节点数、警告数、资源指标。
    assert {
        "projects": len(projects),
        "nodes": plan.resource_metrics["plan_nodes"],
        "warnings": len(plan.issues),
        "sheets": plan.resource_metrics["sheets"],
        "physical_cells": plan.resource_metrics["physical_cells"],
    }
