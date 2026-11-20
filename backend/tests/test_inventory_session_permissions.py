"""Server-authenticated navigation gates, independent of browser login caches."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import _make_token
from app.main import app
from app.models.system import SysUser
from tests.boss_board_helpers import client_for

KEY = "page_inventory_analytics"
URL = "/api/auth/session-permissions"


@pytest.mark.parametrize("role", ["admin", "readonly", "sales"])
@pytest.mark.parametrize("granted", [True, False])
def test_session_inventory_gate_uses_current_account_not_signed_cache(db, role, granted):
    client = client_for(db, username=f"session-{role}", role=role, overrides={KEY: granted})
    user = db.scalar(select(SysUser).where(SysUser.username == f"session-{role}"))
    token, _ = _make_token(role, user.username, None, perms={KEY: not granted},
                           token_version=user.token_version or 0, authn="sys_user")
    client.headers["Authorization"] = f"Bearer {token}"
    reply = client.get(URL)
    assert reply.status_code == 200
    assert reply.headers["Cache-Control"] == "no-store"
    assert reply.json() == {KEY: granted}
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    assert client.get(URL).status_code == 401


@pytest.mark.parametrize("authn,fallback", [(None, False), ("shared_password", True)])
def test_session_inventory_gate_does_not_trust_legacy_or_shared_admin(db, authn, fallback):
    client_for(db, username="session-admin", role="admin", overrides={KEY: True})
    token, _ = _make_token("admin", "session-admin", None, fallback=fallback,
                           perms={KEY: True}, authn=authn)
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    assert client.get(URL).json() == {KEY: False}


def test_session_gate_requires_authentication(db):
    assert TestClient(app).get(URL).status_code == 401
