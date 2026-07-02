"""按用户的细粒度权限（管理员在"账号管理"页逐项勾选）。

三类开关：
- data_*  数据字段可见：驱动 security.apply_field_visibility——关掉则该类字段在所有接口被抹成 null。
- page_*  页面可用：驱动前端菜单显示 + 后端接口准入（require_page）。
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
}
# 页面开关（与前端菜单 key 对齐）
PAGE_KEYS: list[str] = [
    "page_parts", "page_purchases", "page_profit",
    "page_inventory", "page_chat", "page_import", "page_governance",
    "page_master_data", "page_maintenance",
]
ROW_KEYS: list[str] = ["own_customers_only"]
ALL_KEYS: list[str] = [*DATA_GROUPS, *PAGE_KEYS, *ROW_KEYS]
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
    "own_customers_only": "只看自己成交的客户（防恶性竞争）",
}


def _full(own: bool = False) -> dict[str, bool]:
    """全部数据 + 全部页面打开；own_customers_only 单独给。"""
    d = {k: True for k in DATA_GROUPS}
    d.update({k: True for k in PAGE_KEYS})
    d["own_customers_only"] = own
    return d


# 角色模板：建号选角色时套用，可逐项微调（admin 例外，恒全开）
ROLE_TEMPLATES: dict[str, dict[str, bool]] = {
    "admin": _full(),
    "boss": _full(),
    "readonly": {**_full(), "page_import": False, "page_governance": False,
                 "page_master_data": False},
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


def hidden_groups(perms: dict | None) -> set[str]:
    """据 data_* 开关算出要隐藏的 FIELD_GROUPS 组名集合。"""
    if not perms:
        return set()
    hidden: set[str] = set()
    for key, groups in DATA_GROUPS.items():
        if not perms.get(key, False):
            hidden.update(groups)
    return hidden
