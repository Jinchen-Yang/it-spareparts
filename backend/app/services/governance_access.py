"""Protected governance and data-quality use cases, independent of HTTP handlers."""
import logging

from fastapi import HTTPException
from sqlalchemy import desc, func, select

from app.authz import runtime, service
from app.authz.principal import load_principal
from app.models.dimensions import DimPart, PartAlias
from app.models.data_quality import FactDataQualityIssue
from app.models.master_data import ProductDataQualityIssue, ProductMatchCandidate, ProductMergeLog
from app.models.system import SysAuditLog
from app.security import (apply_field_visibility, apply_profit_recompute_visibility,
                          is_field_hidden, is_scoped_sales, require_page, require_action)
from app.services import governance, master_data, match_candidates, merge, part_rename, data_quality, data_quality_calibration

_log = logging.getLogger(__name__)


def _ctx(db, identity, legacy_context, resource, action, *, locked_user=None):
    if runtime.is_authoritative(db, resource):
        return service.field_context(service.resource_access(db, identity, resource, action, locked_user=locked_user), legacy_context)
    load_principal(db, identity, locked_user=locked_user)
    require_page('page_governance')(legacy_context)
    return legacy_context


def _visible(data, ctx):
    if data is None:
        return None
    data = apply_field_visibility(data, ctx)
    if isinstance(data.get('recompute'), dict):
        data['recompute'] = apply_profit_recompute_visibility(data['recompute'], ctx)
    return data


def read_summary(db, identity, *, legacy_context):
    ctx = _ctx(db, identity, legacy_context, 'governance', 'read')
    return _visible(governance.summary(db), ctx)


def read_metrics(db, identity, *, legacy_context):
    ctx = _ctx(db, identity, legacy_context, 'governance', 'read')
    return _visible(master_data.metrics(db), ctx)


def read_parts(db, identity, *, legacy_context, kind='nonstd', page=1, page_size=20):
    ctx = _ctx(db, identity, legacy_context, 'governance', 'read')
    result = _visible(governance.list_parts(db, kind if kind in ('nonstd','needs_review','excluded') else 'nonstd', page, page_size), ctx)
    if is_field_hidden(ctx, 'cost_moving_avg') or is_field_hidden(ctx, 'gross_margin'):
        for row in result['items']:
            row['gross_margin'] = None
    return result


def read_candidates(db, identity, *, legacy_context, status='pending', order='volume', page=1, page_size=20):
    ctx = _ctx(db, identity, legacy_context, 'governance', 'read')
    return _visible(match_candidates.list_candidates(db, status, page, page_size, order), ctx)


def read_governance_issues(db, identity, *, legacy_context, issue_type=None, status='open', page=1, page_size=20):
    ctx = _ctx(db, identity, legacy_context, 'governance', 'read')
    return _visible(master_data.list_issues(db, issue_type, status, page, page_size), ctx)


def read_aliases(db, identity, *, legacy_context, status='pending', page=1, page_size=20):
    ctx = _ctx(db, identity, legacy_context, 'part_identity', 'read')
    return _visible(master_data.list_aliases(db, status, page, page_size), ctx)


def read_merges(db, identity, *, legacy_context, page=1, page_size=20):
    ctx = _ctx(db, identity, legacy_context, 'part_identity', 'read')
    total = db.scalar(select(func.count()).select_from(ProductMergeLog))
    rows = db.scalars(select(ProductMergeLog).order_by(desc(ProductMergeLog.id)).offset((page-1)*page_size).limit(page_size)).all()
    ids = [row.source_part_id for row in rows]+[row.target_part_id for row in rows]
    parts = {part.id: part for part in db.scalars(select(DimPart).where(DimPart.id.in_(ids)))} if ids else {}
    items = []
    for row in rows:
        source, target = parts.get(row.source_part_id), parts.get(row.target_part_id)
        items.append({'merge_log_id': row.id, 'source_pn': (row.source_snapshot or {}).get('pn_std') or (source.pn_std if source else None),
                      'target_pn': target.pn_std if target else None, 'reason': row.merge_reason,
                      'created_by': row.created_by, 'created_at': row.created_at,
                      'reverted': source is None or source.status != 'merged' or source.merged_into_id != row.target_part_id,
                      'affected_counts': {key: len(value) if isinstance(value,list) else value for key,value in (row.affected_records or {}).items()}})
    return _visible({'total': total, 'page': page, 'page_size': page_size, 'items': items}, ctx)



