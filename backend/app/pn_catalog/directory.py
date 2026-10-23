"""SQL-only catalog directory projection; no local Shop copy grants access."""

from sqlalchemy import String, case, cast, func, select
from sqlalchemy.orm import aliased

from app.models.dimensions import DimPart
from .media_models import GalleryEntry, GalleryGroup, GalleryMember, GallerySuppression


def gallery_expressions():
    consumer = aliased(GalleryMember)
    origin = aliased(GalleryMember)
    group = aliased(GalleryGroup)
    entry = aliased(GalleryEntry)
    suppression = aliased(GallerySuppression)
    selection = (
        select(entry.id)
        .select_from(consumer)
        .join(group, group.id == consumer.group_id)
        .join(origin, origin.group_id == group.id)
        .join(entry, entry.origin_part_id == origin.part_id)
        .where(
            consumer.part_id == DimPart.id,
            entry.status == "gallery",
            ~select(suppression.id)
            .where(
                suppression.group_id == group.id,
                suppression.active.is_(True),
                suppression.content_hash == entry.content_hash,
            )
            .exists(),
        )
        .correlate(DimPart)
    )
    ordering = (
        func.jsonb_array_elements_text(group.ordered_entry_ids)
        .table_valued("entry_id", with_ordinality="position")
        .render_derived()
        .lateral()
    )
    main = (
        selection.outerjoin(ordering, cast(entry.id, String) == ordering.c.entry_id)
        .where(entry.purpose == "common")
        .order_by(
            case((entry.id == group.primary_entry_id, 0), else_=1),
            ordering.c.position.asc().nulls_last(),
            entry.created_at,
            entry.id,
        )
        .limit(1)
        .scalar_subquery()
    )
    return selection.exists(), main
