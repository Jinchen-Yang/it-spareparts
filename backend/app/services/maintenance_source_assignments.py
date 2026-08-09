"""Deep write/read service for manual source-order project assignments."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectAuditLog
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.security import FULL_SCOPE_ROLES, UserContext
from app.services import maintenance_project_assignments
from app.services.query_filters import active_orders


class SourceAssignmentError(Exception):
    """Invalid manual assignment request."""


class SourceAssignmentConflict(Exception):
    """The caller's expected assignment is stale."""


class SourceAssignmentPermissionError(Exception):
    """The caller is outside the full project-assignment write scope."""


def assignment_dict(row: MaintenanceSourceOrderAssignment) -> dict:
    return {
        "assignment_id": row.assignment_id,
        "source_order_id": row.source_order_id,
        "project_id": row.project_id,
        "is_active": row.is_active,
        "version": row.version,
    }


def _clean_reason(reason: str) -> str:
    clean = reason.strip()
    if not clean:
        raise SourceAssignmentError("项目归属原因不能为空")
    if len(clean) > 1000:
        raise SourceAssignmentError("项目归属原因过长（最多 1000 个字符）")
    return clean


def _require_full_scope(user_ctx: UserContext) -> None:
    if user_ctx.role not in FULL_SCOPE_ROLES:
        raise SourceAssignmentPermissionError(
            "仅全量项目范围账号可确认、改派或撤销来源维保单归属"
        )


