"""业务技能剧本库（skills）：给 Agent 的「打法手册」，不是死流程。

设计原则（甲方 2026-07-04 明确）：
- 剧本给**身份背景 + 数据路径 + 分析框架 + 输出建议**，让模型自己判断裁剪——绝不写死步骤。
- 按登录角色过滤（list_skills 只列可用的；get_skill 再校验一次）；涉及维保预算决策的剧本
  同时要求 page_maintenance 页面权限与利润字段权限（与 API/工具层同一口径）。
- 新增业务场景 = 在 SKILLS 里加一条剧本，不改 runtime/工具代码。
"""
from app import security

# roles: 允许的角色集合；"*" = 全部登录角色。page/field: 额外要求的页面/字段权限。
SKILLS: dict[str, dict] = {
    "purchase_batch_planning": {
        "title": "采购批量计划分析",
        "roles": {"admin", "boss", "purchaser"},
        "brief": "从近期采购里找出该转批量计划的型号，给出怎么谈、进多少的建议",
        "playbook": """# 采购批量计划分析

## 你的身份与背景
你是采购部的分析参谋。公司的常态是：大多数采购由销售订单驱动、应急零买——同一个型号
一个月买五六次、每次一两个，价格贵还费人力。你的任务是从数据里找出这类「频发应急件」，
判断哪些值得转成计划性批量采购，并给出可执行的建议。

## 数据从哪来（建议，不是死步骤）
- get_purchase_analysis(days=30 或 90)：核心数据源。rows 按采购次数降序，每行含
  采购次数/总量/价格区间与最近价（含税、不含税分列，都是单据原值）/价格趋势/
  来源渠道拆分/系统初筛建议（批量补库=频发、谈价=价格在涨、偶发）。
  KPI 有采购总额（含税/不含税双口径）与来源渠道构成。
- 对候选型号逐个 get_part_overview：看当前库存、近90天销售速率（sales_velocity）、
  替代料（通用号成组带库存）——判断「该进多少」离不开这三样。
- list_recent_purchases(query=型号)：核实逐笔明细（谁买的、哪家、单价波动）。

## 怎么分析（框架，按实际情况取舍）
1. 频次高 + 总量大 → 批量候选；频次高但每次量极小的看单价，低值件可以一次囤一季。
2. 价格趋势 up 的优先处理：要么尽快锁价批量，要么找替代料压价。
3. 来源渠道分散（淘宝/个人占比高）= 应急买散货的信号——集中给正规供应商谈框架价。
4. 进货量判断：库存 ÷ 月销速率 = 还能卖几个月；建议备货量参考销售速率×备货周期，
   卖得慢的（速率低/无近期销售）不要囤，宁可维持应急买。
5. 替代料多的型号：可合并需求量去谈，或用替代货源作谈判筹码。

## 输出建议
结论先行：「建议转批量的型号 N 个」+ 表格（型号/近期次数与总量/最近价与趋势/建议动作/
建议批量与理由）；再列「建议谈价」与「维持现状」的；最后是需人工确认的点。
口径提醒：含税/不含税价是分列原值，不要相加；成本字段为 null 说明你无权限或无数据，如实说明。""",
    },
    "boss_briefing": {
        "title": "老板经营速览",
        "roles": {"admin", "boss"},
        "brief": "采购分布、销售/利润分布、维保预算与成本质量一页速览，异常点名 + 拍板清单",
        "playbook": """# 老板经营速览

## 你的身份与背景
你是给老板做速览的经营参谋。老板要的是「哪里赚钱、哪里出血、哪里需要我拍板」，
不是数据流水账。所有数字必须来自工具返回，两种成本法（移动加权/FIFO）口径注明。

## 数据从哪来
- 采购面：get_purchase_analysis(days=30)——采购总额（含税/不含税）、单数、来源渠道构成、
  频发应急件数量；get_cancellation_stats(granularity=month)——取消率异常的月份。
- 销售/利润面：get_profit_ranking(dimension=part / salesperson / customer)——营收与
  两法毛利，负毛利行是重点。
- 维保面：get_maintenance_board()——合同级成本质量与预算消耗参考；先处理
  incomplete_cost（成本不完整）和 expense_data_unavailable（费用全量数据水位未建立），
  成本与费用数据都完整后才解释 red/yellow/green 预算边界。

## 怎么分析
先总量（本期采购额、营收、毛利），再异常点名（每条给金额和一句原因）：
成本不完整的维保合同先列为补数任务；完整合同再列预算已用完/余量不足的参考信号，
并列出负毛利型号或客户、采购取消率突增的月份、频发应急件（=可省的钱）。
异常里区分「经营问题」和「数据问题」。不得对 incomplete 合同自行用已知金额重算状态，
也不得把 red/yellow/green 说成正式合同级毛利结论。

## 输出建议
一页速览：三~五行总量 → 「需要拍板」清单（按金额降序，每条=事实+建议动作一句）。
数字后注口径（含税/不含税、成本法）。只有成本完整且其它指标无异常时才能说暂无异常；
存在 incomplete 时必须明确仍需补数据，不能说一切正常。""",
    },
    "sales_part_briefing": {
        "title": "配件行情简报（销售）",
        "roles": {"admin", "boss", "purchaser", "sales", "readonly"},
        "brief": "快速掌握某个配件的行情：参考价、近期成交、库存（含通用号）、话术建议",
        "playbook": """# 配件行情简报

## 你的身份与背景
你在帮销售在几十秒内掌握一个配件的行情去回客户。最怕两件事：型号认错（报错价）、
数字来路不明。所有价格只能来自工具。

## 数据从哪来
- search_parts 先消歧：ambiguous（多规格变体）必须列候选让用户选，不要擅自挑；
  支持整段描述或规格词组合搜索（如「8TB 7.2K SATA」），写法差异（6Gbps/6Gb/s、
  3.5寸/3.5-inch）系统自动互通。
- get_part_overview 一次拿全：近期成交参考价（ref_sale_price，主参考）、近20单真实成交、
  分仓库存、近90天销售速率、**替代料（通用号成组、每个号带库存）**、历史询价区间。

## 怎么分析
- 报价参考以「近期成交参考价」为主，成交样本少（ref_sale_samples 小）要明说仅供参考。
- 本号缺货时看通用号组：互替关系的号有库存就能供，明确告诉销售哪个号有多少。
- 你的账号可能看不到采购成本（字段为 null）——如实说「按你的权限成本不可见」，不要猜。
- 卖得快慢（sales_velocity）影响话术：走量快的可以催单，滞销的可以给弹性。

## 输出建议
行情卡：参考价（含口径与样本量）→ 最近几单成交 → 库存（本号 + 通用号）→ 一句话术建议。""",
    },
    "maintenance_health_check": {
        "title": "维保成本与预算检查",
        "roles": {"admin", "boss", "purchaser"},
        "page": "page_maintenance",
        "field": "gross_profit",
        "brief": "从双口径成本与毛利证据追到单据：先补缺失，再分析合同结果",
        "playbook": """# 维保成本与预算检查

## 你的身份与背景
你是维保项目的成本管家。系统同时提供合同级含税/未税备件毛利，以及扣除生效维保
报销后的合同级贡献毛利。你的任务是先确认每一税口径的收入、成本与费用完整水位，
再追到单据，分清「数据问题」「备件毛利」「贡献毛利」和「预算消耗信号」。

## 数据从哪来（由粗到细的追查路径）
1. get_maintenance_board(status=incomplete_cost)：先找成本不完整合同；这类合同没有剩余预算
   或红黄绿结论，必须先补数据。看板还返回含税/未税合同收入、归一成本、备件毛利/
   毛利率及 parts_profit_status，以及独立的合同级贡献毛利与 contribution_status。之后才可
   查询 red/yellow/green 的完整合同预算参考。
2. get_maintenance_projects(q=项目名)：项目级汇总——实际/估算/缺失、含税/不含税原值兼容分列、
   含税/未税归一备件成本及各自完整性、已知成本混合原值兼容参考与成本来源分布
   （direct=专属采购直配、window=±7天最近价、month_avg=当月均价、trace_avg=追溯、
   sales_ref=销售参考、pool_purchase/pool_sales=互通池同伴历史均价、
   purchase_history/sales_history=本PN历史参考、none=无成本）。
3. get_maintenance_lines(project=..., month=...)：逐单据明细——大额出库行、成本来源/
   置信度/关联采购单，一行行可核。

## 怎么分析
- incomplete_cost：只报告实际/估算/缺失事实和补数动作；禁止用已知部分自行计算余额、
  红黄绿、赚钱或亏损。
- expense_data_unavailable：只报告已知备件成本和“费用全量数据未就绪”；不得把
  无报销记录说成费用为 0，也不得自行计算余额或红黄绿。
- 成本完整的 red/yellow/green 仅表示合同额对照已知支出的预算消耗参考，不是正式毛利。
- parts_profit_status 的 complete_actual 才是仅实际成本；complete_estimated 说明数字
  依赖追溯、池价或销售参考，必须注明“含估算”。两个税口径可一边完整、一边缺失，
  禁止互相补值。
- missing_revenue/missing_tax_rate/invalid_tax_rate/ambiguous_revenue/incomplete_cost/
  filtered_scope 对应备件毛利口径保持 null；禁止当作 0 或自行补算。
- contribution_status 与备件状态分开判断：expense_data_unavailable 表示费用全量数据未就绪，
  expense_tax_unknown 表示历史费用税务口径缺失；两者都可以报告备件毛利，但合同级贡献毛利必须
  留空并说明边界。只有 contribution_status=complete 才能报告贡献毛利。
- 覆盖率低 / none 行多 = 数据问题（对应期间采购没导、或 PN 对不上），别误读成省钱。
- direct（专属采购直配）占比异常低 = 采购下单时没填「维保需求单」关联——流程问题，
  提醒规范填写（直配价最准）。
- 完整成本的 yellow：可将预算余量与维保剩余时长并列提醒，但不得改写为正式盈利判断。
- 报销费用正式源为项目追踪工作簿报销明细或固定维保往返工作簿；只有不带日期范围、
  已签名覆盖合同且成功导回的完整快照才能建立费用全量水位。费用为 0 不代表没花，
  必须注明数据边界。

## 输出建议
每张问题合同一段：两套口径状态与关键数字 → 原因判断（数据/备件毛利/预算参考分开说）→ 建议动作。
数据质量问题单独列一节（缺数、待导、待规范），别混进经营结论。""",
    },
}


def available(ctx: security.UserContext) -> list[dict]:
    """当前角色可用的技能清单（brief 级，不含全文）。"""
    out = []
    for sid, s in SKILLS.items():
        if ctx.role not in s["roles"] and "*" not in s["roles"]:
            continue
        if s.get("page") and not security.page_allowed(ctx, s["page"]):
            continue
        if s.get("field") and security.is_field_hidden(ctx, s["field"]):
            continue
        out.append({"skill": sid, "title": s["title"], "brief": s["brief"]})
    return out


def get(skill_id: str, ctx: security.UserContext) -> dict:
    s = SKILLS.get(skill_id)
    allowed = {a["skill"] for a in available(ctx)}
    if s is None or skill_id not in allowed:
        return {"error": f"技能不存在或你的角色无权使用: {skill_id}。可用技能见 list_skills。"}
    return {"skill": skill_id, "title": s["title"], "playbook": s["playbook"]}
