"""Workbook wire constants, shared by the V1, V2 and expense adapters.

This module does not choose business policy or render Excel styles. Mutable
header lists and sets intentionally retain their existing types and ordering.
"""

EXPENSE_COLLECTION_PROTOCOL_VERSION = "expense-collection-v1"

SHEET_EXPENSE = "04_报销订单"

SHEET_COLLECTION = "05_项目经理回款单"

_META_SHEET = "99_元数据"

_EXPENSE_HEADERS = ["报销单号", "报销日期", "报销人员", "报销类别", "费用分类",
                    "支出事由", "合同编号", "未税金额", "含税金额(系统计算)",
                    "流程状态", "备注"]

_COLLECTION_HEADERS = ["操作", "合同编号", "报告月份", "累计回款金额(含税)",
                       "回款凭证号", "状态(系统)", "备注"]

PROJECT_MASTER_PROTOCOL_VERSION = "project-master-v1"

V2_PROTOCOL_ID = "ITDATA_MAINT_PROJECT_MASTER/2.0"

# 2026-08-19（#264/#267）：03 全字段可编辑 + 04 作废/缺行=作废——新增「操作」列、
# 放开行级数据列、只读哈希收窄。旧 2.0.0 工作簿因哈希列集合变更一律拒绝并
# 提示重新下载。
V2_TEMPLATE_VERSION = "2.7.1"

V2_META_SIGNATURE_ALGORITHM = "HMAC-SHA256"

_V2_META_SIGNATURE_DOMAIN = b"ITDATA_MAINT_PROJECT_MASTER_META_V1\x00"

SHEET_BASICS = "01_项目基础信息"

SHEET_OVERVIEW = "02_概览数据"

SHEET_PARTS = "03_备件订单"

SHEET_SITE = "06_现场领用与返还"

_GLOBAL_COST_TOKEN_DOMAIN = b"ITDATA_MAINT_GLOBAL_COST_BASE_V1\x00"

ALL_SHEETS = (SHEET_BASICS, SHEET_OVERVIEW, SHEET_PARTS, SHEET_EXPENSE,
              SHEET_COLLECTION, SHEET_SITE)

# 可回填的四张：01/02 是只读呈现（表 6 规格明写「全只读」）
EDITABLE_SHEETS = (SHEET_PARTS, SHEET_EXPENSE, SHEET_COLLECTION, SHEET_SITE)

# 全项目备件行级表（主页全局下载）
GLOBAL_SHEET = "备件行级表"

_RANGE_PRESETS = ("today", "yesterday", "this_week", "this_month", "custom")

_PARTS_HEADERS = ["维保单号", "制单日期", "合同编号", "项目名称", "PN", "产品描述",
                  "需求数量", "退货数量", "发货SN", "出库仓库", "成本来源(系统)",
                  "未税单位成本", "含税单位成本(系统计算)", "变更原因"]

_SITE_HEADERS = ["现场领用单号", "领用日期", "PN", "备件SN", "领用数量",
                 "是否应返还(行级)", "备注"]

_GLOBAL_HEADERS = ["项目名称", "合同编号", "维保单号", "制单日期", "PN", "产品描述",
                   "需求数量", "退货数量", "成本来源(系统)", "未税单位成本",
                   "含税单位成本(系统计算)", "变更原因"]

V2_SHEET_USAGE = "00_使用说明"

V2_SHEET_OVERVIEW = "01_项目概览"

V2_SHEET_PLAN = "02_回款计划"

V2_SHEET_PARTS = "03_备件明细"

V2_SHEET_EXPENSE = "04_费用报销"

V2_SHEET_RECEIPTS = "05_实收回款"

V2_SHEET_SITE = "06_领用返还"

V2_SHEET_DICTIONARY = "98_字段说明"

V2_SHEET_META = "99_元数据"

V2_ALL_SHEETS = (V2_SHEET_OVERVIEW, V2_SHEET_PLAN, V2_SHEET_PARTS,
                 V2_SHEET_EXPENSE, V2_SHEET_RECEIPTS, V2_SHEET_SITE,
                 V2_SHEET_DICTIONARY, V2_SHEET_USAGE, V2_SHEET_META)

