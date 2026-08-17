"""审核修复回归测试：downgrade 守卫 / 上传安全 / CKD 表头变体 / 前置库 API 门。"""

import io
import zipfile
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import auth
from app.api import maintenance_front_stock, maintenance_project_operations
from app.auth import hash_password
from app.db import engine
from app.models.maintenance_front_stock import MaintenanceFrontStock
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectUserAssignment,
)
from app.models.system import SysUser
from app.services import import_safety, maintenance_ckd_import as ckd
from app.services import maintenance_front_stock as front_stock
from tests.test_maintenance_ckd_import import _ckd_workbook_bytes, _HEAD_MAINT


# ---------------------------------------------------------------- 上传安全
def test_zip_bomb_rejected():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        # 高压缩比成员（全零 10MB）
        archive.writestr("xl/bomb.bin", b"\x00" * (10 * 1024 * 1024))
    with pytest.raises(import_safety.UploadSafetyError):
        import_safety.validate_xlsx_zip(data.getvalue(), max_bytes=64 * 1024 * 1024)


def test_zip_member_limit_and_path_traversal():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("../evil.xml", "x")
    with pytest.raises(import_safety.UploadSafetyError):
        import_safety.validate_xlsx_zip(data.getvalue(), max_bytes=1024 * 1024)


# ---------------------------------------------------------------- CKD 表头变体
def test_ckd_header_variants_and_duplicates():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["F0000001"])
    ws.append(
        ["出库单号(必填)", "出库日期(必填)", "出库类别(必填)", "出库备件/整机(必填)",
         "出库仓库(必填)", "仓储中心", "维保需求单(备件)(必填)", "维保需求单",
         "销售订单(备件)(必填)", "销售订单(必填)", "销售人员", "项目经理", "维保需求人",
         "备注", "数据状态",
         "备件明细.数据ID(不可修改)", "备件明细.序号", "备件明细.数据标题",
         "备件明细.产品名称", "备件明细.备件自贴码", "备件明细.备件PN(必填)",
         "备件明细.备件SN号(必填)", "备件明细.备件描述", "备件明细.所在仓库",
         "备件明细.所在库位", "备件明细.产品大类", "备件明细.产品小类", "备件明细.品牌",
         "备件明细.单位", "备件明细.出库数量", "备件明细.成本单价(必填)",
         "备件明细.成本金额", "备件明细.备件测试合格(必填)"]
    )
    ws.append(
        ["CKD-20260806-0014", "2026-08-06", "维保供货", "备件", "北京成品仓", "北京仓",
         "WBDD-20260702-0014", "", "", "XSDD-20250731-0035", "尤玉玲", "李冰冰", "张工",
         "", "已生效",
         "LID-1", "1", "", "内存", "B1", "02311AYV", "SN1", "", "北京成品仓", "BJCP-AA",
         "内存", "", "品牌", "个", "2", "100", "200", "是"]
    )
    buffer = io.BytesIO()
    wb.save(buffer)
    parsed = ckd.parse_ckd_workbook(buffer.getvalue(), "发货单.xlsx")
    assert parsed["heads"][0].values["出库类别"] == "维保供货"
    assert parsed["heads"][0].lines[0].values["备件明细.备件PN"] == "02311AYV"

    # 重复列名 → 拒绝解析
    wb2 = Workbook()
    ws2 = wb2.active
    ws2.title = "Sheet1"
    ws2.append(["F0000001"])
    ws2.append(["出库单号", "出库单号", "出库日期"])
    buffer2 = io.BytesIO()
    wb2.save(buffer2)
    with pytest.raises(ckd.CkdParseError):
        ckd.parse_ckd_workbook(buffer2.getvalue(), "x.xlsx")


# ---------------------------------------------------------------- 前置库 API 门
def _project_and_users(db):
    project = MaintenanceProject(
        project_id="fs-api-project",
        project_code="FSAPI项目",
        display_name="FSAPI项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add(project)
    db.flush()
    admin = SysUser(
        username="fs_admin",
        role="admin",
        display_name="管理员",
        password_hash=hash_password("synthetic-password-123"),
    )
    owner = SysUser(
        username="fs_owner",
        role="sales",
        display_name="项目负责人",
        password_hash=hash_password("synthetic-password-123"),
        permissions={"page_maintenance": True, "data_purchase_cost": False},
    )
    stranger = SysUser(
        username="fs_stranger",
        role="sales",
        display_name="无关人员",
        password_hash=hash_password("synthetic-password-123"),
        permissions={"page_maintenance": True},
    )
    db.add_all([admin, owner, stranger])
    db.flush()
    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id="fs-api-assign-1",
            project_id="fs-api-project",
            responsibility_type="primary_manager",
            user_id=owner.id,
            assigned_by="合成",
            assignment_reason="测试",
        )
    )
    db.commit()
    return {"project_id": project.project_id, "owner": owner, "stranger": stranger}


