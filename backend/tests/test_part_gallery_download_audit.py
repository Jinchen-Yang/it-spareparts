"""浏览器图库下载审计：复用读取准入，只落计数且不调用 shop。"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import permissions
from app.api import part_gallery
from app.api.part_gallery import get_gallery_client
from app.auth import _make_token, current_role
from app.db import get_db
from app.main import app
from app.models.system import SysAccessLog


@pytest.fixture()
def client(db):
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = lambda: db

    def no_shop_client():
        pytest.fail("下载审计不应连接 shop 或读取图片")

    app.dependency_overrides[get_gallery_client] = no_shop_client
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def _headers(role="sales"):
    token, _ = _make_token(
        role, "图库下载测试", "测试用户",
        perms=permissions.effective(role, None), authn="sys_user",
    )
    return {"Authorization": f"Bearer {token}"}


def _payload(kind="zip"):
    return {
        "items": [
            {"part_id": 42, "image_ids": ["front-image", "back-image"]},
            {"part_id": 43, "image_ids": ["other-image"]},
        ],
        "kind": kind,
    }


def test_download_audit_requires_login(client, db):
    response = client.post("/api/parts/gallery/download-audit", json=_payload())
    assert response.status_code == 401
    assert db.scalar(select(SysAccessLog).where(SysAccessLog.action == "gallery_download")) is None


def test_download_audit_uses_gallery_read_gate(client, db):
    # 既有读取门只有登录准入，没有额外 page 权限；模拟同一个依赖拒绝请求。
    def denied():
        raise HTTPException(403, "无此读取权限")

    app.dependency_overrides[current_role] = denied
    assert client.get("/api/parts/42/gallery", headers=_headers()).status_code == 403
    response = client.post(
        "/api/parts/gallery/download-audit", headers=_headers(), json=_payload()
    )
    assert response.status_code == 403
    assert db.scalar(select(SysAccessLog).where(SysAccessLog.action == "gallery_download")) is None


@pytest.mark.parametrize("kind", ["single", "zip"])
@pytest.mark.parametrize("role", ["sales", "readonly"])
def test_download_audit_records_only_counts_without_master_permission(client, db, kind, role):
    assert not permissions.effective(role, None)["page_master_data"]
    response = client.post(
        "/api/parts/gallery/download-audit", headers=_headers(role), json=_payload(kind)
    )
    assert response.status_code == 204
    assert response.content == b""
    db.expire_all()
    row = db.scalar(select(SysAccessLog).where(SysAccessLog.action == "gallery_download"))
    assert row is not None
    assert row.username == "图库下载测试"
    assert row.resource == "part"
    assert row.detail == {"part_count": 2, "image_count": 3}


def test_download_audit_counts_distinct_parts_and_never_logs_image_ids(client, db):
    response = client.post(
        "/api/parts/gallery/download-audit",
        headers=_headers(),
        json={
            "items": [
                {"part_id": 42, "image_ids": ["https://oss.example/image?signature=secret"]},
                {"part_id": 42, "image_ids": ["another-image"]},
            ],
            "kind": "zip",
        },
    )
    assert response.status_code == 204
    db.expire_all()
    row = db.scalar(select(SysAccessLog).where(SysAccessLog.action == "gallery_download"))
    assert row.detail == {"part_count": 1, "image_count": 2}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"items": [], "kind": "zip"},
        {"items": [{"part_id": 42, "image_ids": ["image"]}]},
        {"items": [{"part_id": 42, "image_ids": ["image"]}], "kind": "all"},
        {"items": [{"part_id": 42, "image_ids": ["image"]}], "kind": "zip", "url": "secret"},
        {"items": [{"part_id": 42, "image_ids": ["image"], "url": "secret"}], "kind": "zip"},
        {"items": [{"part_id": 42, "image_ids": []}], "kind": "zip"},
        {"items": [{"part_id": 42, "image_ids": [""]}], "kind": "zip"},
        {"items": [{"part_id": 42, "image_ids": ["  "]}], "kind": "zip"},
        {"items": [{"part_id": 42, "image_ids": [None]}], "kind": "zip"},
        {"items": [{"part_id": 42, "image_ids": [1]}], "kind": "zip"},
        {"items": [{"part_id": 42}], "kind": "zip"},
        *[
            {"items": [{"part_id": part_id, "image_ids": ["image"]}], "kind": "zip"}
            for part_id in [0, -1, 2**63, True, 1.5, "42", None]
        ],
    ],
)
def test_download_audit_validates_payload(client, db, payload):
    response = client.post(
        "/api/parts/gallery/download-audit", headers=_headers(), json=payload
    )
    assert response.status_code == 422
    assert db.scalar(select(SysAccessLog).where(SysAccessLog.action == "gallery_download")) is None


def test_download_audit_write_failure_does_not_block_request(client, db, monkeypatch):
    failed_audit_connections = 0
    record_access_log = part_gallery.record_access_log

    def unavailable_session():
        nonlocal failed_audit_connections
        failed_audit_connections += 1
        raise RuntimeError("审计连接不可用")

    def record_with_unavailable_audit_session(*args, **kwargs):
        # 只破坏审计自己的连接，不能把此前执行的真实短会话鉴权也替换掉。
        # 仍调用真实审计实现，验证其 best-effort 异常处理而非绕过写入。
        with monkeypatch.context() as audit_patch:
            audit_patch.setattr("app.db.SessionLocal", unavailable_session)
            return record_access_log(*args, **kwargs)

    monkeypatch.setattr(part_gallery, "record_access_log", record_with_unavailable_audit_session)
    response = client.post(
        "/api/parts/gallery/download-audit", headers=_headers(), json=_payload()
    )
    assert response.status_code == 204
    assert failed_audit_connections == 1
    assert db.scalar(select(SysAccessLog).where(SysAccessLog.action == "gallery_download")) is None
