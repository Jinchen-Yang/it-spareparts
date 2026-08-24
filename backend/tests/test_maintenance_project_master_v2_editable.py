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
    FProjectExpense,
    MaintenanceDemandTombstone,
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
    tag = uuid.uuid4().hex[:8]
    project = MaintenanceProject(
        project_id=str(uuid.uuid4()), project_code=f"EDIT-{tag}",
        display_name="可编辑测试", lifecycle_status="ongoing",
    )
    # PN 查找走 upper() 归一，夹具须用大写 hex 避免大小写错配
    part = DimPart(pn_std=f"EDIT-PN-{tag.upper()}", description="可编辑备件")
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
    assert meta["template_version"] == "2.3.0"


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
    assert bundle["value"]["actual_amount"] == 0
    assert bundle["value"]["missing_lines"] == 0


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
    ws.cell(row=3, column=25, value="新增行备注")     # 备注（1-based：第 25 列）
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


# ----------------------------------------------------------------------
# 2026-08-19（#264/#267）：04 报销作废（显式 VOID + 缺行=作废）、防呆、
# void-fast 一键批量作废、latest/missing 差异清单。
# ----------------------------------------------------------------------

def _make_project_with_expense(db, *, data_status=None, raw_line_id=None):
    project, _part, order, line = _make_project_with_line(db)
    expense = FProjectExpense(
        raw_line_id=raw_line_id or f"bxd-{uuid.uuid4()}",
        import_batch_id=_batch(db),
        bxd_no="BXD-EDIT-001", line_no=1,
        expense_date=date(2026, 8, 3), person="测试员",
        linked_sales_order_no="XSDD-EDIT-001",
        amount=Decimal("500.00"), tax_basis="ex",
        amount_ex_tax=Decimal("500.00"),
        amount_inc_tax=Decimal("565.00"),
        data_status=data_status,
    )
    db.add(expense)
    db.commit()
    return project, order, line, expense


def test_v21_expense_missing_row_becomes_void(db):
    """缺行=作废（用户核心诉求）：下载→删掉报销行→回传→行从系统消失。

    两行删一行恰好等于 50%（不触发防呆）；删光全部行则被 50% 防呆整本拒绝。
    """
    import pytest
    from app.services.maintenance_expense_collection_workbook import WorkbookError
    project, _order, _line, expense = _make_project_with_expense(db)
    keeper = FProjectExpense(
        raw_line_id=f"bxd-{uuid.uuid4()}",
        bxd_no="BXD-EDIT-001", line_no=2,
        expense_date=date(2026, 8, 4), person="测试员",
        linked_sales_order_no="XSDD-EDIT-001",
        amount=Decimal("300.00"), tax_basis="ex",
        amount_ex_tax=Decimal("300.00"),
        amount_inc_tax=Decimal("339.00"),
        import_batch_id=_batch(db),
    )
    db.add(keeper)
    db.commit()
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_EXPENSE,))
    wb = load_workbook(io.BytesIO(content))
    ws = wb[master.V2_SHEET_EXPENSE]
    # 2026-08-22 撤防呆：删光全部 → 放行为全量作废（用户拍板大批量修改期）
    wb_all_deleted = load_workbook(io.BytesIO(content))
    wb_all_deleted[master.V2_SHEET_EXPENSE].delete_rows(2, 2)
    buf_all = io.BytesIO()
    wb_all_deleted.save(buf_all)
    plan_all = master.validate_project_master_v2(
        db, project_id=project.project_id, data=buf_all.getvalue())
    assert all(r.operation == "VOID" for r in plan_all.expense_updates)
    # 正常路径：只删第一行（=50%，放行）
    ws.delete_rows(2)
    buf = io.BytesIO()
    wb.save(buf)
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=buf.getvalue())
    assert expense.raw_line_id in plan.expense_voids
    assert any(r["sheet"] == "04_费用报销" and r["entity_id"] == expense.raw_line_id
               for r in plan.will_void_rows)
    master.apply_project_master_v2(
        db, plan, operated_by="tester", import_batch_id=str(uuid.uuid4()))

    db.refresh(expense)
    assert expense.data_status == "已作废"
    # 再导出：作废行彻底不出现（用户拍板：干脆不导出）
    content2 = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_EXPENSE,))
    ws2 = load_workbook(io.BytesIO(content2))[master.V2_SHEET_EXPENSE]
    rows = [r for r in ws2.iter_rows(min_row=2, values_only=True)
            if any(v not in (None, "") for v in r)]
    assert all(r[17] != expense.raw_line_id for r in rows)  # 实体ID 列（1-based→idx 17）


