"""M8 受众可见性；主管判定由调用方传入，服务不认识业务权限键。"""
from collections.abc import Callable, Hashable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.fields_audience import AudienceGroup, AudienceGroupMember, AudienceRule, AudienceRuleMember
from app.models.system import SysUser
from app.security import UserContext

Bypass = Callable[[UserContext], bool]
MODES = {"public", "private", "include", "exclude"}


class AudienceError(ValueError):
    def __init__(self, message: str, *, status_code: int = 422, code: str = "invalid"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _identity(owner: UserContext | str) -> str:
    identity = owner.user_id if isinstance(owner, UserContext) else owner
    if not isinstance(identity, str) or not identity or len(identity) > 64:
        raise AudienceError("需要有效的实名账号", status_code=403, code="forbidden")
    return identity


def _users(db: Session, user_ids: Sequence[str]) -> list[str]:
    if any(not isinstance(uid, str) or not uid or len(uid) > 64 for uid in user_ids):
        raise AudienceError("成员账号格式无效")
    members = sorted(set(user_ids))
    if members:
        found = set(db.scalars(select(SysUser.username).where(
            SysUser.username.in_(members), SysUser.is_active.is_(True), SysUser.deleted_at.is_(None),
        )))
        if found != set(members):
            raise AudienceError("成员包含不存在或已停用的账号")
    return members


def _name(name: str) -> str:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
        raise AudienceError("分组名称须为 1–64 个字符")
    return name.strip()


def create_rule(
    db: Session, mode: str, user_ids: Sequence[str], group_ids: Sequence[int],
    owner: UserContext | str,
) -> int:
    owner_id = _identity(owner)
    if mode not in MODES:
        raise AudienceError("无效的可见性模式")
    if mode in {"public", "private"} and (user_ids or group_ids):
        raise AudienceError("公开或私密模式不能指定成员")
    if mode in {"include", "exclude"} and not (user_ids or group_ids):
        raise AudienceError("部分可见或不给谁看至少需要一个成员")
    if any(type(group_id) is not int or group_id <= 0 for group_id in group_ids):
        raise AudienceError("分组标识无效")
    members = _users(db, user_ids)
    groups = sorted(set(group_ids))
    if groups:
        # 与归档共用行锁，禁止归档动作和新建规则跨过彼此的校验。
        available = list(db.scalars(select(AudienceGroup).where(
            AudienceGroup.id.in_(groups), AudienceGroup.archived.is_(False),
            or_(AudienceGroup.scope == "public", AudienceGroup.owner_id == owner_id),
        ).order_by(AudienceGroup.id).with_for_update()))
        if {group.id for group in available} != set(groups):
            raise AudienceError("分组不存在、已归档或无权引用")
    rule = AudienceRule(mode=mode, owner_id=owner_id)
    db.add(rule)
    db.flush()
    db.add_all([AudienceRuleMember(rule_id=rule.id, user_id=uid) for uid in members])
    db.add_all([AudienceRuleMember(rule_id=rule.id, group_id=gid) for gid in groups])
    db.flush()
    return rule.id


def _rule_states(db: Session, rule_ids: set[int], viewer: UserContext) -> dict[int, tuple[str, bool, bool]]:
    """一次联表查询全部规则及当前查看者的分组成员关系，不缓存旧成员。"""
    if not rule_ids:
        return {}
    rows = db.execute(select(
        AudienceRule.id, AudienceRule.mode, AudienceRuleMember.id,
        AudienceRuleMember.user_id, AudienceRuleMember.group_id,
        AudienceGroup.id, AudienceGroupMember.user_id,
    ).outerjoin(AudienceRuleMember, AudienceRuleMember.rule_id == AudienceRule.id)
      .outerjoin(AudienceGroup, AudienceGroup.id == AudienceRuleMember.group_id)
      .outerjoin(AudienceGroupMember, and_(
          AudienceGroupMember.group_id == AudienceRuleMember.group_id,
          AudienceGroupMember.user_id == viewer.user_id,
      )).where(AudienceRule.id.in_(rule_ids))).all()
    states: dict[int, tuple[str, bool, bool]] = {}
    for rule_id, mode, member_id, user_id, group_id, existing_group, group_user in rows:
        previous = states.get(rule_id)
        matched = (previous[1] if previous else False) or bool(
            viewer.user_id and (user_id == viewer.user_id or group_user == viewer.user_id)
        )
        valid = mode in MODES and (previous[2] if previous else True)
        if mode in {"include", "exclude"}:
            valid = valid and member_id is not None and ((user_id is None) != (group_id is None))
            valid = valid and (group_id is None or existing_group is not None)
        else:
            valid = valid and member_id is None
        states[rule_id] = (mode, matched, valid)
    return states


def _visible(
    rule_id: int | None, author_id: str | None, viewer: UserContext,
    states: Mapping[int, tuple[str, bool, bool]], bypassed: bool,
) -> bool:
    if bypassed or (viewer.user_id is not None and viewer.user_id == author_id):
        return True
    if rule_id is None:
        return True
    state = states.get(rule_id)
    if state is None or not state[2]:
        return False
    mode, matched, _ = state
    return mode == "public" or (mode == "include" and matched) or (mode == "exclude" and not matched)


def can_see(
    db: Session, rule_id: int | None, viewer: UserContext, *,
    author_id: str | None, bypass: Bypass,
) -> bool:
    bypassed = bool(bypass(viewer))
    if bypassed or rule_id is None or (viewer.user_id is not None and viewer.user_id == author_id):
        return True
    return _visible(rule_id, author_id, viewer, _rule_states(db, {rule_id}, viewer), bypassed)


def filter_fields(
    db: Session, records: Sequence[Mapping[Hashable, Any]], viewer: UserContext, *,
    field_rules: Mapping[Hashable, tuple[int | None, str | None]], bypass: Bypass,
) -> list[dict[Hashable, Any]]:
    """返回新字典；受限字段整个移除，未受控字段保持原值。"""
    bypassed = bool(bypass(viewer))
    present = {key for record in records for key in record if key in field_rules}
    rule_ids = {
        field_rules[key][0] for key in present
        if field_rules[key][0] is not None
        and not (viewer.user_id is not None and field_rules[key][1] == viewer.user_id)
    }
    states = _rule_states(db, rule_ids, viewer) if not bypassed else {}
    visible = {
        key: _visible(*field_rules[key], viewer, states, bypassed)
        for key in present
    }
    return [{key: value for key, value in record.items() if visible.get(key, True)} for record in records]


def read_rules(
    db: Session, field_rules: Mapping[Hashable, tuple[int | None, str | None]],
    viewer: UserContext, *, bypass: Bypass,
) -> dict[Hashable, dict]:
    """只向各字段的作者或主管回读规则，批量补齐账号和分组名称。"""
    bypassed = bool(bypass(viewer))
    allowed = {key: rule_id for key, (rule_id, author) in field_rules.items()
               if bypassed or (viewer.user_id is not None and viewer.user_id == author)}
    rule_ids = {rule_id for rule_id in allowed.values() if rule_id is not None}
    views = {None: {"mode": "public", "users": [], "groups": []}}
    if rule_ids:
        rows = db.execute(select(
            AudienceRule.id, AudienceRule.mode, AudienceRuleMember.user_id,
            SysUser.display_name, AudienceRuleMember.group_id, AudienceGroup.name,
        ).outerjoin(AudienceRuleMember, AudienceRuleMember.rule_id == AudienceRule.id)
          .outerjoin(SysUser, SysUser.username == AudienceRuleMember.user_id)
          .outerjoin(AudienceGroup, AudienceGroup.id == AudienceRuleMember.group_id)
          .where(AudienceRule.id.in_(rule_ids))
          .order_by(AudienceRule.id, AudienceRuleMember.user_id, AudienceRuleMember.group_id))
        for rule_id, mode, username, display_name, group_id, group_name in rows:
            view = views.setdefault(rule_id, {"mode": mode, "users": [], "groups": []})
            if username is not None:
                view["users"].append({"id": username, "name": display_name or username})
            elif group_id is not None:
                view["groups"].append({"id": group_id, "name": group_name})
    return {key: views[rule_id] for key, rule_id in allowed.items() if rule_id in views}


def list_groups(db: Session, viewer: UserContext) -> list[AudienceGroup]:
    return list(db.scalars(select(AudienceGroup).where(
        AudienceGroup.archived.is_(False),
        or_(AudienceGroup.scope == "public", AudienceGroup.owner_id == viewer.user_id),
    ).order_by(AudienceGroup.name, AudienceGroup.id)))


def create_group(
    db: Session, name: str, scope: str, user_ids: Sequence[str], owner: UserContext,
    *, bypass: Bypass,
) -> AudienceGroup:
    owner_id = _identity(owner)
    if scope not in {"personal", "public"}:
        raise AudienceError("无效的分组范围")
    if scope == "public" and not bypass(owner):
        raise AudienceError("只有主管可以维护公共分组", status_code=403, code="forbidden")
    members = _users(db, user_ids)
    group = AudienceGroup(name=_name(name), scope=scope, owner_id=owner_id)
    try:
        with db.begin_nested():
            db.add(group)
            db.flush()
            db.add_all([AudienceGroupMember(group_id=group.id, user_id=uid) for uid in members])
            db.flush()
    except IntegrityError as exc:
        raise AudienceError("同名分组已存在", status_code=409, code="conflict") from exc
    return group


def _editable_group(db: Session, group_id: int, viewer: UserContext, bypass: Bypass) -> AudienceGroup:
    group = db.scalar(select(AudienceGroup).where(AudienceGroup.id == group_id)
                      .with_for_update().execution_options(populate_existing=True))
    if group is None or group.archived:
        raise AudienceError("分组不存在或已归档", status_code=404, code="not_found")
    allowed = group.owner_id == viewer.user_id if group.scope == "personal" else bool(bypass(viewer))
    if not allowed:
        raise AudienceError("无权修改此分组", status_code=403, code="forbidden")
    return group


def update_group(
    db: Session, group_id: int, viewer: UserContext, *, name: str | None = None,
    user_ids: Sequence[str] | None = None, bypass: Bypass,
) -> AudienceGroup:
    group = _editable_group(db, group_id, viewer, bypass)
    new_name = _name(name) if name is not None else group.name
    members = _users(db, user_ids) if user_ids is not None else None
    try:
        with db.begin_nested():
            group.name = new_name
            group.updated_at = datetime.now(timezone.utc)
            if members is not None:
                db.execute(delete(AudienceGroupMember).where(AudienceGroupMember.group_id == group.id))
                db.add_all([AudienceGroupMember(group_id=group.id, user_id=uid) for uid in members])
            db.flush()
    except IntegrityError as exc:
        raise AudienceError("同名分组已存在", status_code=409, code="conflict") from exc
    return group


def archive_group(db: Session, group_id: int, viewer: UserContext, *, bypass: Bypass) -> AudienceGroup:
    group = _editable_group(db, group_id, viewer, bypass)
    group.archived = True
    group.updated_at = datetime.now(timezone.utc)
    db.flush()
    return group
