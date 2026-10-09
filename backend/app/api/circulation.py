"""备件循环 API（板块 D 第一批）：回收清单 preview / apply / 批次查询。

- 权限：action_recycle_manage（新键，模板默认 false，失败关闭；admin 常规全开）；
- 文件级幂等：同 SHA-256 重传 → 200 返回原批次（duplicate=true），零重复写入；
- 错误行 → 422 + 完整分级问题清单，零写入；
- 上传先落临时文件再读字节：与既有导入端点保持一致（_save_upload_to_temp）。
"""
import logging
import os
from decimal import Decimal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    status,
)
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.imports import _save_upload_to_temp
from app.models.circulation import CirculationSnItem, RecycleBatch, RecycleLine
from app.security import (
    FULL_SCOPE_ROLES,
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
)
from app.db import get_db
from app.services import archive, detection, recycle_import as recycler
from app.services.archive import ArchivePermissionError, ArchiveValidationError
from app.services.recycle_import import RecycleImportError

_log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/circulation/recycle-imports",
    tags=["circulation"],
    dependencies=[Depends(require_action("action_recycle_manage"))],
)

_MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 与既有导入端点同量级


def _read_upload(file: UploadFile) -> tuple[bytes, str]:
    name = file.filename or "recycle.xlsx"
    if not name.lower().endswith(".xlsx"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "bad_file_type", "message": "回收清单必须是 .xlsx（氚云/在线表格另存为 xlsx）"},
        )
    tmp_path = _save_upload_to_temp(file, name)
    try:
        with open(tmp_path, "rb") as fh:
            data = fh.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            {"code": "file_too_large", "message": f"文件超过 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限"},
        )
    return data, name


def _report_payload(report: recycler.RecycleParseReport) -> dict:
    return {
        "file_sha256": report.file_sha256,
        "rows_total": report.rows_total,
        "lines_aggregated": len(report.lines),
        "total_amount": str(report.total_amount),
        "errors": [vars(i) for i in report.errors],
        "warnings": [vars(i) for i in report.warnings],
        "ok": report.ok,
    }


@router.post("/preview")
def preview_recycle_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    """解析并返回分级问题清单，零写入。"""
    data, filename = _read_upload(file)
    try:
        report = recycler.parse_recycle_workbook(data, db)
    except RecycleImportError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "bad_layout", "message": str(exc)},
        )
    return _report_payload(report)


@router.post("")
def apply_recycle_import(
    file: UploadFile = File(...),
    batch_label: str | None = Form(None),
    source_doc_no: str | None = Form(None),
    db: Session = Depends(get_db),
    user_ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """解析 + 原子落库。错误行 422 零写入；同文件重传 200 duplicate=true。"""
    data, filename = _read_upload(file)
    try:
        report = recycler.parse_recycle_workbook(data, db)
    except RecycleImportError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "bad_layout", "message": str(exc)},
        )
    if not report.ok:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "rows_invalid",
                "message": f"存在 {len(report.errors)} 个错误行，整批未写入",
                **_report_payload(report),
            },
        )
    batch, duplicate = recycler.apply_recycle_import(
        db, report,
        source_filename=filename,
        imported_by=user_ctx.user_id or "unknown",
        batch_label=batch_label,
        source_doc_no=source_doc_no,
    )
    record_access_log(user_ctx, "recycle_import_apply", "circulation_recycle_import",
                      {"filename": filename, "batch_id": batch.batch_id, "duplicate": duplicate})
    return {
        "duplicate": duplicate,
        "batch_id": batch.batch_id,
        "rows_total": batch.row_count,
        "line_count": batch.line_count,
        "warning_count": batch.warning_count,
        "total_amount": str(batch.total_amount),
    }


