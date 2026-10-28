"""D-0045：两类人工建档入口共享规格、字典、权限及原子事务约束。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import permissions
from app.api.presales import _call
from app.auth import _make_token
from app.main import app
from app.models.dimensions import DimPart
from app.models.master_data import ProductSpec
from app.models.system import SysAuditLog, SysUser
from app.services import master_edit, part_search, spec_extract
from app.services.presales import common, inquiries
from app.services.workflow.registry import Conflict, InvalidTransition


@pytest.fixture()
def actors(db):
    rows = {}
    for name, role in (("seller", "sales"), ("buyer", "purchaser"), ("peer", "purchaser")):
        row = SysUser(username=f"part-create-{name}", role=role, password_hash="unused",
                      display_name=name, template_perms=permissions.effective(role, None),
                      perm_overrides={})
        db.add(row)
        rows[name] = row
    db.commit()
    return SimpleNamespace(**rows)


def _client(user):
    token, _ = _make_token(user.role, user.username, user.display_name, authn="sys_user")
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def _unbound(db, actors, pn="FRESH-GPU-90817", *, assigned=True):
    inquiry = inquiries.create_draft(db, actors.seller.id,
        lines=[{"pn_raw": pn}], assignee_ids=[actors.buyer.id] if assigned else [])
    inquiries.submit(db, inquiry.id, actors.seller.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    db.commit()
    return inquiry, line


def _specs():
    return {
        "capacity": {"value": "80GB", "unit": "GB", "numeric_value": "80"},
        "custom:核心数": {"value": "132", "unit": None, "numeric_value": None},
    }


@pytest.mark.parametrize("endpoint", ["master", "binding"])
def test_create_persists_metadata_manual_specs_and_audit_atomically(db, actors, endpoint):
    body = {"pn_std": "FRESH-GPU-90817", "description": "GPU计算卡", "summary": "GPU简介",
            "brand": "测试品牌", "manufacturer": "测试制造商", "category_major": "卡",
            "category_minor": "显卡GPU", "spec_updates": _specs()}
    if endpoint == "binding":
        inquiry, line = _unbound(db, actors)
        old_version = inquiry.version
        body["expected_version"] = line.version
        path = f"/api/presales/lines/{line.id}/create-part"
    else:
        path = "/api/parts/master"
    response = _client(actors.buyer).post(path, json=body)
    assert response.status_code == 200, response.text
    db.expire_all()
    part = db.scalar(select(DimPart).where(DimPart.pn_std == body["pn_std"]))
    assert part and part.description == "GPU计算卡" and part.summary == "GPU简介"
    assert part.brand == "测试品牌" and part.manufacturer == "测试制造商"
    assert part.category_major == "卡" and part.category_minor == "显卡GPU"
    assert part.brand_id and part.category_id and part.master_source == "manual"
    assert {"description", "summary", "category_major", "category_minor"} <= set(part.locked_fields)
    specs = {row.spec_key: row for row in db.scalars(select(ProductSpec).where(ProductSpec.part_id == part.id))}
    assert specs["capacity"].source == specs["custom:核心数"].source == "manual"
    assert specs["capacity"].numeric_value == 80
    assert specs["custom:核心数"].spec_value == "132" and specs["custom:核心数"].numeric_value is None
    audit = db.scalar(select(SysAuditLog).where(SysAuditLog.entity_type == "part", SysAuditLog.entity_id == part.id))
    assert audit.operated_by == actors.buyer.username
    assert audit.after_json["_snapshot"]["specs"]["custom:核心数"]["value"] == "132"
    if endpoint == "binding":
        assert line.part_id == part.id and inquiry.version == old_version + 1
        assert inquiry.submitted_snapshot["lines"][line.line_key]["part_id"] == part.id
    # 自动重抽不会盖掉人工规格，自由参数在候选上使用可读中文名称。
    spec_extract.refresh_part_specs(db, part.id, "600GB SAS HDD")
    db.flush()
    rows = part_search.search(db, part.pn_std)
    candidate = next(row for row in rows if row["part_id"] == part.id)
    assert {"label": "核心数", "value": "132"} in candidate["key_specs"]
    db.refresh(specs["capacity"])
    assert specs["capacity"].spec_value == "80GB"


@pytest.mark.parametrize("endpoint", ["master", "binding"])
@pytest.mark.parametrize("changes", [
    {"pn_std": "   "},
    {"category_major": "GPU"},
    {"category_major": "CPU", "category_minor": "显卡GPU"},
    {"category_minor": "显卡GPU"},
    {"spec_updates": {"capacity": {"value": "2TB", "unit": "GB", "numeric_value": "2"}}},
    {"spec_updates": {"custom:核心数": {"value": "132", "unit": None, "numeric_value": "132"}}},
])
def test_invalid_inputs_never_create_part_or_partial_bind(db, actors, endpoint, changes):
    body = {"pn_std": "FRESH-GPU-90817", "brand": "不得部分写入", **changes}
    if endpoint == "binding":
        inquiry, line = _unbound(db, actors)
        before = (inquiry.version, line.version, inquiry.submitted_snapshot)
        path = f"/api/presales/lines/{line.id}/create-part"
        body["expected_version"] = line.version
    else:
        path = "/api/parts/master"
    response = _client(actors.buyer).post(path, json=body)
    assert response.status_code == (422 if endpoint == "binding" else 400), response.text
    assert response.json()["detail"]["code"] == "validation_error"
    assert db.scalar(select(func.count()).select_from(DimPart)) == 0
    assert db.scalar(select(func.count()).select_from(ProductSpec)) == 0
    assert db.scalar(select(func.count()).select_from(SysAuditLog).where(SysAuditLog.entity_type == "part")) == 0
    if endpoint == "binding":
        db.expire_all()
        assert line.part_id is None
        assert (inquiry.version, line.version, inquiry.submitted_snapshot) == before


def test_duplicate_version_and_already_bound_errors_are_distinct(db, actors):
    part = master_edit.create_part(db, pn_std="KNOWN-GPU-33998", force=True)
    inquiry, line = _unbound(db, actors, pn=part["pn_std"])
    client = _client(actors.buyer)
    path = f"/api/presales/lines/{line.id}"
    body = {"pn_std": part["pn_std"], "expected_version": line.version, "spec_updates": _specs()}
    duplicate = client.post(path + "/create-part", json=body)
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "near_duplicates"
    assert duplicate.json()["detail"]["near_duplicates"]
    bound = client.post(path + "/bind-part", json={"part_id": part["id"], "expected_version": line.version})
    assert bound.status_code == 200, bound.text
    stale = client.post(path + "/create-part", json=body)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    body["expected_version"] = stale.json()["detail"]["version"]
    repeated = client.post(path + "/create-part", json=body)
    assert repeated.status_code == 409 and repeated.json()["detail"]["code"] == "already_bound"
    assert repeated.json()["detail"]["part_id"] == part["id"]
    assert db.scalar(select(func.count()).select_from(ProductSpec)) == 0


@pytest.mark.parametrize("denied", ["page_master_data", "action_presales_bind_part", "unassigned", "unclaimed_pool"])
def test_specs_do_not_expand_binding_permissions(db, actors, denied):
    _, line = _unbound(db, actors, assigned=denied != "unclaimed_pool")
    actor = actors.peer if denied == "unassigned" else actors.buyer
    if denied in {"page_master_data", "action_presales_bind_part"}:
        actor.perm_overrides = {denied: False}
        db.commit()
    response = _client(actor).post(f"/api/presales/lines/{line.id}/create-part", json={
        "pn_std": line.pn_raw, "expected_version": line.version, "spec_updates": _specs()})
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "permission_denied"
    assert db.scalar(select(func.count()).select_from(DimPart)) == 0


def test_binding_failure_after_part_audit_flush_rolls_back_everything(db, actors, monkeypatch):
    inquiry, line = _unbound(db, actors)
    old_versions = (inquiry.version, line.version)
    flushed = []

    def fail_event(session, *_args, **_kwargs):
        flushed.append(session.scalar(select(func.count()).select_from(ProductSpec)))
        assert session.scalar(select(func.count()).select_from(SysAuditLog).where(SysAuditLog.entity_type == "part")) == 1
        raise InvalidTransition("模拟绑定时间线失败")

    monkeypatch.setattr(common, "event", fail_event)
    response = _client(actors.buyer).post(f"/api/presales/lines/{line.id}/create-part", json={
        "pn_std": line.pn_raw, "expected_version": line.version, "spec_updates": _specs()})
    assert response.status_code == 422, response.text
    assert flushed == [2]
    db.expire_all()
    assert line.part_id is None and (inquiry.version, line.version) == old_versions
    for model in (DimPart, ProductSpec, SysAuditLog):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_master_categories_exposes_standard_spec_contract(db, actors):
    response = _client(actors.buyer).get("/api/parts/master/categories")
    assert response.status_code == 200
    assert {"key": "capacity", "label": "容量", "unit": "GB"} in response.json()["spec_fields"]
    assert any(row["name"] == "CPU" for row in response.json()["categories"])


@pytest.mark.parametrize("error,code", [
    (Conflict("旧版本", version=2), "version_conflict"),
    (Conflict("有报价", code="existing_offer", existing_offers=[{"id": 1}]), "existing_offer"),
    (Conflict("有草稿", offer_id=7, version=3), "existing_draft"),
    (Conflict("待办被领取", claimed_by=1, version=2), "conflict"),
    (IntegrityError("secret SQL", {"secret": "credential"}, ValueError("internal")), "integrity_conflict"),
])
def test_api_errors_preserve_business_context_without_mislabeling_or_sql(error, code):
    session = Mock()

    def fail():
        raise error

    with pytest.raises(HTTPException) as caught:
        _call(session, fail, write=True)
    detail = caught.value.detail
    assert detail["code"] == code and detail["message"]
    if isinstance(error, Conflict):
        assert all(detail[key] == value for key, value in error.detail.items())
    assert "secret" not in str(detail) and "credential" not in str(detail)
    session.rollback.assert_called_once()
    session.commit.assert_not_called()
