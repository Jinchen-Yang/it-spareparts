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

from sqlalchemy import Select, String, func, or_, select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.inventory import PartPool, PartPoolMember
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
    """已应用批次的维保供货 CKD 明细（项目经 WBDD 单号 → 活跃归属解析）。

    发货单无项目名列，WBDD 单号是唯一可靠路径（事实档案 §2.1）；映射走
    wbdd_project_map() 的去重子查询，避免同号多单造成的扇出重复计数。
    """
    project_map = (
        select(
            FMaintenanceOrder.order_no.label("wbdd_no"),
            func.min(MaintenanceSourceOrderAssignment.project_id).label("project_id"),
        )
        .join(MaintenanceSourceOrderAssignment,
              (MaintenanceSourceOrderAssignment.source_order_id
               == FMaintenanceOrder.raw_order_id)
              & MaintenanceSourceOrderAssignment.is_active.is_(True))
        .group_by(FMaintenanceOrder.order_no)
        .having(func.count(func.distinct(
            MaintenanceSourceOrderAssignment.project_id)) == 1)
        .subquery()
    )
    # 跨批次去重：raw 暂存行的主键是每批新生成的 row_id，同一发货明细若出现在两个
    # applied 批次（换 Idempotency-Key 重传、周期导出重叠）会被重复求和，看板实发翻倍，
    # 而 front_stock 账本因 (source_type, source_ref) 幂等并不会重复入账 —— 两边永久
    # 不一致。这里按业务键 (出库单号, 明细数据ID/行号) 只取最新 applied 批次的那一行。
    dedup = (
        select(
            MaintenanceCkdHeadRow.order_no.label("order_no"),
            MaintenanceCkdHeadRow.wbdd_no.label("wbdd_no"),
            func.coalesce(MaintenanceCkdLineRow.data_id_raw,
                          MaintenanceCkdLineRow.row_no.cast(String)).label("line_key"),
            MaintenanceCkdLineRow.pn_raw.label("pn"),
            MaintenanceCkdLineRow.out_qty.label("out_qty"),
            func.row_number().over(
                partition_by=(
                    MaintenanceCkdHeadRow.order_no,
                    func.coalesce(MaintenanceCkdLineRow.data_id_raw,
                                  MaintenanceCkdLineRow.row_no.cast(String)),
                ),
                order_by=MaintenanceCkdImportBatch.applied_at.desc(),
            ).label("rn"),
        )
        .join(MaintenanceCkdHeadRow,
              MaintenanceCkdHeadRow.row_id == MaintenanceCkdLineRow.head_row_id)
        .join(MaintenanceCkdImportBatch,
              MaintenanceCkdImportBatch.batch_id == MaintenanceCkdHeadRow.batch_id)
        .where(MaintenanceCkdImportBatch.status == "applied",
               MaintenanceCkdHeadRow.category == "维保供货",
               # 作废/草稿/已取消的发货单不是实发事实：与导入 apply 的判定同形
               # （services/maintenance_ckd_import.py:451 只放行「已生效」）
               or_(MaintenanceCkdHeadRow.data_status_raw.is_(None),
                   MaintenanceCkdHeadRow.data_status_raw == "已生效"),
               MaintenanceCkdLineRow.out_qty.is_not(None))
        .subquery()
    )
    return (
        select(
            project_map.c.project_id.label("project_id"),
            dedup.c.pn.label("pn"),
            func.sum(dedup.c.out_qty).label("qty"),
        )
        .select_from(dedup)
        .join(project_map, project_map.c.wbdd_no == dedup.c.wbdd_no)
        .where(dedup.c.rn == 1)
        .group_by(project_map.c.project_id, dedup.c.pn)
    )


