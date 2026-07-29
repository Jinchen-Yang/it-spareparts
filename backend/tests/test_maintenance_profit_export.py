"""合同详细盈亏在看板、CSV、单本和批量工作簿之间的公开契约。"""

import csv
import io
from datetime import date
from decimal import Decimal
from zipfile import ZipFile

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from app.main import app
from app.models.maintenance import (
    FMaintenanceLine,
    FProjectExpense,
    MaintenanceContractWorkbookState,
)
from app.models.sales import FSalesOrder
from tests.test_maintenance_export_headers import (
    _admin_client,
    _cost_blind_maintenance_client,
    _profit_blind_maintenance_client,
    _readonly_client,
)
from tests.test_maintenance_margin_integration import _load_complete_contract


_WORKBOOK_LABELS = {
    "revenue_inc": "合同收入（含税）",
    "revenue_ex": "合同收入（未税）",
    "expense_inc": "报销费用（含税）",
    "expense_ex": "报销费用（未税）",
    "parts_gross_profit_inc": "合同级备件毛利（含税）",
    "parts_gross_profit_ex": "合同级备件毛利（未税）",
    "parts_gross_margin_inc": "合同级备件毛利率（含税）",
    "parts_gross_margin_ex": "合同级备件毛利率（未税）",
    "contribution_profit_inc": "合同级贡献毛利（含税）",
    "contribution_profit_ex": "合同级贡献毛利（未税）",
    "contribution_margin_inc": "合同级贡献毛利率（含税）",
    "contribution_margin_ex": "合同级贡献毛利率（未税）",
}
_CENT = Decimal("0.01")
_RATE = Decimal("0.0001")


def _summary(workbook) -> dict[str, object]:
    values: dict[str, object] = {}
    for row in workbook["项目预算"].iter_rows(min_row=3, values_only=True):
        if row[0]:
            values[str(row[0])] = row[1]
        if len(row) > 2 and row[2]:
            values[str(row[2])] = row[3]
    return values


def _csv_row(response) -> dict[str, str]:
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 1
    return rows[0]


def _assert_numeric_parity(
    board: dict,
    csv_row: dict[str, str],
    *workbook_summaries: dict[str, object],
) -> None:
    for field, label in _WORKBOOK_LABELS.items():
        quantum = _RATE if "margin" in field else _CENT
        board_value = _quantized(board[field], quantum)
        csv_value = _quantized(csv_row[field], quantum)
        assert csv_value == board_value
        for summary in workbook_summaries:
            workbook_value = _quantized(summary[label], quantum)
            assert workbook_value == board_value


def _quantized(value: object, quantum: Decimal) -> Decimal | None:
    if value in (None, "", "—"):
        return None
    return Decimal(str(value)).quantize(quantum)


def _four_carriers(
    client: TestClient,
    *,
    board_params: dict[str, str] | None = None,
) -> tuple[dict, dict[str, str], dict[str, object], dict[str, object]]:
    board_params = {"lifecycle": "all", **(board_params or {})}
    date_params = {
        key: board_params[key]
        for key in ("date_from", "date_to")
        if key in board_params
    }
    board_response = client.get("/api/maintenance/board", params=board_params)
    csv_response = client.get("/api/maintenance/board/export", params=board_params)
    single_response = client.get(
        "/api/maintenance/export-workbook",
        params={"contract": "XS-MARGIN", **date_params},
    )
    bundle_response = client.get(
        "/api/maintenance/export-workbooks",
        params=date_params,
    )

    assert board_response.status_code == 200, board_response.text
    assert csv_response.status_code == 200, csv_response.text
    assert single_response.status_code == 200, single_response.text
    assert bundle_response.status_code == 200, bundle_response.text
    board_rows = board_response.json()["rows"]
    assert len(board_rows) == 1
    single = load_workbook(io.BytesIO(single_response.content), data_only=False)
    try:
        single_summary = _summary(single)
    finally:
        single.close()
    with ZipFile(io.BytesIO(bundle_response.content)) as archive:
        member = next(
            name for name in archive.namelist()
            if name.startswith("项目工作簿/")
        )
        bundled = load_workbook(
            io.BytesIO(archive.read(member)),
            data_only=False,
        )
    try:
        bundled_summary = _summary(bundled)
    finally:
        bundled.close()
    return board_rows[0], _csv_row(csv_response), single_summary, bundled_summary


