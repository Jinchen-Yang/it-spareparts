"""Actual side and mutation intent gates across governance resources."""
import pytest
from sqlalchemy import select

from app.authz import runtime
from app.config import get_settings
from app.models.dimensions import DimPart
from app.models.master_data import ProductMatchCandidate
from app.models.system import SysAuditLog
from tests.authz_pdp_helpers import cerbos_url
from tests.test_authz_s2_pool_access import client_with, captured_sql
from tests.test_data_quality_foundation import _seed_purchase, _create_issue


@pytest.fixture(autouse=True)
def authority(db, monkeypatch, cerbos_url):
    settings = get_settings()
    monkeypatch.setattr(settings, 'authorization_mode', 'unified_modules')
    monkeypatch.setattr(settings, 'authz_sales_analysis_enabled', False)
    monkeypatch.setattr(settings, 'authz_enabled_resources', ['governance','part_identity','data_quality','data_quality_calibration'])
    monkeypatch.setattr(settings, 'cerbos_url', cerbos_url)
    runtime.activate_configured_resources(db.get_bind())


@pytest.fixture
def candidate(db):
    a,b = DimPart(pn_std='SOURCE',needs_review=True),DimPart(pn_std='TARGET')
    db.add_all([a,b]);db.flush()
    c = ProductMatchCandidate(source_part_id=a.id,candidate_part_id=b.id,raw_text='SOURCE',normalized_text='TARGET',status='pending')
    db.add(c);db.commit()
    return a,b,c


def test_governance_read_never_implies_identity_or_mutations(db, candidate):
    client,_ = client_with(db, {'governance': (['read'], [])}, role='admin')
    assert client.get('/api/governance/summary').status_code == 200
    assert client.get('/api/governance/candidates').status_code == 200
    assert client.get('/api/governance/merges').status_code == 403
    assert client.put('/api/governance/exclude',json={'pn_std':'SOURCE'}).status_code == 403
    assert db.get(DimPart,candidate[0].id).is_excluded is False


@pytest.mark.parametrize('action', ['merge','independent'])
def test_review_requires_exact_identity_branch(db,candidate,action):
    client,_ = client_with(db, {'governance': (['review'], [])})
    result = client.post(f'/api/governance/candidates/{candidate[2].id}/review',json={'action':action})
    assert result.status_code == 403,result.text
    db.expire_all()
    assert db.get(ProductMatchCandidate,candidate[2].id).status == 'pending'
    assert db.get(DimPart,candidate[0].id).status == 'active'
    assert not db.scalars(select(SysAuditLog).where(SysAuditLog.entity_type=='match_candidate')).all()


def test_review_reject_does_not_require_identity_action(db,candidate):
    client,_ = client_with(db, {'governance': (['review'], [])})
    result = client.post(f'/api/governance/candidates/{candidate[2].id}/review',json={'action':'reject'})
    assert result.status_code == 200,result.text
    db.expire_all()
    assert db.get(ProductMatchCandidate,candidate[2].id).status == 'rejected'


def test_candidate_merge_is_one_authorized_transaction(db,candidate):
    client,_ = client_with(db, {'governance': (['review'], []),'part_identity':(['merge'],[])})
    result = client.post(f'/api/governance/candidates/{candidate[2].id}/review',json={'action':'merge'})
    assert result.status_code == 200,result.text
    db.expire_all()
    assert db.get(DimPart,candidate[0].id).merged_into_id == candidate[1].id
    assert db.get(ProductMatchCandidate,candidate[2].id).status == 'accepted'


