"""Private filesystem binding, data-source inheritance, and exclusive publication."""
import json
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.provenance import AgentAccess, execution_access
from app.models.authorization import SysAuthorizationArtifact, SysAuthorizationGrant
from app.models.system import SysUser
from app.services import agent_authorization as authz, agent_files
from app.services import agent_file_authorization as files
from tests.test_authz_s4_agent import agent  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


def _grant(db, user, actions):
    row = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == user.id,
        SysAuthorizationGrant.resource == 'agent_file'))
    row.actions = actions
    db.commit()  # Structural rights fixture; production changes must bump the epoch.


def test_generation_does_not_implicitly_grant_preview_or_download(db, agent):
    client, user, identity = agent
    _grant(db, user, ['generate'])
    access = AgentAccess(db.get_bind(), identity)
    access.bind_execution(authz.ExecutionContract(authz.CHAT, {'messages': [{'role': 'user', 'content': 'report'}]}))
    with execution_access(access):
        result = agent_files.write_report('safe', ['n'], [[1]], None, identity.username)
    assert set(result) == {'file_id', 'ext'}
    assert client.get(f"/api/agent/files/{result['file_id']}/preview").status_code == 403
    assert client.get(f"/api/agent/files/{result['file_id']}").status_code == 403
    row = db.get(SysAuthorizationArtifact, ('agent_file', result['file_id']))
    assert row.authorization_provenance['resources'] == []


def test_upload_requires_no_chat_use_and_denied_preview_does_not_materialize_metadata(db, agent, monkeypatch):
    client, user, _ = agent
    grant = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == user.id,
        SysAuthorizationGrant.resource == 'agent_chat'))
    grant.actions = []
    _grant(db, user, ['upload'])
    result = client.post('/api/agent/upload', files={'file': ('secret-title.txt', b'secret body', 'text/plain')})
    assert result.status_code == 200, result.text
    def private_metadata(*args, **kwargs):
        pytest.fail('private filename/source/content metadata loaded before current preview allow')
    monkeypatch.setattr(files, '_metadata', private_metadata)
    assert client.get(f"/api/agent/files/{result.json()['file_id']}/preview").status_code == 403
    assert client.get(f"/api/agent/files/{result.json()['file_id']}").status_code == 403


def test_publication_failure_removes_only_new_candidate_and_rolls_back_binding(db, agent, monkeypatch):
    client, _, _ = agent
    original = files.artifacts.register_artifact_binding
    def fail_after_actual_binding(*args, **kwargs):
        result = original(*args, **kwargs)
        raise RuntimeError('synthetic after-binding failure')
    monkeypatch.setattr(files.artifacts, 'register_artifact_binding', fail_after_actual_binding)
    with pytest.raises(RuntimeError, match='after-binding'):
        client.post('/api/agent/upload', files={'file': ('new.txt', b'new', 'text/plain')})
    assert list(files.directory().glob('*')) == []
    assert db.scalar(select(SysAuthorizationArtifact.domain_id).where(SysAuthorizationArtifact.domain_kind == 'agent_file')) is None


def test_file_id_collision_never_overwrites_or_compensates_existing_file(db, agent, monkeypatch):
    client, _, _ = agent
    monkeypatch.setattr(agent_files, 'uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex='a' * 32)))
    first = client.post('/api/agent/upload', files={'file': ('original.txt', b'original', 'text/plain')})
    assert first.status_code == 200, first.text
    before = {path.name: path.read_bytes() for path in files.directory().iterdir()}
    with pytest.raises(FileExistsError):
        client.post('/api/agent/upload', files={'file': ('replacement.txt', b'changed', 'text/plain')})
    assert {path.name: path.read_bytes() for path in files.directory().iterdir()} == before
    assert client.get(f"/api/agent/files/{first.json()['file_id']}").content == b'original'


def test_derived_file_revalidates_actual_base_bytes_not_just_grant(db, agent):
    client, _, identity = agent
    source = client.post('/api/agent/upload', files={'file': ('source.txt', b'private source', 'text/plain')}).json()
    access = AgentAccess(db.get_bind(), identity)
    access.bind_execution(authz.ExecutionContract(authz.CHAT, {'messages': [{'role': 'user', 'content': 'derive'}]}))
    with execution_access(access):
        assert agent_files.read_document(source['file_id'])['content'] == 'private source'
        result = agent_files.write_report('derived', ['content'], [['private source']], None, identity.username)
    assert client.get(f"/api/agent/files/{result['file_id']}").status_code == 200
    files.paths(source['file_id'], source['ext']).write_bytes(b'tampered source')
    assert client.get(f"/api/agent/files/{result['file_id']}").status_code == 403


@pytest.mark.parametrize('target', ['metadata', 'header', 'binding'])
def test_immutable_file_carrier_tampering_denied(db, agent, target):
    client, _, _ = agent
    fid = client.post('/api/agent/upload', files={'file': ('file.txt', b'secret', 'text/plain')}).json()['file_id']
    if target == 'binding':
        row = db.get(SysAuthorizationArtifact, ('agent_file', fid))
        row.input_digest = 'f' * 64
        db.commit()
    else:
        path = files.paths(fid) if target == 'metadata' else files.directory() / (fid + '.auth.json')
        value = json.loads(path.read_bytes())
        value['filename' if target == 'metadata' else 'owner_user_id'] = 'changed' if target == 'metadata' else 99999
        path.write_text(json.dumps(value))
    assert client.get(f'/api/agent/files/{fid}').status_code == 403
