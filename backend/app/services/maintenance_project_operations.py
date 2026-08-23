"""Controlled operating facts for stable maintenance projects.

The module is deliberately independent from the legacy WBDD cost/read models.  It
is the only write path for project-contract relationships and the project-scoped
facts added by the stable-project workspace.
"""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAuditLog,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.dimensions import DimPart
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueCommand,
    MaintenanceSiteIssueDeliverySource,
    MaintenanceSiteIssueLine,
    MaintenanceSiteIssueReturnEvent,
    MaintenanceProjectWorkbookState,
)
from app.models.maintenance_manager import (
    BusinessFile,
    BusinessFileLink,
    MaintenanceAcceptanceDeliverable,
    MaintenanceCollectionMilestone,
    MaintenanceManagerUploadBatch,
    MaintenanceManagerUploadBatchProject,
    MaintenanceServicePeriod,
)
from app.business_time import business_today
from app.config import get_settings
from app import tax_policy
from app.security import UserContext, is_field_hidden
from app.services import maintenance_bad_returns
from app.services import maintenance_consumption_cost
from app.services import maintenance_project
from app.services import maintenance_project_assignments
from app.services import maintenance_warehouse_site_issue_bridge
from app.services.query_filters import active_beta_maintenance_orders


class MaintenanceOperationError(Exception):
    """Invalid stable-project operating-fact request."""


class MaintenanceOperationConflict(Exception):
    """Concurrent or duplicate operating-fact request."""


class MaintenanceOperationPermissionError(Exception):
    """Requested operating-fact selector depends on hidden financial facts."""


_QUANTITY_QUANTUM = Decimal("0.001")
_QUANTITY_MAX_EXCLUSIVE = Decimal("100000000000")
_MONEY_QUANTUM = Decimal("0.01")
_MONEY_MAX_EXCLUSIVE = Decimal("1000000000000")
_SITE_ISSUE_LINE_LIMIT = 200
_ALL_FINANCIAL_FILTER_FIELDS = ("contract_amount", "unit_cost", "expense_inc")
_CONTRACT_COMPLETENESS_FILTERS = {
    "completeness:no_effective_contracts",
    "completeness:duplicate_effective_contract",
    "completeness:unmapped_contract_status",
    "completeness:missing_contract_amount",
    "completeness:cross_project_contract_conflict",
}
_COST_COMPLETENESS_FILTERS = {
    "completeness:missing_consumption_cost",
    "completeness:unmapped_site_issue_status",
}
_EXPENSE_COMPLETENESS_FILTERS = {
    "completeness:unmapped_expense_status",
    "completeness:expense_data_not_ready",
    "completeness:expense_readiness_in_future",
}
_EXACT_REMINDER_FILTERS = frozenset(
    {
        "项目经理月度更新",
        "all",
        "info",
        "warning",
        "critical",
        "completeness",
        "collection",
        "cost",
        "cost_ratio",
        *_CONTRACT_COMPLETENESS_FILTERS,
        *_COST_COMPLETENESS_FILTERS,
        *_EXPENSE_COMPLETENESS_FILTERS,
        "collection:missing_confirmed",
        "collection:incomplete",
        "cost:missing_price",
        "cost:sales_fallback_estimate",
        "cost_ratio:yellow",
        "cost_ratio:red",
        "维保期限",
        "计划回款",
        "验收报告",
        "验收审批",
        "service_period:empty",
        "service_period:start_only",
        "service_period:end_only",
        "collection_plan:missing",
        "acceptance:missing_due",
        "acceptance:missing_attachment",
        "acceptance:report_due",
        "acceptance:pending_review",
        "acceptance:rejected",
    }
)
_MANAGER_UPDATE_FILTER = re.compile(
    r"manager_update:[0-9]{4}-(?:0[1-9]|1[0-2])\Z",
    re.ASCII,
)
_COLLECTION_PLAN_FILTER = re.compile(
    r"collection_plan:([A-Za-z0-9_-]{1,36}):([1-9]|1[0-9]|2[0-4])\Z",
    re.ASCII,
)


def _validate_reminder_filter(reminder: str | None) -> None:
    """Reject undeclared reminder selectors without reflecting their content."""

    if reminder is None:
        return
    if reminder in _EXACT_REMINDER_FILTERS:
        return
    if _MANAGER_UPDATE_FILTER.fullmatch(reminder):
        return
    if _COLLECTION_PLAN_FILTER.fullmatch(reminder):
        return
    raise MaintenanceOperationError("不支持的提醒筛选")


def _reminder_filter_required_fields(reminder: str | None) -> tuple[str, ...]:
    """Return hidden fact fields a directory selector would classify by."""

    _validate_reminder_filter(reminder)
    if reminder is None or reminder == "项目经理月度更新" or reminder.startswith(
        "manager_update:"
    ):
        return ()
    if reminder in _COST_COMPLETENESS_FILTERS:
        return ("unit_cost",)
    if reminder in _CONTRACT_COMPLETENESS_FILTERS:
        return ("contract_amount",)
    if reminder in _EXPENSE_COMPLETENESS_FILTERS:
        return ("contract_amount", "expense_inc")
    if reminder == "cost" or reminder.startswith("cost:"):
        return ("unit_cost",)
    if reminder in {"info", "collection"} or reminder.startswith("collection:"):
        return ("contract_amount",)
    if (
        reminder in {"all", "warning", "critical", "completeness", "cost_ratio"}
        or reminder.startswith("cost_ratio:")
    ):
        return _ALL_FINANCIAL_FILTER_FIELDS
    return ()


def _task_type_filter_required_fields(task_type: str | None) -> tuple[str, ...]:
    if not task_type:
        return ()
    if task_type == "项目经理月度更新":
        return ()
    if task_type == "cost":
        return ("unit_cost",)
    if task_type == "collection":
        return ("contract_amount",)
    if task_type in {"completeness", "cost_ratio"}:
        return _ALL_FINANCIAL_FILTER_FIELDS
    return ()


def _quantity(value: Decimal | str) -> Decimal:
    try:
        parsed = Decimal(value)
        if not parsed.is_finite():
            raise InvalidOperation
        normalized = parsed.quantize(
            _QUANTITY_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, ValueError) as exc:
        raise MaintenanceOperationError("现场领用数量超出允许范围") from exc
    if (
        not normalized.is_finite()
        or normalized <= 0
        or normalized >= _QUANTITY_MAX_EXCLUSIVE
    ):
        raise MaintenanceOperationError("现场领用数量超出允许范围")
    return normalized


def _money_amount(value: Decimal | str, *, label: str) -> Decimal:
    """Normalize every Numeric(14,2) write before validation and persistence."""

    try:
        parsed = Decimal(value)
        if not parsed.is_finite():
            raise InvalidOperation
        normalized = parsed.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise MaintenanceOperationError(f"{label}超出允许范围") from exc
    if normalized < 0 or normalized >= _MONEY_MAX_EXCLUSIVE:
        raise MaintenanceOperationError(f"{label}超出允许范围")
    return normalized


def _money(value: Decimal | None) -> str | None:
    return format(value, ".2f") if value is not None else None


def _rate(value: Decimal) -> str:
    """Serialize fixed rates identically before and after a database refresh."""

    return format(value.normalize(), "f")


def _resolve_site_issue_cost(
    db: Session,
    *,
    issue_date: date,
    line: MaintenanceSiteIssueLine,
) -> MaintenanceSiteIssueLine:
    try:
        return maintenance_consumption_cost.resolve_line(
            db,
            issue_date=issue_date,
            line=line,
        )
    except maintenance_consumption_cost.CostResolutionError as exc:
        raise MaintenanceOperationError(str(exc)) from exc


def _resolve_site_issue_costs(
    db: Session,
    *,
    lines: list[tuple[date, MaintenanceSiteIssueLine]],
) -> list[MaintenanceSiteIssueLine]:
    try:
        return maintenance_consumption_cost.resolve_lines(db, lines=lines)
    except maintenance_consumption_cost.CostResolutionError as exc:
        raise MaintenanceOperationError(str(exc)) from exc


def _workbook_data_version(project_id: str, revision: int) -> str:
    return hashlib.sha256(f"{project_id}:{revision}".encode("utf-8")).hexdigest()


def get_or_create_workbook_state(
    db: Session,
    *,
    project_id: str,
    lock: bool = False,
) -> MaintenanceProjectWorkbookState:
    """Return the project concurrency row, creating revision zero idempotently."""

    db.execute(
        insert(MaintenanceProjectWorkbookState)
        .values(
            project_id=project_id,
            revision=0,
            data_version=_workbook_data_version(project_id, 0),
        )
        .on_conflict_do_nothing(index_elements=["project_id"])
    )
    statement = select(MaintenanceProjectWorkbookState).where(
        MaintenanceProjectWorkbookState.project_id == project_id
    )
    if lock:
        statement = statement.with_for_update()
    return db.execute(statement).scalar_one()


def bump_workbook_revision(
    db: Session,
    *,
    project_id: str,
) -> MaintenanceProjectWorkbookState:
    """Bump once inside the caller's transaction after an operating-fact write."""

    state = get_or_create_workbook_state(db, project_id=project_id, lock=True)
    return bump_locked_workbook_revision(db, state=state)


def bump_locked_workbook_revision(
    db: Session,
    *,
    state: MaintenanceProjectWorkbookState,
) -> MaintenanceProjectWorkbookState:
    """Bump a state row the caller already locked with ``FOR UPDATE``."""

    state.revision += 1
    state.data_version = _workbook_data_version(state.project_id, state.revision)
    db.flush()
    return state


def contract_dict(row: MaintenanceProjectContract) -> dict:
    return {
        "project_contract_id": row.project_contract_id,
        "project_id": row.project_id,
        "contract_id": row.contract_id,
        "contract_no": row.contract_no,
        "contract_amount": _money(row.contract_amount),
        "contract_amount_basis": "inc_tax",
        "contract_status": row.contract_status,
        "status_mapping_state": row.status_mapping_state,
        "status_mapping_version": row.status_mapping_version,
        "included_in_total": row.included_in_total,
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "source": row.source,
        "version": row.version,
    }


def _required(value: str | None, label: str, limit: int = 64) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise MaintenanceOperationError(f"{label}不能为空")
    if len(cleaned) > limit:
        raise MaintenanceOperationError(f"{label}过长")
    return cleaned


def _project(db: Session, project_id: str) -> MaintenanceProject | None:
    return db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )


def _lock_project_for_fact_write(
    db: Session, project_id: str
) -> MaintenanceProject | None:
    """Use one global lock order: workbook state, then the concrete fact row."""

    exists = db.scalar(
        select(MaintenanceProject.project_id).where(
            MaintenanceProject.project_id == project_id
        )
    )
    if exists is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    return _project(db, project_id)


def _audit_contract(
    db: Session,
    row: MaintenanceProjectContract,
    *,
    action: str,
    before: dict | None,
    after: dict | None,
    reason: str,
    operated_by: str,
) -> None:
    db.add(
        MaintenanceProjectAuditLog(
            project_id=row.project_id,
            entity_type="project_contract",
            entity_id=row.project_contract_id,
            action=action,
            before_json=before,
            after_json=after,
            reason=_required(reason, "操作原因", 1000),
            operated_by=_required(operated_by, "操作人"),
        )
    )


def backfill_site_issue_costs(
    db: Session, *, operated_by: str, force: bool = False,
    reason: str = "领用成本回填：需求单价格优先的瀑布解析历史领用行"
) -> dict:
    """对没有任何成本证据的历史领用行跑成本解析瀑布（2026-08-22）。

    生产 294 条领用行 cost 全空（导入链路从未跑解析），已领用成本恒「—」。
    resolve_lines 瀰布：手工价 > 关联采购行 > 采购价±7 天窗口；解析不出
    的行保持 NULL（不知道≠0，铁律 5）。幂等：只处理 cost 为空的行。
    """
    if force:
        # 强制重算：清掉算法产生的历史解析结果（manual 输入保留——解析器会
        # 按新瀑布重新套用），让新算法（如 v2 的需求单价格优先）全量生效
        from sqlalchemy import update

        db.execute(
            update(MaintenanceSiteIssueLine).where(
                MaintenanceSiteIssueLine.cost_source.in_(
                    ["direct_purchase", "purchase_window",
                     "sales_window", "maint_demand"])
            ).values(
                cost_source=None, reference_side=None,
                reference_samples=[], reference_sample_ids=[],
                reference_sample_count=0,
                reference_window_from=None, reference_window_to=None,
                unit_cost=None, cost_amount=None,
                unit_cost_ex_tax=None, unit_cost_inc_tax=None,
                cost_amount_ex_tax=None, cost_amount_inc_tax=None,
            )
        )
        db.flush()
    rows = db.execute(
        select(MaintenanceSiteIssueLine, MaintenanceSiteIssue.issue_date)
        .join(MaintenanceSiteIssue,
              MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id)
        .where(MaintenanceSiteIssueLine.cost_amount_inc_tax.is_(None),
               MaintenanceSiteIssueLine.is_active.is_(True))
        .order_by(MaintenanceSiteIssue.issue_date)
    ).all()
    stats = {"total": len(rows), "resolved": 0, "still_unknown": 0,
             "projects_touched": 0}
    resolved_by_project: dict[str, int] = defaultdict(int)
    CHUNK = 200
    for start in range(0, len(rows), CHUNK):
        batch = rows[start:start + CHUNK]
        lines = [(issue_date, line) for line, issue_date in batch]
        try:
            maintenance_consumption_cost.resolve_lines(db, lines=lines)
        except maintenance_consumption_cost.CostResolutionError:
            # 个别坏行（数量/价格越界）不拖累整批：逐行降级重试
            for single in lines:
                try:
                    maintenance_consumption_cost.resolve_lines(db, lines=[single])
                except maintenance_consumption_cost.CostResolutionError:
                    pass
        db.flush()
    for line, _issue_date in rows:
        if line.cost_amount_inc_tax is not None:
            stats["resolved"] += 1
        else:
            stats["still_unknown"] += 1
    for line, _ in rows:
        if line.cost_amount_inc_tax is not None:
            pid = db.scalar(
                select(MaintenanceSiteIssue.project_id).where(
                    MaintenanceSiteIssue.issue_id == line.issue_id))
            if pid:
                resolved_by_project[pid] += 1
    stats["projects_touched"] = len(resolved_by_project)
    for pid, n in resolved_by_project.items():
        _fact_audit(db, project_id=pid, entity_type="site_issue_line",
                    entity_id=f"backfill:{n}", action="recompute",
                    before=None, after={"resolved": n}, reason=reason,
                    operated_by=operated_by)
    db.flush()
    return stats


def backfill_expense_attribution(
    db: Session, *, operated_by: str, reason: str = "报销归因回填：BXD/项目追踪导入 → 合同 → 项目"
) -> dict:
    """把 ETL 落库的 f_project_expense（BXD 报销行）归因到项目事实表。

    2026-08-22：生产 f_project_expense 5787 行而归因表 0 行——报销成本恒 0 的
    根因（「报销双通道割裂」的落地一半）。匹配链：linked_sales_order_no →
    maintenance_project_contract.contract_no → project。幂等（expense_id =
    bxd:{raw_line_id} 已存在即跳过）；共用单（一单多项目）跳过防重复计钱；
    状态映射：已结束→approved、已作废→void、其余→unmapped/unknown。
    每项目写一条汇总审计（不逐行刷 5787 条）。
    """
    from app.models.maintenance import FProjectExpense, FMaintenanceOrder
    from app.models.maintenance_source_assignment import (
        MaintenanceSourceOrderAssignment,
    )
    from app.config import MAINT_EXPENSE_ACTIVE_STATUS

    def _keys(order_no: str) -> list[str]:
        # 归一：报销行上「XSDD-20221008-0165」与裸「20221008-0165」两种形态都有
        return [order_no, order_no.removeprefix("XSDD-")]

    # 匹配源 1：正式合同表（当前生产基本为空，演练数据）
    contract_projects: dict[str, set[str]] = defaultdict(set)
    for contract_no, project_id in db.execute(
        select(
            MaintenanceProjectContract.contract_no,
            MaintenanceProjectContract.project_id,
        )
    ):
        for key in _keys(contract_no):
            contract_projects[key].add(project_id)
    # 匹配源 2（主力）：挂靠关系——WBDD 单的 linked_sales_order_no（XSDD）→ 项目。
    # 与展示板合同额的 XSDD 回退层同一口径（#51）。
    for xsdd_no, project_id in db.execute(
        select(
            FMaintenanceOrder.linked_sales_order_no,
            MaintenanceSourceOrderAssignment.project_id,
        )
        .join(MaintenanceSourceOrderAssignment,
              and_(
                  MaintenanceSourceOrderAssignment.source_order_id
                  == FMaintenanceOrder.raw_order_id,
                  MaintenanceSourceOrderAssignment.is_active.is_(True),
              ))
        .where(FMaintenanceOrder.linked_sales_order_no.isnot(None),
               FMaintenanceOrder.linked_sales_order_no != "")
    ):
        for key in _keys(xsdd_no):
            contract_projects[key].add(project_id)
    existing = {
        eid for (eid,) in db.execute(
            select(MaintenanceProjectExpenseAttribution.expense_id))
    }

    stats = {"total": 0, "attributed": 0, "already": 0,
             "skipped_no_contract": 0, "skipped_ambiguous": 0,
             "skipped_invalid": 0, "skipped_duplicate": 0,
             "projects_touched": 0}
    # ETL 幂等键有「数据ID」与「bxd#line」两种形态，同一逻辑行可能以不同
    # raw_line_id 重复入库——按 (project, expense_ref) 进程内去重防 uq 冲突
    seen_refs: set[tuple[str, str]] = set()
    per_project: dict[str, dict[str, int]] = defaultdict(
        lambda: {"rows": 0, "approved": 0, "void": 0, "unknown": 0})
    pending: list[MaintenanceProjectExpenseAttribution] = []
    for row in db.scalars(select(FProjectExpense)):
        stats["total"] += 1
        expense_id = f"bxd:{row.raw_line_id}"
        if expense_id in existing:
            stats["already"] += 1
            continue
        projects = contract_projects.get(row.linked_sales_order_no or "") or set()
        if not projects:
            stats["skipped_no_contract"] += 1
            continue
        if len(projects) > 1:
            stats["skipped_ambiguous"] += 1
            continue
        project_id = next(iter(projects))
        amount_ex = row.amount_ex_tax
        if amount_ex is None or row.expense_date is None:
            stats["skipped_invalid"] += 1
            continue
        ref = (
            f"{row.bxd_no}#{row.line_no}"
            if row.bxd_no and row.line_no is not None
            else (row.bxd_no or row.raw_line_id))
        if (project_id, ref) in seen_refs:
            stats["skipped_duplicate"] += 1
            continue
        seen_refs.add((project_id, ref))
        if row.data_status == MAINT_EXPENSE_ACTIVE_STATUS:
            mapping_state, normalized = "mapped", "approved"
        elif row.data_status in ("已作废", "作废"):
            mapping_state, normalized = "mapped", "void"
        else:
            mapping_state, normalized = "unmapped", "unknown"
        pending.append(MaintenanceProjectExpenseAttribution(
            expense_id=expense_id,
            project_id=project_id,
            project_contract_id=None,
            expense_ref=ref,
            expense_date=row.expense_date,
            applicant=row.person,
            category=row.fee_category or row.expense_type,
            expense_reason=(row.reason[:500] if row.reason else None),
            amount_ex_tax=amount_ex,
            amount_inc_tax=tax_policy.inc_from_ex(amount_ex),
            tax_rate_used=tax_policy.TAX_RATE,
            raw_status=row.data_status or "",
            status_mapping_state=mapping_state,
            normalized_status=normalized,
            status_mapping_version="backfill-v1",
            version=1,
        ))
        stats["attributed"] += 1
        bucket = per_project[project_id]
        bucket["rows"] += 1
        bucket[normalized if normalized in ("approved", "void") else "unknown"] += 1

    for attribution in pending:
        db.add(attribution)
    for project_id, summary in per_project.items():
        stats["projects_touched"] += 1
        _fact_audit(
            db, project_id=project_id, entity_type="expense",
            entity_id=f"backfill:{summary['rows']}",
            action="bulk_create", before=None, after=summary,
            reason=reason, operated_by=operated_by,
        )
    db.flush()
    return stats


def _fact_audit(
    db: Session,
    *,
    project_id: str,
    entity_type: str,
    entity_id: str,
    action: str,
    before: dict | None,
    after: dict | None,
    reason: str,
    operated_by: str,
) -> None:
    db.add(
        MaintenanceProjectOperationAudit(
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            before_json=before,
            after_json=after,
            reason=_required(reason, "操作原因", 1000),
            operated_by=_required(operated_by, "操作人"),
        )
    )


def _qty(value: Decimal) -> str:
    return format(value, ".3f")


def collection_dict(row: MaintenanceCollectionSnapshot) -> dict:
    return {
        "collection_id": row.collection_id,
        "project_id": row.project_id,
        "project_contract_id": row.project_contract_id,
        "report_month": row.report_month.isoformat(),
        "cumulative_amount": _money(row.cumulative_amount),
        "status": row.status,
        "receipt_reference": row.receipt_reference,
        "remark": row.remark,
        "source": row.source,
        "import_batch_id": row.import_batch_id,
        "version": row.version,
    }


def _validate_confirmed_collection_monotonicity(
    db: Session,
    *,
    project_contract_id: str,
    report_month: date,
    cumulative_amount: Decimal,
    exclude_collection_id: str | None = None,
) -> None:
    """Lock sibling snapshots and reject a confirmed cumulative-value reversal."""

    statement = (
        select(MaintenanceCollectionSnapshot)
        .where(
            MaintenanceCollectionSnapshot.project_contract_id
            == project_contract_id,
            MaintenanceCollectionSnapshot.status == "confirmed",
        )
        .order_by(
            MaintenanceCollectionSnapshot.report_month,
            MaintenanceCollectionSnapshot.collection_id,
        )
        .with_for_update()
    )
    if exclude_collection_id is not None:
        statement = statement.where(
            MaintenanceCollectionSnapshot.collection_id != exclude_collection_id
        )
    for sibling in db.scalars(statement):
        if (
            sibling.report_month < report_month
            and cumulative_amount < sibling.cumulative_amount
        ):
            raise MaintenanceOperationError(
                "已确认累计回款不得低于更早月份的已确认累计回款"
            )
        if (
            sibling.report_month > report_month
            and cumulative_amount > sibling.cumulative_amount
        ):
            raise MaintenanceOperationError(
                "已确认累计回款不得高于更晚月份的已确认累计回款"
            )


_COST_EVIDENCE_META = {
    None: ("missing", False, "待补价格"),
    "direct_purchase": ("purchase_evidence", False, "关联采购单价"),
    "purchase_window": (
        "purchase_evidence",
        False,
        "采购前后 7 天数量加权",
    ),
    "sales_window": (
        "sales_estimate",
        True,
        "估算（销售前后 7 天数量加权）",
    ),
    "manual": ("manual_confirmed", False, "人工确认单价"),
}


def cost_evidence_metadata(source: str | None) -> dict:
    """Return stable business semantics for a resolved cost source."""

    kind, is_estimate, label = _COST_EVIDENCE_META.get(
        source,
        ("unknown", True, "未知成本证据"),
    )
    return {
        "cost_evidence_kind": kind,
        "cost_is_estimate": is_estimate,
        "cost_source_label": label,
    }


def site_issue_line_dict(row: MaintenanceSiteIssueLine) -> dict:
    return {
        "issue_line_id": row.issue_line_id,
        "line_no": row.line_no,
        "part_id": row.part_id,
        "pn": row.pn,
        "quantity": _qty(row.quantity),
        "delivery_line_id": row.delivery_line_id,
        "source_order_id": row.source_order_id,
        "source_line_id": row.source_line_id,
        "serial_number": row.serial_number,
        "no_return": row.no_return,
        "linked_purchase_line_id": row.linked_purchase_line_id,
        "manual_unit_cost": _money(row.manual_unit_cost),
        "manual_unit_cost_inc_tax": _money(row.manual_unit_cost_inc_tax),
        "manual_evidence": row.manual_evidence,
        "unit_cost": _money(row.unit_cost),
        "cost_amount": _money(row.cost_amount),
        "unit_cost_ex_tax": _money(row.unit_cost_ex_tax),
        "unit_cost_inc_tax": _money(row.unit_cost_inc_tax),
        "cost_amount_ex_tax": _money(row.cost_amount_ex_tax),
        "cost_amount_inc_tax": _money(row.cost_amount_inc_tax),
        "tax_rate_used": _rate(row.tax_rate_used),
        "cost_source": row.cost_source,
        **cost_evidence_metadata(row.cost_source),
        "price_basis": row.price_basis,
        "reference_side": row.reference_side,
        "reference_sample_ids": row.reference_sample_ids,
        "reference_sample_count": row.reference_sample_count,
        "reference_samples": row.reference_samples,
        "reference_window_from": (
            row.reference_window_from.isoformat() if row.reference_window_from else None
        ),
        "reference_window_to": (
            row.reference_window_to.isoformat() if row.reference_window_to else None
        ),
        "algorithm_version": row.algorithm_version,
        "version": row.version,
    }


def _cost_audit_snapshot(
    row: MaintenanceSiteIssueLine,
    *,
    as_of: date,
) -> dict:
    """Freeze a cost view and its business cutoff inside an append-only audit row."""

    return {**site_issue_line_dict(row), "as_of": as_of.isoformat()}


