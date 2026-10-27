"""假业务 demo：逐格预览、权限复查、事务回滚和首次回执重放。"""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier
from types import SimpleNamespace

from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest
from sqlalchemy import select

from app import permissions
from app.auth import _make_token
from app.db import SessionLocal
from app.main import app
from app.models.system import SysAccessLog, SysUser
from app.models.workbook import WorkbookOperation
from app.security import UserContext
from app.services.workbook import operations, registry
from app.services.workbook.export import export_workbook
from app.services.workbook.parse import CellResult, ParsedRow, ParseResult
from app.services.workbook.plan import plan
from app.services.workbook.registry import ApplyFailed, Conflict, Forbidden, WorkbookSpec
from app.services.workbook.schema import ColumnSpec, SheetSpec, WorkbookError, WorkbookSchema


def _ctx(user):
    return UserContext(user.username, user.role,
                       permissions=permissions.effective_for_user(user), is_authenticated=True)


def _edit(content, fn):
    wb = load_workbook(BytesIO(content))
    fn(wb["明细"])
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


@pytest.fixture()
def demo(db, monkeypatch):
    monkeypatch.setattr(registry, "_specs", {})
    users = [SysUser(username=name, display_name=label, password_hash="测试不登录", role="readonly",
                     template_perms={"data_supplier": True}, perm_overrides={})
             for name, label in (("operator", "操作人"), ("peer", "其他人"),
                                 ("demo-a", "原值 A"), ("demo-b", "原值 B"))]
    db.add_all(users)
    db.commit()
    owner, peer, a, b = users
    schema = WorkbookSchema("demo", 1, [SheetSpec("items", "明细", [
        ColumnSpec("pn", "编码", "code", "locked", "文本编码"),
        ColumnSpec("name", "名称", "text", "required", "必填名称"),
        ColumnSpec("private", "内部备注", "text", "optional", "内部字段", field_group="data_supplier"),
    ], allow_insert=True, allow_delete=True, custom_columns=True)])
    state = SimpleNamespace(schema=schema, owner=owner, peer=peer, a=a, b=b,
                            allowed=True, calls=0, applied_rows=[], fail=False)

    def load_current(db, sheet, ids, ctx):
        assert sheet == "items"
        records = db.scalars(select(SysUser).where(SysUser.id.in_(ids)).with_for_update()
                             .execution_options(populate_existing=True)).all()
        return {record.id: (record.token_version, {
            "pn": record.username, "name": record.display_name,
            "private": record.salesperson_name,
        }) for record in records}

    def apply_fn(db, comparison, ctx):
        state.calls += 1
        state.applied_rows = comparison.rows
        for index, row in enumerate(comparison.rows):
            if row["status"] == "added":
                db.add(SysUser(username=f"inserted-{index}", display_name=row["values"]["name"],
                               password_hash="unused", role="readonly"))
            else:
                record = db.get(SysUser, row["entity_id"])
                if row["status"] == "deleted":
                    db.delete(record)
                else:
                    record.display_name = row["values"]["name"]
                    record.token_version += 1
            db.flush()
            if state.fail:
                raise ValueError("假业务中途失败")
        return {"message": "整份已应用", "rows": len(comparison.rows), "marker": "首次结果"}

    state.spec = WorkbookSpec(schema, load_current, apply_fn,
                              lambda db, ctx, subject_type, subject_id: state.allowed)
    registry.register("demo", state.spec)

    def export(*, export_id="demo-export", ids=None, visible_columns=None):
        records = [a, b] if ids is None else ids
        return export_workbook(schema, {"items": [{
            "entity_id": record.id, "base_version": record.token_version,
            "values": {"pn": record.username, "name": record.display_name,
                       "private": record.salesperson_name},
        } for record in records]}, export_id=export_id, actor=owner.username,
            visible_columns=visible_columns, ctx=_ctx(owner))

    state.export = export
    return state