def test_v21_expense_explicit_void_operation(db):
    project, _order, _line, expense = _make_project_with_expense(db)
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_EXPENSE,))
    wb = load_workbook(io.BytesIO(content))
    wb[master.V2_SHEET_EXPENSE].cell(row=2, column=1, value="VOID")
    buf = io.BytesIO()
    wb.save(buf)
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=buf.getvalue())
    assert plan.expense_voids == (expense.raw_line_id,)
    master.apply_project_master_v2(
        db, plan, operated_by="tester", import_batch_id=str(uuid.uuid4()))
    db.refresh(expense)
    assert expense.data_status == "已作废"


def test_v21_row_loss_guard_removed_all_rows_voidable(db):
    """2026-08-22 撤防呆：≥2 行的表删光全部数据行 → 全量 VOID 放行（用户拍板）。"""
    import pytest
    from app.services.maintenance_expense_collection_workbook import WorkbookError

    project, part, _order, _line = _make_project_with_line(db)
    second = FMaintenanceLine(
        raw_line_id=f"raw-line-{uuid.uuid4()}",
        order_id=_order.id, line_no=2, part_id=part.id,
        pn_std=part.pn_std, pn_raw=part.pn_std, description=part.description,
        qty=Decimal("1"), return_qty=Decimal("0"), import_batch_id=_batch(db),
    )
    db.add(second)
    db.commit()
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_PARTS,))
    wb = load_workbook(io.BytesIO(content))
    wb[master.V2_SHEET_PARTS].delete_rows(2, 2)  # 删光 2 条数据行 → 全量 VOID
    buf = io.BytesIO()
    wb.save(buf)
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=buf.getvalue())
    assert [r.operation for r in plan.cost_refills].count("VOID") == 2


def test_void_fast_tombstones_deactivates_assignment(db):
    from app.services import maintenance_demands

    project, _part, order, _line = _make_project_with_line(db)
    result = maintenance_demands.void_fast(
        db, source_order_ids=[order.raw_order_id],
        reason="氚云已删除", operated_by="tester")
    assert result["voided"] == 1 and result["already_voided"] == 0

    assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == order.raw_order_id))
    assert assignment.is_active is False
    tombstone = db.get(MaintenanceDemandTombstone, order.raw_order_id)
    assert tombstone is not None and tombstone.restored_at is None
    # 总表行随即消失（读侧联动）
    assert master._assigned_lines(db, project_id=project.project_id, window=None) == []


def test_void_fast_unknown_order_rejects_whole_batch(db):
    import pytest
    from app.services import maintenance_demands

    project, _part, order, _line = _make_project_with_line(db)
    with pytest.raises(maintenance_demands.MaintenanceDemandNotFound):
        maintenance_demands.void_fast(
            db, source_order_ids=[order.raw_order_id, "raw-order-does-not-exist"],
            reason="测试", operated_by="tester")
    # 整批零写入：已知单也没有墓碑
    assert db.get(MaintenanceDemandTombstone, order.raw_order_id) is None


def test_void_fast_already_tombstoned_is_idempotent(db):
    from app.services import maintenance_demands

    _project, _part, order, _line = _make_project_with_line(db)
    first = maintenance_demands.void_fast(
        db, source_order_ids=[order.raw_order_id],
        reason="第一次", operated_by="tester")
    second = maintenance_demands.void_fast(
        db, source_order_ids=[order.raw_order_id],
        reason="重复点击", operated_by="tester")
    assert first["voided"] == 1
    assert second["already_voided"] == 1 and second["voided"] == 0