def site_issue_dict(
    row: MaintenanceSiteIssue,
    lines: list[MaintenanceSiteIssueLine],
    *,
    idempotent_replay: bool = False,
) -> dict:
    return {
        "issue_id": row.issue_id,
        "project_id": row.project_id,
        "issue_no": row.issue_no,
        "issue_date": row.issue_date.isoformat(),
        "workflow_status": row.normalized_status,
        "raw_status": row.raw_status,
        "status_mapping_state": row.status_mapping_state,
        "normalized_status": row.normalized_status,
        "status_mapping_version": row.status_mapping_version,
        "source": row.source,
        "import_batch_id": row.import_batch_id,
        "receiver": row.receiver,
        "issued_by": row.issued_by,
        "site_location": row.site_location,
        "created_by": row.created_by,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "corrected_at": row.corrected_at.isoformat() if row.corrected_at else None,
        "voided_at": row.voided_at.isoformat() if row.voided_at else None,
        "version": row.version,
        "lines": [site_issue_line_dict(line) for line in lines],
        "idempotent_replay": idempotent_replay,
    }


def expense_dict(row: MaintenanceProjectExpenseAttribution) -> dict:
    return {
        "expense_id": row.expense_id,
        "project_id": row.project_id,
        "project_contract_id": row.project_contract_id,
        "expense_ref": row.expense_ref,
        "expense_date": row.expense_date.isoformat(),
        "applicant": row.applicant,
        "category": row.category,
        "expense_reason": row.expense_reason,
        "amount_ex_tax": _money(row.amount_ex_tax),
        "amount_inc_tax": _money(row.amount_inc_tax),
        "tax_rate_used": _rate(row.tax_rate_used),
        "raw_status": row.raw_status,
        "status_mapping_state": row.status_mapping_state,
        "normalized_status": row.normalized_status,
        "status_mapping_version": row.status_mapping_version,
        "version": row.version,
    }


def create_expense(
    db: Session,
    *,
    project_id: str,
    expense_id: str,
    project_contract_id: str | None,
    expense_ref: str,
    expense_date: date,
    applicant: str | None,
    category: str | None,
    expense_reason: str | None,
    amount_ex_tax: Decimal,
    raw_status: str,
    status_mapping_state: str,
    normalized_status: str,
    status_mapping_version: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    if status_mapping_state not in {"mapped", "unmapped"}:
        raise MaintenanceOperationError("报销状态映射结果无效")
    if normalized_status not in {"approved", "rejected", "void", "unknown"}:
        raise MaintenanceOperationError("报销标准状态无效")
    if status_mapping_state == "mapped" and normalized_status == "unknown":
        raise MaintenanceOperationError("mapped 报销不能使用 unknown 标准状态")
    if status_mapping_state != "mapped" and normalized_status != "unknown":
        raise MaintenanceOperationError("未映射报销必须使用 unknown 标准状态")
    amount_ex_tax = _money_amount(amount_ex_tax, label="报销未税金额")
    amount_inc_tax = _money_amount(
        tax_policy.inc_from_ex(amount_ex_tax),
        label="报销含税金额",
    )
    if project_contract_id is not None:
        relation = db.scalar(
            select(MaintenanceProjectContract).where(
                MaintenanceProjectContract.project_contract_id == project_contract_id,
                MaintenanceProjectContract.project_id == project_id,
            )
        )
        if relation is None:
            raise MaintenanceOperationError("报销关联合同不存在或不属于当前项目")
    row = MaintenanceProjectExpenseAttribution(
        expense_id=_required(expense_id, "报销稳定编号"),
        project_id=project_id,
        project_contract_id=project_contract_id,
        expense_ref=_required(expense_ref, "报销单号", 128),
        expense_date=expense_date,
        applicant=applicant.strip() if applicant and applicant.strip() else None,
        category=category.strip() if category and category.strip() else None,
        expense_reason=(
            expense_reason.strip()
            if expense_reason and expense_reason.strip()
            else None
        ),
        amount_ex_tax=amount_ex_tax,
        amount_inc_tax=amount_inc_tax,
        tax_rate_used=tax_policy.TAX_RATE,
        raw_status=_required(raw_status, "报销原始状态"),
        status_mapping_state=status_mapping_state,
        normalized_status=normalized_status,
        status_mapping_version=_required(status_mapping_version, "状态映射版本"),
        version=1,
    )
    db.add(row)
    db.flush()
    payload = expense_dict(row)
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="expense",
        entity_id=row.expense_id,
        action="create",
        before=None,
        after=payload,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return payload


def mark_expense_readiness(
    db: Session,
    *,
    project_id: str,
    ready_through: date,
    expected_version: int | None = None,
    correction_reason: str | None = None,
    reason: str,
    operated_by: str,
) -> dict | None:
    """Confirm that the approved-expense feed is complete through one month."""

    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    if ready_through.day != 1:
        raise MaintenanceOperationError("费用数据水位必须使用当月第一天")
    current_business_month = business_today().replace(day=1)
    if ready_through > current_business_month:
        raise MaintenanceOperationError("费用数据水位不能超过当前业务月份，禁止填写未来月份")
    state = get_or_create_workbook_state(db, project_id=project_id, lock=True)
    is_correction = bool(
        state.expense_ready_through is not None
        and ready_through < state.expense_ready_through
    )
    if is_correction:
        if expected_version is None:
            raise MaintenanceOperationConflict(
                "费用数据水位下调必须提供 expected_version"
            )
        if expected_version != state.revision:
            raise MaintenanceOperationConflict(
                f"费用数据版本冲突：当前版本为 {state.revision}"
            )
        correction_reason = _required(
            correction_reason,
            "费用数据水位纠错原因",
            1000,
        )
    before = {
        "expense_ready_through": (
            state.expense_ready_through.isoformat()
            if state.expense_ready_through
            else None
        ),
        "version": state.revision,
    }
    if state.expense_ready_through == ready_through:
        return {
            "project_id": project_id,
            **before,
            "data_version": state.data_version,
        }
    state.expense_ready_through = ready_through
    state.revision += 1
    state.data_version = _workbook_data_version(project_id, state.revision)
    after = {
        "expense_ready_through": ready_through.isoformat(),
        "version": state.revision,
    }
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="expense_readiness",
        entity_id=project_id,
        action="correct_ready" if is_correction else "mark_ready",
        before=before,
        after=after,
        reason=correction_reason if is_correction else reason,
        operated_by=operated_by,
    )
    db.flush()
    return {
        "project_id": project_id,
        **after,
        "data_version": state.data_version,
    }


def _site_issue_lines(
    db: Session,
    *,
    issue_id: str,
    lock: bool = False,
) -> list[MaintenanceSiteIssueLine]:
    statement = (
        select(MaintenanceSiteIssueLine)
        .where(MaintenanceSiteIssueLine.issue_id == issue_id)
        .order_by(MaintenanceSiteIssueLine.line_no)
    )
    if lock:
        statement = statement.with_for_update()
    return list(db.scalars(statement))


def _site_issue_request_fingerprint(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _lock_idempotency_key(db: Session, idempotency_key: str) -> None:
    """Serialize one client command without persisting sensitive request content."""

    db.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(idempotency_key, 0)
            )
        )
    )


def _site_issue_command_fingerprint(
    *,
    action: str,
    issue_id: str,
    project_id: str,
    version: int,
    reason: str,
) -> str:
    return _site_issue_request_fingerprint(
        {
            "action": action,
            "issue_id": issue_id,
            "project_id": project_id,
            "version": version,
            "reason": reason,
        }
    )


def _site_issue_command_replay(
    db: Session,
    *,
    idempotency_key: str,
    action: str,
    issue_id: str,
    project_id: str,
    request_fingerprint: str,
) -> dict | None:
    row = db.scalar(
        select(MaintenanceSiteIssueCommand).where(
            MaintenanceSiteIssueCommand.idempotency_key == idempotency_key
        )
    )
    if row is None:
        return None
    if (
        row.action != action
        or row.issue_id != issue_id
        or row.project_id != project_id
        or row.request_fingerprint != request_fingerprint
    ):
        raise MaintenanceOperationConflict("幂等键已用于不同的现场领用操作")
    return {**row.response_json, "idempotent_replay": True}


def _record_site_issue_command(
    db: Session,
    *,
    idempotency_key: str,
    action: str,
    issue_id: str,
    project_id: str,
    request_fingerprint: str,
    response: dict,
) -> None:
    db.add(
        MaintenanceSiteIssueCommand(
            command_id=str(uuid4()),
            idempotency_key=idempotency_key,
            project_id=project_id,
            issue_id=issue_id,
            action=action,
            request_fingerprint=request_fingerprint,
            response_json=response,
        )
    )


def _return_event_dict(row: MaintenanceSiteIssueReturnEvent) -> dict:
    return {
        "event_id": row.event_id,
        "event_type": row.event_type,
        "project_id": row.project_id,
        "issue_id": row.issue_id,
        "issue_version": row.issue_version,
        "payload": row.payload,
        "downstream_reference": row.downstream_reference,
    }


def _consume_site_issue_return_event(
    db: Session,
    event: MaintenanceSiteIssueReturnEvent,
) -> None:
    """Synchronously project #207's outbox event into #208 obligations."""

    try:
        maintenance_bad_returns.consume_return_event(db, event)
    except maintenance_bad_returns.BadReturnConflict as exc:
        raise MaintenanceOperationConflict(str(exc)) from exc
    except maintenance_bad_returns.BadReturnError as exc:
        raise MaintenanceOperationError(str(exc)) from exc


def _site_issue_return_payload(
    issue: MaintenanceSiteIssue,
    lines: list[MaintenanceSiteIssueLine],
) -> dict:
    """Expose only stable return-obligation inputs, never inventory mutations."""

    return {
        "schema_version": "maintenance-return-obligation-interface-v1",
        "project_id": issue.project_id,
        "issue_id": issue.issue_id,
        "issue_no": issue.issue_no,
        "issue_date": issue.issue_date.isoformat(),
        "receiver": issue.receiver,
        "issued_by": issue.issued_by,
        "site_location": issue.site_location,
        "lines": [
            {
                "issue_line_id": line.issue_line_id,
                "delivery_line_id": line.delivery_line_id,
                "source_order_id": line.source_order_id,
                "source_line_id": line.source_line_id,
                "part_id": line.part_id,
                "pn": line.pn,
                "serial_number": line.serial_number,
                "no_return": line.no_return,
                "quantity": _qty(line.quantity),
            }
            for line in lines
        ],
    }


def _clone_site_issue_line(line: MaintenanceSiteIssueLine) -> MaintenanceSiteIssueLine:
    """Make an untracked copy so preview can resolve evidence without saving it."""

    return MaintenanceSiteIssueLine(
        **{
            column.key: getattr(line, column.key)
            for column in MaintenanceSiteIssueLine.__table__.columns
            if column.key not in {"created_at", "updated_at"}
        }
    )


def _locked_site_issue_sources(
    db: Session,
    *,
    delivery_line_ids: set[str],
    lock: bool,
) -> dict[str, MaintenanceSiteIssueDeliverySource]:
    statement = (
        select(MaintenanceSiteIssueDeliverySource)
        .where(
            MaintenanceSiteIssueDeliverySource.delivery_line_id.in_(
                sorted(delivery_line_ids)
            )
        )
        .order_by(MaintenanceSiteIssueDeliverySource.delivery_line_id)
    )
    if lock:
        statement = statement.with_for_update()
    return {
        row.delivery_line_id: row
        for row in db.scalars(statement)
    }


def _confirmed_site_issue_quantities(
    db: Session,
    *,
    delivery_line_ids: set[str],
    exclude_issue_id: str | None = None,
) -> dict[str, Decimal]:
    filters = [
        MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
        MaintenanceSiteIssueLine.delivery_line_id.in_(sorted(delivery_line_ids)),
        # 2026-08-19：被 03 行作废级联软删的领用行不计入已领用量（#55）
        MaintenanceSiteIssueLine.is_active.is_(True),
    ]
    if exclude_issue_id is not None:
        filters.append(MaintenanceSiteIssue.issue_id != exclude_issue_id)
    return {
        delivery_line_id: Decimal(quantity)
        for delivery_line_id, quantity in db.execute(
            select(
                MaintenanceSiteIssueLine.delivery_line_id,
                func.sum(MaintenanceSiteIssueLine.quantity),
            )
            .join(
                MaintenanceSiteIssue,
                MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
            )
            .where(*filters)
            .group_by(MaintenanceSiteIssueLine.delivery_line_id)
        )
    }


def _validate_site_issue_sources(
    *,
    issue: MaintenanceSiteIssue,
    lines: list[MaintenanceSiteIssueLine],
    sources: dict[str, MaintenanceSiteIssueDeliverySource],
    confirmed_quantities: dict[str, Decimal],
) -> tuple[list[dict], list[str]]:
    balances: list[dict] = []
    blockers: list[str] = []
    if not lines:
        blockers.append("现场领用单没有明细")
        return balances, blockers
    for line in lines:
        delivery_line_id = str(line.delivery_line_id or "").strip()
        source = sources.get(delivery_line_id)
        if not delivery_line_id or source is None:
            blockers.append(f"第 {line.line_no} 行缺少稳定发货来源")
            continue
        if (
            source.adapter_key
            not in maintenance_warehouse_site_issue_bridge.SUPPORTED_DELIVERY_ADAPTERS
            or source.mapping_state != "ready"
            or not source.is_active
        ):
            blockers.append(f"第 {line.line_no} 行发货来源适配器不可用")
            continue
        if source.project_id != issue.project_id:
            blockers.append(f"第 {line.line_no} 行发货来源不属于当前项目")
            continue
        if (
            source.part_id != line.part_id
            or not str(source.pn or "").strip()
            or source.pn != line.pn
            or source.source_order_id != line.source_order_id
            or source.source_line_id != line.source_line_id
        ):
            blockers.append(f"第 {line.line_no} 行来源身份或 PN 已变化")
            continue
        try:
            requested = _quantity(line.quantity)
        except MaintenanceOperationError:
            blockers.append(f"第 {line.line_no} 行领用数量必须为正数")
            continue
        confirmed = confirmed_quantities.get(delivery_line_id, Decimal("0"))
        available = Decimal(source.delivered_quantity) - confirmed
        balances.append(
            {
                "issue_line_id": line.issue_line_id,
                "delivery_line_id": delivery_line_id,
                "delivered_quantity": _qty(Decimal(source.delivered_quantity)),
                "confirmed_quantity": _qty(confirmed),
                "available_quantity": _qty(max(available, Decimal("0"))),
                "requested_quantity": _qty(requested),
            }
        )
        if requested > available:
            blockers.append(f"第 {line.line_no} 行领用数量超过可用发货余额")
    return balances, blockers


def _site_issue_is_production_blocked() -> bool:
    return get_settings().environment == "prod"


def _site_issue_sources_are_production_ready(
    sources: dict[str, MaintenanceSiteIssueDeliverySource],
) -> bool:
    return bool(sources) and all(
        source.adapter_key
        == maintenance_warehouse_site_issue_bridge.WAREHOUSE_DELIVERY_ADAPTER
        for source in sources.values()
    )


def _site_issue_adapter_profile(
    db: Session,
    *,
    project_id: str,
) -> tuple[dict, tuple[str, ...]]:
    counts = {
        adapter_key: int(count)
        for adapter_key, count in db.execute(
            select(
                MaintenanceSiteIssueDeliverySource.adapter_key,
                func.count(),
            )
            .where(
                MaintenanceSiteIssueDeliverySource.project_id == project_id,
                MaintenanceSiteIssueDeliverySource.mapping_state == "ready",
                MaintenanceSiteIssueDeliverySource.is_active.is_(True),
            )
            .group_by(MaintenanceSiteIssueDeliverySource.adapter_key)
        )
    }
    warehouse_key = maintenance_warehouse_site_issue_bridge.WAREHOUSE_DELIVERY_ADAPTER
    synthetic_key = "synthetic_delivery_v1"
    if counts.get(warehouse_key, 0) > 0:
        return (
            {
                "key": warehouse_key,
                "state": "warehouse_ready",
                "production_ready": True,
                "detail": "仅展示已确认且稳定关联到当前项目的仓库发货明细",
            },
            (warehouse_key,),
        )
    if get_settings().environment != "prod" and counts.get(synthetic_key, 0) > 0:
        return (
            {
                "key": synthetic_key,
                "state": "synthetic_ready",
                "production_ready": False,
                "detail": "当前仅启用稳定合成发货契约；真实适配器接入前不得用于生产确认",
            },
            (synthetic_key,),
        )
    if get_settings().environment != "prod":
        return (
            {
                "key": synthetic_key,
                "state": "unavailable",
                "production_ready": False,
                "detail": "真实 WBDD/仓库发货适配器尚未接入，系统不会按项目名猜测",
            },
            (),
        )
    return (
        {
            "key": warehouse_key,
            "state": "unavailable",
            "production_ready": False,
            "detail": "尚无已确认并完成 WBDD、项目和 PN 稳定关联的仓库发货明细",
        },
        (),
    )


def preview_site_issue(
    db: Session,
    *,
    issue_id: str,
    project_id: str,
    version: int,
) -> dict | None:
    issue = db.scalar(
        select(MaintenanceSiteIssue).where(MaintenanceSiteIssue.issue_id == issue_id)
    )
    if issue is None:
        return None
    if issue.project_id != project_id:
        raise MaintenanceOperationPermissionError("现场领用单不属于当前稳定项目")
    if issue.source != "site_issue_v2":
        raise MaintenanceOperationError("旧版现场领用单不支持此确认流程")
    if issue.version != version:
        raise MaintenanceOperationConflict("现场领用单版本已变化，请刷新后重试")
    if issue.normalized_status != "draft":
        raise MaintenanceOperationConflict("仅草稿现场领用单可以预览确认影响")

    lines = _site_issue_lines(db, issue_id=issue_id)
    delivery_ids = {line.delivery_line_id for line in lines if line.delivery_line_id}
    sources = _locked_site_issue_sources(
        db,
        delivery_line_ids=delivery_ids,
        lock=False,
    )
    confirmed = _confirmed_site_issue_quantities(
        db,
        delivery_line_ids=delivery_ids,
    )
    balances, blockers = _validate_site_issue_sources(
        issue=issue,
        lines=lines,
        sources=sources,
        confirmed_quantities=confirmed,
    )
    if _site_issue_is_production_blocked() and not (
        _site_issue_sources_are_production_ready(sources)
    ):
        blockers.append("生产确认只接受已确认仓库发货明细，合成来源已失败关闭")

    preview_lines = [_clone_site_issue_line(line) for line in lines]
    maintenance_consumption_cost.resolve_lines(
        db,
        lines=[(issue.issue_date, line) for line in preview_lines],
    )
    balance_by_line = {row["issue_line_id"]: row for row in balances}
    return {
        **site_issue_dict(issue, preview_lines),
        "can_confirm": not blockers,
        "blockers": blockers,
        "inventory_effect": "none",
        "lines": [
            {
                **site_issue_line_dict(line),
                **balance_by_line.get(line.issue_line_id, {}),
                "cost_gap": line.cost_source is None,
            }
            for line in preview_lines
        ],
    }


