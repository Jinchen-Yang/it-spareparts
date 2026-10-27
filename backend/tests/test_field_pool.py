"""M7 契约：字段复用、层级隔离、类型上限与 M8 组合。"""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.models.fields_audience import FieldAnchorUsage, FieldDef, FieldValue
from app.models.system import SysUser
from app.security import UserContext
from app.services import audience, field_pool as pool


@pytest.fixture()
def users(db):
    contexts = {name: UserContext(user_id=name, role="sales") for name in ("author", "other", "third")}
    db.add_all([SysUser(username=name, password_hash="test", role="sales", is_active=True)
                for name in contexts])
    db.flush()
    return contexts


def define(db, viewer, name="备注", **kwargs):
    return pool.define_field(db, subject_type="demo", level="line", name=name, viewer=viewer, **kwargs)


def subject(subject_id="1", level="line"):
    return pool.Subject("demo", subject_id, level)


def test_defs_merge_override_order_and_archive(db, users):
    me = users["author"]
    personal = define(db, me, "同名", sort=-100)
    define(db, users["other"], "别人")
    team = define(db, me, "同名", owner_scope="team", sort=2)
    first = define(db, me, "先显示", sort=-1)
    pool.define_field(db, subject_type="other", level="line", name="隔离", viewer=me)
    pool.define_field(db, subject_type="demo", level="header", name="单头", viewer=me)
    assert [f.id for f in pool.list_defs(db, "demo", "line", me)] == [first.id, team.id]
    with pytest.raises(PermissionError):
        pool.archive_field(db, personal, users["other"])
    pool.archive_field(db, team, me)
    assert [f.id for f in pool.list_defs(db, "demo", "line", me)] == [personal.id, first.id]
    with pytest.raises(ValueError):
        define(db, me, "同名")
    pool.archive_field(db, personal, me)
    assert define(db, me, "同名").id != personal.id


def test_template_union_anchor_boundary_and_current_viewer(db, users, monkeypatch):
    me = users["author"]
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    monkeypatch.setattr(pool, "_now", lambda: now)
    auto = define(db, me, "自动", sort=2)
    edge = define(db, me, "边界", auto_include=False, sort=1)
    old = define(db, me, "过期", auto_include=False)
    wrong_anchor = define(db, me, "其他PN", auto_include=False)
    wrong_user = define(db, me, "其他用户", auto_include=False)
    for field, anchor, user, delta in (
        (edge, "part:1", "author", timedelta(days=180)),
        (old, "part:1", "author", timedelta(days=180, microseconds=1)),
        (wrong_anchor, "part:2", "author", timedelta(days=1)),
        (wrong_user, "part:1", "other", timedelta(days=1)),
        (auto, "part:1", "author", timedelta(days=1)),
    ):
        db.add(FieldAnchorUsage(field_def_id=field.id, anchor=anchor, user_id=user,
                                last_used_at=now - delta, uses=1))
    db.flush()
    assert [f.id for f in pool.template_for(db, "demo", "line", me, "part:1")] == [edge.id, auto.id]
    assert [f.id for f in pool.template_for(db, "demo", "line", me)] == [auto.id]
    pool.archive_field(db, edge, me)
    assert [f.id for f in pool.template_for(db, "demo", "line", me, "part:1")] == [auto.id]


def test_new_name_reuse_empty_delete_and_anchor_counts(db, users):
    me = users["author"]
    pool.save_values(db, subject(), [{"new_name": "特殊要求", "value": "加急"}], me, "part:1")
    field = pool.list_defs(db, "demo", "line", me)[0]
    assert field.auto_include
    pool.save_values(db, subject("2"), [{"new_name": "特殊要求", "value": "常规"}], me, "part:1")
    assert len(pool.list_defs(db, "demo", "line", me)) == 1
    usage = db.get(FieldAnchorUsage, (field.id, "part:1", "author"))
    db.refresh(usage)
    assert usage.uses == 2
    assert usage.last_used_at is not None
    pool.save_values(db, subject(), [{"field_def_id": field.id, "value": ""}], me)
    pool.save_values(db, subject("2"), [{"field_def_id": field.id, "value": None}], me)
    pool.save_values(db, subject(), [{"new_name": "空框", "value": ""}], me)
    assert db.scalar(select(func.count()).select_from(FieldValue)) == 0
    assert len(pool.list_defs(db, "demo", "line", me)) == 2
    assert len(pool.template_for(db, "demo", "line", me, "part:1")) == 2


