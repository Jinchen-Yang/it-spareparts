"""导入相关 API（§9）：单文件上传、批量后台作业、批次/作业列表与详情。"""
import csv
import logging
import os
import tempfile
import threading

import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import desc, func, select, update
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from app.auth import current_role
from app.config import MAX_IMPORT_FILES, MAX_UPLOAD_MB, get_settings
from app.db import SessionLocal, get_db
from app.security import (
    apply_field_visibility,
    UserContext,
    apply_profit_recompute_visibility,
    get_current_user_context,
    record_access_log,
    require_page,
)
from app.services import inventory, maintenance_cost, master_data, profit
from app.etl import expense_void, loader, pipeline, precheck as import_precheck
from app.services import import_void_preview
from app.etl.reader import ReaderError
from app.etl.transform import SOFT_ERROR_TYPES
from app.models.system import SysImportBatch, SysImportError, SysImportJob

router = APIRouter(
    prefix="/import",
    tags=["import"],
    dependencies=[Depends(current_role), Depends(require_page("page_import"))],
)
_log = logging.getLogger("imports")

_PROFIT_REFRESH_ERROR = "利润重算失败，请到利润页手动重算"
_INTERNAL_IMPORT_ERROR = "系统处理异常，请联系管理员查看服务端日志"
_IMPORT_CONFLICT_ERROR = "导入期间数据归属发生并发变化，本次已整体回滚，请重试"
_IMPORT_INTEGRITY_ERROR = "导入数据无法通过费用归属完整性校验，本次已整体回滚，请先修正数据"
# Starlette 0.37.2 只有旧名称、新版又会对旧名称发弃用警告；数值 413 是稳定 HTTP 契约。
_HTTP_REQUEST_ENTITY_TOO_LARGE = 413


def _enforce_import_file_limit(files: list[UploadFile]) -> None:
    if len(files) > MAX_IMPORT_FILES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"一次最多导入 {MAX_IMPORT_FILES} 个文件，请分批处理")


def _safe_csv_cell(value: object) -> object:
    if isinstance(value, str):
        content = value.lstrip()
        if content and content[0] in "=+-@":
            return "'" + value
    return value


class _DeletingFileResponse(FileResponse):
    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            except Exception:
                _log.warning("Failed to remove response temporary file", exc_info=True)


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
                    raise HTTPException(_HTTP_REQUEST_ENTITY_TOO_LARGE,
                                        f"{name} 超过 {MAX_UPLOAD_MB}MB 上限")
                out.write(chunk)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return tmp


def _post_import_refresh(
    db: Session,
    did_purchase_sales: bool,
    did_purchase_inventory: bool,
) -> dict | None:
    """非原子派生刷新：利润、库存与主数据。

    维保成本不得在这里接回：它必须在源事实 commit 前由
    ``_maintenance_recompute_in_import_transaction`` 完成。
    """
    recompute_stats = None
    if did_purchase_sales:
        try:
            recompute_stats = profit.recompute(db)
        except Exception:  # noqa: BLE001
            _log.exception("post-import profit recompute failed")
            db.rollback()
            recompute_stats = {"error": _PROFIT_REFRESH_ERROR}
    if did_purchase_inventory:
        try:
            inventory.backfill_costs(db)
        except Exception:  # noqa: BLE001
            _log.exception("post-import inventory cost backfill failed")
            db.rollback()
    try:
        master_data.refresh(db)
    except Exception:  # noqa: BLE001
        _log.exception("post-import master data refresh failed")
        db.rollback()
    return recompute_stats


def _maintenance_recompute_in_import_transaction(db: Session, file_type: str) -> dict | None:
    """在源事实提交前完成维保成本派生，避免新事实/旧成本的可见窗口。"""
    if file_type not in ("purchase", "sales", "maintenance"):
        return None
    return maintenance_cost.recompute(db, commit=False)


