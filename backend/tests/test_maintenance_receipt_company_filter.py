"""D-28: filter a company-wide receipt export without dropping financial legs."""

from datetime import date
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.models.maintenance_project_operations import MaintenanceCollectionReceipt
from app.services import maintenance_bulk_import as bulk
from tests.test_maintenance_bulk_import import _ScalarsOnlyDb, _sheet
from tests.test_maintenance_receipt_ledger_import import (
    ORDER_NO, _apply, _preview, _project_with_contract,
)
from tests.test_maintenance_receipt_scope_and_receipt_level_failclosed import _xlsx


FIELDS = {
    "order_no": 0, "receipt_no": 1, "receipt_date": 2,
    "actual_amount": 3, "receipt_status": 4, "business_type": 5,
    "remark": 6, "document_remark": 7,
    "gross_amount": 8, "discount_amount": 9,
}


def _row(order, receipt, amount="10", *, business="备件销售", status="已生效",
         day=date(2026, 1, 10), gross=None, discount=None, remark="", document=""):
    return (order, receipt, day, amount, status, business, remark, document, gross, discount)


def _plan(monkeypatch, rows):
    contract = SimpleNamespace(
        project_id="project-1", project_contract_id="relation-1",
        contract_no=ORDER_NO, version=1,
    )
    norm = bulk.normalize_order_no(ORDER_NO)
    monkeypatch.setattr(bulk, "_contract_maps", lambda _db: ({norm: [contract]}, {norm: contract}))
    return bulk.ReceiptCollectionAdapter().build_plan(
        _ScalarsOnlyDb(), _sheet(FIELDS, list(enumerate(rows, start=3)))
    )


def test_company_filter_keeps_real_totals_and_bounded_samples(monkeypatch):
    rows = [_row(f"OTHER-{i}", f"OTHER-SKD-{i}") for i in range(105)]
    rows += [_row("", "NO-ORDER", "-2.25"), _row("UNKNOWN", "NO-AMOUNT", None)]
    rows += [_row(ORDER_NO, "MAINT", "100", business="备件销售")]
    rows += [_row("MISSING-MAINT", "TO-CHECK", "30", business="整体维保")]
    plan = _plan(monkeypatch, rows)

    assert len(plan["rows"]) == 102
    assert plan["summary"]["source_rows"] == 109
    assert plan["summary"]["source_actions"]["unmatched"] == 108
    summary = plan["source_filter"]
    assert summary["maintenance_rows"] == 2
    assert summary["maintenance_unmatched_rows"] == 1
    assert summary["ignored_nonmaintenance_rows"] == 107
    assert summary["ignored_without_order_rows"] == 1
    assert summary["ignored_amount"] == "1047.75"
    assert summary["ignored_amount_unknown_count"] == 1
    assert summary["ignored_sample_rows"] == 100
    assert summary["ignored_samples_truncated"] is True
    pending = next(row for row in plan["rows"] if row["receipt_no"] == "TO-CHECK")
    assert pending["scope_classification"] == "maintenance_unmatched"
    assert pending["business_type"] == "整体维保"
    assert pending["action"] == "unmatched"
    assert len(plan["operations"]) == 1
    assert plan["operations"][0]["new_cumulative_amount"] == "100.00"


@pytest.mark.parametrize("peer", [
    {"amount": "-100"},
    {"amount": "100", "gross": "120"},
    {"amount": "100", "day": "not-a-date"},
    {"amount": "100", "status": "已取消"},
    {"amount": None},
])
def test_discarded_other_business_financial_leg_still_blocks_maintenance(monkeypatch, peer):
    rows = [_row(f"OTHER-{i}", f"OTHER-SKD-{i}") for i in range(105)]
    rows += [_row(ORDER_NO, "SHARED", "100")]
    rows += [_row("OTHER-NEGATIVE", "SHARED", **peer)]
    plan = _plan(monkeypatch, rows)

    assert len(plan["rows"]) == 101  # unsafe peer is outside the bounded sample
    assert plan["operations"][0]["action"] == "conflict"
    assert plan["operations"][0]["new_cumulative_amount"] is None
    assert plan["operations"][0]["issues"][0]["code"] == "receipt_level_fail_closed"
    maint = next(row for row in plan["rows"] if row["receipt_no"] == "SHARED")
    assert any(issue["code"] == "receipt_peer_invalid" for issue in maint["issues"])


def test_unmatched_maintenance_type_cannot_hide_a_negative_peer(monkeypatch):
    plan = _plan(monkeypatch, [
        _row(ORDER_NO, "SHARED", "100"),
        _row("MISSING-MAINT", "SHARED", "-100", business="备件维保"),
    ])
    assert plan["source_filter"]["maintenance_unmatched_rows"] == 1
    assert plan["operations"][0]["action"] == "conflict"