def test_rounding_boundary_has_decimal_parity_across_all_four_carriers(db):
    batch = _load_complete_contract(
        db,
        purchase_unit_price=Decimal("100.01"),
    )
    sales_order = db.scalar(
        select(FSalesOrder).where(FSalesOrder.order_no == "XS-MARGIN"),
    )
    assert sales_order is not None
    sales_order.amount_ex_tax = Decimal("1000.05")
    db.add_all([
        FProjectExpense(
            raw_line_id="EXP-MARGIN-PARITY",
            bxd_no="BXD-MARGIN-PARITY",
            line_no=1,
            linked_sales_order_no="XS-MARGIN",
            data_status="已结束",
            expense_date=date(2026, 3, 12),
            amount=Decimal("0.05"),
            amount_ex_tax=Decimal("0.05"),
            amount_inc_tax=Decimal("0.06"),
            tax_basis="ex",
            import_batch_id=batch.id,
        ),
        MaintenanceContractWorkbookState(
            contract_no="XS-MARGIN",
            revision=1,
            expense_snapshot_complete=True,
        ),
    ])
    db.commit()
    board, csv_data, single, bundled = _four_carriers(_admin_client(db))

    assert board["parts_profit_status_inc"] == "complete_actual"
    assert board["parts_profit_status_ex"] == "complete_actual"
    assert board["contribution_status_inc"] == "complete"
    assert board["contribution_status_ex"] == "complete"
    assert csv_data["成本证据状态"] == "actual_only"
    assert csv_data["费用证据状态"] == "complete"
    assert _quantized(board["revenue_inc"], _CENT) == Decimal("1130.06")
    assert _quantized(board["revenue_ex"], _CENT) == Decimal("1000.05")
    assert _quantized(board["parts_cost_inc_tax"], _CENT) == Decimal("226.02")
    assert _quantized(board["parts_cost_ex_tax"], _CENT) == Decimal("200.02")
    assert _quantized(board["expense_inc"], _CENT) == Decimal("0.06")
    assert _quantized(board["expense_ex"], _CENT) == Decimal("0.05")
    assert _quantized(board["contribution_profit_inc"], _CENT) == Decimal(
        "903.98",
    )
    assert _quantized(board["contribution_profit_ex"], _CENT) == Decimal(
        "799.98",
    )
    _assert_numeric_parity(
        board,
        csv_data,
        single,
        bundled,
    )


def test_contract_profit_csv_matches_board_status_and_dual_cost_evidence(db):
    _load_complete_contract(db)
    client = _admin_client(db)

    matching_board = client.get(
        "/api/maintenance/board",
        params={"lifecycle": "all", "status": "expense_data_unavailable"},
    )
    matching_csv = client.get(
        "/api/maintenance/board/export",
        params={"lifecycle": "all", "status": "expense_data_unavailable"},
    )
    excluded_board = client.get(
        "/api/maintenance/board",
        params={"lifecycle": "all", "status": "incomplete_cost"},
    )
    excluded_csv = client.get(
        "/api/maintenance/board/export",
        params={"lifecycle": "all", "status": "incomplete_cost"},
    )

    assert matching_board.status_code == 200, matching_board.text
    assert matching_csv.status_code == 200, matching_csv.text
    assert len(matching_board.json()["rows"]) == 1
    matching_row = _csv_row(matching_csv)
    assert matching_row["成本证据状态-含税"] == "actual_only"
    assert matching_row["成本证据状态-未税"] == "actual_only"
    assert excluded_board.status_code == 200, excluded_board.text
    assert excluded_board.json()["rows"] == []
    assert excluded_csv.status_code == 422
    assert excluded_csv.json()["detail"] == "所选范围内没有可导出的合同详细盈亏数据"


def test_expense_unavailable_is_consistent_across_all_four_carriers(db):
    _load_complete_contract(db)

    board, csv_data, single, bundled = _four_carriers(_admin_client(db))

    assert board["expense_inc"] is None
    assert board["expense_ex"] is None
    assert board["contribution_status_inc"] == "expense_data_unavailable"
    assert board["contribution_status_ex"] == "expense_data_unavailable"
    assert csv_data["费用证据状态"] == "expense_data_unavailable"
    assert single["费用证据状态"] == "未就绪（无记录不等于0）"
    assert bundled["费用证据状态"] == "未就绪（无记录不等于0）"
    _assert_numeric_parity(board, csv_data, single, bundled)


