"""共享口令不能借同名实名账号写入售前、受众或字段池。"""

import base64
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth, config
from app.config import get_settings
from app.main import app
from app.models.system import SysUser


WRITE_REQUESTS = [
    ("POST", "/api/presales/inquiries", {"lines": [{"pn_raw": "R5-LOGIN"}]}),
    ("PATCH", "/api/presales/inquiries/1", {"expected_version": 1, "notes": "修改"}),
    ("DELETE", "/api/presales/inquiries/1", {"expected_version": 1}),
    ("POST", "/api/presales/inquiries/1/submit", {"expected_version": 1}),
    ("POST", "/api/presales/inquiries/1/withdraw", {"expected_version": 1}),
    ("POST", "/api/presales/inquiries/1/resubmit", {"expected_version": 1}),
    ("POST", "/api/presales/inquiries/1/lines", {
        "expected_version": 1, "lines": [{"pn_raw": "追加"}]}),
    ("POST", "/api/presales/inquiries/1/close", {"expected_version": 1, "outcome": "cancelled"}),
    ("POST", "/api/presales/inquiries/1/copy", {"expected_version": 1}),
    ("POST", "/api/presales/inquiries/1/lines/1/offers", {
        "expected_version": 1, "unit_price": "10"}),
    ("POST", "/api/presales/inquiries/1/reply", {"expected_version": 1}),
    ("POST", "/api/presales/inquiries/1/publish", {"expected_version": 1}),
    ("PATCH", "/api/presales/offers/1", {"expected_version": 1, "unit_price": "20"}),
    ("POST", "/api/presales/offers/1/withdraw", {"expected_version": 1, "reason": "撤销"}),
    ("POST", "/api/presales/lines/1/bind-part", {"expected_version": 1, "part_id": 1}),
    ("POST", "/api/presales/lines/1/create-part", {"expected_version": 1, "pn_std": "NEW-PN"}),
    ("POST", "/api/audience/groups", {"name": "共享口令不能创建"}),
    ("PATCH", "/api/audience/groups/1", {"name": "共享口令不能修改"}),
    ("DELETE", "/api/audience/groups/1", None),
    ("POST", "/api/fields/defs", {
        "subject_type": "presales_inquiry", "level": "line", "name": "共享口令不能创建"}),
    ("PATCH", "/api/fields/defs/1", {"name": "共享口令不能修改"}),
]


@pytest.mark.parametrize("rbac", [True, False])
def test_shared_admin_cannot_write_after_named_admin_is_created(db, monkeypatch, rbac):
    monkeypatch.setattr(config, "ENABLE_RBAC", rbac)
    monkeypatch.setattr(get_settings(), "admin_password", "r5-shared-password")
    client = TestClient(app)
    assert db.scalar(select(SysUser).where(SysUser.username == "admin")) is None
    login = client.post("/api/auth/login", json={
        "username": "admin", "password": "r5-shared-password"})
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    identity = auth.verify_token(token)
    assert identity["sub"] == "admin" and identity["authn"] == "shared"
    assert not identity.get("fb")

    # 模拟管理员实名建档后，先前签发的共享口令 token 仍在有效期内。
    db.add(SysUser(username="admin", role="admin", password_hash="实名口令不参与测试"))
    db.commit()
    assert db.scalar(select(SysUser).where(SysUser.username == "admin")) is not None
    client.headers["Authorization"] = "Bearer " + token
    responses = [(method, path, client.request(method, path, json=body).status_code)
                 for method, path, body in WRITE_REQUESTS]
    assert all(code in {401, 403} for _, _, code in responses), responses


@pytest.mark.parametrize("invalid", ["fallback", "missing_sub", "empty_sub"])
def test_non_named_tokens_cannot_write_even_with_existing_admin(db, invalid):
    db.add(SysUser(username="admin", role="admin", password_hash="实名口令不参与测试"))
    db.commit()
    token, _ = auth._make_token("admin", "admin", None,
                                fallback=invalid == "fallback", authn="sys_user")
    if invalid != "fallback":
        identity = auth.verify_token(token)
        if invalid == "missing_sub":
            identity.pop("sub")
        else:
            identity["sub"] = ""
        body = json.dumps(identity, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
        token = encoded + "." + auth._sign(body)
    client = TestClient(app, headers={"Authorization": "Bearer " + token})
    responses = [(method, path, client.request(method, path, json=body).status_code)
                 for method, path, body in WRITE_REQUESTS]
    assert all(code in {401, 403} for _, _, code in responses), responses
