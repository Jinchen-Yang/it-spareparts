"""行级清洗 + 校验：原始 DataFrame → 干净记录 + 错误 + 行级异常标记（§6.8/§11）。

原则：坏行隔离、好行照常。每个明细行独立校验，错误进 errors，不阻断其它行。
订单头从同一行（ffill 后头字段已补齐）解析，按 raw_order_id 首次出现去重。
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from app import config
from app.etl import anomaly, cleaner, mapping

# 非"硬错误"的软标记类型：不计入 fact_rows_error（见 loader），只在错误列表里以"可忽略"展示。
SOFT_ERROR_TYPES = frozenset({"empty_pn_inactive"})


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
    rows_excluded_warehouse: int = 0   # 排除仓（坏品仓等）跳过的库存行，进导入报告


def _row_dict(row, field_map) -> dict:
    """原始行按映射取出 {内部字段: 原值}，用于错误留痕。"""
    return {v: (None if pd.isna(row.get(k)) else str(row.get(k))) for k, v in field_map.items()}


def _empty_pn_error(row_no, status, label_no, raw_row) -> "ErrorRec":
    """空产品名分级：生效单缺产品=真错误 empty_pn；草稿/已取消等非生效单缺产品=
    预期内不完整数据 empty_pn_inactive（不计错误数，只留痕标"可忽略"）。"""
    if status and status != config.ACTIVE_STATUS:
        tail = f"·{label_no}" if label_no else ""
        return ErrorRec(row_no, "empty_pn_inactive",
                        f"产品名称为空（{status}单{tail}，可忽略）", raw_row)
    tail = f"（{label_no}）" if label_no else ""
    return ErrorRec(row_no, "empty_pn", f"产品名称为空{tail}", raw_row)


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
            res.errors.append(_empty_pn_error(
                row_no, cleaner.clean_str(row.get(inv_head["data_status"])),
                cleaner.clean_str(row.get(inv_head["order_no"])), _row_dict(row, full_map)))
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
        supplier_type = cleaner.clean_str(g("supplier_type"))
        tax_amount = _safe_money(g("tax_amount"))
        # 税率列实测常空 → 用 税金/不含税金额 反推（含税单），保证含税未税换算可用
        if head["tax_rate"] is None:
            head["tax_rate"] = cleaner.derive_tax_rate(head["amount_ex_tax"], tax_amount)
        head.update({
            "purchaser": cleaner.clean_str(g("purchaser")),
            "supplier_name_raw": name_raw,
            "supplier_name_normalized": name_norm,
            "supplier_code": cleaner.clean_str(g("supplier_code")),
            "supplier_type": supplier_type,
            "supplier_source_channel": cleaner.classify_source_channel(
                name_raw, name_norm, supplier_type),
            "source_type_raw": cleaner.clean_str(g("source_type_raw")),
            "source_type": cleaner.normalize_source_type(g("source_type_raw")),
            "linked_sales_order_no": cleaner.clean_str(g("linked_sales_order_no")),
            "linked_maintenance_order_no": cleaner.clean_str(g("linked_maintenance_order_no")),
            "is_tax_inclusive": cleaner.parse_tax_inclusive(g("is_tax_inclusive")),
            "tax_amount": tax_amount,
            "amount_inc_tax": _safe_money(g("amount_inc_tax")),
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


# 项目名规范化：剥「预交付-」前缀（聚合键；原值另存 project_raw 供追溯）。
# 横线必需（半/全角/长横容差）：只剥「预交付-X」，不动恰好以「预交付」开头的正常项目名。
_PROJECT_PREFIX = re.compile(r"^预交付[-—－]")
# 预建单容忍窗：制单日期晚于导入日超过此天数 → 记 future_date 异常（不拦截，实测存在预建单）
_FUTURE_TOLERANCE_DAYS = 30


def _transform_maintenance(df: pd.DataFrame) -> TransformResult:
    """维保出库（WBDD）：无任何价格列，成本由 maintenance_cost.recompute 回填。"""
    res = TransformResult(file_type=mapping.MAINTENANCE)
    head_map = mapping.MAINTENANCE_HEAD
    line_map = mapping.MAINTENANCE_LINE
    inv_head = {v: k for k, v in head_map.items()}
    inv_line = {v: k for k, v in line_map.items()}
    res.rows_total = len(df)
    future_cutoff = date.today() + timedelta(days=_FUTURE_TOLERANCE_DAYS)

    for idx, row in df.iterrows():
        row_no = int(idx) + 1
        full_map = {**head_map, **line_map}

        raw_order_id = cleaner.clean_str(row.get(inv_head["raw_order_id"]))
        raw_line_id = cleaner.clean_str(row.get(inv_line["raw_line_id"]))
        if not raw_line_id or not raw_order_id:
            res.errors.append(ErrorRec(row_no, "missing_raw_id",
                                       "缺少维保单/明细数据ID", _row_dict(row, full_map)))
            continue
        # 需求单号(WBDD)为空：无法关联专属采购，且 ffill 可能把上一单单号串下来错配成本 → 整行跳过
        order_no = cleaner.clean_str(row.get(inv_head["order_no"]))
        if not order_no:
            res.errors.append(ErrorRec(row_no, "missing_order_no",
                                       "需求单号为空（无法关联成本，整行跳过）", _row_dict(row, full_map)))
            continue

        pn_std, pn_raw, needs_review = cleaner.standardize_pn(row.get(inv_line["pn_raw"]))
        if pn_std is None:
            res.errors.append(_empty_pn_error(
                row_no, cleaner.clean_str(row.get(inv_head["data_status"])),
                cleaner.clean_str(row.get(inv_head["order_no"])), _row_dict(row, full_map)))
            continue

        try:
            qty = cleaner.parse_qty(row.get(inv_line["qty"]))
            return_qty = cleaner.parse_qty(row.get(inv_line["return_qty"]))
        except ValueError as exc:
            res.errors.append(ErrorRec(row_no, "bad_number", str(exc), _row_dict(row, full_map)))
            continue

        res.lines.append({
            "_order_raw_id": raw_order_id,
            "raw_line_id": raw_line_id,
            "line_no": cleaner.parse_int(row.get(inv_line["line_no"])),
            "pn_std": pn_std, "pn_raw": pn_raw, "needs_review": needs_review,
            "description": cleaner.clean_str(row.get(inv_line["description"])),
            "qty": qty, "return_qty": return_qty,
            "serial_numbers": cleaner.clean_str(row.get(inv_line["serial_numbers"])),
            "anomaly_flags": [],
        })

        if raw_order_id not in res.orders:
            def g(internal):
                return row.get(inv_head[internal]) if internal in inv_head else None

            order_date = maint_start = maint_end = None
            try:
                order_date = cleaner.parse_date(g("order_date"))
            except ValueError as exc:
                res.errors.append(ErrorRec(row_no, "bad_date", str(exc),
                                           {"order_date": str(g("order_date"))}))
            # 维保起止仅展示用，坏值不阻断；两列各自独立 try，一个坏值不连累另一个
            try:
                maint_start = cleaner.parse_date(g("maint_start"))
            except ValueError:
                pass
            try:
                maint_end = cleaner.parse_date(g("maint_end"))
            except ValueError:
                pass
            project_raw = cleaner.clean_str(g("project_raw"))
            res.orders[raw_order_id] = {
                "raw_order_id": raw_order_id,
                "order_no": cleaner.clean_str(g("order_no")),
                "order_date": order_date,
                "linked_sales_order_no": cleaner.clean_str(g("linked_sales_order_no")),
                "project_raw": project_raw,
                "project_std": (_PROJECT_PREFIX.sub("", project_raw).strip() or project_raw)
                                if project_raw else None,
                "customer_name": cleaner.clean_str(g("customer_name")),
                "end_customer": cleaner.clean_str(g("end_customer")),
                "demand_type": cleaner.clean_str(g("demand_type")),
                "business_type": cleaner.clean_str(g("business_type")),
                "salesperson": cleaner.clean_str(g("salesperson")),
                "warehouse": cleaner.clean_str(g("warehouse")),
                "maint_start": maint_start, "maint_end": maint_end,
                "data_status": cleaner.clean_str(g("data_status")),
                "_future_date": bool(order_date and order_date > future_cutoff),
            }
        if res.orders[raw_order_id].get("_future_date"):
            res.lines[-1]["anomaly_flags"] = ["future_date"]

    res.rows_inactive = sum(
        1 for ln in res.lines
        if res.orders.get(ln["_order_raw_id"], {}).get("data_status") not in (None, "已生效")
    )
    return res


_BXD_RX = re.compile(r"BXD-\d{8}-\d+")


def _transform_expense(df: pd.DataFrame) -> TransformResult:
    """维保报销单（BXD 费用侧，§16.3）：单表平铺（无独立头表），行级金额。

    幂等键 = 报销明细数据ID；无（工作簿版）→ BXD单号#序号 复合键退路。
    列名漂移回退：费用分类↔报销明细.费用分类、维保销售订单↔销售订单（取非空互补）。
    生效口径 = 流程状态 MAINT_EXPENSE_ACTIVE_STATUS（'已结束'），非生效行照常入库计 inactive。
    """
    res = TransformResult(file_type=mapping.EXPENSE)
    res.rows_total = len(df)
    full_map = {**mapping.EXPENSE_HEAD, **mapping.EXPENSE_LINE}

    for idx, row in df.iterrows():
        row_no = int(idx) + 1

        def gv(*names):
            for n in names:
                if n in df.columns:
                    v = row.get(n)
                    if v is not None and not pd.isna(v):
                        return v
            return None

        title = cleaner.clean_str(gv("数据标题"))
        m = _BXD_RX.search(title or "")
        bxd_no = m.group(0) if m else None
        line_no = cleaner.parse_int(gv("报销明细.序号", "序号"))
        raw_line = cleaner.clean_str(gv("报销明细.数据ID(不可修改)", "报销明细.数据ID"))
        if not raw_line:
            if not bxd_no or line_no is None:
                res.errors.append(ErrorRec(row_no, "missing_raw_id",
                                           "缺少报销明细数据ID，且 BXD单号/序号 不足以兜底",
                                           _row_dict(row, full_map)))
                continue
            raw_line = f"{bxd_no}#{line_no}"        # 复合键退路（§16.3）
        try:
            amount = cleaner.parse_money(gv("报销明细.报销金额", "报销金额"))
        except ValueError as exc:
            res.errors.append(ErrorRec(row_no, "bad_number", str(exc), _row_dict(row, full_map)))
            continue
        expense_date = None
        try:
            expense_date = cleaner.parse_date(gv("报销日期"))
        except ValueError as exc:
            res.errors.append(ErrorRec(row_no, "bad_date", str(exc),
                                       {"expense_date": str(gv("报销日期"))}))
        res.lines.append({
            "raw_line_id": raw_line, "bxd_no": bxd_no, "line_no": line_no,
            "data_status": cleaner.clean_str(gv("流程状态")),
            "expense_date": expense_date,
            "person": cleaner.clean_str(gv("报销人员")),
            "expense_type": cleaner.clean_str(gv("报销类别")),
            "fee_category": cleaner.clean_str(gv("报销明细.费用分类", "费用分类")),
            "reason": cleaner.clean_str(gv("支出事由")),
            "linked_sales_order_no": cleaner.clean_str(gv("维保销售订单", "销售订单")),
            "amount": amount,
        })
    res.rows_inactive = sum(
        1 for r in res.lines
        if r["data_status"] not in (None, config.MAINT_EXPENSE_ACTIVE_STATUS)
    )
    return res


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
        warehouse = cleaner.clean_str(row.get(inv["warehouse"]))
        # 排除仓（坏品仓等，甲方 2026-07-03：坏品不进系统）：整行跳过、计数进报告，
        # 不算错误也不建档——放在空 PN 判定之前，排除仓的脏行不该产生导入错误。
        if warehouse and any(p in warehouse for p in config.INVENTORY_EXCLUDED_WAREHOUSES):
            res.rows_excluded_warehouse += 1
            continue
        pn_std, pn_raw, needs_review = cleaner.standardize_pn(row.get(inv["pn_raw"]))
        if pn_std is None:
            res.errors.append(_empty_pn_error(
                row_no, cleaner.clean_str(row.get(inv["data_status"])), None, _row_dict(row, m)))
            continue
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
    if file_type == mapping.MAINTENANCE:
        return _transform_maintenance(df)
    if file_type == mapping.EXPENSE:
        return _transform_expense(df)
    return _transform_orders(df, file_type)
