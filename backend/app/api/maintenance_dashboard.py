"""维保数据看板 API（T1–T4 R33 + R34 / D-0024）。

数量与金额分离（R33 第 2 条，R34 延续）：
- 数量端点（options/summary/projects/parts/receipts/quantity-checks/
  orders/consumption/consumption-issues/export）只挂登录 + page_maintenance
  + 显式项目范围；maintenance_manager / purchaser 可用。无金额权时响应
  不出现金额字段（不是 0/null），导出不含金额列（列头都没有）。
- 金额排序（bad_value_desc）在无金额权时 422，不静默降级（pn-ranking 同款）。
- 估价端点（valuation）额外挂利润门（admin 或 data_profit），否则 403；
  估价/参考价/成本覆盖等金额键与政策说明全部走利润门。
- R34：价值保留比例（value_retention_ratio）是本次试算参数 [0,1]，
  **缺省 = 50% 可调**（Sunny 01:20 修正；20%/30% 仅可选，显式 0 合法）；
  有利润门权限者首次查询即按 0.5 试算，比例只作用本次请求与导出。

M01/M03（D-0024 新增）：
- /orders：期间下单（制单日期入期、原 qty、另计单数、不扣退货/免返）。
- /consumption：每日每项目已计消耗（数量全计；金额只读既有 cost_amount
  双税小计 + 未计成本量/覆盖/来源，仅利润门用户可见）。
- /consumption/issues：领用行级明细下钻（日×项目可核对）。

非法日期/排序/分页/比例参数一律 422 不 500：枚举走 Query pattern，业务码
（custom 缺日期、起止倒置、比例越界）统一转 422。

流式导出助手与 CSV 防公式净化在本模块内自持（生产 api.maintenance 无
``_safe_formula_only``，且不可从该巨型 API import 拖入无关业务）：
净化函数提取自 ``services/maintenance_dashboard_export_text``（Lane B 专用）。
"""

import csv
import io
import re
from contextlib import suppress
from datetime import date
from decimal import Decimal, InvalidOperation
from tempfile import SpooledTemporaryFile
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from xlsxwriter import Workbook

from app.api.date_validation import validate_date_query
from app.api.maintenance_expense_collection_workbook import _require_profit_visibility
from app.auth import current_role
from app.db import get_db
from app.models.maintenance_project import MaintenanceProject
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_page,
)
from app.services import maintenance_dashboard as dash
from app.services import maintenance_dashboard_consumption as consumption
from app.services import maintenance_dashboard_valuation_policy as policy
from app.services.maintenance_boss_board import BUSINESS_TYPE_FILTER_PATTERN
from app.services.maintenance_dashboard_export_text import (
    ExportCellLimitError,
    safe_formula_only,
)

router = APIRouter(
    prefix="/maintenance/analytics/maintenance-dashboard", tags=["maintenance"]
)

_RANGE_PATTERN = "^(" + "|".join(dash.RANGE_PRESETS) + ")$"
_PROJECT_SORT_PATTERN = "^(" + "|".join(dash.PROJECT_SORTS) + ")$"
_PART_SORT_PATTERN = "^(" + "|".join(dash.PART_SORTS) + ")$"
_RECEIPT_SORT_PATTERN = "^(" + "|".join(dash.RECEIPT_SORTS) + ")$"
_CHECK_SORT_PATTERN = "^(" + "|".join(dash.CHECK_SORTS) + ")$"
_AMOUNT_SORTS = frozenset({"bad_value_desc"})
_MAX_PROJECT_FILTERS = 50

_ERROR_MESSAGES = {
    "invalid_range_preset": "非法的时间范围预设",
    "custom_requires_dates": "custom 范围必须同时提供 date_from 与 date_to",
    "invalid_date_range": "date_from 不能晚于 date_to",
    "invalid_sort": "非法的排序键",
    "invalid_project_filter": "项目筛选参数非法（最多 50 个，每个 ≤64 字符）",
    "invalid_retention_ratio": "价值保留比例必须是 [0,1] 内的小数（如 0.5）",
}


def _amounts_visible(ctx: UserContext) -> bool:
    """与 ``_require_profit_visibility`` 同口径的布尔判定（数量端点内联用）。"""
    from app import permissions as perm

    perms = ctx.permissions or perm.template_for(ctx.role)
    return ctx.role == "admin" or bool(perm.runtime_safe(perms).get("data_profit"))


def _reject_amount_sort(ctx: UserContext, sort: str) -> None:
    if sort in _AMOUNT_SORTS and not _amounts_visible(ctx):
        record_access_log(
            ctx, "maintenance_dashboard_sort_denied", "maintenance_dashboard",
            {"sort": sort},
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "sort_requires_profit_permission",
                "message": "按金额排序需要金额查看权限（data_profit）",
            },
        )


def _parse_project_filter(project: str | None) -> list[str] | None:
    text = (project or "").strip()
    if not text:
        return None
    items = [item.strip() for item in text.split(",")]
    if (
        len(items) > _MAX_PROJECT_FILTERS
        or any(not item or len(item) > 64 for item in items)
    ):
        raise dash.DashboardInputError("invalid_project_filter")
    return items


def _retention_ratio(raw: str | None) -> Decimal:
    """解析 value_retention_ratio：未提供/空白 → 默认 50%（Sunny 01:20 修正，
    显式 0 保留为 0）；非法/非有限/越界 → 422。

    对所有角色做同样的 422 校验；数值仅金额可见用户参与构建。
    """
    try:
        return policy.resolve_retention_ratio(raw)
    except policy.PolicyInputError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": exc.code,
                "message": _ERROR_MESSAGES.get(exc.code, exc.code),
            },
        ) from exc


def _filters(
    db: Session,
    ctx: UserContext,
    *,
    range_: str,
    date_from: date | None,
    date_to: date | None,
    project: str | None,
    business_type: str,
    q: str | None = None,
) -> dash._Filters:
    """范围先收敛后聚合：时间 × 授权项目 × 业务类型 ×（可选）关键词。"""
    try:
        start, end = dash.resolve_date_range(range_, date_from, date_to)
        requested = _parse_project_filter(project)
        scope = dash.resolve_project_scope(db, ctx, requested)
        return dash._base_filters(
            db, scope=scope, start=start, end=end, q=q,
            business_type=None if business_type == "all" else business_type,
        )
    except dash.DashboardInputError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": exc.code,
                "message": _ERROR_MESSAGES.get(exc.code, exc.code),
            },
        ) from exc


def _audit_flags(scoped: bool | None = None, **flags: bool | str) -> dict:
    """审计详情只记标志位，不落关键词/项目 ID 等自由文本（pn-ranking 同款）。

    action 名由各端点以字面量直传 record_access_log——保持可静态扫描
    （tests/test_access_log_action_width.py 的字面量列宽契约）。
    """
    details = dict(flags)
    if scoped is not None:
        details["scope"] = "full" if not scoped else "scoped"
    return details


