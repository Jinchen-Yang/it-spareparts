"""Canonical command transactions and migration on real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import permissions
from app.api import authorization, accounts, role_templates
from app.auth import _make_token, verify_password
from app.authz import catalog, management, runtime
from app.config import get_settings
from app.db import SessionLocal
from app.models.authorization import SysAuthorizationCutover, SysAuthorizationGrant, SysCustomerAssignment
from app.models.dimensions import DimCustomer
from app.models.system import SysAuditLog, SysRoleTemplate, SysUser


@pytest.fixture
def client(db, monkeypatch):
    settings=get_settings()
    monkeypatch.setattr(settings,'authorization_mode','unified_modules')
    monkeypatch.setattr(settings,'authz_enabled_resources',[])
    monkeypatch.setattr(settings,'authz_suspended_resources',[])
    monkeypatch.setattr(settings,'authz_sales_analysis_enabled',True)
    monkeypatch.setattr(settings,'authz_sales_analysis_suspended',True)
    runtime.activate_configured_resources(db.get_bind())
    app=FastAPI()
    for router in (authorization.router,accounts.router,role_templates.router):app.include_router(router,prefix='/api')
    with TestClient(app) as value:yield value


def _user(db,username,role='sales',**kwargs):
    row=SysUser(username=username,role=role,password_hash='synthetic-no-password',**kwargs)
    db.add(row);db.commit();return row


def _headers(user,**kwargs):
    token,_=_make_token(kwargs.pop('role',user.role),kwargs.pop('sub',user.username),None,
        authn=kwargs.pop('authn','sys_user'),token_version=kwargs.pop('tv',user.token_version),**kwargs)
    return {'Authorization':'Bearer '+token}


def _body(**kwargs):return {'schema_version':1,'catalog_version':catalog.CATALOG_VERSION,**kwargs}


def _config(**kwargs):return {'actions':['read_orders'],'scope':'assigned_customers','field_permissions':[],**kwargs}


def _result(response,status=200):
    assert response.status_code==status,response.text
    assert response.headers['cache-control']=='no-store'
    return response.json()


def _read(client,admin,user):return _result(client.get(f'/api/authz/users/{user.username}/modules',headers=_headers(admin)))


def _write(client,admin,user,*,epoch=0,version=0,config=None,operation='replace_resource',**kwargs):
    u={'resource_id':'sales_analysis','expected_version':version,'operation':operation}
    if operation!='clear_override':u['configuration']=_config() if config is None else config
    return client.put(f'/api/authz/users/{user.username}/modules',headers=_headers(admin),json=_body(expected_epoch=epoch,updates=[u],**kwargs))


def _tpl(client,admin,code='test_team',config=None):
    return _result(client.post('/api/role-templates',headers=_headers(admin),json=_body(
        metadata={'code':code,'name':'测试模板'},authorization={'kind':'explicit','modules':{'sales_analysis':_config() if config is None else config}})),201)['result']


def _bulk(client,admin,users,operation):
    preview=_result(client.post('/api/accounts/bulk',headers=_headers(admin),json=_body(dry_run=True,
        targets=[{'user_id':u.id,'username':u.username,'expected_epoch':u.token_version} for u in users],operation=operation)))
    return preview,client.post('/api/accounts/bulk',headers=_headers(admin),json={**preview['command'],'dry_run':False,'fingerprint':preview['fingerprint']})


def test_module_replacement_tombstone_audit_and_legacy_untouched(db,client):
    admin=_user(db,'operator','admin');user=_user(db,'target',template_perms={'page_sales_analysis':False},permissions={'data_profit':False})
    customers=[DimCustomer(name_raw='first'),DimCustomer(name_raw='second')];db.add_all(customers);db.commit()
    old_headers=_headers(user)
    first=_result(_write(client,admin,user,customer_assignments={'ids':[customers[1].id,customers[0].id]}))['result']
    assert first['resources']['sales_analysis']['personal_override']['present'] is True
    second=_result(_write(client,admin,user,epoch=1,version=1,operation='revoke_resource',config=_config(actions=[])))['result']
    assert second['resources']['sales_analysis']['version']==2
    assert second['resources']['sales_analysis']['configuration']['actions']==[]
    assert second['customer_assignments']['ids']==sorted(c.id for c in customers)
    assert client.get('/api/authz/me',headers=old_headers).status_code==401
    db.refresh(user);assert user.token_version==2 and user.template_perms=={'page_sales_analysis':False} and user.permissions=={'data_profit':False}
    logs=list(db.scalars(select(SysAuditLog).order_by(SysAuditLog.id)))
    assert len(logs)==2 and logs[1].before_json==first and logs[1].after_json==second


@pytest.mark.parametrize('change',[{'actions':['export']},{'actions':['read_orders','read_orders']},{'scope':'mine'},
    {'field_permissions':['data_profit']},{'field_permissions':['data_reconciliation']},{'field_permissions':['unknown']},
    {'actions':'read_orders'},{'scope':None},{'field_permissions':None}])
def test_invalid_resource_is_atomic(db,client,change):
    admin=_user(db,'operator','admin');user=_user(db,'target')
    _result(_write(client,admin,user,config=_config(**change)),422)
    db.refresh(user);assert user.token_version==0
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant))==0


def test_single_write_stale_epoch_resource_and_missing_customer(db,client):
    admin=_user(db,'operator','admin');user=_user(db,'target')
    _result(_write(client,admin,user))
    _result(_write(client,admin,user,epoch=0,version=1),409)
    _result(_write(client,admin,user,epoch=1,version=0),409)
    _result(_write(client,admin,user,epoch=1,version=1,customer_assignments={'ids':[999999]}),404)
    state=_read(client,admin,user);assert state['authorization_epoch']==1 and state['resources']['sales_analysis']['version']==1


def test_concurrent_first_writes_have_exactly_one_commit(db,client,monkeypatch):
    admin=_user(db,'operator','admin');user=_user(db,'target');barrier=Barrier(2,timeout=10)
    original=management.target;headers=_headers(admin);path=f'/api/authz/users/{user.username}/modules'
    bodies=[_body(expected_epoch=0,updates=[{'resource_id':'sales_analysis','expected_version':0,'operation':'replace_resource','configuration':_config(scope=scope)}]) for scope in ('all','assigned_customers')]
    def simultaneous(*args):
        found=original(*args);barrier.wait();return found
    monkeypatch.setattr(management,'target',simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=[f.result(timeout=20) for f in [pool.submit(client.put,path,headers=headers,json=body) for body in bodies]]
    assert sorted(r.status_code for r in results)==[200,409]
    assert db.scalar(select(func.count()).select_from(SysAuditLog))==1


def test_actor_is_revalidated_under_final_lock(db,client,monkeypatch):
    admin=_user(db,'operator','admin');user=_user(db,'target');uid=admin.id;original=management.target
    def demote(*args):
        found=original(*args)
        with SessionLocal() as other:other.execute(update(SysUser).where(SysUser.id==uid).values(role='sales'));other.commit()
        return found
    monkeypatch.setattr(management,'target',demote)
    _result(_write(client,admin,user),403)
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant))==0


def test_audit_failure_rolls_back_root_transaction(db,client):
    admin=_user(db,'operator','admin');user=_user(db,'target')
    before=_result(_write(client,admin,user))['result']
    def fail(session,*_):
        if any(isinstance(x,SysAuditLog) for x in session.new):raise RuntimeError('audit failure')
    event.listen(Session,'before_flush',fail)
    try:
        with pytest.raises(RuntimeError,match='audit failure'):_write(client,admin,user,epoch=1,version=1,config=_config(scope='all'))
    finally:event.remove(Session,'before_flush',fail)
    assert _read(client,admin,user)==before


def test_self_write_returns_result_then_invalidates_session(db,client):
    admin=_user(db,'operator','admin');headers=_headers(admin)
    response=_result(_write(client,admin,admin))
    assert response['actor_session_revoked'] and response['result']['authorization_epoch']==1
    _result(client.get('/api/authz/me',headers=headers),401)


def test_management_read_holds_shared_user_lock(db,client,monkeypatch):
    admin=_user(db,'operator','admin');user=_user(db,'target');uid=user.id
    _result(_write(client,admin,user));db.rollback();ready,release=Event(),Event();original=management._read_account
    def paused(*args,**kwargs):
        result=original(*args,**kwargs);ready.set();assert release.wait(10);return result
    monkeypatch.setattr(management,'_read_account',paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(_read,client,admin,user)
        try:
            assert ready.wait(10)
            with SessionLocal() as other:
                with pytest.raises(OperationalError) as err:other.scalar(select(SysUser).where(SysUser.id==uid).with_for_update(nowait=True))
                assert err.value.orig.sqlstate=='55P03'
        finally:release.set()
        assert future.result(timeout=15)['authorization_epoch']==1


def test_template_saved_snapshot_equal_override_clear_and_sync(db,client):
    admin=_user(db,'operator','admin');user=_user(db,'target');_tpl(client,admin)
    _,applied=_bulk(client,admin,[user],{'kind':'apply_template','template_code':'test_team','expected_template_version':1,'override_policy':'preserve'})
    _result(applied);db.refresh(user)
    _result(_write(client,admin,user,epoch=1,version=1))
    saved=_result(client.put('/api/role-templates/test_team',headers=_headers(admin),json=_body(expected_version=1,metadata={'name':'v2'},updates=[{'resource_id':'sales_analysis','operation':'replace_resource','configuration':_config(scope='all')}])) )
    assert saved['result']['template']['version']==2
    cleared=_result(_write(client,admin,user,epoch=2,version=2,operation='clear_override'))['result']
    assert cleared['resources']['sales_analysis']['configuration']['scope']=='assigned_customers'
    assert cleared['resources']['sales_analysis']['personal_override']['present'] is False
    db.refresh(user)
    preview=_result(client.post('/api/authz/templates/test_team/sync',headers=_headers(admin),json=_body(dry_run=True,targets=[{'user_id':user.id,'username':user.username,'expected_epoch':user.token_version}],operation={'kind':'sync_template','template_code':'test_team','expected_template_version':2,'override_policy':'preserve'})))
    result=_result(client.post('/api/authz/templates/test_team/sync',headers=_headers(admin),json={**preview['command'],'dry_run':False,'fingerprint':preview['fingerprint']}))
    assert result['results'][0]['resources']['sales_analysis']['configuration']['scope']=='all'
    assert result['results'][0]['subject']['role']=='sales'
    assert result['results'][0]['resources']['sales_analysis']['version']==4


def test_template_resource_removal_revokes_but_saved_user_unchanged_until_sync(db,client):
    admin=_user(db,'operator','admin');user=_user(db,'target');_tpl(client,admin)
    _,res=_bulk(client,admin,[user],{'kind':'apply_template','template_code':'test_team','expected_template_version':1,'override_policy':'preserve'});_result(res)
    _result(client.put('/api/role-templates/test_team',headers=_headers(admin),json=_body(expected_version=1,updates=[{'resource_id':'sales_analysis','operation':'remove_resource'}])))
    assert _read(client,admin,user)['resources']['sales_analysis']['configuration']['actions']==['read_orders']
    db.refresh(user)
    preview=_result(client.post('/api/authz/templates/test_team/sync',headers=_headers(admin),json=_body(dry_run=True,targets=[{'user_id':user.id,'username':user.username,'expected_epoch':1}],operation={'kind':'sync_template','template_code':'test_team','expected_template_version':2,'override_policy':'preserve'})))
    assert preview['preview'][0]['resources'][0]['change']=='revoke'
    result=_result(client.post('/api/authz/templates/test_team/sync',headers=_headers(admin),json={**preview['command'],'dry_run':False,'fingerprint':preview['fingerprint']}))
    assert result['results'][0]['resources']['sales_analysis']['configuration']['actions']==[]


def test_template_copy_archive_cas_and_canonical_membership(db,client):
    admin=_user(db,'operator','admin');user=_user(db,'target',template_code='old_legacy');_tpl(client,admin)
    _,res=_bulk(client,admin,[user],{'kind':'apply_template','template_code':'test_team','expected_template_version':1,'override_policy':'preserve'});_result(res)
    members=_result(client.get('/api/role-templates/test_team/accounts',headers=_headers(admin)))
    assert [row['id'] for row in members['accounts']]==[user.id]
    copy=_result(client.post('/api/role-templates',headers=_headers(admin),json=_body(metadata={'name':'复制','code':'copy_team'},authorization={'kind':'copy_template','code':'test_team','expected_version':1})),201)
    assert copy['result']['modules']['sales_analysis']==_config()
    _result(client.post('/api/role-templates/test_team/archive',headers=_headers(admin),json=_body(expected_version=0)),422)
    archived=_result(client.post('/api/role-templates/test_team/archive',headers=_headers(admin),json=_body(expected_version=1)))
    assert not archived['result']['template']['is_active']
    assert _read(client,admin,user)['authorization_epoch']==1
    _result(client.post('/api/role-templates/test_team/restore',headers=_headers(admin),json=_body(expected_version=1)),409)


def test_new_account_atomic_authorization_security_and_password_do_not_leak(db,client):
    admin=_user(db,'operator','admin')
    body=_body(profile={'username':'new','password':'synthetic_password','display_name':'新账号'},authorization={'kind':'explicit','modules':{'sales_analysis':_config()}})
    result=_result(client.post('/api/accounts',headers=_headers(admin),json=body),201)
    assert result['account']['subject']['role']=='readonly' and result['account']['authorization_epoch']==1
    assert 'synthetic_password' not in str(result)
    new=db.scalar(select(SysUser).where(SysUser.username=='new'));assert verify_password('synthetic_password',new.password_hash)
    bad=deepcopy(body);bad['profile']['username']='must_not_exist';bad['authorization']['modules']['purchase_analysis']=_config()
    _result(client.post('/api/accounts',headers=_headers(admin),json=bad),422)
    assert db.scalar(select(SysUser.id).where(SysUser.username=='must_not_exist')) is None
    preview_body=_body(profile={'username':'new_admin'},authorization={'kind':'none'},security_change={'role':'admin'},dry_run=True)
    preview=_result(client.post('/api/accounts',headers=_headers(admin),json=preview_body))
    execute={**preview['command'],'dry_run':False,'fingerprint':preview['fingerprint']};execute['profile']['password']='synthetic_admin_password'
    created=_result(client.post('/api/accounts',headers=_headers(admin),json=execute),201)
    assert created['account']['subject']['role']=='admin'
    assert created['account']['resources']['sales_analysis']['configuration'] is None


def test_profile_security_lifecycle_and_reset_password(db,client):
    admin=_user(db,'operator','admin');user=_user(db,'target')
    result=_result(client.put('/api/accounts/target',headers=_headers(admin),json=_body(profile={'display_name':'new name'})))
    assert result['authorization_epoch']==0
    preview=_result(client.put('/api/accounts/target',headers=_headers(admin),json=_body(expected_epoch=0,security_change={'salesperson_name':'映射甲'},dry_run=True)))
    _result(client.put('/api/accounts/target',headers=_headers(admin),json={**preview['command'],'dry_run':False,'fingerprint':preview['fingerprint']}))
    _result(client.put('/api/accounts/target/password',headers=_headers(admin),json=_body(expected_epoch=1,password='synthetic_reset_password')))
    db.refresh(user);assert user.salesperson_name=='映射甲' and verify_password('synthetic_reset_password',user.password_hash)
    _result(client.put('/api/accounts/target/active',headers=_headers(admin),json=_body(expected_epoch=2,is_active=False)))
    unchanged=_result(client.put('/api/accounts/target/active',headers=_headers(admin),json=_body(expected_epoch=3,is_active=False)))
    assert unchanged['changed'] is False and unchanged['authorization_epoch']==3
    _result(client.request('DELETE','/api/accounts/target',headers=_headers(admin),json=_body(expected_epoch=3)))
    db.refresh(user);assert user.deleted_at is not None and user.token_version==4


@pytest.mark.parametrize('legacy', [{'permissions':{}},{'overrides':{}},{'keys':[]},{'role':None},{'template_code':None}])
def test_mixed_profile_legacy_fields_reject_whole_command(db,client,legacy):
    admin=_user(db,'operator','admin');user=_user(db,'target')
    _result(client.put('/api/accounts/target',headers=_headers(admin),json=_body(profile={'display_name':'must not write'},**legacy)),422)
    db.refresh(user);assert user.display_name is None and user.token_version==0


def test_old_s0_writer_permanently_retired(db,client,monkeypatch):
    admin=_user(db,'operator','admin');_user(db,'target')
    for mode in ('unified_modules','legacy_compat'):
        monkeypatch.setattr(get_settings(),'authorization_mode',mode)
        res=_result(client.put('/api/authz/users/target',headers=_headers(admin),json={}),409)
        assert res['detail']['code']=='AUTHZ_LEGACY_ENDPOINT_RETIRED'


def test_catalog_planned_and_customer_literal_search(db,client):
    admin=_user(db,'operator','admin')
    modules=_result(client.get('/api/authz/catalog',headers=_headers(admin)))['modules']
    assert any(x['id']=='purchase_analysis' and not x['configurable'] for x in modules)
    db.add_all([DimCustomer(name_raw='literal_100%'),DimCustomer(name_raw='literalX100Y')]);db.commit()
    matches=_result(client.get('/api/authz/customers?q=literal_100%25',headers=_headers(admin)))['items']
    assert len(matches)==1 and matches[0]['raw_name']=='literal_100%'
def _migration():
    path = Path(__file__).parents[1] / "alembic/versions/f1a3c5e7b9d2_authorization_sales_customers.py"
    spec = importlib.util.spec_from_file_location("sales_authorization_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("role", ["admin", "boss", "sales", "purchaser", "readonly", "maintenance_manager", "unknown"])
@pytest.mark.parametrize("snapshot", [None, {}, {"page_sales_analysis": True, "data_profit": True, "own_customers_only": True}])
def test_frozen_migration_projection_matches_current_legacy_semantics(role, snapshot):
    migration = _migration()
    values = {"role": role, "template_perms": snapshot,
              "perm_overrides": {"data_supplier": False, "data_purchase_cost": False},
              "permissions": {"page_sales_analysis": True, "own_customers_only": True, "data_purchase_cost": False}}
    expected = permissions.runtime_safe(permissions.effective_for_user(SimpleNamespace(**values)))
    assert migration._legacy_permissions(values) == {key: expected[key] for key in migration._KEYS}




def test_real_postgres_s0_seed_and_s1_source_backfill_are_distinct(db):
    """Old seed interprets legacy once; S1 copies actual Grant, including deny."""
    backend=Path(__file__).parents[1];config=Config(str(backend/'alembic.ini'))
    config.set_main_option('script_location',str(backend/'alembic'));db.rollback()
    command.downgrade(config,'d8f6a2c4b9e7')
    try:
        import json
        for name,role,snapshot in [('old-admin','admin',{'page_sales_analysis':False}),('old-sales','sales',{'page_sales_analysis':True,'own_customers_only':True}),('old-denied','boss',{'page_sales_analysis':False})]:
            db.execute(text('INSERT INTO sys_user (username,password_hash,role,template_perms) VALUES (:n,:p,:r,CAST(:s AS jsonb))'),{'n':name,'p':'unused','r':role,'s':json.dumps(snapshot)})
        db.commit();command.upgrade(config,'f1a3c5e7b9d2')
        rows=db.execute(text('SELECT u.username,g.actions,g.scope,u.token_version FROM sys_user u JOIN sys_authorization_grant g ON g.user_id=u.id ORDER BY u.username')).all()
        assert [r.username for r in rows]==['old-admin','old-sales']
        assert rows[0].scope=='all' and rows[1].scope=='assigned_customers'
        assert all(r.token_version==0 for r in rows)
        db.execute(text("UPDATE sys_authorization_grant SET actions='[]'::jsonb WHERE user_id=(SELECT id FROM sys_user WHERE username='old-sales')"))
        db.commit();command.upgrade(config,'a7c2e9f4b6d1')
        users={u.username:u for u in db.scalars(select(SysUser).where(SysUser.username.like('old-%')))}
        assert users['old-sales'].authorization_profile['resource_overrides']['sales_analysis']['actions']==[]
        assert users['old-sales'].authorization_profile['template_snapshot'] is None
        assert users['old-sales'].token_version==1 and users['old-admin'].token_version==1
        assert users['old-denied'].authorization_profile==management.empty_profile() and users['old-denied'].token_version==0
        assert all(u.authorization_legacy_seed_eligible is True for u in users.values())
        assert db.scalar(select(func.count()).select_from(SysAuthorizationCutover))==0
        assert db.scalar(select(func.count()).select_from(SysCustomerAssignment))==0
        assert all(t.authorization_bundle=={} for t in db.scalars(select(SysRoleTemplate)))
        assert all(t.authorization_legacy_seed_eligible is True for t in db.scalars(select(SysRoleTemplate)))
    finally:
        db.rollback();command.upgrade(config,'head')


@pytest.mark.parametrize('method,path,kwargs,expected',[
    ('put','/api/authz/users/target/modules',{'json':[]},422),
    ('put','/api/accounts/target',{'content':'{broken json','headers':{'Content-Type':'application/json'}},422),
    ('get','/api/authz/customers?limit=0',{},422),
    ('get','/api/authz/catalog',{'without_auth':True},401),
    ('post','/api/role-templates',{'json':[]},422),
])
def test_parser_and_query_errors_are_closed_wire(db,client,method,path,kwargs,expected):
    admin=_user(db,'operator','admin');_user(db,'target');kwargs=deepcopy(kwargs)
    headers={} if kwargs.pop('without_auth',False) else _headers(admin)
    headers.update(kwargs.pop('headers',{}))
    result=_result(getattr(client,method)(path,headers=headers,**kwargs),expected)
    assert result['detail']['committed'] is False
    assert set(result['detail'])=={'code','message','committed','conflicts','field_errors'}


def test_two_admins_cross_edit_without_lock_upgrade_deadlock(db,client,monkeypatch):
    first=_user(db,'admin-one','admin');second=_user(db,'admin-two','admin')
    headers=[_headers(first),_headers(second)];paths=[f'/api/authz/users/{second.username}/modules',f'/api/authz/users/{first.username}/modules']
    body=_body(expected_epoch=0,updates=[{'resource_id':'sales_analysis','expected_version':0,'operation':'replace_resource','configuration':_config()}])
    barrier=Barrier(2,timeout=10);original=management.target
    def simultaneous(*args):
        found=original(*args);barrier.wait();return found
    monkeypatch.setattr(management,'target',simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(client.put,path,headers=header,json=body) for path,header in zip(paths,headers)]
        results=[f.result(timeout=20) for f in futures]
    assert sorted(r.status_code for r in results)==[200,401]
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant))==1
    assert db.scalar(select(func.sum(SysUser.token_version)))==1


def test_two_admins_cross_disable_leave_one_live_admin(db,client):
    first=_user(db,'admin-one','admin');second=_user(db,'admin-two','admin')
    headers=[_headers(first),_headers(second)];names=[second.username,first.username];barrier=Barrier(2,timeout=10)
    def disable(header,name):
        barrier.wait()
        return client.put(f'/api/accounts/{name}/active',headers=header,json=_body(expected_epoch=0,is_active=False))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=[f.result(timeout=20) for f in [pool.submit(disable,h,n) for h,n in zip(headers,names)]]
    assert sorted(r.status_code for r in results)==[200,401]
    assert db.scalar(select(func.count()).select_from(SysUser).where(SysUser.role=='admin',SysUser.is_active.is_(True)))==1
    survivor=db.scalar(select(SysUser).where(SysUser.is_active.is_(True)))
    _result(client.put(f'/api/accounts/{survivor.username}/active',headers=_headers(survivor),json=_body(expected_epoch=survivor.token_version,is_active=False)),400)


def test_archive_and_sync_serialize_on_template_before_user_locks(db,client,monkeypatch):
    admin=_user(db,'operator','admin');user=_user(db,'target');_tpl(client,admin)
    _,res=_bulk(client,admin,[user],{'kind':'apply_template','template_code':'test_team','expected_template_version':1,'override_policy':'preserve'});_result(res)
    headers=_headers(admin)
    preview=_result(client.post('/api/authz/templates/test_team/sync',headers=headers,json=_body(dry_run=True,
        targets=[{'user_id':user.id,'username':user.username,'expected_epoch':1}],operation={'kind':'sync_template','template_code':'test_team','expected_template_version':1,'override_policy':'preserve'})))
    execute={**preview['command'],'dry_run':False,'fingerprint':preview['fingerprint']}
    barrier=Barrier(2,timeout=10);original=management.template_row
    def simultaneous(*args,**kwargs):
        if kwargs.get('lock'):barrier.wait()
        return original(*args,**kwargs)
    monkeypatch.setattr(management,'template_row',simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        archive=pool.submit(client.post,'/api/role-templates/test_team/archive',headers=headers,json=_body(expected_version=1))
        sync=pool.submit(client.post,'/api/authz/templates/test_team/sync',headers=headers,json=execute)
        archived,synced=archive.result(timeout=20),sync.result(timeout=20)
    _result(archived)
    assert synced.status_code in (200,400),synced.text
    db.refresh(user);assert user.token_version==(2 if synced.status_code==200 else 1)
    tpl=db.scalar(select(SysRoleTemplate).where(SysRoleTemplate.code=='test_team'));assert not tpl.is_active and tpl.version==2


@pytest.mark.parametrize('damage',['mixed_actions','mixed_fields','profile_shape','profile_value','source_mismatch','missing_materialization'])
def test_corrupt_persisted_sources_are_diagnostic_not_500(db,client,damage):
    admin=_user(db,'operator','admin');user=_user(db,'target');_result(_write(client,admin,user))
    db.refresh(user);grant=db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==user.id))
    if damage=='mixed_actions':grant.actions=['read_orders',3]
    elif damage=='mixed_fields':grant.field_permissions=['data_customer',{}]
    elif damage=='profile_shape':user.authorization_profile=[]
    elif damage=='profile_value':user.authorization_profile={**management.empty_profile(),'resource_overrides':{'sales_analysis':{'actions':[3],'scope':'all','field_permissions':[]}}}
    elif damage=='source_mismatch':grant.actions=[]
    else:db.delete(grant)
    db.commit()
    response=_result(client.get('/api/authz/users/target/modules',headers=_headers(admin)),409)
    assert response['detail']['code'] in {'AUTHZ_SOURCE_INVALID','AUTHZ_SOURCE_INCONSISTENT'}
    assert response['detail']['committed'] is False


def test_cutover_marker_prevents_schema_downgrade(db,client):
    backend=Path(__file__).parents[1];config=Config(str(backend/'alembic.ini'))
    config.set_main_option('script_location',str(backend/'alembic'));db.rollback()
    starting_revision = db.scalar(text('SELECT version_num FROM alembic_version'))
    db.rollback()
    with pytest.raises(RuntimeError,match='Cannot downgrade after authorization cutover'):
        command.downgrade(config,'f1a3c5e7b9d2')
    assert db.scalar(select(func.count()).select_from(SysAuthorizationCutover))==1
    assert db.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==starting_revision
    assert db.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_name='sys_user' AND column_name='authorization_profile' AND table_schema=current_schema()" )).scalar_one()==1
    assert db.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_name IN ('sys_user','sys_role_template') AND column_name='authorization_legacy_seed_eligible' AND table_schema=current_schema()")).scalar_one()==2
    db.rollback()


def test_legacy_body_validation_still_returns_422_after_dispatch(db,monkeypatch):
    settings=get_settings()
    monkeypatch.setattr(settings,'authorization_mode','legacy_compat')
    monkeypatch.setattr(settings,'authz_sales_analysis_enabled',False)
    monkeypatch.setattr(settings,'authz_sales_analysis_suspended',False)
    monkeypatch.setattr(settings,'authz_enabled_resources',[])
    monkeypatch.setattr(settings,'authz_suspended_resources',[])
    admin=_user(db,'operator','admin');app=FastAPI();app.include_router(accounts.router,prefix='/api')
    with TestClient(app) as client:
        result=_result(client.post('/api/accounts',headers=_headers(admin),json={'password':'not echoed'}),422)
        assert result['detail']['field_errors'][0]['path']=='/username'
        assert 'not echoed' not in str(result)
