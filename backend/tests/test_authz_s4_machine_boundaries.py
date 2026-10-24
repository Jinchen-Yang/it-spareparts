"""Fixed server credential contracts reject before any business SQL."""
import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
import pytest
from sqlalchemy import event

from app.api import resale_catalog_export, resale_export
from app.auth import _make_token
from app.authz.machine_access import SERVICE_ACTIONS, require_service
from app.config import Settings, get_settings
from app.db import get_db

CATALOG = 'synthetic-s4-catalog-secret-at-least-32'
RETURN = 'synthetic-s4-return-secret-at-least-32'
ENTRIES = [
    ('POST', '/parts/resolve', {'items':[{'sourceKey':'synthetic','pnRaw':'not-found'}]}, 'resale_catalog.resolve'),
    ('POST', '/parts/snapshots', {'partIds':[999999]}, 'resale_catalog.snapshots'),
    ('POST', '/parts/list', {'limit':1}, 'resale_catalog.list'),
    ('POST', '/parts/search', {'q':'not-found'}, 'resale_catalog.search'),
    ('POST', '/parts/price-evidence', {'partIds':[999999], 'side':'sales'}, 'resale_catalog.price_evidence'),
    ('GET', '/parts/price-reference?pn=not-found', None, 'resale_price_reference.read'),
    ('GET', '/bad-receipts', None, 'resale_return_feed.read'),
]


