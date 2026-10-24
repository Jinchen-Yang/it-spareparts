"""Complete master-workbook contracts, selected without mixing scopes or fields."""
import re
from decimal import Decimal
from app.services import maintenance_expense_collection_workbook as ec
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract

from app.authz import service, runtime
from app.authz.errors import authz_error
from app.authz.context import read_snapshot
from app.authz.context_registry import ENTRY_CONTRACTS
from app.config import get_settings
from app.services import maintenance_authorization as foundation
from app.services import maintenance_project_master_workbook as master
from app.services.maintenance_workflow_authorization import require_complete

DOWNLOAD = 'GET /api/maintenance/projects/stable/{project_id}/master-workbook.xlsx'
RESOURCES = ('maintenance_managed_workbook', 'maintenance_general_workbook')


def complete_alternative(db, identity, *, entry_id, params, proof, branches=None):
    """Evaluate every registered alternative in the same observation.

    A PDP/configuration error propagates even if another branch would allow.
    The returned set is one complete contract, never a union of partial grants.
    """
    candidates = []
    rules = ENTRY_CONTRACTS[entry_id]['branches']
    if entry_id != DOWNLOAD and branches is None:
        raise ValueError('条件命令必须先由实际输入选择完整合同')
    rules = {name: rule for name, rule in rules.items() if branches is None or name in branches}
    states = runtime.runtime_states(db)
    resources = {resource for rule in rules.values() for resource in rule['requirements']}
    if any(states[resource].runtime_state == 'suspended' for resource in resources):
        raise authz_error(503, 'AUTHZ_RESOURCE_SUSPENDED', '完整工作簿授权资源已暂停')
    for branch, rule in rules.items():
        if any(states[resource].runtime_state == 'legacy' for resource in rule['requirements']):
            continue
        batch = foundation.resolve_entry_context(db, identity, entry_id=entry_id,
            params=params, branch=branch, read_snapshot=proof)
        if all(context.ref.kind == 'collection' for context in batch.contexts):
            principal, _ = foundation.resolve_project_subjects(db, identity, [], read_snapshot=proof)
            bindings = {(context.ref.key, resource): foundation.query_bindings(db, principal, context, resource)
                for context in batch.contexts for resource in context.requirements}
            candidates.append(service.context_query_accesses(db, identity, batch, bindings, read_snapshot=proof))
        else:
            candidates.append(service.context_accesses(db, identity, batch, read_snapshot=proof))
    if not candidates:
        raise authz_error(503, "AUTHZ_RESOURCE_SUSPENDED", "完整工作簿授权资源尚未启用")
    for accesses in candidates:
        if all(accesses.get(context.ref, resource, action) is not None
               for context in accesses.batch.contexts
               for resource, actions in context.requirements.items() for action in actions):
            return accesses
    return candidates[0]


def read_master_workbook(session_factory, identity, *, project_id, sheets=None, authorization_view=False, authorization_precondition=None):
    with read_snapshot(session_factory) as (db, proof):
        accesses = complete_alternative(db, identity, entry_id=DOWNLOAD,
            params={'project_id': project_id, 'sheets': sheets}, proof=proof)
        ref = accesses.batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(authorization_precondition, accesses, ref)
        wanted = tuple(s.strip() for s in sheets.split(',') if s.strip()) if sheets else master.ALL_SHEETS
        if get_settings().maintenance_project_master_v2_enabled:
            v2_names = {master.SHEET_BASICS: master.V2_SHEET_OVERVIEW,
                master.SHEET_OVERVIEW: master.V2_SHEET_PLAN, master.SHEET_PARTS: master.V2_SHEET_PARTS,
                master.SHEET_EXPENSE: master.V2_SHEET_EXPENSE, master.SHEET_COLLECTION: master.V2_SHEET_RECEIPTS,
                master.SHEET_SITE: master.V2_SHEET_SITE}
            content = master.build_project_master_v2(db, project_id=project_id,
                sheets=tuple(v2_names.get(name, name) for name in wanted), read_snapshot=proof)
        else:
            content = master.build_project_master(db, project_id=project_id, sheets=wanted)
        return content, workbook_filename(db, project_id, wanted)


