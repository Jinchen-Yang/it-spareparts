from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app import permissions, security
from app.etl import loader
from app.models.maintenance import (
    FMaintenanceLine,
    FProjectExpense,
    MaintenanceContractWorkbookState,
)
from app.models.sales import FSalesOrder
from app.models.system import SysImportBatch
from app.services import maintenance_cost, maintenance_workbook_renderer
from tests import factories as f


def _load_complete_contract(
    db,
    *,
    purchase_tax_rate: Decimal | None = Decimal("0.13"),
    purchase_is_tax_inclusive: bool = False,
    purchase_unit_price: Decimal = Decimal("100"),
):
    batch = SysImportBatch(
        filename="margin-integration.xlsx",
        file_type="maintenance",
        file_hash="margin-integration",
        status="success",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.sales_result(
            {
                "S1": f.sales_head(
                    "S1",
                    order_no="XS-MARGIN",
                    amount_ex_tax=Decimal("1000"),
                    tax_rate=Decimal("0.06"),
                ),
            },
            [],
        ),
        batch.id,
        date(2026, 7, 28),
    )
    loader.load(
        db,
        f.purchase_result(
            {
                "P1": f.purchase_head(
                    "P1",
                    on=date(2026, 3, 5),
                    source_type="维保需求",
                    linked_maintenance_order_no="WBDD-MARGIN",
                    is_tax_inclusive=purchase_is_tax_inclusive,
                    tax_rate=purchase_tax_rate,
                ),
            },
            [
                f.purchase_line(
                    "P1",
                    "PL1",
                    "PN-MARGIN",
                    qty="2",
                    price=str(purchase_unit_price),
                ),
            ],
        ),
        batch.id,
        date(2026, 7, 28),
    )
    loader.load(
        db,
        f.maintenance_result(
            {
                "M1": f.maintenance_head(
                    "M1",
                    order_no="WBDD-MARGIN",
                    on=date(2026, 3, 10),
                    project="双口径毛利项目",
                    sales_order="XS-MARGIN",
                    maint_end=date(2027, 12, 31),
                ),
            },
            [
                f.maintenance_line(
                    "M1",
                    "ML1",
                    "PN-MARGIN",
                    qty="2",
                ),
            ],
        ),
        batch.id,
        date(2026, 7, 28),
    )
    db.commit()
    maintenance_cost.recompute(db)
    return batch


def _row(db):
    return next(
        row
        for row in maintenance_cost.board(db, lifecycle="all")["rows"]
        if row["contract"] == "XS-MARGIN"
    )


def test_board_exposes_both_tax_bases_from_normalized_cost_not_legacy_mix(db):
    _load_complete_contract(db)

    row = _row(db)

    assert row["revenue_inc"] == 1130.0
    assert row["revenue_ex"] == 1000.0
    assert row["parts_cost_inc_tax"] == 226.0
    assert row["parts_cost_ex_tax"] == 200.0
    assert row["parts_gross_profit_inc"] == 904.0
    assert row["parts_gross_profit_ex"] == 800.0
    assert row["parts_gross_margin_inc"] == 0.8
    assert row["parts_gross_margin_ex"] == 0.8
    assert row["parts_profit_status_inc"] == "complete_actual"
    assert row["parts_profit_status_ex"] == "complete_actual"
    assert row["contribution_profit_inc"] is None
    assert row["contribution_profit_ex"] is None
    assert row["contribution_margin_inc"] is None
    assert row["contribution_margin_ex"] is None
    assert row["contribution_status_inc"] == "expense_data_unavailable"
    assert row["contribution_status_ex"] == "expense_data_unavailable"
    for retired_field in (
        "gross_profit_inc", "gross_profit_ex",
        "gross_margin_inc", "gross_margin_ex",
        "profit_status_inc", "profit_status_ex",
    ):
        assert retired_field not in row


