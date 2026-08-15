"""项目工作簿 v3 导出测试（D1）：六 sheet + 隐藏技术 sheet + 颜色契约。"""

import io
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from app import auth
from app.api import maintenance_project_workbook_v3
from app.auth import hash_password
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder, FProjectExpense
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_front_stock as front_stock
from app.services import maintenance_project_workbook_v3 as workbook_v3


def _seed(db):
    project = MaintenanceProject(
        project_id="wb3-project-1",
        project_code="WB3测试项目",
        display_name="WB3维保项目",
        business_type="整体维保",
        cmo_name="廖晓娟",
        salesperson="李呈辉",
        no_return_default=False,
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add(project)
    db.flush()
    contract = MaintenanceProjectContract(
        project_contract_id="wb3-pc-1",
        project_id="wb3-project-1",
        contract_id="wb3-contract-1",
        contract_no="XSDD-20260731-0086",
        contract_amount=Decimal("39607.08"),
        amount_inc_tax=Decimal("44756.00"),
        contract_status="active",
        status_mapping_state="mapped",
        status_mapping_version="synthetic-v1",
        included_in_total=True,
        effective_from=date(2026, 6, 8),
        effective_to=date(2029, 12, 5),
        source="synthetic-test",
    )
    db.add(contract)
    db.flush()
    import_batch = SysImportBatch(
        filename="w.xlsx", file_type="maintenance", file_hash="h-wb3", status="success"
    )
    db.add(import_batch)
    db.flush()
    order = FMaintenanceOrder(
        raw_order_id="wb3-wbdd-raw-1",
        order_no="WBDD-20260702-0014",
        order_date=date(2026, 7, 2),
        demand_type="补库供货",
        business_type="整体维保",
        project_raw="WB3维保项目",
        project_std="WB3维保项目",
        warehouse="北京成品仓",
        data_status="已生效",
        linked_sales_order_no="XSDD-20260731-0086",
        import_batch_id=import_batch.id,
    )
    db.add(order)
    db.flush()
    part = DimPart(pn_std="02311AYV", description="8G 内存")
    db.add(part)
    db.flush()
    db.add(
        FMaintenanceLine(
            raw_line_id="wb3-line-1",
            order_id=order.id,
            line_no=1,
            part_id=part.id,
            pn_std="02311AYV",
            qty=Decimal("10"),
            import_batch_id=import_batch.id,
        )
    )
    db.add(
        MaintenanceSourceOrderAssignment(
            assignment_id="wb3-assign-1",
            source_order_id="wb3-wbdd-raw-1",
            project_id="wb3-project-1",
            is_active=True,
            created_by="合成归属人",
        )
    )
    db.add(
        FProjectExpense(
            raw_line_id="wb3-fpe-1",
            bxd_no="BXD-20260721-0019",
            line_no=1,
            data_status="已结束",
            expense_date=date(2026, 7, 27),
            person="罗汇康",
            expense_type="维保费用",
            fee_category="外援费用",
            reason="北京2026年6月外援费用",
            linked_sales_order_no="XSDD-20260731-0086",
            amount=Decimal("800"),
            amount_ex_tax=Decimal("707.96"),
            amount_inc_tax=Decimal("800"),
            tax_basis="inc",
            import_batch_id=import_batch.id,
        )
    )
    db.add(
        MaintenanceCollectionSnapshot(
            collection_id="wb3-snap-1",
            project_id="wb3-project-1",
            project_contract_id="wb3-pc-1",
            report_month=date(2026, 10, 1),
            cumulative_amount=Decimal("2986.57"),
            status="confirmed",
            receipt_reference="HKD-0001",
        )
    )
    issue = MaintenanceSiteIssue(
        issue_id="wb3-issue-1",
        project_id="wb3-project-1",
        issue_no="LY-20260710-0001",
        issue_date=date(2026, 7, 10),
        raw_status="已确认",
        status_mapping_state="mapped",
        normalized_status="confirmed",
        status_mapping_version="synthetic-map-v1",
        source="direct_api",
        version=1,
    )
    db.add(issue)
    db.flush()
    db.add(
        MaintenanceSiteIssueLine(
            issue_line_id="wb3-issue-line-1",
            issue_id="wb3-issue-1",
            line_no=1,
            part_id=part.id,
            pn="02311AYV",
            quantity=Decimal("2"),
            serial_number="S0M59S5M",
            algorithm_version="synthetic-algo-v1",
        )
    )
    front_stock.apply_movement(
        db,
        project_id="wb3-project-1",
        part_id=part.id,
        kind="shipment_in",
        source_type="f_maintenance_line",
        source_ref="WB3-SHIP-1",
        qty=Decimal("8"),
        warehouse_name="WB3测试项目",
        unit_cost_ex_tax=Decimal("100"),
        unit_cost_inc_tax=Decimal("113"),
        operated_by="合成测试员",
    )
    db.commit()


def test_build_workbook_sheets_and_data(db):
    _seed(db)
    data = workbook_v3.build_project_workbook(db, "wb3-project-1")
    workbook = load_workbook(io.BytesIO(data))
    assert workbook.sheetnames[:7] == [
        "00_使用说明", "01_项目基础信息", "02_概览数据", "03_备件订单",
        "04_报销订单", "05_项目经理回款单", "06_现场领用与返还",
    ]
    assert "98_字典" in workbook.sheetnames and "99_元数据" in workbook.sheetnames
    assert workbook["98_字典"].sheet_state == "hidden"
    assert workbook["99_元数据"].sheet_state == "hidden"

    ws01 = workbook["01_项目基础信息"]
    row = [c.value for c in ws01[2]]
    assert row[0] == "WB3测试项目"
    assert row[9] == "否"  # 项目级不返还默认
    assert row[11] == 1  # 前置库种类数
    assert row[12] == 8.0  # 前置库件数
    assert row[13] == 904.0  # 8 × 113

    ws02 = workbook["02_概览数据"]
    contract_rows = [r for r in ws02.iter_rows(min_row=3, values_only=True) if r[0]]
    assert contract_rows[0][0] == "XSDD-20260731-0086"
    assert contract_rows[0][1] == 44756.0

    ws03 = workbook["03_备件订单"]
    rows = list(ws03.iter_rows(values_only=True))
    assert rows[1][0] == "WBDD-20260702-0014"
    assert rows[1][6] == "02311AYV"
    assert rows[1][8] == 10.0
    assert rows[1][13] is None  # 未税单位成本：可回填列留空

    ws04 = workbook["04_报销订单"]
    rows = list(ws04.iter_rows(values_only=True))
    assert rows[1][0] == "BXD-20260721-0019"
    assert rows[1][8] == 800.0

    ws05 = workbook["05_项目经理回款单"]
    rows = list(ws05.iter_rows(values_only=True))
    assert rows[1][1] == "XSDD-20260731-0086"
    assert rows[1][2] == "2026-10"
    assert rows[1][3] == 2986.57

    ws06 = workbook["06_现场领用与返还"]
    rows = list(ws06.iter_rows(values_only=True))
    assert rows[1][0] == "LY-20260710-0001"
    assert rows[1][2] == "02311AYV"
    assert rows[1][4] == 2.0


def test_editable_columns_follow_header_fill_contract(db):
    _seed(db)
    data = workbook_v3.build_project_workbook(db, "wb3-project-1")
    workbook = load_workbook(io.BytesIO(data))
    editable_03 = workbook_v3.parse_editable_header_fills(workbook["03_备件订单"])
    assert editable_03 == ["未税单位成本", "变更原因"]
    editable_01 = workbook_v3.parse_editable_header_fills(workbook["01_项目基础信息"])
    assert editable_01 == []  # 全只读
    editable_06 = workbook_v3.parse_editable_header_fills(workbook["06_现场领用与返还"])
    assert editable_06 == ["是否应返还(行级)", "备注"]


def test_workbook_v3_api_export_and_404(db):
    _seed(db)
    db.add(
        SysUser(
            username="wb3_api_admin",
            role="admin",
            display_name="工作簿导出管理员",
            password_hash=hash_password("synthetic-password-123"),
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_project_workbook_v3.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "wb3_api_admin", "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    ok = client.get("/api/maintenance/projects/stable/wb3-project-1/workbook-v3.xlsx")
    assert ok.status_code == 200, ok.text
    assert ok.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    missing = client.get(
        "/api/maintenance/projects/stable/no-such-project/workbook-v3.xlsx"
    )
    assert missing.status_code == 404
