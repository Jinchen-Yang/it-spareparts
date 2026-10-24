"""Bounded, dedicated read-only PN and purchase/sale exports for the shop.

No upstream stock, customer, employee, supplier, profit or project fields can
leave this module. The Bearer credential is independent of receipt export.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from app.config import get_settings
from app.db import get_db
from app.models.dimensions import DimPart
from app.models.master_data import ProductSpec
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.schemas.resale_price_reference import ResalePriceReferenceResponse
from app.services.resale_pn_lookup import canonical_part as _canonical
from app.services.resale_pn_lookup import find_pn_candidates
from app.services.resale_price_reference import (
    PriceReferenceAmbiguous,
    PriceReferenceUnavailable,
    build_price_reference,
)

router = APIRouter(
    prefix="/integrations/resale/parts", tags=["resale-catalog-readonly"]
)
_SALES_TYPES = frozenset({"备件销售", "整机销售", "销售换货"})
_PURCHASE_TYPES = frozenset({"销售订单", "指定采购", "维保需求"})
_ORDER_STATES = frozenset({"已生效", "已取消", "草稿", "进行中"})
_DESCRIPTION_MAX_CHARS = 4000
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
# 游标 id 段只认 ASCII 十进制：int() 会接受 "+1"、"1_0"、全角／阿拉伯-印度数字等，须先挡掉。
_CURSOR_ID = re.compile(r"[0-9]+")
_PG_BIGINT_MAX = 2**63 - 1
_logger = logging.getLogger(__name__)


from app.authz.machine_access import ServiceAccess, require_service

ResolveKey = Annotated[ServiceAccess, Depends(require_service('resale_catalog', 'resale_catalog', 'resolve'))]
SnapshotKey = Annotated[ServiceAccess, Depends(require_service('resale_catalog', 'resale_catalog', 'snapshots'))]
ListKey = Annotated[ServiceAccess, Depends(require_service('resale_catalog', 'resale_catalog', 'list'))]
EvidenceKey = Annotated[ServiceAccess, Depends(require_service('resale_catalog', 'resale_catalog', 'price_evidence'))]
PriceReferenceKey = Annotated[ServiceAccess, Depends(require_service('resale_catalog', 'resale_price_reference', 'read'))]
DbSession = Annotated[Session, Depends(get_db)]


class ResolveItem(BaseModel):
    sourceKey: str = Field(min_length=1, max_length=160)
    pnRaw: str = Field(min_length=1, max_length=256)


class ResolveRequest(BaseModel):
    items: list[ResolveItem] = Field(min_length=1, max_length=100)


class SnapshotRequest(BaseModel):
    partIds: list[int] = Field(min_length=1, max_length=100)


class EvidenceRequest(BaseModel):
    partIds: list[int] = Field(min_length=1, max_length=50)
    side: Literal["sales", "purchases"]
    after: int | None = Field(default=None, ge=0)
    limit: int = Field(default=100, ge=1, le=200)


class ListRequest(BaseModel):
    # 只给 /list 加 forbid：整目录遍历是机器消费，多余字段大概率是消费端拼错键。
    model_config = ConfigDict(extra="forbid")
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=200, ge=1, le=500)


def _safe_specs(db: Session, part_ids: list[int]) -> dict[int, list[dict]]:
    specs: dict[int, list[dict]] = {part_id: [] for part_id in part_ids}
    if part_ids:
        rows = db.scalars(
            select(ProductSpec)
            .where(ProductSpec.part_id.in_(part_ids))
            .order_by(ProductSpec.part_id, ProductSpec.spec_key)
        )
        for row in rows:
            specs[row.part_id].append(
                {
                    "key": row.spec_key,
                    "value": row.spec_value,
                    "unit": row.spec_unit,
                    "source": row.source,
                }
            )
    return specs


def _sales_record_counts(
    db: Session, part_ids: list[int]
) -> tuple[dict[int, int] | None, str | None]:
    """D-36: sorting-only counts; never mistake an unavailable batch for zero."""
    if not part_ids:
        return {}, None
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        # A failed PostgreSQL statement must not poison the request transaction.
        with db.begin_nested():
            rows = db.execute(
                select(FSalesLine.part_id, func.count())
                .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
                .where(
                    FSalesLine.part_id.in_(part_ids),
                    FSalesOrder.data_status == "已生效",
                )
                .group_by(FSalesLine.part_id)
            ).all()
        return dict(rows), observed_at
    except SQLAlchemyError:
        # Do not log SQL/parameters or publish partially read counts.
        _logger.warning("Catalog snapshot sales counts unavailable")
        return None, None


@router.post("/resolve")
def resolve_parts(
    body: ResolveRequest, response: Response, _key: ResolveKey, db: DbSession
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    items = []
    cache: dict[str, dict] = {}
    for candidate in body.items:
        raw = candidate.pnRaw.strip()
        if raw not in cache:
            matches, pending = find_pn_candidates(db, raw)
            bound = list(matches.values())
            if pending or len(bound) > 1:
                result = {
                    "status": "ambiguous",
                    "candidates": [
                        {"partId": row.id, "pnStd": row.pn_std} for row, _ in bound
                    ],
                }
            elif not bound:
                result = {"status": "not_found", "candidates": []}
            else:
                original, kind = bound[0]
                part, merged = _canonical(db, original)
                if part is None:
                    result = {"status": "ambiguous", "candidates": []}
                else:
                    result = {
                        "status": "merged" if merged else kind,
                        "partId": part.id,
                        "pnStd": part.pn_std,
                        "redirectedFrom": original.id if merged else None,
                        "candidates": [],
                    }
            cache[raw] = result
        items.append({"sourceKey": candidate.sourceKey, **cache[raw]})
    return {"schemaVersion": 1, "items": items}


@router.post("/snapshots")
def part_snapshots(
    body: SnapshotRequest, response: Response, _key: SnapshotKey, db: DbSession
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    rows: list[tuple[int, DimPart | None, bool]] = []
    for requested_id in dict.fromkeys(body.partIds):
        if requested_id < 1:
            raise HTTPException(422, "partId 必须为正整数")
        part, merged = _canonical(db, db.get(DimPart, requested_id))
        rows.append((requested_id, part, merged))
    part_ids = sorted({row.id for _, row, _ in rows if row is not None})
    specs = _safe_specs(db, part_ids)
    sales_counts, sales_counts_as_of = _sales_record_counts(db, part_ids)
    items = []
    for requested_id, part, merged in rows:
        if part is None:
            items.append({
                "requestedPartId": requested_id,
                "status": "not_found",
                "salesRecordCount": None,
                "salesRecordCountAsOf": None,
            })
            continue
        items.append(
            {
                "requestedPartId": requested_id,
                "partId": part.id,
                "status": "merged" if merged else part.status,
                "pnStd": part.pn_std,
                "description": part.description,
                "brand": part.brand,
                "categoryMajor": part.category_major,
                "categoryMinor": part.category_minor,
                "unit": part.unit,
                "needsReview": part.needs_review,
                "isExcluded": part.is_excluded,
                "lockedFields": sorted(part.locked_fields or []),
                "updatedAt": part.updated_at.isoformat() if part.updated_at else None,
                "specs": specs.get(part.id, []),
                "salesRecordCount": sales_counts.get(part.id, 0)
                if sales_counts is not None else None,
                "salesRecordCountAsOf": sales_counts_as_of,
            }
        )
    return {"schemaVersion": 1, "items": items}


def _encode_list_cursor(updated_at: datetime, part_id: int) -> str:
    plain = (
        f"{updated_at.astimezone(timezone.utc).isoformat(timespec='microseconds')}"
        f"|{part_id}"
    )
    return base64.urlsafe_b64encode(plain.encode()).decode().rstrip("=")


def _decode_list_cursor(token: str) -> tuple[datetime, int]:
    try:
        if not _BASE64URL.fullmatch(token):
            raise ValueError
        padded = token + "=" * (-len(token) % 4)
        plain = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        ts_text, separator, id_text = plain.partition("|")
        if not separator or not ts_text or not _CURSOR_ID.fullmatch(id_text):
            raise ValueError
        updated_at = datetime.fromisoformat(ts_text)
        if updated_at.tzinfo is None:
            raise ValueError
        # 0001-01-01T00:00+01:00 / 9999-12-31T23:00-05:00 换算 UTC 越界抛 OverflowError。
        updated_at = updated_at.astimezone(timezone.utc)
        part_id = int(id_text)
        # 超出 PG bigint 的 id 会在驱动层报错成 500，按非法游标处理。
        if part_id < 1 or part_id > _PG_BIGINT_MAX:
            raise ValueError
    except (ValueError, OverflowError, binascii.Error, UnicodeDecodeError):
        raise HTTPException(422, "游标无效")
    return updated_at, part_id


@router.post("/list")
def list_parts(
    body: ListRequest, response: Response, _key: ListKey, db: DbSession
) -> dict:
    """D-36 PR1: full-catalog keyset listing — a live read, not a v2 snapshot."""
    response.headers["Cache-Control"] = "no-store"
    target = aliased(DimPart)
    statement = (
        select(DimPart, target.pn_std)
        .outerjoin(target, target.id == DimPart.merged_into_id)
        .order_by(DimPart.updated_at.asc(), DimPart.id.asc())
        .limit(body.limit + 1)
    )
    if body.cursor is not None:
        cursor_at, cursor_id = _decode_list_cursor(body.cursor)
        # 行值比较：同一 updated_at 的并列由 id 决胜，翻页不漏行。
        statement = statement.where(
            tuple_(DimPart.updated_at, DimPart.id) > tuple_(cursor_at, cursor_id)
        )
    fetched = db.execute(statement).all()
    has_more = len(fetched) > body.limit
    page = fetched[: body.limit]
    specs = _safe_specs(db, [part.id for part, _ in page])
    items = [
        {
            "partId": part.id,
            "pnStd": part.pn_std,
            "description": None
            if part.description is None
            else part.description[:_DESCRIPTION_MAX_CHARS],
            "brand": part.brand,
            "categoryMajor": part.category_major,
            "categoryMinor": part.category_minor,
            "unit": part.unit,
            "needsReview": part.needs_review,
            "isExcluded": part.is_excluded,
            "lockedFields": sorted(part.locked_fields or []),
            "updatedAt": part.updated_at.isoformat() if part.updated_at else None,
            "specs": specs.get(part.id, []),
            "status": part.status,
            "mergedIntoId": part.merged_into_id,
            # 一跳指针：A→B→C 时 A 指向 B（直接父行），不解析到最终 C。
            "mergedIntoPnStd": merged_pn_std,
            "machineOrPart": part.machine_or_part,
        }
        for part, merged_pn_std in page
    ]
    return {
        "schemaVersion": 1,
        "items": items,
        "hasMore": has_more,
        # 非空页总给末行游标（即使 hasMore=false）：商城追平末尾后保存续传水位，
        # 下次只拉新变更；仅空页为 null。hasMore 仍只表示是否还有后续行。
        "nextCursor": _encode_list_cursor(page[-1][0].updated_at, page[-1][0].id)
        if page
        else None,
    }


def _money(value: object) -> str | None:
    return str(value) if value is not None else None


@router.post("/price-evidence")
def price_evidence(
    body: EvidenceRequest, response: Response, _key: EvidenceKey, db: DbSession
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if any(part_id < 1 for part_id in body.partIds):
        raise HTTPException(422, "partId 必须为正整数")
    part_ids = list(dict.fromkeys(body.partIds))
    line_type, order_type = (
        (FSalesLine, FSalesOrder)
        if body.side == "sales"
        else (FPurchaseLine, FPurchaseOrder)
    )
    columns = [line_type.id, line_type.part_id, line_type.raw_line_id, line_type.unit_price, line_type.qty,
               order_type.raw_order_id, order_type.order_date, order_type.data_status]
    columns += ([order_type.tax_rate, order_type.business_type, line_type.counts_revenue]
                if body.side == 'sales' else [order_type.is_tax_inclusive, order_type.source_type])
    statement = (
        select(*columns)
        .join(order_type, line_type.order_id == order_type.id)
        .where(line_type.part_id.in_(part_ids))
    )
    if body.after is not None:
        statement = statement.where(line_type.id > body.after)
    fetched = db.execute(statement.order_by(line_type.id).limit(body.limit + 1)).all()
    has_more = len(fetched) > body.limit
    items = []
    for row in fetched[: body.limit]:
        common = {
            "partId": row.part_id,
            "sourceLineId": row.raw_line_id,
            "sourceOrderId": row.raw_order_id,
            "orderDate": row.order_date.isoformat() if row.order_date else None,
            "dataStatus": row.data_status
            if row.data_status in _ORDER_STATES
            else "unknown",
            "unitPrice": _money(row.unit_price),
            "qty": _money(row.qty),
            "itemKind": "unknown",
            "channel": "unknown",
        }
        if body.side == "sales":
            common.update(
                {
                    "side": "sale",
                    "unitPriceBasis": "inc_tax_assumed",
                    "taxRate": _money(row.tax_rate),
                    "businessType": row.business_type
                    if row.business_type in _SALES_TYPES
                    else "other",
                    "countsRevenue": bool(row.counts_revenue),
                }
            )
        else:
            common.update(
                {
                    "side": "purchase",
                    "isTaxInclusive": row.is_tax_inclusive,
                    "sourceType": row.source_type
                    if row.source_type in _PURCHASE_TYPES
                    else "other",
                }
            )
        items.append(common)
    return {
        "schemaVersion": 1,
        "items": items,
        "hasMore": has_more,
        "nextCursor": fetched[body.limit - 1].id if has_more else None,
    }


@router.get(
    "/price-reference",
    response_model=ResalePriceReferenceResponse,
    response_model_exclude_none=False,
)
def price_reference(
    pn: Annotated[str, Query(min_length=1, max_length=256)],
    response: Response,
    _key: PriceReferenceKey,
    db: DbSession,
) -> ResalePriceReferenceResponse:
    """商城详情页按 PN 点击查询销售参考均价；只读，不写任何业务状态。"""
    if not pn.strip():
        raise HTTPException(422, "PN 不能为空白")
    try:
        payload = build_price_reference(db, pn, get_settings())
    except PriceReferenceAmbiguous:
        raise HTTPException(
            409,
            "PN 身份无法唯一确定",
            headers={"Cache-Control": "no-store"},
        ) from None
    except PriceReferenceUnavailable:
        db.rollback()
        _logger.warning("Resale price reference unavailable")
        raise HTTPException(
            503,
            "参考价暂不可用",
            headers={"Cache-Control": "no-store"},
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return payload


from app.api.resale_catalog_search import register_search
register_search(router, require_service('resale_catalog', 'resale_catalog', 'search'))
