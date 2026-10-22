"""D-34 Q1=A（金臣 2026-10-20 裁定）：改回本轮首次快照自动解除保护。

比较基准 = 本轮第一次人工修改前的实际生效值（首次快照），不是最新来源值：
来源 2→人工 5→期间来源 3 时，改回 2 解除、改成 3 仍保护。解除后来源上传
恢复生效；每轮解除后重新取快照；重复提交原值不算变更。
"""

from decimal import Decimal
from io import BytesIO
from uuid import uuid4

from openpyxl import load_workbook
from sqlalchemy import select

from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
)
from app.services import maintenance_project_master_workbook as master
from app.services import maintenance_project_operations as operations
from tests.test_maintenance_field_protection_api import (
    _admin_client,
    _make_project,
    _patch,
    _read_line,
    _seed_line,
    _upload,
)
from tests.test_maintenance_field_protection_workbook import edit_workbook, reimport
from tests.test_maintenance_project_master_v2_editable import _make_project_with_line


def _page_audits(db, raw_line_id):
    return list(db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_id == raw_line_id,
        MaintenanceProjectOperationAudit.action == "page_update",
    ).order_by(MaintenanceProjectOperationAudit.id)))


def _line_audits(db, line):
    return list(db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_type == "maintenance_line",
        MaintenanceProjectOperationAudit.entity_id == str(line.id),
        MaintenanceProjectOperationAudit.action == "UPDATE",
    ).order_by(MaintenanceProjectOperationAudit.id)))


def test_midcycle_source_upload_then_return_to_first_snapshot_releases_and_source_applies_page(db):
    """验收1（网页）：2→5→来源3→改2 解除→来源9 生效。"""
    order, line = _seed_line(db)
    _make_project(db, tag="FIRST-SNAP-PAGE", source_order_id=order.raw_order_id)
    client = _admin_client(db)

    _patch(client, order, line.raw_line_id, {"qty": 5})
    _upload(db, order, line, qty="3")
    protected = _read_line(client, order)
    assert protected["qty"] == "5.000"
    assert protected["protected_fields"] == ["qty"]
    # 首次快照仍是 2；最新来源 3 只作观察证据。
    entry = protected["manual_override"]["qty"]
    assert Decimal(entry["original_source_value"]) == Decimal(2)
    assert Decimal(entry["source_value"]) == Decimal(3)

    # 改成最新来源 3：不是首次快照，仍保护（Q1=A 关键反转）。
    third = _patch(client, order, line.raw_line_id, {"qty": 3})
    assert third["qty"] == "3.000"
    assert third["protected_fields"] == ["qty"]

    # 改回 2：解除，审计写明比较基准为本轮首次快照。
    released = _patch(client, order, line.raw_line_id, {"qty": 2})
    assert released["protected_fields"] == []
    assert released["manual_override"] == {}
    audits = _page_audits(db, line.raw_line_id)
    assert "本轮首次快照" in audits[-1].reason
    assert "保护解除" in audits[-1].reason

    _upload(db, order, line, qty="9")
    assert _read_line(client, order)["qty"] == "9.000"


def test_midcycle_source_upload_then_return_to_first_snapshot_releases_and_source_applies_workbook(db):
    """验收1（工作簿）：同一场景走总表 03 编辑入口。"""
    project, part, order, line = _make_project_with_line(db)
    edit_workbook(db, project, {"需求数量": 5})
    reimport(db, order, line, pn=part.pn_std, qty="3")
    db.refresh(line)
    assert line.qty == Decimal("5.000")
    entry = line.manual_override["qty"]
    assert Decimal(entry["original_source_value"]) == Decimal(2)
    assert Decimal(entry["source_value"]) == Decimal(3)

    # 旧口径（最新来源）下改 3 才解除；Q1=A 下改 3 仍保护。
    edit_workbook(db, project, {"需求数量": 3})
    db.refresh(line)
    assert set(line.manual_override) == {"qty"}

    edit_workbook(db, project, {"需求数量": 2})
    db.refresh(line)
    assert line.manual_override == {}
    audits = _line_audits(db, line)
    assert "本轮首次快照" in audits[-1].reason

    reimport(db, order, line, pn=part.pn_std, qty="9")
    db.refresh(line)
    assert line.qty == Decimal("9.000")


