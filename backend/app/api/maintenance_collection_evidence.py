"""回款提醒凭证 API（F6）：上传凭证 = 回款提醒关闭依据。"""

from datetime import datetime, timezone

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
from app.business_time import business_today
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
    payload: dict | None = None
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
        # 上传凭证 = 回款提醒关闭（同事务；重放也重试关闭——首次可能因节点
        # 状态未关闭，状态修复后重放应自动闭环，round-6 Blocker 6）
        closed = collection_evidence.try_close_milestone_after_upload(
            db,
            milestone_id=milestone_id,
            evidence_id=payload["evidence_id"],
            operator=operator,
            user_ctx=ctx,
            as_of=business_today(),
        )
        payload["closed"] = bool(closed["closed"])
        payload["close_reason"] = None if closed["closed"] else closed.get("reason")
        db.commit()
        # DB 行已定案后落盘；落盘失败 → 凭证置 inactive 补偿（不留指向缺失文件的活跃行）
        if not payload.get("replayed"):
            try:
                collection_evidence.write_evidence_files(
                    file_id=payload["file_id"],
                    object_key=payload["object_key"],
                    content=content,
                    meta={
                        "file_id": payload["file_id"],
                        "milestone_id": milestone_id,
                        "original_filename": payload["original_filename"],
                        "mime_type": payload["mime_type"],
                        "size_bytes": len(content),
                        "md5": payload["md5"],
                        "sha256": payload["sha256"],
                        "uploaded_by": operator,
                        "uploaded_at": datetime.now(timezone.utc).isoformat(),
                        "storage": "local",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _archive_failed_evidence(db, payload["evidence_id"], operator)
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    {
                        "code": "file_write_failed",
                        "message": f"凭证文件落盘失败：{type(exc).__name__}",
                    },
                ) from exc
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


def _archive_failed_evidence(db: Session, evidence_id: str, operator: str) -> None:
    """落盘失败补偿：把已提交的凭证置 inactive（事实保留，防止活跃行指向缺失文件）。"""
    try:
        evidence = db.get(
            collection_evidence.MaintenanceCollectionEvidence, evidence_id
        )
        if evidence is not None and evidence.is_active:
            evidence.is_active = False
            evidence.archived_by = operator[:64]
            evidence.archived_at = datetime.now(timezone.utc)
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
