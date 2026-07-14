"""按用户的细粒度权限（管理员在"账号管理"页逐项勾选）。

四类开关：
- data_*  数据字段可见：驱动 security.apply_field_visibility——关掉则该类字段在所有接口被抹成 null。
- page_*  页面可用：驱动前端菜单显示 + 后端接口准入（require_page）。
- action_* 动作可用：驱动写操作准入（require_action）+ 前端动作入口显隐。
- own_customers_only  行级：销售只看自己成交的客户（同事客户名匿名）。

每个账号把自定义存在 sys_user.permissions(JSONB)；为空 → 回退该 role 的模板(ROLE_TEMPLATES)。
admin 恒为全开（不可被自己/他人锁死）。权限随登录写进 token，改权限后下次登录生效。
"""
from app import config

# data 开关 → 对应要隐藏的 config.FIELD_GROUPS 组名
DATA_GROUPS: dict[str, list[str]] = {
    "data_supplier": ["supplier_info"],
    "data_customer": ["customer_info"],
    "data_purchase_cost": ["purchase_cost"],
    "data_profit": ["profit_amount", "profit_rate"],
    # 互通池价格治理：约束价（采购上限/销售下限及原始录入值）可见性（§12）。
    # 关掉后后端同时隐藏价格、约束差额与越线标记，防止靠颜色/排序反推金额。
    "data_pool_price_governance": ["pool_price_governance"],
}
# 页面开关（与前端菜单 key 对齐）
PAGE_KEYS: list[str] = [
    "page_parts", "page_purchases", "page_profit",
    "page_inventory", "page_chat", "page_import", "page_governance",
    "page_master_data", "page_maintenance", "page_boss_board",
    "page_pool_analysis",
    # 权限中心 v2：账号与权限中心页面（只读查看账号/模板/活动）。critical 级——
    # 内置模板对所有非 admin 角色显式 False，保持"仅管理员可见账号管理"的既有行为。
    "page_accounts",
]
# 动作开关：写操作准入（require_action）。默认仅老板/管理员，可在账号管理页单独授权。
ACTION_KEYS: list[str] = [
    "action_pool_manage",      # 建池/改名称说明/增删成员/归档恢复
    "action_pool_set_policy",  # 设置采购最高价 / 销售最低价
    # 权限中心 v2：账号与权限的写能力（建号/改权/改密/停用/批量/模板管理）。
    # 授予/撤销本键与 page_accounts 仅限 admin 角色操作者（api/accounts._guard_account_write）。
    "action_account_manage",
]
ROW_KEYS: list[str] = ["own_customers_only"]
ALL_KEYS: list[str] = [*DATA_GROUPS, *PAGE_KEYS, *ACTION_KEYS, *ROW_KEYS]
_VALID = set(ALL_KEYS)

# 给前端勾选框用的中文标签
LABELS: dict[str, str] = {
    "data_supplier": "供应商信息",
    "data_customer": "客户信息",
    "data_purchase_cost": "采购进价 / 成本",
    "data_profit": "利润 / 毛利",
    "page_parts": "型号查询",
    "page_purchases": "采购记录",
    "page_profit": "利润分析",
    "page_inventory": "库存查询",
    "page_chat": "AI 助手",
    "page_import": "数据导入",
    "page_governance": "数据治理",
    "page_master_data": "备件主数据（新建/编辑 PN）",
    "page_maintenance": "项目成本（维保出库）",
    "page_boss_board": "老板经营看板",
    "page_pool_analysis": "互通池价格分析",
    "data_pool_price_governance": "池价格治理（约束价/越线差额）",
    "action_pool_manage": "互通PN池维护（建池/成员/归档）",
    "action_pool_set_policy": "池约束价设置（采购上限/销售下限）",
    "own_customers_only": "只看自己成交的客户（防恶性竞争）",
    "page_accounts": "账号与权限中心（查看）",
    "action_account_manage": "账号与权限管理（建号/改权/批量/模板）",
}


def _full(own: bool = False) -> dict[str, bool]:
    """全部数据 + 全部页面 + 全部动作打开；own_customers_only 单独给。"""
    d = {k: True for k in DATA_GROUPS}
    d.update({k: True for k in PAGE_KEYS})
    d.update({k: True for k in ACTION_KEYS})
    d["own_customers_only"] = own
    return d


