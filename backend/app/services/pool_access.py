"""Protected pool queries and mutations. Fields never cross resource Grants."""
from collections.abc import Sequence
from datetime import date
from typing import Literal

from fastapi import HTTPException

from app.authz import runtime, service
from app.authz.principal import VerifiedIdentity, load_principal
from app.security import UserContext, apply_field_visibility, is_field_hidden, require_action, require_login, require_page
from app.services import pool_catalog, pool_price_analysis


def _authoritative(db, *resources):
    states = runtime.runtime_states(db)
    return any(states[resource].runtime_state != 'legacy' for resource in resources)


def _legacy(db, identity, ctx, *, page=None, action=None, data=None):
    load_principal(db, identity)
    if page:
        require_page(page)(ctx)
    elif action:
        require_action(action, require_data=data)(ctx)
    else:
        require_login(ctx)


def _analysis_context(db, identity, legacy_context, resource='pool_price_analysis'):
    if _authoritative(db, resource):
        return service.field_context(service.resource_access(db, identity, resource, 'read'), legacy_context)
    _legacy(db, identity, legacy_context,
            page='page_boss_board' if resource == 'company_pool_order_facts' else 'page_pool_analysis')
    return legacy_context


def read_pool_references(db, identity: VerifiedIdentity, part_ids: Sequence[int], *, legacy_context: UserContext,
                         range_: str = '90d', date_from: date | None = None,
                         date_to: date | None = None, purchase_type: str | None = None) -> list[dict]:
    ctx = _analysis_context(db, identity, legacy_context)
    return pool_price_analysis.apply_visibility(pool_price_analysis.references(
        db, list(part_ids), range_=range_, date_from=date_from, date_to=date_to,
        purchase_type=purchase_type), ctx)


def read_pool_reference(db, identity: VerifiedIdentity, part_id: int, *, legacy_context: UserContext,
                        range_: str = '90d', date_from: date | None = None,
                        date_to: date | None = None, purchase_type: str | None = None) -> dict | None:
    rows = read_pool_references(db, identity, [part_id], legacy_context=legacy_context,
        range_=range_, date_from=date_from, date_to=date_to, purchase_type=purchase_type)
    return rows[0] if rows else None


def _order_fact(db, identity, legacy_context, resource, side, order_id):
    ctx = _analysis_context(db, identity, legacy_context, resource)
    data = pool_price_analysis.order_detail(db, side, order_id)
    return pool_price_analysis.apply_visibility(data, ctx) if data is not None else None


def read_pool_order_fact(db, identity: VerifiedIdentity, *, legacy_context: UserContext,
                         side: Literal['purchase', 'sales'], order_id: int) -> dict | None:
    return _order_fact(db, identity, legacy_context, 'pool_order_facts', side, order_id)


def read_company_pool_order_fact(db, identity: VerifiedIdentity, *, legacy_context: UserContext,
                                 side: Literal['purchase', 'sales'], order_id: int) -> dict | None:
    return _order_fact(db, identity, legacy_context, 'company_pool_order_facts', side, order_id)


def read_pool_analysis_list(db, identity, *, legacy_context, sort='member_count', **filters):
    if not _authoritative(db, 'pool_price_analysis', 'pool_order_facts'):
        ctx = _analysis_context(db, identity, legacy_context)
        accesses = None
    else:
        accesses = service.read_accesses(db, identity, {'pool_price_analysis': ('read',), 'pool_order_facts': ('read',)})
        access = accesses.get('pool_price_analysis', 'read')
        if access is None:
            accesses.require('pool_order_facts', 'read')
            if filters.get('pn') or filters.get('purchase_type'):
                raise HTTPException(403, '未获得分析权限，不能按成员或交易事实筛选池')
            result = pool_catalog.list_pools(db, q=filters.get('q'), page=filters.get('page', 1),
                page_size=filters.get('page_size', 20), include_catalog=False, include_policy=False)
            lower, upper, today, token = pool_price_analysis.resolve_window(filters.get('range_'), filters.get('date_from'), filters.get('date_to'))
            result.update(ranking_restricted=True, effective_sort='group_id', effective_search='name',
                sort=sort, basis='ex_tax', purchase_type=None, window={'range': token,
                'date_from': lower.isoformat() if lower else None, 'date_to': upper.isoformat(), 'as_of': today.isoformat()})
            for row in result['items']:
                row.update(purchase_reference=None, sales_reference=None)
            result['block_access'] = {'price_analysis': accesses.decision('pool_price_analysis', 'read')}
            return result
        ctx = service.field_context(access, legacy_context)
    restricted = is_field_hidden(ctx, 'purchase_ceiling_ex_tax') and sort != 'member_count'
    result = pool_price_analysis.apply_visibility(pool_price_analysis.list_pools(
        db, requested_sort=sort, effective_sort='member_count' if restricted else sort,
        ranking_restricted=restricted, **filters), ctx)
    if accesses is not None:
        result['block_access'] = {'price_analysis': accesses.decision('pool_price_analysis', 'read')}
    return result


