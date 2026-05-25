"""中文列名 → 内部字段映射，文件识别特征，ffill 头字段（§4）。

列名在 reader 中已 strip。映射 key 用真实导出的完整中文列名（含 (必填)/# 等后缀）。
"""

PURCHASE = "purchase"
SALES = "sales"
INVENTORY = "inventory"
INQUIRY = "inquiry"

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

MAPPINGS = {
    PURCHASE: {"head": PURCHASE_HEAD, "line": PURCHASE_LINE},
    SALES: {"head": SALES_HEAD, "line": SALES_LINE},
    INVENTORY: {"head": {}, "line": INVENTORY_MAP},
}

# ffill 的头字段（原始中文列名）—— 库存无
FFILL_COLS = {
    PURCHASE: list(PURCHASE_HEAD.keys()),
    SALES: list(SALES_HEAD.keys()),
    INVENTORY: [],
}


def detect_file_type(cols: list[str]) -> str | None:
    """按 §4.1 特征列识别文件类型；识别不出返回 None。"""
    colset = set(cols)
    if "采购单号(必填)" in colset or any("采购产品" in c for c in cols):
        return PURCHASE
    if "订单编号(必填)" in colset and "业务类型#" in colset:
        return SALES
    if "产品库存ID" in colset or ("库存数量" in colset and "产品名称(PN)" in colset):
        return INVENTORY
    return None
