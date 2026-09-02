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
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.maintenance_expense_collection_workbook import (
    _ACTION_KEY,
    _XLSX_MEDIA,
    _operator,
    _read_upload,
    _read_upload_with_takeover,
    _require_profit_visibility,
)
from app.api.maintenance_project_scope import (
    require_maintenance_project_access,
    resolve_visible_project_ids,
)
from app import config
from app.auth import current_identity, current_role, require_admin
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
from app.services import maintenance_collection_milestone_restore as milestone_restore
from app.services import maintenance_project_master_workbook as master
from app.services import maintenance_workbook_renderer

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

_MASTER = "/projects/stable/{project_id}/master-workbook"
_GLOBAL = "/spare-part-lines"


class MilestoneRestoreItem(BaseModel):
    entity_id: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)
    contract_no: str = Field(min_length=1, max_length=64)
    sequence: int = Field(ge=1, le=24)
    planned_date: date
    planned_amount: Decimal = Field(gt=0, lt=Decimal("1000000000000"))
    date_precision: str = Field(pattern="^(day|month)$")


class MilestoneRestoreRequest(BaseModel):
    reason: str = Field(min_length=4, max_length=500)
    items: list[MilestoneRestoreItem] = Field(min_length=1, max_length=24)


def _require_master_edit(
    project_id: str,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> None:
    """项目总表上传/校验门（2026-09-02 拍板）。

    管理员/全量账号走既有 action 键（含 data_profit）；
    项目负责人（primary_manager 挂靠）与项目销售（canonical salesperson）
    对本人项目拥有全量编辑权（含成本/合同额列——当日拍板放开）。
    """
    if not config.ENABLE_RBAC or ctx.role == "admin":
        return
    from app import permissions as _perm
    from app.services import maintenance_project_assignments as _assignments

    perms = (
        ctx.permissions
        if ctx.permissions is not None
        else _perm.effective(ctx.role, None)
    )
    if perms.get(_ACTION_KEY, False) and perms.get("data_profit", False):
        return
    if _assignments.is_project_workbook_editor(
            db, project_id=project_id, user_ctx=ctx):
        return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "项目总表编辑需要上传权限，或为本项目的负责人/销售",
    )


def _require_contract_amount_manage(
    ctx: UserContext,
    ident: dict,
    db: Session | None = None,
    project_id: str | None = None,
) -> None:
    """合同额改单元格门槛（2026-09-02 拍板放开到项目负责人/销售）。

    仍要求可追责的实名系统账号；权限二选一：
    管理口径（action_maintenance_project_manage + data_profit）或
    本项目负责人/销售（此时 project_id/db 必填）。
    """
    if (ident.get("authn") != "sys_user" or ident.get("fb")
            or not ident.get("sub")):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "修改项目合同总额必须使用实名系统账号",
        )
    if not config.ENABLE_RBAC:
        return
    from app import permissions as _perm

    perms = ctx.permissions if ctx.permissions is not None else _perm.effective(ctx.role, None)
    if perms.get("action_maintenance_project_manage", False) and perms.get(
            "data_profit", False):
        return
    if db is not None and project_id:
        from app.services import maintenance_project_assignments as _assignments

        if _assignments.is_project_workbook_editor(
                db, project_id=project_id, user_ctx=ctx):
            return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "修改项目合同总额需要维保项目主档管理权限和经营数据权限，"
        "或为本项目的负责人/销售",
    )


