"""导入相关 API（§9）：单文件上传、批量后台作业、批次/作业列表与详情。"""
import os
import tempfile
import threading

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import desc, func, select, update
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.config import MAX_UPLOAD_MB
from app.db import SessionLocal, get_db
from app.security import UserContext, get_current_user_context, record_access_log
from app.services import inventory, master_data, profit
from app.etl import pipeline
from app.etl.reader import ReaderError
from app.models.system import SysImportBatch, SysImportError, SysImportJob

router = APIRouter(prefix="/import", tags=["import"])


def _save_upload_to_temp(file: UploadFile, name: str) -> str:
    """落临时 .xlsx 并校验扩展名/大小。返回临时路径；非法抛 HTTPException（调用方负责清理已落文件）。"""
    if not name.lower().endswith(".xlsx"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"仅支持 .xlsx；若为 .xls 请另存为 .xlsx 后再上传：{name}")
    limit = MAX_UPLOAD_MB * 1024 * 1024
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := file.file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                        f"{name} 超过 {MAX_UPLOAD_MB}MB 上限")
                out.write(chunk)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return tmp


def _post_import_refresh(db: Session, did_purchase_sales: bool, did_purchase_inventory: bool) -> dict | None:
    """导入后置刷新（采购/销售→重算利润；采购/库存→回填成本；总是刷新主数据）。失败不影响导入。"""
    recompute_stats = None
    if did_purchase_sales:
        try:
            recompute_stats = profit.recompute(db)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            recompute_stats = {"error": f"利润重算失败，请到利润页手动重算：{exc}"}
    if did_purchase_inventory:
        try:
            inventory.backfill_costs(db)
        except Exception:  # noqa: BLE001
            db.rollback()
    try:
        master_data.refresh(db)
    except Exception:  # noqa: BLE001
        db.rollback()
    return recompute_stats


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
    tmp = _save_upload_to_temp(file, name)
    try:
        try:
            # 审计：登录身份(token sub，RBAC 开启时即用户名)落 batch.uploaded_by → 每条数据可追溯到人。
            batch = pipeline.run_import(db, tmp, name, uploaded_by=ctx.user_id, mode=mode)
            db.commit()
        except pipeline.DuplicateFileError as exc:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"该文件已成功导入（batch {exc.batch_id}）") from exc
        except ReaderError as exc:
            db.commit()  # failed batch 已记录
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except DataError as exc:
            # 字段超长/超限等 DB 数据错误：回滚整批，回干净 422 而非裸 500（审计 2026-06-28 I-4）。
            db.rollback()
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "文件中存在超出字段范围的数据（如金额过大或文本过长），整批未导入，请修正后重试。"
            ) from exc
        recompute_stats = _post_import_refresh(
            db, batch.file_type in ("purchase", "sales"),
            batch.file_type in ("purchase", "inventory"))
        return {"batch_id": batch.id, "file_type": batch.file_type,
                "status": batch.status, "report": batch.report_json,
                "recompute": recompute_stats}
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _process_import_job(job_id: int, files: list[tuple[str, str]], mode: str,
                        created_by: str | None) -> None:
    """后台 worker：逐文件 run_import（各自事务，复用全局导入锁串行），更新作业进度。

    与对话流式 worker 同模式（独立 SessionLocal、daemon 线程），客户端只需轮询 /import/jobs/{id}。
    成功/解析失败的批次都带 import_job_id 入库；重复文件不建批次，计入 note。
    全部文件处理完后做一次利润重算/成本回填/主数据刷新（避免逐文件重复重算）。
    """
    db = SessionLocal()
    notes: list[str] = []
    done = errored = 0
    did_ps = did_pi = False
    try:
        for tmp, name in files:
            try:
                batch = pipeline.run_import(db, tmp, name, uploaded_by=created_by,
                                            mode=mode, import_job_id=job_id)
                db.commit()
                done += 1
                did_ps = did_ps or batch.file_type in ("purchase", "sales")
                did_pi = did_pi or batch.file_type in ("purchase", "inventory")
            except pipeline.DuplicateFileError as exc:
                db.rollback()
                errored += 1
                notes.append(f"重复跳过：{name}（已导入 batch {exc.batch_id}）")
            except ReaderError as exc:
                db.commit()  # 失败批次已建并带 job_id，提交留痕
                errored += 1
                notes.append(f"解析失败：{name}（{exc}）")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                errored += 1
                notes.append(f"导入异常：{name}（{exc}）")
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
                # 实时进度：每文件后更新计数（独立小事务，前端轮询可见）
                db.execute(update(SysImportJob).where(SysImportJob.id == job_id)
                           .values(done_files=done, error_files=errored))
                db.commit()
        recompute_stats = _post_import_refresh(db, did_ps, did_pi)
        if isinstance(recompute_stats, dict) and recompute_stats.get("error"):
            notes.append(recompute_stats["error"])
        job_status = "done" if errored == 0 else ("failed" if done == 0 else "partial")
        db.execute(update(SysImportJob).where(SysImportJob.id == job_id).values(
            status=job_status, finished_at=func.now(), done_files=done, error_files=errored,
            note="；".join(notes) if notes else None))
        db.commit()
    except Exception:  # noqa: BLE001 — worker 不能让异常逃逸（无人接），尽力标失败
        db.rollback()
        try:
            db.execute(update(SysImportJob).where(SysImportJob.id == job_id)
                       .values(status="failed", finished_at=func.now()))
            db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        for tmp, _ in files:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        db.close()


