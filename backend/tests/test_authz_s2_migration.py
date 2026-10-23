"""Real migration/rollback proof for saved sources and monotonic counters."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from alembic import command
import pytest
from sqlalchemy import text
from tests.conftest import _alembic_cfg

path=Path(__file__).parents[1]/'alembic/versions/c9d4e8f2a6b1_business_authorization.py'
spec=importlib.util.spec_from_file_location('frozen_s2',path)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
S1='a7c2e9f4b6d1'
EMPTY={'schema_version':1,'template_snapshot':None,'resource_overrides':{}}
OLD={'actions':['read','export'],'scope':'assigned_customers','field_permissions':['data_customer']}
DENY={'actions':[],'scope':'assigned_customers','field_permissions':[]}

def down(db):
 db.commit();command.downgrade(_alembic_cfg(),S1);db.expire_all()
def up(db):
 db.commit();command.upgrade(_alembic_cfg(),'head');db.expire_all()
def row(db,uid):
 return db.execute(text('SELECT token_version,authorization_profile,authorization_legacy_seed_eligible FROM sys_user WHERE id=:id'),{'id':uid}).mappings().one()
def insert(db,name,*,eligible=True,role='sales',profile=None,active=True):
 return db.execute(text('''INSERT INTO sys_user(username,password_hash,role,is_active,authorization_legacy_seed_eligible,authorization_profile)
 VALUES (:name,'unused',:role,:active,:eligible,CAST(:profile AS jsonb)) RETURNING id'''),
 {'name':name,'role':role,'active':active,'eligible':eligible,'profile':json.dumps(profile or EMPTY)}).scalar_one()
def grant(db,uid,config,version=7):
 db.execute(text('''INSERT INTO sys_authorization_grant(user_id,resource,actions,scope,field_permissions,version)
 VALUES (:id,'sales_analysis',CAST(:actions AS jsonb),:scope,CAST(:fields AS jsonb),:version)'''),
 {'id':uid,'actions':json.dumps(config['actions']),'scope':config['scope'],'fields':json.dumps(config['field_permissions']),'version':version})

@pytest.mark.parametrize('role',['admin','boss','sales','purchaser','readonly','maintenance_manager'])
def test_frozen_legacy_interpreter(role):
 from app import permissions
 for snapshot in (None,{}, {'page_parts':True,'data_purchase_cost':False,'data_profit':True}):
  for overrides in ({},{'page_parts':False,'own_customers_only':True}):
   source={'role':role,'template_perms':snapshot,'perm_overrides':overrides,'permissions':{'page_profit':False}}
   assert m.effective(source)==permissions.runtime_safe(permissions.effective_for_user(SimpleNamespace(**source)))

def test_seed_eligibility_presence_and_old_role_gate():
 source={'role':'admin','is_active':False,'authorization_legacy_seed_eligible':True}
 assert 'inventory' not in m.seed(source,{'inventory':{'actions':[],'scope':'all','field_permissions':[]}})
 assert m.seed(source,{})['purchase_analysis']['actions']
 assert m.seed({**source,'authorization_legacy_seed_eligible':False},{})=={}
 assert m.seed({**source,'deleted_at':'deleted'},{})=={}
 assert m.seed({'role':'purchaser','authorization_legacy_seed_eligible':True,'permissions':{'page_purchase_analytics':True}}, {})['purchase_analysis']['actions']==[]

def test_sales_conversion_independent_expected_actions():
 config={'actions':['read'],'scope':'assigned_customers','field_permissions':['data_customer']}
 result=m.sales_upgrade(config)
 assert result['scope']==config['scope'] and result['field_permissions']==config['field_permissions']
 assert set(result['actions'])=={'read_options','read_summary','read_groups','read_trend','read_orders','read_order_detail',
  'show_overview','show_compact','show_groups','show_trend','show_orders','show_summary_counts','show_summary_amounts','show_order_detail'}
 assert m.sales_upgrade(DENY)==DENY

def test_real_upgrade_retains_saved_snapshot_override_and_canonical_empty(db):
 down(db)
 try:
  profile={'schema_version':1,'template_snapshot':{'code':'historical','version':3,'modules':{'sales_analysis':OLD}},'resource_overrides':{'sales_analysis':DENY}}
  uid=insert(db,'s2_legacy',profile=profile,active=False);grant(db,uid,DENY)
  empty=insert(db,'s2_canonical',eligible=False,role='admin');up(db)
  current=row(db,uid);source=current['authorization_profile']
  assert source['template_snapshot']['version']==3 and set(source['template_snapshot']['modules'])=={'sales_analysis'}
  assert 'read_orders' in source['template_snapshot']['modules']['sales_analysis']['actions']
  assert source['resource_overrides']['sales_analysis']==DENY and len(source['resource_overrides'])==23
  assert current['authorization_legacy_seed_eligible'] is True and current['token_version']>7
  assert row(db,empty)['authorization_profile']==EMPTY
  assert db.execute(text('SELECT count(*) FROM sys_authorization_grant WHERE user_id=:id'),{'id':empty}).scalar()==0
  saved=deepcopy(current);up(db);assert dict(row(db,uid))==dict(saved)
 finally:
  db.rollback();command.upgrade(_alembic_cfg(),'head')

def test_downgrade_upgrade_never_reuses_versions(db):
 down(db)
 try:
  uid=insert(db,'s2_monotone',profile={'schema_version':1,'template_snapshot':None,'resource_overrides':{'sales_analysis':OLD}})
  grant(db,uid,OLD,31);up(db);first=row(db,uid)['token_version']
  first_grant=db.execute(text("SELECT version FROM sys_authorization_grant WHERE user_id=:id AND resource='inventory'"),{'id':uid}).scalar_one()
  down(db);restored=dict(row(db,uid))
  assert restored['authorization_profile']['resource_overrides']=={'sales_analysis':OLD}
  assert restored['token_version']>max(first,first_grant) and restored['authorization_legacy_seed_eligible'] is True
  up(db);assert row(db,uid)['token_version']>restored['token_version']
  assert db.execute(text("SELECT version FROM sys_authorization_grant WHERE user_id=:id AND resource='inventory'"),{'id':uid}).scalar_one()>first_grant
 finally:
  db.rollback();command.upgrade(_alembic_cfg(),'head')

def test_unchanged_preexisting_explicit_denials_have_rollback_receipts(db):
 down(db)
 try:
  profile={'schema_version':1,'template_snapshot':None,'resource_overrides':{'sales_analysis':DENY}}
  uid=insert(db,'s2_unchanged_deny',eligible=False,profile=profile);grant(db,uid,DENY,11)
  tid=db.execute(text('''INSERT INTO sys_role_template(code,name,base_role,permissions,is_system,is_active,
    version,authorization_legacy_seed_eligible,authorization_bundle)
    VALUES ('s2_unchanged_deny','deny','readonly','{}',false,true,9,false,CAST(:bundle AS jsonb)) RETURNING id'''),
    {'bundle':json.dumps({'sales_analysis':DENY})}).scalar_one()
  up(db)
  assert row(db,uid)['token_version']==0
  assert db.execute(text('SELECT version FROM sys_role_template WHERE id=:id'),{'id':tid}).scalar_one()==9
  for kind,entity in [('user',uid),('template',tid)]:
   receipt=db.execute(text('SELECT before_state,after_state FROM sys_authorization_migration_receipt WHERE entity_kind=:kind AND entity_id=:id'),{'kind':kind,'id':str(entity)}).one()
   assert receipt.before_state==receipt.after_state
  down(db)
  assert row(db,uid)['authorization_profile']==profile and row(db,uid)['token_version']>11
  assert db.execute(text('SELECT authorization_bundle FROM sys_role_template WHERE id=:id'),{'id':tid}).scalar_one()=={'sales_analysis':DENY}
 finally:
  db.rollback();command.upgrade(_alembic_cfg(),'head')

@pytest.mark.parametrize('change',['aba','missing_receipt','canonical_insert','artifact','chat'])
def test_downgrade_refuses_new_or_unprovable_state(db,change):
 down(db)
 try:
  uid=insert(db,'s2_protected');up(db)
  if change=='aba':db.execute(text('UPDATE sys_user SET token_version=token_version+2 WHERE id=:id'),{'id':uid})
  elif change=='missing_receipt':db.execute(text("DELETE FROM sys_authorization_migration_receipt WHERE entity_kind='user'"))
  elif change=='canonical_insert':
   other=insert(db,'s2_unrecorded',eligible=False)
   db.execute(text("INSERT INTO sys_authorization_grant(user_id,resource,actions,scope,field_permissions) VALUES (:id,'inventory','[]','all','[]')"),{'id':other})
  elif change=='artifact':
   from app.authz.runtime import mark_artifact_protocol_used
   db.commit();mark_artifact_protocol_used(db.get_bind())
  else:
   from app.models.chat import ChatSession
   db.add(ChatSession(owner_sub='s2_protected',owner_user_id=uid,title='x'))
  db.commit()
  with pytest.raises(RuntimeError):command.downgrade(_alembic_cfg(),S1)
  assert db.execute(text("SELECT to_regclass('sys_authorization_migration_receipt')")).scalar() is not None
 finally:
  db.rollback();command.upgrade(_alembic_cfg(),'head')