def _require_real_admin_restore(ctx: UserContext, ident: dict) -> None:
    """恢复已作废节点必须是可追责的实名管理员，不接受共享口令 token。"""

    if (
        ident.get("authn") != "sys_user"
        or ident.get("fb")
        or not ident.get("sub")
        or ident.get("sub") != ctx.user_id
        or ctx.role != "admin"
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "恢复已作废回款计划必须使用实名管理员账号",
        )


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
    busy = exc.code == "cost_recompute_busy"
    stale = exc.code in {
        "stale_cost_override",
        "stale_cost_fact",
        "stale_workbook",
        "row_conflicts",
    }
    raise HTTPException(
        status.HTTP_409_CONFLICT if (busy or stale) else status.HTTP_422_UNPROCESSABLE_CONTENT,
        {"code": exc.code, "message": exc.message, "issues": exc.issues},
        headers={"Retry-After": "5"} if busy else None,
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
    visible_project_ids = resolve_visible_project_ids(db, ctx)
    try:
        content = master.build_global_lines(
            db,
            preset=range_preset,
            date_from=parsed_from,
            date_to=parsed_to,
            allowed_project_ids=visible_project_ids,
        )
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
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(_ACTION_KEY, require_data="data_profit")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    data = await _read_upload(request)
    visible_project_ids = resolve_visible_project_ids(db, ctx)
    try:
        plan = master.validate_global(
            db,
            data=data,
            allowed_project_ids=visible_project_ids,
        )
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
    visible_project_ids = resolve_visible_project_ids(db, ctx)
    try:
        plan = master.validate_global(
            db,
            data=data,
            allowed_project_ids=visible_project_ids,
        )
        # validate 后项目负责人/销售范围可能已变化；apply 前重新物化一次，
        # service 还会逐行复核，确保范围收缩时整份零写入。
        visible_project_ids = resolve_visible_project_ids(db, ctx)
        result = master.apply_global_lines(
            db,
            plan,
            operated_by=_operator(ident),
            import_batch_id=str(uuid.uuid4()),
            allowed_project_ids=visible_project_ids,
        )
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
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    """项目面板「报销」tab 的只读行（含备注，#47）。

    与 04_报销订单 sheet 同源同口径（稳定 attribution + 当前唯一合同），
    页面只**展示**——改金额/备注仍只能走「下载 → 改 → 上传覆盖」（#40）。
    """
    response.headers["Cache-Control"] = "no-store"
    rows = ec.project_expenses(db, project_id)
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
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(require_maintenance_project_access),
):
    wanted = (tuple(s.strip() for s in sheets.split(",") if s.strip())
              if sheets else master.ALL_SHEETS)
    # 2026-09-02 拍板：项目负责人/销售对本人项目全量可见（含成本列）。
    if not config.ENABLE_RBAC or ctx.role == "admin":
        pass
    else:
        from app import permissions as _perm
        from app.services import maintenance_project_assignments as _assignments

        _perms = (ctx.permissions if ctx.permissions is not None
                  else _perm.effective(ctx.role, None))
        _allowed = (_perms.get("data_profit", False)
                    or _assignments.is_project_workbook_editor(
                        db, project_id=project_id, user_ctx=ctx))
        if not _allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "下载项目总表需要经营数据权限，或为本项目的负责人/销售")
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


