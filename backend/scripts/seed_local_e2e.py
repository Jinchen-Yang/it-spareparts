"""本地 E2E 种子数据：项目/合同/WBDD 单与明细/领用/报销/回款计划与实收/账号。"""
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.auth import hash_password
from app.models.dimensions import DimPart
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    FProjectExpense,
    MaintenanceManualCostOverride,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch, SysUser
from app import permissions as permissions_mod


def batch(db, name="seed.xlsx") -> int:
    b = SysImportBatch(filename=name, file_type="maintenance",
                       file_hash=uuid.uuid4().hex, status="success")
    db.add(b)
    db.flush()
    return b.id


def main() -> None:
    db = SessionLocal()
    tag = uuid.uuid4().hex[:8]

    project = MaintenanceProject(
        project_id=str(uuid.uuid4()), project_code=f"E2E-{tag}",
        display_name="本地测试项目A", lifecycle_status="ongoing",
        period_from=date(2026, 1, 1), period_to=date(2026, 12, 31),
    )
    other = MaintenanceProject(
        project_id=str(uuid.uuid4()), project_code=f"OTH-{tag}",
        display_name="隔壁项目B", lifecycle_status="ongoing",
    )
    db.add_all([project, other])

    parts = {}
    for pn in ("E2E-PN-0001", "E2E-PN-0002", "E2E-PN-0003",
               "AL15SEB120N", "06200288"):
        parts[pn] = DimPart(pn_std=pn, description=f"测试备件{pn}")
    db.add_all(parts.values())
    db.flush()

    db.add(MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()), project_id=project.project_id,
        contract_id="E2E-CONTRACT", contract_no=f"XSDD-E2E-{tag}",
        amount_inc_tax=Decimal("100000.00"), included_in_total=True,
        status_mapping_state="mapped", status_mapping_version="v1",
        effective_from=date(2026, 1, 1), source="ledger", version=1,
    ))
    db.flush()

    order = FMaintenanceOrder(
        raw_order_id=f"raw-order-{uuid.uuid4()}", order_no="WBDD-E2E-001",
        order_date=date(2026, 7, 1), linked_sales_order_no=f"XSDD-E2E-{tag}",
        project_raw="本地测试项目A", data_status="已生效",
        demand_type="报修供货", salesperson="销售甲",
        import_batch_id=batch(db),
    )
    db.add(order)
    db.flush()
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid.uuid4()), project_id=project.project_id,
        source_order_id=order.raw_order_id, is_active=True, created_by="seed",
    ))

    lines = []
    for i, (pn, qty) in enumerate([("E2E-PN-0001", 2), ("E2E-PN-0002", 5),
                                   ("E2E-PN-0003", 1)], start=1):
        line = FMaintenanceLine(
            raw_line_id=f"raw-line-{uuid.uuid4()}", order_id=order.id,
            line_no=i, part_id=parts[pn].id, pn_std=pn, pn_raw=pn,
            description=f"{pn} 描述", qty=Decimal(qty), return_qty=Decimal(0),
            cost_source="direct", cost_tax_basis="ex", confidence="high",
            import_batch_id=batch(db),
        )
        db.add(line)
        lines.append(line)
    db.add(MaintenanceManualCostOverride(
        line_id=lines[0].id, unit_cost_ex_tax=Decimal("88.50"),
        unit_cost_inc_tax=Decimal("100.01"), active=True, updated_by="seed",
    ))

    # 06 领用：一单两行
    issue = MaintenanceSiteIssue(
        issue_id=str(uuid.uuid4()), project_id=project.project_id,
        issue_no="CKD-E2E-001", issue_date=date(2026, 7, 15),
        raw_status="已生效", status_mapping_state="mapped",
        normalized_status="confirmed", status_mapping_version="seed",
        source="legacy", version=1, created_by="seed",
    )
    db.add(issue)
    db.flush()
    for i, pn in enumerate(("E2E-PN-0001", "E2E-PN-0002"), start=1):
        db.add(MaintenanceSiteIssueLine(
            issue_line_id=str(uuid.uuid4()), issue_id=issue.issue_id,
            line_no=i, part_id=parts[pn].id, pn=pn, quantity=Decimal(1),
            remark="", is_active=True,
            algorithm_version="seed-v1",
        ))

    # 04 报销两行
    expense_batch = batch(db, "expense.xlsx")
    for no, amt in (("BXD-E2E-001", Decimal("200.00")), ("BXD-E2E-002", Decimal("500.00"))):
        db.add(FProjectExpense(
            raw_line_id=f"{no}#1@{uuid.uuid4().hex[:8]}", bxd_no=no, line_no=1,
            expense_date=date(2026, 7, 20), person="张三",
            expense_type="维保费用", fee_category="交通费", reason="现场交通",
            linked_sales_order_no=f"XSDD-E2E-{tag}", amount=amt,
            tax_basis="ex", amount_ex_tax=amt,
            amount_inc_tax=(amt * Decimal("1.13")).quantize(Decimal("0.01")),
            data_status="已提交", remark="",
            import_batch_id=expense_batch,
        ))

    # 02 计划 + 05 实收
    contract = db.scalar(select(MaintenanceProjectContract).where(
        MaintenanceProjectContract.project_id == project.project_id))
    db.add(MaintenanceCollectionMilestone(
        milestone_id=str(uuid.uuid4()), project_id=project.project_id,
        project_contract_id=contract.project_contract_id,
        sequence=1, planned_date=date(2026, 9, 30), date_precision="day",
        planned_amount=Decimal("30000.00"), is_active=True, version=1,
        completeness_state="complete", follow_up_status="pending",
        source="project_master_v2",
    ))
    db.add(MaintenanceCollectionSnapshot(
        collection_id=str(uuid.uuid4()), project_id=project.project_id,
        project_contract_id=contract.project_contract_id,
        report_month=date(2026, 7, 1), cumulative_amount=Decimal("10000.00"),
        status="confirmed", receipt_reference="PJ-001",
    ))

    # 账号：负责人/销售/viewer（readonly 模板 + 页面权限，无上传动作键）
    base = permissions_mod.effective("readonly", None)
    for username, salesperson, extra in (
        ("e2e-manager", None, "manager"),
        ("e2e-sales", "销售甲", "sales"),
        ("e2e-viewer", None, "viewer"),
    ):
        user = SysUser(
            username=username, role="readonly", display_name=username,
            salesperson_name=salesperson,
            password_hash=hash_password("pw123456"), is_active=True,
            template_code="readonly", template_version=1, template_perms=base,
            perm_overrides={"page_maintenance": True, "data_profit": False},
            permissions=permissions_mod.effective_from_snapshot(
                base, {"page_maintenance": True, "data_profit": False}),
        )
        db.add(user)
        db.flush()
        if extra == "manager":
            db.add(MaintenanceProjectUserAssignment(
                assignment_id=str(uuid.uuid4()), project_id=project.project_id,
                responsibility_type="primary_manager", user_id=user.id,
                version=1, assigned_by="seed", assignment_reason="E2E",
            ))
        if extra == "viewer":
            db.add(MaintenanceProjectUserAssignment(
                assignment_id=str(uuid.uuid4()), project_id=project.project_id,
                responsibility_type="viewer", user_id=user.id,
                version=1, assigned_by="seed", assignment_reason="E2E",
            ))
    project.salesperson = "销售甲"
    db.commit()
    print("PROJECT_ID=" + project.project_id)
    print("OTHER_ID=" + other.project_id)
    print("XSDD=XSDD-E2E-" + tag)
    print("PART_GLUE=AL15SEB120N PART_ZERO=06200288")
    db.close()


if __name__ == "__main__":
    sys.exit(main())