def test_unknown_expense_tax_evidence_fails_closed_across_all_four_carriers(db):
    batch = _load_complete_contract(db)
    db.add_all([
        FProjectExpense(
            raw_line_id="EXP-MARGIN-TAX-UNKNOWN",
            bxd_no="BXD-MARGIN-TAX-UNKNOWN",
            line_no=1,
            linked_sales_order_no="XS-MARGIN",
            data_status="已结束",
            expense_date=date(2026, 3, 12),
            amount=None,
            amount_ex_tax=None,
            amount_inc_tax=None,
            import_batch_id=batch.id,
        ),
        MaintenanceContractWorkbookState(
            contract_no="XS-MARGIN",
            revision=1,
            expense_snapshot_complete=True,
        ),
    ])
    db.commit()

    board, csv_data, single, bundled = _four_carriers(_admin_client(db))

    assert board["expense_inc"] is None
    assert board["expense_ex"] is None
    assert board["contribution_status_inc"] == "expense_tax_unknown"
    assert board["contribution_status_ex"] == "expense_tax_unknown"
    assert csv_data["费用证据状态"] == "expense_tax_unknown"
    assert single["费用证据状态"] == "费用税务口径缺失"
    assert bundled["费用证据状态"] == "费用税务口径缺失"
    _assert_numeric_parity(board, csv_data, single, bundled)


def test_missing_cost_fails_closed_across_all_four_carriers(db):
    _load_complete_contract(db)
    line = db.scalar(select(FMaintenanceLine))
    assert line is not None
    line.unit_cost = None
    line.cost_amount = None
    line.unit_cost_inc_tax = None
    line.unit_cost_ex_tax = None
    line.cost_amount_inc_tax = None
    line.cost_amount_ex_tax = None
    line.cost_source = "none"
    line.cost_tax_basis = None
    db.add(MaintenanceContractWorkbookState(
        contract_no="XS-MARGIN",
        revision=1,
        expense_snapshot_complete=True,
    ))
    db.commit()

    board, csv_data, single, bundled = _four_carriers(_admin_client(db))

    assert board["parts_profit_status_inc"] == "incomplete_cost"
    assert board["parts_profit_status_ex"] == "incomplete_cost"
    assert board["contribution_status_inc"] == "incomplete_cost"
    assert board["contribution_status_ex"] == "incomplete_cost"
    assert csv_data["成本证据状态-含税"] == "incomplete"
    assert csv_data["成本证据状态-未税"] == "incomplete"
    assert single["含税口径质量"] == "成本不完整，需补数据"
    assert single["未税口径质量"] == "成本不完整，需补数据"
    assert bundled["含税口径质量"] == "成本不完整，需补数据"
    assert bundled["未税口径质量"] == "成本不完整，需补数据"
    _assert_numeric_parity(board, csv_data, single, bundled)


def test_date_filter_keeps_the_same_scope_across_all_four_carriers(db):
    batch = _load_complete_contract(db)
    db.add_all([
        FProjectExpense(
            raw_line_id="EXP-MARGIN-IN-RANGE",
            bxd_no="BXD-MARGIN-IN-RANGE",
            line_no=1,
            linked_sales_order_no="XS-MARGIN",
            data_status="已结束",
            expense_date=date(2026, 3, 12),
            amount=Decimal("10"),
            amount_ex_tax=Decimal("10"),
            amount_inc_tax=Decimal("11.30"),
            tax_basis="ex",
            import_batch_id=batch.id,
        ),
        FProjectExpense(
            raw_line_id="EXP-MARGIN-OUTSIDE-RANGE",
            bxd_no="BXD-MARGIN-OUTSIDE-RANGE",
            line_no=1,
            linked_sales_order_no="XS-MARGIN",
            data_status="已结束",
            expense_date=date(2026, 4, 12),
            amount=Decimal("20"),
            amount_ex_tax=Decimal("20"),
            amount_inc_tax=Decimal("22.60"),
            tax_basis="ex",
            import_batch_id=batch.id,
        ),
        MaintenanceContractWorkbookState(
            contract_no="XS-MARGIN",
            revision=1,
            expense_snapshot_complete=True,
        ),
    ])
    db.commit()

    board, csv_data, single, bundled = _four_carriers(
        _admin_client(db),
        board_params={
            "date_from": "2026-03-01",
            "date_to": "2026-03-31",
        },
    )

    assert board["expense_inc"] == 11.3
    assert board["expense_ex"] == 10.0
    assert board["parts_profit_status_inc"] == "filtered_scope"
    assert board["parts_profit_status_ex"] == "filtered_scope"
    assert board["contribution_status_inc"] == "filtered_scope"
    assert board["contribution_status_ex"] == "filtered_scope"
    assert csv_data["费用证据状态"] == "complete"
    assert single["费用证据状态"] == "完整"
    assert bundled["费用证据状态"] == "完整"
    _assert_numeric_parity(board, csv_data, single, bundled)


