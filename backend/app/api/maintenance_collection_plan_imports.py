"""车道 B2：回款计划导入 preview/binding-options/apply/source-file API（Task 4）。

路由（服务端完整路径，main.py 以 ``api_prefix`` 注册并挂 maintenance Beta 依赖）：
- ``POST /maintenance/collection-plan-imports/preview``（multipart .xls，8 MiB）
- ``GET  /maintenance/collection-plan-imports/{batch_id}/binding-options``
- ``POST /maintenance/collection-plan-imports/{batch_id}/apply``
- ``GET  /maintenance/collection-plan-imports/{batch_id}/source-file``

写/读端点统一使用显式账号 action 门（实名 admin + ``action_maintenance_collection_plan_import``
+ ``data_profit``）：admin 不得短路，无显式授权一律 403；认证与权限检查在任何
请求体读取之前完成。错误体统一为冻结的 ``DomainError`` 形状
（code/message/current_version/current_data_version/issues[]）。
预览上传：Content-Length 先验 + 流式限额同时生效；Idempotency-Key 8–128 必填。
本模块只定义 router；main.py 注册属集成请求，不由本车道修改。
"""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.maintenance_project_operations import _real_operator
from app.auth import current_identity, current_role
from app.business_time import business_today
from app.db import get_db
from app.models.system import SysUser
from app.schemas.maintenance_collection_reminders import (
    ApplyRequest,
    ApplyResponse,
    BindingOptionsResponse,
    PreviewResponse,
)
from app.security import (
    UserContext,
    explicit_account_action_allowed,
    get_current_user_context,
    record_access_log,
    require_page,
)
from app.services import maintenance_collection_plan_imports as imports

router = APIRouter(prefix="/maintenance", tags=["maintenance"])
_IMPORT_ACTION_KEY = "action_maintenance_collection_plan_import"
MAX_PREVIEW_BYTES = 8 * 1024 * 1024


def _domain_error(
    status_code: int,
    *,
    code: str,
    message: str,
    current_version: int | None = None,
    current_data_version: str | None = None,
    issues: list[dict] | None = None,
) -> None:
    raise HTTPException(
        status_code,
        {
            "code": code,
            "message": message,
            "current_version": current_version,
            "current_data_version": current_data_version,
            "issues": issues or [],
        },
    )


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, imports.CollectionPlanImportNotFound):
        _domain_error(
            status.HTTP_404_NOT_FOUND,
            code="not_found",
            message="资源不存在或不可见",
        )
    if isinstance(exc, imports.CollectionPlanImportCanaryDenied):
        _domain_error(
            status.HTTP_403_FORBIDDEN,
            code="canary_scope_denied",
            message=str(exc),
        )
    if isinstance(exc, imports.CollectionPlanImportPermissionError):
        _domain_error(
            status.HTTP_403_FORBIDDEN,
            code="permission_denied",
            message=str(exc),
        )
    if isinstance(exc, imports.CollectionPlanImportConflict):
        _domain_error(
            status.HTTP_409_CONFLICT,
            code="version_conflict",
            message=str(exc),
            current_version=exc.current_version,
            current_data_version=exc.current_data_version,
        )
    if isinstance(exc, imports.CollectionPlanImportInvalid):
        _domain_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_request",
            message=str(exc),
            issues=exc.issues,
        )
    raise exc


def _explicit_import_action(
    ctx: UserContext = Depends(get_current_user_context),
    db: Session = Depends(get_db),
) -> None:
    """导入门依赖：实名 admin + 显式 action + data_profit（设计 §9）。

    admin 不短路；任何请求体读取之前执行。
    """
    from app import permissions as _perm

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
    if user is None:
        _domain_error(
            status.HTTP_403_FORBIDDEN,
            code="permission_denied",
            message="无权执行此操作",
        )
    if user.role != "admin":
        _domain_error(
            status.HTTP_403_FORBIDDEN,
            code="permission_denied",
            message="回款计划导入能力仅限实名管理员",
        )
    if not explicit_account_action_allowed(user, _IMPORT_ACTION_KEY):
        _domain_error(
            status.HTTP_403_FORBIDDEN,
            code="permission_denied",
            message="无权执行此操作",
        )
    graph = _perm.effective_for_user(user)
    if not _perm.runtime_safe(graph).get("data_profit", False):
        _domain_error(
            status.HTTP_403_FORBIDDEN,
            code="permission_denied",
            message="回款计划导入能力要求同时具备利润数据可见权限",
        )


def _real_name_user(db: Session, ctx: UserContext) -> SysUser:
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == ctx.user_id,
            SysUser.is_active.is_(True),
        )
    )
    if user is None:
        _domain_error(
            status.HTTP_403_FORBIDDEN,
            code="permission_denied",
            message="无权执行此操作",
        )
    return user


def _preview_upload_preflight(request: Request) -> None:
    """8 MiB Content-Length 先验 + multipart 类型检查：在读取任何请求体之前拒绝。"""
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_PREVIEW_BYTES:
        _domain_error(
            status.HTTP_413_CONTENT_TOO_LARGE,
            code="upload_too_large",
            message="工作簿超过上传安全上限",
        )
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        _domain_error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            code="unsupported_media_type",
            message="只接受 multipart/form-data 的 .xls 文件",
        )