@router.get(_MASTER + "/collection-plan")
def get_collection_plan(
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _data: None = Depends(_require_profit_visibility),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    """回款计划（02）+ 到款状态：计划期次对比实收累计，供回款 tab 展示待回款。"""
    from app.models.maintenance_manager import MaintenanceCollectionMilestone
    from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot
    from app.business_time import business_today

    contracts = master.ec._contracts(db, project_id)
    contract_by_id = {c.project_contract_id: c.contract_no for c in contracts}
    milestones = list(db.scalars(select(MaintenanceCollectionMilestone).where(
        MaintenanceCollectionMilestone.project_id == project_id,
        MaintenanceCollectionMilestone.is_active.is_(True),
    ).order_by(
        MaintenanceCollectionMilestone.project_contract_id,
        MaintenanceCollectionMilestone.sequence,
    )))
    # 每份合同最新 confirmed 快照的累计实收（待回款判定基准）
    actual: dict[str, Decimal] = {}
    for c in contracts:
        snap = db.scalar(select(MaintenanceCollectionSnapshot).where(
            MaintenanceCollectionSnapshot.project_contract_id == c.project_contract_id,
            MaintenanceCollectionSnapshot.status == "confirmed",
        ).order_by(MaintenanceCollectionSnapshot.report_month.desc()))
        actual[c.project_contract_id] = (
            snap.cumulative_amount if snap and snap.cumulative_amount is not None
            else Decimal("0"))
    today = business_today()
    rows = []
    cum_planned: dict[str, Decimal] = {}
    for m in milestones:
        cid = m.project_contract_id
        cum_planned[cid] = cum_planned.get(cid, Decimal("0")) + (m.planned_amount or Decimal("0"))
        cum_actual = actual.get(cid, Decimal("0"))
        if cum_actual >= cum_planned[cid] and cum_planned[cid] > 0:
            state = "paid"
        elif cum_actual > (cum_planned[cid] - (m.planned_amount or Decimal("0"))):
            state = "partial"
        else:
            state = "pending"
        if state != "paid" and m.planned_date is not None and m.planned_date < today:
            state = "overdue"
        rows.append({
            "milestone_id": m.milestone_id,
            "contract_no": contract_by_id.get(cid, ""),
            "sequence": m.sequence,
            "planned_date": m.planned_date.isoformat() if m.planned_date else None,
            "date_precision": m.date_precision,
            "planned_amount": str(m.planned_amount) if m.planned_amount is not None else None,
            "cumulative_planned": str(cum_planned[cid]),
            "cumulative_actual": str(actual.get(cid, Decimal("0"))),
            "arrival_state": state,
            "follow_up_status": m.follow_up_status,
            "note": m.follow_up_note,
            "version": m.version,
        })
    return {"total": len(rows), "rows": rows}


@router.post("/projects/stable/{project_id}/collection-milestones/restore")
def restore_collection_milestones(
    body: MilestoneRestoreRequest,
    response: Response,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _admin: str = Depends(require_admin),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(_ACTION_KEY, require_data="data_profit")
    ),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    """实名管理员整批恢复已作废的回款计划；事实必须与 Excel 完全一致。"""

    response.headers["Cache-Control"] = "no-store"
    _require_real_admin_restore(ctx, ident)
    try:
        payload = milestone_restore.restore_collection_milestones(
            db,
            project_id=project_id,
            specs=[
                milestone_restore.MilestoneRestoreSpec(
                    entity_id=item.entity_id,
                    expected_version=item.expected_version,
                    contract_no=item.contract_no,
                    sequence=item.sequence,
                    planned_date=item.planned_date,
                    planned_amount=item.planned_amount,
                    date_precision=item.date_precision,
                )
                for item in body.items
            ],
            reason=body.reason,
            operated_by=_operator(ident),
        )
        db.commit()
    except milestone_restore.MilestoneRestoreNotFound as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "milestone_restore_not_found", "message": str(exc)},
        ) from exc
    except milestone_restore.MilestoneRestoreConflict as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "milestone_restore_conflict", "message": str(exc)},
        ) from exc
    except milestone_restore.MilestoneRestoreInvalid as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "milestone_restore_invalid", "message": str(exc)},
        ) from exc
    except Exception:
        db.rollback()
        raise
    record_access_log(
        ctx,
        "restore",
        "maintenance_collection_milestones",
        {
            "project_id": project_id,
            "restored_count": payload["restored_count"],
            "idempotent_replay_count": payload["idempotent_replay_count"],
        },
    )
    return payload


