"""维保展示板测试共享装置（合成数据）。"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import permissions
from app.auth import hash_password
from app.etl import pipeline
from app.main import app
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_ckd_import import (
    MaintenanceCkdHeadRow,
    MaintenanceCkdImportBatch,
    MaintenanceCkdLineRow,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysUser
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook

PASSWORD = "synthetic-board-password-1"


def client_for(db, *, username: str, role: str = "readonly",
               overrides: dict | None = None) -> TestClient:
    base = permissions.effective(role, None)
    effective = permissions.effective_from_snapshot(base, overrides or {})
    db.add(SysUser(
        username=username, role=role, display_name=username,
        password_hash=hash_password(PASSWORD), is_active=True,
        template_code=role, template_version=1, template_perms=base,
        perm_overrides=overrides or {}, permissions=effective,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def boss_client(db, username="board-boss", *, with_cost: bool = True,
                with_profit: bool = True) -> TestClient:
    """老板：全范围；成本/利润维度可独立开关，用于无侧信道断言。

    注意 role 用 readonly 而非 boss —— boss/admin 在 _allowed_scope 中恒为全范围，
    但金额可见性由 data_purchase_cost / data_profit 决定；这里显式给
    page_maintenance_boss 以取得全范围，同时让两组数据权限可独立关断。
    """
    return client_for(db, username=username, role="readonly", overrides={
        "page_maintenance": True, "page_maintenance_boss": True,
        "data_purchase_cost": with_cost,
        "data_profit": with_profit,
    })


def manager_client(db, username="board-manager", *, with_cost=True,
                   with_profit=True) -> TestClient:
    """项目经理：本人范围（无 page_maintenance_boss）；金额权限可独立开关。"""
    return client_for(db, username=username, role="readonly", overrides={
        "page_maintenance": True,
        "data_purchase_cost": with_cost,
        "data_profit": with_profit,
    })


def purchaser_client(db, username="board-purchaser", *, with_cost=True) -> TestClient:
    """真实采购员：有成本无利润（data_purchase_cost=True, data_profit=False）。"""
    return client_for(db, username=username, role="purchaser", overrides={
        "page_maintenance": True,
        "data_purchase_cost": with_cost,
    })


def no_access_client(db, username="board-none") -> TestClient:
    return client_for(db, username=username, role="readonly", overrides={})


def make_project(db, code="合成项目A", lifecycle="ongoing") -> MaintenanceProject:
    period_from = date(2020, 1, 1) if lifecycle in {"ongoing", "ended"} else None
    period_to = (
        date(2099, 12, 31)
        if lifecycle == "ongoing"
        else date(2020, 12, 31) if lifecycle == "ended" else None
    )
    proj = MaintenanceProject(
        project_id=str(uuid.uuid4()), project_code=code, display_name=code,
        period_from=period_from, period_to=period_to,
        # 兼容快照也保留，测试数据同时满足新旧 schema 读者。
        lifecycle_status=lifecycle)
    db.add(proj)
    db.commit()
    return proj


def import_wbdd(db, tmp_path, *, project="合成项目A", orders=1, lines_per_order=1,
                headless=0):
    path = write_workbook(str(tmp_path / f"{uuid.uuid4().hex}.xlsx"), COLUMNS_91,
                          make_rows(orders=orders, lines_per_order=lines_per_order,
                                    headless=headless, project=project))
    pipeline.run_import(db, path, "w.xlsx", uploaded_by="tester", mode="upsert")
    db.commit()
    return db.execute(select(FMaintenanceOrder)).scalars().all()


def assign(db, order, project):
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid.uuid4()), source_order_id=order.raw_order_id,
        project_id=project.project_id, is_active=True, version=1,
        created_by="tester"))
    db.commit()


def add_ckd(db, *, wbdd_no, pn="PN-SYN-0011", qty="4"):
    batch = MaintenanceCkdImportBatch(
        batch_id=str(uuid.uuid4()), file_hash="h" * 64, filename="ckd.xlsx",
        idempotency_key=str(uuid.uuid4()), uploaded_by="tester",
        head_rows=1, line_rows=1, issue_rows=0, status="applied",
        applied_by="tester", applied_at=datetime.now(timezone.utc))
    db.add(batch)
    db.flush()
    head = MaintenanceCkdHeadRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, row_no=1,
        order_no="CKD-1", order_date=date(2026, 7, 20), category="维保供货",
        wbdd_no=wbdd_no, data_status_raw="已生效")
    db.add(head)
    db.flush()
    db.add(MaintenanceCkdLineRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, head_row_id=head.row_id,
        row_no=1, pn_raw=pn, out_qty=Decimal(qty)))
    db.commit()


def set_costs(db, *, source="direct", amount="100.00"):
    """给全部维保明细预置成本（模拟 recompute 结果），用于成本口径断言。"""
    from app.models.maintenance import FMaintenanceLine

    lines = db.execute(select(FMaintenanceLine)).scalars().all()
    for ln in lines:
        ln.cost_source = source
        # normalized 双税金额只有在 legacy 成本事实本身完整时才是已知成本；夹具
        # 模拟 recompute 成功结果，不能只填展示列而留下一个现实中不可能的半成品。
        ln.cost_amount = Decimal(amount)
        ln.cost_amount_inc_tax = Decimal(amount)
        ln.cost_tax_basis = "inc"
        ln.confidence = "high"
    db.commit()
    return lines
