"""Cross-project read-only bad receipt export for an independent resale system."""

from __future__ import annotations

import hashlib
import hmac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine

router = APIRouter(prefix="/integrations/resale", tags=["resale-readonly"])


def _integration_key(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().resale_export_token_sha256.get_secret_value()
    if not expected:
        raise HTTPException(status_code=404, detail="返件只读导出尚未启用")
    scheme, separator, token = (authorization or "").partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not 32 <= len(token) <= 256:
        raise HTTPException(status_code=401, detail="只读导出凭据无效")
    provided = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="只读导出凭据无效")


def _source_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="返件来源 ID 无效") from exc


def _receipt_item(row: MaintenanceRkdReturnLine, *, contract: int = 1) -> dict:
    serials = row.serial_numbers
    if (
        not isinstance(serials, list)
        or not serials
        or any(not isinstance(sn, str) or not sn.strip() for sn in serials)
    ):
        raise HTTPException(
            status_code=503, detail="返件序列号记录不完整，请核对上游来源"
        )
    item = {
        "receiptId": row.rkd_line_id,
        "version": row.version,
        "lineStatus": row.line_status,
        "projectId": row.project_id,
        "pn": row.pn,
        "qty": str(row.qty),
        "serialNumbers": serials,
        "condition": row.test_result,
        "receiptKind": row.receipt_kind,
        "occurredAt": row.occurred_at.isoformat() if row.occurred_at else None,
        "changedAt": (row.updated_at or row.created_at).isoformat(),
    }
    if contract == 2:
        item["partId"] = row.part_id
        item["description"] = row.description
    return item


@router.get("/bad-receipts")
def list_bad_receipts(
    response: Response,
    _key: Annotated[None, Depends(_integration_key)],
    db: Annotated[Session, Depends(get_db)],
    after: str | None = Query(default=None, max_length=36),
    receipt_id: str | None = Query(default=None, max_length=36),
    receipt_ids: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    contract: int = Query(default=1, ge=1, le=2),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if sum(bool(option) for option in (after, receipt_id, receipt_ids)) > 1:
        raise HTTPException(status_code=422, detail="游标与精确查询不能同时使用")
    if receipt_ids and (len(receipt_ids) > 100 or len(receipt_ids) > limit):
        raise HTTPException(status_code=422, detail="精确核对一次不得超过 100 条返件")
    statement = select(MaintenanceRkdReturnLine).where(
        MaintenanceRkdReturnLine.source == "manual",
        func.jsonb_array_length(MaintenanceRkdReturnLine.serial_numbers) > 0,
        MaintenanceRkdReturnLine.test_result.in_(("坏品", "废品")),
    )
    if receipt_ids:
        ids = {_source_id(item) for item in receipt_ids}
        statement = statement.where(MaintenanceRkdReturnLine.rkd_line_id.in_(ids))
    elif receipt_id:
        statement = statement.where(
            MaintenanceRkdReturnLine.rkd_line_id == _source_id(receipt_id)
        )
    elif after:
        statement = statement.where(
            MaintenanceRkdReturnLine.rkd_line_id > _source_id(after)
        )
    rows = list(
        db.scalars(
            statement.order_by(MaintenanceRkdReturnLine.rkd_line_id).limit(limit + 1)
        )
    )
    has_more = not receipt_id and not receipt_ids and len(rows) > limit
    selected = rows[:limit]
    return {
        "schemaVersion": contract,
        "items": [_receipt_item(row, contract=contract) for row in selected],
        "nextCursor": selected[-1].rkd_line_id if has_more else None,
        "hasMore": has_more,
    }