def test_positive_clean_other_business_peer_does_not_add_to_maintenance(monkeypatch):
    plan = _plan(monkeypatch, [
        _row(ORDER_NO, "SHARED", "100"),
        _row("OTHER", "SHARED", "50"),
        _row("DIFFERENT", "UNRELATED", "-90"),
    ])
    assert plan["operations"][0]["action"] == "create"
    assert plan["operations"][0]["new_cumulative_amount"] == "100.00"
    assert plan["source_filter"]["ignored_amount"] == "-40.00"


def test_duplicate_conflicting_other_leg_blocks_shared_receipt(monkeypatch):
    plan = _plan(monkeypatch, [
        _row(ORDER_NO, "SHARED", "100"),
        _row("OTHER", "SHARED", "50"),
        _row("OTHER", "SHARED", "60"),
    ])
    assert plan["operations"][0]["action"] == "conflict"


def test_zero_net_receipt_cannot_write_only_maintenance_positive_leg(db):
    _project_with_contract(db)
    preview = _preview(db, _xlsx([
        (ORDER_NO, "NET-ZERO", "2026-01-10", "100"),
        ("UNRELATED-SALES", "NET-ZERO", "2026-01-10", "-100"),
    ]))
    assert preview["summary"]["ready"] == 0
    blocked = next(row for row in preview["rows"] if row["source_row"] == 2)
    assert blocked["row_status"] == "blocked"
    assert any(issue["code"] == "receipt_level_fail_closed" for issue in blocked["errors"])
    with pytest.raises(bulk.BulkImportInvalid):
        _apply(db, preview, [blocked["row_key"]])
    db.rollback()
    assert list(db.scalars(select(MaintenanceCollectionReceipt))) == []


def test_document_and_detail_remarks_map_separately_and_survive_apply(db):
    _project_with_contract(db)
    wb = Workbook()
    ws = wb.active
    ws.append([
        "F0000007", "F0000037", "SeqNo", "F0000001", "F0000008",
        "F0000062", "Status", "F0000067",
    ])
    ws.append([
        "备注", "收款明细.备注", "收款单号", "收款日期", "收款明细.销售订单",
        "收款明细.实收金额", "数据状态", "收款明细.业务类型",
    ])
    ws.append(["已核实坏账收回", "红冲说明后实际到账", "SKD-NOTES", date(2026, 1, 10),
               ORDER_NO, 100, "已生效", "整体维保"])
    buf = BytesIO()
    wb.save(buf)
    preview = _preview(db, buf.getvalue())
    ready = [row for row in preview["rows"] if row["row_status"] == "ready"]
    assert len(ready) == 1
    assert any(issue["code"] == "receipt_remark_annotation" for issue in ready[0]["warnings"])
    assert ready[0]["errors"] == []
    _apply(db, preview, [ready[0]["row_key"]])
    receipt = db.scalar(select(MaintenanceCollectionReceipt))
    assert receipt.actual_amount == Decimal("100.00")
    assert receipt.remark == "主表备注：已核实坏账收回\n明细备注：红冲说明后实际到账"


def test_legacy_flat_remark_header_still_imports(monkeypatch):
    adapter = bulk.ReceiptCollectionAdapter()
    result = adapter.recognize(("合同编号", "凭证号", "到账日期", "到账金额", "备注"))
    assert result is not None
    _, indexes, _ = result
    sheet = _sheet(indexes, [(3, (ORDER_NO, "LEGACY", date(2026, 1, 10), "10", "原备注"))])
    assert adapter._source_remark(sheet, sheet.rows[0][1]) == "原备注"


def test_legacy_system_detail_remark_caption_is_not_lost():
    adapter = bulk.ReceiptCollectionAdapter()
    headers = ("合同编号", "凭证号", "到账日期", "到账金额", "备注")
    system = ("F0000008", "SeqNo", "F0000001", "F0000062", "F0000037")
    result = adapter.recognize(headers, system)
    assert result is not None
    _, indexes, matches = result
    assert indexes["remark"] == 4
    assert matches["remark"]["system_field"] == "F0000037"
    sheet = _sheet(indexes, [(3, (ORDER_NO, "LEGACY", date(2026, 1, 10), "10", "原明细备注"))])
    assert adapter._source_remark(sheet, sheet.rows[0][1]) == "原明细备注"


def test_legacy_caption_only_template_keeps_both_remarks():
    adapter = bulk.ReceiptCollectionAdapter()
    result = adapter.recognize(("合同编号", "凭证号", "到账日期", "到账金额", "备注", "收款明细.备注"))
    assert result is not None
    _, indexes, _ = result
    assert indexes["document_remark"] == 4
    assert indexes["remark"] == 5
