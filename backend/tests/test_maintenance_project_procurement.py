"""项目采购链契约：稳定归属优先、名称不猜测、金额脱敏（PR4 + 审查修复）。"""

from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import permissions
from app.auth import hash_password
from app.db import SessionLocal
from app.etl import loader
from app.main import app
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch, SysUser
from tests import factories as f


_PASSWORD = "synthetic-password-123"


@pytest.fixture(autouse=True)
def _maintenance_beta_enabled(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "maintenance_beta_enabled", True)


def _seed_demand(db, raw_id: str, project_name: str) -> None:
    batch = SysImportBatch(
        filename="synthetic-procurement-maintenance.xlsx",
        file_type="maintenance",
        file_hash=f"synthetic-procurement-{raw_id}",
        status="processing",
    )
    db.add(batch)
    db.flush()
    orders = {
        raw_id: f.maintenance_head(
            raw_id,
            order_no=f"WBDD-{raw_id}",
            on=date(2026, 8, 1),
            project=project_name,
        )
    }
    lines = [
        f.maintenance_line(
            raw_id, f"LINE-{raw_id}", f"PN-PROC-{raw_id}",
            description="合成采购链备件",
        )
    ]
    loader.load(db, f.maintenance_result(orders, lines), batch.id, date(2026, 8, 1))
    batch.status = "success"
    db.commit()


def _seed_purchase(db, raw_id: str, demand_raw_id: str, price="100",
                   data_status="已生效") -> None:
    batch = SysImportBatch(
        filename="synthetic-procurement-purchase.xlsx",
        file_type="purchase",
        file_hash=f"synthetic-purchase-{raw_id}",
        status="processing",
    )
    db.add(batch)
    db.flush()
    orders = {
        raw_id: f.purchase_head(
            raw_id,
            order_no=raw_id,
            on=date(2026, 7, 30),
            data_status=data_status,
            linked_maintenance_order_no=demand_raw_id,
        )
    }
    lines = [
        f.purchase_line(
            raw_id, f"PO-LINE-{raw_id}", f"PN-PROC-{demand_raw_id}",
            qty="2", price=price,
        )
    ]
    loader.load(db, f.purchase_result(orders, lines), batch.id, date(2026, 7, 30))
    batch.status = "success"
    db.commit()


def _assign_user(db, *, user: SysUser, project_id: str) -> None:
    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id=str(uuid4()),
            project_id=project_id,
            responsibility_type="primary_manager",
            user_id=user.id,
            version=1,
            assigned_by="procurement_admin",
            assignment_reason="合成负责人指派",
        )
    )
    db.commit()


def _assign_source_order(db, *, source_order_id: str, project_id: str) -> None:
    db.add(
        MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid4()),
            source_order_id=source_order_id,
            project_id=project_id,
            is_active=True,
            version=1,
            created_by="procurement_admin",
        )
    )
    db.commit()


def _client(db, username: str, role: str = "purchaser",
            data_purchase_cost: bool = True) -> TestClient:
    graph = permissions.admin_account_defaults()
    graph.update({
        "page_maintenance": True,
        "page_maintenance_beta": True,
        "data_purchase_cost": data_purchase_cost,
    })
    db.add(
        SysUser(
            username=username,
            role=role,
            display_name=f"合成{username}",
            password_hash=hash_password(_PASSWORD),
            template_code=role,
            template_version=1,
            template_perms=graph,
        )
    )
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client, db.scalar(
        select(SysUser).where(SysUser.username == username)
    )


