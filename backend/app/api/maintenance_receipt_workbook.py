"""Signed receipt editing: download, preview, confirm; existing action/scope gates."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.maintenance_bad_returns import _real_operator
from app.api.maintenance_expense_collection_workbook import _read_upload
from app.api.maintenance_project_scope import enforce_maintenance_project_access
from app.api.maintenance_return_receipts import ReceiptAuditRoute, _raise_service_error
from app.auth import current_identity
from app import permissions
from app.models.system import SysUser
from app.db import get_db
from app.maintenance_boss import require_maintenance_boss
from app.security import UserContext, get_current_user_context, record_access_log, require_action, require_page
from app.services import maintenance_receipt_workbook as workbook
from app.services import maintenance_return_receipts as receipts

router = APIRouter(prefix="/maintenance/projects/stable", tags=["maintenance"], route_class=ReceiptAuditRoute,
    dependencies=[Depends(require_maintenance_boss), Depends(require_page("page_maintenance")),
                  Depends(require_action("action_maintenance_bad_return_manage"))])
BASE = "/{project_id}/return-receipt-workbook"


@router.get(BASE + ".xlsx")
def download(project_id: str, db: Session = Depends(get_db),
             ctx: UserContext = Depends(get_current_user_context)):
    receipts.lock_receipt_context(db)
    receipts.lock_receipt_projects(db, {project_id})
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    try:
        content = workbook.build(db, project_id=project_id)
    except Exception as exc:
        _raise_service_error(exc)
        raise
    record_access_log(ctx, "receipt_workbook_download", "maintenance_project", {"project_id": project_id})
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="return-receipts-{project_id}.xlsx"',
                             "Cache-Control": "no-store"})


def _run(db: Session, project_id: str, ctx: UserContext, ident: dict, data: bytes, *, digest: str | None):
    operator = _real_operator(db, ident)
    def authorize():
        # Same current-account reconstruction as master workbook reauthorization;
        # shared user lock keeps revocation ordered after this committed operation.
        user = db.scalar(select(SysUser).where(SysUser.username == operator)
            .with_for_update(read=True).execution_options(populate_existing=True))
        if user is None or not user.is_active or int(ident.get("tv", 0)) != int(user.token_version or 0):
            raise HTTPException(401, "登录状态已失效，请重新登录")
        fresh = UserContext(user_id=operator, role=user.role, salesperson_name=user.salesperson_name,
            permissions=permissions.runtime_safe(permissions.effective_for_user(user)), is_authenticated=True)
        require_maintenance_boss()
        require_page("page_maintenance")(fresh)
        require_action("action_maintenance_bad_return_manage")(fresh)
        enforce_maintenance_project_access(db, project_id=project_id, ctx=fresh)
    try:
        args = dict(data=data, project_id=project_id, operator=operator, authorize=authorize)
        if digest is None:
            result = workbook.preview(db, **args)
            db.rollback()
        else:
            result = workbook.apply(db, digest=digest, **args)
            db.commit()
        return result
    except Exception as exc:
        db.rollback()
        if isinstance(exc, HTTPException):
            raise
        _raise_service_error(exc)
        raise


@router.post(BASE + "/validate")
async def validate(project_id: str, request: Request, response: Response,
                   db: Session = Depends(get_db), ident: dict = Depends(current_identity),
                   ctx: UserContext = Depends(get_current_user_context)):
    data = await _read_upload(request)
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(_run, db, project_id, ctx, ident, data, digest=None)


@router.post(BASE + "/apply")
async def apply(project_id: str, request: Request, response: Response,
                db: Session = Depends(get_db), ident: dict = Depends(current_identity),
                ctx: UserContext = Depends(get_current_user_context)):
    data = await _read_upload(request)
    form = await request.form()
    digest = str(form.get("digest") or "")
    if not digest or len(digest) > 4096:
        raise HTTPException(422, "请先预览同一文件再确认应用")
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(_run, db, project_id, ctx, ident, data, digest=digest)
