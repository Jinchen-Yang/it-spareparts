"""Media invariants exercised with real PostgreSQL and deterministic provider results."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db import engine
from app.models.dimensions import DimPart
from app.pn_catalog import jobs, media
from app.pn_catalog.dto import ActorContext, AuthorizationDecision, CommandContext
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_common import now
from app.pn_catalog.media_dto import (
    AddEntry,
    AddInboxGroupItem,
    ArrangeGallery,
    ArrangeInboxGroup,
    AssignInbox,
    ClaimInbox,
    CompleteMultipart,
    CreateDerivative,
    CreateInboxGroup,
    CreateUpload,
    ExpectedRevision,
    MultipartPart,
    MultipartPartURLs,
    PatchEntry,
    RecordMultipartPart,
    RestoreEntry,
    SuppressHash,
)
from app.pn_catalog.media_models import (
    Asset,
    Derivative,
    GalleryEntry,
    GalleryGroup,
    GalleryMember,
    InboxItem,
    MultipartUpload,
    UploadTicket,
)
from app.pn_catalog.media_ports import VerifiedImage
from app.pn_catalog.models import AuditEvent, CommandReceipt, PartState


def context(principal="account:42", request_id=None):
    return CommandContext(
        actor=ActorContext(
            principal_id=principal,
            client_id="shop",
            executor_id="shop-api",
            entrypoint="shop_ui",
        ),
        request_id=request_id or uuid4(),
    )


def allow(*_):
    return AuthorizationDecision(frozenset({"*"}), frozenset({"*"}), {"scope": "all"})


def seed(db, name="PN-A"):
    part = DimPart(pn_std=name, description="A")
    db.add(part)
    db.commit()
    return part.id


def create_ticket(db, part_id=None, *, ctx=None, sha="a" * 64, size=100, ready=True):
    ctx = ctx or context()
    command = CreateUpload(
        client_request_id=uuid4(),
        filename="image.jpg",
        size_bytes=size,
        content_type="image/jpeg",
        part_id=part_id,
        expected_identity_revision=0 if part_id else None,
    )
    result = media.create_upload(db, command, context=ctx, authorize=allow)
    db.commit()
    identifier = UUID(result["data"]["upload_id"])
    if ready:
        media.complete_upload(
            db,
            identifier,
            ExpectedRevision(expected_revision=1),
            context=context(ctx.actor.principal_id),
            authorize=allow,
        )
        db.commit()
        lease = jobs.claim(
            db, worker_id="worker-1", queues=["media"], kinds=["media.verify_upload"]
        )
        assert lease.resource_id == str(identifier)
        db.commit()
        verified = VerifiedImage(
            sha,
            10,
            10,
            size,
            "image/jpeg",
            "etag-1",
            f"img/pn-catalog/assets/{identifier}/original",
        )
        result = jobs.complete(
            db,
            lease,
            verify_resource=lambda s, lease: media.verify_upload_resource(
                s, lease, authorize=allow
            ),
            publish=lambda s, lease: media.publish_verified_upload(s, lease, verified),
        )
        assert result.status == "succeeded"
        db.commit()
    return identifier


def add(db, part_id, ticket, *, ctx=None):
    return media.add_entry(
        db,
        part_id,
        AddEntry(expected_identity_revision=0, upload_id=ticket),
        context=ctx or context(),
        authorize=allow,
    )


def entry(db, part_id, sha="a" * 64):
    identifier = create_ticket(db, part_id, sha=sha)
    result = add(db, part_id, identifier)
    db.commit()
    return UUID(result["data"]["entry_id"])


def test_tombstone_blocks_old_ticket_and_cross_purpose_reupload(db):
    pid = seed(db)
    old_ticket = create_ticket(db, pid)
    eid = entry(db, pid)
    media.delete_entry(
        db,
        eid,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    with pytest.raises(CatalogError, match="明确恢复") as deleted:
        add(db, pid, old_ticket)
    assert deleted.value.code == "media.deleted"
    db.rollback()
    # Changing the ticket purpose cannot provide a new route around the same source deletion.
    ticket = db.get(UploadTicket, old_ticket)
    ticket.purpose = "defect"
    db.commit()
    with pytest.raises(CatalogError) as deleted:
        media.add_entry(
            db,
            pid,
            AddEntry(
                expected_identity_revision=0, upload_id=old_ticket, purpose="defect"
            ),
            context=context(),
            authorize=allow,
        )
    assert deleted.value.code == "media.deleted"
    db.rollback()
    restored = media.restore_entry(
        db,
        eid,
        RestoreEntry(expected_deleted_revision=2, reason="人工确认恢复"),
        context=context(),
        authorize=allow,
    )
    assert restored["data"]["entry_id"] == str(eid)
    assert restored["data"]["generation"] == 3


def test_duplicate_concurrent_uploads_keep_one_entry_and_two_receipts(db):
    pid = seed(db)
    tickets = [create_ticket(db, pid), create_ticket(db, pid)]
    barrier = Barrier(2)

    def worker(ticket):
        with Session(engine) as session:
            barrier.wait(timeout=5)
            result = add(session, pid, ticket)
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, tickets))
    assert results[0]["data"]["entry_id"] == results[1]["data"]["entry_id"]
    assert results[0]["meta"]["audit_id"] != results[1]["meta"]["audit_id"]
    assert db.scalar(select(func.count()).select_from(GalleryEntry)) == 1


def test_global_key_collision_and_replay_current_projection(db):
    pid = seed(db)
    ticket = create_ticket(db, pid)
    ctx = context()
    first = add(db, pid, ticket, ctx=ctx)
    eid = UUID(first["data"]["entry_id"])
    db.commit()
    media.delete_entry(
        db,
        eid,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    replay = add(db, pid, ticket, ctx=ctx)
    assert replay["meta"]["replayed"]
    assert replay["meta"]["applied_revision"] == 1
    assert replay["meta"]["data_revision"] == 2
    assert replay["data"]["status"] == "deleted"
    with pytest.raises(CatalogError) as clash:
        media.delete_entry(
            db, eid, ExpectedRevision(expected_revision=2), context=ctx, authorize=allow
        )
    assert clash.value.code == "request.idempotency_mismatch"


def test_rollback_rolls_back_gallery_audit_and_receipt(db):
    pid = seed(db)
    ticket = create_ticket(db, pid)
    before = db.scalar(select(func.count()).select_from(AuditEvent))
    add(db, pid, ticket)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(GalleryEntry)) == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == before


def test_source_read_and_reuse_are_required_for_asset_reference(db):
    source_pid, target_pid = seed(db, "SOURCE"), seed(db, "TARGET")
    eid = entry(db, source_pid)
    asset_id = db.get(GalleryEntry, eid).asset_id

    def target_only(_db, _ctx, action, pid):
        if pid == source_pid:
            raise CatalogError("auth.forbidden", "source denied", status_code=403)
        return allow()

    with pytest.raises(CatalogError) as denied:
        media.add_entry(
            db,
            target_pid,
            AddEntry(
                expected_identity_revision=0, asset_id=asset_id, source_entry_id=eid
            ),
            context=context(),
            authorize=target_only,
        )
    assert denied.value.code == "auth.forbidden"
    assert db.scalar(select(func.count()).select_from(GalleryEntry)) == 1


def test_identity_change_fences_upload_archive(db):
    pid = seed(db)
    ticket = create_ticket(db, pid)
    db.get(PartState, pid).identity_revision += 1
    db.commit()
    with pytest.raises(CatalogError) as changed:
        add(db, pid, ticket)
    assert changed.value.code == "catalog.identity_changed"


def test_asset_is_immutable_even_through_direct_dml(db):
    ticket = create_ticket(db)
    asset = db.get(Asset, db.get(UploadTicket, ticket).asset_id)
    asset.object_key = "replace-original"
    with pytest.raises(DBAPIError):
        db.flush()
    db.rollback()


def test_cancelled_upload_cannot_publish_late_verified_object(db):
    ticket = create_ticket(db, ready=False)
    media.complete_upload(
        db,
        ticket,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    lease = jobs.claim(db, worker_id="worker", queues=["media"])
    db.commit()
    media.cancel_upload(
        db,
        ticket,
        ExpectedRevision(expected_revision=2),
        context=context(),
        authorize=allow,
    )
    db.commit()
    with pytest.raises(CatalogError) as lost:
        jobs.complete(
            db,
            lease,
            verify_resource=lambda s, lease: media.verify_upload_resource(
                s, lease, authorize=allow
            ),
            publish=lambda *_: pytest.fail("late upload published"),
        )
    assert lost.value.code == "job.lease_lost"
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Asset)) == 0


def test_delete_restore_fences_thumbnail_and_does_not_bump_gallery_on_ready(db):
    pid = seed(db)
    eid = entry(db, pid)
    asset_id = db.get(GalleryEntry, eid).asset_id
    media.create_derivative(
        db,
        asset_id,
        CreateDerivative(entry_id=eid, expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    lease = jobs.claim(
        db, worker_id="old-worker", queues=["media"], kinds=["media.thumbnail"]
    )
    db.commit()
    media.delete_entry(
        db,
        eid,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    media.restore_entry(
        db,
        eid,
        RestoreEntry(expected_deleted_revision=2, reason="restore"),
        context=context(),
        authorize=allow,
    )
    db.commit()
    result = jobs.complete(
        db,
        lease,
        verify_resource=lambda s, lease: media.verify_derivative_resource(
            s, lease, authorize=allow
        ),
        publish=lambda *_: pytest.fail("old generation published"),
    )
    assert result.status == "stale"
    db.commit()
    media.create_derivative(
        db,
        asset_id,
        CreateDerivative(entry_id=eid, expected_revision=3),
        context=context(),
        authorize=allow,
    )
    db.commit()
    current = jobs.claim(
        db, worker_id="new-worker", queues=["media"], kinds=["media.thumbnail"]
    )
    db.commit()
    revision_before = db.scalar(select(GalleryGroup.revision))
    done = jobs.complete(
        db,
        current,
        verify_resource=lambda s, lease: media.verify_derivative_resource(
            s, lease, authorize=allow
        ),
        publish=media.publish_derivative,
    )
    assert done.status == "succeeded"
    assert db.scalar(select(GalleryGroup.revision)) == revision_before
    assert db.scalar(select(Derivative.status)) == "ready"


def test_gallery_order_membership_and_suppression_survive_new_source(db):
    pid = seed(db)
    a, b = entry(db, pid), entry(db, pid, "b" * 64)
    gallery = media.get_gallery(db, pid, context=context(), authorize=allow)["data"]
    with pytest.raises(CatalogError) as missing:
        media.arrange_gallery(
            db,
            pid,
            ArrangeGallery(
                expected_gallery_revision=gallery["revision"],
                primary_entry_id=a,
                ordered_entry_ids=[a],
            ),
            context=context(),
            authorize=allow,
        )
    assert missing.value.code == "request.validation_failed"
    db.rollback()
    result = media.arrange_gallery(
        db,
        pid,
        ArrangeGallery(
            expected_gallery_revision=gallery["revision"],
            primary_entry_id=b,
            ordered_entry_ids=[b, a],
        ),
        context=context(),
        authorize=allow,
    )
    db.commit()
    hidden = media.suppress_hash(
        db,
        UUID(gallery["group_id"]),
        SuppressHash(
            expected_gallery_revision=result["data"]["revision"],
            content_hash="a" * 64,
            reason="隐藏重复图",
        ),
        context=context(),
        authorize=allow,
    )
    db.commit()
    add(db, pid, create_ticket(db, pid))
    db.commit()
    assert [
        e["entry_id"]
        for e in media.get_gallery(db, pid, context=context(), authorize=allow)["data"][
            "entries"
        ]
    ] == [str(b)]
    media.unsuppress_hash(
        db,
        UUID(gallery["group_id"]),
        UUID(hidden["data"]["suppression_id"]),
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    assert (
        len(
            media.get_gallery(db, pid, context=context(), authorize=allow)["data"][
                "entries"
            ]
        )
        == 2
    )


def test_fresh_urls_require_current_permission_and_are_never_receipt_data(db):
    pid = seed(db)
    eid = entry(db, pid)

    class Signer:
        calls = 0

        def signed_get(self, key, expires_in):
            self.calls += 1
            return f"https://storage.invalid/{self.calls}?ttl={expires_in}"

    signer = Signer()
    a = media.entry_links(db, eid, context=context(), authorize=allow, signer=signer)
    b = media.entry_links(db, eid, context=context(), authorize=allow, signer=signer)
    assert a["url"] != b["url"]

    def deny(*_):
        raise CatalogError("auth.forbidden", "revoked", status_code=403)

    with pytest.raises(CatalogError):
        media.entry_links(db, eid, context=context(), authorize=deny, signer=signer)
    assert signer.calls == 2
    assert all("url" not in str(v) for v in db.scalars(select(CommandReceipt.result)))


def test_inbox_claim_expiry_and_assignment_are_atomic(db):
    pid = seed(db)
    ticket = create_ticket(db)
    item = db.scalar(select(InboxItem).where(InboxItem.upload_id == ticket))
    first = media.claim_inbox(
        db, item.id, ClaimInbox(expected_revision=1), context=context(), authorize=allow
    )
    db.commit()
    item.claim_expires_at = now() - timedelta(seconds=1)
    db.commit()
    second = media.claim_inbox(
        db,
        item.id,
        ClaimInbox(expected_revision=2),
        context=context("account:43"),
        authorize=allow,
    )
    db.commit()
    key = uuid4()
    with pytest.raises(CatalogError) as stale:
        media.assign_inbox(
            db,
            item.id,
            AssignInbox(
                claim_revision=first["data"]["claim_revision"],
                part_id=pid,
                expected_identity_revision=0,
                assignment_key=key,
            ),
            context=context(),
            authorize=allow,
        )
    assert stale.value.code == "inbox.claim_conflict"
    db.rollback()
    cmd = AssignInbox(
        claim_revision=second["data"]["claim_revision"],
        part_id=pid,
        expected_identity_revision=0,
        assignment_key=key,
    )
    ctx = context("account:43")
    result = media.assign_inbox(db, item.id, cmd, context=ctx, authorize=allow)
    db.commit()
    replay = media.assign_inbox(db, item.id, cmd, context=ctx, authorize=allow)
    assert replay["meta"]["replayed"]
    assert result["data"]["entry_id"] == replay["data"]["entry_id"]
    assert db.scalar(select(func.count()).select_from(GalleryEntry)) == 1


def test_shared_inbox_excludes_receiving_and_applies_owner_scope_before_limit(db):
    ticket = create_ticket(db, ready=False)
    group = media.create_inbox_group(
        db,
        CreateInboxGroup(client_group_id=uuid4(), capture_mode="same_item"),
        context=context(),
        authorize=allow,
    )
    media.add_group_item(
        db,
        UUID(group["data"]["group_id"]),
        AddInboxGroupItem(upload_id=ticket, position=0, expected_group_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    assert (
        media.list_inbox(
            db, scope="shared", context=context("account:43"), authorize=allow
        )["data"]
        == []
    )
    create_ticket(db, ctx=context("account:44"))
    mine = create_ticket(db, ctx=context("account:43"))

    def scoped(*_):
        return AuthorizationDecision(
            frozenset({"inbox"}), frozenset(), {"owner_principal_ids": ["account:43"]}
        )

    result = media.list_inbox(
        db, scope="shared", context=context("account:43"), authorize=scoped, limit=1
    )
    assert [row["upload_id"] for row in result["data"]] == [str(mine)]


def test_inbox_group_order_rejects_partial_members_and_stale_revision(db):
    group = media.create_inbox_group(
        db,
        CreateInboxGroup(client_group_id=uuid4(), capture_mode="same_item"),
        context=context(),
        authorize=allow,
    )
    gid = UUID(group["data"]["group_id"])
    tickets = [create_ticket(db), create_ticket(db)]
    for index, ticket in enumerate(tickets):
        media.add_group_item(
            db,
            gid,
            AddInboxGroupItem(
                upload_id=ticket, position=index, expected_group_revision=index + 1
            ),
            context=context(),
            authorize=allow,
        )
        db.commit()
    ids = list(
        db.scalars(
            select(InboxItem.id)
            .where(InboxItem.group_id == gid)
            .order_by(InboxItem.position)
        )
    )
    with pytest.raises(CatalogError):
        media.arrange_inbox_group(
            db,
            gid,
            ArrangeInboxGroup(expected_revision=3, ordered_item_ids=ids[:1]),
            context=context(),
            authorize=allow,
        )
    db.rollback()
    result = media.arrange_inbox_group(
        db,
        gid,
        ArrangeInboxGroup(expected_revision=3, ordered_item_ids=list(reversed(ids))),
        context=context(),
        authorize=allow,
    )
    assert result["data"]["ordered_item_ids"] == list(map(str, reversed(ids)))


def test_multipart_actual_parts_and_cancelled_initialization_fence(db):
    ticket = create_ticket(db, ready=False)
    media.start_multipart(
        db,
        ticket,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    plan = media.multipart_start_plan(db, ticket, context=context(), authorize=allow)
    assert media.publish_multipart_started(
        db,
        ticket,
        operation_token=plan["operation_token"],
        storage_upload_id="OSS-UPLOAD-1",
    )
    db.commit()
    actual = MultipartPart(part_number=1, etag="server-etag", size_bytes=100)
    cmd = RecordMultipartPart(
        expected_revision=3,
        part=MultipartPart(part_number=1, etag="forged", size_bytes=100),
    )
    with pytest.raises(CatalogError) as forged:
        media.record_multipart_part(
            db, ticket, cmd, server_parts=[actual], context=context(), authorize=allow
        )
    assert forged.value.code == "media.invalid_part"
    db.rollback()
    media.record_multipart_part(
        db,
        ticket,
        RecordMultipartPart(expected_revision=3, part=actual),
        server_parts=[actual],
        context=context(),
        authorize=allow,
    )
    db.commit()
    media.complete_multipart(
        db,
        ticket,
        CompleteMultipart(expected_revision=4, parts=[actual]),
        server_parts=[actual],
        context=context(),
        authorize=allow,
    )
    db.commit()
    assert db.get(MultipartUpload, ticket).storage_upload_id == "OSS-UPLOAD-1"
    lease = jobs.claim(
        db,
        worker_id="multipart-worker",
        queues=["media"],
        kinds=["media.complete_multipart"],
    )
    db.commit()
    done = jobs.complete(
        db,
        lease,
        verify_resource=lambda s, lease: media.verify_upload_resource(
            s, lease, authorize=allow
        ),
        publish=media.publish_multipart_completed,
    )
    assert done.status == "succeeded"
    db.commit()
    assert db.get(UploadTicket, ticket).status == "verifying"
    other = create_ticket(db, ready=False)
    media.start_multipart(
        db,
        other,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    plan = media.multipart_start_plan(db, other, context=context(), authorize=allow)
    media.cancel_upload(
        db,
        other,
        ExpectedRevision(expected_revision=2),
        context=context(),
        authorize=allow,
    )
    db.commit()
    assert not media.publish_multipart_started(
        db,
        other,
        operation_token=plan["operation_token"],
        storage_upload_id="late-oss-id",
    )


def test_gallery_get_is_read_only_and_batches_projection_queries(db):
    pid = seed(db)
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        empty = media.get_gallery(db, pid, context=context(), authorize=allow)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert empty["data"]["entries"] == []
    assert not any(
        sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements
    )
    assert db.get(PartState, pid) is None
    db.commit()
    for character in "abcdef":
        entry(db, pid, character * 64)
    statements.clear()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = media.get_gallery(db, pid, context=context(), authorize=allow)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(result["data"]["entries"]) == 6
    assert sum("FROM pn_media_asset " in sql for sql in statements) == 1
    assert sum("FROM pn_media_derivative " in sql for sql in statements) == 1
    assert not any(
        sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements
    )


def test_gallery_pagination_is_revision_bound_and_candidates_deleted_are_discoverable(
    db,
):
    pid = seed(db)
    first, second = entry(db, pid), entry(db, pid, "b" * 64)
    page = media.get_gallery(db, pid, context=context(), authorize=allow, limit=1)
    assert len(page["data"]["entries"]) == 1
    tail = media.get_gallery(
        db,
        pid,
        context=context(),
        authorize=allow,
        limit=1,
        cursor=page["meta"]["next_cursor"],
    )
    assert (
        tail["data"]["entries"][0]["entry_id"] != page["data"]["entries"][0]["entry_id"]
    )
    db.commit()
    media.patch_entry(
        db,
        first,
        PatchEntry(expected_revision=1, status="candidate"),
        context=context(),
        authorize=allow,
    )
    db.commit()
    with pytest.raises(CatalogError) as expired:
        media.get_gallery(
            db,
            pid,
            context=context(),
            authorize=allow,
            cursor=page["meta"]["next_cursor"],
        )
    assert expired.value.code == "pagination.cursor_expired"
    db.rollback()
    candidates = media.get_gallery(
        db, pid, context=context(), authorize=allow, status="candidate"
    )
    assert [row["entry_id"] for row in candidates["data"]["entries"]] == [str(first)]
    media.delete_entry(
        db,
        second,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    deleted = media.get_gallery(
        db, pid, context=context(), authorize=allow, status="deleted"
    )
    assert [row["entry_id"] for row in deleted["data"]["entries"]] == [str(second)]


def test_upload_transport_plan_matches_existing_storage_prefix_and_part_bounds(db):
    ticket = create_ticket(db, ready=False)
    plan = media.upload_transport_plan(db, ticket, context=context(), authorize=allow)
    # The existing shop Storage rejects any object outside this staging prefix.
    assert plan["key"] == f"uploads/pn-catalog/{ticket}"
    assert plan["expected_size"] == 100
    assert plan["expires_at"] <= now() + timedelta(minutes=5)
    media.start_multipart(
        db,
        ticket,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    reserve = media.multipart_start_plan(db, ticket, context=context(), authorize=allow)
    media.publish_multipart_started(
        db,
        ticket,
        operation_token=reserve["operation_token"],
        storage_upload_id="stable-oss-id",
    )
    db.commit()
    valid = media.multipart_part_plan(
        db,
        ticket,
        MultipartPartURLs(expected_revision=3, part_numbers=[1]),
        context=context(),
        authorize=allow,
    )
    assert valid[0]["upload_id"] == "stable-oss-id" and valid[0]["size"] == 100
    with pytest.raises(CatalogError) as invalid:
        media.multipart_part_plan(
            db,
            ticket,
            MultipartPartURLs(expected_revision=3, part_numbers=[2]),
            context=context(),
            authorize=allow,
        )
    assert invalid.value.code == "media.invalid_part"


def test_upload_publication_rechecks_actor_authorization(db):
    ticket = create_ticket(db, ready=False)
    media.complete_upload(
        db,
        ticket,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    lease = jobs.claim(db, worker_id="worker", queues=["media"])
    db.commit()

    def revoked(*_):
        raise CatalogError("auth.revoked", "actor revoked", status_code=403)

    with pytest.raises(CatalogError) as denied:
        jobs.complete(
            db,
            lease,
            verify_resource=lambda session, current: media.verify_upload_resource(
                session, current, authorize=revoked
            ),
            publish=lambda *_: pytest.fail("revoked actor published"),
        )
    assert denied.value.code == "auth.revoked"
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Asset)) == 0


def test_concurrent_inbox_claim_has_one_winner(db):
    ticket = create_ticket(db)
    inbox_id = db.scalar(select(InboxItem.id).where(InboxItem.upload_id == ticket))
    barrier = Barrier(2)

    def worker(principal):
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                media.claim_inbox(
                    session,
                    inbox_id,
                    ClaimInbox(expected_revision=1),
                    context=context(principal),
                    authorize=allow,
                )
                session.commit()
                return "claimed"
            except CatalogError as error:
                session.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["account:42", "account:43"]))
    assert sorted(results) == ["claimed", "inbox.claim_conflict"]


def test_explicit_inbox_restore_can_assign_cancelled_received_upload(db):
    pid = seed(db)
    ticket = create_ticket(db)
    item = db.scalar(select(InboxItem).where(InboxItem.upload_id == ticket))
    media.cancel_upload(
        db,
        ticket,
        ExpectedRevision(expected_revision=3),
        context=context(),
        authorize=allow,
    )
    db.commit()
    assert db.get(InboxItem, item.id).status == "deleted"
    restored = media.restore_inbox(
        db,
        item.id,
        RestoreEntry(expected_deleted_revision=2, reason="用户恢复"),
        context=context(),
        authorize=allow,
    )
    db.commit()
    claim = media.claim_inbox(
        db,
        item.id,
        ClaimInbox(expected_revision=restored["data"]["revision"]),
        context=context(),
        authorize=allow,
    )
    db.commit()
    result = media.assign_inbox(
        db,
        item.id,
        AssignInbox(
            claim_revision=claim["data"]["claim_revision"],
            part_id=pid,
            expected_identity_revision=0,
            assignment_key=uuid4(),
        ),
        context=context(),
        authorize=allow,
    )
    assert result["data"]["status"] == "assigned"


def test_multipart_cancellation_has_durable_abort_job(db):
    ticket = create_ticket(db, ready=False)
    media.start_multipart(
        db,
        ticket,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    plan = media.multipart_start_plan(db, ticket, context=context(), authorize=allow)
    media.publish_multipart_started(
        db,
        ticket,
        operation_token=plan["operation_token"],
        storage_upload_id="real-multipart-id",
    )
    db.commit()
    media.cancel_upload(
        db,
        ticket,
        ExpectedRevision(expected_revision=3),
        context=context(),
        authorize=allow,
    )
    db.commit()
    lease = jobs.claim(
        db, worker_id="abort-worker", queues=["media"], kinds=["media.abort_multipart"]
    )
    assert lease.payload["storage_upload_id"] == "real-multipart-id"
    db.commit()
    result = jobs.complete(
        db,
        lease,
        verify_resource=lambda session, current: media.verify_multipart_abort_resource(
            session, current, authorize=allow
        ),
        publish=media.publish_multipart_aborted,
    )
    assert result.status == "succeeded"
    assert db.get(MultipartUpload, ticket).aborted_at is not None


def test_identity_groups_preserve_origin_order_and_hidden_hash_on_split(db):
    from app.pn_catalog.media_common import identity

    source, target = seed(db, "MERGE-SOURCE"), seed(db, "MERGE-TARGET")
    source_entry = entry(db, source, "a" * 64)
    target_entry = entry(db, target, "b" * 64)
    source_asset = db.get(GalleryEntry, source_entry).asset_id
    identity(db, [source, target])
    merged = media.merge_gallery_groups(db, source, target)
    db.commit()
    view = media.get_gallery(db, target, context=context(), authorize=allow)["data"]
    assert view["ordered_entry_ids"] == [str(target_entry), str(source_entry)]
    media.suppress_hash(
        db,
        UUID(view["group_id"]),
        SuppressHash(
            expected_gallery_revision=view["revision"],
            content_hash="a" * 64,
            reason="合并后隐藏",
        ),
        context=context(),
        authorize=allow,
    )
    db.commit()
    identity(db, [source, target])
    split = media.unmerge_gallery_groups(db, source, target, [source])
    db.commit()
    assert merged["changed"] and split["changed"]
    assert (
        media.get_gallery(db, source, context=context(), authorize=allow)["data"][
            "entries"
        ]
        == []
    )
    assert [
        row["entry_id"]
        for row in media.get_gallery(db, target, context=context(), authorize=allow)[
            "data"
        ]["entries"]
    ] == [str(target_entry)]
    original = db.get(GalleryEntry, source_entry)
    assert (
        original.origin_part_id == source
        and original.asset_id == source_asset
        and original.status == "gallery"
    )


def test_research_candidate_is_not_published_and_deleted_content_cannot_return(db):
    pid = seed(db)
    source_job = uuid4()
    image = VerifiedImage(
        "c" * 64,
        10,
        10,
        100,
        "image/jpeg",
        "etag",
        f"img/pn-research/{source_job}/0/{'c' * 64}/original",
    )
    candidate = media.publish_research_candidate(
        db,
        pid,
        image,
        source_job_id=source_job,
        image_index=0,
        expected_identity_revision=0,
        expected_gallery_revision=0,
        provenance={"source_url": "https://source.invalid/picture", "provider": "test"},
        context=context(),
        authorize=allow,
    )
    db.commit()
    assert candidate["status"] == "candidate"
    assert (
        media.get_gallery(db, pid, context=context(), authorize=allow)["data"][
            "entries"
        ]
        == []
    )
    eid = UUID(candidate["entry_id"])
    media.delete_entry(
        db,
        eid,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    with pytest.raises(CatalogError) as deleted:
        media.publish_research_candidate(
            db,
            pid,
            image,
            source_job_id=source_job,
            image_index=0,
            expected_identity_revision=0,
            expected_gallery_revision=2,
            provenance={"source_url": "https://source.invalid/picture"},
            context=context(),
            authorize=allow,
        )
    assert deleted.value.code == "media.deleted"


def test_media_job_access_checks_current_entry_visibility_and_resource_binding(db):
    pid = seed(db)
    ticket = create_ticket(db, pid)
    result = add(db, pid, ticket)
    db.commit()
    eid = UUID(result["data"]["entry_id"])
    job = SimpleNamespace(
        kind="media.thumbnail",
        resource_type="media.entry",
        resource_id=str(eid),
        part_id=pid,
    )
    media.authorize_job_resource(db, job, context=context(), authorize=allow)
    assert not db.new and not db.dirty
    job.part_id = pid + 1000
    with pytest.raises(CatalogError) as mismatch:
        media.authorize_job_resource(db, job, context=context(), authorize=allow)
    assert mismatch.value.code == "auth.forbidden"
    job.part_id = pid
    group = db.scalar(select(GalleryGroup).where(GalleryGroup.canonical_part_id == pid))
    media.suppress_hash(
        db,
        group.id,
        SuppressHash(
            expected_gallery_revision=group.revision,
            content_hash=result["data"]["content_hash"],
            reason="hide",
        ),
        context=context(),
        authorize=allow,
    )
    db.commit()
    with pytest.raises(CatalogError) as hidden:
        media.authorize_job_resource(db, job, context=context(), authorize=allow)
    assert hidden.value.code == "media.deleted"


def test_media_job_access_keeps_upload_owner_and_terminal_cleanup_visibility(db):
    ticket = create_ticket(db, ready=False)
    job = SimpleNamespace(
        kind="media.verify_upload",
        resource_type="media.upload",
        resource_id=str(ticket),
        part_id=None,
    )

    def read_only(db, ctx, action, pid):
        if action != "uploads.read":
            raise CatalogError("auth.forbidden", "no owner override", status_code=403)
        return allow()

    media.authorize_job_resource(db, job, context=context(), authorize=read_only)
    with pytest.raises(CatalogError):
        media.authorize_job_resource(
            db, job, context=context("account:other"), authorize=read_only
        )
    media.cancel_upload(
        db,
        ticket,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    media.authorize_job_resource(db, job, context=context(), authorize=read_only)
    assert not db.new and not db.dirty
    job.kind = "unexpected.worker"
    with pytest.raises(CatalogError):
        media.authorize_job_resource(db, job, context=context(), authorize=allow)


def test_legacy_identity_members_bootstrap_and_unmerge_without_existing_gallery(db):
    target, source, child = seed(db, "TARGET"), seed(db, "SOURCE"), seed(db, "CHILD")
    db.get(DimPart, source).merged_into_id = target
    db.get(DimPart, source).status = "merged"
    db.get(DimPart, child).merged_into_id = target
    db.get(DimPart, child).status = "merged"
    db.commit()
    scope = {target, source, child}
    first = media.ensure_identity_gallery_members(db, scope)
    db.commit()
    assert len(set(first.values())) == 1
    group = db.get(GalleryGroup, UUID(first[target]))
    before = group.revision
    assert media.ensure_identity_gallery_members(db, scope) == first
    assert group.revision == before
    result = media.unmerge_gallery_groups(db, source, target, [source, child])
    db.commit()
    assert result["moved_part_ids"] == sorted([source, child])
    assert (
        db.get(GalleryMember, source).group_id == db.get(GalleryMember, child).group_id
    )
    assert (
        db.get(GalleryMember, source).group_id != db.get(GalleryMember, target).group_id
    )
    # The caller has not yet updated identity facts: initialization cannot silently
    # move these now-mismatching memberships back into the old canonical group.
    with pytest.raises(CatalogError) as mismatch:
        media.ensure_identity_gallery_members(db, scope)
    assert mismatch.value.code == "catalog.merge_conflict"


def test_bootstrap_includes_premerge_compressed_children_and_rejects_incomplete_scope(
    db,
):
    source, target, child = seed(db, "SOURCE"), seed(db, "TARGET"), seed(db, "CHILD")
    db.get(DimPart, child).merged_into_id = source
    db.get(DimPart, child).status = "merged"
    db.commit()
    with pytest.raises(CatalogError):
        media.ensure_identity_gallery_members(db, {child, target})
    db.rollback()
    media.ensure_identity_gallery_members(db, {source, target, child})
    merged = media.merge_gallery_groups(db, source, target)
    db.commit()
    assert merged["moved_part_ids"] == sorted([source, child])


def test_bundle_snapshot_rechecks_hidden_generation_and_good_grade(db):
    pid = seed(db)
    good, defect = entry(db, pid, "a" * 64), entry(db, pid, "b" * 64)
    media.patch_entry(
        db,
        defect,
        PatchEntry(expected_revision=1, purpose="defect"),
        context=context(),
        authorize=allow,
    )
    db.commit()
    snapshot = media.bundle_snapshot(
        db, pid, grade="good", context=context(), authorize=allow
    )
    assert [row["entry_id"] for row in snapshot["entries"]] == [str(good)]
    assert snapshot["entries"][0]["object_key"].startswith("img/")
    assert (
        len(
            media.bundle_snapshot(
                db, pid, grade="bad", context=context(), authorize=allow
            )["entries"]
        )
        == 2
    )
    db.commit()
    media.delete_entry(
        db,
        good,
        ExpectedRevision(expected_revision=1),
        context=context(),
        authorize=allow,
    )
    db.commit()
    with pytest.raises(CatalogError):
        media.verify_bundle_snapshot(db, snapshot, context=context(), authorize=allow)
