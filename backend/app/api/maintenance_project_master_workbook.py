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
import hashlib
import logging
import re
import uuid
from datetime import date
from decimal import Decimal

from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.date_validation import validate_from_to_query
from app.api.maintenance_expense_collection_workbook import (
    _ACTION_KEY,
    _XLSX_MEDIA,
    _operator,
    _read_upload,
    _read_upload_with_takeover,
    _require_profit_visibility,
)
from app.api.maintenance_boss_board import contract_no_filter
from app.api.maintenance_project_scope import (
    require_maintenance_project_access,
    resolve_visible_project_ids,
)
from app.api import maintenance_project_scope as auth_transport
from app.services import maintenance_master_workbook_access as master_access
from app.services import maintenance_master_command_access as master_command
from app.services import maintenance_global_lines_access as global_lines_access
from app.services import maintenance_milestone_restore_access as restore_access
from app.authz import runtime
from app.services import maintenance_authorization as foundation

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
from app.services.maintenance_site_issue_facts import SiteIssueEditError
from app.services import maintenance_row_workbench as row_workbench
from app.services.maintenance_row_filter_registry import parse_filters
from app.api.maintenance_row_workbench import reject_row_sort

router = APIRouter(prefix="/maintenance", tags=["maintenance"])
_log = logging.getLogger(__name__)

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
    authorization_precondition: dict | None = None
    reason: str = Field(min_length=4, max_length=500)
    items: list[MilestoneRestoreItem] = Field(min_length=1, max_length=24)


def _require_master_edit(
    project_id: str,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> None:
    """项目总表上传/校验门（D-03，2026-09-02 拍板）。

    管理员/全量账号走既有 action 键（含 data_profit）；其余按 D-03 原句：
    「项目负责人对本人项目工作簿全部字段可见可改（含成本与合同额）；销售限
    本人项目，成本与合同额仍受 data_profit 利润键控制」——V2 总表整本带成本列，
    所以没有利润键的销售不放行。判定本体在
    ``maintenance_project_assignments.can_edit_master_workbook``，展示板项目卡
    与稳定项目详情向前端下发的同名字段用的是同一函数。
    """
    from app.services import maintenance_project_assignments as _assignments

    if _assignments.can_edit_master_workbook(
            db, project_id=project_id, user_ctx=ctx):
        return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "项目总表编辑需要上传权限，或为本项目的负责人/销售",
    )


def _legacy_master_validate_edit(project_id: str, db: Session = Depends(get_db),
                                 ctx: UserContext = Depends(get_current_user_context)) -> None:
    if not any(runtime.is_authoritative(db, resource) for resource in master_command.RESOURCES):
        _require_master_edit(project_id, db, ctx)


