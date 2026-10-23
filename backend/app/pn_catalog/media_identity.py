"""Internal identity-coordinator hooks; caller holds topology and all affected PN locks."""

from uuid import uuid4

from sqlalchemy import select

from app.models.dimensions import DimPart

from .errors import CatalogError
from .media_gallery import group_for, reconcile
from .media_models import GalleryGroup, GalleryMember, GallerySuppression


def ensure_identity_gallery_members(db, part_ids):
    """Bootstrap absent members from pre-mutation topology; never repair conflicts.

    The coordinator must hold the topology lock and every PN lock in part_ids.
    Include canonical parents and compressed children in that authorized scope.
    """
    ids = set(part_ids)
    parts = {
        part.id: part for part in db.scalars(select(DimPart).where(DimPart.id.in_(ids)))
    }
    if set(parts) != ids:
        raise CatalogError("catalog.merge_conflict", "图库初始化的 PN 范围已变化")
    roots = {}
    for pid in sorted(ids):
        current, seen = pid, set()
        while parts[current].merged_into_id is not None:
            if current in seen or parts[current].merged_into_id not in parts:
                raise CatalogError(
                    "catalog.merge_conflict", "图库初始化缺少已锁定的归属 PN"
                )
            seen.add(current)
            current = parts[current].merged_into_id
        roots[pid] = current
    groups = {}
    for root in sorted(set(roots.values())):
        group = db.scalar(
            select(GalleryGroup)
            .where(GalleryGroup.canonical_part_id == root)
            .with_for_update()
        )
        if group is None:
            group = GalleryGroup(
                id=uuid4(), canonical_part_id=root, revision=0, ordered_entry_ids=[]
            )
            db.add(group)
            db.flush()
        groups[root] = group
    changed = set()
    for pid, root in sorted(roots.items()):
        member = db.get(GalleryMember, pid, populate_existing=True)
        if member is not None and member.group_id != groups[root].id:
            raise CatalogError("catalog.merge_conflict", "图库分组与现有 PN 归属不一致")
        if member is None:
            db.add(GalleryMember(part_id=pid, group_id=groups[root].id))
            changed.add(root)
    db.flush()
    for root in sorted(changed):
        reconcile(db, groups[root])
    return {pid: str(groups[root].id) for pid, root in roots.items()}


def _copy_suppressions(db, source, target):
    rows = list(
        db.scalars(
            select(GallerySuppression).where(
                GallerySuppression.group_id == source.id,
                GallerySuppression.active.is_(True),
            )
        )
    )
    for row in rows:
        existing = db.scalar(
            select(GallerySuppression)
            .where(
                GallerySuppression.group_id == target.id,
                GallerySuppression.content_hash == row.content_hash,
            )
            .with_for_update()
        )
        if existing is None:
            db.add(
                GallerySuppression(
                    id=uuid4(),
                    group_id=target.id,
                    content_hash=row.content_hash,
                    reason=row.reason,
                    active=True,
                    revision=1,
                )
            )
        elif not existing.active:
            existing.active = True
            existing.reason = row.reason
            existing.revision += 1
    db.flush()


def merge_gallery_groups(db, source_part_id, target_part_id):
    """Join display membership only; never move original entries, objects or inventory."""
    source, target = group_for(db, source_part_id), group_for(db, target_part_id)
    if source.id == target.id:
        return {"changed": False, "group_id": str(target.id)}
    members = list(
        db.scalars(
            select(GalleryMember)
            .where(GalleryMember.group_id == source.id)
            .with_for_update()
        )
    )
    before = {str(source.id): source.revision, str(target.id): target.revision}
    target.ordered_entry_ids = list(
        dict.fromkeys([*target.ordered_entry_ids, *source.ordered_entry_ids])
    )
    _copy_suppressions(db, source, target)
    for member in members:
        member.group_id = target.id
    db.flush()
    # Preserve the retired group's historical arrangement/suppressions for split history.
    source.revision += 1
    reconcile(db, target)
    return {
        "changed": True,
        "source_group_id": str(source.id),
        "target_group_id": str(target.id),
        "moved_part_ids": sorted(member.part_id for member in members),
        "before": before,
        "after": {str(source.id): source.revision, str(target.id): target.revision},
    }


def unmerge_gallery_groups(db, source_part_id, target_part_id, source_member_ids):
    """Split exactly the restored origin set; active hash hides survive on both sides."""
    moved = set(source_member_ids)
    if source_part_id not in moved or target_part_id in moved:
        raise CatalogError("catalog.merge_conflict", "撤销合并的来源成员集无效")
    target = group_for(db, target_part_id)
    source = db.scalar(
        select(GalleryGroup)
        .where(GalleryGroup.canonical_part_id == source_part_id)
        .with_for_update()
    )
    if source is None:
        source = GalleryGroup(
            id=uuid4(),
            canonical_part_id=source_part_id,
            revision=0,
            ordered_entry_ids=[],
        )
        db.add(source)
        db.flush()
    if source.id == target.id:
        raise CatalogError("catalog.merge_conflict", "撤销目标图组无效")
    members = list(
        db.scalars(
            select(GalleryMember)
            .where(GalleryMember.part_id.in_(moved))
            .with_for_update()
        )
    )
    if {member.part_id for member in members} != moved or any(
        member.group_id != target.id for member in members
    ):
        raise CatalogError("catalog.merge_conflict", "来源图库成员已发生变化")
    before = {str(source.id): source.revision, str(target.id): target.revision}
    source.ordered_entry_ids = list(
        dict.fromkeys([*target.ordered_entry_ids, *source.ordered_entry_ids])
    )
    _copy_suppressions(db, target, source)
    _copy_suppressions(db, source, target)
    for member in members:
        member.group_id = source.id
    db.flush()
    reconcile(db, source)
    reconcile(db, target)
    return {
        "changed": True,
        "source_group_id": str(source.id),
        "target_group_id": str(target.id),
        "moved_part_ids": sorted(moved),
        "before": before,
        "after": {str(source.id): source.revision, str(target.id): target.revision},
    }
