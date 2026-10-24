"""Cross-author adversarial checks of S3 shared services with real PG and PDP."""
from types import MappingProxyType
from datetime import date

import anyio
import pytest
from sqlalchemy import event

from app.authz.principal import VerifiedIdentity
from app.models.system import SysUser
from app.services import maintenance_row_workbench_access as rows_service
from tests.test_authz_s3_row_workbench import rows, setup, export_data, cerbos_url, grant, WINDOW  # noqa: F401


@pytest.mark.parametrize('file_format', ['csv', 'xlsx'])
def test_row_export_materializes_before_response_and_uses_no_sql_while_streaming(db, rows, file_format):
    uid, _, _, _ = rows
    grant(db, uid, 'maintenance_global_rows', ['export'], fields=['data_purchase_cost'])
    user = db.get(SysUser, uid)
    identity = VerifiedIdentity(user.username, user.token_version, False, 'sys_user', MappingProxyType({}), uid)
    db.rollback()
    checked_in = []
    statements = []
    engine = db.get_bind()
    def checkin(*args):
        checked_in.append(True)
    def execute(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, 'checkin', checkin)
    event.listen(engine, 'before_cursor_execute', execute)
    try:
        response = rows_service.read_rows(db, identity,
            entry_id='GET /api/maintenance/spare-part-lines/export',
            params={**WINDOW, 'from': date(2026, 7, 1), 'to': date(2026, 7, 31), 'format': file_format}, legacy_context=None)
        assert checked_in, 'RR connection must be released before returning the streaming response'
        assert statements, 'This must exercise actual PG queries before the stream'
        statements.clear()
        messages = []
        async def consume():
            async def send(message):
                messages.append(message)
                await anyio.sleep(0)
            async def receive():
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        anyio.run(consume)
        assert statements == []
        content = b''.join(message.get('body', b'') for message in messages)
        assert len(content) > 200
        assert content.startswith(b'\xef\xbb\xbf') if file_format == 'csv' else content.startswith(b'PK')
        assert response._resource.closed
    finally:
        event.remove(engine, 'checkin', checkin)
        event.remove(engine, 'before_cursor_execute', execute)


from tests.test_authz_s3_acceptance import acceptance  # noqa: E402,F401


@pytest.mark.parametrize('failure_boundary', ['manifest', 'commit'])
def test_acceptance_publication_failure_compensates_bytes_and_entire_domain_transaction(
        db, acceptance, monkeypatch, failure_boundary):
    from pathlib import Path
    from sqlalchemy import select, func
    from app.config import get_settings
    from app.models.authorization import SysAuthorizationArtifact, SysAuthorizationMigrationReceipt
    from app.models.maintenance_manager import BusinessFile, BusinessFileLink, MaintenanceAcceptanceOperation
    from app.services import authorization_artifacts as artifacts
    from tests.test_authz_s3_acceptance import upload
    _, pid, client, _ = acceptance
    published = []
    original = artifacts.publish_artifact
    def publish(*args, **kwargs):
        result = original(*args, **kwargs)  # Real PG/PDP/manifest validation before injected I/O/commit failure.
        files = [path for path in Path(get_settings().raw_file_dir).rglob('*') if path.is_file()]
        assert len(files) == 1 and files[0].read_bytes() == b'canonical acceptance binary'
        published.extend(files)
        if failure_boundary == 'manifest':
            raise RuntimeError('independent publication failure')
        return result
    monkeypatch.setattr(artifacts, 'publish_artifact', publish)
    def before_commit(session):
        if published:
            raise RuntimeError('independent transaction commit failure')
    if failure_boundary == 'commit':
        event.listen(db, 'before_commit', before_commit)
    try:
        with pytest.raises(RuntimeError, match='independent'):
            upload(client, pid)
    finally:
        if failure_boundary == 'commit':
            event.remove(db, 'before_commit', before_commit)
    assert published and all(not path.exists() for path in published)
    for model in (BusinessFile, BusinessFileLink, MaintenanceAcceptanceOperation, SysAuthorizationArtifact):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    # Publication protocol is deliberately durable across failed business work.
    assert db.get(SysAuthorizationMigrationReceipt, ('authz_s3', 'artifact_protocol', 'global')) is not None

from tests.test_authz_s3_replenishment import replenishment, grant as replenishment_grant, submit  # noqa: F401,E402


