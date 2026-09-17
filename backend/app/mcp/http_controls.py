"""Bound office JSON requests and prohibit caching of authenticated MCP data."""

from starlette.responses import JSONResponse


class McpHttpControls:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == "/mcp" or path.startswith("/api/mcp-office/")
        ):
            return await self.app(scope, receive, send)

        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"cache-control"
                ]
                message = {
                    **message,
                    "headers": headers
                    + [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                    ],
                }
            await send(message)

        if scope.get("method") in ("POST", "PUT", "PATCH") and "/uploads/" not in path:
            chunks = []
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > 128 * 1024:
                    return await JSONResponse(
                        {"code": "request_too_large", "message": "请求内容过大"},
                        status_code=413,
                    )(scope, receive, private_send)
                chunks.append(chunk)
                if not message.get("more_body"):
                    break
            replayed = False

            async def bounded_receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {
                        "type": "http.request",
                        "body": b"".join(chunks),
                        "more_body": False,
                    }
                return await receive()

            return await self.app(scope, bounded_receive, private_send)
        return await self.app(scope, receive, private_send)
