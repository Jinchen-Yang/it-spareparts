"""按用户的细粒度权限（管理员在"账号管理"页逐项勾选）。

四类开关：
- data_*  数据字段可见：驱动 security.apply_field_visibility——关掉则该类字段在所有接口被抹成 null。
- page_*  页面可用：驱动前端菜单显示 + 后端接口准入（require_page）。
- action_* 动作可用：驱动写操作准入（require_action）+ 前端动作入口显隐。
- own_customers_only  行级：销售只看自己成交的客户（同事客户名匿名）。

每个账号把自定义存在 sys_user.permissions(JSONB)；为空 → 回退该 role 的模板(ROLE_TEMPLATES)。
admin 的常规权限恒为全开（不可被自己/他人锁死）；两个生产 Beta 页面例外，必须按实名账号
的模板快照与稀疏覆盖显式加入白名单。权限随登录写进 token，改权限后旧 token 立即吊销。
"""
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
    # 维保展示板（plan v1.3）：老板全范围查看入口。flag maintenance_boss_dashboard_enabled
    # 关闭时整组路由 404；打开后本键或 page_maintenance（范围按 M0-B）可读。
    "page_maintenance_boss",
    # 维保新工作台 Beta：稳定版 page_maintenance 仍是基础权限；本键仅给
    # 明确进入灰度名单的账号，关闭后不影响原维保页面与接口。
    "page_maintenance_beta",
    # 销售经理补库购物车 Beta：独立于稳定版库存/维保页面，包括管理员在内，
    # 试用账号由权限中心逐个显式授权。
    "page_replenishment_beta",
    # 权限中心 v2：账号与权限中心页面（只读查看账号/模板/活动）。critical 级——
    # 内置模板对所有非 admin 角色显式 False，保持"仅管理员可见账号管理"的既有行为。
    "page_accounts",
]
# 这两个页面不是角色能力，而是生产灰度名单。即使 role=admin，也必须从实名账号
# template_perms ⊕ perm_overrides 得到 True；旧 token/共享口令/缺失账号快照一律失败关闭。
ACCOUNT_SCOPED_BETA_PAGE_KEYS: frozenset[str] = frozenset({
    "page_maintenance_beta",
    "page_replenishment_beta",
})
# 回款提醒两个写动作同为实名白名单能力（设计 §9）：不进入 admin 常规全开图，
# require_action 的 admin 短路不适用；必须由 security.require_explicit_account_action
# 读取实名账号快照⊕覆盖后显式放行。迁移把存量模板与账号中的这两个键强制回填 false。
ACCOUNT_SCOPED_ACTION_KEYS: frozenset[str] = frozenset({
    "action_maintenance_collection_follow_up",
    "action_maintenance_collection_plan_import",
})
# 动作开关：写操作准入（require_action）。各动作按模板失败关闭，可在账号管理页单独授权。
ACTION_KEYS: list[str] = [
    "action_pool_manage",      # 建池/改名称说明/增删成员/归档恢复
    "action_pool_set_policy",  # 设置采购最高价 / 销售最低价
    # 权限中心 v2：账号与权限的写能力（建号/改权/改密/停用/批量/模板管理）。
    # 授予/撤销本键与 page_accounts 仅限 admin 角色操作者（api/accounts._guard_account_write）。
    "action_account_manage",
    "action_data_quality_review",  # 逐条核实采购/销售事实疑点
    # 直接应用固定维保回填工作簿（原子写订单/报销/人工成本），不走审批。
    "action_maintenance_roundtrip_apply",
    # 项目经理本人范围月度全量工作簿：校验可读，应用另行授权。
    "action_maintenance_manager_workbook_apply",
    # 维护稳定维保项目主档（建档/改展示信息/归档恢复）。
    "action_maintenance_project_manage",
    # WBDD 整单逻辑删除（跨页复核 + 服务端 7 秒双确认）。
    "action_maintenance_demand_delete",
    # 新建、确认、更正和作废现场实际领用单；与库存写入严格隔离。
    "action_maintenance_site_issue_manage",
    # 登记、提交和仓库确认坏件返还；不直接修改成本或库存。
    "action_maintenance_bad_return_manage",
    # 验收报告提交与审批严格分权；审批在业务角色未定前默认仅 admin。
    "action_maintenance_acceptance_submit",
    "action_maintenance_acceptance_review",
    # 仓库单据落库与关联歧义人工裁决（实名、高风险、默认仅管理员）。
    "action_maintenance_warehouse_manage",
    # 成本/库存切换 dry-run、实名对账与双人审批；不包含生产激活。
    "action_maintenance_migration_review",
    # 台账工作簿导入应用：项目/合同/期限/回款计划唯一事实源同步（admin 默认）。
    "action_maintenance_ledger_import",
    # 氚云四单导入（发货/入库/返库/报销）raw 落库与应用（写前置库；默认关闭）。
    "action_maintenance_doc_import",
    # 维保备件需求单（WBDD）专用上传（plan v1.3 M1-6）：只接受 WBDD 文件，
    # 不复用 /api/import/upload 全家桶；文件无价格列故不挂数据组依赖。
    "action_maintenance_wbdd_import",
    # 报销/回款往返工作簿上传覆盖（AB-3）；能改金额 → 依赖 data_profit。
    "action_maintenance_expense_collection_upload",
    # 回款提醒（设计 §9）：标记已处理/改期/重新打开；以及 XLS 回款计划
    # 导入的预览/绑定候选查询/应用。两者都是实名白名单能力：admin 也不得
    # 通过 require_action 短路，必须由 security.require_explicit_account_action
    # 读取账号快照⊕覆盖后显式放行（ACCOUNT_SCOPED_ACTION_KEYS）。
    "action_maintenance_collection_follow_up",
    "action_maintenance_collection_plan_import",
    # Beta 补库申请的创建/复提与审核结果回写严格分权。审核 Agent 本身不在系统内实现。
    "action_replenishment_create",
    "action_replenishment_review",
    # Agent 直查数据库（DSH itdata 插件 text2sql 执行口 /api/agent/sql）。
    # 只读 + 字段级脱敏 + 表黑名单，但仍越过业务层行级过滤，故按账号显式授权：
    # 模板全 False（admin 走 require_action 短路恒放行），权限中心逐人开。
    "action_agent_sql",
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
    "page_maintenance_beta": "维保管理",
    "data_pool_price_governance": "池价格治理（约束价/越线差额）",
    "action_pool_manage": "互通PN池维护（建池/成员/归档）",
    "action_pool_set_policy": "池约束价设置（采购上限/销售下限）",
    "own_customers_only": "只看自己成交的客户（防恶性竞争）",
    "page_accounts": "账号与权限中心（查看）",
    "action_account_manage": "账号与权限管理（建号/改权/批量/模板）",
    "action_data_quality_review": "数据疑点核实（逐条确认/重新打开）",
    "action_maintenance_roundtrip_apply": "维保固定工作簿直接回填",
    "action_maintenance_manager_workbook_apply": "项目经理月度全量表确认应用",
    "action_maintenance_project_manage": "维保项目主档管理",
    "action_maintenance_demand_delete": "维保需求单安全删除",
    "action_maintenance_site_issue_manage": "现场备件领用管理",
    "action_maintenance_bad_return_manage": "维保坏件返还管理",
    "action_maintenance_acceptance_submit": "维保验收报告提交与附件上传",
    "action_maintenance_acceptance_review": "维保验收报告高风险审批",
    "action_maintenance_warehouse_manage": "仓库单据导入与歧义裁决",
    "action_maintenance_migration_review": "维保迁移对账与审批",
    "action_maintenance_ledger_import": "台账工作簿导入应用（项目/合同/回款计划同步）",
    "action_maintenance_doc_import": "氚云单据导入应用（发货/入库/返库/报销；发货入前置库）",
    "page_maintenance_boss": "维保展示板（老板全范围）",
    "action_maintenance_wbdd_import": "维保需求单（WBDD）专用上传",
    "action_maintenance_expense_collection_upload": "报销/回款工作簿上传覆盖",
    "action_maintenance_collection_follow_up": "回款提醒跟进（标记已处理/改期/重新打开）",
    "action_maintenance_collection_plan_import": "回款计划导入（预览/绑定/应用）",
    "page_replenishment_beta": "补库申请",
    "action_replenishment_create": "补库申请创建与复提",
    "action_replenishment_review": "补库审核结果回写",
    "action_agent_sql": "Agent 直查数据库（text2sql，只读+脱敏）",
}


