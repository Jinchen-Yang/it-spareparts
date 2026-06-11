"""Agent 循环：system + 历史 → 模型 → 工具调用 → 回灌 → 直到产出最终答复。

返回 answer + tool_calls 轨迹（前端展示"查了什么"，透明可信）。
服务端无状态：对话历史由客户端持有并随请求带上。
"""
import json
import logging

from sqlalchemy.orm import Session

from app import security
from app.agent import prompts, provider, tools
from app.config import get_settings

_log = logging.getLogger("agent")


def run(db: Session, messages: list[dict], ctx: security.UserContext) -> dict:
    msgs: list[dict] = [{"role": "system", "content": prompts.system_prompt()}] + messages
    trace: list[dict] = []

    for _ in range(get_settings().llm_max_tool_iters):
        res = provider.chat(msgs, tools.TOOLS)
        if not res.tool_calls:
            return {"answer": res.content or "", "tool_calls": trace}

        msgs.append({
            "role": "assistant",
            "content": res.content,
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": c.arguments}}
                for c in res.tool_calls
            ],
        })
        for c in res.tool_calls:
            try:
                args = json.loads(c.arguments or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
            result = tools.dispatch(db, c.name, args, ctx)
            trace.append({"name": c.name, "args": args})
            _log.info("agent tool=%s args=%s", c.name, args)
            msgs.append({"role": "tool", "tool_call_id": c.id,
                         "content": json.dumps(result, ensure_ascii=False)})

    # 达到轮数上限：不再给工具，要求直接作答（避免死循环）
    msgs.append({"role": "user",
                 "content": "工具调用轮数已达上限，请基于以上已获取的数据直接给出最终回答。"})
    res = provider.chat(msgs)
    return {"answer": res.content or "", "tool_calls": trace}


def run_stream(db: Session, messages: list[dict], ctx: security.UserContext):
    """流式 Agent 循环：yield 事件 dict 供 SSE 推送。

    事件：{"type":"delta","text"} 正文增量 / {"type":"tool","name","args"} 工具开始 /
    {"type":"tool_done","name"} 工具完成 / {"type":"done","tool_calls"} 结束。
    中间轮模型旁白（"我先查一下…"）同样流出——过程可见，体验对齐 ChatGPT/Claude。
    """
    msgs: list[dict] = [{"role": "system", "content": prompts.system_prompt()}] + messages
    trace: list[dict] = []

    for _ in range(get_settings().llm_max_tool_iters):
        res = None
        for kind, payload in provider.chat_stream(msgs, tools.TOOLS):
            if kind == "delta":
                yield {"type": "delta", "text": payload}
            else:
                res = payload
        if res is None or not res.tool_calls:
            yield {"type": "done", "tool_calls": trace,
                   "answer": (res.content if res else "") or ""}
            return

        if res.content:  # 本轮有旁白且接着调工具：补段落分隔，避免与后文粘连
            yield {"type": "delta", "text": "\n\n"}
        msgs.append({
            "role": "assistant",
            "content": res.content,
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": c.arguments}}
                for c in res.tool_calls
            ],
        })
        for c in res.tool_calls:
            try:
                args = json.loads(c.arguments or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool", "name": c.name, "args": args}
            result = tools.dispatch(db, c.name, args, ctx)
            trace.append({"name": c.name, "args": args})
            _log.info("agent tool=%s args=%s", c.name, args)
            yield {"type": "tool_done", "name": c.name,
                   "ok": not (isinstance(result, dict) and result.get("error"))}
            msgs.append({"role": "tool", "tool_call_id": c.id,
                         "content": json.dumps(result, ensure_ascii=False)})

    # 轮数上限：收尾作答（非流式一次拿回）
    msgs.append({"role": "user",
                 "content": "工具调用轮数已达上限，请基于以上已获取的数据直接给出最终回答。"})
    res = provider.chat(msgs)
    if res.content:
        yield {"type": "delta", "text": res.content}
    yield {"type": "done", "tool_calls": trace, "answer": res.content or ""}
