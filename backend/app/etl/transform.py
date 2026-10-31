"""行级清洗 + 校验：原始 DataFrame → 干净记录 + 错误 + 行级异常标记（§6.8/§11）。

原则：坏行隔离、好行照常。每个明细行独立校验，错误进 errors，不阻断其它行。
订单头从同一行（ffill 后头字段已补齐）解析，按 raw_order_id 首次出现去重。
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone

import pandas as pd

from app import config, tax_policy
from app.business_time import BUSINESS_TZ
from app.etl import anomaly, cleaner, mapping
from app.etl.maintenance_addresses import AddressPatch, collect_addresses
from app.services.maintenance_expense_integrity import content_key_digest

# 非"硬错误"的软标记类型：不计入 fact_rows_error（见 loader），只在错误列表里以"可忽略"展示。
# bad_reconciliation_amount / bad_source_modified_time（#396 S10，2026-10-18）：行/单
# 照常入库、仅对账区字段置 NULL——是**已入库行上的警告**，不是失败行；计入
# fact_rows_error 会让报告把成功导入误报成有错行。行级计数单列在
# loader counts 的 reconciliation_value_warnings，明细仍在错误列表逐条上报。
SOFT_ERROR_TYPES = frozenset({"empty_pn_inactive", "missing_date_in_progress",
                              "maintenance_address_conflict",
                              "reconciliation_head_conflict",
                              "bad_reconciliation_amount",
                              "bad_source_modified_time"})


@dataclass
class ErrorRec:
    row_no: int
    error_type: str
    error_detail: str
    raw_row: dict
    # 被丢弃行的**业务身份**（已过 gvh 头行继承、已归一为 date/Decimal/int）。
    # raw_row 走 _row_dict 只读本行原始单元格：延续行几乎全空，§17.3 宽松列
    # （单号/序号/报销金额）也不在 full_map 里，两者都读不出可比对的身份。
    # 修复模式的作废豁免（loader._void_exempt_index）只认这个字段，不认 raw_row。
    identity: dict | None = None


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
    # 报销页逐页的页级锚（无锚为 None）。修复模式删除侧只在「每页都有锚」时武装
    # （expense_void.VoidInputs.anchored）；多页合并时由 pipeline 逐页追加。
    expense_anchors: list = field(default_factory=list)
    # WBDD 专用（plan v1.3 M1-2/M1-3）：
    rows_display_issue: int = 0        # 展示补全列坏值（旗标非是/否、日期/数量解析失败）计数，不阻断行
    headless_order_ids: list = field(default_factory=list)  # 「有单头、无明细」订单（保留入库）
    maintenance_address_patches: dict[str, AddressPatch] = field(default_factory=dict)
    # D-0050：本文件实际提供的源字段（内部键），按 head/line 分层，由 reader
    # 规范化后的 df.columns 生成（不看值是否非空、不依赖 df.attrs）。loader 只
    # 更新这里列出的 WBDD 字段：源列缺失保留旧值，源列存在但空白仍按既有规则
    # 覆盖；采购/销售继续走各自 D-41 覆盖规则，不消费此掩码。None（旧调用方
    # 直接构造 TransformResult）→ loader 保持全量更新；空 frozenset 表示该层
    # 源列全部缺失，与 None 严格区分。
    present_fields: dict[str, frozenset[str]] | None = None
    # D-40：销售文件是否带对账区列（至少「已对账金额」列存在）。旧格式文件为
    # False → loader 不写快照（对账区列一个都没有时不产生 NULL 快照）。
    has_reconciliation_columns: bool = False


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


# D-40 对账区单头字段（订单级累计快照，同一订单每行重复出现）
_RECON_MONEY_FIELDS = ("reconciled_amount", "pending_amount", "return_reconciled_amount")
# D-41 含税三件套：与对账区同为销售单头观测，同一「首个非空 + 不一致告警」
# 规则按行聚合（明细校验失败行的值同样属于该订单，D-40 PR-3 口径）。
_TAX_TRIO_FIELDS = ("amount_inc_tax", "is_tax_inclusive", "tax_amount")
_RECON_FIELD_LABELS = {
    "reconciled_amount": "已对账金额",
    "pending_amount": "待对账金额",
    "return_reconciled_amount": "退货已对账金额",
    "source_modified_at": "修改时间",
    "amount_inc_tax": "订单金额",
    "amount_ex_tax": "不含税金额",
    "tax_rate": "税率",
    "is_tax_inclusive": "是否含税",
    "tax_amount": "税金",
}

# D-0050（用户确认的口径）：采购/销售单头金额或税率——非空值无法解析，或同一
# 字段在一张单的全部源行里出现不同的有效值——该单本次不导入，保留库内旧事实；
# 其他合法单据继续。后出现的合法值不能豁免早先的非法值；也绝不引入
# 「不含税×税率=税金」等算术等式或舍入阈值阻断（票面差异按源值保留）。
_HEAD_ECONOMIC_LABELS = {
    "amount_ex_tax": "不含税金额",
    "amount_inc_tax": "申报金额",
    "tax_amount": "税金",
    "tax_rate": "税率",
}


def _head_economic_label(file_type: str, field_name: str) -> str:
    if field_name == "amount_inc_tax":
        return "采购金额" if file_type == mapping.PURCHASE else "订单金额"
    return _HEAD_ECONOMIC_LABELS[field_name]


def _fmt_head_value(value):
    """冲突告警里的值渲染：布尔按业务语义（含税/不含税），其余原样。"""
    if value is True:
        return "含税"
    if value is False:
        return "不含税"
    return value


def _observe_head_value(slot: dict, field: str, value, row_no: int) -> None:
    """登记单头金额/税率的有效观测；同字段出现不同有效值 → 记为冲突待整单拒绝。

    比较用解析后的值（Decimal），"100" 与 "100.00" 不算冲突。首个观测行号
    记录在 _row_by_field，用于「税率缺失时只允许同一源行拼接」。
    """
    row_by_field = slot.setdefault("_row_by_field", {})
    if field not in slot:
        slot[field] = value
        row_by_field[field] = row_no
        return
    if slot[field] != value:
        slot.setdefault("_conflicts", []).append({
            "field": field,
            "row_no": row_no,
            "first_row_no": row_by_field.get(field),
            "first": slot[field],
            "value": value,
        })


def _collect_head_economics(res, row, row_no: int, raw_order_id: str,
                            inv_head: dict, head_obs: dict) -> None:
    """逐行收集采购/销售单头经济字段观测（D-0050）。

    调用于父单身份校验之后、明细身份（明细 ID）与明细级校验（PN/数量/单价）
    闸门之前：缺明细 ID 或明细校验失败的行同样携带单头值，不能因坏行静默
    漏掉非法观测。非空非法值按源行留痕（即使后续行有合法值也不豁免）；
    跨行不同值按字段留痕，最终由 _finalize_head_economics 整单拒绝。
    """
    slot = head_obs.setdefault(raw_order_id, {})

    def g(internal):
        col = inv_head.get(internal)
        return row.get(col) if col is not None else None

    for field_name in ("amount_ex_tax", "amount_inc_tax", "tax_amount"):
        raw = g(field_name)
        try:
            value = cleaner.parse_money(raw)
        except ValueError as exc:
            slot.setdefault("_invalid", []).append({
                "field": field_name, "row_no": row_no,
                "raw": str(raw), "detail": str(exc),
            })
            continue
        if value is not None:
            _observe_head_value(slot, field_name, value, row_no)
    raw_rate = g("tax_rate")
    try:
        rate = cleaner.parse_rate(raw_rate)
    except ValueError as exc:
        slot.setdefault("_invalid", []).append({
            "field": "tax_rate", "row_no": row_no,
            "raw": str(raw_rate), "detail": str(exc),
        })
    else:
        if rate is not None:
            _observe_head_value(slot, "tax_rate", rate, row_no)


def _merge_recon_value(res, slot, raw_order_id, field, value, row_no) -> None:
    """首个非空原始值生效（键在 slot 即视为已占用——坏值锁定 NULL，不被后续
    行的好值悄悄顶掉）；后续行解析值不同 → 软警告留痕（可忽略）。

    比较用解析后的值（Decimal/datetime/bool），"100" 与 "100.00" 不算冲突。"""
    if field not in slot:
        slot[field] = value
        # D-0050：记录首个有效观测所在数据行，用于禁止跨行拼接后才推导税率。
        slot.setdefault("_row_by_field", {})[field] = row_no
        return
    prev = slot[field]
    if prev != value:
        res.errors.append(ErrorRec(
            row_no, "reconciliation_head_conflict",
            f"订单 {raw_order_id} 的{_RECON_FIELD_LABELS[field]}多行取值不一致："
            f"保留首个非空值（{_fmt_head_value(prev) if prev is not None else '坏值已置空'}），"
            f"忽略本行 {_fmt_head_value(value)}（可忽略）",
            {field: str(value)}))


def _collect_sales_reconciliation(res, row, row_no, raw_order_id, inv_head,
                                  head_obs) -> None:
    """按行收集销售单头观测值：D-40 对账区四列 + D-41 含税三件套。

    调用于父单身份校验之后、明细身份（明细 ID）与明细级校验（PN/数量/单价）
    闸门**之前**——缺明细 ID 或明细校验失败的行同样携带该订单的单头值，
    绝不静默跳过（Codex 审查 P1）。
    常规布局「值在首行、下方存活行留空」由 reader 按单向下 ffill 兜住；本
    收集点再兜住 ffill 兜不住的形态（值只在末行坏行上、坏行值与存活行不同）。

    对账区（仅新格式文件）：坏值置 NULL 记行级错误（bad_reconciliation_amount /
    bad_source_modified_time，逐行上报不阻断），首个非空是坏值 → 锁定 NULL。
    单头金额/税率（新旧格式都有）由 _collect_head_economics 按同一调用时点
    逐行收集：非空非法或同字段跨行不同有效值 → 整单拒绝（D-0050）。
    是否含税不是金额/税率字段，保持既有「首个非空 + 软告警」口径。
    全部行校验失败的订单不并入订单头（订单不入库，快照/头字段无从落），但
    已上报的行级错误仍然可见。
    比较用解析后的值（Decimal/datetime），"100" 与 "100.00" 不算冲突。
    """
    slot = head_obs.setdefault(raw_order_id, {})

    def g(internal):
        col = inv_head.get(internal)
        return row.get(col) if col is not None else None

    tax_inclusive = cleaner.parse_tax_inclusive(g("is_tax_inclusive"))
    if tax_inclusive is not None:
        _merge_recon_value(res, slot, raw_order_id, "is_tax_inclusive",
                           tax_inclusive, row_no)

    if not res.has_reconciliation_columns:
        return
    for field_name in _RECON_MONEY_FIELDS:
        try:
            value = cleaner.parse_money(g(field_name))
        except ValueError as exc:
            res.errors.append(ErrorRec(
                row_no, "bad_reconciliation_amount",
                f"{_RECON_FIELD_LABELS[field_name]}{exc}（该单此列置空，不阻断导入）",
                {field_name: str(g(field_name))}))
            if field_name not in slot:
                slot[field_name] = None   # 首个非空值是坏值 → 锁定 NULL
            continue
        if value is not None:
            _merge_recon_value(res, slot, raw_order_id, field_name, value, row_no)
    try:
        modified = cleaner.parse_datetime(g("source_modified_at"))
    except ValueError as exc:
        res.errors.append(ErrorRec(
            row_no, "bad_source_modified_time",
            f"{exc}（该单修改时间置空，不阻断导入）",
            {"source_modified_at": str(g("source_modified_at"))}))
        if "source_modified_at" not in slot:
            slot["source_modified_at"] = None
    else:
        if modified is not None:
            # 氚云导出是北京时间：naive 视为北京时间、aware 原样，折算成 UTC 交给
            # 快照 canonical_time/落库——导入与回填共用本路径，哈希口径一致。
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=BUSINESS_TZ)
            _merge_recon_value(res, slot, raw_order_id, "source_modified_at",
                               modified.astimezone(timezone.utc), row_no)


def _finalize_head_economics(res, file_type: str, raw_order_id: str,
                             values: dict, head: dict) -> bool:
    """按整单观测收口采购/销售单头金额与税率；返回是否需要整单拒绝。

    D-0050（用户确认）：任一源行出现非空无法解析的金额/税率，或同一字段在
    不同源行出现不同的有效值 → 该单本次不导入（不写头/行/快照/合同），
    保留库内旧事实；回执逐字段报告源行与原因。后出现的合法值不能豁免早先
    的非法值。不做任何算术等式/舍入阈值判断，票面差异按源值保留。
    税率只在显式值缺失且不含税额与税金来自同一数据行时才推导，禁止跨行
    拼接自造税率。
    """
    blocked = False
    for rec in values.get("_invalid", []):
        label = _head_economic_label(file_type, rec["field"])
        res.errors.append(ErrorRec(
            rec["row_no"], "invalid_head_value",
            f"{label}「{rec['raw']}」无法解析（{rec['detail']}）；"
            "该单本次不导入，保留库内已有事实，请修正后重传",
            {rec["field"]: rec["raw"]}))
        blocked = True
    for rec in values.get("_conflicts", []):
        label = _head_economic_label(file_type, rec["field"])
        res.errors.append(ErrorRec(
            rec["row_no"], "head_value_conflict",
            f"{label}同单多行取值不一致：第{rec['first_row_no']}行"
            f"{_fmt_head_value(rec['first'])}，第{rec['row_no']}行"
            f"{_fmt_head_value(rec['value'])}；该单本次不导入，保留库内已有事实",
            {rec["field"]: str(rec["value"])}))
        blocked = True
    if blocked:
        return True

    head["amount_ex_tax"] = values.get("amount_ex_tax")
    head["amount_inc_tax"] = values.get("amount_inc_tax")
    head["tax_amount"] = values.get("tax_amount")
    explicit_rate = values.get("tax_rate")
    if explicit_rate is not None:
        head["tax_rate"] = explicit_rate
    else:
        row_by_field = values.get("_row_by_field", {})
        same_row_pair = (
            values.get("amount_ex_tax") is not None
            and values.get("tax_amount") is not None
            and row_by_field.get("amount_ex_tax") == row_by_field.get("tax_amount")
        )
        head["tax_rate"] = (
            cleaner.derive_tax_rate(
                values.get("amount_ex_tax"), values.get("tax_amount"))
            if same_row_pair else None
        )
    return False


def _transform_orders(df: pd.DataFrame, file_type: str) -> TransformResult:
    res = TransformResult(file_type=file_type)
    head_map = mapping.MAPPINGS[file_type]["head"]
    line_map = mapping.MAPPINGS[file_type]["line"]
    inv_head = {v: k for k, v in head_map.items()}   # internal -> chinese
    inv_line = {v: k for k, v in line_map.items()}
    res.rows_total = len(df)
    # D-0050：按 reader 规范化后的实际列判定字段是否由本文件提供。采购/销售
    # loader 不消费此掩码（各自 D-41 规则不变），仅随结果返回供回执/测试核对。
    res.present_fields = {
        "head": frozenset(
            internal for source, internal in head_map.items()
            if source in df.columns
        ),
        "line": frozenset(
            internal for source, internal in line_map.items()
            if source in df.columns
        ),
    }
    # D-40：对账区列存在性 = 至少「已对账金额」列；按订单聚合的单头观测值
    # （D-40 对账区 + D-41 含税三件套 + D-0050 金额/税率，见收集函数）
    head_obs_by_order: dict = {}
    first_row_by_order: dict[str, int] = {}
    pending_heads: dict[str, dict] = {}
    invalid_order_ids: set[str] = set()
    res.has_reconciliation_columns = (
        file_type == mapping.SALES and inv_head.get("reconciled_amount") in df.columns
    )

    for idx, row in df.iterrows():
        row_no = int(idx) + 1
        full_map = {**head_map, **line_map}

        raw_order_id = cleaner.clean_str(row.get(inv_head["raw_order_id"]))
        raw_line_id = cleaner.clean_str(row.get(inv_line["raw_line_id"]))

        if not raw_order_id:
            # 无父单身份：只报本行错误，观测值不得归到任何订单。
            res.errors.append(ErrorRec(row_no, "missing_raw_id",
                                       "缺少订单/明细数据ID", _row_dict(row, full_map)))
            continue

        # D-0050：父单可识别的源行先参与金额/税率观测，再走明细身份门禁——
        # 缺明细ID不能成为跳过非法/冲突单头值的理由（用户确认的第五项：同一单
        # 的所有可识别父单源行）。D-40/D-41 同为按行观测，随同前移。
        _collect_head_economics(res, row, row_no, raw_order_id,
                                inv_head, head_obs_by_order)
        if file_type == mapping.SALES:
            _collect_sales_reconciliation(res, row, row_no, raw_order_id,
                                          inv_head, head_obs_by_order)

        if not raw_line_id:
            res.errors.append(ErrorRec(row_no, "missing_raw_id",
                                       "缺少订单/明细数据ID", _row_dict(row, full_map)))
            continue

        first_row_by_order.setdefault(raw_order_id, row_no)

        # 单头在明细校验前解析并留错：即使后续明细全部被拒，非空非法的单头
        # 金额/税率也不会静默消失。订单只在出现首个有效明细时进入写入计划。
        if raw_order_id not in pending_heads:
            pending_heads[raw_order_id] = _build_head(
                row, file_type, inv_head, row_no, res)

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

        # 订单头（首次有效明细出现时入计划；此前已解析并留错）
        if raw_order_id not in res.orders:
            head = pending_heads[raw_order_id]
            res.orders[raw_order_id] = head
            if head.get("data_status") and head["data_status"] != "已生效":
                pass  # inactive 计数在 line 级更直观，见下
    # inactive 行数：按行所属订单状态统计
    res.rows_inactive = sum(
        1 for ln in res.lines
        if res.orders.get(ln["_order_raw_id"], {}).get("data_status") not in (None, "已生效")
    )
    # 单头观测聚合值并回订单头（只并给本文件中至少一行有效的订单——其余订单
    # 不入库，快照/头字段都无从落，收集时上报的行级错误仍可见）。对账区四个键
    # 补齐为 None：列存在但整单为空/坏值时键仍在，值为 NULL——下游（快照 NULL
    # 语义）与测试断言都按此口径。含税三件套用聚合值覆盖头行原值；是否含税
    # 沿用「首个非空 + 软告警」。D-0050：金额/税率任一非法或同字段跨行不同值
    # → 整单拒绝；税率显式值优先，缺失时只在不含税额与税金来自同一数据行时推导。
    for raw_id, values in head_obs_by_order.items():
        head = res.orders.get(raw_id)
        if head is None:
            # 全部明细行被拒的订单不入库，但其非法/冲突单头值仍须逐字段可见。
            _finalize_head_economics(res, file_type, raw_id, values, {})
            continue
        if file_type == mapping.SALES:
            head["is_tax_inclusive"] = values.get("is_tax_inclusive")
        if file_type == mapping.SALES and res.has_reconciliation_columns:
            for field_name in (*_RECON_MONEY_FIELDS, "source_modified_at"):
                head[field_name] = values.get(field_name)
        if _finalize_head_economics(res, file_type, raw_id, values, head):
            invalid_order_ids.add(raw_id)

    if invalid_order_ids:
        # 单据级阻断：单头非法/不一致时该单的头和行都不进入入库计划，避免
        # 只写半张单或用 NULL 覆盖既有票面原值；错误已逐字段留痕。
        for raw_id in invalid_order_ids:
            res.orders.pop(raw_id, None)
        res.lines = [
            line for line in res.lines
            if line["_order_raw_id"] not in invalid_order_ids
        ]
    return res


def _build_head(row, file_type: str, inv_head: dict, row_no: int,
                res) -> dict:
    """构建单头中与金额/税率无关的字段（D-0050）。

    金额与税率由 _collect_head_economics 逐行观测、_finalize_head_economics
    整单收口；这里只负责日期、状态、供应商/客户等非经济字段，避免先按首行
    吞错或提前推导税率。
    """
    def g(internal):
        return row.get(inv_head[internal]) if internal in inv_head else None

    try:
        order_date = cleaner.parse_date(g("order_date"))
    except ValueError as exc:
        order_date = None
        res.errors.append(ErrorRec(row_no, "bad_date", str(exc), {"order_date": str(g("order_date"))}))

    head = {
        "raw_order_id": cleaner.clean_str(g("raw_order_id")),
        "order_no": cleaner.clean_str(g("order_no")),
        "order_date": order_date,
        "tax_rate": None,
        "amount_ex_tax": None,
        "data_status": cleaner.clean_str(g("data_status")),
    }
    if file_type == mapping.PURCHASE:
        name_raw, name_norm = cleaner.normalize_supplier_name(g("supplier_name"))
        supplier_type = cleaner.clean_str(g("supplier_type"))
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
            "tax_amount": None,
            "amount_inc_tax": None,
        })
        return head

    # 销售：经济字段由整单观测收口；是否含税保持首个非空 + 软告警口径。
    head.update({
        "salesperson": cleaner.clean_str(g("salesperson")),
        "customer_name": cleaner.clean_str(g("customer_name")),
        "customer_type": cleaner.clean_str(g("customer_type")),
        "customer_source": cleaner.clean_str(g("customer_source")),
        "customer_city": cleaner.clean_str(g("customer_city")),
        "maintenance_business": cleaner.clean_str(g("maintenance_business")),
        "business_type": cleaner.clean_str(g("business_type")),
        "maintenance_project_name": cleaner.clean_str(
            g("maintenance_project_name")
        ),
        "maintenance_project_manager": cleaner.clean_str(
            g("maintenance_project_manager")
        ),
        "maintenance_period_from": g("maintenance_period_from"),
        "maintenance_period_to": g("maintenance_period_to"),
        "warehouse": cleaner.clean_str(g("warehouse")),
        "is_tax_inclusive": None,
        "tax_amount": None,
        "amount_inc_tax": None,
    })
    return head


# 项目名规范化：剥「预交付-」前缀（聚合键；原值另存 project_raw 供追溯）。
# 单一事实源在 services/project_names（plan v1.3 M2-3 统一）；此处保留别名兼容既有引用。
from app.services.project_names import PRE_DELIVERY_PREFIX as _PROJECT_PREFIX  # noqa: E402

# ---- WBDD 展示补全列解析规格（plan v1.3 §3；只展示，坏值不阻断行，计 rows_display_issue）----
# 值 = (解析类别, 字符列截断长度 None=Text 不截断)。字段集与 mapping.MAINTENANCE_*_DISPLAY 一一对应。
_WBDD_HEAD_DISPLAY_SPEC: dict[str, tuple[str, int | None]] = {
    "head_demand_qty": ("qty", None), "head_purchase_qty": ("qty", None),
    "head_shipped_qty": ("qty", None), "head_returned_qty": ("qty", None),
    "maintainer_raw": ("str", None), "work_order_no": ("str", None),
    "created_by_raw": ("str", None), "purchaser_raw": ("str", None),
    "purchaser2_raw": ("str", None), "project_manager_raw": ("str", None),
    "project_manager_staff_raw": ("str", None), "co_salesperson_raw": ("str", None),
    "partner_raw": ("str", None), "sales_dept_raw": ("str", None),
    "warehouse_keeper_raw": ("str", None), "storage_center": ("str", None),
    "warehouse_raw": ("str", None), "change_warehouse_flag": ("bool", None),
    "change_warehouse": ("str", None), "change_warehouse_handler": ("str", None),
    "warehouse_handler": ("str", None), "supply_deadline": ("date", None),
    "delivery_address_option": ("str", None), "receiver": ("str", None),
    "receiver_phone": ("str", None), "receiver_address": ("str", None),
    "express_no": ("str", None), "express_no2": ("str", None),
    "image_urls": ("str", None), "attachments": ("str", None),
    "whole_machine_check": ("str", None), "accept_generic_flag": ("bool", None),
    "created_at_raw": ("str", None), "modified_at_raw": ("str", None),
}
_WBDD_LINE_DISPLAY_SPEC: dict[str, tuple[str, int | None]] = {
    "purchase_qty": ("qty", None), "change_warehouse_purchase_qty": ("qty", None),
    "purchased_qty": ("qty", None), "pending_purchase_qty": ("qty", None),
    "direct_ship_qty": ("qty", None), "warehouse_need_qty": ("qty", None),
    "warehouse_shipped_qty": ("qty", None), "supplied_qty": ("qty", None),
    "pending_supply_qty": ("qty", None), "returned_qty": ("qty", None),
    "pending_return_qty": ("qty", None), "consumed_qty": ("qty", None),
    "demand_pending_return_qty": ("qty", None), "return_old_part": ("str", None),
    "whole_or_part": ("str", None), "whole_machine_purchase_part": ("str", None),
    "whole_machine_part_purchased": ("str", None), "purchase_note": ("str", None),
    "line_note": ("str", None), "line_image_urls": ("str", None),
    "warehouse_stock_raw": ("str", None), "adjust_warehouse_flag": ("bool", None),
    "adjust_warehouse": ("str", None), "adjust_storage_center": ("str", None),
    "adjust_keeper": ("str", None), "ship_warehouse": ("str", None),
    "ship_warehouse_object_id": ("str", None), "ship_stock": ("qty", None),
}


def _wbdd_display_extras(row, inv_map: dict, spec: dict, res: TransformResult) -> dict:
    """按规格解析展示补全列：坏值 → NULL＋rows_display_issue+=1，绝不阻断行（只展示用途）。

    例外（D-0050）：`adjust_warehouse_flag` 源列真实语义可能是仓库名；非是/否
    文本不再计坏布尔，只保留在 `adjust_warehouse_raw`，兼容布尔列置 NULL。
    """
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
            elif field_name == "adjust_warehouse_flag":
                # D-0050：该源列真实语义是「个别调整发货仓」，可能是仓库名。
                # 只有明确的是/否进兼容布尔列；其余合法文本保留在
                # adjust_warehouse_raw，不再当作坏布尔累计展示告警。
                out[field_name] = None
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
    # D-0050：个别调整发货仓的源单元格可能是仓库名，布尔列只承载明确的
    # 是/否；原文另存 adjust_warehouse_raw，不猜主数据、不把仓库名当布尔。
    adjust_source = inv_map.get("adjust_warehouse_flag")
    if "adjust_warehouse_flag" in spec and adjust_source:
        out["adjust_warehouse_raw"] = cleaner.clean_str(row.get(adjust_source))
    return out
# 预建单容忍窗：制单日期晚于导入日超过此天数 → 记 future_date 异常（不拦截，实测存在预建单）
_FUTURE_TOLERANCE_DAYS = 30

# D-0050：核心身份/受限列不做静默截断；超长时按字段/行明确报错。展示文本列已
# 迁 Text，不受这些上限约束。上限与 SQLAlchemy 模型一致。
_WBDD_ORDER_TEXT_LIMITS = {
    "raw_order_id": (64, "维保单数据ID"),
    "order_no": (64, "需求单号"),
    "linked_sales_order_no": (64, "销售订单"),
    "project_raw": (256, "项目名"),
    "customer_name": (256, "客户名称"),
    "end_customer": (256, "最终客户名"),
    "demand_type": (16, "需求类型"),
    "business_type": (16, "业务类型"),
    "salesperson": (64, "销售人员"),
    "warehouse": (64, "出库仓库"),
    "data_status": (16, "数据状态"),
}
_WBDD_LINE_TEXT_LIMITS = {
    "raw_line_id": (64, "需求明细数据ID"),
    "pn_raw": (256, "需供货产品"),
    "pn_std": (128, "归一产品名"),
}


def _wbdd_too_long(res: TransformResult, row_no: int, field_name: str,
                   value: str | None, limit: int, label: str) -> bool:
    if value is None or len(value) <= limit:
        return False
    res.errors.append(ErrorRec(
        row_no, "field_too_long",
        f"{label}超过模型上限 {limit} 字符（实际 {len(value)}），"
        "本次不导入该字段所在行；请缩短后重传",
        {field_name: value}))
    return True


def _transform_maintenance(df: pd.DataFrame) -> TransformResult:
    """维保出库（WBDD）：无任何价格列，成本由 maintenance_cost.recompute 回填。"""
    res = TransformResult(file_type=mapping.MAINTENANCE)
    head_map = mapping.MAINTENANCE_HEAD
    line_map = mapping.MAINTENANCE_LINE
    inv_head = {v: k for k, v in head_map.items()}
    inv_line = {v: k for k, v in line_map.items()}
    res.rows_total = len(df)
    res.present_fields = {
        "head": frozenset(
            internal for source, internal in head_map.items()
            if source in df.columns
        ),
        "line": frozenset(
            internal for source, internal in line_map.items()
            if source in df.columns
        ),
    }
    future_cutoff = date.today() + timedelta(days=_FUTURE_TOLERANCE_DAYS)
    bad_order_ids: set[str] = set()

    def head_text(current_row, field_name: str) -> str | None:
        source = inv_head.get(field_name)
        return cleaner.clean_str(current_row.get(source)) if source else None

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

        # D-0050：核心单头字段不做静默截断；任一超长 → 该单本次不导入（头/行
        # 都不进入写入计划），错误逐字段可见。已注册的头/行在末尾统一剔除。
        order_too_long = False
        for field_name, (limit, label) in _WBDD_ORDER_TEXT_LIMITS.items():
            if _wbdd_too_long(
                res, row_no, field_name, head_text(row, field_name), limit, label
            ):
                order_too_long = True
        if order_too_long:
            bad_order_ids.add(raw_order_id)
            continue
        if raw_line_id and _wbdd_too_long(
            res, row_no, "raw_line_id", raw_line_id,
            *_WBDD_LINE_TEXT_LIMITS["raw_line_id"],
        ):
            _register_wbdd_order(res, row, row_no, inv_head, future_cutoff)
            continue
        # 「有单头、无明细」订单保留（plan v1.3 M1-2）：单头照常注册入库、明细 0 行，
        # 不再记 missing_raw_id 错误。ffill 使空白间隔行携带上一单头值——它们落到
        # 已注册订单的 no-op 分支，不会虚增订单。
        if not raw_line_id:
            _register_wbdd_order(res, row, row_no, inv_head, future_cutoff)
            continue

        # D-27: the source header (including cancellation) exists independently
        # of whether its child has a valid PN/quantity. Never silently retain a
        # previous header status just because every child failed validation.
        _register_wbdd_order(res, row, row_no, inv_head, future_cutoff)

        pn_std, pn_raw, needs_review = cleaner.standardize_pn(row.get(inv_line["pn_raw"]))
        if pn_std is None:
            res.errors.append(_empty_pn_error(
                row_no, cleaner.clean_str(row.get(inv_head["data_status"])),
                cleaner.clean_str(row.get(inv_head["order_no"])), _row_dict(row, full_map)))
            continue
        pn_too_long = False
        if _wbdd_too_long(
            res, row_no, "pn_raw", pn_raw,
            *_WBDD_LINE_TEXT_LIMITS["pn_raw"],
        ):
            pn_too_long = True
        if _wbdd_too_long(
            res, row_no, "pn_std", pn_std,
            *_WBDD_LINE_TEXT_LIMITS["pn_std"],
        ):
            pn_too_long = True
        if pn_too_long:
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

    res.maintenance_address_patches = collect_addresses(df)
    for raw_id, head in res.orders.items():
        patch = res.maintenance_address_patches.get(raw_id, AddressPatch())
        head["receiver_address"] = patch.value
        if patch.action == "conflict":
            res.errors.append(ErrorRec(
                patch.rows[0], "maintenance_address_conflict", patch.detail,
                {"raw_order_id": raw_id, "order_no": head["order_no"],
                 "data_rows": list(patch.rows)},
            ))

    if bad_order_ids:
        for raw_id in bad_order_ids:
            res.orders.pop(raw_id, None)
            res.maintenance_address_patches.pop(raw_id, None)
        res.lines = [
            line for line in res.lines
            if line["_order_raw_id"] not in bad_order_ids
        ]
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


# D-0050：报销明细的身份/受限文本列不再静默截断；超过模型列宽即整批拒绝，
# 回执带源数据行、字段、实际长度与上限。展示文本列（reason 等）已是 Text，不在此表。
_EXPENSE_TEXT_LIMITS = {
    "linked_sales_order_no": (64, "销售订单"),
    "bxd_no": (64, "单号"),
    "person": (64, "报销人员"),
    "expense_type": (64, "报销类别"),
    "fee_category": (64, "费用分类"),
    "data_status": (16, "流程状态"),
}


def _expense_text(value: str | None, field_name: str, row_no: int) -> str | None:
    """报销明细受限文本：合法值原样保留，超长整批拒绝（不截断、不静默）。"""
    limit, label = _EXPENSE_TEXT_LIMITS[field_name]
    if value is None or len(value) <= limit:
        return value
    from app.etl.reader import ReaderError     # 延迟导入：reader 不依赖 transform
    raise ReaderError(
        f"报销明细第 {row_no} 行「{label}」源文本长度 {len(value)} 超过字段上限 "
        f"{limit} 字符，整批未导入；请缩短后重传",
        code="expense_field_too_long",
    )


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
    res.expense_anchors = [anchor]
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

        # 头级字段走 gvh：延续行继承头行值（#168 形态）；行级明细列仍走 gv。
        # D-0050：六个受限源字段先校验长度再使用——超长即整批拒绝，不依赖该行
        # 后续是否 missing_link/撞键，任何静默截断都不许进入业务事实或错误身份。
        xsdd = _expense_text(
            cleaner.clean_str(gvh("维保销售订单", "销售订单")) or anchor,
            "linked_sales_order_no", row_no)

        # 身份提取必须早于 missing_link 判定：修复模式放行这类行后，loader 要靠
        # 这套身份证明「被丢的行不对应任何将被作废的旧行」。此处仍在 raw_line_id/
        # duplicate_key 之前——复合键 ck/scope 依赖 xsdd，那部分不能上移。
        title = cleaner.clean_str(gvh("数据标题"))
        m = _BXD_RX.search(title or "")
        bxd_no = _expense_text(
            m.group(0) if m else cleaner.clean_str(gvh("单号", "费用单号", "报销单号")),
            "bxd_no", row_no)
        line_no = cleaner.parse_int(gv("报销明细.序号", "序号"))
        person = _expense_text(
            cleaner.clean_str(gvh("报销人员", "申请人")), "person", row_no)
        reason = cleaner.clean_str(gvh("支出事由", "报销主题"))
        native_line_id = cleaner.clean_str(gv("报销明细.数据ID(不可修改)",
                                              "报销明细.数据ID"))
        data_status = _expense_text(
            cleaner.clean_str(gvh("流程状态")), "data_status", row_no,
        ) or config.MAINT_EXPENSE_ACTIVE_STATUS
        expense_type = _expense_text(
            cleaner.clean_str(gvh("报销类别", "费用类型")), "expense_type", row_no)
        fee_category = _expense_text(
            cleaner.clean_str(gv("报销明细.费用分类", "费用分类")),
            "fee_category", row_no)

        if not xsdd:
            # expense_date/amount 在此必非空：无金额行已在 rows_skipped_no_data
            # 出口 continue，无日期行已在 missing_date 出口 continue。故内容签名
            # （日期+金额+人员+事由）恒可构造，作废豁免不会退化为「永远零命中」。
            # 身份写进 error_detail：raw_row 在宽松列/延续行形态下金额为 null，
            # 而 batch_detail 与 errors.csv 都只回 detail 不回 raw_row——不写在
            # 这里，1,873 行错误明细对人工对账等于没有。
            tail = " ".join(str(v) for v in (
                expense_date.isoformat(), f"¥{amount}", person or "", reason or "",
            ) if v)
            res.errors.append(ErrorRec(
                row_no, "missing_link",
                f"缺少销售订单(XSDD)：行内无该列且工作表无「销售订单」锚｜{tail}",
                _row_dict(row, full_map),
                identity={
                    "raw_line_id": native_line_id,
                    "bxd_no": bxd_no,
                    "line_no": line_no,
                    "expense_date": expense_date,
                    "amount": amount,
                    "person": person,
                    "reason": reason,
                },
            ))
            continue

        raw_line = native_line_id
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
                digest = content_key_digest(xsdd=xsdd, expense_date=expense_date,
                                            amount=amount, reason=reason, person=person)
                dup = content_seen.get(digest, 0)
                content_seen[digest] = dup + 1
                raw_line = f"EXP:{digest}#{dup}"    # 内容派生键（§17.4）

        res.lines.append({
            "raw_line_id": raw_line, "bxd_no": bxd_no, "line_no": line_no,
            "data_status": data_status,
            "expense_date": expense_date,
            "person": person,
            "expense_type": expense_type,
            "fee_category": fee_category,
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