def _prefer_maintenance_recompute_stats(
    maintenance_stats: dict | None,
    post_stats: dict | None,
) -> dict | None:
    """保持既有响应口径：有维保重算时返回其统计，同时保留后置刷新错误。"""
    if maintenance_stats is None:
        return post_stats
    result = dict(maintenance_stats)
    if isinstance(post_stats, dict) and post_stats.get("error"):
        result["error"] = post_stats["error"]
    return result



def _hmac_key() -> bytes:
    key = get_settings().secret_key.encode("utf-8")
    if len(key) < 16:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "服务端预演签名密钥配置无效")
    return key


def _verify_preview_tokens(tokens: list[str], mode: str) -> list[dict]:
    """令牌存在但不合法 ⇒ 409，绝不静默按无令牌路径走。"""
    payloads = []
    for token in tokens:
        if not token:
            continue
        try:
            payloads.append(import_void_preview.verify(
                token, hmac_key=_hmac_key(), now=int(time.time()), mode=mode))
        except import_void_preview.VoidPreviewTokenError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc),
                                headers={"X-Error-Code": exc.code}) from exc
    return payloads


def _fingerprints_by_file(payloads: list[dict], saved: list[tuple[str, str]]) -> dict[str, str]:
    """按实际收到文件的 sha256 命中令牌：预演过的文件在提交前被改过 ⇒ 令牌对不上任何文件 ⇒ 409。"""
    if not payloads:
        return {}          # 没有令牌就不算 hash：跳过模式/无报销页的批次零额外成本
    by_hash = {p["file_hash"]: p["fingerprint"] for p in payloads}
    hashes = {tmp: pipeline.sha256_file(tmp) for tmp, _ in saved}
    unmatched = set(by_hash) - set(hashes.values())
    if unmatched:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "预演的文件与本次提交的文件不一致（预演后文件被修改或换了文件），请重新预演",
            headers={"X-Error-Code": "void_preview_mismatch"})
    return {tmp: by_hash[h] for tmp, h in hashes.items() if h in by_hash}


