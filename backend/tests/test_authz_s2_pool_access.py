"""Real HTTP/PDP/PG tests of B2 block isolation and mutation authorization."""
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app import config
from app.auth import hash_password
from app.authz import runtime
from app.config import get_settings
from app.main import app
from app.models.authorization import SysAuthorizationGrant
from app.models.dimensions import DimPart
from app.models.inventory import PartPool
from app.models.purchase import FPurchaseOrder
from app.models.system import SysUser, SysAuditLog
from tests.authz_pdp_helpers import cerbos_url
from tests.test_pool_analysis_api import priced_pool

RESOURCES = ['company_summary', 'company_sales_orders', 'company_purchase_orders',
             'company_pool_analysis', 'price_discipline', 'company_pool_order_facts',
             'pool_price_analysis', 'pool_order_facts', 'pool_catalog', 'pool_price_policy']
GOV = 'data_pool_price_governance'


@pytest.fixture(autouse=True)
def authority(db, monkeypatch, cerbos_url):
    monkeypatch.setattr(config, 'ENABLE_RBAC', True)
    settings = get_settings()
    monkeypatch.setattr(settings, 'authorization_mode', 'unified_modules')
    monkeypatch.setattr(settings, 'authz_sales_analysis_enabled', False)
    monkeypatch.setattr(settings, 'authz_enabled_resources', RESOURCES)
    monkeypatch.setattr(settings, 'cerbos_url', cerbos_url)
    runtime.activate_configured_resources(db.get_bind())


def client_with(db, modules, *, role='readonly'):
    user = SysUser(username='b2-reader', role=role, password_hash=hash_password('Synthetic-Pool-2026'),
                   permissions={'page_boss_board': False, 'page_pool_analysis': False,
                                'action_pool_manage': False, 'data_pool_price_governance': False})
    db.add(user); db.flush()
    overrides = {}
    for resource, (actions, fields) in modules.items():
        configuration = {'actions': list(actions), 'scope': 'all', 'field_permissions': list(fields)}
        overrides[resource] = configuration
        db.add(SysAuthorizationGrant(user_id=user.id, resource=resource, version=1, **configuration))
    user.authorization_profile = {'schema_version': 1, 'template_snapshot': None, 'resource_overrides': overrides}
    db.commit()
    client = TestClient(app)
    response = client.post('/api/auth/login', json={'username': user.username, 'password': 'Synthetic-Pool-2026'})
    assert response.status_code == 200, response.text
    client.headers['Authorization'] = 'Bearer ' + response.json()['token']
    return client, user


@contextmanager
def captured_sql(db):
    statements = []
    def capture(conn, cursor, statement, params, context, many):
        statements.append(statement.lower())
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        yield statements
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)


def test_catalog_does_not_query_denied_policy_or_coverage(db, priced_pool):
    client, _ = client_with(db, {'pool_catalog': (['read'], [])}, role='admin')
    with captured_sql(db) as sql:
        result = client.get('/api/pools')
        detail = client.get(f"/api/pools/{priced_pool['gid']}")
    assert result.status_code == detail.status_code == 200
    assert not any('part_pool_price_policy' in statement for statement in sql)
    row = result.json()['items'][0]
    assert row['purchase_ceiling_ex_tax'] is None and row['sales_floor_ex_tax'] is None
    assert result.json()['coverage'] is None
    assert result.json()['block_access']['price_policy']['effect'] == 'deny'
    assert detail.json()['price_policy'] is None and detail.json()['price_policy_history'] == []
    with captured_sql(db) as sql:
        assert client.get('/api/pools?policy_missing=both').status_code == 403
    assert not any('from part_pool ' in s or 'from part_pool_price_policy' in s for s in sql)


def test_policy_fields_are_owned_by_policy_grant(db, priced_pool):
    client, _ = client_with(db, {'pool_catalog': (['read'], []), 'pool_price_policy': (['read'], [GOV])})
    result = client.get(f"/api/pools/{priced_pool['gid']}")
    assert result.status_code == 200, result.text
    assert float(result.json()['purchase_ceiling_ex_tax']) == 160
    assert result.json()['block_access']['price_policy']['effect'] == 'allow'


def test_analysis_does_not_execute_denied_transaction_queries(db, priced_pool, monkeypatch):
    from app.services import pool
    original = pool._pool_sales_orders
    def forbidden(*args, **kwargs):
        pytest.fail('Denied purchase transaction helper was invoked')
    def sales(*args, **kwargs):
        assert kwargs.get('authorized') is False
        return original(*args, **kwargs)
    monkeypatch.setattr(pool, '_pool_purchase_orders', forbidden)
    monkeypatch.setattr(pool, '_pool_sales_orders', sales)
    client, _ = client_with(db, {'pool_price_analysis': (['read'], [GOV])})
    response = client.get(f"/api/pool-analysis/pools/{priced_pool['gid']}?range=all")
    assert response.status_code == 200, response.text
    data = response.json()
    for name in ['purchase_transactions', 'sales_transactions', 'purchase_orders', 'sales_orders']:
        assert data[name]['restricted'] is True and data[name]['total'] is None and data[name]['items'] == []
    assert data['block_access']['purchase_transactions']['effect'] == 'deny'


