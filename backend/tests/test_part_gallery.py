"""型号图库代理：权限、503、请求头/幂等键透传、错误映射、响应透传。

shop 侧用 ``httpx.MockTransport`` 假服务，不访问真实商城。
"""

import uuid
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app import permissions
from app.api.part_gallery import get_gallery_client
from app.auth import _make_token
from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models.dimensions import DimPart
from app.models.system import SysAccessLog
from app.services.shop_gallery_client import (
    ShopGalleryClient,
    ShopGalleryNotConfigured,
    ShopGalleryUnavailable,
    get_shop_gallery_client,
)

SHOP_TOKEN = "synthetic-shop-gallery-token-for-tests"
PART_ID = 42
IMAGE_ID = "11111111-1111-4111-8111-111111111111"
UPLOAD_ID = "22222222-2222-4222-8222-222222222222"


def _headers(role="purchaser", user="张三", request_id=None):
    token, _ = _make_token(
        role, user, "测试用户",
        perms=permissions.effective(role, None), authn="sys_user",
    )
    headers = {"Authorization": f"Bearer {token}"}
    if request_id is not None:
        headers["X-Request-Id"] = request_id
    return headers


def _image(image_id=IMAGE_ID):
    return {
        "file_no": 1,
        "filename": "front.jpg",
        "id": image_id,
        "pn_id": "33333333-3333-4333-8333-333333333333",
        "url": "https://oss.example/front.jpg?signature=x",
        "thumb_url": None,
        "width": 800,
        "height": 600,
        "bytes": 12345,
        "content_type": "image/jpeg",
        "source": "upload",
        "status": "gallery",
        "sort_order": 0,
        "is_defect": False,
        "has_foreign_watermark": False,
        "thumbnail_status": "pending",
        "created_at": "2026-10-24T00:00:00Z",
    }


def _gallery_body(revision=3):
    return {
        "part_id": PART_ID,
        "pn_id": "33333333-3333-4333-8333-333333333333",
        "pn_display": "PN-GALLERY",
        "gallery_revision": revision,
        "has_image": True,
        "main_image": _image(),
        "images": [_image()],
        "candidates": [],
    }


@pytest.fixture()
def client(db):
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


@pytest.fixture()
def shop():
    """假 shop：测试按需替换 handler，并记录收到的 httpx.Request。"""
    state = {
        "requests": [],
        "handler": lambda request: httpx.Response(200, json=_gallery_body()),
    }

    def handler(request):
        state["requests"].append(request)
        return state["handler"](request)

    shop_client = ShopGalleryClient(
        "https://shop.example", SHOP_TOKEN, transport=httpx.MockTransport(handler)
    )
    # 路由依赖的是 part_gallery.get_gallery_client（配置检查包装），覆盖它才能绕过本机配置。
    app.dependency_overrides[get_gallery_client] = lambda: shop_client
    try:
        yield state
    finally:
        app.dependency_overrides.pop(get_gallery_client, None)


def _seed_part(db, pn="PN-GALLERY"):
    part = DimPart(pn_std=pn, description="图库测试型号")
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def test_read_requires_login_but_not_master_permission(client, shop, db):
    part = _seed_part(db)
    assert client.get(f"/api/parts/{part.id}/gallery").status_code == 401

    response = client.get(f"/api/parts/{part.id}/gallery", headers=_headers("sales"))
    assert response.status_code == 200
    assert response.json() == _gallery_body()
    # GET 不要求幂等键，也不应伪造一个。
    assert "x-request-id" not in shop["requests"][0].headers


def test_writes_require_login(client, shop, db):
    part = _seed_part(db)
    response = client.post(
        f"/api/parts/{part.id}/gallery/uploads/sign",
        json={"filename": "a.jpg", "bytes": 10, "content_type": "image/jpeg"},
    )
    assert response.status_code == 401
    assert shop["requests"] == []


def test_writes_require_master_data_permission(client, shop, db):
    part = _seed_part(db)
    response = client.post(
        f"/api/parts/{part.id}/gallery/uploads/sign",
        headers=_headers("sales", request_id=str(uuid.uuid4())),
        json={"filename": "a.jpg", "bytes": 10, "content_type": "image/jpeg"},
    )
    assert response.status_code == 403
    assert shop["requests"] == []


def test_unconfigured_service_returns_503(client, db, monkeypatch):
    part = _seed_part(db)
    settings = get_settings()
    monkeypatch.setattr(settings, "shop_gallery_base_url", "")
    monkeypatch.setattr(settings, "shop_gallery_token", SecretStr(""))
    app.dependency_overrides.pop(get_gallery_client, None)
    response = client.get(f"/api/parts/{part.id}/gallery", headers=_headers())
    assert response.status_code == 503
    assert response.json()["detail"] == "图库服务未配置"


