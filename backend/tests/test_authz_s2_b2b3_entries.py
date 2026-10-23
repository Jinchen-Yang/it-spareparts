"""Exact B2/B3 HTTP entry manifest: admin role is never an implicit Grant."""
import pytest

from app.authz import runtime
from app.authz.catalog import RESOURCE_DEFINITIONS
from app.config import get_settings
from app.main import app
from app.services import master_edit
from tests.authz_pdp_helpers import cerbos_url
from tests.test_authz_s2_pool_access import client_with
from tests.test_data_quality_foundation import _seed_purchase, _create_issue

# method, registered path, representative request path, body, exact authority.
ENTRIES = [
    ('GET','/dashboard/kpi','/dashboard/kpi',None,'company_summary.read'),
    ('GET','/dashboard/trend','/dashboard/trend',None,'company_summary.read'),
    ('GET','/dashboard/part-ranking','/dashboard/part-ranking',None,'company_summary.read'),
    ('GET','/dashboard/sales','/dashboard/sales',None,'company_sales_orders.read'),
    ('GET','/dashboard/purchase-orders','/dashboard/purchase-orders',None,'company_purchase_orders.read'),
    ('GET','/dashboard/pools','/dashboard/pools',None,'company_pool_analysis.read_summary|read_sales_transactions|read_customer_cross_brand'),
    ('GET','/dashboard/pool/{group_id}','/dashboard/pool/1',None,'company_pool_analysis.read_summary|read_sales_transactions|read_customer_cross_brand'),
    ('GET','/dashboard/price-discipline-summary','/dashboard/price-discipline-summary',None,'price_discipline.read'),
    ('GET','/dashboard/orders/{side}/{order_id}','/dashboard/orders/purchase/1',None,'company_pool_order_facts.read'),
    ('GET','/pool-analysis/pools','/pool-analysis/pools',None,'pool_price_analysis.read|pool_order_facts.read'),
    ('GET','/pool-analysis/pools/{group_id}','/pool-analysis/pools/1',None,'pool_price_analysis.read|pool_order_facts.read'),
    ('GET','/pool-analysis/pools/{group_id}/price-map','/pool-analysis/pools/1/price-map',None,'pool_price_analysis.read'),
    ('GET','/pool-analysis/orders/{side}/{order_id}','/pool-analysis/orders/sales/1',None,'pool_order_facts.read'),
    ('GET','/pools','/pools',None,'pool_catalog.read|pool_price_policy.read'),
    ('GET','/pools/{group_id}','/pools/1',None,'pool_catalog.read|pool_price_policy.read'),
    ('POST','/pools','/pools',{'name':'deny','member_part_ids':'PART_IDS'},'pool_catalog.create'),
    ('PATCH','/pools/{group_id}','/pools/1',{'version':1,'name':'deny'},'pool_catalog.edit'),
    ('PATCH','/pools/{group_id}/members','/pools/1/members',{'version':1,'add_part_ids':[999]},'pool_catalog.edit_members'),
    ('PUT','/pools/{group_id}/price-policy','/pools/1/price-policy',{'version':1,'purchase_value':'100'},'pool_price_policy.set'),
    ('POST','/pools/{group_id}/archive','/pools/1/archive',{'version':1},'pool_catalog.archive'),
    ('POST','/pools/{group_id}/restore','/pools/1/restore',{'version':1},'pool_catalog.restore'),
    ('GET','/parts/search','/parts/search?q=ENTRY-A',None,'part_lookup.read'),
    ('GET','/parts/categories','/parts/categories',None,'part_lookup.read'),
    ('GET','/parts/overview','/parts/overview?pn_std=ENTRY-A',None,'part_lookup.any_block|part_substitute.read'),
    ('GET','/parts/purchases','/parts/purchases?pn_std=ENTRY-A',None,'part_lookup.purchase_records_read'),
    ('GET','/parts/sales','/parts/sales?pn_std=ENTRY-A',None,'part_lookup.sales_records_read'),
    ('GET','/parts/{part_id}/pool-reference','/parts/1/pool-reference',None,'pool_price_analysis.read'),
    ('POST','/parts/pool-references','/parts/pool-references',{'part_ids':[1]},'pool_price_analysis.read'),
    ('GET','/parts/master/search','/parts/master/search',None,'part_master.read'),
    ('GET','/parts/master/check','/parts/master/check?pn_std=ENTRY-A',None,'part_master.read'),
    ('GET','/parts/master/categories','/parts/master/categories',None,'part_master.read'),
    ('GET','/parts/master/suggest','/parts/master/suggest',None,'part_master.read'),
    ('GET','/parts/master/{part_id}/edit-state','/parts/master/1/edit-state',None,'part_master.read'),
    ('GET','/parts/master/{part_id}/edit-history','/parts/master/1/edit-history',None,'part_master.read'),
    ('GET','/parts/master/batch-preview','/parts/master/batch-preview',None,'part_master.normalize_preview'),
    ('POST','/parts/master/batch-apply','/parts/master/batch-apply',{'part_ids':[]},'part_master.normalize_apply'),
    ('POST','/parts/master','/parts/master',{'pn_std':'ENTRY-NEW'},'part_master.create'),
    ('PATCH','/parts/master','/parts/master','EDIT_BODY','part_master.edit'),
    ('GET','/substitutes','/substitutes?pn_std=ENTRY-A',None,'part_substitute.read'),
    ('POST','/substitutes','/substitutes',{'pn_a':'ENTRY-A','pn_b':'ENTRY-B'},'part_substitute.manage'),
    ('DELETE','/substitutes','/substitutes?pn_a=ENTRY-A&pn_b=ENTRY-B',None,'part_substitute.manage'),
    ('GET','/governance/summary','/governance/summary',None,'governance.read'),
    ('GET','/governance/metrics','/governance/metrics',None,'governance.read'),
    ('GET','/governance/parts','/governance/parts',None,'governance.read'),
    ('GET','/governance/candidates','/governance/candidates',None,'governance.read'),
    ('GET','/governance/issues','/governance/issues',None,'governance.read'),
    ('POST','/governance/refresh','/governance/refresh',None,'governance.refresh'),
    ('POST','/governance/classify-backfill','/governance/classify-backfill',None,'governance.classify'),
    ('PUT','/governance/exclude','/governance/exclude',{'pn_std':'ENTRY-A'},'governance.exclude'),
    ('POST','/governance/candidates/{candidate_id}/review','/governance/candidates/1/review',{'action':'reject'},'governance.review+identity_by_action'),
    ('POST','/governance/issues/{issue_id}/resolve','/governance/issues/1/resolve',{},'governance.review'),
    ('GET','/governance/merges','/governance/merges',None,'part_identity.read'),
    ('GET','/governance/aliases','/governance/aliases',None,'part_identity.read'),
    ('POST','/governance/parts/merge','/governance/parts/merge',{'source_pn':'ENTRY-A','target_pn':'ENTRY-B','recompute':False},'part_identity.merge'),
    ('POST','/governance/parts/unmerge','/governance/parts/unmerge',{'merge_log_id':1,'recompute':False},'part_identity.unmerge'),
    ('POST','/governance/parts/rename','/governance/parts/rename',{'pn_std':'ENTRY-A','new_pn_std':'ENTRY-NEW'},'part_identity.rename'),
    ('POST','/governance/parts/confirm-batch','/governance/parts/confirm-batch',{'pn_stds':['ENTRY-A']},'part_identity.confirm'),
    ('POST','/governance/aliases/{alias_id}/review','/governance/aliases/1/review',{'action':'approve'},'part_identity.review_alias'),
    ('GET','/data-quality/issues','/data-quality/issues',None,'data_quality.read_actual_sides'),
    ('GET','/data-quality/issues/{issue_id}','/data-quality/issues/ISSUE_ID',None,'data_quality.read_actual_side'),
    ('POST','/data-quality/issues/{issue_id}/decision','/data-quality/issues/ISSUE_ID/decision',{'decision':'confirmed_valid','version':1,'note':'deny'},'data_quality.review+read_actual_side'),
    ('POST','/data-quality/issues/{issue_id}/reopen','/data-quality/issues/ISSUE_ID/reopen',{'version':1,'note':'deny'},'data_quality.review+read_actual_side'),
    ('GET','/data-quality/calibration/purchase-price','/data-quality/calibration/purchase-price',None,'data_quality_calibration.preview'),
]


