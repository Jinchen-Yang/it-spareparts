"""维保台账工作簿解析与 apply 测试（B2）。"""

import io

import pytest
from datetime import date

from openpyxl import Workbook
from sqlalchemy import select

from app.models.maintenance_ledger import (
    MaintenanceLedgerContractRow,
    MaintenanceLedgerExpenseRow,
    MaintenanceLedgerImportBatch,
    MaintenanceLedgerPlanRow,
)
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAuditLog,
    MaintenanceProjectContract,
)
from app.models.sales import FSalesOrder
from app.services import maintenance_ledger as ledger


def _old_ledger_workbook_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "维保项目清单"
    ws.append(
        ["订单编号", "订单日期", "销售人员", "业务类型", "项目名称", "维保起始日期",
         "维保终止日期", "CMO", "项目经理", "订单金额", "已收尾款", "待收尾款", "验收材料",
         "验收材料是否完成及上传附件", "巡检时间", "巡检是否完成及上传附件",
         "回款时间1", "回款金额", "回款时间2", "回款金额", "回款时间3", "回款金额"]
    )
    ws.append(
        ["XSDD-20260731-0086", "2026-07-31", "李呈辉", "整体维保",
         "阿里专有云20260608-20291205", "2026-06-08", "2029-12-05", "廖晓娟", "任鑫明",
         44756, 0, 44756, "提供设备硬件维修记录报告；服务总结报告", "否", "/", "否",
         "2026年10月", 2986.57, "2027年1月", 2986.57, "2027年4月", 2986.57]
    )
    ws.append(
        ["XSDD-20260731-0040", "2026-07-31", "李呈辉", "备件维保",
         "正大天晴20260801-20270531因", "2026-08-01", "2027-05-31", "廖晓娟", "/",
         27400, 0, 27400, "备件发货记录", "", "/", "", "2027年2月", 6850]
    )
    cost = wb.create_sheet("项目成本")
    cost.append(
        ["费用单号", "报销人员", "报销类别", "支出事由", "维保销售订单", "项目名称",
         "销售订单", "销售人员", "费用分类", "报销金额", "备注"]
    )
    cost.append(
        ["BXD-20260425-0002", "董学晶", "维保费用", "2026年广西国税2月份第二次巡检和4月",
         "XSDD-20251028-0016", "国税总局存储20251102-20281101北京ST存储设备维保项目",
         "XSDD-20251028-0016", "余俊", "差旅费", 1068.5, ""]
    )
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _new_ledger_workbook_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "01_项目与合同"
    ws.append(
        ["订单编号", "订单日期", "销售人员", "业务类型", "项目名称", "维保起始日期",
         "维保终止日期", "CMO", "项目经理", "订单金额", "已收尾款", "待收尾款", "验收材料",
         "验收材料是否完成及上传附件", "验收附件", "巡检时间", "巡检是否完成及上传附件"]
    )
    ws.append(
        ["XSDD-20260731-0086", "2026-07-31", "李呈辉", "整体维保",
         "阿里专有云20260608-20291205", "2026-06-08", "2029-12-05", "廖晓娟", "任鑫明",
         44756, 0, 44756, "服务总结报告", "否", "", "2026-10", "否"]
    )
    plan = wb.create_sheet("02_回款计划")
    plan.append(["订单编号", "计划期次", "计划回款时间", "计划回款金额"])
    plan.append(["XSDD-20260731-0086", 1, "2026-10", 2986.57])
    plan.append(["XSDD-20260731-0086", 2, "2027-01", 2986.57])
    cost = wb.create_sheet("03_项目成本")
    cost.append(
        ["费用单号", "报销人员", "报销类别", "支出事由", "维保销售订单", "项目名称",
         "销售订单", "销售人员", "费用分类", "报销金额", "备注"]
    )
    cost.append(
        ["BXD-20260425-0002", "董学晶", "维保费用", "巡检",
         "XSDD-20251028-0016", "国税总局存储20251102-20281101北京ST存储设备维保项目",
         "XSDD-20251028-0016", "余俊", "差旅费", 1068.5, ""]
    )
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_parse_expense_sheet_accepts_detail_prefix():
    wb = Workbook()
    ws = wb.active
    ws.title = "维保项目清单"
    ws.append(["订单编号"])
    ws.append(["XSDD-20260731-0086"])
    cost = wb.create_sheet("项目成本")
    cost.append(
        ["费用单号", "报销人员", "报销类别", "支出事由", "维保销售订单", "项目名称",
         "销售订单", "销售人员", "报销明细.费用分类", "报销明细.报销金额", "备注"]
    )
    cost.append(
        ["BXD-20260425-0002", "董学晶", "维保费用", "巡检",
         "XSDD-20251028-0016", "某项目", "XSDD-20251028-0016", "余俊", "差旅费", 1068.5, ""]
    )
    buffer = io.BytesIO()
    wb.save(buffer)
    parsed = ledger.parse_ledger_workbook(buffer.getvalue(), "台账.xlsx")
    assert parsed["expense_rows"][0].values["费用分类"] == "差旅费"
    assert parsed["expense_rows"][0].values["报销金额"] == "1068.5"