def list_source_orders(
    db: Session,
    *,
    q_text: str | None,
    source_order_ids: list[str] | None,
    assignment_status: str,
    project_id: str | None,
    page: int,
    page_size: int,
    user_ctx: UserContext,
) -> dict:
    active_join = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    filters = []
    if user_ctx.role not in FULL_SCOPE_ROLES:
        # Project managers may inspect only already-assigned source orders for
        # projects they currently own.  Unassigned rows must never become an
        # existence oracle for another manager's business data.
        filters.extend(
            [
                MaintenanceSourceOrderAssignment.assignment_id.is_not(None),
                MaintenanceSourceOrderAssignment.project_id.in_(
                    maintenance_project_assignments.owned_project_ids(user_ctx)
                ),
            ]
        )
    if source_order_ids:
        normalized_source_ids = {
            str(source_order_id).strip() for source_order_id in source_order_ids
        }
        if "" in normalized_source_ids:
            raise SourceAssignmentError("来源维保单 ID 不能为空")
        filters.append(FMaintenanceOrder.raw_order_id.in_(normalized_source_ids))
    if assignment_status == "assigned":
        filters.append(MaintenanceSourceOrderAssignment.assignment_id.is_not(None))
    elif assignment_status == "unassigned":
        filters.append(MaintenanceSourceOrderAssignment.assignment_id.is_(None))
    elif assignment_status != "all":
        raise SourceAssignmentError("归属状态仅支持 unassigned、assigned 或 all")
    if project_id:
        filters.append(MaintenanceSourceOrderAssignment.project_id == project_id)
    if q_text and (search := q_text.strip()):
        filters.append(
            or_(
                FMaintenanceOrder.raw_order_id.icontains(search, autoescape=True),
                FMaintenanceOrder.order_no.icontains(search, autoescape=True),
                FMaintenanceOrder.project_raw.icontains(search, autoescape=True),
                FMaintenanceOrder.project_std.icontains(search, autoescape=True),
            )
        )

    count_stmt = active_orders(
        select(func.count())
        .select_from(FMaintenanceOrder)
        .outerjoin(MaintenanceSourceOrderAssignment, active_join)
        .where(*filters),
        FMaintenanceOrder,
    )
    total = int(db.scalar(count_stmt) or 0)
    fact_statement = (
        select(
            FMaintenanceOrder,
            MaintenanceSourceOrderAssignment,
            MaintenanceProject,
        )
        .outerjoin(MaintenanceSourceOrderAssignment, active_join)
        .outerjoin(
            MaintenanceProject,
            MaintenanceProject.project_id
            == MaintenanceSourceOrderAssignment.project_id,
        )
        .where(*filters)
        .order_by(
            FMaintenanceOrder.order_date.desc().nullslast(),
            FMaintenanceOrder.order_no,
            FMaintenanceOrder.raw_order_id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    facts = list(db.execute(active_orders(fact_statement, FMaintenanceOrder)).all())
    rows = []
    for source, assignment, project in facts:
        rows.append(
            {
                "raw_order_id": source.raw_order_id,
                "order_no": source.order_no,
                "order_date": source.order_date,
                "project_raw": source.project_raw,
                "project_std": source.project_std,
                "assignment_id": (
                    assignment.assignment_id if assignment is not None else None
                ),
                "assignment_version": (
                    assignment.version if assignment is not None else None
                ),
                "assigned_project": (
                    {
                        "project_id": project.project_id,
                        "project_code": project.project_code,
                        "display_name": project.display_name,
                        "is_active": project.is_active,
                    }
                    if project is not None
                    else None
                ),
            }
        )
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def assign_source_orders(
    db: Session,
    *,
    project_id: str,
    items: list[dict],
    reason: str,
    operated_by: str,
    user_ctx: UserContext,
) -> list[dict]:
    _require_full_scope(user_ctx)
    clean_reason = _clean_reason(reason)
    project = db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )
    if project is None:
        raise SourceAssignmentError("目标项目主档不存在")
    if not maintenance_project_assignments.can_access_project(
        db,
        project_id=project.project_id,
        user_ctx=user_ctx,
    ):
        raise SourceAssignmentPermissionError("无权访问目标维保项目")

    normalized_items = [
        {**item, "source_order_id": str(item["source_order_id"]).strip()}
        for item in items
    ]
    source_ids = [item["source_order_id"] for item in normalized_items]
    if any(not source_id for source_id in source_ids):
        raise SourceAssignmentError("来源维保单 ID 不能为空")
    if len(set(source_ids)) != len(source_ids):
        raise SourceAssignmentError("同一批次不能重复提交来源维保单")
    source_statement = (
        select(FMaintenanceOrder)
        .where(FMaintenanceOrder.raw_order_id.in_(source_ids))
        .order_by(FMaintenanceOrder.raw_order_id)
        .with_for_update()
    )
    sources = list(db.scalars(active_orders(source_statement, FMaintenanceOrder)))
    source_by_id = {source.raw_order_id: source for source in sources}
    missing = sorted(set(source_ids) - set(source_by_id))
    if missing:
        raise SourceAssignmentError(f"来源维保单不存在：{missing[0]}")

    current = {
        row.source_order_id: row
        for row in db.scalars(
            select(MaintenanceSourceOrderAssignment)
            .where(
                MaintenanceSourceOrderAssignment.source_order_id.in_(source_ids),
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            )
            .order_by(MaintenanceSourceOrderAssignment.source_order_id)
            .with_for_update()
        )
    }
    for current_assignment in current.values():
        if not maintenance_project_assignments.can_access_project(
            db,
            project_id=current_assignment.project_id,
            user_ctx=user_ctx,
        ):
            raise SourceAssignmentPermissionError("无权访问来源维保单当前所属项目")
    for item in normalized_items:
        source_id = item["source_order_id"]
        current_assignment = current.get(source_id)
        expected_id = item.get("expected_assignment_id")
        expected_version = item.get("expected_version")
        if current_assignment is None and (
            expected_id is not None or expected_version is not None
        ):
            raise SourceAssignmentConflict(
                f"来源维保单 {source_id} 的项目归属已变化，请刷新后重试"
            )
        if current_assignment is not None:
            if current_assignment.project_id == project.project_id:
                # State-based idempotency: an exact replay of an initial assign
                # still carries no expectation pair, while a replay loaded from
                # the current directory carries the current pair. Both return
                # the existing generation without writing a second audit row.
                if expected_id is None and expected_version is None:
                    continue
                if (
                    expected_id == current_assignment.assignment_id
                    and expected_version == current_assignment.version
                ):
                    continue
                raise SourceAssignmentConflict(
                    f"来源维保单 {source_id} 的项目归属已变化，请刷新后重试"
                )
            expectation_matches = (
                expected_id == current_assignment.assignment_id
                and expected_version == current_assignment.version
            )
            if not expectation_matches:
                raise SourceAssignmentConflict(
                    f"来源维保单 {source_id} 的项目归属已变化，请刷新后重试"
                )

    requires_assignment_change = any(
        current.get(source_id) is None
        or current[source_id].project_id != project.project_id
        for source_id in source_ids
    )
    if requires_assignment_change and not project.is_active:
        raise SourceAssignmentError("目标项目主档已归档，不能新增项目归属")

    changed_current: dict[str, MaintenanceSourceOrderAssignment] = {}
    for source_id, current_assignment in current.items():
        if current_assignment.project_id == project.project_id:
            continue
        before = assignment_dict(current_assignment)
        current_assignment.is_active = False
        current_assignment.version += 1
        current_assignment.archived_by = operated_by
        current_assignment.archived_at = datetime.now(timezone.utc)
        db.flush()
        db.add(
            MaintenanceProjectAuditLog(
                project_id=current_assignment.project_id,
                entity_type="source_order_assignment",
                entity_id=current_assignment.assignment_id,
                action="reassign_out",
                before_json=before,
                after_json=assignment_dict(current_assignment),
                reason=clean_reason,
                operated_by=operated_by,
            )
        )
        changed_current[source_id] = current_assignment
    if changed_current:
        db.flush()

    resulting: dict[str, MaintenanceSourceOrderAssignment] = {}
    for source_id in source_ids:
        current_assignment = current.get(source_id)
        if (
            current_assignment is not None
            and current_assignment.project_id == project.project_id
        ):
            resulting[source_id] = current_assignment
            continue
        assignment = MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid4()),
            source_order_id=source_id,
            project_id=project.project_id,
            is_active=True,
            version=1,
            created_by=operated_by,
        )
        db.add(assignment)
        db.flush()
        after = assignment_dict(assignment)
        db.add(
            MaintenanceProjectAuditLog(
                project_id=project.project_id,
                entity_type="source_order_assignment",
                entity_id=assignment.assignment_id,
                action="assign",
                before_json=None,
                after_json=after,
                reason=clean_reason,
                operated_by=operated_by,
            )
        )
        resulting[source_id] = assignment
    db.flush()
    return [assignment_dict(resulting[source_id]) for source_id in source_ids]


