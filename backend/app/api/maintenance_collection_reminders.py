"""回款提醒只读与人工操作 API（车道 A，Task 2）。

路由（服务端完整路径，main.py 以 ``api_prefix`` 注册并挂 maintenance Beta 依赖）：
- ``POST /maintenance/collection-reminders/search``
- ``GET  /maintenance/projects/stable/{project_id}/collection-milestones``
- ``POST /maintenance/collection-milestones/{milestone_id}/follow-ups``

写端点使用显式账号 action 门（``explicit_account_action_allowed`` 语义）：
admin 不得短路，无显式授权一律 403（P1-1/设计 §9）。领域错误体统一为
冻结的 ``DomainError`` 形状（code/message/current_version/current_data_version/issues[]）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.maintenance_project_operations import _real_operator
from app.auth import current_identity, current_role
from app.business_time import business_today
from app.db import get_db
from app.models.system import SysUser
from app.schemas.maintenance_collection_reminders import (
    DirectoryResponse,
    FollowUpRequest,
    FollowUpResponse,
    ProjectDetailResponse,
    SearchRequest,
)
from app.security import (
    UserContext,
    explicit_account_action_allowed,
    get_current_user_context,
    record_access_log,
    require_page,
)
from app.services import maintenance_collection_reminders as reminders
from app.services.maintenance_collection_reminders import (
    CollectionReminderCanaryDenied,
    CollectionReminderConflict,
    CollectionReminderInvalid,
    CollectionReminderNotFound,
    CollectionReminderPermissionError,
)


router = APIRouter(prefix="/maintenance", tags=["maintenance"])
_FOLLOW_UP_ACTION_KEY = "action_maintenance_collection_follow_up"


def _domain_error(
    status_code: int,
    *,
    code: str,
    message: str,
    current_version: int | None = None,
    current_data_version: str | None = None,
) -> None:
    """冻结 error_contract 的 DomainError 形状。

    FastAPI 会把传给 HTTPException 的 detail 包进响应体 ``{"detail": ...}``，
    因此这里只传 DomainErrorDetail 本体，避免双重嵌套。
    """
    raise HTTPException(
        status_code,
        {
            "code": code,
            "message": message,
            "current_version": current_version,
            "current_data_version": current_data_version,
            "issues": [],
        },
    )


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, CollectionReminderNotFound):
        _domain_error(
            status.HTTP_404_NOT_FOUND,
            code="not_found",
            message="资源不存在或不可见",
        )
    if isinstance(exc, CollectionReminderCanaryDenied):
        _domain_error(
            status.HTTP_403_FORBIDDEN,
            code="canary_scope_denied",
            message=str(exc),
        )
    if isinstance(exc, CollectionReminderPermissionError):
        _domain_error(
            status.HTTP_403_FORBIDDEN,
            code="permission_denied",
            message="无权执行此操作",
        )
    if isinstance(exc, CollectionReminderConflict):
        _domain_error(
            status.HTTP_409_CONFLICT,
            code="version_conflict",
            message=str(exc),
            current_version=exc.current_version,
            current_data_version=exc.current_data_version,
        )
    if isinstance(exc, CollectionReminderInvalid):
        _domain_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_request",
            message=str(exc),
        )
    raise exc


def _explicit_follow_up_action(
    ctx: UserContext = Depends(get_current_user_context),
    db: Session = Depends(get_db),
) -> None:
    """写端点显式账号门：与 require_explicit_account_action 同一语义
    （admin 不短路、只认实名账号快照⊕覆盖），错误体用冻结 DomainError 形状。
    """
    if not ctx.is_authenticated or not ctx.user_id:
        _domain_error(
            status.HTTP_401_UNAUTHORIZED,
            code="permission_denied",
            message="请先登录",
        )
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == ctx.user_id,
            SysUser.is_active.is_(True),
        )
    )
    if user is None or not explicit_account_action_allowed(user, _FOLLOW_UP_ACTION_KEY):
        _domain_error(
            status.HTTP_403_FORBIDDEN,
            code="permission_denied",
            message="无权执行此操作",
        )


@router.post("/collection-reminders/search")
def search_collection_reminders(
    body: SearchRequest,
    response: Response,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> DirectoryResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        payload = reminders.search_collection_reminders(
            db,
            as_of=business_today(),
            user_ctx=ctx,
            q_text=body.q,
            owner_scope=body.owner_scope,
            reminder_state=body.reminder_state,
            page=body.page,
            page_size=body.page_size,
        )
    except Exception as exc:
        _raise_http(exc)
    record_access_log(
        ctx,
        "collection_reminders_search",
        "maintenance_collection_reminders",
        {
            "searched": bool(body.q and body.q.strip()),
            "owner_scope": body.owner_scope,
            "reminder_state": body.reminder_state,
            "page": body.page,
            "page_size": body.page_size,
            "total": payload["total"],
        },
    )
    return DirectoryResponse(**payload)


@router.get("/projects/stable/{project_id}/collection-milestones")
def project_collection_milestones(
    response: Response,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> ProjectDetailResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        payload = reminders.get_project_collection_milestones(
            db,
            project_id=project_id,
            as_of=business_today(),
            user_ctx=ctx,
        )
    except Exception as exc:
        _raise_http(exc)
    if payload is None:
        _domain_error(
            status.HTTP_404_NOT_FOUND,
            code="not_found",
            message="资源不存在或不可见",
        )
    record_access_log(
        ctx,
        "collection_reminders_detail",
        f"maintenance_project:{project_id}",
        {"milestone_count": len(payload["rows"])},
    )
    return ProjectDetailResponse(**payload)


@router.post("/collection-milestones/{milestone_id}/follow-ups")
def follow_up_collection_milestone(
    body: FollowUpRequest,
    milestone_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(_explicit_follow_up_action),
    ctx: UserContext = Depends(get_current_user_context),
) -> FollowUpResponse:
    operator = _real_operator(db, ident)
    try:
        payload = reminders.follow_up_collection_milestone(
            db,
            milestone_id=milestone_id,
            expected_version=body.expected_version,
            idempotency_key=body.idempotency_key,
            action=body.action,
            planned_month=body.planned_month,
            note=body.note,
            reason=body.reason,
            operator=operator,
            user_ctx=ctx,
            as_of=business_today(),
        )
        if payload is None:
            _domain_error(
                status.HTTP_404_NOT_FOUND,
                code="not_found",
                message="资源不存在或不可见",
            )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _domain_error(
            status.HTTP_409_CONFLICT,
            code="version_conflict",
            message="数据已变化，请刷新后重试",
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        _raise_http(exc)
    record_access_log(
        ctx,
        f"collection_milestone_{body.action}",
        f"maintenance_collection_milestone:{milestone_id}",
        {
            "idempotent_replay": payload["idempotent_replay"],
            "result_version": payload["row"]["version"],
        },
    )
    return FollowUpResponse(**payload)