def test_contract_profit_csv_matches_board_lifecycle_and_project_search(db):
    _load_complete_contract(db)
    client = _admin_client(db)

    matching = {"lifecycle": "ongoing", "q": "双口径毛利"}
    matching_board = client.get("/api/maintenance/board", params=matching)
    matching_csv = client.get("/api/maintenance/board/export", params=matching)
    ended = {"lifecycle": "ended", "q": "双口径毛利"}
    ended_board = client.get("/api/maintenance/board", params=ended)
    ended_csv = client.get("/api/maintenance/board/export", params=ended)
    missing_query = {"lifecycle": "all", "q": "不存在的项目"}
    missing_board = client.get("/api/maintenance/board", params=missing_query)
    missing_csv = client.get(
        "/api/maintenance/board/export",
        params=missing_query,
    )

    assert matching_board.status_code == 200, matching_board.text
    assert matching_csv.status_code == 200, matching_csv.text
    assert [row["contract"] for row in matching_board.json()["rows"]] == [
        "XS-MARGIN",
    ]
    assert _csv_row(matching_csv)["合同"] == "XS-MARGIN"
    for board_response, csv_response in (
        (ended_board, ended_csv),
        (missing_board, missing_csv),
    ):
        assert board_response.status_code == 200, board_response.text
        assert board_response.json()["rows"] == []
        assert csv_response.status_code == 422
        assert (
            csv_response.json()["detail"]
            == "所选范围内没有可导出的合同详细盈亏数据"
        )


def test_contract_profit_csv_has_explicit_empty_and_access_semantics(db):
    admin_response = _admin_client(db).get(
        "/api/maintenance/board/export",
        params={"lifecycle": "all"},
    )
    anonymous_response = TestClient(app).get(
        "/api/maintenance/board/export",
        params={"lifecycle": "all"},
    )
    no_page_response = _readonly_client(db).get(
        "/api/maintenance/board/export",
        params={"lifecycle": "all"},
    )

    assert admin_response.status_code == 422
    assert (
        admin_response.json()["detail"]
        == "所选范围内没有可导出的合同详细盈亏数据"
    )
    assert anonymous_response.status_code == 401
    assert no_page_response.status_code == 403


def test_contract_profit_csv_masks_profit_and_evidence_for_profit_blind_user(db):
    _load_complete_contract(db)
    client = _profit_blind_maintenance_client(db)

    response = client.get(
        "/api/maintenance/board/export",
        params={"lifecycle": "all"},
    )

    assert response.status_code == 200, response.text
    row = _csv_row(response)
    for field in _WORKBOOK_LABELS:
        assert row[field] == ""
    assert row["成本证据状态"] == "actual_only"
    assert row["成本证据状态-含税"] == "actual_only"
    assert row["成本证据状态-未税"] == "actual_only"
    assert row["收入证据状态-含税"] == "restricted"
    assert row["收入证据状态-未税"] == "restricted"
    assert row["费用证据状态"] == "restricted"


def test_contract_profit_csv_masks_cost_and_derived_profit_for_cost_blind_user(db):
    _load_complete_contract(db)

    response = _cost_blind_maintenance_client(db).get(
        "/api/maintenance/board/export",
        params={"lifecycle": "all"},
    )

    assert response.status_code == 200, response.text
    row = _csv_row(response)
    for field in _WORKBOOK_LABELS:
        assert row[field] == ""
    assert row["成本证据状态"] == ""
    assert row["成本证据状态-含税"] == ""
    assert row["成本证据状态-未税"] == ""
    assert row["收入证据状态-含税"] == "restricted"
    assert row["收入证据状态-未税"] == "restricted"
    assert row["费用证据状态"] == "restricted"
