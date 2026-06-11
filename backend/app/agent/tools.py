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
from app.services import agent_files, part_overview, part_resolver, profit

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
                "近90天均售价、库存合计。每项带 status：ok=唯一命中已附价格；ambiguous=多规格变体"
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


def _inspect_file(db: Session, args: dict, ctx: security.UserContext) -> dict:
    return agent_files.inspect_file(str(args.get("file_id", "")))


def _read_file_rows(db: Session, args: dict, ctx: security.UserContext) -> dict:
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


def _write_excel(db: Session, args: dict, ctx: security.UserContext) -> dict:
    return agent_files.write_excel(
        args.get("base_file_id"), args.get("sheet"),
        args.get("cells") or [], args.get("output_name"), ctx.role)


_REGISTRY = {
    "search_parts": _search_parts,
    "get_part_overview": _get_part_overview,
    "get_profit_ranking": _get_profit_ranking,
    "inspect_file": _inspect_file,
    "read_file_rows": _read_file_rows,
    "lookup_prices_bulk": _lookup_prices_bulk,
    "write_excel": _write_excel,
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