def test_plan_all_statuses_and_only_actual_cell_changes():
    exported = lambda number, entity, version, value: ParsedRow(
        "items", number, "exported", entity, version, f"token-{entity}",
        cells={"name": CellResult(value, value, header="名称")}, editable_keys=["name"])
    parsed = ParseResult("demo", "1", "export", "sha", [
        exported(2, 1, 1, "新值"), exported(3, 2, 1, "不变"),
        exported(4, 3, 1, "冲突"), ParsedRow("items", 5, "added", cells={
            "name": CellResult("新增", "新增", header="名称")}, editable_keys=["name"]),
        ParsedRow("items", 6, "deleted", 4, 1, "token-4", baseline={"name": "删除"}),
    ])
    current = {1: (1, {"name": "旧值", "private": "不返回"}), 2: (1, {"name": "不变"}),
               3: (2, {"name": "服务端新值"}), 4: (1, {"name": "删除", "private": "不返回"})}
    result = plan(parsed, lambda sheet, ids: current)
    assert result.counts == {"added": 1, "modified": 1, "unchanged": 1, "deleted": 1, "conflict": 1}
    assert result.rows[0]["changes"] == [{"key": "name", "header": "名称", "before": "旧值", "after": "新值"}]
    assert result.rows[1]["changes"] == []
    assert result.rows[-1]["values"] == {"name": "删除"}


def test_preview_apply_and_replay_never_rerun_business(db, demo):
    content = _edit(demo.export(), lambda sheet: setattr(sheet["C2"], "value", "改后 A"))
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    db.commit()
    assert before["plan"]["counts"]["modified"] == 1
    result = operations.apply(db, before["operation_id"], _ctx(demo.owner), before["plan_digest"])
    db.commit()
    assert not result["replayed"]
    assert demo.calls == 1
    assert db.get(SysUser, demo.a.id).display_name == "改后 A"
    # 服务器已继续变化，仍返回首次回执，不能重新对比并再次调用业务。
    demo.a.display_name = "后续修改"
    db.commit()
    replay = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    assert replay["operation_id"] == before["operation_id"]
    assert replay["replayed"] and replay["response"] == result["response"]
    again = operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"])
    assert again["replayed"] and again["response"] == result["response"]
    assert demo.calls == 1
    assert db.get(SysUser, demo.a.id).display_name == "后续修改"
    assert set(db.scalars(select(SysAccessLog.action))) >= {"workbook_preview", "workbook_apply", "workbook_replay"}


@pytest.mark.parametrize("confirm", [False, True])
def test_delete_candidates_require_each_key_and_unconfirmed_are_excluded(db, demo, confirm):
    content = _edit(demo.export(), lambda sheet: sheet.delete_rows(2, 1))
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    db.commit()
    deleted = next(row for row in before["plan"]["rows"] if row["status"] == "deleted")
    result = operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"],
                              confirm_deletes=[deleted["delete_key"]] if confirm else [])
    db.commit()
    assert result["response"]["rows"] == int(confirm)
    assert (db.get(SysUser, demo.a.id) is None) == confirm


def test_delete_confirmation_only_applies_the_selected_candidate(db, demo):
    content = _edit(demo.export(), lambda sheet: sheet.delete_rows(2, 2))
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    db.commit()
    candidates = [row for row in before["plan"]["rows"] if row["status"] == "deleted"]
    assert len(candidates) == 2
    selected = candidates[0]
    unselected = candidates[1]
    operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"],
                      confirm_deletes=[selected["delete_key"]])
    db.commit()
    assert db.get(SysUser, selected["entity_id"]) is None
    assert db.get(SysUser, unselected["entity_id"]) is not None


def test_unknown_delete_keys_and_digest_are_rejected(db, demo):
    before = operations.preview(db, "demo", demo.export(), ctx=_ctx(demo.owner))
    db.commit()
    with pytest.raises(Conflict, match="预览内容"):
        operations.apply(db, before["operation_id"], demo.owner.id, "0" * 64)
    with pytest.raises(WorkbookError, match="不属于"):
        operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"], confirm_deletes=["fake"])
    assert demo.calls == 0


