"""Deep write/read service for manual source-order project assignments."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY, get_settings
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAlias,
    MaintenanceProjectAuditLog,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_project_operations import (
    MaintenanceProjectWorkbookState,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysUser
from app.security import FULL_SCOPE_ROLES, UserContext
from app.business_time import business_today
from app.services import (
    maintenance_project_assignments,
    maintenance_project_identity,
    project_names,
)
from app.services.maintenance_ledger import (
    _lifecycle_status,
    _period_from_display_name,
)
from app.services.query_filters import active_beta_maintenance_orders


class SourceAssignmentError(Exception):
    """Invalid manual assignment request."""


class SourceAssignmentConflict(Exception):
    """The caller's expected assignment is stale."""


class SourceAssignmentPermissionError(Exception):
    """The caller is outside the full project-assignment write scope."""


def _lock_data_change(db: Session) -> None:
    """与需求单作废/恢复共用的事务锁，必须位于任何 DB probe/state 锁前。"""
    db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": DATA_CHANGE_ADVISORY_LOCK_KEY},
    )


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


# 归属候选（plan v1.3 M2-1）：只出候选、不自动写；确认仍走 assign 的人工+审计通道。
_CANDIDATE_LIMIT = 5
_CANDIDATE_TRGM_THRESHOLD = 0.6


def _candidates_for(db: Session, project_std: str | None) -> list[dict]:
    """按 project_std（ETL 已剥「预交付-」前缀）生成 ≤5 个候选。

    一级：lower(project_code) 精确命中（ux_maintenance_project_code_ci）→ exact/1.0 恒排首；
    二级：pg_trgm 相似度 ≥ 0.6（ix_maintenance_project_*_trgm GIN 索引）按分降序。
    纯只读；多候选/低分一律不自动（ADR-0002：名称只是线索）。
    """
    if not project_std:
        return []
    out: list[dict] = []
    exact = db.execute(
        select(MaintenanceProject)
        .where(
            func.lower(MaintenanceProject.project_code) == project_std.lower(),
            MaintenanceProject.is_active.is_(True),
        )
    ).scalars().all()
    seen: set[str] = set()
    for proj in exact:
        out.append({
            "project_id": proj.project_id,
            "project_code": proj.project_code,
            "display_name": proj.display_name,
            "match_type": "exact",
            "score": 1.0,
        })
        seen.add(proj.project_id)
    if len(out) >= _CANDIDATE_LIMIT:
        return out[:_CANDIDATE_LIMIT]
    score = func.greatest(
        func.similarity(MaintenanceProject.project_code, project_std),
        func.similarity(MaintenanceProject.display_name, project_std),
    )
    fuzzy = db.execute(
        select(MaintenanceProject, score.label("score"))
        .where(
            MaintenanceProject.is_active.is_(True),
            score >= _CANDIDATE_TRGM_THRESHOLD,
        )
        .order_by(score.desc(), MaintenanceProject.project_code)
        .limit(_CANDIDATE_LIMIT)
    ).all()
    for proj, sim in fuzzy:
        if proj.project_id in seen:
            continue
        out.append({
            "project_id": proj.project_id,
            "project_code": proj.project_code,
            "display_name": proj.display_name,
            "match_type": "trgm",
            "score": round(float(sim), 3),
        })
        if len(out) >= _CANDIDATE_LIMIT:
            break
    return out