@router.post("/upload")
def upload(
    file: UploadFile = File(...),
    mode: str = Query("skip"),    # skip(默认,跳过已存在) | upsert(更新已存在,修复数据)
    preview_token: str | None = Form(None),   # 作废预演令牌（修复模式单合同报销页必需）
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    mode = mode if mode in ("skip", "upsert") else "skip"
    name = file.filename or "upload.xlsx"
    record_access_log(ctx, "upload", "import", {"filename": name, "mode": mode})
    payloads = _verify_preview_tokens([preview_token] if preview_token else [], mode)
    tmp = _save_upload_to_temp(file, name)
    try:
        maintenance_recompute_stats = None
        expected_fp = _fingerprints_by_file(payloads, [(tmp, name)]).get(tmp)
        try:
            # 审计：登录身份(token sub，RBAC 开启时即用户名)落 batch.uploaded_by → 每条数据可追溯到人。
            batch = pipeline.run_import(
                db,
                tmp,
                name,
                uploaded_by=ctx.user_id,
                mode=mode,
                auto_assign_maintenance_projects=True,
                expected_void_fingerprint=expected_fp,
                require_void_preview=True,
            )
            try:
                maintenance_recompute_stats = _maintenance_recompute_in_import_transaction(
                    db, batch.file_type,
                )
                if maintenance_recompute_stats is not None:
                    batch.report_json = {
                        **(batch.report_json or {}),
                        "maintenance_recompute": maintenance_recompute_stats,
                    }
            except maintenance_cost.MaintenanceCostRecomputeBusy as exc:
                db.rollback()
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "维保项目成本重算进行中，本次导入已整体回滚，请稍后重试",
                    headers={"Retry-After": "5"},
                ) from exc
            except maintenance_cost.WorkbookInvalidationConflictError as exc:
                db.rollback()
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    _IMPORT_CONFLICT_ERROR,
                    headers={
                        "Retry-After": "5",
                        "X-Error-Code": "import_concurrency_conflict",
                    },
                ) from exc
            except Exception as exc:  # noqa: BLE001 — 对外只返回固定业务文案
                db.rollback()
                _log.exception("maintenance recompute failed before import commit")
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "维保项目成本重算失败，本次导入已整体回滚",
                ) from exc
            db.commit()
        except pipeline.DuplicateFileError as exc:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"该文件已成功导入（batch {exc.batch_id}）") from exc
        except loader.ImportConcurrencyConflict as exc:
            db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                _IMPORT_CONFLICT_ERROR,
                headers={
                    "Retry-After": "5",
                    "X-Error-Code": "import_concurrency_conflict",
                },
            ) from exc
        except loader.ImportIntegrityError as exc:
            db.rollback()
            # 对外保持固定业务文案（稳定失败契约，test_import_precheck_v2 钉住）；冲突对
            # （单号#序号 / 项目 / 两把键 / 原因）走结构化日志与批量作业 notes，不再无痕。
            _log.warning("single upload integrity failure: file=%s: %s", name, exc)
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                _IMPORT_INTEGRITY_ERROR,
                headers={"X-Error-Code": "import_integrity_error"},
            ) from exc
        except pipeline.ArchiveError as exc:
            db.rollback()
            _log.exception("raw archive failed")
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, _INTERNAL_IMPORT_ERROR
            ) from exc
        except expense_void.VoidPlanDrift as exc:
            db.commit()  # failed batch 已记录（任何写入之前中止，只有留痕）
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc),
                                headers={"X-Error-Code": exc.code}) from exc
        except ReaderError as exc:
            db.commit()  # failed batch 已记录
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc),
                                headers={"X-Error-Code": exc.code}) from exc
        except DataError as exc:
            # 字段超长/超限等 DB 数据错误：回滚整批，回干净 422 而非裸 500（审计 2026-06-28 I-4）。
            db.rollback()
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "文件中存在超出字段范围的数据（如金额过大或文本过长），整批未导入，请修正后重试。"
            ) from exc
        post_recompute_stats = _post_import_refresh(
            db, batch.file_type in ("purchase", "sales"),
            batch.file_type in ("purchase", "inventory"),
        )
        recompute_stats = _prefer_maintenance_recompute_stats(
            maintenance_recompute_stats,
            post_recompute_stats,
        )
        return {"batch_id": batch.id, "file_type": batch.file_type,
                "status": batch.status, "report": batch.report_json,
                # recompute 保留旧兼容口径；两个显式命名字段避免调用方再猜
                # “这是利润重算还是维保成本重算”。
                "recompute": apply_profit_recompute_visibility(recompute_stats, ctx),
                "profit_recompute": apply_profit_recompute_visibility(
                    post_recompute_stats, ctx,
                ),
                "maintenance_recompute": apply_profit_recompute_visibility(
                    maintenance_recompute_stats, ctx,
                )}
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


