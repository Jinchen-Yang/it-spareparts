"""用假业务 demo 验证通用待办、时间线与实名接口契约。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text

from app import permissions
from app.auth import _make_token
from app.db import SessionLocal, engine
from app.main import app
from app.models.system import SysAccessLog, SysUser
from app.models.workflow import SubjectEvent, WorkItem
from app.security import UserContext
from app.services.workflow import events, registry, work_items
from app.services.workflow.registry import (
    Conflict, Forbidden, InvalidTransition, NotFound, Subject, SubjectSpec,
)


@pytest.fixture()
def demo(monkeypatch):
    state = SimpleNamespace(denied=set(), summaries=[], filters=0)
    monkeypatch.setattr(registry, "_specs", {})

    def summarize(_db, ids, _ctx):
        state.summaries.append(list(ids))
        return {subject_id: {"title": f"示例 {subject_id}"} for subject_id in ids}

    def filter_events(rows, _ctx):
        state.filters += 1
        return [row for row in rows if row.kind != "private"]

    registry.register("demo", SubjectSpec(
        pool_permission="page_inventory", supervisor_permission="page_accounts",
        pool_key="demo-pool",
        can_view_subject=lambda _db, subject_id, _ctx: subject_id not in state.denied,
        summarize=summarize, filter_events=filter_events,
    ))
    return state


@pytest.fixture()
def users(db, demo):
    result = {}
    for name, member, supervisor in (
        ("owner", True, False), ("peer", True, False),
        ("supervisor", False, True), ("outsider", False, False),
        ("inactive", True, False), ("deleted", True, False),
    ):
        user = SysUser(
            username=name, display_name=f"示例{name}", role="readonly",
            password_hash="测试不走口令登录", is_active=name != "inactive",
            deleted_at=datetime.now(timezone.utc) if name == "deleted" else None,
            template_perms={"page_inventory": member, "page_accounts": supervisor},
            perm_overrides={},
        )
        db.add(user)
        result[name] = user
    db.commit()
    return SimpleNamespace(**result)


def _ctx(user):
    return UserContext(user_id=user.username, role=user.role,
                       permissions=permissions.effective_for_user(user), is_authenticated=True)


def _client(user, **token_kwargs):
    token, _ = _make_token(user.role, user.username, user.display_name,
                           perms=permissions.effective_for_user(user),
                           token_version=user.token_version, **token_kwargs)
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def _open(db, users, subject_id=1, *, pool=False, assignees=None):
    ids = [] if pool else ([users.owner.id] if assignees is None else assignees)
    return work_items.open_items(db, Subject("demo", subject_id), ids,
                                 "demo-pool", users.owner.id)


def _ready(db, users, action):
    item = _open(db, users, pool=action in {"claim", "release"})[0]
    if action == "release":
        work_items.claim(db, item.id, users.owner.id, item.version)
    return item


def _kwargs(item, users, action):
    values = {"expected_version": item.version}
    if action in {"release", "transfer", "reassign", "decline"}:
        values["reason"] = "资源调整"
    if action in {"transfer", "reassign"}:
        values["to_user_id"] = users.peer.id
    return values


ACTIONS = ("claim", "start", "release", "transfer", "reassign", "complete", "decline")


def test_open_multiple_and_pool_items_records_each_event_without_committing(db, users):
    items = _open(db, users, assignees=[users.owner.id, users.peer.id, users.owner.id])
    assert {item.assignee_id for item in items} == {users.owner.id, users.peer.id}
    assert all(item.status == "pending" and item.version == 1 and not item.paused for item in items)
    pool = _open(db, users, 2, pool=True)[0]
    assert pool.assignee_id is None and pool.claimed_by is None
    assert db.scalar(select(func.count()).select_from(SubjectEvent)) == 3
    db.rollback()
    with SessionLocal() as fresh:
        assert fresh.scalar(select(func.count()).select_from(WorkItem)) == 0
        assert fresh.scalar(select(func.count()).select_from(SubjectEvent)) == 0


def test_active_assignee_is_unique_but_closed_item_can_be_reopened(db, users):
    item = _open(db, users)[0]
    with pytest.raises(Conflict), db.begin_nested():
        _open(db, users)
    work_items.complete(db, item.id, users.owner.id, item.version)
    reopened = _open(db, users)[0]
    assert reopened.id != item.id and reopened.status == "pending"


@pytest.mark.parametrize("action", ACTIONS)
def test_each_action_transitions_and_appends_one_event_in_caller_transaction(db, users, action):
    item = _ready(db, users, action)
    before_version = item.version
    before_events = db.scalar(select(func.count()).select_from(SubjectEvent))
    db.commit()
    actor = users.supervisor if action == "reassign" else users.owner
    result = getattr(work_items, action)(db, item.id, actor.id, **_kwargs(item, users, action))
    assert result.version == before_version + 1
    expected_status = {
        "claim": "in_progress", "start": "in_progress", "release": "pending",
        "transfer": "pending", "reassign": "pending", "complete": "done", "decline": "declined",
    }[action]
    assert result.status == expected_status
    last = db.scalar(select(SubjectEvent).order_by(SubjectEvent.id.desc()))
    assert (last.kind, last.actor, last.work_item_id) == (action, actor.id, item.id)
    assert last.payload["version"] == result.version
    assert db.scalar(select(func.count()).select_from(SubjectEvent)) == before_events + 1
    if action in {"release", "transfer", "reassign", "decline"}:
        assert result.last_reason == last.payload["reason"] == "资源调整"
    if action == "claim":
        assert result.assignee_id == result.claimed_by == users.owner.id
        assert result.claimed_at is not None
    if action == "release":
        assert result.assignee_id is result.claimed_by is result.claimed_at is None
    if action in {"transfer", "reassign"}:
        assert result.assignee_id == users.peer.id and result.claimed_by is None
    if action in {"complete", "decline"}:
        assert result.closed_at is not None
    db.rollback()
    db.expire_all()
    assert db.get(WorkItem, item.id).version == before_version
    assert db.scalar(select(func.count()).select_from(SubjectEvent)) == before_events


@pytest.mark.parametrize("action", ACTIONS)
def test_each_action_rejects_unrelated_actor(db, users, action):
    item = _ready(db, users, action)
    actor = users.outsider if action == "claim" else users.peer
    with pytest.raises(Forbidden):
        getattr(work_items, action)(db, item.id, actor.id, **_kwargs(item, users, action))


@pytest.mark.parametrize("action", ACTIONS)
def test_each_action_rejects_stale_version_without_event(db, users, action):
    item = _ready(db, users, action)
    values = _kwargs(item, users, action)
    values["expected_version"] += 1
    count = db.scalar(select(func.count()).select_from(SubjectEvent))
    actor = users.supervisor if action == "reassign" else users.owner
    with pytest.raises(Conflict):
        getattr(work_items, action)(db, item.id, actor.id, **values)
    assert db.scalar(select(func.count()).select_from(SubjectEvent)) == count


@pytest.mark.parametrize("action", ACTIONS)
def test_each_action_rejects_terminal_item(db, users, action):
    item = _ready(db, users, "release" if action == "release" else "complete")
    work_items.complete(db, item.id, users.owner.id, item.version)
    actor = users.supervisor if action == "reassign" else users.owner
    with pytest.raises(Conflict if action == "claim" else InvalidTransition):
        getattr(work_items, action)(db, item.id, actor.id, **_kwargs(item, users, action))


@pytest.mark.parametrize("action", ("release", "transfer", "reassign", "decline"))
def test_reason_is_required_for_reasoned_actions(db, users, action):
    item = _ready(db, users, action)
    values = _kwargs(item, users, action)
    values["reason"] = " \t "
    actor = users.supervisor if action == "reassign" else users.owner
    with pytest.raises(InvalidTransition, match="原因"):
        getattr(work_items, action)(db, item.id, actor.id, **values)


@pytest.mark.parametrize("target", ("outsider", "inactive", "deleted"))
def test_open_transfer_and_reassign_require_active_pool_member(db, users, target):
    target_id = getattr(users, target).id
    with pytest.raises(Forbidden):
        _open(db, users, 2, assignees=[target_id])
    item = _open(db, users)[0]
    with pytest.raises(Forbidden):
        work_items.transfer(db, item.id, users.owner.id, target_id, "转交", item.version)
    with pytest.raises(Forbidden):
        work_items.reassign(db, item.id, users.supervisor.id, target_id, "改派", item.version)


def test_start_release_and_transfer_reject_inapplicable_transitions(db, users):
    item = _open(db, users)[0]
    with pytest.raises(Forbidden):
        work_items.release(db, item.id, users.owner.id, "退回", item.version)
    with pytest.raises(InvalidTransition):
        work_items.transfer(db, item.id, users.owner.id, users.owner.id, "转交", item.version)
    work_items.start(db, item.id, users.owner.id, item.version)
    with pytest.raises(InvalidTransition):
        work_items.start(db, item.id, users.owner.id, item.version)
    pool = _open(db, users, 2, pool=True)[0]
    for action in ("start", "complete", "decline"):
        values = {"expected_version": pool.version}
        if action == "decline":
            values["reason"] = "拒绝"
        with pytest.raises(InvalidTransition):
            getattr(work_items, action)(db, pool.id, users.supervisor.id, **values)


def test_supervisor_reclaims_but_cannot_release_another_claimant(db, users):
    item = _open(db, users, pool=True)[0]
    work_items.claim(db, item.id, users.owner.id, item.version)
    with pytest.raises(Forbidden):
        work_items.release(db, item.id, users.supervisor.id, "收回", item.version)
    work_items.reassign(db, item.id, users.supervisor.id, None, "收回", item.version)
    assert item.status == "pending" and item.assignee_id is None
    assert item.claimed_by is None and item.claimed_at is None


def test_pause_resume_cancel_affect_active_items_only_and_record_events(db, users):
    closed = _open(db, users)[0]
    work_items.complete(db, closed.id, users.owner.id, closed.version)
    first, second = _open(db, users, assignees=[users.owner.id, users.peer.id])
    with pytest.raises(Forbidden):
        work_items.pause_subject(db, ("demo", 1), users.peer.id,
                                expected_versions={first.id: first.version, second.id: second.version})
    with pytest.raises(Conflict):
        work_items.pause_subject(db, ("demo", 1), users.owner.id,
                                expected_versions={first.id: first.version, second.id: second.version + 1})
    assert not first.paused and not second.paused
    work_items.pause_subject(db, ("demo", 1), users.owner.id,
                            expected_versions={first.id: first.version, second.id: second.version})
    assert first.paused and second.paused and not closed.paused
    for action in ("start", "complete", "decline"):
        values = {"expected_version": first.version}
        if action == "decline":
            values["reason"] = "拒绝"
        with pytest.raises(InvalidTransition, match="暂停"):
            getattr(work_items, action)(db, first.id, users.owner.id, **values)
    work_items.resume_subject(db, ("demo", 1), users.supervisor.id,
                             expected_versions={first.id: first.version, second.id: second.version})
    assert not first.paused and not second.paused
    work_items.complete(db, first.id, users.owner.id, first.version)
    with pytest.raises(InvalidTransition):
        work_items.cancel_subject(db, ("demo", 1), users.owner.id, " ",
                                 expected_versions={second.id: second.version})
    work_items.cancel_subject(db, ("demo", 1), users.owner.id, "业务关闭",
                             expected_versions={second.id: second.version})
    assert first.status == closed.status == "done" and second.status == "cancelled"
    assert second.closed_at is not None and second.last_reason == "业务关闭"
    kinds = db.scalars(select(SubjectEvent.kind).where(SubjectEvent.work_item_id == second.id)).all()
    assert kinds == ["open", "pause", "resume", "cancel"]


def test_paused_pool_cannot_be_claimed(db, users):
    item = _open(db, users, pool=True)[0]
    work_items.pause_subject(db, ("demo", 1), users.owner.id,
                            expected_versions={item.id: item.version})
    with pytest.raises(InvalidTransition, match="暂停"):
        work_items.claim(db, item.id, users.peer.id, item.version)


@pytest.mark.parametrize("action", ("pause_subject", "resume_subject", "cancel_subject"))
def test_subject_actions_require_all_current_versions_and_reject_atomically(db, users, action):
    first, second = _open(db, users, assignees=[users.owner.id, users.peer.id])
    if action == "resume_subject":
        work_items.pause_subject(db, ("demo", 1), users.owner.id,
                                expected_versions={first.id: first.version, second.id: second.version})
    before = [(item.status, item.paused, item.version) for item in (first, second)]
    count = db.scalar(select(func.count()).select_from(SubjectEvent))
    kwargs = {"reason": "取消"} if action == "cancel_subject" else {}
    with pytest.raises(InvalidTransition):
        getattr(work_items, action)(db, ("demo", 1), users.owner.id, **kwargs)
    with pytest.raises(InvalidTransition):
        getattr(work_items, action)(db, ("demo", 1), users.owner.id,
                                   expected_versions={first.id: first.version}, **kwargs)
    with pytest.raises(Conflict):
        getattr(work_items, action)(db, ("demo", 1), users.owner.id,
                                   expected_versions={first.id: first.version, second.id: second.version + 1},
                                   **kwargs)
    assert [(item.status, item.paused, item.version) for item in (first, second)] == before
    assert db.scalar(select(func.count()).select_from(SubjectEvent)) == count


def test_claim_conflicts_when_actor_already_has_active_item_for_subject(db, users):
    _open(db, users)
    pool = _open(db, users, pool=True)[0]
    with pytest.raises(Conflict), db.begin_nested():
        work_items.claim(db, pool.id, users.owner.id, pool.version)
    db.refresh(pool)
    assert pool.assignee_id is None and pool.status == "pending" and pool.version == 1


def test_two_independent_connections_claim_simultaneously_only_one_succeeds(db, users):
    item = _open(db, users, pool=True)[0]
    item_id, owner_id, peer_id = item.id, users.owner.id, users.peer.id
    db.commit()
    barrier = Barrier(2)

    def attempt(user_id):
        with SessionLocal() as session:
            connection = session.connection()
            pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()

            def race(_conn, _cursor, statement, _params, _context, _many):
                if statement.lstrip().upper().startswith("UPDATE WORK_ITEM"):
                    barrier.wait(timeout=15)

            event.listen(connection, "before_cursor_execute", race)
            try:
                claimed = work_items.claim(session, item_id, user_id, 1)
                session.commit()
                return {"ok": True, "user_id": claimed.assignee_id, "pid": pid}
            except Conflict as exc:
                session.rollback()
                return {"ok": False, "detail": exc.detail, "pid": pid}

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (owner_id, peer_id)))
    assert len({result["pid"] for result in results}) == 2
    winner, = [result for result in results if result["ok"]]
    loser, = [result for result in results if not result["ok"]]
    assert loser["detail"]["claimed_by"] == winner["user_id"]
    assert loser["detail"]["claimed_by_name"] in {users.owner.display_name, users.peer.display_name}
    db.expire_all()
    stored = db.get(WorkItem, item_id)
    assert stored.assignee_id == stored.claimed_by == winner["user_id"] and stored.version == 2
    assert db.scalar(select(func.count()).select_from(SubjectEvent).where(SubjectEvent.kind == "claim")) == 1


def test_visibility_tabs_summary_and_supervisor_scope(db, users, demo):
    assigned = _open(db, users, 1)[0]
    peer = _open(db, users, 2, assignees=[users.peer.id])[0]
    pool = _open(db, users, 3, pool=True)[0]
    claimed = _open(db, users, 4, pool=True)[0]
    work_items.claim(db, claimed.id, users.owner.id, claimed.version)
    for tab, expected in (("assigned", assigned), ("pool", pool), ("claimed", claimed)):
        response = work_items.visible_items(db, _ctx(users.owner), tab)
        assert response["total"] == 1
        assert [row["id"] for row in response["items"]] == [expected.id]
        assert response["items"][0]["summary"] == {"title": f"示例 {expected.subject_id}"}
        assert peer.id not in {row["id"] for row in response["items"]}
        assert response["can_supervise"] is False
    assert demo.summaries == [[1], [3], [4]]
    assert work_items.visible_items(db, _ctx(users.outsider), "pool")["items"] == []
    with pytest.raises(Forbidden):
        work_items.visible_items(db, _ctx(users.owner), "all")
    all_items = work_items.visible_items(db, _ctx(users.supervisor), "all")
    assert all_items["total"] == 4 and all_items["can_supervise"] is True


def test_done_tab_tracks_previous_handler_excludes_older_than_90_days_and_paginates(db, users):
    first = _open(db, users, 1)[0]
    work_items.transfer(db, first.id, users.owner.id, users.peer.id, "协作", first.version)
    work_items.complete(db, first.id, users.peer.id, first.version)
    second = _open(db, users, 2)[0]
    work_items.decline(db, second.id, users.owner.id, "不能处理", second.version)
    old = _open(db, users, 3)[0]
    work_items.complete(db, old.id, users.owner.id, old.version)
    old.closed_at = datetime.now(timezone.utc) - timedelta(days=91)
    untouched = _open(db, users, 4, assignees=[users.peer.id])[0]
    work_items.complete(db, untouched.id, users.peer.id, untouched.version)
    db.flush()
    page1 = work_items.visible_items(db, _ctx(users.owner), "done", page=1, page_size=1)
    page2 = work_items.visible_items(db, _ctx(users.owner), "done", page=2, page_size=1)
    assert page1["total"] == page2["total"] == 2
    assert [row["id"] for row in page1["items"]] == [second.id]
    assert [row["id"] for row in page2["items"]] == [first.id]


def test_all_tab_is_limited_to_registered_supervisor_scope(db, users):
    visible = _open(db, users)[0]
    registry.register("demo_other", SubjectSpec(
        pool_permission="page_inventory", supervisor_permission="page_parts",
        can_view_subject=lambda _db, _subject_id, _ctx: True,
        summarize=lambda _db, ids, _ctx: {subject_id: {} for subject_id in ids},
    ))
    hidden = work_items.open_items(db, ("demo_other", 1), [], "demo_other", users.owner.id)[0]
    result = work_items.visible_items(db, _ctx(users.supervisor), "all")
    assert [row["id"] for row in result["items"]] == [visible.id]
    assert hidden.id != visible.id
    with pytest.raises(Forbidden):
        work_items.visible_items(db, _ctx(users.supervisor), "all", "demo_other")


def test_badges_match_tabs_using_one_work_item_aggregate(db, users):
    _open(db, users, 1)
    _open(db, users, 2)
    _open(db, users, 3, pool=True)
    claimed = _open(db, users, 4, pool=True)[0]
    work_items.claim(db, claimed.id, users.owner.id, claimed.version)
    _open(db, users, 5, assignees=[users.peer.id])
    closed = _open(db, users, 6)[0]
    work_items.complete(db, closed.id, users.owner.id, closed.version)
    statements = []

    def observe(_conn, _cursor, statement, _params, _context, _many):
        if "FROM work_item" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        result = work_items.badge(db, _ctx(users.owner))
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert result == {"assigned": 2, "pool": 1, "claimed": 1}
    assert len(statements) == 1


def test_timeline_checks_visibility_before_filtering_and_keeps_system_actor(db, users, demo):
    public = events.record(db, ("demo", 1), "public", users.owner.id, {"value": "公开"})
    events.record(db, ("demo", 1), "private", users.owner.id, {"secret": "不可返回"})
    system = events.record(db, ("demo", 1), "system", None, {"value": "系统"})
    assert [row.id for row in events.list_events(db, ("demo", 1), _ctx(users.owner))] == [system.id, public.id]
    demo.denied.add(1)
    before = demo.filters
    with pytest.raises(Forbidden):
        events.list_events(db, ("demo", 1), _ctx(users.owner))
    assert demo.filters == before


def test_registration_and_invalid_service_inputs_fail_closed(db, users, demo):
    with pytest.raises(ValueError):
        registry.register("demo", registry.get_spec("demo"))
    with pytest.raises(NotFound):
        events.record(db, ("missing", 1), "open", None, {})
    with pytest.raises(NotFound):
        work_items.visible_items(db, _ctx(users.owner), "pool", "missing")
    with pytest.raises(NotFound):
        work_items.candidates(db, _ctx(users.owner), "missing")
    with pytest.raises(InvalidTransition):
        events.record(db, ("demo", 0), "open", None, {})
    with pytest.raises(InvalidTransition):
        events.record(db, ("demo", 1), "x" * 49, None, {})
    demo.denied.add(9)
    with pytest.raises(Forbidden):
        _open(db, users, 9)
    with pytest.raises(InvalidTransition):
        work_items.open_items(db, ("demo", 1), [], "wrong-pool", users.owner.id)
    with pytest.raises(InvalidTransition):
        work_items.visible_items(db, _ctx(users.owner), "unknown")
    with pytest.raises(InvalidTransition):
        work_items.visible_items(db, _ctx(users.owner), "pool", page=0)
    with pytest.raises(Forbidden):
        work_items.badge(db, UserContext(None, "admin", is_authenticated=False))


def test_actor_identity_and_permissions_are_reloaded_from_account(db, users):
    pool = _open(db, users, pool=True)[0]
    for actor in (users.inactive.id, users.deleted.id, 999999):
        with pytest.raises(Forbidden):
            work_items.claim(db, pool.id, actor, pool.version)
    forged = UserContext(users.outsider.username, "admin",
                         permissions={"page_inventory": True, "page_accounts": True},
                         is_authenticated=True)
    with pytest.raises(Forbidden):
        work_items.claim(db, pool.id, forged, pool.version)
    assert pool.assignee_id is None and pool.version == 1


def test_api_actions_commit_events_and_access_audit_and_return_claimant_on_conflict(db, users):
    item = _open(db, users, pool=True)[0]
    item_id = item.id
    db.commit()
    owner, peer, supervisor = _client(users.owner), _client(users.peer), _client(users.supervisor)
    response = owner.post(f"/api/work-items/{item_id}/claim", json={"expected_version": 1})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_progress" and response.json()["version"] == 2
    conflict = peer.post(f"/api/work-items/{item_id}/claim", json={"expected_version": 1})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["claimed_by_name"] == users.owner.display_name
    version = 2
    for client, action, extra in (
        (owner, "release", {"reason": "退回"}),
        (owner, "claim", {}),
        (owner, "transfer", {"reason": "转交", "to_user_id": users.peer.id}),
        (peer, "start", {}),
        (supervisor, "reassign", {"reason": "收回", "to_user_id": None}),
        (owner, "claim", {}),
        (owner, "complete", {}),
    ):
        response = client.post(f"/api/work-items/{item_id}/{action}",
                               json={"expected_version": version, **extra})
        assert response.status_code == 200, response.text
        version += 1
        assert response.json()["version"] == version
    second = _open(db, users, 2)[0]
    second_id = second.id
    db.commit()
    assert owner.post(f"/api/work-items/{second_id}/decline",
                      json={"expected_version": 1, "reason": "不能处理"}).status_code == 200
    db.expire_all()
    audits = db.scalars(select(SysAccessLog).where(SysAccessLog.action.like("work_item_%"))).all()
    assert len(audits) == 9
    assert {row.action for row in audits} == {f"work_item_{action}" for action in ACTIONS}
    assert all(row.resource == "work_item" and row.detail["work_item_id"] in {item_id, second_id}
               for row in audits)
    assert db.get(WorkItem, item_id).status == "done"


def test_api_maps_permissions_versions_transition_and_request_errors(db, users):
    item = _open(db, users)[0]
    item_id = item.id
    db.commit()
    owner, peer = _client(users.owner), _client(users.peer)
    assert peer.post(f"/api/work-items/{item_id}/start", json={"expected_version": 1}).status_code == 403
    assert owner.post(f"/api/work-items/{item_id}/start", json={"expected_version": 2}).status_code == 409
    assert owner.post(f"/api/work-items/{item_id}/release",
                      json={"expected_version": 1, "reason": " "}).status_code == 422
    assert owner.post(f"/api/work-items/{item_id}/start", json={}).status_code == 422
    assert _client(users.supervisor).post(f"/api/work-items/{item_id}/reassign",
                                         json={"expected_version": 1, "reason": "改派"}).status_code == 422
    assert owner.post(f"/api/work-items/{item_id}/transfer",
                      json={"expected_version": 1, "reason": "转交", "to_user_id": users.outsider.id}).status_code == 403
    assert owner.post(f"/api/work-items/{item_id}/start", json={"expected_version": 1}).status_code == 200
    assert owner.post(f"/api/work-items/{item_id}/start", json={"expected_version": 2}).status_code == 422
    assert owner.post("/api/work-items/999999/complete", json={"expected_version": 1}).status_code == 404
    assert owner.get("/api/work-items", params={"page_size": 101}).status_code == 422


def test_api_lists_candidates_badges_and_filters_timeline_before_pagination(db, users, demo):
    _open(db, users)
    _open(db, users, 2, pool=True)
    events.record(db, ("demo", 7), "first", users.owner.id, {})
    events.record(db, ("demo", 7), "private", users.peer.id, {"secret": "隐藏"})
    events.record(db, ("demo", 7), "last", None, {})
    db.commit()
    client = _client(users.owner)
    listed = client.get("/api/work-items", params={"tab": "assigned", "subject_type": "demo"})
    assert listed.status_code == 200 and listed.json()["items"][0]["summary"] == {"title": "示例 1"}
    assert client.get("/api/work-items/badge").json() == {"assigned": 1, "pool": 1, "claimed": 0}
    candidates = client.get("/api/work-items/candidates", params={"pool_key": "demo-pool"})
    assert candidates.status_code == 200
    assert candidates.json()["items"] == [
        {"id": users.owner.id, "display_name": users.owner.display_name},
        {"id": users.peer.id, "display_name": users.peer.display_name},
    ]
    assert _client(users.outsider).get("/api/work-items/candidates", params={"pool_key": "demo-pool"}).status_code == 200
    first_page = client.get("/api/subjects/demo/7/events", params={"page_size": 1}).json()
    second_page = client.get("/api/subjects/demo/7/events", params={"page_size": 1, "page": 2}).json()
    assert first_page["total"] == second_page["total"] == 2
    assert first_page["items"][0]["kind"] == "last" and first_page["items"][0]["actor_name"] is None
    assert second_page["items"][0]["kind"] == "first"
    assert second_page["items"][0]["actor_name"] == users.owner.display_name
    demo.denied.add(7)
    assert client.get("/api/subjects/demo/7/events").status_code == 403


@pytest.mark.parametrize("path", (
    "/api/work-items?subject_type=missing", "/api/work-items/candidates?pool_key=missing",
    "/api/subjects/missing/1/events",
))
def test_api_unregistered_subjects_and_pools_return_404(db, users, path):
    assert _client(users.owner).get(path).status_code == 404


def test_api_unregistered_item_type_returns_404(db, users):
    item = WorkItem(subject_type="missing", subject_id=1, pool_key="missing", created_by=users.owner.id)
    db.add(item)
    db.commit()
    response = _client(users.owner).post(f"/api/work-items/{item.id}/claim", json={"expected_version": 1})
    assert response.status_code == 404


@pytest.mark.parametrize("path", (
    "/api/work-items", "/api/work-items/badge", "/api/work-items/candidates?pool_key=demo-pool",
    "/api/subjects/demo/1/events",
))
def test_api_read_endpoints_require_login_and_real_account(db, users, path):
    assert TestClient(app).get(path).status_code == 401
    assert _client(users.owner, fallback=True).get(path).status_code == 403


@pytest.mark.parametrize("action", ACTIONS)
def test_api_write_endpoints_require_login(db, users, action):
    body = {"expected_version": 1, "reason": "原因", "to_user_id": users.peer.id}
    response = TestClient(app).post(f"/api/work-items/1/{action}", json=body)
    assert response.status_code == 401
