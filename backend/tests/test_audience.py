"""M8 反例矩阵、分组实时性、归属和批量脱敏契约。"""
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from app.models.fields_audience import AudienceGroup, AudienceRule, AudienceRuleMember, FieldDef
from app.models.system import SysUser
from app.security import UserContext
from app.services import audience


def _viewer(name):
    return UserContext(user_id=name, role="test", is_authenticated=True)


def _bypass(viewer):
    return viewer.user_id == "supervisor"


@pytest.fixture()
def people(db):
    names = ["author", "member", "group-member", "excluded", "unrelated", "supervisor"]
    db.add_all([SysUser(username=name, password_hash="unused", role="readonly") for name in names])
    db.flush()
    return {name: _viewer(name) for name in names}


@pytest.fixture()
def group(db, people):
    return audience.create_group(db, "常用分组", "personal", ["group-member"], people["author"], bypass=_bypass)


@pytest.mark.parametrize(("mode", "expected"), [
    ("public", [True, True, True, True, True, True]),
    ("private", [True, False, False, False, False, True]),
    ("include", [True, True, True, False, False, True]),
    ("exclude", [True, True, False, False, True, True]),
])
def test_visibility_counterexample_matrix(db, people, group, mode, expected):
    members = ["member"] if mode == "include" else ["author", "excluded", "supervisor"]
    rule = audience.create_rule(
        db, mode, members if mode in {"include", "exclude"} else [],
        [group.id] if mode in {"include", "exclude"} else [], people["author"],
    )
    for (name, viewer), allowed in zip(people.items(), expected, strict=True):
        assert audience.can_see(db, rule, viewer, author_id="author", bypass=_bypass) is allowed, name


@pytest.mark.parametrize(("mode", "user_ids", "group_ids"), [
    ("invalid", [], []), ("public", ["member"], []), ("private", [], [1]),
    ("include", [], []), ("exclude", [], []), ("include", ["ghost"], []),
    ("include", [], [-1]), ("include", [], [True]),
])
def test_invalid_rules_rejected_without_writing(db, people, mode, user_ids, group_ids):
    with pytest.raises(audience.AudienceError):
        audience.create_rule(db, mode, user_ids, group_ids, people["author"])
    assert list(db.scalars(select(AudienceRule))) == []


def test_unknown_rule_fails_closed_but_author_and_bypass_still_see(db, people):
    assert not audience.can_see(db, 99999, people["member"], author_id="author", bypass=_bypass)
    assert audience.can_see(db, 99999, people["author"], author_id="author", bypass=_bypass)
    assert audience.can_see(db, 99999, people["supervisor"], author_id="author", bypass=_bypass)
    assert audience.can_see(db, None, people["unrelated"], author_id="author", bypass=_bypass)


def test_empty_exclusion_rule_fails_closed(db, people):
    # 模拟绕过服务写入的残缺规则；空 exclude 不能意外变成公开。
    rule = AudienceRule(mode="exclude", owner_id="author")
    db.add(rule)
    db.flush()
    assert not audience.can_see(db, rule.id, people["unrelated"], author_id="author", bypass=_bypass)


def test_absent_viewer_and_author_identity_do_not_bypass_or_skip_public_rule(db, people):
    public = audience.create_rule(db, "public", [], [], people["author"])
    private = audience.create_rule(db, "private", [], [], people["author"])
    viewer = UserContext(user_id=None, role="readonly")
    rules = {"public": (public, None), "private": (private, None)}
    record = {"public": "公开值", "private": "私密值", "default-public": "无规则"}
    assert audience.can_see(db, public, viewer, author_id=None, bypass=_bypass)
    assert not audience.can_see(db, private, viewer, author_id=None, bypass=_bypass)
    assert audience.filter_fields(db, [record], viewer, field_rules=rules, bypass=_bypass) == [
        {"public": "公开值", "default-public": "无规则"},
    ]


def test_current_members_and_archived_groups_remain_effective(db, people, group):
    include = audience.create_rule(db, "include", [], [group.id], people["author"])
    exclude = audience.create_rule(db, "exclude", [], [group.id], people["author"])
    assert audience.can_see(db, include, people["group-member"], author_id="author", bypass=_bypass)
    assert not audience.can_see(db, exclude, people["group-member"], author_id="author", bypass=_bypass)
    audience.update_group(db, group.id, people["author"], user_ids=["unrelated"], bypass=_bypass)
    assert not audience.can_see(db, include, people["group-member"], author_id="author", bypass=_bypass)
    assert audience.can_see(db, exclude, people["group-member"], author_id="author", bypass=_bypass)
    assert audience.can_see(db, include, people["unrelated"], author_id="author", bypass=_bypass)
    assert not audience.can_see(db, exclude, people["unrelated"], author_id="author", bypass=_bypass)
    audience.archive_group(db, group.id, people["author"], bypass=_bypass)
    assert audience.can_see(db, include, people["unrelated"], author_id="author", bypass=_bypass)
    assert not audience.can_see(db, exclude, people["unrelated"], author_id="author", bypass=_bypass)
    with pytest.raises(audience.AudienceError, match="归档"):
        audience.create_rule(db, "include", [], [group.id], people["author"])
    assert audience.list_groups(db, people["author"]) == []


