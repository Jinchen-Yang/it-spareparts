"""业务技能剧本库（skills）：给 Agent 的「打法手册」，不是死流程。

设计原则（甲方 2026-07-04 明确）：
- 剧本给**身份背景 + 数据路径 + 分析框架 + 输出建议**，让模型自己判断裁剪——绝不写死步骤。
- 按登录角色过滤（list_skills 只列可用的；get_skill 再校验一次）；涉及维保盈亏的剧本
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
        "brief": "采购分布、销售/利润分布、维保盈亏一页速览，异常点名 + 拍板清单",
        "playbook": """# 老板经营速览

## 你的身份与背景
你是给老板做速览的经营参谋。老板要的是「哪里赚钱、哪里出血、哪里需要我拍板」，
不是数据流水账。所有数字必须来自工具返回，两种成本法（移动加权/FIFO）口径注明。

## 数据从哪来
- 采购面：get_purchase_analysis(days=30)——采购总额（含税/不含税）、单数、来源渠道构成、
  频发应急件数量；get_cancellation_stats(granularity=month)——取消率异常的月份。
- 销售/利润面：get_profit_ranking(dimension=part / salesperson / customer)——营收与
  两法毛利，负毛利行是重点。
- 维保面：get_maintenance_board()——合同级盈亏红黄绿卡；红卡=超支、黄卡=剩余≤20%。

## 怎么分析
先总量（本期采购额、营收、毛利），再异常点名（每条给金额和一句原因）：
超支/预警的维保合同、负毛利型号或客户、采购取消率突增的月份、频发应急件（=可省的钱）。
异常里区分「经营问题」和「数据问题」（如维保覆盖率低是数据没导齐，别当成本异常汇报）。

## 输出建议
一页速览：三~五行总量 → 「需要拍板」清单（按金额降序，每条=事实+建议动作一句）。
数字后注口径（含税/不含税、成本法）。没有异常就明说一切正常，别硬凑。""",
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
        "title": "维保项目健康检查",
        "roles": {"admin", "boss", "purchaser"},
        "page": "page_maintenance",
        "field": "gross_profit",
        "brief": "从盈亏看板追到单据：找出亏损/预警合同的原因和可优化点",
        "playbook": """# 维保项目健康检查

## 你的身份与背景
你是维保项目的成本管家。维保合同是固定收入、成本随出库累积——超支就是亏损。
你的任务：从红/黄卡追根到单据，分清「经营问题」和「数据问题」，给出能落地的动作。

## 数据从哪来（由粗到细的追查路径）
1. get_maintenance_board(status=red 或 yellow)：合同级卡片——预算/已花（备件+报销费用）/
   剩余/覆盖率/低置信成本占比/维保起止。
2. get_maintenance_projects(q=项目名)：项目级汇总——成本分列小计、成本来源分布
   （direct=专属采购直配、window=±7天最近价、month_avg=当月均价、trace_avg=追溯、
   sales_ref=销售参考、none=无成本）。
3. get_maintenance_lines(project=..., month=...)：逐单据明细——大额出库行、成本来源/
   置信度/关联采购单，一行行可核。

## 怎么分析
- 红卡：先看已花构成（备件还是费用爆了）→ 明细里找 top 大额出库行 → 看这些行的成本
  置信度：低置信（追溯/销售参考）占比高说明**数字本身是估的**，先核数再谈问责。
- 覆盖率低 / none 行多 = 数据问题（对应期间采购没导、或 PN 对不上），别误读成"省钱"。
- direct（专属采购直配）占比异常低 = 采购下单时没填「维保需求单」关联——流程问题，
  提醒规范填写（直配价最准）。
- 黄卡：剩余预算对照维保剩余时长——花钱进度快于时间进度就要现在干预，而不是等超支。
- 报销费用侧数据要客户提供 BXD 导出后才有；费用为 0 不代表没花，注明数据边界。

## 输出建议
每张问题合同一段：状态与关键数字 → 原因判断（经营/数据分开说）→ 建议动作（落到人/事）。
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
