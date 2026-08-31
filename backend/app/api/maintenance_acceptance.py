"""Controlled acceptance-report APIs (submit takes effect immediately).

2026-08-24 客户拍板：验收开放给销售/项目经理/维保负责人（sales 与
maintenance_manager 模板默认带 action_maintenance_acceptance_submit），
提交即生效，独立审批环节移除；历史驳回/审批字段仅为存量数据保留。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Path as ApiPath,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.maintenance_project_operations import _real_operator
from app.api.maintenance_project_scope import enforce_maintenance_project_access
from app.auth import current_identity, current_role
from app.db import get_db
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_acceptance as acceptance


router = APIRouter(prefix="/maintenance", tags=["maintenance"])


class AcceptanceSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(default="", max_length=128)
    submission_status: str | None = Field(
        default=None,
        pattern="^(not_submitted|submitted|not_configured)$",
    )
    approval_status: str | None = Field(
        default=None,
        pattern="^(not_reviewed|approved|rejected)$",
    )
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=24, ge=1, le=200)


class AcceptanceSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ge=0：GET 空载荷 version=0（无交付行）是合法起点，服务层放行 0→1
    # 首次自动建行跳变（2026-08-25 去截止日门）。
    expected_version: int = Field(ge=0)


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, acceptance.MaintenanceAcceptanceError):
        raise HTTPException(exc.status_code, str(exc)) from exc
    raise exc


def _rollback_file(db: Session, path: Path | None) -> None:
    db.rollback()
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # The DB write remains rolled back; an unlinked generated object has no
            # metadata and cannot be downloaded through this API.
            pass


@router.post("/acceptance-deliverables/search")
def search_acceptance_deliverables(
    body: AcceptanceSearchRequest,
    response: Response,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    payload = acceptance.search_acceptance(
        db,
        user_ctx=ctx,
        q=body.q,
        submission_status=body.submission_status,
        approval_status=body.approval_status,
        page=body.page,
        page_size=body.page_size,
    )
    record_access_log(ctx, "search_maintenance_acceptance", "maintenance_acceptance")
    return payload


@router.get("/projects/stable/{project_id}/acceptance")
def get_project_acceptance(
    response: Response,
    project_id: str = ApiPath(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    try:
        payload = acceptance.project_acceptance(db, project_id=project_id)
    except Exception as exc:
        _raise_http(exc)
    record_access_log(ctx, "read_maintenance_acceptance", f"maintenance_project:{project_id}")
    return payload


@router.post("/projects/stable/{project_id}/acceptance/attachments")
async def upload_project_acceptance_attachment(
    project_id: str = ApiPath(..., min_length=1, max_length=36),
    # 2026-08-25 客户口径：一个上传口——传文件即落库，不做版本握手；
    # 字段保留为可选（旧客户端兼容），服务端忽略。幂等键同样可选
    # （缺省由服务端生成），前端只需 POST 文件本身。
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_acceptance_submit")),
    ctx: UserContext = Depends(get_current_user_context),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=128),
) -> dict:
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    operator = _real_operator(db, ident)
    if not idempotency_key:
        idempotency_key = f"auto-{uuid4()}"
    path: Path | None = None
    try:
        # Read one byte beyond the hard ceiling so oversize input is rejected
        # without buffering an unbounded request in application memory.
        content = await file.read(acceptance.MAX_ACCEPTANCE_FILE_BYTES + 1)
        result, path = acceptance.upload_attachment(
            db,
            project_id=project_id,
            operator=operator,
            client_key=idempotency_key,
            filename=file.filename,
            mime_type=file.content_type,
            content=content,
        )
        db.commit()
    except IntegrityError as exc:
        _rollback_file(db, path)
        raise HTTPException(status.HTTP_409_CONFLICT, "附件写入发生并发冲突，未写入") from exc
    except Exception as exc:
        _rollback_file(db, path)
        _raise_http(exc)
    finally:
        await file.close()
    record_access_log(
        ctx,
        "upload_maintenance_acceptance_attachment",
        f"maintenance_project:{project_id}",
        {"file_id": result["file_id"], "replayed": result["replayed"]},
    )
    return result


@router.delete("/projects/stable/{project_id}/acceptance/attachments/{file_id}")
def delete_project_acceptance_attachment(
    project_id: str = ApiPath(..., min_length=1, max_length=36),
    file_id: str = ApiPath(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_acceptance_submit")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """删除验收附件（2026-08-25 客户口径：能传也能删）——软删链接，
    页面立即消失；文件字节与审计留痕可追溯，可重新上传。"""
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    operator = _real_operator(db, ident)
    try:
        result = acceptance.archive_attachment(
            db, project_id=project_id, file_id=file_id, operator=operator)
        db.commit()
    except Exception as exc:
        db.rollback()
        _raise_http(exc)
    record_access_log(
        ctx,
        "delete_maintenance_acceptance_attachment",
        f"maintenance_project:{project_id}",
        {"file_id": file_id},
    )
    return result


@router.post("/projects/stable/{project_id}/acceptance/submit")
def submit_project_acceptance(
    body: AcceptanceSubmitRequest,
    project_id: str = ApiPath(..., min_length=1, max_length=36),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_acceptance_submit")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    try:
        result = acceptance.submit_acceptance(
            db,
            project_id=project_id,
            expected_version=body.expected_version,
            operator=_real_operator(db, ident),
            client_key=idempotency_key,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "提交发生并发冲突，未写入") from exc
    except Exception as exc:
        db.rollback()
        _raise_http(exc)
    record_access_log(ctx, "submit_maintenance_acceptance", f"maintenance_project:{project_id}")
    return result


@router.get("/acceptance-files/{file_id}")
def download_project_acceptance_file(
    file_id: str = ApiPath(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> Response:
    operator = _real_operator(db, ident)
    try:
        project_id = acceptance.file_project_id(db, file_id=file_id)
        enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
        content, file_row = acceptance.controlled_download(
            db,
            file_id=file_id,
            operator=operator,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        _raise_http(exc)
    record_access_log(
        ctx,
        "download_maintenance_acceptance_attachment",
        f"maintenance_file:{file_id}",
        {"project_id": project_id},
    )
    # Arbitrary extensions may contain Unicode or header parameter delimiters.
    # Keep the legacy fallback ASCII-only; filename* preserves the complete name.
    disposition = (
        "attachment; filename=acceptance-report.bin; "
        f"filename*=UTF-8''{quote(file_row.original_filename, safe='')}"
    )
    return Response(
        content=content,
        media_type=file_row.mime_type,
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )
