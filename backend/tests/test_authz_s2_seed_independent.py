"""Independent business interpretation of the pre-cutover B2/B3 permissions.

These expected rules come from legacy services, not the migration's predicate AST.
Display actions have their own frontend contract and are intentionally excluded.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest
from app import permissions

path=Path(__file__).parents[1]/'alembic/versions/c9d4e8f2a6b1_business_authorization.py'
spec=importlib.util.spec_from_file_location('independent_s2_seed',path)
migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)

FIELDS={
 'company_summary':['data_purchase_cost','data_profit'],
 'company_sales_orders':['data_customer','data_purchase_cost','data_profit','data_reconciliation'],
 'company_purchase_orders':['data_supplier','data_purchase_cost','data_pool_price_governance'],
 'company_pool_analysis':['data_supplier','data_customer','data_purchase_cost','data_profit','data_pool_price_governance'],
 'price_discipline':['data_pool_price_governance'],
 'company_pool_order_facts':['data_supplier','data_customer','data_pool_price_governance'],
 'pool_price_analysis':['data_supplier','data_customer','data_pool_price_governance'],
 'pool_order_facts':['data_supplier','data_customer','data_pool_price_governance'],
 'pool_catalog':[], 'pool_price_policy':['data_pool_price_governance'],
 'part_lookup':['data_supplier','data_customer','data_purchase_cost','data_profit','data_pool_price_governance'],
 'part_master':['data_purchase_cost','data_profit'], 'part_substitute':[],
 'governance':['data_supplier','data_customer','data_purchase_cost','data_profit'],
 'part_identity':['data_supplier','data_customer','data_purchase_cost','data_profit'],
 'data_quality':['data_supplier','data_customer','data_purchase_cost','data_profit'],
 'data_quality_calibration':['data_supplier','data_customer','data_purchase_cost','data_profit'],
}


def expected(source):
 p=permissions.runtime_safe(permissions.effective_for_user(SimpleNamespace(**source)))
 boss=p['page_boss_board']; whole=not p['own_customers_only']; gov=p['page_governance']
 actions={
  'company_summary':['read'] if boss else [],
  'company_sales_orders':['read'] if boss and whole else [],
  'company_purchase_orders':['read'] if boss else [],
  'company_pool_analysis':(['read_summary'] if boss else [])+(['read_sales_transactions'] if boss and whole else [])+(['read_customer_cross_brand'] if boss and whole and p['data_customer'] else []),
  'price_discipline':['read'] if boss else [],
  'company_pool_order_facts':['read'] if boss else [],
  'pool_price_analysis':['read'] if p['page_pool_analysis'] else [],
  'pool_order_facts':['read'] if p['page_pool_analysis'] else [],
  'pool_catalog':['read']+(['create','edit','edit_members','archive','restore'] if p['action_pool_manage'] else []),
  'pool_price_policy':['read']+(['set'] if p['action_pool_set_policy'] and p['data_pool_price_governance'] else []),
  'part_lookup':['read','purchase_records_read','inventory_read','financial_summary_read','market_prices_read']+(['sales_records_read'] if whole else []),
  'part_master':['read','create','edit','normalize_preview','normalize_apply'] if p['page_master_data'] else [],
  'part_substitute':['read']+(['manage'] if source['role'] in ('admin','purchaser') else []),
  'governance':['read','refresh','classify','exclude','review'] if gov else [],
  'part_identity':['read','confirm','merge','rename','review_alias','unmerge'] if gov else [],
  'data_quality':(['read_purchase_issues'] if gov else [])+(['read_sales_issues'] if gov and whole else [])+(['review'] if gov and p['action_data_quality_review'] and p['data_purchase_cost'] else []),
  'data_quality_calibration':['preview'] if gov and p['data_purchase_cost'] else [],
 }
 return {r:{'actions':sorted(a),'scope':'all','field_permissions':sorted(f for f in FIELDS[r] if p[f]) if a else []} for r,a in actions.items()}


VARIANTS=[
 (None,{},{}),
 ({},{},{}),
 ({'page_boss_board':True,'own_customers_only':True,'data_customer':True},{},{}),
 ({'page_governance':True,'action_data_quality_review':True,'data_purchase_cost':False},{},{}),
 ({'page_governance':True,'action_data_quality_review':True,'data_purchase_cost':True,'data_profit':True},{},{}),
 ({'page_pool_analysis':True,'data_purchase_cost':True,'data_pool_price_governance':False},{},{}),
 ({'action_pool_set_policy':True,'data_pool_price_governance':True},{},{}),
 (None,{'page_master_data':True,'page_governance':True,'own_customers_only':True},{}),
 (dict(permissions._full()),{}, {'data_purchase_cost':False,'data_pool_price_governance':False,'page_master_data':False,'own_customers_only':True}),
]

@pytest.mark.parametrize('role',['admin','boss','sales','purchaser','readonly','maintenance_manager','unknown'])
@pytest.mark.parametrize('snapshot,legacy,overrides',VARIANTS,ids=[f'legacy-vector-{n}' for n in range(9)])
def test_seventeen_resource_seed_matches_independent_business_rules(role,snapshot,legacy,overrides):
 source={'role':role,'template_perms':snapshot,'permissions':legacy,'perm_overrides':overrides,'authorization_legacy_seed_eligible':True,'is_active':False}
 actual=migration.seed(source,{})
 actual={r:{**actual[r],'actions':sorted(a for a in actual[r]['actions'] if not a.startswith('show_'))} for r in FIELDS}
 assert actual==expected(source)
