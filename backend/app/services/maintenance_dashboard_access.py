"""Dashboard read use cases with one query plan and resource-local finance fields."""
from decimal import Decimal,InvalidOperation
from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.authz import service
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.models.maintenance_project import MaintenanceProject
from app.services import maintenance_authorization as authorization
from app.services import maintenance_dashboard as dash
from app.services import maintenance_dashboard_consumption as consumption
from app.services import maintenance_dashboard_valuation_policy as policy

_ACTIONS={'options':'read_options','summary':'read_summary','projects':'read_projects','parts':'read_parts',
    'valuation':'read_valuation','receipts':'read_receipts','quantity-checks':'read_quantity_checks',
    'orders':'read_orders','consumption':'read_consumption','consumption/issues':'read_consumption_issues'}
_EXPORT_KINDS=('projects','parts','receipts','quantity_checks','orders','consumption_daily','consumption_issues')


def _ratio(value):
    if value is None or not str(value).strip():return policy.DEFAULT_RETENTION_RATIO
    try:
        result=Decimal(str(value))
        if not result.is_finite() or result<0 or result>1:raise ValueError()
        return result
    except (InvalidOperation,ValueError):raise authz_error(422,'DOMAIN_INVALID','价值保留比例必须在0和1之间') from None


def _selection_scope(access,params):
    query=authorization.project_query(access,include_unassigned_when_all=True)
    requested=params.get('project')
    if requested:
        ids=[p.strip() for p in requested.split(',')]
        if len(ids)>50 or any(not item or len(item)>64 for item in ids):
            raise authz_error(422,'DOMAIN_INVALID','项目筛选参数无效')
        query=select(MaintenanceProject.project_id).where(
            MaintenanceProject.project_id.in_(ids),*([MaintenanceProject.project_id.in_(query)] if query is not None else []))
    business=params.get('business_type','all')
    if business!='all':
        query=select(MaintenanceProject.project_id).where(MaintenanceProject.business_type==business,
            *([MaintenanceProject.project_id.in_(query)] if query is not None else []))
    return query


def _range_payload(filters):
    return {'start':filters.start.isoformat() if filters.start else None,'end':filters.end.isoformat()}


def _branch(entry,action,params):
    actions={action}
    if params.get('value_retention_ratio') is not None:actions.add('adjust_valuation_parameters')
    for key,value in ENTRY_CONTRACTS[entry]['branches'].items():
        if not value.get('authorization_view_only') and value['requirements']=={'maintenance_dashboard':sorted(actions)}:
            return key
    raise authz_error(403,'AUTHZ_CONTEXT_REQUIRED','该看板动作与参数未登记')


