"""Gallery commands preserve origin, deletion intent and independent edit revisions."""

import base64
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import String, Text, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB

from .errors import CatalogError
from .media_common import (
    access,
    begin,
    finish,
    identity,
    load,
    now,
    read_identity,
    read_response,
    response,
    revision,
)
from .media_models import (
    Asset,
    Derivative,
    GalleryEntry,
    GalleryGroup,
    GalleryMember,
    GallerySuppression,
)


def group_for(db, part_id):
    member = db.get(GalleryMember, part_id, populate_existing=True)
    if member:
        return load(db, GalleryGroup, member.group_id, lock=True)
    group = GalleryGroup(
        id=uuid4(), canonical_part_id=part_id, revision=0, ordered_entry_ids=[]
    )
    db.add(group)
    db.flush()
    db.add(GalleryMember(part_id=part_id, group_id=group.id))
    db.flush()
    return group


PAGE_BYTES = 768 * 1024


def entry_selection(group, *, status="gallery", purpose=None, origins=None):
    """Select only identifiers/ranks. PostgreSQL filters and deduplicates before paging."""
    ordering = (
        func.jsonb_array_elements_text(cast(group.ordered_entry_ids, JSONB))
        .table_valued("entry_id", with_ordinality="position")
        .render_derived()
    )
    rank = (
        func.coalesce(ordering.c.position, len(group.ordered_entry_ids) + 1)
        if status == "gallery"
        else GalleryEntry.created_at
    )
    query = (
        select(
            GalleryEntry.id,
            GalleryEntry.origin_part_id,
            rank.label("rank"),
        )
        .join(GalleryMember, GalleryEntry.origin_part_id == GalleryMember.part_id)
        .where(GalleryMember.group_id == group.id, GalleryEntry.status == status)
    )
    if purpose:
        query = query.where(GalleryEntry.purpose == purpose)
    if origins is not None:
        query = query.where(GalleryEntry.origin_part_id.in_(origins))
    if status == "gallery":
        query = query.outerjoin(
            ordering, cast(GalleryEntry.id, String) == ordering.c.entry_id
        )
        query = query.where(
            ~select(GallerySuppression.id)
            .where(
                GallerySuppression.group_id == group.id,
                GallerySuppression.active.is_(True),
                GallerySuppression.content_hash == GalleryEntry.content_hash,
            )
            .exists()
        )
        query = query.add_columns(
            func.row_number()
            .over(
                partition_by=(GalleryEntry.content_hash, GalleryEntry.purpose),
                order_by=(rank, GalleryEntry.id),
            )
            .label("duplicate_rank")
        )
    selected = query.subquery()
    result = select(selected.c.id, selected.c.origin_part_id, selected.c.rank)
    if status == "gallery":
        result = result.where(selected.c.duplicate_rank == 1)
    return result.order_by(selected.c.rank, selected.c.id)


def visible_entries(db, group, *, limit=1001, purpose=None, entry_ids=None):
    selection = entry_selection(group, purpose=purpose).subquery()
    query = select(selection.c.id).order_by(selection.c.rank, selection.c.id)
    if entry_ids is not None:
        query = query.where(selection.c.id.in_(entry_ids))
    ids = list(db.scalars(query.limit(limit)))
    rows = {
        entry.id: entry
        for entry in db.execute(
            select(
                GalleryEntry.id,
                GalleryEntry.asset_id,
                GalleryEntry.generation,
                GalleryEntry.revision,
                GalleryEntry.purpose,
                GalleryEntry.source["filename"].as_string().label("filename"),
            ).where(GalleryEntry.id.in_(ids))
        )
    }
    return [rows[identifier] for identifier in ids]


def reconcile(db, group):
    ids = list(db.scalars(entry_selection(group)))
    group.ordered_entry_ids = [str(eid) for eid in ids]
    if group.primary_entry_id not in ids:
        group.primary_entry_id = ids[0] if ids else None
    group.revision += 1


