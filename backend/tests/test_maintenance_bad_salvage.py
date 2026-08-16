"""F5 坏件变卖登记与贡献毛利测试。"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.api import maintenance_bad_salvage
from app.auth import hash_password
from app.models.dimensions import DimPart
from app.models.maintenance_bad_salvage import MaintenanceBadSalvage
from app.models.maintenance_project import MaintenanceProject
from app.models.system import SysUser
from app.services import maintenance_bad_salvage as salvage


def _salvage_client(db, *, username: str, permissions: dict | None = None) -> TestClient:
    db.add(
        SysUser(
            username=username,
            role="admin",
            display_name="合成变卖登记操作人",
            password_hash=hash_password("synthetic-password-123"),
            permissions=permissions,
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_bad_salvage.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


@pytest.fixture()
def salvage_project(db):
    project = MaintenanceProject(
        project_id="salvage-project-1",
        project_code="变卖登记测试项目",
        display_name="变卖登记测试项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add(project)
    db.flush()
    part = DimPart(pn_std="SV-A-001", description="变卖测试件")
    db.add(part)
    db.commit()
    return {"project_id": project.project_id, "part_id": part.id}


def _register(db, *, part_id, pn="SV-A-001", qty="2", revenue="500.00",
              salvage_date=date(2026, 8, 12), key="salvage-key-1", project_id="salvage-project-1"):
    return salvage.register_salvage(
        db,
        project_id=project_id,
        part_id=part_id,
        pn=pn,
        qty=Decimal(qty),
        revenue=Decimal(revenue),
        salvage_date=salvage_date,
        buyer_note="合成回收商",
        reason="项目结束坏件集中变卖",
        idempotency_key=key,
        operated_by="合成变卖登记人",
    )


def test_register_and_margin_unknown_cost_is_null(db, salvage_project):
    payload = _register(db, part_id=salvage_project["part_id"])
    db.commit()
    assert payload["salvage_id"]
    assert payload["margin"] is None  # 无领用成本证据 → 缺成本不按 0
    listing = salvage.list_salvage(db, "salvage-project-1")
    assert listing["active_count"] == 1
    assert listing["total_revenue"] == 500.0
    assert listing["total_margin"] is None
    assert listing["margin_completeness"] == "incomplete"
    assert listing["rows"][0]["pn"] == "SV-A-001"
    assert listing["rows"][0]["qty"] == 2.0


def test_register_idempotent_replay_and_conflict(db, salvage_project):
    first = _register(db, part_id=salvage_project["part_id"])
    db.commit()
    replay = _register(db, part_id=salvage_project["part_id"])
    assert first["salvage_id"] == replay["salvage_id"]
    # 同键不同内容 → 失败关闭
    with pytest.raises(salvage.SalvageConflict):
        _register(db, part_id=salvage_project["part_id"], qty="9")
    db.rollback()
    assert len(
        db.execute(select(MaintenanceBadSalvage)).scalars().all()
    ) == 1


def test_void_salvage_soft_deletes_with_version(db, salvage_project):
    payload = _register(db, part_id=salvage_project["part_id"])
    db.commit()
    voided = salvage.void_salvage(
        db,
        salvage_id=payload["salvage_id"],
        operated_by="合成作废人",
        version=payload["version"],
    )
    db.commit()
    assert voided["is_active"] is False
    assert voided["version"] == 2
    listing = salvage.list_salvage(db, "salvage-project-1")
    assert listing["active_count"] == 0
    assert listing["total_revenue"] == 0.0
    with pytest.raises(salvage.SalvageConflict):
        salvage.void_salvage(
            db,
            salvage_id=payload["salvage_id"],
            operated_by="合成作废人",
            version=payload["version"],  # 旧版本 → 乐观锁冲突
        )


def test_salvage_api_register_requires_scope_and_permission(db, salvage_project):
    client = _salvage_client(db, username="salvage_api_admin")
    ok = client.post(
        "/api/maintenance/projects/stable/salvage-project-1/salvages",
        json={
            "part_id": salvage_project["part_id"],
            "pn": "SV-A-001",
            "qty": "2",
            "revenue": "500.00",
            "salvage_date": "2026-08-12",
            "buyer_note": "合成回收商",
            "reason": "项目结束坏件集中变卖",
            "idempotency_key": "salvage-api-key-1",
        },
    )
    assert ok.status_code == 201, ok.text
    listing = client.get(
        "/api/maintenance/projects/stable/salvage-project-1/salvages"
    )
    assert listing.status_code == 200
    assert listing.json()["active_count"] == 1

    voided = client.post(
        f"/api/maintenance/salvages/{ok.json()['salvage_id']}/void",
        json={
            "project_id": "salvage-project-1",
            "version": ok.json()["version"],
            "reason": "录入错误作废",
        },
    )
    assert voided.status_code == 200, voided.text
    assert voided.json()["is_active"] is False

    missing = client.post(
        "/api/maintenance/projects/stable/no-such-project/salvages",
        json={
            "part_id": salvage_project["part_id"],
            "pn": "SV-A-001",
            "qty": "1",
            "revenue": "10.00",
            "salvage_date": "2026-08-12",
            "idempotency_key": "salvage-api-key-2",
        },
    )
    assert missing.status_code == 404


def test_salvage_deducts_front_stock_and_void_reverses(db, salvage_project):
    """变卖同事务 salvage_out 减账本；作废 salvage_in 回冲（round-5 Blocker 4）。"""
    from app.services import maintenance_front_stock as front_stock

    front_stock.apply_movement(
        db,
        project_id="salvage-project-1",
        part_id=salvage_project["part_id"],
        kind="shipment_in",
        source_type="f_maintenance_line",
        source_ref="SV-SHIP-1",
        qty=Decimal("5"),
        warehouse_name="",
        operated_by="合成测试员",
    )
    db.commit()
    payload = _register(db, part_id=salvage_project["part_id"], qty="2")
    db.commit()
    assert payload["stock_deducted"] is True
    balance = front_stock.balance_rows(db, "salvage-project-1")
    assert balance[0]["qty"] == 3.0

    voided = salvage.void_salvage(
        db,
        salvage_id=payload["salvage_id"],
        operated_by="合成作废人",
        version=payload["version"],
    )
    db.commit()
    assert voided["is_active"] is False
    balance = front_stock.balance_rows(db, "salvage-project-1")
    assert balance[0]["qty"] == 5.0
    kinds = [e["kind"] for e in front_stock.ledger_entries(db, "salvage-project-1")]
    assert "salvage_out" in kinds
    assert "salvage_in" in kinds


def test_salvage_rejects_partial_stock_and_pn_mismatch(db, salvage_project):
    from app.services import maintenance_front_stock as front_stock

    # 部分在库：无法判定 → 失败关闭
    front_stock.apply_movement(
        db,
        project_id="salvage-project-1",
        part_id=salvage_project["part_id"],
        kind="shipment_in",
        source_type="f_maintenance_line",
        source_ref="SV-SHIP-PART",
        qty=Decimal("1"),
        operated_by="合成测试员",
    )
    db.commit()
    with pytest.raises(salvage.SalvageError, match="无法判定"):
        _register(db, part_id=salvage_project["part_id"], qty="2", key="salvage-part-1")
    db.rollback()

    # part_id 与 pn 不一致 → 拒绝
    other = DimPart(pn_std="SV-OTHER-001", description="另一件")
    db.add(other)
    db.commit()
    with pytest.raises(salvage.SalvageError, match="不一致"):
        _register(db, part_id=other.id, pn="SV-A-001", key="salvage-mismatch-1")
    db.rollback()


def test_salvage_margin_frozen_at_register(db, salvage_project):
    """毛利按登记时冻结的成本证据，后续领用成本变化不改写历史（round-5 Blocker 4）。"""
    from datetime import date

    from app.models.maintenance_project_operations import (
        MaintenanceSiteIssue,
        MaintenanceSiteIssueLine,
    )

    issue = MaintenanceSiteIssue(
        issue_id="sv-issue-1",
        project_id="salvage-project-1",
        issue_no="SV-ISSUE-0001",
        issue_date=date(2026, 8, 1),
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
            issue_line_id="sv-issue-line-1",
            issue_id="sv-issue-1",
            line_no=1,
            part_id=salvage_project["part_id"],
            pn="SV-A-001",
            quantity=Decimal("1"),
            unit_cost_inc_tax=Decimal("113"),
            cost_amount_inc_tax=Decimal("113"),
            unit_cost_ex_tax=Decimal("100"),
            cost_amount_ex_tax=Decimal("100"),
            unit_cost=Decimal("100"),
            cost_amount=Decimal("100"),
            cost_source="manual",
            algorithm_version="synthetic-algo-v1",
        )
    )
    db.commit()
    payload = _register(db, part_id=salvage_project["part_id"], qty="2", revenue="500.00")
    db.commit()
    assert payload["cost_basis_inc_tax"] == 113.0
    assert payload["margin"] == 274.0  # 500 − 113×2
    assert payload["cost_algorithm_version"]

    # 后续新领用成本 200 → 历史毛利不变（冻结）
    issue2 = MaintenanceSiteIssue(
        issue_id="sv-issue-2",
        project_id="salvage-project-1",
        issue_no="SV-ISSUE-0002",
        issue_date=date(2026, 8, 10),
        raw_status="已确认",
        status_mapping_state="mapped",
        normalized_status="confirmed",
        status_mapping_version="synthetic-map-v1",
        source="direct_api",
        version=1,
    )
    db.add(issue2)
    db.flush()
    db.add(
        MaintenanceSiteIssueLine(
            issue_line_id="sv-issue-line-2",
            issue_id="sv-issue-2",
            line_no=1,
            part_id=salvage_project["part_id"],
            pn="SV-A-001",
            quantity=Decimal("1"),
            unit_cost_inc_tax=Decimal("226"),
            cost_amount_inc_tax=Decimal("226"),
            unit_cost_ex_tax=Decimal("200"),
            cost_amount_ex_tax=Decimal("200"),
            unit_cost=Decimal("200"),
            cost_amount=Decimal("200"),
            cost_source="manual",
            algorithm_version="synthetic-algo-v1",
        )
    )
    db.commit()
    listing = salvage.list_salvage(db, "salvage-project-1")
    row = listing["rows"][0]
    assert row["cost_basis_inc_tax"] == 113.0
    assert row["margin"] == 274.0


def test_salvage_api_rejects_without_profit_data(db, salvage_project):
    """无 data_profit：清单 403、登记 403（round-6 Blocker 11 负向门）。"""
    from app import permissions as _perms
    from app.auth import hash_password
    from app.models.system import SysUser

    graph = _perms.effective("sales", None)
    graph.update(
        {
            "page_maintenance": True,
            "action_maintenance_bad_return_manage": True,
            "data_profit": False,
        }
    )
    user = SysUser(
        username="salvage_no_profit",
        role="sales",
        display_name="无利润权限销售",
        password_hash=hash_password("synthetic-password-123"),
        permissions=graph,
    )
    db.add(user)
    db.flush()
    from app.models.maintenance_project import MaintenanceProjectUserAssignment
    from datetime import datetime, timezone

    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id="salvage-noprofit-assign",
            project_id="salvage-project-1",
            responsibility_type="primary_manager",
            user_id=user.id,
            assigned_at=datetime.now(timezone.utc),
            assigned_by="synthetic-admin",
            assignment_reason="合成负责人映射",
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_bad_salvage.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "salvage_no_profit", "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    listing = client.get(
        "/api/maintenance/projects/stable/salvage-project-1/salvages"
    )
    assert listing.status_code == 403
    register = client.post(
        "/api/maintenance/projects/stable/salvage-project-1/salvages",
        json={
            "part_id": salvage_project["part_id"],
            "pn": "SV-A-001",
            "qty": "1",
            "revenue": "10.00",
            "salvage_date": "2026-08-12",
            "idempotency_key": "salvage-noprofit-key-1",
        },
    )
    assert register.status_code == 403
