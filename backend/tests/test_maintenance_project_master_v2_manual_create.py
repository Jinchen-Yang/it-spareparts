import io
import uuid
from datetime import date
from decimal import Decimal

from openpyxl import load_workbook
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.maintenance import FProjectExpense
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceSiteIssueLine
from app.services import maintenance_project_master_workbook as master


def _project(db):
    project = MaintenanceProject(
        project_id=str(uuid.uuid4()),
        project_code="MANUAL-CREATE",
        display_name="手工新增兼容测试",
        lifecycle_status="ongoing",
    )
    part = DimPart(pn_std="MANUAL-PN-001", description="手工领用测试备件")
    db.add_all([project, part])
    db.flush()
    db.add(MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()),
        project_id=project.project_id,
        contract_id="MANUAL-CONTRACT",
        contract_no="XSDD-MANUAL-001",
        amount_inc_tax=Decimal("10000.00"),
        included_in_total=True,
        status_mapping_state="mapped",
        status_mapping_version="v1",
        effective_from=date(2026, 1, 1),
        source="ledger",
        version=1,
    ))
    db.commit()
    return project, part


def _manual_workbook(db, project_id: str) -> bytes:
    content = master.build_project_master_v2(db, project_id=project_id)
    wb = load_workbook(io.BytesIO(content))
    expense = wb[master.V2_SHEET_EXPENSE]
    # V2.1：第 1 列为「操作」，留空 = 空白实体手工新增（CREATE 语义）
    expense.append([
        "", "BXD-20260818-0001", 1, "2026-08-18 10:00:00", "测试人员",
        "维保费用", "交通费", "人工新增报销", "XSDD-MANUAL-001",
        "手工新增兼容测试", "测试销售", "已生效", 100, "ex", 100,
        113, "人工新增", "",
    ])
    site = wb[master.V2_SHEET_SITE]
    site.append([
        "CKD-20260818-0001", "2026-08-18", "MANUAL-PN-001", "SN-001",
        1, "是", "人工新增领用", 1, "", "", "",
    ])
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


def test_v2_blank_entity_ids_create_expense_and_site_rows(db):
    project, part = _project(db)
    content = _manual_workbook(db, project.project_id)

    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=content,
    )
    assert plan.summary["expense_creates"] == 1
    assert plan.summary["site_creates"] == 1

    result = master.apply_project_master_v2(
        db,
        plan,
        operated_by="manual-test",
        import_batch_id=str(uuid.uuid4()),
    )
    assert result["expense_creates"] == 1
    assert result["site_creates"] == 1

    expense = db.scalar(select(FProjectExpense))
    assert expense is not None
    assert expense.raw_line_id
    assert expense.bxd_no == "BXD-20260818-0001"
    assert expense.line_no == 1
    assert expense.amount_ex_tax == Decimal("100.00")
    assert expense.amount_inc_tax == Decimal("113.00")
    assert expense.data_status == "已结束"

    site_line = db.scalar(select(MaintenanceSiteIssueLine))
    assert site_line is not None
    assert site_line.issue_line_id
    assert site_line.part_id == part.id
    assert site_line.pn == "MANUAL-PN-001"

    exported = load_workbook(io.BytesIO(
        master.build_project_master_v2(db, project_id=project.project_id)
    ), data_only=True)
    assert exported[master.V2_SHEET_EXPENSE]["R2"].value  # V2.1：实体ID 移至第 18 列（操作列插入） == expense.raw_line_id
    assert exported[master.V2_SHEET_SITE]["K2"].value == site_line.issue_line_id


def test_v2_reupload_of_blank_ids_is_idempotent(db):
    project, _part = _project(db)
    content = _manual_workbook(db, project.project_id)

    for _ in range(2):
        plan = master.validate_project_master_v2(
            db, project_id=project.project_id, data=content,
        )
        master.apply_project_master_v2(
            db,
            plan,
            operated_by="manual-test",
            import_batch_id=str(uuid.uuid4()),
        )

    assert len(db.scalars(select(FProjectExpense)).all()) == 1
    assert len(db.scalars(select(MaintenanceSiteIssueLine)).all()) == 1


def test_v2_single_sheet_export_only_validates_that_sheet(db):
    project, _part = _project(db)
    content = master.build_project_master_v2(
        db,
        project_id=project.project_id,
        sheets=(master.V2_SHEET_EXPENSE,),
    )
    wb = load_workbook(io.BytesIO(content), data_only=True)
    assert wb.sheetnames == [
        master.V2_SHEET_EXPENSE,
        master.V2_SHEET_DICTIONARY,
        master.V2_SHEET_USAGE,
        master.V2_SHEET_META,
    ]
    assert wb[master.V2_SHEET_META]["B6"].value == master.V2_SHEET_EXPENSE
    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=content,
    )
    assert plan.sheets == (master.V2_SHEET_EXPENSE,)
