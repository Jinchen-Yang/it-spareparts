"""旧人工 HTTP 编辑入口须带稳定身份和字段基线。"""
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app import permissions
from app.auth import _make_token
from app.main import app
from app.models.system import SysAuditLog, SysUser
from app.services import master_edit, merge, part_rename

client = TestClient(app)


def headers(role="purchaser"):
    token, _ = _make_token(role, "test-editor", "真实编辑人", perms=permissions.effective(role, None),
                           authn="sys_user")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def real_editor(db):
    db.add(SysUser(username="test-editor", display_name="真实编辑人", role="purchaser",
                   password_hash="unused", is_active=True))
    db.commit()


def state(part_id):
    result = client.get(f"/api/parts/master/{part_id}/edit-state", headers=headers())
    assert result.status_code == 200, result.text
    return result.json()


def patch(baseline, **updates):
    return client.patch("/api/parts/master", headers=headers(), json={
        "part_id": baseline["part_id"], "pn_std": baseline["pn_std"],
        "base_snapshot": baseline, **updates})


def test_edit_state_requires_login():
    assert client.get("/api/parts/master/1/edit-state").status_code == 401


def test_http_fields_conflict_atomically_and_disjoint_fields_merge(db):
    created = master_edit.create_part(db, pn_std="HTTP-CONFLICT", description="旧描述", force=True)
    baseline = state(created["id"])
    assert baseline["fields"]["brand"] is None
    assert patch(baseline, description="先提交描述").status_code == 200
    conflict = patch(baseline, description="旧草稿", brand="不得部分写入")
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    assert detail["code"] == "field_conflict"
    assert detail["conflicts"][0] == {"field": "description", "baseline": "旧描述",
                                       "current": "先提交描述", "proposed": "旧草稿"}
    assert state(created["id"])["fields"]["brand"] is None
    assert patch(baseline, brand="可合并品牌").status_code == 200
    final = state(created["id"])
    assert final["fields"]["description"] == "先提交描述"
    assert final["fields"]["brand"] == "可合并品牌"
    history = client.get(f'/api/parts/master/{created["id"]}/edit-history', headers=headers()).json()
    assert history["history_basis"] == "manual_audit"
    assert len(history["items"]) == 3  # create 与两次成功；冲突不留下生效审计。
    assert history["items"][0]["operated_by"] == "test-editor"
    assert history["items"][0]["before"]["_snapshot"]["fields"]["brand"] is None
    assert history["items"][0]["after"]["_snapshot"]["brand_id"] is not None


def test_http_rejects_missing_baseline_unknown_fields_and_denied_reader(db):
    created = master_edit.create_part(db, pn_std="HTTP-GUARD", force=True)
    baseline = state(created["id"])
    assert client.patch("/api/parts/master", headers=headers(), json={
        "pn_std": "HTTP-GUARD", "brand": "不允许"}).status_code == 422
    assert patch(baseline, stock_quantity=9).status_code == 422
    malformed = {**baseline, "fields": {**baseline["fields"], "brand": 9}}
    assert patch(malformed, brand="不允许").status_code == 422
    assert client.get(f'/api/parts/master/{created["id"]}/edit-state',
                      headers=headers("sales")).status_code == 403
    assert state(created["id"])["fields"]["brand"] is None


def test_description_then_manual_specs_and_per_key_conflicts(db):
    created = master_edit.create_part(db, pn_std="HTTP-SPECS", description="600GB SAS HDD", force=True)
    baseline = state(created["id"])
    result = patch(baseline, description="1.2TB 10K SAS HDD", spec_updates={
        "capacity": {"value": "2TB", "unit": "GB", "numeric_value": "2000"}})
    assert result.status_code == 200, result.text
    final = state(created["id"])
    assert final["specs"]["capacity"] == {
        "value": "2TB", "unit": "GB", "numeric_value": "2000", "source": "manual"}
    assert final["specs"]["rpm"]["value"] == "10K"
    assert patch(baseline, spec_updates={"capacity": None}, brand="不得部分写").status_code == 409
    assert patch(baseline, spec_updates={"interface": {
        "value": "SATA", "unit": None, "numeric_value": None}}).status_code == 200
    assert patch(state(created["id"]), spec_updates={"capacity": None}).status_code == 200
    assert "capacity" not in state(created["id"])["specs"]
    before = state(created["id"])
    assert patch(before, brand="不得部分写", spec_updates={"capacity": {
        "value": "2TB", "unit": "TB", "numeric_value": "2"}}).status_code == 400
    assert state(created["id"]) == before