def test_get_shop_gallery_client_requires_both_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "shop_gallery_base_url", "")
    monkeypatch.setattr(settings, "shop_gallery_token", SecretStr("token"))
    with pytest.raises(ShopGalleryNotConfigured):
        get_shop_gallery_client()
    monkeypatch.setattr(settings, "shop_gallery_base_url", "https://shop.example")
    monkeypatch.setattr(settings, "shop_gallery_token", SecretStr(""))
    with pytest.raises(ShopGalleryNotConfigured):
        get_shop_gallery_client()
    monkeypatch.setattr(settings, "shop_gallery_token", SecretStr("token"))
    assert isinstance(get_shop_gallery_client(), ShopGalleryClient)


def test_sign_forwards_actor_headers_and_request_id(client, shop, db):
    part = _seed_part(db)
    request_id = str(uuid.uuid4())
    ticket = {
        "upload_id": UPLOAD_ID,
        "method": "POST",
        "url": "https://oss.example/upload",
        "fields": {"key": "gallery/front.jpg"},
        "headers": {},
        "expires_at": "2026-10-24T01:00:00Z",
        "max_bytes": 20 * 1024 * 1024,
    }
    shop["handler"] = lambda request: httpx.Response(200, json=ticket)
    response = client.post(
        f"/api/parts/{part.id}/gallery/uploads/sign",
        headers=_headers(user="张三", request_id=request_id),
        json={"filename": "front.jpg", "bytes": 12345, "content_type": "image/jpeg"},
    )
    assert response.status_code == 200
    assert response.json() == ticket
    upstream = shop["requests"][0]
    assert upstream.method == "POST"
    assert upstream.url.path == f"/api/integrations/gallery/parts/{part.id}/uploads/sign"
    assert upstream.headers["authorization"] == f"Bearer {SHOP_TOKEN}"
    assert upstream.headers["x-actor-system"] == "itsp"
    assert upstream.headers["x-actor-user"] == quote("张三", safe="")
    assert upstream.headers["x-request-id"] == request_id


def test_write_records_itsp_access_audit(client, shop, db):
    part = _seed_part(db)
    shop["handler"] = lambda request: httpx.Response(200, json={
        "upload_id": UPLOAD_ID, "method": "POST", "url": "https://oss.example/upload",
        "fields": {}, "headers": {}, "expires_at": "2026-10-24T01:00:00Z",
        "max_bytes": 20 * 1024 * 1024,
    })
    request_id = str(uuid.uuid4())
    response = client.post(
        f"/api/parts/{part.id}/gallery/uploads/sign",
        headers=_headers(user="张三", request_id=request_id),
        json={"filename": "front.jpg", "bytes": 10, "content_type": "image/jpeg"},
    )
    assert response.status_code == 200
    db.expire_all()
    row = db.scalar(
        select(SysAccessLog)
        .where(SysAccessLog.action == "gallery_upload_sign")
        .order_by(SysAccessLog.id.desc())
    )
    assert row is not None
    assert row.username == "张三"
    assert row.detail == {"part_id": part.id, "request_id": request_id}


def test_register_passes_201_body_and_etag(client, shop, db):
    part = _seed_part(db)
    body = {
        "image": _image(),
        "gallery_revision": 4,
        "duplicate": False,
        "restored": False,
    }
    shop["handler"] = lambda request: httpx.Response(201, json=body, headers={"ETag": '"4"'})
    request_id = str(uuid.uuid4())
    response = client.post(
        f"/api/parts/{part.id}/gallery/images",
        headers=_headers(request_id=request_id),
        json={"upload_id": UPLOAD_ID, "source": "upload", "is_defect": False},
    )
    assert response.status_code == 201
    assert response.json() == body
    assert response.headers["etag"] == '"4"'
    upstream = shop["requests"][0]
    assert upstream.url.path == f"/api/integrations/gallery/parts/{part.id}/images"
    assert upstream.headers["x-request-id"] == request_id
    assert upstream.content == (
        b'{"upload_id":"' + UPLOAD_ID.encode() + b'","source":"upload","is_defect":false}'
    )


def test_update_passes_only_changed_fields(client, shop, db):
    part = _seed_part(db)
    body = {"image": _image(), "gallery_revision": 5}
    shop["handler"] = lambda request: httpx.Response(200, json=body, headers={"ETag": '"5"'})
    request_id = str(uuid.uuid4())
    response = client.patch(
        f"/api/parts/{part.id}/gallery/images/{IMAGE_ID}",
        headers=_headers(request_id=request_id),
        json={"baseline_gallery_revision": 4, "sort_order": 2},
    )
    assert response.status_code == 200
    assert response.json() == body
    assert response.headers["etag"] == '"5"'
    assert shop["requests"][0].url.path == f"/api/integrations/gallery/images/{IMAGE_ID}"
    assert shop["requests"][0].content == b'{"baseline_gallery_revision":4,"sort_order":2}'


def test_delete_passes_baseline_and_returns_etag(client, shop, db):
    part = _seed_part(db)
    shop["handler"] = lambda request: httpx.Response(204, headers={"ETag": '"6"'})
    request_id = str(uuid.uuid4())
    response = client.delete(
        f"/api/parts/{part.id}/gallery/images/{IMAGE_ID}",
        headers=_headers(request_id=request_id),
        params={"baseline_gallery_revision": 5},
    )
    assert response.status_code == 204
    assert response.headers["etag"] == '"6"'
    upstream = shop["requests"][0]
    assert upstream.url.params["baseline_gallery_revision"] == "5"
    assert upstream.headers["x-request-id"] == request_id


