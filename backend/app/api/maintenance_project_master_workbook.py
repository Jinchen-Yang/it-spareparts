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
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.maintenance_expense_collection_workbook import (
    _ACTION_KEY,
    _XLSX_MEDIA,
    _operator,
    _read_upload,
    _require_profit_visibility,
)
from app.auth import current_identity, current_role
from app.config import get_settings
from app.db import get_db
from app.models.maintenance import MaintenanceManualCostOverride
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
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
    from urllib.parse import quote
    ascii_name = re.sub(r'[^\x20-\x7e]', '_', filename)
    utf8_name = quote(filename, safe="")
    return StreamingResponse(
        iter([content]),
        media_type=_XLSX_MEDIA,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{utf8_name}"
            ),
            "Cache-Control": "no-store",
        },
    )


def _fail(exc: ec.WorkbookError):
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        {"code": exc.code, "message": exc.message, "issues": exc.issues},
    ) from exc


def _safe_filename_part(value: str) -> str:
    """清洗导出文件名片段：去掉路径与非法字符，避免项目名破坏文件名（2026-08-17）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value).strip().strip(".")
    return cleaned or "项目"


def _workbook_filename(
    db: Session, project_id: str, wanted: tuple[str, ...]
) -> str:
    """下载文件名 = XSDD 销售订单号（取第一个）+ 维保项目名 + 表单类型（2026-08-17）。"""
    project = db.get(MaintenanceProject, project_id)
    contracts = list(db.execute(
        select(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_id == project_id)
        .order_by(MaintenanceProjectContract.effective_from)
    ).scalars())
    project_xsdds = master.project_sales_order_nos(db, project_id)
    xsdd = (contracts[0].contract_no if contracts else
            project_xsdds[0] if project_xsdds else
            project.project_code if project else project_id)
    name = _safe_filename_part(project.display_name) if project else _safe_filename_part(project_id)
    form = "总表" if len(wanted) == len(master.ALL_SHEETS) else (
        wanted[0] if len(wanted) == 1 else "多表单"
    )
    return f"{_safe_filename_part(xsdd)}-{name}-{form}.xlsx"


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


@router.get("/projects/stable/{project_id}/expense-rows")
def list_project_expense_rows(
    response: Response,
    project_id: str = Path(..., min_length=1, max_length=36),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _data: None = Depends(_require_profit_visibility),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """项目面板「报销」tab 的只读行（含备注，#47）。

    与 04_报销订单 sheet 同源同口径（都走 ec._expenses 按合同号归集），
    页面只**展示**——改金额/备注仍只能走「下载 → 改 → 上传覆盖」（#40）。
    """
    response.headers["Cache-Control"] = "no-store"
    rows = ec._expenses(db, master.project_sales_order_nos(db, project_id))
    total = len(rows)
    window = rows[(page - 1) * page_size: page * page_size]
    return {
        "rows": [
            {
                "raw_line_id": expense.raw_line_id,
                "bxd_no": expense.bxd_no,
                "expense_date": (expense.expense_date.isoformat()
                                 if expense.expense_date else None),
                "person": expense.person,
                "expense_type": expense.expense_type,
                "fee_category": expense.fee_category,
                "reason": expense.reason,
                "contract_no": expense.linked_sales_order_no,
                "amount_ex_tax": (str(expense.amount_ex_tax)
                                  if expense.amount_ex_tax is not None else None),
                "amount_inc_tax": (str(expense.amount_inc_tax)
                                   if expense.amount_inc_tax is not None else None),
                "data_status": expense.data_status,
                "remark": expense.remark,
            }
            for expense in window
        ],
        "total": total, "page": page, "page_size": page_size,
    }


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
        if get_settings().maintenance_project_master_v2_enabled:
            v2_names = {
                master.SHEET_BASICS: master.V2_SHEET_OVERVIEW,
                master.SHEET_OVERVIEW: master.V2_SHEET_PLAN,
                master.SHEET_PARTS: master.V2_SHEET_PARTS,
                master.SHEET_EXPENSE: master.V2_SHEET_EXPENSE,
                master.SHEET_COLLECTION: master.V2_SHEET_RECEIPTS,
                master.SHEET_SITE: master.V2_SHEET_SITE,
            }
            v2_wanted = tuple(v2_names.get(name, name) for name in wanted)
            content = master.build_project_master_v2(
                db, project_id=project_id, sheets=v2_wanted,
            )
        else:
            content = master.build_project_master(db, project_id=project_id,
                                                  sheets=wanted)
    except ec.WorkbookError as exc:
        _fail(exc)
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            {"code": "not_found", "message": "项目不存在"})
    record_access_log(ctx, "download", "maintenance_project_master_workbook",
                      {"project_id": project_id, "sheets": list(wanted)})
    return _xlsx(content, _workbook_filename(db, project_id, wanted))


@router.get(_MASTER + "/rows")
def list_master_rows(
    project_id: str = Path(..., min_length=1, max_length=36),
    sheet: str = Query(..., description="sheet 名，当前支持 03_备件订单"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _data: None = Depends(_require_profit_visibility),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """备件成本 tab 的 web 呈现：03_备件订单 行级（PN）只读数据源（2026-08-17）。"""
    if (get_settings().maintenance_project_master_v2_enabled
            and sheet in {master.V2_SHEET_PARTS, master.SHEET_PARTS}):
        rows = master._assigned_lines(db, project_id=project_id, window=None)
        line_ids = [line.id for line, _order, _pid in rows]
        overrides = {
            item.line_id: item for item in db.scalars(select(MaintenanceManualCostOverride).where(
                MaintenanceManualCostOverride.line_id.in_(line_ids)
            ))
        } if line_ids else {}
        return {
            "sheet": sheet,
            "total": len(rows),
            "rows": [{
                "line_id": line.id,
                "part_id": line.part_id,
                "order_no": order.order_no,
                "order_date": order.order_date.isoformat() if order.order_date else None,
                "pn_std": line.pn_std or line.pn_raw or "",
                "description": line.description or "",
                "qty": str(line.qty) if line.qty is not None else None,
                "warehouse": order.warehouse or "",
                "cost_source": line.cost_source or "none",
                "cost_source_label": line.cost_source or "无成本结果",
                "confidence": line.confidence or "none",
                "unit_cost_ex_tax": str(line.unit_cost_ex_tax) if line.unit_cost_ex_tax is not None else None,
                "unit_cost_inc_tax": str(line.unit_cost_inc_tax) if line.unit_cost_inc_tax is not None else None,
                "missing_kind": "out_of_scope" if line.cost_source is None and line.unit_cost_ex_tax is None else ("none" if line.unit_cost_ex_tax is None else None),
                "can_refill": line.unit_cost_ex_tax is None,
                "manual_unit_cost_ex_tax": str(overrides[line.id].unit_cost_ex_tax) if line.id in overrides else None,
                "manual_reason": overrides[line.id].reason if line.id in overrides else None,
            } for line, order, _pid in rows],
        }
    if sheet not in {master.SHEET_PARTS}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "unsupported_sheet",
             "message": "当前仅支持 03_备件订单 行级查询"},
        )
    rows = master._assigned_lines(db, project_id=project_id, window=None)
    line_ids = [line.id for line, _order, _pid in rows]
    overrides: dict[int, MaintenanceManualCostOverride] = {}
    if line_ids:
        for override in db.execute(
            select(MaintenanceManualCostOverride)
            .where(MaintenanceManualCostOverride.line_id.in_(line_ids))
        ).scalars():
            overrides[override.line_id] = override
    record_access_log(ctx, "download", "maintenance_project_master_rows",
                      {"project_id": project_id, "sheet": sheet})
    return {
        "sheet": sheet,
        "total": len(rows),
        "rows": [
            {
                "line_id": line.id,
                "order_no": order.order_no,
                "order_date": order.order_date.isoformat() if order.order_date else None,
                "sales_order_no": order.linked_sales_order_no or "",
                "project_raw": order.project_raw or "",
                "pn_std": line.pn_std or line.pn_raw or "",
                "description": line.description or "",
                "qty": float(line.qty) if line.qty is not None else None,
                "return_qty": float(line.return_qty) if line.return_qty is not None else None,
                "serial_numbers": line.serial_numbers or "",
                "warehouse": order.warehouse or "",
                "cost_source": line.cost_source or "",
                "unit_cost_ex_tax": (
                    float(line.unit_cost_ex_tax)
                    if line.unit_cost_ex_tax is not None else None),
                "unit_cost_inc_tax": (
                    float(line.unit_cost_inc_tax)
                    if line.unit_cost_inc_tax is not None else None),
                "change_reason": (
                    override.reason or ""
                    if (override := overrides.get(line.id)) is not None else ""),
            }
            for line, order, _pid in rows
        ],
    }


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
        if get_settings().maintenance_project_master_v2_enabled:
            plan = master.validate_project_master_v2(db, project_id=project_id, data=data)
            return {
                "valid": True,
                "protocol_id": master.V2_PROTOCOL_ID,
                "template_version": master.V2_TEMPLATE_VERSION,
                "project_id": project_id,
                "sheets": list(plan.sheets),
                **plan.summary,
                # #265 契约：作废预览（03 显式 VOID + 04 显式 VOID/缺行），
                # apply 前对用户可见（前端 WorkbookRoundTrip 两阶段确认）。
                "will_void_rows": [dict(r) for r in plan.will_void_rows],
                "warnings": [],
            }
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
        if get_settings().maintenance_project_master_v2_enabled:
            plan = master.validate_project_master_v2(db, project_id=project_id, data=data)
            result = master.apply_project_master_v2(
                db, plan, operated_by=_operator(ident), import_batch_id=str(uuid.uuid4())
            )
        else:
            plan = master.validate(db, project_id=project_id, data=data)
            result = master.apply(db, plan, operated_by=_operator(ident),
                                  import_batch_id=str(uuid.uuid4()))
    except ec.WorkbookError as exc:
        db.rollback()
        _fail(exc)
    record_access_log(ctx, "apply", "maintenance_project_master_workbook",
                      {"project_id": project_id, **plan.summary})
    return {"project_id": project_id, **result}
