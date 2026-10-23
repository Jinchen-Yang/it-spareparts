"""Out-of-order cloud completions never become fabricated capture order."""
from uuid import UUID, uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.pn_catalog import media
from app.db import engine
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_dto import AddInboxGroupItem, ArrangeInboxGroup, CreateInboxGroup
from app.pn_catalog.media_models import InboxItem
from tests.test_pn_catalog_media import allow, context, create_ticket


def group(db):
    value = media.create_inbox_group(db, CreateInboxGroup(client_group_id=uuid4(), capture_mode="same_item"),
        context=context(), authorize=allow)
    db.commit()
    return UUID(value["data"]["group_id"])


def add(db, gid, ticket, capture, revision, *, ctx=None):
    value = media.add_group_item(db, gid, AddInboxGroupItem(upload_id=ticket,
        position=capture, client_order=capture, expected_group_revision=revision),
        context=ctx or context(), authorize=allow)
    db.commit()
    return value["data"]


def test_second_upload_finishes_first_but_group_retains_original_capture_order(db):
    gid = group(db)
    first, second = create_ticket(db), create_ticket(db)
    two = add(db, gid, second, 1, 1)
    one = add(db, gid, first, 0, two["revision"])
    ordered = db.scalars(select(InboxItem).where(InboxItem.group_id == gid).order_by(InboxItem.position)).all()
    assert [item.upload_id for item in ordered] == [first, second]
    assert [item.client_order for item in ordered] == [0, 1]
    assert [item.position for item in ordered] == [0, 1]
    assert one["ordered_item_ids"] == [str(item.id) for item in ordered]


def test_cross_device_manual_arrangement_does_not_change_capture_evidence_or_get_replayed_away(db):
    gid = group(db)
    tickets = [create_ticket(db) for _ in range(3)]
    add(db, gid, tickets[1], 1, 1)
    state = add(db, gid, tickets[0], 0, 2)
    other_device = context().model_copy(update={"actor": context().actor.model_copy(update={"client_id": "itsp", "entrypoint": "itsp_ui"})})
    arranged = media.arrange_inbox_group(db, gid, ArrangeInboxGroup(expected_revision=state["revision"],
        ordered_item_ids=[UUID(value) for value in reversed(state["ordered_item_ids"])]), context=other_device, authorize=allow)["data"]
    db.commit()
    # Retrying an already enrolled capture must not overwrite a later human arrangement.
    repeated = add(db, gid, tickets[0], 0, arranged["revision"])
    assert repeated["ordered_item_ids"] == arranged["ordered_item_ids"]
    final = add(db, gid, tickets[2], 2, repeated["revision"], ctx=other_device.model_copy(update={"request_id": uuid4()}))
    rows = db.scalars(select(InboxItem).where(InboxItem.group_id == gid).order_by(InboxItem.position)).all()
    assert [row.client_order for row in rows] == [1, 0, 2]
    assert final["ordered_item_ids"][:2] == arranged["ordered_item_ids"]


def test_capture_order_collision_and_change_are_explicit_conflicts(db):
    gid = group(db)
    first, second = create_ticket(db), create_ticket(db)
    state = add(db, gid, first, 0, 1)
    with pytest.raises(CatalogError) as error:
        add(db, gid, second, 0, state["revision"])
    assert error.value.code == "inbox.group_conflict"

    db.rollback()
    with pytest.raises(CatalogError) as error:
        add(db, gid, first, 1, state["revision"])
    assert error.value.code == "inbox.group_conflict"


def test_two_devices_add_and_arrange_from_one_baseline_have_only_one_winner(db):
    gid = group(db)
    tickets = [create_ticket(db) for _ in range(3)]
    add(db, gid, tickets[0], 0, 1)
    state = add(db, gid, tickets[1], 1, 2)
    barrier = Barrier(2)
    def write(kind):
        with Session(engine) as session:
            actor = context().model_copy(update={"actor": context().actor.model_copy(update={"client_id": kind})})
            barrier.wait(timeout=5)
            try:
                if kind == "shop":
                    media.add_group_item(session, gid, AddInboxGroupItem(upload_id=tickets[2],
                        position=2, client_order=2, expected_group_revision=state["revision"]), context=actor, authorize=allow)
                else:
                    media.arrange_inbox_group(session, gid, ArrangeInboxGroup(expected_revision=state["revision"],
                        ordered_item_ids=[UUID(value) for value in reversed(state["ordered_item_ids"])]), context=actor, authorize=allow)
                session.commit()
                return kind, "applied"
            except CatalogError as error:
                session.rollback()
                return kind, error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(pool.map(write, ["shop", "itsp"]))
    assert sorted(results.values()) == ["applied", "inbox.group_conflict"]
    rows = db.scalars(select(InboxItem).where(InboxItem.group_id == gid).order_by(InboxItem.position)
        .execution_options(populate_existing=True)).all()
    assert [row.client_order for row in rows] == ([0, 1, 2] if results["shop"] == "applied" else [1, 0])