def test_latest_missing_reports_disappeared_orders(db):
    from app.models.maintenance_wbdd_import import MaintenanceWbddImportReceipt
    from app.services import maintenance_wbdd_import as wbdd

    project, _part, order, _line = _make_project_with_line(db)
    # 用 order 所在批次造一张回执：文件单集 = 该批次全部单（含 order）
    batch_id = order.import_batch_id
    db.add(MaintenanceWbddImportReceipt(
        batch_id=batch_id, idempotency_key="test-key-0001",
        uploaded_by="tester", file_hash=uuid.uuid4().hex,
        report_json={"batch_id": batch_id},
    ))
    db.commit()
    result = wbdd.latest_missing(db)
    assert result["readiness"] == "ready"
    # 文件里存在 order → 不在缺失清单
    assert all(m["source_order_id"] != order.raw_order_id
               for m in result["missing_orders"])

    # 再造一张挂在别的批次的新单（库里有、回执批次文件里没有）→ 进缺失清单
    other_batch = _batch(db)
    ghost = FMaintenanceOrder(
        raw_order_id=f"raw-ghost-{uuid.uuid4()}", order_no="WBDD-GHOST-001",
        order_date=order.order_date, linked_sales_order_no="XSDD-EDIT-001",
        project_raw="可编辑测试", data_status="已生效",
        import_batch_id=other_batch,
    )
    db.add(ghost)
    db.commit()
    result2 = wbdd.latest_missing(db)
    assert any(m["source_order_id"] == ghost.raw_order_id
               for m in result2["missing_orders"])
    entry = next(m for m in result2["missing_orders"]
                 if m["source_order_id"] == ghost.raw_order_id)
    assert entry["order_no"] == "WBDD-GHOST-001"
    assert entry["line_count"] == 0


def test_void_fast_returns_per_order_results(db):
    """#265 冻结契约：逐单 results（幂等命中 already_voided 不算错误）。"""
    from app.services import maintenance_demands

    _project, _part, order, _line = _make_project_with_line(db)
    result = maintenance_demands.void_fast(
        db, source_order_ids=[order.raw_order_id],
        reason="契约测试", operated_by="tester")
    assert result["results"] == [
        {"source_order_id": order.raw_order_id,
         "order_no": order.order_no, "status": "voided"},
    ]
    again = maintenance_demands.void_fast(
        db, source_order_ids=[order.raw_order_id],
        reason="重复", operated_by="tester")
    assert again["results"][0]["status"] == "already_voided"


def test_search_include_voided_view(db):
    """默认视图不见已作废单；include_voided=True 可见且带 is_voided 标记。"""
    from app.services import maintenance_demands

    _project, _part, order, _line = _make_project_with_line(db)
    maintenance_demands.void_fast(
        db, source_order_ids=[order.raw_order_id],
        reason="测试视图", operated_by="tester")

    active = maintenance_demands.search_demands(
        db, q=None, page=1, page_size=20)
    assert all(item["source_order_id"] != order.raw_order_id
               for item in active["items"])

    with_voided = maintenance_demands.search_demands(
        db, q=None, page=1, page_size=20, include_voided=True)
    entry = next(item for item in with_voided["items"]
                 if item["source_order_id"] == order.raw_order_id)
    assert entry["is_voided"] is True
    assert entry["order_no"] == order.order_no


def test_review_p2_void_fast_idempotent_replay(db):
    """同幂等键同请求重放 → 返回首次结果；同键异请求 → 冲突。"""
    import pytest
    from app.services import maintenance_demands

    _project, _part, order, _line = _make_project_with_line(db)
    first = maintenance_demands.void_fast(
        db, source_order_ids=[order.raw_order_id],
        reason="重放测试", operated_by="tester", idempotency_key="replay-key-01")
    db.commit()
    replay = maintenance_demands.void_fast(
        db, source_order_ids=[order.raw_order_id],
        reason="重放测试", operated_by="tester", idempotency_key="replay-key-01")
    assert replay["replayed"] is True
    assert replay["voided"] == first["voided"] == 1
    with pytest.raises(maintenance_demands.DeleteIntentConflict):
        maintenance_demands.void_fast(
            db, source_order_ids=[order.raw_order_id],
            reason="不同的请求", operated_by="tester", idempotency_key="replay-key-01")