@router.post("/precheck")
def precheck(
    files: list[UploadFile] = File(...),
    mode: str = Query("skip"),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """导入前预检（不导入、不建批次）：返回选表结果、风险等级与 v1 兼容字段。"""
    _enforce_import_file_limit(files)
    mode = mode if mode in ("skip", "upsert") else "skip"
    upsert_budget = import_precheck.new_upsert_budget()   # 每请求预算，跨文件累计
    results_with_hashes: list[tuple[dict, str | None]] = []
    for f in files:
        name = f.filename or "upload.xlsx"
        try:
            tmp = _save_upload_to_temp(f, name)
        except HTTPException as exc:
            code = (
                "file_too_large"
                if exc.status_code == _HTTP_REQUEST_ENTITY_TOO_LARGE
                else "invalid_file"
            )
            result = import_precheck.failed_file_result(name, code, str(exc.detail))
            results_with_hashes.append((result, None))
            continue
        try:
            file_hash = pipeline.sha256_file(tmp)
            try:
                result = import_precheck.inspect_file(tmp, name, mode=mode,
                                                      budget=upsert_budget)
            except ReaderError as exc:
                result = import_precheck.failed_file_result(name, exc.code, str(exc))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        results_with_hashes.append((result, file_hash))

    matches = pipeline.successful_batch_ids_by_hash(
        db, {file_hash for _, file_hash in results_with_hashes if file_hash is not None})
    import_precheck.apply_exact_success_matches(results_with_hashes, matches, mode)
    return import_precheck.response([result for result, _ in results_with_hashes], mode)


_NOTE_MAX = 4000          # 作业 note 总长上限：note 会整段渲染到导入页，绝不能无界
_UNSAFE_LEGACY_NOTE_MARKERS = (
    "traceback (most recent call last)",
    "sqlalchemy.",
    "psycopg.",
    "[sql:",
    "[parameters:",
)


def _short_err(exc: Exception) -> str:
    """作业 note 只存固定业务文案；完整异常仅进服务端日志。

    psycopg/SQLAlchemy 的首行也可能包含表名、SQL 或参数，因此不能截断后返回客户端。
    """
    _ = exc
    return _INTERNAL_IMPORT_ERROR


def _public_job_note(note: str | None) -> str | None:
    """清理导入作业对外 note，兼容旧版本已落库的完整数据库异常。

    v1.14.1 已保证新作业只写固定业务文案，但放宽导入页权限后，历史记录也会被
    非管理员读取。旧 note 一旦包含驱动名、SQL 或参数，整条降级为固定文案；数据库
    原值不改，仍供服务端审计使用。安全的重复/解析提示保持原样。
    """
    if not note:
        return note
    lowered = note.casefold()
    if any(marker in lowered for marker in _UNSAFE_LEGACY_NOTE_MARKERS):
        return _INTERNAL_IMPORT_ERROR
    return note


def _process_import_job(job_id: int, files: list[tuple[str, str]], mode: str,
                        created_by: str | None,
                        fingerprints: dict[str, str] | None = None) -> None:
    """后台 worker：逐文件 run_import（各自事务，复用全局导入锁串行），更新作业进度。

    与对话流式 worker 同模式（独立 SessionLocal、daemon 线程），客户端只需轮询 /import/jobs/{id}。
    成功/解析失败的批次都带 import_job_id 入库；重复文件不建批次，计入 note。
    每个文件在源事实提交前完成维保成本重算；全部文件处理完后只做利润、库存与
    主数据刷新，避免暴露「新源事实 + 旧维保成本」。
    """
    db = SessionLocal()
    notes: list[str] = []
    done = errored = 0
    did_ps = did_pi = False
    try:
        for tmp, name in files:
            try:
                batch = pipeline.run_import(
                    db,
                    tmp,
                    name,
                    uploaded_by=created_by,
                    mode=mode,
                    import_job_id=job_id,
                    auto_assign_maintenance_projects=True,
                    expected_void_fingerprint=(fingerprints or {}).get(tmp),
                    require_void_preview=True,
                )
                _maintenance_recompute_in_import_transaction(db, batch.file_type)
                db.commit()
                done += 1
                did_ps = did_ps or batch.file_type in ("purchase", "sales")
                did_pi = did_pi or batch.file_type in ("purchase", "inventory")
            except pipeline.DuplicateFileError as exc:
                db.rollback()
                errored += 1
                notes.append(f"重复跳过：{name}（已导入 batch {exc.batch_id}）")
            except expense_void.VoidPlanDrift as exc:
                db.commit()  # 失败批次已建并带 job_id；任何写入之前中止
                errored += 1
                notes.append(f"预演失效：{name}（{str(exc)[:300]}）")
            except ReaderError as exc:
                db.commit()  # 失败批次已建并带 job_id，提交留痕
                errored += 1
                notes.append(f"解析失败：{name}（{str(exc)[:300]}）")
            except loader.ImportConcurrencyConflict:
                db.rollback()
                errored += 1
                notes.append(f"并发冲突：{name}（{_IMPORT_CONFLICT_ERROR}）")
            except loader.ImportIntegrityError:
                db.rollback()
                errored += 1
                notes.append(f"完整性失败：{name}（{str(exc)[:300]}）")
            except maintenance_cost.WorkbookInvalidationConflictError:
                db.rollback()
                errored += 1
                notes.append(f"并发冲突：{name}（{_IMPORT_CONFLICT_ERROR}）")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                errored += 1
                _log.exception("import job=%s file=%s failed", job_id, name)
                notes.append(f"导入异常：{name}（{_short_err(exc)}）")
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
                # 实时进度：每文件后更新计数（独立小事务，前端轮询可见）
                db.execute(update(SysImportJob).where(SysImportJob.id == job_id)
                           .values(done_files=done, error_files=errored))
                db.commit()
        # 维保成本已逐文件与源事实同事务提交；这里只刷新利润/库存/主数据。
        recompute_stats = _post_import_refresh(db, did_ps, did_pi)
        if isinstance(recompute_stats, dict) and recompute_stats.get("error"):
            notes.append(recompute_stats["error"])
        job_status = "done" if errored == 0 else ("failed" if done == 0 else "partial")
        db.execute(update(SysImportJob).where(SysImportJob.id == job_id).values(
            status=job_status, finished_at=func.now(), done_files=done, error_files=errored,
            note=("；".join(notes))[:_NOTE_MAX] if notes else None))
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



@router.post("/expense-void-preview")
def expense_void_preview(
    file: UploadFile = File(...),
    mode: str = Query("upsert"),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """导入前作废预演（单文件、纯读、不建批次）：修复模式下这一批会作废哪些旧报销行、
    多少钱；status=ready 时附带把 文件 sha256 + 作废指纹 + 合同 绑在一起的令牌，提交
    时必须带上，loader 装载期复核（见 app/etl/expense_void.py）。"""
    mode = mode if mode in ("skip", "upsert") else "skip"
    name = file.filename or "upload.xlsx"
    record_access_log(ctx, "expense_void_preview", "import", {"filename": name, "mode": mode})
    tmp = _save_upload_to_temp(file, name)
    try:
        file_hash = pipeline.sha256_file(tmp)
        try:
            payload = pipeline.preview_expense_void(db, tmp, mode=mode)
        except ReaderError as exc:
            payload = {"status": "unreadable", "code": exc.code, "error": str(exc)}
        finally:
            db.rollback()   # 纯读；不留任何事务状态
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    payload = {"filename": name, "file_hash": file_hash, "mode": mode, **payload}
    if payload["status"] == "ready":
        payload["preview_token"] = import_void_preview.issue(
            file_hash=file_hash, mode=mode, fingerprint=payload["fingerprint"],
            contract=payload.get("contract"), issued_at=int(time.time()),
            hmac_key=_hmac_key())
        payload["expires_in_seconds"] = import_void_preview.TOKEN_TTL_SECONDS
    return apply_field_visibility(payload, ctx)


@router.post("/upload-batch")
def upload_batch(
    files: list[UploadFile] = File(...),
    mode: str = Query("skip"),
    preview_tokens: list[str] = Form([]),   # 作废预演令牌（每个会作废的报销页一枚）
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """批量上传：N 个 .xlsx 归一个作业，后台逐文件导入。立即返回 job_id，前端轮询进度。"""
    _enforce_import_file_limit(files)
    mode = mode if mode in ("skip", "upsert") else "skip"
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "未选择文件")
    record_access_log(ctx, "upload_batch", "import", {"count": len(files), "mode": mode})
    payloads = _verify_preview_tokens(preview_tokens, mode)
    if mode == "upsert" and len(payloads) >= 2:
        # 同批两个会作废的报销页：文件 #1 提交后文件 #2 的作废集合已变，而两份预演都是
        # 在同一库态下算的——逐文件独立事务顺序 commit 之下这不可能同时兑现。后端硬拒，
        # 不交给前端判合同交集（对抗核验证明前端在合同列表截断时会 fail open）。
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "修复模式一次只能导入一个会作废旧行的单合同报销页，请分批提交",
            headers={"X-Error-Code": "void_preview_multiple"})
    # 落临时文件 + 建作业行：任一步失败都清掉已落临时文件（线程一旦启动则由线程负责清理）
    saved: list[tuple[str, str]] = []
    fingerprints: dict[str, str] = {}
    try:
        for f in files:
            name = f.filename or "upload.xlsx"
            saved.append((_save_upload_to_temp(f, name), name))
        fingerprints = _fingerprints_by_file(payloads, saved)
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
    threading.Thread(target=_process_import_job,
                     args=(job_id, saved, mode, ctx.user_id, fingerprints),
                     daemon=True, name=f"import-job-{job_id}").start()
    return {"job_id": job_id, "total_files": len(saved), "status": "processing"}


def _job_dict(j: SysImportJob) -> dict:
    return {
        "id": j.id, "created_by": j.created_by, "created_at": j.created_at,
        "finished_at": j.finished_at, "status": j.status, "mode": j.mode,
        "total_files": j.total_files, "done_files": j.done_files,
        "error_files": j.error_files, "note": _public_job_note(j.note),
    }


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        select(SysImportJob).order_by(desc(SysImportJob.created_at)).limit(50)
    ).scalars().all()
    return [_job_dict(j) for j in rows]


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_db)) -> dict:
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
def list_batches(db: Session = Depends(get_db)) -> list[dict]:
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
def batch_detail(batch_id: int, db: Session = Depends(get_db)) -> dict:
    b = db.get(SysImportBatch, batch_id)
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "批次不存在")
    issue_count = db.scalar(
        select(func.count()).select_from(SysImportError)
        .where(SysImportError.batch_id == batch_id)
    )
    errors = db.execute(
        select(SysImportError).where(SysImportError.batch_id == batch_id)
        .order_by(SysImportError.id).limit(500)
    ).scalars().all()
    return {
        "id": b.id, "filename": b.filename, "file_type": b.file_type, "status": b.status,
        "uploaded_at": b.uploaded_at, "uploaded_by": b.uploaded_by, "report": b.report_json,
        "issue_count": issue_count,
        "errors": [{"row_no": e.row_no,
                    "nature": "提示" if e.error_type in SOFT_ERROR_TYPES else "错误",
                    "error_type": e.error_type,
                    "detail": e.error_detail} for e in errors],
    }


