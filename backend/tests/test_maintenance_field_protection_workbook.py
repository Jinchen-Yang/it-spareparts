"""D-34: real workbook → source upload contracts, isolated PostgreSQL."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.maintenance import MaintenanceManualCostOverride
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
from app.services import maintenance_demand_manual as manual
from app.services import maintenance_project_master_workbook as master
from app.services import maintenance_project_operations as operations
from tests import factories as f
from tests.test_maintenance_project_master_v2_editable import (
    _batch,
    _make_project_with_line,
    _parts_sheet,
    _reupload,
)


def reimport(db, order, line, *, pn, qty="7", note="原件备注"):
    row = f.maintenance_line(order.raw_order_id, line.raw_line_id, pn,
                             qty=qty, return_qty="0")
    row["line_note"] = note
    head = f.maintenance_head(order.raw_order_id, order_no=order.order_no,
                              sales_order=order.linked_sales_order_no)
    result = loader.load(db, f.maintenance_result({order.raw_order_id: head}, [row]),
                         _batch(db), date(2026, 10, 20), mode="upsert")
    db.commit()
    db.refresh(line)
    return result


def edit_workbook(db, project, values):
    _, wb, ws = _parts_sheet(db, project.project_id)
    headers = {cell.value: cell.column for cell in ws[1]}
    for label, value in values.items():
        ws.cell(2, headers[label], value)
    return _reupload(db, project.project_id, wb)


def test_workbook_return_to_source_releases_quantity_with_audit(db):
    project, part, order, line = _make_project_with_line(db)
    edit_workbook(db, project, {"需求数量": 5})
    edit_workbook(db, project, {"需求数量": "2.000"})
    db.refresh(line)
    assert line.manual_override == {}
    audits = list(db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_type == "maintenance_line",
        MaintenanceProjectOperationAudit.entity_id == str(line.id),
        MaintenanceProjectOperationAudit.action == "UPDATE",
    ).order_by(MaintenanceProjectOperationAudit.id)))
    assert len(audits) == 2
    assert "改回来源值，保护解除" in audits[-1].reason
    assert audits[-1].before_json["manual_override"]["qty"]["value"] == "5.000"
    assert audits[-1].after_json["manual_override"] == {}
    reimport(db, order, line, pn=part.pn_std, qty="9")
    assert line.qty == Decimal("9.000")


@pytest.mark.parametrize("final_qty", [5, 3])
def test_workbook_different_manual_quantity_stays_protected(db, final_qty):
    project, part, order, line = _make_project_with_line(db)
    edit_workbook(db, project, {"需求数量": 5})
    edit_workbook(db, project, {"需求数量": final_qty})
    reimport(db, order, line, pn=part.pn_std, qty="9")
    assert line.qty == Decimal(final_qty)
    assert set(line.manual_override) == {"qty"}


@pytest.mark.parametrize("source_note", [None, "来源备注"])
def test_workbook_explicit_blank_releases_only_when_source_is_empty(db, source_note):
    project, part, order, line = _make_project_with_line(db)
    reimport(db, order, line, pn=part.pn_std, qty="2", note=source_note)
    edit_workbook(db, project, {"备注": "人工备注"})
    edit_workbook(db, project, {"备注": " "})
    db.refresh(line)
    assert line.line_note is None
    assert set(line.manual_override) == ({"line_note"} if source_note else set())
    reimport(db, order, line, pn=part.pn_std, note="之后来源备注")
    assert line.line_note == (None if source_note else "之后来源备注")


def test_workbook_note_keeps_source_quantity_live(db):
    project, part, order, line = _make_project_with_line(db)
    edit_workbook(db, project, {"备注": "人工备注"})
    reimport(db, order, line, pn=part.pn_std)
    assert line.qty == Decimal("7.000")
    assert line.line_note == "人工备注"
    assert set(line.manual_override) == {"line_note"}
    assert line.edited_source == "wbdd"


def test_note_edit_does_not_round_unedited_source_quantity(db):
    project, _, _, line = _make_project_with_line(
        db, qty=Decimal("2.001"), return_qty=Decimal("0.001"))
    edit_workbook(db, project, {"备注": "只改备注"})
    db.refresh(line)
    assert line.qty == Decimal("2.001")
    assert line.return_qty == Decimal("0.001")
    assert set(line.manual_override) == {"line_note"}


def test_v1_note_also_records_field_protection(db):
    project, part, order, line = _make_project_with_line(db)
    # V1's public apply supports note even though its current Excel template
    # only exposes price/reason. Freeze this adapter boundary as well.
    plan = master.MasterPlan(project_id=project.project_id, cost_refills=(
        master.CostRefill(line_id=line.id, note="V1人工备注",
                          unit_cost_ex_tax=None, unit_cost_inc_tax=None, reason=None),))
    master.apply(db, plan, operated_by="tester", import_batch_id="v1-note")
    reimport(db, order, line, pn=part.pn_std)
    assert line.line_note == "V1人工备注"
    assert line.qty == Decimal("7.000")
    assert set(line.manual_override) == {"line_note"}


def test_normalized_roundtrip_does_not_protect_or_retire_manual_price(db):
    project, part, order, line = _make_project_with_line(db, unit_cost=None, cost_source=None)
    edit_workbook(db, project, {"人工未税单位成本": 100, "人工成本原因": "核对价格"})
    override = db.scalar(select(MaintenanceManualCostOverride).where(
        MaintenanceManualCostOverride.line_id == line.id))
    state = operations.get_or_create_workbook_state(db, project_id=project.project_id)
    original_revision, price_version = state.revision, override.version
    edit_workbook(db, project, {"PN": part.pn_std.lower(), "需求数量": "2.0004", "备注": " "})
    db.refresh(line)
    db.refresh(override)
    db.refresh(state)
    assert line.manual_override == {}
    assert line.qty == Decimal("2.000")
    assert line.cost_amount_ex_tax == Decimal("200.00")
    assert override.active and override.version == price_version
    assert state.revision == original_revision
    reimport(db, order, line, pn=part.pn_std, qty="8")
    assert line.qty == Decimal("8.000")


def test_price_only_keeps_quantity_live_and_price_evidence(db):
    project, part, order, line = _make_project_with_line(db, unit_cost=None, cost_source=None)
    edit_workbook(db, project, {"人工未税单位成本": 123, "人工成本原因": "核对发票"})
    reimport(db, order, line, pn=part.pn_std)
    override = db.scalar(select(MaintenanceManualCostOverride).where(
        MaintenanceManualCostOverride.line_id == line.id))
    assert line.qty == Decimal("7.000")
    assert line.manual_override == {} and line.edited_source == "wbdd"
    assert override.active and override.unit_cost_ex_tax == Decimal("123.00")
    assert override.reason == "核对发票"


def test_workbook_pn_group_preserved_while_quantity_updates(db):
    project, part, order, line = _make_project_with_line(db)
    replacement = DimPart(pn_std="D34-REPLACEMENT")
    db.add(replacement)
    db.commit()
    edit_workbook(db, project, {"PN": replacement.pn_std})
    reimport(db, order, line, pn=part.pn_std)
    assert (line.pn_raw, line.pn_std, line.part_id) == (
        replacement.pn_std, replacement.pn_std, replacement.id)
    assert line.qty == Decimal("7.000")
    assert set(line.manual_override) == {"pn_raw", "pn_std", "part_id"}


@pytest.mark.parametrize("header,field,value", [
    ("备注", "line_note", "  人工备注  "),
    ("描述", "description", "人工描述"),
    ("SN", "serial_numbers", "SN-NEW"),
    ("需求数量", "qty", "4.000"),
])
def test_page_and_workbook_record_same_actual_field_set(db, header, field, value):
    project, _, _, line = _make_project_with_line(db)
    _, _, _, other = _make_project_with_line(db)
    page_result = manual.patch_demand_line(
        db, raw_line_id=other.raw_line_id, updates={field: value},
        reason="相同编辑意图", operated_by="tester")
    db.commit()
    edit_workbook(db, project, {header: value})
    db.refresh(line)
    assert set(page_result["manual_override"]) == set(line.manual_override) == {field}
