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

from sqlalchemy import Date, and_, false, func, or_, select
from sqlalchemy.orm import Session

from app.business_time import BUSINESS_TZ, business_today
from app.config import Settings, get_settings
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.security import UserContext
from app.services import maintenance_dashboard_valuation_policy as policy
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
    db: Session, scope: set[str] | None
) -> dict[str, MaintenanceProject]:
    statement = select(MaintenanceProject)
    if scope is not None:
        if not scope:
            return {}
        statement = statement.where(MaintenanceProject.project_id.in_(scope))
    statement = statement.order_by(
        MaintenanceProject.project_code.nulls_last(),
        MaintenanceProject.project_id,
    )
    return {project.project_id: project for project in db.scalars(statement)}


def _part_keyword(db: Session, q: str | None) -> tuple[set[int] | None, str | None]:
    """PN/描述关键词：主数据身份命中集合 + 原文兜底 LIKE 关键词。"""
    text = (q or "").strip()
    if not text:
        return None, None
    keyword = f"%{text}%"
    ids = set(
        db.scalars(
            select(DimPart.id).where(or_(
                DimPart.pn_std.ilike(keyword, autoescape=True),
                DimPart.description.ilike(keyword, autoescape=True),
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
) -> _Filters:
    """先范围收敛（项目 × 业务类型 × 关键词），后聚合——一切统计共用。"""
    projects = _project_rows(db, scope)
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


def _scope_condition(column, scope: set[str] | None) -> list:
    if scope is None:
        return []
    if not scope:
        # 空集=无可见项目：恒假条件，绝不回退全量。
        return [false()]
    return [column.in_(scope)]


def _keyword_condition(part_col, pn_col, filters: _Filters) -> list:
    if filters.part_ids is None and filters.keyword is None:
        return []
    clauses = []
    if filters.part_ids is not None:
        clauses.append(part_col.in_(filters.part_ids) if filters.part_ids
                       else false())
    if filters.keyword:
        # 未解析身份的行只按原始 PN 文本兜底；已解析行以主数据身份过滤。
        clauses.append(and_(
            part_col.is_(None), pn_col.ilike(filters.keyword, autoescape=True)
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


def receipt_facts(db: Session, filters: _Filters) -> list[ReceiptFact]:
    """期间内有效实收（有业务日），按项目×身份×件况聚合；退货不参与。"""
    lower, upper = _period_bounds(filters.start, filters.end)
    conditions = [
        MaintenanceRkdReturnLine.line_status == "active",
        MaintenanceRkdReturnLine.occurred_at.is_not(None),
        MaintenanceRkdReturnLine.occurred_at >= lower,
        MaintenanceRkdReturnLine.occurred_at < upper,
        *_scope_condition(MaintenanceRkdReturnLine.project_id, filters.scope),
        *_keyword_condition(
            MaintenanceRkdReturnLine.part_id,
            MaintenanceRkdReturnLine.pn,
            filters,
        ),
    ]
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
    return [
        ReceiptFact(
            project_id=project_id, part_id=part_id, pn=pn, description=description,
            test_result=test_result, receipt_kind=receipt_kind, source=source,
            qty=Decimal(qty),
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
                filters,
            ),
        )
        .group_by(
            MaintenanceSiteIssue.project_id,
            MaintenanceSiteIssueLine.part_id,
            MaintenanceSiteIssueLine.pn,
        )
    ).all()
    return [
        IssueFact(project_id=project_id, part_id=part_id, pn=pn, qty=Decimal(qty))
        for project_id, part_id, pn, qty in rows
    ]


def undated_receipts_by_project(db: Session, filters: _Filters) -> dict[str, Decimal]:
    """当前有效实收中无业务日期的量（当前累计口径，不入期间）。"""
    conditions = [
        MaintenanceRkdReturnLine.line_status == "active",
        MaintenanceRkdReturnLine.occurred_at.is_(None),
        *_scope_condition(MaintenanceRkdReturnLine.project_id, filters.scope),
    ]
    return {
        project_id: Decimal(qty)
        for project_id, qty in db.execute(
            select(
                MaintenanceRkdReturnLine.project_id,
                func.sum(MaintenanceRkdReturnLine.qty),
            ).where(*conditions).group_by(MaintenanceRkdReturnLine.project_id)
        ).all()
    }


def unlinked_receipts_by_project_in_period(
    db: Session, filters: _Filters
) -> dict[str, Decimal]:
    """期间内未关联需求单的有效实收（M10）：项目已定，不抵任何需求单。"""
    lower, upper = _period_bounds(filters.start, filters.end)
    conditions = [
        MaintenanceRkdReturnLine.line_status == "active",
        MaintenanceRkdReturnLine.occurred_at.is_not(None),
        MaintenanceRkdReturnLine.occurred_at >= lower,
        MaintenanceRkdReturnLine.occurred_at < upper,
        MaintenanceRkdReturnLine.source_order_id.is_(None),
        *_scope_condition(MaintenanceRkdReturnLine.project_id, filters.scope),
    ]
    return {
        project_id: Decimal(qty)
        for project_id, qty in db.execute(
            select(
                MaintenanceRkdReturnLine.project_id,
                func.sum(MaintenanceRkdReturnLine.qty),
            ).where(*conditions).group_by(MaintenanceRkdReturnLine.project_id)
        ).all()
    }


def daily_trend(db: Session, filters: _Filters) -> list[dict]:
    """每日实收与实际领用（各按自身业务日）；有界期间按日补零。"""
    lower, upper = _period_bounds(filters.start, filters.end)
    receipt_conditions = [
        MaintenanceRkdReturnLine.line_status == "active",
        MaintenanceRkdReturnLine.occurred_at.is_not(None),
        MaintenanceRkdReturnLine.occurred_at >= lower,
        MaintenanceRkdReturnLine.occurred_at < upper,
        *_scope_condition(MaintenanceRkdReturnLine.project_id, filters.scope),
    ]
    sh_receipt_day = func.cast(
        func.timezone(BUSINESS_TZ.key, MaintenanceRkdReturnLine.occurred_at), Date
    )
    receipts = {
        day: Decimal(qty)
        for day, qty in db.execute(
            select(sh_receipt_day, func.sum(MaintenanceRkdReturnLine.qty))
            .where(*receipt_conditions)
            .group_by(sh_receipt_day)
        ).all()
    }
    issues = {
        day: Decimal(qty)
        for day, qty in db.execute(
            select(
                MaintenanceSiteIssue.issue_date,
                func.sum(MaintenanceSiteIssueLine.quantity),
            )
            .join(
                MaintenanceSiteIssue,
                MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
            )
            .where(*_issue_validity_conditions(filters))
            .group_by(MaintenanceSiteIssue.issue_date)
        ).all()
    }
    days = sorted(set(receipts) | set(issues))
    if not days:
        return []
    cursor = filters.start or min(days)
    filled = []
    while cursor <= filters.end:
        filled.append({
            "date": cursor.isoformat(),
            "receipt_qty": _qty(receipts.get(cursor, _ZERO)),
            "issued_qty": _qty(issues.get(cursor, _ZERO)),
        })
        cursor += timedelta(days=1)
    return filled


def exempt_and_pending_qty(
    db: Session, filters: _Filters
) -> dict[str, dict[str, Decimal]]:
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
    grouped: dict[str, list[MaintenanceSiteIssueLine]] = defaultdict(list)
    for (project_id, line_id, part_id, pn, quantity,
         no_return, delivery_line_id, is_active) in rows:
        grouped[project_id].append(MaintenanceSiteIssueLine(
            issue_line_id=line_id, part_id=part_id, pn=pn, quantity=quantity,
            no_return=no_return, delivery_line_id=delivery_line_id,
            is_active=is_active,
        ))
    result: dict[str, dict[str, Decimal]] = {}
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
        result[project_id] = {"exempt_qty": exempt, "pending_qty": pending}
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
    pending_reasons: dict[str, Decimal] = field(default_factory=dict)

    def add_pending(self, qty: Decimal, reason: str) -> None:
        self.pending_qty += qty
        self.pending_reasons[reason] = (
            self.pending_reasons.get(reason, _ZERO) + qty
        )


@dataclass(slots=True)
class ValuationBundle:
    per_part: dict[int, _PartValuation] = field(default_factory=dict)
    project_bad_value_inc: dict[str, Decimal] = field(default_factory=dict)
    project_bad_value_ex: dict[str, Decimal] = field(default_factory=dict)
    project_bad_estimated_qty: dict[str, Decimal] = field(default_factory=dict)
    project_pending_bad_qty: dict[str, Decimal] = field(default_factory=dict)
    line_values: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_bad_inc: Decimal = _ZERO
    total_bad_ex: Decimal = _ZERO
    total_finished_inc: Decimal = _ZERO
    total_finished_ex: Decimal = _ZERO
    estimated_bad_qty: Decimal = _ZERO
    pending_bad_qty: Decimal = _ZERO
    references: dict[int, Any] = field(default_factory=dict)


def build_valuation(
    db: Session,
    filters: _Filters,
    *,
    settings: Settings | None = None,
) -> ValuationBundle:
    """对期间内有系数件况（坏品类/成品）的实收行做批量估价。

    每行 V = qty × 选用参考价 × 系数（HALF_UP 两位）后求和；待估行记原因与
    数量，绝不算 0。覆盖率由调用方按 坏件实收（M02）计算。
    """
    settings = settings or get_settings()
    bundle = ValuationBundle()
    lower, upper = _period_bounds(filters.start, filters.end)
    conditions = [
        MaintenanceRkdReturnLine.line_status == "active",
        MaintenanceRkdReturnLine.occurred_at.is_not(None),
        MaintenanceRkdReturnLine.occurred_at >= lower,
        MaintenanceRkdReturnLine.occurred_at < upper,
        *_scope_condition(MaintenanceRkdReturnLine.project_id, filters.scope),
        *_keyword_condition(
            MaintenanceRkdReturnLine.part_id,
            MaintenanceRkdReturnLine.pn,
            filters,
        ),
    ]
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
        db, part_ids, anchor=filters.end, settings=settings
    )
    for row in lines:
        factor = policy.factor_for(row.test_result)
        is_bad = policy.is_bad_condition(row.test_result)
        part_id = identity.get(row.rkd_line_id)
        references = bundle.references.get(part_id) if part_id is not None else None
        if factor is None:
            reason = "unknown_condition"
        elif part_id is None:
            reason = "pn_unresolved"
        elif references is None or references.price is None:
            reason = references.chosen_reason if references is not None else "pn_unresolved"
        else:
            reason = None
        if reason is not None:
            bundle.line_values[row.rkd_line_id] = {
                "state": "pending", "reason": reason,
            }
            if is_bad:
                agg = bundle.per_part.setdefault(part_id or -1, _PartValuation())
                agg.add_pending(Decimal(row.qty), reason)
                bundle.pending_bad_qty += Decimal(row.qty)
                bundle.project_pending_bad_qty[row.project_id] = (
                    bundle.project_pending_bad_qty.get(row.project_id, _ZERO)
                    + Decimal(row.qty)
                )
            continue
        price = references.price
        value_inc, value_ex = value_line(Decimal(row.qty), factor, price)
        bundle.line_values[row.rkd_line_id] = {
            "state": "ready",
            "chosen": references.chosen,
            "factor": format(factor, "f"),
            "price_inc_tax": _amount(price[0]),
            "price_ex_tax": _amount(price[1]),
            "value_inc_tax": _amount(value_inc),
            "value_ex_tax": _amount(value_ex),
        }
        agg = bundle.per_part.setdefault(part_id, _PartValuation())
        if is_bad:
            agg.bad_estimated_qty += Decimal(row.qty)
            agg.bad_value_inc += value_inc
            agg.bad_value_ex += value_ex
            bundle.total_bad_inc += value_inc
            bundle.total_bad_ex += value_ex
            bundle.estimated_bad_qty += Decimal(row.qty)
            bundle.project_bad_value_inc[row.project_id] = (
                bundle.project_bad_value_inc.get(row.project_id, _ZERO) + value_inc
            )
            bundle.project_bad_value_ex[row.project_id] = (
                bundle.project_bad_value_ex.get(row.project_id, _ZERO) + value_ex
            )
            bundle.project_bad_estimated_qty[row.project_id] = (
                bundle.project_bad_estimated_qty.get(row.project_id, _ZERO)
                + Decimal(row.qty)
            )
        else:
            agg.finished_value_inc += value_inc
            agg.finished_value_ex += value_ex
            bundle.total_finished_inc += value_inc
            bundle.total_finished_ex += value_ex
    return bundle


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

def build_summary_payload(
    db: Session,
    filters: _Filters,
    *,
    include_amounts: bool,
    settings: Settings | None = None,
) -> dict:
    receipts = receipt_facts(db, filters)
    issues = issue_facts(db, filters)
    receipt_qty = sum((fact.qty for fact in receipts), _ZERO)
    bad_qty = sum(
        (fact.qty for fact in receipts if policy.is_bad_condition(fact.test_result)),
        _ZERO,
    )
    scrap_qty = sum(
        (fact.qty for fact in receipts
         if (fact.test_result or "").strip() == "废品"),
        _ZERO,
    )
    part_qty = sum(
        (fact.qty for fact in receipts if fact.receipt_kind != "machine"), _ZERO
    )
    machine_qty = sum(
        (fact.qty for fact in receipts if fact.receipt_kind == "machine"), _ZERO
    )
    issued_qty = sum((fact.qty for fact in issues), _ZERO)
    projects_with_facts = {f.project_id for f in receipts} | {
        f.project_id for f in issues
    }
    undated = undated_receipts_by_project(db, filters)
    condition_totals: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for fact in receipts:
        key = (fact.test_result or "").strip() or "未填"
        condition_totals[key] += fact.qty

    cards: dict[str, Any] = {
        "receipt_qty": {
            "total": _qty(receipt_qty),
            "part": _qty(part_qty),
            "machine": _qty(machine_qty),
        },
        "bad_receipt_qty": {"total": _qty(bad_qty), "scrap": _qty(scrap_qty)},
        "issued_qty": {
            "total": _qty(issued_qty),
            "note": "实际领用量（未采集件况）",
        },
        "return_rate_pct": rate_pct(receipt_qty, issued_qty),
        "rate_numerator": _qty(receipt_qty),
        "rate_denominator": _qty(issued_qty),
        "project_count": len(projects_with_facts),
    }
    if include_amounts:
        bundle = build_valuation(db, filters, settings=settings)
        cards["valuation"] = {
            "state": "ready" if bundle.estimated_bad_qty > 0 else "pending",
            "bad_estimated_value": {
                "inc_tax": _amount(bundle.total_bad_inc),
                "ex_tax": _amount(bundle.total_bad_ex),
            },
            "finished_estimated_value": {
                "inc_tax": _amount(bundle.total_finished_inc),
                "ex_tax": _amount(bundle.total_finished_ex),
            },
            "estimated_bad_qty": _qty(bundle.estimated_bad_qty),
            "pending_bad_qty": _qty(bundle.pending_bad_qty),
            "coverage_pct": _coverage_pct(bundle.estimated_bad_qty, bad_qty),
            "policy_version": policy.POLICY_VERSION,
            "note": policy.NOTES,
        }

    receipt_by_project: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    bad_by_project: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    issued_by_project: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for fact in receipts:
        receipt_by_project[fact.project_id] += fact.qty
        if policy.is_bad_condition(fact.test_result):
            bad_by_project[fact.project_id] += fact.qty
    for fact in issues:
        issued_by_project[fact.project_id] += fact.qty
    top_projects = sorted(
        receipt_by_project.items(), key=lambda item: (-item[1], item[0])
    )[:10]

    notes = []
    undated_total = sum(undated.values(), _ZERO)
    if undated_total > 0:
        notes.append(
            f"另有 {_qty(undated_total)} 件无业务日期实收（当前累计，未计入期间）"
        )
    return {
        "range": {
            "start": filters.start.isoformat() if filters.start else None,
            "end": filters.end.isoformat(),
        },
        "cards": cards,
        "trend": daily_trend(db, filters),
        "condition_structure": [
            {"test_result": key, "qty": _qty(value)}
            for key, value in sorted(condition_totals.items(), key=lambda kv: -kv[1])
        ],
        "top_projects": [
            {
                "project_id": project_id,
                "project_code": _project_field(filters, project_id, "project_code"),
                "display_name": _project_field(
                    filters, project_id, "display_name"
                ) or project_id,
                "receipt_qty": _qty(qty),
                "bad_receipt_qty": _qty(bad_by_project.get(project_id, _ZERO)),
                "issued_qty": _qty(issued_by_project.get(project_id, _ZERO)),
            }
            for project_id, qty in top_projects
        ],
        "notes": notes,
    }


def _project_field(
    filters: _Filters, project_id: str, attribute: str
) -> Any:
    project = filters.projects.get(project_id)
    return getattr(project, attribute, None)


# ---------- T2 项目对比 ----------

def build_project_rows(
    db: Session,
    filters: _Filters,
    *,
    include_amounts: bool,
    settings: Settings | None = None,
) -> list[dict]:
    receipts = receipt_facts(db, filters)
    issues = issue_facts(db, filters)
    unlinked = unlinked_receipts_by_project_in_period(db, filters)
    exempt = exempt_and_pending_qty(db, filters)

    receipt_by_project: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    bad_by_project: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    scrap_by_project: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for fact in receipts:
        receipt_by_project[fact.project_id] += fact.qty
        if policy.is_bad_condition(fact.test_result):
            bad_by_project[fact.project_id] += fact.qty
            if (fact.test_result or "").strip() == "废品":
                scrap_by_project[fact.project_id] += fact.qty
    issued_by_project: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for fact in issues:
        issued_by_project[fact.project_id] += fact.qty

    bundle = build_valuation(db, filters, settings=settings) if include_amounts else None
    rows = []
    for project_id, project in filters.projects.items():
        issued = issued_by_project.get(project_id, _ZERO)
        received = receipt_by_project.get(project_id, _ZERO)
        bad = bad_by_project.get(project_id, _ZERO)
        row: dict[str, Any] = {
            "project_id": project_id,
            "project_code": project.project_code,
            "display_name": project.display_name,
            "business_type": project.business_type,
            "receipt_qty": _qty(received),
            "bad_receipt_qty": _qty(bad),
            "scrap_receipt_qty": _qty(scrap_by_project.get(project_id, _ZERO)),
            "issued_qty": _qty(issued),
            "exempt_issued_qty": _qty(
                exempt.get(project_id, {}).get("exempt_qty", _ZERO)
            ),
            "pending_requirement_qty": _qty(
                exempt.get(project_id, {}).get("pending_qty", _ZERO)
            ),
            "unlinked_receipt_qty": _qty(unlinked.get(project_id, _ZERO)),
            "return_rate_pct": rate_pct(received, issued),
            "rate_numerator": _qty(received),
            "rate_denominator": _qty(issued),
            "no_return_note": project.no_return_note,
        }
        if include_amounts and bundle is not None:
            estimated = bundle.project_bad_estimated_qty.get(project_id, _ZERO)
            pending_qty = bundle.project_pending_bad_qty.get(project_id, _ZERO)
            row["valuation"] = {
                "bad_estimated_value_inc_tax": _amount(
                    bundle.project_bad_value_inc.get(project_id, _ZERO)
                ),
                "bad_estimated_value_ex_tax": _amount(
                    bundle.project_bad_value_ex.get(project_id, _ZERO)
                ),
                "pending_bad_qty": _qty(pending_qty),
                "coverage_pct": _coverage_pct(estimated, bad),
            }
        rows.append(row)
    return rows


# ---------- T3 PN 排名与估价 ----------

def build_part_rows(
    db: Session,
    filters: _Filters,
    *,
    include_amounts: bool,
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
    bundle = build_valuation(db, filters, settings=settings) if include_amounts else None
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
            row["valuation"] = {
                "bad_estimated_value_inc_tax": _amount(agg.bad_value_inc),
                "bad_estimated_value_ex_tax": _amount(agg.bad_value_ex),
                "estimated_bad_qty": _qty(agg.bad_estimated_qty),
                "pending_bad_qty": _qty(agg.pending_qty),
                "pending_reasons": {
                    reason: _qty(qty)
                    for reason, qty in sorted(agg.pending_reasons.items())
                },
                "coverage_pct": _coverage_pct(agg.bad_estimated_qty, bucket.bad_qty),
            }
            row["references"] = _references_payload(references)
        rows.append(row)
    return rows


def _references_payload(references) -> dict:
    """双参考并列展示（V）；purchase 优先、sales 回退的选择结果。"""
    if references is None:
        return {"purchase": {"available": False,
                             "unavailable_reason": "no_samples"},
                "sales": {"available": False, "unavailable_reason": "no_samples"},
                "chosen": None, "chosen_reason": "no_samples"}
    return {
        "purchase": references.purchase.payload(),
        "sales": references.sales.payload(),
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
    settings: Settings | None = None,
) -> tuple[list[dict], int]:
    """行级实收明细（完整筛选集先取全量再分页；金额列仅 V）。"""
    lower, upper = _period_bounds(filters.start, filters.end)
    conditions = [
        MaintenanceRkdReturnLine.line_status == "active",
        MaintenanceRkdReturnLine.occurred_at.is_not(None),
        MaintenanceRkdReturnLine.occurred_at >= lower,
        MaintenanceRkdReturnLine.occurred_at < upper,
        *_scope_condition(MaintenanceRkdReturnLine.project_id, filters.scope),
        *_keyword_condition(
            MaintenanceRkdReturnLine.part_id,
            MaintenanceRkdReturnLine.pn,
            filters,
        ),
    ]
    if test_result is not None:
        conditions.append(MaintenanceRkdReturnLine.test_result == test_result)
    if source is not None:
        conditions.append(MaintenanceRkdReturnLine.source == source)
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
    bundle = build_valuation(db, filters, settings=settings) if include_amounts else None
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
        if not scope:
            return []
        statement = statement.where(
            MaintenanceSourceOrderAssignment.project_id.in_(scope)
        )
    if order_date_from is not None:
        statement = statement.where(FMaintenanceOrder.order_date >= order_date_from)
    if order_date_to is not None:
        statement = statement.where(FMaintenanceOrder.order_date <= order_date_to)
    return list(db.scalars(statement.order_by(FMaintenanceOrder.raw_order_id)))


def build_quantity_checks_payload(
    db: Session,
    scope: set[str] | None,
    *,
    projects: dict[str, MaintenanceProject],
    order_date_from: date | None = None,
    order_date_to: date | None = None,
) -> dict:
    """T4：整单 N/R/差额/超收，当前累计；拒绝历史截止日（不提供该参数）。"""
    order_ids = _scoped_order_ids(
        db, scope,
        order_date_from=order_date_from, order_date_to=order_date_to,
    )
    summaries: dict[str, Any] = {}
    for start in range(0, len(order_ids), MAX_BATCH_SIZE):
        summaries.update(summarize_demand_returns(
            db,
            source_order_ids=order_ids[start:start + MAX_BATCH_SIZE],
            allowed_project_ids=scope,
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

    demand_total = _ZERO
    received_total = _ZERO
    difference_total = _ZERO
    over_total = _ZERO
    pending_check = 0
    inactive_orders = 0
    rows = []
    for source_order_id, summary in summaries.items():
        meta = order_meta.get(source_order_id)
        if summary.basis_state == "inactive":
            inactive_orders += 1
        elif summary.basis_state in {"incomplete", "ownership_conflict"}:
            pending_check += 1
        if summary.demand_qty is not None and summary.basis_state == "complete":
            demand_total += summary.demand_qty
        if summary.received_qty is not None and summary.basis_state != "ownership_conflict":
            received_total += summary.received_qty
        if summary.pending_qty is not None:
            difference_total += summary.pending_qty
        if summary.over_received_qty is not None:
            over_total += summary.over_received_qty
        project = projects.get(summary.project_id) if summary.project_id else None
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
            "demand_qty": (
                _qty(summary.demand_qty) if summary.demand_qty is not None else None
            ),
            "received_qty": (
                _qty(summary.received_qty)
                if summary.received_qty is not None else None
            ),
            "difference_qty": (
                _qty(summary.pending_qty) if summary.pending_qty is not None else None
            ),
            "over_received_qty": (
                _qty(summary.over_received_qty)
                if summary.over_received_qty is not None else None
            ),
            "basis_state": summary.basis_state,
            "reason_codes": list(summary.reason_codes),
            "undated_receipt_qty": (
                _qty(summary.undated_receipt_qty)
                if summary.undated_receipt_qty is not None else None
            ),
            "no_return_note": project.no_return_note if project else None,
        })
    basis_rank = {"incomplete": 0, "ownership_conflict": 0, "complete": 1, "inactive": 2}
    rows.sort(key=lambda row: (
        basis_rank.get(row["basis_state"], 1),
        -(Decimal(row["difference_qty"]) if row["difference_qty"] else _ZERO),
        row["source_order_id"],
    ))
    unassigned = []
    if scope is None:
        unassigned_conditions = [
            MaintenanceRkdReturnLine.line_status == "active",
            MaintenanceRkdReturnLine.source_order_id.is_(None),
        ]
        for project_id, qty in sorted(db.execute(
            select(
                MaintenanceRkdReturnLine.project_id,
                func.sum(MaintenanceRkdReturnLine.qty),
            ).where(*unassigned_conditions)
            .group_by(MaintenanceRkdReturnLine.project_id)
        ).all()):
            project = projects.get(project_id)
            unassigned.append({
                "project_id": project_id,
                "project_code": project.project_code if project else None,
                "display_name": (
                    project.display_name if project else project_id
                ),
                "received_qty": _qty(Decimal(qty)),
            })
    cards = {
        "checked_demand_qty": _qty(demand_total),
        "received_qty": _qty(received_total),
        "known_difference_qty": _qty(difference_total),
        "over_received_qty": _qty(over_total),
        "pending_check_order_count": pending_check,
        "inactive_order_count": inactive_orders,
        "unassigned_receipt_qty": _qty(sum(
            (Decimal(row["received_qty"]) for row in unassigned), _ZERO
        )),
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
            -Decimal(row["bad_receipt_qty"]),
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
    return sorted(rows, key=lambda row: (-Decimal(row[key]), row["project_id"]))


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
