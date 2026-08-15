"""项目结束收回清单（F4）。

口径（2026-08-15 确认 + Q8）：
- 好件/未用件收回 = 氚云退货返库单（return_order），apply 时对前置库 return_out 入账；
- 坏件/故障件返还 = 氚云收货入库单（RKD），坏品明细是消耗返还事实
  （maintenance_rkd_return_line），不扣前置库账本；
- 未收回件 = 前置库当前结存（发货单入账后尚未被返库单收回的部分）。
本服务纯读模型：从已入账事实聚合，不新建状态、不写库。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_front_stock import MaintenanceFrontStockLedger
from app.services import maintenance_front_stock as front_stock


def recovery_summary(db: Session, project_id: str) -> dict:
    """项目收回清单：好件收回 / 坏件返还 / 未收回结存。"""
    good = db.execute(
        select(MaintenanceFrontStockLedger)
        .where(
            MaintenanceFrontStockLedger.project_id == project_id,
            MaintenanceFrontStockLedger.kind == "return_out",
            MaintenanceFrontStockLedger.source_type == "return_order_line",
        )
        .order_by(MaintenanceFrontStockLedger.occurred_at.desc().nulls_last())
    ).scalars().all()
    bad = db.execute(
        select(MaintenanceRkdReturnLine)
        .where(MaintenanceRkdReturnLine.project_id == project_id)
        .order_by(
            MaintenanceRkdReturnLine.occurred_at.desc().nulls_last(),
            MaintenanceRkdReturnLine.head_no,
        )
    ).scalars().all()
    remaining = [
        row
        for row in front_stock.balance_rows(db, project_id)
        if row["qty"] > 0
    ]
    return {
        "project_id": project_id,
        "good_returned": [
            {
                "source_ref": row.source_ref,
                "qty": float(-row.qty_change),
                "occurred_at": row.occurred_at.isoformat()
                if row.occurred_at
                else None,
                "reason": row.reason,
            }
            for row in good
        ],
        "good_returned_total_qty": round(
            sum(float(-row.qty_change) for row in good), 3
        ),
        "bad_returned": [
            {
                "head_no": row.head_no,
                "pn": row.pn,
                "part_id": row.part_id,
                "qty": float(row.qty),
                "test_result": row.test_result,
                "occurred_at": row.occurred_at.isoformat()
                if row.occurred_at
                else None,
            }
            for row in bad
        ],
        "bad_returned_total_qty": round(sum(float(row.qty) for row in bad), 3),
        "remaining_stock": remaining,
        "remaining_total_qty": round(sum(row["qty"] for row in remaining), 3),
    }
