"""维保数据看板 MVP API（T1–T4，R33 / D-0022）。

数量与金额分离（R33 第 2 条）：
- 数量端点（options/summary/projects/parts/receipts/quantity-checks/export）只挂
  登录 + page_maintenance + 显式项目范围；maintenance_manager / purchaser 可用。
  无金额权时响应不出现金额字段（不是 0/null），导出不含金额列。
- 金额排序（bad_value_desc）在无金额权时 422，不静默降级（pn-ranking 同款）。
- 估价端点（valuation）额外挂利润门（admin 或 data_profit），否则 403。

非法日期/排序/分页参数一律 422 不 500：枚举走 Query pattern，业务码
（custom 缺日期、起止倒置）由 DashboardInputError 统一转 422。
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from tempfile import SpooledTemporaryFile

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from xlsxwriter import Workbook

from app.api.date_validation import validate_date_query
from app.api.maintenance import (
    _ClosingStreamingResponse,
    _csv_stream,
    _iter_download_chunks,
    _release_db_before_stream,
    _safe_formula_only,
)
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
from app.services import maintenance_dashboard_valuation_policy as policy
from app.services.maintenance_boss_board import BUSINESS_TYPE_FILTER_PATTERN

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


def _filters(
    db: Session,
    ctx: UserContext,
    *,
    range_: str,
    date_from: date | None,
    date_to: date | None,
    project: str | None,
    business_type: str,
    q: str | None,
) -> dash._Filters:
    """范围先收敛后聚合：时间 × 授权项目 × 业务类型 × 关键词。"""
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


def _log(
    ctx: UserContext, action: str, *, scoped: bool | None = None, **flags: bool
) -> None:
    """审计只记标志位，不落关键词/项目 ID 等自由文本（pn-ranking 同款）。"""
    details = dict(flags)
    if scoped is not None:
        details["scope"] = "full" if not scoped else "scoped"
    record_access_log(ctx, action, "maintenance_dashboard", details)


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
    }
    if visible:
        payload["valuation_policy"] = policy.policy_payload()
    _log(ctx, "maintenance_dashboard_options", amounts=visible,
         scoped=scope is not None)
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
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """T1 回收与领用总览：卡片 + 趋势 + 件况结构 + Top 项目。

    金额卡片仅对通过利润门的调用方构建（响应内无该键，而非 0 值）。
    """
    response.headers["Cache-Control"] = "no-store"
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type, q=q,
    )
    _log(
        ctx, "maintenance_dashboard_summary", scoped=filters.scope is not None,
        amounts=_amounts_visible(ctx), keyword=bool(q and q.strip()),
    )
    return dash.build_summary_payload(
        db, filters, include_amounts=_amounts_visible(ctx)
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
    page: int = Query(1, ge=1),
    page_size: int = Query(dash.DEFAULT_PAGE_SIZE, ge=1, le=dash.MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """T2 项目对比：范围内项目全量行（含零行）；返还率按项目逐行给出。"""
    response.headers["Cache-Control"] = "no-store"
    _reject_amount_sort(ctx, sort)
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type, q=q,
    )
    _log(
        ctx, "maintenance_dashboard_projects", scoped=filters.scope is not None,
        keyword=bool(q),
    )
    rows = dash.sort_project_rows(
        dash.build_project_rows(
            db, filters, include_amounts=_amounts_visible(ctx)
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
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """T3 PN 排名：PN 身份归并后的返件/坏件/领用与（可见时）估价列。"""
    response.headers["Cache-Control"] = "no-store"
    _reject_amount_sort(ctx, sort)
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type, q=q,
    )
    _log(ctx, "maintenance_dashboard_parts", scoped=filters.scope is not None,
         keyword=bool(q))
    rows = dash.sort_part_rows(
        dash.build_part_rows(
            db, filters, include_amounts=_amounts_visible(ctx)
        ),
        sort,
    )
    return {"range": _range_payload(filters), **dash.paginate(rows, page, page_size)}


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
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _profit: None = Depends(_require_profit_visibility),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """估价专页（金额域）：政策版本 + 总卡 + 分 PN 估价行。

    独立挂利润门（登录 + 页面权限 + 项目范围 + 利润门缺一不可——
    readonly 兜底模板有 data_profit 但无 page_maintenance，不能只查利润键）。
    """
    response.headers["Cache-Control"] = "no-store"
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type, q=q,
    )
    _log(ctx, "maintenance_dashboard_valuation",
         scoped=filters.scope is not None, amounts=True)
    rows = dash.sort_part_rows(
        dash.build_part_rows(db, filters, include_amounts=True), sort
    )
    bad_value_inc = sum(
        (Decimal(row["valuation"]["bad_estimated_value_inc_tax"]) for row in rows),
        Decimal("0"),
    )
    bad_value_ex = sum(
        (Decimal(row["valuation"]["bad_estimated_value_ex_tax"]) for row in rows),
        Decimal("0"),
    )
    estimated_qty = sum(
        (Decimal(row["valuation"]["estimated_bad_qty"]) for row in rows), Decimal("0")
    )
    pending_qty = sum(
        (Decimal(row["valuation"]["pending_bad_qty"]) for row in rows), Decimal("0")
    )
    bad_receipt_qty = sum(
        (Decimal(row["bad_receipt_qty"]) for row in rows), Decimal("0")
    )
    cards = {
        "bad_estimated_value": {
            "inc_tax": dash._amount(bad_value_inc),
            "ex_tax": dash._amount(bad_value_ex),
        },
        "estimated_bad_qty": dash._qty(estimated_qty),
        "pending_bad_qty": dash._qty(pending_qty),
        "coverage_pct": dash._coverage_pct(estimated_qty, bad_receipt_qty),
    }
    return {
        "range": _range_payload(filters),
        "policy": policy.policy_payload(),
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
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """返件实收明细（T1/T3 下钻）：行级；退货 return_qty 不在此口径。"""
    response.headers["Cache-Control"] = "no-store"
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type, q=q,
    )
    _log(
        ctx, "maintenance_dashboard_receipts", scoped=filters.scope is not None,
        condition=bool(test_result), source=bool(source),
    )
    rows, total = dash.build_receipt_detail_rows(
        db, filters,
        include_amounts=_amounts_visible(ctx),
        test_result=(test_result.strip() or None) if test_result else None,
        source=(source.strip() or None) if source else None,
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
    不发布「欠返/逾期/已返」状态。
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
    _log(ctx, "maintenance_dashboard_quantity_checks",
         scoped=narrowed is not None)
    payload = dash.build_quantity_checks_payload(
        db, narrowed,
        projects=projects,
        order_date_from=start,
        order_date_to=end,
    )
    rows = dash.sort_check_rows(payload["rows"], sort)
    start_index = (page - 1) * page_size
    return {
        "cards": payload["cards"],
        "unassigned_receipts": payload["unassigned_receipts"],
        **dash.paginate(rows, page, page_size),
    }


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

_EXPORT_KINDS = ("projects", "parts", "receipts", "quantity_checks")


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
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """导出当前筛选集：CSV 撇号只加公式形态单元格；XLSX 数值写数值单元格。

    无金额权时导出不含任何金额列与估价元数据（列头都没有，不是置空）。
    """
    visible = _amounts_visible(ctx)
    filters = _filters(
        db, ctx, range_=range_, date_from=date_from, date_to=date_to,
        project=project, business_type=business_type, q=q,
    )
    _log(
        ctx, "maintenance_dashboard_export", scoped=filters.scope is not None,
        amounts=visible, kind=kind,
    )
    if kind == "projects":
        rows = dash.build_project_rows(db, filters, include_amounts=visible)
        header, columns = _project_export_columns(visible)
        data = [dict(row) for row in rows]
    elif kind == "parts":
        rows = dash.build_part_rows(db, filters, include_amounts=visible)
        header, columns = _part_export_columns(visible)
        data = [dict(row) for row in rows]
    elif kind == "receipts":
        rows, _total = dash.build_receipt_detail_rows(
            db, filters, include_amounts=visible,
            test_result=(test_result.strip() or None) if test_result else None,
            source=(source.strip() or None) if source else None,
        )
        header, columns = _receipt_export_columns(visible)
        data = rows
    else:
        payload = dash.build_quantity_checks_payload(
            db, filters.scope,
            projects=filters.projects,
            order_date_from=filters.start,
            order_date_to=filters.end,
        )
        header, columns = _check_export_columns()
        data = payload["rows"]
    if len(data) > dash.MAX_EXPORT_ROWS:
        raise HTTPException(
            status_code=413,
            detail=f"筛选集超过 {dash.MAX_EXPORT_ROWS} 行，请缩小范围后导出",
        )
    values = [_export_row(row, columns) for row in data]
    filename = f"maintenance-dashboard-{kind}.{format}"
    if format == "csv":
        return _csv_stream(
            header, values, filename, db=db, sanitize=_safe_formula_only,
        )
    return _xlsx_response(header, values, filename, db)


ExportColumn = tuple[str, str | None, bool]  # (标签, 取值路径或常量列, 是否数值)


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
            ("坏件估算价值-含税", "valuation.bad_estimated_value_inc_tax", True),
            ("坏件估算价值-不含税", "valuation.bad_estimated_value_ex_tax", True),
            ("待估坏件量", "valuation.pending_bad_qty", True),
            ("覆盖率%", "valuation.coverage_pct", True),
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
            ("坏件估算价值-含税", "valuation.bad_estimated_value_inc_tax", True),
            ("坏件估算价值-不含税", "valuation.bad_estimated_value_ex_tax", True),
            ("已估坏件量", "valuation.estimated_bad_qty", True),
            ("待估坏件量", "valuation.pending_bad_qty", True),
            ("覆盖率%", "valuation.coverage_pct", True),
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
            ("折算系数", "valuation.factor", True),
            ("参考价-含税", "valuation.price_inc_tax", True),
            ("参考价-不含税", "valuation.price_ex_tax", True),
            ("行价值-含税", "valuation.value_inc_tax", True),
            ("行价值-不含税", "valuation.value_ex_tax", True),
            ("待估原因", "valuation.reason", False),
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
