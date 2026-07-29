"""利润 API（§9）：page_profit 可读/导出，重算仅管理员。"""
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth import current_role, require_admin
from app.db import get_db
from app.security import (
    UserContext,
    apply_field_visibility,
    get_current_user_context,
    is_field_hidden,
    record_access_log,
    require_page,
)
from app.services import profit

router = APIRouter(
    prefix="/profit",
    tags=["profit"],
    dependencies=[Depends(current_role), Depends(require_page("page_profit"))],
)

_DIMS = ("part", "salesperson", "customer")
_COUPLED_FINANCIAL_FIELDS = (
    "revenue_costed",
    "revenue_costed_ex",
    "revenue_costed_inc",
    "no_cost",
    "cost_moving_avg",
    "cost_moving_avg_ex",
    "cost_moving_avg_inc",
    "gross_profit_moving",
    "gross_profit_moving_ex",
    "gross_profit_moving_inc",
    "gross_margin_moving",
    "cost_fifo",
    "cost_fifo_ex",
    "cost_fifo_inc",
    "gross_profit_fifo",
    "gross_profit_fifo_ex",
    "gross_profit_fifo_inc",
    "gross_margin_fifo",
)


def _financial_visibility_restricted(ctx: UserContext) -> bool:
    """成本和利润在该报表中可互推，任一数据组隐藏都必须按受限口径输出。"""
    return (
        is_field_hidden(ctx, "cost_moving_avg")
        or is_field_hidden(ctx, "gross_profit_moving")
    )


def _visible_profit_data(data: dict, ctx: UserContext) -> dict:
    """利润页的成本与毛利必须成组可见，防止通过公开营收做代数反推。

    在这个报表里 ``毛利 = 已配成本营收 - 成本``；只隐藏等式一侧没有意义。只要
    data_purchase_cost / data_profit 任一关闭，就把两组派生财务值一起置空。营收与
    行数仍可见，页面权限不会变成菜单可见但接口 403。
    """
    visible = apply_field_visibility(data, ctx)
    if _financial_visibility_restricted(ctx):
        for row in visible.get("rows", []):
            for field in _COUPLED_FINANCIAL_FIELDS:
                row[field] = None
    return visible


def _aggregate_for_user(
    db: Session,
    dimension: str,
    date_from: date | None,
    date_to: date | None,
    only_anomaly: bool,
    ctx: UserContext,
) -> dict:
    """统一利润读边界：维度侧信道先拦截，再执行服务并做字段脱敏。

    customer 的名称落在通用 ``dimension`` 键，递归字段脱敏无法从键名识别它，必须在
    聚合前显式拒绝；受限销售的销售员/客户维度由服务层兜底抛 PermissionError，API
    收敛为 403，不能把权限拒绝暴露成 500。
    """
    if dimension == "customer" and is_field_hidden(ctx, "customer_name"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无客户信息查看权限")
    if only_anomaly and _financial_visibility_restricted(ctx):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "异常筛选需要同时具备采购成本和利润查看权限",
        )
    try:
        data = profit.aggregate(db, dimension, date_from, date_to, only_anomaly, ctx)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return _visible_profit_data(data, ctx)


@router.post("/recompute")
def recompute(db: Session = Depends(get_db), _: str = Depends(require_admin),
              ctx: UserContext = Depends(get_current_user_context)) -> dict:
    record_access_log(ctx, "recompute", "profit")
    return profit.recompute(db)


@router.get("")
def aggregate(
    dimension: str = Query("part"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    only_anomaly: bool = Query(False),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    dim = dimension if dimension in _DIMS else "part"
    record_access_log(ctx, "aggregate", "profit", {"dimension": dim})
    return _aggregate_for_user(db, dim, date_from, date_to, only_anomaly, ctx)


@router.get("/export")
def export(
    dimension: str = Query("part"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    only_anomaly: bool = Query(False),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    dim = dimension if dimension in _DIMS else "part"
    record_access_log(ctx, "export", "profit", {"dimension": dim})
    # 导出与页面共用同一准入/脱敏边界，避免 CSV 成为绕过通道。
    data = _aggregate_for_user(db, dim, date_from, date_to, only_anomaly, ctx)
    def _safe(v):
        # 防 CSV 公式注入：以 = + - @ 制表/回车开头的文本前置单引号
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v
        return v

    buf = io.StringIO()
    buf.write("﻿")  # BOM，Excel 正确识别 UTF-8
    w = csv.writer(buf)
    w.writerow(["维度", "营收(含税)", "营收(不含税)",
                "已配成本营收(含税)", "已配成本营收(不含税)",
                "移动加权-成本(含税)", "移动加权-成本(不含税)",
                "移动加权-毛利(含税)", "移动加权-毛利(不含税)", "移动加权-毛利率",
                "FIFO-成本(含税)", "FIFO-成本(不含税)",
                "FIFO-毛利(含税)", "FIFO-毛利(不含税)", "FIFO-毛利率",
                "行数", "无成本行", "被排除营收(含税)", "被排除营收(不含税)"])

    def _value(row: dict, field: str, legacy_field: str | None = None):
        """兼容旧聚合形状；缺失字段输出空单元格，不让导出因 KeyError 整体失败。"""
        if field in row:
            return row[field]
        return row.get(legacy_field) if legacy_field is not None else None

    for r in data["rows"]:
        w.writerow([
            _safe(r.get("dimension")),
            _value(r, "revenue_inc", "revenue"), r.get("revenue_ex"),
            _value(r, "revenue_costed_inc", "revenue_costed"),
            r.get("revenue_costed_ex"),
            _value(r, "cost_moving_avg_inc", "cost_moving_avg"),
            r.get("cost_moving_avg_ex"),
            _value(r, "gross_profit_moving_inc", "gross_profit_moving"),
            r.get("gross_profit_moving_ex"),
            r.get("gross_margin_moving"),
            _value(r, "cost_fifo_inc", "cost_fifo"), r.get("cost_fifo_ex"),
            _value(r, "gross_profit_fifo_inc", "gross_profit_fifo"),
            r.get("gross_profit_fifo_ex"),
            r.get("gross_margin_fifo"),
            r.get("lines"), r.get("no_cost"),
            _value(r, "excluded_revenue_inc", "excluded_revenue"),
            r.get("excluded_revenue_ex"),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=profit_{dim}.csv"},
    )