def test_missing_raw_tax_rate_does_not_make_fixed_policy_an_estimate(db):
    """固定 13% 是业务事实；原始税率缺失不降低双口径质量。"""
    _load_complete_contract(db, purchase_tax_rate=None)

    row = _row(db)

    assert row["parts_cost_inc_tax"] == 226.0
    assert row["parts_cost_ex_tax"] == 200.0
    assert row["parts_cost_inc_tax_quality"] == "actual_only"
    assert row["parts_cost_ex_tax_quality"] == "actual_only"
    assert row["parts_profit_status_inc"] == "complete_actual"
    assert row["parts_profit_status_ex"] == "complete_actual"
    assert row["contribution_status_inc"] == "expense_data_unavailable"
    assert row["contribution_status_ex"] == "expense_data_unavailable"

    workbook_data = maintenance_cost.contract_workbook_data(db, "XS-MARGIN")
    assert workbook_data["dual_cost_summary"]["parts_cost_inc_tax_quality"] == (
        "actual_only"
    )
    assert workbook_data["dual_cost_summary"]["parts_cost_ex_tax_quality"] == (
        "actual_only"
    )
    assert workbook_data["margin"]["parts_profit_status_inc"] == "complete_actual"
    assert workbook_data["margin"]["parts_profit_status_ex"] == "complete_actual"
    assert (
        workbook_data["margin"]["contribution_status_inc"]
        == "expense_data_unavailable"
    )
    assert (
        workbook_data["margin"]["contribution_status_ex"]
        == "expense_data_unavailable"
    )


def test_missing_raw_tax_on_explicit_inc_price_keeps_both_bases_actual(db):
    """明确含税标记足以确定原始口径；固定 13% 生成未税值。"""
    _load_complete_contract(
        db,
        purchase_tax_rate=None,
        purchase_is_tax_inclusive=True,
        purchase_unit_price=Decimal("113"),
    )

    row = _row(db)

    assert row["parts_cost_inc_tax"] == 226.0
    assert row["parts_cost_ex_tax"] == 200.0
    assert row["parts_cost_inc_tax_quality"] == "actual_only"
    assert row["parts_cost_ex_tax_quality"] == "actual_only"
    assert row["parts_profit_status_inc"] == "complete_actual"
    assert row["parts_profit_status_ex"] == "complete_actual"
    assert row["contribution_status_inc"] == "expense_data_unavailable"
    assert row["contribution_status_ex"] == "expense_data_unavailable"


def test_typed_expense_without_complete_snapshot_blocks_contribution_margin(db):
    batch = _load_complete_contract(db)
    db.add(FProjectExpense(
        raw_line_id="EXP-MARGIN",
        linked_sales_order_no="XS-MARGIN",
        data_status="已结束",
        expense_date=date(2026, 3, 12),
        amount=Decimal("50"),
        amount_ex_tax=Decimal("50"),
        amount_inc_tax=Decimal("56.50"),
        import_batch_id=batch.id,
    ))
    db.commit()

    row = _row(db)

    assert row["parts_gross_profit_inc"] == 904.0
    assert row["parts_gross_profit_ex"] == 800.0
    assert row["parts_profit_status_inc"] == "complete_actual"
    assert row["parts_profit_status_ex"] == "complete_actual"
    assert row["contribution_profit_inc"] is None
    assert row["contribution_profit_ex"] is None
    assert row["contribution_status_inc"] == "expense_data_unavailable"
    assert row["contribution_status_ex"] == "expense_data_unavailable"


def test_date_filtered_board_never_compares_period_cost_with_full_revenue(db):
    _load_complete_contract(db)

    row = maintenance_cost.board(
        db,
        date_from=date(2026, 3, 1),
        date_to=date(2026, 3, 31),
        lifecycle="all",
    )["rows"][0]

    assert row["parts_gross_profit_inc"] is None
    assert row["parts_gross_profit_ex"] is None
    assert row["contribution_profit_inc"] is None
    assert row["contribution_profit_ex"] is None
    assert row["parts_profit_status_inc"] == "filtered_scope"
    assert row["parts_profit_status_ex"] == "filtered_scope"
    assert row["contribution_status_inc"] == "expense_data_unavailable"
    assert row["contribution_status_ex"] == "expense_data_unavailable"
    assert row["decision_status"] == "expense_data_unavailable"
    assert row["status"] == "expense_data_unavailable"
    assert row["remaining"] is None
    assert row["remaining_pct"] is None