def entry_data(db, entry, *, asset=None, derivatives=None):
    asset = load(db, Asset, entry.asset_id) if asset is None else asset
    derivatives = (
        list(db.scalars(select(Derivative).where(Derivative.asset_id == asset.id)))
        if derivatives is None
        else derivatives
    )
    return {
        "entry_id": str(entry.id),
        "asset_id": str(entry.asset_id),
        "origin_part_id": entry.origin_part_id,
        "origin_namespace": entry.origin_namespace,
        "origin_id": entry.origin_id,
        "purpose": entry.purpose,
        "status": entry.status,
        "revision": entry.revision,
        "generation": entry.generation,
        "content_hash": entry.content_hash,
        "width": asset.width,
        "height": asset.height,
        "size_bytes": asset.size_bytes,
        "content_type": asset.content_type,
        "source": entry.source,
        "derivatives": [
            {"derivative_id": str(d.id), "recipe_id": d.recipe_id, "status": d.status}
            for d in derivatives
        ],
        "deleted_at": entry.deleted_at.isoformat() if entry.deleted_at else None,
    }


def gallery_data(db, group, *, entries):
    asset_ids = {entry.asset_id for entry in entries}
    assets = (
        {
            asset.id: asset
            for asset in db.scalars(select(Asset).where(Asset.id.in_(asset_ids)))
        }
        if asset_ids
        else {}
    )
    derivatives = {}
    if asset_ids:
        for derivative in db.scalars(
            select(Derivative).where(Derivative.asset_id.in_(asset_ids))
        ):
            derivatives.setdefault(derivative.asset_id, []).append(derivative)
    return {
        "group_id": str(group.id),
        "part_id": group.canonical_part_id,
        "revision": group.revision,
        "primary_entry_id": str(group.primary_entry_id)
        if group.primary_entry_id
        else None,
        "ordered_entry_ids": [
            eid
            for eid in group.ordered_entry_ids
            if eid in {str(e.id) for e in entries}
        ],
        "entries": [
            entry_data(
                db,
                e,
                asset=assets[e.asset_id],
                derivatives=derivatives.get(e.asset_id, []),
            )
            for e in entries
        ],
    }


def get_gallery(
    db,
    part_id,
    *,
    context,
    authorize,
    cursor=None,
    limit=50,
    status="gallery",
    purpose=None,
):
    if (
        not 1 <= limit <= 200
        or status not in ("gallery", "candidate", "processing", "deleted")
        or purpose not in (None, "common", "defect")
    ):
        raise CatalogError("request.validation_failed", "图库筛选无效", status_code=422)
    access(db, context, authorize, "gallery.read", part_id)
    states = read_identity(db, [part_id])
    identity_revision = states[part_id].identity_revision if states[part_id] else 0
    member = db.get(GalleryMember, part_id)
    if member is None:
        result = read_response(
            context,
            {
                "part_id": part_id,
                "group_id": None,
                "revision": 0,
                "primary_entry_id": None,
                "ordered_entry_ids": [],
                "entries": [],
            },
        )
        result["meta"]["identity_revision"] = identity_revision
        return result
    group = load(db, GalleryGroup, member.group_id, lock=True, shared=True)
    offset = 0
    if cursor:
        try:
            if len(cursor) > 512:
                raise ValueError("cursor too long")
            bound = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            offset = bound["offset"]
            if (
                bound
                != {
                    "group": str(group.id),
                    "revision": group.revision,
                    "offset": offset,
                    "status": status,
                    "purpose": purpose,
                }
                or type(offset) is not int
                or offset < 0
            ):
                raise ValueError("cursor changed")
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            raise CatalogError(
                "pagination.cursor_expired", "图库已变化，请重新查询", status_code=410
            ) from error
    # Authorize origin IDs before selecting duplicates so an unreadable duplicate
    # cannot hide a readable source with the same hash and purpose.
    if status != "gallery":
        access(db, context, authorize, f"gallery.{status}.read", part_id)
    permitted_origins = []
    for origin in db.scalars(
        select(GalleryMember.part_id).where(GalleryMember.group_id == group.id)
    ):
        try:
            access(db, context, authorize, "gallery.read", origin)
            if status != "gallery":
                access(db, context, authorize, f"gallery.{status}.read", origin)
            permitted_origins.append(origin)
        except CatalogError as error:
            if error.code != "auth.forbidden":
                raise
    candidates = list(
        db.execute(
            entry_selection(
                group,
                status=status,
                purpose=purpose,
                origins=permitted_origins,
            )
            .offset(offset)
            .limit(limit + 1)
        )
    )
    sizes = dict(
        db.execute(
            select(
                GalleryEntry.id, func.octet_length(cast(GalleryEntry.source, Text))
            ).where(GalleryEntry.id.in_([item.id for item in candidates[:limit]]))
        ).all()
    )
    ids, estimated_bytes = [], 4096
    for item in candidates[:limit]:
        cost = sizes[item.id] + 4096  # includes bounded scalar/derivative metadata
        if estimated_bytes + cost > PAGE_BYTES:
            if not ids:
                raise CatalogError(
                    "media.metadata_too_large",
                    "图片来源资料超过单条读取上限",
                    status_code=413,
                )
            break
        ids.append(item.id)
        estimated_bytes += cost
    rows = {
        entry.id: entry
        for entry in db.scalars(
            select(GalleryEntry)
            .where(GalleryEntry.id.in_(ids))
            .execution_options(populate_existing=True)
        )
    }
    data = gallery_data(db, group, entries=[rows[identifier] for identifier in ids])
    # Account for actual wire bytes as well as the SQL size estimate.
    while (
        len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
        > PAGE_BYTES
    ):
        if len(data["entries"]) <= 1:
            raise CatalogError(
                "media.metadata_too_large",
                "图片来源资料超过单条读取上限",
                status_code=413,
            )
        data["entries"].pop()
    selected = {entry["entry_id"] for entry in data["entries"]}
    data["ordered_entry_ids"] = [
        eid for eid in data["ordered_entry_ids"] if eid in selected
    ]
    if data["primary_entry_id"] not in selected:
        data["primary_entry_id"] = None
    count = len(data["entries"])
    result = read_response(context, data)
    result["meta"]["identity_revision"] = identity_revision
    result["meta"]["next_cursor"] = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "group": str(group.id),
                    "revision": group.revision,
                    "offset": offset + count,
                    "status": status,
                    "purpose": purpose,
                },
                separators=(",", ":"),
            ).encode()
        ).decode()
        if count < len(candidates)
        else None
    )
    return result