def confirm_site_issue(
    db: Session,
    *,
    issue_id: str,
    project_id: str,
    version: int,
    idempotency_key: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    clean_key = _required(idempotency_key, "幂等键", 128)
    if len(clean_key) < 8:
        raise MaintenanceOperationError("幂等键至少需要 8 个字符")
    clean_reason = _required(reason, "操作原因", 1000)
    fingerprint = _site_issue_command_fingerprint(
        action="confirm",
        issue_id=issue_id,
        project_id=project_id,
        version=version,
        reason=clean_reason,
    )
    _lock_idempotency_key(db, clean_key)
    replay = _site_issue_command_replay(
        db,
        idempotency_key=clean_key,
        action="confirm",
        issue_id=issue_id,
        project_id=project_id,
        request_fingerprint=fingerprint,
    )
    if replay is not None:
        return replay

    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    issue = db.scalar(
        select(MaintenanceSiteIssue)
        .where(MaintenanceSiteIssue.issue_id == issue_id)
        .with_for_update()
    )
    if issue is None:
        return None
    if issue.project_id != project_id:
        raise MaintenanceOperationPermissionError("现场领用单不属于当前稳定项目")
    if issue.source != "site_issue_v2":
        raise MaintenanceOperationError("旧版现场领用单不支持此确认流程")
    if issue.version != version:
        raise MaintenanceOperationConflict("现场领用单版本已变化，请刷新后重试")
    if issue.normalized_status != "draft":
        raise MaintenanceOperationConflict("仅草稿现场领用单可以确认")

    lines = _site_issue_lines(db, issue_id=issue_id, lock=True)
    delivery_ids = {line.delivery_line_id for line in lines if line.delivery_line_id}
    maintenance_warehouse_site_issue_bridge.synchronize_delivery_sources(
        db,
        delivery_line_ids=delivery_ids,
    )
    sources = _locked_site_issue_sources(
        db,
        delivery_line_ids=delivery_ids,
        lock=True,
    )
    confirmed = _confirmed_site_issue_quantities(
        db,
        delivery_line_ids=delivery_ids,
    )
    _balances, blockers = _validate_site_issue_sources(
        issue=issue,
        lines=lines,
        sources=sources,
        confirmed_quantities=confirmed,
    )
    if _site_issue_is_production_blocked() and not (
        _site_issue_sources_are_production_ready(sources)
    ):
        raise MaintenanceOperationError(
            "生产确认只接受已确认仓库发货明细，合成来源已失败关闭"
        )
    if blockers:
        raise MaintenanceOperationConflict("；".join(blockers))

    before = site_issue_dict(issue, lines)
    maintenance_consumption_cost.resolve_lines(
        db,
        lines=[(issue.issue_date, line) for line in lines],
    )
    now = datetime.now(UTC)
    issue.raw_status = "confirmed"
    issue.status_mapping_state = "mapped"
    issue.normalized_status = "confirmed"
    issue.status_mapping_version = "site-issue-v2-workflow-v1"
    issue.confirmed_at = now
    issue.version += 1
    for line in lines:
        line.version += 1

    event = MaintenanceSiteIssueReturnEvent(
        event_id=str(uuid4()),
        project_id=project_id,
        issue_id=issue_id,
        event_type="return_obligation_created",
        issue_version=issue.version,
        payload=_site_issue_return_payload(issue, lines),
    )
    db.add(event)
    db.flush()
    _consume_site_issue_return_event(db, event)
    response = {
        **site_issue_dict(issue, lines),
        "return_obligation_event": _return_event_dict(event),
        "inventory_effect": "none",
        "idempotent_replay": False,
    }
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="site_issue",
        entity_id=issue_id,
        action="confirm",
        before=before,
        after=response,
        reason=clean_reason,
        operated_by=operated_by,
    )
    _record_site_issue_command(
        db,
        idempotency_key=clean_key,
        action="confirm",
        issue_id=issue_id,
        project_id=project_id,
        request_fingerprint=fingerprint,
        response=response,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return response


def _normalize_site_issue_patch_lines(lines: list[dict]) -> list[dict]:
    if not lines:
        raise MaintenanceOperationError("现场领用至少需要一条明细")
    if len(lines) > _SITE_ISSUE_LINE_LIMIT:
        raise MaintenanceOperationError("现场领用单最多允许 200 条明细")
    normalized: list[dict] = []
    seen: set[str] = set()
    for raw_line in lines:
        delivery_line_id = _required(
            raw_line.get("delivery_line_id"), "发货明细稳定编号"
        )
        if delivery_line_id in seen:
            raise MaintenanceOperationError("同一发货明细不能在一张领用单中重复")
        seen.add(delivery_line_id)
        no_return = raw_line.get("no_return")
        if no_return is not None and not isinstance(no_return, bool):
            raise MaintenanceOperationError("不返还标记必须是是/否或留空")
        normalized.append(
            {
                "delivery_line_id": delivery_line_id,
                "quantity": _quantity(raw_line["quantity"]),
                "no_return": no_return,
            }
        )
    return normalized


def _build_site_issue_lines(
    *,
    issue_id: str,
    project_id: str,
    requested_lines: list[dict],
    sources: dict[str, MaintenanceSiteIssueDeliverySource],
) -> list[MaintenanceSiteIssueLine]:
    if len(sources) != len(requested_lines):
        raise MaintenanceOperationError("发货来源适配器未提供完整稳定身份")
    result: list[MaintenanceSiteIssueLine] = []
    for line_no, requested in enumerate(requested_lines, start=1):
        source = sources.get(requested["delivery_line_id"])
        if source is None:
            raise MaintenanceOperationError("发货来源适配器未提供完整稳定身份")
        if (
            source.adapter_key
            not in maintenance_warehouse_site_issue_bridge.SUPPORTED_DELIVERY_ADAPTERS
            or source.mapping_state != "ready"
            or not source.is_active
        ):
            raise MaintenanceOperationError("发货来源适配器当前不可用")
        if source.project_id != project_id:
            raise MaintenanceOperationPermissionError("发货明细不属于当前稳定项目")
        result.append(
            MaintenanceSiteIssueLine(
                issue_line_id=str(uuid4()),
                issue_id=issue_id,
                line_no=line_no,
                part_id=source.part_id,
                pn=_required(source.pn, "料号", 128),
                quantity=requested["quantity"],
                delivery_line_id=source.delivery_line_id,
                source_order_id=source.source_order_id,
                source_line_id=source.source_line_id,
                serial_number=source.serial_number,
                no_return=requested.get("no_return"),
                linked_purchase_line_id=source.linked_purchase_line_id,
                manual_unit_cost=None,
                reference_sample_ids=[],
                reference_sample_count=0,
                reference_samples=[],
                algorithm_version=maintenance_consumption_cost.ALGORITHM_VERSION,
                version=1,
            )
        )
    return result


def patch_site_issue(
    db: Session,
    *,
    issue_id: str,
    project_id: str,
    version: int,
    idempotency_key: str,
    issue_date: date | None,
    receiver: str | None,
    issued_by: str | None,
    site_location: str | None,
    lines: list[dict] | None,
    reason: str,
    operated_by: str,
) -> dict | None:
    if all(
        value is None
        for value in (issue_date, receiver, issued_by, site_location, lines)
    ):
        raise MaintenanceOperationError("没有可修改的现场领用业务字段")
    clean_key = _required(idempotency_key, "幂等键", 128)
    if len(clean_key) < 8:
        raise MaintenanceOperationError("幂等键至少需要 8 个字符")
    clean_reason = _required(reason, "操作原因", 1000)
    normalized_lines = (
        _normalize_site_issue_patch_lines(lines) if lines is not None else None
    )
    request_payload = {
        "action": "patch",
        "issue_id": issue_id,
        "project_id": project_id,
        "version": version,
        "issue_date": issue_date.isoformat() if issue_date else None,
        "receiver": receiver,
        "issued_by": issued_by,
        "site_location": site_location,
        "lines": (
            [
                {
                    "delivery_line_id": line["delivery_line_id"],
                    "quantity": _qty(line["quantity"]),
                    "no_return": line.get("no_return"),
                }
                for line in normalized_lines
            ]
            if normalized_lines is not None
            else None
        ),
        "reason": clean_reason,
    }
    fingerprint = _site_issue_request_fingerprint(request_payload)
    _lock_idempotency_key(db, clean_key)

    # The receipt action differs by state; either one is a valid replay target.
    existing_command = db.scalar(
        select(MaintenanceSiteIssueCommand).where(
            MaintenanceSiteIssueCommand.idempotency_key == clean_key
        )
    )
    if existing_command is not None:
        if (
            existing_command.action not in {"update", "correct"}
            or existing_command.issue_id != issue_id
            or existing_command.project_id != project_id
            or existing_command.request_fingerprint != fingerprint
        ):
            raise MaintenanceOperationConflict("幂等键已用于不同的现场领用操作")
        return {**existing_command.response_json, "idempotent_replay": True}

    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    issue = db.scalar(
        select(MaintenanceSiteIssue)
        .where(MaintenanceSiteIssue.issue_id == issue_id)
        .with_for_update()
    )
    if issue is None:
        return None
    if issue.project_id != project_id:
        raise MaintenanceOperationPermissionError("现场领用单不属于当前稳定项目")
    if issue.source != "site_issue_v2":
        raise MaintenanceOperationError("旧版现场领用单不支持此编辑流程")
    if issue.version != version:
        raise MaintenanceOperationConflict("现场领用单版本已变化，请刷新后重试")
    if issue.normalized_status not in {"draft", "confirmed", "corrected"}:
        raise MaintenanceOperationConflict("已作废现场领用单不能编辑")
    is_correction = issue.normalized_status in {"confirmed", "corrected"}
    if is_correction:
        # A pre-#208 confirmation event may still be waiting for projection.
        # Drain older project events before writing the newer correction so a
        # later read cannot replay stale quantities over the corrected facts.
        try:
            maintenance_bad_returns.consume_pending_return_events(
                db,
                project_id=project_id,
            )
        except (
            maintenance_bad_returns.BadReturnConflict,
            maintenance_bad_returns.BadReturnError,
        ) as exc:
            raise MaintenanceOperationConflict(str(exc)) from exc

    old_lines = _site_issue_lines(db, issue_id=issue_id, lock=True)
    requested_ids = {
        line["delivery_line_id"] for line in (normalized_lines or [])
    }
    all_delivery_ids = {
        line.delivery_line_id for line in old_lines if line.delivery_line_id
    } | requested_ids
    maintenance_warehouse_site_issue_bridge.synchronize_delivery_sources(
        db,
        delivery_line_ids=all_delivery_ids,
    )
    sources = _locked_site_issue_sources(
        db,
        delivery_line_ids=all_delivery_ids,
        lock=is_correction,
    )
    replacement_lines = (
        _build_site_issue_lines(
            issue_id=issue_id,
            project_id=project_id,
            requested_lines=normalized_lines,
            sources={key: sources[key] for key in requested_ids if key in sources},
        )
        if normalized_lines is not None
        else old_lines
    )
    if (
        is_correction
        and _site_issue_is_production_blocked()
        and not _site_issue_sources_are_production_ready(sources)
    ):
        raise MaintenanceOperationError(
            "生产更正确认只接受已确认仓库发货明细，合成来源已失败关闭"
        )
    candidate_issue_date = issue_date or issue.issue_date
    candidate_receiver = (
        _required(receiver, "接收人", 128) if receiver is not None else issue.receiver
    )
    candidate_issued_by = (
        _required(issued_by, "领用发出人", 128)
        if issued_by is not None
        else issue.issued_by
    )
    candidate_location = (
        _required(site_location, "现场位置", 256)
        if site_location is not None
        else issue.site_location
    )
    old_line_signature = [
        (line.delivery_line_id, _qty(Decimal(line.quantity))) for line in old_lines
    ]
    candidate_line_signature = [
        (line.delivery_line_id, _qty(Decimal(line.quantity)))
        for line in replacement_lines
    ]
    line_inputs_changed = candidate_line_signature != old_line_signature
    date_changed = candidate_issue_date != issue.issue_date
    metadata_changed = (
        candidate_receiver != issue.receiver
        or candidate_issued_by != issue.issued_by
        or candidate_location != issue.site_location
    )
    if not (line_inputs_changed or date_changed or metadata_changed):
        raise MaintenanceOperationError("现场领用业务内容没有变化")
    if normalized_lines is not None and not line_inputs_changed:
        # A client may resend the visible lines while changing only receiver or
        # location. Keep the original server-owned line identities and frozen
        # cost evidence instead of manufacturing replacement lines.
        replacement_lines = old_lines
    before = site_issue_dict(issue, old_lines)

    event: MaintenanceSiteIssueReturnEvent | None = None
    if is_correction:
        confirmed = _confirmed_site_issue_quantities(
            db,
            delivery_line_ids={
                line.delivery_line_id
                for line in replacement_lines
                if line.delivery_line_id
            },
            exclude_issue_id=issue_id,
        )
        _balances, blockers = _validate_site_issue_sources(
            issue=issue,
            lines=replacement_lines,
            sources=sources,
            confirmed_quantities=confirmed,
        )
        if blockers:
            raise MaintenanceOperationConflict("；".join(blockers))
        if line_inputs_changed or date_changed:
            maintenance_consumption_cost.resolve_lines(
                db,
                lines=[(candidate_issue_date, line) for line in replacement_lines],
            )

    if normalized_lines is not None and line_inputs_changed:
        for old_line in old_lines:
            db.delete(old_line)
        db.flush()
        db.add_all(replacement_lines)
    elif is_correction:
        for line in replacement_lines:
            line.version += 1

    issue.issue_date = candidate_issue_date
    issue.receiver = candidate_receiver
    issue.issued_by = candidate_issued_by
    issue.site_location = candidate_location
    issue.version += 1
    action = "correct" if is_correction else "update"
    if is_correction:
        issue.raw_status = "corrected"
        issue.status_mapping_state = "mapped"
        issue.normalized_status = "corrected"
        issue.status_mapping_version = "site-issue-v2-workflow-v1"
        issue.corrected_at = datetime.now(UTC)
        event = MaintenanceSiteIssueReturnEvent(
            event_id=str(uuid4()),
            project_id=project_id,
            issue_id=issue_id,
            event_type="return_obligation_corrected",
            issue_version=issue.version,
            payload=_site_issue_return_payload(issue, replacement_lines),
        )
        db.add(event)
        db.flush()
        _consume_site_issue_return_event(db, event)
    db.flush()
    response = {
        **site_issue_dict(issue, replacement_lines),
        "return_obligation_event": _return_event_dict(event) if event else None,
        "inventory_effect": "none",
        "idempotent_replay": False,
    }
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="site_issue",
        entity_id=issue_id,
        action="correct" if is_correction else "draft_update",
        before=before,
        after=response,
        reason=clean_reason,
        operated_by=operated_by,
    )
    _record_site_issue_command(
        db,
        idempotency_key=clean_key,
        action=action,
        issue_id=issue_id,
        project_id=project_id,
        request_fingerprint=fingerprint,
        response=response,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return response


def void_site_issue(
    db: Session,
    *,
    issue_id: str,
    project_id: str,
    version: int,
    idempotency_key: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    clean_key = _required(idempotency_key, "幂等键", 128)
    if len(clean_key) < 8:
        raise MaintenanceOperationError("幂等键至少需要 8 个字符")
    clean_reason = _required(reason, "操作原因", 1000)
    fingerprint = _site_issue_command_fingerprint(
        action="void",
        issue_id=issue_id,
        project_id=project_id,
        version=version,
        reason=clean_reason,
    )
    _lock_idempotency_key(db, clean_key)
    replay = _site_issue_command_replay(
        db,
        idempotency_key=clean_key,
        action="void",
        issue_id=issue_id,
        project_id=project_id,
        request_fingerprint=fingerprint,
    )
    if replay is not None:
        return replay

    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    issue = db.scalar(
        select(MaintenanceSiteIssue)
        .where(MaintenanceSiteIssue.issue_id == issue_id)
        .with_for_update()
    )
    if issue is None:
        return None
    if issue.project_id != project_id:
        raise MaintenanceOperationPermissionError("现场领用单不属于当前稳定项目")
    if issue.source != "site_issue_v2":
        raise MaintenanceOperationError("旧版现场领用单不支持此作废流程")
    if issue.version != version:
        raise MaintenanceOperationConflict("现场领用单版本已变化，请刷新后重试")
    if issue.normalized_status == "void":
        raise MaintenanceOperationConflict("现场领用单已经作废")
    if issue.normalized_status not in {"draft", "confirmed", "corrected"}:
        raise MaintenanceOperationConflict("当前现场领用状态不能作废")

    was_confirmed = issue.normalized_status in {"confirmed", "corrected"}
    if was_confirmed:
        # A confirmed issue may have an outbox event that has not yet been
        # projected. Drain every earlier event for the stable project before
        # creating the void event so a delayed projector can never resurrect
        # the withdrawn obligation.
        try:
            maintenance_bad_returns.consume_pending_return_events(
                db,
                project_id=project_id,
            )
        except (
            maintenance_bad_returns.BadReturnConflict,
            maintenance_bad_returns.BadReturnError,
        ) as exc:
            raise MaintenanceOperationConflict(str(exc)) from exc

    return_events = list(
        db.scalars(
            select(MaintenanceSiteIssueReturnEvent)
            .where(MaintenanceSiteIssueReturnEvent.issue_id == issue_id)
            .order_by(MaintenanceSiteIssueReturnEvent.created_at)
            .with_for_update()
        )
    )
    if any(
        event.downstream_reference
        and not event.downstream_reference.startswith(
            "maintenance-return-obligations:"
        )
        for event in return_events
    ):
        raise MaintenanceOperationConflict(
            "返还事件已被未知下游消费，不能直接作废"
        )

    lines = _site_issue_lines(db, issue_id=issue_id, lock=True)
    before = site_issue_dict(issue, lines)
    issue.raw_status = "void"
    issue.status_mapping_state = "mapped"
    issue.normalized_status = "void"
    issue.status_mapping_version = "site-issue-v2-workflow-v1"
    issue.voided_at = datetime.now(UTC)
    issue.version += 1
    event = None
    if was_confirmed:
        event = MaintenanceSiteIssueReturnEvent(
            event_id=str(uuid4()),
            project_id=project_id,
            issue_id=issue_id,
            event_type="return_obligation_voided",
            issue_version=issue.version,
            payload=_site_issue_return_payload(issue, lines),
        )
        db.add(event)
        db.flush()
        _consume_site_issue_return_event(db, event)
    db.flush()
    response = {
        **site_issue_dict(issue, lines),
        "return_obligation_event": _return_event_dict(event) if event else None,
        "inventory_effect": "none",
        "idempotent_replay": False,
    }
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="site_issue",
        entity_id=issue_id,
        action="void",
        before=before,
        after=response,
        reason=clean_reason,
        operated_by=operated_by,
    )
    _record_site_issue_command(
        db,
        idempotency_key=clean_key,
        action="void",
        issue_id=issue_id,
        project_id=project_id,
        request_fingerprint=fingerprint,
        response=response,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return response


def search_site_issues(
    db: Session,
    *,
    project_id: str,
    q_text: str | None,
    workflow_statuses: list[str],
    page: int,
    page_size: int,
) -> dict | None:
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        return None
    allowed = {"draft", "confirmed", "corrected", "void"}
    if not workflow_statuses or any(item not in allowed for item in workflow_statuses):
        raise MaintenanceOperationError("现场领用状态筛选无效")
    filters = [
        MaintenanceSiteIssue.project_id == project_id,
        # 2026-08-18：search 是只读展示，须显示工作簿(workbook)/旧数据(legacy)/线上(site_issue_v2)
        # 全部来源的领用单；之前硬编码 site_issue_v2 导致工作簿上传的领用数据面板不显示
        MaintenanceSiteIssue.source.in_(["site_issue_v2", "workbook", "legacy"]),
        MaintenanceSiteIssue.normalized_status.in_(workflow_statuses),
    ]
    q = (q_text or "").strip()
    if len(q) > 256:
        raise MaintenanceOperationError("现场领用搜索条件无效")
    if q:
        filters.append(
            or_(
                MaintenanceSiteIssue.issue_no.icontains(q, autoescape=True),
                MaintenanceSiteIssue.receiver.icontains(q, autoescape=True),
                MaintenanceSiteIssue.issued_by.icontains(q, autoescape=True),
                MaintenanceSiteIssue.site_location.icontains(q, autoescape=True),
            )
        )
    total = int(
        db.scalar(
            select(func.count())
            .select_from(MaintenanceSiteIssue)
            .where(*filters)
        )
        or 0
    )
    issues = list(
        db.scalars(
            select(MaintenanceSiteIssue)
            .where(*filters)
            .order_by(
                MaintenanceSiteIssue.issue_date.desc(),
                MaintenanceSiteIssue.issue_no.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    line_rows = list(
        db.scalars(
            select(MaintenanceSiteIssueLine)
            .where(
                MaintenanceSiteIssueLine.issue_id.in_(
                    [issue.issue_id for issue in issues]
                ),
                # 2026-08-19：03 行作废级联软删的领用行不在面板展示（#55）
                MaintenanceSiteIssueLine.is_active.is_(True),
            )
            .order_by(
                MaintenanceSiteIssueLine.issue_id,
                MaintenanceSiteIssueLine.line_no,
            )
        )
    ) if issues else []
    by_issue: dict[str, list[MaintenanceSiteIssueLine]] = defaultdict(list)
    for line in line_rows:
        by_issue[line.issue_id].append(line)
    adapter, _candidate_adapters = _site_issue_adapter_profile(
        db,
        project_id=project_id,
    )
    return {
        "project_id": project_id,
        "rows": [site_issue_dict(issue, by_issue[issue.issue_id]) for issue in issues],
        "total": total,
        "page": page,
        "page_size": page_size,
        "adapter": adapter,
    }


def search_site_issue_candidates(
    db: Session,
    *,
    project_id: str,
    q_text: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict | None:
    """List explicit delivery identities; never infer a source from project names."""

    project = db.scalar(
        select(MaintenanceProject).where(MaintenanceProject.project_id == project_id)
    )
    if project is None:
        return None
    adapter, candidate_adapters = _site_issue_adapter_profile(
        db,
        project_id=project_id,
    )
    if not candidate_adapters:
        return {
            "adapter": adapter,
            "rows": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
        }

    consumed = (
        select(
            MaintenanceSiteIssueLine.delivery_line_id.label("delivery_line_id"),
            func.coalesce(func.sum(MaintenanceSiteIssueLine.quantity), 0).label(
                "confirmed_quantity"
            ),
        )
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
        .where(
            MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
            MaintenanceSiteIssueLine.delivery_line_id.is_not(None),
            # 2026-08-19：作废领用行不占用发货可领余量（#55）
            MaintenanceSiteIssueLine.is_active.is_(True),
        )
        .group_by(MaintenanceSiteIssueLine.delivery_line_id)
        .subquery()
    )
    confirmed_quantity = func.coalesce(consumed.c.confirmed_quantity, 0)
    filters = [
        MaintenanceSiteIssueDeliverySource.project_id == project_id,
        MaintenanceSiteIssueDeliverySource.adapter_key.in_(candidate_adapters),
        MaintenanceSiteIssueDeliverySource.mapping_state == "ready",
        MaintenanceSiteIssueDeliverySource.is_active.is_(True),
    ]
    q = (q_text or "").strip()
    if len(q) > 256:
        raise MaintenanceOperationError("发货候选搜索条件无效")
    if q:
        filters.append(
            or_(
                MaintenanceSiteIssueDeliverySource.delivery_line_id.icontains(
                    q, autoescape=True
                ),
                MaintenanceSiteIssueDeliverySource.delivery_no.icontains(
                    q, autoescape=True
                ),
                MaintenanceSiteIssueDeliverySource.source_order_id.icontains(
                    q, autoescape=True
                ),
                MaintenanceSiteIssueDeliverySource.pn.icontains(q, autoescape=True),
                MaintenanceSiteIssueDeliverySource.serial_number.icontains(
                    q, autoescape=True
                ),
            )
        )
    total = int(
        db.scalar(
            select(func.count())
            .select_from(MaintenanceSiteIssueDeliverySource)
            .where(*filters)
        )
        or 0
    )
    rows = list(
        db.execute(
            select(
                MaintenanceSiteIssueDeliverySource,
                confirmed_quantity.label("confirmed_quantity"),
            )
            .outerjoin(
                consumed,
                consumed.c.delivery_line_id
                == MaintenanceSiteIssueDeliverySource.delivery_line_id,
            )
            .where(*filters)
            .order_by(
                MaintenanceSiteIssueDeliverySource.delivery_date.desc(),
                MaintenanceSiteIssueDeliverySource.delivery_line_id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return {
        "adapter": adapter,
        "rows": [
            {
                "delivery_line_id": source.delivery_line_id,
                "source_order_id": source.source_order_id,
                "source_line_id": source.source_line_id,
                "delivery_no": source.delivery_no,
                "delivery_date": source.delivery_date.isoformat(),
                "part_id": source.part_id,
                "pn": source.pn,
                "serial_number": source.serial_number,
                "delivered_quantity": _qty(source.delivered_quantity),
                "confirmed_quantity": _qty(Decimal(confirmed_qty)),
                "available_quantity": _qty(
                    max(
                        Decimal(source.delivered_quantity) - Decimal(confirmed_qty),
                        Decimal("0"),
                    )
                ),
                "mapping_state": source.mapping_state,
                "mapping_version": source.mapping_version,
            }
            for source, confirmed_qty in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def create_site_issue_draft(
    db: Session,
    *,
    project_id: str,
    idempotency_key: str,
    issue_date: date,
    receiver: str,
    issued_by: str,
    site_location: str,
    lines: list[dict],
    reason: str,
    operated_by: str,
) -> dict | None:
    """Create one server-owned draft from explicit stable delivery identities."""

    clean_key = _required(idempotency_key, "幂等键", 128)
    if len(clean_key) < 8:
        raise MaintenanceOperationError("幂等键至少需要 8 个字符")
    clean_receiver = _required(receiver, "接收人", 128)
    clean_issued_by = _required(issued_by, "领用发出人", 128)
    clean_location = _required(site_location, "现场位置", 256)
    clean_reason = _required(reason, "操作原因", 1000)
    if not lines:
        raise MaintenanceOperationError("现场领用至少需要一条明细")
    if len(lines) > _SITE_ISSUE_LINE_LIMIT:
        raise MaintenanceOperationError("现场领用单最多允许 200 条明细")

    normalized_lines: list[dict] = []
    seen_delivery_ids: set[str] = set()
    for raw_line in lines:
        delivery_line_id = _required(
            raw_line.get("delivery_line_id"), "发货明细稳定编号"
        )
        if delivery_line_id in seen_delivery_ids:
            raise MaintenanceOperationError("同一发货明细不能在一张领用单中重复")
        seen_delivery_ids.add(delivery_line_id)
        no_return = raw_line.get("no_return")
        if no_return is not None and not isinstance(no_return, bool):
            raise MaintenanceOperationError("不返还标记必须是是/否或留空")
        normalized_lines.append(
            {
                "delivery_line_id": delivery_line_id,
                "quantity": _quantity(raw_line["quantity"]),
                "no_return": no_return,
            }
        )

    fingerprint = _site_issue_request_fingerprint(
        {
            "project_id": project_id,
            "issue_date": issue_date.isoformat(),
            "receiver": clean_receiver,
            "issued_by": clean_issued_by,
            "site_location": clean_location,
            "lines": [
                {
                    "delivery_line_id": line["delivery_line_id"],
                    "quantity": _qty(line["quantity"]),
                    "no_return": line.get("no_return"),
                }
                for line in normalized_lines
            ],
            "reason": clean_reason,
        }
    )
    _lock_idempotency_key(db, clean_key)
    existing = db.scalar(
        select(MaintenanceSiteIssue).where(
            MaintenanceSiteIssue.idempotency_key == clean_key
        )
    )
    if existing is not None:
        if (
            existing.project_id != project_id
            or existing.request_fingerprint != fingerprint
            or existing.source != "site_issue_v2"
        ):
            raise MaintenanceOperationConflict("幂等键已用于不同的现场领用请求")
        return site_issue_dict(
            existing,
            _site_issue_lines(db, issue_id=existing.issue_id),
            idempotent_replay=True,
        )

    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")

    maintenance_warehouse_site_issue_bridge.synchronize_delivery_sources(
        db,
        delivery_line_ids=seen_delivery_ids,
    )
    source_rows = list(
        db.scalars(
            select(MaintenanceSiteIssueDeliverySource)
            .where(
                MaintenanceSiteIssueDeliverySource.delivery_line_id.in_(
                    sorted(seen_delivery_ids)
                )
            )
            .order_by(MaintenanceSiteIssueDeliverySource.delivery_line_id)
        )
    )
    sources = {row.delivery_line_id: row for row in source_rows}
    if len(sources) != len(seen_delivery_ids):
        raise MaintenanceOperationError("发货来源适配器未提供完整稳定身份")

    issue_id = str(uuid4())
    saved_lines: list[MaintenanceSiteIssueLine] = []
    for line_no, requested in enumerate(normalized_lines, start=1):
        source_row = sources[requested["delivery_line_id"]]
        if (
            source_row.adapter_key
            not in maintenance_warehouse_site_issue_bridge.SUPPORTED_DELIVERY_ADAPTERS
            or source_row.mapping_state != "ready"
            or not source_row.is_active
        ):
            raise MaintenanceOperationError("发货来源适配器当前不可用")
        if source_row.project_id != project_id:
            raise MaintenanceOperationPermissionError("发货明细不属于当前稳定项目")
        saved_lines.append(
            MaintenanceSiteIssueLine(
                issue_line_id=str(uuid4()),
                issue_id=issue_id,
                line_no=line_no,
                part_id=source_row.part_id,
                pn=_required(source_row.pn, "料号", 128),
                quantity=requested["quantity"],
                delivery_line_id=source_row.delivery_line_id,
                source_order_id=source_row.source_order_id,
                source_line_id=source_row.source_line_id,
                serial_number=source_row.serial_number,
                no_return=requested.get("no_return"),
                linked_purchase_line_id=source_row.linked_purchase_line_id,
                manual_unit_cost=None,
                reference_sample_ids=[],
                reference_sample_count=0,
                reference_samples=[],
                algorithm_version=maintenance_consumption_cost.ALGORITHM_VERSION,
                version=1,
            )
        )

    row = MaintenanceSiteIssue(
        issue_id=issue_id,
        project_id=project_id,
        issue_no=f"LYD-{issue_date:%Y%m%d}-{uuid4().hex[:12].upper()}",
        issue_date=issue_date,
        raw_status="draft",
        status_mapping_state="mapped",
        normalized_status="draft",
        status_mapping_version="site-issue-v2-workflow-v1",
        source="site_issue_v2",
        import_batch_id=None,
        idempotency_key=clean_key,
        request_fingerprint=fingerprint,
        receiver=clean_receiver,
        issued_by=clean_issued_by,
        site_location=clean_location,
        created_by=_required(operated_by, "操作人"),
        version=1,
    )
    db.add(row)
    db.add_all(saved_lines)
    db.flush()
    payload = site_issue_dict(row, saved_lines)
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="site_issue",
        entity_id=row.issue_id,
        action="draft_create",
        before=None,
        after=payload,
        reason=clean_reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return payload


def create_site_issue(
    db: Session,
    *,
    project_id: str,
    issue_no: str,
    issue_date: date,
    raw_status: str,
    status_mapping_state: str,
    normalized_status: str,
    status_mapping_version: str,
    lines: list[dict],
    reason: str,
    operated_by: str,
    source: str = "direct_api",
    import_batch_id: str | None = None,
) -> dict | None:
    if source == "direct_api" and _site_issue_is_production_blocked():
        raise MaintenanceOperationError(
            "生产现场领用必须使用已确认仓库发货明细的新版受控流程"
        )
    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    if status_mapping_state not in {"mapped", "unmapped"}:
        raise MaintenanceOperationError("现场领用状态映射结果无效")
    if normalized_status not in {"confirmed", "void", "unknown"}:
        raise MaintenanceOperationError("现场领用标准状态无效")
    if status_mapping_state == "mapped" and normalized_status == "unknown":
        raise MaintenanceOperationError("mapped 现场领用不能使用 unknown 标准状态")
    if status_mapping_state != "mapped" and normalized_status != "unknown":
        raise MaintenanceOperationError("未映射现场领用必须使用 unknown 标准状态")
    if not lines:
        raise MaintenanceOperationError("现场领用至少需要一条明细")
    if len(lines) > _SITE_ISSUE_LINE_LIMIT:
        raise MaintenanceOperationError("现场领用单最多允许 200 条明细")
    issue_id = str(uuid4())
    saved_lines: list[MaintenanceSiteIssueLine] = []
    seen_line_ids: set[str] = set()
    seen_line_numbers: set[int] = set()
    for raw_line in lines:
        line_id = _required(raw_line.get("issue_line_id"), "领用明细稳定编号")
        line_no = int(raw_line["line_no"])
        if line_id in seen_line_ids or line_no in seen_line_numbers:
            raise MaintenanceOperationError("现场领用明细编号或行号重复")
        seen_line_ids.add(line_id)
        seen_line_numbers.add(line_no)
        saved_lines.append(
            MaintenanceSiteIssueLine(
                issue_line_id=line_id,
                issue_id=issue_id,
                line_no=line_no,
                part_id=int(raw_line["part_id"]),
                pn=_required(raw_line.get("pn"), "料号", 128),
                quantity=_quantity(raw_line["quantity"]),
                no_return=raw_line.get("no_return"),
                linked_purchase_line_id=raw_line.get("linked_purchase_line_id"),
                manual_unit_cost=None,
                reference_sample_ids=[],
                reference_sample_count=0,
                reference_samples=[],
                algorithm_version=maintenance_consumption_cost.ALGORITHM_VERSION,
                version=1,
            )
        )

    row = MaintenanceSiteIssue(
        issue_id=issue_id,
        project_id=project_id,
        issue_no=_required(issue_no, "现场领用单号"),
        issue_date=issue_date,
        raw_status=_required(raw_status, "现场领用原始状态"),
        status_mapping_state=status_mapping_state,
        normalized_status=normalized_status,
        status_mapping_version=_required(status_mapping_version, "状态映射版本"),
        source=source,
        import_batch_id=import_batch_id,
        version=1,
    )
    db.add(row)
    db.add_all(saved_lines)
    db.flush()
    if status_mapping_state == "mapped" and normalized_status == "confirmed":
        _resolve_site_issue_costs(
            db,
            lines=[(issue_date, line) for line in saved_lines],
        )
    payload = site_issue_dict(row, saved_lines)
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="site_issue",
        entity_id=row.issue_id,
        action="create",
        before=None,
        after=payload,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return payload


_SITE_ISSUE_STATUS_TRANSITIONS = {
    "unknown": {"confirmed", "void"},
    "confirmed": {"void"},
    "void": set(),
}

_EXPENSE_STATUS_TRANSITIONS = {
    "unknown": {"approved", "rejected", "void"},
    "rejected": {"approved", "void"},
    "approved": {"void"},
    "void": set(),
}


def _status_transition_allowed(
    transitions: dict[str, set[str]],
    *,
    current: str,
    target: str,
) -> bool:
    return target == current or target in transitions.get(current, set())


def update_site_issue_status(
    db: Session,
    *,
    issue_id: str,
    version: int,
    raw_status: str,
    normalized_status: str,
    status_mapping_version: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    """Advance one site issue without deleting or erasing its cost evidence."""

    project_id = db.scalar(
        select(MaintenanceSiteIssue.project_id).where(
            MaintenanceSiteIssue.issue_id == issue_id
        )
    )
    if project_id is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    project = _project(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    row = db.scalar(
        select(MaintenanceSiteIssue)
        .where(MaintenanceSiteIssue.issue_id == issue_id)
        .with_for_update()
    )
    if row is None:
        return None
    if row.source == "site_issue_v2":
        raise MaintenanceOperationError(
            "新版现场领用单必须使用预览、确认、更正或作废命令"
        )
    if normalized_status == "confirmed" and _site_issue_is_production_blocked():
        raise MaintenanceOperationError(
            "生产现场领用必须使用已确认仓库发货明细的新版受控流程"
        )
    lines = list(
        db.scalars(
            select(MaintenanceSiteIssueLine)
            .where(MaintenanceSiteIssueLine.issue_id == issue_id)
            .order_by(MaintenanceSiteIssueLine.line_no)
            .with_for_update()
        )
    )
    if row.version != version:
        raise MaintenanceOperationConflict(
            f"现场领用单已变化（当前版本 {row.version}），请刷新后重试"
        )
    if normalized_status not in {"confirmed", "void"}:
        raise MaintenanceOperationError("现场领用状态只能更新为已确认或已作废")
    if not _status_transition_allowed(
        _SITE_ISSUE_STATUS_TRANSITIONS,
        current=row.normalized_status,
        target=normalized_status,
    ):
        raise MaintenanceOperationError(
            f"现场领用状态不能从 {row.normalized_status} 变更为 {normalized_status}"
        )

    before = site_issue_dict(row, lines)
    previous_status = row.normalized_status
    row.raw_status = _required(raw_status, "现场领用原始状态")
    row.status_mapping_state = "mapped"
    row.normalized_status = normalized_status
    row.status_mapping_version = _required(
        status_mapping_version, "状态映射版本"
    )
    if previous_status != "confirmed" and normalized_status == "confirmed":
        before_by_line = {
            line.issue_line_id: site_issue_line_dict(line) for line in lines
        }
        _resolve_site_issue_costs(
            db,
            lines=[(row.issue_date, line) for line in lines],
        )
        for line in lines:
            if site_issue_line_dict(line) != before_by_line[line.issue_line_id]:
                line.version += 1

    candidate = site_issue_dict(row, lines)
    if candidate == before:
        return before
    row.version += 1
    after = site_issue_dict(row, lines)
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="site_issue",
        entity_id=row.issue_id,
        action="status_update",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return after


def update_expense_status(
    db: Session,
    *,
    expense_id: str,
    version: int,
    raw_status: str,
    normalized_status: str,
    status_mapping_version: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    """Advance one attributed expense while preserving the original fact row."""

    project_id = db.scalar(
        select(MaintenanceProjectExpenseAttribution.project_id).where(
            MaintenanceProjectExpenseAttribution.expense_id == expense_id
        )
    )
    if project_id is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    project = _project(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    row = db.scalar(
        select(MaintenanceProjectExpenseAttribution)
        .where(MaintenanceProjectExpenseAttribution.expense_id == expense_id)
        .with_for_update()
    )
    if row is None:
        return None
    if row.version != version:
        raise MaintenanceOperationConflict(
            f"报销归集事实已变化（当前版本 {row.version}），请刷新后重试"
        )
    if normalized_status not in {"approved", "rejected", "void", "unknown"}:
        raise MaintenanceOperationError("报销标准状态无效")
    if not _status_transition_allowed(
        _EXPENSE_STATUS_TRANSITIONS,
        current=row.normalized_status,
        target=normalized_status,
    ):
        raise MaintenanceOperationError(
            f"报销状态不能从 {row.normalized_status} 变更为 {normalized_status}"
        )

    before = expense_dict(row)
    row.raw_status = _required(raw_status, "报销原始状态")
    row.status_mapping_state = (
        "unmapped" if normalized_status == "unknown" else "mapped"
    )
    row.normalized_status = normalized_status
    row.status_mapping_version = _required(
        status_mapping_version, "状态映射版本"
    )
    candidate = expense_dict(row)
    if candidate == before:
        return before
    row.version += 1
    after = expense_dict(row)
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="expense",
        entity_id=row.expense_id,
        action="status_update",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return after


def list_cost_gaps(
    db: Session,
    *,
    project_id: str,
    page: int = 1,
    page_size: int = 20,
) -> dict | None:
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        return None
    gap_filters = (
        MaintenanceSiteIssue.project_id == project_id,
        MaintenanceSiteIssue.status_mapping_state == "mapped",
        MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
        MaintenanceSiteIssueLine.cost_amount.is_(None),
        # 2026-08-19：作废领用行不再出现在成本缺口（#55）
        MaintenanceSiteIssueLine.is_active.is_(True),
    )
    total = int(
        db.scalar(
            select(func.count())
            .select_from(MaintenanceSiteIssueLine)
            .join(
                MaintenanceSiteIssue,
                MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
            )
            .where(*gap_filters)
        )
        or 0
    )
    offset = (page - 1) * page_size
    gaps = db.execute(
        select(MaintenanceSiteIssue, MaintenanceSiteIssueLine, DimPart)
        .join(
            MaintenanceSiteIssueLine,
            MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id,
        )
        .join(DimPart, DimPart.id == MaintenanceSiteIssueLine.part_id)
        .where(*gap_filters)
        .order_by(
            MaintenanceSiteIssue.issue_date,
            MaintenanceSiteIssue.issue_no,
            MaintenanceSiteIssueLine.line_no,
        )
        .offset(offset)
        .limit(page_size)
    ).all()

    contracts: list[MaintenanceProjectContract] = []
    if gaps:
        issue_dates = [issue.issue_date for issue, _line, _part in gaps]
        contracts = list(
            db.scalars(
                select(MaintenanceProjectContract)
                .where(
                    MaintenanceProjectContract.project_id == project_id,
                    MaintenanceProjectContract.included_in_total.is_(True),
                    MaintenanceProjectContract.effective_from <= max(issue_dates),
                    or_(
                        MaintenanceProjectContract.effective_to.is_(None),
                        MaintenanceProjectContract.effective_to > min(issue_dates),
                    ),
                )
                .order_by(MaintenanceProjectContract.contract_no)
            )
        )

    rows: list[dict] = []
    for issue, line, part in gaps:
        contract_numbers = [
            contract.contract_no
            for contract in contracts
            if contract.effective_from <= issue.issue_date
            and (
                contract.effective_to is None
                or contract.effective_to > issue.issue_date
            )
        ]
        rows.append(
            {
                "line_id": line.issue_line_id,
                "version": line.version,
                "project_id": project_id,
                "project_code": project.project_code,
                "order_no": issue.issue_no,
                "order_date": issue.issue_date.isoformat(),
                "contract_no": " / ".join(contract_numbers) or None,
                "pn": line.pn,
                "description": part.description,
                "quantity": format(line.quantity, "f"),
                "current_unit_cost": _money(line.unit_cost),
                "current_unit_cost_ex_tax": _money(line.unit_cost_ex_tax),
                "current_unit_cost_inc_tax": _money(line.unit_cost_inc_tax),
                "references": [
                    {
                        "source": line.cost_source,
                        "sample_id": sample["sample_id"],
                        "document_no": sample.get("document_no"),
                        "document_date": sample.get("document_date"),
                        "distance_days": sample.get("distance_days"),
                        "sample_quantity": sample["quantity"],
                        "weighted_unit_price": sample["unit_price_ex_tax"],
                        "tax_conversion": sample["tax_conversion"],
                    }
                    for sample in line.reference_samples
                ],
                "cost_source": line.cost_source,
                "algorithm_version": line.algorithm_version,
                "price_basis": line.price_basis,
            }
        )
    state = db.get(MaintenanceProjectWorkbookState, project_id)
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "data_version": (
            state.data_version
            if state is not None
            else _workbook_data_version(project_id, 0)
        ),
    }


_AUTOMATIC_COST_SOURCES = {"direct_purchase", "purchase_window", "sales_window"}
_COST_SOURCE_PRIORITY = {
    None: 0,
    "manual": 1,
    "sales_window": 2,
    "purchase_window": 3,
    "direct_purchase": 4,
}
_COST_RESOLUTION_FIELDS = (
    "unit_cost",
    "cost_amount",
    "unit_cost_ex_tax",
    "unit_cost_inc_tax",
    "cost_amount_ex_tax",
    "cost_amount_inc_tax",
    "manual_unit_cost_inc_tax",
    "tax_rate_used",
    "cost_source",
    "price_basis",
    "reference_side",
    "reference_sample_ids",
    "reference_sample_count",
    "reference_samples",
    "reference_window_from",
    "reference_window_to",
    "algorithm_version",
)
_COST_RECOMPUTE_BATCH_SIZE = 200


def _cost_resolution_snapshot(line: MaintenanceSiteIssueLine) -> dict:
    return {field: getattr(line, field) for field in _COST_RESOLUTION_FIELDS}


def _restore_cost_resolution(
    line: MaintenanceSiteIssueLine,
    snapshot: dict,
) -> None:
    for field, value in snapshot.items():
        setattr(line, field, value)


def _cost_source_priority(source: str | None) -> int:
    # Unknown future sources fail closed as stronger than today's waterfall.
    return _COST_SOURCE_PRIORITY.get(source, len(_COST_SOURCE_PRIORITY) + 1)


def recompute_cost_gaps(
    db: Session,
    *,
    project_id: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    """Persist newly available deterministic evidence for unresolved issue lines.

    A single project revision represents the whole controlled run, while every
    resolved line retains its own before/after audit and optimistic-lock version.
    Repeating a run after all possible matches are resolved is a no-op.
    """

    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    audit_as_of = business_today()
    state = get_or_create_workbook_state(db, project_id=project_id)
    candidate_filters = (
        MaintenanceSiteIssue.project_id == project_id,
        MaintenanceSiteIssue.status_mapping_state == "mapped",
        MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
        # 2026-08-19：作废领用行不参与成本重算（#55）
        MaintenanceSiteIssueLine.is_active.is_(True),
    )
    candidate_total = int(
        db.scalar(
            select(func.count())
            .select_from(MaintenanceSiteIssueLine)
            .join(
                MaintenanceSiteIssue,
                MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
            )
            .where(*candidate_filters)
        )
        or 0
    )
    resolved = 0
    for offset in range(0, candidate_total, _COST_RECOMPUTE_BATCH_SIZE):
        candidates = list(
            db.execute(
                select(MaintenanceSiteIssueLine, MaintenanceSiteIssue)
                .join(
                    MaintenanceSiteIssue,
                    MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
                )
                .where(*candidate_filters)
                .order_by(
                    MaintenanceSiteIssue.issue_date,
                    MaintenanceSiteIssue.issue_no,
                    MaintenanceSiteIssueLine.line_no,
                    MaintenanceSiteIssueLine.issue_line_id,
                )
                .offset(offset)
                .limit(_COST_RECOMPUTE_BATCH_SIZE)
            )
        )
        before_by_line = {
            line.issue_line_id: _cost_audit_snapshot(line, as_of=audit_as_of)
            for line, _issue in candidates
        }
        prior_by_line = {
            line.issue_line_id: _cost_resolution_snapshot(line)
            for line, _issue in candidates
        }
        priority_by_line = {
            line.issue_line_id: _cost_source_priority(line.cost_source)
            for line, _issue in candidates
        }
        try:
            maintenance_consumption_cost.resolve_lines(
                db,
                lines=[(issue.issue_date, line) for line, issue in candidates],
            )
        except maintenance_consumption_cost.CostResolutionError as exc:
            raise MaintenanceOperationError(str(exc)) from exc
        for line, _issue in candidates:
            prior_resolution = prior_by_line[line.issue_line_id]
            candidate_resolution = _cost_resolution_snapshot(line)
            candidate_priority = _cost_source_priority(line.cost_source)
            if (
                candidate_priority < priority_by_line[line.issue_line_id]
                or candidate_resolution == prior_resolution
            ):
                _restore_cost_resolution(line, prior_resolution)
                continue
            line.version += 1
            after = _cost_audit_snapshot(line, as_of=audit_as_of)
            _fact_audit(
                db,
                project_id=project_id,
                entity_type="site_issue_cost",
                entity_id=line.issue_line_id,
                action="auto_recompute",
                before=before_by_line[line.issue_line_id],
                after=after,
                reason=reason,
                operated_by=operated_by,
            )
            resolved += 1
        # Flush each bounded batch so changed rows and their audits do not remain
        # pending in memory.  The project/workbook lock keeps the whole run atomic.
        db.flush()

    if resolved:
        bump_locked_workbook_revision(db, state=state)
    db.flush()
    return {
        "resolved": resolved,
        "remaining": int(
            db.scalar(
                select(func.count())
                .select_from(MaintenanceSiteIssueLine)
                .join(
                    MaintenanceSiteIssue,
                    MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
                )
                .where(
                    *candidate_filters, MaintenanceSiteIssueLine.cost_amount_inc_tax.is_(None)
                )
            )
            or 0
        ),
        "data_version": state.data_version,
    }


def fill_manual_cost(
    db: Session,
    *,
    project_id: str,
    issue_line_id: str,
    version: int,
    manual_unit_cost: Decimal,
    evidence: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    state = get_or_create_workbook_state(db, project_id=project_id)
    result = db.execute(
        select(MaintenanceSiteIssueLine, MaintenanceSiteIssue)
        .join(MaintenanceSiteIssue, MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id)
        .where(
            MaintenanceSiteIssueLine.issue_line_id == issue_line_id,
            MaintenanceSiteIssue.project_id == project_id,
            # 2026-08-19：已作废的领用行不能补价（#55）
            MaintenanceSiteIssueLine.is_active.is_(True),
        )
        .with_for_update()
    ).one_or_none()
    if result is None:
        return None
    line, issue = result
    if line.version != version:
        raise MaintenanceOperationConflict(
            f"领用成本明细已变化（当前版本 {line.version}），请刷新后重试"
        )
    if (
        issue.status_mapping_state != "mapped"
        or issue.normalized_status not in {"confirmed", "corrected"}
    ):
        raise MaintenanceOperationError("只有已确认且状态已映射的现场领用可以补价")
    audit_as_of = business_today()
    manual_unit_cost = _money_amount(manual_unit_cost, label="人工未税单价")
    before = _cost_audit_snapshot(line, as_of=audit_as_of)
    if line.cost_source is not None or line.cost_amount is not None:
        raise MaintenanceOperationConflict("该领用行已有成本，人工补价只能处理缺价行")
    previous_manual = line.manual_unit_cost
    previous_evidence = line.manual_evidence
    line.manual_unit_cost = None
    line.manual_evidence = None
    _resolve_site_issue_cost(db, issue_date=issue.issue_date, line=line)
    if line.cost_source in _AUTOMATIC_COST_SOURCES:
        line.manual_unit_cost = previous_manual
        line.manual_evidence = previous_evidence
        _resolve_site_issue_cost(db, issue_date=issue.issue_date, line=line)
        line.version += 1
        after = _cost_audit_snapshot(line, as_of=audit_as_of)
        _fact_audit(
            db,
            project_id=issue.project_id,
            entity_type="site_issue_cost",
            entity_id=line.issue_line_id,
            action="auto_recompute",
            before=before,
            after=after,
            reason=reason,
            operated_by=operated_by,
        )
        bump_locked_workbook_revision(db, state=state)
        db.flush()
        return {
            **site_issue_line_dict(line),
            "manual_applied": False,
            "resolution": "automatic_evidence",
        }
    line.manual_unit_cost = manual_unit_cost
    line.manual_evidence = _required(evidence, "人工补价证据", 1000)
    _resolve_site_issue_cost(db, issue_date=issue.issue_date, line=line)
    line.version += 1
    after = _cost_audit_snapshot(line, as_of=audit_as_of)
    _fact_audit(
        db,
        project_id=issue.project_id,
        entity_type="site_issue_cost",
        entity_id=line.issue_line_id,
        action="manual_fill",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    bump_locked_workbook_revision(db, state=state)
    db.flush()
    return {
        **site_issue_line_dict(line),
        "manual_applied": True,
        "resolution": "manual",
    }


def create_contract(
    db: Session,
    *,
    project_id: str,
    contract_id: str,
    contract_no: str,
    contract_amount: Decimal | None,
    contract_status: str | None,
    status_mapping_state: str,
    status_mapping_version: str,
    included_in_total: bool,
    effective_from: date,
    effective_to: date | None,
    source: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    if status_mapping_state not in {"mapped", "unmapped"}:
        raise MaintenanceOperationError("合同状态映射结果无效")
    if status_mapping_state != "mapped" and included_in_total:
        raise MaintenanceOperationError("未映射合同不能计入合同总额")
    if effective_to is not None and effective_to <= effective_from:
        raise MaintenanceOperationError("合同关系结束日期必须晚于开始日期")
    if contract_amount is not None:
        contract_amount = _money_amount(contract_amount, label="合同金额")
    row = MaintenanceProjectContract(
        project_contract_id=str(uuid4()),
        project_id=project_id,
        contract_id=_required(contract_id, "合同稳定编号"),
        contract_no=_required(contract_no, "合同编号"),
        contract_amount=contract_amount,
        contract_status=(str(contract_status).strip() if contract_status else None),
        status_mapping_state=status_mapping_state,
        status_mapping_version=_required(status_mapping_version, "状态映射版本"),
        included_in_total=included_in_total,
        effective_from=effective_from,
        effective_to=effective_to,
        source=_required(source, "合同来源"),
        version=1,
    )
    db.add(row)
    db.flush()
    payload = contract_dict(row)
    _audit_contract(
        db,
        row,
        action="create",
        before=None,
        after=payload,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return payload


def update_contract(
    db: Session,
    *,
    project_contract_id: str,
    version: int,
    updates: dict,
    reason: str,
    operated_by: str,
) -> dict | None:
    project_id = db.scalar(
        select(MaintenanceProjectContract.project_id).where(
            MaintenanceProjectContract.project_contract_id == project_contract_id
        )
    )
    if project_id is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    project = _project(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    row = db.scalar(
        select(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_contract_id == project_contract_id)
        .with_for_update()
    )
    if row is None:
        return None
    if row.version != version:
        raise MaintenanceOperationConflict(
            f"项目合同关系已变化（当前版本 {row.version}），请刷新后重试"
        )
    before = contract_dict(row)
    allowed = {
        "contract_no",
        "contract_amount",
        "contract_status",
        "status_mapping_state",
        "status_mapping_version",
        "included_in_total",
        "effective_from",
        "effective_to",
        "source",
    }
    values = {key: value for key, value in updates.items() if key in allowed}
    if not values:
        raise MaintenanceOperationError("没有可修改的合同关系字段")
    non_nullable = {
        "contract_no",
        "status_mapping_state",
        "status_mapping_version",
        "included_in_total",
        "effective_from",
        "source",
    }
    if any(key in values and values[key] is None for key in non_nullable):
        raise MaintenanceOperationError("合同关系必填字段不能清空")
    for key, value in values.items():
        if key in {"contract_no", "status_mapping_version", "source"}:
            value = _required(value, {
                "contract_no": "合同编号",
                "status_mapping_version": "状态映射版本",
                "source": "合同来源",
            }[key])
        elif key == "contract_status":
            value = str(value).strip() if value else None
        elif key == "contract_amount" and value is not None:
            value = _money_amount(value, label="合同金额")
        setattr(row, key, value)
    if row.status_mapping_state not in {"mapped", "unmapped"}:
        raise MaintenanceOperationError("合同状态映射结果无效")
    if row.status_mapping_state != "mapped" and row.included_in_total:
        raise MaintenanceOperationError("未映射合同不能计入合同总额")
    if row.effective_to is not None and row.effective_to <= row.effective_from:
        raise MaintenanceOperationError("合同关系结束日期必须晚于开始日期")
    after = contract_dict(row)
    if after == before:
        return before
    row.version += 1
    after = contract_dict(row)
    _audit_contract(
        db,
        row,
        action="update",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=row.project_id)
    db.flush()
    return after


def archive_contract(
    db: Session,
    *,
    project_contract_id: str,
    version: int,
    effective_to: date,
    reason: str,
    operated_by: str,
) -> dict | None:
    project_id = db.scalar(
        select(MaintenanceProjectContract.project_id).where(
            MaintenanceProjectContract.project_contract_id == project_contract_id
        )
    )
    if project_id is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    project = _project(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    row = db.scalar(
        select(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_contract_id == project_contract_id)
        .with_for_update()
    )
    if row is None:
        return None
    if row.version != version:
        raise MaintenanceOperationConflict(
            f"项目合同关系已变化（当前版本 {row.version}），请刷新后重试"
        )
    if effective_to <= row.effective_from:
        raise MaintenanceOperationError("合同关系结束日期必须晚于开始日期")
    before = contract_dict(row)
    row.effective_to = effective_to
    row.version += 1
    after = contract_dict(row)
    _audit_contract(
        db,
        row,
        action="archive",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=row.project_id)
    db.flush()
    return after


def create_collection(
    db: Session,
    *,
    project_id: str,
    project_contract_id: str,
    report_month: date,
    cumulative_amount: Decimal,
    status: str,
    receipt_reference: str | None,
    remark: str | None,
    reason: str,
    operated_by: str,
    bump_revision: bool = True,
    source: str = "direct_api",
    import_batch_id: str | None = None,
) -> dict | None:
    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    relation = db.scalar(
        select(MaintenanceProjectContract).where(
            MaintenanceProjectContract.project_contract_id == project_contract_id,
            MaintenanceProjectContract.project_id == project_id,
        )
    )
    if relation is None:
        raise MaintenanceOperationError("项目合同关系不存在或不属于当前项目")
    if report_month.day != 1:
        raise MaintenanceOperationError("回款报告月份必须使用当月第一天")
    if status not in {"confirmed", "unconfirmed", "void"}:
        raise MaintenanceOperationError("回款确认状态无效")
    cumulative_amount = _money_amount(cumulative_amount, label="累计回款")
    if status == "confirmed":
        _validate_confirmed_collection_monotonicity(
            db,
            project_contract_id=project_contract_id,
            report_month=report_month,
            cumulative_amount=cumulative_amount,
        )
    row = MaintenanceCollectionSnapshot(
        collection_id=str(uuid4()),
        project_id=project_id,
        project_contract_id=project_contract_id,
        report_month=report_month,
        cumulative_amount=cumulative_amount,
        status=status,
        receipt_reference=(receipt_reference.strip() if receipt_reference else None),
        remark=(remark.strip() if remark else None),
        source=source,
        import_batch_id=import_batch_id,
        version=1,
    )
    db.add(row)
    db.flush()
    payload = collection_dict(row)
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="collection",
        entity_id=row.collection_id,
        action="create",
        before=None,
        after=payload,
        reason=reason,
        operated_by=operated_by,
    )
    if bump_revision:
        bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return payload


def update_collection(
    db: Session,
    *,
    collection_id: str,
    version: int,
    updates: dict,
    reason: str,
    operated_by: str,
    bump_revision: bool = True,
) -> dict | None:
    project_id = db.scalar(
        select(MaintenanceCollectionSnapshot.project_id).where(
            MaintenanceCollectionSnapshot.collection_id == collection_id
        )
    )
    if project_id is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    project = _project(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    row = db.scalar(
        select(MaintenanceCollectionSnapshot)
        .where(MaintenanceCollectionSnapshot.collection_id == collection_id)
        .with_for_update()
    )
    if row is None:
        return None
    if row.version != version:
        raise MaintenanceOperationConflict(
            f"回款快照已变化（当前版本 {row.version}），请刷新后重试"
        )
    before = collection_dict(row)
    if any(
        key in updates and updates[key] is None
        for key in {"report_month", "cumulative_amount", "status"}
    ):
        raise MaintenanceOperationError("报告月份、累计回款和确认状态不能清空")
    proposed_report_month = updates.get("report_month", row.report_month)
    updates = dict(updates)
    proposed_amount = _money_amount(
        updates.get("cumulative_amount", row.cumulative_amount),
        label="累计回款",
    )
    if "cumulative_amount" in updates:
        updates["cumulative_amount"] = proposed_amount
    proposed_status = updates.get("status", row.status)
    if proposed_report_month.day != 1:
        raise MaintenanceOperationError("回款报告月份必须使用当月第一天")
    if proposed_status not in {"confirmed", "unconfirmed", "void"}:
        raise MaintenanceOperationError("回款确认状态无效")
    if proposed_status == "confirmed":
        _validate_confirmed_collection_monotonicity(
            db,
            project_contract_id=row.project_contract_id,
            report_month=proposed_report_month,
            cumulative_amount=proposed_amount,
            exclude_collection_id=row.collection_id,
        )
    for key in {
        "report_month",
        "cumulative_amount",
        "status",
        "receipt_reference",
        "remark",
    }:
        if key in updates:
            value = updates[key]
            if key in {"receipt_reference", "remark"}:
                value = value.strip() if value and value.strip() else None
            setattr(row, key, value)
    after = collection_dict(row)
    if after == before:
        return before
    row.version += 1
    after = collection_dict(row)
    _fact_audit(
        db,
        project_id=row.project_id,
        entity_type="collection",
        entity_id=row.collection_id,
        action="update",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    if bump_revision:
        bump_workbook_revision(db, project_id=row.project_id)
    db.flush()
    return after


def _payload_token(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _task(
    *,
    project_id: str,
    rule_key: str,
    severity: str,
    title: str,
    detail: str,
    entity_id: str | None = None,
    task_type: str | None = None,
    due_date: date | None = None,
    task_status: str = "open",
    owner: str | None = None,
    close_basis: str = "系统重算后该触发条件不再成立",
) -> dict:
    identity = f"{project_id}:{rule_key}:{entity_id or '-'}"
    return {
        "task_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
        "project_id": project_id,
        "rule_key": rule_key,
        "severity": severity,
        "title": title,
        "detail": detail,
        "entity_id": entity_id,
        "task_type": task_type or rule_key.split(":", 1)[0],
        "due_date": due_date.isoformat() if due_date else None,
        "status": task_status,
        "owner": owner,
        "generated_by": "system",
        "close_basis": close_basis,
    }


def _task_runtime_view(task: dict, *, as_of: date) -> dict:
    due = date.fromisoformat(task["due_date"]) if task.get("due_date") else None
    completed = task.get("status") == "completed"
    if completed:
        due_state = "completed"
    elif due is not None and due < as_of:
        due_state = "overdue"
    elif due == as_of:
        due_state = "due_today"
    elif due is not None:
        due_state = "upcoming"
    else:
        due_state = "none"
    return {
        **task,
        "due_state": due_state,
        "is_overdue": due_state == "overdue",
    }


def _task_summary(tasks: list[dict], *, as_of: date) -> dict:
    rows = [_task_runtime_view(row, as_of=as_of) for row in tasks]
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    open_rows = [row for row in rows if row["status"] != "completed"]
    open_rows.sort(
        key=lambda row: (
            0 if row["is_overdue"] else 1,
            severity_rank.get(row["severity"], 9),
            row["due_date"] is None,
            row["due_date"] or "9999-12-31",
            row["task_id"],
        )
    )
    return {
        "primary": open_rows[0] if open_rows else None,
        "open_count": len(open_rows),
        "overdue_count": sum(1 for row in open_rows if row["is_overdue"]),
        "rows": rows,
    }


def _manager_tracking_facts(
    db: Session,
    *,
    project_ids: list[str],
) -> dict[str, dict]:
    """Load manager-entered tracking facts in a fixed number of queries."""

    facts: dict[str, dict] = {
        project_id: {
            "service_period": None,
            "milestones": [],
            "acceptance": None,
        }
        for project_id in project_ids
    }
    if not project_ids:
        return facts
    for period in db.scalars(
        select(MaintenanceServicePeriod).where(
            MaintenanceServicePeriod.project_id.in_(project_ids)
        )
    ):
        facts[period.project_id]["service_period"] = period
    for milestone in db.scalars(
        select(MaintenanceCollectionMilestone)
        .where(MaintenanceCollectionMilestone.project_id.in_(project_ids))
        .order_by(
            MaintenanceCollectionMilestone.project_id,
            MaintenanceCollectionMilestone.project_contract_id,
            MaintenanceCollectionMilestone.sequence,
        )
    ):
        facts[milestone.project_id]["milestones"].append(milestone)
    deliverables = list(
        db.scalars(
            select(MaintenanceAcceptanceDeliverable).where(
                MaintenanceAcceptanceDeliverable.project_id.in_(project_ids),
                MaintenanceAcceptanceDeliverable.deliverable_type
                == "acceptance_report",
            )
        )
    )
    attachment_counts: dict[str, int] = defaultdict(int)
    deliverable_ids = [row.deliverable_id for row in deliverables]
    if deliverable_ids:
        for deliverable_id, count in db.execute(
            select(BusinessFileLink.entity_id, func.count(BusinessFileLink.link_id))
            .join(BusinessFile, BusinessFile.file_id == BusinessFileLink.file_id)
            .where(
                BusinessFileLink.entity_type
                == "maintenance_acceptance_deliverable",
                BusinessFileLink.entity_id.in_(deliverable_ids),
                BusinessFileLink.archived_at.is_(None),
                BusinessFile.security_state == "active",
            )
            .group_by(BusinessFileLink.entity_id)
        ):
            attachment_counts[deliverable_id] = int(count)
    for deliverable in deliverables:
        facts[deliverable.project_id]["acceptance"] = (
            deliverable,
            attachment_counts[deliverable.deliverable_id],
        )
    return facts


def _manager_tracking_payload(
    *,
    base: dict,
    facts: dict | None,
    latest_confirmed: dict[str, Decimal],
    as_of: date,
    hide_financial: bool,
) -> dict:
    facts = facts or {}
    period: MaintenanceServicePeriod | None = facts.get("service_period")
    service_period = {
        "service_start": period.service_start.isoformat() if period and period.service_start else None,
        "service_end": period.service_end.isoformat() if period and period.service_end else None,
        "completeness_state": period.completeness_state if period else "empty",
    }
    contract_numbers = {
        row["project_contract_id"]: row.get("contract_no")
        for row in base.get("contracts") or []
    }
    cumulative_by_contract: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    outstanding: list[dict] = []
    milestone_count = 0
    for milestone in facts.get("milestones") or []:
        milestone_count += 1
        relation_id = milestone.project_contract_id
        if milestone.planned_amount is not None:
            cumulative_by_contract[relation_id] += Decimal(milestone.planned_amount)
        target = cumulative_by_contract[relation_id]
        actual = Decimal(latest_confirmed.get(relation_id, Decimal("0.00")))
        complete = (
            milestone.completeness_state == "complete"
            and target > 0
            and actual >= target
        )
        if complete:
            continue
        due = milestone.planned_date
        overdue_days = max((as_of - due).days, 0) if due else 0
        outstanding.append(
            {
                "project_contract_id": relation_id,
                "contract_no": contract_numbers.get(relation_id),
                "sequence": milestone.sequence,
                "planned_date": due.isoformat() if due else None,
                "planned_amount": (
                    None if hide_financial or milestone.planned_amount is None
                    else _money(milestone.planned_amount)
                ),
                "completeness_state": milestone.completeness_state,
                "overdue_days": overdue_days,
                "is_overdue": overdue_days > 0,
            }
        )
    outstanding.sort(
        key=lambda row: (
            row["planned_date"] is None,
            row["planned_date"] or "9999-12-31",
            row["contract_no"] or "",
            row["sequence"],
        )
    )
    acceptance_pair = facts.get("acceptance")
    deliverable = acceptance_pair[0] if acceptance_pair else None
    attachment_count = int(acceptance_pair[1]) if acceptance_pair else 0
    due_date = deliverable.due_date if deliverable else None
    submission_status = deliverable.submission_status if deliverable else "not_submitted"
    approval_status = deliverable.approval_status if deliverable else "not_reviewed"
    acceptance_overdue = (
        max((as_of - due_date).days, 0)
        if due_date and submission_status != "submitted" and approval_status != "approved"
        else 0
    )
    return {
        "service_period": service_period,
        "next_collection_milestone": outstanding[0] if outstanding else None,
        "outstanding_collection_milestones": len(outstanding),
        "configured_collection_milestones": milestone_count,
        "acceptance": {
            "deliverable_id": deliverable.deliverable_id if deliverable else None,
            "due_date": due_date.isoformat() if due_date else None,
            "submission_status": submission_status,
            "submitted_at": (
                deliverable.submitted_at.isoformat()
                if deliverable and deliverable.submitted_at
                else None
            ),
            "approval_status": approval_status,
            "approved_at": (
                deliverable.approved_at.isoformat()
                if deliverable and deliverable.approved_at
                else None
            ),
            "rejection_reason": deliverable.rejection_reason if deliverable else None,
            "configuration_state": (
                deliverable.configuration_state
                if deliverable
                else "pending_business_configuration"
            ),
            "attachment_count": attachment_count,
            "overdue_days": acceptance_overdue,
            "is_overdue": acceptance_overdue > 0,
        },
    }


def _attach_manager_and_missing_labels(card: dict, assignment: dict | None) -> None:
    card["manager_assignment"] = assignment
    # The source-text manager is never an account identity. System tasks only
    # receive an owner after an administrator creates an explicit mapping.
    task_owner = assignment.get("username") if assignment is not None else None
    task_summary = card.get("task_summary") or {}
    for task in task_summary.get("rows") or []:
        task["owner"] = task_owner
    if task_summary.get("primary") is not None:
        task_summary["primary"]["owner"] = task_owner
    labels: list[str] = []
    if assignment is None:
        labels.append("负责人待映射")
    tracking = card.get("manager_tracking") or {}
    service_period = tracking.get("service_period") or {}
    if service_period.get("completeness_state") == "empty":
        labels.append("维保期限待补")
    elif service_period.get("completeness_state") in {"start_only", "end_only"}:
        labels.append("维保期限不完整")
    metrics = card.get("metrics") or {}
    if metrics.get("contract_amount_complete") is False:
        labels.append("合同额待补")
    if metrics.get("cost_complete") is False:
        labels.append("成本待补")
    acceptance = tracking.get("acceptance") or {}
    attachment_count = int(acceptance.get("attachment_count") or 0)
    card["attachment_status"] = "available" if attachment_count else "missing"
    if not acceptance.get("due_date"):
        labels.append("验收截止日待补")
    if attachment_count == 0:
        labels.append("验收附件待上传")
    if acceptance.get("configuration_state") != "configured":
        labels.append("验收业务配置待确认")
    card["missing_data_labels"] = labels


def _manager_update_completed_project_ids(
    db: Session,
    *,
    project_ids: list[str],
    report_month: date,
) -> set[str]:
    """Return projects covered by an applied v3 batch for the current assignment.

    Matching the still-active assignment identity prevents an old manager's
    historical upload from closing the replacement manager's task.
    """

    if not project_ids:
        return set()
    month_start = report_month.replace(day=1)
    valid_batch_ids = _manager_batches_matching_current_scope(
        db,
        report_month=month_start,
    )
    if not valid_batch_ids:
        return set()
    return set(
        db.scalars(
            select(MaintenanceManagerUploadBatchProject.project_id)
            .join(
                MaintenanceManagerUploadBatch,
                MaintenanceManagerUploadBatch.batch_id
                == MaintenanceManagerUploadBatchProject.batch_id,
            )
            .join(
                MaintenanceProjectUserAssignment,
                MaintenanceProjectUserAssignment.assignment_id
                == MaintenanceManagerUploadBatchProject.assignment_id,
            )
            .where(
                MaintenanceManagerUploadBatchProject.project_id.in_(project_ids),
                MaintenanceManagerUploadBatch.status == "applied",
                MaintenanceManagerUploadBatch.batch_id.in_(valid_batch_ids),
                MaintenanceManagerUploadBatch.report_month == month_start,
                MaintenanceProjectUserAssignment.archived_at.is_(None),
                MaintenanceProjectUserAssignment.version
                == MaintenanceManagerUploadBatchProject.assignment_version,
                MaintenanceProjectUserAssignment.user_id
                == MaintenanceManagerUploadBatch.owner_user_id,
            )
        )
    )


def _manager_batches_matching_current_scope(
    db: Session,
    *,
    report_month: date,
) -> set[str]:
    """Applied monthly batches are complete only while their full owner scope matches.

    A newly assigned, archived, restored, or version-changed project invalidates the
    old whole-month completion signal. This mirrors the workbook status endpoint and
    prevents an old partial scope from closing project-manager tasks.
    """

    batches = list(
        db.scalars(
            select(MaintenanceManagerUploadBatch).where(
                MaintenanceManagerUploadBatch.status == "applied",
                MaintenanceManagerUploadBatch.report_month
                == report_month.replace(day=1),
            )
        )
    )
    if not batches:
        return set()
    owner_ids = {batch.owner_user_id for batch in batches}
    current_scope: dict[int, list[dict]] = defaultdict(list)
    for assignment, project in db.execute(
        select(MaintenanceProjectUserAssignment, MaintenanceProject)
        .join(
            MaintenanceProject,
            MaintenanceProject.project_id
            == MaintenanceProjectUserAssignment.project_id,
        )
        .where(
            MaintenanceProjectUserAssignment.user_id.in_(owner_ids),
            MaintenanceProjectUserAssignment.responsibility_type == "primary_manager",
            MaintenanceProjectUserAssignment.archived_at.is_(None),
            MaintenanceProject.is_active.is_(True),
        )
        .order_by(
            MaintenanceProjectUserAssignment.user_id,
            MaintenanceProject.project_id,
            MaintenanceProjectUserAssignment.assignment_id,
        )
    ):
        current_scope[assignment.user_id].append(
            {
                "project_id": project.project_id,
                "project_version": project.version,
                "assignment_id": assignment.assignment_id,
                "assignment_version": assignment.version,
            }
        )

    valid: set[str] = set()
    for batch in batches:
        persisted = sorted(
            [
                {
                    "project_id": str(row.get("project_id") or ""),
                    "project_version": int(row.get("project_version") or 0),
                    "assignment_id": str(row.get("assignment_id") or ""),
                    "assignment_version": int(row.get("assignment_version") or 0),
                }
                for row in (batch.plan_json or {}).get("project_scope") or []
            ],
            key=lambda row: (row["project_id"], row["assignment_id"]),
        )
        if persisted and persisted == current_scope.get(batch.owner_user_id, []):
            valid.add(batch.batch_id)
    return valid


def _system_tasks(
    *,
    project_id: str,
    completeness: dict,
    has_confirmed_collection: bool,
    confirmed_collection: Decimal,
    total_contract_amount: Decimal | None,
    cost_gap_count: int,
    sales_estimate_lines: int,
    cost_status: str,
    as_of: date,
    manager_update_completed: bool = False,
    manager_tracking: dict | None = None,
) -> list[dict]:
    tasks: list[dict] = []
    due_date = date(as_of.year, as_of.month, monthrange(as_of.year, as_of.month)[1])
    tasks.append(
        _task(
            project_id=project_id,
            rule_key=f"manager_update:{as_of:%Y-%m}",
            severity=(
                "info"
                if manager_update_completed or as_of < due_date
                else "warning"
            ),
            title=(
                f"已完成{as_of:%Y年%m月}月度全量工作簿"
                if manager_update_completed
                else f"待上传{as_of:%Y年%m月}月度全量工作簿"
            ),
            detail=(
                "本人范围的 v3 工作簿已通过校验并原子应用"
                if manager_update_completed
                else "请下载本人范围全量表，追加或更新后上传校验"
            ),
            task_type="项目经理月度更新",
            due_date=due_date,
            task_status=("completed" if manager_update_completed else "pending"),
            owner=None,
            close_basis=(
                "项目经理本人范围的 v3 月度全量工作簿通过校验并成功应用后，"
                "由全量上传批次自动关闭"
            ),
        )
    )
    tracking = manager_tracking or {}
    service_period = tracking.get("service_period") or {}
    service_state = service_period.get("completeness_state") or "empty"
    if service_state != "complete":
        tasks.append(
            _task(
                project_id=project_id,
                rule_key=f"service_period:{service_state}",
                severity="warning",
                title=(
                    "补全维保开始和结束日期"
                    if service_state == "empty"
                    else "补全维保期限缺失的一端"
                ),
                detail="维保期限会直接显示在项目卡片，并用于项目期限提醒",
                task_type="维保期限",
                owner=None,
                close_basis="维保开始日期和结束日期均已填写且结束日不早于开始日",
            )
        )
    milestone = tracking.get("next_collection_milestone")
    if milestone:
        due = (
            date.fromisoformat(milestone["planned_date"])
            if milestone.get("planned_date")
            else None
        )
        amount = milestone.get("planned_amount")
        overdue_days = int(milestone.get("overdue_days") or 0)
        detail_parts = [
            f"合同 {milestone.get('contract_no') or '未提供'}",
            f"第 {milestone.get('sequence')} 期",
        ]
        if amount is not None:
            detail_parts.append(f"计划金额 {amount}")
        if overdue_days:
            detail_parts.append(f"已逾期 {overdue_days} 天")
        elif due is None:
            detail_parts.append("计划日期待补")
        tasks.append(
            _task(
                project_id=project_id,
                rule_key=(
                    "collection_plan:"
                    f"{milestone.get('project_contract_id')}:{milestone.get('sequence')}"
                ),
                severity="critical" if overdue_days else "info",
                title=(
                    "计划回款节点已逾期"
                    if overdue_days
                    else "跟进最近计划回款节点"
                ),
                detail=" · ".join(detail_parts),
                entity_id=(
                    f"{milestone.get('project_contract_id')}:{milestone.get('sequence')}"
                ),
                task_type="计划回款",
                due_date=due,
                owner=None,
                close_basis="财务确认累计实收达到该节点累计计划金额，或月度全量表调整该计划",
            )
        )
    elif int(tracking.get("configured_collection_milestones") or 0) == 0:
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="collection_plan:missing",
                severity="warning",
                title="补充计划回款节点",
                detail="当前项目尚未设置任何计划回款日期或金额",
                task_type="计划回款",
                owner=None,
                close_basis="至少存在一条计划回款节点",
            )
        )
    acceptance = tracking.get("acceptance") or {}
    acceptance_due = (
        date.fromisoformat(acceptance["due_date"])
        if acceptance.get("due_date")
        else None
    )
    if acceptance_due is None:
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="acceptance:missing_due",
                severity="warning",
                title="补充验收报告截止日",
                detail="截止日缺失不会隐藏项目，但无法生成到期提醒",
                task_type="验收报告",
                owner=None,
                close_basis="验收报告截止日已配置",
            )
        )
    if int(acceptance.get("attachment_count") or 0) == 0:
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="acceptance:missing_attachment",
                severity="warning",
                title="上传验收报告附件",
                detail="尚无通过安全校验的有效验收附件",
                task_type="验收报告",
                due_date=acceptance_due,
                owner=None,
                close_basis="至少存在一个有效、未归档的受控验收附件",
            )
        )
    submission_status = acceptance.get("submission_status") or "not_submitted"
    approval_status = acceptance.get("approval_status") or "not_reviewed"
    if submission_status != "submitted":
        overdue_days = int(acceptance.get("overdue_days") or 0)
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="acceptance:report_due",
                severity="critical" if overdue_days else "warning",
                title=(
                    "验收报告提交已逾期"
                    if overdue_days
                    else "按期提交验收报告"
                ),
                detail=(
                    f"已逾期 {overdue_days} 天"
                    if overdue_days
                    else "提交必须包含至少一个有效附件"
                ),
                task_type="验收报告",
                due_date=acceptance_due,
                owner=None,
                close_basis="验收报告已实名提交",
            )
        )
    elif approval_status == "not_reviewed":
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="acceptance:pending_review",
                severity="info",
                title="验收报告待独立审批",
                detail="提交人与审批人必须不同；业务审批角色未配置时仅 admin 可审批",
                task_type="验收审批",
                owner=None,
                close_basis="验收报告已批准或已驳回",
            )
        )
    elif approval_status == "rejected":
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="acceptance:rejected",
                severity="warning",
                title="验收报告被驳回，需修订后重新提交",
                detail=str(acceptance.get("rejection_reason") or "审批人未填写可见原因"),
                task_type="验收报告",
                due_date=acceptance_due,
                owner=None,
                close_basis="修订附件并重新提交后进入待审批状态",
            )
        )
    for issue in completeness.get("issues", []):
        code = str(issue.get("code") or "incomplete")
        if code == "expense_data_not_ready":
            title = "确认本月审批报销数据已就绪"
            detail = (
                f"当前费用水位 {issue.get('ready_through') or '未确认'}；"
                f"需要覆盖 {issue.get('required_month') or '本月'}"
            )
        else:
            title = "补全项目经营事实"
            detail = code
        tasks.append(
            _task(
                project_id=project_id,
                rule_key=f"completeness:{code}",
                severity="warning",
                title=title,
                detail=detail,
                owner=None,
                close_basis="对应项目经营事实已补全且系统重算通过",
            )
        )
    if not has_confirmed_collection:
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="collection:missing_confirmed",
                severity="info",
                title="补充已确认累计回款",
                detail="当前没有已确认的累计回款快照",
                owner=None,
                close_basis="已存在有效的已确认累计回款快照",
            )
        )
    elif (
        total_contract_amount is not None
        and total_contract_amount > 0
        and confirmed_collection < total_contract_amount
    ):
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="collection:incomplete",
                severity="info",
                title="项目回款尚未完成",
                detail="当前已确认累计回款低于全部合同额",
                owner=None,
                close_basis="已确认累计回款达到全部合同额",
            )
        )
    if cost_gap_count:
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="cost:missing_price",
                severity="warning",
                title="回填现场领用缺价",
                detail=f"仍有 {cost_gap_count} 条已确认领用缺少成本",
                owner=None,
                close_basis="已确认现场领用均已具备有效成本依据",
            )
        )
    if sales_estimate_lines:
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="cost:sales_fallback_estimate",
                severity="warning",
                title="核对项目成本中的销售回退估算",
                detail=(
                    f"{sales_estimate_lines} 条已确认现场领用按销售前后 7 天数量加权估算；"
                    "已计入成本进度，但不等于采购或人工确认单价"
                ),
                owner=None,
                close_basis="销售回退估算已由采购或人工确认证据替换",
            )
        )
    if cost_status in {"yellow", "red"}:
        estimate_note = (
            f"，其中含 {sales_estimate_lines} 条销售回退估算"
            if sales_estimate_lines
            else ""
        )
        tasks.append(
            _task(
                project_id=project_id,
                rule_key=f"cost_ratio:{cost_status}",
                severity="critical" if cost_status == "red" else "warning",
                title="项目已计成本达到预警阈值",
                detail=(
                    f"已计成本已超过合同额{estimate_note}"
                    if cost_status == "red"
                    else f"已计成本已超过合同额 80%{estimate_note}"
                ),
                owner=None,
                close_basis="已计成本比例回落至对应预警阈值内",
            )
        )
    return sorted(tasks, key=lambda row: (row["severity"], row["rule_key"], row["task_id"]))


def _visible_tasks(
    reminders: list[dict],
    completeness: dict,
    *,
    user_ctx: UserContext,
) -> tuple[list[dict], dict]:
    """Apply the same fail-closed task visibility to detail and directory reads."""

    cost_restricted = is_field_hidden(user_ctx, "unit_cost")
    profit_restricted = is_field_hidden(user_ctx, "contract_amount")
    expense_restricted = is_field_hidden(user_ctx, "expense_inc")
    hidden_issue_codes: set[str] = set()
    if cost_restricted:
        hidden_issue_codes.update(
            {"missing_consumption_cost", "unmapped_site_issue_status"}
        )
    if expense_restricted:
        hidden_issue_codes.update(
            {
                "unmapped_expense_status",
                "expense_data_not_ready",
                "expense_readiness_in_future",
            }
        )
    if hidden_issue_codes:
        original_status = completeness.get("status")
        visible_issues = [
            issue
            for issue in completeness.get("issues", [])
            if issue.get("code") not in hidden_issue_codes
        ]
        completeness = {
            "status": (
                "restricted"
                if original_status == "restricted"
                else "incomplete" if visible_issues else "complete"
            ),
            "issues": visible_issues,
        }
        reminders = [
            row
            for row in reminders
            if not any(row["rule_key"].endswith(code) for code in hidden_issue_codes)
        ]
    if cost_restricted:
        reminders = [
            row for row in reminders if not row["rule_key"].startswith("cost")
        ]
    if profit_restricted:
        reminders = [
            row
            for row in reminders
            if not row["rule_key"].startswith(("collection:", "cost_ratio:"))
        ]
    if expense_restricted:
        reminders = [
            row for row in reminders if not row["rule_key"].startswith("cost_ratio:")
        ]
    return reminders, completeness


def _cost_rate_and_status(
    *,
    actual_cost_known: Decimal,
    total_contract_amount: Decimal | None,
    has_incomplete_cost_facts: bool,
) -> tuple[Decimal | None, str]:
    """Apply the canonical two-decimal display threshold exactly once."""

    cost_rate = (
        (
            actual_cost_known
            / total_contract_amount
            * Decimal("100")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if total_contract_amount is not None and total_contract_amount > 0
        else None
    )
    if cost_rate is None:
        return None, "unknown"
    if cost_rate > Decimal("100"):
        return cost_rate, "red"
    if cost_rate > Decimal("80"):
        return cost_rate, "yellow"
    if has_incomplete_cost_facts:
        return cost_rate, "unknown"
    return cost_rate, "normal"


def _project_card_from_facts(
    *,
    base: dict,
    latest_confirmed: dict[str, Decimal],
    consumed_known_ex_tax: Decimal,
    consumed_known_inc_tax: Decimal,
    sales_estimate_cost_ex_tax: Decimal,
    sales_estimate_cost_inc_tax: Decimal,
    sales_estimate_lines: int,
    cost_gap_count: int,
    unmapped_issue_count: int,
    approved_expense_ex_tax: Decimal,
    approved_expense_inc_tax: Decimal,
    unmapped_expense_count: int,
    state: MaintenanceProjectWorkbookState | None,
    as_of: date,
    user_ctx: UserContext,
    manager_update_completed: bool = False,
    manager_tracking_facts: dict | None = None,
) -> tuple[dict, list[dict], dict]:
    """Assemble the canonical project card from preloaded summary facts."""

    confirmed_collection = sum(latest_confirmed.values(), start=Decimal("0.00"))
    total = base["total_contract_amount"]
    collection_progress = (
        (confirmed_collection / Decimal(total) * Decimal("100")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        if total is not None and Decimal(total) > 0
        else None
    )
    expense_ready_through = state.expense_ready_through if state else None
    current_business_month = business_today().replace(day=1)
    expense_readiness_in_future = bool(
        expense_ready_through and expense_ready_through > current_business_month
    )
    expense_data_ready = bool(
        expense_ready_through
        and expense_ready_through >= as_of.replace(day=1)
        and not expense_readiness_in_future
    )
    actual_cost_known_ex_tax = consumed_known_ex_tax + approved_expense_ex_tax
    actual_cost_known_inc_tax = consumed_known_inc_tax + approved_expense_inc_tax
    cost_rate, cost_status = _cost_rate_and_status(
        # Contract amount is explicitly inc-tax, so alerts must compare like
        # with like.  Ex-tax totals remain visible as accounting facts only.
        actual_cost_known=actual_cost_known_inc_tax,
        total_contract_amount=(Decimal(total) if total is not None else None),
        has_incomplete_cost_facts=bool(
            cost_gap_count
            or unmapped_issue_count
            or unmapped_expense_count
            or not expense_data_ready
        ),
    )

    completeness_issues = list(base["completeness"].get("issues", []))
    if cost_gap_count:
        completeness_issues.append(
            {"code": "missing_consumption_cost", "line_count": cost_gap_count}
        )
    if unmapped_issue_count:
        completeness_issues.append(
            {"code": "unmapped_site_issue_status", "line_count": unmapped_issue_count}
        )
    if unmapped_expense_count:
        completeness_issues.append(
            {"code": "unmapped_expense_status", "row_count": unmapped_expense_count}
        )
    if expense_readiness_in_future:
        completeness_issues.append(
            {
                "code": "expense_readiness_in_future",
                "ready_through": expense_ready_through.isoformat(),
                "current_business_month": current_business_month.strftime("%Y-%m"),
            }
        )
    if not expense_data_ready:
        completeness_issues.append(
            {
                "code": "expense_data_not_ready",
                "ready_through": (
                    expense_ready_through.isoformat() if expense_ready_through else None
                ),
                "required_month": as_of.strftime("%Y-%m"),
            }
        )
    completeness = dict(base["completeness"])
    if completeness.get("status") != "restricted" and completeness_issues:
        completeness = {"status": "incomplete", "issues": completeness_issues}

    effective_contracts = [row for row in base["contracts"] if row["is_effective"]]
    known_contract_amount = sum(
        (
            Decimal(row["contract_amount"])
            for row in effective_contracts
            if row["contract_amount"] is not None
        ),
        start=Decimal("0.00"),
    )
    profit_restricted = is_field_hidden(user_ctx, "contract_amount")
    cost_restricted = is_field_hidden(user_ctx, "unit_cost")
    expense_restricted = is_field_hidden(user_ctx, "expense_inc")
    contract_rows = [
        {
            **row,
            "amount_status": (
                "restricted"
                if profit_restricted
                else "missing" if row["contract_amount"] is None else "available"
            ),
            "received_amount": (
                None
                if profit_restricted
                else _money(latest_confirmed.get(row["project_contract_id"]))
            ),
        }
        for row in base["contracts"]
    ]
    manager_tracking = _manager_tracking_payload(
        base=base,
        facts=manager_tracking_facts,
        latest_confirmed=latest_confirmed,
        as_of=as_of,
        hide_financial=profit_restricted,
    )
    project_summary = {
        **base["project"],
        "contracts": contract_rows,
        "metrics": {
            "total_contract_amount": _money(total),
            "contract_amount_basis": "inc_tax",
            "known_contract_amount": _money(known_contract_amount),
            "contract_amount_complete": base["completeness"]["status"] == "complete",
            "received_amount": _money(confirmed_collection),
            "collection_progress_pct": _money(collection_progress),
            "site_requisition_known_cost": _money(consumed_known_inc_tax),
            "site_requisition_known_cost_ex_tax": _money(consumed_known_ex_tax),
            "site_requisition_known_cost_inc_tax": _money(consumed_known_inc_tax),
            "site_requisition_priced_cost_ex_tax": _money(consumed_known_ex_tax),
            "site_requisition_priced_cost_inc_tax": _money(consumed_known_inc_tax),
            "sales_estimate_cost_ex_tax": _money(sales_estimate_cost_ex_tax),
            "sales_estimate_cost_inc_tax": _money(sales_estimate_cost_inc_tax),
            "sales_estimate_lines": sales_estimate_lines,
            "cost_progress_includes_sales_estimate": sales_estimate_lines > 0,
            "cost_progress_label": (
                "priced_cost_including_sales_estimate"
                if sales_estimate_lines
                else "priced_cost_without_sales_estimate"
            ),
            "approved_expense": _money(approved_expense_inc_tax),
            "approved_expense_ex_tax": _money(approved_expense_ex_tax),
            "approved_expense_inc_tax": _money(approved_expense_inc_tax),
            "actual_project_cost_known": _money(actual_cost_known_inc_tax),
            "actual_project_cost_known_ex_tax": _money(actual_cost_known_ex_tax),
            "actual_project_cost_known_inc_tax": _money(actual_cost_known_inc_tax),
            "cost_progress_basis": "inc_tax",
            "cost_rate_lower_bound_pct": _money(cost_rate),
            "cost_status": cost_status,
            "cost_complete": (
                cost_gap_count == 0
                and unmapped_issue_count == 0
                and unmapped_expense_count == 0
                and expense_data_ready
            ),
            "missing_cost_lines": cost_gap_count,
            "expense_data_ready": expense_data_ready,
            "expense_ready_through": (
                expense_ready_through.isoformat() if expense_ready_through else None
            ),
        },
        "reminder_count": 0,
        "manager_tracking": manager_tracking,
        "as_of": as_of.isoformat(),
    }
    reminders = _system_tasks(
        project_id=base["project"]["project_id"],
        completeness=completeness,
        has_confirmed_collection=bool(latest_confirmed),
        confirmed_collection=confirmed_collection,
        total_contract_amount=(Decimal(total) if total is not None else None),
        cost_gap_count=cost_gap_count,
        sales_estimate_lines=sales_estimate_lines,
        cost_status=cost_status,
        as_of=as_of,
        manager_update_completed=manager_update_completed,
        manager_tracking=manager_tracking,
    )
    reminders, completeness = _visible_tasks(
        reminders,
        completeness,
        user_ctx=user_ctx,
    )
    project_summary["metrics"].update(
        {
            "contract_amount_complete": (
                None
                if profit_restricted
                else project_summary["metrics"]["contract_amount_complete"]
            ),
            "known_contract_amount": (
                None
                if profit_restricted
                else project_summary["metrics"]["known_contract_amount"]
            ),
            "received_amount": (
                None if profit_restricted else project_summary["metrics"]["received_amount"]
            ),
            "collection_progress_pct": (
                None
                if profit_restricted
                else project_summary["metrics"]["collection_progress_pct"]
            ),
            "site_requisition_known_cost": (
                None
                if cost_restricted
                else project_summary["metrics"]["site_requisition_known_cost"]
            ),
            "site_requisition_known_cost_ex_tax": (
                None
                if cost_restricted
                else project_summary["metrics"]["site_requisition_known_cost_ex_tax"]
            ),
            "site_requisition_known_cost_inc_tax": (
                None
                if cost_restricted
                else project_summary["metrics"]["site_requisition_known_cost_inc_tax"]
            ),
            "site_requisition_priced_cost_ex_tax": (
                None
                if cost_restricted
                else project_summary["metrics"]["site_requisition_priced_cost_ex_tax"]
            ),
            "site_requisition_priced_cost_inc_tax": (
                None
                if cost_restricted
                else project_summary["metrics"]["site_requisition_priced_cost_inc_tax"]
            ),
            "sales_estimate_cost_ex_tax": (
                None
                if cost_restricted
                else project_summary["metrics"]["sales_estimate_cost_ex_tax"]
            ),
            "sales_estimate_cost_inc_tax": (
                None
                if cost_restricted
                else project_summary["metrics"]["sales_estimate_cost_inc_tax"]
            ),
            "sales_estimate_lines": (
                None
                if cost_restricted
                else project_summary["metrics"]["sales_estimate_lines"]
            ),
            "cost_progress_includes_sales_estimate": (
                None
                if cost_restricted
                else project_summary["metrics"]["cost_progress_includes_sales_estimate"]
            ),
            "cost_progress_label": (
                None
                if cost_restricted
                else project_summary["metrics"]["cost_progress_label"]
            ),
            "approved_expense": (
                None
                if expense_restricted
                else project_summary["metrics"]["approved_expense"]
            ),
            "approved_expense_ex_tax": (
                None
                if expense_restricted
                else project_summary["metrics"]["approved_expense_ex_tax"]
            ),
            "approved_expense_inc_tax": (
                None
                if expense_restricted
                else project_summary["metrics"]["approved_expense_inc_tax"]
            ),
            "actual_project_cost_known": (
                None
                if cost_restricted or expense_restricted
                else project_summary["metrics"]["actual_project_cost_known"]
            ),
            "actual_project_cost_known_ex_tax": (
                None
                if cost_restricted or expense_restricted
                else project_summary["metrics"]["actual_project_cost_known_ex_tax"]
            ),
            "actual_project_cost_known_inc_tax": (
                None
                if cost_restricted or expense_restricted
                else project_summary["metrics"]["actual_project_cost_known_inc_tax"]
            ),
            "cost_rate_lower_bound_pct": (
                None
                if cost_restricted or expense_restricted or profit_restricted
                else project_summary["metrics"]["cost_rate_lower_bound_pct"]
            ),
            "cost_status": (
                None
                if cost_restricted or expense_restricted or profit_restricted
                else project_summary["metrics"]["cost_status"]
            ),
            "cost_complete": (
                None
                if cost_restricted or expense_restricted
                else project_summary["metrics"]["cost_complete"]
            ),
            "missing_cost_lines": (
                None
                if cost_restricted
                else project_summary["metrics"]["missing_cost_lines"]
            ),
            "expense_data_ready": (
                None
                if expense_restricted
                else project_summary["metrics"]["expense_data_ready"]
            ),
            "expense_ready_through": (
                None
                if expense_restricted
                else project_summary["metrics"]["expense_ready_through"]
            ),
        }
    )
    project_summary["reminder_count"] = sum(
        1 for row in reminders if row["status"] != "completed"
    )
    project_summary["task_summary"] = _task_summary(reminders, as_of=as_of)
    return project_summary, project_summary["task_summary"]["rows"], completeness


def project_workspace(
    db: Session,
    *,
    project_id: str,
    as_of: date,
    user_ctx: UserContext,
    collection_page: int = 1,
    collection_page_size: int | None = 20,
    requisition_page: int = 1,
    requisition_page_size: int | None = 20,
    expense_page: int = 1,
    expense_page_size: int | None = 20,
) -> dict | None:
    """Load canonical project totals plus independently paged detail rows.

    ``None`` page sizes are reserved for the workbook adapter.  That path runs
    its 20k preflight before asking for the complete, system-generated sheets;
    the public workspace API always supplies bounded integer page sizes.
    """

    base = maintenance_project.project_overview(
        db,
        project_id,
        as_of=as_of,
        user_ctx=user_ctx,
    )
    if base is None:
        return None
    effective_ids = {
        row["project_contract_id"]
        for row in base["contracts"]
        if row["is_effective"]
    }
    latest_confirmed: dict[str, Decimal] = {}
    if effective_ids:
        ranked_collection = (
            select(
                MaintenanceCollectionSnapshot.project_contract_id,
                MaintenanceCollectionSnapshot.cumulative_amount,
                func.row_number()
                .over(
                    partition_by=MaintenanceCollectionSnapshot.project_contract_id,
                    order_by=(
                        MaintenanceCollectionSnapshot.report_month.desc(),
                        MaintenanceCollectionSnapshot.collection_id.desc(),
                    ),
                )
                .label("row_number"),
            )
            .where(
                MaintenanceCollectionSnapshot.project_contract_id.in_(effective_ids),
                MaintenanceCollectionSnapshot.status == "confirmed",
                MaintenanceCollectionSnapshot.report_month <= as_of,
            )
            .cte("workspace_ranked_collection")
        )
        latest_confirmed = {
            relation_id: Decimal(amount)
            for relation_id, amount in db.execute(
                select(
                    ranked_collection.c.project_contract_id,
                    ranked_collection.c.cumulative_amount,
                ).where(ranked_collection.c.row_number == 1)
            )
        }

    eligible_issue = and_(
        MaintenanceSiteIssue.status_mapping_state == "mapped",
        MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
    )
    issue_fact = db.execute(
        select(
            func.count().label("total"),
            func.count().filter(eligible_issue).label("eligible_total"),
            func.count()
            .filter(and_(eligible_issue, MaintenanceSiteIssueLine.cost_amount_inc_tax.is_(None)))
            .label("cost_gap_count"),
            func.count()
            .filter(
                and_(
                    eligible_issue,
                    MaintenanceSiteIssueLine.cost_source == "sales_window",
                )
            )
            .label("sales_estimate_lines"),
            func.coalesce(
                func.sum(
                    case(
                        (eligible_issue, MaintenanceSiteIssueLine.cost_amount_ex_tax),
                        else_=Decimal("0.00"),
                    )
                ),
                Decimal("0.00"),
            ).label("consumed_known_ex_tax"),
            func.coalesce(
                func.sum(
                    case(
                        (eligible_issue, MaintenanceSiteIssueLine.cost_amount_inc_tax),
                        else_=Decimal("0.00"),
                    )
                ),
                Decimal("0.00"),
            ).label("consumed_known_inc_tax"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                eligible_issue,
                                MaintenanceSiteIssueLine.cost_source == "sales_window",
                            ),
                            MaintenanceSiteIssueLine.cost_amount_ex_tax,
                        ),
                        else_=Decimal("0.00"),
                    )
                ),
                Decimal("0.00"),
            ).label("sales_estimate_cost_ex_tax"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                eligible_issue,
                                MaintenanceSiteIssueLine.cost_source == "sales_window",
                            ),
                            MaintenanceSiteIssueLine.cost_amount_inc_tax,
                        ),
                        else_=Decimal("0.00"),
                    )
                ),
                Decimal("0.00"),
            ).label("sales_estimate_cost_inc_tax"),
        )
        .select_from(MaintenanceSiteIssue)
        .join(
            MaintenanceSiteIssueLine,
            MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id,
        )
        .where(
            MaintenanceSiteIssue.project_id == project_id,
            MaintenanceSiteIssue.issue_date <= as_of,
            # 2026-08-19：作废领用行不计入工作区领用/成本统计（#55）
            MaintenanceSiteIssueLine.is_active.is_(True),
        )
    ).one()
    requisition_total = int(issue_fact.total)
    eligible_requisition_total = int(issue_fact.eligible_total)
    cost_gap_count = int(issue_fact.cost_gap_count)
    unmapped_issue_count = int(
        db.scalar(
            select(func.count(func.distinct(MaintenanceSiteIssue.issue_id))).where(
                MaintenanceSiteIssue.project_id == project_id,
                MaintenanceSiteIssue.issue_date <= as_of,
                or_(
                    MaintenanceSiteIssue.status_mapping_state != "mapped",
                    MaintenanceSiteIssue.normalized_status == "unknown",
                ),
            )
        )
        or 0
    )
    consumed_known_ex_tax = Decimal(issue_fact.consumed_known_ex_tax)
    consumed_known_inc_tax = Decimal(issue_fact.consumed_known_inc_tax)
    sales_estimate_cost_ex_tax = Decimal(issue_fact.sales_estimate_cost_ex_tax)
    sales_estimate_cost_inc_tax = Decimal(issue_fact.sales_estimate_cost_inc_tax)
    sales_estimate_lines = int(issue_fact.sales_estimate_lines)

    expense_eligible = and_(
        MaintenanceProjectExpenseAttribution.status_mapping_state == "mapped",
        MaintenanceProjectExpenseAttribution.normalized_status == "approved",
    )
    expense_fact = db.execute(
        select(
            func.count().filter(expense_eligible).label("approved_total"),
            func.count()
            .filter(
                or_(
                    MaintenanceProjectExpenseAttribution.status_mapping_state
                    != "mapped",
                    MaintenanceProjectExpenseAttribution.normalized_status
                    == "unknown",
                )
            )
            .label("unmapped_expense_count"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            expense_eligible,
                            MaintenanceProjectExpenseAttribution.amount_ex_tax,
                        ),
                        else_=Decimal("0.00"),
                    )
                ),
                Decimal("0.00"),
            ).label("approved_expense_ex_tax"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            expense_eligible,
                            MaintenanceProjectExpenseAttribution.amount_inc_tax,
                        ),
                        else_=Decimal("0.00"),
                    )
                ),
                Decimal("0.00"),
            ).label("approved_expense_inc_tax"),
        ).where(
            MaintenanceProjectExpenseAttribution.project_id == project_id,
            MaintenanceProjectExpenseAttribution.expense_date <= as_of,
        )
    ).one()
    approved_expense_total = int(expense_fact.approved_total)
    unmapped_expense_count = int(expense_fact.unmapped_expense_count)
    approved_expense_ex_tax = Decimal(expense_fact.approved_expense_ex_tax)
    approved_expense_inc_tax = Decimal(expense_fact.approved_expense_inc_tax)

    collection_total = int(
        db.scalar(
            select(func.count())
            .select_from(MaintenanceCollectionSnapshot)
            .where(
                MaintenanceCollectionSnapshot.project_id == project_id,
                # 2026-08-21：total 与行集同口径——未来月份照常计数（770c68a 改了
                # 行集漏了计数，出现 rows=4/total=3 的分页错位）。指标仍 <= as_of。
            )
        )
        or 0
    )
    state = db.get(MaintenanceProjectWorkbookState, project_id)
    manager_update_completed = project_id in _manager_update_completed_project_ids(
        db,
        project_ids=[project_id],
        report_month=as_of,
    )
    manager_tracking = _manager_tracking_facts(
        db,
        project_ids=[project_id],
    ).get(project_id)
    project_summary, reminders, completeness = _project_card_from_facts(
        base=base,
        latest_confirmed=latest_confirmed,
        consumed_known_ex_tax=consumed_known_ex_tax,
        consumed_known_inc_tax=consumed_known_inc_tax,
        sales_estimate_cost_ex_tax=sales_estimate_cost_ex_tax,
        sales_estimate_cost_inc_tax=sales_estimate_cost_inc_tax,
        sales_estimate_lines=sales_estimate_lines,
        cost_gap_count=cost_gap_count,
        unmapped_issue_count=unmapped_issue_count,
        approved_expense_ex_tax=approved_expense_ex_tax,
        approved_expense_inc_tax=approved_expense_inc_tax,
        unmapped_expense_count=unmapped_expense_count,
        state=state,
        as_of=as_of,
        user_ctx=user_ctx,
        manager_update_completed=manager_update_completed,
        manager_tracking_facts=manager_tracking,
    )
    _attach_manager_and_missing_labels(
        project_summary,
        maintenance_project_assignments.active_assignment_views(
            db,
            project_ids=[project_id],
        ).get(project_id),
    )
    project_summary["return_rate"] = maintenance_bad_returns.project_return_rate(
        db,
        project_id=project_id,
    )
    manual_count_statement = (
        select(func.count())
        .select_from(MaintenanceSourceOrderAssignment)
        .join(
            FMaintenanceOrder,
            FMaintenanceOrder.raw_order_id
            == MaintenanceSourceOrderAssignment.source_order_id,
        )
        .where(
            MaintenanceSourceOrderAssignment.project_id == project_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
    )
    project_summary["manual_source_order_count"] = int(
        db.scalar(active_beta_maintenance_orders(manual_count_statement, FMaintenanceOrder)) or 0
    )

    issue_statement = (
        select(MaintenanceSiteIssue, MaintenanceSiteIssueLine)
        .join(
            MaintenanceSiteIssueLine,
            MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id,
        )
        .where(
            MaintenanceSiteIssue.project_id == project_id,
            MaintenanceSiteIssue.issue_date <= as_of,
            # 2026-08-19：作废领用行不出现在工作区领用明细（#55）
            MaintenanceSiteIssueLine.is_active.is_(True),
        )
        .order_by(
            MaintenanceSiteIssue.issue_date,
            MaintenanceSiteIssue.issue_no,
            MaintenanceSiteIssueLine.line_no,
            MaintenanceSiteIssueLine.issue_line_id,
        )
    )
    if requisition_page_size is not None:
        issue_statement = issue_statement.offset(
            (requisition_page - 1) * requisition_page_size
        ).limit(requisition_page_size)
    issue_rows = db.execute(issue_statement).all()
    requisition_rows: list[dict] = []
    for issue, line in issue_rows:
        eligible = (
            issue.status_mapping_state == "mapped"
            and issue.normalized_status in {"confirmed", "corrected"}
        )
        requisition_rows.append(
            {
                "issue_id": issue.issue_id,
                "issue_no": issue.issue_no,
                "issue_date": issue.issue_date.isoformat(),
                "status_mapping_state": issue.status_mapping_state,
                "normalized_status": issue.normalized_status,
                "source": issue.source,
                "import_batch_id": issue.import_batch_id,
                **site_issue_line_dict(line),
                "counts_cost": eligible,
            }
        )

    expense_statement = (
        select(MaintenanceProjectExpenseAttribution)
        .where(
            MaintenanceProjectExpenseAttribution.project_id == project_id,
            MaintenanceProjectExpenseAttribution.expense_date <= as_of,
            expense_eligible,
        )
        .order_by(
            MaintenanceProjectExpenseAttribution.expense_date,
            MaintenanceProjectExpenseAttribution.expense_ref,
            MaintenanceProjectExpenseAttribution.expense_id,
        )
    )
    if expense_page_size is not None:
        expense_statement = expense_statement.offset(
            (expense_page - 1) * expense_page_size
        ).limit(expense_page_size)
    approved_expense_rows = list(db.scalars(expense_statement))

    contract_numbers_by_id = {
        row["project_contract_id"]: row["contract_no"] for row in base["contracts"]
    }
    cost_restricted = is_field_hidden(user_ctx, "unit_cost")
    profit_restricted = is_field_hidden(user_ctx, "contract_amount")
    expense_restricted = is_field_hidden(user_ctx, "expense_inc")
    if cost_restricted:
        hidden_cost_keys = {
            "manual_unit_cost",
            "manual_unit_cost_inc_tax",
            "manual_evidence",
            "unit_cost",
            "cost_amount",
            "unit_cost_ex_tax",
            "unit_cost_inc_tax",
            "cost_amount_ex_tax",
            "cost_amount_inc_tax",
            "tax_rate_used",
            "cost_source",
            "cost_evidence_kind",
            "cost_is_estimate",
            "cost_source_label",
            "price_basis",
            "reference_side",
            "reference_sample_ids",
            "reference_sample_count",
            "reference_samples",
            "reference_window_from",
            "reference_window_to",
            "algorithm_version",
        }
        requisition_rows = [
            {
                key: ([] if key in {"reference_sample_ids", "reference_samples"} else None)
                if key in hidden_cost_keys
                else value
                for key, value in row.items()
            }
            for row in requisition_rows
        ]
    if expense_restricted:
        approved_expense_rows = []
    applicable_contracts_by_date: dict[date, str | None] = {}
    part_ids = {line.part_id for _issue, line in issue_rows}
    part_descriptions = {
        part_id: description
        for part_id, description in db.execute(
            select(DimPart.id, DimPart.description).where(DimPart.id.in_(part_ids))
        )
    } if part_ids else {}

    def contract_numbers_on(issue_date: date) -> str | None:
        if issue_date not in applicable_contracts_by_date:
            numbers = [
                row["contract_no"]
                for row in base["contracts"]
                if row["included_in_total"]
                and date.fromisoformat(row["effective_from"]) <= issue_date
                and (
                    row["effective_to"] is None
                    or issue_date < date.fromisoformat(row["effective_to"])
                )
            ]
            applicable_contracts_by_date[issue_date] = " / ".join(numbers) or None
        return applicable_contracts_by_date[issue_date]

    requisition_payload_rows = [
        {
            **row,
            "line_id": row["issue_line_id"],
            "order_no": row["issue_no"],
            "order_date": row["issue_date"],
            "contract_no": contract_numbers_on(date.fromisoformat(row["issue_date"])),
            "description": part_descriptions.get(row["part_id"]),
            "cost_status": (
                "restricted"
                if cost_restricted
                else "not_counted" if not row["counts_cost"]
                else "missing" if row["cost_amount"] is None
                else "available"
            ),
        }
        for row in requisition_rows
    ]
    expense_payload_rows = [
        {
            **expense_dict(row),
            "expense_no": row.expense_ref,
            "contract_no": contract_numbers_by_id.get(row.project_contract_id),
            "category": row.category,
            "reason": row.expense_reason,
            "amount": _money(row.amount_inc_tax),
            "approval_status": "approved",
            "counts_cost": True,
        }
        for row in approved_expense_rows
    ]
    reminder_rows = [
        {
            **row,
            "reminder_id": row["task_id"],
            "type": row["task_type"],
        }
        for row in reminders
    ]
    last_exported_at = (
        state.last_exported_at.isoformat() if state and state.last_exported_at else None
    )
    collection_statement = (
        select(MaintenanceCollectionSnapshot)
        .where(
            MaintenanceCollectionSnapshot.project_id == project_id,
            # 2026-08-21：不再静默隐藏未来月份（用户实测踩坑——实收填了未来月
            # 页面空白无解释）。未来月份照常返回，前端打「未来月份」标记；
            # 指标计算（latest_confirmed）仍保持 <= as_of 口径不变。
        )
        .order_by(
            MaintenanceCollectionSnapshot.report_month,
            MaintenanceCollectionSnapshot.collection_id,
        )
    )
    if collection_page_size is not None:
        collection_statement = collection_statement.offset(
            (collection_page - 1) * collection_page_size
        ).limit(collection_page_size)
    collection_rows = [
        {
            "collection_id": row.collection_id,
            "project_contract_id": row.project_contract_id,
            "contract_no": contract_numbers_by_id.get(row.project_contract_id),
            "report_month": row.report_month.isoformat(),
            "cumulative_amount": (
                None if profit_restricted else _money(row.cumulative_amount)
            ),
            "receipt_reference": (
                None if profit_restricted else row.receipt_reference
            ),
            "status": row.status,
            "remark": None if profit_restricted else row.remark,
            "source": row.source,
            "import_batch_id": row.import_batch_id,
            "version": row.version,
        }
        for row in db.scalars(collection_statement)
    ]
    payload = {
        "project": project_summary,
        "workbook_revision": state.revision if state is not None else 0,
        "collection_snapshots": {
            "rows": collection_rows,
            "total": collection_total,
            "page": collection_page,
            "page_size": collection_page_size or collection_total or 1,
        },
        "requisitions": {
            "rows": requisition_payload_rows,
            "total": requisition_total,
            "page": requisition_page,
            "page_size": requisition_page_size or requisition_total or 1,
        },
        "approved_expenses": {
            "rows": expense_payload_rows,
            "total": 0 if expense_restricted else approved_expense_total,
            "page": expense_page,
            "page_size": expense_page_size or approved_expense_total or 1,
        },
        "reminders": reminder_rows,
        "return_rate": project_summary["return_rate"],
        "workbook_preview": {
            "protocol_version": "2.0",
            "sheets": [
                {"code": "overview", "name": "01_总览", "row_count": len(project_summary["contracts"]) + collection_total, "ownership": "append_only"},
                {"code": "site_requisitions", "name": "02_备件消耗", "row_count": eligible_requisition_total, "ownership": "system"},
                {"code": "approved_expenses", "name": "03_报销单", "row_count": 0 if expense_restricted else approved_expense_total, "ownership": "system"},
                {"code": "manager_tracking", "name": "04_项目经理追踪与提醒", "row_count": len(reminder_rows), "ownership": "system"},
            ],
            "latest_tracking_month": as_of.strftime("%Y-%m"),
            "last_exported_at": last_exported_at,
            "data_version": (
                state.data_version
                if state is not None
                else _workbook_data_version(project_id, 0)
            ),
        },
        "as_of": as_of.isoformat(),
        "completeness": completeness,
    }
    payload["data_version"] = _payload_token(payload)
    return payload


def project_tasks(
    db: Session,
    *,
    project_id: str,
    as_of: date,
    user_ctx: UserContext,
) -> dict | None:
    workspace = project_workspace(db, project_id=project_id, as_of=as_of, user_ctx=user_ctx)
    if workspace is None:
        return None
    return {
        "project_id": project_id,
        "rows": workspace["reminders"],
        "total": len(workspace["reminders"]),
        "as_of": as_of.isoformat(),
        "data_version": workspace["data_version"],
    }


def project_workbook_workspace(
    db: Session,
    *,
    project_id: str,
    as_of: date,
    user_ctx: UserContext,
) -> dict | None:
    """Canonical, read-only input for the generated four-sheet v2 workbook."""

    workspace = project_workspace(
        db,
        project_id=project_id,
        as_of=as_of,
        user_ctx=user_ctx,
        collection_page_size=None,
        requisition_page_size=None,
        expense_page_size=None,
    )
    if workspace is None:
        return None
    state = db.get(MaintenanceProjectWorkbookState, project_id)
    revision = state.revision if state is not None else 0
    collections = [
        collection_dict(row)
        for row in db.scalars(
            select(MaintenanceCollectionSnapshot)
            .where(
                MaintenanceCollectionSnapshot.project_id == project_id,
                MaintenanceCollectionSnapshot.report_month <= as_of,
            )
            .order_by(
                MaintenanceCollectionSnapshot.project_contract_id,
                MaintenanceCollectionSnapshot.report_month,
            )
        )
    ]
    if is_field_hidden(user_ctx, "contract_amount"):
        collections = [
            {
                **row,
                "cumulative_amount": None,
                "receipt_reference": None,
                "remark": None,
            }
            for row in collections
        ]
    expense_attributions = [
        {
            **expense_dict(row),
            "eligible": (
                row.status_mapping_state == "mapped"
                and row.normalized_status == "approved"
            ),
        }
        for row in db.scalars(
            select(MaintenanceProjectExpenseAttribution)
            .where(
                MaintenanceProjectExpenseAttribution.project_id == project_id,
                MaintenanceProjectExpenseAttribution.expense_date <= as_of,
            )
            .order_by(
                MaintenanceProjectExpenseAttribution.expense_date,
                MaintenanceProjectExpenseAttribution.expense_ref,
            )
        )
    ]
    if is_field_hidden(user_ctx, "expense_inc"):
        expense_attributions = []
    payload = {
        "project": {
            key: workspace["project"][key]
            for key in (
                "project_id",
                "project_code",
                "display_name",
                "project_manager_id",
                "lifecycle_status",
                "is_active",
                "version",
            )
        },
        "workbook_revision": revision,
        "as_of": as_of.isoformat(),
        "all_contracts": list(workspace["project"]["contracts"]),
        "effective_contracts": [
            row for row in workspace["project"]["contracts"] if row["is_effective"]
        ],
        "collection_snapshots": collections,
        "confirmed_site_consumptions": [
            row for row in workspace["requisitions"]["rows"] if row["counts_cost"]
        ],
        "approved_expenses": [
            row
            for row in workspace["approved_expenses"]["rows"]
            if row["counts_cost"]
        ],
        "expense_attributions": expense_attributions,
        "derived_tasks": workspace["reminders"],
        # The workbook renderer consumes the same KPI/completeness contract as
        # the project page.  This prevents a second denominator, readiness, or
        # warning implementation from drifting inside the XLSX layer.
        "canonical_metrics": dict(workspace["project"]["metrics"]),
        "canonical_completeness": dict(workspace["completeness"]),
    }
    payload["data_version"] = (
        state.data_version if state is not None else _workbook_data_version(project_id, 0)
    )
    return payload


def _project_cards_for_ids(
    db: Session,
    *,
    project_ids: list[str],
    as_of: date,
    user_ctx: UserContext,
) -> dict[str, dict]:
    """Load and assemble directory cards in a fixed number of queries."""

    if not project_ids:
        return {}
    projects = list(
        db.scalars(
            select(MaintenanceProject).where(
                MaintenanceProject.project_id.in_(project_ids)
            )
        )
    )
    contracts = list(
        db.scalars(
            select(MaintenanceProjectContract)
            .where(MaintenanceProjectContract.project_id.in_(project_ids))
            .order_by(
                MaintenanceProjectContract.project_id,
                MaintenanceProjectContract.contract_no,
                MaintenanceProjectContract.effective_from,
                MaintenanceProjectContract.project_contract_id,
            )
        )
    )
    contracts_by_project: dict[str, list[MaintenanceProjectContract]] = defaultdict(list)
    effective_contract_ids: set[str] = set()
    effective_relation_project: dict[str, str] = {}
    for contract in contracts:
        contracts_by_project[contract.project_id].append(contract)
        if contract.included_in_total and contract.effective_from <= as_of and (
            contract.effective_to is None or as_of < contract.effective_to
        ):
            effective_contract_ids.add(contract.contract_id)
            effective_relation_project[contract.project_contract_id] = contract.project_id

    projects_by_contract: dict[str, set[str]] = defaultdict(set)
    if effective_contract_ids:
        for contract_id, related_project_id in db.execute(
            select(
                MaintenanceProjectContract.contract_id,
                MaintenanceProjectContract.project_id,
            ).where(
                MaintenanceProjectContract.contract_id.in_(effective_contract_ids),
                MaintenanceProjectContract.included_in_total.is_(True),
                MaintenanceProjectContract.effective_from <= as_of,
                or_(
                    MaintenanceProjectContract.effective_to.is_(None),
                    MaintenanceProjectContract.effective_to > as_of,
                ),
            )
        ):
            projects_by_contract[contract_id].add(related_project_id)
    cross_project_contract_ids = {
        contract_id
        for contract_id, related_project_ids in projects_by_contract.items()
        if len(related_project_ids) > 1
    }

    latest_confirmed_by_project: dict[str, dict[str, Decimal]] = defaultdict(dict)
    if effective_relation_project:
        for relation_id, amount in db.execute(
            select(
                MaintenanceCollectionSnapshot.project_contract_id,
                MaintenanceCollectionSnapshot.cumulative_amount,
            )
            .where(
                MaintenanceCollectionSnapshot.project_contract_id.in_(
                    effective_relation_project
                ),
                MaintenanceCollectionSnapshot.status == "confirmed",
                MaintenanceCollectionSnapshot.report_month <= as_of,
            )
            .order_by(
                MaintenanceCollectionSnapshot.project_contract_id,
                MaintenanceCollectionSnapshot.report_month.desc(),
                MaintenanceCollectionSnapshot.collection_id.desc(),
            )
        ):
            project_id = effective_relation_project[relation_id]
            latest_confirmed_by_project[project_id].setdefault(relation_id, amount)

    cost_facts: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "consumed_known_ex_tax": Decimal("0.00"),
            "consumed_known_inc_tax": Decimal("0.00"),
            "sales_estimate_cost_ex_tax": Decimal("0.00"),
            "sales_estimate_cost_inc_tax": Decimal("0.00"),
            "sales_estimate_lines": 0,
            "cost_gap_count": 0,
            "unmapped_issue_count": 0,
        }
    )
    for project_id, anomaly_count in db.execute(
        select(
            MaintenanceSiteIssue.project_id,
            func.count(func.distinct(MaintenanceSiteIssue.issue_id)),
        )
        .where(
            MaintenanceSiteIssue.project_id.in_(project_ids),
            MaintenanceSiteIssue.issue_date <= as_of,
            or_(
                MaintenanceSiteIssue.status_mapping_state != "mapped",
                MaintenanceSiteIssue.normalized_status == "unknown",
            ),
        )
        .group_by(MaintenanceSiteIssue.project_id)
    ):
        cost_facts[project_id]["unmapped_issue_count"] = int(anomaly_count)
    for (
        project_id,
        mapping_state,
        normalized_status,
        cost_source,
        cost_amount_ex_tax,
        cost_amount_inc_tax,
    ) in db.execute(
        select(
            MaintenanceSiteIssue.project_id,
            MaintenanceSiteIssue.status_mapping_state,
            MaintenanceSiteIssue.normalized_status,
            MaintenanceSiteIssueLine.cost_source,
            MaintenanceSiteIssueLine.cost_amount_ex_tax,
            MaintenanceSiteIssueLine.cost_amount_inc_tax,
        )
        .join(
            MaintenanceSiteIssueLine,
            MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id,
        )
        .where(
            MaintenanceSiteIssue.project_id.in_(project_ids),
            MaintenanceSiteIssue.issue_date <= as_of,
            # 2026-08-19：作废领用行不计入卡片成本/缺口统计（#55）
            MaintenanceSiteIssueLine.is_active.is_(True),
        )
    ):
        facts = cost_facts[project_id]
        eligible = mapping_state == "mapped" and normalized_status in {
            "confirmed",
            "corrected",
        }
        if eligible and cost_amount_inc_tax is None:
            facts["cost_gap_count"] += 1
        elif eligible:
            facts["consumed_known_ex_tax"] += Decimal(cost_amount_ex_tax)
            facts["consumed_known_inc_tax"] += Decimal(cost_amount_inc_tax)
            if cost_source == "sales_window":
                facts["sales_estimate_cost_ex_tax"] += Decimal(cost_amount_ex_tax)
                facts["sales_estimate_cost_inc_tax"] += Decimal(cost_amount_inc_tax)
                facts["sales_estimate_lines"] += 1

    expense_facts: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "approved_expense_ex_tax": Decimal("0.00"),
            "approved_expense_inc_tax": Decimal("0.00"),
            "unmapped_expense_count": 0,
        }
    )
    for (
        project_id,
        mapping_state,
        normalized_status,
        amount_ex_tax,
        amount_inc_tax,
    ) in db.execute(
        select(
            MaintenanceProjectExpenseAttribution.project_id,
            MaintenanceProjectExpenseAttribution.status_mapping_state,
            MaintenanceProjectExpenseAttribution.normalized_status,
            MaintenanceProjectExpenseAttribution.amount_ex_tax,
            MaintenanceProjectExpenseAttribution.amount_inc_tax,
        ).where(
            MaintenanceProjectExpenseAttribution.project_id.in_(project_ids),
            MaintenanceProjectExpenseAttribution.expense_date <= as_of,
        )
    ):
        facts = expense_facts[project_id]
        if mapping_state != "mapped" or normalized_status == "unknown":
            facts["unmapped_expense_count"] += 1
        if mapping_state == "mapped" and normalized_status == "approved":
            facts["approved_expense_ex_tax"] += Decimal(amount_ex_tax)
            facts["approved_expense_inc_tax"] += Decimal(amount_inc_tax)

    state_by_project = {
        state.project_id: state
        for state in db.scalars(
            select(MaintenanceProjectWorkbookState).where(
                MaintenanceProjectWorkbookState.project_id.in_(project_ids)
            )
        )
    }
    manager_assignments = maintenance_project_assignments.active_assignment_views(
        db,
        project_ids=project_ids,
    )
    manager_update_completed_ids = _manager_update_completed_project_ids(
        db,
        project_ids=project_ids,
        report_month=as_of,
    )
    manager_tracking_by_project = _manager_tracking_facts(
        db,
        project_ids=project_ids,
    )
    manual_source_order_count_statement = (
        select(
            MaintenanceSourceOrderAssignment.project_id,
            func.count(),
        )
        .join(
            FMaintenanceOrder,
            FMaintenanceOrder.raw_order_id
            == MaintenanceSourceOrderAssignment.source_order_id,
        )
        .where(
            MaintenanceSourceOrderAssignment.project_id.in_(project_ids),
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    )
    manual_source_order_counts = {
        project_id: int(count)
        for project_id, count in db.execute(
            active_beta_maintenance_orders(
                manual_source_order_count_statement,
                FMaintenanceOrder,
            )
        )
    }
    cards: dict[str, dict] = {}
    return_rates = maintenance_bad_returns.return_rates_for_projects(
        db,
        project_ids=project_ids,
    )
    for project in projects:
        base = maintenance_project.project_overview_from_facts(
            project=project,
            contracts=contracts_by_project[project.project_id],
            cross_project_conflicts={
                contract.contract_id
                for contract in contracts_by_project[project.project_id]
                if contract.contract_id in cross_project_contract_ids
                and contract.included_in_total
                and contract.effective_from <= as_of
                and (contract.effective_to is None or as_of < contract.effective_to)
            },
            as_of=as_of,
            user_ctx=user_ctx,
        )
        project_cost_facts = cost_facts[project.project_id]
        project_expense_facts = expense_facts[project.project_id]
        card, _reminders, _completeness = _project_card_from_facts(
            base=base,
            latest_confirmed=latest_confirmed_by_project[project.project_id],
            consumed_known_ex_tax=Decimal(
                project_cost_facts["consumed_known_ex_tax"]
            ),
            consumed_known_inc_tax=Decimal(
                project_cost_facts["consumed_known_inc_tax"]
            ),
            sales_estimate_cost_ex_tax=Decimal(
                project_cost_facts["sales_estimate_cost_ex_tax"]
            ),
            sales_estimate_cost_inc_tax=Decimal(
                project_cost_facts["sales_estimate_cost_inc_tax"]
            ),
            sales_estimate_lines=int(project_cost_facts["sales_estimate_lines"]),
            cost_gap_count=int(project_cost_facts["cost_gap_count"]),
            unmapped_issue_count=int(project_cost_facts["unmapped_issue_count"]),
            approved_expense_ex_tax=Decimal(
                project_expense_facts["approved_expense_ex_tax"]
            ),
            approved_expense_inc_tax=Decimal(
                project_expense_facts["approved_expense_inc_tax"]
            ),
            unmapped_expense_count=int(
                project_expense_facts["unmapped_expense_count"]
            ),
            state=state_by_project.get(project.project_id),
            as_of=as_of,
            user_ctx=user_ctx,
            manager_update_completed=(
                project.project_id in manager_update_completed_ids
            ),
            manager_tracking_facts=manager_tracking_by_project.get(project.project_id),
        )
        _attach_manager_and_missing_labels(
            card,
            manager_assignments.get(project.project_id),
        )
        card["return_rate"] = return_rates[project.project_id]
        card["manual_source_order_count"] = manual_source_order_counts.get(
            project.project_id,
            0,
        )
        cards[project.project_id] = card
    return cards


def _directory_reminder_query(
    *,
    db: Session,
    filters: list,
    as_of: date,
    user_ctx: UserContext,
    reminder: str | None,
    task_type: str | None = None,
    task_status: str | None = None,
    due_from: date | None = None,
    due_to: date | None = None,
):
    """Build reminder/task project sets in SQL before applying pagination."""

    current_contract = and_(
        MaintenanceProjectContract.effective_from <= as_of,
        or_(
            MaintenanceProjectContract.effective_to.is_(None),
            MaintenanceProjectContract.effective_to > as_of,
        ),
    )
    effective_contract = and_(
        MaintenanceProjectContract.included_in_total.is_(True),
        current_contract,
    )
    effective = (
        select(
            MaintenanceProjectContract.project_id,
            MaintenanceProjectContract.project_contract_id,
            MaintenanceProjectContract.contract_id,
        )
        .where(effective_contract)
        .cte("directory_effective_contract")
    )
    duplicate_contracts = (
        select(effective.c.project_id, effective.c.contract_id)
        .group_by(effective.c.project_id, effective.c.contract_id)
        .having(func.count() > 1)
        .cte("directory_duplicate_contract")
    )
    duplicate_by_project = (
        select(
            duplicate_contracts.c.project_id,
            func.count().label("duplicate_count"),
        )
        .group_by(duplicate_contracts.c.project_id)
        .cte("directory_duplicate_by_project")
    )
    conflicting_contracts = (
        select(effective.c.contract_id)
        .group_by(effective.c.contract_id)
        .having(func.count(func.distinct(effective.c.project_id)) > 1)
        .cte("directory_conflicting_contract")
    )
    conflict_by_project = (
        select(
            effective.c.project_id,
            func.count().label("conflict_count"),
        )
        .join(
            conflicting_contracts,
            conflicting_contracts.c.contract_id == effective.c.contract_id,
        )
        .group_by(effective.c.project_id)
        .cte("directory_conflict_by_project")
    )
    contract_by_project = (
        select(
            MaintenanceProjectContract.project_id,
            func.count()
            .filter(effective_contract)
            .label("effective_count"),
            func.count()
            .filter(
                and_(
                    current_contract,
                    MaintenanceProjectContract.status_mapping_state != "mapped",
                )
            )
            .label("unmapped_count"),
            func.count()
            .filter(
                and_(
                    effective_contract,
                    MaintenanceProjectContract.amount_inc_tax.is_(None),
                    MaintenanceProjectContract.contract_amount.is_(None),
                )
            )
            .label("missing_amount_count"),
            func.coalesce(
                func.sum(
                    func.coalesce(
                        MaintenanceProjectContract.amount_inc_tax,
                        MaintenanceProjectContract.contract_amount,
                    )
                ).filter(effective_contract),
                Decimal("0.00"),
            ).label("total_contract_amount"),
        )
        .group_by(MaintenanceProjectContract.project_id)
        .cte("directory_contract_fact")
    )
    ranked_collection = (
        select(
            MaintenanceCollectionSnapshot.project_contract_id,
            MaintenanceCollectionSnapshot.cumulative_amount,
            func.row_number()
            .over(
                partition_by=MaintenanceCollectionSnapshot.project_contract_id,
                order_by=(
                    MaintenanceCollectionSnapshot.report_month.desc(),
                    MaintenanceCollectionSnapshot.collection_id.desc(),
                ),
            )
            .label("row_number"),
        )
        .where(
            MaintenanceCollectionSnapshot.status == "confirmed",
            MaintenanceCollectionSnapshot.report_month <= as_of,
        )
        .cte("directory_ranked_collection")
    )
    collection_by_project = (
        select(
            effective.c.project_id,
            func.count().label("collection_count"),
            func.coalesce(
                func.sum(ranked_collection.c.cumulative_amount),
                Decimal("0.00"),
            ).label("confirmed_collection"),
        )
        .join(
            ranked_collection,
            and_(
                ranked_collection.c.project_contract_id
                == effective.c.project_contract_id,
                ranked_collection.c.row_number == 1,
            ),
        )
        .group_by(effective.c.project_id)
        .cte("directory_collection_fact")
    )
    eligible_issue = and_(
        MaintenanceSiteIssue.status_mapping_state == "mapped",
        MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
    )
    issue_by_project = (
        select(
            MaintenanceSiteIssue.project_id,
            func.count()
            .filter(
                and_(eligible_issue, MaintenanceSiteIssueLine.cost_amount_inc_tax.is_(None))
            )
            .label("cost_gap_count"),
            func.count()
            .filter(
                and_(
                    eligible_issue,
                    MaintenanceSiteIssueLine.cost_source == "sales_window",
                )
            )
            .label("sales_estimate_count"),
            func.coalesce(
                func.sum(
                    case(
                        (eligible_issue, MaintenanceSiteIssueLine.cost_amount_inc_tax),
                        else_=Decimal("0.00"),
                    )
                ),
                Decimal("0.00"),
            ).label("consumed_known"),
        )
        .join(
            MaintenanceSiteIssueLine,
            MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id,
        )
        .where(
            MaintenanceSiteIssue.issue_date <= as_of,
            # 2026-08-19：作废领用行不计入目录提醒的缺口/已领成本（#55）
            MaintenanceSiteIssueLine.is_active.is_(True),
        )
        .group_by(MaintenanceSiteIssue.project_id)
        .cte("directory_issue_fact")
    )
    issue_status_by_project = (
        select(
            MaintenanceSiteIssue.project_id,
            func.count(func.distinct(MaintenanceSiteIssue.issue_id)).label(
                "unmapped_issue_count"
            ),
        )
        .where(
            MaintenanceSiteIssue.issue_date <= as_of,
            or_(
                MaintenanceSiteIssue.status_mapping_state != "mapped",
                MaintenanceSiteIssue.normalized_status == "unknown",
            ),
        )
        .group_by(MaintenanceSiteIssue.project_id)
        .cte("directory_issue_status_fact")
    )
    expense_by_project = (
        select(
            MaintenanceProjectExpenseAttribution.project_id,
            func.count()
            .filter(
                or_(
                    MaintenanceProjectExpenseAttribution.status_mapping_state
                    != "mapped",
                    MaintenanceProjectExpenseAttribution.normalized_status
                    == "unknown",
                )
            )
            .label("unmapped_expense_count"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                MaintenanceProjectExpenseAttribution.status_mapping_state
                                == "mapped",
                                MaintenanceProjectExpenseAttribution.normalized_status
                                == "approved",
                            ),
                            MaintenanceProjectExpenseAttribution.amount_inc_tax,
                        ),
                        else_=Decimal("0.00"),
                    )
                ),
                Decimal("0.00"),
            ).label("approved_expense"),
        )
        .where(MaintenanceProjectExpenseAttribution.expense_date <= as_of)
        .group_by(MaintenanceProjectExpenseAttribution.project_id)
        .cte("directory_expense_fact")
    )
    milestone_progress = (
        select(
            MaintenanceCollectionMilestone.project_id,
            MaintenanceCollectionMilestone.project_contract_id,
            MaintenanceCollectionMilestone.sequence,
            MaintenanceCollectionMilestone.planned_date,
            MaintenanceCollectionMilestone.completeness_state,
            MaintenanceProjectContract.contract_no,
            func.sum(
                func.coalesce(
                    MaintenanceCollectionMilestone.planned_amount,
                    Decimal("0.00"),
                )
            )
            .over(
                partition_by=MaintenanceCollectionMilestone.project_contract_id,
                order_by=MaintenanceCollectionMilestone.sequence,
                rows=(None, 0),
            )
            .label("cumulative_target"),
            func.coalesce(
                ranked_collection.c.cumulative_amount,
                Decimal("0.00"),
            ).label("confirmed_collection"),
        )
        .join(
            MaintenanceProjectContract,
            MaintenanceProjectContract.project_contract_id
            == MaintenanceCollectionMilestone.project_contract_id,
        )
        .outerjoin(
            ranked_collection,
            and_(
                ranked_collection.c.project_contract_id
                == MaintenanceCollectionMilestone.project_contract_id,
                ranked_collection.c.row_number == 1,
            ),
        )
        .cte("directory_milestone_progress")
    )
    outstanding_milestone = (
        select(milestone_progress)
        .where(
            or_(
                milestone_progress.c.completeness_state != "complete",
                milestone_progress.c.cumulative_target <= Decimal("0.00"),
                milestone_progress.c.confirmed_collection
                < milestone_progress.c.cumulative_target,
            )
        )
        .cte("directory_outstanding_milestone")
    )
    ranked_outstanding_milestone = (
        select(
            outstanding_milestone,
            func.row_number()
            .over(
                partition_by=outstanding_milestone.c.project_id,
                order_by=(
                    case(
                        (outstanding_milestone.c.planned_date.is_(None), 1),
                        else_=0,
                    ),
                    outstanding_milestone.c.planned_date,
                    outstanding_milestone.c.contract_no,
                    outstanding_milestone.c.sequence,
                ),
            )
            .label("project_row_number"),
        )
        .cte("directory_ranked_outstanding_milestone")
    )
    milestone_by_project = (
        select(
            MaintenanceCollectionMilestone.project_id,
            func.count().label("milestone_count"),
        )
        .group_by(MaintenanceCollectionMilestone.project_id)
        .cte("directory_milestone_fact")
    )
    acceptance_attachment_by_deliverable = (
        select(
            BusinessFileLink.entity_id.label("deliverable_id"),
            func.count().label("attachment_count"),
        )
        .join(BusinessFile, BusinessFile.file_id == BusinessFileLink.file_id)
        .where(
            BusinessFileLink.entity_type
            == "maintenance_acceptance_deliverable",
            BusinessFileLink.archived_at.is_(None),
            BusinessFile.security_state == "active",
        )
        .group_by(BusinessFileLink.entity_id)
        .cte("directory_acceptance_attachment_fact")
    )
    acceptance_by_project = (
        select(
            MaintenanceAcceptanceDeliverable.project_id,
            MaintenanceAcceptanceDeliverable.due_date,
            MaintenanceAcceptanceDeliverable.submission_status,
            MaintenanceAcceptanceDeliverable.approval_status,
            func.coalesce(
                acceptance_attachment_by_deliverable.c.attachment_count,
                0,
            ).label("attachment_count"),
        )
        .outerjoin(
            acceptance_attachment_by_deliverable,
            acceptance_attachment_by_deliverable.c.deliverable_id
            == MaintenanceAcceptanceDeliverable.deliverable_id,
        )
        .where(
            MaintenanceAcceptanceDeliverable.deliverable_type
            == "acceptance_report"
        )
        .cte("directory_acceptance_fact")
    )
    facts = (
        select(
            MaintenanceProject.project_id,
            MaintenanceProject.project_code,
            func.coalesce(contract_by_project.c.effective_count, 0).label(
                "effective_count"
            ),
            func.coalesce(contract_by_project.c.unmapped_count, 0).label(
                "unmapped_contract_count"
            ),
            func.coalesce(contract_by_project.c.missing_amount_count, 0).label(
                "missing_amount_count"
            ),
            func.coalesce(duplicate_by_project.c.duplicate_count, 0).label(
                "duplicate_count"
            ),
            func.coalesce(conflict_by_project.c.conflict_count, 0).label(
                "conflict_count"
            ),
            func.coalesce(
                contract_by_project.c.total_contract_amount, Decimal("0.00")
            ).label("total_contract_amount"),
            func.coalesce(collection_by_project.c.collection_count, 0).label(
                "collection_count"
            ),
            func.coalesce(
                collection_by_project.c.confirmed_collection, Decimal("0.00")
            ).label("confirmed_collection"),
            func.coalesce(issue_by_project.c.cost_gap_count, 0).label(
                "cost_gap_count"
            ),
            func.coalesce(issue_by_project.c.sales_estimate_count, 0).label(
                "sales_estimate_count"
            ),
            func.coalesce(issue_status_by_project.c.unmapped_issue_count, 0).label(
                "unmapped_issue_count"
            ),
            func.coalesce(
                issue_by_project.c.consumed_known, Decimal("0.00")
            ).label("consumed_known"),
            func.coalesce(expense_by_project.c.unmapped_expense_count, 0).label(
                "unmapped_expense_count"
            ),
            func.coalesce(
                expense_by_project.c.approved_expense, Decimal("0.00")
            ).label("approved_expense"),
            MaintenanceProjectWorkbookState.expense_ready_through,
            func.coalesce(
                MaintenanceServicePeriod.completeness_state,
                "empty",
            ).label("service_period_state"),
            func.coalesce(milestone_by_project.c.milestone_count, 0).label(
                "milestone_count"
            ),
            ranked_outstanding_milestone.c.project_contract_id.label(
                "next_milestone_contract_id"
            ),
            ranked_outstanding_milestone.c.sequence.label(
                "next_milestone_sequence"
            ),
            ranked_outstanding_milestone.c.planned_date.label(
                "next_milestone_date"
            ),
            acceptance_by_project.c.due_date.label("acceptance_due_date"),
            func.coalesce(
                acceptance_by_project.c.submission_status,
                "not_submitted",
            ).label("acceptance_submission_status"),
            func.coalesce(
                acceptance_by_project.c.approval_status,
                "not_reviewed",
            ).label("acceptance_approval_status"),
            func.coalesce(
                acceptance_by_project.c.attachment_count,
                0,
            ).label("acceptance_attachment_count"),
        )
        .select_from(MaintenanceProject)
        .outerjoin(
            contract_by_project,
            contract_by_project.c.project_id == MaintenanceProject.project_id,
        )
        .outerjoin(
            duplicate_by_project,
            duplicate_by_project.c.project_id == MaintenanceProject.project_id,
        )
        .outerjoin(
            conflict_by_project,
            conflict_by_project.c.project_id == MaintenanceProject.project_id,
        )
        .outerjoin(
            collection_by_project,
            collection_by_project.c.project_id == MaintenanceProject.project_id,
        )
        .outerjoin(
            issue_by_project,
            issue_by_project.c.project_id == MaintenanceProject.project_id,
        )
        .outerjoin(
            issue_status_by_project,
            issue_status_by_project.c.project_id == MaintenanceProject.project_id,
        )
        .outerjoin(
            expense_by_project,
            expense_by_project.c.project_id == MaintenanceProject.project_id,
        )
        .outerjoin(
            MaintenanceProjectWorkbookState,
            MaintenanceProjectWorkbookState.project_id
            == MaintenanceProject.project_id,
        )
        .outerjoin(
            MaintenanceServicePeriod,
            MaintenanceServicePeriod.project_id == MaintenanceProject.project_id,
        )
        .outerjoin(
            milestone_by_project,
            milestone_by_project.c.project_id == MaintenanceProject.project_id,
        )
        .outerjoin(
            ranked_outstanding_milestone,
            and_(
                ranked_outstanding_milestone.c.project_id
                == MaintenanceProject.project_id,
                ranked_outstanding_milestone.c.project_row_number == 1,
            ),
        )
        .outerjoin(
            acceptance_by_project,
            acceptance_by_project.c.project_id == MaintenanceProject.project_id,
        )
        .where(*filters)
        .cte("directory_reminder_fact")
    )

    month_start = as_of.replace(day=1)
    current_business_month = business_today().replace(day=1)
    valid_manager_batch_ids = _manager_batches_matching_current_scope(
        db,
        report_month=month_start,
    )
    manager_completed = (
        select(literal(1))
        .select_from(MaintenanceManagerUploadBatchProject)
        .join(
            MaintenanceManagerUploadBatch,
            MaintenanceManagerUploadBatch.batch_id
            == MaintenanceManagerUploadBatchProject.batch_id,
        )
        .join(
            MaintenanceProjectUserAssignment,
            MaintenanceProjectUserAssignment.assignment_id
            == MaintenanceManagerUploadBatchProject.assignment_id,
        )
        .where(
            MaintenanceManagerUploadBatchProject.project_id == facts.c.project_id,
            MaintenanceManagerUploadBatch.status == "applied",
            MaintenanceManagerUploadBatch.batch_id.in_(valid_manager_batch_ids),
            MaintenanceManagerUploadBatch.report_month == month_start,
            MaintenanceProjectUserAssignment.archived_at.is_(None),
            MaintenanceProjectUserAssignment.version
            == MaintenanceManagerUploadBatchProject.assignment_version,
            MaintenanceProjectUserAssignment.user_id
            == MaintenanceManagerUploadBatch.owner_user_id,
        )
        .exists()
    )
    manager_open = ~manager_completed
    profit_visible = not is_field_hidden(user_ctx, "contract_amount")
    cost_visible = not is_field_hidden(user_ctx, "unit_cost")
    expense_visible = not is_field_hidden(user_ctx, "expense_inc")
    contract_complete = and_(
        literal(profit_visible),
        facts.c.effective_count > 0,
        facts.c.unmapped_contract_count == 0,
        facts.c.missing_amount_count == 0,
        facts.c.duplicate_count == 0,
        facts.c.conflict_count == 0,
    )
    expense_readiness_in_future = (
        facts.c.expense_ready_through > current_business_month
    )
    expense_not_ready = or_(
        facts.c.expense_ready_through.is_(None),
        facts.c.expense_ready_through < month_start,
        expense_readiness_in_future,
    )
    cost_value = facts.c.consumed_known + facts.c.approved_expense
    rounded_cost_rate = func.round(
        cost_value
        / func.nullif(facts.c.total_contract_amount, Decimal("0.00"))
        * Decimal("100"),
        2,
    )
    red = and_(
        literal(profit_visible and cost_visible and expense_visible),
        contract_complete,
        facts.c.total_contract_amount > 0,
        rounded_cost_rate > Decimal("100"),
    )
    yellow = and_(
        literal(profit_visible and cost_visible and expense_visible),
        contract_complete,
        facts.c.total_contract_amount > 0,
        rounded_cost_rate > Decimal("80"),
        rounded_cost_rate <= Decimal("100"),
    )
    service_empty = facts.c.service_period_state == "empty"
    service_start_only = facts.c.service_period_state == "start_only"
    service_end_only = facts.c.service_period_state == "end_only"
    service_incomplete = or_(
        service_empty,
        service_start_only,
        service_end_only,
    )
    collection_plan_missing = facts.c.milestone_count == 0
    collection_plan_next = facts.c.next_milestone_contract_id.is_not(None)
    collection_plan_overdue = and_(
        collection_plan_next,
        facts.c.next_milestone_date.is_not(None),
        facts.c.next_milestone_date < as_of,
    )
    collection_plan_info = and_(
        collection_plan_next,
        or_(
            facts.c.next_milestone_date.is_(None),
            facts.c.next_milestone_date >= as_of,
        ),
    )
    acceptance_missing_due = facts.c.acceptance_due_date.is_(None)
    acceptance_missing_attachment = facts.c.acceptance_attachment_count == 0
    acceptance_report_due = facts.c.acceptance_submission_status != "submitted"
    acceptance_report_overdue = and_(
        acceptance_report_due,
        facts.c.acceptance_due_date.is_not(None),
        facts.c.acceptance_due_date < as_of,
    )
    acceptance_report_warning = and_(
        acceptance_report_due,
        or_(
            facts.c.acceptance_due_date.is_(None),
            facts.c.acceptance_due_date >= as_of,
        ),
    )
    acceptance_pending_review = and_(
        facts.c.acceptance_submission_status == "submitted",
        facts.c.acceptance_approval_status == "not_reviewed",
    )
    acceptance_rejected = facts.c.acceptance_approval_status == "rejected"
    rule_conditions = {
        f"manager_update:{as_of:%Y-%m}": manager_open,
        "completeness:no_effective_contracts": and_(
            literal(profit_visible), facts.c.effective_count == 0
        ),
        "completeness:duplicate_effective_contract": and_(
            literal(profit_visible), facts.c.duplicate_count > 0
        ),
        "completeness:unmapped_contract_status": and_(
            literal(profit_visible), facts.c.unmapped_contract_count > 0
        ),
        "completeness:missing_contract_amount": and_(
            literal(profit_visible), facts.c.missing_amount_count > 0
        ),
        "completeness:cross_project_contract_conflict": and_(
            literal(profit_visible), facts.c.conflict_count > 0
        ),
        "completeness:missing_consumption_cost": and_(
            literal(cost_visible), facts.c.cost_gap_count > 0
        ),
        "completeness:unmapped_site_issue_status": and_(
            literal(cost_visible),
            facts.c.unmapped_issue_count > 0,
        ),
        "completeness:unmapped_expense_status": and_(
            literal(profit_visible and expense_visible),
            facts.c.unmapped_expense_count > 0,
        ),
        "completeness:expense_data_not_ready": and_(
            literal(profit_visible and expense_visible), expense_not_ready
        ),
        "completeness:expense_readiness_in_future": and_(
            literal(profit_visible and expense_visible),
            expense_readiness_in_future,
        ),
        "collection:missing_confirmed": and_(
            literal(profit_visible), facts.c.collection_count == 0
        ),
        "collection:incomplete": and_(
            literal(profit_visible),
            facts.c.collection_count > 0,
            contract_complete,
            facts.c.total_contract_amount > 0,
            facts.c.confirmed_collection < facts.c.total_contract_amount,
        ),
        "cost:missing_price": and_(
            literal(cost_visible), facts.c.cost_gap_count > 0
        ),
        "cost:sales_fallback_estimate": and_(
            literal(cost_visible), facts.c.sales_estimate_count > 0
        ),
        "cost_ratio:yellow": yellow,
        "cost_ratio:red": red,
        "service_period:empty": service_empty,
        "service_period:start_only": service_start_only,
        "service_period:end_only": service_end_only,
        "collection_plan:missing": collection_plan_missing,
        "acceptance:missing_due": acceptance_missing_due,
        "acceptance:missing_attachment": acceptance_missing_attachment,
        "acceptance:report_due": acceptance_report_due,
        "acceptance:pending_review": acceptance_pending_review,
        "acceptance:rejected": acceptance_rejected,
    }
    collection_plan_match = _COLLECTION_PLAN_FILTER.fullmatch(reminder or "")
    if collection_plan_match is not None:
        rule_conditions[reminder] = and_(
            collection_plan_next,
            facts.c.next_milestone_contract_id == collection_plan_match.group(1),
            facts.c.next_milestone_sequence == int(collection_plan_match.group(2)),
        )
    completeness_conditions = [
        condition
        for key, condition in rule_conditions.items()
        if key.startswith("completeness:")
    ]
    collection_conditions = [
        rule_conditions["collection:missing_confirmed"],
        rule_conditions["collection:incomplete"],
    ]
    warning_conditions = [
        *completeness_conditions,
        rule_conditions["cost:missing_price"],
        rule_conditions["cost:sales_fallback_estimate"],
        yellow,
        service_incomplete,
        collection_plan_missing,
        acceptance_missing_due,
        acceptance_missing_attachment,
        acceptance_report_warning,
        acceptance_rejected,
    ]
    if as_of.day == monthrange(as_of.year, as_of.month)[1]:
        warning_conditions.append(manager_open)
        manager_info = literal(False)
    else:
        manager_info = manager_open
    task_type_conditions = {
        "项目经理月度更新": manager_open,
        "completeness": or_(*completeness_conditions),
        "collection": or_(*collection_conditions),
        "cost": or_(
            rule_conditions["cost:missing_price"],
            rule_conditions["cost:sales_fallback_estimate"],
        ),
        "cost_ratio": or_(yellow, red),
        "维保期限": service_incomplete,
        "计划回款": or_(collection_plan_missing, collection_plan_next),
        "验收报告": or_(
            acceptance_missing_due,
            acceptance_missing_attachment,
            acceptance_report_due,
            acceptance_rejected,
        ),
        "验收审批": acceptance_pending_review,
    }
    severity_conditions = {
        "info": or_(
            manager_info,
            *collection_conditions,
            collection_plan_info,
            acceptance_pending_review,
        ),
        "warning": or_(*warning_conditions),
        "critical": or_(
            red,
            collection_plan_overdue,
            acceptance_report_overdue,
        ),
    }
    if reminder is None:
        reminder_condition = literal(True)
    elif reminder == "all":
        reminder_condition = or_(
            manager_open,
            *completeness_conditions,
            *collection_conditions,
            rule_conditions["cost:missing_price"],
            rule_conditions["cost:sales_fallback_estimate"],
            yellow,
            red,
            service_incomplete,
            collection_plan_missing,
            collection_plan_next,
            acceptance_missing_due,
            acceptance_missing_attachment,
            acceptance_report_due,
            acceptance_pending_review,
            acceptance_rejected,
        )
    else:
        reminder_condition = rule_conditions.get(
            reminder,
            task_type_conditions.get(
                reminder,
                severity_conditions.get(reminder, literal(False)),
            ),
        )
    has_task_filter = bool(task_type or task_status or due_from or due_to)
    task_condition = literal(True)
    if has_task_filter:
        manager_due = date(
            as_of.year,
            as_of.month,
            monthrange(as_of.year, as_of.month)[1],
        )
        task_facts: list[tuple[str, str, object | None, object]] = [
            ("项目经理月度更新", "pending", manager_due, manager_open),
            (
                "项目经理月度更新",
                "completed",
                manager_due,
                manager_completed,
            ),
        ]
        task_facts.extend(
            (rule_key.split(":", 1)[0], "open", None, condition)
            for rule_key, condition in rule_conditions.items()
            if rule_key.startswith(
                ("completeness:", "collection:", "cost:", "cost_ratio:")
            )
        )
        task_facts.extend(
            [
                ("维保期限", "open", None, service_incomplete),
                ("计划回款", "open", None, collection_plan_missing),
                (
                    "计划回款",
                    "open",
                    facts.c.next_milestone_date,
                    collection_plan_next,
                ),
                ("验收报告", "open", None, acceptance_missing_due),
                (
                    "验收报告",
                    "open",
                    facts.c.acceptance_due_date,
                    acceptance_missing_attachment,
                ),
                (
                    "验收报告",
                    "open",
                    facts.c.acceptance_due_date,
                    acceptance_report_due,
                ),
                ("验收审批", "open", None, acceptance_pending_review),
                (
                    "验收报告",
                    "open",
                    facts.c.acceptance_due_date,
                    acceptance_rejected,
                ),
            ]
        )
        selected_conditions = []
        for fact_type, fact_status, fact_due, condition in task_facts:
            if task_type and fact_type != task_type:
                continue
            if task_status == "open" and fact_status == "completed":
                continue
            if task_status in {"pending", "completed"} and fact_status != task_status:
                continue
            if due_from is not None or due_to is not None:
                if fact_due is None:
                    continue
                if isinstance(fact_due, date):
                    if due_from is not None and fact_due < due_from:
                        continue
                    if due_to is not None and fact_due > due_to:
                        continue
                else:
                    condition = and_(condition, fact_due.is_not(None))
                    if due_from is not None:
                        condition = and_(condition, fact_due >= due_from)
                    if due_to is not None:
                        condition = and_(condition, fact_due <= due_to)
            selected_conditions.append(condition)
        task_condition = (
            or_(*selected_conditions) if selected_conditions else literal(False)
        )
    return (
        select(facts.c.project_id, facts.c.project_code)
        .where(and_(reminder_condition, task_condition))
        .order_by(facts.c.project_code, facts.c.project_id)
    )


