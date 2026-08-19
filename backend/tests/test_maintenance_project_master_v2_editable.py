"""V2.1 项目总表 03 备件明细全字段可编辑：改/作废/新增/数量重算/审计/读侧过滤。

对应 REQUIREMENTS #55。删除=软作废（is_active=false），不计入计算、不再导出、
06 关联行级联作废；氚云 loader 白名单不含作废列（重传不复活）。
"""
import io
import uuid
from datetime import date
from decimal import Decimal

from openpyxl import load_workbook
from sqlalchemy import func, select

from app.models.dimensions import DimPart
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceManualCostOverride,
)
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_project_master_workbook as master


def _make_project_with_line(db, *, qty=Decimal("2"), unit_cost=Decimal("100.00"),
                            cost_source="direct", return_qty=Decimal("0"),
                            source_line_id=None):
    project = MaintenanceProject(
        project_id=str(uuid.uuid4()), project_code="EDIT",
        display_name="可编辑测试", lifecycle_status="ongoing",
    )
    part = DimPart(pn_std="EDIT-PN-001", description="可编辑备件")
    db.add_all([project, part])
    db.flush()
    db.add(MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()), project_id=project.project_id,
        contract_id="EDIT-CONTRACT", contract_no="XSDD-EDIT-001",
        amount_inc_tax=Decimal("10000.00"), included_in_total=True,
        status_mapping_state="mapped", status_mapping_version="v1",
        effective_from=date(2026, 1, 1), source="ledger", version=1,
    ))
    order = FMaintenanceOrder(
        raw_order_id=f"raw-order-{uuid.uuid4()}", order_no="WBDD-EDIT-001",
        order_date=date(2026, 8, 1), linked_sales_order_no="XSDD-EDIT-001",
        project_raw="可编辑测试", data_status="已生效",
        import_batch_id=_batch(db),
    )
    db.add(order)
    db.flush()
    line = FMaintenanceLine(
        raw_line_id=source_line_id or f"raw-line-{uuid.uuid4()}",
        order_id=order.id, line_no=1, part_id=part.id,
        pn_std=part.pn_std, pn_raw=part.pn_std, description=part.description,
        qty=qty, return_qty=return_qty,
        unit_cost_ex_tax=unit_cost if unit_cost is not None else None,
        unit_cost_inc_tax=(unit_cost * Decimal("1.13")).quantize(Decimal("0.01"))
        if unit_cost is not None else None,
        cost_amount_ex_tax=(unit_cost * max(qty - return_qty, Decimal(0))).quantize(Decimal("0.01"))
        if unit_cost is not None else None,
        cost_amount_inc_tax=(unit_cost * Decimal("1.13") * max(qty - return_qty, Decimal(0))).quantize(Decimal("0.01"))
        if unit_cost is not None else None,
        cost_source=cost_source, cost_tax_basis="ex", confidence="high",
        import_batch_id=_batch(db),
    )
    db.add(line)
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid.uuid4()), project_id=project.project_id,
        source_order_id=order.raw_order_id, is_active=True,
        created_by="test",
    ))
    db.commit()
    return project, part, order, line


def _batch(db) -> int:
    from app.models.system import SysImportBatch
    b = SysImportBatch(
        filename="test.xlsx", file_type="maintenance",
        file_hash=uuid.uuid4().hex, status="success",
    )
    db.add(b)
    db.flush()
    return b.id


def _parts_sheet(db, project_id: str):
    content = master.build_project_master_v2(
        db, project_id=project_id, sheets=(master.V2_SHEET_PARTS,))
    wb = load_workbook(io.BytesIO(content))
    return content, wb, wb[master.V2_SHEET_PARTS]


def _reupload(db, project_id, wb):
    buf = io.BytesIO()
    wb.save(buf)
    plan = master.validate_project_master_v2(
        db, project_id=project_id, data=buf.getvalue())
    return master.apply_project_master_v2(
        db, plan, operated_by="tester", import_batch_id=str(uuid.uuid4()))


def test_v21_parts_sheet_has_operation_column_and_new_template(db):
    project, _part, _order, _line = _make_project_with_line(db)
    content = master.build_project_master_v2(db, project_id=project.project_id)
    wb = load_workbook(io.BytesIO(content))
    ws = wb[master.V2_SHEET_PARTS]
    headers = [c.value for c in ws[1]]
    assert headers[0] == "操作"
    assert headers.count("实体ID") == 1
    meta = {r[0].value: r[1].value for r in wb[master.V2_SHEET_META].iter_rows(min_col=1, max_col=2)}
    assert meta["template_version"] == "2.1.0"