def attach_asset(db, *, part_id, asset, purpose, context):
    """Caller holds identity and PN lock and has checked source plus target access."""
    group = group_for(db, part_id)
    matches = list(
        db.scalars(
            select(GalleryEntry)
            .where(
                GalleryEntry.origin_part_id == part_id,
                GalleryEntry.content_hash == asset.sha256,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    # Switching purpose cannot evade an existing deletion of this source image.
    if any(e.status == "deleted" for e in matches):
        raise CatalogError(
            "media.deleted", "该图片已删除，请通过明确恢复操作恢复原条目"
        )
    for existing in matches:
        if existing.purpose == purpose:
            return existing, group, False
    entry = GalleryEntry(
        id=uuid4(),
        asset_id=asset.id,
        origin_part_id=part_id,
        origin_namespace="pn_catalog",
        origin_id=str(part_id),
        content_hash=asset.sha256,
        purpose=purpose,
        status="gallery",
        revision=1,
        generation=1,
        source={
            "principal_id": context.actor.principal_id,
            "provenance": context.actor.provenance.model_dump(mode="json"),
        },
    )
    db.add(entry)
    db.flush()
    reconcile(db, group)
    return entry, group, True


def add_entry(db, part_id, command, *, context, authorize):
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="gallery.archive",
        part_id=part_id,
        resource="media.entry",
        identifier=part_id,
        command=command,
    )
    source = (
        load(db, GalleryEntry, command.source_entry_id)
        if command.source_entry_id
        else None
    )
    if source:
        check_reuse_source(
            db,
            source,
            command.asset_id,
            command.purpose,
            context=context,
            authorize=authorize,
        )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    states = identity(
        db,
        [part_id, source.origin_part_id if source else None],
        expected={part_id: command.expected_identity_revision},
    )
    if source:
        source = load(db, GalleryEntry, source.id, lock=True)
        if (
            source.asset_id != command.asset_id
            or source.status != "gallery"
            or source.purpose != command.purpose
        ):
            raise CatalogError(
                "auth.forbidden", "来源图片不可用于该用途", status_code=403
            )
        source_group = group_for(db, source.origin_part_id)
        if db.scalar(
            select(GallerySuppression.id).where(
                GallerySuppression.group_id == source_group.id,
                GallerySuppression.content_hash == source.content_hash,
                GallerySuppression.active.is_(True),
            )
        ):
            raise CatalogError("media.deleted", "来源图片已隐藏")
        asset = load(db, Asset, source.asset_id)
        ticket = None
    else:
        from .media_uploads import owned_upload

        ticket = owned_upload(db, command.upload_id, context, authorize, lock=True)
        if ticket.part_id is None:
            raise CatalogError(
                "inbox.assignment_required", "未定目标的图片必须通过收件箱认领和归档"
            )
        if ticket.part_id not in (None, part_id) or ticket.purpose != command.purpose:
            raise CatalogError(
                "catalog.identity_changed", "上传票据目标已固定，不能改投"
            )
        if (
            ticket.part_id is not None
            and ticket.identity_revision != command.expected_identity_revision
        ):
            raise CatalogError("catalog.identity_changed", "上传时的型号身份已变化")
        if ticket.status == "cancelled":
            raise CatalogError("media.deleted", "上传已取消")
        if ticket.status not in ("ready", "committed") or ticket.asset_id is None:
            raise CatalogError("request.in_progress", "图片尚未校验完成")
        asset = load(db, Asset, ticket.asset_id)
    entry, group, created = attach_asset(
        db, part_id=part_id, asset=asset, purpose=command.purpose, context=context
    )
    if source and created:
        entry.source = {
            **entry.source,
            "source_entry_id": str(source.id),
            "source_asset_id": str(asset.id),
        }
    if ticket:
        if ticket.entry_id is not None and ticket.entry_id != entry.id:
            raise CatalogError("media.upload_already_committed", "上传已归档到另一条目")
        if ticket.status != "committed":
            ticket.status, ticket.entry_id = "committed", entry.id
            ticket.revision += 1
    receipt = finish(
        db,
        handle,
        part_id=part_id,
        identifier=entry.id,
        before=0 if created else entry.revision,
        after=entry.revision,
        changes={"entry_id": str(entry.id), "created": created},
        identity_revision=states[part_id].identity_revision,
        result_fields={
            "source_entry_id": str(source.id),
            "source_asset_id": str(asset.id),
            "purpose": command.purpose,
        }
        if source
        else None,
    )
    return response(db, receipt, entry_data(db, entry), replayed=False)


def check_reuse_source(db, source, asset_id, purpose, *, context, authorize):
    access(db, context, authorize, "gallery.reuse", source.origin_part_id, write=True)
    access(db, context, authorize, "gallery.read", source.origin_part_id)
    if (
        source.asset_id != asset_id
        or source.status != "gallery"
        or source.purpose != purpose
    ):
        raise CatalogError("auth.forbidden", "来源图片不可用于该用途", status_code=403)
    member = (
        db.get(GalleryMember, source.origin_part_id) if source.origin_part_id else None
    )
    if member and db.scalar(
        select(GallerySuppression.id).where(
            GallerySuppression.group_id == member.group_id,
            GallerySuppression.content_hash == source.content_hash,
            GallerySuppression.active.is_(True),
        )
    ):
        raise CatalogError("media.deleted", "来源图片已隐藏")


def _entry_command(db, entry_id, command, *, context, authorize, operation):
    entry = load(db, GalleryEntry, entry_id, code="media.entry_not_found")
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation=operation,
        part_id=entry.origin_part_id,
        resource="media.entry",
        identifier=entry_id,
        command=command,
    )
    if handle.receipt:
        return handle, None, None
    if entry.origin_part_id is None:
        raise CatalogError("catalog.binding_conflict", "历史来源尚未映射，只允许读取")
    identity(db, [entry.origin_part_id])
    group = group_for(db, entry.origin_part_id)
    return handle, load(db, GalleryEntry, entry_id, lock=True), group


def change_entry(db, entry_id, command, *, context, authorize, operation):
    handle, entry, group = _entry_command(
        db, entry_id, command, context=context, authorize=authorize, operation=operation
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    expected = (
        command.expected_deleted_revision
        if operation == "gallery.restore"
        else command.expected_revision
    )
    revision(context, entry.revision, expected)
    before = entry.revision
    if operation == "gallery.delete":
        if entry.status == "deleted":
            raise CatalogError("media.deleted", "图片已删除")
        entry.status, entry.deleted_at = "deleted", now()
        entry.generation += 1
    elif operation == "gallery.restore":
        if entry.status != "deleted":
            raise CatalogError(
                "request.precondition_failed", "条目未处于删除状态", status_code=412
            )
        entry.status, entry.deleted_at = "gallery", None
        entry.generation += 1
    else:
        if entry.status == "deleted":
            raise CatalogError("media.deleted", "请使用明确恢复命令")
        if command.status == "gallery" and entry.status != "gallery":
            access(
                db,
                context,
                authorize,
                "gallery.adopt",
                entry.origin_part_id,
                write=True,
            )
        if command.purpose and command.purpose != entry.purpose:
            conflict = db.scalar(
                select(GalleryEntry.id).where(
                    GalleryEntry.origin_part_id == entry.origin_part_id,
                    GalleryEntry.content_hash == entry.content_hash,
                    GalleryEntry.purpose == command.purpose,
                )
            )
            if conflict:
                raise CatalogError(
                    "catalog.binding_conflict", "该用途已有同内容来源条目"
                )
            access(
                db,
                context,
                authorize,
                "gallery.change_purpose",
                entry.origin_part_id,
                write=True,
            )
            entry.purpose = command.purpose
        if command.status:
            entry.status = command.status
    entry.revision += 1
    db.flush()
    reconcile(db, group)
    receipt = finish(
        db,
        handle,
        part_id=entry.origin_part_id,
        identifier=entry.id,
        before=before,
        after=entry.revision,
        changes={
            "status": entry.status,
            "purpose": entry.purpose,
            "generation": entry.generation,
            "reason": getattr(command, "reason", None),
        },
    )
    return response(db, receipt, entry_data(db, entry), replayed=False)


def arrange_gallery(db, part_id, command, *, context, authorize):
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="gallery.arrange",
        part_id=part_id,
        resource="media.gallery",
        identifier=part_id,
        command=command,
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    identity(db, [part_id])
    group = group_for(db, part_id)
    revision(context, group.revision, command.expected_gallery_revision)
    entries = list(db.execute(entry_selection(group)))
    for origin in {entry.origin_part_id for entry in entries}:
        access(db, context, authorize, "gallery.read", origin)
    current = {entry.id for entry in entries}
    ordered = command.ordered_entry_ids
    if (
        len(ordered) != len(set(ordered))
        or set(ordered) != current
        or (
            command.primary_entry_id not in current
            if current
            else command.primary_entry_id is not None
        )
    ):
        raise CatalogError(
            "request.validation_failed",
            "排序必须完整包含当前成员且主图属于该成员集",
            status_code=422,
        )
    before = group.revision
    group.ordered_entry_ids, group.primary_entry_id = (
        [str(v) for v in ordered],
        command.primary_entry_id,
    )
    group.revision += 1
    receipt = finish(
        db,
        handle,
        part_id=part_id,
        identifier=group.id,
        before=before,
        after=group.revision,
        changes={
            "ordered_entry_ids": group.ordered_entry_ids,
            "primary_entry_id": str(group.primary_entry_id)
            if group.primary_entry_id
            else None,
        },
    )
    page = get_gallery(db, part_id, context=context, authorize=authorize)
    result = response(db, receipt, page["data"], replayed=False)
    result["meta"]["next_cursor"] = page["meta"]["next_cursor"]
    return result


def suppression_scope(db, group_id, *, context, authorize, operation=None):
    """A group-wide decision must include every current origin, not only its root."""
    # Freeze topology before collecting members; always lock PN before media rows.
    read_identity(db, [])
    group = load(db, GalleryGroup, group_id)
    members = list(db.scalars(select(GalleryMember.part_id).where(
        GalleryMember.group_id == group_id).order_by(GalleryMember.part_id)))
    if not members or group.canonical_part_id not in members:
        raise CatalogError("catalog.identity_changed", "图库归属已变化，请重新打开当前 PN 图库")
    read_identity(db, members)
    decisions = []
    for part_id in members:
        decision = access(db, context, authorize, "gallery.read", part_id)
        decisions.append([part_id, sorted(decision.readable_fields), decision.metadata])
        if operation:
            access(db, context, authorize, operation, part_id, write=True)
    group = load(db, GalleryGroup, group_id, lock=True, shared=operation is None)
    return group, decisions


def list_suppressions(db, group_id, *, context, authorize, cursor=None, limit=20):
    """Active suppression metadata only, bounded keyset pages under current grants."""
    from .api import _decode_cursor, _encode_cursor

    if type(limit) is not int or not 1 <= limit <= 50:
        raise CatalogError("request.validation_failed", "分页数量无效", status_code=422)
    group, decisions = suppression_scope(db, group_id, context=context, authorize=authorize)
    fingerprint = hashlib.sha256(json.dumps([
        "suppressions", str(group.id), group.revision, context.actor.principal_id,
        context.actor.client_id, decisions,
    ], sort_keys=True, default=str).encode()).hexdigest()
    after = _decode_cursor(cursor, fingerprint)
    if after >= 2 ** 128:
        raise CatalogError("request.validation_failed", "分页游标无效", status_code=422)
    statement = select(GallerySuppression).where(
        GallerySuppression.group_id == group.id, GallerySuppression.active.is_(True))
    if after:
        statement = statement.where(GallerySuppression.id > UUID(int=after))
    rows = list(db.scalars(statement.order_by(GallerySuppression.id).limit(limit + 1)))
    result = {"data": [suppression_data(item) for item in rows[:limit]],
              "meta": {"request_id": str(context.request_id), "replayed": False}}
    result["meta"]["next_cursor"] = _encode_cursor(rows[limit - 1].id.int, fingerprint) if len(rows) > limit else None
    return result


def suppress_hash(db, group_id, command, *, context, authorize):
    group = load(db, GalleryGroup, group_id)
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="gallery.suppress",
        part_id=group.canonical_part_id,
        resource="media.suppression",
        identifier=group_id,
        command=command,
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    group, _ = suppression_scope(db, group_id, context=context, authorize=authorize, operation=handle.operation)
    revision(context, group.revision, command.expected_gallery_revision)
    item = db.scalar(
        select(GallerySuppression).where(
            GallerySuppression.group_id == group.id,
            GallerySuppression.content_hash == command.content_hash,
        )
    )
    before = item.revision if item else 0
    if item is None:
        item = GallerySuppression(
            id=uuid4(),
            group_id=group.id,
            content_hash=command.content_hash,
            reason=command.reason,
            active=True,
            revision=1,
        )
        db.add(item)
    else:
        item.active, item.reason = True, command.reason
        item.revision += 1
    db.flush()
    reconcile(db, group)
    receipt = finish(
        db,
        handle,
        part_id=group.canonical_part_id,
        identifier=item.id,
        before=before,
        after=item.revision,
        changes={
            "content_hash": item.content_hash,
            "active": True,
            "reason": item.reason,
        },
    )
    return response(db, receipt, suppression_data(item), replayed=False)


def suppression_data(item):
    return {
        "suppression_id": str(item.id),
        "group_id": str(item.group_id),
        "content_hash": item.content_hash,
        "active": item.active,
        "revision": item.revision,
        "reason": item.reason,
    }


def unsuppress_hash(db, group_id, suppression_id, command, *, context, authorize):
    group = load(db, GalleryGroup, group_id)
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="gallery.unsuppress",
        part_id=group.canonical_part_id,
        resource="media.suppression",
        identifier=suppression_id,
        command=command,
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    group, _ = suppression_scope(db, group_id, context=context, authorize=authorize, operation=handle.operation)
    item = load(db, GallerySuppression, suppression_id, lock=True)
    if item.group_id != group.id:
        raise CatalogError("request.not_found", "隐藏记录不存在", status_code=404)
    revision(context, item.revision, command.expected_revision)
    before = item.revision
    item.active = False
    item.revision += 1
    db.flush()
    reconcile(db, group)
    receipt = finish(
        db,
        handle,
        part_id=group.canonical_part_id,
        identifier=item.id,
        before=before,
        after=item.revision,
        changes={"active": False, "content_hash": item.content_hash},
    )
    return response(db, receipt, suppression_data(item), replayed=False)
