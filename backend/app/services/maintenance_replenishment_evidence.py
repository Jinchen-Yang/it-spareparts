"""补库购物车增强证据 + 审核导出（F2）。

口径（十二问 Q7）：购物车 = WBDD 采购申请的前置审核；只审 PN 合理性；
审核通过后一键导出四列 Excel（PN/数量/采购金额/销售金额），后续人工渠道。
增强证据（业务 2026-08-15 确认）：
- 365 天无采购/销售记录 → 提醒（证明不常用）；
- 通用池替代建议（part_pool 成员）；
- 成本区间（近半年采购/销售数量加权 + CKD 发货单成本）；
- 高频常用件（WBDD 补库供货按 PN 频次）。
"""
from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal

from openpyxl import Workbook
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.inventory import PartPool, PartPoolMember
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.replenishment import (
    ReplenishmentApplication,
    ReplenishmentApplicationLine,
    ReplenishmentApplicationVersion,
)
from app.models.sales import FSalesLine, FSalesOrder
from app.services import pool_price_analysis

INACTIVE_WINDOW_DAYS = 365
PRICE_WINDOW_DAYS = 180
HIGH_FREQUENCY_THRESHOLD = 50  # 补库供货近一年累计数量阈值


def line_evidence(db: Session, line: ReplenishmentApplicationLine) -> dict:
    """单行增强证据（读实时事实表，不改变冻结版本）。"""
    part = db.get(DimPart, line.part_id)
    today = business_today()
    if part is None:
        return {"pn_std": line.pn_std, "error": "PN 不存在"}

    last_purchase = db.scalar(
        select(func.max(FPurchaseOrder.order_date))
        .join(FPurchaseLine, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.part_id == part.id)
    )
    last_sales = db.scalar(
        select(func.max(FSalesOrder.order_date))
        .join(FSalesLine, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.part_id == part.id)
    )
    inactive_days = None
    inactive_side: list[str] = []
    for side, last_date in (("purchase", last_purchase), ("sales", last_sales)):
        if last_date is None or (today - last_date).days > INACTIVE_WINDOW_DAYS:
            inactive_side.append(side)
    if inactive_side:
        inactive_days = (
            min(
                (today - d).days
                for d in (last_purchase, last_sales)
                if d is not None
            )
            if last_purchase is not None or last_sales is not None
            else None
        )

    # 通用池替代建议：同池其他成员（含本 PN 时排除自身）
    alternatives: list[dict] = []
    if line.pool_group_id is not None:
        members = db.execute(
            select(PartPoolMember, DimPart.pn_std, DimPart.description)
            .join(DimPart, DimPart.id == PartPoolMember.part_id)
            .where(
                PartPoolMember.group_id == line.pool_group_id,
                PartPoolMember.part_id != part.id,
            )
            .limit(10)
        ).all()
        for member, pn, description in members:
            alternatives.append(
                {"part_id": member.part_id, "pn_std": pn, "description": description}
            )

    # 高频常用件：近一年补库供货累计数量
    recent_total = db.scalar(
        select(func.coalesce(func.sum(FMaintenanceLine.qty), 0))
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .where(
            FMaintenanceLine.part_id == part.id,
            FMaintenanceOrder.demand_type == "补库供货",
            FMaintenanceOrder.order_date >= today - timedelta(days=INACTIVE_WINDOW_DAYS),
        )
    )
    is_high_frequency = bool(
        recent_total is not None
        and Decimal(str(recent_total)) >= Decimal(HIGH_FREQUENCY_THRESHOLD)
    )

    # 成本区间（近半年采购/销售 + CKD 发货单成本）
    facts = pool_price_analysis.aggregate_part_price_facts(
        db,
        [part.id],
        date_from=today - timedelta(days=PRICE_WINDOW_DAYS - 1),
        date_to=today,
    )
    stats = facts.get(part.id, {"purchase": None, "sales": None})
    ckd_costs = db.execute(
        select(FMaintenanceLine.unit_cost)
        .where(
            FMaintenanceLine.part_id == part.id,
            FMaintenanceLine.unit_cost.is_not(None),
        )
        .limit(500)
    ).scalars().all()

    return {
        "pn_std": part.pn_std,
        "description": part.description,
        "pool_group_id": line.pool_group_id,
        "pool_name": line.pool_name,
        "last_purchase_date": last_purchase.isoformat() if last_purchase else None,
        "last_sales_date": last_sales.isoformat() if last_sales else None,
        "inactive_365d": bool(inactive_side),
        "inactive_sides": inactive_side,
        "inactive_days": inactive_days,
        "recent_supply_qty": float(recent_total or 0),
        "is_high_frequency": is_high_frequency,
        "pool_alternatives": alternatives,
        "purchase_stats": _stats_payload(stats.get("purchase")),
        "sales_stats": _stats_payload(stats.get("sales")),
        "ckd_unit_cost_min": min(ckd_costs) if ckd_costs else None,
        "ckd_unit_cost_max": max(ckd_costs) if ckd_costs else None,
    }


def _stats_payload(value: dict | None) -> dict | None:
    if not value:
        return None
    return {
        "weighted_avg": value.get("weighted_avg"),
        "total_qty": value.get("total_qty"),
        "order_count": value.get("order_count"),
    }


def _latest_lines(
    db: Session, application: ReplenishmentApplication
) -> list[ReplenishmentApplicationLine]:
    """申请最新版本（application.latest_version_no）对应的行，按行号排序。"""
    version_id = db.scalar(
        select(ReplenishmentApplicationVersion.version_id).where(
            ReplenishmentApplicationVersion.application_id
            == application.application_id,
            ReplenishmentApplicationVersion.version_no
            == application.latest_version_no,
        )
    )
    if version_id is None:
        return []
    return list(
        db.execute(
            select(ReplenishmentApplicationLine)
            .where(ReplenishmentApplicationLine.version_id == version_id)
            .order_by(ReplenishmentApplicationLine.line_no)
        ).scalars()
    )


def application_evidence(db: Session, application_id: str) -> dict:
    """整单逐行增强证据（审核辅助）。"""
    application = db.get(ReplenishmentApplication, application_id)
    if application is None:
        return {"error": "申请不存在"}
    lines = _latest_lines(db, application)
    return {
        "application_id": application_id,
        "lines": [line_evidence(db, line) for line in lines],
    }


def export_purchase_list(db: Session, application_id: str) -> bytes:
    """审核通过后的四列导出：PN / 数量 / 采购金额 / 销售金额（参考口径）。"""
    application = db.get(ReplenishmentApplication, application_id)
    if application is None:
        raise ValueError("申请不存在")
    lines = _latest_lines(db, application)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "补库采购清单"
    sheet.append(["PN", "数量", "采购金额(参考)", "销售金额(参考)", "备注"])
    for line in lines:
        purchase = (line.purchase_stats_json or {}).get("weighted_avg")
        sales = (line.sales_stats_json or {}).get("weighted_avg")
        sheet.append(
            [
                line.pn_std,
                float(line.quantity),
                round(float(purchase) * float(line.quantity), 2)
                if purchase is not None
                else "",
                round(float(sales) * float(line.quantity), 2)
                if sales is not None
                else "",
                line.special_note or "",
            ]
        )
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
