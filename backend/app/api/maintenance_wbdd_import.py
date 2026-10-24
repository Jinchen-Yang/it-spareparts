"""维保备件需求单（WBDD）专用上传 API（plan v1.3 M1-6，§4.1 契约）。

- 专用端点，不复用 /api/import/upload（那是 page_import 全家桶，铁律 6）；
- 权限：page_maintenance + action_maintenance_wbdd_import（新键，默认全员 false）；
- flag：maintenance_boss_dashboard_enabled=false 时整组 404（require_maintenance_boss）；
- 非 WBDD 文件 / 布局不符 → 422 零写入；同 Idempotency-Key 重放 → 200 返回原报告。
"""
import logging
from typing import Literal
import os

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Response, status
from fastapi import UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.imports import _save_upload_to_temp
from app.auth import current_identity, current_role
from app.db import get_db
from app.etl import loader, pipeline
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
from app.services import maintenance_wbdd_authorization as canonical
from app.services import maintenance_authorization as foundation
from app.services.maintenance_workflow_authorization import multipart_precondition
from app.api import maintenance_project_scope as auth_transport
from app.services.maintenance_cost import (
    MaintenanceCostRecomputeBusy,
    WorkbookInvalidationConflictError as CostWorkbookInvalidationConflictError,
)

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
    verified=Depends(auth_transport.identity_for('maintenance_wbdd_snapshot')),
    view: Literal['authorization'] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    form_precondition: str | None = Form(None, alias="authorization_precondition"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page("maintenance_wbdd_snapshot", "page_maintenance")),
    _action: None = Depends(auth_transport.legacy_gate("maintenance_wbdd_snapshot", require_action(_ACTION_KEY))),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    name = file.filename or "wbdd.xlsx"
    record_access_log(ctx, "upload", "maintenance_wbdd_import",
                      {"filename": name})
    tmp = _save_upload_to_temp(file, name)
    try:
        try:
            if verified is not None:
                return canonical.upload(db,verified,file_path=tmp,original_name=name,idempotency_key=idempotency_key,
                    authorization_view=view=='authorization',precondition=multipart_precondition(authorization_precondition,form_precondition))
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
        except (
            loader.ImportConcurrencyConflict,
            CostWorkbookInvalidationConflictError,
        ) as exc:
            db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "import_concurrency_conflict",
                    "message": "维保数据归属在导入期间发生变化，本次导入已整体回滚，请重试",
                },
                headers={"Retry-After": "5"},
            ) from exc
        except pipeline.DuplicateFileError as exc:
            db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"code": "duplicate_file",
                 "message": f"该文件已成功导入（batch {exc.batch_id}）"},
            ) from exc
        except loader.ImportIntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "source_identity_conflict", "message": str(exc)},
            ) from exc
        except ReaderError as exc:
            if verified is None:
                db.commit()  # legacy parse-failure audit
            else:
                db.rollback()
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
    verified=Depends(auth_transport.identity_for('maintenance_wbdd_snapshot')),
    view: Literal['authorization'] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page("maintenance_wbdd_snapshot", "page_maintenance")),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if verified is not None:
        return canonical.read(db,verified,entry_id=canonical.HEALTH,
            authorization_view=view=='authorization',precondition=foundation.decode_precondition(authorization_precondition))
    return wbdd.latest_health(db)


@router.get("/latest/missing")
def latest_missing(
    response: Response,
    verified=Depends(auth_transport.identity_for('maintenance_wbdd_reconciliation')),
    view: Literal['authorization'] | None = Query(None),
    authorization_precondition: str | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page("maintenance_wbdd_reconciliation", "page_maintenance")),
    _action: None = Depends(auth_transport.legacy_gate("maintenance_wbdd_reconciliation", require_action(_ACTION_KEY))),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """最近一次快照的差异清单明细（#264/#267）：供「按氚云现状批量作废」页面。"""
    response.headers["Cache-Control"] = "no-store"
    if verified is not None:
        return canonical.read(db,verified,entry_id=canonical.MISSING,
            authorization_view=view=='authorization',precondition=foundation.decode_precondition(authorization_precondition))
    from app.services.maintenance_project_assignments import maintenance_scope_project_ids

    return wbdd.latest_missing(
        db, allowed_project_ids=maintenance_scope_project_ids(db, ctx),
    )


class ReconciliationRequest(BaseModel):
    authorization_precondition: dict | None = None
    model_config = ConfigDict(extra="forbid")

    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_order_ids: list[str] = Field(default_factory=list)
    line_ids: list[int] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


@router.post("/{batch_id}/reconcile")
def reconcile(
    batch_id: int,
    body: ReconciliationRequest,
    response: Response,
    verified=Depends(auth_transport.identity_for('maintenance_wbdd_reconciliation')),
    view: Literal['authorization'] | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page("maintenance_wbdd_reconciliation", "page_maintenance")),
    _upload: None = Depends(auth_transport.legacy_gate("maintenance_wbdd_reconciliation", require_action(_ACTION_KEY))),
    _delete: None = Depends(auth_transport.legacy_gate("maintenance_wbdd_reconciliation", require_action("action_maintenance_demand_delete"))),
    identity: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """Apply only the explicitly selected source differences, under one transaction."""
    from app.api.maintenance_demands import _real_operator
    from app.services import maintenance_wbdd_sync
    from app.services.maintenance_project_assignments import maintenance_scope_project_ids

    response.headers["Cache-Control"] = "no-store"
    if verified is not None:
        return canonical.reconcile(db,verified,params={'batch_id':batch_id,**body.model_dump(exclude={'authorization_precondition'})},
            authorization_view=view=='authorization',precondition=body.authorization_precondition)
    operator = _real_operator(db, identity)
    try:
        result = maintenance_wbdd_sync.apply_reconciliation(
            db, batch_id=batch_id, expected_digest=body.expected_digest,
            source_order_ids=body.source_order_ids, line_ids=body.line_ids,
            reason=body.reason, operated_by=operator,
            allowed_project_ids=maintenance_scope_project_ids(db, ctx),
            idempotency_key=body.idempotency_key,
        )
        db.commit()
        return result
    except maintenance_wbdd_sync.ReconciliationConflict as exc:
        db.rollback()
        raise HTTPException(409, {"code": "reconciliation_changed", "message": str(exc)}) from exc
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