def _applied_return_lines() -> Select:
    """已应用批次的返库单成品明细（未用件收回）。

    与 CKD 同样按业务键 (返库单号, 明细键) 跨批次去重（理由见 _applied_ckd_lines），
    并只认「已生效」头（与 services/maintenance_doc_import.py:523 的 apply 判定同形）。
    """
    dedup = (
        select(
            MaintenanceDocHeadRow.project_id.label("project_id"),
            MaintenanceDocLineRow.pn.label("pn"),
            MaintenanceDocLineRow.qty.label("qty"),
            func.row_number().over(
                partition_by=(
                    MaintenanceDocHeadRow.head_no,
                    func.coalesce(MaintenanceDocLineRow.line_key,
                                  MaintenanceDocLineRow.row_no.cast(String)),
                ),
                order_by=MaintenanceDocImportBatch.applied_at.desc(),
            ).label("rn"),
        )
        .join(MaintenanceDocHeadRow,
              MaintenanceDocHeadRow.row_id == MaintenanceDocLineRow.head_row_id)
        .join(MaintenanceDocImportBatch,
              MaintenanceDocImportBatch.batch_id == MaintenanceDocHeadRow.batch_id)
        .where(MaintenanceDocImportBatch.doc_type == "return_order",
               MaintenanceDocImportBatch.status == "applied",
               MaintenanceDocHeadRow.project_id.is_not(None),
               or_(MaintenanceDocHeadRow.data_status.is_(None),
                   MaintenanceDocHeadRow.data_status == "已生效"),
               MaintenanceDocLineRow.test_result.in_(GOOD_RETURN_RESULTS),
               MaintenanceDocLineRow.qty.is_not(None))
        .subquery()
    )
    return (
        select(dedup.c.project_id, dedup.c.pn, func.sum(dedup.c.qty).label("qty"))
        .select_from(dedup)
        .where(dedup.c.rn == 1)
        .group_by(dedup.c.project_id, dedup.c.pn)
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


def pool_membership(db: Session, pn_stds: set[str]) -> dict[str, dict]:
    """PN → 互通池归属（plan §4.5 pool 列）。一次批量查询，无 N+1。

    一个 PN 只能属于一个**有效**池，但归档池保留成员集合，所以同一 PN 可能同时
    出现在若干归档池行里（models/inventory.py:123 注释）。取值优先有效池，其次
    最近更新的归档池——归档池在前端是黄色警示，不能被别的归档行盖掉。

    并入他档的 PN（status='merged'）要同时看**自己**和**主档**两条线：
    services/merge.py 只 repoint 事实表与别名，**不搬 part_pool_member**（该表是
    复合主键 (group_id, part_id)），所以并档后成员行还挂在源 part 上。只查主档会
    把源 PN 已有的池归属抹成「不在池」，只查源又会漏掉并档后才建的池。
    """
    if not pn_stds:
        return {}
    parts = db.execute(
        select(DimPart.id, DimPart.pn_std, DimPart.status, DimPart.merged_into_id)
        .where(DimPart.pn_std.in_(pn_stds))
    ).all()
    if not parts:
        return {}

    def _targets(part) -> tuple[int, ...]:
        if part.status == "merged" and part.merged_into_id:
            return (part.id, part.merged_into_id)
        return (part.id,)

    lookup_ids = {pid for part in parts for pid in _targets(part)}
    memberships = db.execute(
        select(PartPoolMember.part_id, PartPool.name, PartPool.status,
               PartPool.updated_at)
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .where(PartPoolMember.part_id.in_(lookup_ids))
    ).all()

    def _rank(row) -> tuple:
        # 有效池优先；同档次内取最近更新的（时间戳取负，越新排越前）
        return (0 if row.status == "active" else 1,
                -(row.updated_at.timestamp() if row.updated_at else 0))

    best: dict[int, tuple] = {}
    for row in memberships:
        key = _rank(row)
        if row.part_id not in best or key < best[row.part_id][0]:
            best[row.part_id] = (key, row)
    out: dict[str, dict] = {}
    for part in parts:
        hits = [best[pid] for pid in _targets(part) if pid in best]
        hit = min(hits, key=lambda item: item[0])[1] if hits else None
        out[part.pn_std] = (
            {"in_pool": True, "pool_name": hit.name, "pool_status": hit.status}
            if hit else {"in_pool": False, "pool_name": None, "pool_status": None}
        )
    return out


def line_evidence(db: Session, *, source_order_id: str) -> list[dict]:
    """PN 证据行：需求明细 + 全部流转状态列**原样**（铁律 3：不计算、不标注）。"""
    rows = db.execute(
        select(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .where(FMaintenanceOrder.raw_order_id == source_order_id,
               FMaintenanceLine.is_active.is_(True))
        .order_by(FMaintenanceLine.line_no, FMaintenanceLine.raw_line_id)
    ).scalars().all()
    pools = pool_membership(db, {ln.pn_std for ln in rows if ln.pn_std})
    # 未标准化的 PN 认不出型号，"不在任何池"是句没有依据的断言（铁律 5）：
    # in_pool 留 None 表示「无法判断」，与"确实不在池"（False）区分开。
    unknown_pool = {"in_pool": None, "pool_name": None, "pool_status": None}
    return [
        {
            "pool": (pools.get(ln.pn_std, {"in_pool": False, "pool_name": None,
                                           "pool_status": None})
                     if ln.pn_std else dict(unknown_pool)),
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
