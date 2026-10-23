"""Protected company analytics operations shared by every transport.

The calculation modules remain unaware of token roles. Legacy page rules are
consulted only before a resource's persistent cutover.
"""
from fastapi import HTTPException

from app.authz import runtime, service
from app.authz.principal import VerifiedIdentity, load_principal
from app.security import UserContext, apply_field_visibility, is_field_hidden, is_scoped_sales, require_page
from app.services import dashboard, pool, pool_catalog, price_discipline


def _context(db, identity: VerifiedIdentity, legacy_context: UserContext, resource: str):
    if runtime.is_authoritative(db, resource):
        access = service.resource_access(db, identity, resource, 'read')
        return service.field_context(access, legacy_context), True
    load_principal(db, identity)
    require_page('page_boss_board')(legacy_context)
    return legacy_context, False


def read_company_kpi(db, identity, *, legacy_context, date_from=None, date_to=None):
    ctx, _ = _context(db, identity, legacy_context, 'company_summary')
    return apply_field_visibility(dashboard.kpi(db, date_from, date_to, user_ctx=ctx), ctx)


def read_company_trend(db, identity, *, legacy_context, date_from=None, date_to=None,
                       granularity='day'):
    ctx, _ = _context(db, identity, legacy_context, 'company_summary')
    return apply_field_visibility(dashboard.trend(db, date_from, date_to,
                                                 granularity=granularity, user_ctx=ctx), ctx)


def read_company_part_ranking(db, identity, *, legacy_context, **filters):
    ctx, _ = _context(db, identity, legacy_context, 'company_summary')
    return apply_field_visibility(dashboard.part_ranking(db, user_ctx=ctx, **filters), ctx)


def read_company_sales(db, identity, *, legacy_context, **filters):
    ctx, authoritative = _context(db, identity, legacy_context, 'company_sales_orders')
    if (authoritative or not is_scoped_sales(ctx)) and filters.get('customer') and is_field_hidden(ctx, 'customer'):
        raise HTTPException(403, '当前账号无客户信息权限，不能按客户筛选')
    return apply_field_visibility(dashboard.sales_orders(
        db, user_ctx=ctx, authorized=True if authoritative else None, **filters), ctx)


def read_company_purchase_orders(db, identity, *, legacy_context, **filters):
    ctx, _ = _context(db, identity, legacy_context, 'company_purchase_orders')
    return apply_field_visibility(dashboard.purchase_orders(db, user_ctx=ctx, **filters), ctx)


def read_price_discipline(db, identity, *, legacy_context, date_from=None, date_to=None):
    ctx, _ = _context(db, identity, legacy_context, 'price_discipline')
    if is_field_hidden(ctx, 'purchase_ceiling_ex_tax'):
        return price_discipline.restricted_summary(date_from=date_from, date_to=date_to)
    return apply_field_visibility(price_discipline.summary(
        db, date_from=date_from, date_to=date_to), ctx)


def read_company_pools(db, identity, *, legacy_context, **filters):
    if not runtime.is_authoritative(db, 'company_pool_analysis'):
        load_principal(db, identity)
        require_page('page_boss_board')(legacy_context)
        return apply_field_visibility(pool.list_pools(db, user_ctx=legacy_context, **filters), legacy_context)
    resource = 'company_pool_analysis'
    accesses = service.read_accesses(db, identity, {resource: (
        'read_summary', 'read_sales_transactions', 'read_customer_cross_brand')})
    access = accesses.get(resource, 'read_summary')
    if access is None:
        if not any(accesses.get(resource, a) is not None for a in ('read_sales_transactions', 'read_customer_cross_brand')):
            accesses.require(resource, 'read_summary')
        if filters.get('pn'):
            raise HTTPException(403, '未获得摘要权限，不能按成员型号筛选')
        result = pool_catalog.list_pools(db, q=filters.get('q'), page=filters.get('page', 1),
            page_size=filters.get('page_size', 20), include_catalog=False, include_policy=False)
        result.update(ranking_restricted=True, effective_sort='group_id', effective_search='name')
    else:
        ctx = service.field_context(access, legacy_context)
        result = apply_field_visibility(pool.list_pools(db, user_ctx=ctx, **filters), ctx)
    result['block_access'] = {'summary': accesses.decision(resource, 'read_summary')}
    return result


def read_company_pool(db, identity, group_id: int, *, legacy_context, **filters):
    resource = 'company_pool_analysis'
    if not runtime.is_authoritative(db, resource):
        load_principal(db, identity)
        require_page('page_boss_board')(legacy_context)
        return apply_field_visibility(pool.analyze(db, group_id, user_ctx=legacy_context,
                                                   with_v2=True, **filters), legacy_context)
    accesses = service.read_accesses(db, identity, {resource: (
        'read_summary', 'read_sales_transactions', 'read_customer_cross_brand')})
    access = next((accesses.get(resource, action) for action in (
        'read_summary', 'read_sales_transactions', 'read_customer_cross_brand')
        if accesses.get(resource, action) is not None), None)
    if access is None:
        accesses.require(resource, 'read_summary')
    ctx = service.field_context(access, legacy_context)
    data = pool.analyze(db, group_id, user_ctx=ctx, with_v2=True,
        summary_authorized=accesses.get(resource, 'read_summary') is not None,
        sales_authorized=accesses.get(resource, 'read_sales_transactions') is not None,
        cross_brand_authorized=accesses.get(resource, 'read_customer_cross_brand') is not None,
        **filters)
    if data is None:
        return None
    data = apply_field_visibility(data, ctx)
    data['block_access'] = {block: accesses.decision(resource, action) for block, action in (
        ('summary', 'read_summary'), ('sales_orders', 'read_sales_transactions'),
        ('customer_cross_brand', 'read_customer_cross_brand'))}
    return data
