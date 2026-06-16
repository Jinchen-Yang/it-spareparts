"""行级清洗 + 校验：原始 DataFrame → 干净记录 + 错误 + 行级异常标记（§6.8/§11）。

原则：坏行隔离、好行照常。每个明细行独立校验，错误进 errors，不阻断其它行。
订单头从同一行（ffill 后头字段已补齐）解析，按 raw_order_id 首次出现去重。
"""
from dataclasses import dataclass, field

import pandas as pd

from app.etl import anomaly, cleaner, mapping


@dataclass
class ErrorRec:
    row_no: int
    error_type: str
    error_detail: str
    raw_row: dict


@dataclass
class TransformResult:
    file_type: str
    orders: dict = field(default_factory=dict)      # raw_order_id -> 头字段 dict
    lines: list = field(default_factory=list)        # 明细行 dict（含 _order_raw_id）
    inventory: list = field(default_factory=list)    # 库存行 dict
    errors: list = field(default_factory=list)
    rows_total: int = 0
    rows_inactive: int = 0


def _row_dict(row, field_map) -> dict:
    """原始行按映射取出 {内部字段: 原值}，用于错误留痕。"""
    return {v: (None if pd.isna(row.get(k)) else str(row.get(k))) for k, v in field_map.items()}


def _transform_orders(df: pd.DataFrame, file_type: str) -> TransformResult:
    res = TransformResult(file_type=file_type)
    head_map = mapping.MAPPINGS[file_type]["head"]
    line_map = mapping.MAPPINGS[file_type]["line"]
    inv_head = {v: k for k, v in head_map.items()}   # internal -> chinese
    inv_line = {v: k for k, v in line_map.items()}
    res.rows_total = len(df)

    for idx, row in df.iterrows():
        row_no = int(idx) + 1
        full_map = {**head_map, **line_map}

        raw_order_id = cleaner.clean_str(row.get(inv_head["raw_order_id"]))
        raw_line_id = cleaner.clean_str(row.get(inv_line["raw_line_id"]))

        if not raw_line_id or not raw_order_id:
            res.errors.append(ErrorRec(row_no, "missing_raw_id",
                                       "缺少订单/明细数据ID", _row_dict(row, full_map)))
            continue

        pn_std, pn_raw, needs_review = cleaner.standardize_pn(row.get(inv_line["pn_raw"]))
        if pn_std is None:
            res.errors.append(ErrorRec(row_no, "empty_pn", "产品名称为空", _row_dict(row, full_map)))
            continue

        # 行级数值
        try:
            qty = cleaner.parse_qty(row.get(inv_line["qty"]))
            unit_price = cleaner.parse_money(row.get(inv_line["unit_price"]))
            line_amount = cleaner.parse_money(row.get(inv_line["line_amount"]))
            recent = cleaner.parse_money(row.get(inv_line["recent_purchase_price"])) \
                if "recent_purchase_price" in inv_line else None
        except ValueError as exc:
            res.errors.append(ErrorRec(row_no, "bad_number", str(exc), _row_dict(row, full_map)))
            continue

        line = {
            "_order_raw_id": raw_order_id,
            "raw_line_id": raw_line_id,
            "line_no": cleaner.parse_int(row.get(inv_line["line_no"])),
            "pn_std": pn_std,
            "pn_raw": pn_raw,
            "needs_review": needs_review,
            "description": cleaner.clean_str(row.get(inv_line["description"])),
            "brand": cleaner.clean_str(row.get(inv_line["brand"])),
            "machine_or_part": cleaner.clean_str(row.get(inv_line["machine_or_part"])),
            "unit": cleaner.clean_str(row.get(inv_line["unit"])),
            "qty": qty,
            "unit_price": unit_price,
            "line_amount": line_amount,
            "recent_purchase_price": recent,
            "anomaly_flags": anomaly.line_flags(qty, unit_price, line_amount),
        }
        if file_type == mapping.SALES:
            line["category_major"] = cleaner.clean_category(row.get(inv_line["category_major"]))
            line["category_minor"] = cleaner.clean_category(row.get(inv_line["category_minor"]))
            line["generic_product"] = cleaner.clean_str(row.get(inv_line["generic_product"]))
            line["serial_numbers"] = cleaner.clean_str(row.get(inv_line["serial_numbers"]))
        res.lines.append(line)

        # 订单头（首次出现）
        if raw_order_id not in res.orders:
            head = _build_head(row, file_type, inv_head, row_no, res)
            res.orders[raw_order_id] = head
            if head.get("data_status") and head["data_status"] != "已生效":
                pass  # inactive 计数在 line 级更直观，见下
    # inactive 行数：按行所属订单状态统计
    res.rows_inactive = sum(
        1 for ln in res.lines
        if res.orders.get(ln["_order_raw_id"], {}).get("data_status") not in (None, "已生效")
    )
    return res


