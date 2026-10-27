"""M7/M8 接口的实名准入、归属隔离、权限与前端 JSON 契约。"""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config
from app.api import audience, field_pool
from app.auth import _make_token
from app.models.fields_audience import AudienceGroup, AudienceRule, FieldDef, FieldValue
from app.models.system import SysUser


@pytest.fixture
def clients(db, monkeypatch):
    users = [
        SysUser(username="owner", display_name="张三", role="readonly", password_hash="unused"),
        SysUser(username="other", display_name="李四", role="readonly", password_hash="unused"),
        SysUser(username="admin", display_name="管理员", role="admin", password_hash="unused"),
        SysUser(username="literal%", role="readonly", password_hash="unused"),
        SysUser(username="inactive", role="readonly", password_hash="unused", is_active=False),
        SysUser(username="deleted", role="readonly", password_hash="unused", deleted_at=datetime.now(UTC)),
    ]
    db.add_all(users)
    db.commit()
    app = FastAPI()
    app.include_router(audience.router, prefix="/api")
    app.include_router(field_pool.router, prefix="/api")
    result = {"anonymous": TestClient(app)}
    for user in users:
        token, _ = _make_token(user.role, user.username, user.display_name, authn="sys_user")
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {token}"
        result[user.username] = client
    logs = []
    monkeypatch.setattr(audience, "record_access_log", lambda *args: logs.append(args))
    monkeypatch.setattr(field_pool, "record_access_log", lambda *args: logs.append(args))
    result["logs"] = logs
    return result


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/audience/people", None),
    ("get", "/api/audience/groups", None),
    ("post", "/api/audience/groups", {"name": "同组"}),
    ("patch", "/api/audience/groups/1", {"name": "修改"}),
    ("delete", "/api/audience/groups/1", None),
    ("get", "/api/fields/defs?subject_type=demo&level=line", None),
    ("get", "/api/fields/template?subject_type=demo&level=line", None),
    ("get", "/api/fields/name-suggestions?subject_type=demo", None),
    ("post", "/api/fields/defs", {"subject_type": "demo", "level": "line", "name": "备注"}),
    ("patch", "/api/fields/defs/1", {"name": "修改"}),
])
def test_every_endpoint_requires_login(clients, monkeypatch, method, path, body):
    # RBAC 总闸关闭也不能把个人分组归属降级为匿名身份。
    monkeypatch.setattr(config, "ENABLE_RBAC", False)
    response = clients["anonymous"].request(method, path, json=body)
    assert response.status_code == 401, response.text


def test_people_only_expose_active_account_id_and_display_name(clients):
    response = clients["owner"].get("/api/audience/people")
    assert response.status_code == 200, response.text
    people = response.json()
    assert {person["id"] for person in people} == {"owner", "other", "admin", "literal%"}
    assert all(set(person) == {"id", "display_name"} for person in people)
    assert clients["owner"].get("/api/audience/people", params={"q": "张"}).json() == [
        {"id": "owner", "display_name": "张三"}
    ]
    assert clients["owner"].get("/api/audience/people", params={"q": "%"}).json() == [
        {"id": "literal%", "display_name": "literal%"}
    ]


