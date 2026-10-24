"""Captured legacy AgentAccess must not authorize publication after cutover."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app.authz import runtime
from app.authz.provenance import AgentAccess
from app.config import get_settings
from app.models.chat import ChatMessage, ChatSession
from app.services import chat_store


@pytest.fixture
def legacy_agent(db, monkeypatch):
    settings = get_settings()
    for name, value in {'authorization_mode': 'legacy_compat', 'authz_enabled_resources': [],
            'authz_suspended_resources': [], 'authz_sales_analysis_enabled': False,
            'authz_sales_analysis_suspended': False}.items():
        monkeypatch.setattr(settings, name, value)
    access = AgentAccess(db.get_bind(), None)
    assert access.authoritative is False
    session = ChatSession(owner_sub='legacy-owner', title='Synthetic legacy conversation')
    db.add(session)
    db.commit()
    import app.db as database
    monkeypatch.setattr(database, 'SessionLocal', lambda: Session(db.get_bind()))
    return access, session.id


def activate(db, monkeypatch):
    monkeypatch.setattr(get_settings(), 'authorization_mode', 'unified_modules')
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', ['private_chat_session'])
    runtime.activate_configured_resources(db.get_bind())


def test_captured_legacy_agent_rechecks_current_authority_before_publication(db, legacy_agent, monkeypatch):
    access, sid = legacy_agent
    access.check()
    activate(db, monkeypatch)
    with pytest.raises(HTTPException):
        access.check()
    with pytest.raises(HTTPException):
        chat_store.save_assistant_progress(sid, None, 'must not publish', [], False, access=access)
    assert db.scalar(select(func.count()).select_from(ChatMessage)) == 0


@pytest.mark.parametrize('transition', ['non_agent_resource', 'artifact_protocol'])
def test_legacy_checkpoint_last_check_serializes_first_global_transition(db, legacy_agent, monkeypatch, transition):
    access, sid = legacy_agent
    original = chat_store._owned_access
    attempting = Event()
    observed = {}
    futures = []
    engine = db.get_bind()
    def sql(conn, _cursor, statement, _params, _ctx, _many):
        if 'pg_advisory_xact_lock(' in statement:
            observed['transition_pid'] = conn.connection.driver_connection.info.backend_pid
            attempting.set()
    def transition_command():
        if transition == 'artifact_protocol':
            runtime.mark_artifact_protocol_used(engine, protocol='authz_s4')
        else:
            monkeypatch.setattr(get_settings(), 'authorization_mode', 'unified_modules')
            monkeypatch.setattr(get_settings(), 'authz_enabled_resources', ['inventory'])
            runtime.activate_configured_resources(engine)
    def last_checked(writer, *args, **kwargs):
        result = original(writer, *args, **kwargs)
        if kwargs.get('write'):
            publisher_pid = writer.scalar(text('SELECT pg_backend_pid()'))
            future = pool.submit(transition_command)
            futures.append(future)
            assert attempting.wait(5)
            deadline = time.monotonic() + 5
            blockers = []
            with engine.connect() as observer:
                while time.monotonic() < deadline:
                    blockers = observer.scalar(text('SELECT pg_blocking_pids(:pid)'),
                        {'pid': observed['transition_pid']})
                    if publisher_pid in blockers or future.done():
                        break
                    time.sleep(.01)
            assert publisher_pid in blockers, 'First global authority marker crossed the actual legacy checkpoint transaction'
            assert not future.done()
        return result
    monkeypatch.setattr(chat_store, '_owned_access', last_checked)
    event.listen(engine, 'before_cursor_execute', sql)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            # A transaction already admitted under the barrier commits first.
            # The following marker then permanently retires this old publisher.
            message_id = chat_store.save_assistant_progress(sid, None, 'committed before cutover', [], False, access=access)
            assert message_id is not None and len(futures) == 1
            futures[0].result(timeout=5)
    finally:
        event.remove(engine, 'before_cursor_execute', sql)
    assert db.scalar(select(func.count()).select_from(ChatMessage)) == 1
    with pytest.raises(HTTPException):
        chat_store.save_assistant_progress(sid, message_id, 'forbidden after cutover', [], False, access=access)
    assert db.get(ChatMessage, message_id).content == 'committed before cutover'
