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


_SOURCE_LABEL = {"direct": "实际·专属采购", "window": "实际·±7天最近价",
                 "month_avg": "实际·当月均价",
                 "trace_avg": "预估·追溯均价", "sales_ref": "没有采购有销售", "none": "无成本"}
_CONF_LABEL = {"high": "高", "medium": "中", "low": "低"}


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
              *(_SOURCE_LABEL[s] + "(行)" for s in ("direct", "window", "month_avg",
                                                    "trace_avg", "sales_ref", "none")),
              "月份数", "关联销售订单", "合同额(含税参考)", "合同被多项目共用"]
    rows = []
    for r in data["rows"]:
        bs = r["by_source"]
        rows.append([_safe(r["project"]), r["lines"], r["qty"],
                     r["cost_inc"], r["cost_ex"], r["cost_total"], r["coverage_pct"],
                     bs.get("direct", 0), bs.get("window", 0), bs.get("month_avg", 0),
                     bs.get("trace_avg", 0), bs.get("sales_ref", 0), bs.get("none", 0),
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
              "数量", "退货", "单价", "金额", "成本来源", "置信度", "含税口径", "取价月",
              "追溯月数", "距采购天数", "关联采购单", "异常标记"]
    rows = []
    for r in data["rows"]:
        rows.append([r["order_date"], _safe(r["order_no"]), r["demand_type"], r["business_type"],
                     r["warehouse"], _safe(r["pn_std"]), _safe(r["description"]),
                     r["qty"], r["return_qty"], r["unit_cost"], r["cost_amount"],
                     _SOURCE_LABEL.get(r["cost_source"], r["cost_source"]),
                     _CONF_LABEL.get(r["confidence"], r["confidence"] or ""),
                     r["cost_tax_basis"], r["price_month"], r["trace_months"],
                     r["price_distance_days"],
                     _safe(r["linked_purchase_order_no"]), "、".join(r["anomaly_flags"] or [])])
    return _csv_stream(header, rows, f"maintenance_lines_{project[:40]}.csv")


@router.get("/board")
def board(
    status: str | None = Query(None, pattern=r"^(red|yellow|green|no_budget)$"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """盈亏看板（§16.2）：合同(XSDD)级 红/黄/绿 状态灯，黄红置顶。"""
    record_access_log(ctx, "board", "maintenance")
    data = maintenance_cost.board(db, date_from, date_to, status)
    return apply_field_visibility(data, ctx)


@router.get("/export-workbook")
def export_workbook(
    contract: str = Query(..., max_length=64),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """§16.4 工作簿形态回填导出：与财务现用「项目预算工作簿」同构（三 sheet）。

    xlsx 无法走字段级脱敏 → 显式门禁：本质是成本导出，无 data_purchase_cost 直接 403。
    「产品成本」按财务习惯填单据级总成本于每张 WBDD 首行；行级取价明细作附加列（增强不破坏）。
    """
    from fastapi import HTTPException
    from openpyxl import Workbook
    from openpyxl.styles import Font

    if apply_field_visibility({"unit_cost": 1}, ctx).get("unit_cost") is None:
        raise HTTPException(status_code=403, detail="无成本查看权限，不能导出项目成本工作簿")
    record_access_log(ctx, "export_workbook", "maintenance", {"contract": contract})
    data = maintenance_cost.contract_workbook_data(db, contract)

    wb = Workbook()
    bold = Font(bold=True)

    # ---- Sheet1 项目预算 ----
    ws = wb.active
    ws.title = "项目预算"
    budget = data["budget"]
    so = data["sales_order"]
    heads = [("合同（销售订单）", contract),
             ("合同金额（含税参考）", float(budget) if budget is not None else "（销售表未找到）"),
             ("税率", float(so.tax_rate) if so is not None and so.tax_rate is not None else ""),
             ("导出说明", "由系统按取价瀑布自动核算；金额为含税/不含税原值混合参考口径")]
    for k, v in heads:
        ws.append([k, v])
        ws.cell(row=ws.max_row, column=1).font = bold
    ws.append([])
    cats = sorted({c for m in data["monthly"].values() for c in m})
    ws.append(["月份", *cats, "合计"])
    for c in range(1, len(cats) + 3):
        ws.cell(row=ws.max_row, column=c).font = bold
    for ym in sorted(data["monthly"]):
        m = data["monthly"][ym]
        vals = [float(m.get(c, 0)) for c in cats]
        ws.append([ym, *vals, round(sum(vals), 2)])

    # ---- Sheet2 备件明细-氚云（原列 + 回填/附加列）----
    ws2 = wb.create_sheet("备件明细-氚云")
    ws2.append(["数据标题(WBDD单号)", "制单日期", "销售订单", "项目名", "需求类型", "出库仓库",
                "销售人员", "业务类型", "序号", "需供货产品", "产品描述", "需求数量",
                "产品成本", "单价", "合计", "发货SN",
                "行成本单价", "行成本金额", "成本来源", "置信度", "取价月", "距采购天数", "含税口径"])
    for c in range(1, 24):
        ws2.cell(row=1, column=c).font = bold
    prev_order = None
    for ln, o in data["lines"]:
        first = o.order_no != prev_order
        prev_order = o.order_no
        doc_cost = data["doc_total"].get(o.order_no)
        ws2.append([
            o.order_no, o.order_date.isoformat() if o.order_date else None,
            o.linked_sales_order_no, o.project_raw or o.project_std, o.demand_type,
            o.warehouse, o.salesperson, o.business_type,
            ln.line_no, ln.pn_std, ln.description,
            float(ln.qty) if ln.qty is not None else None,
            # 财务习惯：单据级总成本恒填首行（§16.4 实证 1407/1407 张多行单据如此）
            float(doc_cost) if (first and doc_cost is not None) else None,
            None, None, ln.serial_numbers,
            float(ln.unit_cost) if ln.unit_cost is not None else None,
            float(ln.cost_amount) if ln.cost_amount is not None else None,
            _SOURCE_LABEL.get(ln.cost_source, ln.cost_source),
            _CONF_LABEL.get(ln.confidence, ln.confidence or ""),
            ln.price_month, ln.price_distance_days, ln.cost_tax_basis,
        ])

    # ---- Sheet3 报销明细 ----
    ws3 = wb.create_sheet("报销明细")
    ws3.append(["报销日期", "BXD单号", "序号", "流程状态", "报销人员", "报销类别",
                "费用分类", "支出事由", "报销金额"])
    for c in range(1, 10):
        ws3.cell(row=1, column=c).font = bold
    for e in data["expenses"]:
        ws3.append([e.expense_date.isoformat() if e.expense_date else None,
                    e.bxd_no, e.line_no, e.data_status, e.person, e.expense_type,
                    e.fee_category, e.reason,
                    float(e.amount) if e.amount is not None else None])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=project_workbook_{contract[:40]}.xlsx"},
    )
