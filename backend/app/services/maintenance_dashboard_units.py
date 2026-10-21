"""看板单位分桶纯函数（M12 / B-F09）：数量与分母永不分单位混算。

平面字段兼容规则：桶内只有一种单位（含「仅待核」）→ 平面值照给；
出现 ≥2 种单位 → 平面数量/覆盖率 = None + ``unit_breakdown`` 呈现真实分桶。
金额是同币种小计，允许跨单位相加（金额例外，不在本模块处理）。
"""

from decimal import Decimal

_ZERO = Decimal("0")


def normalize_unit(unit: str | None) -> str | None:
    """null/空串/纯空白 → None（待核桶）；其余原样（不猜单位）。"""
    if unit is None:
        return None
    stripped = unit.strip()
    return stripped or None


def sorted_unit_keys(units) -> list[str | None]:
    """单位名升序；待核桶（None）恒最后。"""
    return sorted((unit for unit in units if unit is not None)) + [
        unit for unit in units if unit is None
    ]


class QtyBuckets:
    """按单位累加数量字段；单桶兼容平面值，多桶平面 = None。

    fields 声明聚合的数量字段名（Decimal）；``flat`` 在仅一种单位（含仅
    待核）时返回该单位小计，混合时返回 None。行级 ``unit`` 同规则。
    """

    __slots__ = ("_fields", "_data")

    def __init__(self, *fields: str):
        self._fields = tuple(fields)
        self._data: dict[str | None, dict[str, Decimal]] = {}

    @property
    def fields(self) -> tuple[str, ...]:
        return self._fields

    def add(self, unit: str | None, **values: Decimal) -> None:
        bucket = self._data.setdefault(
            normalize_unit(unit), {field: _ZERO for field in self._fields}
        )
        for field, value in values.items():
            bucket[field] += value if value is not None else _ZERO

    def units(self) -> list[str | None]:
        return sorted_unit_keys(self._data.keys())

    def items(self) -> dict[str | None, dict[str, Decimal]]:
        """按单位返回字段小计（合并多桶到更大聚合域时用）。"""
        return self._data

    def is_mixed(self) -> bool:
        return len(self._data) > 1

    def single_unit(self) -> str | None:
        """唯一真实单位；仅待核/混合 → None（行级 unit 字段语义）。"""
        if len(self._data) != 1:
            return None
        return next(iter(self._data))

    def flat(self, field: str) -> Decimal | None:
        """单一单位桶（含仅待核）→ 合计；混合 → None；空桶 → 0（兼容平面）。"""
        if len(self._data) > 1:
            return None
        if not self._data:
            return _ZERO
        return next(iter(self._data.values()))[field]

    def total(self, field: str) -> Decimal:
        return sum(
            (bucket[field] for bucket in self._data.values()), _ZERO
        )

    def breakdown(self, *fields: str, formatter) -> list[dict]:
        """按单位输出 ``{unit, **formatted fields}``；排序同 sorted_unit_keys。"""
        return [
            {
                "unit": unit,
                **{field: formatter(field, self._data[unit][field])
                   for field in fields},
            }
            for unit in self.units()
        ]


def add_domain_coverage(
    store: dict, domain: str, unit: str | None, qty: Decimal, *,
    kind: str, reason: str | None = None,
    value_inc: Decimal | None = None, value_ex: Decimal | None = None,
) -> None:
    """三域返件估值覆盖累加（B-F07）。

    kind ∈ estimated|pending|awaiting；reason 仅 pending 记（量归因）。
    逐单位已估金额（静态审查补充）：value_* 仅 estimated 行携带并按单位
    累计，供导出按单位展开时使用本单位金额（factor=0 的真实 0 保留）。
    桶形状：{bad, estimated, pending, awaiting, units, reasons}。
    """
    bucket = store.setdefault(
        domain,
        {
            "bad": _ZERO, "estimated": _ZERO,
            "pending": _ZERO, "awaiting": _ZERO,
            "units": {}, "reasons": {},
        },
    )
    bucket["bad"] += qty
    bucket[kind] += qty
    unit_bucket = bucket["units"].setdefault(
        normalize_unit(unit),
        {
            "bad": _ZERO, "estimated": _ZERO, "pending": _ZERO,
            "awaiting": _ZERO, "value_inc": _ZERO, "value_ex": _ZERO,
            "has_value": False,
        },
    )
    unit_bucket["bad"] += qty
    unit_bucket[kind] += qty
    if kind == "estimated" and value_inc is not None and value_ex is not None:
        unit_bucket["value_inc"] += value_inc
        unit_bucket["value_ex"] += value_ex
        unit_bucket["has_value"] = True
    if kind == "pending":
        key = reason or "pn_unresolved"
        bucket["reasons"][key] = bucket["reasons"].get(key, _ZERO) + qty


def domain_coverage_payload(
    store_entry: dict | None, *, values: dict | None, has_value: bool,
    qty_formatter, amount_formatter, coverage_pct,
) -> dict:
    """域覆盖载荷：平面 null-混合 + unit_breakdown + 金额键仅已估域。

    values/has_value 来自既有域金额小计（factor=0 是已估，"0.00" 保留）；
    coverage_pct(estimated, bad) 统一：分母 0 → None。
    """
    payload: dict = {}
    if has_value and values:
        payload["value_inc_tax"] = amount_formatter(values.get("inc_tax"))
        payload["value_ex_tax"] = amount_formatter(values.get("ex_tax"))
    if store_entry is None:
        return payload
    units = store_entry["units"]
    mixed = len(units) > 1
    single = next(iter(units), None) if len(units) == 1 else None

    def flat(field: str):
        return (
            qty_formatter(sum((u[field] for u in units.values()), _ZERO))
            if not mixed else None
        )

    payload["bad_qty"] = flat("bad")
    payload["estimated_bad_qty"] = flat("estimated")
    payload["pending_bad_qty"] = flat("pending")
    payload["awaiting_ratio_bad_qty"] = flat("awaiting")
    payload["coverage_pct"] = (
        None if mixed else
        coverage_pct(
            sum((u["estimated"] for u in units.values()), _ZERO),
            sum((u["bad"] for u in units.values()), _ZERO),
        )
    )
    payload["pending_reasons"] = {
        reason: qty_formatter(qty)
        for reason, qty in sorted(store_entry["reasons"].items())
    }
    payload["unit"] = single
    payload["unit_breakdown"] = [
        {
            "unit": unit,
            "bad_qty": qty_formatter(units[unit]["bad"]),
            "estimated_bad_qty": qty_formatter(units[unit]["estimated"]),
            "pending_bad_qty": qty_formatter(units[unit]["pending"]),
            "awaiting_ratio_bad_qty": qty_formatter(units[unit]["awaiting"]),
            # 本单位已估金额（仅已估单位给；完全待估单位无金额键）。
            **({
                "value_inc_tax": amount_formatter(units[unit]["value_inc"]),
                "value_ex_tax": amount_formatter(units[unit]["value_ex"]),
            } if units[unit].get("has_value") else {}),
            "coverage_pct": coverage_pct(
                units[unit]["estimated"], units[unit]["bad"]
            ),
        }
        for unit in sorted_unit_keys(units.keys())
    ]
    payload["coverage_basis"] = "bad_receipts"
    return payload
