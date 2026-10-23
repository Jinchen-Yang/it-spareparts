"""Durable inbox discovery, expiring claims and atomic assignment to the gallery."""

from datetime import timedelta
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, tuple_

from .errors import CatalogError
from .media_common import (
    access,
    begin,
    finish,
    identity,
    key_lock,
    load,
    now,
    owner,
    read_response,
    response,
    revision,
)
from .media_gallery import attach_asset
from .media_models import Asset, Derivative, InboxGroup, InboxItem, UploadTicket
from .job_models import CatalogJob
from .enrichment_dto import RecognitionResult


def _inbox_metadata(db, items):
    """Bounded joins, never per-item lazy loading or raw job/source payload projection."""
    if not items:
        return {}
    rows = db.execute(select(InboxItem.id, UploadTicket, Asset)
        .join(UploadTicket, InboxItem.upload_id == UploadTicket.id)
        .outerjoin(Asset, InboxItem.asset_id == Asset.id)
        .where(InboxItem.id.in_([item.id for item in items]))).all()
    values = {identifier: {
        "filename": ticket.filename,
        "size_bytes": asset.size_bytes if asset else ticket.declared_bytes,
        "content_type": asset.content_type if asset else ticket.content_type,
        "uploaded_by": {"principal_id": ticket.owner_principal_id, "client_id": ticket.client_id},
        "created_at": ticket.created_at.isoformat(),
        # Existing rows do not have a persisted inbox-update timestamp. Unknown is explicit.
        "updated_at": None,
        "purpose": ticket.purpose,
        "source": {"kind": "legacy" if asset and asset.verification_status == "legacy_metadata" else "upload",
                   "client_id": ticket.client_id, "namespace": asset.source_namespace if asset else None,
                   "source_id": asset.source_id if asset else None},
        "width": asset.width if asset else None, "height": asset.height if asset else None,
        "recognition": None,
    } for identifier, ticket, asset in rows}
    fences = [(str(item.id), item.generation) for item in items if item.status in ("ready", "assigned")]
    if not fences:
        return values
    query = select(CatalogJob.resource_id, CatalogJob.status, CatalogJob.result, CatalogJob.finished_at).where(CatalogJob.kind == "enrichment.recognize",
        CatalogJob.resource_type == "enrichment.recognize",
        tuple_(CatalogJob.resource_id, CatalogJob.resource_generation).in_(fences))
    latest = {row.resource_id: row for row in db.execute(query.distinct(CatalogJob.resource_id)
        .order_by(CatalogJob.resource_id, CatalogJob.created_at.desc(), CatalogJob.id.desc()))}
    successful = {row.resource_id: row for row in db.execute(query.where(CatalogJob.status == "succeeded")
        .distinct(CatalogJob.resource_id).order_by(CatalogJob.resource_id, CatalogJob.created_at.desc(), CatalogJob.id.desc()))}
    for item in items:
        job = latest.get(str(item.id))
        if job is None or item.id not in values:
            continue
        success = successful.get(str(item.id))
        result = None
        if success is not None:
            try:
                result = RecognitionResult.model_validate(success.result).model_dump(mode="json")
            except ValidationError:
                pass  # Historical or corrupt results must not expose arbitrary provider/job JSON.
        values[item.id]["recognition"] = {"status": job.status, "result": result,
            "completed_at": success.finished_at.isoformat() if success and success.finished_at else None}
    return values


def inbox_data(db, item, *, context, metadata=None):
    metadata = _inbox_metadata(db, [item]) if metadata is None else metadata
    return {
        **metadata[item.id],
        "inbox_id": str(item.id),
        "upload_id": str(item.upload_id),
        "asset_id": str(item.asset_id) if item.asset_id else None,
        "group_id": str(item.group_id) if item.group_id else None,
        "position": item.position,
        "client_order": item.client_order,
        "status": item.status,
        "revision": item.revision,
        "generation": item.generation,
        "claimed_by": item.claimed_by,
        "uploaded_by_me": item.owner_principal_id == context.actor.principal_id,
        "claimed_by_me": item.claimed_by == context.actor.principal_id,
        "claim_revision": item.claim_revision,
        "claim_expires_at": item.claim_expires_at.isoformat()
        if item.claim_expires_at
        else None,
        "part_id": item.part_id,
        "entry_id": str(item.entry_id) if item.entry_id else None,
        "assignment_key": str(item.assignment_key) if item.assignment_key else None,
    }


