"""LLM 客户端抽象 —— "可切换"的落点。

默认 openai_compatible：用 openai SDK 对接一切 OpenAI 兼容端点
（DeepSeek / 通义 Qwen 兼容模式 / Kimi / GLM …），换厂商只改 .env 的
LLM_BASE_URL + LLM_MODEL + LLM_API_KEY，业务代码零改动。

DeepSeek 上下文缓存为磁盘级自动命中：把固定的 system + tools 放在消息最前
即可享缓存折扣，无需显式 cache_control。将来接 Anthropic：在 chat() 加
provider 分支（anthropic SDK + cache_control），接口保持不变。
"""
import base64
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings

# 推理型视觉模型(如 minimax-m3)会在正文前带 <think>…</think> 思考块，OCR 提取要去掉
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    """去掉推理模型的 <think>…</think> 块；无该块则原样返回。"""
    return _THINK_RE.sub("", text).strip()


class LLMNotConfigured(Exception):
    """未配置 LLM_API_KEY。"""


class VisionNotConfigured(Exception):
    """未配置 VISION_API_KEY（图片/扫描件识别需要）。"""


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
                  timeout=s.llm_timeout_seconds, max_retries=s.llm_max_retries), s


def _create_kwargs(s, messages: list[dict], tools: list[dict] | None) -> dict:
    """组装 chat.completions.create 的公共参数（流式/非流式共用）。"""
    kw = {"model": s.llm_model, "messages": messages, "tools": tools or None,
          "extra_body": s.llm_extra_body_dict()}
    if s.llm_max_tokens:                     # None=不传，用端点默认；设了才透传上限
        kw["max_tokens"] = s.llm_max_tokens
    return kw


# ---------- 线格式装配（OpenAI 专有，集中在此，runtime 不碰；RUNTIME-2）----------
def append_assistant_turn(messages: list[dict], result: "ChatResult") -> None:
    """把模型这一轮回复（含工具调用请求）按当前 provider 的线格式追加进 messages。
    tool_calls/function.arguments 是 OpenAI 约定；将来接 Anthropic 在此实现各自线格式即可，
    runtime 只跟中性的 ChatResult/ToolCall 打交道。"""
    messages.append({
        "role": "assistant",
        "content": result.content,
        "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": c.arguments}}
            for c in result.tool_calls
        ],
    })


def append_tool_result(messages: list[dict], tool_call_id: str, content: str) -> None:
    """把一次工具执行结果按 provider 线格式回灌（OpenAI: role=tool + tool_call_id）。"""
    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})


def chat_stream(messages: list[dict], tools: list[dict] | None = None):
    """流式模型调用：逐段 yield ("delta", 文本)，结束时 yield ("result", ChatResult)。

    流式下 tool_calls 按 index 分片增量到达（name 整段、arguments 逐段拼接），
    在此累积重组，调用方拿到的 ChatResult 与非流式完全一致。
    """
    client, s = _client()
    stream = client.chat.completions.create(**_create_kwargs(s, messages, tools), stream=True)
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


def chat(messages: list[dict], tools: list[dict] | None = None) -> ChatResult:
    """单轮模型调用（非流式）：传入 OpenAI 格式 messages/tools，返回文本或工具调用请求。"""
    client, s = _client()
    resp = client.chat.completions.create(**_create_kwargs(s, messages, tools))
    msg = resp.choices[0].message
    calls = [ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
             for tc in (msg.tool_calls or [])]
    return ChatResult(content=msg.content, tool_calls=calls)


# ============================================================
# 视觉识别（图片/扫描件 → 文本）。独立 key/端点，默认 通义 Qwen-VL
# ============================================================

def vision_configured() -> bool:
    return bool(get_settings().vision_api_key)


def _img_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"


def vision_extract(images: list[Path], hint: str) -> str:
    """图片/扫描件 PDF → 识别文本。images 为图片路径（PDF 由调用方先渲染成图）。

    用 openai SDK 对接 OpenAI 兼容视觉端点（默认 DashScope qwen-vl-max）。
    PDF 路径会先用 pypdfium2 逐页渲染成 PNG 再送（最多前若干页）。
    """
    s = get_settings()
    if not s.vision_api_key:
        raise VisionNotConfigured("未配置 VISION_API_KEY")
    from openai import OpenAI

    # 展开：PDF → 多页图；图片原样
    img_paths: list[Path] = []
    tmp: list[Path] = []
    for p in images:
        if p.suffix.lower() == ".pdf":
            tmp.extend(_render_pdf_pages(p, s.vision_max_pages))
        else:
            img_paths.append(p)
    img_paths.extend(tmp)
    if not img_paths:
        return "(无可识别页面)"

    content = [{"type": "text", "text": hint}]
    for ip in img_paths[: s.vision_max_pages]:
        content.append({"type": "image_url", "image_url": {"url": _img_data_url(ip)}})

    client = OpenAI(api_key=s.vision_api_key, base_url=s.vision_base_url,
                    timeout=s.vision_timeout_seconds)
    try:
        resp = client.chat.completions.create(
            model=s.vision_model,
            messages=[{"role": "user", "content": content}],
        )
        out = _strip_think(resp.choices[0].message.content or "")
        return out or "(视觉模型未返回内容)"
    finally:
        for t in tmp:
            t.unlink(missing_ok=True)


def _render_pdf_pages(pdf_path: Path, max_pages: int) -> list[Path]:
    """PDF 逐页渲染成 PNG（pypdfium2，pdfplumber 依赖自带）。返回临时文件路径。"""
    import pypdfium2 as pdfium

    out: list[Path] = []
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        for i in range(min(len(doc), max_pages)):
            bitmap = doc[i].render(scale=2.0)  # ~144dpi，够清晰
            img = bitmap.to_pil()
            dst = pdf_path.with_name(f"{pdf_path.stem}_p{i}.png")
            img.save(dst)
            out.append(dst)
    finally:
        doc.close()
    return out