@pytest.mark.parametrize('analysis_fields,facts_fields', [([GOV], []), ([], [GOV])])
def test_analysis_and_facts_use_their_own_field_contract(db, priced_pool, analysis_fields, facts_fields):
    client, _ = client_with(db, {'pool_price_analysis': (['read'], analysis_fields),
                                'pool_order_facts': (['read'], facts_fields)})
    response = client.get(f"/api/pool-analysis/pools/{priced_pool['gid']}?range=all")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['purchase_transactions'] == data['purchase_orders']
    assert data['sales_transactions'] == data['sales_orders']
    assert data['purchase_orders']['total'] == 2
    amounts = [r['purchase_line_value_ex_tax'] for r in data['purchase_orders']['items']]
    assert all(x is not None for x in amounts) if GOV in facts_fields else all(x is None for x in amounts)
    assert (data['max_purchase_price'] is not None) == (GOV in analysis_fields)


def test_company_and_public_pool_order_actions_are_distinct(db, priced_pool):
    order = db.scalar(select(FPurchaseOrder.id).where(FPurchaseOrder.order_no == 'P1'))
    client, _ = client_with(db, {'pool_order_facts': (['read'], [])})
    assert client.get(f'/api/pool-analysis/orders/purchase/{order}').status_code == 200
    assert client.get(f'/api/dashboard/orders/purchase/{order}').status_code == 403


def test_partial_cutover_never_mixes_legacy_policy(db, priced_pool, monkeypatch):
    client, _ = client_with(db, {'pool_catalog': (['read'], [])}, role='admin')
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', ['pool_catalog'])
    response = client.get('/api/pools')
    assert response.status_code == 503, response.text


def test_readonly_explicit_create_and_edit_work_without_old_role(db, priced_pool):
    client, user = client_with(db, {'pool_catalog': (['create', 'edit'], [])})
    parts = [DimPart(pn_std='CREATE-A'), DimPart(pn_std='CREATE-B')]
    db.add_all(parts); db.commit()
    response = client.post('/api/pools', json={'name': 'explicit only', 'member_part_ids': [p.id for p in parts]})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['created_by'] == user.username and data['purchase_ceiling_ex_tax'] is None
    saved = client.patch(f"/api/pools/{data['group_id']}", json={'version': 1, 'name': 'edited'})
    assert saved.status_code == 200 and saved.json()['version'] == 2, saved.text
    assert saved.json()['name'] == 'edited'


def test_admin_without_action_cannot_mutate_and_leaves_no_audit(db, priced_pool):
    client, _ = client_with(db, {'pool_catalog': (['read'], [])}, role='admin')
    gid = priced_pool['gid']
    before = db.get(PartPool, gid).version
    count = len(db.scalars(select(SysAuditLog)).all())
    response = client.patch(f'/api/pools/{gid}', json={'version': before, 'name': 'forbidden'})
    assert response.status_code == 403, response.text
    db.expire_all()
    assert db.get(PartPool, gid).version == before
    assert len(db.scalars(select(SysAuditLog)).all()) == count


def test_optional_policy_failure_rolls_back_catalog_write(db, priced_pool, monkeypatch):
    client, _ = client_with(db, {'pool_catalog': (['edit'], []), 'pool_price_policy': (['read'], [GOV])})
    monkeypatch.setattr(get_settings(), 'cerbos_url', 'http://127.0.0.1:1')
    gid = priced_pool['gid']; before = db.get(PartPool, gid).version
    response = client.patch(f'/api/pools/{gid}', json={'version': before, 'name': 'must rollback'})
    assert response.status_code == 503, response.text
    db.expire_all()
    assert db.get(PartPool, gid).version == before


def test_company_summary_does_not_authorize_orders_and_pool_optional_blocks(db, priced_pool):
    client, _ = client_with(db, {'company_summary': (['read'], []),
                                'company_pool_analysis': (['read_summary'], [])})
    assert client.get('/api/dashboard/kpi').status_code == 200
    assert client.get('/api/dashboard/sales').status_code == 403
    assert client.get('/api/dashboard/purchase-orders').status_code == 403
    response = client.get(f"/api/dashboard/pool/{priced_pool['gid']}")
    assert response.status_code == 200, response.text
    assert response.json()['sales_orders']['total'] is None
    assert response.json()['customer_cross_brand'] == {'restricted': True, 'multi_brand_customers': None, 'customers': []}


