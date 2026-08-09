"""智能体工具层：把一期查询服务包成 LLM 工具（OpenAI function 格式）。

设计原则：
- 工具结果只来自库内真实数据；异常包成 {"error": ...} 让模型自恢复（换词重搜/向用户澄清）。
- 所有调用过 record_access_log（开关开启后即审计"谁问了什么、查了哪个型号"）。
- 输出过 apply_field_visibility（RBAC 关闭时原样；将来收紧销售/采购可见字段零改动）。
"""
import json
import logging
from datetime import date

from sqlalchemy.orm import Session

from app import security
from app.agent import skills
from app.services import (agent_files, inventory, maintenance_cost, part_overview,
                          part_resolver, profit, purchase_analysis, purchase_query)

_log = logging.getLogger("agent")

# 各工具入参上限的单一真值源（TOOLS-5）：clamp 与工具描述都引用这些常量，
# 避免"描述写最大 20、代码 clamp 到别的值"两处漂移。clamp 本身仍保留（不信模型输入）。
_RANK_ROWS = 50            # get_profit_ranking 返回行上限
_BULK_MAX = 60             # lookup_prices_bulk 单次型号数上限
_SEARCH_LIMIT_MAX = 20     # search_parts 返回条数上限
_RECENT_LIMIT_MAX = 50     # list_recent_purchases 返回条数上限
_RECENT_DAYS_MAX = 365     # list_recent_purchases 时间窗上限（天）
_READ_ROWS_MAX = agent_files._MAX_READ_ROWS   # read_file_rows 行数上限（真值源在 agent_files）

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_parts",
            "description": (
                "搜索备件，返回按匹配度排序的候选。四种查询方式都支持：①型号 PN（容错连字符/"
                "大小写/后缀差异/历史别名，如 4089RT vs 4089-RT）；②整段标准描述（如 '8TB 6Gb/s "
                "7.2K 256MB Cache 3.5-inch SATA HDD'——找同描述的所有型号）；③规格词组合（如 "
                "'8TB 7.2K SATA' 或 'Seagate 8TB'，词序无关、全部命中即返回；写法差异自动互通："
                "6Gbps=6Gb/s、3.5寸=3.5inch=3.5-inch、7200rpm=7.2K、8T=8TB）；④品牌/描述关键词。"
                "每条带 score(0~1)与 match_reason。low_confidence=true 表示没有可靠匹配，"
                "此时应把候选列给用户确认，不要擅自选择；同规格多品牌会轮播展示，指定品牌请把"
                "品牌词加进查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "用户原话中的型号或描述，如 'super 4089RT-x 准系统'"},
                    "limit": {"type": "integer",
                              "description": f"返回条数，默认 10，最大 {_SEARCH_LIMIT_MAX}"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_part_overview",
            "description": (
                "获取某型号的完整全景：基本信息(描述/品牌/品类)、近20单采购(供应商/单价，含税口径"
                "跟随订单标识)、近20单销售(客户/单价)、分仓库存、**替代料（通用号自动成组：查任一"
                "号能看到组内全部互替号，每个号带当前库存合计——缺货时用它找可替代供货）**、"
                "两种成本法(移动加权/FIFO)平均成本与毛利率、历史询价区间、近90天销售速率。"
                "报价、采购压价、解释型号、找替代都以此为依据。pn_std 必须是 search_parts 返回的准确值。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"pn_std": {"type": "string", "description": "标准型号，精确值"}},
                "required": ["pn_std"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_file",
            "description": (
                "查看用户上传的 xlsx 结构：sheet 列表 + 每个 sheet 前几行原样数据(1-based 行号)。"
                "客户文件格式千变万化——由你自己判断表头在第几行、哪列是型号、哪列是数量，"
                "不要假设固定格式。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"file_id": {"type": "string", "description": "上传时返回的 file_id"}},
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_rows",
            "description": "分页读取 xlsx 行数据（1-based）。先 inspect_file 看结构再按需读取全部数据行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "sheet": {"type": "string", "description": "sheet 名，省略=第一个"},
                    "start_row": {"type": "integer", "description": "起始行(1-based)，默认1"},
                    "max_rows": {"type": "integer",
                                 "description": f"读取行数，默认50，最大{_READ_ROWS_MAX}"},
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_prices_bulk",
            "description": (
                "批量查价（询价单场景核心工具）：对每个型号文本做近似解析并返回 最近采购价/日期、"
                "近期加权成交参考价(ref_sale_price)、近90天均售价、库存合计。每项带 status：ok=唯一命中已附价格；ambiguous=多规格变体"
                "（带候选列表，需逐项给用户确认或在结果中标注）；not_found=没找到。"
                f"一次最多 {_BULK_MAX} 个，超过请分批。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {"type": "array", "items": {"type": "string"},
                                "description": "型号文本数组（可以是客户原话写法）"},
                },
                "required": ["queries"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_excel",
            "description": (
                "写 Excel 并生成下载文件（绝不改写原上传件，总是产出新 file_id）。"
                "两种用法：①回填客户模板：传 base_file_id，在其副本上写（如在右侧空列追加"
                "'最近采购价/库存/备注'列头和数据，原格式保留）；②新建报价单：不传 base_file_id，"
                "自己规划表头和数据行。cells 用 1-based 行号 + 列字母(如 'G')或数字。"
                "完成后把返回的 download_url 告诉用户。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "base_file_id": {"type": "string", "description": "基于哪个上传文件回填，可省略"},
                    "sheet": {"type": "string", "description": "sheet 名，省略=第一个；不存在则新建"},
                    "cells": {
                        "type": "array",
                        "description": "[{row:3, col:'G', value:1700}, ...] 最多3000个",
                        "items": {
                            "type": "object",
                            "properties": {
                                "row": {"type": "integer"},
                                "col": {"type": ["string", "integer"]},
                                "value": {},
                            },
                            "required": ["row", "col"],
                        },
                    },
                    "output_name": {"type": "string", "description": "下载文件名，如 '报价单-XX公司.xlsx'"},
                },
                "required": ["cells"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": (
                "读取上传文件的全部内容（Word/PDF/txt/Excel/图片均可）。整机配置拆解场景用它："
                "拿到文本后你自己判断里面有哪些设备/部件，按 品牌+型号+规格+数量 拆成清单。"
                "图片和扫描件 PDF 会自动走视觉识别（需配置视觉模型，未配置时返回提示）。"
                "Excel 若要按行列精确定位用 inspect_file/read_file_rows，要整体内容用本工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"file_id": {"type": "string"}},
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_report",
            "description": (
                "生成**美化** Excel 报表并返回下载链接（表头配色、边框、自适应列宽、金额格式、"
                "冻结表头、斑马纹；备注含'需确认'/'未找到'的行自动标橙/红）。"
                "整机拆解报价单、批量查价结果等用它（比 write_excel 好看）。"
                "headers=列名数组；rows=与列对齐的二维数组（每行一个数组）；money_cols=金额列的"
                "0基下标数组（会按千分位+两位小数格式化）。完成后把 download_url 告诉用户。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "报表标题，可省略，如 'XX公司整机配置报价单'"},
                    "headers": {"type": "array", "items": {"type": "string"},
                                "description": "列名，如 ['序号','部件','品牌','型号','数量','匹配PN','近15天采购均价','库存','近期成交参考价','备注']"},
                    "rows": {"type": "array", "items": {"type": "array"},
                             "description": "数据行，每行一个数组，顺序与 headers 对齐"},
                    "money_cols": {"type": "array", "items": {"type": "integer"},
                                   "description": "金额列的 0 基下标，如 [6,8]"},
                    "output_name": {"type": "string", "description": "下载文件名"},
                },
                "required": ["headers", "rows"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_purchases",
            "description": (
                "查最近的采购记录（跨型号时间线，按采购日期倒序）：日期/供应商/型号/数量/单价"
                "（含 is_tax_inclusive 标识该单价是含税还是不含税）。"
                "回答'最近买了什么''XX 最近进价多少笔'这类问题用它；"
                "查某一个型号的完整行情仍用 get_part_overview。"
                f"query 可按 型号/描述/品牌 关键词过滤；days 默认 30 天，最大 {_RECENT_DAYS_MAX}。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词过滤（型号/描述/品牌），可省略"},
                    "days": {"type": "integer",
                             "description": f"最近多少天，默认 30，最大 {_RECENT_DAYS_MAX}"},
                    "supplier": {"type": "string", "description": "供应商名过滤，可省略"},
                    "limit": {"type": "integer",
                              "description": f"返回条数，默认 20，最大 {_RECENT_LIMIT_MAX}"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_profit_ranking",
            "description": (
                "利润聚合排名（维度三选一：part=按型号 / salesperson=按销售员 / customer=按客户），"
                f"含营收、两种成本法的毛利与毛利率。按营收降序，最多返回前{_RANK_ROWS}行。可选日期范围过滤。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string", "enum": ["part", "salesperson", "customer"]},
                    "date_from": {"type": "string", "description": "起始日期 YYYY-MM-DD，可选"},
                    "date_to": {"type": "string", "description": "截止日期 YYYY-MM-DD，可选"},
                },
                "required": ["dimension"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_purchase_analysis",
            "description": (
                "采购分析聚合（早会/批量采购计划的核心数据）：窗口内逐型号统计——采购次数/总量/"
                "价格区间与最近价(含税、不含税分列，均为单据原值)/价格趋势(up|down|flat|new)/"
                "来源渠道拆分(淘宝/京东/个人/正规供应商)/系统初筛建议(批量补库=频发、谈价=价格在涨、"
                "偶发)。KPI 含采购总额(含税/不含税双口径)、单数、来源构成。rows 按采购次数降序。"
                "「哪些型号该批量采购」「最近采购花了多少」「XX 采购价走势」用它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "统计窗口天数，默认 30，最大 365"},
                    "q": {"type": "string", "description": "型号/描述关键词过滤，可省略"},
                    "supplier": {"type": "string", "description": "供应商名过滤，可省略"},
                    "top": {"type": "integer", "description": "返回型号行数，默认 20，最大 50"},
                    "exclude_designated": {"type": "boolean",
                                           "description": "是否排除「指定采购」大额补库单，默认 true（聚焦应急采购）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_inventory",
            "description": (
                "分页查询库存（锚定动态口径=最近盘点/快照做期初+之后单据流水实时推算，型号级）："
                "按 型号/描述/品牌 关键词过滤，返回 动态可用数量/期初(锚点日)/期初后入库/出库(销售+维保)/"
                "分仓参考(快照行,含单位成本与库存金额)。「XX 还有多少库存」用它；"
                "单个型号的完整行情（含通用号库存）用 get_part_overview。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "型号/描述/品牌关键词，可省略"},
                    "limit": {"type": "integer", "description": "返回条数，默认 20，最大 50"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_maintenance_board",
            "description": (
                "维保合同预算消耗参考：备件先分实际采购参考/估算参考/成本缺失，再与生效报销费用组成"
                "已知支出参考。incomplete_cost=成本不完整；"
                "expense_data_unavailable=项目追踪工作簿报销明细尚未建立费用全量数据水位；两者的 "
                "remaining/remaining_pct 都为空，严禁自行推断红黄绿或盈亏；"
                "仅成本与费用数据都完整时才给 red=预算已用完或超预算、"
                "yellow=预算余量≤20%、green=预算余量>20%、no_budget=无正预算。"
                "同时返回合同级含税/未税收入、归一备件成本、备件毛利及毛利率；两套口径"
                "独立 fail closed。parts_profit_status_inc/ex 中 complete_estimated 必须标注"
                "含估算；missing_revenue/missing_tax_rate/invalid_tax_rate/"
                "ambiguous_revenue/incomplete_cost/filtered_scope 均不得把 null 当 0。"
                "合同级贡献毛利使用独立 contribution_profit/margin/status 字段；"
                "contribution_status_inc/ex 为 expense_data_unavailable 或"
                "expense_tax_unknown 时必须保持为空，不得包装成正式财务毛利。"
                "合同额、预算与余量仅按利润权限返回；无利润权限时不返回这些金额、决策状态、"
                "双口径毛利及其状态、状态计数或筛选结果，改按最近出库日期排列。"
                "需要项目成本页面权限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "incomplete_cost",
                            "expense_data_unavailable",
                            "red",
                            "yellow",
                            "green",
                            "no_budget",
                        ],
                        "description": "按预算消耗参考状态过滤，可省略=全部",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_maintenance_projects",
            "description": (
                "维保项目成本汇总（项目维度）：实际采购参考/估算参考按含税与不含税分列、"
                "缺失成本行、成本完整性、含税/未税归一备件成本、已知成本混合原值兼容参考、出库行数/数量/覆盖率/"
                "成本来源分布(direct=专属采购直配、window=±7天最近价、month_avg=当月均价、"
                "trace_avg=估算追溯均价、sales_ref=估算销售参考、"
                "pool_purchase/pool_sales=互通池同伴历史均价、"
                "purchase_history/sales_history=本PN历史参考、none=成本缺失)/关联销售订单与合同额参考。"
                "合同额参考仅按利润权限返回，空值不等于源数据缺失。"
                "无成本权限时按项目名排序后再截取 top，不按隐藏成本排名。"
                "需要项目成本页面权限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "项目名关键词过滤，可省略"},
                    "top": {"type": "integer", "description": "返回项目数，默认 20，最大 50"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_maintenance_lines",
            "description": (
                "单个维保项目的出库明细（追单据用）：维保单号/日期/需求类型/仓库/PN/数量/退货/"
                "单价/金额/cost_tier(actual|estimated|missing)/原始成本来源与税口径/"
                "置信度(high|medium|low)/取价月/距采购天数/关联采购单/异常标记。"
                "cost_tier 是权威事实层级；missing 时单价和金额为空，原始来源/税口径仅供诊断，"
                "不得据此自行恢复金额或重判 actual/estimated。"
                "project 必须是 get_maintenance_projects 返回的准确项目名。需要项目成本页面权限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "项目名，精确值"},
                    "month": {"type": "string", "description": "按月过滤 YYYY-MM，可省略"},
                    "page": {"type": "integer", "description": "页码，默认 1（每页 50 行）"},
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cancellation_stats",
            "description": (
                "采购取消/作废统计（按月/季/年）：各期间总单数、取消单数、取消率、取消金额。"
                "判断采购流程质量与供应异常（某月取消率突增=供应/审批出问题）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "granularity": {"type": "string", "enum": ["month", "quarter", "year"],
                                    "description": "统计粒度，默认 month"},
                    "days": {"type": "integer", "description": "只统计最近 N 天，可省略=全部"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": (
                "列出你（按当前登录角色）可用的业务技能剧本：采购批量计划分析、老板经营速览、"
                "配件行情简报、维保成本与预算检查等。接到复杂/多步的业务任务时先看这里有没有现成打法。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill",
            "description": (
                "获取指定技能的完整剧本：你的身份背景、建议的数据获取路径（用哪些工具、拿到什么）、"
                "分析框架、输出建议。剧本是指导不是死流程——结合用户实际问题灵活裁剪。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"skill": {"type": "string",
                                         "description": "技能 id，来自 list_skills"}},
                "required": ["skill"],
            },
        },
    },
]


def _jsonable(data):
    """date/Decimal 等转 JSON 可序列化（工具结果要回灌给模型）。"""
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


def _parse_date(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return None


def _search_parts(db: Session, args: dict, ctx: security.UserContext) -> dict:
    q = str(args.get("query", "")).strip()
    if not q:
        return {"error": "query 不能为空"}
    limit = min(int(args.get("limit") or 10), _SEARCH_LIMIT_MAX)
    return part_resolver.resolve(db, q, limit=limit, operated_by=ctx.role)


def _get_part_overview(db: Session, args: dict, ctx: security.UserContext) -> dict:
    pn = str(args.get("pn_std", "")).strip()
    data = part_overview.get_overview(db, pn, ctx)
    if data is None:
        return {"error": f"型号不存在: {pn}。请先用 search_parts 找到准确 pn_std。"}
    return security.apply_field_visibility(data, ctx)


def _get_profit_ranking(db: Session, args: dict, ctx: security.UserContext) -> dict:
    # 防恶性竞争：销售角色禁用按销售员/客户的排名（直接暴露同事经营数据）
    if security.is_scoped_sales(ctx):
        return {"error": "无权限：销售角色不能查看按客户/销售员的经营排名（防恶性竞争）。"
                          "可以问某个型号的行情价或你自己的成交。"}
    dim = args.get("dimension", "part")
    if dim not in ("part", "salesperson", "customer"):
        dim = "part"
    data = profit.aggregate(db, dim, _parse_date(args.get("date_from")),
                            _parse_date(args.get("date_to")), False, ctx)
    rows = data.get("rows", [])
    if len(rows) > _RANK_ROWS:
        data = {**data, "rows": rows[:_RANK_ROWS],
                "note": f"共 {len(rows)} 行，仅返回营收前 {_RANK_ROWS} 行"}
    return security.apply_field_visibility(data, ctx)


def _owns(ctx: security.UserContext, file_id: str | None) -> bool:
    """文件归属 + 当前可见范围重验；生成件在账号降权后 fail closed。"""
    if not file_id:
        return True
    try:
        return agent_files.access_allowed(file_id, ctx)
    except agent_files.FileError:
        # 文件不存在也按"无权"处理（TOOLS-4）：让"不存在"与"非本人"返回不可区分的拒绝，
        # 堵住通过 file_id 探测文件是否存在的 oracle。
        return False


_NO_ACCESS = {"error": "无权访问该文件（非本人上传/生成）"}


def _inspect_file(db: Session, args: dict, ctx: security.UserContext) -> dict:
    fid = str(args.get("file_id", ""))
    if not _owns(ctx, fid):
        return _NO_ACCESS
    return agent_files.inspect_file(fid)


def _read_file_rows(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not _owns(ctx, str(args.get("file_id", ""))):
        return _NO_ACCESS
    return agent_files.read_rows(
        str(args.get("file_id", "")), args.get("sheet"),
        int(args.get("start_row") or 1), int(args.get("max_rows") or 50))


def _lookup_prices_bulk(db: Session, args: dict, ctx: security.UserContext) -> dict:
    queries = args.get("queries")
    if not isinstance(queries, list) or not queries:
        return {"error": "queries 需为非空字符串数组"}
    if len(queries) > _BULK_MAX:
        return {"error": f"一次最多 {_BULK_MAX} 个（收到 {len(queries)}），请分批调用"}
    results, counts = [], {"ok": 0, "ambiguous": 0, "not_found": 0}
    for raw in queries:
        q = str(raw).strip()
        if not q:
            continue
        r = part_resolver.resolve(db, q, limit=3, operated_by=ctx.role)
        cands = [{"pn_std": i["pn_std"], "description": i["description"], "score": i["score"]}
                 for i in r["items"][:3]]
        if not r["items"] or r["low_confidence"]:
            counts["not_found"] += 1
            results.append({"query": q, "status": "not_found", "candidates": cands})
        elif r["ambiguous"]:
            counts["ambiguous"] += 1
            results.append({"query": q, "status": "ambiguous", "candidates": cands})
        else:
            top = r["items"][0]
            counts["ok"] += 1
            results.append({"query": q, "status": "ok", "pn_std": top["pn_std"],
                            "description": top["description"], "score": top["score"],
                            **part_overview.quick_pricing(db, top["pn_std"])})
    # 按角色脱敏：sales 看不到采购成本（quick_pricing 的 last/recent_purchase_* 进 purchase_cost 组），
    # 但保留售价聚合（avg_sale_price_90d/ref_sale_price）。与其它工具出口一致。
    return security.apply_field_visibility({"results": results, "summary": counts}, ctx)


def _list_recent_purchases(db: Session, args: dict, ctx: security.UserContext) -> dict:
    limit = min(int(args.get("limit") or 20), _RECENT_LIMIT_MAX)
    days = min(int(args.get("days") or 30), _RECENT_DAYS_MAX)
    data = purchase_query.recent_purchases(
        db, ctx, q=args.get("query"), days=days,
        supplier=args.get("supplier"), page=1, page_size=limit)
    if data["total"] > limit:
        data["note"] = f"共 {data['total']} 条，仅返回最近 {limit} 条；可加 query/supplier 过滤"
    return security.apply_field_visibility(data, ctx)


def _write_excel(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not _owns(ctx, args.get("base_file_id")):   # 基于他人文件回填 = 变相读他人文件
        return _NO_ACCESS
    return agent_files.write_excel(
        args.get("base_file_id"), args.get("sheet"),
        args.get("cells") or [], args.get("output_name"),
        agent_files.verified_artifact_owner(ctx))


def _read_document(db: Session, args: dict, ctx: security.UserContext) -> dict:
    fid = str(args.get("file_id", ""))
    if not _owns(ctx, fid):
        return _NO_ACCESS
    return agent_files.read_document(fid)


def _write_report(db: Session, args: dict, ctx: security.UserContext) -> dict:
    # 归属对称性（TOOLS-6）：本工具只新建报表、不读任何既有文件，故无需 _owns 校验
    # （对照 _write_excel：它有 base_file_id 回填 = 变相读他人文件，故必须 _owns）。
    # ⚠️ 若将来给 write_report 加 base_file_id 之类的"读既有文件"能力，必须同步补 _owns。
    headers = args.get("headers")
    rows = args.get("rows")
    if not isinstance(headers, list) or not headers:
        return {"error": "headers 需为非空数组"}
    if not isinstance(rows, list):
        return {"error": "rows 需为二维数组"}
    return agent_files.write_report(
        args.get("title"), [str(h) for h in headers], rows,
        args.get("output_name"), agent_files.verified_artifact_owner(ctx),
        money_cols=args.get("money_cols") if isinstance(args.get("money_cols"), list) else None)


# ── v1.5.0 新工具：数据层全面接入（采购分析/库存/维保/取消统计）+ 技能剧本 ──

_MAINT_PAGE_ERR = {"error": "无权限：你的账号未开通「项目成本（维保出库）」页面权限，无法查询维保成本数据。"}


def _get_purchase_analysis(db: Session, args: dict, ctx: security.UserContext) -> dict:
    days = min(int(args.get("days") or 30), 365)
    top = min(int(args.get("top") or 20), 50)
    excl = args.get("exclude_designated")
    data = purchase_analysis.analysis(
        db, ctx, days=days, exclude_designated=True if excl is None else bool(excl),
        q=args.get("q"), supplier=args.get("supplier"), top=top)
    for r in data.get("rows", []):
        r.pop("daily", None)           # 逐日火花线数组对模型无用且费 token
    return security.apply_field_visibility(data, ctx)


def _get_inventory(db: Session, args: dict, ctx: security.UserContext) -> dict:
    limit = min(int(args.get("limit") or 20), 50)
    data = inventory.list_dynamic(db, args.get("query"), page=1, page_size=limit, user_ctx=ctx)
    if data.get("total", 0) > limit:
        data["note"] = f"共 {data['total']} 个型号，仅返回动态库存最高的前 {limit} 个；可加 query 过滤"
    return security.apply_field_visibility(data, ctx)


def _get_maintenance_board(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not security.page_allowed(ctx, "page_maintenance"):
        return _MAINT_PAGE_ERR
    if security.is_scoped_sales(ctx):
        return {
            "error": "无权限：受限销售账号不能查看合同级维保数据。"
            "可以查看本人范围的项目事实。"
        }
    st = args.get("status")
    data = maintenance_cost.board(
        db, None, None,
        st if st in (
            "incomplete_cost",
            "expense_data_unavailable",
            "red",
            "yellow",
            "green",
            "no_budget",
        ) else None,
        user_ctx=ctx,
    )
    return security.apply_field_visibility(data, ctx)


def _get_maintenance_projects(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not security.page_allowed(ctx, "page_maintenance"):
        return _MAINT_PAGE_ERR
    top = min(int(args.get("top") or 20), 50)
    data = maintenance_cost.projects_aggregate(db, None, None, args.get("q"), user_ctx=ctx)
    rows = data.get("rows", [])
    if len(rows) > top:
        sort_label = "按项目名" if data.get("ranking_restricted") else "成本最高"
        data = {**data, "rows": rows[:top],
                "note": f"共 {len(rows)} 个项目，仅返回{sort_label}的前 {top} 个；可用 q 过滤"}
    return security.apply_field_visibility(data, ctx)


def _get_maintenance_lines(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not security.page_allowed(ctx, "page_maintenance"):
        return _MAINT_PAGE_ERR
    project = str(args.get("project", "")).strip()
    if not project:
        return {"error": "project 不能为空（用 get_maintenance_projects 拿准确项目名）"}
    month = args.get("month")
    data = maintenance_cost.project_lines(db, project, month if month else None,
                                          None, None, max(int(args.get("page") or 1), 1), 50,
                                          user_ctx=ctx)
    return security.apply_field_visibility(data, ctx)


def _get_cancellation_stats(db: Session, args: dict, ctx: security.UserContext) -> dict:
    gran = args.get("granularity") or "month"
    days = args.get("days")
    data = purchase_analysis.cancellation_stats(
        db, ctx, granularity=gran, days=int(days) if days else None)
    return security.apply_field_visibility(data, ctx)


def _list_skills(db: Session, args: dict, ctx: security.UserContext) -> dict:
    return {"skills": skills.available(ctx)}


def _get_skill(db: Session, args: dict, ctx: security.UserContext) -> dict:
    return skills.get(str(args.get("skill", "")).strip(), ctx)


_REGISTRY = {
    "search_parts": _search_parts,
    "get_part_overview": _get_part_overview,
    "get_profit_ranking": _get_profit_ranking,
    "list_recent_purchases": _list_recent_purchases,
    "inspect_file": _inspect_file,
    "read_file_rows": _read_file_rows,
    "read_document": _read_document,
    "lookup_prices_bulk": _lookup_prices_bulk,
    "write_excel": _write_excel,
    "write_report": _write_report,
    "get_purchase_analysis": _get_purchase_analysis,
    "get_inventory": _get_inventory,
    "get_maintenance_board": _get_maintenance_board,
    "get_maintenance_projects": _get_maintenance_projects,
    "get_maintenance_lines": _get_maintenance_lines,
    "get_cancellation_stats": _get_cancellation_stats,
    "list_skills": _list_skills,
    "get_skill": _get_skill,
}


def dispatch(db: Session, name: str, args: dict, ctx: security.UserContext) -> dict:
    """执行一次工具调用：审计 → 派发 → 内部异常脱敏后回灌（不让对话崩掉、也不泄实现细节）。

    业务错由各工具显式 return {"error": 文案}（如"型号不存在""query 不能为空"）——这些是给
    模型自恢复的安全文案，原样回灌。这里的 except 只兜底**未预期的内部异常**（如 SQLAlchemyError
    会把 SQL 语句 + 表/列名带出）：原始 type+消息只 _log 到服务端，回灌给模型/用户的只有固定脱敏
    文案。否则裸异常经工具结果 → tool 消息 → SSE delta 直达终端用户（信息泄漏，PR-审计 TOOLS-1）。
    """
    security.record_access_log(ctx, f"agent_tool:{name}", "agent", args)
    fn = _REGISTRY.get(name)
    if fn is None:
        return {"error": f"未知工具: {name}"}
    try:
        return _jsonable(fn(db, args, ctx))
    except Exception:  # noqa: BLE001 —— 内部异常绝不外泄：原始信息只落服务端日志
        _log.exception("agent tool failed name=%s args=%s", name, args)
        return {"error": "工具执行失败，请换个方式或稍后重试", "retriable": True, "kind": "internal"}