def test_board_without_date_to_requires_expense_watermark_through_as_of(db):
    _load_complete_contract(db)
    db.add(MaintenanceContractWorkbookState(
        contract_no="XS-MARGIN",
        revision=1,
        expense_complete_through=date(2026, 3, 31),
        expense_snapshot_complete=True,
    ))
    db.commit()

    row = maintenance_cost.board(
        db,
        lifecycle="all",
        as_of=date(2026, 4, 1),
    )["rows"][0]

    assert row["expense_data_available"] is False
    assert row["contribution_status_inc"] == "expense_data_unavailable"
    assert row["contribution_status_ex"] == "expense_data_unavailable"


def test_contract_workbook_without_date_to_requires_watermark_through_today(
    db,
    monkeypatch,
):
    _load_complete_contract(db)
    db.add(MaintenanceContractWorkbookState(
        contract_no="XS-MARGIN",
        revision=1,
        expense_complete_through=date(2026, 3, 31),
        expense_snapshot_complete=True,
    ))
    db.commit()
    monkeypatch.setattr(
        maintenance_cost,
        "business_today",
        lambda: date(2026, 4, 1),
    )

    data = maintenance_cost.contract_workbook_data(db, "XS-MARGIN")

    assert data["expense_data_available"] is False
    assert data["expense_evidence_status"] == "expense_data_unavailable"
    assert data["margin"]["contribution_status_inc"] == "expense_data_unavailable"
    assert data["margin"]["contribution_status_ex"] == "expense_data_unavailable"


def test_date_filtered_budget_decision_preserves_evidence_gates_then_filters_complete(
    db,
):
    _load_complete_contract(db)

    def decision_statuses() -> tuple[str, str]:
        board_row = maintenance_cost.board(
            db,
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
            lifecycle="all",
        )["rows"][0]
        workbook_data = maintenance_cost.contract_workbook_data(
            db,
            "XS-MARGIN",
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
        )
        return (
            board_row["decision_status"],
            workbook_data["decision"]["decision_status"],
        )

    # 费用快照未确认仍是第一优先级，日期过滤不得掩盖证据缺口。
    assert decision_statuses() == (
        "expense_data_unavailable",
        "expense_data_unavailable",
    )

    db.add(MaintenanceContractWorkbookState(
        contract_no="XS-MARGIN",
        revision=1,
        expense_complete_through=date(2026, 3, 31),
        expense_snapshot_complete=True,
    ))
    db.commit()
    # 成本和费用证据完整时，期间支出不能与整合同预算比较。
    assert decision_statuses() == ("filtered_scope", "filtered_scope")

    line = db.scalars(select(FMaintenanceLine)).one()
    line.cost_amount = None
    db.commit()
    # 缺成本比日期范围限制更具体，必须继续失败关闭并暴露真实缺口。
    assert decision_statuses() == ("incomplete_cost", "incomplete_cost")


def test_profit_blind_service_result_masks_margin_values_and_statuses(db):
    _load_complete_contract(db)
    ctx = security.UserContext(
        user_id="purchaser",
        role="purchaser",
        permissions=permissions.effective("purchaser", None),
        is_authenticated=True,
    )

    row = maintenance_cost.board(
        db,
        lifecycle="all",
        user_ctx=ctx,
    )["rows"][0]

    assert row["parts_cost_inc_tax"] == 226.0
    assert row["parts_cost_ex_tax"] == 200.0
    for field in (
        "revenue_inc", "revenue_ex",
        "parts_gross_profit_inc", "parts_gross_profit_ex",
        "parts_gross_margin_inc", "parts_gross_margin_ex",
        "parts_profit_status_inc", "parts_profit_status_ex",
        "contribution_profit_inc", "contribution_profit_ex",
        "contribution_margin_inc", "contribution_margin_ex",
        "contribution_status_inc", "contribution_status_ex",
    ):
        assert row[field] is None


