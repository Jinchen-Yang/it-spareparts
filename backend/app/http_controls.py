"""Path-scoped HTTP controls for sensitive maintenance migration traffic."""

from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse


class _RequestBodyTooLarge(Exception):
    pass


class MigrationHttpControlsMiddleware:
    """Bound migration request bodies and disable caching on every response."""

    def __init__(self, app: Any, *, path_prefix: str, max_body_bytes: int) -> None:
        self.app = app
        self.path_prefix = path_prefix.rstrip("/")
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or not str(scope.get("path") or "").startswith(
            self.path_prefix
        ):
            await self.app(scope, receive, send)
            return

        async def no_store_send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in {b"cache-control", b"pragma", b"expires"}
                ]
                headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"pragma", b"no-cache"),
                        (b"expires", b"0"),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        if str(scope.get("method") or "").upper() != "POST":
            await self.app(scope, receive, no_store_send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                declared_size = int(declared.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                await JSONResponse(
                    {"detail": "Content-Length 格式无效"}, status_code=400
                )(scope, receive, no_store_send)
                return
            if declared_size < 0:
                await JSONResponse(
                    {"detail": "Content-Length 格式无效"}, status_code=400
                )(scope, receive, no_store_send)
                return
            if declared_size > self.max_body_bytes:
                await JSONResponse(
                    {"detail": "维保迁移请求体超过安全上限"}, status_code=413
                )(scope, receive, no_store_send)
                return

        consumed = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body") or b"")
                if consumed > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, no_store_send)
        except _RequestBodyTooLarge:
            await JSONResponse(
                {"detail": "维保迁移请求体超过安全上限"}, status_code=413
            )(scope, receive, no_store_send)
