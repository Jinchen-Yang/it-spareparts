"""Root review regressions for the independently authored Agent lifecycle."""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from app.api import agent
from app.authz import runtime
from app.authz.provenance import AgentAccess, execution_access
from app.config import get_settings
from app.models.authorization import SysAuthorizationCutover, SysAuthorizationMigrationReceipt
from app.services import agent_files, chat_store
from tests.test_authz_s2_agent_provenance import setup_agent, cerbos_url  # noqa: F401


def _legacy(db, monkeypatch):
    db.execute(delete(SysAuthorizationCutover))
    db.execute(delete(SysAuthorizationMigrationReceipt).where(
        SysAuthorizationMigrationReceipt.entity_kind == 'artifact_protocol'))
    db.commit()
    settings = get_settings()
    monkeypatch.setattr(settings, 'authorization_mode', 'legacy_compat')
    monkeypatch.setattr(settings, 'authz_enabled_resources', [])


def _cutover(db, monkeypatch):
    monkeypatch.setattr(get_settings(), 'authorization_mode', 'unified_modules')
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', ['inventory'])
    runtime.activate_configured_resources(db.get_bind())


def test_legacy_download_cannot_finish_after_persistent_cutover(db, setup_agent, monkeypatch):
    user, identity, _, ctx = setup_agent
    _legacy(db, monkeypatch)
    uploaded = agent_files.save_upload(b'a' * 200_000, 'large.txt', identity.username)
    response = agent.download(uploaded['file_id'], db=db, role=user.role, ctx=ctx, identity=identity)

    async def consume():
        assert len(await anext(response.body_iterator)) == 65536
        _cutover(db, monkeypatch)
        with pytest.raises(HTTPException):
            await anext(response.body_iterator)

    asyncio.run(consume())


def test_legacy_file_vision_result_cannot_cross_cutover(db, setup_agent, monkeypatch):
    from app.agent import provider
    _, identity, _, _ = setup_agent
    _legacy(db, monkeypatch)
    uploaded = agent_files.save_upload(b'image', 'scan.png', identity.username)
    def vision(paths, hint):
        _cutover(db, monkeypatch)
        return 'private after cutover'
    monkeypatch.setattr(provider, 'vision_extract', vision)
    with pytest.raises(HTTPException):
        agent_files.read_document(uploaded['file_id'])


def test_canonical_session_ownership_uses_stable_id_not_old_display_subject(db, setup_agent):
    _, identity, access, _ = setup_agent
    session = chat_store.create_session(db, identity.username, access=access)
    session.owner_sub = 'historical-name-before-rename'
    db.commit()
    assert chat_store.get_owned(db, identity.username, session.id, access=access).id == session.id
    assert chat_store.list_sessions(db, identity.username, access=access)[0]['id'] == session.id


def test_file_metadata_publication_checks_legacy_cutover_after_serialization(db, setup_agent, monkeypatch):
    _, identity, _, _ = setup_agent
    _legacy(db, monkeypatch)
    original = agent_files.json.dumps
    def serialized(value, **kwargs):
        result = original(value, **kwargs)
        if isinstance(value, dict) and value.get('kind') == 'upload':
            _cutover(db, monkeypatch)
        return result
    monkeypatch.setattr(agent_files.json, 'dumps', serialized)
    with execution_access(AgentAccess(db.get_bind(), identity)), pytest.raises(HTTPException):
        agent_files.save_upload(b'input', 'note.txt', identity.username)
    assert not list(agent_files._dir().glob('*.meta.json'))
    assert not list(agent_files._dir().glob('*.tmp'))


@pytest.mark.parametrize('operation', ['append', 'checkpoint'])
def test_artifact_marker_transaction_precedes_chat_actor_lock(db, setup_agent, monkeypatch, operation):
    """A downgrade locks the receipt table before users; never wait for it holding a user lock."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.models.system import SysUser
    user, identity, access, _ = setup_agent
    session = chat_store.create_session(db, identity.username, access=access)
    sid, uid = session.id, user.id
    db.rollback()
    original = runtime.mark_artifact_protocol_used
    calls = []
    def mark(bind):
        # Separate backend connection proves no actor SHARE is retained here.
        with Session(bind) as other:
            assert other.scalar(select(SysUser.id).where(SysUser.id == uid)
                                .with_for_update(nowait=True)) == uid
        calls.append(True)
        original(bind)
    monkeypatch.setattr(runtime, 'mark_artifact_protocol_used', mark)
    if operation == 'append':
        chat_store.append_message(db, session, 'assistant', 'safe', access=access)
    else:
        chat_store.save_assistant_progress(sid, None, 'safe', [], False, access=access)
    assert calls == [True]
