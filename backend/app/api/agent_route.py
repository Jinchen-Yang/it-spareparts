"""No-store Agent transport with closed, non-echoing validation errors."""
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse


class AgentRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            try:
                response = await original(request)
                response.headers['Cache-Control'] = 'no-store'
                return response
            except RequestValidationError:
                return JSONResponse({'detail': {'code': 'AUTHZ_INVALID_REQUEST',
                    'message': '请求字段类型、格式或范围无效'}}, status_code=422,
                    headers={'Cache-Control': 'no-store'})
            except HTTPException as exc:
                return JSONResponse({'detail': exc.detail}, status_code=exc.status_code,
                    headers={**(exc.headers or {}), 'Cache-Control': 'no-store'})
        return handle
