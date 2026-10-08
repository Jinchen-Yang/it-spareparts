"""备件循环 API（板块 D 第一批）：回收清单 preview / apply / 批次查询。

- 权限：action_recycle_manage（新键，模板默认 false，失败关闭；admin 常规全开）；
- 文件级幂等：同 SHA-256 重传 → 200 返回原批次（duplicate=true），零重复写入；
- 错误行 → 422 + 完整分级问题清单，零写入；
- 上传先落临时文件再读字节：与既有导入端点保持一致（_save_upload_to_temp）。
"""
import logging
import os

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
from app.models.circulation import RecycleBatch, RecycleLine
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
)
from app.db import get_db
from app.services import recycle_import as recycler
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