def _require_contract_amount_manage(
    ctx: UserContext,
    ident: dict,
    db: Session | None = None,
    project_id: str | None = None,
) -> None:
    """合同额改单元格门槛（D-03，2026-09-02 拍板放开到项目负责人）。

    仍要求可追责的实名系统账号；权限二选一：
    管理口径（action_maintenance_project_manage + data_profit）或
    本项目负责人 / 持利润键的本项目销售（``is_project_workbook_editor``，
    此时 project_id/db 必填）——D-03：销售的合同额仍受 data_profit 控制。
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


def _project_note_operator(db: Session, ident: dict, ctx: UserContext) -> str:
    """A workbook editor does not automatically get master-note write rights."""
    from app.api.maintenance_projects import _real_operator

    require_action("action_maintenance_project_manage", require_data="data_profit")(ctx)
    return _real_operator(db, ident)


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
        "stale_project_note",
        "stale_site_issue_merge",
    }
    detail = {"code": exc.code, "message": exc.message, "issues": exc.issues}
    for key in ("rows", "total_count", "fix_hint"):
        if key in exc.details:
            detail[key] = exc.details[key]
    if exc.code == "row_conflicts":
        # 前端 WorkbookRoundTrip.extractConflicts 只认 detail.conflicts；端点自己
        # 拼的前置 409 用的就是这个键。服务层抛的 row_conflicts 必须给同一个键，
        # 否则冲突表渲染成空白（2026-09-03 评审）。
        detail["conflicts"] = [dict(item) for item in exc.issues]
    raise HTTPException(
        status.HTTP_409_CONFLICT if (busy or stale) else status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail,
        headers={"Retry-After": "5"} if busy else None,
    ) from exc


def _safe_filename_part(value: str) -> str:
    return master_access.safe_filename_part(value)


def _workbook_filename(db: Session, project_id: str, wanted: tuple[str, ...]) -> str:
    return master_access.workbook_filename(db, project_id, wanted)


# ---------------------------------------------------------- 主页全局备件行级表

@router.get(_GLOBAL + ".xlsx", dependencies=[Depends(validate_from_to_query)])
def download_global_lines(
    range_preset: str = Query("this_month", alias="range"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page("maintenance_global_workbook", "page_maintenance")),
    _data: None = Depends(auth_transport.legacy_gate("maintenance_global_workbook", _require_profit_visibility)),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for("maintenance_global_workbook")),
    view: Literal['authorization'] | None = Query(None),
    authorization_precondition: str | None = Query(None),
):
    """全项目备件行级表（系统自动回填价之后）。时间预设见 #38。"""
    if identity is not None:
        try:
            content = master_access.read_global_workbook(sessionmaker(bind=db.get_bind()), identity,
                preset=range_preset, date_from=date_from, date_to=date_to, authorization_view=view == 'authorization',
                authorization_precondition=foundation.decode_precondition(authorization_precondition) if view != 'authorization' else None)
        except ec.WorkbookError as exc:
            _fail(exc)
        if view == 'authorization':
            return content
        return _xlsx(content, f"spare-part-lines-{range_preset}.xlsx")
    visible_project_ids = resolve_visible_project_ids(db, ctx)
    try:
        content = master.build_global_lines(
            db,
            preset=range_preset,
            date_from=date_from,
            date_to=date_to,
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
    _page: None = Depends(auth_transport.legacy_page("maintenance_global_workbook","page_maintenance")),
    _action: None = Depends(auth_transport.legacy_gate("maintenance_global_workbook",require_action(_ACTION_KEY,require_data="data_profit"))),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for("maintenance_global_workbook")),
    view: Literal["authorization"] | None = Query(None),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    data = await _read_upload(request)
    if identity is not None:
        try:
            return global_lines_access.execute(db,identity,entry_id=global_lines_access.VALIDATE,data=data,view=view,
                precondition=(await request.form()).get("authorization_precondition"))
        except ec.WorkbookError as exc:
            _fail(exc)
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
    _page: None = Depends(auth_transport.legacy_page("maintenance_global_workbook","page_maintenance")),
    _action: None = Depends(auth_transport.legacy_gate("maintenance_global_workbook",require_action(_ACTION_KEY,require_data="data_profit"))),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for("maintenance_global_workbook")),
    view: Literal["authorization"] | None = Query(None),
) -> dict:
    """改价/补价上传覆盖＝真实源（#38）。整份事务，任何一行不合法则整份 422。"""
    response.headers["Cache-Control"] = "no-store"
    data = await _read_upload(request)
    if identity is not None:
        try:
            return global_lines_access.execute(db,identity,entry_id=global_lines_access.APPLY,data=data,view=view,
                precondition=(await request.form()).get("authorization_precondition"))
        except ec.WorkbookError as exc:
            _fail(exc)
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
    _page: None = Depends(auth_transport.legacy_page("maintenance_expense", "page_maintenance")),
    _data: None = Depends(auth_transport.legacy_gate("maintenance_expense", _require_profit_visibility)),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(auth_transport.legacy_project_scope("maintenance_expense")),
    identity=Depends(auth_transport.identity_for("maintenance_expense")),
    view: Literal['authorization'] | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    """项目面板「报销」tab 的只读行（含备注，#47）。

    与 04_报销订单 sheet 同源同口径（稳定 attribution + 当前唯一合同），
    页面只**展示**——改金额/备注仍只能走「下载 → 改 → 上传覆盖」（#40）。
    """
    response.headers["Cache-Control"] = "no-store"
    if identity is not None:
        return master_access.read_project_data(sessionmaker(bind=db.get_bind()), identity,
            entry_id=master_access.EXPENSE_ROWS, project_id=project_id, authorization_view=view == 'authorization', page=page, page_size=page_size,
            authorization_precondition=foundation.decode_precondition(authorization_precondition) if view != 'authorization' else None)
    return master_access.expense_rows(db, project_id, page, page_size)