def test_write_requires_uuid_request_id(client, shop, db):
    part = _seed_part(db)
    missing = client.post(
        f"/api/parts/{part.id}/gallery/uploads/sign",
        headers=_headers(),
        json={"filename": "a.jpg", "bytes": 10, "content_type": "image/jpeg"},
    )
    assert missing.status_code == 400
    assert missing.json()["detail"] == "写操作缺少 X-Request-Id"
    invalid = client.post(
        f"/api/parts/{part.id}/gallery/uploads/sign",
        headers=_headers(request_id="not-a-uuid"),
        json={"filename": "a.jpg", "bytes": 10, "content_type": "image/jpeg"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "X-Request-Id 必须是 UUID"
    assert shop["requests"] == []


@pytest.mark.parametrize(
    "upstream_status, upstream_body, expected_status, expected_detail",
    [
        (404, {"detail": "商品不存在"}, 404, "该型号在商城尚无商品或未对接"),
        (409, {"detail": "图库已被其他人修改，请刷新后重试"}, 409,
         "图库已被其他人修改，请刷新后重试"),
        (503, {"detail": "维护中"}, 503, "图库服务暂不可用"),
        (500, {"detail": "boom"}, 503, "图库服务暂不可用"),
        (422, {"detail": "排序位置超出图库范围"}, 422, "排序位置超出图库范围"),
        (401, {"detail": "服务密钥无效"}, 502, "图库服务鉴权失败"),
    ],
)
def test_maps_shop_errors(
    client, shop, db, upstream_status, upstream_body, expected_status, expected_detail
):
    part = _seed_part(db)
    shop["handler"] = lambda request: httpx.Response(upstream_status, json=upstream_body)
    response = client.get(f"/api/parts/{part.id}/gallery", headers=_headers())
    assert response.status_code == expected_status
    assert response.json()["detail"] == expected_detail


def test_network_failure_maps_to_503(client, db):
    part = _seed_part(db)
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(refuse)
    shop_client = ShopGalleryClient("https://shop.example", SHOP_TOKEN, transport=transport)
    app.dependency_overrides[get_gallery_client] = lambda: shop_client
    try:
        response = client.get(f"/api/parts/{part.id}/gallery", headers=_headers())
        assert response.status_code == 503
        assert response.json()["detail"] == "图库服务暂不可用"
    finally:
        app.dependency_overrides.pop(get_gallery_client, None)


def test_resumable_paths_are_proxied(client, shop, db):
    part = _seed_part(db)
    shop["handler"] = lambda request: (
        httpx.Response(204) if request.method == "DELETE"
        else httpx.Response(200, json={"upload_id": UPLOAD_ID, "status": "uploading"})
    )
    base = f"/api/parts/{part.id}/gallery"
    rid = str(uuid.uuid4())
    init = client.post(
        f"{base}/uploads/resumable",
        headers=_headers(request_id=rid),
        json={
            "filename": "big.jpg", "bytes": 100, "content_type": "image/jpeg",
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert init.status_code == 200
    assert client.get(f"{base}/uploads/{UPLOAD_ID}/resumable", headers=_headers()).status_code == 200
    assert client.post(
        f"{base}/uploads/{UPLOAD_ID}/parts/1/sign",
        headers=_headers(request_id=str(uuid.uuid4())),
    ).status_code == 200
    assert client.post(
        f"{base}/uploads/{UPLOAD_ID}/complete",
        headers=_headers(request_id=str(uuid.uuid4())),
    ).status_code == 200
    assert client.delete(
        f"{base}/uploads/{UPLOAD_ID}/resumable",
        headers=_headers(request_id=str(uuid.uuid4())),
    ).status_code == 204
    assert [request.url.path for request in shop["requests"]] == [
        f"/api/integrations/gallery/parts/{part.id}/uploads/resumable",
        f"/api/integrations/gallery/parts/{part.id}/uploads/{UPLOAD_ID}/resumable",
        f"/api/integrations/gallery/parts/{part.id}/uploads/{UPLOAD_ID}/parts/1/sign",
        f"/api/integrations/gallery/parts/{part.id}/uploads/{UPLOAD_ID}/complete",
        f"/api/integrations/gallery/parts/{part.id}/uploads/{UPLOAD_ID}/resumable",
    ]


def test_client_rejects_redirects_and_oversized_responses():
    redirecting = ShopGalleryClient(
        "https://shop.example", "token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"Location": "https://elsewhere"})
        ),
    )
    with pytest.raises(ShopGalleryUnavailable):
        redirecting.request("GET", "/x", actor_user="u")

    oversized = ShopGalleryClient(
        "https://shop.example", "token", max_response_bytes=8,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"x": "long"})),
    )
    with pytest.raises(ShopGalleryUnavailable):
        oversized.request("GET", "/x", actor_user="u")