def can_read(db, item, context, authorize):
    if item.owner_principal_id == context.actor.principal_id:
        access(db, context, authorize, "inbox.read", None, field="inbox")
    else:
        decision = access(
            db, context, authorize, "inbox.shared.read", None, field="inbox"
        )
        if decision.metadata.get(
            "scope"
        ) != "all" and item.owner_principal_id not in decision.metadata.get(
            "owner_principal_ids", []
        ):
            raise CatalogError("auth.forbidden", "收件箱不在可读范围", status_code=403)
        if item.status == "receiving":
            raise CatalogError("request.not_found", "图片尚未接收完成", status_code=404)


def list_inbox(
    db,
    *,
    context,
    authorize,
    scope="mine",
    group_id=None,
    status="ready",
    after_id=None,
    limit=50,
):
    if (
        scope not in ("mine", "shared")
        or status not in ("all", "ready", "assigned", "deleted", "receiving", "failed")
        or not 1 <= limit <= 200
    ):
        raise CatalogError(
            "request.validation_failed", "收件箱筛选无效", status_code=422
        )
    decision = access(
        db,
        context,
        authorize,
        "inbox.shared.read" if scope == "shared" else "inbox.read",
        None,
        field="inbox",
    )
    query = select(InboxItem)
    if status != "all":
        query = query.where(InboxItem.status == status)
    elif scope == "shared":
        query = query.where(InboxItem.status != "receiving")
    if scope == "mine":
        query = query.where(InboxItem.owner_principal_id == context.actor.principal_id)
    elif status == "receiving":
        raise CatalogError(
            "request.validation_failed",
            "共享收件箱仅显示持久接收的图片",
            status_code=422,
        )
    elif decision.metadata.get("scope") != "all":
        owners = decision.metadata.get("owner_principal_ids")
        if not isinstance(owners, (list, tuple)) or any(
            not isinstance(v, str) for v in owners
        ):
            raise CatalogError(
                "auth.unavailable", "共享收件箱范围尚未接入", status_code=503
            )
        query = query.where(InboxItem.owner_principal_id.in_(owners))
    if group_id:
        query = query.where(InboxItem.group_id == group_id)
    if after_id:
        query = query.where(InboxItem.id > after_id)
    items = list(db.scalars(query.order_by(InboxItem.id).limit(limit + 1)))
    has_more = len(items) > limit
    items = items[:limit]
    metadata = _inbox_metadata(db, items)
    return {
        "data": [inbox_data(db, item, context=context, metadata=metadata) for item in items],
        "meta": {
            "request_id": str(context.request_id),
            "replayed": False,
            "next_cursor": str(items[-1].id) if has_more else None,
        },
    }


def get_inbox(db, inbox_id, *, context, authorize):
    item = load(db, InboxItem, inbox_id)
    can_read(db, item, context, authorize)
    return read_response(context, inbox_data(db, item, context=context))


def inbox_links(db, inbox_id, *, context, authorize, signer):
    """Issue fresh local signatures only after current inbox visibility and deletion checks."""
    item = load(db, InboxItem, inbox_id)
    can_read(db, item, context, authorize)
    item = load(db, InboxItem, inbox_id, lock=True, shared=True)
    can_read(db, item, context, authorize)
    if item.status == "deleted":
        raise CatalogError("request.not_found", "收件箱图片已删除", status_code=404)
    if item.status not in ("ready", "assigned") or item.asset_id is None:
        raise CatalogError("media.inbox_not_ready", "图片尚未验证完成，暂无预览", status_code=409)
    asset = load(db, Asset, item.asset_id)
    derivatives = db.scalars(select(Derivative).where(Derivative.asset_id == asset.id,
        Derivative.status == "ready").order_by(Derivative.recipe_id).limit(20)).all()
    return {"inbox_id": str(item.id), "asset_id": str(asset.id),
        "url": signer.signed_get(asset.object_key, expires_in=300),
        "derivatives": [{"recipe_id": row.recipe_id, "url": signer.signed_get(row.object_key, expires_in=300)} for row in derivatives],
        "expires_at": (now() + timedelta(seconds=300)).isoformat()}


def _begin_item(db, inbox_id, command, *, context, authorize, operation):
    item = load(db, InboxItem, inbox_id)
    can_read(db, item, context, authorize)
    return begin(
        db,
        context=context,
        authorize=authorize,
        operation=operation,
        part_id=None,
        resource="media.inbox",
        identifier=inbox_id,
        command=command,
        field="inbox",
    )