def test_v21_void_line_sets_inactive_and_excludes_from_export(db):
    project, _part, _order, line = _make_project_with_line(db)
    _content, wb, ws = _parts_sheet(db, project.project_id)
    # row 2 = the exported line; set 操作=VOID
    ws.cell(row=2, column=1, value="VOID")
    _reupload(db, project.project_id, wb)

    db.refresh(line)
    assert line.is_active is False
    assert line.voided_by == "tester"
    assert line.voided_at is not None

    # 再导出：该行不再出现
    _content2, _wb2, ws2 = _parts_sheet(db, project.project_id)
    data_rows = [r for r in ws2.iter_rows(min_row=2, values_only=True)
                 if any(v not in (None, "") for v in r)]
    # 只剩空白新增行（无实体ID）——没有真实行
    assert all(r[21] in (None, "") for r in data_rows)


def test_v21_void_excluded_from_assigned_lines_and_bundle(db):
    project, _part, _order, line = _make_project_with_line(db)
    _content, wb, ws = _parts_sheet(db, project.project_id)
    ws.cell(row=2, column=1, value="VOID")
    _reupload(db, project.project_id, wb)

    rows = master._assigned_lines(db, project_id=project.project_id, window=None)
    assert rows == []
    # 看板成本：作废行不计入
    from app.services import maintenance_boss_board as board
    window = (date(2026, 1, 1), date(2026, 12, 31))
    bundle = board._cost_bundle(db, window=window, project_id=project.project_id,
                                can_cost=True)
    assert bundle["total_lines"] == 0
    assert bundle["actual"] == 0


def test_v21_change_quantity_recomputes_cost_amount(db):
    project, _part, _order, line = _make_project_with_line(
        db, qty=Decimal("2"), unit_cost=Decimal("100.00"))
    original_amount = line.cost_amount_inc_tax
    assert original_amount == Decimal("226.00")  # 2 × 113

    _content, wb, ws = _parts_sheet(db, project.project_id)
    # 需求数量 is column 11 (header index: 操作1, 维保单号2, 制单3, XSDD4, 需求类型5,
    # 仓库6, 销售7, 业务8, PN9, 描述10, 需求数量11)
    ws.cell(row=2, column=11, value=5)
    _reupload(db, project.project_id, wb)

    db.refresh(line)
    assert line.qty == Decimal("5.00")
    assert line.cost_amount_inc_tax == Decimal("565.00")  # 5 × 113
    assert line.cost_amount_ex_tax == Decimal("500.00")


def test_v21_void_cascades_to_site_issue_line(db):
    project, part, order, line = _make_project_with_line(
        db, source_line_id="cascade-source-line-1")
    # 06 领用行通过 source_line_id 文本关联到该 03 行
    issue = MaintenanceSiteIssue(
        issue_id=str(uuid.uuid4()), project_id=project.project_id,
        issue_no="CKD-CASC-1", issue_date=date(2026, 8, 2),
        raw_status="已确认", status_mapping_state="mapped",
        normalized_status="confirmed", status_mapping_version="v1",
        source="legacy",
    )
    db.add(issue)
    db.flush()
    site_line = MaintenanceSiteIssueLine(
        issue_line_id=str(uuid.uuid4()), issue_id=issue.issue_id, line_no=1,
        part_id=part.id, pn=part.pn_std, quantity=Decimal("1"),
        source_order_id=order.raw_order_id, source_line_id=line.raw_line_id,
        algorithm_version="v1",
    )
    db.add(site_line)
    db.commit()

    _content, wb, ws = _parts_sheet(db, project.project_id)
    ws.cell(row=2, column=1, value="VOID")
    _reupload(db, project.project_id, wb)

    db.refresh(site_line)
    assert site_line.is_active is False
    # 06 导出不再包含级联作废的行
    rows = db.execute(
        select(MaintenanceSiteIssueLine).join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        ).where(MaintenanceSiteIssue.project_id == project.project_id,
                MaintenanceSiteIssueLine.is_active.is_(True))
    ).all()
    assert rows == []