def _invoke(operation, commit, *args, **kwargs):
    return operation(*args, **kwargs) if commit else operation(*args, _commit=False, **kwargs)


def _write(db, identity, legacy_context, resource, action, operation, *, pns=(), ids=(), all_parts=False,
           record=None, requirements=None, bulk_audit=None):
    states = runtime.runtime_states(db)
    requirements = requirements or {resource: (action,)}
    authoritative = any(states[r].runtime_state != 'legacy' for r in requirements)
    if not authoritative:
        ctx = _ctx(db, identity, legacy_context, resource, action)
        result = operation(True, legacy_context.user_id or legacy_context.role)
        if bulk_audit:
            db.add(SysAuditLog(entity_type='governance', entity_id=0, action=bulk_audit,
                               after_json=result, operated_by=legacy_context.user_id or legacy_context.role))
            db.commit()
        return _visible(result, ctx)
    try:
        master_data.acquire_master_write_lock(db)
        condition = True if all_parts else (DimPart.id.in_(list(ids)) | DimPart.pn_std.in_(list(pns)))
        list(db.scalars(select(DimPart).where(condition).order_by(DimPart.id).with_for_update()
                        .execution_options(populate_existing=True)))
        if record is not None:
            model, record_id = record
            db.scalar(select(model).where(model.id == record_id).with_for_update().execution_options(populate_existing=True))
        actor = service.lock_actor(db, identity)
        accesses = service.read_accesses(db, identity, requirements, locked_user=actor)
        for res, actions in requirements.items():
            for required_action in actions:
                accesses.require(res, required_action)
        ctx = service.field_context(accesses.require(resource, action), legacy_context)
        result = operation(False, identity.username)
        if bulk_audit:
            db.add(SysAuditLog(entity_type='governance', entity_id=0, action=bulk_audit,
                               after_json=result, operated_by=identity.username))
        result = _visible(result, ctx)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def refresh(db, identity, *, legacy_context, with_candidates=True):
    def operation(commit, operator):
        result = _invoke(master_data.refresh, commit, db)
        if with_candidates:
            try:
                result['candidates'] = _invoke(match_candidates.generate, commit, db)
            except Exception as exc:
                if commit:
                    db.rollback()
                    result['candidates'] = {'status': 'failed', 'error_type': type(exc).__name__}
                    db.add(SysAuditLog(entity_type='governance', entity_id=0, action='refresh',
                                       after_json=result, operated_by=operator))
                    db.commit()
                raise
        return result
    return _write(db, identity, legacy_context, 'governance', 'refresh', operation, all_parts=True, bulk_audit='refresh')


def classify_backfill(db, identity, *, legacy_context):
    return _write(db, identity, legacy_context, 'governance', 'classify',
        lambda commit, operator: _invoke(master_data.classify_backfill, commit, db), all_parts=True, bulk_audit='classify_backfill')


def exclude_parts(db, identity, *, legacy_context, pn_std, excluded=True, reason=None):
    return _write(db, identity, legacy_context, 'governance', 'exclude',
        lambda commit, operator: _invoke(governance.set_excluded, commit, db, pn_std, excluded, reason, operator), pns=[pn_std])


def resolve_governance_issue(db, identity, issue_id, *, legacy_context, action='resolved', note=None):
    return _write(db, identity, legacy_context, 'governance', 'review',
        lambda commit, operator: _invoke(master_data.resolve_issue, commit, db, issue_id, action, note, operator),
        record=(ProductDataQualityIssue, issue_id))