@router.get("/batches")
def list_batches(db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(RecycleBatch).order_by(RecycleBatch.imported_at.desc())
    ).scalars().all()
    return {
        "batches": [
            {
                "batch_id": b.batch_id,
                "source_filename": b.source_filename,
                "batch_label": b.batch_label,
                "source_doc_no": b.source_doc_no,
                "imported_by": b.imported_by,
                "imported_at": b.imported_at.isoformat(),
                "row_count": b.row_count,
                "line_count": b.line_count,
                "warning_count": b.warning_count,
                "total_amount": str(b.total_amount),
            }
            for b in rows
        ]
    }


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str, db: Session = Depends(get_db)) -> dict:
    batch = db.get(RecycleBatch, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": "批次不存在"})
    lines = db.execute(
        select(RecycleLine).where(RecycleLine.batch_id == batch_id)
    ).scalars().all()
    return {
        "batch": {
            "batch_id": batch.batch_id,
            "source_filename": batch.source_filename,
            "batch_label": batch.batch_label,
            "source_doc_no": batch.source_doc_no,
            "imported_by": batch.imported_by,
            "imported_at": batch.imported_at.isoformat(),
            "row_count": batch.row_count,
            "line_count": batch.line_count,
            "warning_count": batch.warning_count,
            "total_amount": str(batch.total_amount),
        },
        "lines": [
            {
                "line_id": ln.line_id,
                "pn_raw": ln.pn_raw,
                "description": ln.description,
                "category_label": ln.category_label,
                "warehouse_code": ln.warehouse_code,
                "bin_code": ln.bin_code,
                "condition": ln.condition,
                "qty": str(ln.qty),
                "unit_price": str(ln.unit_price),
                "total_price": str(ln.total_price),
                "company_entity": ln.company_entity,
                "needs_review": ln.needs_review,
                "price_mismatch": ln.price_mismatch,
            }
            for ln in lines
        ],
    }


# ───────────────────────── 检测单（D-2/D-23） ─────────────────────────


@router.post("/detection-sheets")
def create_detection_sheet(
    payload: dict,
    db: Session = Depends(get_db),
    user_ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """创建检测单：校验 + 原子落库（检测单/明细/SN 台账）。任何违规 422 零写入。

    请求体：{batch_id, inspector?, note?, items: [{line_id, received_qty,
    actual_condition, handling, actual_pn_raw?, sns?: [..]}]}
    """
    try:
        draft = detection.DetectionSheetDraft(
            batch_id=str(payload.get("batch_id") or ""),
            inspector=str(payload.get("inspector") or user_ctx.user_id or ""),
            note=payload.get("note"),
            items=[
                detection.DetectionItemDraft(
                    line_id=str(it.get("line_id") or ""),
                    received_qty=Decimal(str(it.get("received_qty"))),
                    actual_condition=str(it.get("actual_condition") or ""),
                    handling=str(it.get("handling") or ""),
                    sns=[str(s) for s in (it.get("sns") or [])],
                    actual_pn_raw=it.get("actual_pn_raw"),
                )
                for it in (payload.get("items") or [])
            ],
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "bad_payload", "message": "请求体字段缺失或数值非法（received_qty 必须为数字）"},
        )
    try:
        sheet = detection.apply_detection_sheet(db, draft, operator=user_ctx.user_id or "unknown")
    except detection.DetectionValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "detection_invalid", "message": str(exc)},
        )
    record_access_log(user_ctx, "detection_sheet_create", "circulation_detection",
                      {"sheet_id": sheet.sheet_id, "batch_id": sheet.batch_id})
    return {"sheet_id": sheet.sheet_id, "batch_id": sheet.batch_id,
            "inspector": sheet.inspector, "created_at": sheet.created_at.isoformat()}


@router.get("/detection-sheets")
def list_detection_sheets(batch_id: str | None = None, db: Session = Depends(get_db)) -> dict:
    stmt = select(detection.DetectionSheet).order_by(detection.DetectionSheet.created_at.desc())
    if batch_id:
        stmt = stmt.where(detection.DetectionSheet.batch_id == batch_id)
    rows = db.execute(stmt).scalars().all()
    return {
        "sheets": [
            {"sheet_id": s.sheet_id, "batch_id": s.batch_id, "inspector": s.inspector,
             "note": s.note, "created_at": s.created_at.isoformat()}
            for s in rows
        ]
    }


@router.get("/detection-sheets/{sheet_id}")
def get_detection_sheet(sheet_id: str, db: Session = Depends(get_db)) -> dict:
    sheet = db.get(detection.DetectionSheet, sheet_id)
    if sheet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": "检测单不存在"})
    items = db.execute(
        select(detection.DetectionItem).where(detection.DetectionItem.sheet_id == sheet_id)
    ).scalars().all()
    sns = db.execute(
        select(CirculationSnItem).where(CirculationSnItem.detection_item_id.in_([i.item_id for i in items] or ["-"]))
    ).scalars().all()
    sns_by_item: dict[str, list] = {}
    for s in sns:
        sns_by_item.setdefault(s.detection_item_id, []).append(s.sn)
    return {
        "sheet": {"sheet_id": sheet.sheet_id, "batch_id": sheet.batch_id,
                  "inspector": sheet.inspector, "note": sheet.note,
                  "created_at": sheet.created_at.isoformat()},
        "items": [
            {
                "item_id": i.item_id,
                "nominal_pn_raw": i.nominal_pn_raw,
                "actual_pn_raw": i.actual_pn_raw,
                "pn_corrected": bool(i.actual_pn_raw and i.actual_pn_raw != i.nominal_pn_raw),
                "received_qty": str(i.received_qty),
                "actual_condition": i.actual_condition,
                "handling": i.handling,
                "sn_count": i.sn_count,
                "sns": sns_by_item.get(i.item_id, []),
            }
            for i in items
        ],
    }


