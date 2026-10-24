from app.authz.service import context_resolution
"""Complete v2 workbook authorization, consistent rendering and durable plan origin."""
from datetime import timedelta
import hashlib
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from app.authz import service
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation
from app.services import maintenance_workflow_authorization as authorization
from app.services import maintenance_project_workbook_adapter as adapter_module
from app.services.maintenance_project_workbook_v2 import (
    ProjectWorkbookV2Error, build_project_workbook, validate_project_workbook, build_error_workbook)
from app.models.maintenance_project_operations import (
    MaintenanceProjectWorkbookOperation, MaintenanceProjectWorkbookValidation)


def _read_adapter(parent, db, proof):
    return adapter_module.MaintenanceProjectWorkbookAdapter(db, user_ctx=parent.user_ctx,
        operator=parent.operator, as_of=parent.as_of, identity=parent.identity, read_snapshot=proof,
        authorization_precondition=parent.authorization_precondition)


def _authorize_write(parent, entry_id, params, expected_data_version):
    parent._state(params['project_id'])
    parent._project(params['project_id'])
    if parent._state(params['project_id']).data_version != expected_data_version:
        raise ProjectWorkbookV2Error('项目事实已变化，请重新下载', status_code=409)
    return parent._authorize(entry_id, params, locked=True)


def authorization_view(parent, entry_id, params):
    with foundation.read_context_snapshot(sessionmaker(bind=parent.db.get_bind()), parent.identity,
            entry_id=entry_id, params=params) as (_, _, batch, accesses):
        return context_resolution(accesses, batch.contexts[0].ref)


def export_workbook(parent, project_id, *, hmac_key):
    entry_id = authorization.PROJECT_WORKBOOK_DOWNLOAD
    params = {'project_id': project_id}
    with foundation.read_context_snapshot(sessionmaker(bind=parent.db.get_bind()), parent.identity,
            entry_id=entry_id, params=params) as (db, proof, _, accesses):
        authorization.require_complete(accesses)
        reader = _read_adapter(parent, db, proof)
        reader._authorize(entry_id, params)
        workspace = reader.load_workspace(project_id)
        artifact = build_project_workbook(workspace, hmac_key=hmac_key,
            exported_by=accesses.principal.username, exported_at=adapter_module._now())
        version = workspace['data_version']
        parent._authorized_accesses = accesses
    _authorize_write(parent, entry_id, params, version)
    state = parent._state(project_id)
    state.last_export_id = artifact.export_id
    state.last_exported_at = adapter_module._now()
    digest = hashlib.sha256(artifact.content).hexdigest()
    parent.db.add(MaintenanceProjectWorkbookOperation(project_id=project_id,
        export_id=artifact.export_id, file_sha256=digest, operation_key=f'export:{artifact.export_id}',
        payload_hash=digest, operation_type='file_export', operated_by=parent.operator))
    parent.db.flush()
    return artifact


def validate_workbook(parent, project_id, content, *, hmac_key):
    entry_id = authorization.PROJECT_WORKBOOK_VALIDATE
    digest = hashlib.sha256(content).hexdigest()
    params = {'project_id': project_id, 'file_sha256': digest}
    validation = None
    issues = ()
    error_content = None
    with foundation.read_context_snapshot(sessionmaker(bind=parent.db.get_bind()), parent.identity,
            entry_id=entry_id, params=params) as (db, proof, _, accesses):
        authorization.require_complete(accesses)
        reader = _read_adapter(parent, db, proof)
        reader._authorize(entry_id, params)
        workspace = reader.load_workspace(project_id)
        version = workspace['data_version']
        revision = reader._state(project_id).revision
        try:
            validation = validate_project_workbook(content, workspace=workspace, hmac_key=hmac_key)
            if validation.project_id != project_id:
                raise ProjectWorkbookV2Error('工作簿项目与 URL 项目不一致', status_code=409)
        except ProjectWorkbookV2Error as exc:
            if exc.status_code == 409:
                raise
            issues = tuple(exc.issues)
            error_content = build_error_workbook(issues, hmac_key=hmac_key,
                project_id=project_id, source_sha256=digest)
        parent._authorized_accesses = accesses
    accesses = _authorize_write(parent, entry_id, params, version)
    validation_id = validation.validation_id if validation is not None else str(uuid4())
    from sqlalchemy import null
    row = MaintenanceProjectWorkbookValidation(validation_id=validation_id, project_id=project_id,
        export_id=validation.export_id if validation is not None else f'error-{validation_id[:36]}',
        expected_revision=revision, file_sha256=digest,
        plan_json=adapter_module._serialize_validation(validation) if validation is not None else null(),
        status='valid' if validation is not None else 'error',
        issues_json=adapter_module._issues_payload(issues), error_workbook=error_content,
        created_by=parent.operator, expires_at=adapter_module._now() + adapter_module.VALIDATION_TTL)
    parent.db.add(row)
    parent.db.flush()
    actor = service.lock_actor(parent.db, parent.identity)
    binding = artifacts.register_artifact_binding(parent.db, parent.identity,
        target=artifacts.ArtifactTarget('project_workbook_validation', validation_id, 1),
        input_digest=authorization.project_workbook_plan_digest(parent.db,row),
        provenance=authorization.captured_provenance(accesses), locked_user=actor)
    if error_content is not None:
        artifacts.publish_artifact(parent.db, parent.identity, binding=binding,
            purpose='error_download', content=error_content, expected_generation=1, locked_user=actor)
    return validation, issues, validation_id


def read_error_workbook(parent, validation_id):
    def read_bytes(db, target, purpose):
        row = db.get(MaintenanceProjectWorkbookValidation, target.id)
        if row.status != 'error' or row.error_workbook is None or row.expires_at <= adapter_module._now():
            raise artifacts.unavailable()
        return bytes(row.error_workbook)
    return artifacts.read_artifact_bytes(sessionmaker(bind=parent.db.get_bind()), parent.identity,
        target=artifacts.ArtifactTarget('project_workbook_validation', validation_id, 1),
        purpose='error_download', read_bytes=read_bytes,
        authorization_precondition=parent.authorization_precondition)
