"""Typed public content and proposal projections, generated into both clients."""
from typing import Any, Literal

from pydantic import Field

from .wire import PageMeta, ResponseMeta, WireModel


class ContentSnapshot(WireModel):
    part_id: int = Field(gt=0)
    grade: Literal["good", "bad"]
    identity_revision: int = Field(ge=0)
    revision: int = Field(ge=0)
    title: str | None = None
    description: str | None = None
    status: Literal["pending", "ai_filled", "confirmed", "needs_review"] | None = None
    field_versions: dict[str, int]
    protected_fields: list[str]
    field_provenance: dict[str, dict[str, Any]]


class ContentResponse(WireModel):
    data: ContentSnapshot
    meta: ResponseMeta


class CandidateSnapshot(WireModel):
    id: str
    part_id: int = Field(gt=0)
    resource: Literal["profile", "content"]
    grade: Literal["good", "bad"] | None
    identity_revision: int = Field(ge=0)
    base_revision: int = Field(ge=0)
    revision: int = Field(ge=1)
    status: Literal["pending", "partially_adopted", "adopted", "rejected"]
    proposal: dict[str, Any]
    provenance: dict[str, Any]
    created_by: str
    adopted_fields: list[str]
    decisions: list[dict[str, Any]]
    created_at: str


class CandidateResponse(WireModel):
    data: CandidateSnapshot
    meta: ResponseMeta


class CandidateListResponse(WireModel):
    data: list[CandidateSnapshot]
    meta: PageMeta
