"""Independent B1 HTTP adversarial checks: real PostgreSQL and real Cerbos."""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select

from app.api import inventory, profit, purchases, sales_analytics, system_settings
from app.auth import _make_token
from app.authz import runtime
from app.config import get_settings
from app.etl import loader
from app.models.authorization import SysAuthorizationGrant
from app.models.inventory import Inventory
from app.models.system import SysAuditLog, SysBusinessSetting, SysImportBatch, SysUser
from app.services import profit as profit_service
from tests import factories as f
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def b1(db, monkeypatch, cerbos_url):
    settings = get_settings()
    for key, value in {'authorization_mode': 'unified_modules',
        'authz_enabled_resources': ['sales_analysis', 'purchase_analysis', 'purchase_records', 'profit_analysis', 'inventory', 'display_policy'],
        'authz_suspended_resources': [], 'authz_sales_analysis_enabled': False, 'authz_sales_analysis_suspended': False,
        'cerbos_url': cerbos_url, 'cerbos_timeout_seconds': 2.0}.items():
        monkeypatch.setattr(settings, key, value)
    runtime.activate_configured_resources(db.get_bind())
    app = FastAPI()
    for router in (inventory.router, profit.router, purchases.router, sales_analytics.router, system_settings.router):
        app.include_router(router, prefix='/api')
    with TestClient(app) as client:
        yield client


def subject(db, resource, actions, fields=(), role='readonly'):
    config = {'actions': list(actions), 'scope': 'all', 'field_permissions': list(fields)}
    user = SysUser(username='b1-adversary', role=role, password_hash='synthetic',
        permissions={'page_purchase_analytics': True, 'own_customers_only': True},
        authorization_profile={'schema_version': 1, 'template_snapshot': None, 'resource_overrides': {resource: config}})
    db.add(user)
    db.flush()
    db.add(SysAuthorizationGrant(user_id=user.id, resource=resource, **config, version=1))
    db.commit()
    token, _ = _make_token(role=role, sub=user.username, name=None, authn='sys_user', token_version=user.token_version)
    return user, {'Authorization': 'Bearer '+token}


def seed_facts(db):
    batch = SysImportBatch(filename='synthetic.xlsx', file_type='purchase', file_hash='b1-adversarial')
    db.add(batch)
    db.flush()
    ph = {'P1': f.purchase_head('P1', on=date.today() - timedelta(days=60), supplier='SECRET-SUPPLIER', is_tax_inclusive=False)}
    loader.load(db, f.purchase_result(ph, [f.purchase_line('P1', 'PL1', 'B1-PART', qty='10', price='137')]), batch.id, date.today())
    sh = {'S1': f.sales_head('S1', on=date.today() - timedelta(days=30))}
    loader.load(db, f.sales_result(sh, [f.sales_line('S1', 'SL1', 'B1-PART', qty='1', price='339')]), batch.id, date.today())
    loader.load(db, f.inventory_result([f.inventory_row('INV1', 'B1-PART')]), batch.id, date.today())
    db.commit()
    profit_service.recompute(db)


@pytest.mark.parametrize('path', ['/options', '/orders', '/groups?group_by=business_type', '/trend', '/amount-histogram', '/orders/export', '/orders/1'])
def test_sales_summary_action_cannot_open_other_endpoints(db, b1, path):
    _, h = subject(db, 'sales_analysis', ['read_summary'])
    assert b1.get('/api/sales/analytics/summary', headers=h).status_code == 200
    seen = []
    def sql(conn, cursor, statement, parameters, context, many):
        if 'f_sales_order' in statement.lower() or 'f_sales_line' in statement.lower():
            seen.append(statement)
    event.listen(db.get_bind(), 'before_cursor_execute', sql)
    try:
        response = b1.get('/api/sales/analytics'+path, headers=h)
        assert response.status_code == 403, response.text
        assert seen == []
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', sql)


def test_explicit_purchase_analysis_grant_allows_non_boss_without_legacy_scope_gate(db, b1):
    _, h = subject(db, 'purchase_analysis', ['read'], role='purchaser')
    response = b1.get('/api/purchases/analytics/summary', headers=h)
    assert response.status_code == 200, response.text
    assert b1.get('/api/purchases/recent', headers=h).status_code == 403


def test_purchase_supplier_filter_rejects_before_fact_sql_and_financial_derivatives_hide(db, b1):
    seed_facts(db)
    _, h = subject(db, 'purchase_records', ['read'])
    seen = []
    def sql(conn, cursor, statement, parameters, context, many):
        if 'f_purchase_order' in statement.lower(): seen.append(statement)
    event.listen(db.get_bind(), 'before_cursor_execute', sql)
    try:
        response = b1.get('/api/purchases/analysis?supplier=SECRET-SUPPLIER', headers=h)
        assert response.status_code == 403, response.text
        assert seen == []
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', sql)
    response = b1.get('/api/purchases/analysis?days=366', headers=h)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['rows'], value
    for row in value['rows']:
        assert row['price_trend'] is None and row['advice'] is None
        for key, field in row.items():
            if key.startswith(('price_ex_', 'price_inc_')): assert field is None, key
    assert 'SECRET-SUPPLIER' not in response.text
    assert value['kpi']['total_amount'] is None


