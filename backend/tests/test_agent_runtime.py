"""Agent runtime PR-2：run/run_stream 合一(RUNTIME-1) + cancel 一等参数(RUNTIME-4) +
韧性配置(RUNTIME-3 max_tokens/max_retries、RUNTIME-6 extra_body 启动期校验)。

用假 provider.chat_stream 驱动，不依赖 LLM key / DB。"""
import json
import threading

import pytest

from app import security
from app.agent import provider, runtime, tools
from app.config import Settings
from app.models.chat import ChatMessage
from app.services import chat_store

_CTX = security.UserContext(user_id=None, role="phase1_full_access")
_MSGS = [{"role": "user", "content": "hi"}]


def _two_round_stream():
    """第1轮：旁白 + 工具调用；第2轮：最终答复（无工具）。"""
    calls = {"n": 0}

    def fake(messages, tools_=None):
        calls["n"] += 1
        if calls["n"] == 1:
            yield "delta", "我先查一下"
            yield "result", provider.ChatResult(
                content="我先查一下",
                tool_calls=[provider.ToolCall(id="c1", name="search_parts",
                                              arguments='{"query":"x"}')])
        else:
            yield "delta", "最终答复"
            yield "result", provider.ChatResult(content="最终答复", tool_calls=[])
    return fake


def test_run_takes_final_answer_only(monkeypatch):
    monkeypatch.setattr(provider, "chat_stream", _two_round_stream())
    monkeypatch.setattr(tools, "dispatch", lambda db, n, a, c: {"ok": True})
    out = runtime.run(None, _MSGS, _CTX)
    # 非流式只取最终答复，不含中间旁白（与旧实现口径一致）
    assert out["answer"] == "最终答复"
    assert out["tool_calls"] == [{
        "name": "search_parts",
        "args": {
            "outcome": "success",
            "arg_count": 1,
            "arg_keys": ["query"],
        },
    }]


def test_run_stream_emits_event_sequence(monkeypatch):
    monkeypatch.setattr(provider, "chat_stream", _two_round_stream())
    monkeypatch.setattr(tools, "dispatch", lambda db, n, a, c: {"ok": True})
    evs = list(runtime.run_stream(None, _MSGS, _CTX))
    assert [e["type"] for e in evs] == ["delta", "delta", "tool", "tool_done", "delta", "done"]
    tool_done = next(e for e in evs if e["type"] == "tool_done")
    assert tool_done["ok"] is True
    tool_started = next(e for e in evs if e["type"] == "tool")
    assert tool_started == {
        "type": "tool",
        "name": "search_parts",
        "args": {
            "outcome": "started",
            "arg_count": 1,
            "arg_keys": ["query"],
        },
    }
    done = evs[-1]
    assert done["answer"] == "最终答复"
    assert done["tool_calls"] == [{
        "name": "search_parts",
        "args": {
            "outcome": "success",
            "arg_count": 1,
            "arg_keys": ["query"],
        },
    }]


def test_runtime_sse_trace_checkpoint_and_log_never_contain_argument_values(
    db, monkeypatch, caplog
):
    sentinel = "CUSTOMER-RUNTIME-SECRET-31f2"
    rounds = {"count": 0}

    def fake(messages, tools_=None):
        rounds["count"] += 1
        if rounds["count"] == 1:
            yield "result", provider.ChatResult(
                content=None,
                tool_calls=[provider.ToolCall(
                    id="c1",
                    name="search_parts",
                    arguments=(
                        '{"query":"' + sentinel + '","' + sentinel + '":"value"}'
                    ),
                )],
            )
        else:
            yield "result", provider.ChatResult(content="done", tool_calls=[])

    monkeypatch.setattr(provider, "chat_stream", fake)
    monkeypatch.setattr(tools, "dispatch", lambda *_args: {"ok": True})
    monkeypatch.setattr(runtime._log, "disabled", False)
    monkeypatch.setattr(runtime._log, "propagate", True)
    with caplog.at_level("INFO", logger="agent"):
        events = list(runtime.run_stream(None, _MSGS, _CTX))

    # SSE 与 done trace 只携带 schema-derived 结构，不带原始参数值。
    assert sentinel not in json.dumps(events, ensure_ascii=False)
    tool_event = next(e for e in events if e["type"] == "tool")
    assert tool_event["args"] == {
        "outcome": "started",
        "arg_count": 2,
        "arg_keys": ["query"],
    }
    trace = events[-1]["tool_calls"]
    assert trace == [{
        "name": "search_parts",
        "args": {
            "outcome": "success",
            "arg_count": 2,
            "arg_keys": ["query"],
        },
    }]

    # 模拟 chat checkpoint 全链路：新落库行也不得出现哨兵值。
    session = chat_store.create_session(db, "runtime-privacy-owner")
    db.commit()
    message_id = chat_store.save_assistant_progress(
        session.id, None, "done", trace, stopped=False,
    )
    db.expire_all()
    stored = db.get(ChatMessage, message_id)
    assert stored is not None
    assert sentinel not in json.dumps(stored.tools, ensure_ascii=False)

    assert sentinel not in caplog.text
    assert "agent tool=search_parts" in caplog.text
    assert "arg_keys=['query']" in caplog.text


