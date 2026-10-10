"""Bounded, dedicated read-only PN and purchase/sale exports for the shop.

No upstream stock, customer, employee, supplier, profit or project fields can
leave this module. The Bearer credential is independent of receipt export.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.etl.cleaner import standardize_pn
from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductSpec
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder

router = APIRouter(
    prefix="/integrations/resale/parts", tags=["resale-catalog-readonly"]
)
_MAX_MERGE_HOPS = 10
_SALES_TYPES = frozenset({"备件销售", "整机销售", "销售换货"})
_PURCHASE_TYPES = frozenset({"销售订单", "指定采购", "维保需求"})
_ORDER_STATES = frozenset({"已生效", "已取消", "草稿", "进行中"})


def _catalog_key(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().resale_catalog_token_sha256.get_secret_value()
    if not expected:
        raise HTTPException(404, "PN 只读导出尚未启用")
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not 32 <= len(token) <= 256:
        raise HTTPException(401, "PN 只读凭据无效")
    if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected):
        raise HTTPException(401, "PN 只读凭据无效")


CatalogKey = Annotated[None, Depends(_catalog_key)]
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


def _canonical(db: Session, requested: DimPart | None) -> tuple[DimPart | None, bool]:
    part = requested
    seen: set[int] = set()
    merged = False
    while part is not None and part.status == "merged":
        if (
            part.id in seen
            or len(seen) >= _MAX_MERGE_HOPS
            or part.merged_into_id is None
        ):
            return None, True
        seen.add(part.id)
        part = db.get(DimPart, part.merged_into_id)
        merged = True
    return part, merged


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


@router.post("/resolve")
def resolve_parts(
    body: ResolveRequest, response: Response, _key: CatalogKey, db: DbSession
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    items = []
    cache: dict[str, dict] = {}
    for candidate in body.items:
        raw = candidate.pnRaw.strip()
        if raw not in cache:
            pn_std, _, _ = standardize_pn(raw)
            matches: dict[int, tuple[DimPart, str]] = {}
            if pn_std:
                exact = db.scalar(select(DimPart).where(DimPart.pn_std == pn_std))
                if exact is not None:
                    matches[exact.id] = (exact, "exact")
            aliases = db.scalars(
                select(PartAlias).where(
                    func.upper(PartAlias.pn_raw) == raw.upper(),
                    PartAlias.status.in_(("active", "pending")),
                )
            )
            pending = False
            for alias in aliases:
                if alias.status == "pending":
                    pending = True
                    continue
                part = db.get(DimPart, alias.part_id) if alias.part_id else None
                if part is not None:
                    matches.setdefault(part.id, (part, "alias"))
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
    body: SnapshotRequest, response: Response, _key: CatalogKey, db: DbSession
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    rows: list[tuple[int, DimPart | None, bool]] = []
    for requested_id in dict.fromkeys(body.partIds):
        if requested_id < 1:
            raise HTTPException(422, "partId 必须为正整数")
        part, merged = _canonical(db, db.get(DimPart, requested_id))
        rows.append((requested_id, part, merged))
    specs = _safe_specs(db, list({row.id for _, row, _ in rows if row is not None}))
    items = []
    for requested_id, part, merged in rows:
        if part is None:
            items.append({"requestedPartId": requested_id, "status": "not_found"})
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
            }
        )
    return {"schemaVersion": 1, "items": items}


def _money(value: object) -> str | None:
    return str(value) if value is not None else None


@router.post("/price-evidence")
def price_evidence(
    body: EvidenceRequest, response: Response, _key: CatalogKey, db: DbSession
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
    statement = (
        select(line_type, order_type)
        .join(order_type, line_type.order_id == order_type.id)
        .where(line_type.part_id.in_(part_ids))
    )
    if body.after is not None:
        statement = statement.where(line_type.id > body.after)
    fetched = db.execute(statement.order_by(line_type.id).limit(body.limit + 1)).all()
    has_more = len(fetched) > body.limit
    items = []
    for line, order in fetched[: body.limit]:
        common = {
            "partId": line.part_id,
            "sourceLineId": line.raw_line_id,
            "sourceOrderId": order.raw_order_id,
            "orderDate": order.order_date.isoformat() if order.order_date else None,
            "dataStatus": order.data_status
            if order.data_status in _ORDER_STATES
            else "unknown",
            "unitPrice": _money(line.unit_price),
            "qty": _money(line.qty),
            "itemKind": "unknown",
            "channel": "unknown",
        }
        if body.side == "sales":
            common.update(
                {
                    "side": "sale",
                    "unitPriceBasis": "inc_tax_assumed",
                    "taxRate": _money(order.tax_rate),
                    "businessType": order.business_type
                    if order.business_type in _SALES_TYPES
                    else "other",
                    "countsRevenue": bool(line.counts_revenue),
                }
            )
        else:
            common.update(
                {
                    "side": "purchase",
                    "isTaxInclusive": order.is_tax_inclusive,
                    "sourceType": order.source_type
                    if order.source_type in _PURCHASE_TYPES
                    else "other",
                }
            )
        items.append(common)
    return {
        "schemaVersion": 1,
        "items": items,
        "hasMore": has_more,
        "nextCursor": fetched[body.limit - 1][0].id if has_more else None,
    }


from app.api.resale_catalog_search import register_search
register_search(router, _catalog_key)