@pytest.fixture
def client(db, monkeypatch):
    settings = get_settings()
    for name, value in [('resale_catalog_token_sha256',CATALOG),('resale_export_token_sha256',RETURN)]:
        monkeypatch.setattr(settings, name, SecretStr(hashlib.sha256(value.encode()).hexdigest()))
    app = FastAPI()
    app.include_router(resale_catalog_export.router, prefix='/api')
    app.include_router(resale_export.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def request(client, entry, token):
    method, path, body, _ = entry
    return client.request(method, '/api/integrations/resale'+path, json=body,
                          headers={'Authorization':'Bearer '+token})


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('credential', ['other_service', 'employee'])
def test_wrong_identity_never_executes_business_sql(db, client, entry, credential):
    token = (RETURN if entry[3] != 'resale_return_feed.read' else CATALOG)
    if credential == 'employee': token = _make_token('admin', 'synthetic-admin', None, authn='sys_user', token_version=0)[0]
    sql = []
    def capture(conn, cursor, statement, parameters, context, executemany): sql.append(statement)
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try: result = request(client, entry, token)
    finally: event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert result.status_code == 401 and not sql
    assert result.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('entry', ENTRIES)
def test_valid_service_can_pause_each_action_before_sql(db, client, monkeypatch, entry):
    service = 'resale_export' if entry[3] == 'resale_return_feed.read' else 'resale_catalog'
    monkeypatch.setattr(get_settings(), service+'_allowed_actions', [])
    sql = []
    def capture(conn, cursor, statement, parameters, context, executemany): sql.append(statement)
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try: result = request(client, entry, RETURN if service == 'resale_export' else CATALOG)
    finally: event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert result.status_code == 403 and not sql


@pytest.mark.parametrize('values', [['unknown.read'], ['resale_catalog.list','resale_catalog.list'], ['resale_return_feed.read']])
def test_invalid_or_cross_service_configuration_is_rejected_and_runtime_fails_closed(db, client, monkeypatch, values):
    with pytest.raises(ValidationError): Settings(resale_catalog_allowed_actions=values)
    monkeypatch.setattr(get_settings(), 'resale_catalog_allowed_actions', values)
    result = request(client, ENTRIES[2], CATALOG)
    assert result.status_code == 503


def test_new_action_not_in_fixed_server_contract():
    with pytest.raises(ValueError): require_service('resale_catalog', 'resale_catalog', 'future_export')
    with pytest.raises(ValueError): require_service('resale_catalog', 'resale_return_feed', 'read')


@pytest.mark.parametrize('side', ['sales', 'purchases'])
def test_price_evidence_never_materializes_unrelated_orm_columns(db, client, side):
    sql = []
    def capture(conn, cursor, statement, parameters, context, executemany): sql.append(statement.lower())
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        result = client.post('/api/integrations/resale/parts/price-evidence',
            headers={'Authorization':'Bearer '+CATALOG}, json={'partIds':[999999], 'side':side})
    finally: event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert result.status_code == 200, result.text
    fact = [s for s in sql if 'from f_sales_line' in s or 'from f_purchase_line' in s]
    assert len(fact) == 1
    for forbidden in ('customer_id', 'supplier_id', 'salesperson', 'profit', 'cost_amount', 'purchase_person'):
        assert forbidden not in fact[0]
    assert result.json() == {'schemaVersion':1,'items':[],'hasMore':False,'nextCursor':None}


def test_price_reference_pause_does_not_pause_catalog_listing(db, client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'resale_catalog_allowed_actions', ['resale_catalog.list'])
    assert request(client, ENTRIES[2], CATALOG).status_code == 200
    assert request(client, ENTRIES[5], CATALOG).status_code == 403


@pytest.mark.parametrize('side', ['sales', 'purchases'])
def test_price_evidence_keeps_existing_fields_values_and_keyset_pages(db, client, side):
    from datetime import date
    from decimal import Decimal
    from app.models.dimensions import DimPart
    from app.models.system import SysImportBatch
    from app.models.sales import FSalesOrder, FSalesLine
    from app.models.purchase import FPurchaseOrder, FPurchaseLine
    batch = SysImportBatch(filename='synthetic', file_type=side, file_hash='synthetic-machine', status='success')
    part = DimPart(pn_std='S4-MACHINE-'+side)
    db.add_all([batch, part]); db.flush()
    order_type, line_type = (FSalesOrder, FSalesLine) if side == 'sales' else (FPurchaseOrder, FPurchaseLine)
    extra = ({'tax_rate':Decimal('0.13'), 'business_type':'备件销售', 'salesperson':'PRIVATE-EMPLOYEE'}
             if side == 'sales' else {'is_tax_inclusive':False, 'source_type':'维保需求', 'purchaser':'PRIVATE-EMPLOYEE'})
    order = order_type(raw_order_id='s4-machine-'+side, order_no='PRIVATE-NUMBER',
        order_date=date(2026,1,2), data_status='已生效', import_batch_id=batch.id, **extra)
    db.add(order); db.flush()
    lines = [line_type(raw_line_id=f's4-{side}-{i}', order_id=order.id, part_id=part.id,
        qty=Decimal('2'), unit_price=Decimal('123.45'), import_batch_id=batch.id,
        description='PRIVATE-DESCRIPTION', **({'counts_revenue':False} if side == 'sales' else {})) for i in range(2)]
    db.add_all(lines); db.flush()
    body = {'partIds':[part.id], 'side':side, 'limit':1}
    first = client.post('/api/integrations/resale/parts/price-evidence',
        headers={'Authorization':'Bearer '+CATALOG}, json=body)
    assert first.status_code == 200, first.text
    value = first.json(); assert value['hasMore'] and value['nextCursor'] == lines[0].id
    expected = {'partId':part.id, 'sourceLineId':lines[0].raw_line_id,
        'sourceOrderId':order.raw_order_id, 'orderDate':'2026-01-02','dataStatus':'已生效',
        'unitPrice':'123.45','qty':'2.000','itemKind':'unknown','channel':'unknown'}
    expected.update({'side':'sale','unitPriceBasis':'inc_tax_assumed','taxRate':'0.1300',
                     'businessType':'备件销售','countsRevenue':False} if side == 'sales' else
                    {'side':'purchase','isTaxInclusive':False,'sourceType':'维保需求'})
    assert value['items'] == [expected]
    second = client.post('/api/integrations/resale/parts/price-evidence',
        headers={'Authorization':'Bearer '+CATALOG}, json={**body,'after':value['nextCursor']}).json()
    assert second['hasMore'] is False and second['nextCursor'] is None
    assert second['items'][0]['sourceLineId'] == lines[1].raw_line_id
    assert 'PRIVATE' not in str(value)+str(second)
