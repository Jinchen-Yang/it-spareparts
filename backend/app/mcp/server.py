"""Official SDK Streamable HTTP transport; all requests authenticated before SDK."""

import anyio
from fastapi import HTTPException
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from starlette.responses import JSONResponse

from app.mcp.core import McpError, actor_context, authenticate, get_settings, json
from app.mcp.tools import REGISTRY, invoke, tool_allowed

server = Server("PARTFLOW", version="1.0.0")


@server.list_tools()
async def list_tools():
    actor = actor_context.get()
    return [
        Tool(
            name=r["name"],
            title=r["title"],
            description=r["description"],
            inputSchema=r["inputSchema"],
            outputSchema=r["outputSchema"],
            annotations=ToolAnnotations(**r["annotations"]),
        )
        for r in REGISTRY.values()
        if tool_allowed(actor, r["name"])
    ]


@server.call_tool()
async def call_tool(name, arguments):
    try:
        result = await anyio.to_thread.run_sync(
            lambda: invoke(actor_context.get(), name, arguments)
        )
        return CallToolResult(
            content=[
                TextContent(type="text", text=json.dumps(result, ensure_ascii=False))
            ],
            structuredContent=result,
        )
    except McpError as exc:
        result = {"code": exc.code, "message": exc.message}
    except HTTPException as exc:
        result = {
            "code": {
                401: "unauthenticated",
                403: "permission_denied",
                404: "not_found",
                409: "business_conflict",
            }.get(exc.status_code, "business_error"),
            "message": "业务权限或状态不允许此操作，请核对账号和项目范围",
        }
    except Exception:  # noqa: BLE001 - protocol/worker boundary must sanitize failures
        result = {
            "code": "operation_failed",
            "message": "操作失败，未确认完成；请通过任务编号核实结果",
        }
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
    )


def create_manager():
    return StreamableHTTPSessionManager(
        server, stateless=True, json_response=True, max_request_body_size=128 * 1024
    )


class McpTransport:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        headers = dict(scope.get("headers", []))
        try:
            origin = headers.get(b"origin", b"").decode()
            if origin and origin.rstrip(
                "/"
            ) != get_settings().mcp_public_base_url.rstrip("/"):
                raise McpError("invalid_origin", "请求来源不允许", 403)
            auth = headers.get(b"authorization", b"").decode()
            if not auth.startswith("Bearer "):
                raise McpError("unauthenticated", "需要 MCP 连接凭据", 401)
            actor = await anyio.to_thread.run_sync(lambda: authenticate(auth[7:]))
        except McpError as exc:
            await JSONResponse(
                {"code": exc.code, "message": exc.message},
                status_code=exc.status,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        token = actor_context.set(actor)
        try:
            await scope["app"].state.mcp_manager.handle_request(scope, receive, send)
        finally:
            actor_context.reset(token)