def test_parse_old_ledger_structure():
    parsed = ledger.parse_ledger_workbook(
        _old_ledger_workbook_bytes(), "维保台账.xlsx"
    )
    assert parsed["source_kind"] == "project_manager_xls_v1"
    assert len(parsed["contract_rows"]) == 2
    assert len(parsed["plan_rows"]) == 4  # 3 + 1 横向回款对展开
    assert len(parsed["expense_rows"]) == 1
    # 第一个合同行的回款计划：期次 1/2/3 金额一致
    first_plans = [p for p in parsed["plan_rows"] if p.order_no_raw == "XSDD-20260731-0086"]
    assert [p.sequence for p in first_plans] == [1, 2, 3]
    assert [str(p.amount_raw) for p in first_plans] == ["2986.57"] * 3


def test_parse_new_template_structure():
    parsed = ledger.parse_ledger_workbook(
        _new_ledger_workbook_bytes(), "维保台账工作簿模板_v1.xlsx"
    )
    assert parsed["source_kind"] == "ledger_template_v1"
    assert len(parsed["contract_rows"]) == 1
    assert len(parsed["plan_rows"]) == 2
    assert len(parsed["expense_rows"]) == 1


def test_parse_rejects_unknown_structure():
    wb = Workbook()
    buffer = io.BytesIO()
    wb.save(buffer)
    try:
        ledger.parse_ledger_workbook(buffer.getvalue(), "x.xlsx")
        raise AssertionError("应抛出 LedgerParseError")
    except ledger.LedgerParseError:
        pass


def test_store_preview_then_apply(db):
    parsed = ledger.parse_ledger_workbook(_old_ledger_workbook_bytes(), "维保台账.xlsx")
    batch_id = ledger.store_preview(db, parsed, "合成管理员", idempotency_key="ledger-test-key-0001")

    batch = db.get(MaintenanceLedgerImportBatch, batch_id)
    assert batch.status == "pending"
    assert batch.contract_rows == 2
    assert batch.plan_rows == 4
    assert batch.expense_rows == 1

    contract_rows = db.execute(
        select(MaintenanceLedgerContractRow).where(
            MaintenanceLedgerContractRow.batch_id == batch_id
        )
    ).scalars().all()
    row = next(r for r in contract_rows if r.order_no == "XSDD-20260731-0086")
    assert row.project_period_from == date(2026, 6, 8)
    assert row.project_period_to == date(2029, 12, 5)
    assert float(row.amount_inc_tax) == 44756.0

    plan_rows = db.execute(
        select(MaintenanceLedgerPlanRow).where(
            MaintenanceLedgerPlanRow.batch_id == batch_id
        )
    ).scalars().all()
    month_plan = next(p for p in plan_rows if p.sequence == 1)
    assert month_plan.planned_date == date(2026, 10, 1)
    assert month_plan.date_precision == "month"

    expense_rows = db.execute(
        select(MaintenanceLedgerExpenseRow).where(
            MaintenanceLedgerExpenseRow.batch_id == batch_id
        )
    ).scalars().all()
    assert expense_rows[0].bxd_no == "BXD-20260425-0002"
    assert float(expense_rows[0].amount) == 1068.5


def test_apply_syncs_canonical_tables(db):
    from app.models.system import SysImportBatch

    import_batch = SysImportBatch(
        filename="sales.xlsx", file_type="sales", file_hash="h", status="success"
    )
    db.add(import_batch)
    db.flush()
    db.add(
        FSalesOrder(
            raw_order_id="r1",
            order_no="XSDD-20260731-0086",
            order_date=date(2026, 7, 31),
            salesperson="李呈辉",
            business_type="整体维保",
            warehouse="北京成品仓",
            amount_ex_tax=39607.08,
            tax_rate=0.13,
            data_status="已生效",
            import_batch_id=import_batch.id,
        )
    )
    db.commit()
    parsed = ledger.parse_ledger_workbook(_old_ledger_workbook_bytes(), "维保台账.xlsx")
    batch_id = ledger.store_preview(db, parsed, "合成管理员", idempotency_key="ledger-test-key-0001")
    summary = ledger.apply_batch(db, batch_id, "合成管理员")

    assert summary["projects_created"] == 2
    assert summary["contracts_created"] == 2
    assert summary["milestones_created"] == 4
    assert summary["skipped_rows"] == 0

    project = db.execute(
        select(MaintenanceProject).where(
            MaintenanceProject.project_code == "阿里专有云20260608-20291205"
        )
    ).scalar_one()
    assert project.lifecycle_status == "ongoing"
    assert project.cmo_name == "廖晓娟"
    assert project.business_type == "整体维保"

    contract = db.execute(
        select(MaintenanceProjectContract).where(
            MaintenanceProjectContract.project_id == project.project_id
        )
    ).scalar_one()
    assert float(contract.amount_inc_tax) == 44756.0
    # 未税金额来自销售单对账：44756 / 1.13 = 39607.08
    assert float(contract.contract_amount) == 39607.08
    assert contract.included_in_total is True
    assert contract.source == "project_manager_xls_v1"

    milestones = db.execute(
        select(MaintenanceCollectionMilestone).where(
            MaintenanceCollectionMilestone.project_contract_id
            == contract.project_contract_id
        )
    ).scalars().all()
    assert len(milestones) == 3
    assert milestones[0].source == "project_manager_xls_v1"

    batch = db.get(MaintenanceLedgerImportBatch, batch_id)
    assert batch.status == "applied"
    assert batch.applied_by == "合成管理员"

    audit = db.execute(
        select(MaintenanceProjectAuditLog).where(
            MaintenanceProjectAuditLog.project_id == project.project_id
        )
    ).scalars().all()
    assert any(a.action == "ledger_create" for a in audit)