def _full(own: bool = False) -> dict[str, bool]:
    """全部数据 + 全部页面 + 全部动作打开；own_customers_only 单独给。

    回款提醒两个动作是实名白名单能力，即使 role=admin 也不在常规全开图中：
    必须由账号快照⊕覆盖显式授权（ACCOUNT_SCOPED_ACTION_KEYS）。
    """
    d = {k: True for k in DATA_GROUPS}
    d.update({k: True for k in PAGE_KEYS})
    d.update({k: True for k in ACTION_KEYS})
    for key in ACCOUNT_SCOPED_ACTION_KEYS:
        d[key] = False
    d["own_customers_only"] = own
    return d


def admin_account_defaults() -> dict[str, bool]:
    """Fail-closed snapshot for a named admin when the DB template is unavailable."""
    graph = _full()
    for key in ACCOUNT_SCOPED_BETA_PAGE_KEYS:
        graph[key] = False
    return graph


# 角色模板：建号选角色时套用，可逐项微调。admin 常规权限恒全开；Beta 页面
# 的生产默认值由各自迁移写成 False，并由 effective_for_user 尊重账号快照/覆盖。
# 权限中心 v2 起，这份 Python 字典只作三类回退用：guest/匿名兜底、无 perms 的旧 token、
# 共享口令回退登录。正常账号的权限底座来自 sys_role_template 套用时的快照（sys_user.template_perms）。
ROLE_TEMPLATES: dict[str, dict[str, bool]] = {
    "admin": _full(),
    # boss 由 _full() 生成会把全部动作打开；账号管理与数据疑点核实必须显式关闭。
    "boss": {**_full(), "page_accounts": False, "action_account_manage": False,
             "action_data_quality_review": False,
             "page_maintenance_beta": False,
             "action_maintenance_manager_workbook_apply": False,
             "action_maintenance_project_manage": False,
              "action_maintenance_demand_delete": False,
              "action_maintenance_site_issue_manage": False,
             "action_maintenance_bad_return_manage": False,
             "action_maintenance_acceptance_submit": False,
             "action_maintenance_acceptance_review": False,
             "action_maintenance_warehouse_manage": False,
             "action_maintenance_migration_review": False,
             "action_maintenance_ledger_import": False,
             "action_maintenance_doc_import": False,
             "page_maintenance_boss": False,
             "action_maintenance_wbdd_import": False,
             "action_maintenance_expense_collection_upload": False,
             "page_replenishment_beta": False,
             "action_replenishment_create": False,
             "action_replenishment_review": False,
             "action_agent_sql": False},
    # readonly 也是 _DEFAULT + 未认证 guest 的兜底模板：page_maintenance/page_boss_board 必须显式关，
    # 否则未知/匿名角色继承 _full() 里的 True，凭 require_page 即可读（方案 §5）。
    # 池写权限（action_pool_*）同理必须显式关——匿名 guest 决不能建池/改约束价；
    # page_pool_analysis / data_pool_price_governance 按规格 §12 全员开（普通员工可看池与约束价）。
    "readonly": {**_full(), "page_import": False, "page_governance": False,
                 "page_master_data": False, "page_maintenance": False,
                 "page_maintenance_beta": False,
                 "page_boss_board": False,
                 "action_pool_manage": False, "action_pool_set_policy": False,
                 "action_data_quality_review": False,
                 "action_maintenance_roundtrip_apply": False,
                 "action_maintenance_manager_workbook_apply": False,
                 "action_maintenance_project_manage": False,
                  "action_maintenance_demand_delete": False,
                  "action_maintenance_site_issue_manage": False,
                 "action_maintenance_bad_return_manage": False,
                 "action_maintenance_acceptance_submit": False,
                 "action_maintenance_acceptance_review": False,
                 "action_maintenance_warehouse_manage": False,
                 "action_maintenance_migration_review": False,
             "action_maintenance_ledger_import": False,
             "action_maintenance_doc_import": False,
                 "page_maintenance_boss": False,
                 "action_maintenance_wbdd_import": False,
                 "action_maintenance_expense_collection_upload": False,
                 "page_replenishment_beta": False,
                 "action_replenishment_create": False,
                 "action_replenishment_review": False,
                 # Agent 直查数据库按账号显式授权，guest/readonly 兜底模板决不能带
                 "action_agent_sql": False,
                 # 账号管理两键必须显式关（同 boss 注释；guest 兜底模板决不能看/管账号）
                 "page_accounts": False, "action_account_manage": False},
    "sales": {
        # 甲方 2026-06-15 确认：销售能看采购成本/毛利（整机拆解加点直卖需要采购价算建议售价）。
        # 供应商仍隐藏（不暴露从谁进货）；逐单销售成交明细另由 own_customers_only 收紧（看不到）。
        "data_supplier": False, "data_customer": True,
        "data_purchase_cost": True, "data_profit": True,
        # page_purchases=True：合同重点"销售和采购都能查最近采购记录"。
        # page_profit=False：销售默认不开放利润分析；管理员可按账号显式授予页面权限。
        "page_parts": True, "page_purchases": True, "page_profit": False,
        "page_inventory": True, "page_chat": True,
        # 项目成本=公司维保项目经营数据，销售不开（同 page_profit 口径）
        "page_maintenance": False,
        "page_maintenance_beta": False,
        # 老板经营看板=全公司经营/个人排名，销售不开
        "page_boss_board": False,
        # 互通池价格分析全员可见（§12），约束价对全员公开；池维护/约束设置默认不开
        "page_pool_analysis": True,
        "data_pool_price_governance": True,
        "action_pool_manage": False, "action_pool_set_policy": False,
        "action_data_quality_review": False,
        "action_maintenance_roundtrip_apply": False,
        "action_maintenance_manager_workbook_apply": False,
        "action_maintenance_project_manage": False,
        "action_maintenance_demand_delete": False,
        "action_maintenance_site_issue_manage": False,
        "action_maintenance_bad_return_manage": False,
        "action_maintenance_acceptance_submit": False,
        "action_maintenance_acceptance_review": False,
        "action_maintenance_warehouse_manage": False,
        "action_maintenance_migration_review": False,
             "action_maintenance_ledger_import": False,
             "action_maintenance_doc_import": False,
        "page_maintenance_boss": False,
        "action_maintenance_wbdd_import": False,
        "action_maintenance_expense_collection_upload": False,
        "action_maintenance_collection_follow_up": False,
        "action_maintenance_collection_plan_import": False,
        "page_replenishment_beta": False,
        "action_replenishment_create": False,
        "action_replenishment_review": False,
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
        "page_maintenance_beta": False,
        # 老板经营看板=全公司经营/个人排名，采购不开
        "page_boss_board": False,
        # 互通池价格分析全员可见（§12），约束价对全员公开；池维护/约束设置默认不开
        "page_pool_analysis": True,
        "data_pool_price_governance": True,
        "action_pool_manage": False, "action_pool_set_policy": False,
        "action_data_quality_review": False,
        # 采购默认没有利润可见权限，故固定工作簿写入也默认失败关闭；
        # 管理员可给同时具备成本+利润可见权限的指定工作人员单独授权。
        "action_maintenance_roundtrip_apply": False,
        "action_maintenance_manager_workbook_apply": False,
        "action_maintenance_project_manage": False,
        "action_maintenance_demand_delete": False,
        "action_maintenance_site_issue_manage": False,
        "action_maintenance_bad_return_manage": False,
        "action_maintenance_acceptance_submit": False,
        "action_maintenance_acceptance_review": False,
        "action_maintenance_warehouse_manage": False,
        "action_maintenance_migration_review": False,
             "action_maintenance_ledger_import": False,
             "action_maintenance_doc_import": False,
        "page_maintenance_boss": False,
        "action_maintenance_wbdd_import": False,
        "action_maintenance_expense_collection_upload": False,
        "action_maintenance_collection_follow_up": False,
        "action_maintenance_collection_plan_import": False,
        "page_replenishment_beta": False,
        "action_replenishment_create": False,
        "action_replenishment_review": False,
        "own_customers_only": False,
    },
}
_DEFAULT = ROLE_TEMPLATES["readonly"]


