"""Protected part use cases. HTTP and tools use the same explicit capabilities."""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.authz import runtime, service
from app.authz.principal import VerifiedIdentity, load_principal
from app.models.dimensions import DimPart
from app.security import UserContext, apply_field_visibility, require_page, require_login, record_access_log, is_field_hidden
from app.services import part_overview, part_resolver, master_edit, batch_normalize, substitute, taxonomy, standardize


def _authoritative(db, *resources):
    states = runtime.runtime_states(db)
    return any(states[r].runtime_state != 'legacy' for r in resources)


def _access(db, identity, ctx, resource, action, *, locked_user=None):
    if _authoritative(db, resource):
        return service.field_context(service.resource_access(db, identity, resource, action, locked_user=locked_user), ctx)
    load_principal(db, identity, locked_user=locked_user)
    if resource == 'part_master':
        require_page('page_master_data')(ctx)
    elif resource == 'part_substitute' and action == 'manage':
        if ctx.role not in ('admin', 'purchaser'):
            raise HTTPException(403, '无替代料维护权限')
    else:
        require_login(ctx)
    return ctx


def _search(db, identity, ctx, resource, q=None, page=1, page_size=20, browse=False, **filters):
    field_ctx = _access(db, identity, ctx, resource, 'read')
    q = (q or '').strip() or None
    resolver = bool(q and not any(value is not None for value in filters.values()) and not browse)
    record_access_log(ctx, 'search', 'parts', {'q': q, 'branch': 'resolver' if resolver else 'structured'})
    if resolver:
        result = part_resolver.resolve(db, q, limit=page_size, operated_by=identity.username or ctx.role, include_similar=True)
        result = {'total': len(result['items']), 'page': 1, 'page_size': page_size, **result}
    else:
        result = part_overview.search_parts(db, q, page, page_size, field_ctx, **filters)
    return apply_field_visibility(result, field_ctx)


def read_part_search(db, identity: VerifiedIdentity, *, legacy_context: UserContext,
                     q=None, page=1, page_size=20, part_type=None, interface=None,
                     capacity_min=None, capacity_max=None, category_major=None,
                     category_minor=None, browse=False):
    return _search(db, identity, legacy_context, 'part_lookup', q=q, page=page, page_size=page_size,
                   browse=browse, part_type=part_type, interface=interface, capacity_min=capacity_min,
                   capacity_max=capacity_max, category_major=category_major, category_minor=category_minor)


def read_master_search(db, identity, *, legacy_context, **filters):
    filters.pop('browse', None)
    return _search(db, identity, legacy_context, 'part_master', browse=True, **filters)


def read_part_categories(db, identity, *, legacy_context):
    _access(db, identity, legacy_context, 'part_lookup', 'read')
    tree = {}
    for major, minor in db.execute(select(DimPart.category_major, DimPart.category_minor).where(
            DimPart.status == 'active', DimPart.category_major.is_not(None)).distinct()):
        tree.setdefault(major, set())
        if minor:
            tree[major].add(minor)
    return {'categories': [{'major': major, 'minors': sorted(minors)} for major, minors in sorted(tree.items())]}


_LOOKUP_BLOCKS = {'identity': 'read', 'purchases_recent': 'purchase_records_read',
                  'sales_recent': 'sales_records_read', 'inventory': 'inventory_read',
                  'profit_summary': 'financial_summary_read', 'market_prices': 'market_prices_read'}


def read_part_overview(db, identity: VerifiedIdentity, *, legacy_context: UserContext,
                       pn_std=None, part_id=None):
    if not _authoritative(db, 'part_lookup', 'part_substitute'):
        ctx = _access(db, identity, legacy_context, 'part_lookup', 'read')
        return apply_field_visibility(part_overview.get_overview(db, pn_std, ctx, part_id=part_id), ctx)
    accesses = service.read_accesses(db, identity, {'part_lookup': tuple(_LOOKUP_BLOCKS.values()), 'part_substitute': ('read',)})
    access = next((accesses.get('part_lookup', action) for action in _LOOKUP_BLOCKS.values() if accesses.get('part_lookup', action) is not None), accesses.get('part_substitute', 'read'))
    if access is None:
        accesses.require('part_lookup', 'read')
    blocks = {block for block, action in _LOOKUP_BLOCKS.items() if accesses.get('part_lookup', action) is not None}
    if accesses.get('part_substitute', 'read') is not None:
        blocks.add('substitutes')
    ctx = service.field_context(access, legacy_context)
    result = part_overview.get_overview(db, pn_std, ctx, part_id=part_id, blocks=blocks)
    if result is None:
        return None
    result = apply_field_visibility(result, ctx)
    result['block_access'] = {block: accesses.decision('part_lookup', action) for block, action in _LOOKUP_BLOCKS.items()}
    result['block_access']['substitutes'] = accesses.decision('part_substitute', 'read')
    result['block_access']['substitute_inventory'] = accesses.decision(
        'part_lookup', 'inventory_read') if 'substitutes' in blocks else accesses.decision('part_substitute', 'read')
    return result


