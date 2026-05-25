"""利润 API（§9）。需管理员。"""
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.security import UserContext, apply_field_visibility, get_current_user_context, record_access_log
from app.services import profit

router = APIRouter(prefix="/profit", tags=["profit"])

_DIMS = ("part", "salesperson", "customer")


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
    _: str = Depends(require_admin),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    dim = dimension if dimension in _DIMS else "part"
    record_access_log(ctx, "aggregate", "profit", {"dimension": dim})
    data = profit.aggregate(db, dim, date_from, date_to, only_anomaly, ctx)
    return apply_field_visibility(data, ctx)


@router.get("/export")
def export(
    dimension: str = Query("part"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    only_anomaly: bool = Query(False),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    dim = dimension if dimension in _DIMS else "part"
    record_access_log(ctx, "export", "profit", {"dimension": dim})
    data = profit.aggregate(db, dim, date_from, date_to, only_anomaly, ctx)
    # 导出同样过脱敏层（§8.5：所有对外数据通道统一脱敏）
    data = apply_field_visibility(data, ctx)
    buf = io.StringIO()
    buf.write("﻿")  # BOM，Excel 正确识别 UTF-8
    w = csv.writer(buf)
    w.writerow(["维度", "营收(不含税)", "已配成本营收",
                "移动加权-成本", "移动加权-毛利", "移动加权-毛利率",
                "FIFO-成本", "FIFO-毛利", "FIFO-毛利率",
                "行数", "无成本行", "被排除营收"])
    for r in data["rows"]:
        w.writerow([r["dimension"], r["revenue"], r["revenue_costed"],
                    r["cost_moving_avg"], r["gross_profit_moving"], r["gross_margin_moving"],
                    r["cost_fifo"], r["gross_profit_fifo"], r["gross_margin_fifo"],
                    r["lines"], r["no_cost"], r["excluded_revenue"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=profit_{dim}.csv"},
    )
