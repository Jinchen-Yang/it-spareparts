"""导入相关 API（§9）：上传、批次列表、批次详情。"""
import os
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.config import MAX_UPLOAD_MB
from app.db import get_db
from app.security import UserContext, get_current_user_context, record_access_log
from app.services import inventory, profit
from app.etl import pipeline
from app.etl.reader import ReaderError
from app.models.system import SysImportBatch, SysImportError

router = APIRouter(prefix="/import", tags=["import"])


@router.post("/upload")
def upload(
    file: UploadFile = File(...),
    mode: str = Query("skip"),    # skip(默认,跳过已存在) | upsert(更新已存在,修复数据)
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    mode = mode if mode in ("skip", "upsert") else "skip"
    name = file.filename or "upload.xlsx"
    record_access_log(ctx, "upload", "import", {"filename": name, "mode": mode})
    if not name.lower().endswith(".xlsx"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "仅支持 .xlsx；若为 .xls 请另存为 .xlsx 后再上传")

    # 落临时文件并限制大小
    limit = MAX_UPLOAD_MB * 1024 * 1024
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := file.file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                        f"文件超过 {MAX_UPLOAD_MB}MB 上限")
                out.write(chunk)
        try:
            batch = pipeline.run_import(db, tmp, name, mode=mode)
            db.commit()
        except pipeline.DuplicateFileError as exc:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"该文件已成功导入（batch {exc.batch_id}）") from exc
        except ReaderError as exc:
            db.commit()  # failed batch 已记录
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        # 采购/销售导入后自动重算利润（§6.1 step13），失败不影响导入本身
        recompute_stats = None
        if batch.file_type in ("purchase", "sales"):
            try:
                recompute_stats = profit.recompute(db)
            except Exception as exc:  # noqa: BLE001
                recompute_stats = {"error": f"利润重算失败，请到利润页手动重算：{exc}"}
        # 采购/库存变化后刷新库存单位成本与库存金额（#9），失败不影响导入
        if batch.file_type in ("purchase", "inventory"):
            try:
                inventory.backfill_costs(db)
            except Exception:  # noqa: BLE001
                pass
        return {"batch_id": batch.id, "file_type": batch.file_type,
                "status": batch.status, "report": batch.report_json,
                "recompute": recompute_stats}
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


@router.get("/batches")
def list_batches(db: Session = Depends(get_db), _: str = Depends(require_admin)) -> list[dict]:
    rows = db.execute(
        select(SysImportBatch).order_by(desc(SysImportBatch.uploaded_at)).limit(100)
    ).scalars().all()
    return [{
        "id": b.id, "filename": b.filename, "file_type": b.file_type, "status": b.status,
        "uploaded_at": b.uploaded_at, "rows_total": b.rows_total,
        "rows_inserted": b.rows_inserted, "rows_skipped": b.rows_skipped,
        "rows_error": b.rows_error, "rows_inactive": b.rows_inactive,
    } for b in rows]


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: int, db: Session = Depends(get_db),
                 _: str = Depends(require_admin)) -> dict:
    b = db.get(SysImportBatch, batch_id)
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "批次不存在")
    errors = db.execute(
        select(SysImportError).where(SysImportError.batch_id == batch_id).limit(500)
    ).scalars().all()
    return {
        "id": b.id, "filename": b.filename, "file_type": b.file_type, "status": b.status,
        "uploaded_at": b.uploaded_at, "report": b.report_json,
        "errors": [{"row_no": e.row_no, "error_type": e.error_type,
                    "error_detail": e.error_detail} for e in errors],
    }
