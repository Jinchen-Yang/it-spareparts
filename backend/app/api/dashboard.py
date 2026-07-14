"""老板经营看板 API（P1 第二阶段第一刀）。仅管理员/老板（page_boss_board）。

只读分析。金额未税口径（与利润引擎同源）。字段仍过 apply_field_visibility 统一脱敏
（boss/admin 全可见；将来若给其它角色开需靠脱敏兜底）。
"""
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from fastapi import HTTPException

from app.auth import current_role, require_admin
from app.db import get_db
from app.security import (UserContext, apply_field_visibility, get_current_user_context,
                          is_field_hidden, is_scoped_sales, record_access_log, require_page)
from app.services import dashboard, pool

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/kpi")
def kpi(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_boss_board")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "kpi", "dashboard", {"date_from": str(date_from), "date_to": str(date_to)})
    data = dashboard.kpi(db, date_from, date_to, user_ctx=ctx)
    return apply_field_visibility(data, ctx)


@router.get("/sales")
def sales(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    status: str | None = Query(None, description="留空=仅已生效；'全部'=不限；或具体状态"),
    q: str | None = Query(None),
    order_no: str | None = Query(None, description="销售单号全等精确查找（q 仍为模糊搜索）"),
    customer: str | None = Query(None),
    salesperson: str | None = Query(None),
    business_type: str | None = Query(None),
    part_id: int | None = Query(None, description="含该型号的订单（整单召回）"),
    pool_group_id: int | None = Query(None, description="含该有效池成员的订单（整单召回）"),
    sort: str = Query("order_date", pattern="^(order_date|revenue|gross_profit|part_count)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_boss_board")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    # 受限销售由服务层在任何 SQL 前返回稳定的“订单不可见”响应；除此之外，
    # 客户字段不可见的账号也不能用筛选结果数量探测客户名是否存在。
    if customer and not is_scoped_sales(ctx) and is_field_hidden(ctx, "customer"):
        raise HTTPException(status_code=403, detail="当前账号无客户信息权限，不能按客户筛选")
    record_access_log(ctx, "sales", "dashboard", {
        "q": q, "order_no": order_no, "status": status, "sort": sort,
        "part_id": part_id, "pool_group_id": pool_group_id,
    })
    data = dashboard.sales_orders(db, date_from=date_from, date_to=date_to, status=status, q=q,
                                  order_no=order_no,
                                  customer=customer, salesperson=salesperson, business_type=business_type,
                                  part_id=part_id, pool_group_id=pool_group_id,
                                  sort=sort, order=order, page=page, page_size=page_size, user_ctx=ctx)
    return apply_field_visibility(data, ctx)


@router.get("/purchase-orders")
def purchase_orders(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    status: str | None = Query(None, description="留空=仅已生效；'全部'=不限；或具体状态"),
    q: str | None = Query(None),
    order_no: str | None = Query(None, description="采购单号全等精确查找（q 仍为模糊搜索）"),
    source_type: str | None = Query(None),
    purchaser: str | None = Query(None, description="采购员（ILIKE 含匹配）"),
    part_id: int | None = Query(None, description="含该型号的订单（整单召回）"),
    pool_group_id: int | None = Query(None, description="含该有效池成员的订单（整单召回）"),
    sort: str = Query("order_date", pattern="^(order_date|amount|part_count)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_boss_board")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "purchase_orders", "dashboard", {
        "q": q, "order_no": order_no, "status": status, "sort": sort,
        "part_id": part_id, "pool_group_id": pool_group_id,
    })
    data = dashboard.purchase_orders(db, date_from=date_from, date_to=date_to, status=status, q=q,
                                     order_no=order_no,
                                     source_type=source_type, purchaser=purchaser,
                                     part_id=part_id, pool_group_id=pool_group_id,
                                     sort=sort, order=order,
                                     page=page, page_size=page_size, user_ctx=ctx)
    return apply_field_visibility(data, ctx)


@router.get("/trend")
def trend(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    granularity: str = Query("day", pattern="^(day|week|month)$"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_boss_board")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "trend", "dashboard", {"granularity": granularity})
    data = dashboard.trend(db, date_from, date_to, granularity=granularity, user_ctx=ctx)
    return apply_field_visibility(data, ctx)


@router.get("/part-ranking")
def part_ranking(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    cost_method: str = Query("moving_avg", pattern="^(moving_avg|fifo)$"),
    top: int = Query(20, ge=1, le=100),
    part_id: int | None = Query(None, description="精确 part_id（优先于 pn）"),
    pn: str | None = Query(None, description="pn_std 全等精确匹配（不模糊，相似 PN 不混入）"),
    pool_group_id: int | None = Query(None, description="限该有效池成员"),
    sort: str = Query("gross_profit", pattern="^(gross_profit|revenue|qty_sold|order_count)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_boss_board")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "part_ranking", "dashboard",
                      {"date_from": str(date_from), "date_to": str(date_to),
                       "cost_method": cost_method, "part_id": part_id, "pn": pn,
                       "pool_group_id": pool_group_id, "sort": sort})
    data = dashboard.part_ranking(db, date_from, date_to, cost_method=cost_method, top=top,
                                  part_id=part_id, pn=pn, pool_group_id=pool_group_id,
                                  sort=sort, order=order, page=page, page_size=page_size,
                                  user_ctx=ctx)
    return apply_field_visibility(data, ctx)


@router.post("/pool/rebuild")
def pool_rebuild(
    _: str = Depends(require_admin),
) -> dict:
    """已停用（互通PN池价格分析 §17.1）：自动重算不再是池的写入路径，恒返回 410 Gone。

    人工池是唯一真值——建池/改成员/归档一律走「数据中心 → 互通PN池管理」（/api/pools*）。
    保留 admin 门是维持旧权限面（非管理员仍 403），不扩大可探测面。"""
    raise HTTPException(
        status_code=410,
        detail="自动重算池已停用：互通 PN 池由人工维护（数据中心 → 互通PN池管理），替代关系变化不再自动改池",
    )


@router.get("/pools")
def pools(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort: str = Query("savings",
                      pattern="^(savings|member_count|purchase_total|purchase_average"
                              "|sales_total|sales_average|purchase_violation_count"
                              "|sale_violation_count)$"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_boss_board")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "pools", "dashboard", {"sort": sort})
    data = pool.list_pools(db, date_from, date_to, page=page, page_size=page_size,
                           sort=sort, user_ctx=ctx)
    return apply_field_visibility(data, ctx)


@router.get("/pool/{group_id}")
def pool_detail(
    group_id: int,
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    purchase_page: int = Query(1, ge=1, description="采购订单板块页码"),
    sales_page: int = Query(1, ge=1, description="销售订单板块页码"),
    orders_page_size: int = Query(20, ge=1, le=100, description="订单板块每页条数"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_boss_board")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "pool_detail", "dashboard", {"group_id": group_id})
    data = pool.analyze(db, group_id, date_from, date_to, user_ctx=ctx, with_v2=True,
                        purchase_page=purchase_page, sales_page=sales_page,
                        orders_page_size=orders_page_size)
    if data is None:
        # 归档池不参与当前经营分析（复审阻塞 2）：明确告知去向，而不是含糊的"不存在"
        from app.models.inventory import PartPool
        row = db.get(PartPool, group_id)
        if row is not None and row.status != "active":
            raise HTTPException(
                status_code=404,
                detail="池已归档，不参与当前经营分析；档案见「互通PN池管理」（状态筛选：已归档），如需分析请先恢复")
        raise HTTPException(status_code=404, detail="池不存在")
    return apply_field_visibility(data, ctx)
