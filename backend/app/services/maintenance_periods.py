"""维保期限唯一事实源写入 helper（K3 双源 P1 修复）。

``MaintenanceProject.period_from``/``period_to`` 是唯一业务事实；
``MaintenanceServicePeriod`` 只是 manager workbook OCC/provenance 兼容投影，
不再参与任何业务展示、筛选或任务规则计算。

锁序约定：调用方必须先按 state → project 顺序持锁（见
``maintenance_project_operations.lock_workbook_states`` 与 catalog 的
``_lock_project_for_master_write``），本模块只锁/写 projection 行，
绝不 bump ``MaintenanceProject.version`` 或 workbook state revision
（每事务一次由调用方负责）。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import and_, case, or_, select
from sqlalchemy.orm import Session

from app.models.maintenance_manager import MaintenanceServicePeriod
from app.models.maintenance_project import MaintenanceProject

SOURCE_DIRECT_API = "direct_api"
SOURCE_MANAGER_WORKBOOK = "manager_workbook_v3"
SOURCE_LEDGER = "project_manager_xls_v1"
_SOURCES = {SOURCE_DIRECT_API, SOURCE_MANAGER_WORKBOOK, SOURCE_LEDGER}


class MaintenancePeriodError(ValueError):
    """维保期限写入违反业务约束（如起止倒置、来源非法）。"""


def completeness_state(period_from: date | None, period_to: date | None) -> str:
    """四态完整性：complete / start_only / end_only / empty。"""
    if period_from is not None and period_to is not None:
        return "complete"
    if period_from is not None:
        return "start_only"
    if period_to is not None:
        return "end_only"
    return "empty"


def lifecycle_status(
    period_from: date | None,
    period_to: date | None,
    as_of: date,
) -> str:
    """期限生命周期：missing / ended / ongoing。

    口径（2026-09-03 负责人拍板）：

    * ``missing`` = **期限数据不完整**——起止任一为空。单边期限仍要被筛出来
      提醒补齐，因此不算 ongoing。
    * ``ended``   = 期限完整且终止日已过。
    * ``ongoing`` = 其余（**含尚未开始的未来项目**）。

    修的是这个假标签：此前 ``period_from > as_of`` 的未开始项目三个分支都不
    命中、掉进兜底判 missing——期限数据明明是完整的，却被当成"期限缺失"，
    回填期限后卡片墙照旧挂假标签（广州农商银行 2026-09-05～2028-09-04 等）。
    """
    if period_from is None or period_to is None:
        return "missing"
    if period_to < as_of:
        return "ended"
    return "ongoing"


def lifecycle_case(period_from_column, period_to_column, *, as_of: date):
    """SQL 版期限生命周期，必须与 :func:`lifecycle_status` 保持同一语义。

    ``MaintenanceProject.lifecycle_status`` 只保留为写入时兼容快照；集合查询若
    直接按该列筛选，业务日跨过 ``period_to`` 后会一直停留在旧状态，直到某次
    无关写入碰巧刷新它。读侧应使用本表达式，因而不依赖日切写任务。
    """
    return case(
        (
            or_(
                period_from_column.is_(None),
                period_to_column.is_(None),
            ),
            "missing",
        ),
        (period_to_column < as_of, "ended"),
        else_="ongoing",
    )


def completeness_case(period_from_column, period_to_column):
    """SQL 版四态完整性（供目录/提醒等集合查询直接基于 project 期限列计算）。"""
    return case(
        (
            and_(period_from_column.is_not(None), period_to_column.is_not(None)),
            "complete",
        ),
        (period_from_column.is_not(None), "start_only"),
        (period_to_column.is_not(None), "end_only"),
        else_="empty",
    )


def _period_dict(
    period_from: date | None,
    period_to: date | None,
    lifecycle: str,
) -> dict:
    return {
        "period_from": period_from.isoformat() if period_from else None,
        "period_to": period_to.isoformat() if period_to else None,
        "lifecycle_status": lifecycle,
    }


def _projection_dict(period: MaintenanceServicePeriod | None) -> dict | None:
    if period is None:
        return None
    return {
        "service_start": period.service_start.isoformat() if period.service_start else None,
        "service_end": period.service_end.isoformat() if period.service_end else None,
        "completeness_state": period.completeness_state,
        "source": period.source,
        "source_batch_id": period.source_batch_id,
        "ledger_batch_id": period.ledger_batch_id,
        "version": period.version,
    }


def apply_canonical_period_locked(
    db: Session,
    *,
    project: MaintenanceProject,
    period_from: date | None,
    period_to: date | None,
    source: str,
    source_batch_id: str | None = None,
    ledger_batch_id: str | None = None,
    as_of: date,
    operated_by: str,
    reason: str,
) -> dict:
    """以 project 期限为准同步 project 与 projection（调用方已持 state→project 锁）。

    - 起止倒置直接拒绝（``MaintenancePeriodError``）。
    - project.period_from/period_to 与 lifecycle_status 按快照口径重算。
    - provenance 互斥由本函数强制：manager_workbook_v3 清 ledger_batch_id，
      project_manager_xls_v1 清 source_batch_id，direct_api 两者清空。
    - 仅日期/完整性等语义变化才 bump projection.version；
      provenance-only 与完全 no-op 不增版本。
    - 不 bump project.version / workbook revision、不写审计——调用方按事务
      合并一次完成；before/after 在返回值中给出。
    """
    if period_from is not None and period_to is not None and period_from > period_to:
        raise MaintenancePeriodError("维保期限起始日期不能晚于终止日期")
    if source not in _SOURCES:
        raise MaintenancePeriodError(f"未知维保期限来源「{source}」")
    if source == SOURCE_MANAGER_WORKBOOK and not source_batch_id:
        raise MaintenancePeriodError("manager_workbook_v3 来源必须携带上传批次")
    if source == SOURCE_LEDGER and not ledger_batch_id:
        raise MaintenancePeriodError("project_manager_xls_v1 来源必须携带台账批次")

    # provenance 互斥：跨来源覆盖时另一条批次引用必须清空（CHECK 约束）。
    new_source_batch_id = source_batch_id if source == SOURCE_MANAGER_WORKBOOK else None
    new_ledger_batch_id = ledger_batch_id if source == SOURCE_LEDGER else None

    before = {
        "project": _period_dict(
            project.period_from, project.period_to, project.lifecycle_status
        ),
        "projection": None,
    }

    new_lifecycle = lifecycle_status(period_from, period_to, as_of)
    project_changed = (
        project.period_from != period_from
        or project.period_to != period_to
        or project.lifecycle_status != new_lifecycle
    )
    if project_changed:
        project.period_from = period_from
        project.period_to = period_to
        project.lifecycle_status = new_lifecycle

    period = db.scalar(
        select(MaintenanceServicePeriod)
        .where(MaintenanceServicePeriod.project_id == project.project_id)
        .with_for_update()
    )
    new_completeness = completeness_state(period_from, period_to)
    projection_changed = False
    if period is None:
        period = MaintenanceServicePeriod(
            project_id=project.project_id,
            service_start=period_from,
            service_end=period_to,
            completeness_state=new_completeness,
            source=source,
            source_batch_id=new_source_batch_id,
            ledger_batch_id=new_ledger_batch_id,
            version=1,
        )
        db.add(period)
        projection_changed = True
    else:
        before["projection"] = _projection_dict(period)
        semantic_changed = (
            period.service_start != period_from
            or period.service_end != period_to
            or period.completeness_state != new_completeness
        )
        provenance_changed = (
            period.source != source
            or period.source_batch_id != new_source_batch_id
            or period.ledger_batch_id != new_ledger_batch_id
        )
        if semantic_changed:
            period.service_start = period_from
            period.service_end = period_to
            period.completeness_state = new_completeness
            period.version += 1
        if semantic_changed or provenance_changed:
            period.source = source
            period.source_batch_id = new_source_batch_id
            period.ledger_batch_id = new_ledger_batch_id
        projection_changed = semantic_changed or provenance_changed

    after = {
        "project": _period_dict(period_from, period_to, new_lifecycle),
        "projection": _projection_dict(period),
    }
    # 项目 Session 为 autoflush=False：helper 负责把本函数写入 flush 落库，
    # 使返回值与后续读取一致；project.version/审计/revision 仍由调用方合并。
    db.flush()
    return {
        "project_changed": project_changed,
        "projection_changed": projection_changed,
        "before": before,
        "after": after,
        "period": period,
    }


def classify_period_divergence(
    db: Session,
    *, project_ids: list[str] | None = None,
) -> list[dict]:
    """只读分类存量 project 事实与 projection 的漂移（dry-run，不写任何行）。

    用于评估历史双源冲突，绝不自动修复：
    - ``missing_projection``：project 有期限但投影行缺失；
    - ``diverged``：两侧都有值但日期不一致（以 project 为准）。
    """
    statement = (
        select(
            MaintenanceProject.project_id,
            MaintenanceProject.project_code,
            MaintenanceProject.period_from,
            MaintenanceProject.period_to,
            MaintenanceServicePeriod.service_start,
            MaintenanceServicePeriod.service_end,
            MaintenanceServicePeriod.source,
            MaintenanceServicePeriod.version,
        )
        .outerjoin(
            MaintenanceServicePeriod,
            MaintenanceServicePeriod.project_id == MaintenanceProject.project_id,
        )
        .order_by(MaintenanceProject.project_code, MaintenanceProject.project_id)
    )
    if project_ids is not None:
        statement = statement.where(MaintenanceProject.project_id.in_(project_ids))
    rows: list[dict] = []
    for row in db.execute(statement):
        (
            project_id,
            project_code,
            period_from,
            period_to,
            service_start,
            service_end,
            source,
            version,
        ) = row
        if service_start is None and service_end is None and source is None:
            if period_from is None and period_to is None:
                continue
            category = "missing_projection"
        elif service_start == period_from and service_end == period_to:
            continue
        else:
            category = "diverged"
        rows.append(
            {
                "project_id": project_id,
                "project_code": project_code,
                "category": category,
                "project_period_from": period_from.isoformat() if period_from else None,
                "project_period_to": period_to.isoformat() if period_to else None,
                "projection_service_start": (
                    service_start.isoformat() if service_start else None
                ),
                "projection_service_end": (
                    service_end.isoformat() if service_end else None
                ),
                "projection_source": source,
                "projection_version": version,
            }
        )
    return rows
