"""维保备件需求单（WBDD）专用上传 API（plan v1.3 M1-6，§4.1 契约）。

- 专用端点，不复用 /api/import/upload（那是 page_import 全家桶，铁律 6）；
- 权限：page_maintenance + action_maintenance_wbdd_import（新键，默认全员 false）；
- flag：maintenance_boss_dashboard_enabled=false 时整组 404（require_maintenance_boss）；
- 非 WBDD 文件 / 布局不符 → 422 零写入；同 Idempotency-Key 重放 → 200 返回原报告。
"""
import logging
import os

from fastapi import APIRouter, Depends, File, Header, HTTPException, Response, status
from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.api.imports import _save_upload_to_temp
from app.auth import current_role
from app.db import get_db
from app.etl import pipeline
from app.etl.reader import ReaderError
from app.maintenance_boss import require_maintenance_boss
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_wbdd_import as wbdd
from app.services.maintenance_cost import MaintenanceCostRecomputeBusy

_log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/maintenance/wbdd-imports",
    tags=["maintenance"],
    dependencies=[Depends(require_maintenance_boss)],
)

_ACTION_KEY = "action_maintenance_wbdd_import"


def _idempotency_key(idempotency_key: str | None = Header(None, alias="Idempotency-Key")) -> str:
    if not idempotency_key or not (8 <= len(idempotency_key) <= 128):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_idempotency_key",
             "message": "必须提供 8–128 字符的 Idempotency-Key 请求头"},
        )
    return idempotency_key


@router.post("")
def upload_wbdd(
    response: Response,
    file: UploadFile = File(...),
    idempotency_key: str = Depends(_idempotency_key),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY)),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    name = file.filename or "wbdd.xlsx"
    record_access_log(ctx, "upload", "maintenance_wbdd_import",
                      {"filename": name})
    tmp = _save_upload_to_temp(file, name)
    try:
        try:
            report, replayed = wbdd.import_wbdd(
                db, file_path=tmp, original_name=name,
                operator=str(ctx.user_id or "unknown"),
                idempotency_key=idempotency_key,
            )
        except wbdd.WbddImportError as exc:
            db.rollback()
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": exc.code, "message": exc.message},
            ) from exc
        except MaintenanceCostRecomputeBusy as exc:
            # 源事实、回执与重算同事务；busy 时整批回滚，客户端整体重试。
            db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"code": "recompute_busy", "message": "成本重算进行中，请稍后重试"},
                headers={"Retry-After": "5"},
            ) from exc
        except pipeline.DuplicateFileError as exc:
            db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"code": "duplicate_file",
                 "message": f"该文件已成功导入（batch {exc.batch_id}）"},
            ) from exc
        except ReaderError as exc:
            db.commit()  # failed batch 已记录（与通用导入语义一致）
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                {"code": "reader_error", "message": str(exc)},
            ) from exc
        return {**report, "replayed": replayed}
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


@router.get("/latest")
def latest(
    response: Response,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return wbdd.latest_health(db)


@router.get("/latest/missing")
def latest_missing(
    response: Response,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY)),
) -> dict:
    """最近一次快照的差异清单明细（#264/#267）：供「按氚云现状批量作废」页面。"""
    response.headers["Cache-Control"] = "no-store"
    return wbdd.latest_missing(db)
