"""Cross-author checks of exact branch authority in the real mounted app."""
from fastapi.testclient import TestClient
from app.authz import runtime
from app.config import get_settings
from app.db import get_db
from tests.test_authz_s3_checklist import setup,cerbos_url  # noqa:F401


def test_actual_ai_document_branch_does_not_require_unrelated_ledger_cutover(db,setup,monkeypatch):
    from app.main import app
    from sqlalchemy import func,select
    from app.models.maintenance_ai_fallback import MaintenanceAiMappingProposal
    from app.services import maintenance_ai_fallback as business
    def forbidden_provider(*args,**kwargs):raise AssertionError('unselected legacy branch must not call LLM')
    monkeypatch.setattr(business,'call_llm_for_mapping',forbidden_provider)
    from tests.test_authz_s3_ai_fallback import grant
    from tests.test_maintenance_ai_fallback import _variant_return_workbook
    uid,_,_,_,headers=setup
    settings=get_settings()
    monkeypatch.setattr(settings,'maintenance_beta_enabled',True)
    monkeypatch.setattr(settings,'authz_enabled_resources',['maintenance_ai_document'])
    runtime.activate_configured_resources(db.get_bind())
    grant(db,uid,'maintenance_ai_document',['propose'])
    assert runtime.runtime_states(db)['maintenance_ai_ledger'].runtime_state=='legacy'
    previous=dict(app.dependency_overrides)
    app.dependency_overrides[get_db]=lambda:db
    try:
        client=TestClient(app)
        response=client.post('/api/maintenance/ai-fallback/propose',
            params={'doc_type':'return_order','view':'authorization'},headers=headers,
            files={'file':('variant.xlsx',_variant_return_workbook())})
        ledger=client.post('/api/maintenance/ai-fallback/propose',
            params={'doc_type':'ledger'},headers=headers,
            files={'file':('variant.xlsx',_variant_return_workbook())})
    finally:
        app.dependency_overrides.clear();app.dependency_overrides.update(previous)
    assert response.status_code==200,response.text
    assert set(response.json())=={'locator','authorization_context'}
    assert ledger.status_code==503,ledger.text
    assert ledger.json()['detail']['code']=='AUTHZ_RESOURCE_UNAVAILABLE'
    assert db.scalar(select(func.count()).select_from(MaintenanceAiMappingProposal))==0


from tests.test_authz_s3_document_imports import imports as staged_imports  # noqa:F401,E402


def test_actual_import_and_branch_with_only_one_cutover_keeps_old_outer_gate(db,staged_imports,monkeypatch):
    from app.main import app
    uid,grant,original,data,preview,read,Model,_,_=staged_imports
    monkeypatch.setattr(get_settings(),'maintenance_beta_enabled',True)
    created=original.post(preview,headers={'Idempotency-Key':'and-partial-bootstrap'},
        files={'file':('synthetic.xlsx',data)})
    assert created.status_code==200,created.text
    batch_id=created.json()['batch_id']
    grant.actions=['apply'];grant.version+=1;db.commit()
    assert runtime.runtime_states(db)[grant.resource+'_targets'].runtime_state=='legacy'
    previous=dict(app.dependency_overrides);app.dependency_overrides[get_db]=lambda:db
    try:
        client=TestClient(app);client.headers.update(original.headers)
        denied=client.post(read+batch_id+'/apply')
    finally:
        app.dependency_overrides.clear();app.dependency_overrides.update(previous)
    assert denied.status_code==403,denied.text
    assert '未获得维保管理页面权限' in denied.text
    assert db.get(Model,batch_id,populate_existing=True).status=='pending'