@pytest.fixture
def entry_context(db,monkeypatch,cerbos_url):
    settings=get_settings()
    monkeypatch.setattr(settings,'authorization_mode','unified_modules')
    monkeypatch.setattr(settings,'authz_sales_analysis_enabled',False)
    monkeypatch.setattr(settings,'authz_enabled_resources',list(RESOURCE_DEFINITIONS))
    monkeypatch.setattr(settings,'cerbos_url',cerbos_url)
    runtime.activate_configured_resources(db.get_bind())
    a=master_edit.create_part(db,pn_std='ENTRY-A',force=True)
    b=master_edit.create_part(db,pn_std='ENTRY-B',force=True)
    baseline=master_edit.get_part_edit_state(db,a['id'])
    _,_,_,line=_seed_purchase(db)
    issue=_create_issue(db,line)
    client,user=client_with(db,{},role='admin')
    return client,a,b,baseline,issue


@pytest.mark.parametrize('method,registered,path,body,authority',ENTRIES,ids=[f'{m} {p}' for m,p,*_ in ENTRIES])
def test_every_entry_denies_role_only_admin(entry_context,method,registered,path,body,authority):
    client,a,b,baseline,issue=entry_context
    if body=='EDIT_BODY':
        body={'part_id':a['id'],'pn_std':'ENTRY-A','base_snapshot':baseline,'brand':'deny'}
    elif isinstance(body,dict):
        body=dict(body)
        if body.get('member_part_ids')=='PART_IDS':body['member_part_ids']=[a['id'],b['id']]
    response=client.request(method,'/api'+path.replace('ISSUE_ID',str(issue['id'])),json=body)
    assert response.status_code==403,(authority,response.status_code,response.text)


def test_exact_route_inventory_requires_explicit_contract_entry():
    modules={f'app.api.{name}' for name in ('dashboard','pool_analysis','pools','parts','substitutes','governance','data_quality','data_quality_calibration')}
    actual={(method,route.path) for route in app.routes if getattr(getattr(route,'endpoint',None),'__module__',None) in modules
            for method in getattr(route,'methods',())}
    expected={(method,'/api'+path) for method,path,*_ in ENTRIES}
    expected.add(('POST','/api/dashboard/pool/rebuild'))  # Retired 410, intentionally no executable Grant.
    assert actual==expected