@router.post("/collection-plan-imports/preview")
async def preview_collection_plan_import(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(_explicit_import_action),
    _preflight: None = Depends(_preview_upload_preflight),
    ctx: UserContext = Depends(get_current_user_context),
) -> PreviewResponse:
    response.headers["Cache-Control"] = "no-store"
    # 认证/权限依赖已在读取任何请求体之前完成；这里才手动解析 multipart。
    try:
        form = await request.form()
    except Exception as exc:  # noqa: BLE001
        _domain_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_request",
            message=f"multipart 表单解析失败：{type(exc).__name__}",
        )
    file = form.get("file")
    if not isinstance(file, UploadFile):
        _domain_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_request",
            message="缺少 file 上传字段",
        )
    # 扩展名与幂等键检查（文件名来自 multipart）。
    filename = file.filename or ""
    if not filename.lower().endswith(".xls"):
        _domain_error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            code="unsupported_media_type",
            message="只接受 multipart/form-data 的 .xls 文件",
        )
    idempotency_key = request.headers.get("idempotency-key", "")
    if not (8 <= len(idempotency_key) <= 128):
        _domain_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_request",
            message="Idempotency-Key 必填且长度必须在 8–128 字符之间",
        )
    # 流式限额：Content-Length 先验之外的第二道防线。
    content = b""
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        content += chunk
        if len(content) > MAX_PREVIEW_BYTES:
            _domain_error(
                status.HTTP_413_CONTENT_TOO_LARGE,
                code="upload_too_large",
                message="工作簿超过上传安全上限",
            )
    operator = _real_operator(db, ident)
    user = _real_name_user(db, ctx)
    try:
        payload = imports.preview_collection_plan_import(
            db,
            content=content,
            filename=filename,
            idempotency_key=idempotency_key,
            owner_user_id=user.id,
            operator=operator,
            user_ctx=ctx,
            as_of=business_today(),
        )
        db.commit()
    except imports.CollectionPlanImportInvalid as exc:
        # 合同级失败也保留哈希 + 受控原件证据（设计 §4.4）：提交 error 批次后抛 422。
        db.commit()
        _raise_http(exc)
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
        "collection_plan_import_preview",
        f"collection_plan_import_batch:{payload['batch_id']}",
        {
            "status": payload["status"],
            "milestones": payload["counts"]["milestones"],
            "blockers": payload["counts"]["blockers"],
        },
    )
    return PreviewResponse(**payload)


@router.get("/collection-plan-imports/{batch_id}/binding-options")
def collection_plan_binding_options(
    response: Response,
    batch_id: str = Path(..., min_length=1, max_length=64),
    q: str = Query(..., min_length=1, max_length=256),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(_explicit_import_action),
    ctx: UserContext = Depends(get_current_user_context),
) -> BindingOptionsResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        payload = imports.search_collection_binding_options(
            db,
            batch_id=batch_id,
            q_text=q,
            page=page,
            page_size=page_size,
            user_ctx=ctx,
        )
    except Exception as exc:
        _raise_http(exc)
    record_access_log(
        ctx,
        "collection_plan_binding_options",
        f"collection_plan_import_batch:{batch_id}",
        {"total": payload["total"], "page": payload["page"], "page_size": payload["page_size"]},
    )
    return BindingOptionsResponse(**payload)


@router.post("/collection-plan-imports/{batch_id}/apply")
def apply_collection_plan_import(
    body: ApplyRequest,
    response: Response,
    batch_id: str = Path(..., min_length=1, max_length=64),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(_explicit_import_action),
    ctx: UserContext = Depends(get_current_user_context),
) -> ApplyResponse:
    response.headers["Cache-Control"] = "no-store"
    operator = _real_operator(db, ident)
    user = _real_name_user(db, ctx)
    try:
        payload = imports.apply_collection_plan_import(
            db,
            batch_id=batch_id,
            expected_batch_version=body.expected_batch_version,
            expected_data_version=body.expected_data_version,
            bindings=body.bindings,
            owner_user_id=user.id,
            operator=operator,
            user_ctx=ctx,
            as_of=business_today(),
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
        "collection_plan_import_apply",
        f"collection_plan_import_batch:{batch_id}",
        {
            "status": payload["status"],
            "idempotent_replay": payload["idempotent_replay"],
            "counts": payload["counts"],
        },
    )
    return ApplyResponse(**payload)


@router.get("/collection-plan-imports/{batch_id}/source-file")
def collection_plan_source_file(
    response: Response,
    batch_id: str = Path(..., min_length=1, max_length=64),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(_explicit_import_action),
    ctx: UserContext = Depends(get_current_user_context),
) -> FileResponse:
    operator = _real_operator(db, ident)
    user = _real_name_user(db, ctx)
    try:
        source = imports.open_collection_plan_source_file(
            db,
            batch_id=batch_id,
            owner_user_id=user.id,
            operator=operator,
            user_ctx=ctx,
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        _raise_http(exc)
    # attachment disposition + no-store；中性文件名（禁按原文件名寻址/回显）。
    response.headers["Cache-Control"] = "no-store"
    return FileResponse(
        source.storage_path,
        media_type=source.content_type,
        headers={
            "Content-Length": str(source.file_size),
            "Content-Disposition": (
                f'attachment; filename="collection-plan-{batch_id}.xls"'
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
