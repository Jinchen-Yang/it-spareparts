"""D-0051: 03跟单地址参考，新旧签名模板及不回写契约。"""
import pytest

from app.services import maintenance_project_master_workbook as master
from tests.test_maintenance_project_master_v2_editable import (
    _make_project_with_line, _parts_sheet, _reupload, _save,
)

ADDRESS = "收货地址（只读）"


def test_address_is_plain_text_and_source_change_is_never_reverted(db):
    project, _, order, line = _make_project_with_line(db)
    order.receiver_address = '=HYPERLINK("https://invalid.example","地址")'
    db.commit()
    _, workbook, sheet = _parts_sheet(db, project.project_id)
    columns = {cell.value: cell.column for cell in sheet[1]}
    cell = sheet.cell(2, columns[ADDRESS])
    assert cell.value == order.receiver_address and cell.data_type == "s"
    assert not sheet.column_dimensions[cell.column_letter].hidden
    assert sheet.column_dimensions[sheet.cell(1, columns[master.V2_BASE_COLUMN]).column_letter].hidden
    old_qty = line.qty
    order.receiver_address = "需求单后续更正的地址"
    db.commit()
    _reupload(db, project.project_id, workbook)
    db.refresh(order); db.refresh(line)
    assert order.receiver_address == "需求单后续更正的地址"
    assert line.qty == old_qty


@pytest.mark.parametrize("mutation", ["change", "drop"])
def test_signed_address_cannot_be_changed_or_removed(db, mutation):
    project, _, order, line = _make_project_with_line(db)
    order.receiver_address = "需求单地址甲"
    db.commit()
    _, workbook, sheet = _parts_sheet(db, project.project_id)
    columns = {cell.value: cell.column for cell in sheet[1]}
    if mutation == "change":
        sheet.cell(2, columns[ADDRESS], "改成地址乙")
    else:
        sheet.delete_cols(columns[ADDRESS])
    with pytest.raises(master.WorkbookError) as error:
        master.validate_project_master_v2(db, project_id=project.project_id, data=_save(workbook))
    assert error.value.code == "readonly_cell_modified"
    assert order.receiver_address == "需求单地址甲"


def test_old_signed_template_without_address_still_edits_existing_fields(db):
    project, _, order, line = _make_project_with_line(db)
    order.receiver_address = "正确跟单地址"
    db.commit()
    _, workbook, sheet = _parts_sheet(db, project.project_id)
    columns = {cell.value: cell.column for cell in sheet[1]}
    baseline = master._parse_v2_row_base_token(
        sheet.cell(2, columns[master.V2_BASE_COLUMN]).value,
        sheet=master.V2_SHEET_PARTS, entity_id=line.id, row_no=2)
    baseline.pop("收货地址参考哈希")
    sheet.cell(2, columns[master.V2_BASE_COLUMN], master._v2_row_base_token(
        sheet=master.V2_SHEET_PARTS, entity_id=line.id, values=baseline))
    sheet.delete_cols(columns[ADDRESS])
    sheet.cell(2, columns["备注"], "旧模板仍可更正备注")
    _reupload(db, project.project_id, workbook)
    db.refresh(line); db.refresh(order)
    assert line.line_note == "旧模板仍可更正备注"
    assert order.receiver_address == "正确跟单地址"


def test_oversize_address_export_fails_instead_of_truncating_signed_reference(db):
    project, _, order, _ = _make_project_with_line(db)
    order.receiver_address = "址" * 32768
    db.commit()
    with pytest.raises(master.WorkbookError) as error:
        _parts_sheet(db, project.project_id)
    assert error.value.code == "text_too_long"