def test_profit_csv_keeps_public_revenue_but_no_financial_or_customer_sidechannel(db, b1):
    seed_facts(db)
    _, h = subject(db, 'profit_analysis', ['read_part_analysis', 'export'])
    result = b1.get('/api/profit', headers=h)
    assert result.status_code == 200 and result.json()['rows'], result.text
    assert b1.get('/api/profit?dimension=customer', headers=h).status_code == 403
    assert b1.get('/api/profit?only_anomaly=true', headers=h).status_code == 403
    exported = b1.get('/api/profit/export', headers=h)
    assert exported.status_code == 200, exported.text
    rows = list(csv.reader(io.StringIO(exported.content.decode('utf-8-sig'))))
    assert rows[1][0] == 'B1-PART' and Decimal(rows[1][1]) == Decimal('339')
    assert all(cell == '' for cell in rows[1][3:15]), rows[1]


def test_inventory_explicit_nonadmin_adjust_projects_postview_and_audits_real_actor(db, b1):
    seed_facts(db)
    user, h = subject(db, 'inventory', ['read', 'adjust'], role='purchaser')
    inv = db.scalar(select(Inventory))
    inv.unit_cost = Decimal('137'); inv.inventory_value = Decimal('685'); db.commit()
    response = b1.put(f'/api/inventory/{inv.id}', headers=h, json={'manual_qty': 7, 'reason': 'synthetic'})
    assert response.status_code == 200, response.text
    assert response.json()['unit_cost'] is None and response.json()['inventory_value'] is None
    db.expire_all()
    assert db.get(Inventory, inv.id).manual_qty == 7
    audit = db.scalar(select(SysAuditLog).where(SysAuditLog.entity_type == 'inventory').order_by(SysAuditLog.id.desc()))
    assert audit.operated_by == user.username


def test_display_policy_update_does_not_grant_audit_identity(db, b1):
    _, h = subject(db, 'display_policy', ['read', 'update'], role='purchaser')
    before = b1.get('/api/system-settings', headers=h)
    assert before.status_code == 200, before.text
    response = b1.put('/api/system-settings', headers=h, json={
        'maintenance_display_basis': 'inc', 'purchase_display_basis': 'both', 'sales_display_basis': 'ex',
        'expected_version': before.json()['version']})
    assert response.status_code == 200, response.text
    assert 'updated_by' not in response.json() and 'updated_at' not in response.json()
    assert db.scalar(select(SysBusinessSetting)).version == before.json()['version']+1


def test_pdp_failure_blocks_inventory_mutation_without_audit(db, b1, monkeypatch):
    seed_facts(db)
    _, h = subject(db, 'inventory', ['read', 'adjust'])
    inv = db.scalar(select(Inventory)); inv_id = inv.id
    before = (inv.manual_qty, db.scalar(select(SysBusinessSetting)).version)
    db.rollback()
    monkeypatch.setattr(get_settings(), 'cerbos_url', 'http://127.0.0.1:1')
    response = b1.put(f'/api/inventory/{inv_id}', headers=h, json={'manual_qty': 99})
    assert response.status_code == 503, response.text
    db.expire_all()
    assert db.get(Inventory, inv_id).manual_qty == before[0]
    assert db.scalar(select(SysAuditLog).where(SysAuditLog.entity_type == 'inventory')) is None


@pytest.mark.parametrize('resource', ['inventory', 'display_policy'])
def test_revocation_while_waiting_for_domain_lock_prevents_write(db, b1, resource):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    seed_facts(db)
    actions = ['read', 'adjust'] if resource == 'inventory' else ['read', 'update']
    user, h = subject(db, resource, actions)
    user_id = user.id
    if resource == 'inventory':
        row = db.scalar(select(Inventory))
        row_id, before = row.id, row.manual_qty
        model, table = Inventory, 'inventory'
        path, body = f'/api/inventory/{row_id}', {'manual_qty': 99}
    else:
        row = db.scalar(select(SysBusinessSetting))
        row_id, before = row.id, row.version
        model, table = SysBusinessSetting, 'sys_business_setting'
        path, body = '/api/system-settings', {
            'maintenance_display_basis': 'inc', 'purchase_display_basis': 'both',
            'sales_display_basis': 'ex', 'expected_version': before}
    db.rollback()
    reached = Event()
    def observe(conn, cursor, statement, parameters, context, many):
        lowered = statement.lower()
        if current_thread().name != 'MainThread' and f'from {table}' in lowered and 'for update' in lowered:
            reached.set()
    blocker = Session(db.get_bind())
    blocker.scalar(select(model).where(model.id == row_id).with_for_update())
    event.listen(db.get_bind(), 'before_cursor_execute', observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(b1.put, path, headers=h, json=body)
            try:
                assert reached.wait(5), 'writer did not reach domain lock'
                # A premature actor SHARE would make this UPDATE time out.
                with Session(db.get_bind()) as revoker:
                    revoker.execute(text("SET LOCAL lock_timeout = '1s'"))
                    actor = revoker.get(SysUser, user_id)
                    actor.token_version += 1
                    revoker.commit()
            finally:
                blocker.rollback()
            response = future.result(timeout=5)
        assert response.status_code == 401, response.text
        db.expire_all()
        current = db.get(model, row_id)
        assert (current.manual_qty if resource == 'inventory' else current.version) == before
        assert db.scalar(select(SysAuditLog).where(SysAuditLog.entity_type == resource)) is None
    finally:
        blocker.close()
        event.remove(db.get_bind(), 'before_cursor_execute', observe)