def unassign_source_orders(
    db: Session,
    *,
    items: list[dict],
    reason: str,
    operated_by: str,
    user_ctx: UserContext,
) -> list[dict]:
    _require_full_scope(user_ctx)
    clean_reason = _clean_reason(reason)
    assignment_ids = [str(item["assignment_id"]).strip() for item in items]
    if any(not assignment_id for assignment_id in assignment_ids):
        raise SourceAssignmentError("项目归属 ID 不能为空")
    if len(set(assignment_ids)) != len(assignment_ids):
        raise SourceAssignmentError("同一批次不能重复提交项目归属")

    probes = list(
        db.execute(
            select(
                MaintenanceSourceOrderAssignment.assignment_id,
                MaintenanceSourceOrderAssignment.source_order_id,
                MaintenanceSourceOrderAssignment.project_id,
            ).where(
                MaintenanceSourceOrderAssignment.assignment_id.in_(assignment_ids),
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            )
        ).all()
    )
    if len(probes) != len(assignment_ids):
        raise SourceAssignmentConflict("项目归属已变化，请刷新后重试")
    for _assignment_id, _source_order_id, project_id in probes:
        if not maintenance_project_assignments.can_access_project(
            db,
            project_id=project_id,
            user_ctx=user_ctx,
        ):
            raise SourceAssignmentPermissionError("无权访问来源维保单当前所属项目")
    source_ids = sorted({source_order_id for _, source_order_id, _ in probes})
    source_statement = (
        select(FMaintenanceOrder)
        .where(FMaintenanceOrder.raw_order_id.in_(source_ids))
        .order_by(FMaintenanceOrder.raw_order_id)
        .with_for_update()
    )
    locked_sources = list(
        db.scalars(active_orders(source_statement, FMaintenanceOrder))
    )
    if len(locked_sources) != len(source_ids):
        raise SourceAssignmentConflict(
            "来源维保单已删除或状态发生变化，请刷新后重试"
        )
    locked = {
        row.assignment_id: row
        for row in db.scalars(
            select(MaintenanceSourceOrderAssignment)
            .where(
                MaintenanceSourceOrderAssignment.assignment_id.in_(assignment_ids),
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            )
            .order_by(MaintenanceSourceOrderAssignment.assignment_id)
            .with_for_update()
        )
    }
    if len(locked) != len(assignment_ids):
        raise SourceAssignmentConflict("项目归属已变化，请刷新后重试")
    for item in items:
        row = locked.get(str(item["assignment_id"]).strip())
        if row is None or row.version != item["expected_version"]:
            raise SourceAssignmentConflict("项目归属已变化，请刷新后重试")

    archived: list[MaintenanceSourceOrderAssignment] = []
    for assignment_id in assignment_ids:
        row = locked[assignment_id]
        before = assignment_dict(row)
        row.is_active = False
        row.version += 1
        row.archived_by = operated_by
        row.archived_at = datetime.now(timezone.utc)
        db.flush()
        db.add(
            MaintenanceProjectAuditLog(
                project_id=row.project_id,
                entity_type="source_order_assignment",
                entity_id=row.assignment_id,
                action="unassign",
                before_json=before,
                after_json=assignment_dict(row),
                reason=clean_reason,
                operated_by=operated_by,
            )
        )
        archived.append(row)
    db.flush()
    return [assignment_dict(row) for row in archived]