def test_shared_fallback_and_inactive_tokens_cannot_own_groups(clients):
    token, _ = _make_token("admin", "owner", None, fallback=True)
    response = clients["anonymous"].get(
        "/api/audience/groups", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert clients["inactive"].get("/api/audience/people").status_code == 401
    assert clients["deleted"].get("/api/audience/people").status_code == 401


def test_personal_group_crud_ownership_and_audit(clients, db):
    owner = clients["owner"]
    response = owner.post("/api/audience/groups", json={"name": "常用人员", "user_ids": ["other"]})
    assert response.status_code == 201, response.text
    group = response.json()
    assert group["owner_id"] == "owner"
    assert group["scope"] == "personal"
    assert group["user_ids"] == ["other"]
    path = f"/api/audience/groups/{group['id']}"
    assert owner.get("/api/audience/groups").json() == [group]
    assert clients["other"].get("/api/audience/groups").json() == []
    for actor in ("other", "admin"):
        assert clients[actor].patch(path, json={"name": "不能改"}).status_code == 403
        assert clients[actor].delete(path).status_code == 403
    response = owner.patch(path, json={"name": "新组名", "user_ids": ["owner", "other"]})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "新组名"
    assert response.json()["user_ids"] == ["other", "owner"]
    assert owner.post("/api/audience/groups", json={"name": "新组名"}).status_code == 409
    response = owner.delete(path)
    assert response.status_code == 200, response.text
    assert response.json()["archived"] is True
    assert owner.get("/api/audience/groups").json() == []
    assert db.scalar(select(AudienceGroup.archived).where(AudienceGroup.id == group["id"])) is True
    assert [entry[1] for entry in clients["logs"]] == [
        "audience_group_create", "audience_group_update", "audience_group_archive"
    ]


def test_public_group_writes_require_account_management_permission(clients):
    body = {"name": "公共组", "scope": "public", "user_ids": ["owner"]}
    assert clients["owner"].post("/api/audience/groups", json=body).status_code == 403
    response = clients["admin"].post("/api/audience/groups", json=body)
    assert response.status_code == 201, response.text
    group_id = response.json()["id"]
    path = f"/api/audience/groups/{group_id}"
    assert clients["owner"].get("/api/audience/groups").json()[0]["id"] == group_id
    assert clients["owner"].patch(path, json={"name": "不能改"}).status_code == 403
    assert clients["owner"].delete(path).status_code == 403
    assert clients["admin"].patch(path, json={"name": "改名"}).status_code == 200
    assert clients["admin"].delete(path).status_code == 200


def test_group_validates_people_and_rejects_owner_mass_assignment(clients):
    for username in ("missing", "inactive", "deleted"):
        response = clients["owner"].post(
            "/api/audience/groups", json={"name": "无效成员", "user_ids": [username]}
        )
        assert response.status_code == 422, response.text
    assert clients["owner"].post(
        "/api/audience/groups", json={"name": "伪造归属", "owner_id": "admin"}
    ).status_code == 422


def test_field_definition_template_rule_and_archive_contract(clients):
    owner = clients["owner"]
    response = owner.post("/api/fields/defs", json={
        "subject_type": "demo", "level": "line", "name": "备注1",
        "default_rule": {"mode": "private"},
    })
    assert response.status_code == 201, response.text
    field = response.json()
    assert field["owner_id"] == "owner"
    assert field["default_rule_id"] is not None
    assert field["default_rule"] == {"mode": "private", "user_ids": [], "group_ids": []}
    assert field["value_type"] == "text"
    params = {"subject_type": "demo", "level": "line"}
    assert owner.get("/api/fields/defs", params=params).json() == [field]
    assert owner.get("/api/fields/template", params=params).json() == [field]
    assert clients["other"].get("/api/fields/defs", params=params).json() == []
    path = f"/api/fields/defs/{field['id']}"
    for actor in ("other", "admin"):
        assert clients[actor].patch(path, json={"name": "不能改"}).status_code == 403
    response = owner.patch(path, json={
        "name": "质保要求", "auto_include": False,
        "default_rule": {"mode": "include", "user_ids": ["other"]},
    })
    assert response.status_code == 200, response.text
    assert response.json()["default_rule"] == {"mode": "include", "user_ids": ["other"], "group_ids": []}
    assert owner.get("/api/fields/template", params=params).json() == []
    assert owner.get("/api/fields/name-suggestions", params={"subject_type": "demo", "q": "质保"}).json() == [
        {"field_def_id": field["id"], "name": "质保要求", "uses": 0}
    ]
    cleared = owner.patch(path, json={"default_rule": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["default_rule_id"] is None
    assert cleared.json()["default_rule"] is None
    response = owner.patch(path, json={"archived": True})
    assert response.status_code == 200, response.text
    assert owner.get("/api/fields/defs", params=params).json() == []
    assert [entry[1] for entry in clients["logs"]] == [
        "field_def_create", "field_def_update", "field_def_update", "field_def_update"
    ]


def test_team_field_permission_is_explicit_by_subject_type(clients):
    body = {"subject_type": "demo", "level": "line", "name": "公共备注", "owner_scope": "team"}
    assert clients["owner"].post("/api/fields/defs", json=body).status_code == 403
    assert clients["admin"].post("/api/fields/defs", json={**body, "subject_type": "unregistered"}).status_code == 403
    response = clients["admin"].post("/api/fields/defs", json=body)
    assert response.status_code == 201, response.text
    field_id = response.json()["id"]
    path = f"/api/fields/defs/{field_id}"
    assert clients["owner"].get(
        "/api/fields/defs", params={"subject_type": "demo", "level": "line"}
    ).json()[0]["id"] == field_id
    assert clients["owner"].patch(path, json={"name": "不能改"}).status_code == 403
    assert clients["owner"].patch(path, json={"archived": True}).status_code == 403
    assert clients["admin"].patch(path, json={"name": "新公共备注"}).status_code == 200
    assert clients["admin"].patch(path, json={"archived": True}).status_code == 200


def test_field_validation_rolls_back_invalid_rule_and_duplicate(clients, db):
    owner = clients["owner"]
    body = {"subject_type": "demo", "level": "line", "name": "备注"}
    response = owner.post("/api/fields/defs", json={**body, "default_rule": {"mode": "include"}})
    assert response.status_code == 422, response.text
    assert db.scalar(select(func.count()).select_from(FieldDef)) == 0
    assert db.scalar(select(func.count()).select_from(AudienceRule)) == 0
    assert owner.post("/api/fields/defs", json=body).status_code == 201
    response = owner.post("/api/fields/defs", json={**body, "default_rule": {"mode": "private"}})
    assert response.status_code == 409, response.text
    assert db.scalar(select(func.count()).select_from(AudienceRule)) == 0
    assert owner.post("/api/fields/defs", json={**body, "name": "新字段", "owner_id": "other"}).status_code == 422


@pytest.mark.parametrize("path,params", [
    ("/api/fields/defs", {"subject_type": "   ", "level": "line"}),
    ("/api/fields/template", {"subject_type": "demo", "level": "line", "anchor": ""}),
    ("/api/fields/template", {"subject_type": "demo", "level": "line", "anchor": "   "}),
    ("/api/fields/name-suggestions", {"subject_type": "   "}),
])
def test_invalid_query_returns_validation_error(clients, path, params):
    response = clients["owner"].get(path, params=params)
    assert response.status_code == 422, response.text


def test_field_type_and_choice_changes_preserve_existing_values(clients, db):
    owner = clients["owner"]
    response = owner.post("/api/fields/defs", json={
        "subject_type": "demo", "level": "line", "name": "状态",
        "value_type": "choice", "choices": ["合格", "待复检"],
    })
    assert response.status_code == 201, response.text
    field_id = response.json()["id"]
    db.add(FieldValue(
        subject_type="demo", subject_id="synthetic-subject", field_def_id=field_id,
        value="合格", updated_by="owner",
    ))
    db.commit()
    path = f"/api/fields/defs/{field_id}"
    assert owner.patch(path, json={"choices": ["待复检"]}).status_code == 422
    assert owner.patch(path, json={"value_type": "text", "choices": None}).status_code == 422
    response = owner.patch(path, json={"name": "质检状态"})
    assert response.status_code == 200, response.text
    assert response.json()["choices"] == ["合格", "待复检"]
    db.expire_all()
    assert db.scalar(select(FieldValue.value).where(FieldValue.field_def_id == field_id)) == "合格"