def rename_part(db, identity, *, legacy_context, pn_std, new_pn_std, reason=None):
    def operation(commit, operator):
        part = db.scalar(select(DimPart).where(DimPart.pn_std == pn_std))
        if part is None:
            return None
        return _invoke(part_rename.rename_part, commit, db, part.id, new_pn_std, reason, operator)
    return _write(db, identity, legacy_context, 'part_identity', 'rename', operation, pns=[pn_std,new_pn_std.strip()])


def review_alias(db, identity, alias_id, *, legacy_context, action, target_pn=None):
    master_data.acquire_master_write_lock(db)
    alias = db.scalar(select(PartAlias).where(PartAlias.id == alias_id)
                      .execution_options(populate_existing=True))
    return _write(db, identity, legacy_context, 'part_identity', 'review_alias',
        lambda commit, operator: _invoke(master_data.review_alias, commit, db, alias_id, action, target_pn, operator),
        ids=[alias.part_id] if alias and alias.part_id else [], pns=[target_pn] if target_pn else [], record=(PartAlias,alias_id))


def confirm_parts(db, identity, *, legacy_context, pn_stds=None, scope=None):
    # Resolve set after the same master lock as every canonical identity change.
    master_data.acquire_master_write_lock(db)
    if pn_stds:
        ids = list(db.scalars(select(DimPart.id).where(DimPart.pn_std.in_(pn_stds))))
    elif scope == 'no_candidates':
        ids = match_candidates.parts_without_candidates(db)
    else:
        raise HTTPException(400, '需提供 pn_stds 或 scope=no_candidates')
    return _write(db, identity, legacy_context, 'part_identity', 'confirm',
        lambda commit, operator: _invoke(match_candidates.confirm_independent, commit, db, ids, operator), ids=ids)


def review_candidate(db, identity, candidate_id, *, legacy_context, action, reason=None):
    master_data.acquire_master_write_lock(db)
    candidate = db.get(ProductMatchCandidate, candidate_id)
    ids = [value for value in (candidate.source_part_id, candidate.candidate_part_id) if value] if candidate else []
    requirements = {'governance': ('review',)}
    if action in ('merge', 'independent'):
        requirements['part_identity'] = ('merge' if action == 'merge' else 'confirm',)
    return _write(db, identity, legacy_context, 'governance', 'review',
        lambda commit, operator: _invoke(match_candidates.review, commit, db, candidate_id, action, reason, operator),
        ids=ids, record=(ProductMatchCandidate,candidate_id), requirements=requirements)


def _post_merge(db, result, recompute, canonical=False):
    if recompute:
        from app.services import profit, inventory, maintenance_cost
        try:
            result['recompute'] = profit.recompute(db)
            result['inventory_costs'] = inventory.backfill_costs(db)
            result['maintenance_costs'] = maintenance_cost.recompute(db)
            if canonical:
                for key in ('recompute', 'inventory_costs', 'maintenance_costs'):
                    result.pop(key, None)
                result['recomputed'] = True
        except Exception:
            _log.exception('post-identity recompute failed')
            db.rollback()
            if canonical:
                for key in ('recompute', 'inventory_costs', 'maintenance_costs'):
                    result.pop(key, None)
            result['recompute_failed'] = '治理操作已完成，但后置重算失败，请联系管理员处理'
    return result


def merge_parts(db, identity, *, legacy_context, source_pn, target_pn, reason=None, recompute=True):
    canonical = runtime.is_authoritative(db, 'part_identity')
    result = _write(db, identity, legacy_context, 'part_identity', 'merge',
        lambda commit, operator: _invoke(merge.merge_parts, commit, db, source_pn, target_pn, reason, operator), pns=[source_pn,target_pn])
    result = _post_merge(db, result, recompute, canonical)
    return result if canonical else _visible(result, legacy_context)


def unmerge_parts(db, identity, *, legacy_context, merge_log_id, recompute=True):
    master_data.acquire_master_write_lock(db)
    log = db.get(ProductMergeLog, merge_log_id)
    ids = [log.source_part_id, log.target_part_id] if log else []
    canonical = runtime.is_authoritative(db, 'part_identity')
    result = _write(db, identity, legacy_context, 'part_identity', 'unmerge',
        lambda commit, operator: _invoke(merge.unmerge, commit, db, merge_log_id, operator), ids=ids)
    result = _post_merge(db, result, recompute, canonical)
    return result if canonical else _visible(result, legacy_context)


