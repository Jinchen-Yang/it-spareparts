"""中文列名 → 内部字段映射，文件识别特征，ffill 头字段（§4）。

列名在 reader 中已 strip。映射 key 用真实导出的完整中文列名（含 (必填)/# 等后缀）。
氚云不同字段视图/模板会让同一列带或不带 (必填) 注解——canonicalize_columns 负责容差归一。
"""
import re

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
    # 含税口径（实测真实列；税率列常空 → 由 是否含税+税金+不含税金额 反推，见 transform）
    # 注意：是否含税 列名带 (必填) 后缀，税金/采购金额 无后缀（实测导出确认）
    "是否含税(必填)": "is_tax_inclusive",
    "税金": "tax_amount",
    "采购金额": "amount_inc_tax",
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


# ============================================================
# 列名容差归一（§4.2）：氚云不同字段视图/模板会让同一列带或不带 (必填)/(选填)/(不可修改)
# 注解（实测「明细.产品名称」vs「明细.产品名称(必填)」→ 整列读空、全行 empty_pn 整文件 0 入）。
# 归一只剥这类「表单注解」后缀，不动 # 与 (PN) 等语义后缀，规范到 mapping 的键。
# ============================================================
_OPT_SUFFIX = re.compile(r"[（(](?:必填|选填|不可修改)[）)]\s*$")


def _strip_opt(name) -> str:
    """剥尾部表单注解 (必填)/(选填)/(不可修改)（半/全角），其余原样。"""
    return _OPT_SUFFIX.sub("", str(name).strip()).strip()


def _sig_norm(name) -> str:
    """识别签名用：在 _strip_opt 基础上再去 # 与空白（仅判类型，不改数据列名）。"""
    return _strip_opt(name).replace("#", "").strip()


_ALL_KEYS = (set(PURCHASE_HEAD) | set(PURCHASE_LINE) | set(SALES_HEAD)
             | set(SALES_LINE) | set(INVENTORY_MAP))
# 去注解名 → 规范键（mapping 原 key）；同名冲突保留先遇到的（sorted 确定化）。
_CANON_BY_STRIPPED: dict[str, str] = {}
for _k in sorted(_ALL_KEYS):
    _CANON_BY_STRIPPED.setdefault(_strip_opt(_k), _k)


def canonicalize_columns(cols: list[str]) -> list[str]:
    """把导出列名归一到 mapping 规范键。精确命中保持原样；否则按「去注解名」匹配规范键并改名。

    冲突保护：目标规范键已在列里（或本次已用）则不改名，绝不造重复列（如「产品名称」与
    「产品名称#」并存时只认前者、后者原样保留）。对标准导出零影响（列名已是规范键，全走精确分支）。
    """
    present = set(cols)
    out: list[str] = []
    seen: set[str] = set()
    for c in cols:
        if c in _ALL_KEYS:
            canon = c
        else:
            cand = _CANON_BY_STRIPPED.get(_strip_opt(c))
            canon = cand if (cand and cand not in present and cand not in seen) else c
        out.append(canon)
        seen.add(canon)
    return out


def detect_file_type(cols: list[str]) -> str | None:
    """按 §4.1 特征列识别文件类型；识别不出返回 None。

    签名比对走 _sig_norm（容忍 (必填)/# 等注解差异），对非标导出视图更稳。
    """
    sig = {_sig_norm(c) for c in cols}
    if "采购单号" in sig or any("采购产品" in _sig_norm(c) for c in cols):
        return PURCHASE
    if "订单编号" in sig and "业务类型" in sig:
        return SALES
    if "产品库存ID" in sig or ("库存数量" in sig and "产品名称(PN)" in sig):
        return INVENTORY
    return None
