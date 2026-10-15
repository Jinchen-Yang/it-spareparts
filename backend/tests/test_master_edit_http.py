"""旧人工 HTTP 编辑入口须带稳定身份和字段基线。"""
from fastapi.testclient import TestClient

from app import permissions
from app.auth import _make_token
from app.main import app
from app.services import master_edit, merge, part_rename

client = TestClient(app)


def headers(role="purchaser"):
    token, _ = _make_token(role, "test-editor", "真实编辑人", perms=permissions.effective(role, None))
    return {"Authorization": f"Bearer {token}"}


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