def test_quality_other_side_does_not_read_or_review_purchase(db):
    _,_,_,line = _seed_purchase(db)
    issue = _create_issue(db,line)
    client,_=client_with(db,{'data_quality':(['read_sales_issues','review'],['data_purchase_cost'])})
    listed=client.get('/api/data-quality/issues')
    assert listed.status_code == 200 and listed.json()['total']==0,listed.text
    with captured_sql(db) as statements:
        assert client.get(f"/api/data-quality/issues/{issue['id']}").status_code==404
    detail_reads=[sql for sql in statements if 'from fact_data_quality_issue' in sql]
    assert detail_reads and all('fact_data_quality_issue.side in' in sql for sql in detail_reads)
    assert not any('from f_purchase_line' in sql for sql in statements)
    denied=client.post(f"/api/data-quality/issues/{issue['id']}/decision",json={
        'decision':'confirmed_valid','version':1,'note':'must not apply'})
    assert denied.status_code==403,denied.text
    from app.models.data_quality import FactDataQualityIssue
    db.expire_all()
    assert db.get(FactDataQualityIssue,issue['id']).status=='open'


def test_quality_review_and_detail_share_side_and_field_contract(db):
    _,_,_,line = _seed_purchase(db)
    issue = _create_issue(db,line)
    client,_=client_with(db,{'data_quality':(['read_purchase_issues','review'],['data_purchase_cost'])})
    response=client.post(f"/api/data-quality/issues/{issue['id']}/decision",json={
        'decision':'confirmed_valid','version':1,'note':'verified'})
    assert response.status_code==200,response.text
    assert response.json()['version']==2 and response.json()['review_note']=='verified'
    assert client.get('/api/data-quality/issues?side=sales').status_code==403


def test_calibration_no_action_denied_before_fact_sql(db):
    client,_=client_with(db,{'data_quality':(['read_purchase_issues'],['data_purchase_cost'])},role='admin')
    with captured_sql(db) as statements:
        response=client.get('/api/data-quality/calibration/purchase-price')
    assert response.status_code==403,response.text
    assert not any('f_purchase_line' in s for s in statements)


def test_alias_parent_is_resolved_after_waiting_for_master_lock(db,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from time import monotonic,sleep
    from sqlalchemy import event,text
    from app.db import SessionLocal
    from app.models.dimensions import PartAlias
    from app.services import master_data
    a,b=DimPart(pn_std='ALIAS-OLD'),DimPart(pn_std='ALIAS-NEW')
    db.add_all([a,b]);db.flush()
    alias=PartAlias(pn_raw='raw alias',pn_std=a.pn_std,part_id=a.id,source='manual',status='pending')
    db.add(alias);db.commit();alias_id=alias.id;new_id=b.id
    client,_=client_with(db,{'part_identity':(['review_alias'],[])})
    events=[]
    def record(conn,cursor,statement,parameters,context,many):
        events.append((statement.lower(),parameters))
    with SessionLocal() as writer:
        master_data.acquire_master_write_lock(writer)
        pid=writer.scalar(text('select pg_backend_pid()'))
        current=writer.get(PartAlias,alias_id);current.part_id=new_id;current.pn_std='ALIAS-NEW';writer.flush()
        event.listen(db.get_bind(),'before_cursor_execute',record)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                request=executor.submit(client.post,f'/api/governance/aliases/{alias_id}/review',json={'action':'approve'})
                try:
                    deadline=monotonic()+8;blocked=False
                    while monotonic()<deadline:
                        with db.get_bind().connect() as probe:
                            blocked=bool(probe.scalar(text('select exists(select 1 from pg_stat_activity where :pid = any(pg_blocking_pids(pid)))'),{'pid':pid}))
                        if blocked:break
                        sleep(.02)
                    assert blocked,'review did not wait on the actual domain lock'
                    assert not any('from part_alias' in sql for sql,_ in events),'alias read occurred before the master lock'
                finally:
                    writer.commit()
                response=request.result(timeout=10)
        finally:
            event.remove(db.get_bind(),'before_cursor_execute',record)
    assert response.status_code==200,response.text
    parent_reads=[params for sql,params in events if 'from dim_part' in sql and 'for update' in sql]
    assert parent_reads and any(new_id in params.values() for params in parent_reads),parent_reads
    db.expire_all();assert db.get(PartAlias,alias_id).part_id==new_id
