"""V2 workbook application transaction.

Owns the existing lock sequence, writes, first receipt and commit. Nested
writers participate in the same transaction; parsing and HTTP authorization
remain at their existing boundaries.
"""
from __future__ import annotations

import hashlib
import logging
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app import config
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder, FProjectExpense, MaintenanceManualCostOverride
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceProjectExpenseAttribution, MaintenanceProjectOperationAudit, MaintenanceProjectWorkbookOperation, MaintenanceSiteIssue, MaintenanceSiteIssueLine
from app.models.system import SysImportBatch
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.security import UserContext
from app.services import maintenance_project_identity
from app.services import maintenance_field_protection as field_protection
from app.services import maintenance_site_issue_merge as site_merges
from app.services import maintenance_expense_collection_workbook as ec
from app.services.maintenance_collection_milestones import write_collection_milestone
from app.services.maintenance_site_issue_facts import LegacySiteIssueEditContext, apply_locked_site_issue_edits
from app.services.maintenance_workbook_protocol import V2_SHEET_RECEIPTS
from app.services.maintenance_workbook_plans import MasterV2Plan, WorkbookError
from app.services.maintenance_workbook_values import _v2_auto_site_prefix
from app.services.maintenance_workbook_queries import _v2_editable_contracts
from app.services.maintenance_workbook_scope import _v2_assert_entity_scope, _v2_recheck_row_guards
from app.services.maintenance_workbook_cost_writes import _merge_manual_cost_to_line, _recompute_line_amounts
from app.services.maintenance_workbook_audit import _write_audit
from app.services.maintenance_workbook_site_identity import _v2_duplicate_issue_no, _v2_check_site_issue_numbers
from app.services.maintenance_workbook_fact_writes import _ensure_contract_for_xsdd_apply, _cascade_void_site_lines, _v2_allocate_site_display_no
from app.services.maintenance_workbook_receipts import (
    _v2_apply_operation_key, _v2_apply_result, _v2_stored_apply_result,
)

logger = logging.getLogger(__name__)


def apply_project_master_v2(
    db: Session,
    plan: MasterV2Plan,
    *,
    operated_by: str,
    import_batch_id: str,
    user_ctx: UserContext | None = None,
) -> dict:
    # 本服务拥有整本事务及 commit；所有写入/flush（含下游原语与回执）
    # 的约束失败都必须回滚，不能把 failed Session 或数据库 500 留给调用方。
    try:
        return _apply_project_master_v2(
            db, plan, operated_by=operated_by, import_batch_id=import_batch_id,
            user_ctx=user_ctx,
        )
    except IntegrityError as exc:
        db.rollback()
        logger.warning("项目总表应用发生数据库约束冲突，整本已回滚", exc_info=True)
        diag = getattr(exc.orig, "diag", None)
        constraint = getattr(diag, "constraint_name", None)
        if constraint == "uq_maintenance_site_issue_project_no":
            # 只把本计划已有的单号带给用户，不透出数据库 SQL/参数。
            detail = getattr(diag, "message_detail", "") or ""
            matching = [flag for flag in plan.site_flags
                        if flag.issue_no and f", {flag.issue_no})" in detail]
            if matching:
                raise _v2_duplicate_issue_no(matching[0].issue_no, matching) from exc
            raise WorkbookError(
                "duplicate_issue_no",
                "领用单号与本项目其他领用单重复，本次上传未写入任何数据；"
                "请核对单号或重新下载总表后重试",
            ) from exc
        raise WorkbookError(
            "apply_conflict",
            "应用项目总表时数据发生冲突，本次上传未写入任何数据；请重新下载总表后重试",
        ) from exc