V2_PART_HEADERS = [
    "操作", "维保单号", "制单日期", "XSDD", "需求类型", "仓库", "销售人员", "业务类型",
    "PN", "描述", "需求数量", "SN", "退货数量", "成本来源", "置信度",
    "系统未税单位成本", "系统含税单位成本", "人工未税单位成本", "人工成本原因",
    "成本缺失类型", "可补价", "实体ID", "备件主键", "只读哈希",
    "备注", "来源", "基线令牌",
]

# 2026-08-19 全字段可编辑（1-based 列号）：操作列 + 行级数据列（PN/描述/数量/SN/
# 退货数量）+ 人工成本两列 + 备注。维保单号/制单日期/XSDD/头级字段/系统成本/来源仍锁。
V2_PART_EDITABLE = {1, 9, 10, 11, 12, 13, 18, 19, 25}

# 参与只读哈希的列（按表头名定位，避免插列后错位）：行级身份与系统事实。
# 可编辑列一律不进哈希，改黄底列不再触发 readonly_cell_modified。
V2_PART_HASH_COLUMNS = [
    "维保单号", "制单日期", "XSDD", "需求类型", "仓库", "销售人员", "业务类型",
    "成本来源", "置信度", "系统未税单位成本", "系统含税单位成本", "来源", "实体ID",
]

V2_PLAN_HEADERS = ["操作", "合同编号", "期次", "计划回款日期", "日期精度", "计划回款金额（含税）",
                   "累计计划金额", "最新累计实收", "到款状态", "提醒状态", "负责人", "备注", "实体ID", "基础版本"]

V2_EXPENSE_HEADERS = ["操作", "费用单号", "明细序号", "报销日期", "报销人员", "报销类别", "费用分类",
                      "支出事由", "维保销售订单（归集键）", "项目名称", "销售人员", "流程状态",
                      "原始报销金额", "金额口径", "未税金额", "含税金额（系统）", "备注", "实体ID",
                      "基线令牌"]

V2_RECEIPT_HEADERS = ["合同编号", "报告月份", "累计实收金额（含税）", "状态", "回款凭证号", "备注", "实体ID",
                      "基线令牌"]

V2_SITE_HEADERS = ["领用单号", "领用日期", "PN", "SN", "领用数量", "是否应返还", "备注",
                   "应返数量", "返还状态", "返还单号", "实体ID", "基线令牌", "关联需求单号",
                   "领用地址", "地址操作"]

# 行级基线令牌域（2.7.0）：把每行可编辑字段的导出值签进隐藏列，上传侧据此做
# 三路合并（用户值 vs 导出基线 vs 服务端现值），取代整本 revision 作废。
_V2_ROW_BASE_DOMAIN = b"ITDATA_MAINT_MASTER_ROW_BASE_V1\x00"

V2_BASE_COLUMN = "基线令牌"

V2_PART_BASE_FIELDS = ("PN", "描述", "需求数量", "SN", "退货数量",
                       "人工未税单位成本", "人工成本原因", "备注")

V2_EXPENSE_BASE_FIELDS = ("报销日期", "报销人员", "报销类别", "费用分类", "支出事由",
                          "维保销售订单（归集键）", "原始报销金额", "未税金额", "流程状态", "备注")

V2_RECEIPT_BASE_FIELDS = ("累计实收金额（含税）", "状态", "回款凭证号", "备注")

V2_SITE_BASE_FIELDS = ("领用单号", "领用日期", "PN", "SN", "领用数量", "是否应返还", "备注")

# 保留旧七字段用于缺行哈希和自动编号重放；地址不改变删除或身份契约。
V2_SITE_ADDRESS_GUARD_FIELDS = (*V2_SITE_BASE_FIELDS, "领用地址")

_META_CELL_SAFE_LENGTH = 30000

_META_CHUNK_SEP = "@"

V2_SHEET_RETURN_LEDGER = "返还收货台账（只读）"
