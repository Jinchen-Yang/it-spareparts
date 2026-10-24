"""Real signed workbook parsing, worker identity, atomic writes and receipt replay."""
import json
from pathlib import Path
from types import MappingProxyType

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session

from app.api import maintenance as api
from app.authz.principal import VerifiedIdentity
from app.models.authorization import SysAuthorizationArtifact, SysAuthorizationGrant
from app.models.maintenance import FMaintenanceLine, MaintenanceRoundtripOperation
from app.models.system import SysImportBatch, SysRawFile, SysUser
from app.services import maintenance_roundtrip as business
from app.services import maintenance_roundtrip_authorization as protected
from tests.test_maintenance_roundtrip import _seed_contract, _export_to_path, _edit_data_row
from tests.test_authz_s3_source_cost import source_cost, give  # noqa: F401
from tests.test_authz_s3_analysis import analysis_client  # noqa: F401
from tests.test_authz_s3_workspace import workspace  # noqa: F401
from tests.test_authz_s3_projects import canonical  # noqa: F401
from tests.test_authz_s3_project_foundation import projects  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def upload(db, source_cost, tmp_path, monkeypatch):
    client, headers, actor = source_cost
    grant = give(db, actor, ['apply'], fields=['data_purchase_cost','data_profit','data_customer'],
        resource=protected.RESOURCE, scope='all_contracts')
    _, line_id = _seed_contract(db, suffix='S3-ROUNDTRIP', contract='XSDD-RT-S3')
    path = _export_to_path(db, tmp_path / 'actual.xlsx', contract='XSDD-RT-S3')
    _edit_data_row(path, '03_订单明细', {'操作':'UPDATE', '产品描述':'S3 authorized update', '变更原因':'核对'})
    identity = VerifiedIdentity(actor.username, actor.token_version, False, 'sys_user', MappingProxyType({}), actor.id)
    bind = db.get_bind()
    monkeypatch.setattr(api, 'SessionLocal', lambda: Session(bind))
    return client, headers, actor, grant, path, line_id, identity


def post(upload, *, view=None, precondition=None, content=None):
    client, headers, _, _, path, _, _ = upload
    data = {} if precondition is None else {'authorization_precondition': json.dumps(precondition)}
    return client.post('/api/maintenance/roundtrip-import', headers=headers,
        params={'view':view} if view else {}, data=data,
        files={'file':(path.name, content or path.read_bytes(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})


def test_real_worker_apply_only_preflight_write_and_stable_owned_replay(db, upload):
    client, headers, actor, _, path, line_id, _ = upload
    view = post(upload, view='authorization')
    assert view.status_code == 200, view.text
    assert set(view.json()) == {'locator','authorization_context'}
    assert db.scalar(select(func.count()).select_from(SysImportBatch).where(SysImportBatch.file_type == business.ROUNDTRIP_FILE_TYPE)) == 0
    assert client.get('/api/maintenance/roundtrip-template', headers=headers).status_code == 403
    ctx = view.json()['authorization_context']
    precondition = {k:ctx[k] for k in ('catalog_version','authorization_epoch','context')}
    response = post(upload, precondition=precondition)
    assert response.status_code == 200, response.text
    assert response.json()['changed_rows'] == 1
    db.expire_all()
    batch = db.get(SysImportBatch, response.json()['batch_id'])
    assert batch.owner_user_id == actor.id
    assert db.get(FMaintenanceLine, line_id).description == 'S3 authorized update'
    operation = db.scalar(select(MaintenanceRoundtripOperation).where(MaintenanceRoundtripOperation.import_batch_id == batch.id))
    assert operation.owner_user_id == actor.id
    assert db.get(SysAuthorizationArtifact, (protected.KIND, str(batch.id))) is not None
    replay = post(upload)
    assert replay.status_code == 200 and replay.json()['no_op'], replay.text
    # A different ZIP of the same logical operation uses the actual immutable receipt.
    from zipfile import ZipFile
    with ZipFile(path, 'a') as archive:
        archive.comment = b'repacked'
    replay = post(upload)
    assert replay.status_code == 200 and replay.json()['logical_replay'], replay.text


def test_replay_reauthorizes_current_apply_and_actual_bytes(db, upload):
    _, _, actor, grant, _, _, _ = upload
    response = post(upload); assert response.status_code == 200, response.text
    batch_id = response.json()['batch_id']
    grant.actions = []; grant.version += 1; db.commit()
    assert post(upload).status_code == 403
    grant.actions = ['apply']; grant.version += 1; db.commit()
    raw = db.scalar(select(SysRawFile).where(SysRawFile.batch_id == batch_id))
    Path(raw.storage_path).write_bytes(b'tampered actual upload')
    assert post(upload).status_code == 403


def test_original_legacy_receipt_is_not_adopted(db, upload):
    _, _, _, _, path, _, _ = upload
    business.import_roundtrip_workbook(db, str(path), filename=path.name, operated_by='legacy-user')
    response = post(upload)
    assert response.status_code == 403, response.text
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact).where(
        SysAuthorizationArtifact.domain_kind == protected.KIND)) == 0


