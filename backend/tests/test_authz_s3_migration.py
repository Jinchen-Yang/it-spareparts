"""Frozen legacy seeds and actual adjacent migration/rollback chains."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from alembic import command
import pytest
from sqlalchemy import text
from tests.conftest import _alembic_cfg

spec = importlib.util.spec_from_file_location('frozen_s3', Path(__file__).parents[1] / 'alembic/versions/d6e9f2a4b7c1_maintenance_authorization.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
S2 = 'c9d4e8f2a6b1'
S1 = 'a7c2e9f4b6d1'
EMPTY = {'schema_version': 1, 'template_snapshot': None, 'resource_overrides': {}}

def migrate(db, target, *, down=False):
    db.commit()
    (command.downgrade if down else command.upgrade)(_alembic_cfg(), target)
    db.expire_all()

def insert(db, name, *, eligible=True, role='sales', active=True, profile=None):
    return db.execute(text('''INSERT INTO sys_user(username,password_hash,role,is_active,authorization_legacy_seed_eligible,authorization_profile)
        VALUES (:name,'synthetic',:role,:active,:eligible,CAST(:profile AS jsonb)) RETURNING id'''),
        {'name':name,'role':role,'active':active,'eligible':eligible,'profile':json.dumps(profile or EMPTY)}).scalar_one()

def user(db, ident):
    return dict(db.execute(text('SELECT * FROM sys_user WHERE id=:id'), {'id':ident}).mappings().one())

@pytest.mark.parametrize('role', ['admin','boss','sales','purchaser','readonly','maintenance_manager'])
def test_frozen_effective_graph_and_explicit_whitelist(role):
    from app import permissions
    for snapshot in (None, {}, dict.fromkeys(permissions.ALL_KEYS, True), dict.fromkeys(permissions.ALL_KEYS, False)):
        for key in permissions.ALL_KEYS:
            raw = {'role':role,'template_perms':snapshot,'perm_overrides':{key:False},
                   'permissions':{'data_profit':True, 'data_purchase_cost':False}}
            assert m.effective(raw) == permissions.runtime_safe(permissions.effective_for_user(SimpleNamespace(**raw)))
            values = m.predicates(raw)
            expected = permissions.effective_from_snapshot(snapshot, raw['perm_overrides']) if snapshot is not None else permissions.sanitize(raw['permissions'])
            assert values['explicit_follow_up'] == bool(expected.get('action_maintenance_collection_follow_up'))
    admin = {'role':'admin','template_perms':{'page_maintenance_beta':True},'permissions':{},'perm_overrides':{},'authorization_legacy_seed_eligible':True}
    result = m.seed(admin, {})
    assert 'read' in result['maintenance_collection_followup']['actions']
    assert not {'record', 'evidence_upload'}.intersection(result['maintenance_collection_followup']['actions'])
    deny = {'actions': [], 'scope': 'assigned_or_sales_projects', 'field_permissions': []}
    assert 'maintenance_project' not in m.seed(admin, {'maintenance_project': deny})


def test_real_upgrade_preserves_canonical_empty_and_inactive_legacy(db):
    migrate(db, S2, down=True)
    uid = insert(db,'s3-inactive',active=False,role='admin')
    canonical = insert(db,'s3-explicit-none',eligible=False,role='admin')
    migrate(db, 'head')
    actual = user(db,uid)
    assert len(actual['authorization_profile']['resource_overrides']) == 73 and not actual['is_active']
    assert user(db,canonical)['authorization_profile'] == EMPTY
    assert not db.execute(text('SELECT EXISTS(SELECT 1 FROM sys_authorization_grant WHERE user_id=:id)'),{'id':canonical}).scalar()
    first = deepcopy(actual['authorization_profile']);epoch=actual['token_version']
    migrate(db,S2,down=True)
    assert user(db,uid)['authorization_profile']==EMPTY and user(db,uid)['token_version']>epoch
    migrate(db,'head')
    assert user(db,uid)['authorization_profile']==first and user(db,uid)['token_version']>epoch+1


def test_proven_adjacent_s2_s3_downgrade_forwards_only_counters(db):
    migrate(db,S1,down=True)
    uid=insert(db,'s3-adjacent',role='admin')
    migrate(db,'head')
    full=user(db,uid); assert len(full['authorization_profile']['resource_overrides'])==95
    assert 'sales_analysis' not in full['authorization_profile']['resource_overrides']
    migrate(db,S1,down=True)
    restored=user(db,uid)
    assert restored['authorization_profile']==EMPTY and restored['token_version']>full['token_version']
    migrate(db,'head')


def test_manual_change_between_stages_cannot_be_laundered_into_s2_receipt(db):
    migrate(db,S1,down=True)
    uid=insert(db,'s3-gap',role='admin');migrate(db,S2)
    db.execute(text('UPDATE sys_user SET token_version=token_version+2 WHERE id=:id'),{'id':uid})
    migrate(db,'head')
    with pytest.raises(RuntimeError,match='Authorization changed'):
        migrate(db,S1,down=True)
    db.rollback();migrate(db,'head')


@pytest.mark.parametrize('marker', ['sidecar','owner','actor','external'])
def test_new_protocol_use_prevents_identity_erasure(db,marker):
    uid=insert(db,'s3-protected',eligible=False)
    if marker=='sidecar':
        db.execute(text("INSERT INTO sys_authorization_artifact(domain_kind,domain_id,owner_user_id,generation,enqueued_epoch,input_digest) VALUES ('pending','1',:id,1,0,:sha)"),{'id':uid,'sha':'a'*64})
    elif marker=='owner':
        batch=db.execute(text("INSERT INTO sys_import_batch(filename,file_type,file_hash,status,uploaded_by) VALUES ('synthetic','wbdd','a','success','s3-protected') RETURNING id")).scalar_one()
        db.execute(text("INSERT INTO maintenance_wbdd_import_receipt(batch_id,idempotency_key,uploaded_by,owner_user_id,file_hash,report_json) VALUES (:batch,'s3','s3-protected',:id,'hash','{}')"),{'batch':batch,'id':uid})
    elif marker=='actor':
        db.execute(text("INSERT INTO maintenance_migration_run(run_id,idempotency_key,request_fingerprint,rule_version,source_snapshot_hash,business_as_of,status,preview_json,created_by,created_by_user_id) VALUES ('s3-run','s3-key',:sha,'1',:sha,'2026-10-23','previewed','{}','s3-protected',:id)"),{'sha':'a'*64,'id':uid})
    else:
        from app.authz.runtime import mark_artifact_protocol_used
        db.commit();mark_artifact_protocol_used(db.get_bind(),protocol='authz_s3')
    with pytest.raises(RuntimeError,match='Cannot downgrade'):
        migrate(db,S2,down=True)
    db.rollback()
    assert db.execute(text("SELECT to_regclass('sys_authorization_artifact')")).scalar()


@pytest.mark.parametrize('kind', ['file', 'workbook_operation', 'acceptance_operation', 'replenishment_version'])
def test_stable_command_and_file_owner_columns_prevent_downgrade(db, kind):
    from uuid import uuid4
    from app.models.maintenance_manager import BusinessFile, MaintenanceAcceptanceDeliverable, MaintenanceAcceptanceOperation
    from app.models.maintenance_project_operations import MaintenanceProjectWorkbookOperation
    from tests.boss_board_helpers import make_project
    uid = insert(db, 'stable-owner-guard', eligible=False)
    project = make_project(db, 'stable-owner-project')
    if kind == 'file':
        db.add(BusinessFile(file_id=str(uuid4()), storage_provider='local', object_key='maintenance_acceptance/synthetic',
            original_filename='synthetic.bin', mime_type='application/octet-stream', size_bytes=1,
            sha256='a' * 64, security_state='active', uploaded_by='stable-owner-guard', owner_user_id=uid))
    elif kind == 'workbook_operation':
        db.add(MaintenanceProjectWorkbookOperation(project_id=project.project_id, file_sha256='a' * 64,
            operation_key='stable-workbook-operation', payload_hash='b' * 64, operation_type='file_apply',
            operated_by='stable-owner-guard', owner_user_id=uid))
    elif kind == 'replenishment_version':
        from app.models.replenishment import ReplenishmentApplication, ReplenishmentApplicationVersion
        app = ReplenishmentApplication(application_id=str(uuid4()), application_no='OWNER-GUARD',
            owner_username='stable-owner-guard', project_id=project.project_id,
            client_request_id='version-owner-guard-request', request_digest='a' * 64,
            project_code_snapshot=project.project_code, project_name_snapshot=project.display_name)
        db.add(app)
        db.flush()
        db.add(ReplenishmentApplicationVersion(version_id=str(uuid4()), application_id=app.application_id,
            version_no=1, created_by='stable-owner-guard', owner_user_id=uid))
    else:
        deliverable = MaintenanceAcceptanceDeliverable(deliverable_id=str(uuid4()), project_id=project.project_id,
            deliverable_type='acceptance_report', configuration_state='configured', submission_status='not_submitted',
            approval_status='not_reviewed')
        db.add(deliverable)
        db.flush()
        db.add(MaintenanceAcceptanceOperation(operation_id=str(uuid4()), operation_key='stable-acceptance-operation',
            payload_hash='a' * 64, operation_type='attachment_upload', deliverable_id=deliverable.deliverable_id,
            project_id=project.project_id, result_json={'file_id': 'synthetic'},
            operated_by='stable-owner-guard', owner_user_id=uid))
    with pytest.raises(RuntimeError, match='stable command or file owner'):
        migrate(db, S2, down=True)
    db.rollback()
    assert db.execute(text("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='business_file' AND column_name='owner_user_id')")).scalar()


@pytest.mark.parametrize('kind',['import_batch','roundtrip_operation'])
def test_final_batch_and_operation_stable_owner_cannot_be_downgraded(db,kind):
    from uuid import uuid4
    from app.models.system import SysImportBatch
    from app.models.maintenance import MaintenanceRoundtripOperation
    uid=insert(db,'final-batch-owner',eligible=False)
    batch=SysImportBatch(file_type='maint_roundtrip',filename='synthetic.xlsx',file_hash='a'*64,
        uploaded_by='synthetic',status='success',owner_user_id=uid if kind=='import_batch' else None)
    db.add(batch);db.flush()
    if kind=='roundtrip_operation':
        db.add(MaintenanceRoundtripOperation(owner_user_id=uid,export_id=str(uuid4()),sheet_code='lines',
            client_row_id=str(uuid4()),operation='UPDATE',payload_hash='b'*64,result_json={},
            import_batch_id=batch.id,applied_by='synthetic'))
    with pytest.raises(RuntimeError,match='stable command or file owner'):
        migrate(db,S2,down=True)
    db.rollback()


def test_current_s3_orm_matches_actual_migration(db):
    db.commit()
    command.check(_alembic_cfg())