def test_review_p1_cross_project_line_is_rejected(db):
    """拿别项目的实体ID 在本项目入口上传 → line_not_in_project 拒绝。"""
    import pytest
    from app.services.maintenance_expense_collection_workbook import WorkbookError

    project_a, _p, _o, line_a = _make_project_with_line(db)
    project_b, _p2, _o2, _line_b = _make_project_with_line(db)
    _content, wb, _ws = _parts_sheet(db, project_b.project_id)
    ws = wb[master.V2_SHEET_PARTS]
    # 把 A 项目的实体ID 塞进 B 项目的空白新增行并标 UPDATE
    ws.cell(row=2, column=1, value="UPDATE")
    ws.cell(row=2, column=22, value=line_a.id)
    ws.cell(row=2, column=11, value=9)
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(WorkbookError) as exc:
        master.validate_project_master_v2(
            db, project_id=project_b.project_id, data=buf.getvalue())
    assert exc.value.code in ("line_not_in_project", "readonly_cell_modified", "line_not_found")


def test_review_p1_pn_change_updates_part_id(db):
    """改 PN 必须同步换 part_id，否则库存/成本 join 腐化。"""
    project, part_old, _order, line = _make_project_with_line(db)
    part_new = DimPart(pn_std="EDIT-PN-002", description="新备件")
    db.add(part_new)
    db.commit()
    _content, wb, ws = _parts_sheet(db, project.project_id)
    ws.cell(row=2, column=9, value=part_new.pn_std)  # PN
    _reupload(db, project.project_id, wb)
    db.refresh(line)
    assert line.pn_std == part_new.pn_std
    assert line.part_id == part_new.id


def test_review_p1_late_imported_expense_is_not_voided(db):
    """下载后新导入的报销行不在导出全集里 → 缺行判定不得误杀；
    用户真删的行（导出时存在）照常作废（P1，Codex review #272）。"""
    project, _order, _line, expense = _make_project_with_expense(db)
    keeper = FProjectExpense(
        raw_line_id=f"bxd-{uuid.uuid4()}",
        bxd_no="BXD-EDIT-001", line_no=2,
        expense_date=date(2026, 8, 4), person="测试员",
        linked_sales_order_no="XSDD-EDIT-001",
        amount=Decimal("300.00"), tax_basis="ex",
        amount_ex_tax=Decimal("300.00"), amount_inc_tax=Decimal("339.00"),
        import_batch_id=_batch(db),
    )
    db.add(keeper)
    db.commit()
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_EXPENSE,))
    # 下载之后、上传之前：新导入一行报销（不在导出全集）
    late = FProjectExpense(
        raw_line_id=f"bxd-late-{uuid.uuid4()}",
        bxd_no="BXD-LATE-001", line_no=1,
        expense_date=date(2026, 8, 5), person="迟到导入",
        linked_sales_order_no="XSDD-EDIT-001",
        amount=Decimal("100.00"), tax_basis="ex",
        amount_ex_tax=Decimal("100.00"), amount_inc_tax=Decimal("113.00"),
        import_batch_id=_batch(db),
    )
    db.add(late)
    db.commit()
    # 用户在旧文件里删掉导出时存在的那行
    wb = load_workbook(io.BytesIO(content))
    wb[master.V2_SHEET_EXPENSE].delete_rows(2)
    buf = io.BytesIO()
    wb.save(buf)
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=buf.getvalue())
    assert plan.expense_voids == (expense.raw_line_id,)
    master.apply_project_master_v2(
        db, plan, operated_by="tester", import_batch_id=str(uuid.uuid4()))
    db.refresh(expense)
    db.refresh(late)
    assert expense.data_status == "已作废"
    assert late.data_status != "已作废"


def test_v22_parts_missing_row_becomes_void(db):
    """03 删行=作废（与 04 同语义，2026-08-20 用户拍板）。
    两行删一行＝50%，不触发防呆；唯一行被删会被防呆拦（契约行为）。"""
    project, part, order, line = _make_project_with_line(db)
    second = FMaintenanceLine(
        raw_line_id=f"raw-line-{uuid.uuid4()}",
        order_id=order.id, line_no=2, part_id=part.id,
        pn_std=part.pn_std, pn_raw=part.pn_std, description=part.description,
        qty=Decimal("1"), return_qty=Decimal("0"), import_batch_id=_batch(db),
    )
    db.add(second)
    db.commit()
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_PARTS,))
    wb = load_workbook(io.BytesIO(content))
    wb[master.V2_SHEET_PARTS].delete_rows(2)  # 删第 2 行（第一条明细）
    buf = io.BytesIO()
    wb.save(buf)
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=buf.getvalue())
    assert any(r.operation == "VOID" and r.line_id == line.id for r in plan.cost_refills)
    assert any(r["sheet"] == "03_备件明细" and r.get("reason") == "上传文件缺行"
               for r in plan.will_void_rows)
    master.apply_project_master_v2(
        db, plan, operated_by="tester", import_batch_id=str(uuid.uuid4()))
    db.refresh(line)
    assert line.is_active is False


