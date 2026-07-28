"""维保项目毛利默认口径：单例设置、权限、乐观锁与审计契约。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

from app import permissions
from app.auth import hash_password
from app.main import app
from app.models.system import SysAuditLog, SysBusinessSetting, SysUser


def _client_for(db, username: str, role: str, *, page_maintenance: bool) -> TestClient:
    user_perms = permissions.effective(role, {"page_maintenance": page_maintenance})
    db.add(
        SysUser(
            username=username,
            role=role,
            password_hash=hash_password("pw123456"),
            permissions=user_perms,
        ),
    )
    db.commit()
    client = TestClient(app)
    token = client.post(
        "/api/auth/login",
        json={"username": username, "password": "pw123456"},
    ).json()["token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def test_singleton_seed_and_database_checks(db):
    setting = db.get(SysBusinessSetting, 1)
    assert setting is not None
    assert setting.maintenance_project_profit_default_basis == "both"
    assert setting.version == 1

    singleton_savepoint = db.begin_nested()
    with pytest.raises(IntegrityError):
        db.execute(text(
            "INSERT INTO sys_business_setting"
            " (id, maintenance_project_profit_default_basis, version)"
            " VALUES (2, 'both', 1)",
        ))
    singleton_savepoint.rollback()

    basis_savepoint = db.begin_nested()
    with pytest.raises(IntegrityError):
        db.execute(text(
            "UPDATE sys_business_setting"
            " SET maintenance_project_profit_default_basis = 'invalid'"
            " WHERE id = 1",
        ))
    basis_savepoint.rollback()
    db.expire_all()


def test_get_requires_login_and_maintenance_page_permission(db):
    assert TestClient(app).get("/api/system-settings").status_code == 401

    denied = _client_for(db, "denied", "readonly", page_maintenance=False)
    assert denied.get("/api/system-settings").status_code == 403

    allowed = _client_for(db, "allowed", "purchaser", page_maintenance=True)
    response = allowed.get("/api/system-settings")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "maintenance_project_profit_default_basis": "both",
        "version": 1,
    }


def test_put_is_admin_only_and_records_audit_in_same_commit(db):
    non_admin = _client_for(db, "buyer", "purchaser", page_maintenance=True)
    assert non_admin.put(
        "/api/system-settings",
        json={
            "maintenance_project_profit_default_basis": "inc",
            "expected_version": 1,
        },
    ).status_code == 403

    admin = _client_for(db, "admin", "admin", page_maintenance=True)
    response = admin.put(
        "/api/system-settings",
        json={
            "maintenance_project_profit_default_basis": "inc",
            "expected_version": 1,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["maintenance_project_profit_default_basis"] == "inc"
    assert response.json()["version"] == 2
    assert response.json()["updated_by"] == "admin"

    # 普通维保用户只需要展示默认值；管理员登录名和审计时间不得外泄。
    public_view = non_admin.get("/api/system-settings")
    assert public_view.status_code == 200
    assert public_view.json() == {
        "maintenance_project_profit_default_basis": "inc",
        "version": 2,
    }

    db.expire_all()
    setting = db.get(SysBusinessSetting, 1)
    audit = db.scalar(
        select(SysAuditLog)
        .where(SysAuditLog.entity_type == "sys_business_setting")
        .order_by(SysAuditLog.id.desc()),
    )
    assert setting is not None and setting.version == 2
    assert audit is not None
    assert audit.entity_id == 1
    assert audit.action == "maintenance_profit_basis_update"
    assert audit.before_json == {
        "maintenance_project_profit_default_basis": "both",
        "version": 1,
    }
    assert audit.after_json == {
        "maintenance_project_profit_default_basis": "inc",
        "version": 2,
    }
    assert audit.operated_by == "admin"


def test_put_optimistic_lock_and_same_value_idempotency(db):
    admin = _client_for(db, "admin", "admin", page_maintenance=True)
    statements: list[str] = []

    def capture_sql(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        first = admin.put(
            "/api/system-settings",
            json={
                "maintenance_project_profit_default_basis": "ex",
                "expected_version": 1,
            },
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
    assert first.status_code == 200
    assert first.json()["version"] == 2
    assert any(
        "sys_business_setting" in statement and "FOR UPDATE" in statement
        for statement in statements
    )

    stale = admin.put(
        "/api/system-settings",
        json={
            "maintenance_project_profit_default_basis": "both",
            "expected_version": 1,
        },
    )
    assert stale.status_code == 409
    assert "刷新" in stale.json()["detail"]

    idempotent = admin.put(
        "/api/system-settings",
        json={
            "maintenance_project_profit_default_basis": "ex",
            "expected_version": 2,
        },
    )
    assert idempotent.status_code == 200
    assert idempotent.json()["version"] == 2

    db.expire_all()
    audits = db.scalars(
        select(SysAuditLog).where(
            SysAuditLog.entity_type == "sys_business_setting",
        ),
    ).all()
    assert len(audits) == 1
