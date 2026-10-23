"""Control-plane-only error envelope, including FastAPI parser validation."""
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.responses import JSONResponse

from app.authz import runtime
from app.authz.errors import authz_error


class AuthorizationRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except (RequestValidationError, ValidationError) as exc:
                # Never echo input/ctx: malformed payloads can contain passwords.
                fields = [{'path': '/' + '/'.join(str(x) for x in error['loc'] if x != 'body'),
                           'code': 'AUTHZ_INVALID_REQUEST', 'message': '请求字段类型、格式或范围无效'}
                          for error in exc.errors()]
                error = authz_error(422, 'AUTHZ_INVALID_REQUEST', '请求格式无效', field_errors=fields)
            except HTTPException as exc:
                if isinstance(exc.detail, dict) and 'committed' in exc.detail:
                    error = exc
                elif runtime.authorization_mode() == 'unified_modules' or request.url.path.startswith('/api/authz/'):
                    code = {401: 'AUTHN_SESSION_INVALID', 403: 'AUTHZ_FORBIDDEN', 404: 'ACCOUNT_NOT_FOUND'}.get(exc.status_code, 'AUTHZ_REQUEST_REJECTED')
                    error = authz_error(exc.status_code, code, str(exc.detail))
                else:
                    raise
            return JSONResponse({'detail': error.detail}, status_code=error.status_code,
                                headers={**(error.headers or {}), 'Cache-Control': 'no-store'})
        return handler
