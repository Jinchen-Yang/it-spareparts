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

from sqlalchemy import and_, case, func, or_, select, union_all
from sqlalchemy.orm import Session

from app import tax_policy
from app.business_time import business_today
from app.etl import mapping
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceManualCostOverride,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.security import UserContext, is_field_hidden
from app.services import (
    maintenance_boss_facts,
    maintenance_cost_quality,
    maintenance_periods,
    maintenance_project_identity,
    maintenance_source_health,
    project_names,
)
from app.services.query_filters import active_orders

# 未归属桶的伪项目 ID（§4.5）：与真实 project_id 不可能冲突
UNASSIGNED_BUCKET = "unassigned"

# 铁律 3 白名单：聚合表达式只允许引用这些事实列。
# 需求侧只认 qty/return_qty 与成本回填列；三源事实来自 boss_facts（各自源表）。
AGGREGATE_SOURCE_COLUMNS: frozenset[str] = frozenset({
    # f_maintenance_order
    "raw_order_id", "order_no", "order_date", "project_std", "project_raw",
    "data_status",
    # salesperson（2026-08-21 客户反馈）：卡片「销售」与负责人回填的身份列，
    # 仅用于分组取众数（非数值聚合，也非流转状态列），与 project_std 同类。
    "salesperson",
    # f_maintenance_line：数量事实
    "qty", "return_qty",
    # f_maintenance_line：成本回填列（recompute 独占写）
    "cost_amount", "cost_amount_inc_tax", "cost_amount_ex_tax", "cost_source",
    "cost_tax_basis", "confidence", "anomaly_flags",
}) | frozenset({"warehouse_shipped_qty", "direct_ship_qty"})
# 「维保备件采购数」的两列（REQUIREMENTS #41，业务 2026-08-16 明文指定公式
# ＝库房发货＋直采直发）。铁律 3 禁止进聚合的状态列，原文枚举的是
# 「已采/待供/待返/领用」——这两列不在其中，且 #41 显式授权按此聚合，
# 故从 STATUS_ONLY_COLUMNS 里豁免这两列，其余 26 列照旧禁止。
PROCURED_QTY_COLUMNS: frozenset[str] = frozenset({
    "warehouse_shipped_qty", "direct_ship_qty",
})
# 流转状态列（只展示，永不进聚合）——由 mapping 的明细展示列取前 14 项定义域
STATUS_ONLY_COLUMNS: frozenset[str] = (frozenset(
    mapping.MAINTENANCE_LINE_DISPLAY_FIELDS
) | frozenset({
    # 头级自报四列同样只展示（M4-4 无判定并排）
    "head_demand_qty", "head_purchase_qty", "head_shipped_qty", "head_returned_qty",
})) - PROCURED_QTY_COLUMNS


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


def can_view_contract(user_ctx: UserContext) -> bool:
    """合同额/回款/预算/余额归属 data_profit，通过字段级隐藏判定（不按 role）。"""
    return not is_field_hidden(user_ctx, "contract_amount")


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


_FULL_LIFETIME_START = date.min


def _order_date_in_window(window: tuple[date, date]):
    """Date predicate; lifetime mode also keeps undated demand facts visible."""
    start, end = window
    dated = and_(
        FMaintenanceOrder.order_date >= start,
        FMaintenanceOrder.order_date <= end,
    )
    return (
        or_(FMaintenanceOrder.order_date.is_(None), dated)
        if start == _FULL_LIFETIME_START
        else dated
    )


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
    stmt = (select(*_cost_columns())
            .select_from(FMaintenanceLine)
            .join(FMaintenanceOrder,
                  FMaintenanceOrder.id == FMaintenanceLine.order_id)
            .outerjoin(
                MaintenanceManualCostOverride,
                and_(
                    MaintenanceManualCostOverride.line_id == FMaintenanceLine.id,
                    MaintenanceManualCostOverride.active.is_(True),
                ),
            )
            .where(_order_date_in_window(window),
                   FMaintenanceLine.is_active.is_(True)))
    stmt = _scope_stmt(stmt, project_id=project_id,
                       unassigned_only=unassigned_only,
                       allowed_project_ids=allowed_project_ids)
    stmt = active_orders(stmt, FMaintenanceOrder)
    return _bundle_from_row(*db.execute(stmt).one())


def _cost_columns():
    """成本五件套的聚合列（复用于全局/逐项目分组查询，口径单一）。

    2026-08-19：合并人工成本覆盖——主表 cost_source='none' 但存在
    maintenance_manual_cost_override 的行，按 override 含税金额×数量计入
    actual 成本（ACTUAL_SOURCES 含 'manual'）；否则人工回填只影响 03 面板，
    看板成本率会漏算。调用方须 outerjoin override（line_id 唯一，不翻倍）。
    """
    return _cost_columns_for_basis("inc")


def _cost_columns_for_basis(basis: str):
    """Aggregate one normalized tax basis with active-manual/net-qty semantics."""
    normalized_amount = (
        FMaintenanceLine.cost_amount_inc_tax
        if basis == "inc"
        else FMaintenanceLine.cost_amount_ex_tax
    )
    manual_unit = (
        MaintenanceManualCostOverride.unit_cost_inc_tax
        if basis == "inc"
        else MaintenanceManualCostOverride.unit_cost_ex_tax
    )
    amount, actual_known, estimated_known, _missing = (
        maintenance_cost_quality.sql_normalized_line_cost(
            source_column=FMaintenanceLine.cost_source,
            tax_basis_column=FMaintenanceLine.cost_tax_basis,
            legacy_amount_column=FMaintenanceLine.cost_amount,
            normalized_amount_column=normalized_amount,
            normalized_basis=basis,
            anomaly_flags_column=FMaintenanceLine.anomaly_flags,
            qty_column=FMaintenanceLine.qty,
            return_qty_column=FMaintenanceLine.return_qty,
            manual_unit_cost_column=manual_unit,
            manual_active_column=MaintenanceManualCostOverride.active,
        )
    )
    return (
        func.coalesce(func.sum(case((actual_known, amount), else_=0)), 0),
        func.coalesce(func.sum(case(
            (estimated_known, amount), else_=0)), 0),
        func.count(case((actual_known, 1))),
        func.count(case((estimated_known, 1))),
        func.count(FMaintenanceLine.id),
    )


def _normalized_inc_cost_tier_predicates():
    """Boss board 含税成本统一复用 normalized-inc 严格质量真值。"""
    return maintenance_cost_quality.sql_normalized_tax_tier_predicates(
        FMaintenanceLine.cost_source,
        FMaintenanceLine.cost_tax_basis,
        FMaintenanceLine.cost_amount,
        FMaintenanceLine.cost_amount_inc_tax,
        normalized_basis="inc",
        anomaly_flags_column=FMaintenanceLine.anomaly_flags,
    )


