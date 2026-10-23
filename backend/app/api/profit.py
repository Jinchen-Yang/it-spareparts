"""利润 HTTP 入口：维度读取、导出和重算分别调用受保护领域服务。"""
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth import current_role
from app.authz.principal import VerifiedIdentity, current_verified_identity
from app.db import get_db
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
)
from app.services import profit

router = APIRouter(
    prefix="/profit",
    tags=["profit"],
    dependencies=[Depends(current_role)],
)

from app.services.profit import _DIMS, _financial_visibility_restricted, _visible_profit_data, _aggregate_for_user


@router.post("/recompute")
def recompute(db: Session = Depends(get_db),
              ctx: UserContext = Depends(get_current_user_context),
              identity: VerifiedIdentity = Depends(current_verified_identity)) -> dict:
    record_access_log(ctx, "recompute", "profit")
    return profit.recompute_authorized(db, identity, legacy_context=ctx)


@router.get("")
def aggregate(
    dimension: str = Query("part"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    only_anomaly: bool = Query(False),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    dim = dimension if dimension in _DIMS else "part"
    record_access_log(ctx, "aggregate", "profit", {"dimension": dim})
    return profit.read_profit(db, identity, legacy_context=ctx, dimension=dim, date_from=date_from, date_to=date_to, only_anomaly=only_anomaly)


@router.get("/export")
def export(
    dimension: str = Query("part"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    only_anomaly: bool = Query(False),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> StreamingResponse:
    dim = dimension if dimension in _DIMS else "part"
    record_access_log(ctx, "export", "profit", {"dimension": dim})
    # 导出与页面共用同一准入/脱敏边界，避免 CSV 成为绕过通道。
    data = profit.read_profit(db, identity, legacy_context=ctx, dimension=dim, date_from=date_from, date_to=date_to, only_anomaly=only_anomaly, export=True)
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
