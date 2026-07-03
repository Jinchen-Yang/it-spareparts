"""维保项目成本 API（docs/维保出库成本核算-开发方案.md §5）。

鉴权双层：current_role 硬鉴权（缺/失效凭证 → 401，与 purchases/parts 一致，不依赖 RBAC 开关）
+ require_page('page_maintenance') 页面准入（admin/boss/purchaser 模板默认开）。
成本金额字段随 data_purchase_cost 脱敏（成本 7 键 + 聚合派生键已登记 FIELD_GROUPS）。
"""
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth import current_role, require_admin
from app.db import get_db
from app.security import (
    UserContext, apply_field_visibility, get_current_user_context, record_access_log,
    require_page,
)
from app.services import maintenance_cost

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.post("/recompute")
def recompute(db: Session = Depends(get_db),
              _auth: str = Depends(require_admin),   # 全表重算(~1min 写库)：限管理员，与导入触发方口径一致
              ctx: UserContext = Depends(get_current_user_context)) -> dict:
    record_access_log(ctx, "recompute", "maintenance")
    return maintenance_cost.recompute(db)


@router.get("/projects")
def projects(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    q: str | None = Query(None, max_length=128),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401，不依赖全局 RBAC 开关
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "projects", "maintenance")
    data = maintenance_cost.projects_aggregate(db, date_from, date_to, q)
    return apply_field_visibility(data, ctx)


@router.get("/lines")
def lines(
    project: str = Query(..., max_length=256),
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "lines", "maintenance", {"project": project})
    data = maintenance_cost.project_lines(db, project, month, date_from, date_to, page, page_size)
    return apply_field_visibility(data, ctx)


_SOURCE_LABEL = {"direct": "实际·专属采购", "month_avg": "实际·当月均价",
                 "trace_avg": "预估·追溯均价", "sales_ref": "没有采购有销售", "none": "无成本"}


def _safe(v):
    # 防 CSV 公式注入：以 = + - @ 制表/回车开头的文本前置单引号
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _csv_stream(header: list, rows: list, filename: str) -> StreamingResponse:
    buf = io.StringIO()
    buf.write("﻿")  # BOM，Excel 正确识别 UTF-8
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export")
def export(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    q: str | None = Query(None, max_length=128),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    record_access_log(ctx, "export", "maintenance")
    data = maintenance_cost.projects_aggregate(db, date_from, date_to, q)
    data = apply_field_visibility(data, ctx)   # 导出同样过脱敏层（§8.5）
    header = ["项目", "出库行数", "出库数量", "备件成本-含税小计", "备件成本-不含税小计",
              "成本合计(混合口径参考)", "覆盖率%",
              *(_SOURCE_LABEL[s] + "(行)" for s in ("direct", "month_avg", "trace_avg",
                                                    "sales_ref", "none")),
              "月份数", "关联销售订单", "合同额(含税参考)", "合同被多项目共用"]
    rows = []
    for r in data["rows"]:
        bs = r["by_source"]
        rows.append([_safe(r["project"]), r["lines"], r["qty"],
                     r["cost_inc"], r["cost_ex"], r["cost_total"], r["coverage_pct"],
                     bs.get("direct", 0), bs.get("month_avg", 0), bs.get("trace_avg", 0),
                     bs.get("sales_ref", 0), bs.get("none", 0),
                     r["months"], _safe("、".join(r["sales_orders"])),
                     r["contract_amount"], "是" if r["contract_shared"] else ""])
    return _csv_stream(header, rows, "maintenance_projects.csv")


@router.get("/lines/export")
def lines_export(
    project: str = Query(..., max_length=256),
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """单项目 SKU 明细导出（财务逐行复核入账用）——全量、含成本来源/税口径。"""
    record_access_log(ctx, "lines_export", "maintenance", {"project": project})
    data = maintenance_cost.project_lines(db, project, month, date_from, date_to,
                                          page=1, page_size=1_000_000)
    data = apply_field_visibility(data, ctx)
    header = ["日期", "维保单号", "需求类型", "业务类型", "出库仓库", "PN", "描述",
              "数量", "退货", "单价", "金额", "成本来源", "含税口径", "取价月", "追溯月数",
              "关联采购单", "异常标记"]
    rows = []
    for r in data["rows"]:
        rows.append([r["order_date"], _safe(r["order_no"]), r["demand_type"], r["business_type"],
                     r["warehouse"], _safe(r["pn_std"]), _safe(r["description"]),
                     r["qty"], r["return_qty"], r["unit_cost"], r["cost_amount"],
                     _SOURCE_LABEL.get(r["cost_source"], r["cost_source"]),
                     r["cost_tax_basis"], r["price_month"], r["trace_months"],
                     _safe(r["linked_purchase_order_no"]), "、".join(r["anomaly_flags"] or [])])
    return _csv_stream(header, rows, f"maintenance_lines_{project[:40]}.csv")