def test_contract_workbook_contains_both_margin_bases_and_explicit_status(db):
    _load_complete_contract(db)

    data = maintenance_cost.contract_workbook_data(db, "XS-MARGIN")
    assert data["margin"]["parts_gross_profit_inc"] == Decimal("904.00")
    assert data["margin"]["parts_gross_profit_ex"] == Decimal("800.00")
    assert data["margin"]["parts_profit_status_inc"] == "complete_actual"
    assert data["margin"]["parts_profit_status_ex"] == "complete_actual"
    assert data["margin"]["contribution_profit_inc"] is None
    assert data["margin"]["contribution_profit_ex"] is None
    assert (
        data["margin"]["contribution_status_inc"]
        == "expense_data_unavailable"
    )
    assert (
        data["margin"]["contribution_status_ex"]
        == "expense_data_unavailable"
    )

    workbook = maintenance_workbook_renderer.render_contract_workbook(
        "XS-MARGIN",
        data,
        lambda value: value,
    )
    try:
        values = {
            cell.value
            for row in workbook["项目预算"].iter_rows()
            for cell in row
            if cell.value is not None
        }
        assert "合同收入（含税）" in values
        assert "合同收入（未税）" in values
        assert "合同级备件毛利（含税）" in values
        assert "合同级备件毛利（未税）" in values
        assert "合同级贡献毛利（含税）" in values
        assert "合同级贡献毛利（未税）" in values
        assert "合同级贡献毛利率（含税）" in values
        assert "合同级贡献毛利率（未税）" in values
        assert "未就绪（无记录不等于0）" in values
        assert "含税备件毛利状态" in values
        assert "未税备件毛利状态" in values
        assert "含税贡献毛利状态" in values
        assert "未税贡献毛利状态" in values
        assert "完整：仅实际成本" in values
        assert "费用数据未就绪" in values
        assert "当月已知合计（费用非全量）" in values
        assert "当前已知合计（费用非全量）" in values
    finally:
        workbook.close()


def test_contract_workbook_hides_dirty_margin_values_when_status_blocks(db):
    _load_complete_contract(db)
    data = maintenance_cost.contract_workbook_data(db, "XS-MARGIN")
    data["margin"].update({
        "revenue_inc": Decimal("9999.00"),
        "parts_gross_profit_inc": Decimal("9998.00"),
        "parts_gross_margin_inc": Decimal("0.9999"),
        "parts_profit_status_inc": "ambiguous_revenue",
        "contribution_profit_inc": Decimal("9997.00"),
        "contribution_margin_inc": Decimal("0.9998"),
        "contribution_status_inc": "complete",
    })

    workbook = maintenance_workbook_renderer.render_contract_workbook(
        "XS-MARGIN",
        data,
        lambda value: value,
    )
    try:
        sheet = workbook["项目预算"]

        def value_after(label: str):
            label_cell = next(
                cell
                for row in sheet.iter_rows()
                for cell in row
                if cell.value == label
            )
            return sheet.cell(label_cell.row, label_cell.column + 1).value

        assert value_after("合同收入（含税）") == "—"
        assert value_after("合同级备件毛利（含税）") == "—"
        assert value_after("合同级备件毛利率（含税）") == "—"
        assert value_after("合同级贡献毛利（含税）") == "—"
        assert value_after("合同级贡献毛利率（含税）") == "—"
    finally:
        workbook.close()


def test_contract_workbook_uses_latest_effective_sales_version(
    db,
):
    batch = _load_complete_contract(db)
    db.add(FSalesOrder(
        raw_order_id="S2-DUPLICATE-TAX",
        order_no="XS-MARGIN",
        order_date=date(2026, 3, 2),
        amount_ex_tax=Decimal("1000"),
        tax_rate=Decimal("0.13"),
        data_status="已生效",
        import_batch_id=batch.id,
    ))
    db.commit()

    data = maintenance_cost.contract_workbook_data(db, "XS-MARGIN")

    assert data["contract_tax_status"] == "available"
    assert data["contract_tax_rate"] == Decimal("0.13")
    assert "sales_order" not in data
    assert data["margin"]["parts_profit_status_inc"] == "complete_actual"
    assert data["margin"]["parts_profit_status_ex"] == "complete_actual"

    workbook = maintenance_workbook_renderer.render_contract_workbook(
        "XS-MARGIN",
        data,
        lambda value: value,
    )
    try:
        sheet = workbook["项目预算"]
        tax_label = next(
            cell
            for row in sheet.iter_rows()
            for cell in row
            if cell.value == "税率"
        )
        tax_cell = sheet.cell(tax_label.row, tax_label.column + 1)
        assert tax_cell.value == 0.13
        assert tax_cell.number_format == "0.00%"
    finally:
        workbook.close()


