"""Acceptance evidence: current project grants, stable owners and complete bytes."""
from hashlib import sha256
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.authz import service
from app.authz.context import ObjectRef, create_context_batch, relation_revision, read_snapshot
from app.authz.errors import authz_error
from app.models.maintenance_manager import BusinessFile, BusinessFileLink, MaintenanceAcceptanceDeliverable
from app.models.maintenance_project import MaintenanceProject
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation
from app.services import maintenance_acceptance as business
from app.services.maintenance_workflow_authorization import require_complete, captured_provenance

PROJECT = '/api/maintenance/projects/stable/{project_id}/acceptance'
READ = 'GET ' + PROJECT
UPLOAD = 'POST ' + PROJECT + '/attachments'
SUBMIT = 'POST ' + PROJECT + '/submit'
DELETE = 'DELETE ' + PROJECT + '/attachments/{file_id}'
DOWNLOAD = 'GET /api/maintenance/acceptance-files/{file_id}'
SEARCH = 'POST /api/maintenance/acceptance-deliverables/search'


def _file(db, file_id):
    rows = list(db.execute(select(BusinessFile, BusinessFileLink, MaintenanceAcceptanceDeliverable)
        .options(load_only(MaintenanceAcceptanceDeliverable.deliverable_id,
            MaintenanceAcceptanceDeliverable.project_id,raiseload=True))
        .join(BusinessFileLink, BusinessFileLink.file_id == BusinessFile.file_id)
        .join(MaintenanceAcceptanceDeliverable, MaintenanceAcceptanceDeliverable.deliverable_id == BusinessFileLink.entity_id)
        .where(BusinessFile.file_id == file_id, BusinessFile.security_state == 'active',
               BusinessFileLink.entity_type == 'maintenance_acceptance_deliverable', BusinessFileLink.archived_at.is_(None))
        .execution_options(populate_existing=True)))
    if not rows:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '验收附件不存在或已归档')
    if len(rows) != 1:
        raise artifacts.unavailable()
    return rows[0]


def _file_digest(file, link, deliverable):
    return relation_revision({'file_id': file.file_id, 'version': file.version,
        'owner_user_id': file.owner_user_id, 'object_key': file.object_key, 'storage_provider': file.storage_provider,
        'sha256': file.sha256, 'size_bytes': file.size_bytes, 'filename': file.original_filename,
        'mime_type': file.mime_type, 'security_state': file.security_state,
        'link_id': link.link_id, 'entity_id': link.entity_id, 'project_id': deliverable.project_id})


def _file_context(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    file, link, deliverable = _file(db, params['file_id'])
    if params.get('project_id') is not None and params['project_id'] != deliverable.project_id:
        raise authz_error(403, 'AUTHZ_FORBIDDEN', '附件不属于当前项目')
    _, projects = foundation.resolve_project_subjects(db, identity, [deliverable.project_id],
        read_snapshot=read_snapshot, locked_user=locked_user)
    parent = projects[deliverable.project_id]
    ref = foundation.slot_ref(entry_id, 'context_0', file.file_id)
    return create_context_batch(db, [foundation.resolved_context(db, entry_id=entry_id,
        branch='default', carrier=ref, slots={'context_0': (ref,)},
        subjects={'maintenance_acceptance': (parent.subject,)},
        revision=relation_revision({'parent': parent.authorization_revision, 'file': _file_digest(file, link, deliverable)}))])


def _reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    if ref.kind != 'business_file':
        raise artifacts.unavailable()
    return _file_context(db, identity, entry_id=entry_id, params={'file_id': ref.id},
        branch=branch, read_snapshot=read_snapshot, locked_user=locked_user)


for entry in (DOWNLOAD, DELETE):
    foundation.register_entry_resolver(entry, _file_context, reference_resolver=_reference)


def _file_state(db, target):
    file, link, deliverable = _file(db, target.id)
    if file.owner_user_id is None or target.generation != file.version:
        raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(artifacts.ArtifactTarget('business_file', file.file_id, file.version),
        ObjectRef('business_file', file.file_id), _file_digest(file, link, deliverable), file.owner_user_id)


def _producer_ref(db, state):
    _, _, deliverable = _file(db, state.target.id)
    return foundation.slot_ref(UPLOAD, 'context_0', deliverable.project_id)


def _document(db, identity, ref, *, entry_id, format_purpose, read_snapshot=None, locked_user=None):
    file, link, deliverable = _file(db, ref.id)
    if entry_id not in (DOWNLOAD, UPLOAD) or format_purpose != 'attachment_download':
        raise artifacts.unavailable()
    params = {'project_id': deliverable.project_id} if entry_id == UPLOAD else {'file_id': ref.id}
    batch = foundation.resolve_entry_context(db, identity, entry_id=entry_id, params=params,
        read_snapshot=read_snapshot, locked_user=locked_user)
    accesses = service.context_accesses(db, identity, batch, read_snapshot=read_snapshot, locked_user=locked_user)
    require_complete(accesses)
    parent = foundation.slot_ref(UPLOAD, 'context_0', deliverable.project_id)
    return foundation.DocumentAccess(ref, batch.contexts[0].authorization_revision,
        tuple(accesses.accesses.values()), (ref, parent), _file_digest(file, link, deliverable))


_PROJECTION = {'version': 1, 'content': 'entire_uploaded_binary', 'redaction': 'none'}
artifacts.register_artifact_domain('business_file', artifacts.ArtifactDomainAdapter(
    _file_state, _document, (artifacts.ArtifactFormat('maintenance_attachment_v1', 'attachment_download', DOWNLOAD,
        _PROJECTION, lambda content: _PROJECTION,
        producer_entry_id=UPLOAD, producer_carrier_kind='project', producer_ref=_producer_ref),), private=False))


def view(db, identity, *, entry_id, params, precondition=None, require=False):
    with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=entry_id, params=params) as (_, _, batch, accesses):
        if require:
            require_complete(accesses)
            service.check_context_precondition(precondition, accesses, batch.contexts[0].ref)
        return service.context_resolution(accesses, batch.contexts[0].ref)