def _project(db, project_id: str, display_name: str) -> MaintenanceProject:
    project = MaintenanceProject(
        project_id=project_id,
        project_code=project_id.upper(),
        display_name=display_name,
        project_manager_id="来源负责人",
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.commit()
    return project


def _fetch(client, project_id: str):
    return client.get(
        f"/api/maintenance/projects/stable/{project_id}/purchases"
    )


def test_same_name_projects_never_see_each_others_purchase_orders(db):
    """两个同名项目：采购链只认稳定归属，同名不猜测，互不泄露。"""
    p1 = _project(db, "proc-dup-a", "同名维保项目")
    p2 = _project(db, "proc-dup-b", "同名维保项目")
    _seed_demand(db, "RAW-DUP-001", "同名维保项目")
    _seed_purchase(db, "PO-DUP-001", "RAW-DUP-001")
    _assign_source_order(db, source_order_id="RAW-DUP-001", project_id=p1.project_id)

    admin, _ = _client(db, "proc_admin_dup", role="admin")
    r1 = _fetch(admin, p1.project_id)
    assert r1.status_code == 200, r1.text
    assert len(r1.json()["purchases"]) == 1
    assert r1.json()["purchases"][0]["purchase_order_no"] == "PO-DUP-001"

    r2 = _fetch(admin, p2.project_id)
    assert r2.status_code == 200, r2.text
    assert r2.json()["purchases"] == []


def test_unassigned_demand_has_no_procurement_chain(db):
    p = _project(db, "proc-unassigned", "未归属项目")
    _seed_demand(db, "RAW-UNASG-001", "未归属项目")
    _seed_purchase(db, "PO-UNASG-001", "RAW-UNASG-001")
    # no source assignment

    admin, _ = _client(db, "proc_admin_unasg", role="admin")
    r = _fetch(admin, p.project_id)
    assert r.status_code == 200, r.text
    assert r.json()["purchases"] == []


def test_cross_project_access_returns_403(db):
    own = _project(db, "proc-own", "本人项目")
    other = _project(db, "proc-other", "他人项目")
    _seed_demand(db, "RAW-OWN-001", "本人项目")
    _seed_demand(db, "RAW-OTHER-001", "他人项目")
    _assign_source_order(db, source_order_id="RAW-OWN-001", project_id=own.project_id)
    _assign_source_order(db, source_order_id="RAW-OTHER-001", project_id=other.project_id)

    manager, manager_user = _client(db, "proc_manager")
    _assign_user(db, user=manager_user, project_id=own.project_id)
    ok = _fetch(manager, own.project_id)
    assert ok.status_code == 200, ok.text
    denied = _fetch(manager, other.project_id)
    assert denied.status_code == 403


def test_voided_purchase_orders_are_excluded(db):
    p = _project(db, "proc-voided", "作废过滤项目")
    _seed_demand(db, "RAW-VOID-001", "作废过滤项目")
    _seed_purchase(db, "PO-ACTIVE-001", "RAW-VOID-001", price="100", data_status="已生效")
    _seed_purchase(db, "PO-VOIDED-001", "RAW-VOID-001", price="200", data_status="已作废")
    _assign_source_order(db, source_order_id="RAW-VOID-001", project_id=p.project_id)

    admin, _ = _client(db, "proc_admin_void", role="admin")
    r = _fetch(admin, p.project_id)
    assert r.status_code == 200, r.text
    assert [po["purchase_order_no"] for po in r.json()["purchases"]] == ["PO-ACTIVE-001"]


def test_purchase_cost_permission_masks_unit_price(db):
    """无 data_purchase_cost 权限时 unit_price 置 null，订单号与数量保留。"""
    p_no = _project(db, "proc-mask-no", "脱敏项目A")
    p_yes = _project(db, "proc-mask-yes", "脱敏项目B")
    _seed_demand(db, "RAW-MASK-001", "脱敏项目A")
    _seed_demand(db, "RAW-MASK-002", "脱敏项目B")
    _seed_purchase(db, "PO-MASK-001", "RAW-MASK-001", price="88")
    _seed_purchase(db, "PO-MASK-002", "RAW-MASK-002", price="88")
    _assign_source_order(db, source_order_id="RAW-MASK-001", project_id=p_no.project_id)
    _assign_source_order(db, source_order_id="RAW-MASK-002", project_id=p_yes.project_id)

    no_cost, no_cost_user = _client(db, "proc_no_cost", data_purchase_cost=False)
    _assign_user(db, user=no_cost_user, project_id=p_no.project_id)
    r = _fetch(no_cost, p_no.project_id)
    assert r.status_code == 200, r.text
    assert r.json()["purchases"][0]["purchase_order_no"] == "PO-MASK-001"
    assert r.json()["purchases"][0]["lines"][0]["unit_price"] is None

    with_cost, with_cost_user = _client(db, "proc_with_cost", data_purchase_cost=True)
    _assign_user(db, user=with_cost_user, project_id=p_yes.project_id)
    r2 = _fetch(with_cost, p_yes.project_id)
    assert r2.status_code == 200, r2.text
    assert r2.json()["purchases"][0]["lines"][0]["unit_price"] == "88.00"


def test_admin_sees_chain_for_any_project(db):
    p = _project(db, "proc-admin", "管理员项目")
    _seed_demand(db, "RAW-ADM-001", "管理员项目")
    _seed_purchase(db, "PO-ADM-001", "RAW-ADM-001")
    _assign_source_order(db, source_order_id="RAW-ADM-001", project_id=p.project_id)

    admin, _ = _client(db, "proc_admin_all", role="admin")
    r = _fetch(admin, p.project_id)
    assert r.status_code == 200, r.text
    assert len(r.json()["purchases"]) == 1


def _fetch_params(client, project_id: str, **params):
    return client.get(
        f"/api/maintenance/projects/stable/{project_id}/purchases", params=params,
    )


def test_purchases_paging_total_and_source_order_scope(db):
    """#259：page 省略＝全量（旧协议）；分页 total 是收敛后的真实总数；
    source_order_id 只看挂在该需求单上的采购单，且仍只认稳定归属。"""
    p = _project(db, "proc-paging", "分页项目")
    _seed_demand(db, "RAW-PG-001", "分页项目")
    _seed_demand(db, "RAW-PG-002", "分页项目")
    for i in (1, 2, 3):
        _seed_purchase(db, f"PO-PG-00{i}", "RAW-PG-001")
    _seed_purchase(db, "PO-PG-OTHER", "RAW-PG-002")
    _assign_source_order(db, source_order_id="RAW-PG-001", project_id=p.project_id)
    _assign_source_order(db, source_order_id="RAW-PG-002", project_id=p.project_id)
    admin, _ = _client(db, "proc_admin_paging", role="admin")

    full = _fetch(admin, p.project_id).json()
    assert len(full["purchases"]) == 4
    assert full["total"] == 4
    assert full["page"] is None and full["page_size"] is None
    assert {po["demand_source_order_id"] for po in full["purchases"]} == {
        "RAW-PG-001", "RAW-PG-002",
    }

    page1 = _fetch_params(admin, p.project_id, page=1, page_size=3).json()
    assert len(page1["purchases"]) == 3
    assert (page1["total"], page1["page"], page1["page_size"]) == (4, 1, 3)
    page2 = _fetch_params(admin, p.project_id, page=2, page_size=3).json()
    assert len(page2["purchases"]) == 1 and page2["total"] == 4
    # 跨页不重不漏
    assert {po["purchase_order_no"] for po in page1["purchases"] + page2["purchases"]} == {
        "PO-PG-001", "PO-PG-002", "PO-PG-003", "PO-PG-OTHER",
    }
    assert _fetch_params(admin, p.project_id, page=1, page_size=201).status_code == 422

    scoped = _fetch_params(admin, p.project_id, source_order_id="RAW-PG-001").json()
    assert {po["purchase_order_no"] for po in scoped["purchases"]} == {
        "PO-PG-001", "PO-PG-002", "PO-PG-003",
    }
    assert scoped["total"] == 3
    scoped_page = _fetch_params(
        admin, p.project_id, source_order_id="RAW-PG-001", page=2, page_size=2,
    ).json()
    assert len(scoped_page["purchases"]) == 1 and scoped_page["total"] == 3
    # 不在本项目归属内的需求单：即使传了也是空（稳定归属优先，不按名猜）
    foreign = _fetch_params(admin, p.project_id, source_order_id="RAW-NOT-MINE").json()
    assert foreign["purchases"] == [] and foreign["total"] == 0


def test_masked_unit_price_is_distinguishable_from_missing_price(db):
    """#259：脱敏置空与源表本来没价都是 null——unit_price_masked 显式标注区分。"""
    from app.models.purchase import FPurchaseLine, FPurchaseOrder

    p = _project(db, "proc-mask-flag", "脱敏标注项目")
    _seed_demand(db, "RAW-MF-001", "脱敏标注项目")
    _seed_purchase(db, "PO-MF-PRICED", "RAW-MF-001", price="66")
    _seed_purchase(db, "PO-MF-NOPRICE", "RAW-MF-001", price="1")
    line = db.scalar(
        select(FPurchaseLine).join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
        .where(FPurchaseOrder.raw_order_id == "PO-MF-NOPRICE")
    )
    line.unit_price = None   # 源表就没价
    db.commit()
    _assign_source_order(db, source_order_id="RAW-MF-001", project_id=p.project_id)

    # 有成本权限的读者用管理员（同一项目只能有一位在任主负责人）
    with_cost, _ = _client(db, "proc_flag_admin", role="admin")
    body = _fetch(with_cost, p.project_id).json()
    by_no = {po["purchase_order_no"]: po for po in body["purchases"]}
    priced = by_no["PO-MF-PRICED"]["lines"][0]
    assert priced["unit_price"] == "66.00" and priced["unit_price_masked"] is False
    missing = by_no["PO-MF-NOPRICE"]["lines"][0]
    assert missing["unit_price"] is None and missing["unit_price_masked"] is False

    no_cost, no_cost_user = _client(db, "proc_flag_no_cost", data_purchase_cost=False)
    _assign_user(db, user=no_cost_user, project_id=p.project_id)
    body = _fetch(no_cost, p.project_id).json()
    assert len(body["purchases"]) == 2
    for po in body["purchases"]:
        for masked_line in po["lines"]:
            assert masked_line["unit_price"] is None
            assert masked_line["unit_price_masked"] is True
