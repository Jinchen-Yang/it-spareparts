"""维保展示板只读端点（plan v1.3 M3-2，§4.4/§4.5 契约）。

七个端点，全部只读；router 受 require_maintenance_boss（flag 关闭 → 整组 404）。
查看权限：page_maintenance_boss（全范围）或 page_maintenance（范围按 M0-B）。
成本字段独立受 data_purchase_cost 控制，无权限时返回 restricted 信封且无侧信道
（响应键集恒定、成本排序 422 而非静默降级）。
"""
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth import current_role
from app.business_time import business_today
from app.db import get_db
from app.maintenance_boss import require_maintenance_boss
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_page,
)
from app.services import maintenance_boss_board as board
from app.services import maintenance_project_export as project_export

router = APIRouter(
    prefix="/maintenance/boss-board",
    tags=["maintenance"],
    dependencies=[Depends(require_maintenance_boss)],
)

def require_board_view(
    ctx: UserContext = Depends(get_current_user_context),
) -> UserContext:
    """page_maintenance_boss（全范围）或 page_maintenance（范围内）任一即可查看。"""
    from app import permissions as perm

    perms = ctx.permissions
    if perms is None:
        perms = perm.template_for(ctx.role)
    safe = perm.runtime_safe(perms)
    if ctx.role == "admin" or safe.get("page_maintenance_boss") or safe.get(
        "page_maintenance"
    ):
        return ctx
    raise HTTPException(status.HTTP_403_FORBIDDEN, "无维保展示板查看权限")


def _allowed_scope(db: Session, ctx: UserContext) -> set[str] | None:
    """None = 全范围。

    M0-B 已于 2026-08-16 改判为**①全部可见**（签署清单 / 增补包 AB-1）：
    展示板的查看权限本身就是勾选名单制——能进这个页面的账号（`page_maintenance`
    或 `page_maintenance_boss`）即视为获授全部项目可见，与既有「老板＋被勾选项目
    经理整套可见」口径一致（REQUIREMENTS #2/#14）。

    2026-08-21（客户反馈「销售只能看到自己的」）：在此恢复收敛，但只对开了
    own_maintenance_projects_only 行键的账号生效——可见集 =
    「我是维保负责人 ∪ 项目销售 = 我的销售名」（maintenance_scope_project_ids）。
    没开键的账号维持 M0-B 全量口径，既有勾选名单行为零变化。
    """
    from app.services import maintenance_project_assignments

    return maintenance_project_assignments.maintenance_scope_project_ids(db, ctx)


def _project_card_scope(db: Session, ctx: UserContext) -> set[str] | None:
    """维保负责人可在卡片墙核对全部项目；其余入口仍沿用本人范围。"""
    if ctx.role == "maintenance_manager":
        return None
    return _allowed_scope(db, ctx)


class ProjectSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=128)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)
    lifecycle: str = Field(default="all", pattern=r"^(ongoing|ended|missing|all)$")
    sort: str = Field(default="name", pattern=r"^(attention|orders|name|known_cost|cost_ratio)$")
    card_status: str | None = Field(default=None, pattern=r"^(normal|warning|alert)$")


class ProjectExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: list[str] = Field(min_length=1, max_length=len(project_export.EXPORT_FIELDS))
    q: str | None = Field(default=None, max_length=128)
    lifecycle: str = Field(default="all", pattern=r"^(ongoing|ended|missing|all)$")
    card_status: str | None = Field(default=None, pattern=r"^(normal|warning|alert)$")
    sort: str = Field(
        default="name",
        pattern=r"^(attention|orders|name|known_cost|cost_ratio)$",
    )


@router.get("/health")
def board_health(
    response: Response,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(require_board_view),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return board.health(db)


@router.get("/summary")
def board_summary(
    response: Response,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(require_board_view),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return board.summary(db, user_ctx=ctx, date_from=date_from, date_to=date_to,
                         allowed_project_ids=_allowed_scope(db, ctx))


@router.get("/attention")
def board_attention(
    response: Response,
    limit: int = Query(10, ge=1, le=10),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(require_board_view),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return board.attention(db, user_ctx=ctx, limit=limit,
                           allowed_project_ids=_allowed_scope(db, ctx))


@router.get("/projects")
def board_projects(
    response: Response,
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    lifecycle: str = Query("all", pattern=r"^(ongoing|ended|missing|all)$"),
    sort: str = Query("name", pattern=r"^(attention|orders|name|known_cost|cost_ratio)$"),
    has_activity: bool | None = Query(None),
    card_status: str | None = Query(None, pattern=r"^(normal|warning|alert)$"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(require_board_view),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if q is not None:
        # 仓库既有约定：GET 带自由文本 → 422，改用 POST /projects/search
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "use_search_endpoint",
             "message": "项目搜索请使用 POST /maintenance/boss-board/projects/search"},
        )
    record_access_log(ctx, "boss_board_projects", "maintenance",
                      {"page": page, "page_size": page_size, "sort": sort})
    try:
        return board.projects(
            db, user_ctx=ctx, page=page, page_size=page_size, lifecycle=lifecycle,
            sort=sort, has_activity=has_activity, card_status_filter=card_status,
            date_from=date_from, date_to=date_to,
            allowed_project_ids=_project_card_scope(db, ctx),
        )
    except board.BoardSortNotPermitted as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "sort_requires_cost_permission",
             "message": "按成本排序需要成本数据权限"},
        ) from exc
    except board.BoardCostContractNotPermitted as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "cost_contract_permission_required",
             "message": "成本及合同财务数据权限"},
        ) from exc