# ---------------------------------------------------------- 项目总表 / 单 sheet

@router.get(_MASTER + ".xlsx")
def download_project_master(
    project_id: str = Path(..., min_length=1, max_length=36),
    sheets: str | None = Query(None, description="逗号分隔的 sheet 名；缺省=全六张"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(master_access.RESOURCES, "page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(auth_transport.legacy_project_scope(master_access.RESOURCES)),
    identity=Depends(auth_transport.identity_for(*master_access.RESOURCES)),
    view: Literal['authorization'] | None = Query(None),
    authorization_precondition: str | None = Query(None),
):
    if identity is not None:
        try:
            result = master_access.read_master_workbook(sessionmaker(bind=db.get_bind()), identity,
                project_id=project_id, sheets=sheets, authorization_view=view == 'authorization',
                authorization_precondition=foundation.decode_precondition(authorization_precondition) if view != 'authorization' else None)
        except ec.WorkbookError as exc:
            _fail(exc)
        if view == 'authorization':
            return result
        content, filename = result
        if content is None:
            raise HTTPException(404, '项目不存在')
        return _xlsx(content, filename)
    wanted = (tuple(s.strip() for s in sheets.split(",") if s.strip())
              if sheets else master.ALL_SHEETS)
    # D-03：负责人对本人项目全量可见（含成本列）；销售的成本仍受 data_profit
    # 控制——无利润键的销售在 is_project_workbook_editor 里就不算编辑者。
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
    _page: None = Depends(auth_transport.legacy_page("maintenance_collection", "page_maintenance")),
    _data: None = Depends(auth_transport.legacy_gate("maintenance_collection", _require_profit_visibility)),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(auth_transport.legacy_project_scope("maintenance_collection")),
    identity=Depends(auth_transport.identity_for("maintenance_collection")),
    view: Literal['authorization'] | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    """回款计划（02）+ 到款状态：计划期次对比实收累计，供回款 tab 展示待回款。"""
    if identity is not None:
        return master_access.read_project_data(sessionmaker(bind=db.get_bind()), identity,
            entry_id=master_access.COLLECTION_PLAN, project_id=project_id, authorization_view=view == 'authorization',
                authorization_precondition=foundation.decode_precondition(authorization_precondition) if view != 'authorization' else None)
    return master_access.collection_plan(db, project_id)


@router.post("/projects/stable/{project_id}/collection-milestones/restore")
def restore_collection_milestones(
    body: MilestoneRestoreRequest,
    response: Response,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _admin: str = Depends(auth_transport.legacy_gate("maintenance_collection",lambda ctx: require_admin(ctx.role))),
    _page: None = Depends(auth_transport.legacy_page("maintenance_collection","page_maintenance")),
    _action: None = Depends(
        auth_transport.legacy_gate("maintenance_collection",require_action(_ACTION_KEY,require_data="data_profit"))
    ),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for("maintenance_collection")),
    view: Literal["authorization"] | None = Query(None),
    _scope: None = Depends(auth_transport.legacy_project_scope("maintenance_collection")),
) -> dict:
    """实名管理员整批恢复已作废的回款计划；事实必须与 Excel 完全一致。"""

    response.headers["Cache-Control"] = "no-store"
    if identity is None:
        _require_real_admin_restore(ctx, ident)
    try:
        if identity is not None:
            return restore_access.execute(db,identity,project_id=project_id,command=body.model_dump(exclude={"authorization_precondition"}),view=view,precondition=body.authorization_precondition)
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
    order_no: str | None = Query(
        None, min_length=1, max_length=64, description="只看这张需求单（WBDD 单号精确相等）"),
    contract_no: str | None = Depends(contract_no_filter),
    page: int | None = Query(None, ge=1, description="省略＝全量返回（旧协议）"),
    page_size: int | None = Query(
        None, ge=1, le=200, description="只在给了 page 时生效，省略＝20"),
    filters: str | None = Query(None),
    _sort: None = Depends(reject_row_sort),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page("maintenance_row_workbench", "page_maintenance")),
    _data: None = Depends(auth_transport.legacy_gate("maintenance_row_workbench", _require_profit_visibility)),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(auth_transport.legacy_project_scope("maintenance_row_workbench")),
    identity=Depends(auth_transport.identity_for("maintenance_row_workbench")),
    view: Literal['authorization'] | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    """备件成本 tab 的 web 呈现：03_备件订单 行级（PN）只读数据源（2026-08-17）。"""
    if identity is not None:
        return master_access.read_master_rows(sessionmaker(bind=db.get_bind()), identity,
            project_id=project_id, sheet=sheet, order_no=order_no, contract_no=contract_no,
            page=page, page_size=page_size, filters=filters, authorization_view=view == 'authorization',
            authorization_precondition=foundation.decode_precondition(authorization_precondition) if view != 'authorization' else None)
    return master_access.build_master_rows(db, ctx, project_id=project_id, sheet=sheet,
        order_no=order_no, contract_no=contract_no, page=page, page_size=page_size, filters=filters)


