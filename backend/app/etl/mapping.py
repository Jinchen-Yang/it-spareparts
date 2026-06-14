"""中文列名 → 内部字段映射，文件识别特征，ffill 头字段（§4）。

列名在 reader 中已 strip。映射 key 用真实导出的完整中文列名（含 (必填)/# 等后缀）。
"""

PURCHASE = "purchase"
SALES = "sales"
INVENTORY = "inventory"
INQUIRY = "inquiry"
STOCK_LEDGER = "stock_ledger"   # 库存出入流水（备件库存流水 / 整机库存流水）

# ---- 采购订单 ----
PURCHASE_HEAD = {
    "采购单号(必填)": "order_no",
    "数据ID(不可修改)": "raw_order_id",
    "采购日期(必填)": "order_date",
    "采购人员(必填)": "purchaser",
    "供应商(必填)": "supplier_name",
    "供应商编码#": "supplier_code",
    "供应商类型": "supplier_type",
    "批量采购(必填)": "source_type_raw",
    "关联订单": "linked_sales_order_no",
    "不含税金额": "amount_ex_tax",
    "税率(必填)": "tax_rate",
    "数据状态": "data_status",
}
PURCHASE_LINE = {
    "明细.数据ID(不可修改)": "raw_line_id",
    "明细.序号": "line_no",
    "明细.产品名称(必填)": "pn_raw",
    "明细.产品描述": "description",
    "明细.品牌": "brand",
    "明细.整机/备件": "machine_or_part",
    "明细.单位": "unit",
    "明细.采购数量(必填)": "qty",
    "明细.单价(必填)": "unit_price",
    "明细.合计金额": "line_amount",
    "明细.最近采购价": "recent_purchase_price",
}

# ---- 销售订单 ----
SALES_HEAD = {
    "订单编号(必填)": "order_no",
    "数据ID(不可修改)": "raw_order_id",
    "订单日期": "order_date",
    "销售人员": "salesperson",
    "客户名称": "customer_name",
    "客户类型": "customer_type",
    "客户来源": "customer_source",
    "客户城市": "customer_city",
    "业务类型#": "business_type",
    "仓库": "warehouse",
    "不含税金额": "amount_ex_tax",
    "税率": "tax_rate",
    "数据状态": "data_status",
}
SALES_LINE = {
    "订单明细.数据ID(不可修改)": "raw_line_id",
    "订单明细.序号": "line_no",
    "订单明细.产品名称": "pn_raw",
    "订单明细.产品描述": "description",
    "订单明细.品牌": "brand",
    "订单明细.产品大类": "category_major",
    "订单明细.产品小类": "category_minor",
    "订单明细.整机/备件": "machine_or_part",
    "订单明细.单位": "unit",
    "订单明细.订单数量": "qty",
    "订单明细.单价": "unit_price",
    "订单明细.金额": "line_amount",
    "订单明细.通用产品": "generic_product",
    "订单明细.发货SN": "serial_numbers",
}

# ---- 产品库存（单实体，无 head/line 之分，无 ffill）----
INVENTORY_MAP = {
    "产品库存ID": "raw_inventory_id",
    "产品名称(PN)": "pn_raw",
    "库存数量": "source_qty",
    "仓库": "warehouse",
    "产品描述": "description",
    "品牌": "brand",
    "整机/备件": "machine_or_part",
    "单位": "unit",
    "通用产品": "generic_product",
    "数据状态": "data_status",
}

