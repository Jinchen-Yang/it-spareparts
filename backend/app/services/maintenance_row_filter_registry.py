"""D-33 行级查询白名单；表达式附加到调用方集合，不重定义其有效性与范围。"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import aliased

from app.models.dimensions import DimPart
from app.models.inventory import PartPool, PartPoolMember
from app.models.maintenance import (
    FMaintenanceLine as Line, FMaintenanceOrder as Order, MaintenanceManualCostOverride as Override,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment as Assignment
from app.services import maintenance_cost_quality as quality


OPERATORS = {
    "text": ("contains", "eq", "prefix", "is_empty"),
    "enum": ("in", "is_empty"),
    "number": ("eq", "range", "is_empty"),
    "date": ("eq", "range", "is_empty"),
}


@dataclass(frozen=True)
class RowField:
    field: str
    label: str
    type: str
    expression: Any
    amount: bool = False
    options: tuple[str, ...] = ()

    def metadata(self):
        return {
            "field": self.field, "label": self.label, "type": self.type,
            "operators": OPERATORS[self.type],
            "options": [{"value": value, "label": value} for value in self.options],
        }


FIELDS = {
    item.field: item for item in (
        RowField("order_no", "维保单号", "text", Order.order_no),
        RowField("order_date", "制单日期", "date", Order.order_date),
        RowField("pn_std", "PN", "text", func.coalesce(func.nullif(Line.pn_std, ""), Line.pn_raw)),
        RowField("description", "描述", "text", Line.description),
        RowField("qty", "需求数量", "number", Line.qty),
        RowField("return_qty", "退货数量", "number", Line.return_qty),
    )
}


_row_manual = aliased(Override, name="row_filter_manual")


def _manual(column):
    # apply_row_filters adds this one-to-one active override join once, including
    # an empty filter list. Every projected cost expression shares that lookup.
    return getattr(_row_manual, column.key)


_manual_active = _manual(Override.active)
_manual_ex, _manual_inc = _manual(Override.unit_cost_ex_tax), _manual(Override.unit_cost_inc_tax)
_project_name = select(MaintenanceProject.display_name).join(
    Assignment, Assignment.project_id == MaintenanceProject.project_id,
).where(
    Assignment.source_order_id == Order.raw_order_id, Assignment.is_active.is_(True),
).correlate(Order).scalar_subquery()


def _round_cent(amount):
    """Match Decimal.quantize(.01): PostgreSQL round alone rounds ties away."""
    return case(
        (
            func.mod(func.abs(amount * 100), 2) == Decimal("0.5"),
            func.trunc(amount, 2),
        ),
        else_=func.round(amount, 2),
    )


def _pool(column):
    """SQL twin of boss_facts.pool_membership, including both merged PN paths."""
    return (
        select(column)
        .select_from(DimPart)
        .join(PartPoolMember, or_(
            PartPoolMember.part_id == DimPart.id,
            and_(DimPart.status == "merged",
                 PartPoolMember.part_id == DimPart.merged_into_id),
        ))
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .where(DimPart.pn_std == Line.pn_std)
        .order_by(
            case((PartPool.status == "active", 0), else_=1),
            PartPool.updated_at.desc().nullslast(),
        )
        .limit(1)
        .correlate(Line)
        .scalar_subquery()
    )


def _effective(basis, manual_unit):
    amount, actual, estimated, missing = quality.sql_normalized_line_cost(
        source_column=Line.cost_source, tax_basis_column=Line.cost_tax_basis,
        legacy_amount_column=Line.cost_amount,
        normalized_amount_column=getattr(Line, f"cost_amount_{basis}_tax"),
        normalized_basis=basis, anomaly_flags_column=Line.anomaly_flags,
        qty_column=Line.qty, return_qty_column=Line.return_qty,
        manual_unit_cost_column=manual_unit, manual_active_column=_manual_active,
    )
    unit = case(
        (amount.is_(None), None),
        (and_(Line.cost_source == "manual", _manual_active.is_(True)), manual_unit),
        (or_(Line.cost_source.is_(None), Line.cost_source == "none"), manual_unit),
        else_=getattr(Line, f"unit_cost_{basis}_tax"),
    )
    # Keep the unrounded presence test: using the rounded expression in every
    # source/tier/reference CASE would repeatedly expand the same cost query.
    return _round_cent(amount), unit, actual, estimated, missing, amount.is_not(None)


_inc, _ex = _effective("inc", _manual_inc), _effective("ex", _manual_ex)
_fallback = and_(
    or_(Line.cost_source.is_(None), Line.cost_source == "none"),
    _inc[5], _ex[5],
)
_source = case(
    (and_(or_(Line.cost_source.is_(None), Line.cost_source == "none"),
          _inc[5]), "manual"),
    else_=func.coalesce(Line.cost_source, "none"),
)
_missing_kind = case(
    (_inc[5], None), (Line.cost_source.is_(None), "out_of_scope"),
    (Line.cost_source == "none", "none"), else_="invalid_cost_fact",
)
_source_options = ("direct", "window", "month_avg", "trace_avg", "sales_ref",
                   "pool_purchase", "pool_sales", "purchase_history", "sales_history", "manual", "none")

# The old /lines DTO uses resolved_line_cost_fields, whereas project/global
# rows use master._line_cost_evidence. Keep both established projections:
# only a complete dual-tax fallback can replace the old legacy cost evidence.
_legacy_actual, _legacy_estimated, _legacy_missing = quality.sql_tier_predicates(
    Line.cost_source, Line.cost_tax_basis, Line.cost_amount,
)
_legacy_known = or_(_fallback, _legacy_actual, _legacy_estimated)
_legacy_tier = case(
    (or_(_fallback, _legacy_actual), "actual"),
    (_legacy_estimated, "estimated"),
    else_="missing",
)
_resolved_flags = Line.anomaly_flags
for _flag in sorted(quality.COST_DERIVED_ANOMALY_FLAGS):
    _resolved_flags = func.array_remove(_resolved_flags, _flag)
_resolved_flags = case((_fallback, _resolved_flags), else_=Line.anomaly_flags)
_legacy_expressions = {
    "pn_std": Line.pn_std,
    "cost_source": case((_fallback, "manual"), else_=Line.cost_source),
    "confidence": case((_fallback, "high"), else_=Line.confidence),
}
for _basis, _fact, _unit in (("inc", _inc, _manual_inc), ("ex", _ex, _manual_ex)):
    _legacy_expressions[f"effective_unit_cost_{_basis}_tax"] = case(
        (_fallback, _round_cent(_unit)),
        (and_(_legacy_known, _fact[5]),
         getattr(Line, f"unit_cost_{_basis}_tax")),
        else_=None,
    )
    _legacy_expressions[f"effective_cost_amount_{_basis}_tax"] = case(
        (_legacy_known, _fact[0]), else_=None,
    )
_legacy_expressions["known_apply_cost_inc_tax"] = (
    _legacy_expressions["effective_cost_amount_inc_tax"]
)
_legacy_expressions["effective_cost_tier"] = case(
    (and_(_legacy_known, _inc[2]), "actual"),
    (and_(_legacy_known, _inc[3]), "estimated"),
    else_="missing",
)


def _register(name, label, type, expression, *, amount=False, options=()):
    FIELDS[name] = RowField(name, label, type, expression, amount, options)


for name, label, expression in (
    ("project_name", "项目名称", _project_name),
    ("project_raw", "原项目名", Order.project_raw),
    ("sales_order_no", "合同编号", Order.linked_sales_order_no),
    ("salesperson", "销售人员", Order.salesperson),
    ("warehouse", "出库仓库", Order.warehouse),
    ("serial_numbers", "SN", Line.serial_numbers),
    ("line_note", "备注", Line.line_note),
):
    _register(name, label, "text", expression)
for name, label, options in (
    ("demand_type", "需求类型", ("报修供货", "补库供货")),
    ("business_type", "业务类型", ("备件维保", "整体维保", "算力运维")),
):
    _register(name, label, "enum", getattr(Order, name), options=options)
_register("source", "来源", "enum", Line.edited_source,
          options=("wbdd", "workbook_manual", "page_manual"))
_register("pool_name", "互通池名称", "text", _pool(PartPool.name))
_register("pool_status", "互通池状态", "enum", _pool(PartPool.status),
          options=("active", "archived"))
_register("in_pool", "是否在互通池", "enum", case(
    (or_(Line.pn_std.is_(None), Line.pn_std == ""), None),
    (_pool(PartPool.group_id).is_not(None), "是"), else_="否",
), options=("是", "否"))
for name, label in (
    ("line_no", "明细序号"), ("returned_qty", "已返数量"), ("pending_return_qty", "待返数量"),
    ("purchase_qty", "需采数量"), ("change_warehouse_purchase_qty", "变更仓需采数量"),
    ("purchased_qty", "已采数量"), ("pending_purchase_qty", "待采数量"),
    ("direct_ship_qty", "直采直发数"), ("warehouse_need_qty", "库房需发数"),
    ("warehouse_shipped_qty", "库房发货数"), ("supplied_qty", "已供数量"),
    ("pending_supply_qty", "待供数量"), ("consumed_qty", "领用数量"),
    ("demand_pending_return_qty", "需求待返数"),
):
    _register(name, label, "number", getattr(Line, name))
_register("return_old_part", "退返旧件", "text", Line.return_old_part)
for basis, label, fact, manual in (("ex", "未税", _ex, _manual_ex), ("inc", "含税", _inc, _manual_inc)):
    _register(f"system_unit_cost_{basis}_tax", f"系统{label}单位成本", "number",
              getattr(Line, f"unit_cost_{basis}_tax"), amount=True)
    _register(f"system_cost_amount_{basis}_tax", f"系统{label}成本金额", "number",
              getattr(Line, f"cost_amount_{basis}_tax"), amount=True)
    _register(f"effective_unit_cost_{basis}_tax", f"有效{label}单位成本", "number", fact[1], amount=True)
    _register(f"effective_cost_amount_{basis}_tax", f"有效{label}成本金额", "number", fact[0], amount=True)
    _register(f"manual_unit_cost_{basis}_tax", f"人工{label}单位成本", "number", manual, amount=True)
_register("manual_reason", "人工成本原因", "text", _manual(Override.reason), amount=True)
_register("known_apply_cost_inc_tax", "已知需求含税成本", "number", _inc[0], amount=True)
_register("effective_cost_tier", "有效含税成本分层", "enum", case(
    (_inc[2], "actual"), (_inc[3], "estimated"), else_="missing",
), amount=True, options=("actual", "estimated", "missing"))
_register("legacy_unit_cost", "兼容原值单位成本", "number", case(
    (_fallback, _round_cent(_manual_ex)),
    (_legacy_known, Line.unit_cost), else_=None,
), amount=True)
_register("legacy_cost_amount", "兼容原值成本金额", "number", case(
    (_fallback, _ex[0]), (_legacy_known, Line.cost_amount), else_=None,
), amount=True)
_register("legacy_cost_tier", "兼容原值成本分层", "enum", _legacy_tier, amount=True,
          options=("actual", "estimated", "missing"))
_register("legacy_cost_tax_basis", "兼容原值税口径", "enum", case(
    (_fallback, "ex"), else_=Line.cost_tax_basis,
), amount=True, options=("inc", "ex"))
# The label makes text operators explicit: eq compares the complete joined
# sequence, contains/prefix operate on the same visible string; [] is empty.
_register("anomaly_flags", "数据提示（以；连接）", "text",
          func.array_to_string(_resolved_flags, "；"), amount=True)
_register("system_cost_source", "系统成本来源", "enum", Line.cost_source, amount=True, options=_source_options)
_register("cost_source", "有效成本来源", "enum", _source, amount=True, options=_source_options)
_register("system_confidence", "系统置信度", "enum", func.coalesce(Line.confidence, "none"),
          amount=True, options=("high", "medium", "low", "none"))
_register("confidence", "有效置信度", "enum", case(
    (_source == "manual", "high"), (~_inc[5], "none"), else_=Line.confidence,
), amount=True, options=("high", "medium", "low", "none"))
_register("missing_kind", "有效成本缺失类型", "enum", _missing_kind, amount=True,
          options=("out_of_scope", "none", "invalid_cost_fact"))
_register("can_refill", "有效成本可补价", "enum", case((and_(
    ~_inc[5], or_(Line.cost_source.is_(None), Line.cost_source == "none"),
    _manual_active.is_(None),
), "是"), else_="否"), amount=True, options=("是", "否"))
_register("system_missing_kind", "系统成本缺失类型（V2）", "enum", case(
    (Line.unit_cost_ex_tax.is_not(None), None),
    (Line.cost_source.is_(None), "out_of_scope"), else_="none",
), amount=True, options=("out_of_scope", "none"))
_register("system_can_refill", "系统成本可补价（V2）", "enum", case(
    (and_(Line.unit_cost_ex_tax.is_(None),
          or_(Line.cost_source.is_(None), Line.cost_source == "none")), "是"), else_="否",
), amount=True, options=("是", "否"))
for name, label, type in (
    ("price_month", "取价月份", "text"), ("trace_months", "追溯月数", "number"),
    ("linked_purchase_order_no", "关联采购单", "text"),
    ("price_distance_days", "距采购天数", "number"), ("reference_side", "参考侧", "enum"),
    ("reference_sample_count", "参考样本数", "number"),
    ("reference_from_date", "参考起始日", "date"), ("reference_to_date", "参考截止日", "date"),
    ("reference_latest_date", "最近样本日", "date"),
):
    _register(name, label, type, case((_fallback, None), else_=getattr(Line, name)),
              amount=True, options=("purchase", "sales") if name == "reference_side" else ())


# 金臣 10-15：「需求数量为空按 0 算」。相等/范围按 0 比较；「为空」仍只匹配 NULL。
NULL_AS_ZERO_FIELDS = frozenset({"qty"})


@dataclass(frozen=True)
class RowFilter:
    field: str
    op: str
    value: Any = None
    min: Any = None
    max: Any = None


def _invalid(message="筛选条件无效"):
    raise HTTPException(422, {"code": "invalid_row_filter", "message": message})


def _value(value, field):
    if isinstance(value, bool) or value is None:
        _invalid("筛选值不能为空或布尔值")
    if field.type == "number":
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            _invalid("数值条件必须为有效数字")
        if not result.is_finite():
            _invalid("数值条件超出有效范围")
        # 查询端点可以超出字段存储域，不能套用金额写入/证据有效性边界。
        # 只在绑定前拒绝 PostgreSQL numeric 无法表示的精度与指数。
        if (not -16383 <= result.as_tuple().exponent <= 131071
                or result.adjusted() >= 131072):
            _invalid("数值条件精度超出有效范围")
        return result
    if (
        not isinstance(value, str) or not value or len(value) > 2048
        or "\x00" in value
    ):
        _invalid("筛选值必须为非空文本（最多 2048 字符）")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _invalid("筛选值必须为有效 UTF-8 文本")
    if field.type == "date":
        try:
            result = date.fromisoformat(value)
        except ValueError:
            _invalid("日期条件必须为 YYYY-MM-DD")
        if result.isoformat() != value:
            _invalid("日期条件必须为 YYYY-MM-DD")
        return result
    return value


def parse_filters(raw: str | None) -> tuple[RowFilter, ...]:
    if raw is None:
        return ()
    if len(raw) > 32768:
        _invalid("筛选条件过长")
    try:
        items = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        _invalid("filters 必须为 JSON 条件数组")
    if not isinstance(items, list) or len(items) > 30:
        _invalid("筛选条件须为数组，最多 30 条")
    result = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {"field", "op", "value", "min", "max"}:
            _invalid()
        name, op = item.get("field"), item.get("op")
        if not isinstance(name, str) or name not in FIELDS:
            _invalid("字段未注册，不可查询")
        field = FIELDS[name]
        if op not in OPERATORS[field.type]:
            _invalid("该字段不支持此运算")
        if op == "is_empty":
            if set(item) != {"field", "op"}:
                _invalid("为空条件不接受值")
            result.append(RowFilter(name, op))
        elif op == "range":
            if "value" in item or not ({"min", "max"} & set(item)):
                _invalid("范围条件至少需要一个端点")
            low = _value(item["min"], field) if "min" in item else None
            high = _value(item["max"], field) if "max" in item else None
            if low is not None and high is not None and low > high:
                _invalid("范围下限不能大于上限")
            result.append(RowFilter(name, op, min=low, max=high))
        else:
            if set(item) != {"field", "op", "value"}:
                _invalid("该条件需要且只接受 value")
            value = item["value"]
            if op == "in":
                if not isinstance(value, list) or not 1 <= len(value) <= 100:
                    _invalid("枚举条件需要 1 至 100 个值")
                value = tuple(_value(v, field) for v in value)
            else:
                value = _value(value, field)
            result.append(RowFilter(name, op, value=value))
    return tuple(result)


def field_expression(field: RowField | str, *, projection="workbook"):
    """Select the existing outlet's display semantics for filters and exports."""
    if projection not in {"workbook", "legacy", "v1"}:
        raise ValueError("unknown maintenance row projection")
    if isinstance(field, str):
        field = FIELDS[field]
    if projection == "legacy":
        return _legacy_expressions.get(field.field, field.expression)
    if projection == "v1" and field.field == "cost_source":
        return case((_inc[5], _source), else_="")
    return field.expression