@router.post(_MASTER + "/validate")
async def validate_project_master(
    project_id: str = Path(..., min_length=1, max_length=36),
    request: Request = None,
    response: Response = None,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(master_command.RESOURCES, "page_maintenance")),
    _edit: None = Depends(_legacy_master_validate_edit),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(auth_transport.legacy_project_scope(master_command.RESOURCES)),
    identity=Depends(auth_transport.identity_for(*master_command.RESOURCES)),
    view: Literal['authorization'] | None = Query(None),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    data, force_takeover = await _read_upload_with_takeover(request)
    try:
        if identity is not None:
            precondition = foundation.decode_precondition((await request.form()).get('authorization_precondition'))
            result = master_command.validate_upload(sessionmaker(bind=db.get_bind()), identity,
                project_id=project_id, content=data, force_takeover=force_takeover,
                authorization_view=view == 'authorization', authorization_precondition=precondition)
            if view == 'authorization':
                return result
            plan, principal, authorization_context = result
            if isinstance(plan, master.MasterV2Plan):
                payload = master_command.validation_payload(plan, project_id, f'authz-user:{principal.id}')
            else:
                payload = {'project_id': project_id, 'valid': True, 'sheets': list(plan.sheets), **plan.summary}
            return {**payload, 'authorization_context': authorization_context}
        if get_settings().maintenance_project_master_v2_enabled:
            # force_takeover 必须透传：接管预检要让用户在按下「强制接管」之前
            # 看到自己将要覆盖的具体值（plan.overridden）。不传则预检永远返回
            # 普通冲突计划，客户端无从预览（2026-09-03 评审 P2）。
            plan = master.validate_project_master_v2(
                db, project_id=project_id, data=data, user_ctx=ctx,
                force_takeover=force_takeover)
            if plan.contract_amount_change is not None:
                _require_contract_amount_manage(
                    ctx, ident, db=db, project_id=project_id)
            operator = (_project_note_operator(db, ident, ctx)
                        if plan.project_note_change is not None else _operator(ident))
            record_access_log(
                ctx, "workbook_validate", "maintenance_project_master_workbook",
                {"project_id": project_id, "conflicts": len(plan.conflicts),
                 "changes": len(plan.field_changes)})
            return master_command.validation_payload(plan, project_id, operator)
        plan = master.validate(db, project_id=project_id, data=data)
    except ec.WorkbookError as exc:
        try:
            # 失败请求也留访问记录；只传 JSON 原始量，不把含日期的计划或
            # 业务差异送入审计，兼容尚无通用 JSON 归一化的主线。
            record_access_log(
                ctx, "workbook_validate", "maintenance_project_master_workbook",
                {"project_id": project_id, "error_code": exc.code,
                 "conflict_count": exc.details.get(
                     "total_count", len(exc.issues) if exc.code == "row_conflicts" else 0)},
            )
        except Exception as audit_exc:  # noqa: BLE001 — 审计不能覆盖原始校验错误
            _log.warning("workbook validation access log failed (%s)",
                         type(audit_exc).__name__)
        _fail(exc)
    return {"project_id": project_id, "valid": True,
            "sheets": list(plan.sheets), **plan.summary}


