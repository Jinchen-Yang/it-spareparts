"""售前模板下载；导入和应用统一交给 M4 两步接口。"""

import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.presales import Ctx, Db
from app.models.system import SysAccessLog
from app.services.presales import common, inquiry_workbook, quote_workbook, sales_workbook
from app.services.presales.workbook_registration import register_workbooks
from app.services.workbook.schema import WorkbookError
from app.services.workflow.registry import WorkflowError

router = APIRouter(prefix="/presales/workbooks", tags=["presales-workbooks"])
register_workbooks()
InquiryId = Annotated[int, Path(gt=0, le=2**63 - 1)]


class ExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inquiry_ids: list[Annotated[int, Field(strict=True, gt=0, le=2**31 - 1)]] = Field(min_length=1, max_length=200)
    visible_columns: dict[str, list[str]] | None = None


def _columns(value):
    if value is None:
        return None
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        raise WorkbookError("visible_columns 必须是 JSON 对象") from None
    if not isinstance(result, dict) or any(not isinstance(columns, list)
            or any(not isinstance(column, str) for column in columns) for columns in result.values()):
        raise WorkbookError("visible_columns 必须按工作表列出列键")
    return result


def _download(content, filename):
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Cache-Control": "no-store"})


def _error(db, exc):
    db.rollback()
    status = exc.status_code if isinstance(exc, (WorkflowError, WorkbookError)) else 403 if isinstance(exc, PermissionError) else 422
    return HTTPException(status, getattr(exc, "detail", str(exc)))


@router.get("/inquiry/{inquiry_id}")
def inquiry_export(inquiry_id: InquiryId, db: Db, ctx: Ctx,
                   visible_columns: str | None = Query(default=None, max_length=20000)):
    try:
        user, ctx = common.resolve_actor(db, ctx)
        content = inquiry_workbook.export_inquiry(db, inquiry_id, ctx, visible_columns=_columns(visible_columns))
        db.add(SysAccessLog(username=user.username, role=ctx.role, action="presales_inquiry_export",
                            resource="presales", detail={"inquiry_id": inquiry_id}))
        db.commit()
        return _download(content, f"presales-inquiry-{inquiry_id}.xlsx")
    except (WorkflowError, WorkbookError, PermissionError, ValueError) as exc:
        raise _error(db, exc) from exc


@router.get("/inquiry-template")
def inquiry_template(db: Db, ctx: Ctx,
                     visible_columns: str | None = Query(default=None, max_length=20000)):
    try:
        user, ctx = common.resolve_actor(db, ctx)
        content = inquiry_workbook.export_template(db, ctx, visible_columns=_columns(visible_columns))
        db.add(SysAccessLog(username=user.username, role=ctx.role, action="presales_template_export",
                            resource="presales", detail={}))
        db.commit()
        return _download(content, "presales-inquiry-template.xlsx")
    except (WorkflowError, WorkbookError, PermissionError, ValueError) as exc:
        raise _error(db, exc) from exc


@router.post("/quote-sheet")
def quote_sheet(body: ExportInput, db: Db, ctx: Ctx):
    try:
        user, ctx = common.resolve_actor(db, ctx)
        content = quote_workbook.export_quote_sheet(db, body.inquiry_ids, ctx, visible_columns=body.visible_columns)
        db.add(SysAccessLog(username=user.username, role=ctx.role, action="presales_quote_sheet_export",
                            resource="presales", detail={"inquiry_ids": body.inquiry_ids}))
        db.commit()
        return _download(content, "presales-quote-sheet.xlsx")
    except (WorkflowError, WorkbookError, PermissionError, ValueError) as exc:
        raise _error(db, exc) from exc


@router.post("/sales-summary")
def sales_summary(body: ExportInput, db: Db, ctx: Ctx):
    try:
        user, ctx = common.resolve_actor(db, ctx)
        content = sales_workbook.export_sales_summary(db, body.inquiry_ids, ctx, visible_columns=body.visible_columns)
        db.add(SysAccessLog(username=user.username, role=ctx.role, action="presales_summary_export",
                            resource="presales", detail={"inquiry_ids": body.inquiry_ids}))
        db.commit()
        return _download(content, "presales-sales-summary.xlsx")
    except (WorkflowError, WorkbookError, PermissionError, ValueError) as exc:
        raise _error(db, exc) from exc