# 角色模板：建号选角色时套用，可逐项微调（admin 例外，恒全开）。
# 权限中心 v2 起，这份 Python 字典只作三类回退用：guest/匿名兜底、无 perms 的旧 token、
# 共享口令回退登录。正常账号的权限底座来自 sys_role_template 套用时的快照（sys_user.template_perms）。
ROLE_TEMPLATES: dict[str, dict[str, bool]] = {
    "admin": _full(),
    # boss 由 _full() 生成会连账号管理两键一起 True——必须显式关：账号与权限管理
    # 历来仅 admin 可见，v2 引入 page_accounts/action_account_manage 不改变这一现状。
    "boss": {**_full(), "page_accounts": False, "action_account_manage": False},
    # readonly 也是 _DEFAULT + 未认证 guest 的兜底模板：page_maintenance/page_boss_board 必须显式关，
    # 否则未知/匿名角色继承 _full() 里的 True，凭 require_page 即可读（方案 §5）。
    # 池写权限（action_pool_*）同理必须显式关——匿名 guest 决不能建池/改约束价；
    # page_pool_analysis / data_pool_price_governance 按规格 §12 全员开（普通员工可看池与约束价）。
    "readonly": {**_full(), "page_import": False, "page_governance": False,
                 "page_master_data": False, "page_maintenance": False,
                 "page_boss_board": False,
                 "action_pool_manage": False, "action_pool_set_policy": False,
                 # 账号管理两键必须显式关（同 boss 注释；guest 兜底模板决不能看/管账号）
                 "page_accounts": False, "action_account_manage": False},
    "sales": {
        # 甲方 2026-06-15 确认：销售能看采购成本/毛利（整机拆解加点直卖需要采购价算建议售价）。
        # 供应商仍隐藏（不暴露从谁进货）；逐单销售成交明细另由 own_customers_only 收紧（看不到）。
        "data_supplier": False, "data_customer": True,
        "data_purchase_cost": True, "data_profit": True,
        # page_purchases=True：合同重点"销售和采购都能查最近采购记录"。
        # page_profit=False：利润分析接口本就 require_admin，给 sales 这个菜单只会点了 403。
        "page_parts": True, "page_purchases": True, "page_profit": False,
        "page_inventory": True, "page_chat": True,
        "page_import": False, "page_governance": False,
        # 项目成本=公司维保项目经营数据，销售不开（同 page_profit 口径）
        "page_maintenance": False,
        # 老板经营看板=全公司经营/个人排名，销售不开
        "page_boss_board": False,
        # 互通池价格分析全员可见（§12），约束价对全员公开；池维护/约束设置默认不开
        "page_pool_analysis": True,
        "data_pool_price_governance": True,
        "action_pool_manage": False, "action_pool_set_policy": False,
        "own_customers_only": True,
    },
    "purchaser": {
        "data_supplier": True, "data_customer": False,
        "data_purchase_cost": True, "data_profit": False,
        "page_parts": True, "page_purchases": True, "page_profit": False,
        "page_inventory": True, "page_chat": True,
        "page_import": False, "page_governance": False,
        # 甲方 2026-06-30：备件主数据(新建/编辑 PN)对采购开放
        "page_master_data": True,
        # 维保项目成本对采购开放（成本口径本就对采购可见，data_purchase_cost=True）
        "page_maintenance": True,
        # 老板经营看板=全公司经营/个人排名，采购不开
        "page_boss_board": False,
        # 互通池价格分析全员可见（§12），约束价对全员公开；池维护/约束设置默认不开
        "page_pool_analysis": True,
        "data_pool_price_governance": True,
        "action_pool_manage": False, "action_pool_set_policy": False,
        "own_customers_only": False,
    },
}
_DEFAULT = ROLE_TEMPLATES["readonly"]


def template_for(role: str) -> dict[str, bool]:
    return dict(ROLE_TEMPLATES.get(role, _DEFAULT))


