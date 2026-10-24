"""Independent exact-entry dispatch poison tests; actual domain batch, no fake proof."""
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.authz import domain_contexts as core
from app.authz.context import read_snapshot
from tests.test_authz_s3_project_foundation import projects  # noqa: F401

READ = 'GET /api/maintenance/projects/stable/{project_id}'
UPDATE = 'PATCH /api/maintenance/projects/stable/{project_id}'


@pytest.mark.parametrize('requested_entry,requested_branch', [(UPDATE, 'default'), (READ, 'authorization_view')])
def test_registered_dispatch_cannot_return_another_entry_or_branch_batch(db, projects, monkeypatch, requested_entry, requested_branch):
    _, identity, ids = projects
    with read_snapshot(lambda: Session(db.get_bind())) as (reader, proof):
        actual = core.resolve_entry_context(reader, identity, entry_id=READ,
            params={'project_id': ids['manager']}, branch='default', read_snapshot=proof)
        assert actual.contexts[0].entry_id == READ and actual.contexts[0].branch == 'default'
        # Deliberate adapter regression: a valid batch from a different registered contract.
        monkeypatch.setitem(core._ENTRY_RESOLVERS, requested_entry, lambda *a, **kw: actual)
        with pytest.raises((HTTPException, ValueError)):
            core.resolve_entry_context(reader, identity, entry_id=requested_entry,
                params={'project_id': ids['manager']}, branch=requested_branch, read_snapshot=proof)


def test_history_reference_cannot_relabel_a_valid_context_entry(db, projects, monkeypatch):
    _, identity, ids = projects
    with read_snapshot(lambda: Session(db.get_bind())) as (reader, proof):
        actual = core.resolve_entry_context(reader, identity, entry_id=READ,
            params={'project_id': ids['manager']}, branch='default', read_snapshot=proof)
        monkeypatch.setitem(core._REFERENCE_RESOLVERS, UPDATE, lambda *a, **kw: actual)
        with pytest.raises((HTTPException, ValueError)):
            core.revalidate_dependency(reader, identity, actual.contexts[0].ref,
                entry_id=UPDATE, branch='default', actions_by_resource={'maintenance_project': ['read']}, read_snapshot=proof)