@router.post("/projects/search")
def board_projects_search(
    payload: ProjectSearch,
    response: Response,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(require_board_view),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    record_access_log(ctx, "boss_board_projects_search", "maintenance",
                      {"page": payload.page, "page_size": payload.page_size})
    try:
        return board.projects(
            db, user_ctx=ctx, page=payload.page, page_size=payload.page_size,
            lifecycle=payload.lifecycle, sort=payload.sort, q_text=payload.q,
            card_status_filter=payload.card_status,
            allowed_project_ids=_project_card_scope(db, ctx),
        )
    except board.BoardSortNotPermitted as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "sort_requires_cost_permission",
             "message": "按成本排序需要成本数据权限"},
        ) from exc
    except board.BoardCostContractNotPermitted as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "cost_contract_permission_required",
             "message": "成本及合同财务数据权限"},
        ) from exc


@router.get("/projects/export/options")
def board_project_export_options(
    response: Response,
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(require_board_view),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return project_export.export_options(ctx)


@router.post("/projects/export")
def board_project_export(
    payload: ProjectExportRequest,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(require_board_view),
) -> Response:
    record_access_log(
        ctx,
        "boss_board_projects_export",
        "maintenance",
        {
            "field_count": len(payload.fields),
            "searched": bool(payload.q and payload.q.strip()),
            "lifecycle": payload.lifecycle,
            "card_status": payload.card_status,
            "sort": payload.sort,
        },
    )
    try:
        content, row_count = project_export.build_project_export(
            db,
            user_ctx=ctx,
            field_keys=payload.fields,
            q_text=payload.q,
            lifecycle=payload.lifecycle,
            card_status=payload.card_status,
            sort=payload.sort,
            allowed_project_ids=_allowed_scope(db, ctx),
        )
    except project_export.UnknownProjectExportField as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "invalid_export_fields",
                "message": "包含服务端白名单之外的导出字段",
                "fields": exc.fields,
            },
        ) from exc
    except project_export.ForbiddenProjectExportField as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {
                "code": "export_field_permission_denied",
                "message": "所选字段超出当前账号的数据权限",
                "fields": exc.fields,
            },
        ) from exc
    except project_export.ProjectExportTooLarge as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "project_export_too_large", "message": str(exc)},
        ) from exc
    except board.BoardSortNotPermitted as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "sort_requires_cost_permission",
             "message": "按成本排序需要成本数据权限"},
        ) from exc
    except board.BoardCostContractNotPermitted as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "cost_contract_permission_required",
             "message": "成本及合同财务数据权限"},
        ) from exc

    stamp = business_today().strftime("%Y%m%d")
    ascii_name = f"maintenance-projects-{stamp}.xlsx"
    utf8_name = quote(f"维保项目清单-{stamp}.xlsx")
    return Response(
        content=content,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{utf8_name}"
            ),
            "X-Export-Row-Count": str(row_count),
        },
    )


@router.get("/projects/{project_id}")
def board_project(
    response: Response,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(require_board_view),
) -> dict:
    """按稳定项目 ID 读取一张聚合卡；详情页不得靠名称搜索第 1 页猜项目。"""
    response.headers["Cache-Control"] = "no-store"
    allowed = _allowed_scope(db, ctx)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "项目不存在或无权查看")
    result = board.projects(
        db,
        user_ctx=ctx,
        page=1,
        page_size=1,
        lifecycle="all",
        allowed_project_ids={project_id},
    )
    row = next(
        (item for item in result["rows"] if item["project_id"] == project_id),
        None,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "项目不存在或无权查看")
    record_access_log(ctx, "boss_board_project", project_id, {})
    return row


@router.get("/projects/{project_id}/orders")
def board_project_orders(
    response: Response,
    project_id: str = Path(..., min_length=1, max_length=36),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(require_board_view),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    allowed = _allowed_scope(db, ctx)
    if project_id == board.UNASSIGNED_BUCKET:
        # 未归属单没有「本人范围」可言：仅全范围账号可见，其余 404（不暴露存在性）
        if allowed is not None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "项目不存在")
    elif (allowed is not None and project_id not in allowed) or not (
        board.project_exists(db, project_id=project_id)
    ):
        # 不存在的 id 与越权 id 返回同一个 404：既不暴露存在性，也不用空列表
        # 冒充「这个项目没有单」（M0-B 改判后范围不再收敛，存在性校验必须自己做）
        raise HTTPException(status.HTTP_404_NOT_FOUND, "项目不存在")
    return board.project_orders(db, user_ctx=ctx, project_id=project_id,
                                page=page, page_size=page_size)


@router.get("/orders/{source_order_id}/lines")
def board_order_lines(
    response: Response,
    source_order_id: str = Path(..., min_length=1, max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(require_board_view),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if not board.order_exists(db, source_order_id=source_order_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "单据不存在")
    allowed = _allowed_scope(db, ctx)
    if allowed is not None:
        # 本人范围账号：单据必须归属于其可见项目，否则 404（不暴露存在性）。
        # 未归属单没有「本人范围」可言，同样 404——与 /projects/{id}/orders 口径一致。
        owner_project = board.order_project_id(db, source_order_id=source_order_id)
        if owner_project is None or owner_project not in allowed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "单据不存在")
    return board.order_lines(db, user_ctx=ctx, source_order_id=source_order_id,
                             page=page, page_size=page_size)