def effective(role: str, custom: dict | None) -> dict[str, bool]:
    """最终权限：role 模板打底、custom(自定义)逐项覆盖。admin 恒全开，不可自锁。"""
    if role == "admin":
        return _full()
    perms = template_for(role)
    if custom:
        for k, v in custom.items():
            if k in _VALID:
                perms[k] = bool(v)
    for k in ALL_KEYS:
        perms.setdefault(k, False)
    return perms


def sanitize(custom: dict | None) -> dict[str, bool]:
    """存库前清洗：只留合法 key、值转 bool。"""
    if not custom:
        return {}
    return {k: bool(v) for k, v in custom.items() if k in _VALID}


# "能改必须能看"的动作→数据依赖（复审阻塞 4）：动作权限开而对应数据可见权限关，
# 用户会在看不见现值的情况下改写它（约束价单侧被静默清空的根源）。
# 账号保存时拒绝（combo_errors），接口层 require_action(require_data=...) 兜底。
ACTION_DATA_DEPENDENCIES: dict[str, str] = {
    "action_pool_set_policy": "data_pool_price_governance",
}

# "页面内操作必须能进页面"的动作→页面依赖（权限中心 v2）：改账号权限先要能打开
# 账号管理页看到现状。池两动作不在此表——互通PN池管理页的入口就是这两把动作钥匙本身
# （nav anyPerm），没有独立的 page_* 键。
ACTION_PAGE_DEPENDENCIES: dict[str, str] = {
    "action_account_manage": "page_accounts",
}


def combo_errors(perms: dict[str, bool]) -> list[str]:
    """校验**最终生效**权限（模板/快照+覆盖叠加后）的非法组合，返回人话错误清单。
    账号保存、模板保存、批量操作、模板同步四条写路径统一走这里。"""
    errors: list[str] = []
    for action_key, data_key in ACTION_DATA_DEPENDENCIES.items():
        if perms.get(action_key, False) and not perms.get(data_key, False):
            errors.append(
                f"「{LABELS.get(action_key, action_key)}」需要同时开启"
                f"「{LABELS.get(data_key, data_key)}」——能设置就必须能查看，"
                f"否则会在看不见现值的情况下改写它")
    for action_key, page_key in ACTION_PAGE_DEPENDENCIES.items():
        if perms.get(action_key, False) and not perms.get(page_key, False):
            errors.append(
                f"「{LABELS.get(action_key, action_key)}」需要同时开启"
                f"「{LABELS.get(page_key, page_key)}」——操作发生在该页面里，"
                f"进不了页面就无法看着现状做修改")
    return errors


def hidden_groups(perms: dict | None) -> set[str]:
    """据 data_* 开关算出要隐藏的 FIELD_GROUPS 组名集合。"""
    if not perms:
        return set()
    hidden: set[str] = set()
    for key, groups in DATA_GROUPS.items():
        if not perms.get(key, False):
            hidden.update(groups)
    return hidden


# ══════════════════════════ 权限中心 v2 ══════════════════════════

# 高风险键：授予/撤销仅限 admin 角色操作者（防非 admin 的账号管理代理自我提权/互相提权）
HIGH_RISK_KEYS: set[str] = {"page_accounts", "action_account_manage"}

# 前端矩阵五分组（顺序即展示序）：页面入口 / 数据可见 / 操作能力 / 行级范围 / 高风险管理
UI_GROUPS: list[dict] = [
    {"key": "page", "label": "页面入口",
     "hint": "决定左侧菜单能看到、能打开哪些页面。进得了页面≠看得到敏感字段（由「数据可见范围」控制）。",
     "keys": [k for k in PAGE_KEYS if k != "page_accounts"]},
    {"key": "data", "label": "数据可见范围",
     "hint": "决定所有页面、导出、AI 助手里对应字段是否显示。关掉后字段在后端就被抹成空，前端换页面也看不到。",
     "keys": list(DATA_GROUPS)},
    {"key": "action", "label": "操作能力",
     "hint": "决定能不能执行写操作（新建/修改/设置）。看见≠能改，改的能力在这里单独授权。",
     "keys": ["action_pool_manage", "action_pool_set_policy"]},
    {"key": "row", "label": "行级范围",
     "hint": "在能看的数据里进一步收紧范围（限制型开关：勾上=看得更少）。",
     "keys": list(ROW_KEYS)},
    {"key": "admin", "label": "高风险管理能力",
     "hint": "接近管理员的能力，只有管理员本人可以授予或撤销，请谨慎开放。",
     "keys": ["page_accounts", "action_account_manage"]},
]

# 每个权限键的业务语言八要素（甲方语言，不是开发语言）。
# summary=一句话；can/cannot=能看到能做什么/不能看到不能做什么；typical=典型使用岗位；
# sensitivity=数据敏感级 low/medium/high/critical；risk=开放风险提醒。
PERMISSION_META: dict[str, dict] = {
    # ---- 数据可见范围 ----
    "data_supplier": {
        "label": "查看供应商信息",
        "summary": "允许查看采购单据上的供应商与采购来源渠道。",
        "can": "供应商名称、编号、联系人、电话，以及采购来源渠道分类。",
        "cannot": "不含采购价格与金额（另由「查看采购成本」控制）；不代表可以编辑供应商资料。",
        "typical": ["采购", "老板"],
        "sensitivity": "high",
        "risk": "泄露进货渠道——知道从谁进货，就可能绕开公司直接找货源。",
    },
    "data_customer": {
        "label": "查看客户信息",
        "summary": "允许查看销售单据上的客户名称、城市与联系方式。",
        "can": "客户名称、城市、联系人、电话。",
        "cannot": "不含逐单成交明细的归属（销售角色另受「只看自己成交的客户」收紧）；不代表可以编辑客户资料。",
        "typical": ["销售", "老板"],
        "sensitivity": "high",
        "risk": "客户名单是业务命脉，外泄可被同行或离职员工带走。",
    },
    "data_purchase_cost": {
        "label": "查看采购成本",
        "summary": "允许查看采购单价、采购金额、池采购均价以及由采购成本派生的差额。",
        "can": "采购单价/金额、库存成本、移动加权/FIFO 成本、池标杆成本、节省额、维保项目成本明细。",
        "cannot": "不代表可以修改采购数据；关闭后所有页面、导出、AI 助手中相关字段一律置空，无法靠排序或差额反推。",
        "typical": ["采购", "销售", "老板"],
        "sensitivity": "high",
        "risk": "泄露进货底价，影响对外报价谈判空间。",
    },
    "data_profit": {
        "label": "查看利润 / 毛利",
        "summary": "允许查看毛利额、毛利率与盈亏排行。",
        "can": "单据与汇总的毛利额、毛利率、赚钱/亏钱排行榜。",
        "cannot": "关闭后连「哪个型号在赚钱榜还是亏损榜」的归类都不返回，不只是数字打码。",
        "typical": ["老板", "销售"],
        "sensitivity": "high",
        "risk": "暴露公司真实盈利水平。",
    },
    "data_pool_price_governance": {
        "label": "查看池约束价",
        "summary": "允许查看互通PN池的采购最高价/销售最低价、越线差额与越线标记。",
        "can": "各池的约束价现值、原始录入值、越线单据标记与差额。",
        "cannot": "不代表可以设置或修改约束价（另由「池约束价设置」控制）。",
        "typical": ["全员（甲方口径：约束价对员工公开）"],
        "sensitivity": "medium",
        "risk": "关闭后该员工在采购/销售录入时收不到越线提醒。",
    },
    # ---- 页面入口 ----
    "page_parts": {
        "label": "型号查询",
        "summary": "可打开型号查询页，检索 PN 全景（历史价格、库存、替代关系）。",
        "can": "按 PN/描述搜索，查看型号全景卡片。",
        "cannot": "页面内的成本、利润、供应商等字段仍由对应「数据可见范围」决定。",
        "typical": ["全员"],
        "sensitivity": "low",
        "risk": "基础查询入口，风险低。",
    },
    "page_purchases": {
        "label": "采购页面（分析/异常/明细）",
        "summary": "可打开采购分析、采购异常、采购明细三个页面。",
        "can": "查看采购单据流水、异常单、渠道分析。",
        "cannot": "无「查看采购成本」时价格金额列为空；无「查看供应商信息」时供应商列为空。",
        "typical": ["采购", "销售", "老板"],
        "sensitivity": "medium",
        "risk": "配合数据权限使用，本身只是入口。",
    },
    "page_profit": {
        "label": "利润分析",
        "summary": "可打开利润分析页，查看公司利润报表。",
        "can": "按型号/客户/时间维度看利润汇总与排行。",
        "cannot": "无「查看利润/毛利」时报表数值为空——两者通常应一起开。",
        "typical": ["老板"],
        "sensitivity": "high",
        "risk": "公司经营核心数据入口。",
    },
    "page_inventory": {
        "label": "库存查询",
        "summary": "可打开库存查询页，查看各仓库存数量。",
        "can": "按 PN/仓库查库存数量与快照。",
        "cannot": "无「查看采购成本」时库存金额为空。",
        "typical": ["全员"],
        "sensitivity": "low",
        "risk": "库存数量属一般业务数据。",
    },
    "page_chat": {
        "label": "AI 助手",
        "summary": "可打开 AI 助手对话页，用自然语言查数。",
        "can": "向 AI 提问库存、价格、采购建议等。",
        "cannot": "AI 的回答同样受本账号数据权限脱敏——问出来的和页面上看到的一样多，不会多。",
        "typical": ["全员"],
        "sensitivity": "medium",
        "risk": "入口本身无额外风险（数据侧已兜底）。",
    },
    "page_import": {
        "label": "数据导入",
        "summary": "可打开数据导入页，上传氚云 Excel 写入数据库。",
        "can": "上传采购/销售/库存/询价文件，查看导入报告与历史批次。",
        "cannot": "不能删除已导入数据（无删除入口）。",
        "typical": ["管理员", "数据专员"],
        "sensitivity": "critical",
        "risk": "写入口——传错文件会污染业务数据，建议只给足够熟悉流程的人。",
    },
    "page_governance": {
        "label": "数据治理",
        "summary": "可打开数据治理页，处理数据质量问题与合并候选。",
        "can": "查看/处理质量问题清单、PN 合并候选。",
        "cannot": "不含账号权限管理（另由「账号与权限中心」控制）。",
        "typical": ["管理员", "数据专员"],
        "sensitivity": "high",
        "risk": "误合并会改变型号归组，影响所有统计口径。",
    },
    "page_master_data": {
        "label": "备件主数据",
        "summary": "可打开备件主数据页，新建/编辑 PN 与分类。",
        "can": "新建 PN、编辑描述/品牌/分类。",
        "cannot": "改动会留审计痕迹；ETL 导入不会覆盖人工编辑。",
        "typical": ["采购", "管理员"],
        "sensitivity": "medium",
        "risk": "主数据错误会传导到搜索与统计。",
    },
    "page_maintenance": {
        "label": "项目成本（维保出库）",
        "summary": "可打开维保项目成本页，查看合同级盈亏看板。",
        "can": "查看维保合同的成本瀑布、红黄绿盈亏、报销明细。",
        "cannot": "无「查看采购成本」时成本列为空，看板只剩结构。",
        "typical": ["采购", "老板"],
        "sensitivity": "high",
        "risk": "公司维保项目的真实盈亏。",
    },
    "page_boss_board": {
        "label": "老板经营看板",
        "summary": "可打开经营看板，查看全公司经营汇总与个人排名。",
        "can": "营收/成本/利润总览、销售排名、池节省汇总。",
        "cannot": "看板数值仍受数据权限脱敏（无成本权限时成本相关卡片为空）。",
        "typical": ["老板"],
        "sensitivity": "critical",
        "risk": "全公司经营全景+员工排名，默认只给老板与管理员。",
    },
    "page_pool_analysis": {
        "label": "互通池价格分析",
        "summary": "可打开互通PN池价格分析页。",
        "can": "查看池内价格分布、约束价执行情况。",
        "cannot": "不能建池/改约束价（另由两个池操作权限控制）。",
        "typical": ["全员"],
        "sensitivity": "medium",
        "risk": "甲方口径：池分析对员工公开。",
    },
    "page_accounts": {
        "label": "账号与权限中心（查看）",
        "summary": "可打开账号与权限中心，查看账号列表、权限矩阵、职位模板与活动记录。",
        "can": "看每个账号的权限现状、模板使用情况、登录活动。",
        "cannot": "只能看不能改——建号/改权/批量/模板管理需要「账号与权限管理」。",
        "typical": ["管理员"],
        "sensitivity": "critical",
        "risk": "能看到全员权限分布与活动记录，仅管理员可授予本权限。",
    },
    # ---- 操作能力 ----
    "action_pool_manage": {
        "label": "互通PN池维护",
        "summary": "允许新建互通池、修改名称说明、增删成员、归档与恢复。",
        "can": "建池/改池/归档池；本权限自带互通PN池管理页入口。",
        "cannot": "不能设置采购最高价/销售最低价（另由「池约束价设置」控制）。",
        "typical": ["采购主管", "管理员"],
        "sensitivity": "medium",
        "risk": "池成员变化会改变库存合并口径与看板统计。",
    },
    "action_pool_set_policy": {
        "label": "池约束价设置",
        "summary": "允许设置/修改互通池的采购最高价与销售最低价。",
        "can": "录入、调整、清空约束价；本权限自带互通PN池管理页入口。",
        "cannot": "必须同时持有「查看池约束价」——看不见现值不允许改（系统强制）。",
        "typical": ["老板", "采购主管"],
        "sensitivity": "high",
        "risk": "约束价直接约束一线采购/销售定价，改错影响所有相关单据的越线判断。",
    },
    "action_account_manage": {
        "label": "账号与权限管理",
        "summary": "允许建号、修改权限、重置密码、停用账号、批量设置与管理职位模板。",
        "can": "对普通账号做全部管理操作；新建/修改/停用职位模板；批量套用模板。",
        "cannot": "不能操作管理员账号、不能授予/撤销本权限与「账号与权限中心（查看）」、"
                  "不能把账号升为管理员——这些仍只有管理员本人能做。",
        "typical": ["管理员"],
        "sensitivity": "critical",
        "risk": "接近管理员的能力：持有者可以改动其他普通账号看到什么。必须同时开启「账号与权限中心（查看）」。",
    },
    # ---- 行级范围 ----
    "own_customers_only": {
        "label": "只看自己成交的客户",
        "summary": "限制型开关：勾上后该账号看不到逐单销售成交明细，同事的客户名被匿名。",
        "can": "仍可看聚合行情（平均售价、成交参考价）与自己名下客户。",
        "cannot": "看不到「某单卖给谁、卖了多少」——这是防恶性竞争的收紧，不是能力。",
        "typical": ["销售"],
        "sensitivity": "low",
        "risk": "对销售角色建议保持勾选；取消后该账号能看到全部客户成交归属。",
    },
}


