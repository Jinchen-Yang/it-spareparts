"""D-26: WBDD address intent, scoped to this file type and source order ID."""
from collections import defaultdict
from dataclasses import dataclass

import pandas as pd

from app.etl import cleaner

ADDRESS_HEADERS = frozenset({"收货地址", "地址"})
OPERATION_HEADERS = frozenset({"地址操作", "收货地址操作"})


def address_header(name: str) -> str:
    name = name.strip().replace("（", "(").replace("）", ")")
    if name.endswith("(选填)"):
        name = name[:-4].strip()
    return name


@dataclass(frozen=True)
class AddressPatch:
    action: str = "keep"
    value: str | None = None
    rows: tuple[int, ...] = ()
    detail: str | None = None


def collect_addresses(frame: pd.DataFrame) -> dict[str, AddressPatch]:
    """Blanks do not clear. An explicit order-level operation overrides defaults.

    Keep original data-row numbers for diagnostics, without putting address
    text in error reports. No detail rows are filtered by this collector.
    """
    columns = [c for c in frame.columns if address_header(c) in ADDRESS_HEADERS]
    operations = [c for c in frame.columns if address_header(c) in OPERATION_HEADERS]
    if not columns and not operations:
        return {}
    groups = defaultdict(lambda: {"values": set(), "operations": set(), "rows": [], "order_no": ""})
    for position, (_, row) in enumerate(frame.iterrows(), 1):
        raw_id = cleaner.clean_str(row.get("数据ID(不可修改)"))
        order_no = cleaner.clean_str(row.get("需求单号"))
        if not raw_id or not order_no:
            continue
        group = groups[raw_id]
        group["order_no"] = order_no
        group["rows"].append(position)
        group["values"].update(value for c in columns if (value := cleaner.clean_str(row.get(c))))
        group["operations"].update(value for c in operations if (value := cleaner.clean_str(row.get(c))))
    result = {}
    for raw_id, group in groups.items():
        values, ops = group["values"], group["operations"]
        rows = tuple(group["rows"])
        reason = None
        if ops - {"保留", "设置", "清空"}:
            reason = "地址操作只支持保留、设置、清空"
        elif len(ops) > 1:
            reason = "同单填写了不同的地址操作"
        elif len(values) > 1:
            reason = "同单填写了不同的非空地址"
        elif ops == {"清空"}:
            result[raw_id] = AddressPatch("clear", rows=rows)
            continue
        elif ops == {"保留"}:
            result[raw_id] = AddressPatch(rows=rows)
            continue
        elif ops == {"设置"} and not values:
            reason = "设置地址时必须填写地址"
        elif values:
            result[raw_id] = AddressPatch("set", next(iter(values)), rows)
            continue
        if reason:
            row_labels = "、".join(str(number) for number in rows)
            detail = f"需求单 {group['order_no']}（数据行 {row_labels}）：{reason}，该单原地址保留，其他合法字段照常处理"
            result[raw_id] = AddressPatch("conflict", rows=rows, detail=detail)
        else:
            result[raw_id] = AddressPatch(rows=rows)
    return result


def address_report(orders: dict, patches: dict[str, AddressPatch], *, mode: str) -> dict:
    """Report parsed order intents, not a count of database value changes."""
    counts = {action: 0 for action in ("set", "keep", "clear", "conflict")}
    warnings = []
    for raw_id, head in orders.items():
        patch = patches.get(raw_id, AddressPatch())
        counts[patch.action] += 1
        if patch.action == "conflict":
            warnings.append({"order_no": head["order_no"],
                             "data_rows": list(patch.rows), "message": patch.detail})
    return {"mode": mode, **counts, "warnings": warnings}
