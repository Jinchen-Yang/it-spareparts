"""Request correlation for presales operations without logging business payloads."""

import logging
import traceback
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_log = logging.getLogger("app.request_diagnostics")


def _log_failure(
    scope: Scope, request_id: str, status_code: int, exc: Exception | None = None
) -> None:
    # A registered route template is safe to log; the raw path may contain user
    # input (including unmatched paths), and query strings are never recorded.
    route = getattr(scope.get("route"), "path", "<unmatched>")
    method = scope.get("method")
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        method = "OTHER"
    fields = {
        "request_id": request_id,
        "method": method,
        "route": route,
        "status_code": status_code,
    }
    if exc is not None:
        # Exception text, SQL parameters, source lines and frame locals can all
        # contain credentials or business data. Keep only code locations.
        frames = [
            f"{frame.f_code.co_filename}:{line}:{frame.f_code.co_name}"
            for frame, line in traceback.walk_tb(exc.__traceback__)
        ]
        _log.error(
            "request_failed request_id=%s method=%s route=%s status=%s "
            "error_type=%s frames=%s",
            request_id,
            method,
            route,
            status_code,
            type(exc).__name__,
            frames,
            extra=fields,
        )
    else:
        _log.log(
            logging.ERROR if status_code >= 500 else logging.WARNING,
            "request_failed request_id=%s method=%s route=%s status=%s",
            request_id,
            method,
            route,
            status_code,
            extra=fields,
        )


class RequestDiagnosticsMiddleware:
    """Attach a server-generated ID to scoped responses and failure logs."""

    def __init__(self, app: ASGIApp, *, path_prefixes: tuple[str, ...]) -> None:
        self.app = app
        self.path_prefixes = tuple(prefix.rstrip("/") for prefix in path_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in self.path_prefixes
        ):
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        status_code: int | None = None

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            _log_failure(scope, request_id, status_code or 500, exc)
            if status_code is not None:
                # Do not send a second response if a stream/background task
                # fails. Preserve the transport failure, without repeating the
                # original exception text in the ASGI server's error log.
                raise RuntimeError(
                    f"Response interrupted; request_id={request_id}"
                ) from None
            # Match Starlette's existing non-debug 500 body; handled HTTP and
            # validation errors are still rendered by FastAPI, unchanged.
            await PlainTextResponse("Internal Server Error", status_code=500)(
                scope, receive, send_with_request_id
            )
        else:
            if status_code is not None and status_code >= 400:
                _log_failure(scope, request_id, status_code)