def test_contract_workbook_uses_latest_amount_without_duplicate_ambiguity(db):
    batch = _load_complete_contract(db)
    db.add(FSalesOrder(
        raw_order_id="S2-DUPLICATE-AMOUNT",
        order_no="XS-MARGIN",
        order_date=date(2026, 3, 2),
        amount_ex_tax=Decimal("1200"),
        tax_rate=Decimal("0.06"),
        data_status="已生效",
        import_batch_id=batch.id,
    ))
    db.commit()

    data = maintenance_cost.contract_workbook_data(db, "XS-MARGIN")

    assert data["contract_tax_status"] == "available"
    assert data["contract_tax_rate"] == Decimal("0.13")
    assert data["margin"]["revenue_ex"] == Decimal("1200")
    assert data["margin"]["revenue_inc"] == Decimal("1356.00")
    assert data["margin"]["parts_gross_profit_inc"] == Decimal("1130.00")
    assert data["margin"]["parts_gross_profit_ex"] == Decimal("1000.00")
    assert data["margin"]["parts_profit_status_inc"] == "complete_actual"
    assert data["margin"]["parts_profit_status_ex"] == "complete_actual"

    workbook = maintenance_workbook_renderer.render_contract_workbook(
        "XS-MARGIN",
        data,
        lambda value: value,
    )
    try:
        sheet = workbook["项目预算"]
        tax_label = next(
            cell
            for row in sheet.iter_rows()
            for cell in row
            if cell.value == "税率"
        )
        tax_cell = sheet.cell(tax_label.row, tax_label.column + 1)
        assert tax_cell.value == 0.13
        assert tax_cell.number_format == "0.00%"
    finally:
        workbook.close()


def test_empty_contract_workbook_does_not_fabricate_zero_cost_margin(db):
    """合同有收入但没有维保明细时，成本不完整且毛利必须为空。"""
    batch = SysImportBatch(
        filename="margin-empty-contract.xlsx",
        file_type="sales",
        file_hash="margin-empty-contract",
        status="success",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.sales_result(
            {
                "S1": f.sales_head(
                    "S1",
                    order_no="XS-EMPTY-MARGIN",
                    amount_ex_tax=Decimal("1000"),
                    tax_rate=Decimal("0.13"),
                ),
            },
            [],
        ),
        batch.id,
        date(2026, 7, 28),
    )
    db.commit()

    data = maintenance_cost.contract_workbook_data(db, "XS-EMPTY-MARGIN")

    assert data["cost_summary"]["cost_quality"] == "incomplete"
    assert data["decision"]["decision_status"] == "incomplete_cost"
    assert data["decision"]["remaining"] is None
    assert data["decision"]["remaining_pct"] is None
    assert data["dual_cost_summary"] == {
        "parts_cost_inc_tax": None,
        "parts_cost_inc_tax_complete": False,
        "parts_cost_inc_tax_quality": "incomplete",
        "parts_cost_inc_tax_missing_lines": 0,
        "parts_cost_ex_tax": None,
        "parts_cost_ex_tax_complete": False,
        "parts_cost_ex_tax_quality": "incomplete",
        "parts_cost_ex_tax_missing_lines": 0,
    }
    assert data["margin"]["parts_gross_profit_inc"] is None
    assert data["margin"]["parts_gross_profit_ex"] is None
    assert data["margin"]["contribution_profit_inc"] is None
    assert data["margin"]["contribution_profit_ex"] is None
    assert data["margin"]["parts_profit_status_inc"] == "incomplete_cost"
    assert data["margin"]["parts_profit_status_ex"] == "incomplete_cost"
    assert data["margin"]["contribution_status_inc"] == "incomplete_cost"
    assert data["margin"]["contribution_status_ex"] == "incomplete_cost"