@router.post("/upload-batch")
def upload_batch(
    files: list[UploadFile] = File(...),
    mode: str = Query("skip"),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """批量上传：N 个 .xlsx 归一个作业，后台逐文件导入。立即返回 job_id，前端轮询进度。"""
    mode = mode if mode in ("skip", "upsert") else "skip"
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "未选择文件")
    record_access_log(ctx, "upload_batch", "import", {"count": len(files), "mode": mode})
    # 落临时文件 + 建作业行：任一步失败都清掉已落临时文件（线程一旦启动则由线程负责清理）
    saved: list[tuple[str, str]] = []
    try:
        for f in files:
            name = f.filename or "upload.xlsx"
            saved.append((_save_upload_to_temp(f, name), name))
        job = SysImportJob(created_by=ctx.user_id, mode=mode, total_files=len(saved),
                           status="processing")
        db.add(job)
        db.commit()
    except Exception:
        for tmp, _ in saved:
            if os.path.exists(tmp):
                os.remove(tmp)
        raise
    job_id = job.id
    threading.Thread(target=_process_import_job, args=(job_id, saved, mode, ctx.user_id),
                     daemon=True, name=f"import-job-{job_id}").start()
    return {"job_id": job_id, "total_files": len(saved), "status": "processing"}


def _job_dict(j: SysImportJob) -> dict:
    return {
        "id": j.id, "created_by": j.created_by, "created_at": j.created_at,
        "finished_at": j.finished_at, "status": j.status, "mode": j.mode,
        "total_files": j.total_files, "done_files": j.done_files,
        "error_files": j.error_files, "note": j.note,
    }


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db), _: str = Depends(require_admin)) -> list[dict]:
    rows = db.execute(
        select(SysImportJob).order_by(desc(SysImportJob.created_at)).limit(50)
    ).scalars().all()
    return [_job_dict(j) for j in rows]


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_db),
               _: str = Depends(require_admin)) -> dict:
    j = db.get(SysImportJob, job_id)
    if j is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "作业不存在")
    batches = db.execute(
        select(SysImportBatch).where(SysImportBatch.import_job_id == job_id)
        .order_by(SysImportBatch.id)
    ).scalars().all()
    return {**_job_dict(j), "batches": [{
        "id": b.id, "filename": b.filename, "file_type": b.file_type, "status": b.status,
        "rows_total": b.rows_total, "rows_inserted": b.rows_inserted,
        "rows_skipped": b.rows_skipped, "rows_error": b.rows_error,
        "rows_inactive": b.rows_inactive,
    } for b in batches]}


@router.get("/batches")
def list_batches(db: Session = Depends(get_db), _: str = Depends(require_admin)) -> list[dict]:
    rows = db.execute(
        select(SysImportBatch).order_by(desc(SysImportBatch.uploaded_at)).limit(100)
    ).scalars().all()
    return [{
        "id": b.id, "filename": b.filename, "file_type": b.file_type, "status": b.status,
        "uploaded_at": b.uploaded_at, "uploaded_by": b.uploaded_by, "rows_total": b.rows_total,
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
        "uploaded_at": b.uploaded_at, "uploaded_by": b.uploaded_by, "report": b.report_json,
        "errors": [{"row_no": e.row_no, "error_type": e.error_type,
                    "error_detail": e.error_detail} for e in errors],
    }