@router.get("/options")
def options(
    response: Response,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """筛选器候选项与可见性说明；项目列表随授权范围收敛。"""
    response.headers["Cache-Control"] = "no-store"
    visible = _amounts_visible(ctx)
    scope = dash.resolve_project_scope(db, ctx, None)
    projects = dash._project_rows(db, scope)
    sorts = {
        "projects": [s for s in dash.PROJECT_SORTS
                     if visible or s not in _AMOUNT_SORTS],
        "parts": [s for s in dash.PART_SORTS if visible or s not in _AMOUNT_SORTS],
        "receipts": list(dash.RECEIPT_SORTS),
        "quantity_checks": list(dash.CHECK_SORTS),
    }
    payload: dict = {
        "range_presets": list(dash.RANGE_PRESETS),
        "default_range": "7d",
        "default_date_rule": "近 7 天含今天（上海日历日）",
        "sorts": sorts,
        "projects": [
            {
                "project_id": project.project_id,
                "project_code": project.project_code,
                "display_name": project.display_name,
                "business_type": project.business_type,
            }
            for project in projects.values()
        ],
        "amounts_visible": visible,
        "max_export_rows": dash.MAX_EXPORT_ROWS,
        "export_kinds": list(_EXPORT_KINDS),
    }
    if visible:
        # 缺省 50%（Sunny 01:20）：options 即回显默认比例，UI 无须先发请求试算。
        payload["valuation_policy"] = policy.policy_payload(
            policy.DEFAULT_RETENTION_RATIO
        )
    record_access_log(ctx, "maintenance_dashboard_options",
                      "maintenance_dashboard",
                      _audit_flags(amounts=visible, scoped=scope is not None))
    return payload


@router.get("/summary", dependencies=[Depends(validate_date_query)])
def summary(
    response: Response,
    range_: str = Query("7d", alias="range", pattern=_RANGE_PATTERN),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    project: str | None = Query(None, max_length=4000),
    business_type: str = Query(
        "all", pattern=BUSINESS_TYPE_FILTER_PATTERN, max_length=128
    ),
    q: str | None = Query(None, max_length=128),
    value_retention_ratio: str | None = Query(None, max_length=32),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """T1 回收与领用总览：卡片 + 趋势 + 件况结构 + Top 项目。

    金额卡片仅对通过利润门的调用方构建（响应内无该键，而非 0 值）；
    比例参数只在金额可见时生效（数量视图不受影响）。
    """
    response.headers["Cache-Control"] = "no-store"
    visible = _amounts_visible(ctx)
    # 比例合法性对所有角色一致校验（422）；仅金额可见时才参与构建。
    ratio = _retention_ratio(value_retention_ratio)
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type, q=q,
    )
    record_access_log(
        ctx, "maintenance_dashboard_summary", "maintenance_dashboard",
        _audit_flags(scoped=filters.scope is not None,
                     amounts=visible,
                     keyword=bool(q and q.strip()),
                     ratio_explicit=value_retention_ratio is not None),
    )
    return dash.build_summary_payload(
        db, filters, include_amounts=visible, ratio=ratio if visible else None,
    )


@router.get("/projects", dependencies=[Depends(validate_date_query)])
def projects(
    response: Response,
    range_: str = Query("7d", alias="range", pattern=_RANGE_PATTERN),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    project: str | None = Query(None, max_length=4000),
    business_type: str = Query(
        "all", pattern=BUSINESS_TYPE_FILTER_PATTERN, max_length=128
    ),
    q: str | None = Query(None, max_length=128),
    sort: str = Query("bad_receipt_qty_desc", pattern=_PROJECT_SORT_PATTERN),
    value_retention_ratio: str | None = Query(None, max_length=32),
    page: int = Query(1, ge=1),
    page_size: int = Query(dash.DEFAULT_PAGE_SIZE, ge=1, le=dash.MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """T2 项目对比：范围内项目全量行（含零行）；返还率按项目逐行给出。

    行级试算与 summary/parts/导出同参（root 裁决补传）：缺省 0.5、显式值
    （含 0）逐字生效，避免表格与导出同屏口径分裂。
    """
    response.headers["Cache-Control"] = "no-store"
    _reject_amount_sort(ctx, sort)
    ratio = _retention_ratio(value_retention_ratio)
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type, q=q,
    )
    record_access_log(
        ctx, "maintenance_dashboard_projects", "maintenance_dashboard",
        _audit_flags(scoped=filters.scope is not None, keyword=bool(q),
                     ratio_explicit=value_retention_ratio is not None),
    )
    visible = _amounts_visible(ctx)
    rows = dash.sort_project_rows(
        dash.build_project_rows(
            db, filters, include_amounts=visible,
            ratio=ratio if visible else None,
        ),
        sort,
    )
    return {"range": _range_payload(filters), **dash.paginate(rows, page, page_size)}


@router.get("/parts", dependencies=[Depends(validate_date_query)])
def parts(
    response: Response,
    range_: str = Query("7d", alias="range", pattern=_RANGE_PATTERN),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    project: str | None = Query(None, max_length=4000),
    business_type: str = Query(
        "all", pattern=BUSINESS_TYPE_FILTER_PATTERN, max_length=128
    ),
    q: str | None = Query(None, max_length=128),
    sort: str = Query("bad_receipt_qty_desc", pattern=_PART_SORT_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(dash.DEFAULT_PAGE_SIZE, ge=1, le=dash.MAX_PAGE_SIZE),
    value_retention_ratio: str | None = Query(None, max_length=32),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """T3 PN 排名：PN 身份归并后的返件/坏件/领用与（可见时）试算列。"""
    response.headers["Cache-Control"] = "no-store"
    _reject_amount_sort(ctx, sort)
    visible = _amounts_visible(ctx)
    ratio = _retention_ratio(value_retention_ratio)
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type, q=q,
    )
    record_access_log(ctx, "maintenance_dashboard_parts",
                      "maintenance_dashboard",
                      _audit_flags(scoped=filters.scope is not None,
                                   keyword=bool(q),
                                   ratio_explicit=value_retention_ratio is not None))
    rows = dash.sort_part_rows(
        dash.build_part_rows(
            db, filters, include_amounts=visible,
            ratio=ratio if visible else None,
        ),
        sort,
    )
    payload = {"range": _range_payload(filters), **dash.paginate(rows, page, page_size)}
    if visible:
        payload["policy"] = policy.policy_payload(ratio)
    return payload


@router.get("/valuation", dependencies=[Depends(validate_date_query)])
def valuation(
    response: Response,
    range_: str = Query("7d", alias="range", pattern=_RANGE_PATTERN),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    project: str | None = Query(None, max_length=4000),
    business_type: str = Query(
        "all", pattern=BUSINESS_TYPE_FILTER_PATTERN, max_length=128
    ),
    q: str | None = Query(None, max_length=128),
    sort: str = Query("bad_value_desc", pattern=_PART_SORT_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(dash.DEFAULT_PAGE_SIZE, ge=1, le=dash.MAX_PAGE_SIZE),
    value_retention_ratio: str | None = Query(None, max_length=32),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _profit: None = Depends(_require_profit_visibility),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """估价专页（金额域）：政策版本 + 总卡 + 分 PN 估价行（R34 三域）。

    独立挂利润门（登录 + 页面权限 + 项目范围 + 利润门缺一不可——
    readonly 兜底模板有 data_profit 但无 page_maintenance，不能只查利润键）。
    比例缺省 = 50%（可调；显式 0 合法）；待估行不填 0，也不计入试算合计。
    """
    response.headers["Cache-Control"] = "no-store"
    ratio = _retention_ratio(value_retention_ratio)
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type, q=q,
    )
    record_access_log(ctx, "maintenance_dashboard_valuation",
                      "maintenance_dashboard",
                      _audit_flags(scoped=filters.scope is not None,
                                   amounts=True,
                                   ratio_explicit=value_retention_ratio is not None))
    rows = dash.sort_part_rows(
        dash.build_part_rows(db, filters, include_amounts=True, ratio=ratio), sort
    )
    # 卡与 PN 行同源（KPI=分组）：主域覆盖按单位分桶（B-F09），
    # 三域覆盖从行载荷合并（B-F07）；混合单位平面 null + unit_breakdown。
    main_units = dash.aggregate_main_units_from_rows(rows)
    mixed_units = len(main_units) > 1
    estimated_qty = sum(
        (Decimal(row["valuation"]["estimated_bad_qty"]) for row in rows), Decimal("0")
    )
    bad_receipt_qty = sum(
        (Decimal(row["bad_receipt_qty"]) for row in rows), Decimal("0")
    )

    def _flat_total(field: str) -> str | None:
        if mixed_units:
            return None
        return dash._qty(sum(
            (bucket[field] for bucket in main_units.values()), Decimal("0")
        ))

    cards: dict = {
        "estimated_bad_qty": _flat_total("estimated"),
        "pending_bad_qty": _flat_total("pending"),
        "awaiting_ratio_bad_qty": _flat_total("awaiting"),
        "coverage_pct": (
            None if mixed_units
            else dash._coverage_pct(estimated_qty, bad_receipt_qty)
        ),
        "unit": next(iter(main_units), None) if not mixed_units else None,
        "unit_breakdown": [
            {
                "unit": unit,
                "bad_qty": dash._qty(main_units[unit]["bad"]),
                "estimated_bad_qty": dash._qty(main_units[unit]["estimated"]),
                "pending_bad_qty": dash._qty(main_units[unit]["pending"]),
                "awaiting_ratio_bad_qty": dash._qty(main_units[unit]["awaiting"]),
                # 本单位已估金额（静态审查补充；仅已估单位给）。
                **({
                    "value_inc_tax": dash._amount(main_units[unit]["value_inc"]),
                    "value_ex_tax": dash._amount(main_units[unit]["value_ex"]),
                } if main_units[unit].get("has_value") else {}),
                "coverage_pct": dash._coverage_pct(
                    main_units[unit]["estimated"], main_units[unit]["bad"]
                ),
            }
            for unit in dash.sorted_unit_keys(main_units.keys())
        ],
        "domains": dash.aggregate_domain_cards_from_rows(rows),
    }
    # 待估行（pending）不带 bad_estimated_value_* 键（不填 0）——用 .get 计 0，
    # 否则任一待估 PN 会让整个估价页 500。
    bad_value_inc = sum(
        (Decimal(row["valuation"].get("bad_estimated_value_inc_tax", "0"))
         for row in rows),
        Decimal("0"),
    )
    bad_value_ex = sum(
        (Decimal(row["valuation"].get("bad_estimated_value_ex_tax", "0"))
         for row in rows),
        Decimal("0"),
    )
    if estimated_qty > 0:
        # 与 summary 估价卡同规则：全部待估（estimated==0）时不出现 0 元卡。
        cards["bad_estimated_value"] = {
            "inc_tax": dash._amount(bad_value_inc),
            "ex_tax": dash._amount(bad_value_ex),
        }
    return {
        "range": _range_payload(filters),
        "policy": policy.policy_payload(ratio),
        "note": policy.NOTES,
        "cards": cards,
        **dash.paginate(rows, page, page_size),
    }


@router.get("/receipts", dependencies=[Depends(validate_date_query)])
def receipts(
    response: Response,
    range_: str = Query("7d", alias="range", pattern=_RANGE_PATTERN),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    project: str | None = Query(None, max_length=4000),
    business_type: str = Query(
        "all", pattern=BUSINESS_TYPE_FILTER_PATTERN, max_length=128
    ),
    q: str | None = Query(None, max_length=128),
    test_result: str | None = Query(None, max_length=32),
    source: str | None = Query(None, max_length=32),
    sort: str = Query("occurred_desc", pattern=_RECEIPT_SORT_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(dash.DEFAULT_PAGE_SIZE, ge=1, le=dash.MAX_PAGE_SIZE),
    value_retention_ratio: str | None = Query(None, max_length=32),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """返件实收明细（T1/T3 下钻）：行级；退货 return_qty 不在此口径。"""
    response.headers["Cache-Control"] = "no-store"
    visible = _amounts_visible(ctx)
    ratio = _retention_ratio(value_retention_ratio)
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type, q=q,
    )
    record_access_log(
        ctx, "maintenance_dashboard_receipts", "maintenance_dashboard",
        _audit_flags(scoped=filters.scope is not None,
                     condition=bool(test_result), source=bool(source),
                     ratio_explicit=value_retention_ratio is not None),
    )
    rows, total = dash.build_receipt_detail_rows(
        db, filters,
        include_amounts=visible,
        test_result=(test_result.strip() or None) if test_result else None,
        source=(source.strip() or None) if source else None,
        ratio=ratio if visible else None,
    )
    rows = dash.sort_receipt_rows(rows, sort)
    start = (page - 1) * page_size
    return {
        "range": _range_payload(filters),
        "rows": rows[start:start + page_size],
        "pagination": {"page": page, "page_size": page_size, "total": total},
    }


@router.get("/quantity-checks", dependencies=[Depends(validate_date_query)])
def quantity_checks(
    response: Response,
    range_: str = Query("7d", alias="range", pattern=_RANGE_PATTERN),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    project: str | None = Query(None, max_length=4000),
    business_type: str = Query(
        "all", pattern=BUSINESS_TYPE_FILTER_PATTERN, max_length=128
    ),
    q: str | None = Query(None, max_length=128),
    sort: str = Query("difference_desc", pattern=_CHECK_SORT_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(dash.DEFAULT_PAGE_SIZE, ge=1, le=dash.MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """T4 需求单数量核对（当前累计）：N/R/差额/超收 + 未关联实收。

    不提供历史截止日参数（summarize_demand_returns 拒绝 as_of）；
    不发布「欠返/逾期/已返」状态。q（B-F10）按有效需求行 PN 命中**选择
    整张需求单**（选入单仍整单全 PN/全期累计 N/R，不裁局部、不忽略 q）。
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        requested = _parse_project_filter(project)
        scope = dash.resolve_project_scope(db, ctx, requested)
        start, end = dash.resolve_date_range(range_, date_from, date_to)
    except dash.DashboardInputError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": exc.code,
                "message": _ERROR_MESSAGES.get(exc.code, exc.code),
            },
        ) from exc
    narrowed = _narrow_scope_by_business_type(db, scope, business_type)
    projects = dash._project_rows(db, narrowed)
    part_ids, keyword = dash._part_keyword(db, q)
    record_access_log(ctx, "maintenance_dashboard_quantity_checks",
                      "maintenance_dashboard",
                      _audit_flags(scoped=narrowed is not None,
                                   keyword=bool(q)))
    payload = dash.build_quantity_checks_payload(
        db, narrowed,
        projects=projects,
        order_date_from=start,
        order_date_to=end,
        part_ids=part_ids, keyword=keyword,
    )
    rows = dash.sort_check_rows(payload["rows"], sort)
    return {
        "cards": payload["cards"],
        "unassigned_receipts": payload["unassigned_receipts"],
        **dash.paginate(rows, page, page_size),
    }


@router.get("/orders", dependencies=[Depends(validate_date_query)])
def orders(
    response: Response,
    range_: str = Query("7d", alias="range", pattern=_RANGE_PATTERN),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    project: str | None = Query(None, max_length=4000),
    business_type: str = Query(
        "all", pattern=BUSINESS_TYPE_FILTER_PATTERN, max_length=128
    ),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """M01 期间下单（数量域）：制单日期入期的有效需求单原 qty + 去重单数。

    不扣退货、不扣免返（原 qty 口径）；无关键词参数（单域，同 T4）；
    无金额列——任何角色都只有数量。
    """
    response.headers["Cache-Control"] = "no-store"
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type,
    )
    record_access_log(ctx, "maintenance_dashboard_orders",
                      "maintenance_dashboard",
                      _audit_flags(scoped=filters.scope is not None))
    payload = consumption.period_orders(
        db, start=filters.start, end=filters.end, projects=filters.projects,
    )
    return {"range": _range_payload(filters), **payload}


@router.get("/consumption", dependencies=[Depends(validate_date_query)])
def consumption_daily(
    response: Response,
    range_: str = Query("7d", alias="range", pattern=_RANGE_PATTERN),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    project: str | None = Query(None, max_length=4000),
    business_type: str = Query(
        "all", pattern=BUSINESS_TYPE_FILTER_PATTERN, max_length=128
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(dash.DEFAULT_PAGE_SIZE, ge=1, le=dash.MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """M03 每日每项目已计消耗：数量全计；金额只读既有成本（利润门）。

    未计成本量/覆盖率/来源标注仅金额可见用户获得；只读，不回算。
    """
    response.headers["Cache-Control"] = "no-store"
    visible = _amounts_visible(ctx)
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type,
    )
    record_access_log(ctx, "maintenance_dashboard_consumption",
                      "maintenance_dashboard",
                      _audit_flags(scoped=filters.scope is not None,
                                   amounts=visible))
    payload = consumption.daily_consumption(
        db, start=filters.start, end=filters.end, projects=filters.projects,
        include_amounts=visible,
    )
    return {
        "range": _range_payload(filters),
        "cards": payload["cards"],
        **dash.paginate(payload["rows"], page, page_size),
    }


@router.get("/consumption/issues", dependencies=[Depends(validate_date_query)])
def consumption_issues(
    response: Response,
    range_: str = Query("7d", alias="range", pattern=_RANGE_PATTERN),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    project: str | None = Query(None, max_length=4000),
    business_type: str = Query(
        "all", pattern=BUSINESS_TYPE_FILTER_PATTERN, max_length=128
    ),
    issue_date: date | None = Query(None),
    project_id: str | None = Query(None, max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(dash.DEFAULT_PAGE_SIZE, ge=1, le=dash.MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """M03 领用明细下钻（数量域 + 利润门成本证据）：日×项目可核对。

    ``project_id`` 与授权范围取交集（集合语义），越权项目自然为空，
    不扩全量。
    """
    response.headers["Cache-Control"] = "no-store"
    visible = _amounts_visible(ctx)
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type,
    )
    record_access_log(ctx, "maintenance_dashboard_consumption_issues",
                      "maintenance_dashboard",
                      _audit_flags(scoped=filters.scope is not None,
                                   amounts=visible,
                                   dated=issue_date is not None))
    rows = consumption.consumption_issue_rows(
        db, start=filters.start, end=filters.end, projects=filters.projects,
        include_amounts=visible, issue_date=issue_date, project_id=project_id,
    )
    return {"range": _range_payload(filters), **dash.paginate(rows, page, page_size)}


def _narrow_scope_by_business_type(
    db: Session, scope: set[str] | None, business_type: str
) -> set[str] | None:
    """业务类型筛选只影响集合语义：None（全范围）收敛为该类型项目集合。"""
    if business_type == "all":
        return scope
    statement = select(MaintenanceProject.project_id).where(
        MaintenanceProject.business_type == business_type
    )
    if scope is not None:
        if not scope:
            return set()
        statement = statement.where(MaintenanceProject.project_id.in_(scope))
    return set(db.scalars(statement))


def _range_payload(filters: dash._Filters) -> dict:
    return {
        "start": filters.start.isoformat() if filters.start else None,
        "end": filters.end.isoformat(),
    }


# ---------- 导出（完整筛选集；数量/金额列随权限分离） ----------

_EXPORT_KINDS = (
    "projects", "parts", "receipts", "quantity_checks",
    "orders", "consumption_daily", "consumption_issues",
)


@router.get("/export", dependencies=[Depends(validate_date_query)])
def export(
    range_: str = Query("7d", alias="range", pattern=_RANGE_PATTERN),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    project: str | None = Query(None, max_length=4000),
    business_type: str = Query(
        "all", pattern=BUSINESS_TYPE_FILTER_PATTERN, max_length=128
    ),
    q: str | None = Query(None, max_length=128),
    kind: str = Query(..., pattern="^(" + "|".join(_EXPORT_KINDS) + ")$"),
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    test_result: str | None = Query(None, max_length=32),
    source: str | None = Query(None, max_length=32),
    value_retention_ratio: str | None = Query(None, max_length=32),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """导出当前筛选集：CSV 撇号只加公式形态单元格；XLSX 数值写数值单元格。

    无金额权时导出不含任何金额列与估价元数据（列头都没有，不是置空）。
    价值保留比例仅金额可见导出携带（参数伴随结果，无全局写入）。
    """
    visible = _amounts_visible(ctx)
    ratio = _retention_ratio(value_retention_ratio)
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type, q=q,
    )
    record_access_log(
        ctx, "maintenance_dashboard_export", "maintenance_dashboard",
        _audit_flags(scoped=filters.scope is not None,
                     amounts=visible, kind=kind,
                     ratio_explicit=value_retention_ratio is not None),
    )
    if kind == "projects":
        rows = dash.build_project_rows(
            db, filters, include_amounts=visible,
            ratio=ratio if visible else None,
        )
        header, columns = _project_export_columns(visible)
        # 静态审查补充（§106）：混合单位按 项目×单位 展开——数量/率用本桶
        # 值，金额不重复父项总额（单位内已估金额；待估单位留空）。单一单位
        # 行保持原样。
        data = []
        for row in rows:
            buckets = row.get("unit_breakdown") or []
            if len(buckets) <= 1:
                item = dict(row)
                item["_unit_label"] = _unit_label_of(row)
                data.append(item)
                continue
            valuation = dict(row.get("valuation") or {})
            main_by_unit = {
                entry.get("unit"): entry
                for entry in valuation.get("unit_breakdown", [])
            }
            domains_by_unit = {
                domain: {
                    entry.get("unit"): entry
                    for entry in payload.get("unit_breakdown", [])
                }
                for domain, payload in valuation.get("domains", {}).items()
            }
            for entry in buckets:
                unit = entry.get("unit")
                item = dict(row)
                item["_unit_label"] = unit or "待核"
                for key in ("receipt_qty", "bad_receipt_qty",
                            "scrap_receipt_qty", "issued_qty",
                            "exempt_issued_qty", "pending_requirement_qty",
                            "unlinked_receipt_qty", "return_rate_pct",
                            "rate_numerator", "rate_denominator"):
                    item[key] = entry.get(key)
                if valuation:
                    main = main_by_unit.get(unit) or {
                        "estimated_bad_qty": "0.000",
                        "pending_bad_qty": "0.000",
                        "awaiting_ratio_bad_qty": "0.000",
                        "coverage_pct": None,
                    }
                    unit_valuation = dict(valuation)
                    unit_valuation["estimated_bad_qty"] = main.get(
                        "estimated_bad_qty"
                    )
                    unit_valuation["pending_bad_qty"] = main.get(
                        "pending_bad_qty"
                    )
                    unit_valuation["awaiting_ratio_bad_qty"] = main.get(
                        "awaiting_ratio_bad_qty"
                    )
                    unit_valuation["coverage_pct"] = main.get("coverage_pct")
                    # 值键用本单位已估金额（B-F06：全待估单位不写 0.00）。
                    if "value_inc_tax" in main:
                        unit_valuation["bad_estimated_value_inc_tax"] = (
                            main["value_inc_tax"]
                        )
                        unit_valuation["bad_estimated_value_ex_tax"] = (
                            main["value_ex_tax"]
                        )
                    else:
                        unit_valuation.pop("bad_estimated_value_inc_tax", None)
                        unit_valuation.pop("bad_estimated_value_ex_tax", None)
                    if valuation.get("domains"):
                        unit_domains = {}
                        for domain, by_unit in domains_by_unit.items():
                            domain_entry = by_unit.get(unit) or {
                                "coverage_pct": None,
                            }
                            merged = {
                                **valuation["domains"][domain],
                                "coverage_pct": domain_entry.get("coverage_pct"),
                            }
                            # 三域值用本单位已估金额（B3 05:12）：无已估本桶
                            # → 键移除（空格，B-F06 口径），不摊/复制父域总额。
                            for value_key in (
                                "value_inc_tax", "value_ex_tax"
                            ):
                                if value_key in domain_entry:
                                    merged[value_key] = domain_entry[value_key]
                                else:
                                    merged.pop(value_key, None)
                            unit_domains[domain] = merged
                        unit_valuation["domains"] = unit_domains
                    item["valuation"] = unit_valuation
                data.append(item)
    elif kind == "parts":
        rows = dash.build_part_rows(
            db, filters, include_amounts=visible,
            ratio=ratio if visible else None,
        )
        header, columns = _part_export_columns(visible)
        data = [dict(row) for row in rows]
    elif kind == "receipts":
        rows, _total = dash.build_receipt_detail_rows(
            db, filters, include_amounts=visible,
            test_result=(test_result.strip() or None) if test_result else None,
            source=(source.strip() or None) if source else None,
            ratio=ratio if visible else None,
        )
        header, columns = _receipt_export_columns(visible)
        data = rows
    elif kind == "quantity_checks":
        # q 整单选择与 API 同参同集（B-F10）。
        part_ids, keyword = dash._part_keyword(db, q)
        payload = dash.build_quantity_checks_payload(
            db, filters.scope,
            projects=filters.projects,
            order_date_from=filters.start,
            order_date_to=filters.end,
            part_ids=part_ids, keyword=keyword,
        )
        header, columns = _check_export_columns()
        # 静态审查补充（§104）：导出按 单×单位 展开——件/台分母不并算，
        # 待核量不丢；无单位事实的行保留原行。
        data = []
        for row in payload["rows"]:
            entries = row.get("unit_breakdown") or []
            if not entries:
                item = dict(row)
                item["_unit_label"] = _unit_label_of(row)
                data.append(item)
                continue
            for entry in entries:
                item = dict(row)
                item["_unit_label"] = entry.get("unit") or "待核"
                for key in ("demand_qty", "received_qty", "difference_qty",
                            "over_received_qty", "undated_receipt_qty"):
                    item[key] = entry.get(key)
                data.append(item)
    elif kind == "orders":
        payload = consumption.period_orders(
            db, start=filters.start, end=filters.end, projects=filters.projects,
        )
        header, columns = _orders_export_columns()
        # 静态审查补充（§106）：按 项目×单位 展开，数量不丢；去重单数是
        # 不可相加元信息——只在首单位行出现一次。
        data = []
        for row in payload["rows"]:
            dated = {
                entry.get("unit"): entry
                for entry in row.get("unit_breakdown", [])
            }
            undated = {
                entry.get("unit"): entry
                for entry in row.get("undated_unit_breakdown", [])
            }
            units = dash.sorted_unit_keys(set(dated) | set(undated))
            if not units:
                item = dict(row)
                item["_unit_label"] = _unit_label_of(row)
                data.append(item)
                continue
            for index, unit in enumerate(units):
                item = dict(row)
                item["_unit_label"] = unit or "待核"
                item["order_qty"] = dated.get(unit, {}).get(
                    "order_qty", "0.000"
                )
                item["undated_order_qty"] = undated.get(unit, {}).get(
                    "order_qty", "0.000"
                )
                item["order_count"] = row["order_count"] if index == 0 else None
                item["undated_order_count"] = (
                    row["undated_order_count"] if index == 0 else None
                )
                data.append(item)
    elif kind == "consumption_daily":
        payload = consumption.daily_consumption(
            db, start=filters.start, end=filters.end, projects=filters.projects,
            include_amounts=visible,
        )
        # B-F09：导出按 日×项目×单位 展开（不再输出跨单位合算行）。
        data = []
        for row in payload["rows"]:
            for entry in row.get("unit_breakdown", []):
                data.append({
                    "issue_date": row["issue_date"],
                    "project_code": row["project_code"],
                    "display_name": row["display_name"],
                    "unit": entry.get("unit") or "待核",
                    "issued_qty": entry.get("issued_qty"),
                    "cost_amount_ex_tax": entry.get("cost_amount_ex_tax"),
                    "cost_amount_inc_tax": entry.get("cost_amount_inc_tax"),
                    "uncosted_qty": entry.get("uncosted_qty"),
                    "coverage_pct": entry.get("coverage_pct"),
                    "sources": row.get("sources"),
                })
        header, columns = _consumption_daily_export_columns(visible)
    else:
        rows = consumption.consumption_issue_rows(
            db, start=filters.start, end=filters.end, projects=filters.projects,
            include_amounts=visible,
        )
        data = [dict(row) for row in rows]
        for row in data:
            row["_unit_label"] = row.get("unit") or "待核"
        header, columns = _consumption_issues_export_columns(visible)
    if len(data) > dash.MAX_EXPORT_ROWS:
        raise HTTPException(
            status_code=413,
            detail=f"筛选集超过 {dash.MAX_EXPORT_ROWS} 行，请缩小范围后导出",
        )
    if visible and kind in ("projects", "parts", "receipts"):
        # R34：试算参数伴随导出（仅金额可见）；缺省已按 50% 解析（显式值回显，
        # 未提供时回显默认 0.5）。注意：本端点参数 format 遮蔽内建，不能写
        # format(ratio, "f")。
        for row in data:
            row["_retention_ratio"] = f"{ratio:f}"
    values = [_export_row(row, columns) for row in data]
    filename = f"maintenance-dashboard-{kind}.{format}"
    if format == "csv":
        return _csv_stream(
            header, values, filename, db=db, sanitize=_safe_formula_only,
        )
    return _xlsx_response(header, values, filename, db)


ExportColumn = tuple[str, str | None, bool]  # (标签, 取值路径或常量列, 是否数值)


def _unit_label_of(row: dict) -> str:
    """单位列显示（B-F09/M12）：单一单位照写；仅待核写「待核」；混合「混合」。"""
    unit = row.get("unit")
    if unit is not None:
        return unit
    breakdown = row.get("unit_breakdown") or []
    if not breakdown:
        return ""
    if len(breakdown) == 1 and breakdown[0].get("unit") is None:
        return "待核"
    return "混合"


def _cell(row: dict, path: str):
    node: object = row
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _export_row(row: dict, columns: list[ExportColumn]) -> list:
    values = []
    for _label, path, numeric in columns:
        if path is None:  # 常量列：估价政策版本（仅金额可见导出存在）
            values.append(policy.POLICY_VERSION)
            continue
        value = _cell(row, path)
        if isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value)
        if isinstance(value, dict):
            value = ",".join(f"{k}={v}" for k, v in value.items())
        if numeric and value is not None and value != "":
            value = _decimal_or_text(value)
        values.append(value)
    return values


def _decimal_or_text(value):
    """数值列：Decimal 保留精确文本（CSV），XLSX 侧再转 float；解析失败原样。"""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value


def _project_export_columns(visible: bool) -> tuple[list[str], list[ExportColumn]]:
    columns: list[ExportColumn] = [
        ("项目编码", "project_code", False),
        ("项目名称", "display_name", False),
        ("业务类型", "business_type", False),
        ("单位", "_unit_label", False),
        ("返件实收合计", "receipt_qty", True),
        ("其中坏件实收", "bad_receipt_qty", True),
        ("其中废品", "scrap_receipt_qty", True),
        ("实际领用（未采集件况）", "issued_qty", True),
        ("免返领用量", "exempt_issued_qty", True),
        ("待判定返还要求量", "pending_requirement_qty", True),
        ("未关联需求单实收", "unlinked_receipt_qty", True),
        ("同期返还率%", "return_rate_pct", True),
        ("率分子（同期实收）", "rate_numerator", True),
        ("率分母（同期领用）", "rate_denominator", True),
        ("无返还备注", "no_return_note", False),
    ]
    if visible:
        columns += [
            ("坏件试算价值-含税", "valuation.bad_estimated_value_inc_tax", True),
            ("坏件试算价值-不含税", "valuation.bad_estimated_value_ex_tax", True),
            ("已估坏件量", "valuation.estimated_bad_qty", True),
            ("待估坏件量", "valuation.pending_bad_qty", True),
            ("待试算坏件量（缺比例）", "valuation.awaiting_ratio_bad_qty", True),
            ("覆盖率%", "valuation.coverage_pct", True),
            ("采购域覆盖率%", "valuation.domains.purchase.coverage_pct", True),
            ("销售域覆盖率%", "valuation.domains.sales.coverage_pct", True),
            ("成本域覆盖率%", "valuation.domains.cost.coverage_pct", True),
            # 三域各双税（B3 05:12/M10/R34-06）：双税分别计算，缺估域空格。
            ("三域-采购-坏件价值-含税", "valuation.domains.purchase.value_inc_tax", True),
            ("三域-采购-坏件价值-不含税", "valuation.domains.purchase.value_ex_tax", True),
            ("三域-销售-坏件价值-含税", "valuation.domains.sales.value_inc_tax", True),
            ("三域-销售-坏件价值-不含税", "valuation.domains.sales.value_ex_tax", True),
            ("三域-领用已计成本-价值-含税", "valuation.domains.cost.value_inc_tax", True),
            ("三域-领用已计成本-价值-不含税", "valuation.domains.cost.value_ex_tax", True),
            ("价值保留比例", "_retention_ratio", False),
            ("估价政策版本", None, False),
        ]
    return [label for label, _path, _numeric in columns], columns


def _part_export_columns(visible: bool) -> tuple[list[str], list[ExportColumn]]:
    columns: list[ExportColumn] = [
        ("PN", "pn", False),
        ("描述", "description", False),
        ("单位", "unit", False),
        ("返件实收", "receipt_qty", True),
        ("坏件实收", "bad_receipt_qty", True),
        ("实际领用（未采集件况）", "issued_qty", True),
        ("项目数", "project_count", True),
        ("同期返还率%", "return_rate_pct", True),
        ("率分子（同期实收）", "rate_numerator", True),
        ("率分母（同期领用）", "rate_denominator", True),
    ]
    if visible:
        columns += [
            ("坏件试算价值-含税", "valuation.bad_estimated_value_inc_tax", True),
            ("坏件试算价值-不含税", "valuation.bad_estimated_value_ex_tax", True),
            ("已估坏件量", "valuation.estimated_bad_qty", True),
            ("待估坏件量", "valuation.pending_bad_qty", True),
            ("待试算坏件量（缺比例）", "valuation.awaiting_ratio_bad_qty", True),
            ("覆盖率%", "valuation.coverage_pct", True),
            ("三域-采购-坏件价值-含税", "valuation.domains.purchase.value_inc_tax", True),
            ("三域-采购-坏件价值-不含税", "valuation.domains.purchase.value_ex_tax", True),
            ("三域-销售-坏件价值-含税", "valuation.domains.sales.value_inc_tax", True),
            ("三域-销售-坏件价值-不含税", "valuation.domains.sales.value_ex_tax", True),
            ("三域-领用已计成本-价值-含税", "valuation.domains.cost.value_inc_tax", True),
            ("三域-领用已计成本-价值-不含税", "valuation.domains.cost.value_ex_tax", True),
            ("采购域已估坏件量", "valuation.domains.purchase.estimated_bad_qty", True),
            ("采购域待估坏件量", "valuation.domains.purchase.pending_bad_qty", True),
            ("采购域覆盖率%", "valuation.domains.purchase.coverage_pct", True),
            ("销售域已估坏件量", "valuation.domains.sales.estimated_bad_qty", True),
            ("销售域待估坏件量", "valuation.domains.sales.pending_bad_qty", True),
            ("销售域覆盖率%", "valuation.domains.sales.coverage_pct", True),
            ("成本域已估坏件量", "valuation.domains.cost.estimated_bad_qty", True),
            ("成本域待估坏件量", "valuation.domains.cost.pending_bad_qty", True),
            ("成本域覆盖率%", "valuation.domains.cost.coverage_pct", True),
            ("价值保留比例", "_retention_ratio", False),
            ("估价政策版本", None, False),
        ]
    return [label for label, _path, _numeric in columns], columns


def _receipt_export_columns(visible: bool) -> tuple[list[str], list[ExportColumn]]:
    columns: list[ExportColumn] = [
        ("业务日", "occurred_at", False),
        ("项目编码", "project_code", False),
        ("来源需求单号", "source_order_no", False),
        ("PN", "pn", False),
        ("描述", "description", False),
        ("类型", "receipt_kind", False),
        ("单位", "unit", False),
        ("件况", "test_result", False),
        ("数量", "qty", True),
        ("来源", "source", False),
        ("需复核", "review_required", False),
    ]
    if visible:
        columns += [
            ("估价状态", "valuation.state", False),
            ("选用参考", "valuation.chosen", False),
            ("件况分类", "valuation.classification", False),
            ("折算系数", "valuation.factor", True),
            ("参考价-含税", "valuation.price_inc_tax", True),
            ("参考价-不含税", "valuation.price_ex_tax", True),
            ("行价值-含税", "valuation.value_inc_tax", True),
            ("行价值-不含税", "valuation.value_ex_tax", True),
            ("三域-采购-行价值-含税", "valuation.domains.purchase.value_inc_tax", True),
            ("三域-采购-行价值-不含税", "valuation.domains.purchase.value_ex_tax", True),
            ("三域-销售-行价值-含税", "valuation.domains.sales.value_inc_tax", True),
            ("三域-销售-行价值-不含税", "valuation.domains.sales.value_ex_tax", True),
            ("三域-领用已计成本-价值-含税", "valuation.domains.cost.value_inc_tax", True),
            ("三域-领用已计成本-价值-不含税", "valuation.domains.cost.value_ex_tax", True),
            ("待估原因", "valuation.reason", False),
            ("价值保留比例", "_retention_ratio", False),
            ("估价政策版本", None, False),
        ]
    return [label for label, _path, _numeric in columns], columns


def _check_export_columns() -> tuple[list[str], list[ExportColumn]]:
    columns: list[ExportColumn] = [
        ("需求单号", "order_no", False),
        ("项目编码", "project_code", False),
        ("项目名称", "display_name", False),
        ("制单日期", "order_date", False),
        ("需求类型", "demand_type", False),
        ("仓库", "warehouse", False),
        ("单位", "_unit_label", False),
        ("需求量N", "demand_qty", True),
        ("实收R", "received_qty", True),
        ("差额max(N-R,0)", "difference_qty", True),
        ("超收max(R-N,0)", "over_received_qty", True),
        ("基数状态", "basis_state", False),
        ("待核对原因", "reason_codes", False),
        ("无业务日实收", "undated_receipt_qty", True),
        ("无返还备注", "no_return_note", False),
    ]
    return [label for label, _path, _numeric in columns], columns


def _orders_export_columns() -> tuple[list[str], list[ExportColumn]]:
    """M01 导出：纯数量域，无金额列（任何角色）；无日期单单列（B-F12）。"""
    columns: list[ExportColumn] = [
        ("项目编码", "project_code", False),
        ("项目名称", "display_name", False),
        ("业务类型", "business_type", False),
        ("单位", "_unit_label", False),
        ("期间下单量（原qty，不扣退货/免返）", "order_qty", True),
        ("期间下单单数", "order_count", True),
        ("无制单日期下单量（未计入期间）", "undated_order_qty", True),
        ("无制单日期单数", "undated_order_count", True),
    ]
    return [label for label, _path, _numeric in columns], columns


def _consumption_daily_export_columns(
    visible: bool,
) -> tuple[list[str], list[ExportColumn]]:
    """M03 日表导出：日×项目×单位展开（B-F09）；成本/覆盖/来源列仅利润门。"""
    columns: list[ExportColumn] = [
        ("业务日", "issue_date", False),
        ("项目编码", "project_code", False),
        ("项目名称", "display_name", False),
        ("单位", "unit", False),
        ("领用量", "issued_qty", True),
    ]
    if visible:
        columns += [
            ("已计成本-不含税", "cost_amount_ex_tax", True),
            ("已计成本-含税", "cost_amount_inc_tax", True),
            ("未计成本量", "uncosted_qty", True),
            ("已计成本覆盖率%", "coverage_pct", True),
            ("成本来源", "sources", False),
        ]
    return [label for label, _path, _numeric in columns], columns


def _consumption_issues_export_columns(
    visible: bool,
) -> tuple[list[str], list[ExportColumn]]:
    """M03 领用明细导出：行级数量 + （有权时）成本证据，可核对。"""
    columns: list[ExportColumn] = [
        ("业务日", "issue_date", False),
        ("项目编码", "project_code", False),
        ("项目名称", "display_name", False),
        ("领用单号", "issue_no", False),
        ("PN", "pn", False),
        ("单位", "_unit_label", False),
        ("数量", "qty", True),
    ]
    if visible:
        columns += [
            ("成本来源", "cost_source", False),
            ("参考侧", "reference_side", False),
            ("算法版本", "algorithm_version", False),
            ("不含税单价", "unit_cost_ex_tax", True),
            ("含税单价", "unit_cost_inc_tax", True),
            ("不含税成本", "cost_amount_ex_tax", True),
            ("含税成本", "cost_amount_inc_tax", True),
        ]
    return [label for label, _path, _numeric in columns], columns


# ---------- 流式下载助手（自持副本；勿 import 巨型 api.maintenance） ----------

_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_CSV_SPOOL_MEMORY_BYTES = 5 * 1024 * 1024
_MAX_CSV_DATA_ROWS = 1_000_000
_MAX_CSV_DYNAMIC_TEXT_BYTES = 64 * 1024 * 1024
_MAX_CSV_OUTPUT_BYTES = 512 * 1024 * 1024

_INVALID_DOWNLOAD_NAME = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')
_INVALID_ASCII_FALLBACK = re.compile(r"[^A-Za-z0-9._-]+")
_RFC5987_SAFE = "!#$&+-.^_`|~"


def _safe_formula_only(value):
    """CSV 防公式净化（services/maintenance_dashboard_export_text）。

    单元格超限（控制字符清理后仍 >32767）转 413，不在响应中途 500。
    """
    try:
        return safe_formula_only(value)
    except ExportCellLimitError as exc:
        raise HTTPException(
            status_code=413,
            detail=str(exc),
        ) from exc


class _ClosingStreamingResponse(StreamingResponse):
    def __init__(self, content, resource, **kwargs):
        super().__init__(content, **kwargs)
        self._resource = resource

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with suppress(Exception):
                self._resource.close()


def _iter_download_chunks(resource):
    """按固定块读取二进制下载，避免文件迭代器按换行产生碎片或超大块。"""
    while chunk := resource.read(_DOWNLOAD_CHUNK_BYTES):
        yield chunk


def _release_db_before_stream(db: Session, resource) -> None:
    """流式响应结束前依赖不会 teardown；先归还连接，失败时同时关闭文件。"""
    try:
        db.rollback()
    except BaseException:
        resource.close()
        raise


def _content_disposition(filename: str, ascii_fallback: str | None = None) -> str:
    """生成纯 ASCII 下载响应头，同时保留 RFC 5987 UTF-8 文件名。"""
    clean_name = _INVALID_DOWNLOAD_NAME.sub("_", filename).strip(" .") or "download"
    fallback_source = ascii_fallback or clean_name
    fallback = _INVALID_DOWNLOAD_NAME.sub("_", fallback_source)
    fallback = fallback.encode("ascii", "ignore").decode()
    fallback = _INVALID_ASCII_FALLBACK.sub("_", fallback).strip(" ._") or "download"
    encoded_name = quote(clean_name, safe=_RFC5987_SAFE)
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{encoded_name}"
    )


def _csv_stream(
    header: list,
    rows,
    filename: str,
    ascii_fallback: str | None = None,
    db: Session | None = None,
    sanitize=None,
) -> StreamingResponse:
    """增量写入有界内存 spool；完整校验成功后才开始响应。"""
    sanitize = sanitize or _safe_formula_only
    output = SpooledTemporaryFile(max_size=_CSV_SPOOL_MEMORY_BYTES, mode="w+b")
    row_buffer = io.StringIO(newline="")
    writer = csv.writer(row_buffer)
    dynamic_text_bytes = 0

    def write_row(values) -> None:
        nonlocal dynamic_text_bytes
        safe_values = []
        for value in values:
            safe_value = sanitize(value)
            safe_values.append(safe_value)
            if isinstance(safe_value, str):
                dynamic_text_bytes += len(safe_value.encode("utf-8"))
                if dynamic_text_bytes > _MAX_CSV_DYNAMIC_TEXT_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="CSV 动态文本超过 64 MiB 安全上限",
                    )
        writer.writerow(safe_values)
        row_bytes = row_buffer.getvalue().encode("utf-8")
        if output.tell() + len(row_bytes) > _MAX_CSV_OUTPUT_BYTES:
            raise HTTPException(
                status_code=413,
                detail="CSV 文件超过 512 MiB 安全上限",
            )
        output.write(row_bytes)
        row_buffer.seek(0)
        row_buffer.truncate(0)

    row_iterator = None
    try:
        output.write(b"\xef\xbb\xbf")  # BOM，Excel 正确识别 UTF-8
        write_row(header)
        row_iterator = iter(rows)
        for row_count, row in enumerate(row_iterator, 1):
            if row_count > _MAX_CSV_DATA_ROWS:
                raise HTTPException(
                    status_code=413,
                    detail=f"CSV 数据行超过 {_MAX_CSV_DATA_ROWS} 行上限",
                )
            write_row(row)
        output.seek(0)
        if db is not None:
            _release_db_before_stream(db, output)
    except BaseException:
        output.close()
        raise
    finally:
        close_rows = getattr(row_iterator, "close", None)
        if close_rows is not None:
            with suppress(Exception):
                close_rows()
        row_buffer.close()
    return _ClosingStreamingResponse(
        _iter_download_chunks(output),
        resource=output,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": _content_disposition(filename, ascii_fallback),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _xlsx_response(
    header: list[str], rows: list[list], filename: str, db: Session
) -> StreamingResponse:
    """XLSX：常量行内存 + 有界 spool；数值写数值单元格，文本显式写字符串。"""
    output = SpooledTemporaryFile(max_size=4 * 1024 * 1024, mode="w+b")
    try:
        with Workbook(output, {
            "constant_memory": True, "strings_to_formulas": False,
            "strings_to_urls": False,
        }) as workbook:
            sheet = workbook.add_worksheet("维保数据看板")
            sheet.write_row(0, 0, header)
            sheet.freeze_panes(1, 0)
            for row_index, values in enumerate(rows, 1):
                for column, value in enumerate(values):
                    if value is None:
                        continue
                    if isinstance(value, Decimal):
                        if not value.is_finite():
                            raise HTTPException(422, "导出数值无效")
                        sheet.write_number(row_index, column, float(value))
                        continue
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        sheet.write_number(row_index, column, value)
                        continue
                    if isinstance(value, (list, tuple)):
                        value = ",".join(str(item) for item in value)
                    value = str(value)
                    if len(value) > 32767:
                        raise HTTPException(413, "单元格超过 Excel 字符上限")
                    sheet.write_string(row_index, column, value)
        output.seek(0)
        _release_db_before_stream(db, output)
    except BaseException:
        output.close()
        raise
    return _ClosingStreamingResponse(
        _iter_download_chunks(output), output,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