def test_conflict_and_post_preview_version_change_block_all_rows(db, demo):
    content = _edit(demo.export(), lambda sheet: setattr(sheet["C2"], "value", "改后 A"))
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    db.commit()
    demo.a.token_version += 1
    db.commit()
    with pytest.raises(Conflict, match="预览后"):
        operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"])
    assert demo.calls == 0
    other = operations.preview(db, "demo", _edit(content, lambda sheet: setattr(sheet["C3"], "value", "改后 B")), ctx=_ctx(demo.owner))
    assert other["plan"]["counts"]["conflict"] == 1
    with pytest.raises(Conflict, match="版本冲突"):
        operations.apply(db, other["operation_id"], demo.owner.id, other["plan_digest"])


def test_mid_apply_failure_rolls_back_all_changes_and_records_failed(db, demo):
    content = _edit(demo.export(), lambda sheet: [setattr(sheet[f"C{n}"], "value", f"改后 {n}") for n in (2, 3)])
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    db.commit()
    demo.fail = True
    with pytest.raises(ApplyFailed, match="整份业务更改已回滚"):
        operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"])
    db.commit()
    assert db.get(SysUser, demo.a.id).display_name == "原值 A"
    assert db.get(SysUser, demo.b.id).display_name == "原值 B"
    operation = db.get(WorkbookOperation, before["operation_id"])
    assert operation.status == "failed"
    assert operation.response["error_type"] == "ValueError"


def test_callback_cannot_commit_partial_batch(db, demo):
    content = _edit(demo.export(), lambda sheet: setattr(sheet["C2"], "value", "改后 A"))
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    db.commit()

    def bad_callback(session, comparison, ctx):
        session.get(SysUser, demo.a.id).display_name = "提前提交"
        session.flush()
        session.commit()
        return {}

    with pytest.raises(ApplyFailed):
        operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"], bad_callback)
    db.commit()
    assert db.get(SysUser, demo.a.id).display_name == "原值 A"


def test_error_rows_and_foreign_actor_cannot_apply(db, demo):
    content = _edit(demo.export(), lambda sheet: setattr(sheet["C2"], "value", None))
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    db.commit()
    assert before["plan"]["errors"]
    with pytest.raises(Forbidden, match="本人"):
        operations.apply(db, before["operation_id"], demo.peer.id, before["plan_digest"])
    with pytest.raises(WorkbookError, match="存在错误"):
        operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"])


def test_permission_revocation_blocks_pending_and_replayed_plan(db, demo):
    content = demo.export()
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    db.commit()
    demo.allowed = False
    with pytest.raises(Forbidden):
        operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"])
    demo.allowed = True
    demo.owner.perm_overrides = {"data_supplier": False}
    db.commit()
    with pytest.raises(Forbidden, match="字段权限已变更"):
        operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    with pytest.raises(Forbidden, match="字段权限已变更"):
        operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"])


def test_added_rows_are_applied_and_unselected_columns_not_cleared(db, demo):
    content = _edit(demo.export(visible_columns={"items": ["name"]}),
                    lambda sheet: setattr(sheet["C4"], "value", "新增名称"))
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    assert before["plan"]["counts"]["added"] == 1
    assert all("private" not in row["values"] for row in before["plan"]["rows"])
    operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"])
    db.commit()
    assert db.scalar(select(SysUser).where(SysUser.username == "inserted-0")).display_name == "新增名称"


def test_optional_subject_scope_reaches_business_callback_and_cannot_be_rebound(db, demo):
    content = demo.export()
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner), subject_type="demo", subject_id=7)
    assert before["plan"]["subject_type"] == "demo"
    assert before["plan"]["subject_id"] == 7
    seen = []
    operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"],
                      lambda session, comparison, ctx: seen.append(
                          (comparison.subject_type, comparison.subject_id)) or {"ok": True})
    db.commit()
    assert seen == [("demo", 7)]
    with pytest.raises(Conflict, match="关联其他"):
        operations.preview(db, "demo", content, ctx=_ctx(demo.owner), subject_type="demo", subject_id=8)


