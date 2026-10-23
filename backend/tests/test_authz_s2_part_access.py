"""Part projections and independent master/substitute authorization with real PDP."""
import pytest
from sqlalchemy import select

from app.authz import runtime
from app.config import get_settings
from app.models.dimensions import DimPart
from app.models.inventory import PartSubstitute
from tests.test_authz_s2_pool_access import client_with, captured_sql
from tests.authz_pdp_helpers import cerbos_url


@pytest.fixture(autouse=True)
def authority(db, monkeypatch, cerbos_url):
    settings = get_settings()
    monkeypatch.setattr(settings, 'authorization_mode', 'unified_modules')
    monkeypatch.setattr(settings, 'authz_sales_analysis_enabled', False)
    monkeypatch.setattr(settings, 'authz_enabled_resources', ['part_lookup', 'part_master', 'part_substitute'])
    monkeypatch.setattr(settings, 'cerbos_url', cerbos_url)
    runtime.activate_configured_resources(db.get_bind())


@pytest.fixture
def parts(db):
    a, b = DimPart(pn_std='PART-A'), DimPart(pn_std='PART-B')
    db.add_all([a, b]); db.flush()
    db.add(PartSubstitute(part_id_a=a.id, part_id_b=b.id, source='manual', direction='both', status='active'))
    db.commit()
    return a, b


def test_overview_identity_only_never_queries_denied_facts(db, parts):
    client, _ = client_with(db, {'part_lookup': (['read'], [])}, role='admin')
    with captured_sql(db) as statements:
        response = client.get('/api/parts/overview', params={'part_id': parts[0].id})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['part']['pn_std'] == 'PART-A'
    assert data['purchases_recent'] == data['sales_recent'] == data['inventory'] == data['substitutes'] == []
    assert data['sales_velocity'] is None and data['stock_dynamic'] is None
    assert data['profit_summary']['total_qty_sold'] is None
    assert data['inquiry_ref']['count'] is None
    assert data['sale_price_ref']['ref_sale_samples'] is None
    assert all(v['effect'] == 'deny' for k, v in data['block_access'].items() if k != 'identity')
    assert not any(any(table in s for table in ('f_purchase', 'f_sales', 'inventory ', 'part_substitute', 'f_part_inquiry')) for s in statements)


def test_substitute_relationship_never_implies_stock(db, parts):
    client, _ = client_with(db, {'part_lookup': (['read'], []), 'part_substitute': (['read'], [])})
    response = client.get('/api/parts/overview', params={'pn_std': 'PART-A'})
    assert response.status_code == 200, response.text
    row = response.json()['substitutes'][0]
    assert row['pn_std'] == 'PART-B' and row['stock_qty'] is None
    assert response.json()['block_access']['substitutes']['effect'] == 'allow'
    assert response.json()['block_access']['substitute_inventory']['effect'] == 'deny'


def test_master_search_does_not_borrow_lookup_or_browse_override(db, parts):
    client, _ = client_with(db, {'part_master': (['read'], [])})
    master = client.get('/api/parts/master/search?q=PART')
    assert master.status_code == 200 and master.json()['total'] == 2, master.text
    assert client.get('/api/parts/search?q=PART&browse=true').status_code == 403
    assert client.get('/api/parts/master/categories').status_code == 200
    assert client.get('/api/parts/master/check?pn_std=PART-A').status_code == 200
    assert client.get(f'/api/parts/master/{parts[0].id}/edit-state').status_code == 200
    assert client.post('/api/parts/master', json={'pn_std': 'MUST-NOT-CREATE'}).status_code == 403
    assert db.scalar(select(DimPart).where(DimPart.pn_std == 'MUST-NOT-CREATE')) is None


def test_master_explicit_create_without_legacy_page(db):
    client, _ = client_with(db, {'part_master': (['read', 'create'], [])})
    response = client.post('/api/parts/master', json={'pn_std': 'NEW-CANONICAL', 'force': True})
    assert response.status_code == 200 and response.json()['created'] is True, response.text
    assert db.scalar(select(DimPart.id).where(DimPart.pn_std == 'NEW-CANONICAL'))


def test_substitute_manage_without_purchaser_role(db, parts):
    client, _ = client_with(db, {'part_substitute': (['manage'], [])})
    response = client.delete('/api/substitutes', params={'pn_a': 'PART-A', 'pn_b': 'PART-B'})
    assert response.status_code == 200 and response.json()['deleted'] is True, response.text
    response = client.post('/api/substitutes', json={'pn_a': 'PART-A', 'pn_b': 'PART-B'})
    assert response.status_code == 200 and response.json()['created'] is True, response.text
    assert client.get('/api/substitutes?pn_std=PART-A').status_code == 403