def test_edit_snapshot_never_redirects_renamed_or_merged_identity(db):
    created = master_edit.create_part(db, pn_std="HTTP-IDENTITY", force=True)
    target = master_edit.create_part(db, pn_std="HTTP-TARGET", force=True)
    baseline = state(created["id"])
    part_rename.rename_part(db, created["id"], "HTTP-RENAMED", "人工确认", operated_by="operator")
    assert patch(baseline, brand="旧身份草稿").status_code == 409
    renamed = state(created["id"])
    merge.merge_parts(db, "HTTP-RENAMED", "HTTP-TARGET", reason="人工确认", operated_by="operator")
    assert patch(renamed, brand="墓碑草稿").status_code == 409
    assert state(target["id"])["fields"]["brand"] is None


@pytest.mark.parametrize("first,second", [
    ({"category_major": "内存"}, {"category_minor": "DDR4"}),
    ({"category_minor": "SATA"}, {"category_major": "内存"}),
])
def test_category_pair_conflicts_when_only_the_other_level_changed(db, first, second):
    created = master_edit.create_part(db, pn_std="HTTP-CATEGORY", category_major="硬盘",
                                     category_minor="SAS", force=True)
    baseline = state(created["id"])
    assert patch(baseline, **first).status_code == 200
    conflict = patch(baseline, **second, brand="不得部分写")
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["conflicts"][0]["field"] == next(iter(first))
    assert state(created["id"])["fields"]["brand"] is None


def test_minor_edit_locks_both_category_levels(db):
    from app.models.dimensions import DimPart

    part = DimPart(pn_std="HTTP-CATEGORY-LOCK", category_major="硬盘", category_minor="SAS",
                   locked_fields=[])
    db.add(part)
    db.commit()
    result = patch(state(part.id), category_minor="SATA")
    assert result.status_code == 200, result.text
    assert {"category_major", "category_minor"} <= set(result.json()["locked_fields"])


@pytest.mark.parametrize("updates", [
    {"description": "同一个新值"},
    {"brand": None},
    {"category_major": "内存", "category_minor": "DDR4"},
    {"spec_updates": {"capacity": {"value": "2TB", "unit": "GB", "numeric_value": "2000.0000"}}},
    {"spec_updates": {"capacity": None}},
])
def test_same_new_value_merges_despite_changed_baseline(db, updates):
    created = master_edit.create_part(db, pn_std="HTTP-CONVERGE", description="600GB SAS HDD",
                                     brand="旧品牌", category_major="硬盘", category_minor="SAS",
                                     force=True)
    baseline = state(created["id"])
    first = patch(baseline, **updates)
    assert first.status_code == 200, first.text
    second = patch(baseline, **updates, unit="台")
    assert second.status_code == 200, second.text
    assert state(created["id"])["fields"]["unit"] == "台"


@pytest.mark.parametrize("operation", ["create", "edit"])
@pytest.mark.parametrize("identity", ["legacy", "fallback", "missing", "empty", "inactive"])
def test_manual_write_requires_real_system_user(db, operation, identity):
    created = master_edit.create_part(db, pn_std="HTTP-REAL-ACTOR", force=True)
    baseline = state(created["id"])
    subject = {"missing": "nonexistent", "empty": ""}.get(identity, "test-editor")
    if identity == "inactive":
        user = db.query(SysUser).filter_by(username="test-editor").one()
        user.is_active = False
        db.commit()
    token, _ = _make_token("purchaser", subject, "自报名字",
                           perms=permissions.effective("purchaser", None),
                           fallback=identity == "fallback",
                           authn=None if identity == "legacy" else "sys_user")
    auth = {"Authorization": f"Bearer {token}"}
    result = (client.post("/api/parts/master", headers=auth, json={"pn_std": "HTTP-FORBIDDEN"})
              if operation == "create" else client.patch("/api/parts/master", headers=auth, json={
                  "part_id": baseline["part_id"], "pn_std": baseline["pn_std"],
                  "base_snapshot": baseline, "brand": "不得写入"}))
    assert result.status_code == (401 if identity == "inactive" else 403), result.text
    # 通过公共历史读取确认没有成功命令；未执行的操作不留下生效审计。
    history = client.get(f'/api/parts/master/{created["id"]}/edit-history', headers=headers("admin"))
    if identity != "inactive":
        assert len(history.json()["items"]) == 1


