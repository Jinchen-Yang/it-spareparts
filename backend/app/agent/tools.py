"""智能体工具层：把一期查询服务包成 LLM 工具（OpenAI function 格式）。

设计原则：
- 工具结果只来自库内真实数据；异常包成 {"error": ...} 让模型自恢复（换词重搜/向用户澄清）。
- 所有调用过 record_access_log（开关开启后即审计"谁问了什么、查了哪个型号"）。
- 输出过 apply_field_visibility（RBAC 关闭时原样；将来收紧销售/采购可见字段零改动）。
"""
import json
from datetime import date

from sqlalchemy.orm import Session

from app import security
from app.services import agent_files, part_overview, part_resolver, profit, purchase_query

_RANK_ROWS = 50
_BULK_MAX = 60

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_parts",
            "description": (
                "按型号/品牌/描述近似搜索备件，返回按匹配度排序的候选。"
                "容错：连字符差异(4089RT vs 4089-RT)、大小写、多余后缀、中英品牌混写(super/超微)、历史别名。"
                "每条带 score(0~1)与 match_reason(命中解释)。low_confidence=true 表示没有可靠匹配，"
                "此时应把候选列给用户确认，不要擅自选择。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "用户原话中的型号或描述，如 'super 4089RT-x 准系统'"},
                    "limit": {"type": "integer", "description": "返回条数，默认 10，最大 20"},
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
                "获取某型号的完整全景：基本信息(描述/品牌/品类)、近20单采购(供应商/单价)、"
                "近20单销售(客户/单价)、分仓库存、替代料、两种成本法(移动加权/FIFO)平均成本与毛利率、"
                "历史询价区间、近90天销售速率。报价、采购压价、解释型号都以此为依据。"
                "pn_std 必须是 search_parts 返回的准确值。"
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
                    "max_rows": {"type": "integer", "description": "读取行数，默认50，最大200"},
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
                "一次最多 60 个，超过请分批。"
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
                "查最近的采购记录（跨型号时间线，按采购日期倒序）：日期/供应商/型号/数量/单价。"
                "回答'最近买了什么''XX 最近进价多少笔'这类问题用它；"
                "查某一个型号的完整行情仍用 get_part_overview。"
                "query 可按 型号/描述/品牌 关键词过滤；days 默认 30 天。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词过滤（型号/描述/品牌），可省略"},
                    "days": {"type": "integer", "description": "最近多少天，默认 30，最大 365"},
                    "supplier": {"type": "string", "description": "供应商名过滤，可省略"},
                    "limit": {"type": "integer", "description": "返回条数，默认 20，最大 50"},
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
                "含营收、两种成本法的毛利与毛利率。按营收降序，最多返回前50行。可选日期范围过滤。"
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
    limit = min(int(args.get("limit") or 10), 20)
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
    """文件归属校验：全量角色(admin/boss/readonly/RBAC关闭的 phase1)放行；
    否则需创建者==当前用户。防越权读他人上传的报价/合同（主要拦 sales 互看）。"""
    if not file_id:
        return True
    if ctx.role in security.FULL_SCOPE_ROLES:
        return True
    try:
        owner = agent_files.owner_of(file_id)
    except agent_files.FileError:
        return True   # 文件不存在交给底层工具报"找不到"，更明确
    return owner == ctx.user_id


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
    return {"results": results, "summary": counts}


def _list_recent_purchases(db: Session, args: dict, ctx: security.UserContext) -> dict:
    limit = min(int(args.get("limit") or 20), 50)
    days = min(int(args.get("days") or 30), 365)
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
        args.get("cells") or [], args.get("output_name"), ctx.user_id)


def _read_document(db: Session, args: dict, ctx: security.UserContext) -> dict:
    fid = str(args.get("file_id", ""))
    if not _owns(ctx, fid):
        return _NO_ACCESS
    return agent_files.read_document(fid)


def _write_report(db: Session, args: dict, ctx: security.UserContext) -> dict:
    headers = args.get("headers")
    rows = args.get("rows")
    if not isinstance(headers, list) or not headers:
        return {"error": "headers 需为非空数组"}
    if not isinstance(rows, list):
        return {"error": "rows 需为二维数组"}
    return agent_files.write_report(
        args.get("title"), [str(h) for h in headers], rows,
        args.get("output_name"), ctx.user_id,
        money_cols=args.get("money_cols") if isinstance(args.get("money_cols"), list) else None)


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
}


def dispatch(db: Session, name: str, args: dict, ctx: security.UserContext) -> dict:
    """执行一次工具调用：审计 → 派发 → 任何异常都转成 error 字段（不让对话崩掉）。"""
    security.record_access_log(ctx, f"agent_tool:{name}", "agent", args)
    fn = _REGISTRY.get(name)
    if fn is None:
        return {"error": f"未知工具: {name}"}
    try:
        return _jsonable(fn(db, args, ctx))
    except Exception as exc:  # noqa: BLE001 —— 工具失败要让模型看见并自恢复
        return {"error": f"{type(exc).__name__}: {exc}"}