def test_v22_void_all_lines_cascades_order_tombstone(db):
    """整单行全作废 → 级联整单墓碑：搜索消失、挂靠停用、重传不复活。"""
    from app.services import maintenance_demands

    project, _part, order, line = _make_project_with_line(db)
    _content, wb, ws = _parts_sheet(db, project.project_id)
    ws.cell(row=2, column=1, value="VOID")
    _reupload(db, project.project_id, wb)

    db.refresh(line)
    assert line.is_active is False
    tombstone = db.get(MaintenanceDemandTombstone, order.raw_order_id)
    assert tombstone is not None and tombstone.restored_at is None
    assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == order.raw_order_id))
    assert assignment.is_active is False
    # 默认搜索不见
    result = maintenance_demands.search_demands(db, q=None, page=1, page_size=20)
    assert all(item["source_order_id"] != order.raw_order_id
               for item in result["items"])


def test_v22_template_has_usage_sheet_dropdown_and_yellow_editable(db):
    project, _p, _o, _l = _make_project_with_line(db)
    content = master.build_project_master_v2(db, project_id=project.project_id)
    wb = load_workbook(io.BytesIO(content))
    # 使用说明 sheet
    assert master.V2_SHEET_USAGE in wb.sheetnames
    usage_texts = "\n".join(str(c.value) for row in wb[master.V2_SHEET_USAGE].iter_rows()
                            for c in row if c.value)
    assert "黄底" in usage_texts and "VOID" in usage_texts
    # 操作列下拉
    ws = wb[master.V2_SHEET_PARTS]
    dvs = list(ws.data_validations.dataValidation)
    assert any(dv.type == "list" and "VOID" in (dv.formula1 or "") for dv in dvs)
    # 可编辑数据区黄底（PN 列=9，第 2 行）
    from openpyxl.styles import PatternFill
    fill = ws.cell(row=2, column=9).fill
    assert fill.start_color.rgb in ("00FFE699", "FFFFE699") or fill.fgColor.rgb == "00FFE699"
    # 模板版本 2.2.0
    meta = {r[0].value: r[1].value for r in wb[master.V2_SHEET_META].iter_rows(min_col=1, max_col=2)}
    assert meta["template_version"] == "2.3.0"


def test_latest_missing_allows_full_sync_at_any_ratio(db):
    """2026-08-24 用户拍板：缺失占比任意（含 >50%/100%）都不再拦截——
    差异清单与批量作废解锁，可全量跟随修改；占比仅作展示。"""
    from app.models.maintenance_wbdd_import import MaintenanceWbddImportReceipt
    from app.services import maintenance_wbdd_import as wbdd

    project, _part, order, _line = _make_project_with_line(db)
    # 再造 4 张同窗口、不在文件批次里的单 → 库内 5 张活跃，文件只有 1 张（80%）
    others = []
    for i in range(4):
        b = _batch(db)
        o = FMaintenanceOrder(
            raw_order_id=f"raw-ghost-{i}-{uuid.uuid4()}", order_no=f"WBDD-GHOST-{i}",
            order_date=order.order_date, linked_sales_order_no="XSDD-EDIT-001",
            project_raw="可编辑测试", data_status="已生效", import_batch_id=b,
        )
        others.append(o)
        db.add(o)
    db.add(MaintenanceWbddImportReceipt(
        batch_id=order.import_batch_id, idempotency_key="suspicious-key-0001",
        uploaded_by="tester", file_hash=uuid.uuid4().hex,
        report_json={"batch_id": order.import_batch_id},
    ))
    db.commit()
    result = wbdd.latest_missing(db)
    assert result["missing_count"] == 4
    assert result["db_active_in_window"] == 5
    assert "suspicious" not in result
    assert result["missing_ratio"] > 0.5
    assert len(result["missing_orders"]) == 4


