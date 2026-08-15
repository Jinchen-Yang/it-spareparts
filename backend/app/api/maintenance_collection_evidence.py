"""回款提醒凭证 API（F6）：上传凭证 = 回款提醒关闭依据。"""

from fastapi import APIRouter, Depends, File, HTTPException, Path, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.maintenance_collection_reminders import (
    _domain_error,
    _explicit_follow_up_action,
    _raise_http,
    _real_operator,
)
from app.api.maintenance_project_scope import enforce_maintenance_project_access
from app.auth import current_identity, current_role
from app.db import get_db
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.security import UserContext, get_current_user_context, require_page
from app.services import maintenance_collection_evidence as collection_evidence
from app.services.maintenance_acceptance import (
    MaintenanceAcceptanceTooLarge,
    MaintenanceAcceptanceUnsupported,
)

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

MAX_UPLOAD_READ_BYTES = 25 * 1024 * 1024  # 校验层上限 20MB，留余量给出准确错误


def _milestone_project(db: Session, milestone_id: str) -> str | None:
    return db.scalar(
        select(MaintenanceCollectionMilestone.project_id).where(
            MaintenanceCollectionMilestone.milestone_id == milestone_id
        )
    )


@router.get("/collection-milestones/{milestone_id}/evidence")
def list_milestone_evidence(
    milestone_id: str = Path(..., min_length=1, max_length=36),
    response: Response = None,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """回款提醒凭证元数据列表（md5/sha256/大小/上传人，不含文件内容）。"""
    response.headers["Cache-Control"] = "no-store"
    project_id = _milestone_project(db, milestone_id)
    if project_id is None:
        _domain_error(
            status.HTTP_404_NOT_FOUND,
            code="not_found",
            message="资源不存在或不可见",
        )
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    return {
        "milestone_id": milestone_id,
        "rows": collection_evidence.list_evidence(db, milestone_id),
    }


@router.post(
    "/collection-milestones/{milestone_id}/evidence",
    status_code=status.HTTP_201_CREATED,
)
async def upload_milestone_evidence(
    milestone_id: str = Path(..., min_length=1, max_length=36),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(_explicit_follow_up_action),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """上传回款提醒凭证（巡检报告/图片/PDF）。同节点同 md5 重放幂等。"""
    project_id = _milestone_project(db, milestone_id)
    if project_id is None:
        _domain_error(
            status.HTTP_404_NOT_FOUND,
            code="not_found",
            message="资源不存在或不可见",
        )
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    operator = _real_operator(db, ident)
    content = await file.read(MAX_UPLOAD_READ_BYTES + 1)
    if len(content) > MAX_UPLOAD_READ_BYTES:
        _domain_error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            code="file_too_large",
            message="凭证文件过大",
        )
    try:
        payload = collection_evidence.save_evidence(
            db,
            milestone_id=milestone_id,
            operator=operator,
            filename=file.filename,
            mime_type=file.content_type,
            content=content,
        )
        if payload is None:
            _domain_error(
                status.HTTP_404_NOT_FOUND,
                code="not_found",
                message="资源不存在或不可见",
            )
        db.commit()
        return payload
    except MaintenanceAcceptanceTooLarge as exc:
        db.rollback()
        _domain_error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            code="file_too_large",
            message=str(exc),
        )
    except MaintenanceAcceptanceUnsupported as exc:
        db.rollback()
        _domain_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_request",
            message=str(exc),
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        _raise_http(exc)
