"""维保展示板聚合服务（plan v1.3 M3-1/M3-4/M3-5）。

首屏五段：来源健康 → 本期变化 → 需关注事项 → 全项目分页列表 → 单据/PN 证据下钻。

两条硬约束：
1. **六态信封**（§4.6）：restricted / not_imported / partial / ready / stale / error。
   not_imported / restricted / error 一律不带 value（前端绝不渲染 0，铁律 5）；
   restricted 与 ready 的键集合完全一致（防「字段存在性」侧信道）。
2. **状态列白名单**（铁律 3）：聚合只允许引用 AGGREGATE_SOURCE_COLUMNS 中的列；
   28 个流转状态列禁止进入任何聚合表达式，由单测锁死交集为空。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.etl import mapping
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.security import UserContext
from app.services import (
    maintenance_boss_facts,
    maintenance_cost_quality,
    maintenance_source_health,
    project_names,
)

# 未归属桶的伪项目 ID（§4.5）：与真实 project_id 不可能冲突
UNASSIGNED_BUCKET = "unassigned"

# 铁律 3 白名单：聚合表达式只允许引用这些事实列。
# 需求侧只认 qty/return_qty 与成本回填列；三源事实来自 boss_facts（各自源表）。
AGGREGATE_SOURCE_COLUMNS: frozenset[str] = frozenset({
    # f_maintenance_order
    "raw_order_id", "order_no", "order_date", "project_std", "project_raw",
    "data_status",
    # f_maintenance_line：数量事实
    "qty", "return_qty",
    # f_maintenance_line：成本回填列（recompute 独占写）
    "cost_amount_inc_tax", "cost_source", "cost_tax_basis", "confidence",
    "anomaly_flags",
})
# 流转状态列（只展示，永不进聚合）——由 mapping 的明细展示列取前 14 项定义域
STATUS_ONLY_COLUMNS: frozenset[str] = frozenset(
    mapping.MAINTENANCE_LINE_DISPLAY_FIELDS
) | frozenset({
    # 头级自报四列同样只展示（M4-4 无判定并排）
    "head_demand_qty", "head_purchase_qty", "head_shipped_qty", "head_returned_qty",
})


# ---------------------------------------------------------------- 信封

def ready(value, *, as_of: date | None = None) -> dict:
    return {"state": "ready", "value": value,
            "as_of": as_of.isoformat() if as_of else None}


def restricted() -> dict:
    """权限不可见：无 value、无 as_of，但键集合与 ready 一致（无侧信道）。"""
    return {"state": "restricted", "value": None, "as_of": None}


def not_imported() -> dict:
    return {"state": "not_imported", "value": None, "as_of": None}


def partial(value, *, as_of: date | None = None, unlinked: int | None = None) -> dict:
    out = ready(value, as_of=as_of)
    out["state"] = "partial"
    out["unlinked"] = unlinked
    return out


def error() -> dict:
    return {"state": "error", "value": None, "as_of": None}


def can_view_cost(user_ctx: UserContext) -> bool:
    from app import permissions as perm

    perms = user_ctx.permissions
    if perms is None:
        perms = perm.template_for(user_ctx.role)
    return bool(perm.runtime_safe(perms).get("data_purchase_cost", False))


# ---------------------------------------------------------------- 时间窗

def resolve_window(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    """默认当年 1-1 至今；字段名 orders_ytd/lines_ytd **不写死年份**。"""
    today = business_today()
    return (date_from or date(today.year, 1, 1), date_to or today)


def previous_window(window: tuple[date, date]) -> tuple[date, date]:
    """环比基期：等长窗口紧邻前移（[start-span, start-1天]）。"""
    start, end = window
    span = end - start
    prev_end = start - timedelta(days=1)
    return (prev_end - span, prev_end)


# ---------------------------------------------------------------- 成本五件套

def _cost_bundle(db: Session, *, window: tuple[date, date],
                 project_id: str | None = None,
                 unassigned_only: bool = False,
                 can_cost: bool,
                 allowed_project_ids: set[str] | None = None) -> dict:
    """「已知申请估算成本（含税）」actual/estimated/missing/coverage/quality（§4.3）。

    口径与 services/maintenance_cost_quality 完全一致；缺价不按 0——missing_lines
    单列、quality=incomplete（前端显示「不完整/已知下限」）。
    """
    if not can_cost:
        return restricted()
    start, end = window
    stmt = (select(*_cost_columns())
            .select_from(FMaintenanceLine)
            .join(FMaintenanceOrder,
                  FMaintenanceOrder.id == FMaintenanceLine.order_id)
            .where(FMaintenanceOrder.order_date >= start,
                   FMaintenanceOrder.order_date <= end))
    stmt = _scope_stmt(stmt, project_id=project_id,
                       unassigned_only=unassigned_only,
                       allowed_project_ids=allowed_project_ids)
    return _bundle_from_row(*db.execute(stmt).one())


def _cost_columns():
    """成本五件套的聚合列（复用于全局/逐项目分组查询，口径单一）。"""
    return (
        func.coalesce(func.sum(case(
            (FMaintenanceLine.cost_source.in_(
                tuple(maintenance_cost_quality.ACTUAL_SOURCES)),
             FMaintenanceLine.cost_amount_inc_tax), else_=0)), 0),
        func.coalesce(func.sum(case(
            (FMaintenanceLine.cost_source.in_(
                tuple(maintenance_cost_quality.ESTIMATED_SOURCES)),
             FMaintenanceLine.cost_amount_inc_tax), else_=0)), 0),
        func.count(case((FMaintenanceLine.cost_source.in_(
            tuple(maintenance_cost_quality.ACTUAL_SOURCES)), 1))),
        func.count(case((FMaintenanceLine.cost_source.in_(
            tuple(maintenance_cost_quality.ESTIMATED_SOURCES)), 1))),
        func.count(FMaintenanceLine.id),
    )


def _bundle_from_row(actual, estimated, actual_lines, estimated_lines,
                     total_lines) -> dict:
    known = (actual or 0) + (estimated or 0)
    missing_lines = int(total_lines) - int(actual_lines) - int(estimated_lines)
    coverage = (round((int(actual_lines) + int(estimated_lines))
                      / int(total_lines) * 100, 1) if total_lines else None)
    if missing_lines or not total_lines:
        quality = "incomplete"
    elif estimated_lines:
        quality = "contains_estimate"
    else:
        quality = "actual_only"
    return {
        "state": "ready",
        "value": {
            "actual_amount": actual, "estimated_amount": estimated,
            "known_amount": known, "missing_lines": missing_lines,
            "coverage_pct": coverage, "quality": quality,
        },
        "as_of": None,
    }


def _cost_bundles_by_project(db: Session, *, window: tuple[date, date],
                             project_ids: list[str], can_cost: bool) -> dict:
    """本页全部项目的成本五件套：**一次分组查询**（M3-4 禁 N+1）。"""
    if not can_cost:
        return {pid: restricted() for pid in project_ids}
    if not project_ids:
        return {}
    start, end = window
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    rows = db.execute(
        select(MaintenanceSourceOrderAssignment.project_id, *_cost_columns())
        .select_from(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .join(MaintenanceSourceOrderAssignment, active)
        .where(MaintenanceSourceOrderAssignment.project_id.in_(project_ids),
               FMaintenanceOrder.order_date >= start,
               FMaintenanceOrder.order_date <= end)
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    ).all()
    found = {row[0]: _bundle_from_row(*row[1:]) for row in rows}
    # 本页无成本行的项目也要给出恒定形状（0 行 → incomplete，不是缺字段）
    empty = _bundle_from_row(0, 0, 0, 0, 0)
    return {pid: found.get(pid, empty) for pid in project_ids}


def _order_cost_bundles(db: Session, order_ids: list[int], *,
                        can_cost: bool) -> dict:
    """本页全部单据的成本五件套与行数：**一次分组查询**。"""
    if not order_ids:
        return {}
    if not can_cost:
        counts = db.execute(
            select(FMaintenanceLine.order_id, func.count(FMaintenanceLine.id))
            .where(FMaintenanceLine.order_id.in_(order_ids))
            .group_by(FMaintenanceLine.order_id)
        ).all()
        line_counts = {oid: int(n) for oid, n in counts}
        return {oid: (restricted(), line_counts.get(oid, 0)) for oid in order_ids}
    rows = db.execute(
        select(FMaintenanceLine.order_id, *_cost_columns())
        .where(FMaintenanceLine.order_id.in_(order_ids))
        .group_by(FMaintenanceLine.order_id)
    ).all()
    found = {row[0]: (_bundle_from_row(*row[1:]), int(row[5])) for row in rows}
    empty = (_bundle_from_row(0, 0, 0, 0, 0), 0)
    return {oid: found.get(oid, empty) for oid in order_ids}


def _scope_stmt(stmt, *, project_id: str | None, unassigned_only: bool,
                allowed_project_ids: set[str] | None = None):
    """按项目归属收敛语句：项目桶 / 未归属桶 / 全局。"""
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    if unassigned_only:
        return stmt.outerjoin(MaintenanceSourceOrderAssignment, active).where(
            MaintenanceSourceOrderAssignment.assignment_id.is_(None))
    if project_id:
        return stmt.join(MaintenanceSourceOrderAssignment, active).where(
            MaintenanceSourceOrderAssignment.project_id == project_id)
    if allowed_project_ids is not None:
        return stmt.join(MaintenanceSourceOrderAssignment, active).where(
            MaintenanceSourceOrderAssignment.project_id.in_(
                allowed_project_ids or {""}))
    return stmt


# ---------------------------------------------------------------- 首屏

def health(db: Session) -> dict:
    return maintenance_source_health.source_health(db)


def _window_counts(db: Session, window: tuple[date, date], *,
                   project_id: str | None = None,
                   unassigned_only: bool = False,
                   allowed_project_ids: set[str] | None = None) -> tuple[int, int]:
    start, end = window
    orders_stmt = select(func.count(func.distinct(FMaintenanceOrder.id))).where(
        FMaintenanceOrder.order_date >= start, FMaintenanceOrder.order_date <= end)
    orders_stmt = _scope_stmt(orders_stmt, project_id=project_id,
                              unassigned_only=unassigned_only,
                              allowed_project_ids=allowed_project_ids)
    lines_stmt = (select(func.count(FMaintenanceLine.id))
                  .select_from(FMaintenanceLine)
                  .join(FMaintenanceOrder,
                        FMaintenanceOrder.id == FMaintenanceLine.order_id)
                  .where(FMaintenanceOrder.order_date >= start,
                         FMaintenanceOrder.order_date <= end))
    lines_stmt = _scope_stmt(lines_stmt, project_id=project_id,
                             unassigned_only=unassigned_only,
                             allowed_project_ids=allowed_project_ids)
    return int(db.execute(orders_stmt).scalar_one()), int(
        db.execute(lines_stmt).scalar_one())


def wbdd_imported(db: Session) -> bool:
    """WBDD 源是否已导入。

    未导入时首屏三个指标槽必须显示「尚未导入」而不是 0——「本期需求单 0」会被
    老板读成「本期没人提申请」，与「数据没传」是两件完全不同的事（铁律 5）。
    """
    return (maintenance_source_health.source_health(db)["sources"]["wbdd"]
            ["readiness"] != "not_imported")


def summary(db: Session, *, user_ctx: UserContext,
            date_from: date | None = None, date_to: date | None = None,
            allowed_project_ids: set[str] | None = None) -> dict:
    """本期变化：orders_ytd / lines_ytd / 成本五件套 + 环比基期（§4.4）。

    allowed_project_ids 非空 = 本人范围账号：全部计数与成本必须收敛到该范围
    （§6.2「经理 200（范围聚合）」），否则经理会拿到全公司口径，且与 /projects
    的范围不一致导致恒等式必然对不上。
    """
    window = resolve_window(date_from, date_to)
    prev = previous_window(window)
    can_cost = can_view_cost(user_ctx)
    if not wbdd_imported(db):
        empty = {
            "orders_ytd": not_imported(),
            "lines_ytd": not_imported(),
            "known_apply_cost_inc_tax": (
                restricted() if not can_cost else not_imported()),
        }
        return {
            "window": {"from": window[0].isoformat(), "to": window[1].isoformat()},
            **empty,
            "prev_window": {
                "window": {"from": prev[0].isoformat(), "to": prev[1].isoformat()},
                **empty,
            },
        }
    orders, lines = _window_counts(db, window, allowed_project_ids=allowed_project_ids)
    prev_orders, prev_lines = _window_counts(db, prev,
                                             allowed_project_ids=allowed_project_ids)
    return {
        "window": {"from": window[0].isoformat(), "to": window[1].isoformat()},
        "orders_ytd": ready(orders),
        "lines_ytd": ready(lines),
        "known_apply_cost_inc_tax": _cost_bundle(
            db, window=window, can_cost=can_cost,
            allowed_project_ids=allowed_project_ids),
        "prev_window": {
            "window": {"from": prev[0].isoformat(), "to": prev[1].isoformat()},
            "orders_ytd": ready(prev_orders),
            "lines_ytd": ready(prev_lines),
            "known_apply_cost_inc_tax": _cost_bundle(
                db, window=prev, can_cost=can_cost,
                allowed_project_ids=allowed_project_ids),
        },
    }


# 需关注事项 kind 注册表（M3-6）：M0-A 拍板前只搭框架，不预置内容。
ATTENTION_KINDS: tuple[str, ...] = ()


def attention(db: Session, *, user_ctx: UserContext, limit: int = 10) -> dict:
    """需关注队列 ≤10 条。M0-A 未拍板 → 空队列 + 明示待确认（不自行编造决策口径）。"""
    return {
        "items": [],
        "registered_kinds": list(ATTENTION_KINDS),
        "pending_decision": "M0-A",
    }


# ---------------------------------------------------------------- 项目列表

_SORTS = {"attention", "orders", "name", "known_cost"}


class BoardSortNotPermitted(Exception):
    """成本相关排序需要成本数据权限（不静默降级——降级会通过顺序泄露排名）。"""


def projects(db: Session, *, user_ctx: UserContext, page: int = 1,
             page_size: int = 20, lifecycle: str = "all",
             sort: str = "name", q_text: str | None = None,
             has_activity: bool | None = None,
             date_from: date | None = None,
             date_to: date | None = None,
             allowed_project_ids: set[str] | None = None) -> dict:
    """全项目分页列表 + 未归属桶（§4.5）。

    项目集合口径：全量项目（不按窗口过滤）＋未归属桶；窗口只影响 has_activity_in_window
    与本期计数。allowed_project_ids 非空时收敛到该范围（M0-B「本人项目」案）。
    """
    if sort not in _SORTS:
        sort = "name"
    can_cost = can_view_cost(user_ctx)
    if sort == "known_cost" and not can_cost:
        raise BoardSortNotPermitted()
    window = resolve_window(date_from, date_to)

    # 归档项目（is_active=False）**仍带着单**时必须留在列表里：它既不在项目行、
    # 也进不了未归属桶（归属还是活跃的），一旦滤掉，那些单就从看板上凭空消失，
    # §6.2 的母集恒等式跟着不成立——老板看到的总数会因为有人归档了一个项目而
    # 无声变小。已经空掉的归档项目照旧隐藏，不给列表添乱。
    carries_orders = (
        select(1)
        .select_from(MaintenanceSourceOrderAssignment)
        .where(MaintenanceSourceOrderAssignment.project_id
               == MaintenanceProject.project_id,
               MaintenanceSourceOrderAssignment.is_active.is_(True))
        .exists()
    )
    filters = [or_(MaintenanceProject.is_active.is_(True), carries_orders)]
    if lifecycle in ("ongoing", "ended", "missing"):
        filters.append(MaintenanceProject.lifecycle_status == lifecycle)
    if allowed_project_ids is not None:
        filters.append(MaintenanceProject.project_id.in_(allowed_project_ids or {""}))
    if q_text:
        needle = q_text.strip()
        filters.append(or_(
            MaintenanceProject.project_code.icontains(needle, autoescape=True),
            MaintenanceProject.display_name.icontains(needle, autoescape=True),
        ))

    # 窗口内的每项目计数/成本子查询：既用于 has_activity 过滤，也用于真实排序。
    # 不这么做的话 sort/has_activity 会变成「接收但静默忽略」的假参数——调用方
    # 以为已排序/已筛选，实际拿到的是 project_code 字典序全集。
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    win_start, win_end = window
    window_stats = (
        select(
            MaintenanceSourceOrderAssignment.project_id.label("project_id"),
            func.count(func.distinct(FMaintenanceOrder.id)).label("orders_n"),
            func.coalesce(func.sum(
                FMaintenanceLine.cost_amount_inc_tax), 0).label("known_cost"),
        )
        .select_from(FMaintenanceOrder)
        .join(MaintenanceSourceOrderAssignment, active)
        .outerjoin(FMaintenanceLine,
                   FMaintenanceLine.order_id == FMaintenanceOrder.id)
        .where(FMaintenanceOrder.order_date >= win_start,
               FMaintenanceOrder.order_date <= win_end)
        .group_by(MaintenanceSourceOrderAssignment.project_id)
        .subquery()
    )
    base = (select(MaintenanceProject)
            .outerjoin(window_stats,
                       window_stats.c.project_id == MaintenanceProject.project_id)
            .where(*filters))
    if has_activity is True:
        base = base.where(func.coalesce(window_stats.c.orders_n, 0) > 0)
    elif has_activity is False:
        base = base.where(func.coalesce(window_stats.c.orders_n, 0) == 0)

    total = int(db.execute(
        select(func.count()).select_from(base.subquery())).scalar_one())

    if sort == "orders":
        order_by = (func.coalesce(window_stats.c.orders_n, 0).desc(),
                    MaintenanceProject.project_code)
    elif sort == "known_cost":
        order_by = (func.coalesce(window_stats.c.known_cost, 0).desc(),
                    MaintenanceProject.project_code)
    elif sort == "attention":
        # 需关注排序的口径由 M0-A 拍板（plan §2.1）；未拍板前按「本期活动量」代理，
        # 并在响应里如实回显 sort_applied，不假装已按最终口径排序。
        order_by = (func.coalesce(window_stats.c.orders_n, 0).desc(),
                    MaintenanceProject.project_code)
    else:
        order_by = (MaintenanceProject.project_code,)
    rows = db.execute(
        base.order_by(*order_by)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()

    project_ids = [p.project_id for p in rows]
    # 一次性取本页项目的窗口计数、成本五件套与三源事实（M3-4：查询数与页大小无关）
    counts = _project_window_counts(db, window, project_ids)
    # WBDD 未导入时，需求单/明细/成本一律 not_imported——不得用 0 冒充「没有申请」
    wbdd_ready = wbdd_imported(db)
    cost_bundles = _cost_bundles_by_project(
        db, window=window, project_ids=project_ids, can_cost=can_cost)
    fact_totals = (maintenance_boss_facts.project_totals(db, project_ids=project_ids)
                   if project_ids else {})
    pre_delivery = _pre_delivery_counts(db, project_ids)
    source_states = maintenance_source_health.source_health(db)["sources"]

    out_rows = []
    for proj in rows:
        orders_n, lines_n = counts.get(proj.project_id, (0, 0))
        out_rows.append({
            "project_id": proj.project_id,
            "project_code": proj.project_code,
            "display_name": proj.display_name,
            "lifecycle": proj.lifecycle_status,
            # 归档但仍带单：留在列表里保住母集恒等式，用标记让老板知道它已归档
            "is_archived": not proj.is_active,
            "has_activity_in_window": bool(orders_n),
            "pre_delivery_order_count": pre_delivery.get(proj.project_id, 0),
            "orders_ytd": ready(orders_n) if wbdd_ready else not_imported(),
            "lines_ytd": ready(lines_n) if wbdd_ready else not_imported(),
            "known_apply_cost_inc_tax": (
                cost_bundles[proj.project_id] if wbdd_ready
                else (restricted() if not can_cost else not_imported())),
            **_fact_envelopes(fact_totals.get(proj.project_id), source_states),
        })

    # 未归属桶恒为一行（不静默丢单）：仅全范围账号可见（未归属单无「本人」范围）。
    # 搜索/生命周期筛选下不注入——桶不是搜索命中项，混入会污染结果集。
    if (allowed_project_ids is None and page == 1
            and not q_text and lifecycle == "all"):
        u_orders, u_lines = _window_counts(db, window, unassigned_only=True)
        out_rows.insert(0, {
            "project_id": UNASSIGNED_BUCKET,
            "project_code": UNASSIGNED_BUCKET,
            "display_name": "未归属（待人工确认）",
            "lifecycle": "missing",
            "is_archived": False,      # 键集与项目行保持一致
            "has_activity_in_window": bool(u_orders),
            "pre_delivery_order_count": 0,
            "orders_ytd": ready(u_orders) if wbdd_ready else not_imported(),
            "lines_ytd": ready(u_lines) if wbdd_ready else not_imported(),
            "known_apply_cost_inc_tax": (
                _cost_bundle(db, window=window, unassigned_only=True,
                             can_cost=can_cost) if wbdd_ready
                else (restricted() if not can_cost else not_imported())),
            # 未归属单没有项目口径的三源事实（CKD 靠归属才落项目）——系统「无法知道」，
            # 不是「等于 0」。用 not_imported 信封而非 ready(0)（铁律 5）。
            **{k: fact_not_imported() for k in FACT_FIELDS},
        })
    return {"rows": out_rows, "total": total, "page": page,
            "page_size": page_size, "sort": sort,
            # attention 的最终口径待 M0-A；当前按本期活动量代理，如实回显
            "sort_applied": ("orders" if sort == "attention" else sort),
            "sort_pending_decision": ("M0-A" if sort == "attention" else None),
            "window": {"from": window[0].isoformat(), "to": window[1].isoformat()}}


FACT_FIELDS = ("shipped_qty", "returned_good_qty", "returned_bad_qty")


def fact_not_imported() -> dict:
    """三源事实位的 not_imported 信封。

    键集与 `partial` 对齐（带 `unlinked`）：`rows` 是一个同构数组，桶行与项目行
    的同名字段不能一个有 `unlinked` 一个没有，否则按数组统一取数的调用方在桶行
    上拿到 undefined。
    """
    env = not_imported()
    env["unlinked"] = None
    return env


def _fact_envelopes(totals: dict | None, source_states: dict) -> dict:
    """三源事实按各自 readiness 包信封：未导入 → not_imported（绝不 0）。"""
    mapping_ = (("shipped_qty", "ckd", "shipped"),
                ("returned_good_qty", "return_order", "returned_good"),
                ("returned_bad_qty", "rkd_inbound", "returned_bad"))
    out = {}
    for field, source_key, fact_key in mapping_:
        state = source_states[source_key]["readiness"]
        if state == "not_imported":
            out[field] = fact_not_imported()
            continue
        value = (totals or {}).get(fact_key)
        value = value if value is not None else Decimal(0)
        if state == "partial":
            out[field] = partial(value,
                                 unlinked=source_states[source_key]["unlinked_rows"])
        elif state == "stale":
            env = ready(value)
            env["state"] = "stale"
            env["as_of"] = source_states[source_key]["as_of"]
            out[field] = env
        else:
            out[field] = ready(value)
    return out


def _project_window_counts(db: Session, window: tuple[date, date],
                           project_ids: list[str]) -> dict:
    if not project_ids:
        return {}
    start, end = window
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    rows = db.execute(
        select(MaintenanceSourceOrderAssignment.project_id,
               func.count(func.distinct(FMaintenanceOrder.id)),
               func.count(FMaintenanceLine.id))
        .select_from(FMaintenanceOrder)
        .join(MaintenanceSourceOrderAssignment, active)
        .outerjoin(FMaintenanceLine,
                   FMaintenanceLine.order_id == FMaintenanceOrder.id)
        .where(MaintenanceSourceOrderAssignment.project_id.in_(project_ids),
               FMaintenanceOrder.order_date >= start,
               FMaintenanceOrder.order_date <= end)
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    ).all()
    return {pid: (int(o), int(l)) for pid, o, l in rows}


def _pre_delivery_counts(db: Session, project_ids: list[str]) -> dict:
    """预交付单计数（方案 B 徽标）：project_raw 带前缀的已归属单。"""
    if not project_ids:
        return {}
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    rows = db.execute(
        select(MaintenanceSourceOrderAssignment.project_id,
               func.count(FMaintenanceOrder.id))
        .select_from(FMaintenanceOrder)
        .join(MaintenanceSourceOrderAssignment, active)
        .where(MaintenanceSourceOrderAssignment.project_id.in_(project_ids),
               FMaintenanceOrder.project_raw.op("~")(r"^预交付[-—－–]"))
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    ).all()
    return {pid: int(n) for pid, n in rows}


# ---------------------------------------------------------------- 下钻

def project_orders(db: Session, *, user_ctx: UserContext, project_id: str,
                   page: int = 1, page_size: int = 20) -> dict:
    """单据下钻（project_id 可为 unassigned 伪桶）。"""
    can_cost = can_view_cost(user_ctx)
    unassigned = project_id == UNASSIGNED_BUCKET
    base = select(FMaintenanceOrder)
    base = _scope_stmt(base, project_id=None if unassigned else project_id,
                       unassigned_only=unassigned)
    total = int(db.execute(
        select(func.count()).select_from(base.subquery())).scalar_one())
    rows = db.execute(
        base.order_by(FMaintenanceOrder.order_date.desc().nullslast(),
                      FMaintenanceOrder.order_no)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    source_states = maintenance_source_health.source_health(db)["sources"]
    fact_totals = ({} if unassigned
                   else maintenance_boss_facts.project_totals(
                       db, project_ids=[project_id]))
    # 单据成本与行数一次分组查出（M3-4：禁逐单 N+1）
    order_bundles = _order_cost_bundles(db, [o.id for o in rows], can_cost=can_cost)
    out = []
    for order in rows:
        bundle, line_count = order_bundles[order.id]
        out.append({
            "source_order_id": order.raw_order_id,
            "order_no": order.order_no,
            "order_date": order.order_date.isoformat() if order.order_date else None,
            "data_status": order.data_status,          # 原样展示（铁律 3）
            "project_raw": order.project_raw,
            "is_pre_delivery": project_names.is_pre_delivery(order.project_raw),
            "line_count": line_count,
            "known_apply_cost_inc_tax": bundle,
            # 自报四列原样返回；与事实**无判定并排**（M4-4，不产出 mismatch）
            "self_report": {
                "head_demand_qty": order.head_demand_qty,
                "head_purchase_qty": order.head_purchase_qty,
                "head_shipped_qty": order.head_shipped_qty,
                "head_returned_qty": order.head_returned_qty,
            },
            # facts 是**项目级**卷积（M0-D 粒度下 CKD/RKD 无单据行级键，无法分摊到
            # 单张需求单）。必须显式标注口径，否则与单据级自报列并排会被读成
            # 「这张单发了 800 件」；未归属桶没有项目口径事实，返回 not_imported。
            "facts": (_fact_envelopes(fact_totals.get(project_id), source_states)
                      if not unassigned
                      else {k: fact_not_imported() for k in FACT_FIELDS}),
            "facts_scope": None if unassigned else "project",
        })
    return {"rows": out, "total": total, "page": page, "page_size": page_size}


def order_project_id(db: Session, *, source_order_id: str) -> str | None:
    """单据当前的活跃归属项目；未归属返回 None（供 API 层做范围校验）。"""
    return db.execute(
        select(MaintenanceSourceOrderAssignment.project_id)
        .join(FMaintenanceOrder,
              FMaintenanceOrder.raw_order_id
              == MaintenanceSourceOrderAssignment.source_order_id)
        .where(FMaintenanceOrder.raw_order_id == source_order_id,
               MaintenanceSourceOrderAssignment.is_active.is_(True))
    ).scalar_one_or_none()


def order_lines(db: Session, *, user_ctx: UserContext, source_order_id: str,
                page: int = 1, page_size: int = 20) -> dict:
    """PN 证据行：流转状态列原样，成本列按权限包信封。"""
    can_cost = can_view_cost(user_ctx)
    order = db.execute(
        select(FMaintenanceOrder)
        .where(FMaintenanceOrder.raw_order_id == source_order_id)
    ).scalar_one_or_none()
    if order is None:
        return {"rows": [], "total": 0, "page": page, "page_size": page_size}
    total = int(db.execute(
        select(func.count(FMaintenanceLine.id))
        .where(FMaintenanceLine.order_id == order.id)).scalar_one())
    rows = db.execute(
        select(FMaintenanceLine).where(FMaintenanceLine.order_id == order.id)
        .order_by(FMaintenanceLine.line_no, FMaintenanceLine.raw_line_id)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    pools = maintenance_boss_facts.pool_membership(
        db, {ln.pn_std for ln in rows if ln.pn_std})
    # 认不出型号时不断言「不在池」（铁律 5）：in_pool=None 表示无法判断
    no_pool = {"in_pool": False, "pool_name": None, "pool_status": None}
    unknown_pool = {"in_pool": None, "pool_name": None, "pool_status": None}
    out = []
    for ln in rows:
        out.append({
            "raw_line_id": ln.raw_line_id,
            "pn_std": ln.pn_std, "pn_raw": ln.pn_raw,
            # 归档池在前端是黄色警示（plan §4.5）
            "pool": (pools.get(ln.pn_std, dict(no_pool)) if ln.pn_std
                     else dict(unknown_pool)),
            "description": ln.description,
            "qty": ln.qty, "return_qty": ln.return_qty,
            # 14 个流转状态列原样（铁律 3：不计算、不标注）
            "purchase_qty": ln.purchase_qty,
            "purchased_qty": ln.purchased_qty,
            "pending_purchase_qty": ln.pending_purchase_qty,
            "direct_ship_qty": ln.direct_ship_qty,
            "warehouse_need_qty": ln.warehouse_need_qty,
            "warehouse_shipped_qty": ln.warehouse_shipped_qty,
            "supplied_qty": ln.supplied_qty,
            "pending_supply_qty": ln.pending_supply_qty,
            "returned_qty": ln.returned_qty,
            "pending_return_qty": ln.pending_return_qty,
            "consumed_qty": ln.consumed_qty,
            "demand_pending_return_qty": ln.demand_pending_return_qty,
            "change_warehouse_purchase_qty": ln.change_warehouse_purchase_qty,
            "return_old_part": ln.return_old_part,
            "serial_numbers": ln.serial_numbers,
            # 成本与取价来源同属成本数据组（无权限时整体 restricted，无侧信道）
            "known_apply_cost_inc_tax": (
                ready(ln.cost_amount_inc_tax) if can_cost else restricted()),
            "cost_source": (ready(ln.cost_source) if can_cost else restricted()),
            "confidence": (ready(ln.confidence) if can_cost else restricted()),
        })
    return {"rows": out, "total": total, "page": page, "page_size": page_size}
