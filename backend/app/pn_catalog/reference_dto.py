"""Exact reference input and bounded public projections."""
from typing import Literal
from uuid import UUID

from .dto import ExternalReferenceInput  # noqa: F401
from .wire import PageMeta, WireModel


class ReferenceSnapshot(WireModel):
    namespace: str
    source_id: UUID
    state: Literal["bound", "unresolved"]
    part_id: int | None = None
    canonical_part_id: int | None = None
    binding_version: int | None = None
    binding_identity_revision: int | None = None
    identity_revision: int | None = None


class ReferenceResponse(WireModel):
    data: ReferenceSnapshot
    meta: PageMeta


class ReferenceListResponse(WireModel):
    data: list[ReferenceSnapshot]
    meta: PageMeta
