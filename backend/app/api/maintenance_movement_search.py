"""跨项目流转搜索（只读 POST）。"""

import csv
from io import StringIO

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.security import UserContext, get_current_user_context, record_access_log, require_page
from app.services.maintenance_movement_search import MovementSearchQuery, search_movements
from app.services.maintenance_dashboard_export_text import ExportCellLimitError, safe_formula_only

router = APIRouter(prefix="/maintenance/movements", tags=["maintenance"])


@router.post("/search")
def search_maintenance_movements(
    body: MovementSearchQuery,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    payload = search_movements(db, body, ctx)
    record_access_log(ctx, "maintenance_movement_search", "maintenance_project", {
        "searched": True, "entity": body.entity, "field": body.field,
        "page": body.page, "page_size": body.page_size,
    })
    return payload


@router.post("/export")
def export_maintenance_movements(
    body: MovementSearchQuery,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> Response:
    """完整筛选集导出；分页参数不裁切结果，超限明确失败。"""
    payload = search_movements(db, body, ctx, export=True)
    buffer = StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["类型", "项目名称", "项目编码", "关联销售单号XSDD", "单号", "日期",
                     "PN", "SN", "导入源SN", "数量", "状态", "来源", "匹配摘要"])
    entities = {"issue": "领用", "receipt": "返还实收", "demand_order": "需求单", "bad_return": "坏件返还"}
    for row in payload["items"]:
        project, document = row["project"], row["document"]
        number = document.get("number") or ""
        if number.startswith("LY-AUTO-") and document.get("display_number"):
            number = document["display_number"]
        values = [entities[row["entity_type"]], project["name"], project["code"],
                  " / ".join(project.get("sales_order_nos") or []), number, document.get("date"),
                  row["pn"], " / ".join(row["serials"]), row.get("source_serial_number"),
                  row["quantity"], document["status"], document["source"], row["excerpt"]]
        try:
            writer.writerow([safe_formula_only(value) for value in values])
        except ExportCellLimitError as exc:
            raise HTTPException(413, "导出单元格内容超限，请收窄搜索条件或在页面查看该记录") from exc
        if buffer.tell() > 8 * 1024 * 1024:
            raise HTTPException(413, "导出内容超限，请收窄搜索条件")
    record_access_log(ctx, "maintenance_movement_export", "maintenance_project", {
        "entity": body.entity, "field": body.field, "rows": len(payload["items"]),
    })
    return Response(content="\ufeff" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="maintenance-movements.csv"',
                             "Cache-Control": "no-store"})
