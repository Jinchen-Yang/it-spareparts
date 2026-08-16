"""M5-2：维保展示板发布闸门契约（plan v1.3 §2.6）。

集中锁死「回滚 = 关 flag」所依赖的三条运行时不变量：
  1. flag 关闭 → 展示板与 WBDD 上传整组 404（与未发布不可区分）；
  2. flag 关闭 → 既有稳定维保能力**完全不受影响**（回滚零副作用）；
  3. flag 与权限正交：开闸不等于放权，权限键仍逐一把关。
"""
import pytest
from fastapi.testclient import TestClient

from app import permissions
from app.auth import hash_password
from app.config import get_settings
from app.main import app
from app.models.system import SysUser

_PASSWORD = "synthetic-release-gate-1"

_BOSS_PATHS = (
    "/api/maintenance/boss-board/health",
    "/api/maintenance/boss-board/summary",
    "/api/maintenance/boss-board/attention",
    "/api/maintenance/boss-board/projects",
    "/api/maintenance/wbdd-imports/latest",
)
# 回滚后必须照常工作的稳定端点（旧应用兼容面）
_STABLE_PATHS = (
    "/api/maintenance/projects",
    "/api/maintenance/board",
)


def _client(db, *, username: str, role: str = "readonly",
            overrides: dict | None = None) -> TestClient:
    base = permissions.effective(role, None)
    effective = permissions.effective_from_snapshot(base, overrides or {})
    db.add(SysUser(
        username=username, role=role, display_name=username,
        password_hash=hash_password(_PASSWORD), is_active=True,
        template_code=role, template_version=1, template_perms=base,
        perm_overrides=overrides or {}, permissions=effective,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": username, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


@pytest.fixture
def flag():
    settings = get_settings()
    original = settings.maintenance_boss_dashboard_enabled

    def _set(value: bool):
        settings.maintenance_boss_dashboard_enabled = value

    yield _set
    settings.maintenance_boss_dashboard_enabled = original


def test_flag_defaults_closed():
    """发布默认态：总闸关闭（迁移可先行，功能后开——铁律 7）。"""
    from app.config import Settings

    assert Settings().maintenance_boss_dashboard_enabled is False


def test_flag_off_hides_every_board_endpoint(db, flag):
    flag(False)
    client = _client(db, username="gate-boss", role="admin")
    for path in _BOSS_PATHS:
        assert client.get(path).status_code == 404, path


def test_flag_off_keeps_stable_maintenance_intact(db, flag):
    """回滚零副作用：关闸后既有维保能力照常（这是「回滚=关 flag」的业务前提）。"""
    flag(False)
    client = _client(db, username="gate-stable", role="admin")
    for path in _STABLE_PATHS:
        assert client.get(path).status_code == 200, path


def test_flag_on_exposes_board_to_authorized_accounts_only(db, flag):
    flag(True)
    authorized = _client(db, username="gate-authorized",
                         overrides={"page_maintenance_boss": True,
                                    "page_maintenance": True})
    unauthorized = _client(db, username="gate-unauthorized", overrides={})
    for path in _BOSS_PATHS:
        assert authorized.get(path).status_code == 200, path
        # 开闸 ≠ 放权：无查看权限仍是 403（不是 404，因为路由已存在）
        assert unauthorized.get(path).status_code == 403, path


def test_flag_toggle_is_reversible_within_one_process(db, flag):
    """回滚可逆性：同一进程内关→开→关，端点可达性完全跟随 flag。"""
    client = _client(db, username="gate-toggle", role="admin")
    flag(False)
    assert client.get(_BOSS_PATHS[0]).status_code == 404
    flag(True)
    assert client.get(_BOSS_PATHS[0]).status_code == 200
    flag(False)
    assert client.get(_BOSS_PATHS[0]).status_code == 404


def test_wbdd_upload_write_path_is_gated_too(db, flag):
    """写入路径同受闸控：关闸时上传端点也必须 404，避免绕过展示层写数据。"""
    flag(False)
    client = _client(db, username="gate-writer",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    resp = client.post("/api/maintenance/wbdd-imports",
                       files={"file": ("x.xlsx", b"not-a-real-xlsx",
                                       "application/octet-stream")},
                       headers={"Idempotency-Key": "gate-check-key-1"})
    assert resp.status_code == 404