def read_pool_price_map(db, identity, group_id, *, legacy_context, sort='pn', order='asc', **filters):
    ctx = _analysis_context(db, identity, legacy_context)
    restricted = is_field_hidden(ctx, 'purchase_ceiling_ex_tax')
    data = pool_price_analysis.price_map(db, group_id, requested_sort=sort, requested_order=order,
        effective_sort='pn' if restricted else sort, effective_order='asc' if restricted else order, **filters)
    return pool_price_analysis.apply_visibility(data, ctx) if data is not None else None


def read_pool_analysis_detail(db, identity, group_id, *, legacy_context, **filters):
    if not _authoritative(db, 'pool_price_analysis', 'pool_order_facts'):
        _legacy(db, identity, legacy_context, page='page_pool_analysis')
        data = pool_price_analysis.pool_detail(db, group_id, **filters)
        return pool_price_analysis.apply_visibility(data, legacy_context) if data is not None else None
    accesses = service.read_accesses(db, identity, {
        'pool_price_analysis': ('read',), 'pool_order_facts': ('read',)})
    main = accesses.get('pool_price_analysis', 'read')
    facts = accesses.get('pool_order_facts', 'read')
    if main is None and facts is None:
        accesses.require('pool_price_analysis', 'read')
    data = pool_price_analysis.pool_detail(db, group_id, include_transactions=facts is not None, include_analysis=main is not None, **filters)
    if data is None:
        return None
    # Strip both aliases before applying analysis fields. Facts are projected
    # independently, then the exact same projected objects restore each alias.
    transactions = {name: data.pop(name) for name in (
        'purchase_transactions', 'sales_transactions', 'purchase_orders', 'sales_orders')}
    if main is not None:
        data = pool_price_analysis.apply_visibility(data, service.field_context(main, legacy_context))
    if facts is not None:
        transactions = pool_price_analysis.apply_visibility(
            transactions, service.field_context(facts, legacy_context))
    data.update(transactions)
    data['block_access'] = {
        'price_analysis': accesses.decision('pool_price_analysis', 'read'),
        'purchase_transactions': accesses.decision('pool_order_facts', 'read'),
        'sales_transactions': accesses.decision('pool_order_facts', 'read'),
    }
    return data


def _policy_decision(accesses):
    access = accesses.get('pool_price_policy', 'read')
    if access is None:
        return False, accesses.decision('pool_price_policy', 'read')
    if 'data_pool_price_governance' not in access.field_permissions:
        return False, {'effect': 'deny', 'reason': {'code': 'FIELD_NOT_GRANTED', 'message': '未获得池约束价字段授权'}}
    return True, accesses.decision('pool_price_policy', 'read')


def read_pool_catalog(db, identity, *, legacy_context, policy_missing=None, **filters):
    if not _authoritative(db, 'pool_catalog', 'pool_price_policy'):
        _legacy(db, identity, legacy_context)
        visible = not is_field_hidden(legacy_context, 'purchase_ceiling_ex_tax')
        if policy_missing is not None and not visible:
            raise HTTPException(403, '无池约束价查看权限，不能按约束价缺失状态筛选')
        data = pool_catalog.list_pools(db, policy_missing=policy_missing, include_policy=visible, **filters)
        data.update(price_restricted=not visible, coverage_restricted=not visible)
        for row in data['items']:
            row['price_restricted'] = not visible
        return apply_field_visibility(data, legacy_context)
    accesses = service.read_accesses(db, identity, {'pool_catalog': ('read',), 'pool_price_policy': ('read',)})
    catalog = accesses.get('pool_catalog', 'read')
    if catalog is None and accesses.get('pool_price_policy', 'read') is None:
        accesses.require('pool_catalog', 'read')
    visible, decision = _policy_decision(accesses)
    if policy_missing is not None and not visible:
        raise HTTPException(403, '无池约束价查看权限，不能按约束价缺失状态筛选')
    data = pool_catalog.list_pools(db, policy_missing=policy_missing, include_policy=visible, include_catalog=catalog is not None, **filters)
    data.update(price_restricted=not visible, coverage_restricted=not visible)
    for row in data['items']:
        row['price_restricted'] = not visible
    data['block_access'] = {'catalog': accesses.decision('pool_catalog', 'read'),
                            'price_policy': decision, 'policy_coverage': decision}
    if catalog is None:
        data.update(ranking_restricted=True, effective_sort='group_id', effective_search='name')
    return data