def project_xsdd_keys(db: Session, project_id: str) -> set[str]:
    """项目名下的全部 XSDD 销售订单号（台账合同表为准，#45/#46）。

    多合同项目要把名下**每一个**合同号都算作本项目的键——只取第一个会把
    「兵装财务…整体维保」那种 1 项目 2 合同的另一半单据判成不相关。
    """
    from app.models.maintenance_project import MaintenanceProjectContract

    rows = db.execute(
        select(MaintenanceProjectContract.contract_no)
        .where(MaintenanceProjectContract.project_id == project_id,
               MaintenanceProjectContract.contract_no.is_not(None))
    ).scalars().all()
    return {no for no in rows if no}


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
    include_candidates: bool = False,
    xsdd_project_id: str | None = None,
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

    # #48 归属挂靠候选预筛：命中「本项目 XSDD 集合」的未归属单排最前，其余在后。
    # 判定依据＝XSDD 销售订单（#45）；多合同项目把名下**全部**合同号都算本项目的键
    # （生产唯一例外「兵装财务…整体维保」1 项目 2 XSDD，见 #46）。
    # 这是**排序**不是过滤：其余未归属单仍要能看到、能挂，只是排在后面。
    xsdd_keys = project_xsdd_keys(db, xsdd_project_id) if xsdd_project_id else set()
    xsdd_rank = (
        case((FMaintenanceOrder.linked_sales_order_no.in_(xsdd_keys), 0), else_=1)
        if xsdd_keys else None
    )

    count_stmt = active_beta_maintenance_orders(
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
            *([xsdd_rank] if xsdd_rank is not None else []),
            FMaintenanceOrder.order_date.desc().nullslast(),
            FMaintenanceOrder.order_no,
            FMaintenanceOrder.raw_order_id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    facts = list(db.execute(active_beta_maintenance_orders(fact_statement, FMaintenanceOrder)).all())
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
        if include_candidates:
            # 展示板扩展字段（plan v1.3 M2）。默认关闭 → 目录响应形状逐字节不变，
            # 既有契约测试与前端不受影响。
            # - candidates：只对未归属行生成（纯只读，绝不自动写 assignment）；
            # - is_pre_delivery：预交付徽标（方案 B），取自 project_raw 前缀，不落库。
            rows[-1]["candidates"] = (
                _candidates_for(db, source.project_std)
                if assignment is None else []
            )
            rows[-1]["is_pre_delivery"] = project_names.is_pre_delivery(
                source.project_raw
            )
        if xsdd_project_id:
            # 同样只在显式请求时追加，保持既有目录契约逐字节不变
            rows[-1]["matches_project_xsdd"] = bool(
                source.linked_sales_order_no
                and source.linked_sales_order_no in xsdd_keys
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
    _prelocked_states: dict[str, MaintenanceProjectWorkbookState] | None = None,
    _changed_project_ids: set[str] | None = None,
) -> list[dict]:
    _require_full_scope(user_ctx)
    clean_reason = _clean_reason(reason)
    normalized_items = [
        {**item, "source_order_id": str(item["source_order_id"]).strip()}
        for item in items
    ]
    source_ids = [item["source_order_id"] for item in normalized_items]
    if any(not source_id for source_id in source_ids):
        raise SourceAssignmentError("来源维保单 ID 不能为空")
    if len(set(source_ids)) != len(source_ids):
        raise SourceAssignmentError("同一批次不能重复提交来源维保单")
    if _prelocked_states is None:
        _lock_data_change(db)

    # XSDD identity locks precede workbook/project row locks.  Manual assignment
    # may not split one XSDD across projects; reviewed whole-project merge is a
    # separate operation and is intentionally not smuggled through this API.
    preflight_xsdds = [
        value
        for value in db.scalars(
            select(FMaintenanceOrder.linked_sales_order_no).where(
                FMaintenanceOrder.raw_order_id.in_(source_ids)
            )
        )
        if maintenance_project_identity.normalize_xsdd(value)
    ]
    maintenance_project_identity.lock_xsdd_identities(db, preflight_xsdds)
    for xsdd in preflight_xsdds:
        try:
            owner = maintenance_project_identity.resolve_xsdd_project(db, xsdd)
        except maintenance_project_identity.XsddProjectConflict as exc:
            raise SourceAssignmentConflict(str(exc)) from exc
        if owner is not None and owner != project_id:
            raise SourceAssignmentConflict(
                f"XSDD {xsdd} 已归属于其他项目，不能拆分挂靠"
            )

    # Probe before taking any fact lock, then acquire every old/new workbook
    # state in one sorted pass.  The locked reread below rejects an owner that
    # appeared after this probe instead of taking a late state lock and
    # creating an order->state inversion.
    if db.scalar(
        select(MaintenanceProject.project_id).where(
            MaintenanceProject.project_id == project_id
        )
    ) is None:
        raise SourceAssignmentError("目标项目主档不存在")
    probed_project_ids = {project_id}
    probed_project_ids.update(
        value
        for value in db.scalars(
            select(MaintenanceSourceOrderAssignment.project_id).where(
                MaintenanceSourceOrderAssignment.source_order_id.in_(source_ids),
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            )
        ).all()
        if value
    )
    from app.services import maintenance_project_operations as operations

    if _prelocked_states is None:
        locked_states = operations.lock_workbook_states(
            db,
            project_ids=probed_project_ids,
        )
    else:
        locked_states = _prelocked_states
        if not probed_project_ids.issubset(locked_states):
            raise SourceAssignmentConflict(
                "来源维保单的项目归属已变化，请刷新后重试"
            )

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

    source_statement = (
        select(FMaintenanceOrder)
        .where(FMaintenanceOrder.raw_order_id.in_(source_ids))
        .order_by(FMaintenanceOrder.raw_order_id)
        .with_for_update()
    )
    sources = list(db.scalars(active_beta_maintenance_orders(source_statement, FMaintenanceOrder)))
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
    if any(
        assignment.project_id not in probed_project_ids
        for assignment in current.values()
    ):
        raise SourceAssignmentConflict(
            "来源维保单的项目归属已变化，请刷新后重试"
        )
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

    for xsdd in preflight_xsdds:
        try:
            maintenance_project_identity.claim_xsdd_project(
                db,
                value=xsdd,
                project_id=project.project_id,
                source="manual_assign",
            )
        except maintenance_project_identity.XsddProjectConflict as exc:
            raise SourceAssignmentConflict(str(exc)) from exc

    requires_assignment_change = any(
        current.get(source_id) is None
        or current[source_id].project_id != project.project_id
        for source_id in source_ids
    )
    if requires_assignment_change and not project.is_active:
        raise SourceAssignmentError("目标项目主档已归档，不能新增项目归属")

    changed_current: dict[str, MaintenanceSourceOrderAssignment] = {}
    changed_projects: set[str] = set()
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
        changed_projects.add(current_assignment.project_id)
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
        changed_projects.add(project.project_id)
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
    for changed_project_id in sorted(changed_projects):
        operations.bump_locked_workbook_revision(
            db,
            state=locked_states[changed_project_id],
        )
    if _changed_project_ids is not None:
        _changed_project_ids.update(changed_projects)
    # A source-order assignment is also the only stable project edge allowed
    # for warehouse shipment candidates.  Repair that projection in the same
    # transaction so reassignment can never leave an actionable old-project
    # delivery row behind.
    from app.services import maintenance_warehouse

    maintenance_warehouse.reconcile_project_assignment_links(
        db,
        operated_by=operated_by,
        reason=clean_reason,
        source_order_ids=set(source_ids),
    )
    # plan v1.3 M4-3：新归属立刻让先前无法解析的已应用单据头补上项目
    # （上传顺序无关）。同事务、幂等、不覆盖既有归属。
    # 受展示板总闸约束：flag 关闭时本次发布的新行为必须整体收回，归属确认端点
    # 回到 v1.2 语义（铁律 7「回滚=关 flag」）。
    if get_settings().maintenance_boss_dashboard_enabled:
        from app.services import maintenance_doc_import

        maintenance_doc_import.relink_projects(db, commit=False)
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
    _lock_data_change(db)

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
    from app.services import maintenance_project_operations as operations

    locked_states = operations.lock_workbook_states(
        db,
        project_ids={project_id for _, _, project_id in probes},
    )
    source_ids = sorted({source_order_id for _, source_order_id, _ in probes})
    source_statement = (
        select(FMaintenanceOrder)
        .where(FMaintenanceOrder.raw_order_id.in_(source_ids))
        .order_by(FMaintenanceOrder.raw_order_id)
        .with_for_update()
    )
    locked_sources = list(
        db.scalars(active_beta_maintenance_orders(source_statement, FMaintenanceOrder))
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
    if any(row.project_id not in locked_states for row in locked.values()):
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
    for project_id in sorted({row.project_id for row in archived}):
        operations.bump_locked_workbook_revision(
            db,
            state=locked_states[project_id],
        )
    from app.services import maintenance_warehouse

    maintenance_warehouse.reconcile_project_assignment_links(
        db,
        operated_by=operated_by,
        reason=clean_reason,
        source_order_ids=set(source_ids),
    )
    return [assignment_dict(row) for row in archived]


AUTO_OWNER_BACKFILL_REASON = "导入销售订单自动回填：销售人员列众数"


def salesperson_modes_by_project(
    db: Session, project_ids: list[str]
) -> dict[str, str]:
    """每项目活单 XSDD 销售众数（2026-08-21 客户反馈；并列按名字稳定排序）。

    卡片「销售」与维保负责人自动回填的共用口径：活单条件下按
    (project, salesperson) 分组计数取众数，一次查询覆盖整批项目。
    """
    if not project_ids:
        return {}
    from app.services import maintenance_demands

    rows = db.execute(
        select(
            MaintenanceSourceOrderAssignment.project_id,
            FMaintenanceOrder.salesperson,
            func.count(),
        )
        .select_from(FMaintenanceOrder)
        .join(
            MaintenanceSourceOrderAssignment,
            and_(
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            ),
        )
        .where(
            MaintenanceSourceOrderAssignment.project_id.in_(project_ids),
            FMaintenanceOrder.salesperson.isnot(None),
            FMaintenanceOrder.salesperson != "",
            maintenance_demands.active_demand_condition(),
        )
        .group_by(
            MaintenanceSourceOrderAssignment.project_id,
            FMaintenanceOrder.salesperson,
        )
    ).all()
    counts: dict[str, list[tuple[str, int]]] = {}
    for pid, person, n in rows:
        counts.setdefault(pid, []).append((person, int(n)))
    return {
        pid: sorted(v, key=lambda it: (-it[1], it[0]))[0][0]
        for pid, v in counts.items()
    }


def _backfill_project_owner(
    db: Session,
    *,
    project: MaintenanceProject,
    salesperson: str,
    operated_by: str,
    reason: str = AUTO_OWNER_BACKFILL_REASON,
    _prelocked_state: MaintenanceProjectWorkbookState | None = None,
    _skip_workbook_bump: bool = False,
) -> dict:
    """维保负责人/销售自动回填——**只补空，绝不覆盖人工编辑**（幂等）。

    - `project.salesperson` 为空且未被人工覆盖时补销售众数；人工明确清空不回填；
    - `project.project_manager_id`（维保负责人原文）为空时同值回填；
    - 文本与销售一致且尚无活跃 primary_manager 时，按
      `sys_user.salesperson_name` 匹配活跃账号自动建账号级指派
      （锁/审计走 assign_primary_manager 既有链路）。

    任一字段/指派真实改变 → 同事务 workbook revision +1（bump 按根事务去重，
    多处变更仍只 +1）；全 no-op → +0。``_prelocked_state`` 由调用方按
    state(sorted)→project(sorted) 锁序传入，本函数不得在项目锁后再晚锁 state；
    ``_skip_workbook_bump`` 用于 auto 新建项目的首次成形（保持 revision 0）。
    """
    stats = {"sales_filled": False, "manager_filled": False, "assignment_created": False}
    salesperson = salesperson.strip()
    if not salesperson:
        return stats
    before = {
        "salesperson": project.salesperson,
        "salesperson_override_active": project.salesperson_override_active,
        "project_manager_id": project.project_manager_id,
    }
    if (
        not project.salesperson_override_active
        and not (project.salesperson or "").strip()
    ):
        project.salesperson = salesperson[:64]
        stats["sales_filled"] = True
    manager_text = (project.project_manager_id or "").strip()
    if not manager_text:
        project.project_manager_id = salesperson[:64]
        manager_text = salesperson[:64]
        stats["manager_filled"] = True
    if stats["sales_filled"] or stats["manager_filled"]:
        db.flush()
        db.add(
            MaintenanceProjectAuditLog(
                project_id=project.project_id,
                entity_type="project",
                entity_id=project.project_id,
                action="update",
                before_json=before,
                after_json={
                    "salesperson": project.salesperson,
                    "salesperson_override_active": (
                        project.salesperson_override_active
                    ),
                    "project_manager_id": project.project_manager_id,
                },
                reason=reason,
                operated_by=operated_by,
            )
        )
        db.flush()
        if not _skip_workbook_bump:
            from app.services import maintenance_project_operations as operations

            state = _prelocked_state
            if state is None:
                state = operations.lock_workbook_states(
                    db, project_ids=[project.project_id]
                )[project.project_id]
            operations.bump_locked_workbook_revision(db, state=state)
    # 账号级指派：仅当负责人文本就是销售（含刚回填）且当前无人负责
    if manager_text != (salesperson[:64]):
        return stats
    current = db.scalar(
        select(MaintenanceProjectUserAssignment.assignment_id).where(
            MaintenanceProjectUserAssignment.project_id == project.project_id,
            MaintenanceProjectUserAssignment.responsibility_type == "primary_manager",
            MaintenanceProjectUserAssignment.archived_at.is_(None),
        )
    )
    if current is not None:
        return stats
    user = db.scalar(
        select(SysUser)
        .where(
            SysUser.salesperson_name == salesperson,
            SysUser.is_active.is_(True),
        )
        .order_by(SysUser.id)
    )
    if user is None:
        return stats
    maintenance_project_assignments.assign_primary_manager(
        db,
        project_id=project.project_id,
        user_id=user.id,
        expected_assignment_id=None,
        expected_assignment_version=None,
        reason=reason,
        operated_by=operated_by,
        _prelocked_state=_prelocked_state,
        _skip_workbook_bump=_skip_workbook_bump,
    )
    stats["assignment_created"] = True
    return stats


def _owner_backfill_candidate_condition():
    """Only missing, non-overridden sales fields remain auto-fill candidates."""
    return or_(
        and_(
            MaintenanceProject.salesperson_override_active.is_(False),
            func.coalesce(MaintenanceProject.salesperson, "") == "",
        ),
        func.coalesce(MaintenanceProject.project_manager_id, "") == "",
    )


def backfill_owner_fields(
    db: Session,
    *,
    operated_by: str,
    _prelocked_states: dict[str, MaintenanceProjectWorkbookState] | None = None,
    _no_bump_project_ids: set[str] | None = None,
) -> dict:
    """存量项目销售/维保负责人补齐（幂等：只填空，不动人工编辑）。

    auto-assign 运维按钮顺带执行：未人工覆盖的空销售或空负责人原文按活单销售
    众数回填；台账事实源与人工覆盖均不被自动销售回填改写。

    锁序：候选项目只读探明 → state(sorted) → project(sorted) → 指派行。
    ``_prelocked_states`` 由 auto_assign 在并集排序锁后传入；候选集出现
    未覆盖的项目（并发新建）→ fail closed，绝不晚锁 state。
    ``_no_bump_project_ids`` 是同事务内新建的 auto 项目：首次完整成形保持
    revision 0。
    """
    stats = {"sales_filled_projects": 0, "manager_filled_projects": 0,
             "assignments_created": 0}
    if _prelocked_states is None:
        _lock_data_change(db)
    no_bump = set(_no_bump_project_ids or ())
    candidate_ids = list(
        db.scalars(
            select(MaintenanceProject.project_id).where(
                MaintenanceProject.is_active.is_(True),
                _owner_backfill_candidate_condition(),
            )
        )
    )
    if not candidate_ids:
        return stats
    from app.services import maintenance_project_operations as operations

    if _prelocked_states is None:
        locked_states = operations.lock_workbook_states(
            db, project_ids=candidate_ids
        )
    else:
        locked_states = _prelocked_states
        if not set(candidate_ids).issubset(set(locked_states) | no_bump):
            raise SourceAssignmentConflict("项目主档已变化，请刷新后重试")
    modes = salesperson_modes_by_project(db, candidate_ids)
    projects = list(
        db.scalars(
            select(MaintenanceProject)
            .where(MaintenanceProject.project_id.in_(candidate_ids))
            .order_by(MaintenanceProject.project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    locked_by_id = {project.project_id: project for project in projects}
    if set(locked_by_id) != set(candidate_ids):
        raise SourceAssignmentConflict("项目主档已变化，请刷新后重试")
    for project_id in sorted(candidate_ids):
        project = locked_by_id[project_id]
        if not project.is_active:
            # 锁后复核：并发归档 → fail closed（本轮整体回滚，不写半截）
            raise SourceAssignmentConflict("项目主档已变化，请刷新后重试")
        salesperson = modes.get(project_id)
        if not salesperson:
            continue
        one = _backfill_project_owner(
            db,
            project=project,
            salesperson=salesperson,
            operated_by=operated_by,
            _prelocked_state=locked_states.get(project_id),
            _skip_workbook_bump=project_id in no_bump,
        )
        stats["sales_filled_projects"] += int(one["sales_filled"])
        stats["manager_filled_projects"] += int(one["manager_filled"])
        stats["assignments_created"] += int(one["assignment_created"])
    db.flush()
    return stats


def auto_assign_unassigned(
    db: Session,
    *,
    operated_by: str,
    user_ctx: UserContext,
) -> dict:
    """自动补挂靠（2026-08-18 全自动版）：

    未归属维保订单用自身 project_std（去「预交付-」前缀）：
    - 精确匹配已有项目主档 display_name → 命中唯一项目直接挂靠；
    - 匹配不到任何项目 → 自动创建项目主档（项目名取自 project_std，
      期限从名称解析，编号 AUTO- 递增，lifecycle 由期限计算）再挂靠；
    - 多个项目同名（歧义）或单据无项目名 → 跳过留人工。

    2026-08-21（客户反馈）：收尾顺带对存量/新建项目做销售与维保负责人
    自动回填（`backfill_owner_fields`，只补空不动人工编辑）。
    返回本次执行统计。
    """
    _require_full_scope(user_ctx)
    clean_reason = "自动补挂靠：project_std 精确匹配项目主档"
    today = business_today()
    _lock_data_change(db)

    # 1. 未归属单（assignment_id IS NULL 且未删）
    unassigned_stmt = (
        select(FMaintenanceOrder)
        .outerjoin(
            MaintenanceSourceOrderAssignment,
            and_(
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            ),
        )
        .where(MaintenanceSourceOrderAssignment.assignment_id.is_(None))
    )
    unassigned = list(
        db.scalars(active_beta_maintenance_orders(unassigned_stmt, FMaintenanceOrder))
    )
    if not unassigned:
        result = {"assigned_orders": 0, "matched_projects": 0,
                  "created_projects": 0, "skipped_groups": 0, "skipped_ambiguous": 0}
        # 没有未归属单也要做存量回填——按钮本身就是运维入口
        result.update(backfill_owner_fields(db, operated_by=operated_by))
        db.flush()
        return result

    # 2. XSDD 是项目归并键；只有无 XSDD 的罕见旧单才按项目名兜底。
    # 同一 XSDD 下的不同 project_std 不再各建一个 AUTO 项目，而是作为
    # 同一 canonical 项目的展示 aliases 留存。
    GroupKey = tuple[str, str]

    def _name_for(order: FMaintenanceOrder) -> str:
        return project_names.strip_pre_delivery(order.project_std or "") or (
            project_names.strip_pre_delivery(order.project_raw or "") or ""
        )

    def _group_key(order: FMaintenanceOrder) -> GroupKey | None:
        xsdd = maintenance_project_identity.normalize_xsdd(
            order.linked_sales_order_no
        )
        if xsdd:
            return ("xsdd", xsdd)
        name = _name_for(order)
        return ("name", name) if name else None

    grouped: dict[GroupKey, list[FMaintenanceOrder]] = {}
    group_names: dict[GroupKey, set[str]] = {}
    for order in unassigned:
        key = _group_key(order)
        if key is None:
            continue
        grouped.setdefault(key, []).append(order)
        name = _name_for(order)
        if name:
            group_names.setdefault(key, set()).add(name)

    def _primary_name(key: GroupKey) -> str:
        counts: dict[str, int] = {}
        for order in grouped[key]:
            name = _name_for(order)
            if name:
                counts[name] = counts.get(name, 0) + 1
        if counts:
            return sorted(counts, key=lambda name: (-counts[name], name))[0]
        return f"XSDD-{key[1]}" if key[0] == "xsdd" else key[1]

    def _matched_project_ids(names: set[str]) -> set[str]:
        if not names:
            return set()
        identity_keys = {
            project_names.display_name_identity(name) for name in names
        }
        direct = set(db.scalars(select(MaintenanceProject.project_id).where(
            MaintenanceProject.display_name.in_(sorted(names)),
            MaintenanceProject.is_active.is_(True),
        )))
        aliased = set(db.scalars(
            select(MaintenanceProjectAlias.project_id)
            .join(
                MaintenanceProject,
                MaintenanceProject.project_id == MaintenanceProjectAlias.project_id,
            )
            .where(
                MaintenanceProjectAlias.alias_key.in_(sorted(identity_keys)),
                MaintenanceProject.is_active.is_(True),
            )
        ))
        return direct | aliased

    # XSDD/name advisory 均必须先于 state/project 锁。全局数据锁在更前面，
    # 与人工挂靠、作废/恢复保持同一锁序。
    maintenance_project_identity.lock_xsdd_identities(
        db, [key[1] for key in grouped if key[0] == "xsdd"]
    )
    project_names.lock_display_name_identities(
        db, {name for names in group_names.values() for name in names}
    )

    # 3. 只读规划：XSDD 已有唯一 owner 时优先；无 XSDD 证据才允许
    # 名称/alias 辅助选择。历史同号多项目一律跳过，绝不继续制造新分裂。
    # owner 回填候选也在此探明，与挂靠目标并集一次性排序锁，绝不晚锁。
    planned_existing: dict[GroupKey, str] = {}
    create_groups: set[GroupKey] = set()
    ambiguous_groups: set[GroupKey] = set()
    for key in grouped:
        resolved: str | None = None
        if key[0] == "xsdd":
            try:
                resolved = maintenance_project_identity.resolve_xsdd_project(
                    db, key[1]
                )
            except maintenance_project_identity.XsddProjectConflict:
                ambiguous_groups.add(key)
                continue
            if resolved is not None and db.scalar(
                select(MaintenanceProject.project_id).where(
                    MaintenanceProject.project_id == resolved,
                    MaintenanceProject.is_active.is_(True),
                )
            ) is None:
                ambiguous_groups.add(key)
                continue
        matched_ids = _matched_project_ids(group_names.get(key, set()))
        if resolved is not None:
            if matched_ids and matched_ids != {resolved}:
                ambiguous_groups.add(key)
            else:
                planned_existing[key] = resolved
        elif len(matched_ids) > 1:
            ambiguous_groups.add(key)
        elif matched_ids:
            planned_existing[key] = next(iter(matched_ids))
        else:
            create_groups.add(key)
    backfill_candidate_ids = set(
        db.scalars(
            select(MaintenanceProject.project_id).where(
                MaintenanceProject.is_active.is_(True),
                _owner_backfill_candidate_condition(),
            )
        )
    )
    target_ids = set(planned_existing.values())

    # 4. 锁序铁律：state(sorted) → project(sorted) → 单据/指派行。
    from app.services import maintenance_project_operations as operations

    union_ids = target_ids | backfill_candidate_ids
    locked_states = operations.lock_workbook_states(db, project_ids=union_ids)
    locked_projects: dict[str, MaintenanceProject] = {}
    if union_ids:
        locked_projects = {
            project.project_id: project
            for project in db.scalars(
                select(MaintenanceProject)
                .where(MaintenanceProject.project_id.in_(union_ids))
                .order_by(MaintenanceProject.project_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        }
        if set(locked_projects) != union_ids:
            raise SourceAssignmentConflict("项目主档已变化，请刷新后重试")

    # 5. 锁后复核（fail closed）：XSDD owner、名称 alias、待建组与来源单
    # 单据仍未归属——任一被并发改变则整体回滚，零半截写入。
    for key, planned_id in planned_existing.items():
        current_ids = _matched_project_ids(group_names.get(key, set()))
        if key[0] == "xsdd":
            try:
                current_owner = maintenance_project_identity.resolve_xsdd_project(
                    db, key[1]
                )
            except maintenance_project_identity.XsddProjectConflict as exc:
                raise SourceAssignmentConflict(str(exc)) from exc
            if current_owner not in {None, planned_id}:
                raise SourceAssignmentConflict("XSDD 项目归属已变化，请刷新后重试")
        if current_ids and current_ids != {planned_id}:
            raise SourceAssignmentConflict("目标项目已变化，请刷新后重试")
    for key in create_groups:
        if _matched_project_ids(group_names.get(key, set())):
            raise SourceAssignmentConflict("目标项目已变化，请刷新后重试")
        if key[0] == "xsdd":
            try:
                current_owner = maintenance_project_identity.resolve_xsdd_project(
                    db, key[1]
                )
            except maintenance_project_identity.XsddProjectConflict as exc:
                raise SourceAssignmentConflict(str(exc)) from exc
            if current_owner is not None:
                raise SourceAssignmentConflict("XSDD 项目归属已变化，请刷新后重试")
    order_ids = sorted(order.raw_order_id for order in unassigned)
    locked_orders = {
        order.raw_order_id: order
        for order in db.scalars(
            active_beta_maintenance_orders(
                select(FMaintenanceOrder)
                .where(FMaintenanceOrder.raw_order_id.in_(order_ids))
                .order_by(FMaintenanceOrder.raw_order_id)
                .with_for_update()
                .execution_options(populate_existing=True),
                FMaintenanceOrder,
            )
        )
    }
    if set(locked_orders) != set(order_ids):
        raise SourceAssignmentConflict("来源维保单已变化，请刷新后重试")
    if db.scalar(
        select(MaintenanceSourceOrderAssignment.assignment_id).where(
            MaintenanceSourceOrderAssignment.source_order_id.in_(order_ids),
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
    ) is not None:
        raise SourceAssignmentConflict(
            "来源维保单的项目归属已变化，请刷新后重试"
        )
    for key, orders in grouped.items():
        for order in orders:
            locked = locked_orders[order.raw_order_id]
            if _group_key(locked) != key:
                raise SourceAssignmentConflict(
                    "来源维保单已变化，请刷新后重试"
                )

    # 6. 实际挂靠：已有项目直接挂；无项目则自动建项目再挂
    assigned_orders = 0
    matched_projects: set[str] = set()
    created_projects = 0
    created_project_ids: set[str] = set()
    changed_existing: set[str] = set()
    skipped_groups = 0
    for key, orders in grouped.items():
        if key in ambiguous_groups:
            continue
        primary_name = _primary_name(key)
        if key in planned_existing:
            project = locked_projects[planned_existing[key]]
            matched_projects.add(project.project_id)
        else:
            # 一个新 XSDD 至多创建一个 AUTO 项目，其他来源名全部成为 alias。
            period_from, period_to = _period_from_display_name(primary_name)
            project = _create_auto_project(
                db, display_name=primary_name,
                period_from=period_from, period_to=period_to,
                lifecycle=_lifecycle_status(period_from, period_to, today),
            )
            created_projects += 1
            created_project_ids.add(project.project_id)
        if key[0] == "xsdd":
            try:
                maintenance_project_identity.claim_xsdd_project(
                    db,
                    value=key[1],
                    project_id=project.project_id,
                    source="auto_assign",
                )
            except maintenance_project_identity.XsddProjectConflict as exc:
                raise SourceAssignmentConflict(str(exc)) from exc
        maintenance_project_identity.record_alias(
            db,
            project_id=project.project_id,
            alias_name=project.display_name,
            source="project_primary",
        )
        for alias_name in sorted(group_names.get(key, set())):
            maintenance_project_identity.record_alias(
                db,
                project_id=project.project_id,
                alias_name=alias_name,
                source="source_order",
            )
        for order in orders:
            assignment = MaintenanceSourceOrderAssignment(
                assignment_id=str(uuid4()),
                source_order_id=order.raw_order_id,
                project_id=project.project_id,
                is_active=True,
                version=1,
                created_by=operated_by,
            )
            db.add(assignment)
            db.flush()
            db.add(
                MaintenanceProjectAuditLog(
                    project_id=project.project_id,
                    entity_type="source_order_assignment",
                    entity_id=assignment.assignment_id,
                    action="assign",
                    before_json=None,
                    after_json=assignment_dict(assignment),
                    reason=clean_reason,
                    operated_by=operated_by,
                )
            )
            assigned_orders += 1
            matched_projects.add(project.project_id)
            if project.project_id not in created_project_ids:
                changed_existing.add(project.project_id)
    db.flush()
    # 既有项目导出投影真实改变 → 同事务 revision 恰好 +1（bump 内部按根事务
    # 去重，与后续 owner 回填的 bump 合并）；新建项目首次成形保持 revision 0。
    for changed_project_id in sorted(changed_existing):
        operations.bump_locked_workbook_revision(
            db,
            state=locked_states[changed_project_id],
        )
    # 2026-08-21 客户反馈：存量与新建项目的销售/维保负责人自动回填（只补空，
    # 不覆盖台账与人工编辑；新建项目在此刻已落库，可与存量一并处理）
    owner = backfill_owner_fields(
        db,
        operated_by=operated_by,
        _prelocked_states=locked_states,
        _no_bump_project_ids=created_project_ids,
    )
    # 同步仓配候选投影（与 assign_source_orders 一致）
    if assigned_orders:
        from app.services import maintenance_warehouse

        maintenance_warehouse.reconcile_project_assignment_links(
            db,
            operated_by=operated_by,
            reason=clean_reason,
            source_order_ids=set(),
        )
    return {
        "assigned_orders": assigned_orders,
        "matched_projects": len(matched_projects),
        "created_projects": created_projects,
        "skipped_groups": skipped_groups,
        "skipped_ambiguous": len(ambiguous_groups),
        **owner,
    }


def _create_auto_project(
    db: Session,
    *,
    display_name: str,
    period_from,
    period_to,
    lifecycle: str,
) -> MaintenanceProject:
    """自动创建项目主档；名称锁已由调用方持有，后来出现项目即 OCC 冲突。"""
    existing = db.scalar(
        select(MaintenanceProject).where(
            MaintenanceProject.display_name == display_name,
            MaintenanceProject.is_active.is_(True),
        )
    )
    if existing is not None:
        raise SourceAssignmentConflict("目标项目已变化，请刷新后重试")
    prefix = "AUTO-"
    max_seq = db.scalar(
        select(func.max(MaintenanceProject.project_code))
        .where(MaintenanceProject.project_code.like(f"{prefix}%"))
    )
    next_seq = 1
    if max_seq:
        try:
            next_seq = int(str(max_seq).removeprefix(prefix)) + 1
        except ValueError:
            next_seq = 1
    project = MaintenanceProject(
        project_id=str(uuid4()),
        project_code=f"{prefix}{next_seq:05d}",
        display_name=display_name,
        period_from=period_from,
        period_to=period_to,
        lifecycle_status=lifecycle,
        is_active=True,
        version=1,
    )
    db.add(project)
    try:
        db.flush()
    except IntegrityError as exc:
        # project_code 仍受 DB 唯一约束保护；任何并发身份变化都作为整批
        # 可重试冲突暴露，禁止把外部项目误标为 created_here。
        raise SourceAssignmentConflict("自动项目创建发生并发变化，请刷新后重试") from exc
    return project
