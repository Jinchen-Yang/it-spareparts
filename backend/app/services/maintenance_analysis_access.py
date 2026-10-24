"""Scoped analysis: SQL plans precede every source/issue/receipt aggregation."""
from sqlalchemy.orm import Session
from app.authz import service
from app.authz.errors import authz_error
from app.services import maintenance_authorization as authorization
from app.services import maintenance_analytics as analysis
from app.services import maintenance_analytics_explorer as explorer

_ACTIONS={'pn-ranking':'read_pn_ranking','spend-trend':'read_spend_trend',
          'filter-options':'read_filter_options','explorer':'read_explorer'}


def _hide_cost_diagnostics(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if key in {'missing_lines','known_count','unknown_count','coverage_pct'}:value[key]=None
            else:_hide_cost_diagnostics(item)
    elif isinstance(value,list):
        for item in value:_hide_cost_diagnostics(item)


def read(db,identity,*,endpoint,params,view=None,precondition=None):
    if endpoint not in _ACTIONS:raise ValueError('unknown analysis endpoint')
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知的授权视图')
    params=dict(params)
    if 'range_' in params:params['range']=params.pop('range_')
    entry='GET /api/maintenance/analytics/'+endpoint
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=entry,params=params) as (snapshot,proof,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        access=accesses.require(ref,'maintenance_analysis',_ACTIONS[endpoint])
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        cost='data_purchase_cost' in access.field_permissions
        customer='data_customer' in access.field_permissions
        if not customer and (params.get('customer') or params.get('dimension')=='customer'):
            raise authz_error(403,'AUTHZ_FIELD_REQUIRED','客户筛选或维度需要本模块客户字段权限')
        if not cost and (params.get('cost_source','all')!='all' or
                params.get('sort') in {*analysis.COST_SORTS,'missing_lines'} or
                params.get('metric') in {'cost','missing'}):
            raise authz_error(403,'AUTHZ_FIELD_REQUIRED','成本筛选、排序与缺价统计需要本模块成本字段权限')
        allowed=authorization.project_query(access,include_unassigned_when_all=True)
        if endpoint=='filter-options':
            result=analysis.filter_options(snapshot,authorized_project_query=allowed)
        else:
            try:
                project_ids=analysis.parse_csv_items(params.get('project'),max_items=50,max_len=64,field='project')
                warehouses=analysis.parse_csv_items(params.get('warehouse'),max_items=10,max_len=64,field='warehouse',trim=False)
                demand_codes=analysis.parse_csv_items(None if params.get('demand_type','all')=='all' else params['demand_type'],max_items=2,max_len=16,field='demand_type')
                cost_sources=analysis.parse_csv_items(None if params.get('cost_source','all')=='all' else params['cost_source'],max_items=4,max_len=16,field='cost_source')
                args={key:params[key] for key in ('date_from','date_to','q','business_type','order_no') if key in params}
                args.update(range_=params.get('range','ytd'),project_ids=project_ids,
                    customer=params.get('customer'),salesperson=params.get('sp'),warehouses=warehouses,
                    demand_types=[analysis.DEMAND_TYPE_LITERALS[code] for code in demand_codes] if demand_codes else None,
                    cost_sources=cost_sources,can_cost=cost,strict_field_projection=True)
                if endpoint=='explorer':
                    args.update({key:params[key] for key in ('dimension','metric','focus','page','page_size','top_n',
                        'focus_page','focus_page_size','include_pn_overview') if key in params})
                    result=explorer.explorer(snapshot,**args,can_customer=customer,allowed_project_ids=allowed)
                    navigation_rows=[*result['rows'],*result['chart_rows'],*result['focus']['rows'],
                        *result['matrix']['rows'],*result['matrix']['columns']]
                    if result['focus']['row'] is not None:navigation_rows.append(result['focus']['row'])
                    project_ids={row['project_id'] for row in navigation_rows if row.get('project_id')}
                    capabilities=authorization.project_read_capabilities(snapshot,identity,project_ids,read_snapshot=proof)
                    for row in navigation_rows:row['can_open_project']=capabilities.get(row.get('project_id'),False)
                elif endpoint=='pn-ranking':
                    args.update({key:params[key] for key in ('sort','page','page_size') if key in params})
                    result=analysis.pn_ranking(snapshot,**args,authorized_project_query=allowed)
                else:
                    result=analysis.spend_trend(snapshot,**args,granularity=params.get('granularity','month'),
                                               authorized_project_query=allowed)
            except (analysis.AnalyticsValidationError,KeyError) as exc:
                raise authz_error(422,'DOMAIN_INVALID',str(exc)) from exc
        if not cost:_hide_cost_diagnostics(result)
        return {**result,'authorization_context':accesses.wire(ref)}