@pytest.mark.parametrize('other_write', ['pool_members_fk', 'substitute_parents'])
def test_same_actor_cross_domain_writes_do_not_upgrade_shared_user_lock(db, monkeypatch, other_write):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep
    from sqlalchemy import text
    from app.services import master_edit

    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', ['part_lookup','part_master','part_substitute','pool_catalog','pool_price_policy'])
    runtime.activate_configured_resources(db.get_bind())
    a=master_edit.create_part(db,pn_std='LOCK-A',force=True)
    b=master_edit.create_part(db,pn_std='LOCK-B',force=True)
    client,_=client_with(db,{'part_master':(['read','edit'],[]),'part_substitute':(['manage'],[]),'pool_catalog':(['create'],[])})
    baseline=client.get(f"/api/parts/master/{a['id']}/edit-state").json()
    locked,release=Event(),Event()
    original=master_edit.lock_part
    blocker_pid=[]
    def pause_after_parent_lock(session, **kwargs):
        part=original(session,**kwargs)
        blocker_pid.append(session.scalar(text('select pg_backend_pid()')))
        locked.set()
        assert release.wait(10), 'test failed to release the parent writer'
        return part
    monkeypatch.setattr(master_edit,'lock_part',pause_after_parent_lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first=executor.submit(client.patch,'/api/parts/master',json={'part_id':a['id'],'pn_std':'LOCK-A',
            'base_snapshot':baseline,'description':'a verified edit'})
        try:
            assert locked.wait(8)
            if other_write=='pool_members_fk':
                second=executor.submit(client.post,'/api/pools',json={'name':'concurrent pool','member_part_ids':[a['id'],b['id']]})
            else:
                second=executor.submit(client.post,'/api/substitutes',json={'pn_a':'LOCK-A','pn_b':'LOCK-B'})
            deadline=monotonic()+8; waiting=[]
            while monotonic()<deadline:
                with db.get_bind().connect() as probe:
                    waiting=list(probe.scalars(text('select query from pg_stat_activity where :pid = any(pg_blocking_pids(pid))'),{'pid':blocker_pid[0]}))
                if waiting:break
                sleep(.02)
            assert waiting, 'second domain never blocked on its actual PostgreSQL lock'
            if other_write=='pool_members_fk':
                assert any('part_pool_member' in query for query in waiting), waiting
        finally:
            release.set()
        responses=[first.result(timeout=10),second.result(timeout=10)]
    assert [response.status_code for response in responses]==[200,200], [response.text for response in responses]
    assert client.get(f"/api/parts/master/{a['id']}/edit-state").json()['fields']['description']=='a verified edit'


def test_normalization_financial_fields_do_not_control_hidden_ranking(db, parts):
    client,_=client_with(db,{'part_master':(['normalize_preview'],[])})
    with captured_sql(db) as statements:
        response=client.get('/api/parts/master/batch-preview?only_changes=false')
    assert response.status_code==200,response.text
    assert all(row['recent_sales_amount'] is None for row in response.json()['items'])
    assert not any('f_sales_line' in sql for sql in statements)


@pytest.mark.parametrize('resource,action,allowed_block', [
    ('part_lookup','inventory_read','inventory'),
    ('part_lookup','purchase_records_read','purchases_recent'),
    ('part_lookup','sales_records_read','sales_recent'),
    ('part_lookup','financial_summary_read','profit_summary'),
    ('part_lookup','market_prices_read','market_prices'),
    ('part_substitute','read','substitutes'),
])
def test_independent_overview_block_does_not_require_identity(db,parts,resource,action,allowed_block):
    client,_=client_with(db,{resource:([action],[])})
    part_id = parts[0].id
    with captured_sql(db) as statements:
        response=client.get('/api/parts/overview',params={'part_id':part_id})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['part'] is None and result['block_access']['identity']['effect']=='deny'
    assert result['block_access'][allowed_block]['effect']=='allow'
    # Relationship itself includes the other model description. Other blocks
    # need only the four routing columns; never hydrate the current model.
    if resource=='part_lookup':
        assert not any('dim_part.description' in sql or 'dim_part.category_major' in sql for sql in statements)


def test_normalize_revalidates_after_each_committed_row(db, monkeypatch):
    from sqlalchemy import update
    from sqlalchemy.orm import Session
    from app.db import SessionLocal
    from app.models.system import SysUser
    from app.services import batch_normalize
    a = DimPart(pn_std='ST8000NM001A', description='8TB 12Gbps 7200转 256MB 3.5inch SAS HDD', machine_or_part='备件')
    b = DimPart(pn_std='AL15SEB18EQ', description='1.8TB 12Gbps 10K 128MB Cache 2.5inch SAS HDD', machine_or_part='备件')
    db.add_all([a,b]); db.commit()
    ids = [a.id,b.id]; before = b.description
    client,user=client_with(db,{'part_master':(['read','normalize_apply'],[])})
    user_id=user.id; revoked=[]
    original_apply, original_commit = batch_normalize.apply_batch, Session.commit
    def track_row(session,*args,**kwargs):
        result=original_apply(session,*args,**kwargs)
        session.info['normalized_row']=True
        return result
    def commit_and_revoke(session):
        original_commit(session)
        if session.info.pop('normalized_row',False) and not revoked:
            with SessionLocal() as admin:
                admin.execute(update(SysUser).where(SysUser.id==user_id).values(token_version=SysUser.token_version+1))
                original_commit(admin)
            revoked.append(True)
    monkeypatch.setattr(batch_normalize,'apply_batch',track_row)
    monkeypatch.setattr(Session,'commit',commit_and_revoke)
    response=client.post('/api/parts/master/batch-apply',json={'part_ids':ids})
    assert response.status_code==401,response.text
    assert revoked==[True]
    db.expire_all()
    assert db.get(DimPart,ids[0]).description=='8TB SAS HDD 12Gb 7.2K 3.5'
    assert db.get(DimPart,ids[1]).description==before


@pytest.mark.parametrize('side,action', [('purchases','purchase_records_read'),('sales','sales_records_read')])
def test_history_action_resolves_only_routing_identity(db,parts,side,action):
    client,_=client_with(db,{'part_lookup':([action],[])})
    with captured_sql(db) as statements:
        response=client.get('/api/parts/'+side,params={'pn_std':'PART-A'})
    assert response.status_code==200,response.text
    routing=[sql for sql in statements if 'from dim_part' in sql]
    assert routing and not any('dim_part.description' in sql or 'dim_part.category_major' in sql or 'dim_part.brand' in sql for sql in routing)