def read(db,identity,*,endpoint,params,view=None,precondition=None):
    if endpoint not in _ACTIONS:raise ValueError('unknown dashboard endpoint')
    if view not in (None,'authorization'):raise authz_error(422,'AUTHZ_INVALID_REQUEST','未知的授权视图')
    params=dict(params)
    if 'range_' in params:params['range']=params.pop('range_')
    entry='GET /api/maintenance/analytics/maintenance-dashboard/'+endpoint
    branch=_branch(entry,_ACTIONS[endpoint],params)
    with authorization.read_context_snapshot(lambda:Session(db.get_bind()),identity,
            entry_id=entry,params=params,branch=branch) as (snapshot,_,batch,accesses):
        ref=batch.contexts[0].ref
        if view=='authorization':return service.context_resolution(accesses,ref)
        access=accesses.require(ref,'maintenance_dashboard',_ACTIONS[endpoint])
        if params.get('value_retention_ratio') is not None:accesses.require(ref,'maintenance_dashboard','adjust_valuation_parameters')
        service.check_context_precondition(authorization.decode_precondition(precondition),accesses,ref)
        visible='data_profit' in access.field_permissions
        if params.get('sort')=='bad_value_desc' and not visible:
            raise authz_error(403,'AUTHZ_FIELD_REQUIRED','金额排序需要本模块金额字段权限')
        scope=_selection_scope(access,params)
        if endpoint=='options':
            projects=dash._project_rows(snapshot,scope,strict_projection=True)
            result={'range_presets':list(dash.RANGE_PRESETS),'default_range':'7d',
                'default_date_rule':'近 7 天含今天（上海日历日）',
                'sorts':{'projects':[s for s in dash.PROJECT_SORTS if visible or s!='bad_value_desc'],
                         'parts':[s for s in dash.PART_SORTS if visible or s!='bad_value_desc'],
                         'receipts':list(dash.RECEIPT_SORTS),'quantity_checks':list(dash.CHECK_SORTS)},
                'projects':[{'project_id':p.project_id,'project_code':p.project_code,'display_name':p.display_name,
                             'business_type':p.business_type} for p in projects.values()],
                'amounts_visible':visible,'max_export_rows':dash.MAX_EXPORT_ROWS,'export_kinds':list(_EXPORT_KINDS)}
            if visible:result['valuation_policy']=policy.policy_payload(policy.DEFAULT_RETENTION_RATIO)
        else:
            try:
                start,end=dash.resolve_date_range(params.get('range','7d'),params.get('date_from'),params.get('date_to'))
                filters=dash._base_filters(snapshot,scope=scope,start=start,end=end,q=params.get('q'),strict_projection=True)
                ratio=_ratio(params.get('value_retention_ratio'));page=params.get('page',1);size=params.get('page_size',dash.DEFAULT_PAGE_SIZE)
                if endpoint=='summary':
                    result=dash.build_summary_payload(snapshot,filters,include_amounts=visible,ratio=ratio if visible else None)
                elif endpoint=='projects':
                    rows=dash.sort_project_rows(dash.build_project_rows(snapshot,filters,include_amounts=visible,ratio=ratio if visible else None),params.get('sort','bad_receipt_qty_desc'))
                    result={'range':_range_payload(filters),**dash.paginate(rows,page,size)}
                elif endpoint=='parts':
                    rows=dash.sort_part_rows(dash.build_part_rows(snapshot,filters,include_amounts=visible,ratio=ratio if visible else None),params.get('sort','bad_receipt_qty_desc'))
                    result={'range':_range_payload(filters),**dash.paginate(rows,page,size)}
                    if visible:result['policy']=policy.policy_payload(ratio)
                elif endpoint=='valuation':result=_valuation_payload(snapshot,filters,ratio,params.get('sort','bad_value_desc'),page,size)
                elif endpoint=='receipts':
                    rows,total=dash.build_receipt_detail_rows(snapshot,filters,include_amounts=visible,
                        test_result=(params.get('test_result') or '').strip() or None,source=(params.get('source') or '').strip() or None,
                        created_by=(params.get('created_by') or '').strip() or None,ratio=ratio if visible else None)
                    rows=dash.sort_receipt_rows(rows,params.get('sort','occurred_desc'))
                    result={'range':_range_payload(filters),'rows':rows[(page-1)*size:page*size],
                            'pagination':{'page':page,'page_size':size,'total':total}}
                elif endpoint=='quantity-checks':
                    part_ids,keyword=dash._part_keyword(snapshot,params.get('q'))
                    value=dash.build_quantity_checks_payload(snapshot,scope,projects=filters.projects,
                        order_date_from=start,order_date_to=end,part_ids=part_ids,keyword=keyword)
                    rows=dash.sort_check_rows(value['rows'],params.get('sort','difference_desc'))
                    result={'cards':value['cards'],'unassigned_receipts':value['unassigned_receipts'],**dash.paginate(rows,page,size)}
                elif endpoint=='orders':
                    result={'range':_range_payload(filters),**consumption.period_orders(snapshot,start=start,end=end,projects=filters.projects)}
                elif endpoint=='consumption':
                    value=consumption.daily_consumption(snapshot,start=start,end=end,projects=filters.projects,include_amounts=visible)
                    result={'range':_range_payload(filters),'cards':value['cards'],**dash.paginate(value['rows'],page,size)}
                else:
                    rows=consumption.consumption_issue_rows(snapshot,start=start,end=end,projects=filters.projects,
                        include_amounts=visible,issue_date=params.get('issue_date'),project_id=params.get('project_id'))
                    result={'range':_range_payload(filters),**dash.paginate(rows,page,size)}
            except dash.DashboardInputError as exc:raise authz_error(422,'DOMAIN_INVALID',exc.code) from exc
        return {**result,'authorization_context':accesses.wire(ref)}