# ───────────────────────── 循环档案（D-3/D-22） ─────────────────────────


def _archive_payload(db: Session, ar) -> dict:
    summary = archive.attachment_summary(db, ar)
    return {
        "archive_id": ar.archive_id,
        "pn_std": ar.pn_std,
        "part_id": ar.part_id,
        "listing_status": ar.listing_status,
        "requirements_met": summary["requirements_met"],
        "photo_count": summary["photo_count"],
        "report_count": summary["report_count"],
        "force_listed_by": ar.force_listed_by,
        "force_listed_at": ar.force_listed_at.isoformat() if ar.force_listed_at else None,
        "force_reason": ar.force_reason,
        "attachments": summary["attachments"],
    }


@router.get("/archives")
def list_archives(status: str | None = None, db: Session = Depends(get_db)) -> dict:
    stmt = select(archive.CirculationArchive).order_by(archive.CirculationArchive.pn_std)
    if status:
        stmt = stmt.where(archive.CirculationArchive.listing_status == status)
    rows = db.execute(stmt).scalars().all()
    return {"archives": [_archive_payload(db, a) for a in rows]}


@router.get("/archives/{pn_std}")
def get_archive(pn_std: str, db: Session = Depends(get_db)) -> dict:
    aid = archive._get_archive_id(db, pn_std)
    if aid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": "该 PN 尚无循环档案"})
    return _archive_payload(db, db.get(archive.CirculationArchive, aid))


@router.post("/archives/{pn_std}/attachments")
def upload_archive_attachment(
    pn_std: str,
    file: UploadFile = File(...),
    kind: str = Form(...),
    db: Session = Depends(get_db),
    user_ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """上传照片/检测报告；PN 无档案时自动建档。"""
    data = await_file_bytes(file)
    try:
        att = archive.add_attachment(
            db, pn_std, kind, file.filename or "file", data,
            operator=user_ctx.user_id or "unknown",
        )
    except ArchiveValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            {"code": "attachment_invalid", "message": str(exc)})
    record_access_log(user_ctx, "archive_attachment_upload", "circulation_archive",
                      {"pn_std": pn_std, "kind": kind})
    ar = db.get(archive.CirculationArchive, att.archive_id)
    return _archive_payload(db, ar)


@router.delete("/archives/{pn_std}/attachments/{attachment_id}")
def delete_archive_attachment(
    pn_std: str, attachment_id: str,
    db: Session = Depends(get_db),
    user_ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        archive.remove_attachment(db, pn_std, attachment_id, operator=user_ctx.user_id or "unknown")
    except ArchiveValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            {"code": "attachment_invalid", "message": str(exc)})
    return {"deleted": True}


@router.post("/archives/{pn_std}/listing")
def set_archive_listing(
    pn_std: str,
    payload: dict,
    db: Session = Depends(get_db),
    user_ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    action = str(payload.get("action") or "")
    # 与 require_action 同口径：admin/boss 恒有全量动作；其余账号看 token 内权限图
    has_force = user_ctx.role in FULL_SCOPE_ROLES or bool(
        (user_ctx.permissions or {}).get("action_recycle_force_list"))
    try:
        ar = archive.set_listing(
            db, pn_std, action, operator=user_ctx.user_id or "unknown",
            has_force_permission=has_force, reason=payload.get("reason"),
        )
    except ArchivePermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": "forbidden", "message": str(exc)})
    except ArchiveValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            {"code": "listing_invalid", "message": str(exc)})
    record_access_log(user_ctx, f"archive_{action}", "circulation_archive", {"pn_std": pn_std})
    return _archive_payload(db, ar)


@router.get("/sn-items")
def list_sn_ledger(
    pn_std: str | None = None, lifecycle_status: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    try:
        rows = archive.list_sn_ledger(db, pn_std=pn_std, lifecycle_status=lifecycle_status)
    except ArchiveValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            {"code": "bad_filter", "message": str(exc)})
    return {"sn_items": [
        {"sn": s.sn, "pn_std": s.pn_std, "part_id": s.part_id,
         "lifecycle_status": s.lifecycle_status,
         "source_batch_id": s.source_batch_id,
         "detection_item_id": s.detection_item_id,
         "created_at": s.created_at.isoformat()}
        for s in rows
    ]}


def await_file_bytes(file: UploadFile) -> bytes:
    # 注意：不走 _save_upload_to_temp（那是导入管道专用，仅收 .xlsx）；
    # 档案附件是照片/PDF，直接读 UploadFile 流。
    data = file.file.read()
    file.file.seek(0)
    return data