# ---- 库存出入流水（备件库存流水 / 整机库存流水，单实体，无 head/line，无 ffill）----
# ⚠️ 列名按源系统常见「库存流水」导出推断。落地时请用真实「备件库存流水」表头校正
#    本 dict 与下方 detect_file_type 的特征列即可——transform/loader 的逻辑与列名无关。
# 数量布局两种都支持：①「入库数量/出库数量」两列；②「数量」单列（带正负）。哪种都行，
# 缺的列在 transform 里取不到即视为空，不报错。
STOCK_LEDGER_MAP = {
    "流水ID": "raw_movement_id",
    "单据日期": "movement_date",
    "单据类型": "doc_type_raw",
    "单据编号": "doc_no",
    "产品名称(PN)": "pn_raw",
    "仓库": "warehouse",
    "对方仓库": "counterpart_warehouse",
    "入库数量": "qty_in",
    "出库数量": "qty_out",
    "数量": "qty_signed",          # 单列带符号布局（+入/-出）
    "结存数量": "snapshot_balance",  # 源系统结存，用于对账
    "单价": "unit_price",
    "整机/备件": "machine_or_part",  # → ledger_kind
}

MAPPINGS = {
    PURCHASE: {"head": PURCHASE_HEAD, "line": PURCHASE_LINE},
    SALES: {"head": SALES_HEAD, "line": SALES_LINE},
    INVENTORY: {"head": {}, "line": INVENTORY_MAP},
    STOCK_LEDGER: {"head": {}, "line": STOCK_LEDGER_MAP},
}

# ffill 的头字段（原始中文列名）—— 库存/流水无
FFILL_COLS = {
    PURCHASE: list(PURCHASE_HEAD.keys()),
    SALES: list(SALES_HEAD.keys()),
    INVENTORY: [],
    STOCK_LEDGER: [],
}

# 单据类型词表：原始中文（子串匹配，按从具体到笼统排序）→ (标准 doc_type, 默认方向, 是否绝对值)。
# 方向：+1 入 / -1 出 / 0 不影响在库；is_absolute=True 仅盘点（回放时重置结存而非累加）。
# 真实方向优先取自「入库/出库数量」列或带符号「数量」列；此处方向仅在无显式数量列时兜底。
# 若你的「盘点」流水记录的是调整增减而非盘点后绝对数，把 stocktake 的 is_absolute 改 False 即可。
MOVEMENT_DOC_TYPES: list[tuple[str, str, int, bool]] = [
    ("调拨入", "transfer_in", 1, False),
    ("调入", "transfer_in", 1, False),
    ("调拨出", "transfer_out", -1, False),
    ("调出", "transfer_out", -1, False),
    ("退货返库", "return_in", 1, False),
    ("退返", "return_in", 1, False),
    ("退库", "return_in", 1, False),
    ("组装入库", "assembly_in", 1, False),
    ("组装领料", "assembly_out", -1, False),
    ("领料", "assembly_out", -1, False),
    ("盘盈", "stocktake_gain", 1, False),
    ("盘亏", "stocktake_loss", -1, False),
    ("盘点", "stocktake", 0, True),
    ("直发", "direct_ship", 0, False),
    ("收货", "receipt", 1, False),
    ("入库", "receipt", 1, False),
    ("退货", "return_in", 1, False),
    ("发货", "issue", -1, False),
    ("出库", "issue", -1, False),
]


def resolve_doc_type(raw: str | None) -> tuple[str, int, bool]:
    """原始单据类型 → (标准 doc_type, 默认方向, is_absolute)。识别不出归 other/方向待数量列决定。"""
    if raw:
        for keyword, doc_type, direction, is_absolute in MOVEMENT_DOC_TYPES:
            if keyword in raw:
                return doc_type, direction, is_absolute
    return "other", 0, False


def detect_file_type(cols: list[str]) -> str | None:
    """按 §4.1 特征列识别文件类型；识别不出返回 None。"""
    colset = set(cols)
    if "采购单号(必填)" in colset or any("采购产品" in c for c in cols):
        return PURCHASE
    if "订单编号(必填)" in colset and "业务类型#" in colset:
        return SALES
    if "产品库存ID" in colset or ("库存数量" in colset and "产品名称(PN)" in colset):
        return INVENTORY
    # 出入流水：有「流水ID」，或「单据类型 + 仓库 + 任一数量列」
    if "流水ID" in colset or (
        "单据类型" in colset and "仓库" in colset
        and bool({"入库数量", "出库数量", "数量"} & colset)
    ):
        return STOCK_LEDGER
    return None