def _quality_visible(data, ctx, detail=False):
    restricted = is_field_hidden(ctx, 'unit_price')
    data['price_restricted'] = restricted
    data.pop('source_fingerprint', None)
    if detail:
        data['evidence_restricted'] = data['review_note_restricted'] = restricted
        if restricted:
            data['evidence'] = data['review_note'] = None
            for entry in data.get('audit', []):
                entry['before'] = entry['after'] = entry['reason'] = None
    return apply_field_visibility(data, ctx)


def _quality_reads(db, identity, legacy_context):
    if not runtime.is_authoritative(db, 'data_quality'):
        ctx = _ctx(db, identity, legacy_context, 'data_quality', 'read_purchase_issues')
        return ctx, {'purchase'} if is_scoped_sales(ctx) else {'purchase','sales'}
    accesses = service.read_accesses(db, identity, {'data_quality': ('read_purchase_issues','read_sales_issues')})
    sides = {side for side in ('purchase','sales') if accesses.get('data_quality',f'read_{side}_issues') is not None}
    if not sides:
        accesses.require('data_quality','read_purchase_issues')
    access = accesses.require('data_quality',f'read_{sorted(sides)[0]}_issues')
    return service.field_context(access, legacy_context), sides


def read_quality_issues(db, identity, *, legacy_context, side=None, **filters):
    ctx, sides = _quality_reads(db, identity, legacy_context)
    if side is not None and side not in sides and runtime.is_authoritative(db, 'data_quality'):
        raise HTTPException(403, '未获得该侧疑点读取权限')
    result = data_quality.list_issues(db, side=side, allowed_sides=sides, **filters)
    return _quality_visible(result, ctx)


def read_quality_issue(db, identity, issue_id, *, legacy_context):
    ctx, sides = _quality_reads(db, identity, legacy_context)
    result = data_quality.get_issue(db, issue_id, allowed_sides=sides)
    return _quality_visible(result, ctx, True) if result is not None else None


def _quality_write(db, identity, legacy_context, issue_id, operation, **values):
    canonical = runtime.is_authoritative(db, 'data_quality')
    if not canonical:
        ctx = _ctx(db, identity, legacy_context, 'data_quality', 'review')
        require_action('action_data_quality_review', require_data='data_purchase_cost')(ctx)
        operation(db, issue_id=issue_id, operated_by=ctx.user_id or ctx.role, allow_sales=not is_scoped_sales(ctx), **values)
        return _quality_visible(data_quality.get_issue(db,issue_id,allow_sales=not is_scoped_sales(ctx)),ctx,True)
    field_ctx = None
    def authorize(issue):
        nonlocal field_ctx
        actor = service.lock_actor(db, identity)
        accesses = service.read_accesses(db, identity, {'data_quality': ('review', f'read_{issue.side}_issues')}, locked_user=actor)
        accesses.require('data_quality',f'read_{issue.side}_issues')
        field_ctx = service.field_context(accesses.require('data_quality','review'), legacy_context)
    try:
        operation(db, issue_id=issue_id, operated_by=identity.username, _authorize=authorize, _commit=False, **values)
        result = _quality_visible(data_quality.get_issue(db,issue_id),field_ctx,True)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def decide_quality_issue(db, identity, issue_id, *, legacy_context, **values):
    return _quality_write(db, identity, legacy_context, issue_id, data_quality.decide_issue, **values)


def reopen_quality_issue(db, identity, issue_id, *, legacy_context, **values):
    return _quality_write(db, identity, legacy_context, issue_id, data_quality.reopen_issue, **values)


def preview_quality_calibration(db, identity, *, legacy_context, **filters):
    ctx = _ctx(db, identity, legacy_context, 'data_quality_calibration', 'preview')
    if is_field_hidden(ctx,'unit_price'):
        raise HTTPException(403, '无权查看采购成本校准预览')
    return _visible(data_quality_calibration.purchase_price_preview(db, **filters),ctx)
