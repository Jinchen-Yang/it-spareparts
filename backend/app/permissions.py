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
]
# 动作开关：写操作准入（require_action）。默认仅老板/管理员，可在账号管理页单独授权。
ACTION_KEYS: list[str] = [
    "action_pool_manage",      # 建池/改名称说明/增删成员/归档恢复
    "action_pool_set_policy",  # 设置采购最高价 / 销售最低价
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
}


def _full(own: bool = False) -> dict[str, bool]:
    """全部数据 + 全部页面 + 全部动作打开；own_customers_only 单独给。"""
    d = {k: True for k in DATA_GROUPS}
    d.update({k: True for k in PAGE_KEYS})
    d.update({k: True for k in ACTION_KEYS})
    d["own_customers_only"] = own
    return d


# 角色模板：建号选角色时套用，可逐项微调（admin 例外，恒全开）
ROLE_TEMPLATES: dict[str, dict[str, bool]] = {
    "admin": _full(),
    "boss": _full(),
    # readonly 也是 _DEFAULT + 未认证 guest 的兜底模板：page_maintenance/page_boss_board 必须显式关，
    # 否则未知/匿名角色继承 _full() 里的 True，凭 require_page 即可读（方案 §5）。
    # 池写权限（action_pool_*）同理必须显式关——匿名 guest 决不能建池/改约束价；
    # page_pool_analysis / data_pool_price_governance 按规格 §12 全员开（普通员工可看池与约束价）。
    "readonly": {**_full(), "page_import": False, "page_governance": False,
                 "page_master_data": False, "page_maintenance": False,
                 "page_boss_board": False,
                 "action_pool_manage": False, "action_pool_set_policy": False},
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

# "开毛利必须开成本"的数据→数据依赖（2026-07-14 看板对抗审计）：营收对全员可见，
# data_profit=True 而 data_purchase_cost=False 时，revenue_costed − gross_profit_moving
# 可精确重构被遮的移动加权成本（part_ranking 榜单/items、sales_orders 的
# total_revenue − total_gross_profit 同理）——遮成本形同虚设。内置角色模板无此组合
# （sales 双开、purchaser 反向"看成本不看毛利"），仅账号管理页自定义可达，保存时拒绝。
# 注意：不要把利润组再登记进 FIELD_GROUPS.purchase_cost 来"双遮"——那会破坏
# part_ranking 的 profit_restricted 结构语义（只关成本时盈亏分类仍应可见）。
DATA_DATA_DEPENDENCIES: dict[str, str] = {
    "data_profit": "data_purchase_cost",
}


def combo_errors(perms: dict[str, bool]) -> list[str]:
    """校验**最终生效**权限（模板+自定义叠加后）的非法组合，返回人话错误清单。"""
    errors: list[str] = []
    for action_key, data_key in ACTION_DATA_DEPENDENCIES.items():
        if perms.get(action_key, False) and not perms.get(data_key, False):
            errors.append(
                f"「{LABELS.get(action_key, action_key)}」需要同时开启"
                f"「{LABELS.get(data_key, data_key)}」——能设置就必须能查看，"
                f"否则会在看不见现值的情况下改写它")
    for src_key, dep_key in DATA_DATA_DEPENDENCIES.items():
        if perms.get(src_key, False) and not perms.get(dep_key, False):
            errors.append(
                f"「{LABELS.get(src_key, src_key)}」需要同时开启"
                f"「{LABELS.get(dep_key, dep_key)}」——营收全员可见，"
                f"营收减毛利即可精确反推被隐藏的采购成本，单开毛利等于没遮成本")
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
