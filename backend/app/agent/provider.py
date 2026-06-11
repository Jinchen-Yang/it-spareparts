"""LLM 客户端抽象 —— "可切换"的落点。

默认 openai_compatible：用 openai SDK 对接一切 OpenAI 兼容端点
（DeepSeek / 通义 Qwen 兼容模式 / Kimi / GLM …），换厂商只改 .env 的
LLM_BASE_URL + LLM_MODEL + LLM_API_KEY，业务代码零改动。

DeepSeek 上下文缓存为磁盘级自动命中：把固定的 system + tools 放在消息最前
即可享缓存折扣，无需显式 cache_control。将来接 Anthropic：在 chat() 加
provider 分支（anthropic SDK + cache_control），接口保持不变。
"""
from dataclasses import dataclass, field

from app.config import get_settings


class LLMNotConfigured(Exception):
    """未配置 LLM_API_KEY。"""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON 字符串（按 OpenAI 约定）


@dataclass
class ChatResult:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


def is_configured() -> bool:
    return bool(get_settings().llm_api_key)


def _client():
    s = get_settings()
    if not s.llm_api_key:
        raise LLMNotConfigured("未配置 LLM_API_KEY")
    if s.llm_provider != "openai_compatible":
        raise NotImplementedError(f"未知 LLM provider: {s.llm_provider}")
    from openai import OpenAI  # 延迟导入：未配置 LLM 时后端其余功能不依赖该包
    return OpenAI(api_key=s.llm_api_key, base_url=s.llm_base_url,
                  timeout=s.llm_timeout_seconds), s


def chat_stream(messages: list[dict], tools: list[dict] | None = None):
    """流式模型调用：逐段 yield ("delta", 文本)，结束时 yield ("result", ChatResult)。

    流式下 tool_calls 按 index 分片增量到达（name 整段、arguments 逐段拼接），
    在此累积重组，调用方拿到的 ChatResult 与非流式完全一致。
    """
    client, s = _client()
    stream = client.chat.completions.create(
        model=s.llm_model, messages=messages, tools=tools or None,
        extra_body=_extra_body(s.llm_extra_body), stream=True,
    )
    content_parts: list[str] = []
    acc: dict[int, dict] = {}
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            content_parts.append(delta.content)
            yield "delta", delta.content
        for tc in (delta.tool_calls or []):
            slot = acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
            if tc.id:
                slot["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function.arguments:
                    slot["arguments"] += tc.function.arguments
    calls = [ToolCall(id=v["id"], name=v["name"], arguments=v["arguments"])
             for _, v in sorted(acc.items())]
    yield "result", ChatResult(content="".join(content_parts) or None, tool_calls=calls)


def _extra_body(raw: str) -> dict | None:
    """LLM_EXTRA_BODY(JSON 字符串) → dict；空/非法返回 None（不阻断调用）。"""
    import json
    if not raw or not raw.strip():
        return None
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) and v else None
    except json.JSONDecodeError:
        return None


def chat(messages: list[dict], tools: list[dict] | None = None) -> ChatResult:
    """单轮模型调用（非流式）：传入 OpenAI 格式 messages/tools，返回文本或工具调用请求。"""
    client, s = _client()
    resp = client.chat.completions.create(
        model=s.llm_model,
        messages=messages,
        tools=tools or None,
        extra_body=_extra_body(s.llm_extra_body),
    )
    msg = resp.choices[0].message
    calls = [ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
             for tc in (msg.tool_calls or [])]
    return ChatResult(content=msg.content, tool_calls=calls)
