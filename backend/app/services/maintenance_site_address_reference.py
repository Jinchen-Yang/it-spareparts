"""Read-only demand shipping-address references for the site-issue workbook.

The caller supplies current, scoped demand rows.  This index never resolves a
PN by text or changes the issue's actual site address.  Warehouse source-line
IDs refer to CKD lines and deliberately do not serve as WBDD identities here.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping


MULTIPLE_ADDRESSES = "多个地址待核对"
ADDRESS_MISSING = "需求单未填写地址"
DEMAND_NOT_FOUND = "未找到对应需求单"
_WBDD_NO = re.compile(r"WBDD-\d{8}-\d+")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


@dataclass(frozen=True)
class AddressReferenceIndex:
    """Address sets keyed by project and stable part, with explicit head lookup."""

    _by_part: Mapping[tuple[str, int], frozenset[str]]
    _by_order_part: Mapping[tuple[str, str, int], frozenset[str]]
    _orders_by_raw_id: Mapping[tuple[str, str], frozenset[str]]
    _orders_by_no: Mapping[tuple[str, str], frozenset[str]]

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[Any, Any, str]]) -> AddressReferenceIndex:
        """Build once from ``master._assigned_lines`` tuples, with no DB access.

        Empty address sets retain a matching demand whose address is absent.
        Explicit inactive rows are ignored defensively; project assignment and
        tombstone eligibility remain the query caller's responsibility.
        """
        by_part: dict[tuple[str, int], set[str]] = defaultdict(set)
        by_order_part: dict[tuple[str, str, int], set[str]] = defaultdict(set)
        orders_by_raw_id: dict[tuple[str, str], set[str]] = defaultdict(set)
        orders_by_no: dict[tuple[str, str], set[str]] = defaultdict(set)
        for demand_line, order, project_id in rows:
            if (getattr(demand_line, "is_active", True) is not True
                    or getattr(order, "data_status", "已生效") != "已生效"):
                continue
            project = _text(project_id)
            raw_id = _text(getattr(order, "raw_order_id", None))
            part_id = getattr(demand_line, "part_id", None)
            if not project or not raw_id or part_id is None:
                continue
            order_no = _text(getattr(order, "order_no", None))
            orders_by_raw_id[(project, raw_id)].add(raw_id)
            if order_no:
                orders_by_no[(project, order_no)].add(raw_id)
            # Materialize empty sets too: no demand differs from no address.
            part_addresses = by_part[(project, part_id)]
            order_addresses = by_order_part[(project, raw_id, part_id)]
            address = _text(getattr(order, "receiver_address", None))
            if address:
                part_addresses.add(address)
                order_addresses.add(address)

        def freeze(values):
            return MappingProxyType({key: frozenset(value) for key, value in values.items()})

        return cls(freeze(by_part), freeze(by_order_part),
                   freeze(orders_by_raw_id), freeze(orders_by_no))

    def resolve(self, site_line: Any, issue: Any) -> str:
        """Reference an address without guessing an explicit demand association."""
        project = _text(getattr(issue, "project_id", None))
        part_id = getattr(site_line, "part_id", None)
        if not project or part_id is None:
            return DEMAND_NOT_FOUND

        raw_id = _text(getattr(site_line, "source_order_id", None))
        order_no = _text(getattr(site_line, "demand_order_no", None))
        # Legacy files sometimes used the WBDD number as the issue number.
        # It is only a lookup hint and must exist in this project's demand map.
        if not raw_id and not order_no:
            legacy_no = _text(getattr(issue, "issue_no", None))
            if _WBDD_NO.fullmatch(legacy_no):
                order_no = legacy_no

        if raw_id or order_no:
            targets = None
            if raw_id:
                targets = self._orders_by_raw_id.get((project, raw_id), frozenset())
            if order_no:
                numbered = self._orders_by_no.get((project, order_no), frozenset())
                targets = numbered if targets is None else targets & numbered
            # A wrong/stale head, conflicting references or duplicated number
            # cannot fall back to another demand of the same part.
            if not targets or len(targets) != 1:
                return DEMAND_NOT_FOUND
            address_set = self._by_order_part.get((project, next(iter(targets)), part_id))
        else:
            address_set = self._by_part.get((project, part_id))

        if address_set is None:
            return DEMAND_NOT_FOUND
        if not address_set:
            return ADDRESS_MISSING
        if len(address_set) > 1:
            return MULTIPLE_ADDRESSES
        return next(iter(address_set))