def template_for(role: str) -> dict[str, bool]:
    return dict(ROLE_TEMPLATES.get(role, _DEFAULT))


def effective(role: str, custom: dict | None) -> dict[str, bool]:
    """旧角色口径：role 模板打底、custom 逐项覆盖；admin 返回传统全开图。

    实名账号运行时必须使用 ``effective_for_user``。该入口保留传统 admin 全开，供旧 token
    与历史迁移对账；Beta 页不会再通过 security 的 admin 短路或缺失权限图获得放行。
    """
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
    # 逐条确认必须看得到原始价格和规则证据，不能在证据被脱敏时盲判。
    "action_data_quality_review": "data_purchase_cost",
    "action_maintenance_roundtrip_apply": "data_profit",
    "action_maintenance_manager_workbook_apply": "data_profit",
    "action_maintenance_project_manage": "data_profit",
    "action_maintenance_site_issue_manage": "data_purchase_cost",
    "action_maintenance_migration_review": "data_profit",
    # 台账导入能看到合同额与计划金额：能改必须能看（follow-up 不需金额可见性）。
    "action_maintenance_ledger_import": "data_profit",
    # 回款计划导入能看到计划金额与合同额：能改必须能看（follow-up 不需金额可见性）。
    "action_maintenance_collection_plan_import": "data_profit",
    "action_maintenance_ledger_import": "data_profit",
    # 报销/回款工作簿能改金额并写回累计回款：能改必须能看（AB-3）。
    "action_maintenance_expense_collection_upload": "data_profit",
    "action_maintenance_doc_import": "data_purchase_cost",
    "action_replenishment_create": "data_pool_price_governance",
}

