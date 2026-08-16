"""中文列名 → 内部字段映射，文件识别特征，ffill 头字段（§4）。

列名在 reader 中已 strip。映射 key 用真实导出的完整中文列名（含 (必填)/# 等后缀）。
氚云不同字段视图/模板会让同一列带或不带 (必填) 注解——canonicalize_columns 负责容差归一。
"""
import re

PURCHASE = "purchase"
SALES = "sales"
INVENTORY = "inventory"
MAINTENANCE = "maintenance"
EXPENSE = "expense"
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
    # 维保需求单（WBDD）：维保出库成本"专属采购直配"层的关联键（同名列另有「选择维保需求单」，
    # 两列内容一致，只认无「选择」前缀者，避免 canonicalize 撞重复列）
    "维保需求单(必填)": "linked_maintenance_order_no",
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

# ---- 维保订单（WBDD 备件出库需求；90 列双表头导出，明细前缀「需求明细.」）----
# 导出本身无任何成本/金额列（实测零命中）——成本由 maintenance_cost.recompute 回填。
MAINTENANCE_HEAD = {
    "数据ID(不可修改)": "raw_order_id",
    "需求单号": "order_no",
    "制单日期": "order_date",
    "销售订单": "linked_sales_order_no",
    "项目名": "project_raw",
    "客户名称": "customer_name",
    "最终客户名": "end_customer",
    "需求类型": "demand_type",
    "业务类型": "business_type",
    "销售人员": "salesperson",
    "出库仓库(必填)": "warehouse",
    "维保起始日期": "maint_start",
    "维保终止日期": "maint_end",
    "数据状态": "data_status",
}
MAINTENANCE_LINE = {
    "需求明细.数据ID(不可修改)": "raw_line_id",
    "需求明细.序号": "line_no",
    "需求明细.需供货产品": "pn_raw",
    "需求明细.产品描述": "description",
    "需求明细.需求数量": "qty",
    "需求明细.退货数量": "return_qty",
    "需求明细.发货SN": "serial_numbers",
}