def read(db, identity, *, entry_id, params, legacy_context, authorization_view=False, precondition=None):
    with foundation.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=entry_id, params=params) as (snapshot, _, batch, accesses):
        ref = batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, ref)
        if entry_id == SEARCH:
            access = accesses.require(ref, 'maintenance_acceptance_directory', 'search')
            result = business.search_acceptance(snapshot, user_ctx=legacy_context,
                authorization_condition=foundation.project_condition(access), **params)
        else:
            result = business.project_acceptance(snapshot, project_id=params['project_id'])
        return {**result, 'authorization_context': accesses.wire(ref)}


def _locks(db, project_id):
    project = db.scalar(select(MaintenanceProject).where(MaintenanceProject.project_id == project_id)
        .with_for_update().execution_options(populate_existing=True))
    if project is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '维保项目不存在')
    deliverables = list(db.scalars(select(MaintenanceAcceptanceDeliverable)
        .where(MaintenanceAcceptanceDeliverable.project_id == project_id)
        .order_by(MaintenanceAcceptanceDeliverable.deliverable_id).with_for_update().execution_options(populate_existing=True)))
    ids = [row.deliverable_id for row in deliverables]
    if ids:
        links = list(db.scalars(select(BusinessFileLink).where(BusinessFileLink.entity_id.in_(ids))
            .order_by(BusinessFileLink.link_id).with_for_update().execution_options(populate_existing=True)))
        if links:
            list(db.scalars(select(BusinessFile).where(BusinessFile.file_id.in_([row.file_id for row in links]))
                .order_by(BusinessFile.file_id).with_for_update().execution_options(populate_existing=True)))


def _verify_file_binding(db, file):
    if file.owner_user_id is None:
        raise artifacts.unavailable()
    return artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget('business_file', file.file_id, file.version))


def command(db, identity, *, entry_id, params, precondition=None, **payload):
    # Short read gate precedes all writes and byte publication. Domain locks then
    # serialize first-deliverable creation and collect every existing file/link.
    view(db, identity, entry_id=entry_id, params=params, precondition=precondition, require=True)
    if entry_id == UPLOAD:
        artifacts.begin_artifact_protocol(db.get_bind())
    _locks(db, params['project_id'])
    actor = service.lock_actor(db, identity)
    batch = foundation.resolve_entry_context(db, identity, entry_id=entry_id, params=params, locked_user=actor)
    accesses = service.context_accesses(db, identity, batch, locked_user=actor)
    require_complete(accesses)
    service.check_context_precondition(precondition, accesses, batch.contexts[0].ref)
    if entry_id == UPLOAD:
        path = None
        try:
            result, path = business.upload_attachment(db, project_id=params['project_id'], operator=actor.username,
                owner_user_id=actor.id, verify_file_binding=lambda file: _verify_file_binding(db, file), **payload)
            if not result['replayed']:
                target = artifacts.ArtifactTarget('business_file', result['file_id'], 1)
                state = _file_state(db, target)
                binding = artifacts.register_artifact_binding(db, identity, target=target,
                    input_digest=state.input_digest, provenance=captured_provenance(accesses), locked_user=actor)
                artifacts.publish_artifact(db, identity, binding=binding, purpose='attachment_download',
                    content=payload['content'], expected_generation=1, locked_user=actor)
            return result, path
        except BaseException:
            if path is not None:
                path.unlink(missing_ok=True)
            raise
    if entry_id == SUBMIT:
        return business.submit_acceptance(db, project_id=params['project_id'], operator=actor.username,
            expected_version=params['expected_version'], owner_user_id=actor.id, **payload)
    if entry_id == DELETE:
        return business.archive_attachment(db, project_id=params['project_id'], file_id=params['file_id'],
            operator=actor.username, owner_user_id=actor.id)
    raise ValueError('Unknown acceptance command')


def download(db, identity, file_id, *, precondition=None):
    # Audit is an independent append after the read observation; never write in RR.
    with read_snapshot(lambda: Session(db.get_bind())) as (snapshot, proof):
        file, link, deliverable = _file(snapshot, file_id)
        target = artifacts.ArtifactTarget('business_file', file_id, file.version)
        manifest = artifacts.require_artifact(snapshot, identity, target=target, purpose='attachment_download',
            read_snapshot=proof, authorization_precondition=precondition)
        content, _ = business.controlled_download(snapshot, file_id=file_id, operator=identity.username, audit=False)
        if sha256(content).hexdigest() != manifest['content_sha256']:
            raise artifacts.unavailable()
        metadata = {'file_id': file_id, 'link_id': link.link_id, 'deliverable_id': deliverable.deliverable_id,
            'project_id': deliverable.project_id, 'downloaded_by': identity.username,
            'sha256_at_download': file.sha256}
        info = {'filename': file.original_filename, 'mime_type': file.mime_type, 'project_id': deliverable.project_id}
    from app.models.maintenance_manager import BusinessFileDownloadAudit
    db.add(BusinessFileDownloadAudit(**metadata))
    db.flush()
    return content, info