def _client(db, username: str) -> TestClient:
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_project_operations.router, prefix="/api")
    app.include_router(maintenance_project_operations.site_issue_router, prefix="/api")
    app.include_router(maintenance_front_stock.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def test_front_stock_api_denies_other_project(db):
    seed = _project_and_users(db)
    client = _client(db, "fs_stranger")
    response = client.get(f"/api/maintenance/projects/stable/{seed['project_id']}/front-stock")
    assert response.status_code == 403


def test_front_stock_api_owner_can_read_without_cost(db):
    seed = _project_and_users(db)
    # owner 无 data_purchase_cost → 成本字段脱敏，数量可见
    client = _client(db, "fs_owner")
    response = client.get(f"/api/maintenance/projects/stable/{seed['project_id']}/front-stock")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cost_visible"] is False
    assert body["total_value_ex_tax"] is None
    assert body["value_completeness"] == "not_visible"


def test_front_stock_api_admin_sees_cost_completeness(db):
    seed = _project_and_users(db)
    client = _client(db, "fs_admin")
    response = client.get(f"/api/maintenance/projects/stable/{seed['project_id']}/front-stock")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cost_visible"] is True
    assert body["total_value_ex_tax"] == 0.0  # 无库存行：空集视为完整
    assert body["value_completeness"] == "complete"


# ---------------------------------------------------------------- downgrade 守卫
@pytest.mark.parametrize(
    "revision,parent,setup_sql",
    [
        # 目标语义与冻结基线测试一致：downgrade 到父版本触发本 revision 的守卫
        (
            "b1e3f7d9c2a5",
            "e7b3d9f2c1a4",
            [
                "INSERT INTO maintenance_project (project_id, project_code,"
                " display_name, lifecycle_status, is_active)"
                " VALUES ('g1','G1','G1','ongoing', true)"
                " ON CONFLICT (project_id) DO NOTHING",
                "INSERT INTO dim_part (id, pn_std) VALUES (1, 'G-PN-1')"
                " ON CONFLICT (id) DO NOTHING",
                "INSERT INTO maintenance_front_stock (stock_id, project_id,"
                " part_id, warehouse_name, qty) VALUES ('g1','g1',1,'w',1)"
                " ON CONFLICT (stock_id) DO NOTHING",
            ],
        ),
        (
            "e7b3d9f2c1a4",
            "c8e2a4f6b1d3",
            [
                "INSERT INTO maintenance_ledger_import_batch (batch_id, file_hash,"
                " filename, idempotency_key, source_kind, uploaded_by, status)"
                " VALUES ('g1','h','f.xlsx','k','project_manager_xls_v1','t','pending')",
            ],
        ),
        (
            "c3b5d9e1f7a2",
            "b1e3f7d9c2a5",
            [
                "INSERT INTO maintenance_project (project_id, project_code,"
                " display_name, lifecycle_status, is_active)"
                " VALUES ('g1','G1','G1','ongoing', true)"
                " ON CONFLICT (project_id) DO NOTHING",
                "UPDATE maintenance_project SET no_return_default = true"
                " WHERE project_id = 'g1'",
            ],
        ),
        (
            "d1e3f5a7c2b9",
            "c3b5d9e1f7a2",
            [
                "INSERT INTO maintenance_ckd_import_batch (batch_id, file_hash,"
                " filename, idempotency_key, uploaded_by, status)"
                " VALUES ('g1','h','f.xlsx','k','t','pending')",
            ],
        ),
        (
            "e9f2d4b7a1c6",
            "d1e3f5a7c2b9",
            [
                "INSERT INTO maintenance_doc_import_batch (batch_id, doc_type,"
                " file_hash, filename, idempotency_key, uploaded_by, status)"
                " VALUES ('g1','return_order','h','f.xlsx','k','t','pending')",
            ],
        ),
    ],
)
def test_downgrade_guard_blocks_when_facts_exist(db, revision, parent, setup_sql):
    """存在新事实时 downgrade 失败关闭；版本与事实不变。"""
    import os

    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    from app.db import engine as _engine

    db.close()
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    # 逐案铺最小事实；守卫在 DDL 前触发。
    with _engine.begin() as conn:
        for statement in setup_sql:
            conn.execute(text(statement))
    try:
        from sqlalchemy.exc import DBAPIError

        with pytest.raises((DBAPIError, RuntimeError)):
            alembic_command.downgrade(cfg, parent)
    finally:
        alembic_command.upgrade(cfg, "head")
        # 清理本用例事实，避免污染后续文件的迁移守卫（顺序敏感基线）
        with _engine.begin() as conn:
            for statement in (
                "DELETE FROM maintenance_doc_line_row WHERE batch_id = 'g1'",
                "DELETE FROM maintenance_doc_head_row WHERE batch_id = 'g1'",
                "DELETE FROM maintenance_doc_import_batch WHERE batch_id = 'g1'",
                "DELETE FROM maintenance_ckd_line_row WHERE batch_id = 'g1'",
                "DELETE FROM maintenance_ckd_head_row WHERE batch_id = 'g1'",
                "DELETE FROM maintenance_ckd_import_batch WHERE batch_id = 'g1'",
                "DELETE FROM maintenance_ledger_expense_row WHERE batch_id = 'g1'",
                "DELETE FROM maintenance_ledger_plan_row WHERE batch_id = 'g1'",
                "DELETE FROM maintenance_ledger_contract_row WHERE batch_id = 'g1'",
                "DELETE FROM maintenance_ledger_import_batch WHERE batch_id = 'g1'",
                "DELETE FROM maintenance_front_stock_ledger WHERE project_id = 'g1'",
                "DELETE FROM maintenance_front_stock WHERE project_id = 'g1'",
                "DELETE FROM maintenance_collection_milestone WHERE project_id = 'g1'",
                "DELETE FROM maintenance_service_period WHERE project_id = 'g1'",
                "DELETE FROM maintenance_project WHERE project_id = 'g1'",
                "DELETE FROM dim_part WHERE id = 1",
            ):
                conn.execute(text(statement))
    with _engine.connect() as conn:
        version = conn.scalar(text("SELECT version_num FROM alembic_version"))
    # 守卫触发 → 版本必须仍停在原 head（本 revision 未被降级）
    assert version != parent