def test_filter_removes_keys_and_batches_regardless_of_record_count(db, people, group):
    private = audience.create_rule(db, "private", [], [], people["author"])
    include = audience.create_rule(db, "include", [], [group.id], people["author"])
    exclude = audience.create_rule(db, "exclude", [], [group.id], people["author"])
    rules = {"private": (private, "author"), "include": (include, "author"),
             "exclude": (exclude, "author"), 41: (private, "group-member")}
    original = {"private": "秘密", "include": "可见", "exclude": "隐藏", "plain": "原样", 41: "作者可见"}
    counts = []

    def counted(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            counts[-1] += 1

    event.listen(db.get_bind(), "before_cursor_execute", counted)
    try:
        for size in [1, 100]:
            counts.append(0)
            result = audience.filter_fields(db, [original] * size, people["group-member"], field_rules=rules, bypass=_bypass)
            assert result == [{"include": "可见", "plain": "原样", 41: "作者可见"}] * size
            assert "private" in original and "exclude" in original
        assert counts == [1, 1]
        counts.append(0)
        assert audience.filter_fields(db, [original], people["supervisor"], field_rules=rules, bypass=_bypass) == [original]
        assert counts[-1] == 0
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", counted)


def test_group_permissions_and_private_group_discovery(db, people, group):
    for viewer in [people["unrelated"], people["supervisor"]]:
        with pytest.raises(audience.AudienceError) as exc:
            audience.update_group(db, group.id, viewer, name="越权", bypass=_bypass)
        assert exc.value.status_code == 403
        with pytest.raises(audience.AudienceError):
            audience.archive_group(db, group.id, viewer, bypass=_bypass)
    with pytest.raises(audience.AudienceError):
        audience.create_group(db, "公共", "public", [], people["author"], bypass=_bypass)
    public = audience.create_group(db, "公共", "public", [], people["supervisor"], bypass=_bypass)
    assert audience.list_groups(db, people["unrelated"]) == [public]
    assert {item.id for item in audience.list_groups(db, people["author"])} == {group.id, public.id}
    with pytest.raises(audience.AudienceError):
        audience.create_rule(db, "include", [], [group.id], people["unrelated"])
    assert audience.create_rule(db, "include", [], [public.id], people["unrelated"])
    with pytest.raises(audience.AudienceError):
        audience.update_group(db, public.id, people["author"], user_ids=["member"], bypass=_bypass)
    audience.update_group(db, public.id, people["supervisor"], user_ids=["member"], bypass=_bypass)


def test_group_name_unique_until_archived_and_invalid_members_rejected(db, people, group):
    with pytest.raises(audience.AudienceError) as exc:
        audience.create_group(db, group.name, "personal", [], people["author"], bypass=_bypass)
    assert exc.value.status_code == 409
    audience.archive_group(db, group.id, people["author"], bypass=_bypass)
    replacement = audience.create_group(db, group.name, "personal", [], people["author"], bypass=_bypass)
    assert replacement.id != group.id
    inactive = SysUser(username="inactive", password_hash="unused", role="readonly", is_active=False)
    db.add(inactive)
    db.flush()
    for users in [["ghost"], ["inactive"]]:
        with pytest.raises(audience.AudienceError):
            audience.update_group(db, replacement.id, people["author"], user_ids=users, bypass=_bypass)
    for name in ["", "  ", "组" * 65]:
        with pytest.raises(audience.AudienceError):
            audience.create_group(db, name, "personal", [], people["author"], bypass=_bypass)


def test_member_target_database_constraint(db, people, group):
    rule = audience.create_rule(db, "public", [], [], people["author"])
    for user_id, group_id in [(None, None), ("member", group.id)]:
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(AudienceRuleMember(rule_id=rule, user_id=user_id, group_id=group_id))
            db.flush()


def test_team_field_nullable_owner_still_has_unique_active_name(db):
    def definition():
        return FieldDef(owner_scope="team", owner_id=None, subject_type="demo", level="line",
                        name="统一字段", value_type="text")

    first = definition()
    db.add(first)
    db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(definition())
        db.flush()
    first.archived = True
    db.flush()
    db.add(definition())
    db.flush()