# "页面内操作必须能进页面"的动作→页面依赖（权限中心 v2）：改账号权限先要能打开
# 账号管理页看到现状。池两动作不在此表——互通PN池管理页的入口就是这两把动作钥匙本身
# （nav anyPerm），没有独立的 page_* 键。
ACTION_PAGE_DEPENDENCIES: dict[str, str] = {
    "action_account_manage": "page_accounts",
    "action_data_quality_review": "page_governance",
    "action_maintenance_roundtrip_apply": "page_maintenance",
    "action_maintenance_manager_workbook_apply": "page_maintenance",
    "action_maintenance_project_manage": "page_maintenance",
    "action_maintenance_demand_delete": "page_maintenance",
    "action_maintenance_site_issue_manage": "page_maintenance",
    "action_maintenance_bad_return_manage": "page_maintenance",
    "action_maintenance_acceptance_submit": "page_maintenance",
    "action_maintenance_acceptance_review": "page_maintenance",
    "action_maintenance_warehouse_manage": "page_maintenance",
    "action_maintenance_migration_review": "page_maintenance",
    "action_maintenance_ledger_import": "page_maintenance",
    "action_maintenance_doc_import": "page_maintenance",
    "action_maintenance_wbdd_import": "page_maintenance",
    "action_maintenance_expense_collection_upload": "page_maintenance",
    "action_maintenance_collection_follow_up": "page_maintenance",
    "action_maintenance_collection_plan_import": "page_maintenance",
    "action_replenishment_create": "page_replenishment_beta",
}

# 新维保动作同时依赖稳定版基础权限和 Beta 白名单。本表是叠加约束，保留
# ACTION_PAGE_DEPENDENCIES 中既有的 page_maintenance 映射，避免历史权限契约漂移。
ACTION_ADDITIONAL_PAGE_DEPENDENCIES: dict[str, str] = {
    "action_maintenance_manager_workbook_apply": "page_maintenance_beta",
    "action_maintenance_project_manage": "page_maintenance_beta",
    "action_maintenance_demand_delete": "page_maintenance_beta",
    "action_maintenance_site_issue_manage": "page_maintenance_beta",
    "action_maintenance_bad_return_manage": "page_maintenance_beta",
    "action_maintenance_acceptance_submit": "page_maintenance_beta",
    "action_maintenance_acceptance_review": "page_maintenance_beta",
    "action_maintenance_warehouse_manage": "page_maintenance_beta",
    "action_maintenance_migration_review": "page_maintenance_beta",
    "action_maintenance_ledger_import": "page_maintenance_beta",
    "action_maintenance_doc_import": "page_maintenance_beta",
    "action_maintenance_collection_follow_up": "page_maintenance_beta",
    "action_maintenance_collection_plan_import": "page_maintenance_beta",
}

# Beta 只是稳定维保能力之上的附加入口，禁止出现“看不到稳定版却能进 Beta”的孤岛权限。
PAGE_PAGE_DEPENDENCIES: dict[str, str] = {
    "page_maintenance_beta": "page_maintenance",
}

