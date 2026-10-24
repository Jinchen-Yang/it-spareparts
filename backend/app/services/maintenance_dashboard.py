"""维保数据看板 MVP（T1–T4）数量聚合与组装（R33 / D-0022）。

口径要点（docs/decisions/0022）：
- 返件实收：``maintenance_rkd_return_line`` ``line_status=active``，按
  ``occurred_at`` 的上海业务日入期（半开区间 [start 00:00, end+1 00:00)）；
  无业务日期的实收不进有界期间，单独报告。
- 实际领用：``maintenance_site_issue`` 映射有效且 confirmed/corrected、行
  ``is_active=true``，按 ``issue_date``；「好件领用」即实际领用量（未采集件况）。
- 退货 ``f_maintenance_line.return_qty`` 只冲需求净量，与返件实收互不抵扣、不相加。
- 同期返还率 = 期间有效实收 ÷ 期间实际领用（复用 ``rate_pct``）；分母≤0 无率，
  有领用无实收为 0%，>100% 如实展示；不剔除免返项目。
- 免返/待判定复用 ``line_details``（只读）；免返量单独一列，不冲减任何量。
- T4 复用 ``summarize_demand_returns``（当前累计，拒绝历史截止日）；逐单
  max(N−R,0) / max(R−N,0)，不发布返还状态。
- 估价：采购参考优先、销售回退；行金额 = qty × 参考价 × 件况系数（R33 系数，
  版本化），四舍五入两位后求和；只估可证明同单位的行；待估不能算成 0。

权限：数量与金额分离。金额区域只对通过利润门（admin 或 data_profit）的
调用方构建——无金额权时响应里不出现金额字段（不是 0/null 数值）。
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Date, and_, case, false, func, or_, select
from sqlalchemy.orm import Session, load_only
from sqlalchemy.sql.selectable import SelectBase

from app.business_time import BUSINESS_TZ, business_today
from app.config import Settings, get_settings
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project import MaintenanceProject
from app.models.system import SysUser
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.security import UserContext
from app.services import maintenance_dashboard_valuation_policy as policy
from app.services.maintenance_dashboard_units import (
    QtyBuckets,
    add_domain_coverage,
    domain_coverage_payload,
    normalize_unit,
    sorted_unit_keys,
)
from app.services.maintenance_dashboard_valuation import (
    batch_price_references,
    value_line,
)
from app.services.maintenance_project_assignments import (
    maintenance_scope_project_ids,
)
from app.services.maintenance_return_metrics import rate_pct
from app.services.maintenance_return_request_summary import (
    MAX_BATCH_SIZE,
    summarize_demand_returns,
)
from app.services.maintenance_site_return_requirements import line_details

RANGE_PRESETS = ("7d", "15d", "30d", "half_year", "year", "all", "custom")
PROJECT_SORTS = (
    "bad_receipt_qty_desc",
    "receipt_qty_desc",
    "issued_qty_desc",
    "project_code_asc",
    "bad_value_desc",
)
PART_SORTS = ("bad_receipt_qty_desc", "receipt_qty_desc", "bad_value_desc")
RECEIPT_SORTS = ("occurred_desc", "qty_desc", "pn_asc")
CHECK_SORTS = ("difference_desc", "over_received_desc", "demand_qty_desc")
_ZERO = Decimal("0")
_QUANT = Decimal("0.01")
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
MAX_EXPORT_ROWS = 50_000
# 总览返件实收排行条数（项目 / 登记人同一口径，2026-10-22 由前 10 扩为前 40）。
TOP_N = 40
# 登记人全量清单上限（前端按人名检索；登记人数远小于此值，超出即截断并标记）。
REGISTRANT_LIMIT = 500


class DashboardInputError(ValueError):
    """非法筛选参数；API 层转 422，不 500。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _qty(value: Decimal) -> str:
    return format(value, ".3f")


def _amount(value: Decimal) -> str:
    return format(value, ".2f")


# ---------- 筛选解析（时间 / 项目 / 关键词） ----------

def _shift_months(day: date, months: int) -> date:
    """日历月平移（R33 第 6 条：半年/一年按日历月），日溢出收到当月末。"""
    month_index = day.month - 1 - months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def resolve_date_range(
    preset: str,
    date_from: date | None,
    date_to: date | None,
    *,
    today: date | None = None,
) -> tuple[date | None, date]:
    """预设 → 实际起止日；默认近 7 天含今天（上海日历日：今天−6 ～ 今天）。"""
    if preset not in RANGE_PRESETS:
        raise DashboardInputError("invalid_range_preset")
    if preset == "custom":
        if date_from is None or date_to is None:
            raise DashboardInputError("custom_requires_dates")
        if date_from > date_to:
            raise DashboardInputError("invalid_date_range")
        return date_from, date_to
    end = today or business_today()
    if preset == "7d":
        start: date | None = end - timedelta(days=6)
    elif preset == "15d":
        start = end - timedelta(days=14)
    elif preset == "30d":
        start = end - timedelta(days=29)
    elif preset == "half_year":
        start = _shift_months(end, 6)
    elif preset == "year":
        start = _shift_months(end, 12)
    else:  # all：无下界，仍不取未来事实（end=今天）。
        start = None
    return start, end


def resolve_project_scope(
    db: Session, ctx: UserContext, requested: list[str] | None
) -> set[str] | None:
    """用户所选项目与授权集合取交集。

    None=全范围（仅未收敛账号）；空集=无可见项目（调用方返回空结果，绝不全量）。
    受限账号手填未授权项目 ID 得到空交集，看不到任何数据。
    """
    authorized = maintenance_scope_project_ids(db, ctx)
    if not requested:
        return authorized
    selected = {item.strip() for item in requested if item.strip()}
    if not selected:
        return authorized
    if authorized is None:
        return selected
    return selected & authorized


def _project_rows(
    db: Session, scope: set[str] | None, *, strict_projection=False
) -> dict[str, MaintenanceProject]:
    statement = select(MaintenanceProject)
    if strict_projection:
        statement = statement.options(load_only(MaintenanceProject.project_id, MaintenanceProject.project_code,
            MaintenanceProject.display_name, MaintenanceProject.business_type, MaintenanceProject.no_return_note,
            raiseload=True))
    if scope is not None:
        if not isinstance(scope, SelectBase) and not scope:
            return {}
        statement = statement.where(MaintenanceProject.project_id.in_(scope))
    statement = statement.order_by(
        MaintenanceProject.project_code.nulls_last(),
        MaintenanceProject.project_id,
    )
    return {project.project_id: project for project in db.scalars(statement)}


