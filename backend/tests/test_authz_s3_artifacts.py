"""Real PostgreSQL/PDP tests of sidecar lifecycle, using a registered test domain."""
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.authz import catalog, provenance, runtime, service
from app.authz.context import ObjectRef, read_snapshot
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.models.authorization import SysAuthorizationArtifact, SysAuthorizationGrant
from app.models.system import SysImportBatch, SysUser
from app.services import authorization_artifacts as artifacts
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@dataclass(frozen=True)
class TestDocument:
    __test__ = False
    ref: ObjectRef
    authorization_revision: str
    accesses: tuple
    content_manifest: tuple
    content_revision: str


@pytest.fixture
def domain(db, monkeypatch, cerbos_url):
    settings = get_settings()
    for name, value in {'authorization_mode': 'unified_modules',
        'authz_enabled_resources': ['inventory'], 'authz_suspended_resources': [],
        'authz_sales_analysis_enabled': False, 'authz_sales_analysis_suspended': False,
        'cerbos_url': cerbos_url, 'cerbos_timeout_seconds': 2.0}.items():
        monkeypatch.setattr(settings, name, value)
    runtime.activate_configured_resources(db.get_bind())
    artifacts.begin_artifact_protocol(db.get_bind())
    user = SysUser(username='artifact-owner', role='readonly', password_hash='synthetic')
    db.add(user)
    db.flush()
    db.add(SysAuthorizationGrant(user_id=user.id, resource='inventory', actions=['read'],
        scope='all', field_permissions=[], version=1))
    batch = SysImportBatch(filename='synthetic', file_type='authz_test', file_hash='a' * 64,
        status='processing', uploaded_by=user.username,
        report_json={'owner_user_id': user.id, 'generation': 1})
    db.add(batch)
    db.commit()
    identity = VerifiedIdentity(user.username, user.token_version, False, 'sys_user', MappingProxyType({}), user.id)
    target = artifacts.ArtifactTarget('test_document', str(batch.id), 1)

    def load(session, target):
        actual = session.get(SysImportBatch, int(target.id), populate_existing=True)
        if actual is None or actual.file_type != 'authz_test':
            raise artifacts.unavailable()
        return artifacts.ArtifactDomainState(
            artifacts.ArtifactTarget(target.kind, str(actual.id), actual.report_json['generation']),
            ObjectRef('workbook_validation', str(actual.id)), actual.file_hash,
            actual.report_json['owner_user_id'])

    def authorize(session, actor, ref, *, entry_id, format_purpose, read_snapshot=None, locked_user=None):
        # This test domain's only content is inventory. A real PDP decision is
        # required; no HTTP facade or allow boolean is patched.
        access = service.resource_access(session, actor, 'inventory', 'read',
            read_snapshot=read_snapshot, locked_user=locked_user)
        assert entry_id == 'test inventory document' and format_purpose == 'download'
        return TestDocument(ref, 'actual-domain-revision-1', (access,), (ref,), 'actual-content-revision-1')

    def inspect_csv(content):
        return {'sheets': [{'name': 'stock', 'columns': content.decode().splitlines()[0].split(',')}],
                'hidden_columns': [], 'contract_version': 1}

    descriptor = artifacts.ArtifactFormat('test_inventory_v1', 'download',
        'test inventory document', {'sheets': [{'name': 'stock', 'columns': ['pn', 'qty']}],
                                  'hidden_columns': [], 'contract_version': 1}, inspect_csv)
    adapter = artifacts.ArtifactDomainAdapter(load, authorize, (descriptor,), private=True)
    monkeypatch.setitem(artifacts._DOMAINS, target.kind, adapter)
    return user.id, identity, target, batch.id


def capture(db, identity):
    access = service.resource_access(db, identity, 'inventory', 'read')
    collector = provenance.AccessCollector(access.principal.id, access.authorization_epoch, catalog.CATALOG_VERSION)
    collector.record(access)
    return collector.snapshot()


def register(db, identity, target, *, complete=True):
    return artifacts.register_artifact_binding(db, identity, target=target,
        input_digest='a' * 64, operation=artifacts.IdempotentOperation('test_upload', 'request-1'),
        provenance=capture(db, identity) if complete else None)


def test_business_and_sidecar_rollback_together(db, domain):
    _, identity, target, bid = domain
    register(db, identity, target, complete=False)
    db.get(SysImportBatch, bid).status = 'failed'
    db.rollback()
    with Session(db.get_bind()) as other:
        assert other.get(SysAuthorizationArtifact, (target.kind, target.id)) is None
        assert other.get(SysImportBatch, bid).status == 'processing'


