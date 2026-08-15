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
