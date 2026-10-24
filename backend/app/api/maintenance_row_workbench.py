"""D-33 普通核对导出与行级工作台，不改变往返工作簿协议。"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.auth import current_role
from app.api import maintenance_project_scope as auth_transport
from app.services import maintenance_row_workbench_access as canonical
from app.db import get_db
from app.security import UserContext, get_current_user_context, require_page
from app.services import maintenance_project_master_workbook as master
from app.services import maintenance_row_workbench as workbench
from app.services.maintenance_row_filter_registry import parse_filters
from app.services.maintenance_expense_collection_workbook import WorkbookError
from app.api.maintenance_boss_board import contract_no_filter


def reject_row_sort(request: Request):
    if any(key in request.query_params for key in ("sort", "sort_by", "order_by", "sort_order")):
        raise HTTPException(422, {"code": "unsupported_row_sort", "message": "行级表保留既有顺序，不支持自定义排序"})


router = APIRouter(
    prefix="/maintenance", tags=["maintenance"],
    dependencies=[Depends(current_role), Depends(auth_transport.legacy_page(canonical.RESOURCES, "page_maintenance")),
                  Depends(reject_row_sort)],
)


def _window(preset, date_from, date_to):
    try:
        return master.resolve_range(preset, date_from, date_to)
    except WorkbookError as exc:
        raise HTTPException(422, {"code": exc.code, "message": exc.message}) from exc


@router.get("/spare-part-lines/fields")
@router.get("/projects/stable/{project_id}/master-workbook/row-fields")
def row_fields(
    request: Request,
    response: Response,
    project_id: str | None = None,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(*canonical.RESOURCES)),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
):
    response.headers["Cache-Control"] = "no-store"
    direct_project = "project_id" in request.path_params
    if identity is not None:
        entry = canonical.PROJECT + "/row-fields" if direct_project else canonical.GLOBAL + "/fields"
        return canonical.read_rows(db, identity, entry_id="GET " + entry, params={"project_id": project_id},
            legacy_context=ctx, view=view, precondition=authorization_precondition)
    return {
        "fields": workbench.field_metadata(
            db, ctx, project_id, projection=(
                workbench.project_projection() if direct_project else "workbook"
            ), direct_project=direct_project,
        ),
        "amount_filter_allowed": workbench.can_view_amounts(db, ctx, project_id),
    }


@router.get("/spare-part-lines/rows")
def global_rows(
    response: Response,
    range_preset: str = Query("this_month", alias="range"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    project_id: str | None = Query(None, min_length=1, max_length=36),
    filters: str | None = Query(None),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(*canonical.RESOURCES)),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
):
    response.headers["Cache-Control"] = "no-store"
    if identity is not None:
        return canonical.read_rows(db, identity, entry_id="GET " + canonical.GLOBAL + "/rows",
            params={"project_id": project_id, "range": range_preset, "from": date_from, "to": date_to, "filters": filters},
            legacy_context=ctx, view=view, precondition=authorization_precondition, page=page, page_size=page_size)
    stmt = workbench.query(
        db, ctx, project_id=project_id,
        window=_window(range_preset, date_from, date_to),
        filters=parse_filters(filters),
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "total": workbench.count(db, stmt), "page": page, "page_size": page_size,
        "rows": list(workbench.read_rows(
            db, stmt.offset((page - 1) * page_size).limit(page_size),
            workbench.visible_fields(db, ctx, project_id),
        )),
    }


@router.get("/spare-part-lines/export")
def global_export(
    response: Response,
    range_preset: str = Query("this_month", alias="range"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    project_id: str | None = Query(None, min_length=1, max_length=36),
    filters: str | None = Query(None),
    format: str = Query("xlsx", pattern="^(csv|xlsx)$"),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(*canonical.RESOURCES)),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
):
    if identity is not None:
        result = canonical.read_rows(db, identity, entry_id="GET " + canonical.GLOBAL + "/export",
            params={"project_id": project_id, "range": range_preset, "from": date_from, "to": date_to, "filters": filters, "format": format},
            legacy_context=ctx, view=view, precondition=authorization_precondition)
        response.headers["Cache-Control"] = "no-store"
        return result
    stmt = workbench.query(
        db, ctx, project_id=project_id,
        window=_window(range_preset, date_from, date_to),
        filters=parse_filters(filters), export_order=True,
    )
    return _download(db, stmt, workbench.visible_fields(db, ctx, project_id), format)


@router.get("/projects/stable/{project_id}/master-workbook/rows/export")
def project_export(
    project_id: str,
    response: Response,
    filters: str | None = Query(None),
    order_no: str | None = Query(None, min_length=1, max_length=64),
    contract_no: str | None = Depends(contract_no_filter),
    format: str = Query("xlsx", pattern="^(csv|xlsx)$"),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(*canonical.RESOURCES)),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
):
    if identity is not None:
        result = canonical.read_rows(db, identity, entry_id="GET " + canonical.PROJECT + "/rows/export",
            params={"project_id": project_id, "filters": filters, "order_no": order_no, "contract_no": contract_no, "format": format},
            legacy_context=ctx, view=view, precondition=authorization_precondition)
        response.headers["Cache-Control"] = "no-store"
        return result
    projection = workbench.project_projection()
    stmt = workbench.query(
        db, ctx, project_id=project_id, filters=parse_filters(filters),
        order_no=order_no, contract_no=contract_no, export_order=True,
        projection=projection, direct_project=True,
    )
    return _download(db, stmt, workbench.visible_fields(db, ctx, project_id), format,
                     projection=projection)


def _download(db, stmt, fields, format, *, projection="workbook", release_db=True):
    # 复用已覆盖断连/异常清理的下载出口；不在慢客户端下载期间占用数据库连接。
    from app.api.maintenance import (
        _ClosingStreamingResponse, _csv_stream, _iter_download_chunks, _release_db_before_stream,
        _safe_formula_only,
    )
    workbench.check_export_size(db, stmt)
    if format == "csv":
        return _csv_stream(
            [field.label for field in fields],
            workbench.export_rows(db, stmt, fields, projection=projection),
            "maintenance-filtered-readonly.csv", db=db if release_db else None,
            sanitize=_safe_formula_only,
        )
    output = workbench.xlsx_file(db, stmt, fields, projection=projection)
    if release_db:
        _release_db_before_stream(db, output)
    return _ClosingStreamingResponse(
        _iter_download_chunks(output), output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
        "Content-Disposition": f'attachment; filename="maintenance-filtered-readonly.{format}"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })
