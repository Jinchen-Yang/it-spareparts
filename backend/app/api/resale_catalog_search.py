"""Add master-identity search to the existing scoped resale export router.

Deploy as app/api/resale_catalog_search.py, together with the shared matcher as
app/services/resale_catalog_matching.py. Register with the existing catalog
credential dependency; ordinary session/receipt credentials cannot access it.
"""

from __future__ import annotations

from typing import Annotated

from app.db import get_db
from app.models.dimensions import DimPart, PartAlias
from app.services.resale_catalog_matching import (
    candidate_match,
    like_pattern,
    ranked,
    search_condition,
    search_order,
    search_terms,
)
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, or_, select
from sqlalchemy.orm import Session


class SearchRequest(BaseModel):
    q: str = Field(min_length=2, max_length=160)
    limit: int = Field(default=12, ge=1, le=30)


def register_search(router: APIRouter, catalog_key: object) -> None:
    @router.post("/search", dependencies=[Depends(catalog_key)])
    def search(
        body: SearchRequest, response: Response, db: Annotated[Session, Depends(get_db)]
    ) -> dict:
        response.headers["Cache-Control"] = "no-store"
        q = body.q.strip()
        if not any(c not in "*? \t\r\n" for c in q):
            return {"schemaVersion": 1, "items": []}
        columns = [DimPart.pn_std, DimPart.brand, DimPart.description]
        escaped = "".join("\\" + c if c in "%_\\" else c for c in q)
        # Each term may match a different field or an approved PN alias.

        conditions = and_(
            *(
                or_(
                    search_condition(columns, term),
                    DimPart.id.in_(
                        select(PartAlias.part_id).where(
                            PartAlias.status == "active",
                            PartAlias.pn_raw.ilike(like_pattern(term), escape="\\"),
                        )
                    ),
                )
                for term in search_terms(q)
            )
        )
        parts = list(
            db.scalars(
                select(DimPart)
                .where(
                    DimPart.status == "active",
                    conditions,
                )
                .order_by(
                    case(
                        (DimPart.pn_std.ilike(escaped, escape="\\"), 0),
                        (
                            DimPart.id.in_(
                                select(PartAlias.part_id).where(
                                    PartAlias.status == "active",
                                    PartAlias.pn_raw.ilike(escaped, escape="\\"),
                                )
                            ),
                            1,
                        ),
                        else_=2,
                    ),
                    search_order(columns, q),
                    DimPart.pn_std,
                )
                .limit(120)
            )
        )
        aliases: dict[int, list[str]] = {}
        if parts:
            for row in db.scalars(
                select(PartAlias).where(
                    PartAlias.part_id.in_([part.id for part in parts]),
                    PartAlias.status == "active",
                )
            ):
                aliases.setdefault(row.part_id, []).append(row.pn_raw)
        items = []
        for part in parts:
            fields = {"pn": part.pn_std, "brand": part.brand, "description": part.description}
            fields.update(
                {f"alias{index}": alias for index, alias in enumerate(aliases.get(part.id, []))}
            )
            match = candidate_match(q, fields)
            if match:
                if match["matchField"].startswith("alias"):
                    match["matchField"] = "alias"
                    if match["matchKind"] == "exact":
                        match["score"] = 98
                items.append(
                    {
                        "partId": part.id,
                        "pnStd": part.pn_std,
                        "description": part.description or "",
                        "brand": part.brand or "",
                        "categoryMajor": part.category_major,
                        "categoryMinor": part.category_minor,
                        **match,
                    }
                )
        return {"schemaVersion": 1, "items": ranked(items, body.limit)}