def _completed_v2_upload(
    db: Session, *, project_id: str, file_sha256: str,
    ident: dict, ctx: UserContext,
) -> dict | None:
    replay = master.get_project_master_v2_replay(
        db, project_id=project_id, file_sha256=file_sha256,
        operated_by=_operator(ident),
    )
    if replay is None:
        return None
    result, requires_amount = replay
    if requires_amount:
        _require_contract_amount_manage(ctx, ident, db=db, project_id=project_id)
    record_access_log(
        ctx, "workbook_apply_replay", "maintenance_project_master_workbook",
        {"project_id": project_id, "replayed": True},
    )
    return {"project_id": project_id, **result}


@router.post(_MASTER + "/apply")
async def apply_project_master(
    project_id: str = Path(..., min_length=1, max_length=36),
    request: Request = None,
    response: Response = None,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(master_command.RESOURCES, "page_maintenance")),
    _edit: None = Depends(_legacy_master_validate_edit),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(auth_transport.legacy_project_scope(master_command.RESOURCES)),
    identity=Depends(auth_transport.identity_for(*master_command.RESOURCES)),
    view: Literal['authorization'] | None = Query(None),
) -> dict:
    """上传覆盖。文件里有哪张 sheet 就应用哪张——单 sheet 上传走同一入口。

    2.7.0：行级冲突默认 409（带三值明细）；multipart 额外字段
    force_takeover=true 时按用户值强制接管并逐项留痕。
    """
    response.headers["Cache-Control"] = "no-store"
    data, force_takeover = await _read_upload_with_takeover(request)
    try:
        if identity is not None:
            from app.services.maintenance_master_apply_access import apply_upload
            form = await request.form()
            return apply_upload(db, identity, project_id=project_id, content=data,
                force_takeover=force_takeover, merge_token=str(form.get('site_issue_merge_token') or ''),
                authorization_view=view == 'authorization',
                authorization_precondition=foundation.decode_precondition(form.get('authorization_precondition')) if view != 'authorization' else None)
        if get_settings().maintenance_project_master_v2_enabled:
            file_sha256 = hashlib.sha256(data).hexdigest()
            replay = _completed_v2_upload(
                db, project_id=project_id, file_sha256=file_sha256,
                ident=ident, ctx=ctx,
            )
            if replay is not None:
                return replay
            try:
                plan = master.validate_project_master_v2(
                    db, project_id=project_id, data=data, user_ctx=ctx,
                    force_takeover=force_takeover)
            except ec.WorkbookError:
                # Another first apply may commit after our initial lookup.
                # Current facts can then fail validation before apply's locks.
                replay = _completed_v2_upload(
                    db, project_id=project_id, file_sha256=file_sha256,
                    ident=ident, ctx=ctx,
                )
                if replay is not None:
                    return replay
                raise
            replay = _completed_v2_upload(
                db, project_id=project_id, file_sha256=file_sha256,
                ident=ident, ctx=ctx,
            )
            if replay is not None:
                return replay
            if plan.contract_amount_change is not None:
                _require_contract_amount_manage(
                    ctx, ident, db=db, project_id=project_id)
            operator = (_project_note_operator(db, ident, ctx)
                        if plan.project_note_change is not None else _operator(ident))
            merge_token = str((await request.form()).get("site_issue_merge_token") or "")
            master.verify_site_issue_merge_token(plan, operator, merge_token)
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
                db, plan, operated_by=operator, import_batch_id=str(uuid.uuid4()),
                user_ctx=ctx,
            )
        else:
            plan = master.validate(db, project_id=project_id, data=data)
            result = master.apply(db, plan, operated_by=_operator(ident),
                                  import_batch_id=str(uuid.uuid4()))
    except SiteIssueEditError as exc:
        db.rollback()
        record_access_log(
            ctx, "workbook_apply_failed", "maintenance_project_master_workbook",
            {"project_id": project_id, "code": exc.code,
             "message": exc.message[:200]},
        )
        raise HTTPException(exc.status_code, {**exc.detail, "issues": []}) from exc
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
