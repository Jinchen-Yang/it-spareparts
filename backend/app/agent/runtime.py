"""Agent 循环：system + 历史 → 模型 → 工具调用 → 回灌 → 直到产出最终答复。

单一内部循环 _agent_loop（run 与 run_stream 共用，仅"增量如何出口"分叉，RUNTIME-1）：
- run：非流式入口，消费循环只取最终答复（不含中间旁白），返回 {answer, tool_calls}。
- run_stream：流式入口，把循环事件原样转发供 SSE 推送。

事件：{type:"delta",text} 正文增量 / {type:"thinking",text} 思考链增量(开思考时) /
{type:"tool",name,args} 工具开始 / {type:"tool_done",name,ok} 工具完成 /
{type:"done",tool_calls,answer,stopped?} 结束。

服务端无状态：对话历史由调用方持有并随请求带上。OpenAI 线格式装配下沉到 provider.append_*
（RUNTIME-2），此处只跟中性的 ChatResult/ToolCall 打交道。
"""
import json
import logging
import threading

from sqlalchemy.orm import Session

from app import security
from app.agent import prompts, provider, tools
from app.config import get_settings

_log = logging.getLogger("agent")

_ITER_LIMIT_PROMPT = "工具调用轮数已达上限，请基于以上已获取的数据直接给出最终回答。"


def _parse_args(raw: str | None) -> dict:
    """模型给的 arguments 是 JSON 字符串；非法或非 dict 一律降级为空 dict（不崩对话）。"""
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return args if isinstance(args, dict) else {}


def _agent_loop(db: Session, messages: list[dict], ctx: security.UserContext,
                *, cancel: "threading.Event | None" = None):
    """单一 Agent 循环，yield 事件 dict。

    cancel（threading.Event）为一等入参（RUNTIME-4）：每次 LLM 调用前、流内 chunk 间检查，
    置位即尽快收束——不再只能在事件之间被动等待，收尾作答也可中断。
    """
    msgs: list[dict] = [{"role": "system", "content": prompts.system_prompt()}] + messages
    trace: list[dict] = []

    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    for _ in range(get_settings().llm_max_tool_iters):
        if cancelled():
            yield {"type": "done", "tool_calls": trace, "answer": "", "stopped": True}
            return
        res = None
        for kind, payload in provider.chat_stream(msgs, tools.TOOLS):
            if kind == "delta":
                yield {"type": "delta", "text": payload}
            elif kind == "reasoning":
                yield {"type": "thinking", "text": payload}
            else:
                res = payload
            if cancelled():
                yield {"type": "done", "tool_calls": trace,
                       "answer": (res.content if res else "") or "", "stopped": True}
                return
        if res is None or not res.tool_calls:
            yield {"type": "done", "tool_calls": trace, "answer": (res.content if res else "") or ""}
            return

        if res.content:   # 本轮有旁白又接着调工具：补段落分隔，避免与后文粘连
            yield {"type": "delta", "text": "\n\n"}
        provider.append_assistant_turn(msgs, res)
        for c in res.tool_calls:
            args = _parse_args(c.arguments)
            yield {"type": "tool", "name": c.name, "args": args}
            result = tools.dispatch(db, c.name, args, ctx)
            trace.append({"name": c.name, "args": args})
            _log.info("agent tool=%s args=%s", c.name, args)
            yield {"type": "tool_done", "name": c.name,
                   "ok": not (isinstance(result, dict) and result.get("error"))}
            provider.append_tool_result(msgs, c.id, json.dumps(result, ensure_ascii=False))

    # 轮数上限：收尾作答仍走流式逐字（RUNTIME-5：不再退化为非流式整段，避免最长对话突兀静默）
    msgs.append({"role": "user", "content": _ITER_LIMIT_PROMPT})
    final = None
    for kind, payload in provider.chat_stream(msgs):
        if kind == "delta":
            yield {"type": "delta", "text": payload}
        elif kind == "reasoning":
            yield {"type": "thinking", "text": payload}
        else:
            final = payload
        if cancelled():
            yield {"type": "done", "tool_calls": trace,
                   "answer": (final.content if final else "") or "", "stopped": True}
            return
    yield {"type": "done", "tool_calls": trace, "answer": (final.content if final else "") or ""}


def run(db: Session, messages: list[dict], ctx: security.UserContext) -> dict:
    """非流式：跑完循环，返回 {answer, tool_calls}。

    answer 只取最终答复（done 事件的 answer），不含中间旁白——与旧实现口径一致。
    """
    answer, trace = "", []
    for ev in _agent_loop(db, messages, ctx):
        if ev["type"] == "done":
            answer = ev.get("answer", "")
            trace = ev.get("tool_calls", trace)
    return {"answer": answer, "tool_calls": trace}


def run_stream(db: Session, messages: list[dict], ctx: security.UserContext,
               cancel: "threading.Event | None" = None):
    """流式：把 _agent_loop 的事件原样转发供 SSE 推送。cancel 透传以便及时收束。"""
    yield from _agent_loop(db, messages, ctx, cancel=cancel)
