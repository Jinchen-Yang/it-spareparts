"""四源健康（plan v1.3 M4-1）：readiness / as_of / batch，事实源严格分离。

铁律 5：实发=CKD 发货单、未用件收回=return_order 返库单(成品)、
坏件回收=rkd_inbound 入库单(坏品/废品，返件类)；各源独立 readiness；
**未导入显示 not_imported，绝不显示 0**（value 键一律缺省，不返回数字）。

readiness 状态机：
  无 applied 批次                    → not_imported
  最新 applied 批次有 issue_rows>0，
  或存在项目未解析的已应用头行        → partial
  否则                               → ready
  （stale 是展示态，由读侧按 as_of 距今 > STALE_DAYS 判定，不落库——F2 默认 45 天）
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_ckd_import import (
    MaintenanceCkdHeadRow,
    MaintenanceCkdImportBatch,
)
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
)
from app.models.maintenance_wbdd_import import MaintenanceWbddImportReceipt

# F2 建议默认：周/月上传节奏的 1.5 倍
STALE_DAYS = 45

SOURCE_KEYS = ("wbdd", "ckd", "return_order", "rkd_inbound")
SOURCE_LABELS = {
    "wbdd": "维保备件需求单",
    "ckd": "发货单（实发）",
    "return_order": "返库单（未用件收回·成品）",
    "rkd_inbound": "入库单（坏件回收·返件类）",
}


def _envelope(readiness: str, *, as_of: date | None = None,
              batch_id: str | int | None = None, uploaded_at=None,
              unlinked_rows: int = 0, label: str = "") -> dict:
    """统一信封：not_imported 时不带任何计数值（铁律 5）。"""
    out: dict = {"readiness": readiness, "label": label}
    if readiness == "not_imported":
        out.update({"as_of": None, "batch_id": None, "uploaded_at": None,
                    "unlinked_rows": None})
        return out
    if as_of is not None and readiness == "ready":
        if (business_today() - as_of) > timedelta(days=STALE_DAYS):
            out["readiness"] = "stale"
    out.update({
        "as_of": as_of.isoformat() if as_of else None,
        "batch_id": str(batch_id) if batch_id is not None else None,
        "uploaded_at": uploaded_at.isoformat() if uploaded_at else None,
        "unlinked_rows": unlinked_rows,
    })
    return out


def _wbdd_source(db: Session) -> dict:
    as_of, total = db.execute(
        select(func.max(FMaintenanceOrder.order_date),
               func.count(FMaintenanceOrder.id))
    ).one()
    if not total:
        return _envelope("not_imported", label=SOURCE_LABELS["wbdd"])
    receipt = db.execute(
        select(MaintenanceWbddImportReceipt)
        .order_by(MaintenanceWbddImportReceipt.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _envelope(
        "ready", as_of=as_of,
        batch_id=receipt.batch_id if receipt else None,
        uploaded_at=receipt.created_at if receipt else None,
        label=SOURCE_LABELS["wbdd"],
    )


def _ckd_source(db: Session) -> dict:
    batch = db.execute(
        select(MaintenanceCkdImportBatch)
        .where(MaintenanceCkdImportBatch.status == "applied")
        .order_by(MaintenanceCkdImportBatch.applied_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if batch is None:
        return _envelope("not_imported", label=SOURCE_LABELS["ckd"])
    as_of = db.execute(
        select(func.max(MaintenanceCkdHeadRow.order_date))
        .join(MaintenanceCkdImportBatch,
              MaintenanceCkdImportBatch.batch_id == MaintenanceCkdHeadRow.batch_id)
        .where(MaintenanceCkdImportBatch.status == "applied")
    ).scalar_one()
    # CKD 无项目列：未命中 WBDD 单号即无法归属（事实档案 §2.1）
    unlinked = db.execute(
        select(func.count(MaintenanceCkdHeadRow.row_id))
        .join(MaintenanceCkdImportBatch,
              MaintenanceCkdImportBatch.batch_id == MaintenanceCkdHeadRow.batch_id)
        .where(MaintenanceCkdImportBatch.status == "applied",
               MaintenanceCkdHeadRow.category == "维保供货",
               MaintenanceCkdHeadRow.wbdd_no.is_(None))
    ).scalar_one()
    readiness = "partial" if (batch.issue_rows or unlinked) else "ready"
    return _envelope(readiness, as_of=as_of, batch_id=batch.batch_id,
                     uploaded_at=batch.applied_at, unlinked_rows=int(unlinked),
                     label=SOURCE_LABELS["ckd"])


def _doc_source(db: Session, doc_type: str) -> dict:
    batch = db.execute(
        select(MaintenanceDocImportBatch)
        .where(MaintenanceDocImportBatch.doc_type == doc_type,
               MaintenanceDocImportBatch.status == "applied")
        .order_by(MaintenanceDocImportBatch.applied_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if batch is None:
        return _envelope("not_imported", label=SOURCE_LABELS[doc_type])
    applied_heads = (
        select(MaintenanceDocHeadRow)
        .join(MaintenanceDocImportBatch,
              MaintenanceDocImportBatch.batch_id == MaintenanceDocHeadRow.batch_id)
        .where(MaintenanceDocImportBatch.doc_type == doc_type,
               MaintenanceDocImportBatch.status == "applied")
        .subquery()
    )
    as_of = db.execute(select(func.max(applied_heads.c.head_date))).scalar_one()
    unlinked = db.execute(
        select(func.count(applied_heads.c.row_id))
        .where(applied_heads.c.project_id.is_(None))
    ).scalar_one()
    readiness = "partial" if (batch.issue_rows or unlinked) else "ready"
    return _envelope(readiness, as_of=as_of, batch_id=batch.batch_id,
                     uploaded_at=batch.applied_at, unlinked_rows=int(unlinked),
                     label=SOURCE_LABELS[doc_type])


def source_health(db: Session) -> dict:
    """四源健康总览（M3 /boss-board/health 直接返回本结构）。"""
    return {
        "sources": {
            "wbdd": _wbdd_source(db),
            "ckd": _ckd_source(db),
            "return_order": _doc_source(db, "return_order"),
            "rkd_inbound": _doc_source(db, "rkd_inbound"),
        },
        "stale_days": STALE_DAYS,
    }
