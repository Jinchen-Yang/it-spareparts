"""项目＋PN 事实聚合读模型（plan v1.3 M4-2/M4-4，M0-D 建议默认粒度）。

粒度依据（事实档案 §4 复审核验）：CKD/RKD 明细无需求单行级键，需求单重复 PN
无法行级分配 → v1 按 (project_id, pn) 聚合，不做明细行分配。

项目解析（严格按各源可靠路径）：
- CKD 发货单：**无项目名列**，唯一可靠路径 = WBDD 单号 → f_maintenance_order →
  活跃 assignment → project_id（事实档案 §2.1，99.8% 命中）；
- return_order 返库单：沿用导入期 _resolve_project_id 落在 head.project_id 上的结果
  （wbdd_no → assignment / xsdd_no → 合同 / project_name → project_code 三级）；
- rkd_inbound：maintenance_rkd_return_line.project_id（导入期已 fail-closed 解析）。

未关联行不摊进任何项目、也不丢：计入 unlinked_rows，由 /health 与列表 partial 态透出。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_ckd_import import (
    MaintenanceCkdHeadRow,
    MaintenanceCkdImportBatch,
    MaintenanceCkdLineRow,
)
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
    MaintenanceDocLineRow,
    MaintenanceRkdReturnLine,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment

# 未用件收回口径（铁律 5）：返库单只认成品；坏品/废品属坏件回收（走 RKD）
GOOD_RETURN_RESULTS = ("成品",)


def _applied_ckd_lines() -> Select:
    """已应用批次的维保供货 CKD 明细（项目经 WBDD 单号 → 活跃归属解析）。"""
    return (
        select(
            MaintenanceSourceOrderAssignment.project_id.label("project_id"),
            MaintenanceCkdLineRow.pn_raw.label("pn"),
            func.sum(MaintenanceCkdLineRow.out_qty).label("qty"),
        )
        .join(MaintenanceCkdHeadRow,
              MaintenanceCkdHeadRow.row_id == MaintenanceCkdLineRow.head_row_id)
        .join(MaintenanceCkdImportBatch,
              MaintenanceCkdImportBatch.batch_id == MaintenanceCkdHeadRow.batch_id)
        .join(FMaintenanceOrder,
              FMaintenanceOrder.order_no == MaintenanceCkdHeadRow.wbdd_no)
        .join(MaintenanceSourceOrderAssignment,
              (MaintenanceSourceOrderAssignment.source_order_id
               == FMaintenanceOrder.raw_order_id)
              & MaintenanceSourceOrderAssignment.is_active.is_(True))
        .where(MaintenanceCkdImportBatch.status == "applied",
               MaintenanceCkdHeadRow.category == "维保供货",
               MaintenanceCkdLineRow.out_qty.is_not(None))
        .group_by(MaintenanceSourceOrderAssignment.project_id,
                  MaintenanceCkdLineRow.pn_raw)
    )


def _applied_return_lines() -> Select:
    """已应用批次的返库单成品明细（未用件收回）。"""
    return (
        select(
            MaintenanceDocHeadRow.project_id.label("project_id"),
            MaintenanceDocLineRow.pn.label("pn"),
            func.sum(MaintenanceDocLineRow.qty).label("qty"),
        )
        .join(MaintenanceDocHeadRow,
              MaintenanceDocHeadRow.row_id == MaintenanceDocLineRow.head_row_id)
        .join(MaintenanceDocImportBatch,
              MaintenanceDocImportBatch.batch_id == MaintenanceDocHeadRow.batch_id)
        .where(MaintenanceDocImportBatch.doc_type == "return_order",
               MaintenanceDocImportBatch.status == "applied",
               MaintenanceDocHeadRow.project_id.is_not(None),
               MaintenanceDocLineRow.test_result.in_(GOOD_RETURN_RESULTS),
               MaintenanceDocLineRow.qty.is_not(None))
        .group_by(MaintenanceDocHeadRow.project_id, MaintenanceDocLineRow.pn)
    )


def _applied_rkd_lines() -> Select:
    """坏件回收规范事实（导入期已按返件类白名单 + 坏品/废品枚举 fail-closed）。"""
    return (
        select(
            MaintenanceRkdReturnLine.project_id.label("project_id"),
            MaintenanceRkdReturnLine.pn.label("pn"),
            func.sum(MaintenanceRkdReturnLine.qty).label("qty"),
        )
        .group_by(MaintenanceRkdReturnLine.project_id, MaintenanceRkdReturnLine.pn)
    )


def project_pn_facts(db: Session, *, project_ids: list[str] | None = None) -> dict:
    """返回 {project_id: {pn: {shipped, returned_good, returned_bad}}}。

    只读聚合；空源返回空 dict（调用方据 /health 的 readiness 决定显示
    not_imported 而非 0）。
    """
    out: dict[str, dict[str, dict[str, Decimal]]] = {}
    for key, stmt in (
        ("shipped", _applied_ckd_lines()),
        ("returned_good", _applied_return_lines()),
        ("returned_bad", _applied_rkd_lines()),
    ):
        rows = db.execute(stmt).all()
        for project_id, pn, qty in rows:
            if project_ids is not None and project_id not in project_ids:
                continue
            if not project_id or not pn:
                continue
            bucket = out.setdefault(project_id, {}).setdefault(
                pn, {"shipped": None, "returned_good": None, "returned_bad": None})
            bucket[key] = qty
    return out


def project_totals(db: Session, *, project_ids: list[str] | None = None) -> dict:
    """项目级卷积（列表用）：{project_id: {shipped, returned_good, returned_bad}}。"""
    facts = project_pn_facts(db, project_ids=project_ids)
    totals: dict[str, dict[str, Decimal | None]] = {}
    for project_id, pn_map in facts.items():
        agg: dict[str, Decimal | None] = {
            "shipped": None, "returned_good": None, "returned_bad": None}
        for values in pn_map.values():
            for key, value in values.items():
                if value is None:
                    continue
                agg[key] = (agg[key] or Decimal(0)) + value
        totals[project_id] = agg
    return totals


def order_self_report_and_facts(db: Session, *, source_order_id: str) -> dict:
    """单据级：自报四列与三源事实**无判定并排**（M4-4）。

    铁律 3 字面：服务端不产出任何 mismatch/差异字段；肉眼对比由并排布局完成。
    （若业务确需差异提示，须先经 M0 附带项 F5 书面豁免。）
    """
    order = db.execute(
        select(FMaintenanceOrder)
        .where(FMaintenanceOrder.raw_order_id == source_order_id)
    ).scalar_one_or_none()
    if order is None:
        return {}
    assignment = db.execute(
        select(MaintenanceSourceOrderAssignment.project_id)
        .where(MaintenanceSourceOrderAssignment.source_order_id == source_order_id,
               MaintenanceSourceOrderAssignment.is_active.is_(True))
    ).scalar_one_or_none()
    facts = {"shipped": None, "returned_good": None, "returned_bad": None}
    if assignment:
        totals = project_totals(db, project_ids=[assignment])
        facts = totals.get(assignment, facts)
    return {
        "source_order_id": order.raw_order_id,
        "order_no": order.order_no,
        # 自报四列（M1 展示列）：原样返回，系统不做任何判定
        "self_report": {
            "head_demand_qty": order.head_demand_qty,
            "head_purchase_qty": order.head_purchase_qty,
            "head_shipped_qty": order.head_shipped_qty,
            "head_returned_qty": order.head_returned_qty,
        },
        # 事实（项目级卷积，M0-D 聚合粒度下单据级不做行级分配）
        "facts": facts,
        "facts_scope": "project" if assignment else None,
    }


def line_evidence(db: Session, *, source_order_id: str) -> list[dict]:
    """PN 证据行：需求明细 + 全部流转状态列**原样**（铁律 3：不计算、不标注）。"""
    rows = db.execute(
        select(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .where(FMaintenanceOrder.raw_order_id == source_order_id)
        .order_by(FMaintenanceLine.line_no, FMaintenanceLine.raw_line_id)
    ).scalars().all()
    return [
        {
            "raw_line_id": ln.raw_line_id,
            "pn_std": ln.pn_std, "pn_raw": ln.pn_raw,
            "description": ln.description,
            "qty": ln.qty, "return_qty": ln.return_qty,
            # 流转状态列原样展示（14 列）
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
        }
        for ln in rows
    ]