def read_pool_catalog_detail(db, identity, group_id, *, legacy_context):
    if not _authoritative(db, 'pool_catalog', 'pool_price_policy'):
        _legacy(db, identity, legacy_context)
        visible = not is_field_hidden(legacy_context, 'purchase_ceiling_ex_tax')
        data = pool_catalog.get_pool(db, group_id, include_policy=visible)
        if data is None:
            return None
        data['price_restricted'] = not visible
        return apply_field_visibility(data, legacy_context)
    accesses = service.read_accesses(db, identity, {'pool_catalog': ('read',), 'pool_price_policy': ('read',)})
    catalog = accesses.get('pool_catalog', 'read')
    if catalog is None and accesses.get('pool_price_policy', 'read') is None:
        accesses.require('pool_catalog', 'read')
    visible, decision = _policy_decision(accesses)
    data = pool_catalog.get_pool(db, group_id, include_policy=visible, include_catalog=catalog is not None)
    if data is None:
        return None
    data['price_restricted'] = not visible
    data['block_access'] = {'catalog': accesses.decision('pool_catalog', 'read'),
                            'price_policy': decision, 'price_policy_history': decision}
    return data


def _change(db, identity, legacy_context, action, operation, **values):
    resource = 'pool_price_policy' if action == 'set' else 'pool_catalog'
    if not _authoritative(db, 'pool_catalog', 'pool_price_policy'):
        _legacy(db, identity, legacy_context, action='action_pool_set_policy' if action == 'set' else 'action_pool_manage',
                data='data_pool_price_governance' if action == 'set' else None)
        return apply_field_visibility(operation(db, operated_by=legacy_context.user_id or legacy_context.role, **values), legacy_context)
    result_access = None

    def authorize():
        nonlocal result_access
        actor = service.lock_actor(db, identity)
        requirements = {'pool_price_policy': ('read', 'set') if action == 'set' else ('read',)}
        requirements['pool_catalog'] = ('read',) if action == 'set' else (action,)
        result_access = service.read_accesses(db, identity, requirements, locked_user=actor)
        result_access.require(resource, action)

    try:
        data = operation(db, operated_by=identity.username, _authorize=authorize, _commit=False, **values)
        if data is None:
            return None
        visible, _ = _policy_decision(result_access)
        if action == 'set' and result_access.get('pool_catalog', 'read') is None:
            for key in ('description', 'source', 'member_count', 'created_by',
                        'updated_by', 'created_at', 'updated_at'):
                if key in data:
                    data[key] = None
        if action != 'set' and visible:
            policy = pool_catalog._current_policy(db, data['group_id'])
            data['purchase_ceiling_ex_tax'] = policy.purchase_ceiling_ex_tax if policy else None
            data['sales_floor_ex_tax'] = policy.sales_floor_ex_tax if policy else None
        if not visible:
            data['purchase_ceiling_ex_tax'] = data['sales_floor_ex_tax'] = None
            if 'price_policy' in data:
                data['price_policy'] = None
        db.commit()
        return data
    except BaseException:
        db.rollback()
        raise


def create_pool(db, identity, *, legacy_context, **values):
    return _change(db, identity, legacy_context, 'create', pool_catalog.create_pool, **values)


def edit_pool(db, identity, *, legacy_context, **values):
    return _change(db, identity, legacy_context, 'edit', pool_catalog.update_pool, **values)


def edit_pool_members(db, identity, *, legacy_context, **values):
    return _change(db, identity, legacy_context, 'edit_members', pool_catalog.update_members, **values)


def archive_pool(db, identity, *, legacy_context, **values):
    return _change(db, identity, legacy_context, 'archive', pool_catalog.archive_pool, **values)


def restore_pool(db, identity, *, legacy_context, **values):
    return _change(db, identity, legacy_context, 'restore', pool_catalog.restore_pool, **values)


def set_pool_price_policy(db, identity, *, legacy_context, **values):
    return _change(db, identity, legacy_context, 'set', pool_catalog.set_price_policy, **values)