def test_same_owner_rename_preserves_binding_and_namespace(db, domain):
    uid, identity, target, _ = domain
    binding = register(db, identity, target)
    db.commit()
    user = db.get(SysUser, uid)
    user.username = 'renamed-owner'
    db.commit()
    resumed = artifacts.resume_artifact_task(db, target=target)
    assert resumed.expected_user_id == uid and resumed.username == 'renamed-owner'
    assert artifacts.load_artifact_binding(db, target=target).subject == binding.subject
    assert artifacts.stable_operation_key(uid, 'test_upload', 'request-1') == artifacts.stable_operation_key(uid, 'test_upload', 'request-1')
    assert artifacts.stable_operation_key(uid, 'test_upload', 'request-1') != artifacts.stable_operation_key(uid + 1, 'test_upload', 'request-1')


def test_unknown_domain_and_mismatched_actual_row_fail_closed(db, domain):
    _, identity, target, bid = domain
    with pytest.raises(HTTPException):
        register(db, identity, artifacts.ArtifactTarget('not_registered', target.id, 1), complete=False)
    register(db, identity, target, complete=False)
    db.get(SysImportBatch, bid).file_hash = 'b' * 64
    db.flush()
    with pytest.raises(HTTPException):
        artifacts.load_artifact_binding(db, target=target)


def test_incomplete_provenance_cannot_publish_or_resume(db, domain):
    _, identity, target, _ = domain
    binding = register(db, identity, target, complete=False)
    with pytest.raises(HTTPException):
        artifacts.publish_artifact(db, identity, binding=binding, purpose='download',
            content=b'private', expected_generation=1)
    with pytest.raises(HTTPException):
        artifacts.resume_artifact_task(db, target=target)
    assert db.get(SysAuthorizationArtifact, (target.kind, target.id)).artifact_manifests is None


def test_real_pdp_publish_then_read_bytes_and_revoke(db, domain):
    uid, identity, target, _ = domain
    binding = register(db, identity, target)
    manifest = artifacts.publish_artifact(db, identity, binding=binding, purpose='download',
        content=b'pn,qty\nPN,2', expected_generation=1)
    assert manifest['content_sha256'] == sha256(b'pn,qty\nPN,2').hexdigest()
    db.commit()
    factory = lambda: Session(db.get_bind())
    assert artifacts.read_artifact_bytes(factory, identity, target=target,
        purpose='download', read_bytes=lambda *_: b'pn,qty\nPN,2') == b'pn,qty\nPN,2'
    with pytest.raises(HTTPException):
        artifacts.read_artifact_bytes(factory, identity, target=target,
            purpose='download', read_bytes=lambda *_: b'different bytes')
    db.get(SysUser, uid).token_version += 1
    db.commit()
    with pytest.raises(HTTPException):
        artifacts.read_artifact_bytes(factory, identity, target=target,
            purpose='download', read_bytes=lambda *_: b'pn,qty\nPN,2')


def test_no_shared_private_read_or_foreign_snapshot_proof(db, domain):
    _, identity, target, _ = domain
    binding = register(db, identity, target)
    artifacts.publish_artifact(db, identity, binding=binding, purpose='download',
        content=b'pn,qty\nPN,2', expected_generation=1)
    other = SysUser(username='different-admin', role='admin', password_hash='synthetic')
    db.add(other)
    db.commit()
    stranger = VerifiedIdentity(other.username, other.token_version, False, 'sys_user', MappingProxyType({}), other.id)
    with read_snapshot(lambda: Session(db.get_bind())) as (snapshot, proof):
        with pytest.raises(HTTPException):
            artifacts.require_artifact(snapshot, stranger, target=target, purpose='download', read_snapshot=proof)
        with pytest.raises((ValueError, HTTPException)):
            artifacts.require_artifact(db, identity, target=target, purpose='download', read_snapshot=proof)


def test_retry_generation_prevents_old_worker_publication(db, domain):
    _, identity, target, bid = domain
    old = register(db, identity, target)
    current = capture(db, identity)
    actual = db.get(SysImportBatch, bid)
    actual.report_json = {**actual.report_json, 'generation': 2}
    db.flush()
    fresh = artifacts.restart_artifact_task(db, identity, binding=old,
        expected_generation=1, provenance=current)
    assert fresh.target.generation == 2 and fresh.manifests is None
    with pytest.raises(HTTPException) as denied:
        artifacts.publish_artifact(db, identity, binding=old, purpose='download',
            content=b'pn,qty\nold', expected_generation=1)
    assert denied.value.status_code == 409
    artifacts.publish_artifact(db, identity, binding=fresh, purpose='download',
        content=b'pn,qty\nnew', expected_generation=2)