def test_run_stream_cancel_stops_promptly(monkeypatch):
    """cancel 置位后，循环在 chunk 间收束并发 stopped done（不等整轮跑完）。"""
    cancel = threading.Event()

    def fake(messages, tools_=None):
        yield "delta", "部分"
        cancel.set()              # 模拟流中途用户点"停止"
        yield "delta", "更多"
        yield "result", provider.ChatResult(content="部分更多", tool_calls=[])
    monkeypatch.setattr(provider, "chat_stream", fake)

    evs = list(runtime.run_stream(None, _MSGS, _CTX, cancel=cancel))
    assert evs[-1]["type"] == "done" and evs[-1].get("stopped") is True
    # 取消后不应再有 tool 事件（没跑到工具就收束）
    assert not any(e["type"] == "tool" for e in evs)


# ---------- 韧性配置 ----------
def test_extra_body_validation_and_dict():
    s = Settings(llm_extra_body='{"thinking": {"type": "disabled"}}')
    assert s.llm_extra_body_dict() == {"thinking": {"type": "disabled"}}
    assert Settings(llm_extra_body="{}").llm_extra_body_dict() is None   # 空对象→不透传
    # 非法 JSON / 非 dict → 启动期拒绝（不再静默回退成 None）
    with pytest.raises(Exception):
        Settings(llm_extra_body="not-json")
    with pytest.raises(Exception):
        Settings(llm_extra_body="[1, 2]")


def test_create_kwargs_max_tokens_optional():
    assert "max_tokens" not in provider._create_kwargs(Settings(llm_max_tokens=None), [], None)
    assert provider._create_kwargs(Settings(llm_max_tokens=1024), [], None)["max_tokens"] == 1024


# ---------- 思考链（thinking）----------
def test_default_extra_body_enables_thinking():
    # 空 LLM_EXTRA_BODY → 回退默认；默认现在开思考
    assert Settings(llm_extra_body="").llm_extra_body_dict() == {"thinking": {"type": "enabled"}}


def test_run_stream_forwards_reasoning_as_thinking(monkeypatch):
    def fake(messages, tools_=None):
        yield "reasoning", "先看一下最近采购价"
        yield "reasoning", "，再算个加点"
        yield "delta", "建议报 2200"
        yield "result", provider.ChatResult(content="建议报 2200", tool_calls=[])
    monkeypatch.setattr(provider, "chat_stream", fake)
    evs = list(runtime.run_stream(None, _MSGS, _CTX))
    thinking = [e["text"] for e in evs if e["type"] == "thinking"]
    assert thinking == ["先看一下最近采购价", "，再算个加点"]   # 逐段转发，不计入正文
    done = evs[-1]
    assert done["type"] == "done" and done["answer"] == "建议报 2200"


def test_run_ignores_thinking_in_answer(monkeypatch):
    def fake(messages, tools_=None):
        yield "reasoning", "内部思考不该进答复"
        yield "delta", "最终答复"
        yield "result", provider.ChatResult(content="最终答复", tool_calls=[])
    monkeypatch.setattr(provider, "chat_stream", fake)
    out = runtime.run(None, _MSGS, _CTX)   # 非流式只取 done.answer
    assert out["answer"] == "最终答复" and "思考" not in out["answer"]