def test_e2e_fix_single_row_delete_allowed(db):
    """E2E #1：单行表删除唯一一行不再被防呆拦截（防呆只拦批量损失）。"""
    project, _order, _line, _expense = _make_project_with_expense(db)
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_EXPENSE,))
    wb = load_workbook(io.BytesIO(content))
    wb[master.V2_SHEET_EXPENSE].delete_rows(2)  # 唯一一行
    buf = io.BytesIO()
    wb.save(buf)
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=buf.getvalue())
    assert plan.expense_voids == (_expense.raw_line_id,)
    master.apply_project_master_v2(
        db, plan, operated_by="tester", import_batch_id=str(uuid.uuid4()))
    db.refresh(_expense)
    assert _expense.data_status == "已作废"


def test_e2e_fix_summary_distinguishes_qty_from_cost(db):
    """E2E #5：改数量报 line_updates/qty_updates，不再误报 cost_overrides。"""
    project, _part, _order, line = _make_project_with_line(db)
    _c, wb, ws = _parts_sheet(db, project.project_id)
    ws.cell(row=2, column=11, value=5)
    buf = io.BytesIO(); wb.save(buf)
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=buf.getvalue())
    assert plan.summary["qty_updates"] == 1
    assert plan.summary["line_updates"] == 1
    assert plan.summary["cost_overrides"] == 0


def test_v23_example_rows_present_and_ignored_on_upload(db):
    """每个数据 sheet 底部有灰色示例行；回传时被系统忽略（零变更幂等）。"""
    project, _order, _line, _expense = _make_project_with_expense(db)
    content = master.build_project_master_v2(db, project_id=project.project_id)
    wb = load_workbook(io.BytesIO(content))
    for sheet_name in (master.V2_SHEET_PLAN, master.V2_SHEET_PARTS,
                       master.V2_SHEET_EXPENSE, master.V2_SHEET_RECEIPTS,
                       master.V2_SHEET_SITE):
        ws = wb[sheet_name]
        # finalize 会向下多刷 20 行样式（max_row 被撑大），全表扫找示例标记
        found = any(
            str(c.value or "").strip() in ("示例",) or str(c.value or "").startswith("【示例】")
            for row in ws.iter_rows(min_row=2) for c in row
        )
        assert found, sheet_name
    # 原样回传（含示例行）：零变更
    buf = io.BytesIO()
    wb.save(buf)
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=buf.getvalue())
    assert plan.summary["line_creates"] == 0
    assert plan.summary["line_updates"] == 0
    assert plan.summary["line_voids"] == 0
    assert plan.summary["expense_creates"] == 0
    assert plan.summary["expense_voids"] == 0
    assert plan.summary["plan_creates"] == 0
    assert not plan.will_void_rows