def test_same_generation_purpose_cannot_be_rewritten(db, domain):
    _, identity, target, _ = domain
    binding = register(db, identity, target)
    artifacts.publish_artifact(db, identity, binding=binding, purpose='download',
        content=b'pn,qty\noriginal', expected_generation=1)
    current = artifacts.load_artifact_binding(db, target=target)
    with pytest.raises(HTTPException) as denied:
        artifacts.publish_artifact(db, identity, binding=current, purpose='download',
            content=b'pn,qty\nchanged', expected_generation=1)
    assert denied.value.status_code == 409
    assert db.get(SysAuthorizationArtifact, (target.kind, target.id)).artifact_manifests['download']['content_sha256'] == sha256(b'pn,qty\noriginal').hexdigest()


def test_extra_column_cannot_hide_behind_registered_projection_digest(db, domain):
    _, identity, target, _ = domain
    binding = register(db, identity, target)
    with pytest.raises(HTTPException):
        artifacts.publish_artifact(db, identity, binding=binding, purpose='download',
            content=b'pn,qty,secret_cost\nPN,2,800', expected_generation=1)
    assert db.get(SysAuthorizationArtifact, (target.kind, target.id)).artifact_manifests is None


@pytest.fixture
def shared_project(db, domain, monkeypatch):
    """Actual project, assignments and per-reader PDP Grants; no static revision."""
    from uuid import uuid4
    from app.authz.context import relation_revision
    from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectUserAssignment
    from app.services import maintenance_authorization as foundation
    from app.services import maintenance_workflow_authorization as workflows
    uid, owner, _, _ = domain
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', ['inventory', 'maintenance_acceptance'])
    runtime.activate_configured_resources(db.get_bind())
    reader = SysUser(username='shared-artifact-reader', role='readonly', password_hash='synthetic')
    db.add(reader)
    db.flush()
    db.add_all([
        SysAuthorizationGrant(user_id=uid, resource='maintenance_acceptance', actions=['read_checklist'],
            scope='all_projects', field_permissions=[], version=7),
        SysAuthorizationGrant(user_id=reader.id, resource='maintenance_acceptance', actions=['read_checklist'],
            scope='assigned_or_sales_projects', field_permissions=[], version=13),
    ])
    project = MaintenanceProject(project_id=str(uuid4()), project_code='shared-artifact-project',
        display_name='合成共享项目', lifecycle_status='ongoing')
    db.add(project)
    db.flush()
    db.add(MaintenanceProjectUserAssignment(assignment_id=str(uuid4()), project_id=project.project_id,
        responsibility_type='viewer', user_id=reader.id, assigned_by=owner.username, assignment_reason='synthetic'))
    db.commit()
    reader_identity = VerifiedIdentity(reader.username, reader.token_version, False, 'sys_user', MappingProxyType({}), reader.id)
    target = artifacts.ArtifactTarget('test_shared_project', project.project_id, 1)
    entry = 'GET /api/maintenance/projects/stable/{project_id}/acceptance/checklist'
    input_hash = sha256(('project-report-v1:' + project.project_id).encode()).hexdigest()

    def load(session, requested):
        actual = session.get(MaintenanceProject, requested.id, populate_existing=True)
        if actual is None:
            raise artifacts.unavailable()
        return artifacts.ArtifactDomainState(artifacts.ArtifactTarget(requested.kind, actual.project_id, 1),
            ObjectRef('project', actual.project_id), input_hash, None)

    observed = []
    def document(session, identity, ref, *, entry_id, format_purpose, read_snapshot=None, locked_user=None):
        batch = foundation.resolve_entry_context(session, identity, entry_id=entry_id,
            params={'project_id': ref.id}, read_snapshot=read_snapshot, locked_user=locked_user)
        decisions = service.context_accesses(session, identity, batch,
            read_snapshot=read_snapshot, locked_user=locked_user)
        workflows.require_complete(decisions)
        _, evidence = foundation.resolve_project_subjects(session, identity, [ref.id],
            read_snapshot=read_snapshot, locked_user=locked_user)
        project_evidence = evidence[ref.id]
        access = decisions.require(ref, 'maintenance_acceptance', 'read_checklist')
        observed.append((identity.expected_user_id, access.grant_version, project_evidence.authorization_revision,
                         project_evidence.content_revision))
        return foundation.DocumentAccess(ref, project_evidence.authorization_revision,
            (access,), (ref,), relation_revision({'project': project_evidence.content_revision,
                'input': input_hash, 'generation': 1}))

    projection = {'columns': ['project_code'], 'schema_version': 1}
    descriptor = artifacts.ArtifactFormat('shared_project_csv_v1', 'download', entry, projection,
        lambda content: {'columns': content.decode().splitlines()[0].split(','), 'schema_version': 1})
    monkeypatch.setitem(artifacts._DOMAINS, target.kind,
        artifacts.ArtifactDomainAdapter(load, document, (descriptor,), private=False))
    locked = foundation.lock_context_batch(db, owner, entry_id=entry,
        params={'project_id': project.project_id}, lock_domain=workflows.lock_project)
    workflows.require_complete(locked.accesses)
    binding = artifacts.register_artifact_binding(db, owner, target=target, input_digest=input_hash,
        provenance=workflows.captured_provenance(locked.accesses), locked_user=locked.actor)
    content = b'project_code\nshared-artifact-project'
    artifacts.publish_artifact(db, owner, binding=binding, purpose='download', content=content,
        expected_generation=1, locked_user=locked.actor)
    db.commit()
    return owner, reader_identity, target, content, observed


