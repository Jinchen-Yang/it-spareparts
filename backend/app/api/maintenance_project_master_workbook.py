"""项目总表 / 单 sheet / 全项目备件行级表 的下载与上传（页面重设计 R2）。

三处下载点各配一个上传点（REQUIREMENTS #38「在哪下载就在哪上传」）：

| 位置 | 下载 | 上传 |
| --- | --- | --- |
| 主页全局 | `GET /maintenance/spare-part-lines.xlsx?range=` | `POST /maintenance/spare-part-lines/{validate,apply}` |
| 项目面板 | `GET /maintenance/projects/stable/{id}/master-workbook.xlsx` | `POST .../master-workbook/{validate,apply}` |
| 各 tab | 同上带 `?sheets=03_备件订单` | 同上（按文件里实际存在的 sheet 解析） |

权限沿用既有键：查看金额 `data_profit`，回填上传
`action_maintenance_expense_collection_upload`（AB-3 已建，不再新增动作键）。
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.maintenance_expense_collection_workbook import (
    _ACTION_KEY,
    _XLSX_MEDIA,
    _operator,
    _read_upload,
    _require_profit_visibility,
)
from app.auth import current_identity, current_role
from app.db import get_db
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_expense_collection_workbook as ec
from app.services import maintenance_project_master_workbook as master

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

_MASTER = "/projects/stable/{project_id}/master-workbook"
_GLOBAL = "/spare-part-lines"


def _xlsx(content: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        iter([content]),
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "Cache-Control": "no-store"},
    )


def _fail(exc: ec.WorkbookError):
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        {"code": exc.code, "message": exc.message, "issues": exc.issues},
    ) from exc


# ---------------------------------------------------------- 主页全局备件行级表

@router.get(_GLOBAL + ".xlsx")
def download_global_lines(
    range_preset: str = Query("this_month", alias="range"),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _data: None = Depends(_require_profit_visibility),
    ctx: UserContext = Depends(get_current_user_context),
):
    """全项目备件行级表（系统自动回填价之后）。时间预设见 #38。"""
    from datetime import date as _date

    parsed_from = _date.fromisoformat(date_from) if date_from else None
    parsed_to = _date.fromisoformat(date_to) if date_to else None
    try:
        content = master.build_global_lines(
            db, preset=range_preset, date_from=parsed_from, date_to=parsed_to)
    except ec.WorkbookError as exc:
        _fail(exc)
    record_access_log(ctx, "download", "maintenance_spare_part_lines",
                      {"range": range_preset})
    return _xlsx(content, f"spare-part-lines-{range_preset}.xlsx")


@router.post(_GLOBAL + "/validate")
async def validate_global_lines(
    request: Request = None,
    response: Response = None,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY, require_data="data_profit")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    data = await _read_upload(request)
    try:
        plan = master.validate_global(db, data=data)
    except ec.WorkbookError as exc:
        _fail(exc)
    return {"valid": True, **plan.summary}


@router.post(_GLOBAL + "/apply")
async def apply_global_lines(
    request: Request = None,
    response: Response = None,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY, require_data="data_profit")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """改价/补价上传覆盖＝真实源（#38）。整份事务，任何一行不合法则整份 422。"""
    response.headers["Cache-Control"] = "no-store"
    data = await _read_upload(request)
    try:
        plan = master.validate_global(db, data=data)
        result = master.apply(db, plan, operated_by=_operator(ident),
                              import_batch_id=str(uuid.uuid4()))
    except ec.WorkbookError as exc:
        db.rollback()
        _fail(exc)
    record_access_log(ctx, "apply", "maintenance_spare_part_lines", plan.summary)
    return result


# ---------------------------------------------------------- 项目总表 / 单 sheet

@router.get(_MASTER + ".xlsx")
def download_project_master(
    project_id: str = Path(..., min_length=1, max_length=36),
    sheets: str | None = Query(None, description="逗号分隔的 sheet 名；缺省=全六张"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _data: None = Depends(_require_profit_visibility),
    ctx: UserContext = Depends(get_current_user_context),
):
    wanted = (tuple(s.strip() for s in sheets.split(",") if s.strip())
              if sheets else master.ALL_SHEETS)
    try:
        content = master.build_project_master(db, project_id=project_id,
                                              sheets=wanted)
    except ec.WorkbookError as exc:
        _fail(exc)
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            {"code": "not_found", "message": "项目不存在"})
    record_access_log(ctx, "download", "maintenance_project_master_workbook",
                      {"project_id": project_id, "sheets": list(wanted)})
    suffix = "master" if len(wanted) == len(master.ALL_SHEETS) else "sheet"
    return _xlsx(content, f"project-{project_id}-{suffix}.xlsx")


@router.post(_MASTER + "/validate")
async def validate_project_master(
    project_id: str = Path(..., min_length=1, max_length=36),
    request: Request = None,
    response: Response = None,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY, require_data="data_profit")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    data = await _read_upload(request)
    try:
        plan = master.validate(db, project_id=project_id, data=data)
    except ec.WorkbookError as exc:
        _fail(exc)
    return {"project_id": project_id, "valid": True,
            "sheets": list(plan.sheets), **plan.summary}


@router.post(_MASTER + "/apply")
async def apply_project_master(
    project_id: str = Path(..., min_length=1, max_length=36),
    request: Request = None,
    response: Response = None,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY, require_data="data_profit")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """上传覆盖。文件里有哪张 sheet 就应用哪张——单 sheet 上传走同一入口。"""
    response.headers["Cache-Control"] = "no-store"
    data = await _read_upload(request)
    try:
        plan = master.validate(db, project_id=project_id, data=data)
        result = master.apply(db, plan, operated_by=_operator(ident),
                              import_batch_id=str(uuid.uuid4()))
    except ec.WorkbookError as exc:
        db.rollback()
        _fail(exc)
    record_access_log(ctx, "apply", "maintenance_project_master_workbook",
                      {"project_id": project_id, **plan.summary})
    return {"project_id": project_id, **result}