def read_part_purchases(db, identity, *, legacy_context, pn_std, page=1, page_size=20):
    ctx = _access(db, identity, legacy_context, 'part_lookup', 'purchase_records_read')
    return apply_field_visibility(part_overview.list_purchases(db, pn_std, page, page_size, ctx), ctx)


def read_part_sales(db, identity, *, legacy_context, pn_std, page=1, page_size=20):
    ctx = _access(db, identity, legacy_context, 'part_lookup', 'sales_records_read')
    return apply_field_visibility(part_overview.list_sales(db, pn_std, page, page_size, ctx), ctx)


def read_quick_pricing(db, identity: VerifiedIdentity, *, legacy_context: UserContext, pn_std: str):
    if not _authoritative(db, 'part_lookup'):
        ctx = _access(db, identity, legacy_context, 'part_lookup', 'read')
        return apply_field_visibility(part_overview.quick_pricing(db, pn_std), ctx)
    actions = ('read', 'financial_summary_read', 'market_prices_read', 'inventory_read')
    accesses = service.read_accesses(db, identity, {'part_lookup': actions})
    access = next((accesses.get('part_lookup', action) for action in actions if accesses.get('part_lookup', action) is not None), None)
    if access is None:
        accesses.require('part_lookup', 'read')
    allowed = lambda action: accesses.get('part_lookup', action) is not None
    result = part_overview.quick_pricing(db, pn_std, financial=allowed('financial_summary_read'),
                                       market=allowed('market_prices_read'), inventory=allowed('inventory_read'), identity_details=allowed('read'))
    if not allowed('financial_summary_read'):
        for key in ('last_purchase_price', 'last_purchase_date', 'last_purchase_type', 'recent_purchase_days',
                    'recent_purchase_avg', 'recent_purchase_min', 'recent_purchase_max', 'recent_purchase_count', 'avg_sale_price_90d'):
            result[key] = None
    if not allowed('market_prices_read'):
        for key in ('ref_sale_price', 'ref_sale_samples', 'ref_window_days'):
            result[key] = None
    if not allowed('inventory_read'):
        result['stock_total'] = None
    result = apply_field_visibility(result, service.field_context(access, legacy_context))
    result['block_access'] = {block: accesses.decision('part_lookup', action) for block, action in (
        ('identity', 'read'), ('profit_summary', 'financial_summary_read'),
        ('market_prices', 'market_prices_read'), ('inventory', 'inventory_read'))}
    return result


def check_master_duplicates(db, identity, *, legacy_context, pn_std):
    _access(db, identity, legacy_context, 'part_master', 'read')
    return {'near_duplicates': master_edit.find_near_duplicates(db, pn_std)}


def read_master_edit_state(db, identity, part_id, *, legacy_context):
    _access(db, identity, legacy_context, 'part_master', 'read')
    return master_edit.get_part_edit_state(db, part_id)


def read_master_edit_history(db, identity, part_id, *, legacy_context, limit=20, before_id=None):
    _access(db, identity, legacy_context, 'part_master', 'read')
    if not 1 <= part_id <= 2147483647 or db.get(DimPart, part_id) is None:
        return None
    return master_edit.list_part_edit_history(db, part_id, limit=limit, before_id=before_id)


def read_master_categories(db, identity, *, legacy_context):
    _access(db, identity, legacy_context, 'part_master', 'read')
    return {'categories': [{'code': code, 'name': name, 'children': [
        {'code': c, 'name': n} for c, n in taxonomy.CATEGORY_NAMES.items() if len(c) == 4 and c.startswith(code)]}
        for code, name in taxonomy.CATEGORY_NAMES.items() if len(code) == 2],
        'battery_subtypes': taxonomy.BATTERY_SUBTYPES, 'cooling_types': taxonomy.COOLING_TYPES}


def read_master_suggestion(db, identity, *, legacy_context, description='', pn='', brand=''):
    _access(db, identity, legacy_context, 'part_master', 'read')
    return {'suggestion': {k: v for k, v in standardize.standardize(pn, description, brand).items() if not k.startswith('_')}}