def test_repeated_release_cycles_resnapshot_each_round_with_audit_per_action(db):
    """验收2：2→5→2→7→2 每轮解除后重新取快照，审计条数与动作一一对应。"""
    order, line = _seed_line(db)
    _make_project(db, tag="CYCLE-RESNAP", source_order_id=order.raw_order_id)
    client = _admin_client(db)

    _patch(client, order, line.raw_line_id, {"qty": 5})
    first = _patch(client, order, line.raw_line_id, {"qty": 2})
    assert first["protected_fields"] == []
    assert first["manual_override"] == {}

    # 第二轮：无来源变化，从当前实际生效值 2 重新建快照；7 是第三值仍保护。
    second = _patch(client, order, line.raw_line_id, {"qty": 7})
    assert second["protected_fields"] == ["qty"]
    assert Decimal(second["manual_override"]["qty"]["source_value"]) == Decimal(2)
    released = _patch(client, order, line.raw_line_id, {"qty": 2})
    assert released["protected_fields"] == []
    assert released["manual_override"] == {}

    audits = _page_audits(db, line.raw_line_id)
    assert len(audits) == 4
    assert "保护解除" not in audits[0].reason  # 建账：2→5
    assert "保护解除" in audits[1].reason       # 解除：5→2
    assert "保护解除" not in audits[2].reason  # 建账：2→7
    assert "保护解除" in audits[3].reason       # 解除：7→2

    # 解除后来源上传立即生效；随后再入保护时快照取当时的实际生效值。
    _upload(db, order, line, qty="9")
    assert _read_line(client, order)["qty"] == "9.000"
    third = _patch(client, order, line.raw_line_id, {"qty": 7})
    assert Decimal(third["manual_override"]["qty"]["source_value"]) == Decimal(9)
    back = _patch(client, order, line.raw_line_id, {"qty": 9})
    assert back["protected_fields"] == []
    assert len(_page_audits(db, line.raw_line_id)) == 6


def test_midcycle_source_upload_keeps_latest_value_protected_until_first_snapshot_returns(db):
    """验收2（中途来源变体）：2→5→来源3→改3 仍保护→改2 解除→来源9 生效。"""
    order, line = _seed_line(db)
    _make_project(db, tag="MID-SOURCE", source_order_id=order.raw_order_id)
    client = _admin_client(db)

    _patch(client, order, line.raw_line_id, {"qty": 5})
    _upload(db, order, line, qty="3")

    to_latest = _patch(client, order, line.raw_line_id, {"qty": 3})
    assert to_latest["protected_fields"] == ["qty"]
    assert to_latest["qty"] == "3.000"

    to_first = _patch(client, order, line.raw_line_id, {"qty": 2})
    assert to_first["protected_fields"] == []
    assert to_first["manual_override"] == {}

    _upload(db, order, line, qty="9")
    assert _read_line(client, order)["qty"] == "9.000"


def test_third_value_then_return_to_first_snapshot_releases(db):
    """第三值不成为新基准：2→5→7 仍保护，再改 2 解除。"""
    order, line = _seed_line(db)
    _make_project(db, tag="THIRD-VALUE", source_order_id=order.raw_order_id)
    client = _admin_client(db)

    _patch(client, order, line.raw_line_id, {"qty": 5})
    third = _patch(client, order, line.raw_line_id, {"qty": 7})
    assert third["protected_fields"] == ["qty"]
    assert Decimal(third["manual_override"]["qty"]["source_value"]) == Decimal(2)

    released = _patch(client, order, line.raw_line_id, {"qty": "2.000"})
    assert released["protected_fields"] == []
    assert released["manual_override"] == {}


def test_repeated_same_value_after_release_is_noop_page(db):
    """验收3（网页）：解除后重复提交原值——不写审计、不升版、digest 不变。"""
    order, line = _seed_line(db)
    project = _make_project(db, tag="NOOP-PAGE", source_order_id=order.raw_order_id)
    client = _admin_client(db)

    _patch(client, order, line.raw_line_id, {"qty": 5})
    released = _patch(client, order, line.raw_line_id, {"qty": 2})
    state = operations.get_or_create_workbook_state(db, project_id=project.project_id)
    revision = state.revision
    digest = released["digest"]
    audits_before = len(_page_audits(db, line.raw_line_id))

    repeated = _patch(client, order, line.raw_line_id, {"qty": "2.000"})
    assert repeated["changed"] is False
    assert repeated["protected_fields"] == []
    assert repeated["manual_override"] == {}
    assert repeated["digest"] == digest
    assert len(_page_audits(db, line.raw_line_id)) == audits_before
    db.refresh(state)
    assert state.revision == revision


def test_repeated_same_workbook_after_release_is_noop(db):
    """验收3（工作簿）：解除后原样回传——无新审计、不升版、账本仍空。"""
    project, _part, _order, line = _make_project_with_line(db)
    edit_workbook(db, project, {"需求数量": 5})
    edit_workbook(db, project, {"需求数量": 2})
    db.refresh(line)
    assert line.manual_override == {}
    state = operations.get_or_create_workbook_state(db, project_id=project.project_id)
    revision = state.revision
    audits_before = len(_line_audits(db, line))

    # 重新导出当前总表（与服务端一致）并原样回传：未触碰、无操作。
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_PARTS,))
    wb = load_workbook(BytesIO(content))
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id,
        data=_workbook_bytes(wb),
    )
    result = master.apply_project_master_v2(
        db, plan, operated_by="noop-workbook", import_batch_id=str(uuid4()))
    db.commit()

    db.refresh(line)
    db.refresh(state)
    assert line.manual_override == {}
    assert state.revision == revision
    assert len(_line_audits(db, line)) == audits_before
    assert result["line_updates"] == 0


def _workbook_bytes(wb) -> bytes:
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
