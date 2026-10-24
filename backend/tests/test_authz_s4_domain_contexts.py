"""Cross-domain extraction cannot accidentally inherit maintenance fallback."""
from fastapi import HTTPException
import pytest
from sqlalchemy.orm import Session

from app.authz import domain_contexts as core
from app.authz.context import ObjectRef, read_snapshot
from app.services import maintenance_authorization as maintenance
from tests.test_authz_s3_project_foundation import projects  # noqa: F401


def test_compatibility_reexports_are_same_dispatch_and_single_registry():
    for name in ('DocumentAccess', 'LockedContextBatch', 'resolve_entry_context',
                 'read_context_snapshot', 'register_entry_resolver', 'revalidate_dependency',
                 '_ENTRY_RESOLVERS', '_REFERENCE_RESOLVERS', '_DOCUMENT_RESOLVERS', '_QUERY_BINDINGS'):
        assert getattr(core, name) is getattr(maintenance, name)


@pytest.mark.parametrize('entry', ['GET /api/maintenance/projects/stable/{project_id}',
                                  'GET /api/maintenance/projects/stable'])
def test_unregistered_project_or_collection_does_not_get_implicit_maintenance_resolver(db, projects, monkeypatch, entry):
    _, identity, ids = projects
    monkeypatch.delitem(core._ENTRY_RESOLVERS, entry)
    with read_snapshot(lambda: Session(db.get_bind())) as (reader, proof):
        with pytest.raises(HTTPException) as error:
            core.resolve_entry_context(reader, identity, entry_id=entry,
                params={'project_id': ids['manager']}, read_snapshot=proof)
    assert error.value.status_code == 503
    assert error.value.detail['code'] == 'AUTHZ_DOMAIN_RESOLVER_UNAVAILABLE'


def test_historical_project_requires_explicit_reference_registration(db, projects, monkeypatch):
    _, identity, ids = projects
    entry = 'GET /api/maintenance/projects/stable/{project_id}'
    monkeypatch.delitem(core._REFERENCE_RESOLVERS, entry)
    with read_snapshot(lambda: Session(db.get_bind())) as (reader, proof):
        with pytest.raises(HTTPException) as error:
            core.revalidate_dependency(reader, identity, ObjectRef('project', ids['manager']),
                entry_id=entry, branch='default', actions_by_resource={'maintenance_project':['read']}, read_snapshot=proof)
    assert error.value.status_code == 403


def test_reference_registration_conflict_is_atomic_and_same_callable_is_idempotent(monkeypatch):
    entry = 'GET /api/maintenance/projects/stable/{project_id}'
    original = core._ENTRY_RESOLVERS[entry]
    reference = core._REFERENCE_RESOLVERS[entry]
    core.register_entry_resolver(entry, original, reference_resolver=reference)
    with pytest.raises(ValueError): core.register_reference_resolver(entry, lambda *a, **k: None)
    monkeypatch.delitem(core._ENTRY_RESOLVERS, entry)
    with pytest.raises(ValueError):
        core.register_entry_resolver(entry, lambda *a, **k: None, reference_resolver=lambda *a, **k: None)
    assert entry not in core._ENTRY_RESOLVERS
    assert core._REFERENCE_RESOLVERS[entry] is reference


def test_cross_session_proof_rejected_before_domain_callback(db, projects):
    _, identity, ids = projects
    with read_snapshot(lambda: Session(db.get_bind())) as (reader, proof):
        with Session(db.get_bind()) as wrong:
            with pytest.raises(HTTPException) as error:
                core.resolve_entry_context(wrong, identity,
                    entry_id='GET /api/maintenance/projects/stable/{project_id}',
                    params={'project_id': ids['manager']}, read_snapshot=proof)
    assert error.value.status_code == 409