def preview_master_normalization(db, identity, *, legacy_context, page=1, page_size=20, only_changes=True):
    ctx = _access(db, identity, legacy_context, 'part_master', 'normalize_preview')
    canonical = runtime.is_authoritative(db, 'part_master')
    result = batch_normalize.preview(db, page, page_size, only_changes,
        include_financial=not canonical or not is_field_hidden(ctx, 'unit_price'))
    return apply_field_visibility(result, ctx)


def read_substitutes(db, identity, *, legacy_context, pn_std):
    _access(db, identity, legacy_context, 'part_substitute', 'read')
    return substitute.list_substitutes(db, pn_std)


def _master_change(db, identity, legacy_context, action, operation, **values):
    authoritative = _authoritative(db, 'part_master')
    if not authoritative:
        _access(db, identity, legacy_context, 'part_master', action)
        principal = load_principal(db, identity)
        if principal is None:
            raise HTTPException(403, '主数据库编辑必须使用实名系统账号，请重新登录')
    ctx = None
    def authorize():
        nonlocal ctx
        actor = service.lock_actor(db, identity)
        ctx = _access(db, identity, legacy_context, 'part_master', action, locked_user=actor)
    try:
        data = operation(db, operated_by=identity.username, _authorize=authorize if authoritative else None, **values)
        result = apply_field_visibility(data, ctx or legacy_context)
        db.commit()
        return result
    except DBAPIError as exc:
        db.rollback()
        master_edit._raise_concurrent_write(exc)
        raise
    except BaseException:
        db.rollback()
        raise


def create_master_part(db, identity, *, legacy_context, **values):
    return _master_change(db, identity, legacy_context, 'create', master_edit.create_part_in_transaction, **values)


def edit_master_part(db, identity, *, legacy_context, **values):
    return _master_change(db, identity, legacy_context, 'edit', master_edit.edit_part_in_transaction, **values)


def apply_master_normalization(db, identity, *, legacy_context, part_ids, fields=None):
    if not _authoritative(db, 'part_master'):
        _access(db, identity, legacy_context, 'part_master', 'normalize_apply')
        return batch_normalize.apply_batch(db, part_ids, fields, legacy_context.user_id or legacy_context.role)
    result = {'applied': 0, 'skipped': 0}
    if not part_ids:
        _access(db, identity, legacy_context, 'part_master', 'normalize_apply')
    for part_id in part_ids:
        # Preserve the domain's historical per-row commit boundary. Each new
        # row obtains its domain locks before a fresh actor lock and decision.
        ctx = None
        def authorize():
            nonlocal ctx
            actor = service.lock_actor(db, identity)
            ctx = _access(db, identity, legacy_context, 'part_master', 'normalize_apply', locked_user=actor)
        try:
            row = batch_normalize.apply_batch(db, [part_id], fields, identity.username,
                                              _commit=False, _authorize=authorize)
            row = apply_field_visibility(row, ctx)
            db.commit()
            result['applied'] += row['applied']
            result['skipped'] += row['skipped']
        except BaseException:
            db.rollback()
            raise
    return result


def _substitute_change(db, identity, legacy_context, operation, pn_a, pn_b, **values):
    if not _authoritative(db, 'part_substitute'):
        _access(db, identity, legacy_context, 'part_substitute', 'manage')
        return operation(db, pn_a=pn_a, pn_b=pn_b, operated_by=legacy_context.role, **values)
    try:
        # Same master lock as rename/merge; order all involved parent rows before actor.
        from app.services.master_data import acquire_master_write_lock
        acquire_master_write_lock(db)
        first, _ = part_overview.resolve_part(db, pn_a)
        second, _ = part_overview.resolve_part(db, pn_b)
        ids = [p.id for p in (first, second) if p is not None]
        list(db.scalars(select(DimPart).where(DimPart.id.in_(ids)).order_by(DimPart.id)
                        .with_for_update().execution_options(populate_existing=True)))
        actor = service.lock_actor(db, identity)
        _access(db, identity, legacy_context, 'part_substitute', 'manage', locked_user=actor)
        result = operation(db, pn_a=pn_a, pn_b=pn_b, operated_by=identity.username, _commit=False, **values)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def add_substitute(db, identity, *, legacy_context, pn_a, pn_b, note=None, direction='both', substitute_type=None):
    return _substitute_change(db, identity, legacy_context, substitute.add_substitute, pn_a, pn_b,
                              note=note, direction=direction, substitute_type=substitute_type)


def delete_substitute(db, identity, *, legacy_context, pn_a, pn_b):
    return _substitute_change(db, identity, legacy_context, substitute.remove_substitute, pn_a, pn_b)