@router.get(_MASTER + "/rows")
def list_master_rows(
    project_id: str = Path(..., min_length=1, max_length=36),
    sheet: str = Query(..., description="sheet 名，当前支持 03_备件订单"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _data: None = Depends(_require_profit_visibility),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    """备件成本 tab 的 web 呈现：03_备件订单 行级（PN）只读数据源（2026-08-17）。"""
    if (get_settings().maintenance_project_master_v2_enabled
            and sheet in {master.V2_SHEET_PARTS, master.SHEET_PARTS}):
        rows = master._assigned_lines(db, project_id=project_id, window=None)
        line_ids = [line.id for line, _order, _pid in rows]
        overrides = {
            item.line_id: item for item in db.scalars(select(MaintenanceManualCostOverride).where(
                MaintenanceManualCostOverride.line_id.in_(line_ids),
                MaintenanceManualCostOverride.active.is_(True),
            ))
        } if line_ids else {}

        def _v2_row(line, order):
            override = overrides.get(line.id)
            inc = master._line_cost_evidence(line, override, basis="inc")
            ex = master._line_cost_evidence(line, override, basis="ex")
            known = inc["tier"] != "missing"
            resolved_source = inc["source"] if known else (line.cost_source or "none")
            can_refill = (
                not known
                and line.cost_source in (None, "none")
                and override is None
            )
            missing_kind = None
            if not known:
                missing_kind = (
                    "out_of_scope"
                    if line.cost_source is None
                    else "none"
                    if line.cost_source == "none"
                    else "invalid_cost_fact"
                )
            return {
                "line_id": line.id,
                "part_id": line.part_id,
                "order_no": order.order_no,
                "order_date": order.order_date.isoformat() if order.order_date else None,
                "pn_std": line.pn_std or line.pn_raw or "",
                "description": line.description or "",
                "qty": str(line.qty) if line.qty is not None else None,
                "return_qty": str(line.return_qty) if line.return_qty is not None else None,
                "returned_qty": (
                    str(line.returned_qty) if line.returned_qty is not None else None
                ),
                "pending_return_qty": (
                    str(line.pending_return_qty)
                    if line.pending_return_qty is not None else None
                ),
                "cost_amount_inc_tax": (
                    str(inc["amount"]) if inc["amount"] is not None else None
                ),
                "warehouse": order.warehouse or "",
                "cost_source": resolved_source,
                "cost_source_label": maintenance_workbook_renderer.SOURCE_LABELS.get(
                    resolved_source, "成本事实异常" if not known else "成本缺失"
                ),
                "confidence": (
                    "high" if resolved_source == "manual"
                    else line.confidence if known
                    else "none"
                ),
                "unit_cost_ex_tax": (
                    str(ex["unit_cost"]) if ex["unit_cost"] is not None else None
                ),
                "unit_cost_inc_tax": (
                    str(inc["unit_cost"]) if inc["unit_cost"] is not None else None
                ),
                "missing_kind": missing_kind,
                "can_refill": can_refill,
                "manual_unit_cost_ex_tax": (
                    str(override.unit_cost_ex_tax) if override else None
                ),
                "manual_reason": override.reason if override else None,
            }

        return {
            "sheet": sheet,
            "total": len(rows),
            "rows": [_v2_row(line, order) for line, order, _pid in rows],
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
            .where(
                MaintenanceManualCostOverride.line_id.in_(line_ids),
                MaintenanceManualCostOverride.active.is_(True),
            )
        ).scalars():
            overrides[override.line_id] = override
    record_access_log(ctx, "download", "maintenance_project_master_rows",
                      {"project_id": project_id, "sheet": sheet})

    def _legacy_row(line, order):
        override = overrides.get(line.id)
        inc = master._line_cost_evidence(line, override, basis="inc")
        ex = master._line_cost_evidence(line, override, basis="ex")
        return {
            "line_id": line.id,
            "order_no": order.order_no,
            "order_date": order.order_date.isoformat() if order.order_date else None,
            "sales_order_no": order.linked_sales_order_no or "",
            "project_raw": order.project_raw or "",
            "pn_std": line.pn_std or line.pn_raw or "",
            "description": line.description or "",
            "qty": float(line.qty) if line.qty is not None else None,
            "return_qty": float(line.return_qty) if line.return_qty is not None else None,
            "returned_qty": (
                float(line.returned_qty) if line.returned_qty is not None else None
            ),
            "pending_return_qty": (
                float(line.pending_return_qty)
                if line.pending_return_qty is not None else None
            ),
            "serial_numbers": line.serial_numbers or "",
            "warehouse": order.warehouse or "",
            "cost_source": inc["source"] if inc["tier"] != "missing" else "",
            "unit_cost_ex_tax": (
                float(ex["unit_cost"]) if ex["unit_cost"] is not None else None
            ),
            "unit_cost_inc_tax": (
                float(inc["unit_cost"]) if inc["unit_cost"] is not None else None
            ),
            "cost_amount_inc_tax": (
                float(inc["amount"]) if inc["amount"] is not None else None
            ),
            "cost_quality": inc["tier"],
            "change_reason": override.reason or "" if override is not None else "",
        }

    return {
        "sheet": sheet,
        "total": len(rows),
        "rows": [_legacy_row(line, order) for line, order, _pid in rows],
    }


@router.post(_MASTER + "/validate")
async def validate_project_master(
    project_id: str = Path(..., min_length=1, max_length=36),
    request: Request = None,
    response: Response = None,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _edit: None = Depends(_require_master_edit),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    data, force_takeover = await _read_upload_with_takeover(request)
    try:
        if get_settings().maintenance_project_master_v2_enabled:
            plan = master.validate_project_master_v2(
                db, project_id=project_id, data=data, user_ctx=ctx)
            if plan.contract_amount_change is not None:
                _require_contract_amount_manage(
                    ctx, ident, db=db, project_id=project_id)
            record_access_log(
                ctx, "workbook_validate", "maintenance_project_master_workbook",
                {"project_id": project_id, "conflicts": len(plan.conflicts),
                 "changes": len(plan.field_changes)})
            return {
                "valid": True,
                "protocol_id": master.V2_PROTOCOL_ID,
                "template_version": master.V2_TEMPLATE_VERSION,
                "project_id": project_id,
                "sheets": list(plan.sheets),
                **plan.summary,
                # 2.7.0：字段级改动预览 + 行级冲突预览（前端据此提供接管入口）
                "changes": [dict(item) for item in plan.field_changes],
                "conflicts": [dict(item) for item in plan.conflicts],
                "overridden": [dict(item) for item in plan.overridden],
                # #265 契约：作废预览（03 显式 VOID + 04 显式 VOID/缺行），
                # apply 前对用户可见（前端 WorkbookRoundTrip 两阶段确认）。
                "will_void_rows": [dict(r) for r in plan.will_void_rows],
                "will_reassign_orders": [{
                    "source_order_id": change.source_order_id,
                    "order_no": change.order_no,
                    "from_project_id": change.previous_project_id,
                    "from_project_name": change.previous_project_name,
                    "to_project_id": project_id,
                } for change in plan.assignment_changes],
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
    _edit: None = Depends(_require_master_edit),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    """上传覆盖。文件里有哪张 sheet 就应用哪张——单 sheet 上传走同一入口。

    2.7.0：行级冲突默认 409（带三值明细）；multipart 额外字段
    force_takeover=true 时按用户值强制接管并逐项留痕。
    """
    response.headers["Cache-Control"] = "no-store"
    data, force_takeover = await _read_upload_with_takeover(request)
    try:
        if get_settings().maintenance_project_master_v2_enabled:
            plan = master.validate_project_master_v2(
                db, project_id=project_id, data=data, user_ctx=ctx,
                force_takeover=force_takeover)
            if plan.contract_amount_change is not None:
                _require_contract_amount_manage(
                    ctx, ident, db=db, project_id=project_id)
            if plan.conflicts and not force_takeover:
                record_access_log(
                    ctx, "workbook_apply_conflict",
                    "maintenance_project_master_workbook",
                    {"project_id": project_id,
                     "conflicts": len(plan.conflicts)})
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    {
                        "code": "row_conflicts",
                        "message": "部分行已被他人更新，本次上传未写入任何数据；"
                                   "请对照下方明细处理后重试，或确认后强制接管。",
                        "conflicts": [dict(item) for item in plan.conflicts],
                    },
                )
            result = master.apply_project_master_v2(
                db, plan, operated_by=_operator(ident), import_batch_id=str(uuid.uuid4()),
                user_ctx=ctx,
            )
        else:
            plan = master.validate(db, project_id=project_id, data=data)
            result = master.apply(db, plan, operated_by=_operator(ident),
                                  import_batch_id=str(uuid.uuid4()))
    except ec.WorkbookError as exc:
        db.rollback()
        record_access_log(
            ctx, "workbook_apply_failed",
            "maintenance_project_master_workbook",
            {"project_id": project_id, "code": exc.code,
             "message": exc.message[:200]})
        _fail(exc)
    record_access_log(
        ctx, "workbook_apply" if not force_takeover else "workbook_apply_takeover",
        "maintenance_project_master_workbook",
        {"project_id": project_id, "overridden": len(result.get("overridden", [])),
         **plan.summary})
    return {"project_id": project_id, **result}