def safe_filename_part(value: str) -> str:
    """清洗导出文件名片段：去掉路径与非法字符，避免项目名破坏文件名（2026-08-17）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value).strip().strip(".")
    return cleaned or "项目"


def workbook_filename(
    db: Session, project_id: str, wanted: tuple[str, ...]
) -> str:
    """下载文件名 = XSDD 销售订单号（取第一个）+ 维保项目名 + 表单类型（2026-08-17）。"""
    project = db.get(MaintenanceProject, project_id)
    contracts = list(db.execute(
        select(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_id == project_id)
        .order_by(MaintenanceProjectContract.effective_from)
    ).scalars())
    project_xsdds = master.project_sales_order_nos(db, project_id)
    xsdd = (contracts[0].contract_no if contracts else
            project_xsdds[0] if project_xsdds else
            project.project_code if project else project_id)
    name = safe_filename_part(project.display_name) if project else safe_filename_part(project_id)
    form = "总表" if len(wanted) == len(master.ALL_SHEETS) else (
        wanted[0] if len(wanted) == 1 else "多表单"
    )
    return f"{safe_filename_part(xsdd)}-{name}-{form}.xlsx"




def expense_rows(db, project_id, page, page_size):
    rows = ec.project_expenses(db, project_id)
    total = len(rows)
    window = rows[(page - 1) * page_size: page * page_size]
    return {
        "rows": [
            {
                "raw_line_id": expense.raw_line_id,
                "bxd_no": expense.bxd_no,
                "expense_date": (expense.expense_date.isoformat()
                                 if expense.expense_date else None),
                "person": expense.person,
                "expense_type": expense.expense_type,
                "fee_category": expense.fee_category,
                "reason": expense.reason,
                "contract_no": expense.linked_sales_order_no,
                "amount_ex_tax": (str(expense.amount_ex_tax)
                                  if expense.amount_ex_tax is not None else None),
                "amount_inc_tax": (str(expense.amount_inc_tax)
                                   if expense.amount_inc_tax is not None else None),
                "data_status": expense.data_status,
                "remark": expense.remark,
            }
            for expense in window
        ],
        "total": total, "page": page, "page_size": page_size,
    }



def collection_plan(db, project_id):
    from app.models.maintenance_manager import MaintenanceCollectionMilestone
    from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot
    from app.business_time import business_today

    contracts = master.ec._contracts(db, project_id)
    contract_by_id = {c.project_contract_id: c.contract_no for c in contracts}
    milestones = list(db.scalars(select(MaintenanceCollectionMilestone).where(
        MaintenanceCollectionMilestone.project_id == project_id,
        MaintenanceCollectionMilestone.is_active.is_(True),
    ).order_by(
        MaintenanceCollectionMilestone.project_contract_id,
        MaintenanceCollectionMilestone.sequence,
    )))
    # 每份合同最新 confirmed 快照的累计实收（待回款判定基准）
    actual: dict[str, Decimal] = {}
    for c in contracts:
        snap = db.scalar(select(MaintenanceCollectionSnapshot).where(
            MaintenanceCollectionSnapshot.project_contract_id == c.project_contract_id,
            MaintenanceCollectionSnapshot.status == "confirmed",
        ).order_by(MaintenanceCollectionSnapshot.report_month.desc()))
        actual[c.project_contract_id] = (
            snap.cumulative_amount if snap and snap.cumulative_amount is not None
            else Decimal("0"))
    today = business_today()
    rows = []
    cum_planned: dict[str, Decimal] = {}
    for m in milestones:
        cid = m.project_contract_id
        cum_planned[cid] = cum_planned.get(cid, Decimal("0")) + (m.planned_amount or Decimal("0"))
        cum_actual = actual.get(cid, Decimal("0"))
        if cum_actual >= cum_planned[cid] and cum_planned[cid] > 0:
            state = "paid"
        elif cum_actual > (cum_planned[cid] - (m.planned_amount or Decimal("0"))):
            state = "partial"
        else:
            state = "pending"
        if state != "paid" and m.planned_date is not None and m.planned_date < today:
            state = "overdue"
        rows.append({
            "milestone_id": m.milestone_id,
            "contract_no": contract_by_id.get(cid, ""),
            "sequence": m.sequence,
            "planned_date": m.planned_date.isoformat() if m.planned_date else None,
            "date_precision": m.date_precision,
            "planned_amount": str(m.planned_amount) if m.planned_amount is not None else None,
            "cumulative_planned": str(cum_planned[cid]),
            "cumulative_actual": str(actual.get(cid, Decimal("0"))),
            "arrival_state": state,
            "follow_up_status": m.follow_up_status,
            "note": m.follow_up_note,
            "version": m.version,
        })
    return {"total": len(rows), "rows": rows}



COLLECTION_PLAN = 'GET /api/maintenance/projects/stable/{project_id}/master-workbook/collection-plan'
EXPENSE_ROWS = 'GET /api/maintenance/projects/stable/{project_id}/expense-rows'


def read_project_data(session_factory, identity, *, entry_id, project_id, page=1, page_size=50, authorization_view=False, authorization_precondition=None):
    if entry_id not in (COLLECTION_PLAN, EXPENSE_ROWS):
        raise ValueError('未登记的项目表单读取入口')
    with foundation.read_context_snapshot(session_factory, identity, entry_id=entry_id,
            params={'project_id': project_id}) as (db, _, batch, accesses):
        ref = batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(authorization_precondition, accesses, ref)
        result = (collection_plan(db, project_id) if entry_id == COLLECTION_PLAN
                  else expense_rows(db, project_id, page, page_size))
        return {**result, 'authorization_context': accesses.wire(ref)}


MASTER_ROWS = 'GET /api/maintenance/projects/stable/{project_id}/master-workbook/rows'


def read_master_rows(session_factory, identity, *, project_id, sheet, order_no=None,
        contract_no=None, page=None, page_size=None, filters=None,
        authorization_view=False, authorization_precondition=None):
    params = {'project_id': project_id, 'sheet': sheet, 'order_no': order_no,
              'contract_no': contract_no, 'filters': filters}
    with foundation.read_context_snapshot(session_factory, identity,
            entry_id=MASTER_ROWS, params=params) as (db, proof, batch, accesses):
        ref = batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(authorization_precondition, accesses, ref)
        access = accesses.require(ref, 'maintenance_row_workbench', 'read_cost_rows')
        return {**build_master_rows(db, None, project_id=project_id, sheet=sheet,
            order_no=order_no, contract_no=contract_no, page=page, page_size=page_size,
            filters=filters, authorization_access=access),
            'authorization_context': accesses.wire(ref)}


def build_master_rows(db, ctx, *, project_id, sheet, order_no=None, contract_no=None,
        page=None, page_size=None, filters=None, authorization_access=None):
    from fastapi import HTTPException, status
    from app.models.maintenance import MaintenanceManualCostOverride
    from app.security import record_access_log
    from app.services import maintenance_row_workbench as row_workbench
    from app.services import maintenance_workbook_renderer
    from app.services.maintenance_row_filter_registry import parse_filters
    row_filters = parse_filters(filters)
    amount_allowed = row_workbench.can_view_amounts(db, ctx, project_id, authorization_access=authorization_access)
    if (get_settings().maintenance_project_master_v2_enabled
            and sheet in {master.V2_SHEET_PARTS, master.SHEET_PARTS}):
        rows, total, page_size = row_workbench.project_page(
            db, ctx, project_id=project_id, filters=row_filters,
            order_no=order_no, contract_no=contract_no,
            page=page, page_size=page_size, authorization_access=authorization_access)
        line_ids = [line.id for line, _order, _pid in rows]
        overrides = {
            item.line_id: item for item in db.scalars(select(MaintenanceManualCostOverride).where(
                MaintenanceManualCostOverride.line_id.in_(line_ids),
                MaintenanceManualCostOverride.active.is_(True),
            ))
        } if line_ids else {}

        def _v2_row(line, order):
            override = overrides.get(line.id)
            inc = master._line_cost_evidence(line, override, basis="inc")
            ex = master._line_cost_evidence(line, override, basis="ex")
            known = inc["tier"] != "missing"
            resolved_source = inc["source"] if known else (line.cost_source or "none")
            can_refill = (
                not known
                and line.cost_source in (None, "none")
                and override is None
            )
            missing_kind = None
            if not known:
                missing_kind = (
                    "out_of_scope"
                    if line.cost_source is None
                    else "none"
                    if line.cost_source == "none"
                    else "invalid_cost_fact"
                )
            return {
                "line_id": line.id,
                "part_id": line.part_id,
                "order_no": order.order_no,
                "order_date": order.order_date.isoformat() if order.order_date else None,
                "sales_order_no": order.linked_sales_order_no or "",
                "pn_std": line.pn_std or line.pn_raw or "",
                "description": line.description or "",
                "qty": str(line.qty) if line.qty is not None else None,
                "return_qty": str(line.return_qty) if line.return_qty is not None else None,
                "returned_qty": (
                    str(line.returned_qty) if line.returned_qty is not None else None
                ),
                "pending_return_qty": (
                    str(line.pending_return_qty)
                    if line.pending_return_qty is not None else None
                ),
                "cost_amount_inc_tax": (
                    str(inc["amount"]) if inc["amount"] is not None else None
                ),
                "warehouse": order.warehouse or "",
                "cost_source": resolved_source,
                "cost_source_label": maintenance_workbook_renderer.SOURCE_LABELS.get(
                    resolved_source, "成本事实异常" if not known else "成本缺失"
                ),
                "confidence": (
                    "high" if resolved_source == "manual"
                    else line.confidence if known
                    else "none"
                ),
                "unit_cost_ex_tax": (
                    str(ex["unit_cost"]) if ex["unit_cost"] is not None else None
                ),
                "unit_cost_inc_tax": (
                    str(inc["unit_cost"]) if inc["unit_cost"] is not None else None
                ),
                "missing_kind": missing_kind,
                "can_refill": can_refill,
                "manual_unit_cost_ex_tax": (
                    str(override.unit_cost_ex_tax) if override else None
                ),
                "manual_reason": override.reason if override else None,
            }

        return {
            "sheet": sheet,
            "total": total,
            "page": page,
            "page_size": page_size,
            "rows": row_workbench.redact_project_rows(
                [_v2_row(line, order) for line, order, _pid in rows], allowed=amount_allowed),
        }
    if sheet not in {master.SHEET_PARTS}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "unsupported_sheet",
             "message": "当前仅支持 03_备件订单 行级查询"},
        )
    rows, total, page_size = row_workbench.project_page(
        db, ctx, project_id=project_id, filters=row_filters,
        order_no=order_no, contract_no=contract_no,
        page=page, page_size=page_size, authorization_access=authorization_access)
    line_ids = [line.id for line, _order, _pid in rows]
    overrides: dict[int, MaintenanceManualCostOverride] = {}
    if line_ids:
        for override in db.execute(
            select(MaintenanceManualCostOverride)
            .where(
                MaintenanceManualCostOverride.line_id.in_(line_ids),
                MaintenanceManualCostOverride.active.is_(True),
            )
        ).scalars():
            overrides[override.line_id] = override
    if authorization_access is None:
        record_access_log(ctx, "download", "maintenance_project_master_rows",
                          {"project_id": project_id, "sheet": sheet})

    def _legacy_row(line, order):
        override = overrides.get(line.id)
        inc = master._line_cost_evidence(line, override, basis="inc")
        ex = master._line_cost_evidence(line, override, basis="ex")
        return {
            "line_id": line.id,
            "order_no": order.order_no,
            "order_date": order.order_date.isoformat() if order.order_date else None,
            "sales_order_no": order.linked_sales_order_no or "",
            "project_raw": order.project_raw or "",
            "pn_std": line.pn_std or line.pn_raw or "",
            "description": line.description or "",
            "qty": float(line.qty) if line.qty is not None else None,
            "return_qty": float(line.return_qty) if line.return_qty is not None else None,
            "returned_qty": (
                float(line.returned_qty) if line.returned_qty is not None else None
            ),
            "pending_return_qty": (
                float(line.pending_return_qty)
                if line.pending_return_qty is not None else None
            ),
            "serial_numbers": line.serial_numbers or "",
            "warehouse": order.warehouse or "",
            "cost_source": inc["source"] if inc["tier"] != "missing" else "",
            "unit_cost_ex_tax": (
                float(ex["unit_cost"]) if ex["unit_cost"] is not None else None
            ),
            "unit_cost_inc_tax": (
                float(inc["unit_cost"]) if inc["unit_cost"] is not None else None
            ),
            "cost_amount_inc_tax": (
                float(inc["amount"]) if inc["amount"] is not None else None
            ),
            "cost_quality": inc["tier"],
            "change_reason": override.reason or "" if override is not None else "",
        }

    return {
        "sheet": sheet,
        "total": total,
        "page": page,
        "page_size": page_size,
        "rows": row_workbench.redact_project_rows(
            [_legacy_row(line, order) for line, order, _pid in rows], allowed=amount_allowed),
    }



GLOBAL_DOWNLOAD = 'GET /api/maintenance/spare-part-lines.xlsx'


def read_global_workbook(session_factory, identity, *, preset, date_from=None, date_to=None,
        authorization_view=False, authorization_precondition=None):
    with foundation.read_context_snapshot(session_factory, identity, entry_id=GLOBAL_DOWNLOAD,
            params={'range': preset, 'from': date_from, 'to': date_to}) as (db, proof, batch, accesses):
        ref = batch.contexts[0].ref
        if authorization_view:
            return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(authorization_precondition, accesses, ref)
        access = accesses.require(ref, 'maintenance_global_workbook', 'download')
        allowed = select(MaintenanceProject.project_id).where(foundation.project_condition(access))
        return master.build_global_lines(db, preset=preset, date_from=date_from, date_to=date_to,
            allowed_project_ids=allowed)
