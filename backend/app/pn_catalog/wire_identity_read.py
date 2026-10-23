"""Minimal identity selectors; no historical fact rows or profile snapshots."""
from datetime import datetime

from pydantic import Field

from .wire import PageMeta, WireModel


class MergeLogSummary(WireModel):
    merge_log_id: int = Field(gt=0)
    source_part_id: int = Field(gt=0)
    target_part_id: int = Field(gt=0)
    source_pn: str
    target_pn: str
    relationship_active: bool
    created_at: datetime


class MergeLogListResponse(WireModel):
    data: list[MergeLogSummary]
    meta: PageMeta


class AliasSummary(WireModel):
    alias_id: int = Field(gt=0)
    pn_raw: str
    part_id: int | None
    pn_std: str
    status: str | None
    created_at: datetime


class AliasListResponse(WireModel):
    data: list[AliasSummary]
    meta: PageMeta