def test_create_audit_records_verified_username(db):
    result = client.post("/api/parts/master", headers=headers(), json={"pn_std": "HTTP-REAL-CREATE"})
    assert result.status_code == 200, result.text
    history = client.get(f'/api/parts/master/{result.json()["id"]}/edit-history', headers=headers())
    assert history.json()["items"][0]["operated_by"] == "test-editor"


def test_edit_history_requires_login_and_page_permission(db):
    created = master_edit.create_part(db, pn_std="HTTP-HISTORY-AUTH", force=True)
    endpoint = f'/api/parts/master/{created["id"]}/edit-history'
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.get(endpoint, headers=headers("sales")).status_code == 403


@pytest.mark.parametrize("operation", ["create", "edit"])
def test_deadlock_at_final_commit_returns_conflict_and_rolls_back_everything(db, operation):
    created = master_edit.create_part(db, pn_std="HTTP-DEADLOCK", description="600GB SAS HDD",
                                     force=True)
    baseline = state(created["id"])
    precommit_audits = []

    def fail_commit(session):
        if session.in_nested_transaction():
            return  # 新建 PN 的保存点先正常完成，仅在整次人工命令的最终提交时失败。
        precommit_audits.append(session.scalar(select(func.count()).select_from(SysAuditLog)))
        # 真实 PG 40P01，模拟尚未适配的其他写路径在最终提交点造成死锁。
        session.execute(text("DO $$ BEGIN RAISE EXCEPTION 'simulated deadlock' "
                             "USING ERRCODE = '40P01'; END $$"))

    event.listen(Session, "before_commit", fail_commit)
    try:
        http = TestClient(app, raise_server_exceptions=False)
        result = (http.post("/api/parts/master", headers=headers(), json={
            "pn_std": "HTTP-DEADLOCK-NEW", "description": "2TB SAS HDD",
            "brand": "不得半提交品牌", "force": True}) if operation == "create" else http.patch(
                "/api/parts/master", headers=headers(), json={
                    "part_id": baseline["part_id"], "pn_std": baseline["pn_std"],
                    "base_snapshot": baseline, "description": "2TB SAS HDD", "brand": "不得半提交品牌"}))
    finally:
        event.remove(Session, "before_commit", fail_commit)
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "concurrent_write"
    assert precommit_audits == [2]  # 原 create + 本次完整命令，证明审计已 flush 后才注入故障。
    assert state(created["id"]) == baseline
    assert client.get("/api/parts/overview", headers=headers(), params={
        "pn_std": "HTTP-DEADLOCK-NEW"}).status_code == 404
    history = client.get(f'/api/parts/master/{created["id"]}/edit-history', headers=headers())
    assert len(history.json()["items"]) == 1


def test_create_reports_pn_length_limit(db):
    result = client.post("/api/parts/master", headers=headers(), json={"pn_std": "X" * 129})
    assert result.status_code == 400, result.text
    assert result.json()["detail"] == {
        "code": "validation_error", "message": "PN 不能超过 128 个字符"}


def test_create_rejects_unknown_fields(db):
    result = client.post("/api/parts/master", headers=headers(), json={
        "pn_std": "HTTP-UNKNOWN", "operated_by": "自报用户"})
    assert result.status_code == 422


def test_equal_auto_spec_can_be_confirmed_manually_from_old_baseline(db):
    created = master_edit.create_part(db, pn_std="HTTP-AUTO-CONVERGE", description="600GB SAS HDD",
                                     force=True)
    baseline = state(created["id"])
    assert patch(baseline, description="2TB SAS HDD").status_code == 200
    assert state(created["id"])["specs"]["capacity"]["source"] == "auto"
    result = patch(baseline, spec_updates={"capacity": {
        "value": "2TB", "unit": "GB", "numeric_value": "2000"}})
    assert result.status_code == 200, result.text
    assert state(created["id"])["specs"]["capacity"]["source"] == "manual"