def claim_inbox(db, inbox_id, command, *, context, authorize):
    handle = _begin_item(
        db,
        inbox_id,
        command,
        context=context,
        authorize=authorize,
        operation="inbox.claim",
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    item = load(db, InboxItem, inbox_id, lock=True)
    revision(
        context, item.revision, command.expected_revision, code="inbox.claim_conflict"
    )
    if item.status != "ready":
        raise CatalogError("inbox.claim_conflict", "图片当前不可认领")
    active_other = (
        item.claimed_by not in (None, context.actor.principal_id)
        and item.claim_expires_at
        and item.claim_expires_at > now()
    )
    if active_other:
        if not command.takeover_reason:
            raise CatalogError("inbox.claim_conflict", "图片已被其他人认领")
        access(
            db, context, authorize, "inbox.takeover", None, field="inbox", write=True
        )
    before = item.revision
    item.claimed_by = context.actor.principal_id
    item.claim_expires_at = now() + timedelta(minutes=15)
    item.claim_revision += 1
    item.revision += 1
    receipt = finish(
        db,
        handle,
        part_id=item.part_id,
        identifier=item.id,
        before=before,
        after=item.revision,
        changes={
            "claimed_by": item.claimed_by,
        "uploaded_by_me": item.owner_principal_id == context.actor.principal_id,
        "claimed_by_me": item.claimed_by == context.actor.principal_id,
            "claim_revision": item.claim_revision,
            "takeover_reason": command.takeover_reason,
        },
    )
    return response(db, receipt, inbox_data(db, item, context=context), replayed=False)


def release_inbox(db, inbox_id, command, *, context, authorize):
    handle = _begin_item(
        db,
        inbox_id,
        command,
        context=context,
        authorize=authorize,
        operation="inbox.release",
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    item = load(db, InboxItem, inbox_id, lock=True)
    revision(
        context,
        item.claim_revision,
        command.expected_claim_revision,
        code="inbox.claim_conflict",
    )
    if item.claimed_by != context.actor.principal_id:
        if not command.reason:
            raise CatalogError(
                "auth.forbidden", "只有认领人可释放，管理接手需要原因", status_code=403
            )
        access(
            db, context, authorize, "inbox.takeover", None, field="inbox", write=True
        )
    before = item.revision
    item.claimed_by, item.claim_expires_at = None, None
    item.claim_revision += 1
    item.revision += 1
    receipt = finish(
        db,
        handle,
        part_id=item.part_id,
        identifier=item.id,
        before=before,
        after=item.revision,
        changes={
            "released": True,
            "claim_revision": item.claim_revision,
            "reason": command.reason,
        },
    )
    return response(db, receipt, inbox_data(db, item, context=context), replayed=False)


def assign_inbox(db, inbox_id, command, *, context, authorize):
    # Hash target uses requested destination, stable even after the inbox gains part_id.
    item = load(db, InboxItem, inbox_id)
    can_read(db, item, context, authorize)
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="inbox.assign",
        part_id=command.part_id,
        resource="media.inbox",
        identifier=inbox_id,
        command=command,
        field="inbox",
    )
    access(db, context, authorize, "gallery.archive", command.part_id, write=True)
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    identity(
        db,
        [command.part_id],
        expected={command.part_id: command.expected_identity_revision},
    )
    # Upload is always before inbox to agree with cancellation and verified receive.
    upload = load(db, UploadTicket, item.upload_id, lock=True)
    item = load(db, InboxItem, inbox_id, lock=True)
    key_lock(db, "inbox-assignment", str(command.assignment_key))
    other = db.scalar(
        select(InboxItem).where(InboxItem.assignment_key == command.assignment_key)
    )
    if other is not None and other.id != item.id:
        raise CatalogError("request.idempotency_mismatch", "归档编号已用于其他图片")
    before = item.revision
    if item.assignment_key == command.assignment_key and item.status == "assigned":
        if item.part_id != command.part_id:
            raise CatalogError("request.idempotency_mismatch", "归档目标已改变")
    else:
        if item.status != "ready" or upload.status == "cancelled":
            raise CatalogError("inbox.claim_conflict", "图片当前不可归档")
        if (
            item.claimed_by != context.actor.principal_id
            or item.claim_revision != command.claim_revision
            or (item.claim_expires_at is None or item.claim_expires_at <= now())
        ):
            raise CatalogError("inbox.claim_conflict", "认领已过期或被接手")
        asset = load(db, Asset, item.asset_id)
        entry, _group, _created = attach_asset(
            db,
            part_id=command.part_id,
            asset=asset,
            purpose=command.purpose,
            context=context,
        )
        item.status, item.part_id, item.entry_id = "assigned", command.part_id, entry.id
        item.assignment_key, item.claim_expires_at = command.assignment_key, None
        item.revision += 1
        upload.status, upload.entry_id = "committed", entry.id
        upload.revision += 1
    receipt = finish(
        db,
        handle,
        part_id=command.part_id,
        identifier=item.id,
        before=before,
        after=item.revision,
        changes={
            "part_id": item.part_id,
            "entry_id": str(item.entry_id),
            "assignment_key": str(item.assignment_key),
        },
        identity_revision=command.expected_identity_revision,
    )
    return response(db, receipt, inbox_data(db, item, context=context), replayed=False)


def change_inbox(db, inbox_id, command, *, context, authorize, restore=False):
    handle = _begin_item(
        db,
        inbox_id,
        command,
        context=context,
        authorize=authorize,
        operation="inbox.restore" if restore else "inbox.delete",
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    initial = load(db, InboxItem, inbox_id)
    identity(db, [initial.part_id])
    upload = load(db, UploadTicket, initial.upload_id, lock=True)
    item = load(db, InboxItem, inbox_id, lock=True)
    owner(db, item, context, authorize, admin_action="inbox.manage", field="inbox")
    expected = (
        command.expected_deleted_revision if restore else command.expected_revision
    )
    revision(context, item.revision, expected, code="inbox.claim_conflict")
    before = item.revision
    if restore:
        if item.status != "deleted" or (
            item.asset_id is None and upload.asset_id is None
        ):
            raise CatalogError(
                "request.precondition_failed", "无可恢复的已接收图片", status_code=412
            )
        item.asset_id = item.asset_id or upload.asset_id
        if upload.status == "cancelled":
            upload.status = "ready"
            upload.generation += 1
            upload.revision += 1
        item.status, item.deleted_at = "ready", None
        item.part_id, item.entry_id, item.assignment_key = None, None, None
    else:
        if item.status == "assigned":
            raise CatalogError(
                "media.upload_already_committed", "照片已归档，请从图库删除"
            )
        if item.status == "deleted":
            raise CatalogError("media.deleted", "图片已删除")
        item.status, item.deleted_at = "deleted", now()
    item.claimed_by, item.claim_expires_at = None, None
    item.claim_revision += 1
    item.generation += 1
    item.revision += 1
    receipt = finish(
        db,
        handle,
        part_id=item.part_id,
        identifier=item.id,
        before=before,
        after=item.revision,
        changes={
            "status": item.status,
            "generation": item.generation,
            "reason": getattr(command, "reason", None),
        },
    )
    return response(db, receipt, inbox_data(db, item, context=context), replayed=False)


def group_data(db, group):
    items = list(
        db.scalars(
            select(InboxItem)
            .where(InboxItem.group_id == group.id)
            .order_by(InboxItem.position, InboxItem.id)
        )
    )
    return {
        "group_id": str(group.id),
        "capture_mode": group.capture_mode,
        "revision": group.revision,
        "ordered_item_ids": [str(item.id) for item in items],
    }


def owned_group(db, identifier, context, authorize, *, lock=False):
    group = load(db, InboxGroup, identifier, lock=lock)
    owner(db, group, context, authorize, admin_action="inbox.manage", field="inbox")
    return group


def create_inbox_group(db, command, *, context, authorize):
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="inbox.group.create",
        part_id=None,
        resource="media.inbox_group",
        identifier=command.client_group_id,
        command=command,
        field="inbox",
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    key_lock(
        db,
        "inbox-group",
        context.actor.principal_id,
        context.actor.client_id,
        str(command.client_group_id),
    )
    group = db.scalar(
        select(InboxGroup).where(
            InboxGroup.owner_principal_id == context.actor.principal_id,
            InboxGroup.client_id == context.actor.client_id,
            InboxGroup.client_group_id == command.client_group_id,
        )
    )
    if group and group.capture_mode != command.capture_mode:
        raise CatalogError("request.idempotency_mismatch", "分组编号已用于其他拍摄模式")
    if group is None:
        group = InboxGroup(
            id=uuid4(),
            owner_principal_id=context.actor.principal_id,
            client_id=context.actor.client_id,
            client_group_id=command.client_group_id,
            capture_mode=command.capture_mode,
            revision=1,
        )
        db.add(group)
        db.flush()
    receipt = finish(
        db,
        handle,
        part_id=None,
        identifier=group.id,
        before=0,
        after=group.revision,
        changes={"capture_mode": group.capture_mode},
    )
    return response(db, receipt, group_data(db, group), replayed=False)


def add_group_item(db, group_id, command, *, context, authorize):
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="inbox.group.add",
        part_id=None,
        resource="media.inbox_group",
        identifier=group_id,
        command=command,
        field="inbox",
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    from .media_uploads import owned_upload

    upload = owned_upload(db, command.upload_id, context, authorize, lock=True)
    group = owned_group(db, group_id, context, authorize, lock=True)
    revision(
        context,
        group.revision,
        command.expected_group_revision,
        code="inbox.group_conflict",
    )
    if upload.part_id is not None or upload.status == "cancelled":
        raise CatalogError(
            "catalog.binding_conflict", "只有未定目标的有效上传可进入收件箱"
        )
    item = db.scalar(
        select(InboxItem).where(InboxItem.upload_id == upload.id).with_for_update()
    )
    if item and item.group_id not in (None, group.id):
        raise CatalogError("inbox.group_conflict", "照片已经属于其他分组")
    if item is None:
        item = InboxItem(
            id=uuid4(),
            upload_id=upload.id,
            owner_principal_id=upload.owner_principal_id,
            asset_id=upload.asset_id,
            status="ready" if upload.asset_id else "receiving",
            revision=1,
            generation=1,
            claim_revision=0,
        )
        db.add(item)
    if item.status not in ("receiving", "ready"):
        raise CatalogError("inbox.group_conflict", "照片当前不可分组")
    if (item.group_id == group.id and command.client_order is not None
            and item.client_order == command.client_order):
        receipt = finish(db, handle, part_id=None, identifier=group.id,
            before=group.revision, after=group.revision,
            changes={"inbox_id": str(item.id), "client_order": item.client_order, "already_grouped": True})
        return response(db, receipt, group_data(db, group), replayed=False)
    existing = list(
        db.scalars(
            select(InboxItem)
            .where(InboxItem.group_id == group.id, InboxItem.id != item.id)
            .order_by(InboxItem.position, InboxItem.id)
            .with_for_update()
        )
    )
    if len(existing) >= 2000:
        raise CatalogError(
            "request.validation_failed", "分组位置无效或分组已满", status_code=422
        )
    if command.client_order is not None:
        if item.client_order not in (None, command.client_order):
            raise CatalogError("inbox.group_conflict", "照片的原始拍摄顺序不能改写")
        if any(row.client_order == command.client_order for row in existing):
            raise CatalogError("inbox.group_conflict", "该拍摄顺序已对应另一张照片")
        item.client_order = command.client_order
        # Late uploads insert relative to original capture order; existing manual
        # relative order stays intact. Old rows without capture order remain where they are.
        insertion = next((position for position, row in enumerate(existing)
            if row.client_order is not None and row.client_order > command.client_order), len(existing))
    else:
        if command.position > len(existing):
            raise CatalogError("request.validation_failed", "分组位置无效或分组已满", status_code=422)
        insertion = command.position
    existing.insert(insertion, item)
    for position, row in enumerate(existing):
        row.position = position
    item.group_id = group.id
    item.revision += 1
    before = group.revision
    group.revision += 1
    db.flush()
    receipt = finish(
        db,
        handle,
        part_id=None,
        identifier=group.id,
        before=before,
        after=group.revision,
        changes={"inbox_id": str(item.id), "position": item.position, "client_order": item.client_order},
    )
    return response(db, receipt, group_data(db, group), replayed=False)


def arrange_inbox_group(db, group_id, command, *, context, authorize):
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="inbox.group.arrange",
        part_id=None,
        resource="media.inbox_group",
        identifier=group_id,
        command=command,
        field="inbox",
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    group = owned_group(db, group_id, context, authorize, lock=True)
    revision(
        context, group.revision, command.expected_revision, code="inbox.group_conflict"
    )
    items = list(
        db.scalars(
            select(InboxItem).where(InboxItem.group_id == group.id).with_for_update()
        )
    )
    by_id = {item.id: item for item in items}
    if len(set(command.ordered_item_ids)) != len(command.ordered_item_ids) or set(
        command.ordered_item_ids
    ) != set(by_id):
        raise CatalogError(
            "request.validation_failed", "分组顺序必须完整包含所有照片", status_code=422
        )
    for position, identifier in enumerate(command.ordered_item_ids):
        by_id[identifier].position = position
    before = group.revision
    group.revision += 1
    receipt = finish(
        db,
        handle,
        part_id=None,
        identifier=group.id,
        before=before,
        after=group.revision,
        changes={"ordered_item_ids": [str(v) for v in command.ordered_item_ids]},
    )
    return response(db, receipt, group_data(db, group), replayed=False)
