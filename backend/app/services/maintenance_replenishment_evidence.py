"""补库购物车增强证据 + 审核导出（F2，round-4 审核修复版）。

口径（十二问 Q7 + PRD §19 + Codex round-4 Blocker 1/2/8/9）：
- 两端点仅申请 owner（或 admin）可见：复用 replenishment._application_scope，
  非 owner 与不存在同 404；
- 仅 status=approved 的申请可查看证据与导出（与 WBDD 子集导出同口径，否则 409）；
- 导出 = 跨版本累计批准意向（request_line_id 稳定键，后批准的覆盖前版），
  精确四列（PN/数量/采购金额(参考)/销售金额(参考)），值经 _excel_safe 转义；
- 成本区间 = 已应用 CKD 发货单 unit_cost（C1 事实）+ 近半年采购/销售聚合
  （min/max/latest/latest_date 全量输出）；
- 有效事实过滤：采购/销售/补库供货只认 ACTIVE_STATUS(已生效) 且日期不晚于业务日；
  替代件过滤 excluded/merged/inactive。
"""
from __future__ import annotations

import io
from datetime import timedelta

from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.config import ACTIVE_STATUS
from app.models.dimensions import DimPart
from app.models.inventory import PartPool, PartPoolMember
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_ckd_import import (
    MaintenanceCkdHeadRow,
    MaintenanceCkdImportBatch,
    MaintenanceCkdLineRow,
)
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.replenishment import (
    ReplenishmentApplication,
    ReplenishmentApplicationLine,
)
from app.models.sales import FSalesLine, FSalesOrder
from app.services import pool_price_analysis
from app.services import replenishment as replenishment_service

INACTIVE_WINDOW_DAYS = 365
PRICE_WINDOW_DAYS = 180
HIGH_FREQUENCY_THRESHOLD = 50  # 补库供货近一年累计数量阈值（待业务确认的参考值）


def _scoped_application(
    db: Session, application_id: str, *, username: str, role: str
) -> ReplenishmentApplication:
    """owner/admin 作用域（非 owner 与不存在同为 404）。"""
    return replenishment_service._application_scope(
        db, application_id, username=username, role=role
    )


def _require_approved(application: ReplenishmentApplication) -> None:
    if application.status != "approved":
        raise replenishment_service.ReplenishmentError(
            "全部条目通过审核后才能查看证据或导出采购清单",
            code="invalid_state",
            status_code=409,
        )


