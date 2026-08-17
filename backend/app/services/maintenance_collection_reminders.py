"""车道 A：回款提醒纯状态派生、目录/详情只读查询与人工操作写服务（Task 2）。

设计依据：``.ai/MAINTENANCE_COLLECTION_REMINDERS_DESIGN.md`` §4.3/§7/§9/§10，
DTO 依据冻结的 ``collection-reminders-api-v1.yaml``（K0 已实现）。

规则摘要：
- ``derive_reminder_state`` 优先级固定为
  ``needs_review > handled > incomplete > overdue > due_this_month > upcoming``；
  month 精度只比较 YYYY-MM，day 精度先比较具体日期；``as_of`` 必须显式传入。
- 目录/详情的排序、七类计数与金额脱敏（无 ``data_profit`` → ``planned_amount=None``）
  全部在服务端产生；DTO 不允许携带实收/待收/到账率/凭证字段。
- 写操作（handle/reschedule/reopen）走不可变操作账本幂等：同 key + 同 actor +
  同路径 milestone + 同 payload_hash 重放首次 ``result_json``，其余 409；
  版本冲突 409；IDOR/撤权/inactive project/contract fail-closed；
  canary 项目配置时其他项目固定 403 / ``canary_scope_denied`` 且零写入。
- 锁顺序复用既有稳定顺序：workbook state → project → contract → milestone。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.maintenance_manager import (
    MaintenanceCollectionMilestone,
    MaintenanceCollectionMilestoneOperation,
    MaintenanceServicePeriod,
)
from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.models.system import SysUser
from app.security import FULL_SCOPE_ROLES, UserContext
from app.services import maintenance_collection_evidence as collection_evidence
from app.services import maintenance_project_assignments as assignments
from app.services import maintenance_project_operations as operations

# 提醒状态派生优先级（设计 §4.3）。
_REMINDER_STATE_RANK = {
    "needs_review": 0,
    "handled": 1,
    "incomplete": 2,
    "overdue": 3,
    "due_this_month": 4,
    "upcoming": 5,
}
# 下一条可跟进节点的选择优先级（设计 §7.1）：needs_review > overdue >
# due_this_month > incomplete > upcoming；普通 handled 不参与。
_NEXT_ACTIONABLE_RANK = {
    "needs_review": 0,
    "overdue": 1,
    "due_this_month": 2,
    "incomplete": 3,
    "upcoming": 4,
}
# 目录默认排序按“下一条”状态优先级 + 计划月份 + 项目编号；无节点项目排最后。
_EMPTY_PROJECT_RANK = 6

_ALLOWED_STATES = frozenset(_REMINDER_STATE_RANK)


class CollectionReminderError(Exception):
    """回款提醒领域错误基类。"""


class CollectionReminderInvalid(CollectionReminderError):
    """请求不符合业务规则 → 422 invalid_request。"""


class CollectionReminderPermissionError(CollectionReminderError):
    """无权限 → 403 permission_denied。"""


class CollectionReminderCanaryDenied(CollectionReminderError):
    """灰度期间仅允许 canary 项目 → 403 canary_scope_denied。"""


class CollectionReminderNotFound(CollectionReminderError):
    """资源不存在或不可见 → 404 not_found。"""


class CollectionReminderConflict(CollectionReminderError):
    """版本/幂等冲突 → 409 version_conflict。"""

    def __init__(
        self,
        message: str,
        *,
        current_version: int | None = None,
        current_data_version: str | None = None,
    ):
        super().__init__(message)
        self.current_version = current_version
        self.current_data_version = current_data_version


# ---------- 通用小工具 ----------

def _attr(milestone, name):
    """同时支持 ORM 对象与 dict 形态的合成节点（纯函数测试用）。"""
    if isinstance(milestone, dict):
        return milestone.get(name)
    return getattr(milestone, name)


def _month_key(value: date) -> tuple[int, int]:
    return (value.year, value.month)


def _month_text(value: date | None) -> str | None:
    if value is None:
        return None
    return f"{value:%Y-%m}"


def _money_text(value) -> str | None:
    if value is None:
        return None
    return f"{Decimal(value):.2f}"


def _data_version(payload: dict) -> str:
    """规范化摘要：与既有 ``_payload_token`` 同一算法（排序键、紧凑分隔、str 兜底）。"""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def follow_up_payload_hash(
    *,
    actor_user_id: int,
    milestone_id: str,
    expected_version: int,
    action: str,
    planned_month: str | None,
    note: str | None,
    reason: str | None,
) -> str:
    """幂等请求规范化摘要：固定包含实名 actor、路径 milestone 与请求体字段。

    与 YAML ``idempotency_scope.payload_hash_fields`` 完全一致；同 key 跨账号
    或跨节点即使 body 相同也必须 409。
    """
    canonical = json.dumps(
        [
            actor_user_id,
            milestone_id,
            expected_version,
            action,
            planned_month,
            note,
            reason,
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _amount_visibility(user_ctx: UserContext) -> str:
    """计划金额继续服从利润/合同金额可见性（设计 §9）：无 data_profit → restricted。"""
    from app import permissions as _perm

    perms = user_ctx.permissions
    if perms is None:
        perms = _perm.template_for(user_ctx.role)
    safe = _perm.runtime_safe(perms)
    return "visible" if safe.get("data_profit", False) else "restricted"


# ---------- 纯状态派生 ----------

def derive_reminder_state(milestone, *, as_of: date) -> str:
    """派生提醒状态（设计 §4.3）。``as_of`` 必须显式传入，禁止隐式系统日期。

    优先级固定为 ``needs_review > handled > incomplete > overdue >
    due_this_month > upcoming``：
    - month 精度只比较 YYYY-MM；早于当前自然月 → overdue，等于 → due_this_month，
      晚于 → upcoming。
    - day 精度：早于 ``as_of`` 当日 → overdue；当日或本月内未来日期 →
      due_this_month；下月及以后 → upcoming。
    """
    if _attr(milestone, "follow_up_review_required"):
        return "needs_review"
    if _attr(milestone, "follow_up_status") == "handled":
        return "handled"
    if _attr(milestone, "completeness_state") != "complete":
        return "incomplete"
    planned_date = _attr(milestone, "planned_date")
    if planned_date is None:
        return "incomplete"
    if _attr(milestone, "date_precision") == "day" and planned_date < as_of:
        return "overdue"
    if _month_key(planned_date) < _month_key(as_of):
        return "overdue"
    if _month_key(planned_date) == _month_key(as_of):
        return "due_this_month"
    return "upcoming"


def derive_collection_payment_state(
    milestone,
    *,
    cumulative_planned: Decimal,
    previous_cumulative_planned: Decimal,
    latest_confirmed_snapshot=None,
    as_of: date,
) -> str:
    """Derive receipt progress without treating a missing snapshot as zero."""
    if _attr(milestone, "completeness_state") != "complete":
        return "incomplete"
    if latest_confirmed_snapshot is None:
        return "not_reported"
    received = Decimal(str(_attr(latest_confirmed_snapshot, "cumulative_amount") or 0))
    if received >= cumulative_planned:
        return "paid"
    if received > previous_cumulative_planned:
        return "partial"
    planned_date = _attr(milestone, "planned_date")
    if planned_date is not None and (
        _attr(milestone, "date_precision") == "day" and planned_date < as_of
        or _month_key(planned_date) <= _month_key(as_of)
    ):
        return "unpaid"
    return "not_due"


def _payment_fields(db: Session, milestone: MaintenanceCollectionMilestone, *, as_of: date) -> dict:
    active = MaintenanceCollectionMilestone.is_active.is_(True)
    current = list(db.scalars(select(MaintenanceCollectionMilestone).where(
        MaintenanceCollectionMilestone.project_contract_id == milestone.project_contract_id,
        active,
        MaintenanceCollectionMilestone.sequence <= milestone.sequence,
    ).order_by(MaintenanceCollectionMilestone.sequence)))
    cumulative = sum((item.planned_amount or Decimal(0)) for item in current)
    previous = sum((item.planned_amount or Decimal(0)) for item in current[:-1])
    snapshot = db.scalar(select(MaintenanceCollectionSnapshot).where(
        MaintenanceCollectionSnapshot.project_contract_id == milestone.project_contract_id,
        MaintenanceCollectionSnapshot.status == "confirmed",
    ).order_by(MaintenanceCollectionSnapshot.report_month.desc()))
    return {
        "payment_state": derive_collection_payment_state(
            milestone, cumulative_planned=cumulative,
            previous_cumulative_planned=previous,
            latest_confirmed_snapshot=snapshot, as_of=as_of,
        ),
        "cumulative_planned_amount": _money_text(cumulative),
        "latest_cumulative_received": _money_text(snapshot.cumulative_amount) if snapshot else None,
        "latest_received_month": snapshot.report_month.isoformat() if snapshot else None,
    }


def select_next_actionable_milestone(milestones, *, as_of: date):
    """选“下一条”可跟进节点：``needs_review > overdue > due_this_month >
    incomplete > upcoming``，再按计划月份和期次；绝不选择普通 handled 节点。
    无候选时返回 None。
    """
    candidates = []
    for milestone in milestones:
        state = derive_reminder_state(milestone, as_of=as_of)
        rank = _NEXT_ACTIONABLE_RANK.get(state)
        if rank is None:
            continue
        planned_date = _attr(milestone, "planned_date") or date.max
        sequence = _attr(milestone, "sequence") or 0
        candidates.append((rank, planned_date, sequence, milestone))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1], item[2]))[3]


def _reminder_counts(states: list[str]) -> dict:
    counts = {state: 0 for state in _ALLOWED_STATES}
    for state in states:
        counts[state] += 1
    return {"total": len(states), **counts}


# ---------- 只读装配 ----------

def _contract_ref(contract: MaintenanceProjectContract, *, as_of: date) -> dict:
    """reminder-only 最小合同结构：不含任何实收/到账字段。"""
    relation_status = "active" if contract.effective_to is None else "archived"
    if contract.effective_from > as_of:
        lifecycle_status = "upcoming"
    elif contract.effective_to is not None and contract.effective_to < as_of:
        lifecycle_status = "ended"
    else:
        lifecycle_status = "active"
    return {
        "project_contract_id": contract.project_contract_id,
        "contract_no": contract.contract_no,
        "relation_status": relation_status,
        "lifecycle_status": lifecycle_status,
        "version": contract.version,
    }


def _manager_assignment(assignment_view: dict | None) -> dict:
    if assignment_view is None:
        return {"username": None, "display_name": None}
    return {
        "username": assignment_view.get("username"),
        "display_name": assignment_view.get("display_name"),
    }


def _service_period_dict(period: MaintenanceServicePeriod | None) -> dict:
    if period is None:
        return {"service_start": None, "service_end": None, "completeness_state": "empty"}
    return {
        "service_start": period.service_start.isoformat() if period.service_start else None,
        "service_end": period.service_end.isoformat() if period.service_end else None,
        "completeness_state": period.completeness_state,
    }


def _display_names(db: Session, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    users = db.scalars(select(SysUser).where(SysUser.id.in_(user_ids))).all()
    return {user.id: user.display_name or user.username for user in users}


def _milestone_row(
    db: Session,
    milestone: MaintenanceCollectionMilestone,
    *,
    as_of: date,
    amount_visible: bool,
    contract_no_by_id: dict[str, str],
    followed_up_display: dict[int, str],
    last_operation_dict: dict | None,
) -> dict:
    state = derive_reminder_state(milestone, as_of=as_of)
    return {
        "milestone_id": milestone.milestone_id,
        "project_contract_id": milestone.project_contract_id,
        "contract_no": contract_no_by_id.get(milestone.project_contract_id),
        "sequence": milestone.sequence,
        "planned_date": milestone.planned_date.isoformat() if milestone.planned_date else None,
        "date_precision": milestone.date_precision,
        "planned_month": _month_text(milestone.planned_date),
        "planned_amount": (
            None if not amount_visible else _money_text(milestone.planned_amount)
        ),
        "completeness_state": milestone.completeness_state,
        "follow_up_status": milestone.follow_up_status,
        "reminder_state": state,
        "follow_up_review_required": bool(milestone.follow_up_review_required),
        "followed_up_by": (
            followed_up_display.get(milestone.followed_up_by)
            if milestone.followed_up_by is not None
            else None
        ),
        "followed_up_at": (
            milestone.followed_up_at.isoformat() if milestone.followed_up_at else None
        ),
        "follow_up_note": milestone.follow_up_note,
        "last_operation": last_operation_dict,
        "version": milestone.version,
        **_payment_fields(db, milestone, as_of=as_of),
    }


def _actionable_dict(
    db: Session,
    milestone: MaintenanceCollectionMilestone,
    *,
    as_of: date,
    amount_visible: bool,
    contract_no_by_id: dict[str, str],
) -> dict:
    return {
        "milestone_id": milestone.milestone_id,
        "project_contract_id": milestone.project_contract_id,
        "contract_no": contract_no_by_id.get(milestone.project_contract_id),
        "sequence": milestone.sequence,
        "planned_month": _month_text(milestone.planned_date),
        "planned_amount": (
            None if not amount_visible else _money_text(milestone.planned_amount)
        ),
        "reminder_state": derive_reminder_state(milestone, as_of=as_of),
        "version": milestone.version,
        **_payment_fields(db, milestone, as_of=as_of),
    }


def _operation_dict(
    operation: MaintenanceCollectionMilestoneOperation,
    *,
    actor_display: dict[int, str],
) -> dict:
    return {
        "operation_id": operation.operation_id,
        "action": operation.action,
        "reason": operation.reason,
        "actor_display_name": actor_display.get(operation.actor_user_id),
        "created_at": operation.created_at.isoformat() if operation.created_at else None,
        "result_version": operation.result_version,
    }


# ---------- 目录 ----------

def search_collection_reminders(
    db: Session,
    *,
    as_of: date,
    user_ctx: UserContext,
    q_text: str | None,
    owner_scope: str | None,
    reminder_state: str | None,
    page: int,
    page_size: int,
) -> dict:
    """项目回款提醒目录（设计 §7.1）。搜索只匹配项目编号/项目名称/合同编号。"""
    if page_size > 200:
        raise CollectionReminderInvalid("每页最多 200 条")
    if reminder_state is not None and reminder_state not in _ALLOWED_STATES:
        raise CollectionReminderInvalid("提醒状态筛选无效")
    try:
        scope = assignments.resolve_owner_scope(user_ctx, owner_scope)
    except assignments.MaintenanceProjectAssignmentPermissionError as exc:
        raise CollectionReminderPermissionError("当前账号只能查看本人负责的维保项目") from exc
    except assignments.MaintenanceProjectAssignmentError as exc:
        raise CollectionReminderInvalid(str(exc)) from exc

    allowed_owner_scopes = ["me", "all"] if user_ctx.role in FULL_SCOPE_ROLES else ["me"]
    amount_visible = _amount_visibility(user_ctx) == "visible"

    project_query = select(MaintenanceProject).where(
        MaintenanceProject.is_active.is_(True)
    )
    if scope == "me":
        project_query = project_query.where(assignments.owned_project_condition(user_ctx))
    search = (q_text or "").strip()
    if search:
        contract_match = select(MaintenanceProjectContract.project_id).where(
            MaintenanceProjectContract.contract_no.icontains(search, autoescape=True)
        )
        project_query = project_query.where(
            or_(
                MaintenanceProject.project_code.icontains(search, autoescape=True),
                MaintenanceProject.display_name.icontains(search, autoescape=True),
                MaintenanceProject.project_id.in_(contract_match),
            )
        )
    projects = list(db.scalars(project_query))
    project_ids = [project.project_id for project in projects]
    if not project_ids:
        payload = {
            "rows": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "owner_scope": scope,
            "allowed_owner_scopes": allowed_owner_scopes,
            "as_of": as_of,
            "data_version": _data_version({"rows": [], "as_of": as_of}),
            "amount_visibility": "visible" if amount_visible else "restricted",
        }
        return payload

    contracts = list(
        db.scalars(
            select(MaintenanceProjectContract).where(
                MaintenanceProjectContract.project_id.in_(project_ids)
            )
        )
    )
    contract_no_by_id = {c.project_contract_id: c.contract_no for c in contracts}
    contracts_by_project: dict[str, list[MaintenanceProjectContract]] = {}
    for contract in contracts:
        contracts_by_project.setdefault(contract.project_id, []).append(contract)
    periods = {
        period.project_id: period
        for period in db.scalars(
            select(MaintenanceServicePeriod).where(
                MaintenanceServicePeriod.project_id.in_(project_ids)
            )
        )
    }
    assignment_views = assignments.active_assignment_views(db, project_ids=project_ids)
    milestones = list(
        db.scalars(
            select(MaintenanceCollectionMilestone).where(
                MaintenanceCollectionMilestone.project_id.in_(project_ids),
                MaintenanceCollectionMilestone.is_active.is_(True),
            )
        )
    )
    milestones_by_project: dict[str, list[MaintenanceCollectionMilestone]] = {}
    for milestone in milestones:
        milestones_by_project.setdefault(milestone.project_id, []).append(milestone)

    rows: list[dict] = []
    for project in projects:
        project_milestones = milestones_by_project.get(project.project_id, [])
        states = [
            derive_reminder_state(milestone, as_of=as_of)
            for milestone in project_milestones
        ]
        if reminder_state is not None and reminder_state not in states:
            continue
        next_actionable = select_next_actionable_milestone(
            [milestone for milestone in project_milestones
             if _payment_fields(db, milestone, as_of=as_of)["payment_state"] != "paid"],
            as_of=as_of,
        )
        if next_actionable is None:
            next_actionable_dict = None
            sort_rank = _EMPTY_PROJECT_RANK
            sort_month = date.max
        else:
            next_actionable_dict = _actionable_dict(
                db,
                next_actionable,
                as_of=as_of,
                amount_visible=amount_visible,
                contract_no_by_id=contract_no_by_id,
            )
            sort_rank = _NEXT_ACTIONABLE_RANK[next_actionable_dict["reminder_state"]]
            sort_month = next_actionable.planned_date or date.max
        project_contracts = sorted(
            contracts_by_project.get(project.project_id, []),
            key=lambda c: (c.effective_from, c.project_contract_id),
        )
        rows.append(
            {
                "project": {
                    "project_id": project.project_id,
                    "project_code": project.project_code,
                    "display_name": project.display_name,
                    "lifecycle_status": project.lifecycle_status,
                    "version": project.version,
                    "manager_assignment": _manager_assignment(
                        assignment_views.get(project.project_id)
                    ),
                    "service_period": _service_period_dict(
                        periods.get(project.project_id)
                    ),
                    "contracts": [
                        _contract_ref(contract, as_of=as_of)
                        for contract in project_contracts
                    ],
                },
                "reminder_counts": _reminder_counts(states),
                "next_actionable_milestone": next_actionable_dict,
                "_sort": (sort_rank, sort_month, project.project_code),
            }
        )

    rows.sort(key=lambda row: row["_sort"])
    total = len(rows)
    paged = [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in rows[(page - 1) * page_size : page * page_size]
    ]
    return {
        "rows": paged,
        "total": total,
        "page": page,
        "page_size": page_size,
        "owner_scope": scope,
        "allowed_owner_scopes": allowed_owner_scopes,
        "as_of": as_of,
        "data_version": _data_version(
            {
                "rows": [
                    {key: value for key, value in row.items() if not key.startswith("_")}
                    for row in rows
                ],
                "as_of": as_of,
            }
        ),
        "amount_visibility": "visible" if amount_visible else "restricted",
    }


# ---------- 详情 ----------

def get_project_collection_milestones(
    db: Session,
    *,
    project_id: str,
    as_of: date,
    user_ctx: UserContext,
) -> dict | None:
    """项目全部计划节点详情（设计 §7.2）。项目不存在或归档 → None（404）；
    无项目权限 → 403，不泄漏不可见项目。
    """
    project = db.scalar(
        select(MaintenanceProject).where(MaintenanceProject.project_id == project_id)
    )
    if project is None or not project.is_active:
        return None
    if not assignments.can_access_project(db, project_id=project_id, user_ctx=user_ctx):
        raise CollectionReminderPermissionError("无权访问该维保项目")

    amount_visible = _amount_visibility(user_ctx) == "visible"
    contracts = list(
        db.scalars(
            select(MaintenanceProjectContract)
            .where(MaintenanceProjectContract.project_id == project_id)
            .order_by(
                MaintenanceProjectContract.effective_from,
                MaintenanceProjectContract.project_contract_id,
            )
        )
    )
    contract_no_by_id = {c.project_contract_id: c.contract_no for c in contracts}
    period = db.scalar(
        select(MaintenanceServicePeriod).where(
            MaintenanceServicePeriod.project_id == project_id
        )
    )
    assignment_views = assignments.active_assignment_views(db, project_ids=[project_id])
    milestones = list(
        db.scalars(
            select(MaintenanceCollectionMilestone)
            .where(MaintenanceCollectionMilestone.project_id == project_id)
            .where(MaintenanceCollectionMilestone.is_active.is_(True))
            .order_by(
                MaintenanceCollectionMilestone.planned_date.asc().nulls_last(),
                MaintenanceCollectionMilestone.sequence,
                MaintenanceCollectionMilestone.milestone_id,
            )
        )
    )
    milestone_ids = [milestone.milestone_id for milestone in milestones]
    operations_by_milestone: dict[str, MaintenanceCollectionMilestoneOperation] = {}
    actor_ids: set[int] = set()
    followed_up_ids: set[int] = set()
    if milestone_ids:
        operations = list(
            db.scalars(
                select(MaintenanceCollectionMilestoneOperation)
                .where(
                    MaintenanceCollectionMilestoneOperation.milestone_id.in_(milestone_ids)
                )
                .order_by(
                    MaintenanceCollectionMilestoneOperation.created_at.desc(),
                    MaintenanceCollectionMilestoneOperation.operation_id,
                )
            )
        )
        for operation in operations:
            operations_by_milestone.setdefault(operation.milestone_id, operation)
            actor_ids.add(operation.actor_user_id)
        for milestone in milestones:
            if milestone.followed_up_by is not None:
                followed_up_ids.add(milestone.followed_up_by)
    actor_display = _display_names(db, actor_ids)
    followed_up_display = _display_names(db, followed_up_ids)

    states = [derive_reminder_state(milestone, as_of=as_of) for milestone in milestones]
    rows = [
        _milestone_row(
            db,
            milestone,
            as_of=as_of,
            amount_visible=amount_visible,
            contract_no_by_id=contract_no_by_id,
            followed_up_display=followed_up_display,
            last_operation_dict=(
                _operation_dict(operations_by_milestone[milestone.milestone_id], actor_display=actor_display)
                if milestone.milestone_id in operations_by_milestone
                else None
            ),
        )
        for milestone in milestones
    ]
    payload = {
        "project": {
            "project_id": project.project_id,
            "project_code": project.project_code,
            "display_name": project.display_name,
            "lifecycle_status": project.lifecycle_status,
            "version": project.version,
            "manager_assignment": _manager_assignment(
                assignment_views.get(project.project_id)
            ),
            "service_period": _service_period_dict(period),
            "contracts": [_contract_ref(contract, as_of=as_of) for contract in contracts],
        },
        "summary": _reminder_counts(states),
        "rows": rows,
        "as_of": as_of,
        "data_version": _data_version({"rows": rows, "as_of": as_of}),
        "amount_visibility": "visible" if amount_visible else "restricted",
    }
    return payload


# ---------- 写操作 ----------

def _controlled_snapshot(milestone: MaintenanceCollectionMilestone) -> dict:
    """操作账本 before/after 只保存受控字段，不保存金额或整行业务值。"""
    return {
        "follow_up_status": milestone.follow_up_status,
        "follow_up_review_required": bool(milestone.follow_up_review_required),
        "followed_up_by": milestone.followed_up_by,
        "followed_up_at": (
            milestone.followed_up_at.isoformat() if milestone.followed_up_at else None
        ),
        "follow_up_note": milestone.follow_up_note,
        "planned_date": (
            milestone.planned_date.isoformat() if milestone.planned_date else None
        ),
        "date_precision": milestone.date_precision,
        "version": milestone.version,
    }


def _replay_or_conflict(
    db: Session,
    existing: MaintenanceCollectionMilestoneOperation,
    *,
    milestone_id: str,
    actor: SysUser,
    payload_hash: str,
    user_ctx: UserContext,
) -> dict:
    """幂等账本命中：沿其 milestone 重新校验项目 scope；actor、路径 milestone
    与 payload hash 全部相同才重放首次 ``result_json``，否则 409。"""
    op_milestone = db.scalar(
        select(MaintenanceCollectionMilestone).where(
            MaintenanceCollectionMilestone.milestone_id == existing.milestone_id
        )
    )
    if op_milestone is None or not assignments.can_access_project(
        db, project_id=op_milestone.project_id, user_ctx=user_ctx
    ):
        raise CollectionReminderPermissionError("无权访问该维保项目")
    if (
        existing.milestone_id != milestone_id
        or existing.actor_user_id != actor.id
        or existing.payload_hash != payload_hash
    ):
        raise CollectionReminderConflict("同一幂等键已被不同请求使用，请刷新后重试")
    return {**existing.result_json, "idempotent_replay": True}


def follow_up_collection_milestone(
    db: Session,
    *,
    milestone_id: str,
    expected_version: int,
    idempotency_key: str,
    action: str,
    planned_month: str | None,
    note: str | None,
    reason: str | None,
    operator: str,
    user_ctx: UserContext,
    as_of: date,
) -> dict | None:
    """标记已处理/改期/重新打开（设计 §7.3）。

    节点不存在 → None（404）。canary 门、实名账号、项目 scope、inactive
    project/contract 全部在执行时重新校验，任何失败零写入。
    """
    milestone = db.scalar(
        select(MaintenanceCollectionMilestone).where(
            MaintenanceCollectionMilestone.milestone_id == milestone_id
        )
    )
    if milestone is None:
        return None
    project_id = milestone.project_id

    # canary 运行时门（设计 §12 / Task 1）：其他项目固定 403，零写入。
    if get_settings().maintenance_collection_canary_project_id:
        if project_id != get_settings().maintenance_collection_canary_project_id:
            raise CollectionReminderCanaryDenied("灰度期间仅允许在指定项目上操作")

    actor = db.scalar(
        select(SysUser).where(
            SysUser.username == operator,
            SysUser.is_active.is_(True),
        )
    )
    if actor is None:
        raise CollectionReminderPermissionError("账号不存在或已停用")
    if not assignments.can_access_project(db, project_id=project_id, user_ctx=user_ctx):
        raise CollectionReminderPermissionError("无权访问该维保项目")

    payload_hash = follow_up_payload_hash(
        actor_user_id=actor.id,
        milestone_id=milestone_id,
        expected_version=expected_version,
        action=action,
        planned_month=planned_month,
        note=note,
        reason=reason,
    )

    existing = db.scalar(
        select(MaintenanceCollectionMilestoneOperation).where(
            MaintenanceCollectionMilestoneOperation.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        return _replay_or_conflict(
            db,
            existing,
            milestone_id=milestone_id,
            actor=actor,
            payload_hash=payload_hash,
            user_ctx=user_ctx,
        )

    # 既有稳定锁顺序：workbook state → project → contract → milestone。
    exists = db.scalar(
        select(MaintenanceProject.project_id).where(
            MaintenanceProject.project_id == project_id
        )
    )
    if exists is None:
        return None
    state = operations.get_or_create_workbook_state(db, project_id=project_id, lock=True)
    project = db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )
    if project is None or not project.is_active:
        raise CollectionReminderNotFound("资源不存在或不可见")
    contract = db.scalar(
        select(MaintenanceProjectContract)
        .where(
            MaintenanceProjectContract.project_contract_id
            == milestone.project_contract_id
        )
        .with_for_update()
    )
    if contract is None or contract.project_id != project_id:
        raise CollectionReminderNotFound("资源不存在或不可见")
    if contract.effective_to is not None and contract.effective_to < as_of:
        raise CollectionReminderNotFound("资源不存在或不可见")
    milestone = db.scalar(
        select(MaintenanceCollectionMilestone)
        .where(MaintenanceCollectionMilestone.milestone_id == milestone_id)
        .with_for_update()
    )
    if milestone is None:
        return None
    if milestone.project_id != project_id:
        raise CollectionReminderNotFound("资源不存在或不可见")
    if milestone.version != expected_version:
        raise CollectionReminderConflict(
            "数据已变化，请刷新后重试",
            current_version=milestone.version,
            current_data_version=state.data_version,
        )

    current_state = derive_reminder_state(milestone, as_of=as_of)
    clean_reason = (reason or "").strip()
    if action == "handle":
        if current_state not in ("overdue", "due_this_month", "upcoming"):
            raise CollectionReminderInvalid("只有待处理且完整的节点可以标记已处理")
        # F6：回款提醒关闭 = 已上传凭证（巡检报告/图片/PDF）。
        if collection_evidence.active_evidence_count(db, milestone_id) == 0:
            raise CollectionReminderInvalid(
                "关闭回款提醒前必须上传凭证（巡检报告/图片/PDF）"
            )
    elif action == "reschedule":
        if current_state not in ("overdue", "due_this_month", "upcoming"):
            raise CollectionReminderInvalid("只有待处理且完整的节点可以改期")
        if planned_month is None:
            raise CollectionReminderInvalid("改期必须提供计划月份")
        new_planned_date = date(
            int(planned_month[:4]), int(planned_month[5:7]), 1
        )
    elif action == "reopen":
        if current_state not in ("handled", "needs_review"):
            raise CollectionReminderInvalid("只有已处理节点可以重新打开")
        if not clean_reason:
            raise CollectionReminderInvalid("重新打开必须提供理由")
    else:
        raise CollectionReminderInvalid("不支持的操作类型")

    before = _controlled_snapshot(milestone)
    if action == "handle":
        milestone.follow_up_status = "handled"
        milestone.followed_up_by = actor.id
        milestone.followed_up_at = datetime.now(UTC)
        milestone.follow_up_note = (note or "").strip() or None
        milestone.follow_up_review_required = False
    elif action == "reschedule":
        milestone.planned_date = new_planned_date
        milestone.date_precision = "month"
    elif action == "reopen":
        milestone.follow_up_status = "pending"
        milestone.followed_up_by = None
        milestone.followed_up_at = None
        milestone.follow_up_note = None
        milestone.follow_up_review_required = False
    milestone.version += 1
    after = _controlled_snapshot(milestone)

    amount_visible = _amount_visibility(user_ctx) == "visible"
    row = _milestone_row(
        db,
        milestone,
        as_of=as_of,
        amount_visible=amount_visible,
        contract_no_by_id={milestone.project_contract_id: contract.contract_no},
        followed_up_display={actor.id: actor.display_name or actor.username},
        last_operation_dict=None,
    )
    payload = {
        "row": row,
        "data_version": _data_version({"row": row}),
        "idempotent_replay": False,
    }
    operation = MaintenanceCollectionMilestoneOperation(
        operation_id=str(uuid4()),
        milestone_id=milestone.milestone_id,
        action=action,
        idempotency_key=idempotency_key,
        expected_version=expected_version,
        result_version=milestone.version,
        payload_hash=payload_hash,
        before_payload=before,
        after_payload=after,
        result_json=payload,
        reason=None if action == "handle" else clean_reason,
        actor_user_id=actor.id,
    )
    try:
        db.add(operation)
        db.flush()
    except IntegrityError:
        # 并发相同幂等键：回滚本次写入，回读账本后重放或 409。
        db.rollback()
        existing = db.scalar(
            select(MaintenanceCollectionMilestoneOperation).where(
                MaintenanceCollectionMilestoneOperation.idempotency_key == idempotency_key
            )
        )
        if existing is None:
            raise
        return _replay_or_conflict(
            db,
            existing,
            milestone_id=milestone_id,
            actor=actor,
            payload_hash=payload_hash,
            user_ctx=user_ctx,
        )
    return payload