def test_waiting_domain_writer_rechecks_epoch_after_lock(db, priced_pool):
    from concurrent.futures import ThreadPoolExecutor
    from time import monotonic, sleep
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    client, user = client_with(db, {'pool_catalog': (['edit'], [])})
    gid = priced_pool['gid']
    db.rollback()
    with Session(db.get_bind()) as blocker, ThreadPoolExecutor(max_workers=1) as executor:
        blocker.execute(select(PartPool).where(PartPool.group_id == gid).with_for_update())
        pid = blocker.scalar(text('select pg_backend_pid()'))
        pending = executor.submit(client.patch, f'/api/pools/{gid}', json={'version': 2, 'name': 'revoked write'})
        deadline = monotonic()+8
        while monotonic()<deadline:
            with db.get_bind().connect() as probe:
                waiting = probe.scalar(text('select count(*) from pg_stat_activity where :pid = any(pg_blocking_pids(pid))'), {'pid':pid})
            if waiting:
                break
            sleep(.02)
        assert waiting, 'writer never reached the real domain lock'
        with Session(db.get_bind()) as revoker:
            actor=revoker.get(SysUser,user.id)
            actor.token_version += 1
            revoker.commit()
        blocker.commit()
        response=pending.result(timeout=8)
    assert response.status_code == 401, response.text
    db.expire_all()
    assert db.get(PartPool,gid).name != 'revoked write'


def test_fact_only_pool_has_locator_without_analysis_sql(db, priced_pool, monkeypatch):
    from app.services import pool_price_analysis
    def forbidden(*args,**kwargs):
        pytest.fail('Analysis aggregate executed without its Grant')
    monkeypatch.setattr(pool_price_analysis,'references',forbidden)
    monkeypatch.setattr(pool_price_analysis,'_purchase_group_stats',forbidden)
    monkeypatch.setattr(pool_price_analysis,'_sales_group_stats',forbidden)
    client,_=client_with(db,{'pool_order_facts':(['read'],[GOV])})
    with captured_sql(db) as statements:
        listing=client.get('/api/pool-analysis/pools')
        response=client.get(f"/api/pool-analysis/pools/{priced_pool['gid']}?range=all")
    assert listing.status_code==response.status_code==200,response.text
    assert listing.json()['total'] is None and listing.json()['effective_sort']=='group_id'
    data=response.json()
    assert data['block_access']['price_analysis']['effect']=='deny'
    assert data['members']==[] and data['description'] is None and data['purchase_reference'] is None
    assert data['purchase_transactions']['total']>0
    assert data['purchase_transactions']==data['purchase_orders']
    assert client.get('/api/pool-analysis/pools?pn=SECRET').status_code==403
    assert not any('part_pool.description' in sql or 'part_pool.member_count' in sql for sql in statements)


def test_policy_only_pool_can_locate_without_catalog_member_queries(db, priced_pool):
    client,_=client_with(db,{'pool_price_policy':(['read'],[GOV])})
    with captured_sql(db) as statements:
        listing=client.get('/api/pools')
        response=client.get(f"/api/pools/{priced_pool['gid']}")
    assert listing.status_code==response.status_code==200,response.text
    assert listing.json()['total'] is None
    data=response.json()
    assert data['block_access']['catalog']['effect']=='deny'
    assert data['members']==[] and data['description'] is None and data['member_count'] is None
    assert float(data['purchase_ceiling_ex_tax'])==160
    assert not any('dim_part' in sql or 'part_pool_member' in sql or 'part_pool.description' in sql for sql in statements)


@pytest.mark.parametrize('action,fields', [('read_sales_transactions',[]),('read_customer_cross_brand',['data_customer'])])
def test_company_pool_independent_blocks_skip_summary_sql(db,priced_pool,monkeypatch,action,fields):
    from app.services import pool
    def forbidden(*args,**kwargs):
        pytest.fail('Summary query executed for an independent block')
    monkeypatch.setattr(pool,'_purchase_member_stats',forbidden)
    monkeypatch.setattr(pool,'_sale_member_stats',forbidden)
    monkeypatch.setattr(pool,'_pool_stats',forbidden)
    client,_=client_with(db,{'company_pool_analysis':([action],fields)})
    with captured_sql(db) as statements:
        listing=client.get('/api/dashboard/pools')
        response=client.get(f"/api/dashboard/pool/{priced_pool['gid']}")
    assert listing.status_code==response.status_code==200,response.text
    assert listing.json()['total'] is None and listing.json()['effective_sort']=='group_id'
    data=response.json()
    assert data['block_access']['summary']['effect']=='deny'
    assert data['members']==[] and data['demand'] is None and data['description'] is None
    assert data['purchase_orders']['items']==[] and data['purchase_orders']['total'] is None
    block='sales_orders' if action=='read_sales_transactions' else 'customer_cross_brand'
    assert data['block_access'][block]['effect']=='allow'
    assert not any('f_purchase' in sql or 'part_pool.description' in sql for sql in statements)


def test_policy_write_receipt_does_not_borrow_catalog_identity(db,priced_pool):
    client,_=client_with(db,{'pool_price_policy':(['read','set'],[GOV])})
    current=client.get(f"/api/pools/{priced_pool['gid']}").json()
    response=client.put(f"/api/pools/{priced_pool['gid']}/price-policy",json={
        'version':current['version'],'purchase_value':'170','purchase_basis':'ex_tax'})
    assert response.status_code==200,response.text
    result=response.json()
    assert float(result['purchase_ceiling_ex_tax'])==170 and result['price_policy'] is not None
    assert result['member_count'] is None and result['created_by'] is None and result['description'] is None