# ---- WBDD 展示补全列（plan v1.3 §3，34 头 + 28 明细，全部只展示）----
# 头级 34 列：源列名 → f_maintenance_order 列。头段位置见 §1.1（91 列布局 [0..6]∪[44..90]）。
# 排除不落库 6 列：项目经理#、备注(头)、数据标题、创建人(必填)、拥有者(必填)、所属部门(必填)。
MAINTENANCE_HEAD_DISPLAY = {
    "需求数量": "head_demand_qty",
    "需采数量": "head_purchase_qty",
    "已发货数量": "head_shipped_qty",
    "已返货数量": "head_returned_qty",
    "维保负责人": "maintainer_raw",
    "维保工单": "work_order_no",
    "制单人员": "created_by_raw",
    "采购员": "purchaser_raw",
    # 与采购单头「采购人员(必填)」共用同一规范键（canonicalize 会把裸「采购人员」归一到
    # 注解形式），两个文件类型各自的映射表按此键取各自字段，互不干扰。
    "采购人员(必填)": "purchaser2_raw",
    "项目经理": "project_manager_raw",
    "项目经理人员": "project_manager_staff_raw",
    "协同销售人员": "co_salesperson_raw",
    "合作伙伴人": "partner_raw",
    "销售部门": "sales_dept_raw",
    "仓管员": "warehouse_keeper_raw",
    "仓储中心": "storage_center",
    "仓库": "warehouse_raw",
    "是否变仓库": "change_warehouse_flag",
    "变更仓库": "change_warehouse",
    "变更仓承办人(必填)": "change_warehouse_handler",
    "仓库承办人(必填)": "warehouse_handler",
    "供货期限": "supply_deadline",
    "选择收货地址": "delivery_address_option",
    "收货人": "receiver",
    "收货人电话": "receiver_phone",
    "收货地址": "receiver_address",
    "快递单号": "express_no",
    "快递单号#": "express_no2",
    "图片": "image_urls",
    "附件": "attachments",
    "整机需采备件校验": "whole_machine_check",
    "是否可以接受通用号": "accept_generic_flag",   # 91 列布局独有；90 列文件 → NULL
    "创建时间(必填)": "created_at_raw",
    "修改时间(必填)": "modified_at_raw",
}
# 明细级 28 列：前 14 为「流转状态列」——只原样展示、不参与任何计算（铁律 3）。
# 排除不落库 2 列：数据标题(明细)、产品名称#。「整机/备件」「图片/附件」是名称含斜杠的单列。
MAINTENANCE_LINE_DISPLAY = {
    "需求明细.需采数量(必填)": "purchase_qty",
    "需求明细.变更仓需采数量(必填)": "change_warehouse_purchase_qty",
    "需求明细.已采数量": "purchased_qty",
    "需求明细.待采数量": "pending_purchase_qty",
    "需求明细.直采直发数": "direct_ship_qty",
    "需求明细.库房需发数": "warehouse_need_qty",
    "需求明细.库房发货数": "warehouse_shipped_qty",
    "需求明细.已供数量": "supplied_qty",
    "需求明细.待供数量": "pending_supply_qty",
    "需求明细.已返数量": "returned_qty",
    "需求明细.待返数量": "pending_return_qty",
    "需求明细.领用数量": "consumed_qty",
    "需求明细.需求待返数": "demand_pending_return_qty",
    "需求明细.退返旧件": "return_old_part",
    "需求明细.整机/备件": "whole_or_part",
    "需求明细.整机需采备件": "whole_machine_purchase_part",
    "需求明细.整机备件已采": "whole_machine_part_purchased",
    "需求明细.需采备件说明": "purchase_note",
    "需求明细.备注": "line_note",
    "需求明细.图片/附件": "line_image_urls",
    "需求明细.各仓库存": "warehouse_stock_raw",
    "需求明细.个别调整发货仓": "adjust_warehouse_flag",
    "需求明细.调整仓库": "adjust_warehouse",
    "需求明细.调整仓储中心": "adjust_storage_center",
    "需求明细.调整库管员": "adjust_keeper",
    "需求明细.发货仓库": "ship_warehouse",
    "需求明细.发货仓ObjectID": "ship_warehouse_object_id",
    "需求明细.发货库存": "ship_stock",
}
MAINTENANCE_HEAD_DISPLAY_FIELDS: tuple[str, ...] = tuple(MAINTENANCE_HEAD_DISPLAY.values())
MAINTENANCE_LINE_DISPLAY_FIELDS: tuple[str, ...] = tuple(MAINTENANCE_LINE_DISPLAY.values())
# 并入主映射：ffill / canonicalize / transform 反查共用一份
MAINTENANCE_HEAD.update(MAINTENANCE_HEAD_DISPLAY)
MAINTENANCE_LINE.update(MAINTENANCE_LINE_DISPLAY)

# ---- 维保报销单（BXD 费用侧，§16.3）：正式源=项目追踪工作簿的报销明细页 ----
# 金额在行级；「数据标题」= BXD单号+姓名，单号由 transform 正则提取；
# 工作簿版存在列名漂移（费用分类 vs 报销明细.费用分类、冗余「销售订单」列），transform 做回退互补。
EXPENSE_HEAD = {
    "数据ID(不可修改)": "raw_order_id",
    "数据标题": "bxd_title",
    "流程状态": "data_status",
    "报销人员": "person",
    "报销类别": "expense_type",
    "支出事由": "reason",
    "维保销售订单": "linked_sales_order_no",
    "报销日期": "expense_date",
}
EXPENSE_LINE = {
    "报销明细.数据ID(不可修改)": "raw_line_id",
    "报销明细.序号": "line_no",
    "报销明细.费用分类": "fee_category",
    "报销明细.报销金额": "amount",
    "报销明细.报销金额（未税）": "amount_ex_tax",
    "报销明细.报销金额（含税）": "amount_inc_tax",
    "报销明细.金额口径": "tax_basis",
}

