"""验收需求清单 Excel 导入 API（2026-08-21 客户反馈）。

- GET  checklist：当前生效清单 + 历史（项目可见即可读）；
- GET  checklist/template：标准模板下载（项目可见即可读）；
- POST checklist/preview：multipart 上传落 raw（action 键 + 幂等头）；
- POST /acceptance-checklist/{batch_id}/apply：整表替换当前清单（仅本人批次）。
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from starlette.datastructures import UploadFile
from sqlalchemy.orm import Session

from app.auth import current_identity, current_role
from app.db import get_db
from app.api.maintenance_project_scope import enforce_maintenance_project_access
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import import_safety
from app.services import maintenance_acceptance_checklist as checklist

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

_ACTION_KEY = "action_maintenance_acceptance_checklist_import"


def _real_operator(ident: dict) -> str:
    return str(ident.get("username") or ident.get("sub") or "unknown")


def _preflight(request: Request) -> None:
    content_length = request.headers.get("content-length")
    if (content_length and content_length.isdigit()
            and int(content_length) > checklist.MAX_CHECKLIST_BYTES):
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            {"code": "upload_too_large", "message": "清单超过上传安全上限"},
        )
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            {"code": "unsupported_media_type",
             "message": "只接受 multipart/form-data 的 .xlsx 文件"},
        )


@router.get("/projects/stable/{project_id}/acceptance/checklist")
def get_project_checklist(
    project_id: str = Path(..., min_length=1, max_length=36),
    response: Response = None,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    return checklist.project_checklist(db, project_id)


@router.get("/projects/stable/{project_id}/acceptance/checklist/template")
def download_checklist_template(
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> Response:
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    data = checklist.build_template()
    return Response(
        content=data,
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={
            "Content-Disposition": 'attachment; filename="acceptance-checklist.xlsx"',
        },
    )


@router.post("/projects/stable/{project_id}/acceptance/checklist/preview")
async def preview_checklist(
    project_id: str = Path(..., min_length=1, max_length=36),
    request: Request = None,
    response: Response = None,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY)),
    _preflight: None = Depends(_preflight),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    try:
        form = await request.form()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_request", "message": f"multipart 表单解析失败：{type(exc).__name__}"},
        ) from exc
    file = form.get("file")
    if not isinstance(file, UploadFile):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_request", "message": "缺少 file 上传字段"},
        )
    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            {"code": "unsupported_media_type", "message": "只接受 .xlsx 清单"},
        )
    idempotency_key = request.headers.get("idempotency-key", "")
    if not (8 <= len(idempotency_key) <= 128):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_request",
             "message": "Idempotency-Key 必填且长度必须在 8–128 字符之间"},
        )
    try:
        data = await import_safety.read_limited(file, checklist.MAX_CHECKLIST_BYTES)
    except import_safety.UploadSafetyError as exc:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            {"code": "upload_too_large", "message": str(exc)},
        ) from exc
    try:
        import_safety.validate_xlsx_zip(data, max_bytes=checklist.MAX_CHECKLIST_BYTES)
    except import_safety.UploadSafetyError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_doc", "message": str(exc)},
        ) from exc
    try:
        parsed = await import_safety.parse_in_threadpool(
            checklist.parse_checklist_workbook, data, filename
        )
        batch_id = checklist.store_preview(
            db, parsed, project_id=project_id,
            uploaded_by=_real_operator(ident), idempotency_key=idempotency_key,
        )
        db.commit()
    except checklist.ChecklistParseError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_doc", "message": str(exc)},
        ) from exc
    except checklist.ChecklistBatchError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "batch_conflict", "message": str(exc)},
        ) from exc
    current = checklist.project_checklist(db, project_id)["current"]
    return {
        "batch_id": batch_id,
        "file_hash": parsed["file_hash"],
        "item_rows": parsed["item_rows"],
        "issue_rows": parsed["issue_rows"],
        "done_rows": sum(1 for it in parsed["items"] if it["done"] is True),
        "todo_rows": sum(1 for it in parsed["items"] if it["done"] is False),
        "issues": [
            f"第 {it['row_no']} 行：{msg}"
            for it in parsed["items"] for msg in it["issues"]
        ][:20],
        "will_replace_rows": current["item_rows"] if current else 0,
    }


@router.post("/acceptance-checklist/{batch_id}/apply")
def apply_checklist(
    batch_id: str = Path(..., min_length=36, max_length=36),
    response: Response = None,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY)),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    operator = _real_operator(ident)
    try:
        summary = checklist.apply_batch(db, batch_id, operated_by=operator)
        db.commit()
    except checklist.ChecklistBatchError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "batch_conflict", "message": str(exc)},
        ) from exc
    record_access_log(ctx, "maintenance_acceptance_checklist_apply",
                      "maintenance_acceptance_checklist_batch",
                      {"batch_id": batch_id, **{
                          k: v for k, v in summary.items() if k != "batch_id"}})
    return summary