def _bundle_from_row(actual, estimated, actual_lines, estimated_lines,
                     total_lines) -> dict:
    line_count = int(total_lines)
    # 没有有效明细时，SUM 的 0 只是 SQL 单位元，不是“真实成本为 0”。这既可能
    # 是空项目，也可能是有 WBDD 单头但明细被作废/未导入；两者都必须不可判定。
    known = None if line_count == 0 else (actual or 0) + (estimated or 0)
    known_lines = int(actual_lines) + int(estimated_lines)
    missing_lines = max(0, line_count - known_lines)
    coverage = (round(known_lines / line_count * 100, 1)
                if line_count else None)
    if line_count == 0 or missing_lines:
        quality = "incomplete"
    elif estimated_lines:
        quality = "contains_estimate"
    else:
        quality = "actual_only"
    return {
        # 有缺价行时数值只是“已知下限”，不是完整 ready。保留 value 让前端
        # 展示已知金额与缺口，但完全缺价时不得再被解释成真实 0。
        "state": "partial" if quality == "incomplete" else "ready",
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
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    statement = (
        select(MaintenanceSourceOrderAssignment.project_id, *_cost_columns())
        .select_from(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .outerjoin(
            MaintenanceManualCostOverride,
            and_(
                MaintenanceManualCostOverride.line_id == FMaintenanceLine.id,
                MaintenanceManualCostOverride.active.is_(True),
            ),
        )
        .join(MaintenanceSourceOrderAssignment, active)
        .where(MaintenanceSourceOrderAssignment.project_id.in_(project_ids),
               _order_date_in_window(window),
               FMaintenanceLine.is_active.is_(True))
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    )
    rows = db.execute(active_orders(statement, FMaintenanceOrder)).all()
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
            .where(FMaintenanceLine.order_id.in_(order_ids),
                   FMaintenanceLine.is_active.is_(True))
            .group_by(FMaintenanceLine.order_id)
        ).all()
        line_counts = {oid: int(n) for oid, n in counts}
        return {oid: (restricted(), line_counts.get(oid, 0)) for oid in order_ids}
    rows = db.execute(
        select(FMaintenanceLine.order_id, *_cost_columns())
        .outerjoin(
            MaintenanceManualCostOverride,
            and_(
                MaintenanceManualCostOverride.line_id == FMaintenanceLine.id,
                MaintenanceManualCostOverride.active.is_(True),
            ),
        )
        .where(FMaintenanceLine.order_id.in_(order_ids),
               FMaintenanceLine.is_active.is_(True))
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
    orders_stmt = select(func.count(func.distinct(FMaintenanceOrder.id))).where(
        _order_date_in_window(window))
    orders_stmt = _scope_stmt(orders_stmt, project_id=project_id,
                              unassigned_only=unassigned_only,
                              allowed_project_ids=allowed_project_ids)
    orders_stmt = active_orders(orders_stmt, FMaintenanceOrder)
    lines_stmt = (select(func.count(FMaintenanceLine.id))
                  .select_from(FMaintenanceLine)
                  .join(FMaintenanceOrder,
                        FMaintenanceOrder.id == FMaintenanceLine.order_id)
                  .where(_order_date_in_window(window),
                         FMaintenanceLine.is_active.is_(True)))
    lines_stmt = _scope_stmt(lines_stmt, project_id=project_id,
                             unassigned_only=unassigned_only,
                             allowed_project_ids=allowed_project_ids)
    lines_stmt = active_orders(lines_stmt, FMaintenanceOrder)
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
# M0-A 已于 2026-08-16 拍板（签署清单 / 增补包 AB-2）：只注册这两类。
# ②归档池件仍在流转、④无参照价占比高、⑤未归属单、⑥快照差异单 **未获选**，不得自行加。
ATTENTION_KINDS: tuple[str, ...] = ("budget_remaining", "pending_return")

# 「多」不设业务阈值——业务只指定了口径，没有指定分界线。因此本队列是**排序取前
# N**（多的排前面），不是「超过某阈值才报警」。响应里显式回传 ranking/threshold，
# 免得下游把它当成阈值告警。预算侧的红黄语义另有出处（budget_decision），沿用。
ATTENTION_RANKING = "budget_status_desc,pending_return_qty_desc"


def _attention_demand(db: Session) -> dict[str, dict]:
    """逐项目的需求侧数量：Σ需求数量 与 Σ退货（应返）数量。

    两列都在 AGGREGATE_SOURCE_COLUMNS 白名单内。**不碰**「已返/待返」这两个
    流转状态列（铁律 3：状态列只展示、不进聚合）——所以"已回收"一律取三源事实，
    不取单据自报。
    """
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    statement = (
        select(
            MaintenanceSourceOrderAssignment.project_id,
            func.coalesce(func.sum(FMaintenanceLine.qty), 0),
            func.coalesce(func.sum(FMaintenanceLine.return_qty), 0),
        )
        .select_from(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .join(MaintenanceSourceOrderAssignment, active)
        .where(FMaintenanceLine.is_active.is_(True))
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    )
    rows = db.execute(active_orders(statement, FMaintenanceOrder)).all()
    return {pid: {"demand_qty": Decimal(d or 0), "demand_return_qty": Decimal(r or 0)}
            for pid, d, r in rows}


def _current_contract_budget_stats():
    """One current, complete tax-inclusive budget aggregate for every consumer.

    Completeness matches the project read model: at least one current included
    contract, no unmapped current status, no missing included amount, no
    duplicate current relationship for one contract, and no cross-project
    ownership conflict.
    """
    from app.models.maintenance_project import MaintenanceProjectContract

    today = business_today()
    current = and_(
        MaintenanceProjectContract.effective_from <= today,
        or_(
            MaintenanceProjectContract.effective_to.is_(None),
            MaintenanceProjectContract.effective_to > today,
        ),
    )
    counted = and_(
        current,
        MaintenanceProjectContract.included_in_total.is_(True),
    )
    effective = (
        select(
            MaintenanceProjectContract.project_id.label("project_id"),
            MaintenanceProjectContract.contract_id.label("contract_id"),
            MaintenanceProjectContract.contract_no.label("contract_no"),
        )
        .where(counted)
        .cte("boss_current_effective_contract")
    )
    duplicate_contract_ids = (
        select(effective.c.project_id)
        .group_by(effective.c.project_id, effective.c.contract_id)
        .having(func.count() > 1)
    )
    duplicate_contract_nos = (
        select(effective.c.project_id)
        .group_by(effective.c.project_id, effective.c.contract_no)
        .having(func.count() > 1)
    )
    duplicate_contracts = union_all(
        duplicate_contract_ids,
        duplicate_contract_nos,
    ).subquery()
    duplicate_by_project = (
        select(
            duplicate_contracts.c.project_id,
            func.count().label("duplicate_count"),
        )
        .group_by(duplicate_contracts.c.project_id)
        .subquery()
    )
    conflicting_contract_ids = (
        select(effective.c.contract_id)
        .group_by(effective.c.contract_id)
        .having(func.count(func.distinct(effective.c.project_id)) > 1)
        .subquery()
    )
    conflicting_contract_nos = (
        select(effective.c.contract_no)
        .group_by(effective.c.contract_no)
        .having(func.count(func.distinct(effective.c.project_id)) > 1)
        .subquery()
    )
    conflict_id_projects = (
        select(effective.c.project_id)
        .join(
            conflicting_contract_ids,
            conflicting_contract_ids.c.contract_id == effective.c.contract_id,
        )
        .group_by(effective.c.project_id, effective.c.contract_id)
    )
    conflict_no_projects = (
        select(effective.c.project_id)
        .join(
            conflicting_contract_nos,
            conflicting_contract_nos.c.contract_no == effective.c.contract_no,
        )
        .group_by(effective.c.project_id, effective.c.contract_no)
    )
    conflicting_contracts = union_all(
        conflict_id_projects,
        conflict_no_projects,
    ).subquery()
    conflict_by_project = (
        select(
            conflicting_contracts.c.project_id,
            func.count().label("conflict_count"),
        )
        .group_by(conflicting_contracts.c.project_id)
        .subquery()
    )
    base = (
        select(
            MaintenanceProjectContract.project_id.label("project_id"),
            func.count().filter(counted).label("effective_count"),
            func.count().filter(and_(
                current,
                MaintenanceProjectContract.status_mapping_state != "mapped",
            )).label("unmapped_count"),
            func.count().filter(and_(
                counted,
                MaintenanceProjectContract.amount_inc_tax.is_(None),
            )).label("missing_count"),
            func.sum(MaintenanceProjectContract.amount_inc_tax)
            .filter(counted)
            .label("budget"),
        )
        .group_by(MaintenanceProjectContract.project_id)
        .subquery()
    )
    return (
        select(
            base,
            func.coalesce(
                duplicate_by_project.c.duplicate_count, 0
            ).label("duplicate_count"),
            func.coalesce(
                conflict_by_project.c.conflict_count, 0
            ).label("conflict_count"),
        )
        .outerjoin(
            duplicate_by_project,
            duplicate_by_project.c.project_id == base.c.project_id,
        )
        .outerjoin(
            conflict_by_project,
            conflict_by_project.c.project_id == base.c.project_id,
        )
        .subquery()
    )


def _complete_contract_budget(stats):
    """Canonical current-contract completeness gate shared by budget consumers."""
    return and_(
        stats.c.effective_count > 0,
        stats.c.unmapped_count == 0,
        stats.c.missing_count == 0,
        stats.c.duplicate_count == 0,
        stats.c.conflict_count == 0,
        stats.c.budget.is_not(None),
    )


def _attention_budget(db: Session) -> dict[str, Decimal]:
    """逐项目的台账合同额（含税）：只认 included_in_total 的合同行。

    口径出处 REQUIREMENTS #8/#31：正式金额列 = amount_inc_tax；是否计入总额由
    台账 included_in_total 明示，**不从 contract_status 文本猜**。
    """
    stats = _current_contract_budget_stats()
    rows = db.execute(
        select(stats.c.project_id, stats.c.budget).where(
            _complete_contract_budget(stats)
        )
    ).all()
    return {pid: Decimal(amount or 0) for pid, amount in rows}


def _budget_item(project, budget: Decimal, bundle: dict) -> dict | None:
    """①超预算/预算余量。仅红/黄进队列；成本不完整时给 incomplete_cost，不报绿。"""
    from app import config

    value = bundle.get("value") or {}
    summary = {"known_cost_total": Decimal(str(value.get("known_amount") or 0)),
               "cost_quality": value.get("quality") or "incomplete"}
    decision = maintenance_cost_quality.budget_decision(
        summary, budget=budget,
        warn_pct=Decimal(str(config.MAINT_BUDGET_WARN_PCT)))
    status = decision["decision_status"]
    if status not in ("red", "yellow", "incomplete_cost"):
        return None
    return {
        "kind": "budget_remaining",
        "project_id": project.project_id,
        "project_code": project.project_code,
        "display_name": project.display_name,
        "evidence_link": f"/maintenance/boss-board/projects/{project.project_id}/orders",
        "value": {
            "status": status,
            "budget_inc_tax": budget,
            "known_spend_inc_tax": decision["known_spend_total"],
            "remaining": decision["remaining"],
            "remaining_pct": decision["remaining_pct"],
            # 成本不完整时余量是「已知下限」，不是结论（M0-C/铁律 5）
            "cost_quality": summary["cost_quality"],
        },
    }


def _pending_return_item(project, demand: dict, facts: dict | None,
                         rkd_ready: bool) -> dict | None:
    """③待返件多。

    返件率分子 = **返件类**回收（维保拆旧返件＋旧库退返 → RKD 事实 returned_bad），
    分母 = 需求单「需求」数量（业务 2026-08-16 指定，不是领用列）。
    RKD 未导入 → 回收量「无法知道」，返件率与待返件给 not_imported 信封，
    **不按 0 算**（铁律 5）。
    """
    demand_return = demand.get("demand_return_qty") or Decimal(0)
    demand_qty = demand.get("demand_qty") or Decimal(0)
    if demand_return <= 0:
        return None
    recovered = (facts or {}).get("returned_bad") if rkd_ready else None
    if not rkd_ready:
        pending, rate = not_imported(), not_imported()
    else:
        recovered = recovered or Decimal(0)
        pending = ready(demand_return - recovered)
        rate = ready(round(recovered / demand_qty * 100, 1)
                     if demand_qty > 0 else None)
    return {
        "kind": "pending_return",
        "project_id": project.project_id,
        "project_code": project.project_code,
        "display_name": project.display_name,
        "evidence_link": f"/maintenance/boss-board/projects/{project.project_id}/orders",
        "value": {
            "demand_return_qty": demand_return,
            "recovered_return_qty": ready(recovered) if rkd_ready else not_imported(),
            "pending_return_qty": pending,
            "return_rate_pct": rate,
        },
        "_rank": demand_return,
    }


def attention(db: Session, *, user_ctx: UserContext, limit: int = 10,
              allowed_project_ids: set[str] | None = None) -> dict:
    """需关注队列 ≤10 条（M0-A 已拍板：只有 ①超预算 与 ③待返件多）。

    无成本权限的账号看不到 ①——预算条目本身就是金额派生物，「它在不在队列里」
    已经泄露成本排名，所以整条略去而不是包 restricted 信封（无侧信道）。
    allowed_project_ids 非 None（行键 own_maintenance_projects_only 开）时，
    队列收敛到该范围，不得经「关注事项」泄露他人项目。
    """
    can_cost = can_view_cost(user_ctx)
    can_contract = can_view_contract(user_ctx)
    project_filters = [MaintenanceProject.is_active.is_(True)]
    if allowed_project_ids is not None:
        project_filters.append(
            MaintenanceProject.project_id.in_(allowed_project_ids or {""}))
    projects = db.execute(
        select(MaintenanceProject).where(*project_filters)
    ).scalars().all()
    if not projects:
        return {"items": [], "registered_kinds": list(ATTENTION_KINDS),
                "ranking": ATTENTION_RANKING, "threshold": None}
    project_ids = [p.project_id for p in projects]
    demand = _attention_demand(db)
    facts = maintenance_boss_facts.project_totals(db, project_ids=project_ids)
    rkd_ready = (maintenance_source_health.source_health(db)["sources"]
                 ["rkd_inbound"]["readiness"] != "not_imported")

    budget_items: list[dict] = []
    if can_cost and can_contract:
        budgets = _attention_budget(db)
        bundles = _cost_bundles_by_project(
            db, window=(_FULL_LIFETIME_START, business_today()),
            project_ids=project_ids, can_cost=True)
        for project in projects:
            budget = budgets.get(project.project_id)
            if budget is None or budget <= 0:
                continue           # 无台账合同额 → 谈不上预算余量，不编造
            item = _budget_item(project, budget,
                                bundles.get(project.project_id, {}))
            if item is not None:
                budget_items.append(item)
    _STATUS_WEIGHT = {"red": 0, "yellow": 1, "incomplete_cost": 2}
    budget_items.sort(key=lambda i: (_STATUS_WEIGHT[i["value"]["status"]],
                                     i["project_code"]))

    return_items = [
        item for item in (
            _pending_return_item(p, demand.get(p.project_id, {}),
                                 facts.get(p.project_id), rkd_ready)
            for p in projects)
        if item is not None
    ]
    return_items.sort(key=lambda i: (-i.pop("_rank"), i["project_code"]))

    items = (budget_items + return_items)[:limit]
    return {
        "items": items,
        "registered_kinds": list(ATTENTION_KINDS),
        "ranking": ATTENTION_RANKING,
        # 显式为 null：业务只给了口径没给分界线，这是排序取前 N，不是阈值告警
        "threshold": None,
    }


# ---------------------------------------------------------------- 项目列表

_SORTS = {"attention", "orders", "name", "known_cost", "cost_ratio"}


class BoardSortNotPermitted(Exception):
    """成本相关排序需要成本数据权限（不静默降级——降级会通过顺序泄露排名）。"""


class BoardCostContractNotPermitted(Exception):
    """成本率/三态筛选同时依赖成本与合同财务数据，缺任一均不得执行（防侧信道）。"""


def sort_project_ids_by_cost_ratio(
    project_ids: list[str],
    *,
    cost_bundles: dict,
    contracts: dict,
) -> list[str]:
    """Use the same cost bundle and contract snapshot as the project card.

    Unknown ratios (no positive contract amount or no resolved cost bundle) are
    deliberately last; project IDs provide a deterministic final tie-breaker.
    """
    def ratio(project_id: str) -> Decimal | None:
        contract = contracts.get(project_id) or {}
        amount = contract.get("amount_inc_tax")
        bundle = cost_bundles.get(project_id) or {}
        if (contract.get("contract_incomplete")
                or not amount or Decimal(str(amount)) <= 0
                or bundle.get("state") not in {"ready", "partial", "stale"}):
            return None
        value = bundle.get("value") or {}
        # partial 且覆盖率为 0 = 所有行都缺价；known_amount=0 只是聚合单位元，
        # 不是可排序的真实成本率。0 行项目的 ready+0 仍是合法零成本。
        if bundle.get("state") == "partial" and not value.get("coverage_pct"):
            return None
        known = value.get("known_amount")
        if known is None:
            return None
        return Decimal(str(known)) / Decimal(str(amount))

    return sorted(
        project_ids,
        key=lambda project_id: (
            ratio(project_id) is None,
            -(ratio(project_id) or Decimal("0")),
            project_id,
        ),
    )


def projects(db: Session, *, user_ctx: UserContext, page: int = 1,
             page_size: int = 20, lifecycle: str = "all",
             sort: str = "name", q_text: str | None = None,
             has_activity: bool | None = None,
             card_status_filter: str | None = None,
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
    can_contract = can_view_contract(user_ctx)
    if sort == "known_cost" and not can_cost:
        raise BoardSortNotPermitted()
    if sort == "cost_ratio" and not (can_cost and can_contract):
        raise BoardCostContractNotPermitted()
    if card_status_filter in CARD_STATUSES and not (can_cost and can_contract):
        raise BoardCostContractNotPermitted()
    # 生命周期由 canonical period 在请求业务日动态派生；数据库列只是最近一次
    # 写入的兼容快照，不能参与跨日筛选或返回。
    today = business_today()
    lifecycle_expr = maintenance_periods.lifecycle_case(
        MaintenanceProject.period_from,
        MaintenanceProject.period_to,
        as_of=today,
    )
    # 项目卡默认展示“该项目全生命周期”，与下钻需求单的全历史母集一致。
    # 旧默认 YTD 会让 223 个只有往年需求的生产项目卡显示 0，却能在下钻看到单据。
    window = (
        (_FULL_LIFETIME_START, today)
        if date_from is None and date_to is None
        else resolve_window(date_from, date_to)
    )

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
        filters.append(lifecycle_expr == lifecycle)
    if allowed_project_ids is not None:
        filters.append(MaintenanceProject.project_id.in_(allowed_project_ids or {""}))
    if q_text:
        needle = q_text.strip()
        # 除项目名/编号外还命中 XSDD 合同号（#37：搜项目名、项目单号）
        from app.models.maintenance_project import (
            MaintenanceProjectAlias,
            MaintenanceProjectContract,
        )

        by_contract = (
            select(MaintenanceProjectContract.project_id)
            .where(MaintenanceProjectContract.contract_no
                   .icontains(needle, autoescape=True))
        )
        by_alias = (
            select(MaintenanceProjectAlias.project_id)
            .where(MaintenanceProjectAlias.alias_name
                   .icontains(needle, autoescape=True))
        )
        filters.append(or_(
            MaintenanceProject.project_code.icontains(needle, autoescape=True),
            MaintenanceProject.display_name.icontains(needle, autoescape=True),
            MaintenanceProject.project_id.in_(by_contract),
            MaintenanceProject.project_id.in_(by_alias),
        ))

    # 窗口内的每项目计数/成本子查询：既用于 has_activity 过滤，也用于真实排序。
    # 不这么做的话 sort/has_activity 会变成「接收但静默忽略」的假参数——调用方
    # 以为已排序/已筛选，实际拿到的是 project_code 字典序全集。
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    known_actual_amount, known_estimated_amount, *_ = _cost_columns()
    window_stats_stmt = (
        select(
            MaintenanceSourceOrderAssignment.project_id.label("project_id"),
            func.count(func.distinct(FMaintenanceOrder.id)).label("orders_n"),
            (known_actual_amount + known_estimated_amount).label("known_cost"),
        )
        .select_from(FMaintenanceOrder)
        .join(MaintenanceSourceOrderAssignment, active)
        .outerjoin(FMaintenanceLine,
                   and_(FMaintenanceLine.order_id == FMaintenanceOrder.id,
                        FMaintenanceLine.is_active.is_(True)))
        .outerjoin(
            MaintenanceManualCostOverride,
            and_(
                MaintenanceManualCostOverride.line_id == FMaintenanceLine.id,
                MaintenanceManualCostOverride.active.is_(True),
            ),
        )
        .where(_order_date_in_window(window))
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    )
    window_stats = active_orders(
        window_stats_stmt, FMaintenanceOrder,
    ).subquery()
    # sort=attention 的两个注册口径（AB-2）。都做成子查询，排序才是**全量**排序，
    # 而不是「先取一页再排」那种只在当页内成立的假排序。
    return_stats_stmt = (
        select(MaintenanceSourceOrderAssignment.project_id.label("project_id"),
               func.coalesce(func.sum(FMaintenanceLine.return_qty), 0)
               .label("demand_return_qty"))
        .select_from(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .join(MaintenanceSourceOrderAssignment, active)
        .where(FMaintenanceLine.is_active.is_(True))
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    )
    return_stats = active_orders(
        return_stats_stmt, FMaintenanceOrder,
    ).subquery()
    budget_stats = (
        _budget_overspend_stats()
        if sort == "attention" and can_cost and can_contract
        else None
    )

    base = (select(MaintenanceProject)
            .outerjoin(window_stats,
                       window_stats.c.project_id == MaintenanceProject.project_id)
            .outerjoin(return_stats,
                       return_stats.c.project_id == MaintenanceProject.project_id))
    if budget_stats is not None:
        base = base.outerjoin(
            budget_stats,
            budget_stats.c.project_id == MaintenanceProject.project_id,
        )
    base = base.where(*filters)
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
        # M0-A 已拍板（AB-2）：注册口径 = ①超预算 ③待返件多。这里按注册口径中
        # **可在 SQL 表达**的两项排序：应返数量（Σ退货列，白名单内）优先，其次本期
        # 单量。预算红黄是成本派生物——无成本权限的账号若让它参与排序，顺序本身
        # 就泄露了金额排名（§6.2 无侧信道），故只有有成本权限时才计入。
        attn = [func.coalesce(return_stats.c.demand_return_qty, 0).desc()]
        if can_cost and can_contract:
            assert budget_stats is not None
            attn.insert(0, func.coalesce(budget_stats.c.overspend, 0).desc())
        order_by = (*attn, func.coalesce(window_stats.c.orders_n, 0).desc(),
                    MaintenanceProject.project_code)
    elif sort == "cost_ratio":
        # This branch is handled after the candidate query below because the
        # card's ratio depends on the same Python cost bundle and contract
        # snapshot used for rendering.
        order_by = (MaintenanceProject.project_code,)
    else:
        order_by = (MaintenanceProject.project_code,)
    if sort == "cost_ratio":
        candidates = db.execute(base.order_by(*order_by)).scalars().all()
        candidate_ids = [project.project_id for project in candidates]
        all_cost_bundles = _cost_bundles_by_project(
            db, window=window, project_ids=candidate_ids, can_cost=True
        )
        all_contracts = _card_contracts(db, candidate_ids)
        ordered_ids = sort_project_ids_by_cost_ratio(
            candidate_ids, cost_bundles=all_cost_bundles, contracts=all_contracts
        )
        by_id = {project.project_id: project for project in candidates}
        rows = [by_id[project_id] for project_id in ordered_ids]
        rows = rows[(page - 1) * page_size: page * page_size]
    else:
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
    # 项目卡墙补充数据（REQUIREMENTS #34/#35/#41），逐项一次分组查询，与页大小无关
    contracts = _card_contracts(db, project_ids)
    procured = _card_procured_qty(db, window, project_ids)
    collections = _card_collections(db, project_ids)
    manager_names = _manager_display_names(
        db, [p.project_manager_id for p in rows])
    cost_ex = _card_cost_ex_tax(db, window, project_ids)
    expense_costs, requisition_costs = _card_expense_and_requisition_costs(
        db, project_ids)
    # 卡片「销售」（2026-08-21 客户反馈）：台账 salesperson 优先，XSDD 众数兜底
    sales_modes = _card_salesperson_modes(db, project_ids)
    aliases = maintenance_project_identity.aliases_by_project(db, project_ids)

    out_rows = []
    for proj in rows:
        orders_n, lines_n = counts.get(proj.project_id, (0, 0))
        out_rows.append({
            "project_id": proj.project_id,
            "project_code": proj.project_code,
            "display_name": proj.display_name,
            "aliases": [
                name for name in aliases.get(proj.project_id, [])
                if project_names.display_name_identity(name)
                != project_names.display_name_identity(proj.display_name)
            ],
            "lifecycle": maintenance_periods.lifecycle_status(
                proj.period_from,
                proj.period_to,
                today,
            ),
            # 维保期限主数据（#51）：WBDD 聚合/名称解析回填，台账导入后为台账值
            "period_from": proj.period_from.isoformat() if proj.period_from else None,
            "period_to": proj.period_to.isoformat() if proj.period_to else None,
            # 归档但仍带单：留在列表里保住母集恒等式，用标记让老板知道它已归档
            "is_archived": not proj.is_active,
            "has_activity_in_window": bool(orders_n),
            "pre_delivery_order_count": pre_delivery.get(proj.project_id, 0),
            "orders_ytd": ready(orders_n) if wbdd_ready else not_imported(),
            "lines_ytd": ready(lines_n) if wbdd_ready else not_imported(),
            "known_apply_cost_inc_tax": (
                cost_bundles[proj.project_id] if wbdd_ready
                else (restricted() if not can_cost else not_imported())),
            **_card_fields(proj, can_cost=can_cost, can_contract=can_contract,
                           wbdd_ready=wbdd_ready,
                           contracts=contracts.get(proj.project_id),
                           procured=procured.get(proj.project_id),
                           collected=collections.get(proj.project_id),
                           cost_ex=cost_ex.get(proj.project_id),
                           bundle=cost_bundles.get(proj.project_id),
                           manager_display=manager_names.get(proj.project_manager_id or ""),
                           salesperson=(proj.salesperson
                                        or sales_modes.get(proj.project_id)),
                           expense_cost=expense_costs.get(proj.project_id),
                           requisition_cost=requisition_costs.get(proj.project_id)),
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
            "aliases": [],
            "lifecycle": "missing",
            "period_from": None,       # 桶不是项目，没有期限可言
            "period_to": None,
            "is_archived": False,      # 键集与项目行保持一致
            "has_activity_in_window": bool(u_orders),
            "pre_delivery_order_count": 0,
            "orders_ytd": ready(u_orders) if wbdd_ready else not_imported(),
            "lines_ytd": ready(u_lines) if wbdd_ready else not_imported(),
            "known_apply_cost_inc_tax": (
                _cost_bundle(db, window=window, unassigned_only=True,
                             can_cost=can_cost) if wbdd_ready
                else (restricted() if not can_cost else not_imported())),
            # 桶不是项目：没有合同/经理/回款可言，一律 not_imported 而非 0
            **_card_fields(None, can_cost=can_cost, can_contract=can_contract,
                           wbdd_ready=wbdd_ready,
                           contracts=None, procured=None, collected=None,
                           cost_ex=None, bundle=None,
                           expense_cost=None, requisition_cost=None),
            # 未归属单没有项目口径的三源事实（CKD 靠归属才落项目）——系统「无法知道」，
            # 不是「等于 0」。用 not_imported 信封而非 ready(0)（铁律 5）。
            **{k: fact_not_imported() for k in FACT_FIELDS},
        })
    if card_status_filter in CARD_STATUSES:
        # 三态只由成本率决定（#43）。这里在**取完当页后**过滤而不是下推 SQL：
        # 口径必须与卡片显示的完全一致，两处各写一份迟早会漂。代价是筛选态下
        # 分页是「页内过滤」，total 如实回传过滤前的口径，前端据 rows 长度续拉。
        out_rows = [r for r in out_rows if r.get("card_status") == card_status_filter]
    return {"rows": out_rows, "total": total, "page": page,
            "page_size": page_size, "sort": sort,
            "sort_applied": sort,
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


def _budget_overspend_stats():
    """逐项目「已知支出 − 台账合同额(含税)」子查询，供 sort=attention 排序用。

    正数=已超；负数=尚有余量。取值口径与 attention() 的 ①一致（合同额只认
    included_in_total，成本取回填含税列），差别只是这里要能进 ORDER BY。
    """
    contract_facts = _current_contract_budget_stats()
    contract = (
        select(contract_facts.c.project_id, contract_facts.c.budget)
        .where(_complete_contract_budget(contract_facts))
        .subquery()
    )
    known_actual_amount, known_estimated_amount, *_ = _cost_columns()
    spend_stmt = (
        select(MaintenanceSourceOrderAssignment.project_id.label("project_id"),
               (known_actual_amount + known_estimated_amount).label("spend"))
        .select_from(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .outerjoin(
            MaintenanceManualCostOverride,
            and_(
                MaintenanceManualCostOverride.line_id == FMaintenanceLine.id,
                MaintenanceManualCostOverride.active.is_(True),
            ),
        )
        .join(MaintenanceSourceOrderAssignment,
              and_(MaintenanceSourceOrderAssignment.source_order_id
                   == FMaintenanceOrder.raw_order_id,
                   MaintenanceSourceOrderAssignment.is_active.is_(True)))
        .where(
            _order_date_in_window((_FULL_LIFETIME_START, business_today())),
            FMaintenanceLine.is_active.is_(True),
        )
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    )
    spend = active_orders(spend_stmt, FMaintenanceOrder).subquery()
    return (
        select(contract.c.project_id.label("project_id"),
               (func.coalesce(spend.c.spend, 0) - contract.c.budget)
               .label("overspend"))
        .select_from(contract)
        .outerjoin(spend, spend.c.project_id == contract.c.project_id)
        .subquery()
    )


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
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    statement = (
        select(MaintenanceSourceOrderAssignment.project_id,
               func.count(func.distinct(FMaintenanceOrder.id)),
               func.count(FMaintenanceLine.id))
        .select_from(FMaintenanceOrder)
        .join(MaintenanceSourceOrderAssignment, active)
        .outerjoin(FMaintenanceLine,
                   and_(FMaintenanceLine.order_id == FMaintenanceOrder.id,
                        FMaintenanceLine.is_active.is_(True)))
        .where(MaintenanceSourceOrderAssignment.project_id.in_(project_ids),
               _order_date_in_window(window))
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    )
    rows = db.execute(active_orders(statement, FMaintenanceOrder)).all()
    return {pid: (int(o), int(l)) for pid, o, l in rows}


# 卡片三态（REQUIREMENTS #35/#43）：成本÷合同额 <80% 绿 / 80–100% 黄 / >100% 红。
# 「需关注」= 提醒 = 黄，不再单列一栏（#43）。
CARD_STATUSES = ("normal", "warning", "alert")
_WARNING_AT = Decimal("80")
_ALERT_AT = Decimal("100")


def card_status(cost_ratio_pct: Decimal | None) -> str | None:
    """算不出来就返回 None（无合同额或无成本）——不拿绿色冒充「健康」（铁律 5）。"""
    if cost_ratio_pct is None:
        return None
    if cost_ratio_pct > _ALERT_AT:
        return "alert"
    if cost_ratio_pct >= _WARNING_AT:
        return "warning"
    return "normal"


def _card_contracts(db: Session, project_ids: list[str]) -> dict[str, dict]:
    """逐项目：合同总额（含税）+ 合同号清单 + 诚实标注（#51 两层取数）。

    1. **台账合同**（MaintenanceProjectContract，只认当前 included_in_total）＝权威源；
       缺含税额、未映射、重复稳定合同或跨项目冲突均 fail-closed。
    2. **XSDD 回退**（v1.17 老版口径，业务指示 2026-08-17）：台账缺位时按项目挂靠
       单据的 distinct XSDD 去成功销售批次取金额——只有生效、未税额和税率均
       明确，且同一单号的全部有效候选经济值一致时，才计算
       `amount_ex_tax×(1+tax_rate)`，跨单求和。两个诚实标注随行返回：
       - `contract_shared`：某 XSDD 同时挂在多个项目上（生产 13 张共用单），合同额
         会在项目间重复计入——只标注，不擅自分摊（Q5：合同额仅参考，不出毛利）；
       - `contract_incomplete`：有 XSDD 不在销售表（生产 5 个 2023 老单），合同额被
         低估——标注而非静默按 0（铁律 5）。

    合同号即 XSDD 销售订单号（#45 归属判定依据）；回退层的合同号=挂靠 XSDD 清单。
    """
    from app import config
    from app.models.maintenance_project import MaintenanceProjectContract
    from app.models.sales import FSalesOrder
    from app.models.system import SysImportBatch
    if not project_ids:
        return {}
    rows = db.execute(
        select(MaintenanceProjectContract.project_id,
               MaintenanceProjectContract.contract_id,
               MaintenanceProjectContract.contract_no,
               MaintenanceProjectContract.amount_inc_tax,
               MaintenanceProjectContract.included_in_total,
               MaintenanceProjectContract.status_mapping_state)
        .where(
            MaintenanceProjectContract.project_id.in_(project_ids),
            MaintenanceProjectContract.effective_from <= business_today(),
            or_(
                MaintenanceProjectContract.effective_to.is_(None),
                MaintenanceProjectContract.effective_to > business_today(),
            ),
        )
        .order_by(MaintenanceProjectContract.effective_from)
    ).all()
    included_contract_ids = {
        contract_id for _pid, contract_id, _no, _amount, included, _mapping in rows
        if included
    }
    included_contract_nos = {
        no for _pid, _contract_id, no, _amount, included, _mapping in rows
        if included and no
    }
    projects_by_contract_id: dict[str, set[str]] = {}
    projects_by_contract_no: dict[str, set[str]] = {}
    if included_contract_ids or included_contract_nos:
        for contract_id, contract_no, related_project_id in db.execute(
            select(
                MaintenanceProjectContract.contract_id,
                MaintenanceProjectContract.contract_no,
                MaintenanceProjectContract.project_id,
            )
            .where(
                or_(
                    MaintenanceProjectContract.contract_id.in_(included_contract_ids),
                    MaintenanceProjectContract.contract_no.in_(included_contract_nos),
                ),
                MaintenanceProjectContract.included_in_total.is_(True),
                MaintenanceProjectContract.effective_from <= business_today(),
                or_(
                    MaintenanceProjectContract.effective_to.is_(None),
                    MaintenanceProjectContract.effective_to > business_today(),
                ),
            )
            .group_by(
                MaintenanceProjectContract.contract_id,
                MaintenanceProjectContract.contract_no,
                MaintenanceProjectContract.project_id,
            )
        ):
            projects_by_contract_id.setdefault(contract_id, set()).add(
                related_project_id
            )
            if contract_no:
                projects_by_contract_no.setdefault(contract_no, set()).add(
                    related_project_id
                )
    conflicting_contract_ids = {
        contract_id
        for contract_id, related_projects in projects_by_contract_id.items()
        if len(related_projects) > 1
    }
    conflicting_contract_nos = {
        contract_no
        for contract_no, related_projects in projects_by_contract_no.items()
        if len(related_projects) > 1
    }
    out: dict[str, dict] = {}
    for pid, contract_id, no, amount, included, mapping_state in rows:
        bucket = out.setdefault(
            pid, {"contract_nos": [], "amount_inc_tax": None,
                  "contract_shared": False, "contract_incomplete": False,
                  "_included_contract_ids": [], "_included_contract_nos": [],
                  "_included_count": 0})
        if no and no not in bucket["contract_nos"]:
            bucket["contract_nos"].append(no)
        if mapping_state != "mapped":
            bucket["contract_incomplete"] = True
        elif included:
            bucket["_included_count"] += 1
            bucket["_included_contract_ids"].append(contract_id)
            bucket["_included_contract_nos"].append(no)
            if amount is not None:
                bucket["amount_inc_tax"] = (bucket["amount_inc_tax"] or Decimal(0)) + amount
            else:
                bucket["contract_incomplete"] = True

    for bucket in out.values():
        contract_ids = bucket.pop("_included_contract_ids")
        contract_nos = bucket.pop("_included_contract_nos")
        included_count = bucket.pop("_included_count")
        duplicate = (
            len(contract_ids) != len(set(contract_ids))
            or len(contract_nos) != len(set(contract_nos))
        )
        shared = (
            any(contract_id in conflicting_contract_ids for contract_id in contract_ids)
            or any(contract_no in conflicting_contract_nos for contract_no in contract_nos)
        )
        if included_count == 0 or duplicate or shared:
            bucket["contract_incomplete"] = True
        if duplicate or shared:
            # 重复或跨项目冲突时连“已知小计”都可能双计，不返回伪精确金额。
            bucket["amount_inc_tax"] = None
        bucket["contract_shared"] = shared

    # —— XSDD 回退：只对「台账没给出金额」的项目生效，台账永远优先 ——
    # 只有“完全没有当前合同行”的项目才走销售事实回退。已有合同行但含税额
    # 缺失时必须诚实标 incomplete，不能用 13%/0% 猜测覆盖权威事实。
    fallback_ids = [pid for pid in project_ids if pid not in out]
    if not fallback_ids:
        return out
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    # 全量「XSDD→挂了哪些项目」映射：shared 判定必须看全局（不止本页），
    # 否则翻页会把共用单误标成独占。
    xsdd_rows = db.execute(active_orders(
        select(MaintenanceSourceOrderAssignment.project_id,
               FMaintenanceOrder.linked_sales_order_no)
        .select_from(FMaintenanceOrder)
        .join(MaintenanceSourceOrderAssignment, active)
        .where(FMaintenanceOrder.linked_sales_order_no.is_not(None))
        .group_by(MaintenanceSourceOrderAssignment.project_id,
                  FMaintenanceOrder.linked_sales_order_no),
        FMaintenanceOrder,
    )).all()
    order_projects: dict[str, set] = {}
    project_orders: dict[str, list] = {}
    for pid, ono in xsdd_rows:
        order_projects.setdefault(ono, set()).add(pid)
        if pid in fallback_ids:
            project_orders.setdefault(pid, []).append(ono)
    all_orders = sorted({o for pid in fallback_ids
                         for o in project_orders.get(pid, [])})
    amounts: dict[str, Decimal] = {}
    ambiguous_orders: set[str] = set()
    if all_orders:
        # One batched evidence query for every fallback project.  Failed or
        # wrong-type import batches are not facts, and ACTIVE_STATUS_ONLY must
        # not weaken this financial boundary when disabled elsewhere.
        candidates = db.execute(
            select(
                FSalesOrder.order_no,
                FSalesOrder.amount_ex_tax,
                FSalesOrder.tax_rate,
            )
            .join(
                SysImportBatch,
                SysImportBatch.id == FSalesOrder.import_batch_id,
            )
            .where(
                FSalesOrder.order_no.in_(all_orders),
                FSalesOrder.data_status == config.ACTIVE_STATUS,
                FSalesOrder.amount_ex_tax.is_not(None),
                FSalesOrder.tax_rate.is_not(None),
                SysImportBatch.file_type == "sales",
                SysImportBatch.status == "success",
            )
            .order_by(
                FSalesOrder.order_no,
                SysImportBatch.uploaded_at,
                FSalesOrder.created_at,
                SysImportBatch.id,
                FSalesOrder.id,
            )
        ).all()
        economics_by_order: dict[
            str, set[tuple[Decimal, Decimal, Decimal]]
        ] = {}
        for order_no, amount_ex_tax, tax_rate in candidates:
            ex_tax = Decimal(str(amount_ex_tax))
            rate = Decimal(str(tax_rate))
            inc_tax = tax_policy.round_money(
                ex_tax * (Decimal("1") + rate)
            )
            economics_by_order.setdefault(order_no, set()).add(
                (ex_tax, rate, inc_tax)
            )
        for order_no, economics in economics_by_order.items():
            if len(economics) != 1:
                ambiguous_orders.add(order_no)
                continue
            amounts[order_no] = next(iter(economics))[2]
    for pid in fallback_ids:
        onos = sorted(project_orders.get(pid, []))
        if not onos:
            continue
        bucket = out.setdefault(
            pid, {"contract_nos": [], "amount_inc_tax": None,
                  "contract_shared": False, "contract_incomplete": False})
        if not bucket["contract_nos"]:
            bucket["contract_nos"] = onos
        total = sum((amounts.get(o) or Decimal(0)) for o in onos)
        missing = [o for o in onos if o not in amounts]
        conflicts = [o for o in onos if o in ambiguous_orders]
        if conflicts:
            # One ambiguous XSDD invalidates the project total: returning the
            # remaining known subtotal would look like a precise contract cap.
            bucket["amount_inc_tax"] = None
        elif total > 0:
            bucket["amount_inc_tax"] = total
        bucket["contract_shared"] = any(
            len(order_projects.get(o, set())) > 1 for o in onos)
        # 共享 XSDD 可保留金额作“参考”，但不能据此给多个项目计算成本率/红黄绿；
        # 否则同一合同总额会被重复当成每个项目的独占预算。
        bucket["contract_incomplete"] = (
            bool(missing)
            or bool(conflicts)
            or bucket["contract_shared"]
        )
    return out


def _card_salesperson_modes(db: Session,
                            project_ids: list[str]) -> dict[str, str]:
    """每项目 XSDD 需求单销售众数（2026-08-21 客户反馈：卡片显示销售）。

    台账 salesperson 缺省时的兜底口径，与总表导出 `_project_order_salesperson`
    一致；查询实现在 maintenance_source_assignments.salesperson_modes_by_project
    （与维保负责人自动回填共用同一口径），此处只做转发。
    """
    from app.services import maintenance_source_assignments

    return maintenance_source_assignments.salesperson_modes_by_project(
        db, project_ids)


def _card_expense_and_requisition_costs(
    db: Session, project_ids: list[str]
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """卡片「报销成本 / 已领用成本」（2026-08-22 客户反馈：上卡彩色展示）。

    报销 = 归因表 mapped+approved 的含税额合计（日期 ≤ 今天）；
    领用 = 现场领用行 mapped+confirmed/corrected 的已知含税成本合计。
    与 workspace 指标同一口径（_project_card_from_facts），批量一次查询。
    """
    from app.models.maintenance_project_operations import (
        MaintenanceProjectExpenseAttribution,
    )
    from app.models.maintenance_project_operations import (
        MaintenanceSiteIssue,
        MaintenanceSiteIssueLine,
    )

    if not project_ids:
        return {}, {}
    today = business_today()
    expense_rows = db.execute(
        select(
            MaintenanceProjectExpenseAttribution.project_id,
            func.coalesce(func.sum(
                MaintenanceProjectExpenseAttribution.amount_inc_tax), 0),
        )
        .where(
            MaintenanceProjectExpenseAttribution.project_id.in_(project_ids),
            MaintenanceProjectExpenseAttribution.status_mapping_state == "mapped",
            MaintenanceProjectExpenseAttribution.normalized_status == "approved",
            MaintenanceProjectExpenseAttribution.ownership_mapping_state == "mapped",
            MaintenanceProjectExpenseAttribution.project_contract_id.is_not(None),
            MaintenanceProjectExpenseAttribution.expense_date <= today,
        )
        .group_by(MaintenanceProjectExpenseAttribution.project_id)
    ).all()
    requisition_rows = db.execute(
        select(
            MaintenanceSiteIssue.project_id,
            func.coalesce(func.sum(
                MaintenanceSiteIssueLine.cost_amount_inc_tax), 0),
        )
        .select_from(MaintenanceSiteIssueLine)
        .join(MaintenanceSiteIssue,
              MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id)
        .where(
            MaintenanceSiteIssue.project_id.in_(project_ids),
            MaintenanceSiteIssue.status_mapping_state == "mapped",
            MaintenanceSiteIssue.normalized_status.in_(["confirmed", "corrected"]),
            MaintenanceSiteIssue.issue_date <= today,
            MaintenanceSiteIssueLine.is_active.is_(True),
            MaintenanceSiteIssueLine.cost_amount_inc_tax.isnot(None),
        )
        .group_by(MaintenanceSiteIssue.project_id)
    ).all()
    # 有领用行但全无参照价 → None（「—」= 算不出）；完全没有领用行 → 0（可知的零）
    has_lines = {
        pid for (pid,) in db.execute(
            select(MaintenanceSiteIssue.project_id).where(
                MaintenanceSiteIssue.project_id.in_(project_ids),
                MaintenanceSiteIssue.status_mapping_state == "mapped",
                MaintenanceSiteIssue.normalized_status.in_(["confirmed", "corrected"]),
                MaintenanceSiteIssue.issue_date <= today,
            ).distinct())
    }
    requisition_costs: dict[str, Decimal] = {
        pid: Decimal(v or 0) for pid, v in requisition_rows}
    for pid in project_ids:
        if pid not in requisition_costs and pid not in has_lines:
            requisition_costs[pid] = Decimal(0)
    return (
        {pid: Decimal(v or 0) for pid, v in expense_rows},
        requisition_costs,
    )


def _card_procured_qty(db: Session, window: tuple[date, date],
                       project_ids: list[str]) -> dict[str, Decimal]:
    """维保备件采购数 = 库房发货 + 直采直发（REQUIREMENTS #41 业务指定公式）。

    这两列在需求单上，属流转状态列家族；#41 是业务对这两列的**明文授权**，
    豁免范围见 PROCURED_QTY_COLUMNS 的注释，其余状态列一律不得入聚合。
    """
    if not project_ids:
        return {}
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    statement = (
        select(MaintenanceSourceOrderAssignment.project_id,
               func.coalesce(func.sum(
                   func.coalesce(FMaintenanceLine.warehouse_shipped_qty, 0)
                   + func.coalesce(FMaintenanceLine.direct_ship_qty, 0)), 0))
        .select_from(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .join(MaintenanceSourceOrderAssignment, active)
        .where(MaintenanceSourceOrderAssignment.project_id.in_(project_ids),
               _order_date_in_window(window),
               FMaintenanceLine.is_active.is_(True))
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    )
    rows = db.execute(active_orders(statement, FMaintenanceOrder)).all()
    return {pid: Decimal(qty or 0) for pid, qty in rows}


def _card_collections(db: Session, project_ids: list[str]) -> dict[str, Decimal]:
    """回款预览 = 每份合同最新 confirmed 月度累计快照之和（REQUIREMENTS #30）。"""
    from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot

    if not project_ids:
        return {}
    rows = db.execute(
        select(MaintenanceCollectionSnapshot)
        .where(MaintenanceCollectionSnapshot.project_id.in_(project_ids),
               MaintenanceCollectionSnapshot.status == "confirmed",
               MaintenanceCollectionSnapshot.report_month <= business_today())
        .order_by(MaintenanceCollectionSnapshot.report_month)
    ).scalars()
    latest: dict[tuple[str, str], MaintenanceCollectionSnapshot] = {}
    for snapshot in rows:
        latest[(snapshot.project_id, snapshot.project_contract_id)] = snapshot
    out: dict[str, Decimal] = {}
    for (pid, _), snapshot in latest.items():
        out[pid] = (out.get(pid) or Decimal(0)) + snapshot.cumulative_amount
    return out


def _card_cost_ex_tax(db: Session, window: tuple[date, date],
                      project_ids: list[str]) -> dict[str, dict]:
    """备件成本（未税）——与含税卡片复用同一严格成本五件套。"""
    if not project_ids:
        return {}
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    statement = (
        select(
            MaintenanceSourceOrderAssignment.project_id,
            *_cost_columns_for_basis("ex"),
        )
        .select_from(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .outerjoin(
            MaintenanceManualCostOverride,
            and_(
                MaintenanceManualCostOverride.line_id == FMaintenanceLine.id,
                MaintenanceManualCostOverride.active.is_(True),
            ),
        )
        .join(MaintenanceSourceOrderAssignment, active)
        .where(MaintenanceSourceOrderAssignment.project_id.in_(project_ids),
               _order_date_in_window(window),
               FMaintenanceLine.is_active.is_(True))
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    )
    rows = db.execute(active_orders(statement, FMaintenanceOrder)).all()
    return {row[0]: _bundle_from_row(*row[1:]) for row in rows}


def _manager_display_names(db: Session, usernames: list[str | None]) -> dict[str, str]:
    """账号 → 显示名（批量）。项目经理卡片展示用人名，不用账号串。"""
    from app.models.system import SysUser

    wanted = {u for u in usernames if u}
    if not wanted:
        return {}
    rows = db.execute(
        select(SysUser.username, SysUser.display_name).where(
            SysUser.username.in_(wanted))
    ).all()
    return {u: (d or u) for u, d in rows}


def _card_fields(project, *, can_cost: bool, can_contract: bool,
                 wbdd_ready: bool,
                 contracts: dict | None, procured, collected, cost_ex,
                 bundle: dict | None,
                 manager_display: str | None = None,
                 salesperson: str | None = None,
                 expense_cost=None, requisition_cost=None) -> dict:
    """项目卡的补充字段（REQUIREMENTS #34/#35）。

    成本字段（已知成本、报销/领用成本、成本未税）挂 `data_purchase_cost`；
    合同额/回款/预算/余额挂 `data_profit`：均通过 `is_field_hidden` 判定，不按 role。
    成本率与三态同时依赖两组权限，缺任一均 restricted / None，避免侧信道。
    """
    money_cost = (lambda value: ready(value)) if can_cost else (lambda _v: restricted())
    money_contract = (lambda value: ready(value)) if can_contract else (lambda _v: restricted())
    contract_nos = (contracts or {}).get("contract_nos") or []
    contract_amount = (contracts or {}).get("amount_inc_tax")
    contract_incomplete = bool((contracts or {}).get("contract_incomplete"))
    known_inc = None
    if bundle is not None and bundle.get("state") in {"ready", "partial", "stale"}:
        value = bundle.get("value") or {}
        # 所有行缺价时 known_amount=0 仅是 SQL 聚合单位元。只在至少有一条
        # 已知成本，或项目确实没有需求明细（ready, coverage=None）时计算比例。
        if not (bundle.get("state") == "partial" and not value.get("coverage_pct")):
            known_inc = Decimal(str(value.get("known_amount") or 0))
    ratio = None
    if (can_cost and can_contract and not contract_incomplete and contract_amount
            and contract_amount > 0 and known_inc is not None):
        ratio = (known_inc / contract_amount * Decimal("100")).quantize(Decimal("0.1"))
    status_value = card_status(ratio) if (can_cost and can_contract) else None
    if (status_value == "normal" and bundle is not None
            and (bundle.get("value") or {}).get("quality") == "incomplete"):
        # 已知下限低于 80% 并不能证明项目正常；补齐缺价后可能直接越线。
        status_value = None
    return {
        # XSDD 销售订单号即归属判定依据（#45）；多合同项目返回多个
        "contract_nos": contract_nos,
        # 2026-08-20 修复：此处曾误填 cmo_name（张冠李戴）。项目经理 =
        # project_manager_id 解析出的账号显示名（无账号回退原值）。
        "project_manager": manager_display or (
            getattr(project, "project_manager_id", None) if project is not None else None),
        # 2026-08-21 客户反馈：卡片改显销售（台账 salesperson 优先，XSDD 众数兜底）；
        # project_manager 字段保留给老消费方兼容，前端不再展示。
        "salesperson": salesperson,
        "contract_amount_inc_tax": (
            restricted()
            if not can_contract
            else (partial(contract_amount) if contract_incomplete
                  else ready(contract_amount))
        ),
        # #51 诚实标注：XSDD 回退层的共用单/缺单提示（台账层恒 false）
        "contract_shared": bool((contracts or {}).get("contract_shared")),
        "contract_incomplete": contract_incomplete,
        "known_apply_cost_ex_tax": (
            (cost_ex or _bundle_from_row(0, 0, 0, 0, 0))
            if wbdd_ready and can_cost
            else (restricted() if not can_cost else not_imported())),
        "procured_qty": (ready(procured if procured is not None else None)
                         if wbdd_ready and project is not None else not_imported()),
        # 2026-08-22 客户反馈：报销/已领用成本上卡（金额位，成本权限门控，
        # 无权限 restricted 不泄露）
        "expense_cost_inc_tax": money_cost(expense_cost),
        "requisition_cost_inc_tax": money_cost(requisition_cost),
        "collection_preview_inc_tax": money_contract(collected),
        "cost_ratio_pct": (
            restricted() if not (can_cost and can_contract) else ready(ratio)
        ),
        # 三态只由成本率决定（#43）；算不出来是 None，前端显示「数据不足」
        "card_status": status_value,
    }


def _pre_delivery_counts(db: Session, project_ids: list[str]) -> dict:
    """预交付单计数（方案 B 徽标）：project_raw 带前缀的已归属单。"""
    if not project_ids:
        return {}
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    statement = (
        select(MaintenanceSourceOrderAssignment.project_id,
               func.count(FMaintenanceOrder.id))
        .select_from(FMaintenanceOrder)
        .join(MaintenanceSourceOrderAssignment, active)
        .where(MaintenanceSourceOrderAssignment.project_id.in_(project_ids),
               FMaintenanceOrder.project_raw.op("~")(r"^预交付[-—－–]"))
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    )
    rows = db.execute(active_orders(statement, FMaintenanceOrder)).all()
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


def project_exists(db: Session, *, project_id: str) -> bool:
    """项目主档是否存在（含归档）。不存在的 id 一律 404，不返回空列表冒充成功。"""
    return db.execute(
        select(func.count(MaintenanceProject.project_id))
        .where(MaintenanceProject.project_id == project_id)
    ).scalar_one() > 0


def order_exists(db: Session, *, source_order_id: str) -> bool:
    return db.execute(
        select(func.count(FMaintenanceOrder.id))
        .where(FMaintenanceOrder.raw_order_id == source_order_id)
    ).scalar_one() > 0


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
        .where(FMaintenanceLine.order_id == order.id,
               FMaintenanceLine.is_active.is_(True))).scalar_one())
    rows = db.execute(
        select(FMaintenanceLine).where(FMaintenanceLine.order_id == order.id,
                                       FMaintenanceLine.is_active.is_(True))
        .order_by(FMaintenanceLine.line_no, FMaintenanceLine.raw_line_id)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    line_ids = [line.id for line in rows]
    overrides = {
        override.line_id: override
        for override in db.scalars(
            select(MaintenanceManualCostOverride).where(
                MaintenanceManualCostOverride.line_id.in_(line_ids or {-1}),
                MaintenanceManualCostOverride.active.is_(True),
            )
        )
    }
    pools = maintenance_boss_facts.pool_membership(
        db, {ln.pn_std for ln in rows if ln.pn_std})
    # 认不出型号时不断言「不在池」（铁律 5）：in_pool=None 表示无法判断
    no_pool = {"in_pool": False, "pool_name": None, "pool_status": None}
    unknown_pool = {"in_pool": None, "pool_name": None, "pool_status": None}
    out = []
    for ln in rows:
        override = overrides.get(ln.id)
        inc = maintenance_cost_quality.normalized_line_cost(
            source=ln.cost_source,
            tax_basis=ln.cost_tax_basis,
            legacy_amount=ln.cost_amount,
            normalized_amount=ln.cost_amount_inc_tax,
            normalized_basis="inc",
            anomaly_flags=ln.anomaly_flags,
            qty=ln.qty,
            return_qty=ln.return_qty,
            manual_unit_cost=(override.unit_cost_inc_tax if override else None),
            manual_active=override is not None,
        )
        ex = maintenance_cost_quality.normalized_line_cost(
            source=ln.cost_source,
            tax_basis=ln.cost_tax_basis,
            legacy_amount=ln.cost_amount,
            normalized_amount=ln.cost_amount_ex_tax,
            normalized_basis="ex",
            anomaly_flags=ln.anomaly_flags,
            qty=ln.qty,
            return_qty=ln.return_qty,
            manual_unit_cost=(override.unit_cost_ex_tax if override else None),
            manual_active=override is not None,
        )
        resolved_source = inc["source"] if inc["tier"] != "missing" else None

        def cost_stat(value, tier):
            if not can_cost:
                return restricted()
            envelope = ready(value)
            if tier == "missing":
                envelope["state"] = "partial"
            return envelope

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
                cost_stat(inc["amount"], inc["tier"])),
            "unit_cost_ex_tax": cost_stat(
                (override.unit_cost_ex_tax if ex["source"] == "manual" and override
                 else ln.unit_cost_ex_tax if ex["tier"] != "missing" else None),
                ex["tier"],
            ),
            "unit_cost_inc_tax": cost_stat(
                (override.unit_cost_inc_tax if inc["source"] == "manual" and override
                 else ln.unit_cost_inc_tax if inc["tier"] != "missing" else None),
                inc["tier"],
            ),
            "cost_source": cost_stat(resolved_source, inc["tier"]),
            "confidence": cost_stat(
                "high" if resolved_source == "manual" else ln.confidence,
                inc["tier"],
            ),
        })
    return {"rows": out, "total": total, "page": page, "page_size": page_size}