# §17.3 宽松变体列（项目追踪工作簿报销页 / 任意含最小列的表格）。
# 不并入 EXPENSE_HEAD：EXPENSE_HEAD 同时是 ffill 头字段清单——「单号」若 ffill 会把上一行
# 的单号灌进无单号行，复合幂等键 单号#序号 随即撞键静默丢行。这里只登记进 _ALL_KEYS
# 供 canonicalize_columns 剥 (必填) 注解，transform 直接按列名读。
EXPENSE_LOOSE_COLS = {
    "报销金额",
    "报销金额（未税）",
    "报销金额(未税)",
    "未税金额",
    "不含税金额",
    "报销金额（含税）",
    "报销金额(含税)",
    "含税金额",
    "金额口径",
    "税务口径",
    "含税/未税",
    "是否含税",
    "含税标记",
    "报销明细.是否含税",
    "报销明细.含税标记",
    "费用分类",
    "单号",
    "序号",
    "费用单号",
    "报销单号",
    "销售订单",
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
    MAINTENANCE: {"head": MAINTENANCE_HEAD, "line": MAINTENANCE_LINE},
    EXPENSE: {"head": EXPENSE_HEAD, "line": EXPENSE_LINE},
    INVENTORY: {"head": {}, "line": INVENTORY_MAP},
}

VALUE_ALIASES = {
    PURCHASE: {
        # 报销兼容字段也会使用裸「是否含税」；按文件类型合并，避免全局列名
        # 归一后采购头部的 is_tax_inclusive 静默读空。
        "是否含税(必填)": ("是否含税",),
    },
    SALES: {
        "业务类型#": ("业务类型",),
    },
}

# ffill 的头字段（原始中文列名）—— 库存无
FFILL_COLS = {
    PURCHASE: list(PURCHASE_HEAD.keys()),
    SALES: list(SALES_HEAD.keys()),
    MAINTENANCE: list(MAINTENANCE_HEAD.keys()),
    # 报销不 ffill（§17.3）：扁平表单每行是一笔独立报销——留空是「无/默认」不是「同上」。
    # ffill 会让员工只填日期+金额的新行继承上一行的 流程状态/人员/事由（状态=流程中时
    # 新报销静默不计已花），也会让合计行继承日期被误吃。头明细式多行 BXD 导出已随
    # 「氚云无报销导出」口径一并废止（§16.3 更正）。
    EXPENSE: [],
    INVENTORY: [],
}

# 价格/金额列（成交单价 + 行金额 + 头部含税未税/税金）——导入前预检用：
# 采购/销售文件若一个价格列都没有（如导出视图选错），导入后这些单将无金额。
PRICE_INTERNALS = {"unit_price", "line_amount", "amount_ex_tax", "tax_amount", "amount_inc_tax"}


def has_price_columns(cols: list[str], file_type: str | None) -> bool:
    """文件列里是否含任一价格列。库存/询价/未识别一律 True（不涉及价格，不拦）。"""
    if file_type not in (PURCHASE, SALES):
        return True
    m = {**MAPPINGS[file_type]["head"], **MAPPINGS[file_type]["line"]}
    price_chinese = {ch for ch, internal in m.items() if internal in PRICE_INTERNALS}
    return any(c in cols for c in price_chinese)


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
             | set(SALES_LINE) | set(MAINTENANCE_HEAD) | set(MAINTENANCE_LINE)
             | set(EXPENSE_HEAD) | set(EXPENSE_LINE) | EXPENSE_LOOSE_COLS
             | set(INVENTORY_MAP))
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
    # 维保出库（WBDD）在销售之前判：维保导出也有「业务类型」但无「订单编号」，
    # 特征列取 需求单号 +（需求类型 或 维保起始日期），2023-2026 各年份导出实测均含
    if "需求单号" in sig and ("需求类型" in sig or "维保起始日期" in sig):
        return MAINTENANCE
    # 维保报销单（BXD，费用侧 §16.3）：报销类别 + 维保销售订单 双特征
    if "报销类别" in sig and "维保销售订单" in sig:
        return EXPENSE
    # 宽松变体（§17.3 项目追踪工作簿报销页/来源无关模板）：报销金额 是强特征——
    # 采购/销售/维保出库/库存导出均无此列，零误伤；辅以 费用分类 或 报销日期 双保险
    has_expense_amount = any(
        value == "报销金额"
        or "报销金额（未税）" in value
        or "报销金额(未税)" in value
        or "报销金额（含税）" in value
        or "报销金额(含税)" in value
        for value in sig
    ) or bool(sig & {"未税金额", "不含税金额", "含税金额"})
    if has_expense_amount and ("费用分类" in sig or "报销日期" in sig):
        return EXPENSE
    if "订单编号" in sig and "业务类型" in sig:
        return SALES
    if "产品库存ID" in sig or ("库存数量" in sig and "产品名称(PN)" in sig):
        return INVENTORY
    return None
