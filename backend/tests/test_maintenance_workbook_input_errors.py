"""工作簿公开计划/校验入口对无效输入返回可读错误。"""

from decimal import Decimal

import pytest

from app.services import maintenance_project_master_workbook as master
from tests.test_maintenance_project_master_v2_editable import (
    _make_project_with_line,
    _parts_sheet,
    _save,
)


@pytest.mark.parametrize("line_project_ids", [{}, None], ids=["missing-row", "missing-context"])
def test_v1_note_without_project_context_returns_workbook_error(db, line_project_ids):
    _, _, _, line = _make_project_with_line(db)
    plan = master.MasterPlan(
        project_id=None,
        cost_refills=(master.CostRefill(
            line_id=line.id, note="需要归属才能审计的备注",
            unit_cost_ex_tax=None, unit_cost_inc_tax=None, reason=None,
        ),),
    )

    with pytest.raises(master.WorkbookError) as raised:
        master.apply(
            db, plan, operated_by="input-error-test",
            import_batch_id="missing-project-context",
            _line_project_ids=line_project_ids,
        )

    assert raised.value.code == "project_scope_denied"
    assert "请重新下载" in raised.value.message
    assert line.line_note is None
    assert line.manual_override == {}


@pytest.mark.parametrize("header,value", [
    pytest.param("需求数量", "1e25", id="quantity-too-large-to-quantize"),
    pytest.param("人工未税单位成本", "1e26", id="cost-too-large-to-quantize"),
    pytest.param("需求数量", "NaN", id="quantity-nan"),
    pytest.param("需求数量", "Infinity", id="quantity-infinity"),
    pytest.param("需求数量", "sNaN", id="quantity-signaling-nan"),
    pytest.param("人工未税单位成本", "NaN", id="cost-nan"),
    pytest.param("人工未税单位成本", "Infinity", id="cost-infinity"),
    pytest.param("人工未税单位成本", "sNaN", id="cost-signaling-nan"),
    pytest.param("需求数量", "999999999999.9999", id="quantity-rounds-out-of-range"),
    pytest.param("人工未税单位成本", "999999999999.999", id="cost-rounds-out-of-range"),
])
def test_v2_invalid_decimal_returns_workbook_error(db, header, value):
    project, _, _, line = _make_project_with_line(db)
    _, workbook, sheet = _parts_sheet(db, project.project_id)
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers[header], value)

    with pytest.raises(master.WorkbookError) as raised:
        master.validate_project_master_v2(
            db, project_id=project.project_id, data=_save(workbook),
        )

    assert raised.value.code == "invalid_amount"
    assert "第 2 行" in raised.value.message
    assert header in raised.value.message
    assert line.qty == Decimal("2.000")
    assert line.manual_override == {}


@pytest.mark.parametrize("header,value,field,expected", [
    pytest.param("需求数量", "0.001", "qty", "0.001", id="minimum-positive-quantity"),
    pytest.param("需求数量", "3.0006", "qty", "3.001", id="quantity-keeps-three-places"),
    pytest.param(
        "人工未税单位成本", "0", "unit_cost_ex_tax", "0.00", id="zero-cost-is-valid",
    ),
    pytest.param(
        "人工未税单位成本", "12.345", "unit_cost_ex_tax", "12.35",
        id="cost-rounds-half-up",
    ),
])
def test_v2_valid_decimal_keeps_existing_precision(db, header, value, field, expected):
    project, _, _, _ = _make_project_with_line(db)
    _, workbook, sheet = _parts_sheet(db, project.project_id)
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers[header], value)

    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=_save(workbook),
    )

    assert len(plan.cost_refills) == 1
    assert getattr(plan.cost_refills[0], field) == Decimal(expected)
