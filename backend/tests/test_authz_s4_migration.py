"""Frozen S4 seeds and persistent import protocol, actual PostgreSQL migrations."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from alembic import command
import pytest
from sqlalchemy import text
from tests.conftest import _alembic_cfg

spec=importlib.util.spec_from_file_location('frozen_s4',Path(__file__).parents[1]/'alembic/versions/e4a7c9f2b6d8_platform_import_authorization.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
S3='d6e9f2a4b7c1'
S1='a7c2e9f4b6d1'
EMPTY={'schema_version':1,'template_snapshot':None,'resource_overrides':{}}

def migrate(db,target,down=False):
    db.commit();(command.downgrade if down else command.upgrade)(_alembic_cfg(),target);db.expire_all()

def insert(db,name,*,eligible=True,active=True,perms=None):
    return db.execute(text("INSERT INTO sys_user(username,password_hash,role,is_active,authorization_legacy_seed_eligible,permissions) VALUES (:name,'synthetic','readonly',:active,:eligible,CAST(:perms AS jsonb)) RETURNING id"),{'name':name,'active':active,'eligible':eligible,'perms':json.dumps(perms or {})}).scalar_one()

def stored(db,uid):
    return dict(db.execute(text('SELECT authorization_profile,token_version FROM sys_user WHERE id=:id'),{'id':uid}).mappings().one())

@pytest.mark.parametrize('role',['admin','boss','sales','purchaser','readonly','maintenance_manager'])
def test_frozen_effective_matches_actual_legacy_graph(role):
    from app import permissions
    for snapshot in (None,{},dict.fromkeys(permissions.ALL_KEYS,True)):
        for key in permissions.ALL_KEYS:
            row={'role':role,'template_perms':snapshot,'permissions':{key:True},'perm_overrides':{key:False}}
            assert m.effective(row)==permissions.runtime_safe(permissions.effective_for_user(SimpleNamespace(**row)))

def test_import_seed_does_not_promote_old_unmasked_reports_or_empty_canonical_keys():
    row={'role':'readonly','permissions':{'page_import':True,'data_customer':False,'data_supplier':False,'data_purchase_cost':False,'data_profit':False},'template_perms':None,'perm_overrides':{},'authorization_legacy_seed_eligible':True}
    result=m.seed(row,{})
    for name in ('purchase','sales','inventory','maintenance','expense'):
        grant=result['import_'+name]
        assert {'precheck','submit_skip','submit_upsert','read_status'} <= set(grant['actions'])
        assert not {'read_report','export_errors','show_report','show_errors'} & set(grant['actions'])
    deny={'actions':[],'scope':'all','field_permissions':[]}
    assert 'import_sales' not in m.seed(row,{'import_sales':deny})
    row['authorization_legacy_seed_eligible']=False
    assert m.seed(row,{})=={}

def test_eligible_inactive_seed_and_false_none_roundtrip(db):
    migrate(db,S3,True)
    uid=insert(db,'s4-inactive',active=False,perms={'page_import':True,'page_chat':True,'page_accounts':True})
    none=insert(db,'s4-none',eligible=False,perms={'page_import':True})
    migrate(db,'head');first=stored(db,uid)
    assert len(first['authorization_profile']['resource_overrides'])==11
    assert stored(db,none)['authorization_profile']==EMPTY
    assert db.execute(text('SELECT is_active FROM sys_user WHERE id=:id'),{'id':uid}).scalar() is False
    migrate(db,S3,True)
    assert stored(db,uid)['authorization_profile']==EMPTY
    assert stored(db,uid)['token_version']>first['token_version']
    migrate(db,'head')
    assert stored(db,uid)['authorization_profile']==first['authorization_profile']

def test_latest_chain_roundtrip_preserves_all_stage_receipts_and_monotonic_versions(db):
    """S1 -> current head -> S1 remains separate from frozen S3 count assertions."""
    from app.authz.catalog import RESOURCE_DEFINITIONS

    def versions(uid):
        return dict(db.execute(text('SELECT resource,version FROM sys_authorization_grant WHERE user_id=:id'),
            {'id':uid}).all())

    migrate(db,S1,True)
    try:
        uid=insert(db,'s4-full-chain-inactive',active=False,
            perms={'page_import':True,'page_chat':True,'page_accounts':True})
        none=insert(db,'s4-full-chain-none',eligible=False,perms={'page_import':True})
        migrate(db,'head')
        first=deepcopy(stored(db,uid));first_versions=versions(uid)
        # Sales has no old explicit Grant; the other 22 + 73 + 11 are recorded,
        # including explicit denials. No stage may infer sales access from role.
        expected=set(RESOURCE_DEFINITIONS)-{'sales_analysis'}
        assert len(expected)==106
        assert set(first['authorization_profile']['resource_overrides'])==expected
        assert set(first_versions)==expected
        assert set(db.scalars(text("SELECT migration_key FROM sys_authorization_migration_receipt WHERE entity_kind='user' AND entity_id=:id"),
            {'id':str(uid)}))=={'authz_s2','authz_s3','authz_s4'}
        assert stored(db,none)['authorization_profile']==EMPTY and versions(none)=={}
        assert db.scalar(text('SELECT is_active FROM sys_user WHERE id=:id'),{'id':uid}) is False

        migrate(db,S1,True)
        restored=stored(db,uid)
        assert restored['authorization_profile']==EMPTY and versions(uid)=={}
        assert restored['token_version']>max(first['token_version'],*first_versions.values())
        assert stored(db,none)['authorization_profile']==EMPTY and versions(none)=={}

        migrate(db,'head')
        second=stored(db,uid);second_versions=versions(uid)
        assert second['authorization_profile']==first['authorization_profile']
        assert second['token_version']>restored['token_version']
        assert set(second_versions)==expected
        assert all(second_versions[key]>first_versions[key] for key in expected)
        assert stored(db,none)['authorization_profile']==EMPTY and versions(none)=={}
        migrate(db,'head')
        assert stored(db,uid)==second and versions(uid)==second_versions
    finally:
        db.rollback();migrate(db,'head')

@pytest.mark.parametrize('marker',['job','batch','protocol','grant'])
def test_new_protocol_or_grant_prevents_rollback(db,marker):
    uid=insert(db,'s4-protected',eligible=False)
    if marker=='job':
        db.execute(text("INSERT INTO sys_import_job(mode,total_files,done_files,error_files,status,owner_user_id) VALUES ('skip',1,0,0,'processing',:id)"),{'id':uid})
    elif marker=='batch':
        db.execute(text("INSERT INTO sys_import_batch(filename,file_type,file_hash,status,authorization_protocol,source_domain) VALUES ('synthetic','sales','a','success','authz_s4','sales')"))
    elif marker=='protocol':
        db.commit();from app.authz.runtime import mark_artifact_protocol_used
        mark_artifact_protocol_used(db.get_bind(),protocol='authz_s4')
    else:
        db.execute(text("INSERT INTO sys_authorization_grant(user_id,resource,actions,scope,field_permissions,version) VALUES (:id,'agent_file','[]','self','[]',1)"),{'id':uid})
    with pytest.raises(RuntimeError,match='Cannot downgrade|Unrecorded'):
        migrate(db,S3,True)
    db.rollback()
    assert db.execute(text("SELECT to_regclass('sys_import_job_item')")).scalar()

def test_schema_and_boundary_closed_vocabulary(db):
    from app.authz.catalog import validate_catalog,validate_grant,RESOURCE_DEFINITIONS
    from app.authz.boundaries import boundary_descriptors
    validate_catalog()
    assert len(RESOURCE_DEFINITIONS)==107
    for boundary in boundary_descriptors():
        with pytest.raises(ValueError):validate_grant(boundary['id'],[], 'all',[])
    db.commit();command.check(_alembic_cfg())
