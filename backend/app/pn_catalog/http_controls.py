"""Bound every catalog body before parsing; never cache catalog responses."""
import hashlib
from uuid import UUID, uuid4

from starlette.responses import JSONResponse


def error_body(code, message, request_id, *, details=None, retryable=False):
    return {"error": {"code": code, "message": message, "request_id": str(request_id),
                      "retryable": retryable, "retry_after_seconds": None,
                      "details": details or {}}}


class CatalogHttpControls:
    def __init__(self, app, *, path_prefix="/api/pn-catalog/v1", max_body_bytes=65536):
        self.app = app
        self.path_prefix = path_prefix.rstrip("/")
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") != "http" or not (
            path == self.path_prefix or path.startswith(self.path_prefix + "/")
        ):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", ()))
        try:
            request_id = UUID(headers.get(b"idempotency-key", headers.get(b"x-request-id", b"")).decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            request_id = uuid4()
        scope.setdefault("state", {})["pn_catalog_request_id"] = request_id

        async def no_store(message):
            if message["type"] == "http.response.start":
                response_headers = [(k, v) for k, v in message.get("headers", ())
                                    if k.lower() not in {b"cache-control", b"x-request-id"}]
                response_headers += [(b"cache-control", b"no-store"),
                                     (b"x-request-id", str(request_id).encode())]
                message = {**message, "headers": response_headers}
            await send(message)

        async def reject(status, code, message):
            await JSONResponse(error_body(code, message, request_id), status_code=status)(
                scope, receive, no_store)

        for name in (b"idempotency-key", b"if-match", b"x-request-id"):
            if sum(key.lower() == name for key, _ in scope.get("headers", ())) > 1:
                return await reject(422, "request.validation_failed", "请求前置条件不能重复")
        if trace := headers.get(b"x-request-id"):
            try:
                trace_id = UUID(trace.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                return await reject(422, "request.validation_failed", "X-Request-ID 必须是 UUID")
            if b"idempotency-key" in headers and trace_id != request_id:
                return await reject(422, "request.validation_failed", "请求编号与幂等编号不一致")

        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                length = int(declared)
                if length < 0:
                    raise ValueError
            except ValueError:
                return await reject(400, "request.invalid_json", "请求长度无效")
            if length > self.max_body_bytes:
                return await reject(413, "request.payload_too_large", "请求体超过上限")
        # Buffer only up to the fixed budget. This also covers chunked requests
        # with missing/incorrect Content-Length before any database mutation.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes:
                return await reject(413, "request.payload_too_large", "请求体超过上限")
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        scope["state"]["pn_catalog_body_sha256"] = hashlib.sha256(body).hexdigest()
        sent = False

        async def replay_body():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_body, no_store)