@pytest.mark.parametrize(("value_type", "choices", "valid", "invalid"), [
    ("text", None, "普通文本", {"nested": True}),
    ("number", None, "-1.25e3", "NaN"),
    ("number", None, "0", "Infinity"),
    ("number", None, "1", "abc"),
    ("date", None, "2024-02-29", "2026-02-29"),
    ("date", None, "2026-10-25", "20261025"),
    ("bool", None, False, "yes"),
    ("choice", ["A", "B"], "A", "C"),
])
def test_type_validation(db, users, value_type, choices, valid, invalid):
    me = users["author"]
    field = define(db, me, value_type=value_type, choices=choices)
    pool.save_values(db, subject(), [{"field_def_id": field.id, "value": valid}], me)
    before = db.scalar(select(FieldValue.value))
    with pytest.raises(ValueError):
        pool.save_values(db, subject(), [{"field_def_id": field.id, "value": invalid}], me)
    assert db.scalar(select(FieldValue.value)) == before


def test_name_and_value_lengths(db, users):
    me = users["author"]
    for name in ("", " ", "字" * 33):
        with pytest.raises(ValueError):
            define(db, me, name)
    field = define(db, me, "字" * 32)
    pool.save_values(db, subject(), [{"field_def_id": field.id, "value": "值" * 2000}], me)
    with pytest.raises(ValueError):
        pool.save_values(db, subject(), [{"field_def_id": field.id, "value": "值" * 2001}], me)
    assert len(db.scalar(select(FieldValue.value))) == 2000


def test_twenty_per_subject_level_atomic_patch(db, users):
    me = users["author"]
    entries = [{"new_name": f"字段{i}", "value": str(i)} for i in range(20)]
    pool.save_values(db, subject(), entries, me)
    assert db.scalar(select(func.count()).select_from(FieldValue)) == 20
    with pytest.raises(ValueError, match="最多 20"):
        pool.save_values(db, subject(), [{"new_name": "第21个", "value": "x"}], me)
    assert db.scalar(select(func.count()).select_from(FieldValue)) == 20
    assert "第21个" not in [f.name for f in pool.list_defs(db, "demo", "line", me)]
    first = pool.list_defs(db, "demo", "line", me)[0]
    pool.save_values(db, subject(), [{"field_def_id": first.id, "value": None},
                                   {"new_name": "换一个", "value": "x"}], me)
    pool.save_values(db, subject("2"), [{"new_name": "不同主体", "value": "x"}], me)
    pool.save_values(db, subject("1", "header"), [{"new_name": "单头字段", "value": "x"}], me)
    assert db.scalar(select(func.count()).select_from(FieldValue)) == 22


def test_visibility_rule_override_author_and_bypass(db, users):
    me, other = users["author"], users["other"]
    private = audience.create_rule(db, "private", [], [], me)
    default = define(db, me, "默认私密", default_rule_id=private)
    override = define(db, me, "覆盖公开", default_rule_id=private)
    excluded = define(db, me, "覆盖排除")
    pool.save_values(db, subject(), [
        {"field_def_id": default.id, "value": "secret"},
        {"field_def_id": override.id, "value": "visible", "rule": {"mode": "public"}},
        {"field_def_id": excluded.id, "value": "hidden", "rule": {"mode": "exclude", "user_ids": ["other"]}},
    ], me)
    read = lambda viewer, bypass=lambda _: False: pool.read_values(db, [subject()], viewer, bypass)["1"]
    assert [v["name"] for v in read(other)] == ["覆盖公开"]
    assert len(read(me)) == 3
    assert len(read(other, lambda _: True)) == 3
    pool.save_values(db, subject(), [{"field_def_id": override.id, "value": "again"}], me)
    assert [v["name"] for v in read(other)] == ["覆盖公开"]
    pool.save_values(db, subject(), [{"field_def_id": override.id, "value": "again", "rule": None}], me)
    assert read(other) == []
    pool.archive_field(db, default, me)
    assert len(read(me)) == 3


def test_read_is_batched_across_subjects(db, users):
    me, other = users["author"], users["other"]
    field = define(db, me, default_rule_id=audience.create_rule(db, "private", [], [], me))
    for i in range(12):
        pool.save_values(db, subject(str(i)), [{"field_def_id": field.id, "value": str(i)}], me)
    queries = []
    def count(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)
    event.listen(db.bind, "before_cursor_execute", count)
    try:
        pool.read_values(db, [subject("0")], other, lambda _: False)
        one = len(queries)
        queries.clear()
        assert all(v == [] for v in pool.read_values(db, [subject(str(i)) for i in range(12)], other, lambda _: False).values())
        assert len(queries) == one == 2
    finally:
        event.remove(db.bind, "before_cursor_execute", count)