def apply_row_filters(stmt, filters, *, can_filter_amounts: bool, projection="workbook"):
    stmt = stmt.outerjoin(
        _row_manual,
        and_(_row_manual.line_id == Line.id, _row_manual.active.is_(True)),
    )
    for item in filters:
        field = FIELDS[item.field]
        if field.amount and not can_filter_amounts:
            raise HTTPException(403, {
                "code": "row_filter_amount_forbidden",
                "message": "此范围不可按金额或成本证据筛选；请先明确选择有金额资格的项目",
            })
        expr = field_expression(field, projection=projection)
        if field.field in NULL_AS_ZERO_FIELDS and item.op in {"eq", "range"}:
            expr = func.coalesce(expr, 0)
        if item.op == "is_empty":
            predicate = or_(expr.is_(None), expr == "") if field.type in {"text", "enum"} else expr.is_(None)
        elif item.op == "contains":
            predicate = expr.contains(item.value, autoescape=True)
        elif item.op == "prefix":
            predicate = expr.startswith(item.value, autoescape=True)
        elif item.op == "eq":
            predicate = expr == item.value
        elif item.op == "in":
            predicate = expr.in_(item.value)
        else:
            if item.min is not None:
                stmt = stmt.where(expr >= item.min)
            if item.max is not None:
                stmt = stmt.where(expr <= item.max)
            continue
        stmt = stmt.where(predicate)
    return stmt