def _apply_project_master_v2(
    db: Session,
    plan: MasterV2Plan,
    *,
    operated_by: str,
    import_batch_id: str,
    user_ctx: UserContext | None = None,
) -> dict:
    # All mutations deliberately happen on this one Session transaction.
    apply_operation_key = _v2_apply_operation_key(plan, operated_by)
    if apply_operation_key is not None:
        # The caller creates a fresh request UUID for every HTTP attempt.  A
        # deterministic server batch identity makes an ACK-loss replay return
        # the same observable result and keeps downstream import references
        # stable as well.
        import_batch_id = (
            "v2-" + hashlib.sha256(
                apply_operation_key.encode("utf-8")
            ).hexdigest()[:36]
        )
    audit_reason = f"项目总表应用 {import_batch_id[:8]}"
    from app.services import maintenance_project_operations as operations
    operating_fact_changed = False

    # Global write lock order for the complete workbook transaction:
    # data-change advisory -> workbook states(sorted) -> project -> source
    # order -> assignment -> detail facts.  Import/recompute already take the
    # advisory first; doing the same here prevents state->global inversion when
    # this apply later cascades a demand tombstone or recomputes line costs.
    db.execute(
        select(func.pg_advisory_xact_lock(config.DATA_CHANGE_ADVISORY_LOCK_KEY))
    )
    # 取锁之后必须清掉 identity map：validate_* 全程在这把锁之外读服务端值，
    # 并把读到的每一行留在了本 Session 里。不 expire，apply 侧的 db.get() 会
    # 命中旧对象一条 SQL 都不发，SELECT ... FOR UPDATE 也不会覆盖已加载属性
    # ——于是「加锁后复检版本号」变成拿陈旧值跟陈旧值比，永远判「没变」。
    # 实测：同一事务里原生 SQL FOR UPDATE 读到 (version=2, amount=99999)，
    # ORM 读到 (1, 10000)，且返回的就是 validate 期那个对象。
    # 安全前提：validate_* 只读（无 db.add/flush/commit/属性赋值），
    # SessionLocal 是 autoflush=False（app/db.py），此处无待写改动可丢。
    #
    # 有了这一句，锁内**第一次**读到的就是真实行，因此除「取锁阶段」的加锁
    # 读之外，一律不要再加 populate_existing：autoflush=False 下，写阶段的
    # 重读若 repopulate，会把尚未 flush 的改动直接冲掉（同一行既改数量又填
    # 人工成本时，数量会被静默丢弃而成本照常提交 —— Codex P1，2026-09-04，
    # 已有回归用例守着）。持锁之后没人能在我们脚下改数据，一次新鲜读就够。
    db.expire_all()
    project_exists = db.scalar(
        select(MaintenanceProject.project_id).where(
            MaintenanceProject.project_id == plan.project_id)
    )
    if project_exists is None:
        raise WorkbookError("project_not_editable", "项目已不存在，请重新下载")
    state_project_ids = {plan.project_id}
    assignment_source_ids = {
        change.source_order_id for change in plan.assignment_changes
    }
    prestate_contract_nos = {
        change.contract_no for change in plan.milestone_changes
    } | {
        operation.contract_no for operation in plan.receipt_ops
    } | {
        update.contract_no for update in plan.expense_updates
        if update.contract_no
    }
    prestate_assignment_xsdds = list(db.scalars(
        select(FMaintenanceOrder.linked_sales_order_no).where(
            FMaintenanceOrder.raw_order_id.in_(assignment_source_ids or {""}),
            FMaintenanceOrder.linked_sales_order_no.is_not(None),
        )
    ))
    # VOID is identity-bound, so its contract comes from the raw fact, never
    # from an editable workbook cell.  This unlocked pre-read only defines the
    # advisory-lock envelope; the value is re-read after the expense row lock.
    prelock_void_contract_nos = {
        str(value)
        for value in db.scalars(
            select(FProjectExpense.linked_sales_order_no).where(
                FProjectExpense.raw_line_id.in_(plan.expense_voids),
                FProjectExpense.linked_sales_order_no.is_not(None),
            )
        ).all()
        if value
    } if plan.expense_voids else set()
    # XSDD advisory identities are absent-row locks and therefore precede every
    # workbook/project row lock.  Later contract/assignment triggers reacquire
    # the same transaction locks without creating a state -> identity inversion.
    maintenance_project_identity.lock_xsdd_identities(
        db,
        prestate_contract_nos
        | set(prestate_assignment_xsdds)
        | prelock_void_contract_nos,
    )
    if assignment_source_ids:
        state_project_ids.update(
            value
            for value in db.scalars(
                select(MaintenanceSourceOrderAssignment.project_id).where(
                    MaintenanceSourceOrderAssignment.source_order_id.in_(
                        assignment_source_ids
                    ),
                    MaintenanceSourceOrderAssignment.is_active.is_(True),
                )
            ).all()
            if value
        )
    void_line_ids = {
        int(refill.line_id)
        for refill in plan.cost_refills
        if (
            refill.operation == "VOID"
            and refill.line_id is not None
            and not refill.is_create
        )
    }
    if void_line_ids:
        # A line-level VOID may cascade into an order tombstone and deactivate
        # its active assignment.  Include every current owner in the same
        # initial sorted state-lock pass; the cascade service only reuses this
        # map and fails closed if ownership changes afterwards.
        state_project_ids.update(
            value
            for value in db.scalars(
                select(MaintenanceSourceOrderAssignment.project_id)
                .join(
                    FMaintenanceOrder,
                    FMaintenanceOrder.raw_order_id
                    == MaintenanceSourceOrderAssignment.source_order_id,
                )
                .join(
                    FMaintenanceLine,
                    FMaintenanceLine.order_id == FMaintenanceOrder.id,
                )
                .where(
                    FMaintenanceLine.id.in_(sorted(void_line_ids)),
                    MaintenanceSourceOrderAssignment.is_active.is_(True),
                )
            ).all()
            if value
        )
    workbook_states = operations.lock_workbook_states(
        db,
        project_ids=state_project_ids,
    )
    workbook_state = workbook_states[plan.project_id]
    project = db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == plan.project_id)
        .with_for_update()
    )
    if project is None or not project.is_active:
        raise WorkbookError("project_not_editable", "项目已不存在或归档，请重新下载")
    if apply_operation_key is not None:
        # SELECT ... FOR UPDATE cannot lock an absent unique key.  Serialize
        # the deterministic receipt identity first so concurrent first applies
        # become writer + replay instead of two writers racing at commit.
        db.execute(select(func.pg_advisory_xact_lock(
            func.hashtextextended(
                f"maintenance-master-v2-apply:{apply_operation_key}", 0
            )
        )))
        receipt = db.scalar(
            select(MaintenanceProjectWorkbookOperation)
            .where(
                MaintenanceProjectWorkbookOperation.operation_key
                == apply_operation_key
            )
            .with_for_update()
        )
        if receipt is not None:
            if (
                receipt.project_id != plan.project_id
                or receipt.export_id != plan.export_id
                or receipt.file_sha256 != plan.file_sha256
                or receipt.payload_hash != plan.file_sha256
                or receipt.operated_by != operated_by
                or receipt.operation_type != "file_apply"
            ):
                raise WorkbookError(
                    "idempotency_conflict",
                    "工作簿应用回执与当前请求不一致，请重新下载",
                )
            stored_result = _v2_stored_apply_result(db, receipt)
            db.commit()
            if stored_result is not None:
                return stored_result[0]
            return _v2_apply_result(
                plan,
                operated_by=operated_by,
                import_batch_id=import_batch_id,
                replayed=True,
            )  # replay：以原回执语义返回，冲突字段为空
    carries_export_identity = bool(plan.export_id or plan.file_sha256)
    if carries_export_identity and (
        plan.expected_workbook_revision is None
        or not plan.expected_workbook_data_version
    ):
        raise WorkbookError(
            "invalid_concurrency_token",
            "工作簿缺少项目并发版本，请重新下载当前项目总表。",
        )
    # 2.7.0：整本 revision 不再硬拒（行级基线三路合并已区分用户改动与
    # 导出旧值）；漂移仅记录，供回执与审计观测。
    revision_drift = bool(
        plan.expected_workbook_revision is not None
        and (
            workbook_state.revision != plan.expected_workbook_revision
            or workbook_state.data_version != plan.expected_workbook_data_version
        )
    )
    if plan.conflicts and not plan.force_takeover:
        raise WorkbookError(
            "row_conflicts",
            "部分行已被他人更新，本次上传未写入任何数据；请处理后重试或选择强制接管。",
            issues=[dict(item) for item in plan.conflicts],
        )
    plan_contract_nos = prestate_contract_nos
    prelock_contract_nos = plan_contract_nos | prelock_void_contract_nos
    # The plan may have been validated minutes earlier.  Rebuild all live
    # ownership sets and lock the target facts before *any* contract, demand,
    # site, or expense write, so a stale/crafted plan fails as one zero-write
    # transaction.
    assigned_xsdd_nos = _v2_assert_entity_scope(
        db,
        project_id=plan.project_id,
        part_ids={
            int(refill.line_id)
            for refill in plan.cost_refills
            if not refill.is_create and refill.line_id is not None
        },
        site_ids={
            flag.issue_line_id for flag in plan.site_flags if not flag.is_create
        },
        expense_ids=(
            {
                update.raw_line_id
                for update in plan.expense_updates
                if not getattr(update, "is_create", False)
            }
            | set(plan.expense_voids)
        ),
        linked_sales_order_nos=prelock_contract_nos,
        lock_rows=True,
        lock_site_merge_scope=bool(plan.site_flags or plan.site_issue_merges),
    )
    # 行锁已在手且读的是新鲜值：此刻复核 validate 时刻的行指纹。必须在任何写
    # 之前 —— 冲突时整本零写入，由调用方 rollback。
    _v2_recheck_row_guards(db, plan)
    requested_headers = _v2_check_site_issue_numbers(db, plan.project_id, plan.site_flags)
    participant_ids = {source["issue_id"] for item in plan.site_issue_merges for source in item["sources"]}
    site_merges.references.snapshot_references(db, participant_ids, lock=True)
    current_merges = site_merges.plan_merges(db, plan.project_id, requested_headers, plan.site_flags,
        intents=plan.site_header_intents, issue_rows=plan.site_issue_rows)
    if current_merges != plan.site_issue_merges:
        raise WorkbookError("stale_site_issue_merge",
            "领用单合并对象、明细或单头已变化，本次未写入；请重新校验并核对合并计划后再应用")
    if plan.project_note_change is not None:
        from app.services import maintenance_project_catalog as catalog

        change = plan.project_note_change
        try:
            # Reuse the page command before any other workbook mutations: it
            # refreshes preloaded ORM state and performs version/audit/bump.
            catalog.update_project(
                db, project_id=plan.project_id, version=change.base_version,
                updates={"project_note": change.value}, reason=audit_reason,
                operated_by=operated_by,
            )
        except catalog.MaintenanceProjectCatalogConflict as exc:
            raise WorkbookError(
                "stale_project_note",
                "项目备注所依据的项目版本已被他人修改，本次上传未写入任何数据；"
                "请保留文件草稿，重新下载项目总表核对后再上传。",
            ) from exc
        except catalog.MaintenanceProjectCatalogError as exc:
            raise WorkbookError("invalid_project_note", str(exc)) from exc
    locked_void_contract_nos = {
        str(value)
        for value in db.scalars(
            select(FProjectExpense.linked_sales_order_no).where(
                FProjectExpense.raw_line_id.in_(plan.expense_voids),
                FProjectExpense.linked_sales_order_no.is_not(None),
            )
        ).all()
        if value
    } if plan.expense_voids else set()
    if not locked_void_contract_nos.issubset(prelock_void_contract_nos):
        raise WorkbookError(
            "expense_not_in_project",
            "报销归集合同在应用前已变化，请重新下载",
        )
    needed_contract_nos = sorted(plan_contract_nos | locked_void_contract_nos)
    current_contract_by_no: dict[str, MaintenanceProjectContract] = {}
    for contract_no in needed_contract_nos:
        contract, contract_created = _ensure_contract_for_xsdd_apply(
            db,
            project=project,
            contract_no=contract_no,
            operated_by=operated_by,
            assigned_xsdd_nos=assigned_xsdd_nos,
        )
        current_contract_by_no[contract_no] = contract
        operating_fact_changed = contract_created or operating_fact_changed
    receipt_ops = tuple(
        ec.CollectionOp(
            operation=operation.operation,
            project_contract_id=current_contract_by_no[
                operation.contract_no
            ].project_contract_id,
            contract_no=operation.contract_no,
            report_month=operation.report_month,
            cumulative_amount=operation.cumulative_amount,
            receipt_reference=operation.receipt_reference,
            remark=operation.remark,
            collection_status=operation.collection_status,
        )
        for operation in plan.receipt_ops
    )
    if plan.contract_amount_change is not None:
        change = plan.contract_amount_change
        # Direct contract create/update also takes the state and project locks.
        # Therefore no second current contract can appear after this recheck.
        contracts = list(db.scalars(
            select(MaintenanceProjectContract)
            .where(MaintenanceProjectContract.project_id == plan.project_id)
            .order_by(MaintenanceProjectContract.project_contract_id)
            .with_for_update()
        ))
        editable_contracts = _v2_editable_contracts(db, plan.project_id, contracts)
        if (len(editable_contracts) != 1
                or editable_contracts[0].project_contract_id
                != change.project_contract_id):
            raise WorkbookError(
                "contract_total_ambiguous",
                "合同关系已变化，当前项目不再只有一条生效计入合同，请重新下载",
            )
        contract = editable_contracts[0]
        if contract.version != change.base_version:
            raise WorkbookError(
                "stale_contract",
                f"合同金额已被他人更新（当前版本 {contract.version}），请重新下载后再改",
            )
        before_amount = contract.amount_inc_tax
        if before_amount != change.amount_inc_tax:
            before = operations.contract_dict(contract)
            contract.amount_inc_tax = change.amount_inc_tax
            contract.source = "project_master_workbook"
            contract.version += 1
            operations._audit_contract(
                db,
                contract,
                action="workbook_update_amount",
                operated_by=operated_by,
                reason=audit_reason,
                before=before,
                after=operations.contract_dict(contract),
            )
            operating_fact_changed = True
    if plan.assignment_changes:
        if user_ctx is None:
            raise WorkbookError(
                "assignment_permission_denied",
                "本次回传包含 WBDD 项目归属更正，缺少可审计的登录身份")
        from app.services import maintenance_source_assignments as source_assignments

        assignment_changed_project_ids: set[str] = set()
        try:
            source_assignments.assign_source_orders(
                db,
                project_id=plan.project_id,
                items=[{
                    "source_order_id": change.source_order_id,
                    "expected_assignment_id": change.expected_assignment_id,
                    "expected_version": change.expected_version,
                } for change in plan.assignment_changes],
                reason=f"项目总表人工认证更正 WBDD 归属（{import_batch_id[:8]}）",
                operated_by=operated_by,
                user_ctx=user_ctx,
                _prelocked_states=workbook_states,
                _changed_project_ids=assignment_changed_project_ids,
            )
            operating_fact_changed = (
                bool(assignment_changed_project_ids)
                or operating_fact_changed
            )
        except source_assignments.SourceAssignmentConflict as exc:
            raise WorkbookError("stale_assignment", str(exc)) from exc
        except source_assignments.SourceAssignmentPermissionError as exc:
            raise WorkbookError("assignment_permission_denied", str(exc)) from exc
        except source_assignments.SourceAssignmentError as exc:
            raise WorkbookError("assignment_invalid", str(exc)) from exc
    # 手工新增行需要一个 import_batch（NOT NULL FK）
    manual_batch: SysImportBatch | None = None
    if any(r.is_create for r in plan.cost_refills):
        manual_batch = SysImportBatch(
            filename="manual-maintenance-line-workbook.xlsx",
            file_type="maintenance",
            file_hash=hashlib.sha256(
                f"manual-line:{apply_operation_key or import_batch_id}".encode(
                    "utf-8"
                )
            ).hexdigest(),
            uploaded_by=operated_by,
            rows_total=sum(r.is_create for r in plan.cost_refills),
            rows_inserted=sum(r.is_create for r in plan.cost_refills),
            status="success",
            report_json={"source": "workbook_manual_create",
                         "project_id": plan.project_id},
        )
        db.add(manual_batch)
        db.flush()

    demand_reprice_line_ids: set[int] = set()
    for refill_index, refill in enumerate(plan.cost_refills):
        if refill.is_create:
            order = db.get(FMaintenanceOrder, refill.order_id)
            base_line_no = db.scalar(
                select(func.coalesce(func.max(FMaintenanceLine.line_no), 0))
                .where(FMaintenanceLine.order_id == refill.order_id)) or 0
            stable_create_identity = "|".join([
                plan.export_id or import_batch_id,
                plan.file_sha256 or import_batch_id,
                "parts",
                str(refill_index),
                str(refill.order_id),
                str(refill.part_id),
            ])
            new_line = FMaintenanceLine(
                raw_line_id=(
                    "manual-line:"
                    + hashlib.sha256(
                        stable_create_identity.encode("utf-8")
                    ).hexdigest()[:48]
                ),
                order_id=refill.order_id,
                line_no=int(base_line_no) + 1,
                part_id=refill.part_id,
                pn_std=refill.pn, pn_raw=refill.pn,
                description=refill.description,
                qty=refill.qty, return_qty=refill.return_qty or Decimal(0),
                serial_numbers=refill.serial_numbers,
                line_note=refill.note,
                edited_source="workbook_manual",
                is_active=True,
                import_batch_id=manual_batch.id,
            )
            if refill.unit_cost_ex_tax is not None:
                new_line.cost_source = "manual"
                new_line.cost_tax_basis = "ex"
                _recompute_line_amounts(new_line, unit_cost_ex_tax=refill.unit_cost_ex_tax,
                                        unit_cost_inc_tax=refill.unit_cost_inc_tax)
            db.add(new_line)
            db.flush()
            if refill.unit_cost_ex_tax is not None:
                db.add(MaintenanceManualCostOverride(
                    line_id=new_line.id,
                    unit_cost_ex_tax=refill.unit_cost_ex_tax,
                    unit_cost_inc_tax=refill.unit_cost_inc_tax,
                    reason=refill.reason, active=True, version=1,
                    updated_by=operated_by))
            # 新行无论是否同时填了人工价都进入权威取价瀑布：自动采购/销售
            # 证据优先，人工 override 只在自动证据仍缺失时兜底。
            demand_reprice_line_ids.add(new_line.id)
            _write_audit(db, project_id=plan.project_id,
                         entity_type="maintenance_line", entity_id=new_line.id,
                         action="CREATE", operated_by=operated_by, reason=audit_reason,
                         after={"pn": refill.pn, "qty": str(refill.qty),
                                "order_no": refill.order_no})
            operating_fact_changed = True
            continue

        line = db.get(FMaintenanceLine, refill.line_id)
        if line is None:
            raise WorkbookError("line_not_found",
                                f"备件行 {refill.line_id} 已不存在，请重新下载")
        if refill.operation == "VOID":
            if line.is_active:
                line.is_active = False
                line.voided_at = datetime.now(timezone.utc)
                line.voided_by = operated_by
                line.void_reason = audit_reason
                cascaded = _cascade_void_site_lines(
                    db, line, project_id=plan.project_id)
                _write_audit(db, project_id=plan.project_id,
                             entity_type="maintenance_line", entity_id=line.id,
                             action="VOID", operated_by=operated_by, reason=audit_reason,
                             after={"cascaded_site_lines": cascaded})
                operating_fact_changed = True
            continue

        # UPDATE：先归一化再计算真实差异，成本与保护使用同一组最终值。
        protection_before = field_protection.snapshot_fields(line)
        proposed = {
            key: field_protection.normalized_value(key, value)
            for key, value in {
                "pn_std": refill.pn, "pn_raw": refill.pn,
                "part_id": refill.part_id, "description": refill.description,
                "qty": refill.qty, "return_qty": refill.return_qty,
                "serial_numbers": refill.serial_numbers, "line_note": refill.note,
            }.items() if value is not None
        }
        actual_fields = field_protection.changed_fields(protection_before, proposed)
        before = {
            key: str(protection_before[key]) if isinstance(protection_before[key], Decimal)
            else protection_before[key] for key in sorted(actual_fields)
        }
        for key in sorted(actual_fields):
            setattr(line, key, proposed[key])
        if "part_id" in actual_fields:
            # 真正换绑才停用旧身份价格；等值 PN 回传不能丢失人工证据。
            stale_override = db.scalar(
                select(MaintenanceManualCostOverride).where(
                    MaintenanceManualCostOverride.line_id == line.id,
                    MaintenanceManualCostOverride.active.is_(True),
                )
            )
            if stale_override is not None:
                stale_override.active = False
                stale_override.version += 1
                stale_override.updated_by = operated_by
        if actual_fields & field_protection.PN_IDENTITY_FIELDS:
            demand_reprice_line_ids.add(line.id)
        if actual_fields & {"qty", "return_qty"}:
            _recompute_line_amounts(line)
        # 人工成本（写 override + 合并主表，沿用既有语义）
        cost_changed = False
        if refill.unit_cost_ex_tax is not None or refill.reason is not None:
            cost_changed = _merge_manual_cost_to_line(
                db, refill, operated_by=operated_by)
        # 来源身份保持原值；旧整行保护也不因新增字段证据而自动解除。
        field_protection.record_manual_changes(
            line, before=protection_before, operated_by=operated_by,
            source="workbook_manual",
        )
        if before or cost_changed:
            after = {
                key: str(getattr(line, key)) if isinstance(getattr(line, key), Decimal)
                else getattr(line, key) for key in sorted(actual_fields)
            }
            after["manual_override"] = dict(line.manual_override or {})
            if refill.unit_cost_ex_tax is not None:
                after["cost"] = str(refill.unit_cost_ex_tax)
            if refill.reason is not None:
                after["cost_reason"] = refill.reason
            _write_audit(db, project_id=plan.project_id,
                         entity_type="maintenance_line", entity_id=line.id,
                         action="UPDATE", operated_by=operated_by, reason=audit_reason,
                         before=before or None, after=after)
            operating_fact_changed = True
    # 行级作废的整单级联：被 VOID 的行所属需求单若活动行归零 → 整单墓碑
    # （需求单搜索消失、氚云重传不复活；恢复走 demands 页面既有入口）。
    db.flush()  # Session autoflush=False：先把 is_active=False 刷库，级联计数才准
    voided_order_ids = {
        line.order_id
        for line in (
            db.get(FMaintenanceLine, r.line_id)
            for r in plan.cost_refills if r.operation == "VOID"
        )
        if line is not None
    }
    if voided_order_ids:
        from app.services import maintenance_demands as _demands

        raw_ids = db.scalars(
            select(FMaintenanceOrder.raw_order_id).where(
                FMaintenanceOrder.id.in_(voided_order_ids))
        ).all()
        try:
            cascaded = _demands.cascade_tombstone_orders(
                db, source_order_ids=list(raw_ids),
                operated_by=operated_by,
                reason=f"{audit_reason}（行全部作废级联）",
                _prelocked_states=workbook_states,
            )
        except _demands.DeleteIntentConflict as exc:
            raise WorkbookError(
                "stale_workbook",
                "需求单状态或项目归属已变化，请重新下载项目总表后再操作",
            ) from exc
        if cascaded:
            # entity_id 是 varchar(64)，不能把多张 source_order_id 逗号拼接到一行。
            # 每张单独立审计也让后续检索/追责无需再拆字符串。
            for source_order_id in cascaded:
                _write_audit(
                    db,
                    project_id=plan.project_id,
                    entity_type="maintenance_order",
                    entity_id=source_order_id,
                    action="VOID",
                    operated_by=operated_by,
                    reason=audit_reason,
                    after={
                        "cascaded_order": source_order_id,
                        "cascade_count": len(cascaded),
                    },
                )
    if demand_reprice_line_ids:
        # 与 WBDD/采购导入共用同一权威价格瀑布；commit=False 保持整本工作簿
        # “全成或全败”。这一步也会清除 PN 变化前的旧成本 provenance。
        from app.services import maintenance_cost as _maintenance_cost

        db.flush()
        try:
            _maintenance_cost.recompute(
                db,
                commit=False,
                line_ids=demand_reprice_line_ids,
            )
        except _maintenance_cost.MaintenanceCostRecomputeBusy as exc:
            raise WorkbookError("cost_recompute_busy", str(exc)) from exc
    # 2026-08-24：工作簿建行/改量后立即取价——此前建行不调取价，新行成本
    # 为空要等下一次全局回填才恢复（8-24 两个项目新增 38 行无价的根因）。
    # Apply each repeated issue header once.  Parser normalization guarantees
    # all rows of the same issue carry one chosen value, but recheck the plan so
    # a forged in-process plan cannot restore row-order-dependent behavior.
    requested_addresses: dict[str, str | None] = {}
    new_addresses: dict[str, str | None] = {}
    for flag in plan.site_flags:
        if not flag.is_void and flag.site_location_action != "keep":
            if flag.site_location_action not in {"set", "clear"} or (
                    flag.site_location_action == "set" and
                    (not flag.site_location or len(flag.site_location) > 256)):
                raise WorkbookError("invalid_site_address", "领用地址更新无效，本次未写入")
            address = flag.site_location if flag.site_location_action == "set" else None
            address_issue = db.get(MaintenanceSiteIssue, flag.issue_id) if flag.issue_id else None
            address_map = requested_addresses if address_issue else new_addresses
            address_key = address_issue.issue_id if address_issue else flag.issue_no
            if address_key in address_map and address_map[address_key] != address:
                raise WorkbookError("conflicting_issue_address", "同一领用单包含不同地址，本次未写入")
            address_map[address_key] = address
    merged_into = {}
    occupied_numbers = set(db.scalars(select(MaintenanceSiteIssue.issue_no).where(
        MaintenanceSiteIssue.project_id == plan.project_id,
    ))) | {number for number, _date in requested_headers.values()}
    for item in plan.site_issue_merges:
        source_ids = [source["issue_id"] for source in item["sources"]]
        try:
            site_merges.references.prepare_references(db, source_ids)
        except WorkbookError as exc:
            raise site_merges.conflict(item["target_issue_no"], item["rows"], exc.message) from exc
        survivor_id = item["survivor_issue_id"]
        for issue_id in source_ids:
            merged_into[issue_id] = survivor_id
            requested_addresses.pop(issue_id, None)
            if issue_id != survivor_id:
                tombstone_no = "WB-MERGED-" + uuid4().hex
                while tombstone_no in occupied_numbers:
                    tombstone_no = "WB-MERGED-" + uuid4().hex
                occupied_numbers.add(tombstone_no)
                requested_headers[issue_id] = (tombstone_no, db.get(MaintenanceSiteIssue, issue_id).issue_date)
        requested_headers[survivor_id] = (
            item["target_issue_no"], date.fromisoformat(item["final_header"]["issue_date"]),
        )
        requested_addresses[survivor_id] = item["final_header"]["site_location"]
    for issue_id in requested_addresses:
        address_issue = db.get(MaintenanceSiteIssue, issue_id)
        requested_headers.setdefault(issue_id, (address_issue.issue_no, address_issue.issue_date))
    # 强引用保留原始 ORM 头；Session identity map 为弱引用，不能在临时改号后
    # 重新加载头，否则审计与原语快照会读到数据库中的暂号。
    header_issues = {issue_id: db.get(MaintenanceSiteIssue, issue_id)
                     for issue_id in requested_headers}
    # 两阶段改名：先在数据库内腾空所有待改号，再由既有原语一次完成原号→终号。
    # Core UPDATE 不同步 identity map，原语的 before 快照与外层审计仍看到原号；
    # 不额外 bump、不记临时号审计，也不让中途的逐单 flush 撞互换/链式改名。
    renames = {
        issue_id: issue_no for issue_id, (issue_no, _date) in requested_headers.items()
        if header_issues[issue_id].issue_no != issue_no
    }
    if renames:
        occupied = set(db.scalars(select(MaintenanceSiteIssue.issue_no).where(
            MaintenanceSiteIssue.project_id == plan.project_id,
        ))) | set(renames.values())
        with db.no_autoflush:
            for issue_id in sorted(renames):
                temporary_no = "WB-TEMP-" + uuid4().hex  # 40 字符，列上限 64。
                while temporary_no in occupied:
                    temporary_no = "WB-TEMP-" + uuid4().hex
                occupied.add(temporary_no)
                db.execute(
                    MaintenanceSiteIssue.__table__.update()
                    .where(MaintenanceSiteIssue.issue_id == issue_id)
                    .values(issue_no=temporary_no)
                )
        db.flush()
    header_changed_issue_ids: set[str] = set()
    for issue_id, (issue_no, issue_date) in requested_headers.items():
        issue = header_issues[issue_id]
        if issue is None or issue.project_id != plan.project_id:
            raise WorkbookError("project_mismatch", "领用单归属已变化，请重新下载")
        header_changed = issue.issue_no != issue_no or issue.issue_date != issue_date
        address_changed = issue_id in requested_addresses and issue.site_location != requested_addresses[issue_id]
        if header_changed or address_changed:
            before_header = {
                "issue_no": issue.issue_no,
                "issue_date": issue.issue_date.isoformat(),
                "site_location": issue.site_location,
            }
            apply_locked_site_issue_edits(
                db, project=project, issue=issue, line_changes={},
                operated_by=operated_by, reason=audit_reason,
                legacy_context=LegacySiteIssueEditContext("v2"),
                header_changes={
                    "issue_no": issue_no, "issue_date": issue_date,
                    **({"site_location": requested_addresses[issue_id]} if address_changed else {}),
                },
            )
            issue.version += 1
            if header_changed:
                header_changed_issue_ids.add(issue_id)
            _write_audit(
                db,
                project_id=plan.project_id,
                entity_type="site_issue",
                entity_id=issue_id,
                action="UPDATE",
                operated_by=operated_by,
                reason=audit_reason,
                before=before_header,
                after={
                    "issue_no": issue_no,
                    "issue_date": issue_date.isoformat(),
                    "site_location": issue.site_location,
                },
            )
            operating_fact_changed = True

    if header_changed_issue_ids:
        # Session 禁用 autoflush；同文件新明细随后按新单号查头，必须先
        # 刷出本次合法改名，否则会误建同号新单并撞唯一键。
        db.flush()

    for item in plan.site_issue_merges:
        try:
            site_merges.move_lines(db, item, project_id=plan.project_id,
                                  operated_by=operated_by, reason=audit_reason)
        except WorkbookError as exc:
            raise site_merges.conflict(item["target_issue_no"], item["rows"], exc.message) from exc
        header_changed_issue_ids.add(item["survivor_issue_id"])
        operating_fact_changed = True
    # 合单后新增明细追加到最终头的最大行号后，不能复用合单前的序号。
    site_flags = []
    next_line_numbers = {}
    for flag in plan.site_flags:
        if flag.is_create and flag.issue_id in merged_into:
            owner = merged_into[flag.issue_id]
            if owner not in next_line_numbers:
                next_line_numbers[owner] = db.scalar(select(func.max(MaintenanceSiteIssueLine.line_no)).where(
                    MaintenanceSiteIssueLine.issue_id == owner,
                )) or 0
            next_line_numbers[owner] += 1
            flag = replace(flag, issue_id=owner, line_no=next_line_numbers[owner])
        site_flags.append(flag)

    # Recheck generated scopes under the apply locks. A second validation of
    # an edited original file must not add a second copy after the first wins.
    auto_prefix = _v2_auto_site_prefix(plan.project_id, plan.export_id)
    if any(f.is_create and (f.issue_no or "").startswith(auto_prefix) for f in site_flags):
        if db.scalar(select(MaintenanceSiteIssue.issue_id).where(
            MaintenanceSiteIssue.project_id == plan.project_id,
            MaintenanceSiteIssue.issue_no.startswith(auto_prefix),
        ).limit(1)):
            raise WorkbookError("stale_site_replay", "原文件已完成自动编号领用导入，请重新下载总表后编辑")
    pricing_entries: dict[str, tuple[date, MaintenanceSiteIssueLine]] = {}
    from app.services import maintenance_site_return_requirements as requirements
    requirement_before = {
        flag.issue_line_id: requirements.snapshot(line)
        for flag in site_flags
        if (line := db.get(MaintenanceSiteIssueLine, flag.issue_line_id)) is not None
    }
    created_issues: dict[str, MaintenanceSiteIssue] = {}
    for flag in site_flags:
        line = db.get(MaintenanceSiteIssueLine, flag.issue_line_id)
        # 2026-08-23：缺行=作废——领用行软作废，退出成本与返还义务计算；
        # 一张单全部行都作废时，单据状态同步置 void（与系统内作废同观感）
        if flag.is_void:
            if line is not None and line.is_active:
                line.is_active = False
                db.flush()
                issue_doc = db.get(MaintenanceSiteIssue, line.issue_id)
                if issue_doc is not None:
                    remaining = db.scalar(
                        select(func.count()).select_from(MaintenanceSiteIssueLine).where(
                            MaintenanceSiteIssueLine.issue_id == issue_doc.issue_id,
                            MaintenanceSiteIssueLine.is_active.is_(True)))
                    if not remaining:
                        issue_doc.normalized_status = "void"
                        issue_doc.version += 1
                        db.flush()
                _write_audit(db, project_id=plan.project_id,
                             entity_type="site_issue_line", entity_id=line.issue_line_id,
                             action="VOID", operated_by=operated_by, reason=audit_reason,
                             after={"issue_no": (db.get(MaintenanceSiteIssue, line.issue_id).issue_no
                                                 if db.get(MaintenanceSiteIssue, line.issue_id) else None)})
                operating_fact_changed = True
            continue
        if flag.is_create and line is not None:
            # validate→apply 窗口内同身份行被建出/作废：手工新增行的单头没有行锁，
            # 不能拿 validate 时的判断当真。作废优先，整本零写入让用户重传。
            raise WorkbookError(
                "row_voided",
                f"领用单 {flag.issue_no} 的同单号/PN/SN 行在校验后已存在或已作废，"
                "本次上传未写入任何数据；请重新下载后再传",
            )
        if line is None and flag.is_create:
            # 附加明细已在 parse 明确绑定、锁内复核，不能被并发出现的同号头接走。
            issue = (db.get(MaintenanceSiteIssue, flag.issue_id) if flag.issue_id
                     else created_issues.get(flag.issue_no))
            if issue is not None and issue.normalized_status == "void":
                # 解析期已按 D-02 把落在 void 单头下的新增行挡下；这里只剩
                # validate→apply 窗口内被面板作废的单头。活行不得挂到 void 单头。
                raise WorkbookError(
                    "row_voided",
                    f"领用单 {flag.issue_no} 在校验后已被作废，本次上传未写入任何"
                    "数据；作废行不复活，如需重新领用请换单号重录",
                )
            if issue is None:
                issue = MaintenanceSiteIssue(
                    issue_id=str(uuid4()),
                    project_id=plan.project_id,
                    issue_no=flag.issue_no,
                    # LY-AUTO 自动单首次落库时分配可读展示号；用户单号不带展示号。
                    # replay 永远命中上方 issue 查询，不会二次分配（决策 0006）。
                    display_no=(
                        _v2_allocate_site_display_no(db, flag.issue_date)
                        if flag.issue_no.startswith("LY-AUTO-") else None
                    ),
                    issue_date=flag.issue_date,
                    site_location=new_addresses.get(flag.issue_no),
                    raw_status="已确认",
                    status_mapping_state="mapped",
                    normalized_status="confirmed",
                    status_mapping_version="workbook-manual-v1",
                    source="workbook",
                    import_batch_id=import_batch_id,
                    created_by=operated_by,
                    version=1,
                )
                db.add(issue)
                db.flush()
                created_issues[flag.issue_no] = issue
            line = MaintenanceSiteIssueLine(
                issue_line_id=flag.issue_line_id,
                issue_id=issue.issue_id,
                line_no=flag.line_no,
                part_id=flag.part_id,
                pn=flag.pn,
                quantity=flag.quantity,
                serial_number=flag.serial_number,
                demand_order_no=flag.demand_order_no,
                remark=flag.remark,
                no_return=flag.no_return,
                is_active=True,
                algorithm_version="workbook-manual-v1",
            )
            db.add(line)
            pricing_entries[line.issue_line_id] = (issue.issue_date, line)
            operating_fact_changed = True
            continue
        if line is None:
            raise WorkbookError("line_not_found", f"领用行 {flag.issue_line_id} 已不存在，请重新下载")
        patch = {"no_return": flag.no_return}
        if flag.pn is not None and (
            flag.pn != line.pn
            or (flag.part_id is not None and flag.part_id != line.part_id)
        ):
            if flag.part_id is None:
                raise WorkbookError(
                    "part_not_found", "PN 已变化但未解析到备件主数据"
                )
            patch.update(pn=flag.pn, part_id=flag.part_id)
        patch.update({key: value for key in ("serial_number", "quantity", "remark")
                      if (value := getattr(flag, key)) is not None})
        issue_for_price = db.get(MaintenanceSiteIssue, line.issue_id)
        edited = apply_locked_site_issue_edits(
            db, project=project, issue=issue_for_price,
            line_changes={line.issue_line_id: patch}, header_changes=None,
            operated_by=operated_by, reason=audit_reason,
            legacy_context=LegacySiteIssueEditContext("v2"),
        )
        if edited.pricing_line_ids:
            pricing_entries[line.issue_line_id] = (issue_for_price.issue_date, line)
        operating_fact_changed = edited.changed or operating_fact_changed
    # Preserve one P3 correction/event per issue, including caller-owned VOID.
    corrections_by_issue: dict[str, dict] = {}
    for line_id, snapshot in requirement_before.items():
        issue_id = db.get(MaintenanceSiteIssueLine, line_id).issue_id
        corrections_by_issue.setdefault(issue_id, {})[line_id] = snapshot
    for issue_id, snapshots in corrections_by_issue.items():
        apply_locked_site_issue_edits(
            db, project=db.get(MaintenanceProject, plan.project_id),
            issue=db.get(MaintenanceSiteIssue, issue_id),
            line_changes={}, header_changes=None,
            operated_by=operated_by, reason=audit_reason,
            legacy_context=LegacySiteIssueEditContext("v2", snapshots),
        )
    if header_changed_issue_ids:
        for issue, line in db.execute(
            select(MaintenanceSiteIssue, MaintenanceSiteIssueLine)
            .join(
                MaintenanceSiteIssueLine,
                MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id,
            )
            .where(
                MaintenanceSiteIssue.issue_id.in_(
                    sorted(header_changed_issue_ids)
                ),
                MaintenanceSiteIssueLine.is_active.is_(True),
            )
            .order_by(
                MaintenanceSiteIssue.issue_id,
                MaintenanceSiteIssueLine.line_no,
            )
        ):
            pricing_entries[line.issue_line_id] = (issue.issue_date, line)
    if pricing_entries:
        from app.services import maintenance_consumption_cost as _consumption_cost
        _consumption_cost.resolve_lines(db, lines=list(pricing_entries.values()))
    receipt_voided_rows: list[dict] = []
    if plan.expense_updates or receipt_ops:
        inner_result = ec.apply(
            db,
            ec.WorkbookPlan(plan.project_id, plan.expense_updates, receipt_ops),
            operated_by=operated_by,
            import_batch_id=import_batch_id,
            commit=False,
            workbook_state=workbook_state,
            bump_revision=False,
            track_change=True,
        )
        operating_fact_changed = (
            bool(inner_result.pop("_operating_fact_changed", False))
            or operating_fact_changed
        )
        # D-02 作废优先（05）：共享写路径跳过的已作废月份并入本表的行级回执，
        # 与 06 表 voided_rows 同一形状；05 导出只带 confirmed 行，V2 解析器把
        # 落在作废月上的新行当 UPDATE 送来，不带回执用户以为改成了。
        receipt_voided_rows = [
            {**item, "sheet": V2_SHEET_RECEIPTS}
            for item in inner_result.get("voided_rows", ())
        ]
    # 04 作废（显式 VOID + 缺行=作废）：软删标记，读侧从此不导出（#264 契约）。
    for raw_line_id in plan.expense_voids:
        expense = db.scalar(select(FProjectExpense).where(FProjectExpense.raw_line_id == raw_line_id))
        if expense is None or expense.data_status == "已作废":
            continue
        expense.data_status = "已作废"
        attribution = db.get(
            MaintenanceProjectExpenseAttribution, f"bxd:{raw_line_id}")
        if attribution is None or attribution.project_id != plan.project_id:
            raise WorkbookError(
                "expense_not_in_project", "报销归因已变化，请重新下载")
        attribution.raw_status = "已作废"
        attribution.status_mapping_state = "mapped"
        attribution.normalized_status = "void"
        attribution.status_mapping_version = ec.PROTOCOL_VERSION
        attribution.version += 1
        _write_audit(db, project_id=plan.project_id,
                     entity_type="project_expense", entity_id=raw_line_id,
                     action="VOID", operated_by=operated_by, reason=audit_reason,
                     before={"data_status": None})
        operating_fact_changed = True
    milestone_entity_ids = {
        change.entity_id
        for change in plan.milestone_changes
        if change.entity_id is not None
    }
    locked_milestones = list(db.scalars(
        select(MaintenanceCollectionMilestone)
        .where(
            MaintenanceCollectionMilestone.milestone_id.in_(
                milestone_entity_ids
            )
        )
        .order_by(MaintenanceCollectionMilestone.milestone_id)
        .with_for_update()
    ).all()) if milestone_entity_ids else []
    if (
        {milestone.milestone_id for milestone in locked_milestones}
        != milestone_entity_ids
        or any(
            milestone.project_id != plan.project_id
            or not milestone.is_active
            for milestone in locked_milestones
        )
    ):
        raise WorkbookError(
            "milestone_not_found",
            "回款计划已不存在、已作废或不属于本项目，请重新下载",
        )
    milestones_by_id = {
        milestone.milestone_id: milestone for milestone in locked_milestones
    }
    create_coordinates: set[tuple[str, int]] = set()
    for change in plan.milestone_changes:
        contract = current_contract_by_no.get(change.contract_no)
        if contract is None:
            raise WorkbookError(
                "contract_not_found",
                f"合同 {change.contract_no} 已不是本项目当前合同，请重新下载",
            )
        existing = (
            milestones_by_id.get(change.entity_id)
            if change.entity_id is not None else None
        )
        if existing is not None:
            # The hidden ID is the identity.  Editable contract/sequence cells
            # are attributes only and may never redirect UPDATE/VOID to a
            # different row.
            if (
                existing.project_contract_id != contract.project_contract_id
                or existing.sequence != change.sequence
            ):
                raise WorkbookError(
                    "milestone_identity_mismatch",
                    "回款计划的合同或期次已变化，请重新下载",
                )
            if (
                change.base_version is not None
                and existing.version != change.base_version
            ):
                raise WorkbookError(
                    "stale_row",
                    f"合同 {change.contract_no} 第 {change.sequence} 期已被更新",
                )
        if change.operation == "VOID":
            if existing is None:
                raise WorkbookError(
                    "void_target_missing",
                    "作废行缺少可验证的回款计划实体ID",
                )
            existing.is_active = False
            existing.version += 1
            _write_audit(db, project_id=plan.project_id,
                         entity_type="collection_milestone",
                         entity_id=existing.milestone_id, action="VOID",
                         operated_by=operated_by, reason=audit_reason,
                         after={"contract_no": change.contract_no,
                                "sequence": change.sequence})
            operating_fact_changed = True
            continue
        if change.operation == "CREATE":
            coordinate = (contract.project_contract_id, change.sequence)
            if coordinate in create_coordinates:
                raise WorkbookError(
                    "milestone_target_conflict",
                    f"合同 {change.contract_no} 第 {change.sequence} 期在工作簿中重复新建",
                )
            create_coordinates.add(coordinate)
            coordinate_owner = db.scalar(
                select(MaintenanceCollectionMilestone)
                .where(
                    MaintenanceCollectionMilestone.project_contract_id
                    == contract.project_contract_id,
                    MaintenanceCollectionMilestone.sequence == change.sequence,
                )
                .with_for_update()
            )
            if coordinate_owner is not None:
                raise WorkbookError(
                    "milestone_target_conflict",
                    f"合同 {change.contract_no} 第 {change.sequence} 期已有回款计划，请重新下载",
                )
        elif existing is None:
            raise WorkbookError(
                "milestone_not_found",
                "更新行缺少可验证的回款计划实体ID",
            )
        milestone = write_collection_milestone(
            db, project_id=plan.project_id, project_contract_id=contract.project_contract_id,
            sequence=change.sequence, planned_date=change.planned_date,
            planned_amount=change.planned_amount, completeness_state=("complete" if change.planned_date and change.planned_amount else "date_only" if change.planned_date else "amount_only"),
            source="project_master_v2", date_precision=change.date_precision, operator=operated_by,
        )
        milestone.is_active = True
        operating_fact_changed = True
    if operating_fact_changed:
        # One successful workbook transaction invalidates every older V3/data
        # version exactly once, regardless of how many sheets it changed.
        operations.bump_locked_workbook_revision(db, state=workbook_state)
    result = _v2_apply_result(
        plan,
        operated_by=operated_by,
        import_batch_id=import_batch_id,
        replayed=False,
        revision_drift=revision_drift,
    )
    result["voided_rows"].extend(receipt_voided_rows)
    result["warnings"].extend(item["message"] for item in receipt_voided_rows)
    if apply_operation_key is not None:
        receipt = MaintenanceProjectWorkbookOperation(
            project_id=plan.project_id,
            export_id=plan.export_id,
            file_sha256=plan.file_sha256,
            operation_key=apply_operation_key,
            payload_hash=plan.file_sha256,
            operation_type="file_apply",
            operated_by=operated_by,
        )
        db.add(receipt)
        db.flush()
        db.add(MaintenanceProjectOperationAudit(
            project_id=plan.project_id,
            entity_type="master_workbook_operation",
            entity_id=str(receipt.id),
            action="apply_completed",
            before_json=None,
            after_json={
                "schema": "master_v2_apply_receipt_v1",
                "operation_key": apply_operation_key,
                "response": deepcopy(result),
                "requires_contract_amount_manage": plan.contract_amount_change is not None,
            },
            reason=audit_reason,
            operated_by=operated_by,
            operated_at=receipt.created_at,
        ))
    db.commit()
    return result
