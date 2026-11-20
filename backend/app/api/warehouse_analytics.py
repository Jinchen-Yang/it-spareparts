"""Warehouse analytics reports. These resources read effective source documents only."""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import permissions
from app.auth import current_identity
from app.db import get_db
from app.services import warehouse_analytics as service


def _require_inventory_access(ident: Annotated[dict, Depends(current_identity)]) -> None:
    # Inventory always uses the authenticated account grant, including deployments
    # operating older RBAC-compatible pages with their role checks disabled.
    if not permissions.page_permission_allowed(
        role=ident["role"],
        permission_map=ident.get("perms"),
        page_key=permissions.INVENTORY_ANALYTICS_PAGE,
    ):
        raise HTTPException(403, "无权访问库存管理分析")


router = APIRouter(
    prefix="/inventory/analytics",
    tags=["inventory-analytics"],
    dependencies=[Depends(_require_inventory_access)],
)


def _db(db: Annotated[Session, Depends(get_db)]):
    # Each response may need multiple aggregates and evidence queries. A concurrent
    # successful source refresh must appear either before or after the whole report.
    if not db.in_transaction():
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    return db


def _filters(
    date_from: date | None = None,
    date_to: date | None = None,
    source: str | None = Query(None, max_length=20),
    center: str | None = Query(None, max_length=256),
    operator: str | None = Query(None, max_length=256),
    operator_role: Literal["pick", "direct", "outbound"] | None = None,
    kind: str | None = Query(None, max_length=256),
    pn: str | None = Query(None, max_length=256),
    unit: str | None = Query(None, max_length=64),
    category: str | None = Query(None, max_length=256),
    brand: str | None = Query(None, max_length=256),
    warehouse: str | None = Query(None, max_length=256),
    bin_name: str | None = Query(None, max_length=256),
    test_result: str | None = Query(None, max_length=256),
    tester: str | None = Query(None, max_length=256),
    whole_pn: str | None = Query(None, max_length=256),
    whole_unit: str | None = Query(None, max_length=64),
    q: str | None = Query(None, max_length=128),
    scope: str | None = Query(None, max_length=4096),
    require_lines: bool = False,
    testing: Literal["required", "completed", "pending", "unknown", "attributed"] | None = None,
):
    exact = {
        "source": source,
        "center": center,
        "operator": operator,
        "operator_role": operator_role,
        "kind": kind,
        "pn": pn,
        "unit": unit,
        "category": category,
        "brand": brand,
        "warehouse": warehouse,
        "bin_name": bin_name,
        "test_result": test_result,
        "tester": tester,
        "whole_pn": whole_pn,
        "whole_unit": whole_unit,
    }
    try:
        return service.read_filters(date_from, date_to, exact, scope, q, testing, require_lines)
    except service.InvalidQuery as exc:
        raise HTTPException(422, str(exc)) from exc


ReportFilters = Annotated[service.Filters, Depends(_filters)]
ReportSession = Annotated[Session, Depends(_db)]
Metric = Literal["documents", "lines", "pns", "quantity", "active_days"]


@router.get("/coverage")
def coverage(db: ReportSession):
    return service.coverage(db)


@router.get("/cube")
def cube(
    filters: ReportFilters,
    db: ReportSession,
    row: str = Query("source", max_length=32),
    col: str | None = Query(None, max_length=32),
    metric: Metric = "documents",
    row_limit: int = Query(30, ge=1, le=366),
    col_limit: int = Query(10, ge=1, le=64),
):
    try:
        return service.cube(db, filters, row, col, metric, row_limit, col_limit)
    except service.InvalidQuery as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/rankings")
def rankings(
    filters: ReportFilters,
    db: ReportSession,
    by: Literal["operator", "pn"] = "operator",
    metric: Metric = "documents",
    page: int = Query(1, ge=1, le=1_000_000),
    page_size: int = Query(20, ge=1, le=200),
):
    try:
        return service.rankings(db, filters, by, metric, page, page_size)
    except service.InvalidQuery as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/documents")
def documents(
    filters: ReportFilters,
    db: ReportSession,
    page: int = Query(1, ge=1, le=1_000_000),
    page_size: int = Query(50, ge=1, le=200),
):
    return service.documents(db, filters, page, page_size)


@router.get("/documents/{document_id}/lines")
def lines(
    document_id: int,
    filters: ReportFilters,
    db: ReportSession,
    page: int = Query(1, ge=1, le=1_000_000),
    page_size: int = Query(50, ge=1, le=200),
):
    data = service.document_lines(db, filters, document_id, page, page_size)
    if data is None:
        raise HTTPException(404, "当前筛选范围内未找到该仓库单据")
    return data


@router.get("/equipment")
def equipment(
    filters: ReportFilters,
    db: ReportSession,
    page: int = Query(1, ge=1, le=1_000_000),
    page_size: int = Query(20, ge=1, le=200),
):
    return service.equipment(db, filters, page, page_size)


@router.get("/testing")
def testing(
    filters: ReportFilters,
    db: ReportSession,
    by: Literal["center", "tester"] = "center",
):
    return service.testing(db, filters, by)
