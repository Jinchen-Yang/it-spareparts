"""Batch read projection used by worklists and future external consumers."""

from typing import Annotated, Literal

from fastapi import Request
from pydantic import Field

from .bulk_read import get_snapshots
from .dto import StrictDTO
from .http import AccessDep, DbDep, _context, router
from .wire import PageMeta, PartSnapshot, WireModel
from .wire_attributes import AttributesSnapshot
from .wire_content import ContentSnapshot


class ReadSnapshots(StrictDTO):
    part_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=50)
    cursor: str | None = Field(default=None, max_length=2048)


class AggregateSnapshot(WireModel):
    part_id: int = Field(gt=0)
    state: Literal["found", "not_found"]
    profile: PartSnapshot | None
    content_good: ContentSnapshot | None
    content_bad: ContentSnapshot | None
    attributes: AttributesSnapshot | None
    unavailable_resources: list[Literal["content.good", "content.bad", "attributes"]]
    readable_fields: list[str]


class SnapshotListResponse(WireModel):
    data: list[AggregateSnapshot]
    meta: PageMeta


@router.post(
    "/parts/snapshots",
    response_model=SnapshotListResponse,
    response_model_exclude_unset=True,
    openapi_extra={"x-read-only": True},
)
def snapshots(command: ReadSnapshots, request: Request, db: DbDep, access: AccessDep):
    return get_snapshots(
        db,
        command.part_ids,
        cursor=command.cursor,
        context=_context(request, access),
        authorize=access.authorize,
    )