def _escape_like(term: str) -> str:
    """关键词字面转义：%/\\_/反斜杠一律按字面匹配，不得变成通配符（B-F02）。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _part_keyword(db: Session, q: str | None) -> tuple[set[int] | None, str | None]:
    """PN/描述关键词：主数据身份命中集合 + 原文兜底 LIKE 关键词。"""
    text = (q or "").strip()
    if not text:
        return None, None
    keyword = f"%{_escape_like(text)}%"
    ids = set(
        db.scalars(
            select(DimPart.id).where(or_(
                DimPart.pn_std.ilike(keyword, escape="\\"),
                DimPart.description.ilike(keyword, escape="\\"),
            ))
        )
    )
    return ids, keyword


@dataclass(slots=True)
class _Filters:
    scope: set[str] | None
    start: date | None
    end: date
    part_ids: set[int] | None
    keyword: str | None
    projects: dict[str, MaintenanceProject]


def _base_filters(
    db: Session,
    *,
    scope: set[str] | None,
    start: date | None,
    end: date,
    q: str | None,
    business_type: str | None = None,
    strict_projection=False,
) -> _Filters:
    """先范围收敛（项目 × 业务类型 × 关键词），后聚合——一切统计共用。"""
    projects = _project_rows(db, scope, strict_projection=strict_projection)
    if business_type:
        projects = {
            project_id: project
            for project_id, project in projects.items()
            if project.business_type == business_type
        }
        scope = set(projects)
    part_ids, keyword = _part_keyword(db, q)
    return _Filters(
        scope=scope,
        start=start,
        end=end,
        part_ids=part_ids,
        keyword=keyword,
        projects=projects,
    )


def _period_bounds(start: date | None, end: date) -> tuple[datetime | None, datetime]:
    lower = (
        datetime.combine(start, time.min, tzinfo=BUSINESS_TZ)
        if start is not None
        else None
    )
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=BUSINESS_TZ)
    return lower, upper


def _occurred_period_conditions(lower: datetime | None, upper: datetime) -> list:
    """实收行期间条件；range=all 无下界时省略 ``>=``（None 不可比较，B-F01）。"""
    conditions = [
        MaintenanceRkdReturnLine.occurred_at.is_not(None),
        MaintenanceRkdReturnLine.occurred_at < upper,
    ]
    if lower is not None:
        conditions.append(MaintenanceRkdReturnLine.occurred_at >= lower)
    return conditions


def _scope_condition(column, scope: set[str] | None) -> list:
    if scope is None:
        return []
    if not isinstance(scope, SelectBase) and not scope:
        # 空集=无可见项目：恒假条件，绝不回退全量。
        return [false()]
    return [column.in_(scope)]


def _keyword_condition(
    part_col, pn_col, *, part_ids: set[int] | None = None,
    keyword: str | None = None,
) -> list:
    """PN/描述关键词条件（B-F10 起可脱离 _Filters 复用，如需求行匹配）。"""
    if part_ids is None and keyword is None:
        return []
    clauses = []
    if part_ids is not None:
        clauses.append(part_col.in_(part_ids) if part_ids else false())
    if keyword:
        # 未解析身份的行只按原始 PN 文本兜底；已解析行以主数据身份过滤。
        clauses.append(and_(
            part_col.is_(None), pn_col.ilike(keyword, escape="\\")
        ))
    if not clauses:
        return [false()]
    return [or_(*clauses)] if len(clauses) > 1 else clauses


def _issue_validity_conditions(filters: _Filters) -> list:
    """实际领用有效口径（S02）：映射有效 + confirmed/corrected + 行有效 + 期间。"""
    conditions = [
        MaintenanceSiteIssue.status_mapping_state == "mapped",
        MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
        MaintenanceSiteIssueLine.is_active.is_(True),
        MaintenanceSiteIssue.issue_date <= filters.end,
        *_scope_condition(MaintenanceSiteIssue.project_id, filters.scope),
    ]
    if filters.start is not None:
        conditions.append(MaintenanceSiteIssue.issue_date >= filters.start)
    return conditions


# ---------- 数量事实 ----------

@dataclass(frozen=True, slots=True)
class ReceiptFact:
    project_id: str
    part_id: int | None
    pn: str
    description: str | None
    test_result: str | None
    receipt_kind: str
    source: str
    qty: Decimal
    unit: str | None = None  # 主数据主单位；未解析身份 → 待核（None，M12）


def _receipt_conditions(filters: _Filters) -> list:
    """期间内有效实收的公共条件（有业务日、授权范围、关键词）。"""
    lower, upper = _period_bounds(filters.start, filters.end)
    return [
        MaintenanceRkdReturnLine.line_status == "active",
        *_occurred_period_conditions(lower, upper),
        *_scope_condition(MaintenanceRkdReturnLine.project_id, filters.scope),
        *_keyword_condition(
            MaintenanceRkdReturnLine.part_id,
            MaintenanceRkdReturnLine.pn,
            part_ids=filters.part_ids, keyword=filters.keyword,
        ),
    ]


def receipt_facts(db: Session, filters: _Filters) -> list[ReceiptFact]:
    """期间内有效实收（有业务日），按项目×身份×件况聚合；退货不参与。"""
    conditions = _receipt_conditions(filters)
    rows = db.execute(
        select(
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.part_id,
            MaintenanceRkdReturnLine.pn,
            func.max(MaintenanceRkdReturnLine.description),
            MaintenanceRkdReturnLine.test_result,
            MaintenanceRkdReturnLine.receipt_kind,
            MaintenanceRkdReturnLine.source,
            func.sum(MaintenanceRkdReturnLine.qty),
        ).where(*conditions).group_by(
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.part_id,
            MaintenanceRkdReturnLine.pn,
            MaintenanceRkdReturnLine.test_result,
            MaintenanceRkdReturnLine.receipt_kind,
            MaintenanceRkdReturnLine.source,
        )
    ).all()
    units = _part_units(
        db, {part_id for _p, part_id, *_rest in rows if part_id is not None}
    )
    return [
        ReceiptFact(
            project_id=project_id, part_id=part_id, pn=pn, description=description,
            test_result=test_result, receipt_kind=receipt_kind, source=source,
            qty=Decimal(qty),
            unit=units.get(part_id) if part_id is not None else None,
        )
        for (project_id, part_id, pn, description, test_result,
             receipt_kind, source, qty) in rows
    ]


@dataclass(frozen=True, slots=True)
class IssueFact:
    project_id: str
    part_id: int | None
    pn: str
    qty: Decimal
    unit: str | None = None  # 主数据主单位；未解析身份 → 待核（None，M12）


def issue_facts(db: Session, filters: _Filters) -> list[IssueFact]:
    """期间内有效实际领用（业务称好件；模型未采集领用件况）。"""
    rows = db.execute(
        select(
            MaintenanceSiteIssue.project_id,
            MaintenanceSiteIssueLine.part_id,
            MaintenanceSiteIssueLine.pn,
            func.sum(MaintenanceSiteIssueLine.quantity),
        )
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
        .where(
            *_issue_validity_conditions(filters),
            *_keyword_condition(
                MaintenanceSiteIssueLine.part_id,
                MaintenanceSiteIssueLine.pn,
                part_ids=filters.part_ids, keyword=filters.keyword,
            ),
        )
        .group_by(
            MaintenanceSiteIssue.project_id,
            MaintenanceSiteIssueLine.part_id,
            MaintenanceSiteIssueLine.pn,
        )
    ).all()
    units = _part_units(
        db, {part_id for _p, part_id, _pn, _q in rows if part_id is not None}
    )
    return [
        IssueFact(
            project_id=project_id, part_id=part_id, pn=pn, qty=Decimal(qty),
            unit=units.get(part_id) if part_id is not None else None,
        )
        for project_id, part_id, pn, qty in rows
    ]


def _part_units(db: Session, part_ids: set[int]) -> dict[int, str | None]:
    """主数据主单位（规范化：空 = 待核 None）；只按 part_id，不猜。"""
    if not part_ids:
        return {}
    return {
        part_id: normalize_unit(unit)
        for part_id, unit in db.execute(
            select(DimPart.id, DimPart.unit).where(DimPart.id.in_(part_ids))
        ).all()
    }


def undated_receipts_by_project(
    db: Session, filters: _Filters
) -> dict[str, dict[str | None, Decimal]]:
    """当前有效实收中无业务日期的量（当前累计口径，不入期间；项目×单位）。"""
    conditions = [
        MaintenanceRkdReturnLine.line_status == "active",
        MaintenanceRkdReturnLine.occurred_at.is_(None),
        *_scope_condition(MaintenanceRkdReturnLine.project_id, filters.scope),
    ]
    rows = db.execute(
        select(
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.part_id,
            func.sum(MaintenanceRkdReturnLine.qty),
        ).where(*conditions).group_by(
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.part_id,
        )
    ).all()
    units = _part_units(db, {part_id for _p, part_id, _q in rows
                             if part_id is not None})
    result: dict[str, dict[str | None, Decimal]] = defaultdict(dict)
    for project_id, part_id, qty in rows:
        unit = units.get(part_id) if part_id is not None else None
        result[project_id][unit] = (
            result[project_id].get(unit, _ZERO) + Decimal(qty)
        )
    return dict(result)


def unlinked_receipts_by_project_in_period(
    db: Session, filters: _Filters
) -> dict[str, dict[str | None, Decimal]]:
    """期间内未关联需求单的有效实收（M10）：项目×单位分桶，不抵任何需求单。"""
    lower, upper = _period_bounds(filters.start, filters.end)
    conditions = [
        MaintenanceRkdReturnLine.line_status == "active",
        *_occurred_period_conditions(lower, upper),
        MaintenanceRkdReturnLine.source_order_id.is_(None),
        *_scope_condition(MaintenanceRkdReturnLine.project_id, filters.scope),
    ]
    rows = db.execute(
        select(
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.part_id,
            func.sum(MaintenanceRkdReturnLine.qty),
        ).where(*conditions).group_by(
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.part_id,
        )
    ).all()
    units = _part_units(db, {part_id for _p, part_id, _q in rows
                             if part_id is not None})
    result: dict[str, dict[str | None, Decimal]] = defaultdict(dict)
    for project_id, part_id, qty in rows:
        unit = units.get(part_id) if part_id is not None else None
        result[project_id][unit] = (
            result[project_id].get(unit, _ZERO) + Decimal(qty)
        )
    return dict(result)


def daily_trend(db: Session, filters: _Filters) -> list[dict]:
    """每日实收与实际领用（各按自身业务日，日×单位分桶）；有界期间按日补零。"""
    lower, upper = _period_bounds(filters.start, filters.end)
    receipt_conditions = [
        MaintenanceRkdReturnLine.line_status == "active",
        *_occurred_period_conditions(lower, upper),
        *_scope_condition(MaintenanceRkdReturnLine.project_id, filters.scope),
    ]
    sh_receipt_day = func.cast(
        func.timezone(BUSINESS_TZ.key, MaintenanceRkdReturnLine.occurred_at), Date
    )
    receipt_rows = db.execute(
        select(
            sh_receipt_day, MaintenanceRkdReturnLine.part_id,
            func.sum(MaintenanceRkdReturnLine.qty),
        ).where(*receipt_conditions).group_by(sh_receipt_day,
                                              MaintenanceRkdReturnLine.part_id)
    ).all()
    receipt_units = _part_units(db, {part_id for _d, part_id, _q in receipt_rows
                                     if part_id is not None})
    receipts: dict[date, dict[str | None, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: _ZERO)
    )
    for day, part_id, qty in receipt_rows:
        unit = receipt_units.get(part_id) if part_id is not None else None
        receipts[day][unit] += Decimal(qty)
    issue_rows = db.execute(
        select(
            MaintenanceSiteIssue.issue_date,
            MaintenanceSiteIssueLine.part_id,
            func.sum(MaintenanceSiteIssueLine.quantity),
        )
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
        .where(*_issue_validity_conditions(filters))
        .group_by(
            MaintenanceSiteIssue.issue_date, MaintenanceSiteIssueLine.part_id
        )
    ).all()
    issue_units = _part_units(db, {part_id for _d, part_id, _q in issue_rows
                                   if part_id is not None})
    issues: dict[date, dict[str | None, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: _ZERO)
    )
    for day, part_id, qty in issue_rows:
        unit = issue_units.get(part_id) if part_id is not None else None
        issues[day][unit] += Decimal(qty)
    days = sorted(set(receipts) | set(issues))
    if not days:
        return []
    cursor = filters.start or min(days)
    filled = []
    while cursor <= filters.end:
        day_receipts = receipts.get(cursor, {})
        day_issues = issues.get(cursor, {})
        filled.append({
            "date": cursor.isoformat(),
            "receipt_qty": _flat_qty_by_units(day_receipts),
            "issued_qty": _flat_qty_by_units(day_issues),
            "unit_breakdown": [
                {
                    "unit": unit,
                    "receipt_qty": _qty(day_receipts.get(unit, _ZERO)),
                    "issued_qty": _qty(day_issues.get(unit, _ZERO)),
                }
                for unit in sorted_unit_keys(
                    set(day_receipts) | set(day_issues)
                )
            ],
        })
        cursor += timedelta(days=1)
    return filled


def _flat_qty_by_units(unit_qtys: dict[str | None, Decimal]) -> str | None:
    """单一单位（含仅待核/空）→ 合计；混合单位 → None（M12 不混算）。"""
    if len(unit_qtys) > 1:
        return None
    return _qty(sum(unit_qtys.values(), _ZERO))


def exempt_and_pending_qty(
    db: Session, filters: _Filters
) -> dict[str, dict[str, Any]]:
    """免返领用量（M08）与返还要求待判定量（M09），按项目分桶。

    复用 ``line_details`` 当前规则（含旧冻结义务覆盖），只读；免返量独立展示，
    不冲减实收/领用/N/R，也不参与返还率。
    """
    conditions = [
        *_issue_validity_conditions(filters),
    ]
    if filters.part_ids is not None:
        conditions.append(
            MaintenanceSiteIssueLine.part_id.in_(filters.part_ids)
            if filters.part_ids
            else false()
        )
    rows = db.execute(
        select(
            MaintenanceSiteIssue.project_id,
            MaintenanceSiteIssueLine.issue_line_id,
            MaintenanceSiteIssueLine.part_id,
            MaintenanceSiteIssueLine.pn,
            MaintenanceSiteIssueLine.quantity,
            MaintenanceSiteIssueLine.no_return,
            MaintenanceSiteIssueLine.delivery_line_id,
            MaintenanceSiteIssueLine.is_active,
        )
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
        .where(*conditions)
    ).all()
    rows_part_ids = {part_id for (_p, _l, part_id, *_rest) in rows
                     if part_id is not None}
    grouped: dict[str, list[MaintenanceSiteIssueLine]] = defaultdict(list)
    for (project_id, line_id, part_id, pn, quantity,
         no_return, delivery_line_id, is_active) in rows:
        grouped[project_id].append(MaintenanceSiteIssueLine(
            issue_line_id=line_id, part_id=part_id, pn=pn, quantity=quantity,
            no_return=no_return, delivery_line_id=delivery_line_id,
            is_active=is_active,
        ))
    line_units = _part_units(
        db, {part_id for part_id in rows_part_ids if part_id is not None}
    )
    result: dict[str, dict[str, Any]] = {}
    for project_id, lines in grouped.items():
        project = filters.projects.get(project_id)
        if project is None:
            # 项目不在收敛后的范围（如业务类型筛选后）：不借道免返桶泄漏。
            continue
        details = line_details(db, lines, project=project)
        exempt = sum(
            (Decimal(item["return_requirement"]["exempt_quantity"])
             for item in details.values()),
            _ZERO,
        )
        pending = sum(
            (Decimal(item["return_requirement"]["pending_quantity"])
             for item in details.values()),
            _ZERO,
        )
        line_unit_by_id = {
            line.issue_line_id: (
                line_units.get(line.part_id) if line.part_id is not None else None
            )
            for line in lines
        }
        exempt_units: dict[str | None, Decimal] = defaultdict(lambda: _ZERO)
        pending_units: dict[str | None, Decimal] = defaultdict(lambda: _ZERO)
        for line_id, item in details.items():
            unit = line_unit_by_id.get(line_id)
            exempt_units[unit] += Decimal(
                item["return_requirement"]["exempt_quantity"]
            )
            pending_units[unit] += Decimal(
                item["return_requirement"]["pending_quantity"]
            )
        result[project_id] = {
            "exempt_qty": exempt, "pending_qty": pending,
            "exempt_units": dict(exempt_units),
            "pending_units": dict(pending_units),
        }
    return result


# ---------- PN 身份归并（S06：part_id 优先，唯一规范 PN 回退，歧义保留原桶） ----------

@dataclass(slots=True)
class _PnBucket:
    key: tuple[str, int | str]
    part_id: int | None
    pn: str
    description: str | None = None
    receipt_qty: Decimal = _ZERO
    bad_qty: Decimal = _ZERO
    issued_qty: Decimal = _ZERO
    project_ids: set[str] = field(default_factory=set)


def _pn_buckets(
    db: Session,
    receipts: list[ReceiptFact],
    issues: list[IssueFact],
) -> dict[tuple[str, int | str], _PnBucket]:
    fallback_pns = {
        fact.pn.strip().upper()
        for fact in receipts
        if fact.part_id is None and fact.pn.strip()
    } | {
        fact.pn.strip().upper()
        for fact in issues
        if fact.part_id is None and fact.pn.strip()
    }
    parts: dict[int, DimPart] = {}
    candidates: dict[str, set[int]] = defaultdict(set)
    part_ids = {
        fact.part_id for fact in receipts if fact.part_id is not None
    } | {fact.part_id for fact in issues if fact.part_id is not None}
    if part_ids or fallback_pns:
        for part in db.scalars(select(DimPart).where(or_(
            DimPart.id.in_(part_ids),
            func.upper(func.btrim(DimPart.pn_std)).in_(fallback_pns),
        ))):
            parts[part.id] = part
            candidates[part.pn_std.strip().upper()].add(part.id)

    def _bucket_for(part_id: int | None, pn: str, description: str | None) -> _PnBucket:
        resolved = part_id
        if resolved is None:
            matches = candidates.get((pn or "").strip().upper(), set())
            resolved = next(iter(matches)) if len(matches) == 1 else None
        part = parts.get(resolved) if resolved is not None else None
        key = ("part", resolved) if resolved is not None else ("pn", (pn or "").strip().upper())
        return _PnBucket(
            key=key, part_id=resolved,
            pn=part.pn_std if part is not None else (pn or "").strip().upper(),
            description=part.description if part is not None else description,
        )

    buckets: dict[tuple[str, int | str], _PnBucket] = {}
    for fact in receipts:
        probe = _bucket_for(fact.part_id, fact.pn, fact.description)
        bucket = buckets.setdefault(probe.key, probe)
        bucket.receipt_qty += fact.qty
        if policy.is_bad_condition(fact.test_result):
            bucket.bad_qty += fact.qty
        bucket.description = bucket.description or fact.description
        bucket.project_ids.add(fact.project_id)
    for fact in issues:
        probe = _bucket_for(fact.part_id, fact.pn, None)
        bucket = buckets.setdefault(probe.key, probe)
        bucket.issued_qty += fact.qty
        bucket.project_ids.add(fact.project_id)
    return buckets


# ---------- 估价 ----------

@dataclass(slots=True)
class _PartValuation:
    bad_estimated_qty: Decimal = _ZERO
    bad_value_inc: Decimal = _ZERO
    bad_value_ex: Decimal = _ZERO
    finished_value_inc: Decimal = _ZERO
    finished_value_ex: Decimal = _ZERO
    pending_qty: Decimal = _ZERO
    awaiting_qty: Decimal = _ZERO
    pending_reasons: dict[str, Decimal] = field(default_factory=dict)
    bad_domain_values: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    domain_units: dict[str, dict] = field(default_factory=dict)  # B-F07 域覆盖分桶

    def add_pending(self, qty: Decimal, reason: str) -> None:
        self.pending_qty += qty
        self.pending_reasons[reason] = (
            self.pending_reasons.get(reason, _ZERO) + qty
        )

    def add_awaiting(self, qty: Decimal) -> None:
        self.awaiting_qty += qty

    def add_domain_value(self, domain: str, inc: Decimal, ex: Decimal) -> None:
        slot = self.bad_domain_values.setdefault(
            domain, {"inc": _ZERO, "ex": _ZERO}
        )
        slot["inc"] += inc
        slot["ex"] += ex


def _domain_values_payload(
    values: dict[str, dict[str, Decimal]] | None
) -> dict[str, dict[str, str]]:
    """三域分值并列（R34-05）：只呈现已有值，缺域不补 0、不相加。"""
    if not values:
        return {}
    return {
        domain: {
            "value_inc_tax": _amount(slot["inc"]),
            "value_ex_tax": _amount(slot["ex"]),
        }
        for domain, slot in sorted(values.items())
    }


def aggregate_main_units_from_rows(
    rows: list[dict],
) -> dict[str | None, dict[str, Decimal]]:
    """PN 行（各行单单位）合并为主域覆盖单位桶（/valuation 卡；KPI=分组）。"""
    units: dict[str | None, dict[str, Decimal]] = {}
    for row in rows:
        valuation = row.get("valuation") or {}
        bucket = units.setdefault(row.get("unit"), {
            "bad": _ZERO, "estimated": _ZERO,
            "pending": _ZERO, "awaiting": _ZERO,
            "value_inc": _ZERO, "value_ex": _ZERO, "has_value": False,
        })
        bucket["bad"] += Decimal(row.get("bad_receipt_qty") or "0")
        for field, key in (("estimated", "estimated_bad_qty"),
                           ("pending", "pending_bad_qty"),
                           ("awaiting", "awaiting_ratio_bad_qty")):
            value = valuation.get(key)
            if value is not None:
                bucket[field] += Decimal(value)
        # 主域逐单位已估金额（静态审查补充；仅已估行携带）。
        if "bad_estimated_value_inc_tax" in valuation:
            bucket["value_inc"] += Decimal(
                valuation["bad_estimated_value_inc_tax"]
            )
            bucket["value_ex"] += Decimal(
                valuation["bad_estimated_value_ex_tax"]
            )
            bucket["has_value"] = True
    return units


def aggregate_domain_cards_from_rows(rows: list[dict]) -> dict[str, dict]:
    """PN 行三域载荷合并为卡级域覆盖（值 + 分桶；/valuation cards.domains）。"""
    store: dict[str, dict] = {}
    values: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        unit = row.get("unit")
        for domain, payload in (row.get("valuation") or {}).get(
            "domains", {}
        ).items():
            estimated = payload.get("estimated_bad_qty")
            if estimated is not None:
                add_domain_coverage(
                    store, domain, unit, Decimal(estimated), kind="estimated",
                    value_inc=(
                        Decimal(payload["value_inc_tax"])
                        if "value_inc_tax" in payload else None
                    ),
                    value_ex=(
                        Decimal(payload["value_ex_tax"])
                        if "value_ex_tax" in payload else None
                    ),
                )
            for reason, qty in payload.get("pending_reasons", {}).items():
                add_domain_coverage(
                    store, domain, unit, Decimal(qty),
                    kind="pending", reason=reason,
                )
            awaiting = payload.get("awaiting_ratio_bad_qty")
            if awaiting is not None:
                add_domain_coverage(
                    store, domain, unit, Decimal(awaiting), kind="awaiting"
                )
            if "value_inc_tax" in payload:
                slot = values.setdefault(
                    domain, {"inc": _ZERO, "ex": _ZERO}
                )
                slot["inc"] += Decimal(payload["value_inc_tax"])
                slot["ex"] += Decimal(payload["value_ex_tax"])
    return _domain_payloads(store, values)


def _domain_payloads(
    coverage: dict[str, dict] | None,
    values: dict[str, dict[str, Decimal]] | None,
) -> dict[str, dict[str, Any]]:
    """三域覆盖 + 金额合并载荷（B-F07）。

    有坏件行的三域覆盖照报（缺样域无金额键、覆盖 0% 是真实 0%）；
    分母 = 同单位坏件量（coverage_basis=bad_receipts）。
    """
    coverage = coverage or {}
    values = values or {}
    payloads: dict[str, dict[str, Any]] = {}
    for domain in sorted(set(coverage) | set(values)):
        slot = values.get(domain)
        payloads[domain] = domain_coverage_payload(
            coverage.get(domain),
            values=(
                {"inc_tax": slot["inc"], "ex_tax": slot["ex"]}
                if slot is not None else None
            ),
            has_value=slot is not None,
            qty_formatter=_qty,
            amount_formatter=_amount,
            coverage_pct=_coverage_pct,
        )
    return payloads


@dataclass(slots=True)
class ValuationBundle:
    per_part: dict[int, _PartValuation] = field(default_factory=dict)
    project_bad_value_inc: dict[str, Decimal] = field(default_factory=dict)
    project_bad_value_ex: dict[str, Decimal] = field(default_factory=dict)
    project_bad_estimated_qty: dict[str, Decimal] = field(default_factory=dict)
    project_pending_bad_qty: dict[str, Decimal] = field(default_factory=dict)
    project_awaiting_ratio_bad_qty: dict[str, Decimal] = field(default_factory=dict)
    line_values: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_bad_inc: Decimal = _ZERO
    total_bad_ex: Decimal = _ZERO
    total_finished_inc: Decimal = _ZERO
    total_finished_ex: Decimal = _ZERO
    total_bad_domains: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    estimated_bad_qty: Decimal = _ZERO
    pending_bad_qty: Decimal = _ZERO
    awaiting_ratio_bad_qty: Decimal = _ZERO
    ratio: Decimal | None = None
    references: dict[int, Any] = field(default_factory=dict)
    # B-F07：三域返件估值覆盖（分母=同单位坏件），按 PN/项目/总览三级。
    project_domain_coverage: dict[str, dict[str, dict]] = field(
        default_factory=dict
    )
    total_domain_coverage: dict[str, dict] = field(default_factory=dict)
    project_domain_values: dict[str, dict[str, dict[str, Decimal]]] = field(
        default_factory=dict
    )
    # 主域覆盖的单位分桶（B-F09）：平面混合 → null + unit_breakdown。
    project_main_units: dict[str, dict[str | None, dict[str, Decimal]]] = field(
        default_factory=dict
    )
    total_main_units: dict[str | None, dict[str, Decimal]] = field(
        default_factory=dict
    )


def _line_domains_payload(
    qty: Decimal,
    factor: Decimal | None,
    references: Any,
) -> dict[str, dict[str, Any]]:
    """行级三域并列：市场域给参考价，成本域给单位成本；有系数才给行值。"""
    domains: dict[str, dict[str, Any]] = {}
    for name, domain in (("purchase", references.purchase),
                         ("sales", references.sales)):
        entry: dict[str, Any] = {"available": domain.available}
        if domain.available:
            entry["price_inc_tax"] = _amount(domain.inc_tax)
            entry["price_ex_tax"] = _amount(domain.ex_tax)
            if factor is not None:
                value_inc, value_ex = value_line(
                    qty, factor, (domain.inc_tax, domain.ex_tax)
                )
                entry["value_inc_tax"] = _amount(value_inc)
                entry["value_ex_tax"] = _amount(value_ex)
        else:
            entry["reason"] = domain.unavailable_reason
        domains[name] = entry
    cost = references.cost
    entry = {"available": cost.available}
    if cost.available:
        entry["unit_cost_inc_tax"] = _amount(cost.unit_cost_inc_tax)
        entry["unit_cost_ex_tax"] = _amount(cost.unit_cost_ex_tax)
        if factor is not None:
            value_inc, value_ex = value_line(
                qty, factor, (cost.unit_cost_inc_tax, cost.unit_cost_ex_tax)
            )
            entry["value_inc_tax"] = _amount(value_inc)
            entry["value_ex_tax"] = _amount(value_ex)
    else:
        entry["reason"] = cost.unavailable_reason
    domains["cost"] = entry
    return domains


def build_valuation(
    db: Session,
    filters: _Filters,
    *,
    ratio: Decimal | None = None,
    settings: Settings | None = None,
) -> ValuationBundle:
    """对期间内件况已知的实收行做三域批量试算估价（R34）。

    每行 V = qty × 参考价 × 系数（HALF_UP 两位）后求和；系数 = 价值保留比例
    （坏品类行）或成品全值。HTTP 层按缺省 50% 解析（Sunny 01:20），ratio=None
    仅服务直调出现——此时坏品行标「待试算」（awaiting_ratio），绝不算 0。
    三域（采购/销售/领用已计成本）分值并列不相加；
    主展示口径采购优先、销售回退，成本不暗替。待估行记原因与数量。
    """
    settings = settings or get_settings()
    bundle = ValuationBundle(ratio=ratio)
    conditions = _receipt_conditions(filters)
    lines = db.execute(
        select(
            MaintenanceRkdReturnLine.rkd_line_id,
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.part_id,
            MaintenanceRkdReturnLine.pn,
            MaintenanceRkdReturnLine.qty,
            MaintenanceRkdReturnLine.test_result,
        ).where(*conditions)
    ).all()
    if not lines:
        return bundle
    identity = _resolve_receipt_parts(
        db, [(row.rkd_line_id, row.part_id, row.pn) for row in lines]
    )
    part_ids = {part_id for part_id in identity.values() if part_id is not None}
    bundle.references = batch_price_references(
        db, part_ids, anchor=filters.end, scope=filters.scope, settings=settings
    )
    raw_units = _part_units(
        db, {row.part_id for row in lines if row.part_id is not None}
    )
    for row in lines:
        classification = policy.classify_line(row.test_result)
        is_bad = classification == "bad"
        factor = policy.line_factor(classification, ratio)
        qty = Decimal(row.qty)
        part_id = identity.get(row.rkd_line_id)
        references = bundle.references.get(part_id) if part_id is not None else None
        # 单位归属只认原始 part_id（未解析 → 待核桶，M12 不借道 PN 猜测）。
        unit = raw_units.get(row.part_id) if row.part_id is not None else None
        if classification == "unknown":
            state, reason = "pending", "unknown_condition"
        elif part_id is None:
            state, reason = "pending", "pn_unresolved"
        elif references is None or references.price is None:
            state = "pending"
            reason = (
                references.chosen_reason
                if references is not None else "pn_unresolved"
            )
        elif is_bad and ratio is None:
            state, reason = "awaiting_ratio", None
        else:
            state, reason = "ready", None
        line_payload: dict[str, Any] = {"state": state, "classification": classification}
        if reason is not None:
            line_payload["reason"] = reason
        if references is not None:
            line_payload["domains"] = _line_domains_payload(qty, factor, references)
        if state == "ready":
            price = references.price
            assert price is not None  # ready 必有市场主展示价
            value_inc, value_ex = value_line(qty, factor, price)
            line_payload.update({
                "chosen": references.chosen,
                "chosen_reason": references.chosen_reason,
                "factor": format(factor, "f"),
                "price_inc_tax": _amount(price[0]),
                "price_ex_tax": _amount(price[1]),
                "value_inc_tax": _amount(value_inc),
                "value_ex_tax": _amount(value_ex),
            })
        bundle.line_values[row.rkd_line_id] = line_payload
        if state == "pending":
            if is_bad:
                agg = bundle.per_part.setdefault(part_id or -1, _PartValuation())
                agg.add_pending(qty, reason)
                bundle.pending_bad_qty += qty
                bundle.project_pending_bad_qty[row.project_id] = (
                    bundle.project_pending_bad_qty.get(row.project_id, _ZERO) + qty
                )
                # 某域缺样本不抹其它域（R34-05）：主展示待估时，可用域
                #（如领用已计成本）的分值仍并列累计，行级/NP/总览一致。
                _aggregate_bad_domains(agg, bundle, line_payload, row.project_id)
                _accumulate_bad_coverage(
                    bundle, agg, project_id=row.project_id, unit=unit,
                    qty=qty, state=state, line_payload=line_payload,
                )
            continue
        if state == "awaiting_ratio":
            agg = bundle.per_part.setdefault(part_id or -1, _PartValuation())
            agg.add_awaiting(qty)
            bundle.awaiting_ratio_bad_qty += qty
            bundle.project_awaiting_ratio_bad_qty[row.project_id] = (
                bundle.project_awaiting_ratio_bad_qty.get(row.project_id, _ZERO)
                + qty
            )
            _accumulate_bad_coverage(
                bundle, agg, project_id=row.project_id, unit=unit,
                qty=qty, state=state, line_payload=line_payload,
            )
            continue
        # ready：主展示口径 + 三域分值并列。
        agg = bundle.per_part.setdefault(part_id, _PartValuation())
        if is_bad:
            agg.bad_estimated_qty += qty
            agg.bad_value_inc += value_inc
            agg.bad_value_ex += value_ex
            bundle.total_bad_inc += value_inc
            bundle.total_bad_ex += value_ex
            bundle.estimated_bad_qty += qty
            bundle.project_bad_value_inc[row.project_id] = (
                bundle.project_bad_value_inc.get(row.project_id, _ZERO) + value_inc
            )
            bundle.project_bad_value_ex[row.project_id] = (
                bundle.project_bad_value_ex.get(row.project_id, _ZERO) + value_ex
            )
            bundle.project_bad_estimated_qty[row.project_id] = (
                bundle.project_bad_estimated_qty.get(row.project_id, _ZERO) + qty
            )
            _aggregate_bad_domains(agg, bundle, line_payload, row.project_id)
            _accumulate_bad_coverage(
                bundle, agg, project_id=row.project_id, unit=unit,
                qty=qty, state=state, line_payload=line_payload,
            )
        else:
            # 成品按参考全值另列（R33 语义保留）：不混入坏件三域聚合。
            agg.finished_value_inc += value_inc
            agg.finished_value_ex += value_ex
            bundle.total_finished_inc += value_inc
            bundle.total_finished_ex += value_ex
    return bundle


def _aggregate_bad_domains(
    agg: _PartValuation, bundle: ValuationBundle, line_payload: dict[str, Any],
    project_id: str,
) -> None:
    """坏件行三域分值并列累计（R34-05）。

    只累计已有值键的域（缺样域无值自然跳过）；坏件主卡与总览同口径，
    成品行不混入。无比例（factor None）时各域无值键，天然不累计。
    """
    for domain_name, entry in line_payload.get("domains", {}).items():
        if "value_inc_tax" not in entry:
            continue
        value_inc = Decimal(entry["value_inc_tax"])
        value_ex = Decimal(entry["value_ex_tax"])
        agg.add_domain_value(domain_name, value_inc, value_ex)
        slot = bundle.total_bad_domains.setdefault(
            domain_name, {"inc": _ZERO, "ex": _ZERO}
        )
        slot["inc"] += value_inc
        slot["ex"] += value_ex
        project_slot = (
            bundle.project_domain_values.setdefault(project_id, {})
            .setdefault(domain_name, {"inc": _ZERO, "ex": _ZERO})
        )
        project_slot["inc"] += value_inc
        project_slot["ex"] += value_ex


def _accumulate_bad_coverage(
    bundle: ValuationBundle,
    agg: _PartValuation,
    *,
    project_id: str,
    unit: str | None,
    qty: Decimal,
    state: str,
    line_payload: dict[str, Any],
) -> None:
    """坏件行覆盖累计（B-F07/B-F09）：三域三级 + 主域单位分桶。

    域 kind：有值键 = 已估（factor=0 也是已估）；无值且行待试算 =
    awaiting；其余 = pending（原因 = 域不可用原因 / pn_unresolved）。
    主域 kind 随行 state；单位桶 {bad, estimated, pending, awaiting}。
    """
    project_domains = bundle.project_domain_coverage.setdefault(project_id, {})
    for domain_name, entry in line_payload.get("domains", {}).items():
        if "value_inc_tax" in entry:
            kind, reason = "estimated", None
        elif state == "awaiting_ratio":
            kind, reason = "awaiting", None
        else:
            kind = "pending"
            reason = entry.get("reason") or "pn_unresolved"
        domain_value_inc = (
            Decimal(entry["value_inc_tax"]) if kind == "estimated" else None
        )
        domain_value_ex = (
            Decimal(entry["value_ex_tax"]) if kind == "estimated" else None
        )
        add_domain_coverage(
            agg.domain_units, domain_name, unit, qty, kind=kind, reason=reason,
            value_inc=domain_value_inc, value_ex=domain_value_ex,
        )
        add_domain_coverage(
            project_domains, domain_name, unit, qty, kind=kind, reason=reason,
            value_inc=domain_value_inc, value_ex=domain_value_ex,
        )
        add_domain_coverage(
            bundle.total_domain_coverage, domain_name, unit, qty,
            kind=kind, reason=reason,
            value_inc=domain_value_inc, value_ex=domain_value_ex,
        )
    main_kind = (
        "estimated" if state == "ready"
        else "awaiting" if state == "awaiting_ratio"
        else "pending"
    )
    # 主域逐单位已估金额（静态审查补充：导出按单位展开用本桶值）。
    main_value_inc = (
        Decimal(line_payload["value_inc_tax"])
        if main_kind == "estimated" and "value_inc_tax" in line_payload
        else None
    )
    main_value_ex = (
        Decimal(line_payload["value_ex_tax"])
        if main_kind == "estimated" and "value_ex_tax" in line_payload
        else None
    )
    project_units = bundle.project_main_units.setdefault(project_id, {})
    for units_store in (project_units, bundle.total_main_units):
        bucket = units_store.setdefault(unit, {
            "bad": _ZERO, "estimated": _ZERO,
            "pending": _ZERO, "awaiting": _ZERO,
            "value_inc": _ZERO, "value_ex": _ZERO, "has_value": False,
        })
        bucket["bad"] += qty
        bucket[main_kind] += qty
        if main_value_inc is not None and main_value_ex is not None:
            bucket["value_inc"] += main_value_inc
            bucket["value_ex"] += main_value_ex
            bucket["has_value"] = True


def _resolve_receipt_parts(
    db: Session, lines: list[tuple[str, int | None, str]]
) -> dict[str, int | None]:
    """行稳定身份：part_id 优先；无 id 时唯一规范 PN 回退，歧义保持待估。"""
    resolved: dict[str, int | None] = {}
    fallback_pns = {
        pn.strip().upper() for _line_id, part_id, pn in lines
        if part_id is None and pn
    }
    parts: dict[str, list[int]] = defaultdict(list)
    if fallback_pns:
        for part_id, pn_std in db.execute(
            select(DimPart.id, DimPart.pn_std).where(
                func.upper(func.btrim(DimPart.pn_std)).in_(fallback_pns)
            )
        ):
            parts[pn_std.strip().upper()].append(part_id)
    for line_id, part_id, pn in lines:
        if part_id is not None:
            resolved[line_id] = part_id
        else:
            candidates = parts.get((pn or "").strip().upper(), [])
            resolved[line_id] = candidates[0] if len(candidates) == 1 else None
    return resolved


def _coverage_pct(estimated: Decimal, received: Decimal) -> float | None:
    """覆盖率 = 已估坏件数量 ÷ 坏件实收数量；M02=0 时无比例。"""
    if received <= 0:
        return None
    return rate_pct(estimated, received)


# ---------- T1 总览 ----------

def _user_display_names(db: Session, usernames: set[str]) -> dict[str, str]:
    """登记人账号 → 显示名（停用账号照常解析；无显示名不臆造）。"""
    if not usernames:
        return {}
    return {
        username: name.strip()
        for username, name in db.execute(
            select(SysUser.username, SysUser.display_name)
            .where(SysUser.username.in_(usernames))
        ).all()
        if name and name.strip()
    }


def registrant_rows(db: Session, filters: _Filters) -> tuple[list[dict], bool]:
    """期间内返件实收按登记人（created_by）聚合，返件实收降序。

    登记人 = 行的 created_by：手工登记为登记账号，RKD 导入为执行导入的账号。
    数量按单位分桶（同项目排行，不跨单位求和）；坏件判定同总览卡片。
    """
    rows = db.execute(
        select(
            MaintenanceRkdReturnLine.created_by,
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.part_id,
            MaintenanceRkdReturnLine.test_result,
            MaintenanceRkdReturnLine.source,
            func.sum(MaintenanceRkdReturnLine.qty),
            func.count(),
            func.max(MaintenanceRkdReturnLine.occurred_at),
        ).where(*_receipt_conditions(filters)).group_by(
            MaintenanceRkdReturnLine.created_by,
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.part_id,
            MaintenanceRkdReturnLine.test_result,
            MaintenanceRkdReturnLine.source,
        )
    ).all()
    units = _part_units(
        db, {part_id for _u, _p, part_id, *_rest in rows if part_id is not None}
    )
    per_user: dict[str, dict[str, Any]] = {}
    for (username, project_id, part_id, test_result, source, qty, count,
         last_at) in rows:
        entry = per_user.setdefault(username, {
            "buckets": QtyBuckets("receipt_qty", "bad_receipt_qty"),
            "lines": 0, "projects": set(), "sources": set(), "last_at": None,
        })
        unit = units.get(part_id) if part_id is not None else None
        qty = Decimal(qty)
        entry["buckets"].add(
            unit, receipt_qty=qty,
            bad_receipt_qty=qty if policy.is_bad_condition(test_result) else _ZERO,
        )
        entry["lines"] += int(count)
        entry["projects"].add(project_id)
        entry["sources"].add(source)
        if last_at is not None and (
            entry["last_at"] is None or last_at > entry["last_at"]
        ):
            entry["last_at"] = last_at
    ordered = sorted(
        per_user.items(),
        key=lambda item: (-item[1]["buckets"].total("receipt_qty"), item[0]),
    )
    truncated = len(ordered) > REGISTRANT_LIMIT
    ordered = ordered[:REGISTRANT_LIMIT]
    names = _user_display_names(db, {username for username, _ in ordered})

    def _flat(value: Decimal | None) -> str | None:
        return None if value is None else _qty(value)

    return [
        {
            "created_by": username,
            "created_by_name": names.get(username),
            "receipt_qty": _flat(entry["buckets"].flat("receipt_qty")),
            "bad_receipt_qty": _flat(entry["buckets"].flat("bad_receipt_qty")),
            "line_count": entry["lines"],
            "project_count": len(entry["projects"]),
            "sources": sorted(entry["sources"]),
            "last_occurred_at": (
                entry["last_at"].isoformat() if entry["last_at"] else None
            ),
            "unit_breakdown": entry["buckets"].breakdown(
                "receipt_qty", "bad_receipt_qty", formatter=_qty_formatter,
            ),
        }
        for username, entry in ordered
    ], truncated


def build_summary_payload(
    db: Session,
    filters: _Filters,
    *,
    include_amounts: bool,
    ratio: Decimal | None = None,
    settings: Settings | None = None,
) -> dict:
    receipts = receipt_facts(db, filters)
    issues = issue_facts(db, filters)
    # 数量按单位分桶（M12/B-F09）：单一单位平面兼容，混合 → null + 分桶。
    receipt_units = QtyBuckets("total", "part", "machine")
    bad_units = QtyBuckets("total", "scrap")
    issue_units = QtyBuckets("total")
    condition_units: dict[str, QtyBuckets] = {}
    for fact in receipts:
        receipt_units.add(
            fact.unit, total=fact.qty,
            part=fact.qty if fact.receipt_kind != "machine" else _ZERO,
            machine=fact.qty if fact.receipt_kind == "machine" else _ZERO,
        )
        if policy.is_bad_condition(fact.test_result):
            bad_units.add(
                fact.unit, total=fact.qty,
                scrap=(
                    fact.qty
                    if (fact.test_result or "").strip() == "废品" else _ZERO
                ),
            )
        key = (fact.test_result or "").strip() or "未填"
        condition_units.setdefault(key, QtyBuckets("qty")).add(
            fact.unit, qty=fact.qty
        )
    for fact in issues:
        issue_units.add(fact.unit, total=fact.qty)
    projects_with_facts = {f.project_id for f in receipts} | {
        f.project_id for f in issues
    }
    undated = undated_receipts_by_project(db, filters)

    def _flat(value: Decimal | None) -> str | None:
        return None if value is None else _qty(value)

    def _qty_entry(_field: str, value: Decimal) -> str:
        return _qty(value)

    receipt_total = receipt_units.flat("total")
    issue_total = issue_units.flat("total")
    # 率平面可比性按**单位并集**：只收件+只领台时两侧各自单桶、flat 均
    # 非 None，但分子分母跨单位 → 平面率三字段必须 null（B3 05:10 独立
    # 验收 66.7 反例）；数量卡各自身真实值不受影响。
    rate_unit_set = set(receipt_units.units()) | set(issue_units.units())
    rate_comparable = len(rate_unit_set) <= 1
    # v3：每单位率/分子/分母容器（混合单位平面 null 时 UI 的可消费来源；
    # 同单位内部计算，不跨单位，不猜未知物理单位）。
    rate_breakdown = []
    for unit in sorted_unit_keys(rate_unit_set):
        numerator = receipt_units.items().get(unit, {}).get("total", _ZERO)
        denominator = issue_units.items().get(unit, {}).get("total", _ZERO)
        rate_breakdown.append({
            "unit": unit,
            "rate_numerator": _qty(numerator),
            "rate_denominator": _qty(denominator),
            "return_rate_pct": rate_pct(numerator, denominator),
        })
    cards: dict[str, Any] = {
        "receipt_qty": {
            "total": _flat(receipt_total),
            "part": _flat(receipt_units.flat("part")),
            "machine": _flat(receipt_units.flat("machine")),
            "unit_breakdown": receipt_units.breakdown(
                "total", "part", "machine", formatter=_qty_entry
            ),
        },
        "bad_receipt_qty": {
            "total": _flat(bad_units.flat("total")),
            "scrap": _flat(bad_units.flat("scrap")),
            "unit_breakdown": bad_units.breakdown(
                "total", "scrap", formatter=_qty_entry
            ),
        },
        "issued_qty": {
            "total": _flat(issue_total),
            "note": "实际领用量（未采集件况）",
            "unit_breakdown": issue_units.breakdown(
                "total", formatter=_qty_entry
            ),
        },
        "return_rate_pct": (
            rate_pct(receipt_total, issue_total)
            if rate_comparable and receipt_total is not None
            and issue_total is not None else None
        ),
        "rate_numerator": _flat(receipt_total) if rate_comparable else None,
        "rate_denominator": _flat(issue_total) if rate_comparable else None,
        "return_rate_breakdown": rate_breakdown,
        "project_count": len(projects_with_facts),
    }
    if include_amounts:
        bundle = build_valuation(db, filters, ratio=ratio, settings=settings)
        # 全待估不再 ready+0.00（B-F06 同口径）：已估>0 → ready，
        # 否则待估>0 → pending，再待试算，无坏件行按有无比例。
        if bundle.estimated_bad_qty > 0:
            card_state = "ready"
        elif bundle.pending_bad_qty > 0:
            card_state = "pending"
        elif bundle.awaiting_ratio_bad_qty > 0:
            card_state = "awaiting_ratio"
        else:
            card_state = "ready" if ratio is not None else "awaiting_ratio"
        main_units = bundle.total_main_units
        main_mixed = len(main_units) > 1

        def _main_flat(field: str) -> str | None:
            if main_mixed:
                return None
            return _qty(sum((b[field] for b in main_units.values()), _ZERO))

        valuation_card: dict[str, Any] = {
            "state": card_state,
            "estimated_bad_qty": _main_flat("estimated"),
            "pending_bad_qty": _main_flat("pending"),
            "awaiting_ratio_bad_qty": _main_flat("awaiting"),
            "coverage_pct": (
                _coverage_pct(
                    sum((b["estimated"] for b in main_units.values()), _ZERO),
                    sum((b["bad"] for b in main_units.values()), _ZERO),
                )
                if ratio is not None and not main_mixed else None
            ),  # 无比例 → 未试算，覆盖率不给 0%；混合单位 → 分桶内给
            "retention_ratio": (
                format(ratio, "f") if ratio is not None else None
            ),
            "domains": _domain_payloads(
                bundle.total_domain_coverage, bundle.total_bad_domains
            ),
            "unit_breakdown": [
                {
                    "unit": unit,
                    "bad_qty": _qty(bucket["bad"]),
                    "estimated_bad_qty": _qty(bucket["estimated"]),
                    "pending_bad_qty": _qty(bucket["pending"]),
                    "awaiting_ratio_bad_qty": _qty(bucket["awaiting"]),
                    # 本单位已估金额（仅已估单位给；待估单位无金额键）。
                    **({
                        "value_inc_tax": _amount(bucket["value_inc"]),
                        "value_ex_tax": _amount(bucket["value_ex"]),
                    } if bucket.get("has_value") else {}),
                    "coverage_pct": _coverage_pct(
                        bucket["estimated"], bucket["bad"]
                    ),
                }
                for unit, bucket in sorted(
                    main_units.items(), key=lambda item: (
                        item[0] is None, item[0] or ""
                    )
                )
            ],
            "policy_version": policy.POLICY_VERSION,
            "note": policy.NOTES,
        }
        if ratio is not None:
            # 待估不填 0：全部坏件行均无可估样本时不出现 0 元卡（数量桶仍在）。
            if bundle.estimated_bad_qty > 0:
                valuation_card["bad_estimated_value"] = {
                    "inc_tax": _amount(bundle.total_bad_inc),
                    "ex_tax": _amount(bundle.total_bad_ex),
                }
            if (
                bundle.total_finished_inc != _ZERO
                or bundle.total_finished_ex != _ZERO
            ):
                valuation_card["finished_estimated_value"] = {
                    "inc_tax": _amount(bundle.total_finished_inc),
                    "ex_tax": _amount(bundle.total_finished_ex),
                }
        cards["valuation"] = valuation_card

    project_units: dict[str, QtyBuckets] = {}
    for fact in receipts:
        buckets = project_units.setdefault(
            fact.project_id,
            QtyBuckets("receipt_qty", "bad_receipt_qty", "issued_qty"),
        )
        buckets.add(
            fact.unit, receipt_qty=fact.qty,
            bad_receipt_qty=_ZERO, issued_qty=_ZERO,
        )
        if policy.is_bad_condition(fact.test_result):
            buckets.add(
                fact.unit, receipt_qty=_ZERO,
                bad_receipt_qty=fact.qty, issued_qty=_ZERO,
            )
    for fact in issues:
        buckets = project_units.setdefault(
            fact.project_id,
            QtyBuckets("receipt_qty", "bad_receipt_qty", "issued_qty"),
        )
        buckets.add(
            fact.unit, receipt_qty=_ZERO,
            bad_receipt_qty=_ZERO, issued_qty=fact.qty,
        )
    top_projects = sorted(
        project_units.items(),
        key=lambda item: (-item[1].total("receipt_qty"), item[0]),
    )[:TOP_N]

    registrants, registrants_truncated = registrant_rows(db, filters)

    notes = []
    # v3：无业务日期实收提示单位写实——单一单位给合计；混合不 asserting
    # 单一单位，逐单位列出（不猜「件」）。
    undated_units = QtyBuckets("qty")
    for per_project in undated.values():
        for unit, qty in per_project.items():
            undated_units.add(unit, qty=qty)
    if undated_units.total("qty") > 0:
        entries = [
            f"{unit if unit is not None else '待核'} {_qty(fields['qty'])}"
            for unit, fields in sorted(
                undated_units.items().items(),
                key=lambda item: (item[0] is None, item[0] or ""),
            )
        ]
        if len(undated_units.units()) == 1:
            (only_unit,) = undated_units.units()
            label = only_unit if only_unit is not None else "待核"
            notes.append(
                f"另有 {_qty(undated_units.flat('qty'))} "
                f"{label}无业务日期实收（当前累计，未计入期间）"
            )
        else:
            notes.append(
                "另有无业务日期实收（当前累计，未计入期间）："
                + "、".join(entries)
            )
    return {
        "range": {
            "start": filters.start.isoformat() if filters.start else None,
            "end": filters.end.isoformat(),
        },
        "cards": cards,
        "trend": daily_trend(db, filters),
        "condition_structure": [
            {
                "test_result": key,
                "qty": _flat(buckets.flat("qty")),
                "unit_breakdown": buckets.breakdown(
                    "qty", formatter=_qty_entry
                ),
            }
            for key, buckets in sorted(
                condition_units.items(),
                key=lambda kv: (-kv[1].total("qty"), kv[0]),
            )
        ],
        "top_projects": [
            {
                "project_id": project_id,
                "project_code": _project_field(filters, project_id, "project_code"),
                "display_name": _project_field(
                    filters, project_id, "display_name"
                ) or project_id,
                "receipt_qty": _flat(buckets.flat("receipt_qty")),
                "bad_receipt_qty": _flat(buckets.flat("bad_receipt_qty")),
                "issued_qty": _flat(buckets.flat("issued_qty")),
                "unit_breakdown": buckets.breakdown(
                    "receipt_qty", "bad_receipt_qty", "issued_qty",
                    formatter=_qty_entry,
                ),
            }
            for project_id, buckets in top_projects
        ],
        "registrants": registrants,
        "registrants_truncated": registrants_truncated,
        "notes": notes,
    }


def _project_field(
    filters: _Filters, project_id: str, attribute: str
) -> Any:
    project = filters.projects.get(project_id)
    return getattr(project, attribute, None)


# ---------- T2 项目对比 ----------

def _flat_qty(value: Decimal | None) -> str | None:
    """平面数量：None（混合单位）保持 None；其余照常格式化（M12）。"""
    return None if value is None else _qty(value)


def _qty_formatter(_field: str, value: Decimal) -> str:
    return _qty(value)


def build_project_rows(
    db: Session,
    filters: _Filters,
    *,
    include_amounts: bool,
    ratio: Decimal | None = None,
    settings: Settings | None = None,
) -> list[dict]:
    receipts = receipt_facts(db, filters)
    issues = issue_facts(db, filters)
    unlinked = unlinked_receipts_by_project_in_period(db, filters)
    exempt = exempt_and_pending_qty(db, filters)

    # 数量按项目×单位分桶（B-F09）：单一单位平面兼容，混合 → null + 分桶。
    qty_by_project: dict[str, QtyBuckets] = {}

    def _buckets(store_project_id: str) -> QtyBuckets:
        return qty_by_project.setdefault(
            store_project_id,
            QtyBuckets(
                "receipt_qty", "bad_receipt_qty", "scrap_receipt_qty",
                "issued_qty", "exempt_issued_qty", "pending_requirement_qty",
                "unlinked_receipt_qty",
            ),
        )

    for fact in receipts:
        buckets = _buckets(fact.project_id)
        buckets.add(fact.unit, receipt_qty=fact.qty)
        if policy.is_bad_condition(fact.test_result):
            buckets.add(fact.unit, bad_receipt_qty=fact.qty)
            if (fact.test_result or "").strip() == "废品":
                buckets.add(fact.unit, scrap_receipt_qty=fact.qty)
    for fact in issues:
        _buckets(fact.project_id).add(fact.unit, issued_qty=fact.qty)
    for project_id, unit_qtys in unlinked.items():
        for unit, qty in unit_qtys.items():
            _buckets(project_id).add(unit, unlinked_receipt_qty=qty)
    for project_id, entry in exempt.items():
        for unit, qty in entry.get("exempt_units", {}).items():
            _buckets(project_id).add(unit, exempt_issued_qty=qty)
        for unit, qty in entry.get("pending_units", {}).items():
            _buckets(project_id).add(unit, pending_requirement_qty=qty)

    bundle = (
        build_valuation(db, filters, ratio=ratio, settings=settings)
        if include_amounts else None
    )
    rows = []
    for project_id, project in filters.projects.items():
        buckets = qty_by_project.get(project_id)
        if buckets is None:
            buckets = QtyBuckets(
                "receipt_qty", "bad_receipt_qty", "scrap_receipt_qty",
                "issued_qty", "exempt_issued_qty", "pending_requirement_qty",
                "unlinked_receipt_qty",
            )
        received = buckets.flat("receipt_qty")
        issued = buckets.flat("issued_qty")
        bad = buckets.total("bad_receipt_qty")
        row: dict[str, Any] = {
            "project_id": project_id,
            "project_code": project.project_code,
            "display_name": project.display_name,
            "business_type": project.business_type,
            "receipt_qty": _flat_qty(received),
            "bad_receipt_qty": _flat_qty(buckets.flat("bad_receipt_qty")),
            "scrap_receipt_qty": _flat_qty(buckets.flat("scrap_receipt_qty")),
            "issued_qty": _flat_qty(issued),
            "exempt_issued_qty": _flat_qty(buckets.flat("exempt_issued_qty")),
            "pending_requirement_qty": _flat_qty(
                buckets.flat("pending_requirement_qty")
            ),
            "unlinked_receipt_qty": _flat_qty(
                buckets.flat("unlinked_receipt_qty")
            ),
            "return_rate_pct": (
                rate_pct(received, issued)
                if received is not None and issued is not None else None
            ),
            "rate_numerator": _flat_qty(received),
            "rate_denominator": _flat_qty(issued),
            "no_return_note": project.no_return_note,
            "unit": buckets.single_unit(),
            # 静态审查补充：单位内给全 率/分子/分母（可核对，不只有数量）。
            "unit_breakdown": [
                {
                    "unit": unit,
                    **{
                        field: _qty(unit_fields[field])
                        for field in (
                            "receipt_qty", "bad_receipt_qty",
                            "scrap_receipt_qty", "issued_qty",
                            "exempt_issued_qty", "pending_requirement_qty",
                            "unlinked_receipt_qty",
                        )
                    },
                    "rate_numerator": _qty(unit_fields["receipt_qty"]),
                    "rate_denominator": _qty(unit_fields["issued_qty"]),
                    "return_rate_pct": rate_pct(
                        unit_fields["receipt_qty"], unit_fields["issued_qty"]
                    ),
                }
                for unit, unit_fields in (
                    (unit, buckets.items()[unit])
                    for unit in sorted_unit_keys(buckets.units())
                )
            ],
        }
        if include_amounts and bundle is not None:
            estimated = bundle.project_bad_estimated_qty.get(project_id, _ZERO)
            pending_qty = bundle.project_pending_bad_qty.get(project_id, _ZERO)
            awaiting_qty = bundle.project_awaiting_ratio_bad_qty.get(
                project_id, _ZERO
            )
            # B-F06 状态机：全待估不得 ready+0.00；显式 ratio=0 有合格参考
            # 是已估（estimated>0）→ ready、值 "0.00" 真实保留。
            if estimated > 0:
                state = "ready"
            elif pending_qty > 0:
                state = "pending"
            elif awaiting_qty > 0:
                state = "awaiting_ratio"
            else:
                state = "ready" if ratio is not None else "awaiting_ratio"
            main_units = bundle.project_main_units.get(project_id, {})
            main_mixed = len(main_units) > 1
            valuation: dict[str, Any] = {
                "state": state,
                "estimated_bad_qty": (
                    None if main_mixed else _qty(estimated)
                ),
                "pending_bad_qty": (
                    None if main_mixed else _qty(pending_qty)
                ),
                "awaiting_ratio_bad_qty": (
                    None if main_mixed else _qty(awaiting_qty)
                ),
                "coverage_pct": (
                    _coverage_pct(estimated, bad)
                    if ratio is not None and not main_mixed else None
                ),  # 无比例 → 未试算；混合单位 → 分桶内给
                "unit_breakdown": [
                    {
                        "unit": unit,
                        "bad_qty": _qty(main_units[unit]["bad"]),
                        "estimated_bad_qty": _qty(main_units[unit]["estimated"]),
                        "pending_bad_qty": _qty(main_units[unit]["pending"]),
                        "awaiting_ratio_bad_qty": _qty(
                            main_units[unit]["awaiting"]
                        ),
                        # 本单位已估金额（静态审查补充；仅已估单位给）。
                        **({
                            "value_inc_tax": _amount(
                                main_units[unit]["value_inc"]
                            ),
                            "value_ex_tax": _amount(
                                main_units[unit]["value_ex"]
                            ),
                        } if main_units[unit].get("has_value") else {}),
                        "coverage_pct": _coverage_pct(
                            main_units[unit]["estimated"],
                            main_units[unit]["bad"],
                        ),
                    }
                    for unit in sorted_unit_keys(main_units.keys())
                ],
                "domains": _domain_payloads(
                    bundle.project_domain_coverage.get(project_id),
                    bundle.project_domain_values.get(project_id),
                ),
            }
            if ratio is not None and estimated > 0:
                # 值键仅已估出现（B-F06）：全待估不写 0.00；金额跨单位小计。
                valuation["bad_estimated_value_inc_tax"] = _amount(
                    bundle.project_bad_value_inc.get(project_id, _ZERO)
                )
                valuation["bad_estimated_value_ex_tax"] = _amount(
                    bundle.project_bad_value_ex.get(project_id, _ZERO)
                )
            row["valuation"] = valuation
        rows.append(row)
    return rows


# ---------- T3 PN 排名与估价 ----------

def build_part_rows(
    db: Session,
    filters: _Filters,
    *,
    include_amounts: bool,
    ratio: Decimal | None = None,
    settings: Settings | None = None,
) -> list[dict]:
    receipts = receipt_facts(db, filters)
    issues = issue_facts(db, filters)
    buckets = _pn_buckets(db, receipts, issues)
    part_master: dict[int, DimPart] = {}
    valued_part_ids = {
        bucket.part_id for bucket in buckets.values() if bucket.part_id is not None
    }
    if valued_part_ids:
        for part in db.scalars(select(DimPart).where(DimPart.id.in_(valued_part_ids))):
            part_master[part.id] = part
    bundle = (
        build_valuation(db, filters, ratio=ratio, settings=settings)
        if include_amounts else None
    )
    rows = []
    for bucket in buckets.values():
        part = part_master.get(bucket.part_id) if bucket.part_id is not None else None
        row: dict[str, Any] = {
            "part_id": bucket.part_id,
            "pn": bucket.pn,
            "description": bucket.description,
            "unit": part.unit if part is not None else None,
            "receipt_qty": _qty(bucket.receipt_qty),
            "bad_receipt_qty": _qty(bucket.bad_qty),
            "issued_qty": _qty(bucket.issued_qty),
            "project_count": len(bucket.project_ids),
            "return_rate_pct": rate_pct(bucket.receipt_qty, bucket.issued_qty),
            "rate_numerator": _qty(bucket.receipt_qty),
            "rate_denominator": _qty(bucket.issued_qty),
        }
        if include_amounts and bundle is not None:
            agg = bundle.per_part.get(bucket.part_id, _PartValuation())
            references = bundle.references.get(bucket.part_id)
            if agg.bad_estimated_qty > 0:
                state = "ready"
            elif agg.awaiting_qty > 0:
                state = "awaiting_ratio"
            elif agg.pending_qty > 0:
                state = "pending"
            else:
                state = "ready" if ratio is not None else "awaiting_ratio"
            valuation: dict[str, Any] = {
                "state": state,
                "estimated_bad_qty": _qty(agg.bad_estimated_qty),
                "pending_bad_qty": _qty(agg.pending_qty),
                "awaiting_ratio_bad_qty": _qty(agg.awaiting_qty),
                "pending_reasons": {
                    reason: _qty(qty)
                    for reason, qty in sorted(agg.pending_reasons.items())
                },
                "coverage_pct": (
                    _coverage_pct(agg.bad_estimated_qty, bucket.bad_qty)
                    if ratio is not None else None
                ),  # 无比例 → 未试算，覆盖率不给 0%
            }
            if ratio is not None:
                if state != "pending":
                    # 待估（无任何可估样本）不填 0 元主卡；混合行按已估部分给值。
                    valuation["bad_estimated_value_inc_tax"] = _amount(agg.bad_value_inc)
                    valuation["bad_estimated_value_ex_tax"] = _amount(agg.bad_value_ex)
                # 成品按参考全值另列（不混坏件主卡）。
                valuation["finished_estimated_value_inc_tax"] = _amount(
                    agg.finished_value_inc
                )
                valuation["finished_estimated_value_ex_tax"] = _amount(
                    agg.finished_value_ex
                )
                valuation["domains"] = _domain_payloads(
                    agg.domain_units, agg.bad_domain_values
                )
            row["valuation"] = valuation
            row["references"] = _references_payload(references)
        rows.append(row)
    return rows


def _references_payload(references) -> dict:
    """三域参考并列展示（R34-05）：采购优先、销售回退、领用已计成本独立。"""
    if references is None:
        return {
            "purchase": {"available": False, "unavailable_reason": "no_samples"},
            "sales": {"available": False, "unavailable_reason": "no_samples"},
            "cost": {"available": False, "unavailable_reason": "no_samples"},
            "chosen": None, "chosen_reason": "no_samples",
        }
    cost_payload = references.cost.payload()
    if "coverage_pct" in cost_payload:
        # 两覆盖分别命名（B-F07）：参考侧是「领用已计覆盖」（如 1/10），
        # 与成本域「返件估值覆盖」（bad_receipts 分母）不得混用。
        cost_payload["coverage_basis"] = "issued_lines"
    return {
        "purchase": references.purchase.payload(),
        "sales": references.sales.payload(),
        "cost": cost_payload,
        "chosen": references.chosen,
        "chosen_reason": references.chosen_reason,
    }


# ---------- 实收明细（T1/T3 下钻） ----------

def build_receipt_detail_rows(
    db: Session,
    filters: _Filters,
    *,
    include_amounts: bool,
    test_result: str | None = None,
    source: str | None = None,
    created_by: str | None = None,
    ratio: Decimal | None = None,
    settings: Settings | None = None,
) -> tuple[list[dict], int]:
    """行级实收明细（完整筛选集先取全量再分页；金额列仅 V）。"""
    conditions = _receipt_conditions(filters)
    if test_result is not None:
        conditions.append(MaintenanceRkdReturnLine.test_result == test_result)
    if source is not None:
        conditions.append(MaintenanceRkdReturnLine.source == source)
    if created_by is not None:
        conditions.append(MaintenanceRkdReturnLine.created_by == created_by)
    lines = db.execute(
        select(
            MaintenanceRkdReturnLine.rkd_line_id,
            MaintenanceRkdReturnLine.occurred_at,
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.source_order_id,
            MaintenanceRkdReturnLine.part_id,
            MaintenanceRkdReturnLine.pn,
            MaintenanceRkdReturnLine.description,
            MaintenanceRkdReturnLine.receipt_kind,
            MaintenanceRkdReturnLine.test_result,
            MaintenanceRkdReturnLine.qty,
            MaintenanceRkdReturnLine.source,
            MaintenanceRkdReturnLine.review_required,
            MaintenanceRkdReturnLine.created_by,
        )
        .where(*conditions)
        .order_by(
            MaintenanceRkdReturnLine.occurred_at.desc(),
            MaintenanceRkdReturnLine.rkd_line_id,
        )
    ).all()
    order_nos = {}
    order_ids = {row.source_order_id for row in lines if row.source_order_id}
    if order_ids:
        order_nos = dict(db.execute(
            select(FMaintenanceOrder.raw_order_id, FMaintenanceOrder.order_no)
            .where(FMaintenanceOrder.raw_order_id.in_(order_ids))
        ).all())
    units = _line_units(db, {row.part_id for row in lines if row.part_id is not None})
    creator_names = _user_display_names(db, {row.created_by for row in lines})
    bundle = (
        build_valuation(db, filters, ratio=ratio, settings=settings)
        if include_amounts else None
    )
    rows = []
    for line in lines:
        row: dict[str, Any] = {
            "rkd_line_id": line.rkd_line_id,
            "occurred_at": line.occurred_at.isoformat() if line.occurred_at else None,
            "project_id": line.project_id,
            "project_code": _project_field(filters, line.project_id, "project_code"),
            "source_order_id": line.source_order_id,
            "source_order_no": order_nos.get(line.source_order_id),
            "part_id": line.part_id,
            "pn": line.pn,
            "description": line.description,
            "receipt_kind": line.receipt_kind,
            "unit": units.get(line.part_id) if line.part_id is not None else None,
            "test_result": line.test_result,
            "qty": _qty(Decimal(line.qty)),
            "source": line.source,
            "review_required": bool(line.review_required),
            "created_by": line.created_by,
            "created_by_name": creator_names.get(line.created_by),
        }
        if include_amounts and bundle is not None:
            valuation = bundle.line_values.get(line.rkd_line_id, {
                "state": "pending", "reason": "unknown_condition",
            })
            row["valuation"] = valuation
        rows.append(row)
    return rows, len(rows)


def _line_units(db: Session, part_ids: set[int]) -> dict[int, str | None]:
    if not part_ids:
        return {}
    return {
        part_id: unit
        for part_id, unit in db.execute(
            select(DimPart.id, DimPart.unit).where(DimPart.id.in_(part_ids))
        ).all()
    }


# ---------- T4 需求单数量核对（当前累计） ----------

def _scoped_order_ids(
    db: Session,
    scope: set[str] | None,
    *,
    order_date_from: date | None = None,
    order_date_to: date | None = None,
) -> list[str]:
    """范围内有效归属的需求单（稳定 raw_order_id）；制单日期仅筛列表集合。"""
    from app.models.maintenance_source_assignment import (
        MaintenanceSourceOrderAssignment,
    )

    statement = (
        select(FMaintenanceOrder.raw_order_id)
        .join(
            MaintenanceSourceOrderAssignment,
            and_(
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            ),
        )
        .where(FMaintenanceOrder.data_status == "已生效")
    )
    if scope is not None:
        if not isinstance(scope, SelectBase) and not scope:
            return []
        statement = statement.where(
            MaintenanceSourceOrderAssignment.project_id.in_(scope)
        )
    if order_date_from is not None:
        statement = statement.where(FMaintenanceOrder.order_date >= order_date_from)
    if order_date_to is not None:
        statement = statement.where(FMaintenanceOrder.order_date <= order_date_to)
    return list(db.scalars(statement.order_by(FMaintenanceOrder.raw_order_id)))


def _quantity_check_unit_split(
    db: Session, order_ids: list[str],
) -> dict[str, dict[str, dict[str | None, Decimal]]]:
    """整单 N/R 的逐单位只读适配（静态审查补充 §104）。

    核心口径镜像：需求 = is_active 行；实收 = line_status=active 收货行
    （全历史）；不改 summarize_demand_returns。part_id → DimPart.unit
    （null/空 → 待核）；part_id=None 的收货行也入待核桶。
    """
    if not order_ids:
        return {}
    id_to_raw = {
        row.id: row.raw_order_id
        for row in db.execute(
            select(FMaintenanceOrder.id, FMaintenanceOrder.raw_order_id)
            .where(FMaintenanceOrder.raw_order_id.in_(order_ids))
        )
    }
    demand_rows = db.execute(
        select(
            FMaintenanceLine.order_id,
            FMaintenanceLine.part_id,
            func.sum(func.coalesce(FMaintenanceLine.qty, _ZERO)),
        )
        .where(
            FMaintenanceLine.order_id.in_(id_to_raw),
            FMaintenanceLine.is_active.is_(True),
        )
        .group_by(FMaintenanceLine.order_id, FMaintenanceLine.part_id)
    ).all()
    receipt_rows = db.execute(
        select(
            MaintenanceRkdReturnLine.source_order_id,
            MaintenanceRkdReturnLine.part_id,
            func.sum(MaintenanceRkdReturnLine.qty),
            func.sum(case(
                (MaintenanceRkdReturnLine.occurred_at.is_(None),
                 MaintenanceRkdReturnLine.qty),
                else_=_ZERO,
            )),
        )
        .where(
            MaintenanceRkdReturnLine.source_order_id.in_(order_ids),
            MaintenanceRkdReturnLine.line_status == "active",
        )
        .group_by(
            MaintenanceRkdReturnLine.source_order_id,
            MaintenanceRkdReturnLine.part_id,
        )
    ).all()
    part_units = _part_units(db, {
        part_id for _order, part_id, _qty in demand_rows
    } | {
        part_id for _order, part_id, _qty, _undated in receipt_rows
    })
    split: dict[str, dict[str, dict[str | None, Decimal]]] = {
        raw: {"demand": {}, "received": {}, "undated": {}}
        for raw in order_ids
    }
    for order_id, part_id, qty in demand_rows:
        raw = id_to_raw[order_id]
        unit = part_units.get(part_id) if part_id is not None else None
        bucket = split[raw]["demand"]
        bucket[unit] = bucket.get(unit, _ZERO) + Decimal(qty)
    for source_order_id, part_id, qty, undated in receipt_rows:
        unit = part_units.get(part_id) if part_id is not None else None
        received = split[source_order_id]["received"]
        received[unit] = received.get(unit, _ZERO) + Decimal(qty)
        undated_bucket = split[source_order_id]["undated"]
        undated_bucket[unit] = (
            undated_bucket.get(unit, _ZERO) + Decimal(undated)
        )
    return split


def _unit_reliability(units: set) -> tuple[str, str | None]:
    """单一已证单位 → ("single_unit", unit)；仅待核 → 平面兼容但标注未证。

    仅待核桶（unit=None）数量平面照给（§0 兼容），unit=None +
    unit_reliability="unverified_unit" 说明物理单位未证明（不猜「件」）；
    ≥2 桶 → mixed_units，平面不给跨单位确定值。
    """
    if len(units) == 1:
        only = next(iter(units))
        if only is not None:
            return "single_unit", only
        return "unverified_unit", None
    if not units:
        return "single_unit", None  # 无行：无单位主张，平面兼容 0
    return "mixed_units", None


def build_quantity_checks_payload(
    db: Session,
    scope: set[str] | None,
    *,
    projects: dict[str, MaintenanceProject],
    order_date_from: date | None = None,
    order_date_to: date | None = None,
    part_ids: set[int] | None = None,
    keyword: str | None = None,
) -> dict:
    """T4：整单 N/R/差额/超收，当前累计；拒绝历史截止日（不提供该参数）。

    B-F10：q/PN 关键词只**选择整张需求单**（有效需求行命中即选入），
    选入单仍按原整单全 PN/全期累计 N/R 呈现——不裁局部、不忽略 q。
    B-F08：未关联实收桶对受限但非空范围账号照常返回（授权项目交集）。
    """
    order_ids = _scoped_order_ids(
        db, scope,
        order_date_from=order_date_from, order_date_to=order_date_to,
    )
    if (part_ids is not None or keyword is not None) and order_ids:
        # q 整单选择：命中行的所属单全保留（整单 N/R 不动）；哨兵单排除。
        hit_rows = db.execute(
            select(FMaintenanceOrder.raw_order_id)
            .join(
                FMaintenanceLine,
                FMaintenanceLine.order_id == FMaintenanceOrder.id,
            )
            .where(
                FMaintenanceOrder.raw_order_id.in_(order_ids),
                FMaintenanceLine.is_active.is_(True),
                *_keyword_condition(
                    FMaintenanceLine.part_id,
                    FMaintenanceLine.pn_raw,  # 需求行 part_id 非空，兜底分支不触发
                    part_ids=part_ids, keyword=keyword,
                ),
            )
        ).all()
        hit_order_ids = {row[0] for row in hit_rows}
        order_ids = [
            order_id for order_id in order_ids if order_id in hit_order_ids
        ]
    summaries: dict[str, Any] = {}
    for start in range(0, len(order_ids), MAX_BATCH_SIZE):
        summaries.update(summarize_demand_returns(
            db,
            source_order_ids=order_ids[start:start + MAX_BATCH_SIZE],
            allowed_project_ids=set(projects) if isinstance(scope, SelectBase) else scope,
        ))
    order_meta = {}
    if order_ids:
        for row in db.execute(
            select(
                FMaintenanceOrder.raw_order_id,
                FMaintenanceOrder.order_no,
                FMaintenanceOrder.order_date,
                FMaintenanceOrder.demand_type,
                FMaintenanceOrder.warehouse,
            ).where(FMaintenanceOrder.raw_order_id.in_(order_ids))
        ):
            order_meta[row.raw_order_id] = row

    # 静态审查补充（§104）：整单 N/R/差额/超收按单位分桶——只读适配层，
    # 不改核心 summarize_demand_returns。件/台不合成一个分母（不输出
    # 3件+7台=10 的确定合计）；待核未证明可比 → 平面 null + 原因。
    unit_split = _quantity_check_unit_split(db, order_ids)
    card_units: dict[str | None, dict[str, Decimal]] = {}

    def _card_bucket(unit: str | None) -> dict[str, Decimal]:
        return card_units.setdefault(unit, {
            "demand": _ZERO, "received": _ZERO,
            "pending": _ZERO, "over": _ZERO,
        })

    pending_check = 0
    inactive_orders = 0
    rows = []
    for source_order_id, summary in summaries.items():
        meta = order_meta.get(source_order_id)
        if summary.basis_state == "inactive":
            inactive_orders += 1
        elif summary.basis_state in {"incomplete", "ownership_conflict"}:
            pending_check += 1
        split = unit_split.get(source_order_id) or {
            "demand": {}, "received": {}, "undated": {}
        }
        units = set(split["demand"]) | set(split["received"])
        reliability, row_unit = _unit_reliability(units)
        comparable = reliability != "mixed_units"
        complete = summary.basis_state == "complete"
        # 卡片单位桶（与既有平面合计同门：N=complete 且非 None；R=非冲突；
        # 差额/超收按单位 max 求和，不跨单位抵消）。
        if summary.demand_qty is not None and complete:
            for unit, qty in split["demand"].items():
                _card_bucket(unit)["demand"] += qty
        if summary.received_qty is not None:
            for unit, qty in split["received"].items():
                _card_bucket(unit)["received"] += qty
        breakdown = []
        for unit in sorted_unit_keys(units):
            demand_u = split["demand"].get(unit, _ZERO)
            received_u = split["received"].get(unit, _ZERO)
            pending_u = max(demand_u - received_u, _ZERO)
            over_u = max(received_u - demand_u, _ZERO)
            if complete:
                _card_bucket(unit)["pending"] += pending_u
                _card_bucket(unit)["over"] += over_u
            breakdown.append({
                "unit": unit,
                "demand_qty": _qty(demand_u),
                "received_qty": _qty(received_u),
                "difference_qty": _qty(pending_u) if complete else None,
                "over_received_qty": _qty(over_u) if complete else None,
                "undated_receipt_qty": _qty(
                    split["undated"].get(unit, _ZERO)
                ),
            })
        project = projects.get(summary.project_id) if summary.project_id else None

        def _flat(core_value: Decimal | None) -> str | None:
            # 单一桶（含仅待核）→ 核心口径平面值；混合 → 不给跨单位确定值。
            if not comparable or core_value is None:
                return None
            return _qty(core_value)

        rows.append({
            "source_order_id": source_order_id,
            "order_no": summary.order_no or (meta.order_no if meta else None),
            "project_id": summary.project_id,
            "project_code": project.project_code if project else None,
            "display_name": project.display_name if project else summary.project_id,
            "order_date": (
                meta.order_date.isoformat()
                if meta is not None and meta.order_date is not None else None
            ),
            "demand_type": meta.demand_type if meta is not None else None,
            "warehouse": meta.warehouse if meta is not None else None,
            "demand_qty": _flat(summary.demand_qty),
            "received_qty": _flat(summary.received_qty),
            "difference_qty": _flat(summary.pending_qty),
            "over_received_qty": _flat(summary.over_received_qty),
            "basis_state": summary.basis_state,
            "reason_codes": list(summary.reason_codes),
            "undated_receipt_qty": _flat(summary.undated_receipt_qty),
            "no_return_note": project.no_return_note if project else None,
            "unit": row_unit,
            "unit_reliability": reliability,
            "unit_breakdown": breakdown,
        })
    basis_rank = {"incomplete": 0, "ownership_conflict": 0, "complete": 1, "inactive": 2}
    rows.sort(key=lambda row: (
        basis_rank.get(row["basis_state"], 1),
        -(Decimal(row["difference_qty"]) if row["difference_qty"] else _ZERO),
        row["source_order_id"],
    ))
    unassigned = []
    unassigned_conditions = [
        MaintenanceRkdReturnLine.line_status == "active",
        MaintenanceRkdReturnLine.source_order_id.is_(None),
        # B-F08：受限范围按授权项目交集过滤（空集恒空），不再只认全范围。
        *_scope_condition(MaintenanceRkdReturnLine.project_id, scope),
        *_keyword_condition(
            MaintenanceRkdReturnLine.part_id,
            MaintenanceRkdReturnLine.pn,
            part_ids=part_ids, keyword=keyword,
        ),
    ]
    unassigned_rows = db.execute(
        select(
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.part_id,
            func.sum(MaintenanceRkdReturnLine.qty),
        ).where(*unassigned_conditions)
        .group_by(
            MaintenanceRkdReturnLine.project_id,
            MaintenanceRkdReturnLine.part_id,
        )
    ).all()
    unassigned_part_units = _part_units(db, {
        part_id for _p, part_id, _q in unassigned_rows
        if part_id is not None
    })
    unassigned_by_project: dict[str, QtyBuckets] = defaultdict(
        lambda: QtyBuckets("received_qty")
    )
    for project_id, part_id, qty in unassigned_rows:
        unassigned_by_project[project_id].add(
            unassigned_part_units.get(part_id) if part_id is not None else None,
            received_qty=Decimal(qty),
        )
    # v3：未关联实收同样不跨单位合算——行/卡单一单位平面兼容，混合 → null
    # + 按单位桶（当前累计口径、授权交集、q 过滤均不变）。
    unassigned_total = QtyBuckets("received_qty")
    for project_id in sorted(unassigned_by_project):
        buckets = unassigned_by_project[project_id]
        project = projects.get(project_id)
        unassigned.append({
            "project_id": project_id,
            "project_code": project.project_code if project else None,
            "display_name": (
                project.display_name if project else project_id
            ),
            "received_qty": _flat_qty(buckets.flat("received_qty")),
            "unit_breakdown": buckets.breakdown(
                "received_qty", formatter=_qty_formatter
            ),
        })
        for unit, fields in buckets.items().items():
            unassigned_total.add(unit, received_qty=fields["received_qty"])
    cards_reliability, cards_unit = _unit_reliability(set(card_units))
    cards_flat_comparable = cards_reliability != "mixed_units"

    def _cards_flat(field: str) -> str:
        # 单一桶（含仅待核）→ 合计（兼容原语义）；混合 → 不给确定值。
        total = sum((bucket[field] for bucket in card_units.values()), _ZERO)
        return _qty(total) if cards_flat_comparable else None

    cards = {
        "checked_demand_qty": _cards_flat("demand"),
        "received_qty": _cards_flat("received"),
        "known_difference_qty": _cards_flat("pending"),
        "over_received_qty": _cards_flat("over"),
        "pending_check_order_count": pending_check,
        "inactive_order_count": inactive_orders,
        "unassigned_receipt_qty": _flat_qty(
            unassigned_total.flat("received_qty")
        ),
        "unassigned_receipt_unit_breakdown": unassigned_total.breakdown(
            "received_qty", formatter=_qty_formatter
        ),
        "unit": cards_unit,
        "unit_reliability": cards_reliability,
        "unit_breakdown": [
            {
                "unit": unit,
                "checked_demand_qty": _qty(card_units[unit]["demand"]),
                "received_qty": _qty(card_units[unit]["received"]),
                "known_difference_qty": _qty(card_units[unit]["pending"]),
                "over_received_qty": _qty(card_units[unit]["over"]),
            }
            for unit in sorted_unit_keys(card_units.keys())
        ],
        "note": "当前累计（整单跨全部 PN/来源/件况）；不发布返还状态",
    }
    return {"cards": cards, "rows": rows, "unassigned_receipts": unassigned}


# ---------- 排序 / 分页 ----------

def sort_project_rows(rows: list[dict], sort: str) -> list[dict]:
    if sort not in PROJECT_SORTS:
        raise DashboardInputError("invalid_sort")
    if sort == "bad_value_desc":
        return sorted(rows, key=lambda row: (
            -Decimal(row.get("valuation", {}).get("bad_estimated_value_inc_tax", "0")
                     or "0"),
            -Decimal(row["bad_receipt_qty"] or "0"),  # 混合单位 null 按 0 权重
            row["project_id"],
        ))
    if sort == "project_code_asc":
        return sorted(rows, key=lambda row: (
            row["project_code"] or "", row["project_id"]
        ))
    key = {
        "bad_receipt_qty_desc": "bad_receipt_qty",
        "receipt_qty_desc": "receipt_qty",
        "issued_qty_desc": "issued_qty",
    }[sort]
    return sorted(rows, key=lambda row: (
        -Decimal(row[key] or "0"), row["project_id"],
    ))


def sort_part_rows(rows: list[dict], sort: str) -> list[dict]:
    if sort not in PART_SORTS:
        raise DashboardInputError("invalid_sort")
    if sort == "bad_value_desc":
        return sorted(rows, key=lambda row: (
            -Decimal(row.get("valuation", {}).get("bad_estimated_value_inc_tax", "0")
                     or "0"),
            -Decimal(row["bad_receipt_qty"]),
            row["pn"].upper(),
        ))
    key = {
        "bad_receipt_qty_desc": "bad_receipt_qty",
        "receipt_qty_desc": "receipt_qty",
    }[sort]
    return sorted(rows, key=lambda row: (-Decimal(row[key]), row["pn"].upper()))


def sort_receipt_rows(rows: list[dict], sort: str) -> list[dict]:
    if sort not in RECEIPT_SORTS:
        raise DashboardInputError("invalid_sort")
    if sort == "qty_desc":
        return sorted(rows, key=lambda row: (
            -Decimal(row["qty"]), row["occurred_at"] or "", row["rkd_line_id"],
        ))
    if sort == "pn_asc":
        return sorted(rows, key=lambda row: (row["pn"].upper(), row["rkd_line_id"]))
    return rows  # occurred_desc：SQL 已按业务日倒序


def sort_check_rows(rows: list[dict], sort: str) -> list[dict]:
    if sort not in CHECK_SORTS:
        raise DashboardInputError("invalid_sort")
    if sort == "over_received_desc":
        return sorted(rows, key=lambda row: (
            row["basis_state"] in {"complete"} and 1 or 0,
            -(Decimal(row["over_received_qty"] or "0")),
            row["source_order_id"],
        ))
    if sort == "demand_qty_desc":
        return sorted(rows, key=lambda row: (
            row["basis_state"] == "complete" and 1 or 0,
            -(Decimal(row["demand_qty"] or "0")),
            row["source_order_id"],
        ))
    return rows  # difference_desc：默认序（未知基数置顶 + 差额降序）


def paginate(rows: list[dict], page: int, page_size: int) -> dict:
    total = len(rows)
    start = (page - 1) * page_size
    return {
        "rows": rows[start:start + page_size],
        "pagination": {
            "page": page, "page_size": page_size, "total": total,
        },
    }
