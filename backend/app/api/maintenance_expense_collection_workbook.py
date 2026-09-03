"""报销/回款往返工作簿 API（增补包 AB-3）。

三个端点，与仓库既有工作簿端点同形：下载 .xlsx → validate（无副作用预演）
→ apply（整份事务覆盖）。金额可见性挂 ``data_profit``，上传另需专用动作键
``action_maintenance_expense_collection_upload``（WBDD-only 账号传不了）。
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.datastructures import UploadFile
from sqlalchemy.orm import Session

from app.api.maintenance_project_scope import require_maintenance_project_access
from app.auth import current_identity, current_role
from app.db import get_db
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import import_safety
from app.services import maintenance_expense_collection_workbook as wbk

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

_ACTION_KEY = "action_maintenance_expense_collection_upload"
_MAX_BYTES = 20 * 1024 * 1024
_XLSX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
_BASE = "/projects/stable/{project_id}/expense-collection-workbook"


def _operator(ident: dict) -> str:
    return str(ident.get("username") or ident.get("sub") or "unknown")


async def _read_upload_with_takeover(request: Request) -> tuple[bytes, bool]:
    """读取上传文件 + force_takeover 标志（2.7.0 行级冲突强制接管）。"""
    data = await _read_upload(request)
    # starlette 会缓存已解析的 form，二次 await 不会重复读体。
    form = await request.form()
    value = form.get("force_takeover")
    raw = str(value).strip().lower() if value is not None else ""
    return data, raw in {"1", "true", "yes", "on"}


async def _read_upload(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            {"code": "unsupported_media_type",
             "message": "只接受 multipart/form-data 的 .xlsx 文件"},
        )
    form = await request.form()
    file = form.get("file")
    if not isinstance(file, UploadFile):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_request", "message": "缺少 file 上传字段"},
        )
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            {"code": "unsupported_media_type", "message": "只接受 .xlsx"},
        )
    try:
        data = await import_safety.read_limited(file, _MAX_BYTES)
        import_safety.validate_xlsx_zip(data, max_bytes=_MAX_BYTES)
    except import_safety.UploadSafetyError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_file", "message": str(exc)},
        ) from exc
    return data


def _plan(db: Session, project_id: str, data: bytes) -> wbk.WorkbookPlan:
    try:
        return wbk.validate(db, project_id=project_id, data=data)
    except wbk.WorkbookError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": exc.code, "message": exc.message, "issues": exc.issues},
        ) from exc


def _require_profit_visibility(
    ctx: UserContext = Depends(get_current_user_context),
) -> None:
    """下载只要求「看得到金额」。

    上传另有动作键；下载不挂动作键是有意的——只读对账的人本来就该能把表拉下来
    核对，不必为此获得写权限。
    """
    from app import permissions as perm

    perms = ctx.permissions or perm.template_for(ctx.role)
    if ctx.role == "admin" or perm.runtime_safe(perms).get("data_profit"):
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN,
                        {"code": "permission_denied", "message": "无金额查看权限"})


@router.get(_BASE + ".xlsx")
def download_workbook(
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    # 下载即看到金额：只挂数据组，不要求上传动作键（只读对账的人也能下载）
    _data: None = Depends(_require_profit_visibility),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(require_maintenance_project_access),
):
    content = wbk.build_workbook(db, project_id=project_id)
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            {"code": "not_found", "message": "项目不存在"})
    record_access_log(ctx, "download", "maintenance_expense_collection_workbook",
                      {"project_id": project_id})
    filename = f"expense-collection-{project_id}.xlsx"
    return StreamingResponse(
        iter([content]),
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "Cache-Control": "no-store"},
    )


@router.post(_BASE + "/validate")
async def validate_workbook(
    project_id: str = Path(..., min_length=1, max_length=36),
    request: Request = None,
    response: Response = None,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY, require_data="data_profit")),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    """无副作用预演：返回将要发生的改动条数，不写库。"""
    response.headers["Cache-Control"] = "no-store"
    data = await _read_upload(request)
    plan = _plan(db, project_id, data)
    return {"project_id": project_id, "valid": True, **plan.summary}


@router.post(_BASE + "/apply")
async def apply_workbook(
    project_id: str = Path(..., min_length=1, max_length=36),
    request: Request = None,
    response: Response = None,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY, require_data="data_profit")),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    """上传覆盖：整份事务应用；任何一行不合法则整份 422，不部分写入。"""
    response.headers["Cache-Control"] = "no-store"
    data = await _read_upload(request)
    plan = _plan(db, project_id, data)
    batch_id = str(uuid.uuid4())
    try:
        result = wbk.apply(db, plan, operated_by=_operator(ident),
                           import_batch_id=batch_id)
    except wbk.WorkbookError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": exc.code, "message": exc.message},
        ) from exc
    record_access_log(ctx, "apply", "maintenance_expense_collection_workbook",
                      {"project_id": project_id, **plan.summary})
    return {"project_id": project_id, **result}
