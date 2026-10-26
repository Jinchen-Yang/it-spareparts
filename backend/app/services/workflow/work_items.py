"""通用待办引擎：调用方持有事务，状态变更和事件始终同事务。"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import permissions
from app.models.system import SysUser
from app.models.workflow import SubjectEvent, WorkItem
from app.security import UserContext
from app.services.workflow import events
from app.services.workflow.registry import (
    Conflict, Forbidden, InvalidTransition, NotFound, Subject, as_subject, get_spec, registered,
)

ACTIVE = ("pending", "in_progress")
CLOSED = ("done", "declined", "cancelled")
HANDLED = ("claim", "start", "release", "transfer", "complete", "decline")


def has_permission(ctx: UserContext, key: str) -> bool:
    graph = ctx.permissions if ctx.permissions is not None else permissions.effective(ctx.role, None)
    return bool(graph.get(key, False))


def actor_context(db: Session, actor: int | UserContext) -> tuple[SysUser, UserContext]:
    if isinstance(actor, UserContext):
        if not actor.is_authenticated or not actor.user_id:
            raise Forbidden("待办操作需要实名账号")
        predicate = SysUser.username == actor.user_id
    elif isinstance(actor, int) and not isinstance(actor, bool):
        predicate = SysUser.id == actor
    else:
        raise Forbidden("待办操作需要实名账号")
    user = db.scalar(select(SysUser).where(predicate).execution_options(populate_existing=True))
    if user is None or not user.is_active or user.deleted_at is not None:
        raise Forbidden("账号不存在或已离职停用")
    ctx = UserContext(user_id=user.username, role=user.role,
                      salesperson_name=user.salesperson_name,
                      permissions=permissions.effective_for_user(user), is_authenticated=True)
    return user, ctx


def _reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidTransition("必须填写原因")
    return reason.strip()


def _target(db: Session, user_id: int, spec) -> SysUser:
    user, ctx = actor_context(db, user_id)
    if not has_permission(ctx, spec.pool_permission):
        raise Forbidden("目标账号不是池成员")
    if spec.access_permission is not None and not has_permission(ctx, spec.access_permission):
        raise Forbidden("目标账号无权访问该业务")
    return user


def _load(db: Session, item_id: int, *, lock: bool = False) -> WorkItem:
    query = select(WorkItem).where(WorkItem.id == item_id)
    if lock:
        query = query.with_for_update()
    item = db.scalar(query.execution_options(populate_existing=True))
    if item is None:
        raise NotFound("待办不存在")
    get_spec(item.subject_type)
    return item


def _supervisor(ctx, item) -> bool:
    return has_permission(ctx, get_spec(item.subject_type).supervisor_permission)


def _check_actor(db, item, user, ctx, *, supervisor_only=False):
    spec = get_spec(item.subject_type)
    access = spec.access_permission
    if access is not None and not has_permission(ctx, access):
        raise Forbidden("无权访问该类型的待办")
    if not spec.can_view_subject(db, item.subject_id, ctx):
        raise Forbidden("无权访问该业务对象")
    allowed = _supervisor(ctx, item)
    if not supervisor_only:
        allowed = allowed or item.assignee_id == user.id or item.claimed_by == user.id
    if not allowed:
        raise Forbidden("无权处理该待办")


def _version(item, expected_version):
    if expected_version is None or isinstance(expected_version, bool) or expected_version < 1:
        raise InvalidTransition("必须提供 expected_version")
    if item.version != expected_version:
        raise Conflict("待办已更新，请刷新后重试", version=item.version)


def _active(item):
    if item.status not in ACTIVE:
        raise InvalidTransition("待办已结束，不能执行该动作")


def _event(db, item, kind, user_id, **payload):
    events.record(db, Subject(item.subject_type, item.subject_id), kind, user_id,
                  {"version": item.version, **payload}, work_item_id=item.id)


def _flush(db):
    try:
        db.flush()
    except IntegrityError as exc:
        # 外层事务由调用方回滚；不在引擎里提交或吞掉部分写入。
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise Conflict("该处理人已有未结束的待办") from exc
        raise


def open_items(db: Session, subject, assignee_ids, pool_key: str, actor) -> list[WorkItem]:
    subject = as_subject(subject)
    spec = get_spec(subject.subject_type)
    user, ctx = actor_context(db, actor)
    if not spec.can_view_subject(db, subject.subject_id, ctx):
        raise Forbidden("无权为该对象创建待办")
    if pool_key != (spec.pool_key or subject.subject_type):
        raise InvalidTransition("pool_key 与注册表不一致")
    ids = list(dict.fromkeys(assignee_ids or []))
    for user_id in ids:
        _target(db, user_id, spec)
    items = [WorkItem(subject_type=subject.subject_type, subject_id=subject.subject_id,
                      pool_key=pool_key, assignee_id=user_id, status="pending", version=1,
                      paused=False, created_by=user.id) for user_id in (ids or [None])]
    db.add_all(items)
    _flush(db)
    for item in items:
        _event(db, item, "open", user.id, assignee_id=item.assignee_id)
    return items


def claim(db: Session, item_id: int, actor, expected_version: int) -> WorkItem:
    user, ctx = actor_context(db, actor)
    item = _load(db, item_id)
    spec = get_spec(item.subject_type)
    if spec.access_permission is not None and not has_permission(ctx, spec.access_permission):
        raise Forbidden("无权访问该类型的待办")
    if not has_permission(ctx, spec.pool_permission):
        raise Forbidden("不是该池的成员")
    if item.assignee_id is not None or item.status != "pending":
        _claim_conflict(db, item)
    _version(item, expected_version)
    if item.paused:
        raise InvalidTransition("待办已暂停")
    try:
        result = db.execute(update(WorkItem).where(
            WorkItem.id == item_id, WorkItem.assignee_id.is_(None),
            WorkItem.status == "pending", WorkItem.version == expected_version,
            WorkItem.paused.is_(False),
        ).values(assignee_id=user.id, claimed_by=user.id, claimed_at=func.now(),
                 status="in_progress", version=WorkItem.version + 1, updated_at=func.now())
          .returning(WorkItem.id).execution_options(synchronize_session=False)).scalar_one_or_none()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise Conflict("该处理人已有未结束的待办") from exc
        raise
    if result is None:
        _claim_conflict(db, _load(db, item_id))
    item = _load(db, item_id)
    _event(db, item, "claim", user.id)
    return item


def _claim_conflict(db, item):
    claimant = db.get(SysUser, item.claimed_by) if item.claimed_by else None
    name = (claimant.display_name or claimant.username) if claimant else None
    message = f"已被 {name} 领取" if name else "待办已更新或不能领取，请刷新后重试"
    raise Conflict(message, claimed_by=item.claimed_by, claimed_by_name=name, version=item.version)


def _prepare(db, item_id, actor, expected_version, *, supervisor_only=False):
    user, ctx = actor_context(db, actor)
    item = _load(db, item_id, lock=True)
    _check_actor(db, item, user, ctx, supervisor_only=supervisor_only)
    _version(item, expected_version)
    _active(item)
    return item, user


def _changed(db, item, kind, user_id, **payload):
    item.version += 1
    item.updated_at = datetime.now(timezone.utc)
    _flush(db)
    _event(db, item, kind, user_id, **payload)
    return item


def start(db, item_id, actor, expected_version=None):
    item, user = _prepare(db, item_id, actor, expected_version)
    if item.status != "pending" or item.assignee_id is None:
        raise InvalidTransition("只能开始已指派且待处理的待办")
    if item.paused:
        raise InvalidTransition("待办已暂停")
    item.status = "in_progress"
    return _changed(db, item, "start", user.id)


def release(db, item_id, actor, reason, expected_version=None):
    reason = _reason(reason)
    item, user = _prepare(db, item_id, actor, expected_version)
    if item.claimed_by != user.id:
        raise Forbidden("只有领单人可以退回公共池，主管请使用收回")
    item.assignee_id = item.claimed_by = item.claimed_at = None
    item.status, item.last_reason = "pending", reason
    return _changed(db, item, "release", user.id, reason=reason)


def transfer(db, item_id, actor, to_user_id, reason, expected_version=None):
    reason = _reason(reason)
    item, user = _prepare(db, item_id, actor, expected_version)
    _target(db, to_user_id, get_spec(item.subject_type))
    if to_user_id == item.assignee_id:
        raise InvalidTransition("不能转交给当前处理人")
    previous = item.assignee_id
    item.assignee_id, item.claimed_by, item.claimed_at = to_user_id, None, None
    item.status, item.last_reason = "pending", reason
    return _changed(db, item, "transfer", user.id, reason=reason,
                    from_user_id=previous, to_user_id=to_user_id)


def reassign(db, item_id, supervisor, to_user_id, reason, expected_version=None):
    reason = _reason(reason)
    item, user = _prepare(db, item_id, supervisor, expected_version, supervisor_only=True)
    if to_user_id is not None:
        _target(db, to_user_id, get_spec(item.subject_type))
    previous = item.assignee_id
    item.assignee_id, item.claimed_by, item.claimed_at = to_user_id, None, None
    item.status, item.last_reason = "pending", reason
    return _changed(db, item, "reassign", user.id, reason=reason,
                    from_user_id=previous, to_user_id=to_user_id)


def complete(db, item_id, actor, expected_version=None):
    item, user = _prepare(db, item_id, actor, expected_version)
    if item.paused:
        raise InvalidTransition("待办已暂停，不能完成")
    if item.assignee_id is None:
        raise InvalidTransition("请先领取待办")
    validator = get_spec(item.subject_type).validate_complete
    if validator is not None:
        validator(db, item, actor_context(db, actor)[1])
    item.status, item.closed_at = "done", datetime.now(timezone.utc)
    return _changed(db, item, "complete", user.id)


def decline(db, item_id, actor, reason, expected_version=None):
    reason = _reason(reason)
    item, user = _prepare(db, item_id, actor, expected_version)
    if item.paused:
        raise InvalidTransition("待办已暂停，不能拒绝")
    if item.assignee_id is None:
        raise InvalidTransition("请先领取待办")
    item.status, item.closed_at, item.last_reason = "declined", datetime.now(timezone.utc), reason
    return _changed(db, item, "decline", user.id, reason=reason)


def _subject_action(db, subject, actor, kind, reason=None, expected_versions=None):
    subject = as_subject(subject)
    spec = get_spec(subject.subject_type)
    user, ctx = actor_context(db, actor)
    items = db.scalars(select(WorkItem).where(
        WorkItem.subject_type == subject.subject_type, WorkItem.subject_id == subject.subject_id,
        WorkItem.status.in_(ACTIVE),
    ).order_by(WorkItem.id).with_for_update().execution_options(populate_existing=True)).all()
    # 业务发起人可以撤回、恢复和关单；普通处理人不能影响其他人的待办。
    if not has_permission(ctx, spec.supervisor_permission):
        business_owner = spec.can_manage_subject is not None and spec.can_manage_subject(
            db, subject.subject_id, ctx)
        if not spec.can_view_subject(db, subject.subject_id, ctx) or (
                not business_owner and any(item.created_by != user.id for item in items)):
            raise Forbidden("只有发起人或主管可以操作整个对象的待办")
    if not isinstance(expected_versions, dict):
        raise InvalidTransition("必须提供各待办的 expected_versions")
    for item in items:
        _version(item, expected_versions.get(item.id))
    for item in items:
        if kind == "cancel":
            item.status, item.closed_at, item.last_reason = "cancelled", datetime.now(timezone.utc), reason
        else:
            paused = kind == "pause"
            if item.paused == paused:
                continue
            item.paused = paused
        _changed(db, item, kind, user.id, **({"reason": reason} if reason else {}))
    return items


def cancel_subject(db, subject, actor, reason, *, expected_versions=None):
    return _subject_action(db, subject, actor, "cancel", _reason(reason), expected_versions)


def pause_subject(db, subject, actor, *, expected_versions=None):
    return _subject_action(db, subject, actor, "pause", expected_versions=expected_versions)


def resume_subject(db, subject, actor, *, expected_versions=None):
    return _subject_action(db, subject, actor, "resume", expected_versions=expected_versions)


def _scopes(ctx, subject_type=None):
    specs = {subject_type: get_spec(subject_type)} if subject_type else registered()
    specs = {name: spec for name, spec in specs.items()
             if spec.access_permission is None or has_permission(ctx, spec.access_permission)}
    pools = [name for name, spec in specs.items() if has_permission(ctx, spec.pool_permission)]
    supervised = [name for name, spec in specs.items() if has_permission(ctx, spec.supervisor_permission)]
    return specs, pools, supervised


def _pool_predicate(pools):
    return and_(WorkItem.subject_type.in_(pools), WorkItem.assignee_id.is_(None),
                WorkItem.status == "pending")


def _tab_predicate(user_id, pools, supervised, tab):
    if tab == "assigned":
        return and_(WorkItem.assignee_id == user_id, WorkItem.claimed_by.is_(None), WorkItem.status.in_(ACTIVE))
    if tab == "claimed":
        return and_(WorkItem.claimed_by == user_id, WorkItem.status.in_(ACTIVE))
    if tab == "pool":
        return _pool_predicate(pools)
    if tab == "all":
        if not supervised:
            raise Forbidden("只有主管可以查看全部待办")
        return WorkItem.subject_type.in_(supervised)
    if tab == "done":
        handled = exists(select(SubjectEvent.id).where(
            SubjectEvent.work_item_id == WorkItem.id, SubjectEvent.actor == user_id,
            SubjectEvent.kind.in_(HANDLED)))
        return and_(WorkItem.status.in_(CLOSED),
                    WorkItem.closed_at >= datetime.now(timezone.utc) - timedelta(days=90), handled)
    raise InvalidTransition("未知的待办页签")


def allowed_actions(item, user_id, ctx):
    if item.status not in ACTIVE:
        return []
    spec = get_spec(item.subject_type)
    if item.assignee_id is None:
        actions = ["claim"] if not item.paused and has_permission(ctx, spec.pool_permission) else []
    elif item.assignee_id == user_id or item.claimed_by == user_id or _supervisor(ctx, item):
        actions = ["transfer"]
        if item.claimed_by == user_id:
            actions.append("release")
        if not item.paused:
            actions.extend(["complete", "decline"])
            if item.status == "pending":
                actions.append("start")
    else:
        actions = []
    if _supervisor(ctx, item):
        actions.append("reassign")
    return actions


def serialize_items(db, items, user_id, ctx):
    names = {user.id: user.display_name or user.username for user in db.scalars(
        select(SysUser).where(SysUser.id.in_({i for item in items for i in
            (item.assignee_id, item.claimed_by) if i is not None}))).all()} if items else {}
    summaries = {}
    for name in dict.fromkeys(item.subject_type for item in items):
        ids = list(dict.fromkeys(item.subject_id for item in items if item.subject_type == name))
        summaries[name] = get_spec(name).summarize(db, ids, ctx)
    result = [{**{column.name: getattr(item, column.name) for column in WorkItem.__table__.columns},
             "summary": summaries[item.subject_type].get(item.subject_id, {}),
             "assignee_name": names.get(item.assignee_id), "claimed_by_name": names.get(item.claimed_by),
             "allowed_actions": allowed_actions(item, user_id, ctx)} for item in items]
    for name in summaries:
        field_filter = get_spec(name).filter_items
        if field_filter is None:
            continue
        indices = [index for index, item in enumerate(items) if item.subject_type == name]
        filtered = field_filter(db, [result[index] for index in indices], ctx)
        # 回调只过滤字段，不能改变记录数量或顺序；记录准入在分页前完成。
        for index, row in zip(indices, filtered, strict=True):
            result[index] = row
    return result


def visible_items(db, ctx, tab, subject_type=None, *, page=1, page_size=20):
    if page < 1 or not 1 <= page_size <= 100:
        raise InvalidTransition("分页参数不合法")
    user, ctx = actor_context(db, ctx)
    specs, pools, supervised = _scopes(ctx, subject_type)
    predicate = and_(WorkItem.subject_type.in_(specs), _tab_predicate(user.id, pools, supervised, tab))
    rows = db.scalars(select(WorkItem).where(predicate).order_by(WorkItem.id.desc())).all()
    # 受众分组可即时变化；旧指派关系不能代替当前业务准入。先过滤，再分页和计数。
    access_cache = {}
    items = []
    for item in rows:
        subject = (item.subject_type, item.subject_id)
        if subject not in access_cache:
            access_cache[subject] = specs[item.subject_type].can_view_subject(db, item.subject_id, ctx)
        if access_cache[subject]:
            items.append(item)
    page_items = items[(page - 1) * page_size:page * page_size]
    return {"items": serialize_items(db, page_items, user.id, ctx), "total": len(items),
            "page": page, "page_size": page_size, "can_supervise": bool(supervised)}


def badge(db, ctx):
    user, ctx = actor_context(db, ctx)
    specs, pools, supervised = _scopes(ctx)
    visibility = [or_(WorkItem.subject_type != name,
                      WorkItem.subject_id.in_(spec.badge_subject_ids(db, ctx)))
                  for name, spec in specs.items() if spec.badge_subject_ids is not None]
    # 三个角标共用一次聚合，不逐页签查询。
    row = db.execute(select(*[func.count().filter(_tab_predicate(user.id, pools, supervised, tab)).label(tab)
                              for tab in ("assigned", "pool", "claimed")])
                     .select_from(WorkItem).where(WorkItem.subject_type.in_(specs), *visibility)).one()
    return dict(row._mapping)


def candidates(db, ctx, pool_key):
    _, ctx = actor_context(db, ctx)
    specs = [spec for name, spec in registered().items() if (spec.pool_key or name) == pool_key]
    if not specs:
        raise NotFound("未注册的 pool_key")
    result = []
    for user in db.scalars(select(SysUser).where(SysUser.is_active.is_(True), SysUser.deleted_at.is_(None))
                            .order_by(SysUser.id)).all():
        graph = permissions.effective_for_user(user)
        if graph.get(specs[0].pool_permission, False) and (
                specs[0].access_permission is None or graph.get(specs[0].access_permission, False)):
            result.append({"id": user.id, "display_name": user.display_name or user.username})
    return {"items": result}


def participation_batch(db: Session, subject_type, subject_ids, actor) -> dict[int, dict[str, bool]]:
    """批量返回本人和主体的关系，不读取业务对象，供批量准入回调使用。"""
    spec = get_spec(subject_type)
    ids = list(dict.fromkeys(subject_ids))
    for subject_id in ids:
        as_subject(Subject(subject_type, subject_id))
    if not ids:
        return {}
    user, ctx = actor_context(db, actor)
    rows = db.execute(select(WorkItem.subject_id, WorkItem.assignee_id, WorkItem.created_by, WorkItem.status).where(
        WorkItem.subject_type == subject_type, WorkItem.subject_id.in_(ids),
    )).all()
    result = {subject_id: {"assigned": False, "creator": False, "unclaimed": False}
              for subject_id in ids}
    member = has_permission(ctx, spec.pool_permission)
    for row in rows:
        flags = result[row.subject_id]
        flags["assigned"] |= row.assignee_id == user.id
        flags["creator"] |= row.created_by == user.id
        flags["unclaimed"] |= member and row.assignee_id is None and row.status == "pending"
    return result


def subject_participation(db: Session, subject, actor) -> dict[str, bool]:
    """只返回本人和主体的关系，供准入回调使用，避免回调递归。"""
    subject = as_subject(subject)
    return participation_batch(db, subject.subject_type, [subject.subject_id], actor)[subject.subject_id]


def list_by_subject(db: Session, subject, actor, *, lock=False) -> list[WorkItem]:
    """业务读取待办的公开入口；发起人/主管读全部，处理人只读本人或公共池。"""
    subject = as_subject(subject)
    user, ctx = actor_context(db, actor)
    spec = get_spec(subject.subject_type)
    if not spec.can_view_subject(db, subject.subject_id, ctx):
        raise Forbidden("无权查看该对象的待办")
    stmt = select(WorkItem).where(
        WorkItem.subject_type == subject.subject_type, WorkItem.subject_id == subject.subject_id,
    ).order_by(WorkItem.id)
    if lock:
        stmt = stmt.with_for_update()
    rows = list(db.scalars(stmt.execution_options(populate_existing=True)))
    business_owner = spec.can_manage_subject is not None and spec.can_manage_subject(
        db, subject.subject_id, ctx)
    if business_owner or has_permission(ctx, spec.supervisor_permission) or any(row.created_by == user.id for row in rows):
        return rows
    return [row for row in rows if row.assignee_id == user.id or (
        row.assignee_id is None and row.status == "pending" and has_permission(ctx, spec.pool_permission)
    )]


subject_items = list_by_subject


def reopen(db: Session, item_id: int, actor, expected_version=None) -> WorkItem:
    """主体追加待处理内容后，发起人或主管可重新打开已回复的待办。"""
    user, ctx = actor_context(db, actor)
    item = _load(db, item_id, lock=True)
    spec = get_spec(item.subject_type)
    business_owner = spec.can_manage_subject is not None and spec.can_manage_subject(
        db, item.subject_id, ctx)
    if not spec.can_view_subject(db, item.subject_id, ctx) or not (
        business_owner or item.created_by == user.id or has_permission(ctx, spec.supervisor_permission)
    ):
        raise Forbidden("只有发起人或主管可以重新打开待办")
    _version(item, expected_version)
    if item.status not in ("done", "declined"):
        raise InvalidTransition("只能重新打开已回复的待办")
    item.status, item.closed_at, item.paused = "pending", None, False
    return _changed(db, item, "reopen", user.id)