def _valuation_payload(db,filters,ratio,sort,page,page_size):
    rows = dash.sort_part_rows(
        dash.build_part_rows(db, filters, include_amounts=True, ratio=ratio), sort
    )
    # 卡与 PN 行同源（KPI=分组）：主域覆盖按单位分桶（B-F09），
    # 三域覆盖从行载荷合并（B-F07）；混合单位平面 null + unit_breakdown。
    main_units = dash.aggregate_main_units_from_rows(rows)
    mixed_units = len(main_units) > 1
    estimated_qty = sum(
        (Decimal(row["valuation"]["estimated_bad_qty"]) for row in rows), Decimal("0")
    )
    bad_receipt_qty = sum(
        (Decimal(row["bad_receipt_qty"]) for row in rows), Decimal("0")
    )

    def _flat_total(field: str) -> str | None:
        if mixed_units:
            return None
        return dash._qty(sum(
            (bucket[field] for bucket in main_units.values()), Decimal("0")
        ))

    cards: dict = {
        "estimated_bad_qty": _flat_total("estimated"),
        "pending_bad_qty": _flat_total("pending"),
        "awaiting_ratio_bad_qty": _flat_total("awaiting"),
        "coverage_pct": (
            None if mixed_units
            else dash._coverage_pct(estimated_qty, bad_receipt_qty)
        ),
        "unit": next(iter(main_units), None) if not mixed_units else None,
        "unit_breakdown": [
            {
                "unit": unit,
                "bad_qty": dash._qty(main_units[unit]["bad"]),
                "estimated_bad_qty": dash._qty(main_units[unit]["estimated"]),
                "pending_bad_qty": dash._qty(main_units[unit]["pending"]),
                "awaiting_ratio_bad_qty": dash._qty(main_units[unit]["awaiting"]),
                # 本单位已估金额（静态审查补充；仅已估单位给）。
                **({
                    "value_inc_tax": dash._amount(main_units[unit]["value_inc"]),
                    "value_ex_tax": dash._amount(main_units[unit]["value_ex"]),
                } if main_units[unit].get("has_value") else {}),
                "coverage_pct": dash._coverage_pct(
                    main_units[unit]["estimated"], main_units[unit]["bad"]
                ),
            }
            for unit in dash.sorted_unit_keys(main_units.keys())
        ],
        "domains": dash.aggregate_domain_cards_from_rows(rows),
    }
    # 待估行（pending）不带 bad_estimated_value_* 键（不填 0）——用 .get 计 0，
    # 否则任一待估 PN 会让整个估价页 500。
    bad_value_inc = sum(
        (Decimal(row["valuation"].get("bad_estimated_value_inc_tax", "0"))
         for row in rows),
        Decimal("0"),
    )
    bad_value_ex = sum(
        (Decimal(row["valuation"].get("bad_estimated_value_ex_tax", "0"))
         for row in rows),
        Decimal("0"),
    )
    if estimated_qty > 0:
        # 与 summary 估价卡同规则：全部待估（estimated==0）时不出现 0 元卡。
        cards["bad_estimated_value"] = {
            "inc_tax": dash._amount(bad_value_inc),
            "ex_tax": dash._amount(bad_value_ex),
        }
    return {
        "range": _range_payload(filters),
        "policy": policy.policy_payload(ratio),
        "note": policy.NOTES,
        "cards": cards,
        **dash.paginate(rows, page, page_size),
    }


@dataclass(frozen=True)
class PreparedExport:
    header: tuple
    values: tuple
    provenance: dict


def prepare_export(db, identity, *, params, view=None, precondition=None):
    """Materialize the full scoped export and its lineage in one RR observation."""
    from app.authz.provenance import AccessCollector
    from app.services import maintenance_dashboard_export as export_rows
    if view not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知的授权视图')
    kind = params.get('kind')
    if kind not in _EXPORT_KINDS:
        raise authz_error(422, 'DOMAIN_INVALID', '未知的导出类型')
    action = 'read_' + {'quantity_checks': 'quantity_checks', 'consumption_daily': 'consumption',
                        'consumption_issues': 'consumption_issues'}.get(kind, kind)
    required = {'export', action}
    if params.get('value_retention_ratio') is not None:
        required.add('adjust_valuation_parameters')
    entry = 'GET /api/maintenance/analytics/maintenance-dashboard/export'
    branch = next((name for name, rule in ENTRY_CONTRACTS[entry]['branches'].items()
        if rule['requirements'] == {'maintenance_dashboard': sorted(required)}), None)
    if branch is None:
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '导出分支未登记')
    with authorization.read_context_snapshot(lambda: Session(db.get_bind()), identity,
            entry_id=entry, params=params, branch=branch) as (snapshot, proof, batch, accesses):
        ref = batch.contexts[0].ref
        if view == 'authorization':
            return service.context_resolution(accesses, ref)
        required_accesses = [accesses.require(ref, 'maintenance_dashboard', name) for name in sorted(required)]
        access = accesses.require(ref, 'maintenance_dashboard', 'export')
        service.check_context_precondition(authorization.decode_precondition(precondition), accesses, ref)
        visible = 'data_profit' in access.field_permissions
        try:
            start, end = dash.resolve_date_range(params.get('range', '7d'), params.get('date_from'), params.get('date_to'))
            filters = dash._base_filters(snapshot, scope=_selection_scope(access, params), start=start,
                end=end, q=params.get('q'), strict_projection=True)
            header, values = export_rows.materialize(snapshot, filters, kind=kind, visible=visible,
                ratio=_ratio(params.get('value_retention_ratio')), q=params.get('q'),
                test_result=params.get('test_result'), source=params.get('source'), created_by=params.get('created_by'))
        except dash.DashboardInputError as exc:
            raise authz_error(422, 'DOMAIN_INVALID', exc.code) from exc
        collector = AccessCollector(access.principal.id, access.authorization_epoch, access.catalog_version)
        for granted in required_accesses:
            collector.record(granted)
        return PreparedExport(tuple(header), tuple(tuple(row) for row in values), collector.snapshot())