def test_v21_create_new_line_under_existing_order(db):
    project, part, order, _line = _make_project_with_line(db)
    _content, wb, ws = _parts_sheet(db, project.project_id)
    # 找一个空白新增行（row 3）填入 CREATE
    ws.cell(row=3, column=1, value="CREATE")
    ws.cell(row=3, column=2, value=order.order_no)   # 维保单号
    ws.cell(row=3, column=9, value=part.pn_std)      # PN
    ws.cell(row=3, column=11, value=3)               # 需求数量
    ws.cell(row=3, column=26, value="新增行备注")     # 备注
    _reupload(db, project.project_id, wb)

    new_line = db.scalar(
        select(FMaintenanceLine).where(
            FMaintenanceLine.order_id == order.id,
            FMaintenanceLine.edited_source == "workbook_manual",
        ))
    assert new_line is not None
    assert new_line.qty == Decimal("3.00")
    assert new_line.raw_line_id.startswith("manual-line:")
    assert new_line.line_note == "新增行备注"
    assert new_line.is_active is True
    # 审计写了 CREATE
    audit = db.scalar(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_id == str(new_line.id),
        MaintenanceProjectOperationAudit.action == "CREATE"))
    assert audit is not None


def test_v21_editing_locked_column_is_rejected(db):
    project, _part, _order, _line = _make_project_with_line(db)
    _content, wb, ws = _parts_sheet(db, project.project_id)
    # XSDD is column 4 — locked
    ws.cell(row=2, column=4, value="XSDD-TAMPERED")
    import pytest
    from app.services.maintenance_expense_collection_workbook import WorkbookError
    with pytest.raises(WorkbookError) as exc:
        master.validate_project_master_v2(
            db, project_id=project.project_id,
            data=_save(wb))
    assert exc.value.code == "readonly_cell_modified"


def test_v21_audit_written_for_update_and_void(db):
    project, _part, _order, line = _make_project_with_line(db)
    _content, wb, ws = _parts_sheet(db, project.project_id)
    ws.cell(row=2, column=11, value=4)  # 改数量
    _reupload(db, project.project_id, wb)

    audits = db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_type == "maintenance_line",
        MaintenanceProjectOperationAudit.entity_id == str(line.id))).all()
    actions = {a.action for a in audits}
    assert "UPDATE" in actions

    # 再作废
    _content2, wb2, ws2 = _parts_sheet(db, project.project_id)
    ws2.cell(row=2, column=1, value="VOID")
    _reupload(db, project.project_id, wb2)
    audits2 = db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_type == "maintenance_line",
        MaintenanceProjectOperationAudit.entity_id == str(line.id))).all()
    assert {a.action for a in audits2} >= {"UPDATE", "VOID"}


def test_v21_unchanged_reupload_is_idempotent(db):
    """原样回传下载的工作簿：不应写 UPDATE 审计、不应改 edited_source、不应重算金额。
    回归：导出总是带上数量/人工成本列，早期实现未与现值 diff，导致原样上传把所有
    行标成 workbook_manual 并写假审计。"""
    project, _part, _order, line = _make_project_with_line(db)
    _content, wb, ws = _parts_sheet(db, project.project_id)
    original_amount = line.cost_amount_inc_tax
    _reupload(db, project.project_id, wb)

    db.refresh(line)
    assert line.edited_source == "wbdd"
    assert line.cost_amount_inc_tax == original_amount
    audits = db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_type == "maintenance_line",
        MaintenanceProjectOperationAudit.entity_id == str(line.id))).all()
    assert audits == []


def test_v21_cannot_edit_already_voided_line(db):
    """已作废行再次出现在旧工作簿里被改并上传 → 报 line_not_found（哈希/实体失效）。"""
    import pytest
    from app.services.maintenance_expense_collection_workbook import WorkbookError
    project, _part, _order, line = _make_project_with_line(db)
    # 先作废
    _c, wb, ws = _parts_sheet(db, project.project_id)
    ws.cell(row=2, column=1, value="VOID")
    _reupload(db, project.project_id, wb)
    db.refresh(line)
    assert line.is_active is False

    # 用另一份（作废前导出的）工作簿改数量后上传：实体已作废应拒绝
    _c2, wb2, ws2 = _parts_sheet(db, project.project_id)
    # 作废后导出已不含该行；手工在第 2 行塞一条带旧实体ID 的改单不可行（行已消失），
    # 因此直接校验：对已作废行 db.get 走解析会报 line_not_found。
    from app.services import maintenance_project_master_workbook as master
    # 构造一个带已作废行实体ID 的最小工作簿
    wb3 = load_workbook(io.BytesIO(_c2))
    ws3 = wb3[master.V2_SHEET_PARTS]
    ws3.cell(row=2, column=1, value="UPDATE")
    ws3.cell(row=2, column=22, value=line.id)  # 实体ID（openpyxl 1-based；22=实体ID）
    ws3.cell(row=2, column=11, value=9)        # 需求数量
    with pytest.raises(WorkbookError) as exc:
        master.validate_project_master_v2(
            db, project_id=project.project_id, data=_save(wb3))
    assert exc.value.code == "line_not_found"


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
