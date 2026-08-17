"""行级清洗 + 校验：原始 DataFrame → 干净记录 + 错误 + 行级异常标记（§6.8/§11）。

原则：坏行隔离、好行照常。每个明细行独立校验，错误进 errors，不阻断其它行。
订单头从同一行（ffill 后头字段已补齐）解析，按 raw_order_id 首次出现去重。
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from app import config, tax_policy
from app.etl import anomaly, cleaner, mapping

# 非"硬错误"的软标记类型：不计入 fact_rows_error（见 loader），只在错误列表里以"可忽略"展示。
SOFT_ERROR_TYPES = frozenset({"empty_pn_inactive", "missing_date_in_progress"})


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
    rows_skipped_no_data: int = 0      # 报销页缺 日期/金额 跳过的行（合计行/空行，非错误，§17.3）
    # WBDD 专用（plan v1.3 M1-2/M1-3）：
    rows_display_issue: int = 0        # 展示补全列坏值（旗标非是/否、日期/数量解析失败）计数，不阻断行
    headless_order_ids: list = field(default_factory=list)  # 「有单头、无明细」订单（保留入库）


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
# 单一事实源在 services/project_names（plan v1.3 M2-3 统一）；此处保留别名兼容既有引用。
from app.services.project_names import PRE_DELIVERY_PREFIX as _PROJECT_PREFIX  # noqa: E402

# ---- WBDD 展示补全列解析规格（plan v1.3 §3；只展示，坏值不阻断行，计 rows_display_issue）----
# 值 = (解析类别, 字符列截断长度 None=Text 不截断)。字段集与 mapping.MAINTENANCE_*_DISPLAY 一一对应。
_WBDD_HEAD_DISPLAY_SPEC: dict[str, tuple[str, int | None]] = {
    "head_demand_qty": ("qty", None), "head_purchase_qty": ("qty", None),
    "head_shipped_qty": ("qty", None), "head_returned_qty": ("qty", None),
    "maintainer_raw": ("str", 64), "work_order_no": ("str", 64),
    "created_by_raw": ("str", 64), "purchaser_raw": ("str", 64),
    "purchaser2_raw": ("str", 64), "project_manager_raw": ("str", 64),
    "project_manager_staff_raw": ("str", 64), "co_salesperson_raw": ("str", 64),
    "partner_raw": ("str", 64), "sales_dept_raw": ("str", 64),
    "warehouse_keeper_raw": ("str", 64), "storage_center": ("str", 64),
    "warehouse_raw": ("str", 64), "change_warehouse_flag": ("bool", None),
    "change_warehouse": ("str", 64), "change_warehouse_handler": ("str", 64),
    "warehouse_handler": ("str", 64), "supply_deadline": ("date", None),
    "delivery_address_option": ("str", 128), "receiver": ("str", 64),
    "receiver_phone": ("str", 32), "receiver_address": ("str", None),
    "express_no": ("str", 128), "express_no2": ("str", 128),
    "image_urls": ("str", None), "attachments": ("str", None),
    "whole_machine_check": ("str", 16), "accept_generic_flag": ("bool", None),
    "created_at_raw": ("str", 32), "modified_at_raw": ("str", 32),
}
_WBDD_LINE_DISPLAY_SPEC: dict[str, tuple[str, int | None]] = {
    "purchase_qty": ("qty", None), "change_warehouse_purchase_qty": ("qty", None),
    "purchased_qty": ("qty", None), "pending_purchase_qty": ("qty", None),
    "direct_ship_qty": ("qty", None), "warehouse_need_qty": ("qty", None),
    "warehouse_shipped_qty": ("qty", None), "supplied_qty": ("qty", None),
    "pending_supply_qty": ("qty", None), "returned_qty": ("qty", None),
    "pending_return_qty": ("qty", None), "consumed_qty": ("qty", None),
    "demand_pending_return_qty": ("qty", None), "return_old_part": ("str", 16),
    "whole_or_part": ("str", 8), "whole_machine_purchase_part": ("str", None),
    "whole_machine_part_purchased": ("str", 16), "purchase_note": ("str", None),
    "line_note": ("str", None), "line_image_urls": ("str", None),
    "warehouse_stock_raw": ("str", None), "adjust_warehouse_flag": ("bool", None),
    "adjust_warehouse": ("str", 64), "adjust_storage_center": ("str", 64),
    "adjust_keeper": ("str", 64), "ship_warehouse": ("str", 64),
    "ship_warehouse_object_id": ("str", 64), "ship_stock": ("qty", None),
}


def _wbdd_display_extras(row, inv_map: dict, spec: dict, res: TransformResult) -> dict:
    """按规格解析展示补全列：坏值 → NULL＋rows_display_issue+=1，绝不阻断行（只展示用途）。"""
    out: dict = {}
    for field_name, (kind, clip) in spec.items():
        source_col = inv_map.get(field_name)
        raw = row.get(source_col) if source_col else None
        if kind == "qty":
            try:
                out[field_name] = cleaner.parse_qty(raw)
            except ValueError:
                out[field_name] = None
                res.rows_display_issue += 1
        elif kind == "bool":
            s = cleaner.clean_str(raw)
            if not s:
                out[field_name] = None
            elif s in ("是", "否"):
                out[field_name] = (s == "是")
            else:
                out[field_name] = None
                res.rows_display_issue += 1
        elif kind == "date":
            try:
                out[field_name] = cleaner.parse_date(raw)
            except ValueError:
                out[field_name] = None
                res.rows_display_issue += 1
        else:
            s = cleaner.clean_str(raw)
            out[field_name] = (s[:clip] if (s and clip) else s)
    return out
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
        if not raw_order_id:
            res.errors.append(ErrorRec(row_no, "missing_raw_id",
                                       "缺少维保单数据ID", _row_dict(row, full_map)))
            continue
        # 需求单号(WBDD)为空：无法关联专属采购，且 ffill 可能把上一单单号串下来错配成本 → 整行跳过
        order_no = cleaner.clean_str(row.get(inv_head["order_no"]))
        if not order_no:
            res.errors.append(ErrorRec(row_no, "missing_order_no",
                                       "需求单号为空（无法关联成本，整行跳过）", _row_dict(row, full_map)))
            continue
        # 「有单头、无明细」订单保留（plan v1.3 M1-2）：单头照常注册入库、明细 0 行，
        # 不再记 missing_raw_id 错误。ffill 使空白间隔行携带上一单头值——它们落到
        # 已注册订单的 no-op 分支，不会虚增订单。
        if not raw_line_id:
            _register_wbdd_order(res, row, row_no, inv_head, future_cutoff)
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
            **_wbdd_display_extras(row, inv_line, _WBDD_LINE_DISPLAY_SPEC, res),
        })

        _register_wbdd_order(res, row, row_no, inv_head, future_cutoff)
        if res.orders[raw_order_id].get("_future_date"):
            res.lines[-1]["anomaly_flags"] = ["future_date"]

    res.rows_inactive = sum(
        1 for ln in res.lines
        if res.orders.get(ln["_order_raw_id"], {}).get("data_status") not in (None, "已生效")
    )
    line_order_ids = {ln["_order_raw_id"] for ln in res.lines}
    res.headless_order_ids = sorted(
        oid for oid in res.orders if oid not in line_order_ids
    )
    return res


def _register_wbdd_order(res: TransformResult, row, row_no: int,
                         inv_head: dict, future_cutoff: date) -> None:
    """按 raw_order_id 首次出现注册 WBDD 单头（幂等；有/无明细行共用同一路径）。"""
    raw_order_id = cleaner.clean_str(row.get(inv_head["raw_order_id"]))
    if raw_order_id in res.orders:
        return

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
        **_wbdd_display_extras(row, inv_head, _WBDD_HEAD_DISPLAY_SPEC, res),
    }


_BXD_RX = re.compile(r"BXD-\d{8}-\d+")


def _clip(s: str | None, limit: int = 64) -> str | None:
    """手填自由文本进定长列前钳制长度：超长截断而非流到 DB 炸整批（坏行隔离原则）。"""
    return s[:limit] if s else s


def _transform_expense(df: pd.DataFrame, anchor: str | None = None) -> TransformResult:
    """报销明细（费用侧，§17.3 来源无关宽松口径）：单表平铺，行级金额，头级字段块内继承。

    真实氚云导出（生产批次 #168 实锤）是「头行＋明细延续行」结构：头行带费用单号/
    报销日期/人员/事由/销售订单，同一张报销单的后续明细行这些头级格留空（Excel
    「同上」习惯），只有行级明细金额。原「行行独立（无 ffill）」口径把延续行全判
    missing_date——97 行只进 14 行。现口径：
      - **头行**（报销日期非空）：快照头级字段，供后续延续行继承；
      - **延续行**（有明细金额、头级全空）：继承上方头行的 日期/单号/人员/事由/
        类别/销售订单/流程状态；行级明细列（报销明细.*）不继承；
      - 文件开头无头可承的孤行仍报 missing_date；**新单据块首行有单号却没日期**
        同样报错并作废旧快照——绝不跨单据块继承日期（宁缺毋错）。
    行分拣（其余不变）：
      - 金额全空/只有日期 → 跳过（rows_skipped_no_data，空行）
      - 有金额但缺日期且无头可承：任一文本格含「合计」→ 跳过（导出件合计行）；
        否则 **missing_date 错误**——半截行必须响，不许静默丢（对账口径）
    归集键：行级 维保销售订单/销售订单 > 页级锚(anchor)；皆无 → 错误行。
    幂等键（§17.4，生产 f_project_expense 为空表，无历史键兼容包袱）：
      1. 报销明细.数据ID（氚云原生，实际已无此来源，保留兼容）
      2. 单号#序号@合同域hash —— 手填单号是自由文本，必须限定在合同域内防跨合同撞键；
         同文件内重复 单号+序号 → duplicate_key 错误行（否则 upsert 撞 CardinalityViolation 炸整批）
      3. 内容派生 EXP:sha1(xsdd|日期|金额|事由|人员)#重复序 —— 月更工作簿重传天然幂等
    流程状态为空 → 默认已结束（表单心智：写上来的就是要计入的账）。
    """
    res = TransformResult(file_type=mapping.EXPENSE)
    res.rows_total = len(df)
    full_map = {**mapping.EXPENSE_HEAD, **mapping.EXPENSE_LINE}
    content_seen: dict[str, int] = {}
    composite_seen: set[tuple] = set()
    # 延续行可继承的头级列（仅头级；报销明细.* 行级列绝不继承）
    _HEAD_COLS = ("报销日期", "费用单号", "单号", "报销单号", "数据标题",
                  "报销人员", "申请人", "支出事由", "报销主题",
                  "报销类别", "费用类型", "维保销售订单", "销售订单", "流程状态")
    head_ctx: dict[str, object] = {}

    for idx, row in df.iterrows():
        row_no = int(idx) + 1

        def gv(*names):
            for n in names:
                if n in df.columns:
                    v = row.get(n)
                    if v is not None and not pd.isna(v):
                        return v
            return None

        raw_date = gv("报销日期")
        is_continuation = False
        if raw_date is not None or gv("费用单号", "单号", "报销单号") is not None:
            # 头行（有日期或有单号）：快照头级字段（只存非空格）。在途单（审批未
            # 完成、日期未生成）的头行也快照——快照里没有「报销日期」键，延续行
            # 不会错误继承旧单日期，但能拿到流程状态按「在途」归类。
            head_ctx = {c: gv(c) for c in _HEAD_COLS if gv(c) is not None}

        def gvh(*names):
            """头级字段读取：本行为延续行时回退到头行快照（行级列不走此函数）。"""
            v = gv(*names)
            if v is not None:
                return v
            if is_continuation:
                for n in names:
                    if n in head_ctx:
                        return head_ctx[n]
            return None
        raw_amount = gv("报销明细.报销金额", "报销金额")
        raw_amount_ex = gv(
            "报销明细.报销金额（未税）",
            "报销金额（未税）",
            "报销金额(未税)",
            "未税金额",
            "不含税金额",
        )
        raw_amount_inc = gv(
            "报销明细.报销金额（含税）",
            "报销金额（含税）",
            "报销金额(含税)",
            "含税金额",
        )
        if raw_amount is None and raw_amount_ex is None and raw_amount_inc is None:
            res.rows_skipped_no_data += 1          # 空行/只有日期的行：跳过不算错
            continue
        if raw_date is None:
            texts = " ".join(str(gv(c) or "") for c in ("支出事由", "报销人员", "报销类别",
                                                        "费用分类", "报销明细.费用分类"))
            if "合计" in texts:
                res.rows_skipped_no_data += 1      # 导出件/手填的合计行
                continue
            if head_ctx.get("报销日期") is not None:
                # 延续行：继承上方头行日期（同一报销单的明细，「同上」语义）
                raw_date = head_ctx["报销日期"]
                is_continuation = True
            else:
                # 无日期可继承：区分「在途单」与「真孤行」。流程状态非生效
                # （如「进行中」＝审批未走完，氚云尚未生成报销日期）是预期内
                # 状态——软标记不计错误数（同 empty_pn_inactive 惯例）；单据
                # 走完审批后重新导出上传，快照 upsert 自然计入。
                status = cleaner.clean_str(gv("流程状态")) or cleaner.clean_str(
                    head_ctx.get("流程状态"))
                head_no = (cleaner.clean_str(gv("费用单号", "单号", "报销单号"))
                           or cleaner.clean_str(head_ctx.get("费用单号")))
                if status and status != config.MAINT_EXPENSE_ACTIVE_STATUS:
                    tail = f"（{head_no}）" if head_no else ""
                    res.errors.append(ErrorRec(
                        row_no, "missing_date_in_progress",
                        f"报销单{tail}流程状态为「{status}」，报销日期尚未生成，"
                        "本次跳过；审批完成后重新导出上传即会计入（可忽略）",
                        _row_dict(row, full_map)))
                else:
                    res.errors.append(ErrorRec(
                        row_no, "missing_date",
                        "该行有金额但缺报销日期，且上方没有可继承的头行"
                        "（同一报销单的明细行日期可留空「同上」；孤行请补齐日期）",
                        _row_dict(row, full_map)))
                continue
        try:
            expense_date = cleaner.parse_date(raw_date)
            amount = cleaner.parse_money(
                raw_amount,
                rounding=tax_policy.MONEY_ROUNDING,
            )
            amount_ex_tax = cleaner.parse_money(
                raw_amount_ex,
                rounding=tax_policy.MONEY_ROUNDING,
            )
            amount_inc_tax = cleaner.parse_money(
                raw_amount_inc,
                rounding=tax_policy.MONEY_ROUNDING,
            )
        except ValueError as exc:
            res.errors.append(ErrorRec(row_no, "bad_number", str(exc), _row_dict(row, full_map)))
            continue
        if expense_date is None:
            res.rows_skipped_no_data += 1
            continue

        amount = tax_policy.round_money(amount) if amount is not None else None
        amount_ex_tax = (
            tax_policy.round_money(amount_ex_tax)
            if amount_ex_tax is not None else None
        )
        amount_inc_tax = (
            tax_policy.round_money(amount_inc_tax)
            if amount_inc_tax is not None else None
        )
        basis_columns = (
            "报销明细.金额口径",
            "金额口径",
            "税务口径",
            "含税/未税",
            "是否含税",
            "是否含税(必填)",
            "含税标记",
            "报销明细.是否含税",
            "报销明细.含税标记",
        )
        inc_values = {
            "含税",
            "含税金额",
            "含税口径",
            "inc",
            "inc_tax",
            "是",
            "yes",
            "y",
            "true",
            "1",
            "1.0",
        }
        ex_values = {
            "未税",
            "不含税",
            "未税金额",
            "不含税金额",
            "未税口径",
            "ex",
            "ex_tax",
            "否",
            "no",
            "n",
            "false",
            "0",
            "0.0",
        }
        basis_hints: list[tuple[str, str]] = []
        bad_basis: tuple[str, str] | None = None
        for basis_column in basis_columns:
            tax_basis_raw = cleaner.clean_str(gv(basis_column))
            basis_key = (tax_basis_raw or "").strip().casefold()
            if not basis_key:
                continue
            if basis_key in inc_values:
                basis_hints.append((basis_column, "inc"))
            elif basis_key in ex_values:
                basis_hints.append((basis_column, "ex"))
            else:
                bad_basis = (basis_column, tax_basis_raw)
                break
        if bad_basis is not None:
            basis_column, tax_basis_raw = bad_basis
            res.errors.append(ErrorRec(
                row_no,
                "bad_tax_basis",
                f"无法识别{basis_column}：{tax_basis_raw}（仅支持含税/未税）",
                _row_dict(row, full_map),
            ))
            continue
        distinct_hints = {hint for _column, hint in basis_hints}
        if len(distinct_hints) > 1:
            res.errors.append(ErrorRec(
                row_no,
                "conflicting_tax_basis",
                "同一行的金额口径字段互相冲突，请统一为含税或未税",
                _row_dict(row, full_map),
            ))
            continue
        if distinct_hints:
            basis_hint = next(iter(distinct_hints))
        else:
            basis_hint = None

        if amount_ex_tax is not None or amount_inc_tax is not None:
            # 显式双税列优先；“金额口径”决定权威侧，缺口只能由同口径 raw amount 补齐。
            tax_basis = basis_hint or (
                "ex" if amount_ex_tax is not None else "inc"
            )
            if tax_basis == "inc":
                authority = (
                    amount_inc_tax
                    if amount_inc_tax is not None else amount
                )
                if authority is None:
                    res.errors.append(ErrorRec(
                        row_no,
                        "missing_authoritative_tax_amount",
                        "金额口径为含税，但未提供含税金额",
                        _row_dict(row, full_map),
                    ))
                    continue
                expected_ex = tax_policy.ex_from_inc(authority)
                if amount_ex_tax is not None and amount_ex_tax != expected_ex:
                    res.errors.append(ErrorRec(
                        row_no,
                        "inconsistent_tax_amount",
                        "含税金额与未税金额不符合统一 13% 税率",
                        _row_dict(row, full_map),
                    ))
                    continue
                amount_inc_tax = authority
                amount_ex_tax = expected_ex
            else:
                authority = (
                    amount_ex_tax
                    if amount_ex_tax is not None else amount
                )
                if authority is None:
                    res.errors.append(ErrorRec(
                        row_no,
                        "missing_authoritative_tax_amount",
                        "金额口径为未税，但未提供未税金额",
                        _row_dict(row, full_map),
                    ))
                    continue
                expected_inc = tax_policy.inc_from_ex(authority)
                if amount_inc_tax is not None and amount_inc_tax != expected_inc:
                    res.errors.append(ErrorRec(
                        row_no,
                        "inconsistent_tax_amount",
                        "含税金额与未税金额不符合统一 13% 税率",
                        _row_dict(row, full_map),
                    ))
                    continue
                amount_ex_tax = authority
                amount_inc_tax = expected_inc

            if amount is not None and amount != authority:
                res.errors.append(ErrorRec(
                    row_no,
                    "inconsistent_raw_amount",
                    "报销金额与金额口径指定的权威含税/未税金额不一致",
                    _row_dict(row, full_map),
                ))
                continue
            amount = authority
        else:
            if amount is None:
                res.rows_skipped_no_data += 1
                continue
            if basis_hint == "inc":
                tax_basis = "inc"
                amount_inc_tax = amount
                amount_ex_tax = tax_policy.ex_from_inc(amount)
            elif basis_hint == "ex":
                tax_basis = "ex"
                amount_ex_tax = amount
                amount_inc_tax = tax_policy.inc_from_ex(amount)
            else:
                tax_basis = "default_ex"
                amount_ex_tax = amount
                amount_inc_tax = tax_policy.inc_from_ex(amount)

        amount = tax_policy.round_money(amount)
        amount_ex_tax = tax_policy.round_money(amount_ex_tax)
        amount_inc_tax = tax_policy.round_money(amount_inc_tax)

        # 头级字段走 gvh：延续行继承头行值（#168 形态）；行级明细列仍走 gv
        xsdd = _clip(cleaner.clean_str(gvh("维保销售订单", "销售订单")) or anchor)
        if not xsdd:
            res.errors.append(ErrorRec(row_no, "missing_link",
                                       "缺少销售订单(XSDD)：行内无该列且工作表无「销售订单」锚",
                                       _row_dict(row, full_map)))
            continue

        title = cleaner.clean_str(gvh("数据标题"))
        m = _BXD_RX.search(title or "")
        bxd_no = _clip(m.group(0) if m
                       else cleaner.clean_str(gvh("单号", "费用单号", "报销单号")))
        line_no = cleaner.parse_int(gv("报销明细.序号", "序号"))
        person = _clip(cleaner.clean_str(gvh("报销人员", "申请人")))
        reason = cleaner.clean_str(gvh("支出事由", "报销主题"))

        raw_line = cleaner.clean_str(gv("报销明细.数据ID(不可修改)", "报销明细.数据ID"))
        if not raw_line:
            if bxd_no and line_no is not None:
                ck = (xsdd, bxd_no, line_no)
                if ck in composite_seen:
                    res.errors.append(ErrorRec(
                        row_no, "duplicate_key",
                        f"同一文件内 单号+序号 重复（{bxd_no}#{line_no}），请改序号或删重复行",
                        _row_dict(row, full_map)))
                    continue
                composite_seen.add(ck)
                # 合同域后缀：手填单号自由文本，跨合同同名不该互撞；≤80 字符列宽内
                scope = hashlib.sha1(xsdd.encode("utf-8")).hexdigest()[:8]
                raw_line = f"{bxd_no[:40]}#{line_no}@{scope}"
            else:
                basis = "|".join([xsdd, expense_date.isoformat(), str(amount),
                                  reason or "", person or ""])
                digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:36]
                dup = content_seen.get(digest, 0)
                content_seen[digest] = dup + 1
                raw_line = f"EXP:{digest}#{dup}"    # 内容派生键（§17.4）

        res.lines.append({
            "raw_line_id": raw_line, "bxd_no": bxd_no, "line_no": line_no,
            "data_status": _clip(cleaner.clean_str(gvh("流程状态")), 16)
                           or config.MAINT_EXPENSE_ACTIVE_STATUS,
            "expense_date": expense_date,
            "person": person,
            "expense_type": _clip(cleaner.clean_str(gvh("报销类别", "费用类型"))),
            "fee_category": _clip(cleaner.clean_str(gv("报销明细.费用分类", "费用分类"))),
            "reason": reason,
            "linked_sales_order_no": xsdd,
            "amount": amount,
            "amount_ex_tax": amount_ex_tax,
            "amount_inc_tax": amount_inc_tax,
            "tax_basis": tax_basis,
            "tax_rate_used": tax_policy.TAX_RATE,
        })
    res.rows_inactive = sum(
        1 for r in res.lines
        if r["data_status"] != config.MAINT_EXPENSE_ACTIVE_STATUS
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


def transform(df: pd.DataFrame, file_type: str, anchor: str | None = None) -> TransformResult:
    """anchor：报销页归集锚（§17.3 页级 XSDD），仅 expense 使用。"""
    if file_type == mapping.INVENTORY:
        return _transform_inventory(df)
    if file_type == mapping.MAINTENANCE:
        return _transform_maintenance(df)
    if file_type == mapping.EXPENSE:
        return _transform_expense(df, anchor)
    return _transform_orders(df, file_type)