def test_apply_idempotent_and_version_bump(db):
    parsed = ledger.parse_ledger_workbook(_old_ledger_workbook_bytes(), "维保台账.xlsx")
    batch_id = ledger.store_preview(db, parsed, "合成管理员", idempotency_key="ledger-test-key-0001")
    first = ledger.apply_batch(db, batch_id, "合成管理员")
    assert first["contracts_created"] == 2

    # 同一文件再次导入：新批次，同值不重复写（无 updated/created 增量）
    parsed2 = ledger.parse_ledger_workbook(_old_ledger_workbook_bytes(), "维保台账.xlsx")
    batch_id2 = ledger.store_preview(db, parsed2, "合成管理员", idempotency_key="ledger-test-key-0002")
    second = ledger.apply_batch(db, batch_id2, "合成管理员")
    assert second["contracts_created"] == 0
    assert second["contracts_updated"] == 0
    assert second["projects_created"] == 0

    # 金额变化 → 合同版本 +1（重建一个金额不同的工作簿）
    wb2 = Workbook()
    ws = wb2.active
    ws.title = "维保项目清单"
    ws.append(
        ["订单编号", "订单日期", "销售人员", "业务类型", "项目名称", "维保起始日期",
         "维保终止日期", "CMO", "项目经理", "订单金额", "已收尾款", "待收尾款", "验收材料",
         "验收材料是否完成及上传附件", "巡检时间", "巡检是否完成及上传附件",
         "回款时间1", "回款金额"]
    )
    ws.append(
        ["XSDD-20260731-0086", "2026-07-31", "李呈辉", "整体维保",
         "阿里专有云20260608-20291205", "2026-06-08", "2029-12-05", "廖晓娟", "任鑫明",
         50000, 0, 50000, "", "", "", "", "2026-10", 2986.57]
    )
    buffer = io.BytesIO()
    wb2.save(buffer)
    parsed3 = ledger.parse_ledger_workbook(buffer.getvalue(), "维保台账.xlsx")
    batch_id3 = ledger.store_preview(db, parsed3, "合成管理员", idempotency_key="ledger-test-key-0003")
    third = ledger.apply_batch(db, batch_id3, "合成管理员")
    assert third["contracts_updated"] == 1

    contract = db.execute(
        select(MaintenanceProjectContract).where(
            MaintenanceProjectContract.contract_no == "XSDD-20260731-0086"
        )
    ).scalar_one()
    assert float(contract.amount_inc_tax) == 50000.0
    assert contract.version == 2


def test_apply_rejects_duplicate_apply(db):
    parsed = ledger.parse_ledger_workbook(_old_ledger_workbook_bytes(), "维保台账.xlsx")
    batch_id = ledger.store_preview(db, parsed, "合成管理员", idempotency_key="ledger-test-key-0001")
    ledger.apply_batch(db, batch_id, "合成管理员")
    try:
        ledger.apply_batch(db, batch_id, "合成管理员")
        raise AssertionError("重复 apply 应抛出 LedgerBatchError")
    except ledger.LedgerBatchError:
        pass


def test_apply_skips_missing_period(db):
    wb = Workbook()
    ws = wb.active
    ws.title = "维保项目清单"
    ws.append(
        ["订单编号", "订单日期", "销售人员", "业务类型", "项目名称", "维保起始日期",
         "维保终止日期", "CMO", "项目经理", "订单金额", "已收尾款", "待收尾款", "验收材料",
         "验收材料是否完成及上传附件", "巡检时间", "巡检是否完成及上传附件"]
    )
    ws.append(
        ["XSDD-20260731-0099", "", "李呈辉", "整体维保",
         "无期限项目", "", "", "", "", 1000, 0, 1000, "", "", "", ""]
    )
    buffer = io.BytesIO()
    wb.save(buffer)
    parsed = ledger.parse_ledger_workbook(buffer.getvalue(), "维保台账.xlsx")
    batch_id = ledger.store_preview(db, parsed, "合成管理员", idempotency_key="ledger-test-key-0001")
    # 缺期限行 → 关键异常 → 整批失败关闭：project/contract/milestone 零写入
    with pytest.raises(ledger.LedgerBatchError):
        ledger.apply_batch(db, batch_id, "合成管理员")
    batch = db.get(MaintenanceLedgerImportBatch, batch_id)
    assert batch.status == "failed"
    assert db.execute(select(MaintenanceProject)).scalars().all() == []