def normalize(perms: dict | None) -> dict[str, bool]:
    """把任意权限图规范化成「全键→bool」的完整图（未知键丢弃、缺键补 False）。"""
    src = perms or {}
    return {k: bool(src.get(k, False)) for k in ALL_KEYS}


def diff_overrides(base: dict, desired: dict) -> dict[str, bool]:
    """算稀疏覆盖：desired 相对 base（模板快照）逐键 diff，只留不同的键。"""
    return {k: bool(desired.get(k, False)) for k in ALL_KEYS
            if bool(desired.get(k, False)) != bool(base.get(k, False))}


def effective_from_snapshot(template_perms: dict | None, overrides: dict | None) -> dict[str, bool]:
    """v2 有效权限（纯函数）：模板快照打底 + 稀疏覆盖逐键盖上。"""
    perms = normalize(template_perms)
    for k, v in (overrides or {}).items():
        if k in _VALID:
            perms[k] = bool(v)
    return perms


def effective_for_user(user) -> dict[str, bool]:
    """v2 有效权限（账号级单一真值源）：admin 恒全开；有模板快照走快照⊕覆盖；
    无快照（迁移前旧行/异常兜底）回退旧口径 effective(role, permissions)。"""
    if user.role == "admin":
        return _full()
    if getattr(user, "template_perms", None) is not None:
        return effective_from_snapshot(user.template_perms, user.perm_overrides)
    return effective(user.role, user.permissions)