def line_evidence(db: Session, line: ReplenishmentApplicationLine) -> dict:
    """单行增强证据（读实时有效事实，不改变冻结版本）。"""
    part = db.get(DimPart, line.part_id)
    today = business_today()
    if part is None:
        return {"pn_std": line.pn_std, "error": "PN 不存在"}

    # 有效事实：仅已生效且不晚于业务日
    last_purchase = db.scalar(
        select(func.max(FPurchaseOrder.order_date))
        .join(FPurchaseLine, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(
            FPurchaseLine.part_id == part.id,
            FPurchaseOrder.data_status == ACTIVE_STATUS,
            FPurchaseOrder.order_date <= today,
            FPurchaseLine.qty > 0,
        )
    )
    last_sales = db.scalar(
        select(func.max(FSalesOrder.order_date))
        .join(FSalesLine, FSalesLine.order_id == FSalesOrder.id)
        .where(
            FSalesLine.part_id == part.id,
            FSalesOrder.data_status == ACTIVE_STATUS,
            FSalesOrder.order_date <= today,
            FSalesLine.qty > 0,
        )
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

    # 通用池替代建议：仅 active 池、非 excluded/merged 的有效 PN
    alternatives: list[dict] = []
    if line.pool_group_id is not None:
        members = db.execute(
            select(PartPoolMember.part_id, DimPart.pn_std, DimPart.description)
            .join(DimPart, DimPart.id == PartPoolMember.part_id)
            .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
            .where(
                PartPoolMember.group_id == line.pool_group_id,
                PartPoolMember.part_id != part.id,
                PartPool.status == "active",
                DimPart.is_excluded.is_(False),
                DimPart.merged_into_id.is_(None),
            )
            .limit(10)
        ).all()
        for member_part_id, pn, description in members:
            alternatives.append(
                {"part_id": member_part_id, "pn_std": pn, "description": description}
            )

    # 高频常用件：近一年已生效补库供货累计数量（不晚于业务日）
    recent_total = db.scalar(
        select(func.coalesce(func.sum(FMaintenanceLine.qty), 0))
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .where(
            FMaintenanceLine.part_id == part.id,
            FMaintenanceOrder.demand_type == "补库供货",
            FMaintenanceOrder.data_status == ACTIVE_STATUS,
            FMaintenanceOrder.order_date >= today - timedelta(days=INACTIVE_WINDOW_DAYS),
            FMaintenanceOrder.order_date <= today,
            FMaintenanceLine.qty > 0,
            FMaintenanceLine.is_active.is_(True),
        )
    )
    is_high_frequency = bool(
        recent_total is not None and float(recent_total) >= HIGH_FREQUENCY_THRESHOLD
    )

    # 成本区间（近半年采购/销售聚合 + 已应用 CKD 发货单成本）
    facts = pool_price_analysis.aggregate_part_price_facts(
        db,
        [part.id],
        date_from=today - timedelta(days=PRICE_WINDOW_DAYS - 1),
        date_to=today,
    )
    stats = facts.get(part.id, {"purchase": None, "sales": None})
    ckd_costs = db.execute(
        select(MaintenanceCkdLineRow.unit_cost)
        .join(DimPart, DimPart.pn_std == MaintenanceCkdLineRow.pn)
        .join(
            MaintenanceCkdImportBatch,
            MaintenanceCkdImportBatch.batch_id == MaintenanceCkdLineRow.batch_id,
        )
        .join(
            MaintenanceCkdHeadRow,
            MaintenanceCkdHeadRow.row_id == MaintenanceCkdLineRow.head_row_id,
        )
        .where(
            DimPart.id == part.id,
            MaintenanceCkdLineRow.unit_cost.is_not(None),
            MaintenanceCkdLineRow.out_qty > 0,
            MaintenanceCkdImportBatch.status == "applied",
            # 有效头门：维保供货、已生效、有日期、无 issue（round-5 Blocker 12）
            MaintenanceCkdHeadRow.category == "维保供货",
            MaintenanceCkdHeadRow.data_status_raw == ACTIVE_STATUS,
            MaintenanceCkdHeadRow.order_date.is_not(None),
            MaintenanceCkdHeadRow.order_date <= today,
            func.cardinality(MaintenanceCkdHeadRow.issues) == 0,
        )
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
        "ckd_unit_cost_sample_count": len(ckd_costs),
    }


def _stats_payload(value: dict | None) -> dict | None:
    if not value:
        return None
    return {
        "weighted_avg": value.get("weighted_avg"),
        "median": value.get("median"),
        "min": value.get("min"),
        "max": value.get("max"),
        "latest": value.get("latest"),
        "latest_date": value.get("latest_date"),
        "total_qty": value.get("total_qty"),
        "order_count": value.get("order_count"),
    }


def application_evidence(
    db: Session, application_id: str, *, username: str, role: str
) -> dict:
    """整单逐行增强证据（仅 owner/admin，仅 approved 状态）。"""
    application = _scoped_application(
        db, application_id, username=username, role=role
    )
    _require_approved(application)
    lines = replenishment_service._approved_lines(db, application)
    return {
        "application_id": application_id,
        "lines": [line_evidence(db, line) for _, line in lines],
    }


def export_purchase_list(
    db: Session, application_id: str, *, username: str, role: str
) -> bytes:
    """审核通过后的精确四列导出（PN/数量/采购金额(参考)/销售金额(参考)）。

    行集 = 跨版本累计批准意向（request_line_id 稳定键取最新批准行）。
    """
    application = _scoped_application(
        db, application_id, username=username, role=role
    )
    _require_approved(application)
    approved = replenishment_service._approved_lines(db, application)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "补库采购清单"
    sheet.append(["PN", "数量", "采购金额(参考)", "销售金额(参考)"])
    for _, line in approved:
        purchase = (line.purchase_stats_json or {}).get("weighted_avg")
        sales = (line.sales_stats_json or {}).get("weighted_avg")
        sheet.append(
            [
                replenishment_service._excel_safe(line.pn_std),
                float(line.quantity),
                round(float(purchase) * float(line.quantity), 2)
                if purchase is not None
                else "",
                round(float(sales) * float(line.quantity), 2)
                if sales is not None
                else "",
            ]
        )
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