def test_generic_api_schema_preview_apply_and_failed_state(db, demo):
    token, _ = _make_token(demo.owner.role, demo.owner.username, demo.owner.display_name,
                           perms=permissions.effective_for_user(demo.owner), token_version=demo.owner.token_version)
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    schema = client.get("/api/workbooks/demo/schema")
    assert schema.status_code == 200
    assert schema.json()["name"] == "demo"
    content = _edit(demo.export(), lambda sheet: setattr(sheet["C2"], "value", "接口修改"))
    result = client.post("/api/workbooks/demo/preview", files={"file": ("demo.xlsx", content)})
    assert result.status_code == 200, result.text
    before = result.json()
    demo.fail = True
    result = client.post(f"/api/workbooks/operations/{before['operation_id']}/apply", json={
        "expected_plan_digest": before["plan_digest"], "confirm_deletes": [],
    })
    assert result.status_code == 422
    db.expire_all()
    assert db.get(WorkbookOperation, before["operation_id"]).status == "failed"
    assert db.get(SysUser, demo.a.id).display_name == "原值 A"
    assert client.post("/api/workbooks/demo/preview", files={"file": ("old.xls", content)}).status_code == 422


def test_preview_duplicate_preserves_original_plan_and_schema_version_changes_block(db, demo):
    content = demo.export()
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    db.commit()
    again = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    assert again == before
    changed_schema = WorkbookSchema("demo", 2, demo.schema.sheets)
    registry.unregister("demo")
    registry.register("demo", WorkbookSpec(changed_schema, demo.spec.load_current,
                                           demo.spec.apply_fn, demo.spec.can_access))
    with pytest.raises(Conflict, match="模板版本已更新"):
        operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"])


def test_applied_file_replays_first_receipt_after_schema_upgrade(db, demo):
    content = demo.export()
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    first = operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"])
    db.commit()
    registry.unregister("demo")
    registry.register("demo", WorkbookSpec(WorkbookSchema("demo", 2, demo.schema.sheets),
                                           demo.spec.load_current, demo.spec.apply_fn, demo.spec.can_access))
    replay = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    assert replay["replayed"]
    assert replay["response"] == first["response"]
    assert demo.calls == 1


def test_historical_removed_fields_fail_closed_before_replaying(db, demo):
    content = demo.export()
    before = operations.preview(db, "demo", content, ctx=_ctx(demo.owner))
    operations.apply(db, before["operation_id"], demo.owner.id, before["plan_digest"])
    db.commit()
    old = demo.schema.sheets[0]
    new_sheet = SheetSpec(old.key, old.title, [c for c in old.columns if c.key != "private"],
                          old.allow_insert, old.allow_delete, old.custom_columns)
    registry.unregister("demo")
    registry.register("demo", WorkbookSpec(WorkbookSchema("demo", 2, [new_sheet]),
                                           demo.spec.load_current, demo.spec.apply_fn, demo.spec.can_access))
    with pytest.raises(Forbidden, match="无法确认历史字段权限"):
        operations.preview(db, "demo", content, ctx=_ctx(demo.owner))


def test_simultaneous_previews_and_applies_run_business_exactly_once(db, demo):
    content = _edit(demo.export(), lambda sheet: setattr(sheet["C2"], "value", "并发修改"))
    ctx = _ctx(demo.owner)
    db.commit()
    barrier = Barrier(2)

    def concurrent_preview():
        with SessionLocal() as session:
            barrier.wait(timeout=15)
            result = operations.preview(session, "demo", content, ctx=ctx)
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        previews = list(pool.map(lambda _: concurrent_preview(), range(2)))
    assert previews[0]["operation_id"] == previews[1]["operation_id"]
    barrier = Barrier(2)

    def concurrent_apply():
        with SessionLocal() as session:
            barrier.wait(timeout=15)
            result = operations.apply(session, previews[0]["operation_id"], ctx, previews[0]["plan_digest"])
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: concurrent_apply(), range(2)))
    assert sorted(result["replayed"] for result in results) == [False, True]
    assert results[0]["response"] == results[1]["response"]
    assert demo.calls == 1