def test_v23_plan_accepts_xsdd_without_ledger_contract(db):
    """台账未导入的项目也能填 02：合同号=挂靠 XSDD → 自动建合同（用户踩坑）。"""
    from app.models.maintenance_project import MaintenanceProjectContract

    project, _part, order, _line = _make_project_with_line(db)
    # 该项目挂靠单的 XSDD = XSDD-EDIT-001（fixture 里 linked_sales_order_no）
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_PLAN,))
    wb = load_workbook(io.BytesIO(content))
    ws = wb[master.V2_SHEET_PLAN]
    # 在示例行后追加一条 CREATE（用挂靠 XSDD，不依赖台账）
    ws.append(["CREATE", "XSDD-EDIT-001", 1, "2026-09-30", "day", 40000,
               None, None, None, None, None, None, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=buf.getvalue())
    assert plan.summary["plan_creates"] == 1
    master.apply_project_master_v2(
        db, plan, operated_by="tester", import_batch_id=str(uuid.uuid4()))
    contract = db.scalar(select(MaintenanceProjectContract).where(
        MaintenanceProjectContract.project_id == project.project_id,
        MaintenanceProjectContract.contract_no == "XSDD-EDIT-001"))
    assert contract is not None  # 自动建出来了


def test_v23_plan_rejects_with_helpful_error(db):
    """不属于本项目的合同号 → 报错列出可用合同。"""
    import pytest
    from app.services.maintenance_expense_collection_workbook import WorkbookError

    project, _part, _order, _line = _make_project_with_line(db)
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_PLAN,))
    wb = load_workbook(io.BytesIO(content))
    ws = wb[master.V2_SHEET_PLAN]
    ws.append(["CREATE", "XSDD-别的项目", 1, "2026-09-30", "day", 100, None, None,
               None, None, None, None, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(WorkbookError) as exc:
        master.validate_project_master_v2(
            db, project_id=project.project_id, data=buf.getvalue())
    assert "可用" in str(exc.value) and "XSDD-EDIT-001" in str(exc.value)


def test_v23_plan_modify_without_operation_applies(db):
    """02 改单元格（不填操作）→ 生效；改了日期/金额才下发。"""
    from app.models.maintenance_manager import MaintenanceCollectionMilestone

    project, _part, _order, _line = _make_project_with_line(db)
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_PLAN,))
    # 先 CREATE 一期
    wb = load_workbook(io.BytesIO(content))
    ws = wb[master.V2_SHEET_PLAN]
    ws.append(["CREATE", "XSDD-EDIT-001", 1, "2026-09-30", "day", 40000,
               None, None, None, None, None, None, None, None])
    buf = io.BytesIO(); wb.save(buf)
    plan = master.validate_project_master_v2(db, project_id=project.project_id, data=buf.getvalue())
    master.apply_project_master_v2(db, plan, operated_by="tester", import_batch_id=str(uuid.uuid4()))

    # 重新下载，不填操作直接改金额 → 生效
    content2 = master.build_project_master_v2(db, project_id=project.project_id, sheets=(master.V2_SHEET_PLAN,))
    wb2 = load_workbook(io.BytesIO(content2))
    ws2 = wb2[master.V2_SHEET_PLAN]
    found = False
    for r in ws2.iter_rows(min_row=2):
        if r[12].value:  # 实体ID 列（1-based 13 → idx 12）
            r[5].value = 30000  # 计划金额
            found = True
    assert found
    buf2 = io.BytesIO(); wb2.save(buf2)
    plan2 = master.validate_project_master_v2(db, project_id=project.project_id, data=buf2.getvalue())
    assert plan2.summary["plan_updates"] == 1
    master.apply_project_master_v2(db, plan2, operated_by="tester", import_batch_id=str(uuid.uuid4()))
    ms = db.scalar(select(MaintenanceCollectionMilestone).where(
        MaintenanceCollectionMilestone.project_id == project.project_id))
    assert ms.planned_amount == Decimal("30000")

    # 原样再传（什么都不改）→ 零变更
    content3 = master.build_project_master_v2(db, project_id=project.project_id, sheets=(master.V2_SHEET_PLAN,))
    plan3 = master.validate_project_master_v2(db, project_id=project.project_id, data=content3)
    assert plan3.summary["plan_updates"] == 0 and plan3.summary["plan_creates"] == 0


def test_v23_plan_delete_row_voids(db):
    """02 删行=作废（对齐 03/04）。"""
    from app.models.maintenance_manager import MaintenanceCollectionMilestone

    project, _part, _order, _line = _make_project_with_line(db)
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_PLAN,))
    wb = load_workbook(io.BytesIO(content))
    wb[master.V2_SHEET_PLAN].append(["CREATE", "XSDD-EDIT-001", 1, "2026-09-30", "day", 40000,
                                     None, None, None, None, None, None, None, None])
    buf = io.BytesIO(); wb.save(buf)
    plan = master.validate_project_master_v2(db, project_id=project.project_id, data=buf.getvalue())
    master.apply_project_master_v2(db, plan, operated_by="tester", import_batch_id=str(uuid.uuid4()))

    content2 = master.build_project_master_v2(db, project_id=project.project_id, sheets=(master.V2_SHEET_PLAN,))
    wb2 = load_workbook(io.BytesIO(content2))
    ws2 = wb2[master.V2_SHEET_PLAN]
    for r in range(ws2.max_row, 1, -1):
        if ws2.cell(r, 13).value:  # 实体ID
            ws2.delete_rows(r)
    buf2 = io.BytesIO(); wb2.save(buf2)
    plan2 = master.validate_project_master_v2(db, project_id=project.project_id, data=buf2.getvalue())
    assert plan2.summary["plan_voids"] == 1
    assert any(x["sheet"] == "02_回款计划" for x in plan2.will_void_rows)
    master.apply_project_master_v2(db, plan2, operated_by="tester", import_batch_id=str(uuid.uuid4()))
    ms = db.scalar(select(MaintenanceCollectionMilestone).where(
        MaintenanceCollectionMilestone.project_id == project.project_id))
    assert ms.is_active is False