def test_suggestions_usage_names_scope_and_no_anchor(db, users):
    me = users["author"]
    rare = define(db, me, "要求低")
    popular = define(db, me, "要求高", owner_scope="team")
    define(db, users["other"], "要求私有")
    for field in (rare, popular, popular):
        pool.save_values(db, subject(), [{"field_def_id": field.id, "value": "v"}], me)
    assert pool.name_suggestions(db, "demo", "要求", me) == [
        {"field_def_id": popular.id, "name": "要求高", "uses": 2},
        {"field_def_id": rare.id, "name": "要求低", "uses": 1}]
    assert pool.name_suggestions(db, "demo", "无匹配", me) == []


def test_reject_cross_pool_subject_level_and_invalid_rule(db, users):
    me, other = users["author"], users["other"]
    field = define(db, me)
    with pytest.raises(ValueError):
        pool.save_values(db, subject(), [{"field_def_id": field.id, "value": "x"}], other)
    with pytest.raises(ValueError):
        pool.save_values(db, subject(level="header"), [{"field_def_id": field.id, "value": "x"}], me)
    other_rule = audience.create_rule(db, "private", [], [], other)
    with pytest.raises(ValueError):
        pool.save_values(db, subject(), [{"field_def_id": field.id, "value": "x", "rule": other_rule}], me)
    with pytest.raises(ValueError):
        pool.save_values(db, subject(), [{"field_def_id": field.id, "new_name": "重复", "value": "x"}], me)
    assert db.scalar(select(func.count()).select_from(FieldValue)) == 0


@pytest.mark.parametrize("choices", [None, [], ["A", "A"], [1], [""]])
def test_choice_definition_rejected(db, users, choices):
    with pytest.raises(ValueError):
        define(db, users["author"], value_type="choice", choices=choices)


def test_rename_keeps_history_and_type_changes_rejected(db, users):
    me = users["author"]
    field = define(db, me)
    pool.save_values(db, subject(), [{"field_def_id": field.id, "value": "保留"}], me)
    pool.update_field(db, field, name="新名称")
    assert pool.read_values(db, [subject()], me, lambda _: False)["1"] == [
        {"field_def_id": field.id, "name": "新名称", "value": "保留"}]
    with pytest.raises(ValueError, match="历史值"):
        pool.update_field(db, field, value_type="number")


def test_historical_archived_or_overridden_values_can_be_cleared(db, users):
    me = users["author"]
    archived = define(db, me, "归档字段")
    overridden = define(db, me, "同名覆盖")
    pool.save_values(db, subject(), [{"field_def_id": f.id, "value": "历史"}
                                    for f in (archived, overridden)], me)
    pool.archive_field(db, archived, me)
    define(db, me, "同名覆盖", owner_scope="team")
    with pytest.raises(ValueError):
        pool.save_values(db, subject(), [{"field_def_id": archived.id, "value": "重新写入"}], me)
    pool.save_values(db, subject(), [{"field_def_id": f.id, "value": None}
                                    for f in (archived, overridden)], me)
    assert pool.read_values(db, [subject()], me, lambda _: False) == {"1": []}


def test_concurrent_saves_cannot_exceed_twenty(db, users):
    me = users["author"]
    pool.save_values(db, subject(), [{"new_name": f"已有{i}", "value": "1"} for i in range(18)], me)
    db.commit()
    start = Barrier(2)
    def worker(prefix):
        with Session(db.get_bind()) as session:
            start.wait(timeout=10)
            try:
                pool.save_values(session, subject(), [{"new_name": f"{prefix}{i}", "value": "2"} for i in range(2)], me)
                session.commit()
                return "saved"
            except ValueError as exc:
                session.rollback()
                assert "最多 20" in str(exc)
                return "limit"
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(worker, ["A", "B"])) == ["limit", "saved"]
    assert db.scalar(select(func.count()).select_from(FieldValue)) == 20


def test_save_refreshes_cached_definition_after_another_session_changes_type(db, users):
    me = users["author"]
    field = define(db, me)
    field_id = field.id
    db.commit()
    assert pool.list_defs(db, "demo", "line", me)[0].value_type == "text"
    with Session(db.get_bind()) as session:
        other_copy = session.get(FieldDef, field_id)
        pool.update_field(session, other_copy, value_type="number")
        session.commit()
    with pytest.raises(ValueError, match="数字"):
        pool.save_values(db, subject(), [{"field_def_id": field_id, "value": "abc"}], me)
    assert db.scalar(select(func.count()).select_from(FieldValue)) == 0