def test_replenishment_binding_publication_failure_rolls_back_actual_version_and_audit(db, replenishment, monkeypatch):
    from sqlalchemy import func, select
    from app.models.authorization import SysAuthorizationArtifact
    from app.models.replenishment import ReplenishmentApplication, ReplenishmentApplicationVersion, ReplenishmentApplicationLine, ReplenishmentAuditEvent
    from app.services import replenishment_authorization as module
    uid, pid, part, client = replenishment
    replenishment_grant(db, uid, 'replenishment_project_commands', ['submit'])
    real_register = module.artifacts.register_artifact_binding
    published = []
    def register(*args, **kwargs):
        result = real_register(*args, **kwargs)
        published.append(result)
        assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationVersion)) == 1
        assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact)) == 1
        raise RuntimeError('independent replenishment publication failure')
    monkeypatch.setattr(module.artifacts, 'register_artifact_binding', register)
    with pytest.raises(RuntimeError, match='independent replenishment'):
        submit(client, pid, part)
    assert published
    for model in (ReplenishmentApplication, ReplenishmentApplicationVersion,
                  ReplenishmentApplicationLine, ReplenishmentAuditEvent, SysAuthorizationArtifact):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_replenishment_whole_version_binding_rejects_changed_stored_line_for_read_and_export(db, replenishment):
    from sqlalchemy import select
    from app.models.replenishment import ReplenishmentApplicationLine
    uid, pid, part, client = replenishment
    replenishment_grant(db, uid, 'replenishment_project_commands', ['submit'])
    created = submit(client, pid, part)
    assert created.status_code == 201, created.text
    url = '/api/replenishment-beta/applications/' + created.json()['application_id']
    replenishment_grant(db, uid, 'replenishment_request', ['read', 'export_screening'], scope='own_applications')
    assert client.get(url).status_code == 200
    assert client.get(url + '/exports/system-screening.xlsx').status_code == 200
    from sqlalchemy.exc import DBAPIError
    from app.models.replenishment import ReplenishmentAuditEvent
    from uuid import uuid4
    line = db.scalar(select(ReplenishmentApplicationLine))
    version_id = line.version_id
    line.quantity += 1
    with pytest.raises(DBAPIError, match='submitted replenishment lines are immutable'):
        db.commit()
    db.rollback()
    # The DB protects line mutation independently. An appended actual audit row
    # is legal SQL, and must still invalidate the previously frozen whole record.
    db.add(ReplenishmentAuditEvent(event_id=str(uuid4()),
        application_id=created.json()['application_id'], version_id=version_id,
        action='manual_exported', reason='independent stored-content change',
        operated_by='independent-auditor'))
    db.commit()
    for path in (url, url + '/exports/system-screening.xlsx'):
        response = client.get(path)
        assert response.status_code == 403, response.text
        assert response.headers['cache-control'] == 'no-store'

from tests.test_authz_s3_warehouse import warehouse  # noqa: F401,E402


def test_warehouse_real_manifest_publication_failure_rolls_back_business_and_lineage(db, warehouse, monkeypatch):
    from sqlalchemy import func, select
    from app.models.authorization import SysAuthorizationArtifact
    from app.models.maintenance_warehouse import (MaintenanceWarehouseImportBatch,
        MaintenanceWarehouseDocument, MaintenanceWarehouseDocumentLink, MaintenanceWarehouseAuditEvent)
    from app.services import maintenance_warehouse_authorization as module
    from tests.test_authz_s3_warehouse import applied
    client, uid, content = warehouse
    models = (MaintenanceWarehouseImportBatch, MaintenanceWarehouseDocument,
              MaintenanceWarehouseDocumentLink, MaintenanceWarehouseAuditEvent, SysAuthorizationArtifact)
    counts = {model: db.scalar(select(func.count()).select_from(model)) for model in models}
    real_publish = module.artifacts.publish_artifact
    published = []
    def publish(*args, **kwargs):
        result = real_publish(*args, **kwargs)
        assert result['complete'] is True
        published.append(result)
        raise RuntimeError('independent warehouse publication failure')
    monkeypatch.setattr(module.artifacts, 'publish_artifact', publish)
    with pytest.raises(RuntimeError, match='independent warehouse'):
        applied(client, content)
    assert published
    for model in models:
        assert db.scalar(select(func.count()).select_from(model)) == counts[model]