# 数据之间的可推导依赖：营收在经营报表中是公开口径，毛利一旦可见，
# ``营收 - 毛利`` 就能精确反推出采购成本。因此 data_profit 只能在同时持有
# data_purchase_cost 时开启。反向（看成本、不看利润）合法，采购模板正是此口径。
DATA_DATA_DEPENDENCIES: dict[str, str] = {
    "data_profit": "data_purchase_cost",
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
    for action_key, page_key in ACTION_ADDITIONAL_PAGE_DEPENDENCIES.items():
        if perms.get(action_key, False) and not perms.get(page_key, False):
            errors.append(
                f"「{LABELS.get(action_key, action_key)}」需要同时开启"
                f"「{LABELS.get(page_key, page_key)}」——该操作仅在灰度页面中开放")
    for page_key, required_page_key in PAGE_PAGE_DEPENDENCIES.items():
        if perms.get(page_key, False) and not perms.get(required_page_key, False):
            errors.append(
                f"「{LABELS.get(page_key, page_key)}」需要同时开启"
                f"「{LABELS.get(required_page_key, required_page_key)}」——正式工作台沿用基础页面的数据边界")
    for data_key, required_key in DATA_DATA_DEPENDENCIES.items():
        if perms.get(data_key, False) and not perms.get(required_key, False):
            errors.append(
                f"「{LABELS.get(data_key, data_key)}」需要同时开启"
                f"「{LABELS.get(required_key, required_key)}」——营收减毛利可精确"
                f"反推出采购成本，不能只开放利润而隐藏成本")
    return errors


def runtime_safe(perms: dict | None) -> dict[str, bool]:
    """把可能来自历史脏数据/旧 token 的非法数据权限组合收紧为安全图。

    保存路径必须用 ``combo_errors`` 明确拒绝，不能静默改用户选择；本函数只用于
    运行时纵深防御：依赖缺失时关闭被依赖的数据权限。例如历史账号若仍是
    ``data_profit=True, data_purchase_cost=False``，运行时只会得到利润关闭。
    """
    safe = normalize(perms)
    for data_key, required_key in DATA_DATA_DEPENDENCIES.items():
        if safe.get(data_key, False) and not safe.get(required_key, False):
            safe[data_key] = False
    for page_key, required_page_key in PAGE_PAGE_DEPENDENCIES.items():
        if safe.get(page_key, False) and not safe.get(required_page_key, False):
            safe[page_key] = False
    return safe


def hidden_groups(perms: dict | None) -> set[str]:
    """据 data_* 开关算出要隐藏的 FIELD_GROUPS 组名集合。

    ``None`` 表示调用方尚未解析权限，应由 security._hidden_fields 按角色模板回退；
    ``{}`` 则是明确的“零权限图”，normalize 后所有 data_* 都为 false，必须全隐藏。
    """
    if perms is None:
        return set()
    # 构造 UserContext 的测试、旧 token 或存量脏账号可能绕过新保存校验；字段层仍按
    # 安全图隐藏，保证所有 apply_field_visibility/is_field_hidden 调用方都失败关闭。
    perms = runtime_safe(perms)
    hidden: set[str] = set()
    for key, groups in DATA_GROUPS.items():
        if not perms.get(key, False):
            hidden.update(groups)
    return hidden


# ══════════════════════════ 权限中心 v2 ══════════════════════════

# 高风险键：授予/撤销仅限 admin 角色操作者（防非 admin 的账号管理代理自我提权/互相提权）
HIGH_RISK_KEYS: set[str] = {
    "page_maintenance_beta",
    "page_maintenance_boss",
    "page_replenishment_beta",
    "page_accounts",
    "action_account_manage",
    "action_maintenance_roundtrip_apply",
    "action_maintenance_manager_workbook_apply",
    "action_maintenance_project_manage",
    "action_maintenance_demand_delete",
    "action_maintenance_site_issue_manage",
    "action_maintenance_bad_return_manage",
    "action_maintenance_acceptance_review",
    "action_maintenance_warehouse_manage",
    "action_maintenance_migration_review",
    "action_maintenance_collection_follow_up",
    "action_maintenance_collection_plan_import",
    "action_maintenance_ledger_import",
    "action_maintenance_doc_import",
    "action_maintenance_wbdd_import",
    "action_maintenance_expense_collection_upload",
    "action_replenishment_review",
}

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
     "keys": [
         "action_pool_manage",
         "action_pool_set_policy",
         "action_data_quality_review",
         "action_maintenance_roundtrip_apply",
         "action_maintenance_manager_workbook_apply",
         "action_maintenance_project_manage",
         "action_maintenance_demand_delete",
         "action_maintenance_site_issue_manage",
         "action_maintenance_bad_return_manage",
         "action_maintenance_acceptance_submit",
         "action_maintenance_acceptance_review",
         "action_maintenance_warehouse_manage",
         "action_maintenance_collection_follow_up",
         "action_maintenance_collection_plan_import",
         "action_replenishment_create",
         "action_replenishment_review",
         "action_agent_sql",
     ]},
    {"key": "row", "label": "行级范围",
     "hint": "在能看的数据里进一步收紧范围（限制型开关：勾上=看得更少）。",
     "keys": list(ROW_KEYS)},
    {"key": "admin", "label": "高风险管理能力",
     "hint": "接近管理员的能力，只有管理员本人可以授予或撤销，请谨慎开放。",
     "keys": ["page_accounts", "action_account_manage",
              "action_maintenance_migration_review",
               "action_maintenance_ledger_import",
               "action_maintenance_doc_import",
               "action_maintenance_wbdd_import",
               "action_maintenance_expense_collection_upload"]},
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
        "summary": "允许查看利润成本、库存成本及维保项目成本等公司成本数据。",
        "can": "利润计算使用的采购成本、库存移动加权/FIFO 成本、维保项目成本明细。",
        "cannot": "不代表可以修改采购数据；关闭后所有页面、导出、AI 助手中相关字段一律置空，无法靠排序或差额反推。",
        "typical": ["采购", "销售", "老板"],
        "sensitivity": "high",
        "risk": "泄露进货底价，影响对外报价谈判空间。",
    },
    "data_profit": {
        "label": "查看利润 / 毛利",
        "summary": "允许查看毛利额、毛利率与盈亏排行。",
        "can": "单据与汇总的毛利额、毛利率、赚钱/亏钱排行榜。",
        "cannot": "必须同时开启「查看采购成本」；否则营收减毛利会反推出采购成本。关闭后连「哪个型号在赚钱榜还是亏损榜」的归类都不返回。",
        "typical": ["老板", "销售"],
        "sensitivity": "high",
        "risk": "暴露公司真实盈利水平。",
    },
    "data_pool_price_governance": {
        "label": "查看互通池价格纪律",
        "summary": "允许查看互通PN池的历史采购/销售价、池均价、人工约束价和越线事实。",
        "can": "池内历史采购价与销售价、经办人、池均价/中位价、人工上下限、差额与越线标记。",
        "cannot": "供应商和客户仍按各自权限隐藏；不代表可以设置约束价（另由「池约束价设置」控制）。",
        "typical": ["全员（甲方口径：约束价对员工公开）"],
        "sensitivity": "medium",
        "risk": "关闭后池价格、约束价、差额、越线标记和价格排序会一起隐藏，避免通过顺序或颜色反推。",
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
    "page_maintenance_boss": {
        "label": "维保展示板（老板全范围）",
        "summary": "可打开维保展示板首屏与全项目列表，查看四源健康、本期变化与项目证据下钻（全项目范围）。",
        "can": "四源 readiness/截止日期、orders_ytd/lines_ytd、全部项目分页列表、单据/PN 证据下钻、未归属桶。",
        "cannot": "成本金额仍由「查看采购成本」控制（无权限时相关字段与排序整体受限，无侧信道）；服务端总闸关闭时页面整体 404。",
        "typical": ["老板", "管理员"],
        "sensitivity": "critical",
        "risk": "全部维保项目的申请与成本全景，默认关闭、逐账号勾选。",
    },
    "action_maintenance_wbdd_import": {
        "label": "维保需求单（WBDD）专用上传",
        "summary": "允许通过维保专用端点上传氚云维保备件需求单（90/91 列导出），快照式更新需求单事实并触发成本回填。",
        "can": "上传 WBDD .xlsx（自动识别 90/91 列布局），返回精确对账报告（计数/快照差异/成本重算统计）；同幂等键重放返回原报告。",
        "cannot": "不能上传采购/销售/库存/报销文件（非 WBDD 一律 422 零写入）；不含通用导入页 page_import 的任何能力；不改成本回填列。",
        "typical": ["管理员", "维保数据维护人员（需单独授权）"],
        "sensitivity": "critical",
        "risk": "写维保需求单事实表并触发全表成本重算；默认关闭，仅名单勾选。",
    },
    "action_maintenance_expense_collection_upload": {
        "label": "报销/回款工作簿上传覆盖",
        "summary": "允许上传报销/回款往返工作簿（04_报销订单＋05_项目经理回款单 两张 sheet 合一），按上传内容覆盖报销未税金额与月度累计回款快照。",
        "can": "下载本项目工作簿、预演（validate）与应用（apply）；同合同同月份重传即覆盖累计回款额；显式 VOID 作废历史快照。",
        "cannot": "不能新增报销单（报销单在源系统产生，本表只改金额）；不能直填含税金额（系统按未税×1.13 计算）；不录回款计划（唯一事实源是台账 02_回款计划）。",
        "typical": ["项目经理", "商务"],
        "sensitivity": "high",
        "risk": "改写项目报销金额与累计回款事实，直接影响成本率与回款进度；默认关闭。",
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
    "page_replenishment_beta": {
        "label": "补库申请",
        "summary": "可打开补库申请页面，用购物车方式准备前置库补库申请。",
        "can": "打开补库申请页面；同时具备价格数据权限时可只读查看本人申请与历史版本。",
        "cannot": "搜索价格事实还需「池价格治理」，维护/提交还需「补库申请创建与复提」；不代表可回写审核结果。",
        "typical": ["管理员", "已授权的销售经理"],
        "sensitivity": "high",
        "risk": "页面、价格数据、申请操作是三把独立钥匙；服务端功能开关关闭时拒绝全部业务请求。",
    },
    "page_maintenance_beta": {
        "label": "维保管理",
        "summary": "可进入维保管理正式工作台，并继续使用兼容入口。",
        "can": "在稳定版入口不变的前提下，使用项目面板、需求删除、经理月报、现场领用、坏件返还、仓库单据、验收和迁移核对。",
        "cannot": "不自动获得任何写操作或敏感数据权限；服务端功能开关关闭时所有新工作台接口均不可用。",
        "typical": ["管理员", "已授权的项目经理"],
        "sensitivity": "critical",
        "risk": "直接接触同一生产数据库中的新业务流程，必须逐账号白名单开放，并保留稳定版回退入口。",
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
    "action_data_quality_review": {
        "label": "数据疑点核实",
        "summary": "允许逐条确认采购/销售事实疑点，并在有新依据时重新打开。",
        "can": "填写原因后确认数据正确、确认源数据错误，或撤销旧结论重新核实。",
        "cannot": "必须同时持有「查看采购成本」；不能批量确认、不能修改原始订单，也不会因此自动排除利润、库存或池分析统计。",
        "typical": ["管理员", "数据维护人员（需单独授权）"],
        "sensitivity": "high",
        "risk": "结论会实名留痕，并为后续正式参考口径提供依据；必须看着原始证据逐条判断。",
    },
    "action_maintenance_roundtrip_apply": {
        "label": "维保固定工作簿直接回填",
        "summary": "允许把系统导出的固定维保工作簿直接、原子地写回订单、报销和人工成本。",
        "can": "在签名合同及日期范围内执行 CREATE、UPDATE、VOID；成功后立即生效，不走审批。",
        "cannot": "不能越过模板签名范围，不能绕过成本与利润可见权限；同一行键改成不同内容会被拒绝。",
        "typical": ["老板", "管理员指定的数据维护人员"],
        "sensitivity": "critical",
        "risk": "会直接改写经营事实并触发成本重算；默认仅管理员和老板开启，其他工作人员须由管理员单独授权。",
    },
    "action_maintenance_manager_workbook_apply": {
        "label": "项目经理月度全量表确认应用",
        "summary": "允许把本人项目范围内、已通过整表校验的月度 v3 工作簿原子写入。",
        "can": "写入维保期限、验收截止日和最多 24 期计划回款节点，并关闭对应月度任务。",
        "cannot": "不能修改财务确认实收，不能越过实名项目负责人范围，也不能绕过版本冲突。",
        "typical": ["管理员授权的项目经理"],
        "sensitivity": "critical",
        "risk": "会直接写入项目跟踪事实；默认对所有非管理员角色关闭，必须逐账号授权。",
    },
    "action_maintenance_project_manage": {
        "label": "维保项目主档管理",
        "summary": "允许新建稳定维保项目，并维护展示信息、归档与恢复。",
        "can": "建立不可变项目身份，修改展示名称和负责人标识，归档或恢复项目主档。",
        "cannot": "不能删除项目、修改稳定项目编号，也不能新增或修改项目合同关系。",
        "typical": ["管理员", "项目数据维护人员（需单独授权）"],
        "sensitivity": "critical",
        "risk": "项目身份会成为回款、领用、费用和待办的关联地基；误建或误归档会影响后续全链路归集。",
    },
    "action_maintenance_demand_delete": {
        "label": "维保需求单安全删除",
        "summary": "允许把误导入的 WBDD 整单逻辑删除，并从全部有效业务视图中排除。",
        "can": "跨页选择需求单、查看完整复核清单、填写理由并经两次确认后执行可恢复的逻辑删除。",
        "cannot": "不能删除单行备件、不能物理删除原始订单或项目归属；恢复必须走独立实名管理员入口。",
        "typical": ["管理员", "管理员指定的数据维护人员"],
        "sensitivity": "critical",
        "risk": "会改变成本、库存推导、项目看板和导出的有效数据范围；系统强制服务端等待与整批原子校验。",
    },
    "action_maintenance_site_issue_manage": {
        "label": "现场备件领用管理",
        "summary": "允许项目经理建立、确认、更正和作废现场实际领用单。",
        "can": "从稳定发货明细选择备件，保存草稿并确认现场实际消耗；确认后冻结成本证据并生成返还义务接口事件。",
        "cannot": "不能指定系统单号或实体 ID，不能超发货余额，也不会直接修改公司库、地区库或前置库库存。",
        "typical": ["管理员", "项目经理（需单独授权）"],
        "sensitivity": "critical",
        "risk": "确认结果直接进入项目成本并触发后续返还义务；必须同时具备维保页面和采购成本查看权限。",
    },
    "action_maintenance_bad_return_manage": {
        "label": "维保坏件返还管理",
        "summary": "允许按已确认现场领用义务登记、提交并确认坏件返还。",
        "can": "建立返还草稿、登记在途、仓库确认，并保存正式入库的外部稳定引用。",
        "cannot": "不能人工点选豁免，不能超出应返数量，也不会冲减项目成本或直接增加库存。",
        "typical": ["管理员", "项目经理或仓库协同人员（需单独授权）"],
        "sensitivity": "critical",
        "risk": "仓库确认量仅作返还率试算；官方返还率分子待业务确认。操作必须实名、幂等并保留追加式审计。",
    },
    "action_maintenance_acceptance_submit": {
        "label": "维保验收报告提交与附件上传",
        "summary": "允许本人负责项目上传受控附件并提交验收报告。",
        "can": "上传通过安全校验的 PDF、Word、Excel 或图片，并把验收报告提交审核。",
        "cannot": "不能审批自己的提交，不能访问非本人项目，也不能上传外部链接或可执行内容。",
        "typical": ["管理员授权的项目经理"],
        "sensitivity": "high",
        "risk": "会形成正式验收提交事实和持久化附件；建议仅授权真实项目负责人。",
    },
    "action_maintenance_acceptance_review": {
        "label": "维保验收报告高风险审批",
        "summary": "允许批准或驳回已提交的维保验收报告。",
        "can": "查看受控附件后批准或填写理由驳回；全部操作实名审计。",
        "cannot": "不能审批本人提交、不能审批未提交或没有有效附件的报告。",
        "typical": ["管理员（业务审批角色确定前）"],
        "sensitivity": "critical",
        "risk": "审批结果是正式业务结论；业务审批角色尚未配置，默认仅 admin 可用。",
    },
    "action_maintenance_warehouse_manage": {
        "label": "仓库单据导入与歧义裁决",
        "summary": "允许把仓库导出单据固化为只读事实，并实名处理无法自动关联的歧义。",
        "can": "先零写预览，再按稳定 ID 原子落库；对多候选、未知版本和字段冲突填写理由后裁决。",
        "cannot": "不能按项目名、日期加 PN 或列数猜关联；不会修改库存、成本或返还率，附件内容也不进入事实库。",
        "typical": ["管理员", "仓库数据维护人员（需单独授权）"],
        "sensitivity": "critical",
        "risk": "人工裁决会成为后续项目归集的正式关系证据，因此要求实名、乐观锁和前后值审计。",
    },
    "action_maintenance_migration_review": {
        "label": "维保迁移对账与审批",
        "summary": "允许生成成本/库存切换 dry-run、实名对账并审批哈希绑定的 manifest。",
        "can": "查看逐项目差异，确认历史成本基线与库存期初，并在职责分离后生成审批 manifest。",
        "cannot": "不能启用生产开关、不能执行生产迁移，也不能用文字理由跳过未解决 blocker。",
        "typical": ["管理员", "独立复核人（需单独授权）"],
        "sensitivity": "critical",
        "risk": "错误审批会把成本和库存切换到错误基线；系统默认仅管理员持有且生产开关仍独立关闭。",
    },
    "action_maintenance_ledger_import": {
        "label": "台账工作簿导入应用",
        "summary": "允许上传维保台账工作簿（项目/合同/期限/回款计划），预览后同步为正式项目与合同事实。",
        "can": "上传台账 Excel 零写入预览，核对行数与异常清单后应用；项目、合同与回款计划以台账为唯一事实源。",
        "cannot": "不能删除台账中没有的历史事实；报销归集行只保留原始记录，待与氚云报销逐条对账后才进入正式统计。",
        "typical": ["管理员", "维保台账维护人员（需单独授权）"],
        "sensitivity": "critical",
        "risk": "应用会批量创建或更新项目与合同事实；金额口径（台账含税额）与销售单未税额自动对账，异常进入清单不静默。",
    },
    "action_maintenance_doc_import": {
        "label": "氚云单据导入应用",
        "summary": "允许上传氚云发货/入库/返库/报销四类单据，预览后落原始事实；发货维保供货会写入项目前置库账本。",
        "can": "上传 .xlsx 零写入预览，查看行数与异常清单后应用；仅已生效单据参与入账，来源事件幂等且带 payload 校验。",
        "cannot": "不能修改原始单元格值；未归属项目、未知 PN 或同来源不同内容重放时整批失败关闭，不按名称猜测。",
        "typical": ["管理员", "仓库数据维护人员（需单独授权）"],
        "sensitivity": "critical",
        "risk": "应用会写入项目前置库结存与流水；默认仅管理员可授予，且要求同时具备成本数据可见权限。",
    },
    "action_maintenance_collection_follow_up": {
        "label": "回款提醒跟进",
        "summary": "允许把本人可见项目的计划回款提醒标记为已处理、改期或重新打开。",
        "can": "对待办计划节点标记已处理（可留备注），把待处理节点改到新的计划月份，或把误处理节点重新打开。",
        "cannot": "不能确认到账或产生任何实收事实；已处理只表示本次提醒跟进完毕；需要稳定版维保页与维保管理灰度页同时可见。",
        "typical": ["已授权的维保负责人"],
        "sensitivity": "high",
        "risk": "写入口会留下不可变操作账本；管理员没有显式授权时即使 role=admin 也失败关闭。",
    },
    "action_maintenance_collection_plan_import": {
        "label": "回款计划导入",
        "summary": "允许上传项目经理 XLS 回款排期，预览差异、人工绑定订单并原子应用计划节点。",
        "can": "上传 .xls 零写入预览，为待绑定订单选择项目和合同，确认后原子应用或幂等重放。",
        "cannot": "不能删除来源缺失节点、不能覆盖人工跟进状态（只标记计划有变更）；必须同时持有利润可见与两个维保页面权限，且首期仅实名 admin。",
        "typical": ["实名管理员（首期）"],
        "sensitivity": "critical",
        "risk": "会批量改写计划节点并留下导入批次证据；admin 没有显式授权时仍失败关闭，apply 还受独立生产开关门禁。",
    },
    "action_replenishment_create": {
        "label": "补库申请创建与复提",
        "summary": "允许维护本人补库购物车、提交不可变版本并处理被打回条目。",
        "can": "新增/修改/移除本人草稿行，提交版本，按打回结果建立下一版并导出。",
        "cannot": "不能查看或修改他人申请，不能审批，不会修改库存或自动生成采购/维保事实。",
        "typical": ["已授权的销售经理"],
        "sensitivity": "high",
        "risk": "提交内容会成为留存业务版本；必须同时具备补库申请页面和池价格查看权限。",
    },
    "action_replenishment_review": {
        "label": "补库审核结果回写",
        "summary": "允许受控回写外部审核结果，不包含审核 Agent 或审批规则本身。",
        "can": "按不可变版本摘要逐行回写批准/打回结果，幂等留痕并形成汇总。",
        "cannot": "不能读取申请目录或历史价格，不能替销售修改内容，也不能自动审批、自动定价或调用外部系统。",
        "typical": ["管理员", "受控审核集成账号"],
        "sensitivity": "critical",
        "risk": "审核结论决定哪些行可进入最终 WBDD 子集导出，仅管理员可授予本权限。",
    },
    "action_agent_sql": {
        "label": "Agent 直查数据库",
        "summary": "允许 AI 助手（DSH 企业插件）以该账号身份执行只读 SQL 查询（text2sql）。",
        "can": "单条只读 SELECT/WITH；结果仍按该账号的数据可见范围逐字段脱敏，敏感系统表不可查。",
        "cannot": "不能写库、不能绕过字段脱敏、不能查凭据/审计表；「只看自己成交的客户」账号整体禁用直查。",
        "typical": ["管理员", "数据岗（显式授权）"],
        "sensitivity": "critical",
        "risk": "越过业务层行级过滤直接读库，仅给确有 text2sql 需要的账号逐人开通。",
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
    """v2 有效权限（账号级单一真值源）。

    admin 的常规权限仍强制全开，防止账号中心把管理员锁死；Beta 页面是灰度名单，
    唯独尊重该实名账号的 template_perms ⊕ perm_overrides。无快照的旧 admin 只读取
    legacy permissions 中明确存在的 Beta 位，缺失时默认 False。
    """
    if user.role == "admin":
        result = _full()
        if getattr(user, "template_perms", None) is not None:
            stored = effective_from_snapshot(user.template_perms, user.perm_overrides)
        else:
            stored = sanitize(getattr(user, "permissions", None))
        for key in ACCOUNT_SCOPED_BETA_PAGE_KEYS:
            result[key] = bool(stored.get(key, False))
        # 回款提醒两个写动作同样只认实名账号快照⊕覆盖，admin 无显式授权保持 false。
        for key in ACCOUNT_SCOPED_ACTION_KEYS:
            result[key] = bool(stored.get(key, False))
        return result
    if getattr(user, "template_perms", None) is not None:
        return effective_from_snapshot(user.template_perms, user.perm_overrides)
    return effective(user.role, user.permissions)


def page_permission_allowed(
    *,
    role: str,
    permission_map: dict | None,
    page_key: str,
) -> bool:
    """Resolve a page gate without letting admin bypass account-scoped Beta pages."""
    if page_key in ACCOUNT_SCOPED_BETA_PAGE_KEYS:
        return isinstance(permission_map, dict) and bool(permission_map.get(page_key, False))
    if role == "admin":
        return True
    graph = permission_map if isinstance(permission_map, dict) else effective(role, None)
    return bool(graph.get(page_key, False))
