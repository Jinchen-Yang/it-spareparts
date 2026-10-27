"""itsp → shop 图库服务的最小 HTTP 客户端。

图库归 shop 所有，itsp 只做代理：服务密钥只留在后端，浏览器永远看不到。
调用约束（按集成方案第 3 节）：

- ``Authorization: Bearer <明文密钥>``、``X-Actor-System: itsp``；
- ``X-Actor-User`` 是当前 itsp 登录用户名，URL 编码后透传；
- 写请求透传前端生成的 ``X-Request-Id``（同一操作重试复用，shop 侧幂等）；
- 超时、禁止重定向、限制响应体大小；网络/畸形响应统一抛「不可用」。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.config import get_settings

_log = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ShopGalleryNotConfigured(Exception):
    """基地址或服务密钥为空；代理接口应返回 503「图库服务未配置」。"""


class ShopGalleryUnavailable(Exception):
    """shop 图库服务不可达、超时、重定向或返回无法解析的响应。"""


@dataclass(frozen=True)
class ShopGalleryResponse:
    status_code: int
    body: Any
    headers: dict[str, str]

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class ShopGalleryClient:
    """按请求创建短连接；transport 参数只供测试注入 MockTransport。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes

    def request(
        self,
        method: str,
        path: str,
        *,
        actor_user: str,
        request_id: str | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ShopGalleryResponse:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "X-Actor-System": "itsp",
            "X-Actor-User": quote(actor_user, safe=""),
        }
        if request_id is not None:
            headers["X-Request-Id"] = request_id

        try:
            with httpx.Client(
                timeout=httpx.Timeout(self._timeout, connect=CONNECT_TIMEOUT_SECONDS),
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                with client.stream(
                    method.upper(),
                    f"{self._base_url}{path}",
                    headers=headers,
                    json=json_body,
                    params=params,
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise ShopGalleryUnavailable("shop 图库服务返回重定向")
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            declared_bytes = int(declared)
                        except ValueError as exc:
                            raise ShopGalleryUnavailable(
                                "shop 图库服务响应长度非法"
                            ) from exc
                        if declared_bytes > self._max_response_bytes:
                            raise ShopGalleryUnavailable("shop 图库服务响应过大")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self._max_response_bytes:
                            raise ShopGalleryUnavailable("shop 图库服务响应过大")
                        chunks.append(chunk)
                    body = self._decode(b"".join(chunks))
                    return ShopGalleryResponse(
                        status_code=response.status_code,
                        body=body,
                        headers={key.lower(): value for key, value in response.headers.items()},
                    )
        except ShopGalleryUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            # 只记录异常类型：URL 里可能带 part_id，响应体可能含业务数据。
            _log.warning("shop 图库服务请求失败：%s", type(exc).__name__)
            raise ShopGalleryUnavailable("shop 图库服务不可用") from exc

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _log.warning("shop 图库服务返回非 JSON 响应")
            raise ShopGalleryUnavailable("shop 图库服务响应无法解析") from exc


def get_shop_gallery_client() -> ShopGalleryClient:
    """未配置时抛 ShopGalleryNotConfigured，由 API 层统一映射成 503。"""
    settings = get_settings()
    base_url = settings.shop_gallery_base_url.strip()
    token = settings.shop_gallery_token.get_secret_value().strip()
    if not base_url or not token:
        raise ShopGalleryNotConfigured
    return ShopGalleryClient(base_url, token)
