"""将 Shop 专属的好坏件库存附到机型目录。跨服务故障保留目录与未知库存。"""

import asyncio
import hashlib
import hmac
from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings

MAX_IDS = 1000
MAX_REPLY_BYTES = 1024 * 1024
TIMEOUT = 8.0
Quantity = Annotated[int, Field(ge=0, le=2**31 - 1)]


class StockItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    partId: str
    good: Quantity | None
    bad: Quantity | None


class StockReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: Annotated[list[StockItem], Field(max_length=MAX_IDS)]
    asOf: str


def _endpoint(settings) -> tuple[str, str]:
    url = settings.model_parts_shop_stock_url
    parsed = urlsplit(url)
    token = settings.model_parts_shop_stock_token.get_secret_value()
    catalog_hash = settings.model_parts_catalog_token_sha256.get_secret_value()
    if (
        not parsed.hostname
        or (
            parsed.scheme != "https"
            and not (
                parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            )
        )
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not 32 <= len(token) <= 256
        or not all(0x21 <= ord(c) <= 0x7E for c in token)
        or hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), catalog_hash)
    ):
        raise ValueError("库存读取配置无效")
    return url, token


async def _read_batch(client: httpx.AsyncClient, url: str, ids: list[str]):
    async with client.stream("POST", url, json={"partIds": ids}) as response:
        if (
            response.status_code != 200
            or response.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            raise ValueError("商城库存响应无效")
        length = response.headers.get("content-length")
        if length is not None and not 0 <= int(length) <= MAX_REPLY_BYTES:
            raise ValueError("库存响应过大")
        if response.is_stream_consumed:
            raw = response.content
            if len(raw) > MAX_REPLY_BYTES:
                raise ValueError("库存响应过大")
        else:
            raw = bytearray()
            async for chunk in response.aiter_raw():
                if len(raw) + len(chunk) > MAX_REPLY_BYTES:
                    raise ValueError("库存响应过大")
                raw.extend(chunk)
    reply = StockReply.model_validate_json(raw)
    if len(reply.items) != len(ids) or {row.partId for row in reply.items} != set(ids):
        raise ValueError("库存响应和请求的备件身份不一致")
    stamp = datetime.fromisoformat(reply.asOf)
    if stamp.utcoffset() is None:
        raise ValueError("库存读取时间未含时区")
    return reply.items, stamp


async def _read(ids: list[str], url: str, token: str, transport):
    values = {}
    oldest = None
    async with (
        asyncio.timeout(TIMEOUT),
        httpx.AsyncClient(
            headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client,
    ):
        # 空清单也能区分「可读取库存」与「未配置」。多批取各批最早的读取时间。
        for offset in range(0, max(1, len(ids)), MAX_IDS):
            rows, stamp = await _read_batch(client, url, ids[offset : offset + MAX_IDS])
            oldest = stamp if oldest is None else min(stamp, oldest)
            values.update((row.partId, (row.good, row.bad)) for row in rows)
    return values, oldest.isoformat()


def enrich_model_parts_stock(catalog: dict, *, settings=None, transport=None):
    """同步 itsp 路由在数据库只读事务结束后调用。任何批次失败都不能变成零库存。"""
    parts = [dict(part, good=None, bad=None) for part in catalog["parts"]]
    result = {**catalog, "parts": parts}
    try:
        url, token = _endpoint(settings or get_settings())
        ids = list(dict.fromkeys(part["id"] for part in parts))
        values, timestamp = asyncio.run(_read(ids, url, token, transport))
    except (TimeoutError, httpx.HTTPError, httpx.InvalidURL, ValueError):
        return result, "unavailable", None
    for part in parts:
        part["good"], part["bad"] = values[part["id"]]
    return result, "live", timestamp