def project_operations(
    db: Session,
    *,
    as_of: date,
    user_ctx: UserContext,
    q_text: str | None = None,
    lifecycle: str = "all",
    reminder: str | None = None,
    include_inactive: bool = False,
    page: int = 1,
    page_size: int = 24,
    owner_scope: str = "all",
    task_type: str | None = None,
    task_status: str | None = None,
    due_from: date | None = None,
    due_to: date | None = None,
) -> dict:
    required_fields = _reminder_filter_required_fields(reminder)
    required_fields += _task_type_filter_required_fields(task_type)
    if any(is_field_hidden(user_ctx, field) for field in required_fields):
        raise MaintenanceOperationPermissionError
    if due_from is not None and due_to is not None and due_from > due_to:
        raise MaintenanceOperationError("截止日期起点不能晚于终点")
    rows: list[dict] = []
    filters = []
    if owner_scope == "me":
        filters.append(
            maintenance_project_assignments.owned_project_condition(user_ctx)
        )
    if not include_inactive:
        filters.append(MaintenanceProject.is_active.is_(True))
    if lifecycle != "all":
        filters.append(MaintenanceProject.lifecycle_status == lifecycle)
    if q_text and (search := q_text.strip()):
        filters.append(
            or_(
                MaintenanceProject.project_code.icontains(search, autoescape=True),
                MaintenanceProject.display_name.icontains(search, autoescape=True),
                MaintenanceProject.project_id.in_(
                    select(MaintenanceProjectContract.project_id).where(
                        MaintenanceProjectContract.contract_no.icontains(
                            search,
                            autoescape=True,
                        )
                    )
                ),
            )
        )
    offset = (page - 1) * page_size
    has_task_filter = bool(task_type or task_status or due_from or due_to)
    if reminder is None and not has_task_filter:
        total = int(
            db.scalar(
                select(func.count()).select_from(MaintenanceProject).where(*filters)
            )
            or 0
        )
        project_ids = list(
            db.scalars(
                select(MaintenanceProject.project_id)
                .where(*filters)
                .order_by(
                    MaintenanceProject.project_code, MaintenanceProject.project_id
                )
                .offset(offset)
                .limit(page_size)
            )
        )
    else:
        matching_query = _directory_reminder_query(
            db=db,
            filters=filters,
            as_of=as_of,
            user_ctx=user_ctx,
            reminder=reminder,
            task_type=task_type,
            task_status=task_status,
            due_from=due_from,
            due_to=due_to,
        )
        total = int(
            db.scalar(select(func.count()).select_from(matching_query.subquery()))
            or 0
        )
        project_ids = list(
            db.scalars(matching_query.offset(offset).limit(page_size))
        )
    cards_by_project = _project_cards_for_ids(
        db,
        project_ids=project_ids,
        as_of=as_of,
        user_ctx=user_ctx,
    )
    rows.extend(
        cards_by_project[project_id]
        for project_id in project_ids
        if project_id in cards_by_project
    )
    payload = {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "as_of": as_of.isoformat(),
        "owner_scope": owner_scope,
        "filters": {
            "task_type": task_type,
            "task_status": task_status,
            "due_from": due_from.isoformat() if due_from else None,
            "due_to": due_to.isoformat() if due_to else None,
        },
    }
    payload["data_version"] = _payload_token(payload)
    return payload