@router.get("/batches/{batch_id}/errors.csv")
def batch_errors_csv(
    batch_id: int,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> FileResponse:
    if db.get(SysImportBatch, batch_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "批次不存在")
    record_access_log(ctx, "download_errors", f"import_batch:{batch_id}")
    fd, tmp = tempfile.mkstemp(prefix="it-data-import-issues-", suffix=".csv")
    try:
        try:
            output = os.fdopen(fd, "w", encoding="utf-8-sig", newline="")
        except Exception:
            try:
                os.close(fd)
            except Exception:
                _log.warning("Failed to close error CSV temporary file", exc_info=True)
            raise
        try:
            writer = csv.writer(output)
            writer.writerow(("行号", "性质", "问题类型", "问题明细"))
            rows = db.execute(
                select(SysImportError.row_no, SysImportError.error_type,
                       SysImportError.error_detail)
                .where(SysImportError.batch_id == batch_id)
                .order_by(SysImportError.id)
                .execution_options(yield_per=1000)
            )
            for row_no, error_type, error_detail in rows:
                nature = "提示" if error_type in SOFT_ERROR_TYPES else "错误"
                writer.writerow(
                    _safe_csv_cell(value)
                    for value in (row_no, nature, error_type, error_detail))
        except Exception:
            try:
                output.close()
            except Exception:
                _log.warning("Failed to close error CSV output", exc_info=True)
            raise
        else:
            output.close()
        db.rollback()
        return _DeletingFileResponse(
            tmp, media_type="text/csv; charset=utf-8",
            filename=f"import-batch-{batch_id}-issues.csv",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    except Exception:
        try:
            db.rollback()
        except Exception:
            _log.warning("Failed to rollback error CSV query", exc_info=True)
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        except Exception:
            _log.warning("Failed to remove error CSV temporary file", exc_info=True)
        raise