def test_real_project_shared_reader_with_different_grant_and_identity_is_allowed(db, shared_project):
    owner, reader, target, content, observed = shared_project
    assert artifacts.read_artifact_bytes(lambda: Session(db.get_bind()), reader,
        target=target, purpose='download', read_bytes=lambda *_: content) == content
    producer_check, reader_check = observed[0], observed[-1]
    assert producer_check[:2] == (owner.expected_user_id, 7)
    assert reader_check[:2] == (reader.expected_user_id, 13)
    assert producer_check[2] != reader_check[2]  # Current permission observations differ.
    assert producer_check[3] == reader_check[3]  # Actual content topology is identical.


@pytest.mark.parametrize('change', ['relationship', 'grant'])
def test_shared_reader_rechecks_real_topology_and_own_grant(db, shared_project, change):
    from sqlalchemy import select
    from app.models.maintenance_project import MaintenanceProjectUserAssignment
    owner, reader, target, content, _ = shared_project
    if change == 'relationship':
        from uuid import uuid4
        # Reader remains a viewer and keeps today's action. A new real manager
        # changes the complete topology used by the stored file binding.
        db.add(MaintenanceProjectUserAssignment(assignment_id=str(uuid4()), project_id=target.id,
            responsibility_type='primary_manager', user_id=owner.expected_user_id,
            assigned_by=owner.username, assignment_reason='changed actual topology'))
    else:
        row = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == reader.expected_user_id))
        row.actions, row.version = [], row.version + 1
    db.commit()
    with pytest.raises(HTTPException):
        artifacts.read_artifact_bytes(lambda: Session(db.get_bind()), reader,
            target=target, purpose='download', read_bytes=lambda *_: content)


def test_document_precondition_is_checked_before_reading_bytes_in_same_snapshot(db, domain):
    _, identity, target, _ = domain
    binding = register(db, identity, target)
    artifacts.publish_artifact(db, identity, binding=binding, purpose='download',
        content=b'pn,qty\nPN,2', expected_generation=1)
    db.commit()
    condition = {'catalog_version': catalog.CATALOG_VERSION, 'authorization_epoch': identity.token_version,
        'context': {**ObjectRef('workbook_validation', target.id).wire(),
                    'authorization_revision': 'actual-domain-revision-1'}}
    reads = []
    def read_bytes(snapshot, *_):
        assert snapshot.connection().exec_driver_sql('SHOW transaction_read_only').scalar() == 'on'
        reads.append(True)
        return b'pn,qty\nPN,2'
    factory = lambda: Session(db.get_bind())
    assert artifacts.read_artifact_bytes(factory, identity, target=target, purpose='download',
        authorization_precondition=condition, read_bytes=read_bytes).startswith(b'pn,qty')
    assert reads == [True]
    condition['context']['authorization_revision'] = 'stale'
    with pytest.raises(HTTPException) as denied:
        artifacts.read_artifact_bytes(factory, identity, target=target, purpose='download',
            authorization_precondition=condition, read_bytes=read_bytes)
    assert denied.value.status_code == 409
    assert reads == [True]


def test_document_cannot_combine_accesses_from_different_catalogs(db, domain, monkeypatch):
    from dataclasses import replace
    _, identity, target, _ = domain
    binding = register(db, identity, target)
    original = artifacts._DOMAINS[target.kind]
    def incompatible(session, actor, ref, **kwargs):
        document = original.document_access(session, actor, ref, **kwargs)
        return replace(document, accesses=(*document.accesses,
            replace(document.accesses[0], catalog_version='different-catalog')))
    monkeypatch.setitem(artifacts._DOMAINS, target.kind, replace(original, document_access=incompatible))
    with pytest.raises(HTTPException) as denied:
        artifacts.publish_artifact(db, identity, binding=binding, purpose='download',
            content=b'pn,qty\nPN,2', expected_generation=1)
    assert denied.value.status_code == 403
    assert db.get(SysAuthorizationArtifact, (target.kind, target.id)).artifact_manifests is None
