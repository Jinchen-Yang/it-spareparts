"""维保展示板只读端点（plan v1.3 M3-2，§4.4/§4.5 契约）。

七个端点，全部只读；router 受 require_maintenance_boss（flag 关闭 → 整组 404）。
查看权限：page_maintenance_boss（全范围）或 page_maintenance（范围按 M0-B）。
成本字段独立受 data_purchase_cost 控制，无权限时返回 restricted 信封且无侧信道
（响应键集恒定、成本排序 422 而非静默降级）。
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.maintenance_project_scope import resolve_visible_project_ids
from app.auth import current_role
from app.db import get_db
from app.maintenance_boss import require_maintenance_boss
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_page,
)
from app.services import maintenance_boss_board as board

router = APIRouter(
    prefix="/maintenance/boss-board",
    tags=["maintenance"],
    dependencies=[Depends(require_maintenance_boss)],
)

_FULL_SCOPE_ROLES = ("admin", "boss")


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
    """None = 全范围（老板/管理员/持 page_maintenance_boss）；否则收敛到本人项目。"""
    from app import permissions as perm

    perms = ctx.permissions or perm.template_for(ctx.role)
    if ctx.role in _FULL_SCOPE_ROLES or perm.runtime_safe(perms).get(
        "page_maintenance_boss"
    ):
        return None
    return set(resolve_visible_project_ids(db, ctx) or set())


class ProjectSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=128)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)
    lifecycle: str = Field(default="all", pattern=r"^(ongoing|ended|missing|all)$")
    sort: str = Field(default="name", pattern=r"^(attention|orders|name|known_cost)$")


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
    return board.summary(db, user_ctx=ctx, date_from=date_from, date_to=date_to)


@router.get("/attention")
def board_attention(
    response: Response,
    limit: int = Query(10, ge=1, le=10),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(require_board_view),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return board.attention(db, user_ctx=ctx, limit=limit)


@router.get("/projects")
def board_projects(
    response: Response,
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    lifecycle: str = Query("all", pattern=r"^(ongoing|ended|missing|all)$"),
    sort: str = Query("name", pattern=r"^(attention|orders|name|known_cost)$"),
    has_activity: bool | None = Query(None),
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
            sort=sort, has_activity=has_activity, date_from=date_from,
            date_to=date_to, allowed_project_ids=_allowed_scope(db, ctx),
        )
    except board.BoardSortNotPermitted as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "sort_requires_cost_permission",
             "message": "按成本排序需要成本数据权限"},
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
            allowed_project_ids=_allowed_scope(db, ctx),
        )
    except board.BoardSortNotPermitted as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "sort_requires_cost_permission",
             "message": "按成本排序需要成本数据权限"},
        ) from exc


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
    elif allowed is not None and project_id not in allowed:
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
    return board.order_lines(db, user_ctx=ctx, source_order_id=source_order_id,
                             page=page, page_size=page_size)