from tests.test_authz_s3_ai_fallback import ai  # noqa: F401,E402


def test_ai_acceptance_binds_actual_acceptor_without_reusing_producer_permissions(db, ai):
    from app.auth import _make_token
    from app.models.authorization import SysAuthorizationArtifact
    from app.models.maintenance_ai_fallback import MaintenanceAiMappingProposal
    from app.models.maintenance_doc_import import MaintenanceDocImportBatch
    from tests.test_authz_s3_ai_fallback import grant as ai_grant, propose, accept
    from tests.test_maintenance_ai_fallback import _variant_return_workbook
    uid, identity, client=ai
    data=_variant_return_workbook()
    ai_grant(db,uid,'maintenance_ai_document',['propose'])
    created=propose(client,data);assert created.status_code==200,created.text
    pid=created.json()['proposal_id']
    producer=db.get(SysUser,uid);producer.is_active=False;producer.token_version+=1
    reader=SysUser(username='independent-ai-acceptor',role='readonly',password_hash='synthetic')
    db.add(reader);db.commit()
    reader_id=reader.id
    ai_grant(db,reader_id,'maintenance_ai_document',['accept'])
    token,_=_make_token('readonly',reader.username,None,token_version=reader.token_version,authn='sys_user')
    client.headers['Authorization']='Bearer '+token
    response=accept(client,pid,data);assert response.status_code==200,response.text
    bid=response.json()['batch_id']
    assert db.get(MaintenanceAiMappingProposal,pid,populate_existing=True).owner_user_id==uid
    assert db.get(MaintenanceDocImportBatch,bid).owner_user_id==reader_id
    assert db.get(SysAuthorizationArtifact,('ai_proposal_acceptance',pid)).owner_user_id==reader_id
    assert db.get(SysAuthorizationArtifact,('ai_proposal',pid)).owner_user_id==uid
    assert accept(client,pid,data).status_code==200
    ai_grant(db,reader_id,'maintenance_ai_document',[])
    assert accept(client,pid,data).status_code==403

from tests.test_authz_s3_returns import workflow  # noqa: E402,F401


def test_return_receipt_manifest_publish_failure_rolls_back_real_command(db,workflow,monkeypatch):
    from sqlalchemy import select,func
    from app.models.authorization import SysAuthorizationArtifact
    from app.models.maintenance_bad_return import MaintenanceBadReturn,MaintenanceBadReturnCommand
    from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
    from app.services import authorization_artifacts
    from tests.test_authz_s3_returns import bad_body,request
    client,_,oid=workflow
    models=(MaintenanceBadReturn,MaintenanceBadReturnCommand,MaintenanceProjectOperationAudit,SysAuthorizationArtifact)
    counts={Model:db.scalar(select(func.count()).select_from(Model)) for Model in models}
    original=authorization_artifacts.publish_artifact
    def fail(*args,**kwargs):
        manifest=original(*args,**kwargs)
        assert manifest['content_sha256']
        raise RuntimeError('independent actual return manifest publication failure')
    monkeypatch.setattr(authorization_artifacts,'publish_artifact',fail)
    with pytest.raises(RuntimeError,match='independent actual return'):
        request(client,'POST','/bad-returns',bad_body(oid))
    for Model in models:
        assert db.scalar(select(func.count()).select_from(Model))==counts[Model]


def test_denied_ai_accept_reads_only_proposal_identity_and_opaque_digest(db,ai):
    from tests.test_authz_s3_ai_fallback import grant as ai_grant,propose,accept
    from tests.test_maintenance_ai_fallback import _variant_return_workbook
    uid,_,client=ai
    content=_variant_return_workbook()
    ai_grant(db,uid,'maintenance_ai_document',['propose'])
    created=propose(client,content);assert created.status_code==200,created.text
    selected=[]
    def capture(_conn,cursor,statement,_params,_context,_many):
        if statement.lstrip().lower().startswith('select') and 'maintenance_ai_mapping_proposal' in statement.lower():
            selected.extend(column[0] for column in cursor.description or ())
    event.listen(db.get_bind(),'after_cursor_execute',capture)
    try:
        denied=accept(client,created.json()['proposal_id'],content)
    finally:event.remove(db.get_bind(),'after_cursor_execute',capture)
    assert denied.status_code==403,denied.text
    assert selected
    assert not any(name.endswith(('header_snapshot','sample_rows','proposal')) for name in selected),selected