def test_staged_workbook_changes_are_rolled_back_on_final_epoch_revocation(db, upload, monkeypatch):
    _, _, actor, _, _, line_id, _ = upload
    original = business._recompute_in_transaction
    actor_id = actor.id
    def revoke(transaction):
        result = original(transaction)
        with Session(db.get_bind()) as administrator:
            administrator.execute(update(SysUser).where(SysUser.id == actor_id).values(token_version=SysUser.token_version + 1))
            administrator.commit()
        return result
    monkeypatch.setattr(business, '_recompute_in_transaction', revoke)
    response = post(upload)
    assert response.status_code == 401, response.text
    db.expire_all()
    assert db.get(FMaintenanceLine, line_id).description == '原始描述'
    assert db.scalar(select(func.count()).select_from(MaintenanceRoundtripOperation)) == 0
    assert db.scalar(select(func.count()).select_from(SysImportBatch).where(SysImportBatch.file_type == business.ROUNDTRIP_FILE_TYPE)) == 0


def test_actual_receipt_publication_failure_cleans_only_owned_file(db, upload, monkeypatch):
    from app.config import get_settings
    _, _, _, _, path, line_id, identity = upload
    directory = Path(get_settings().raw_file_dir)
    directory.mkdir(parents=True, exist_ok=True)
    prior = set(directory.glob('**/*'))
    original = protected.artifacts.register_artifact_binding
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('real receipt publish failed')
    monkeypatch.setattr(protected.artifacts, 'register_artifact_binding', fail)
    with pytest.raises(RuntimeError, match='real receipt publish failed'):
        protected.execute(db, identity, path=str(path), filename=path.name)
    assert set(directory.glob('**/*')) == prior
    db.expire_all()
    assert db.get(FMaintenanceLine, line_id).description == '原始描述'
    assert db.scalar(select(func.count()).select_from(MaintenanceRoundtripOperation)) == 0


def test_changed_workbook_bytes_cannot_reuse_authorization_view(db, upload):
    view = post(upload, view='authorization'); assert view.status_code == 200, view.text
    ctx = view.json()['authorization_context']
    precondition = {k:ctx[k] for k in ('catalog_version','authorization_epoch','context')}
    path = upload[4]
    _edit_data_row(path, '03_订单明细', {'产品描述':'different input'})
    response = post(upload, precondition=precondition)
    assert response.status_code == 409, response.text
    assert db.scalar(select(func.count()).select_from(MaintenanceRoundtripOperation)) == 0


def test_revoke_while_waiting_actual_global_lock_prevents_all_import_writes(db, upload):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from sqlalchemy import text
    from app import config
    _, _, actor, _, path, line_id, identity = upload
    uid = actor.id
    db.rollback()
    def worker():
        with Session(db.get_bind()) as command:
            try:
                protected.execute(command, identity, path=str(path), filename=path.name)
                return 200
            except HTTPException as exc:
                return exc.status_code
    with Session(db.get_bind()) as blocker:
        blocker.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key':config.DATA_CHANGE_ADVISORY_LOCK_KEY})
        pid = blocker.scalar(text('SELECT pg_backend_pid()'))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            blocked = False
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline and not future.done():
                    with db.get_bind().connect() as observer:
                        blocked = observer.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)))'), {'pid':pid})
                    if blocked:
                        break
                    time.sleep(.02)
                assert blocked, 'Did not reach actual global lock wait'
                with Session(db.get_bind()) as administrator:
                    administrator.execute(update(SysUser).where(SysUser.id == uid).values(token_version=SysUser.token_version + 1))
                    administrator.commit()
            finally:
                blocker.rollback()
            assert future.result(timeout=15) == 401
    db.expire_all()
    assert db.get(FMaintenanceLine, line_id).description == '原始描述'
    assert db.scalar(select(func.count()).select_from(MaintenanceRoundtripOperation)) == 0
    assert db.scalar(select(func.count()).select_from(SysImportBatch).where(SysImportBatch.file_type == business.ROUNDTRIP_FILE_TYPE)) == 0



def test_apply_needs_complete_own_fields_even_when_other_module_has_all_fields(db, upload):
    _, _, actor, grant, _, line_id, _ = upload
    grant.field_permissions = ['data_purchase_cost', 'data_profit']; db.commit()
    give(db, actor, ['export_workbook'], fields=['data_purchase_cost','data_profit','data_customer'], scope='all_sources')
    response = post(upload)
    assert response.status_code == 403, response.text
    assert db.get(FMaintenanceLine, line_id).description == '原始描述'
    assert db.scalar(select(func.count()).select_from(MaintenanceRoundtripOperation)) == 0


def test_denied_roundtrip_replay_never_loads_prior_report_or_result(db,upload):
    from sqlalchemy import event
    response=post(upload);assert response.status_code==200,response.text
    grant=upload[3];grant.actions=[];grant.version+=1;db.commit();columns=[]
    def capture(_conn,cursor,statement,_params,_ctx,_many):
        if cursor.description:columns.extend(str(column[0]) for column in cursor.description)
    event.listen(db.get_bind(),'after_cursor_execute',capture)
    try:
        denied=post(upload);assert denied.status_code==403,denied.text
    finally:event.remove(db.get_bind(),'after_cursor_execute',capture)
    assert not any(name.endswith(('report_json','result_json')) for name in columns),columns