def _build_head(row, file_type, inv_head, row_no, res) -> dict:
    def g(internal):
        return row.get(inv_head[internal]) if internal in inv_head else None

    try:
        order_date = cleaner.parse_date(g("order_date"))
    except ValueError as exc:
        order_date = None
        res.errors.append(ErrorRec(row_no, "bad_date", str(exc), {"order_date": str(g("order_date"))}))
    try:
        tax_rate = cleaner.parse_rate(g("tax_rate"))
    except ValueError:
        tax_rate = None
    head = {
        "raw_order_id": cleaner.clean_str(g("raw_order_id")),
        "order_no": cleaner.clean_str(g("order_no")),
        "order_date": order_date,
        "tax_rate": tax_rate,
        "amount_ex_tax": _safe_money(g("amount_ex_tax")),
        "data_status": cleaner.clean_str(g("data_status")),
    }
    if file_type == mapping.PURCHASE:
        name_raw, name_norm = cleaner.normalize_supplier_name(g("supplier_name"))
        head.update({
            "purchaser": cleaner.clean_str(g("purchaser")),
            "supplier_name_raw": name_raw,
            "supplier_name_normalized": name_norm,
            "supplier_code": cleaner.clean_str(g("supplier_code")),
            "supplier_type": cleaner.clean_str(g("supplier_type")),
            "source_type_raw": cleaner.clean_str(g("source_type_raw")),
            "source_type": cleaner.normalize_source_type(g("source_type_raw")),
            "linked_sales_order_no": cleaner.clean_str(g("linked_sales_order_no")),
        })
    else:  # sales
        head.update({
            "salesperson": cleaner.clean_str(g("salesperson")),
            "customer_name": cleaner.clean_str(g("customer_name")),
            "customer_type": cleaner.clean_str(g("customer_type")),
            "customer_source": cleaner.clean_str(g("customer_source")),
            "customer_city": cleaner.clean_str(g("customer_city")),
            "business_type": cleaner.clean_str(g("business_type")),
            "warehouse": cleaner.clean_str(g("warehouse")),
        })
    return head


def _safe_money(x):
    try:
        return cleaner.parse_money(x)
    except ValueError:
        return None


def _transform_inventory(df: pd.DataFrame) -> TransformResult:
    res = TransformResult(file_type=mapping.INVENTORY)
    m = mapping.INVENTORY_MAP
    inv = {v: k for k, v in m.items()}
    res.rows_total = len(df)
    for idx, row in df.iterrows():
        row_no = int(idx) + 1
        raw_inv_id = cleaner.clean_str(row.get(inv["raw_inventory_id"]))
        if not raw_inv_id:
            res.errors.append(ErrorRec(row_no, "missing_raw_id", "缺少产品库存ID", _row_dict(row, m)))
            continue
        pn_std, pn_raw, needs_review = cleaner.standardize_pn(row.get(inv["pn_raw"]))
        if pn_std is None:
            res.errors.append(ErrorRec(row_no, "empty_pn", "产品名称为空", _row_dict(row, m)))
            continue
        warehouse = cleaner.clean_str(row.get(inv["warehouse"]))
        try:
            source_qty = cleaner.parse_qty(row.get(inv["source_qty"]))
        except ValueError as exc:
            res.errors.append(ErrorRec(row_no, "bad_number", str(exc), _row_dict(row, m)))
            continue
        if source_qty is None or warehouse is None:
            res.errors.append(ErrorRec(row_no, "missing_required", "库存数量或仓库为空",
                                       _row_dict(row, m)))
            continue
        res.inventory.append({
            "raw_inventory_id": raw_inv_id,
            "pn_std": pn_std, "pn_raw": pn_raw, "needs_review": needs_review,
            "warehouse": warehouse,
            "source_qty": source_qty,
            "description": cleaner.clean_str(row.get(inv["description"])),
            "brand": cleaner.clean_str(row.get(inv["brand"])),
            "machine_or_part": cleaner.clean_str(row.get(inv["machine_or_part"])),
            "unit": cleaner.clean_str(row.get(inv["unit"])),
            "generic_product": cleaner.clean_str(row.get(inv["generic_product"])),
            "data_status": cleaner.clean_str(row.get(inv["data_status"])),
        })
    res.rows_inactive = sum(
        1 for r in res.inventory if r["data_status"] not in (None, "已生效")
    )
    return res


def transform(df: pd.DataFrame, file_type: str) -> TransformResult:
    if file_type == mapping.INVENTORY:
        return _transform_inventory(df)
    return _transform_orders(df, file_type)