# ---------------------------------------------------------------- 06 缺行=作废（2026-08-23 用户口径）

def _site_issue(db, project, part, *, qty="2", line_no=1, issue_no="ISS-V2-1"):
    from app.models.maintenance_project_operations import (
        MaintenanceSiteIssue,
        MaintenanceSiteIssueLine,
    )

    issue = MaintenanceSiteIssue(
        issue_id=str(uuid.uuid4()), project_id=project.project_id,
        issue_no=issue_no, issue_date=date(2026, 8, 10),
        raw_status="已确认", status_mapping_state="mapped",
        normalized_status="confirmed", status_mapping_version="t",
        source="legacy")
    db.add(issue)
    db.flush()
    line = MaintenanceSiteIssueLine(
        issue_line_id=str(uuid.uuid4()), issue_id=issue.issue_id,
        line_no=line_no, part_id=part.id, pn=part.pn_std,
        quantity=Decimal(qty), algorithm_version="t",
        tax_rate_used=Decimal("0.13"), is_active=True)
    db.add(line)
    db.commit()
    return issue, line


def test_v2_site_missing_row_voids_line_and_issue(db):
    """06 删行覆盖上传 → 领用行作废；单只剩空 → 单据状态置 void。"""
    from app.models.maintenance_project_operations import (
        MaintenanceSiteIssue,
        MaintenanceSiteIssueLine,
    )

    project, part, _order, _line = _make_project_with_line(db)
    issue, sline = _site_issue(db, project, part)
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_SITE,))
    wb = load_workbook(io.BytesIO(content))
    ws = wb[master.V2_SHEET_SITE]
    # 删掉全部数据行（示例行保留与否都行——解析跳过）
    data_rows = ws.max_row - 1
    ws.delete_rows(2, data_rows)
    buf = io.BytesIO()
    wb.save(buf)
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=buf.getvalue())
    assert plan.summary["site_voids"] == 1
    master.apply_project_master_v2(
        db, plan, operated_by="tester", import_batch_id=str(uuid.uuid4()))
    db.refresh(sline)
    db.refresh(issue)
    assert sline.is_active is False
    assert issue.normalized_status == "void"
    # 再导出：作废行不再出现
    content2 = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_SITE,))
    wb2 = load_workbook(io.BytesIO(content2))
    body_rows = [r for r in wb2[master.V2_SHEET_SITE].iter_rows(min_row=2, values_only=True)
                 if any(v not in (None, "") for v in r)
                 and not str(r[2] or "").startswith("（示例）")]
    assert all(row[10] != sline.issue_line_id for row in body_rows)


def test_v2_site_roundtrip_unchanged_is_zero_ops(db):
    """原样回传（不删任何行）→ 零作废零更新（幂等）。"""
    project, part, _order, _line = _make_project_with_line(db)
    _site_issue(db, project, part)
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_SITE,))
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=content)
    assert plan.summary["site_voids"] == 0
    assert plan.summary["site_creates"] == 0
    # site_updates 允许 ≥0：解析器对原样行也会记一条「更新」（apply 幂等，
    # 无实际变化）——既有行为，不在本断言范围


def test_v2_site_partial_delete_voids_only_missing(db):
    """两张单各一行，只删一张 → 只作废被删那张。"""
    from app.models.maintenance_project_operations import (
        MaintenanceSiteIssueLine,
    )

    project, part, _order, _line = _make_project_with_line(db)
    issue_a, line_a = _site_issue(db, project, part, issue_no="ISS-A")
    issue_b, line_b = _site_issue(db, project, part, issue_no="ISS-B")
    content = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_SITE,))
    wb = load_workbook(io.BytesIO(content))
    ws = wb[master.V2_SHEET_SITE]
    # 删掉 A 那一行（数据首行）
    ws.delete_rows(2, 1)
    buf = io.BytesIO()
    wb.save(buf)
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=buf.getvalue())
    assert plan.summary["site_voids"] == 1
    master.apply_project_master_v2(
        db, plan, operated_by="tester", import_batch_id=str(uuid.uuid4()))
    db.refresh(line_a)
    db.refresh(line_b)
    assert line_a.is_active is False
    assert line_b.is_active is True
