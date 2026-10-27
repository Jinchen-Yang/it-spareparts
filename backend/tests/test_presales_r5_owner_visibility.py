"""报价人私密不影响单据所有人查看自己的处理进度。"""

import pytest

from app.services.presales import inquiries, offers, visibility
from app.services.workflow import work_items
from tests.test_presales_api_visibility import _client, _ctx, users


@pytest.mark.parametrize("pool", [False, True], ids=["assigned", "claimed-pool"])
def test_owner_keeps_handler_identity_when_reporter_is_private(db, users, pool):
    assignees = [] if pool else [users.buyer.id]
    inquiry = inquiries.create_draft(db, users.seller.id,
        assignee_ids=assignees, lines=[{"part_id": users.part.id}])
    inquiries.submit(db, inquiry.id, users.seller.id, inquiry.version)
    item = work_items.list_by_subject(db, inquiries.subject(inquiry), users.seller.id)[0]
    if pool:
        work_items.claim(db, item.id, users.buyer.id, item.version)
    offer = offers.save_draft(db, inquiry.id, inquiries.lines_for(db, inquiry.id)[0].id,
        users.buyer.id, {"unit_price": "25", "reporter_audience": {"mode": "private"}},
        expected_version=inquiry.version)
    offers.reply(db, inquiry.id, users.buyer.id, expected_version=inquiry.version)
    db.commit()

    response = _client(users.seller).get(f"/api/presales/inquiries/{inquiry.id}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["assignee_ids"] == assignees
    assert detail["work_items"] == [{
        "id": item.id, "assignee_id": users.buyer.id,
        "claimed_by": users.buyer.id if pool else None,
        "status": "done", "paused": False, "version": item.version,
    }]
    assert "reporter" not in detail["offers"][0]
    assert "created_by" not in detail["offers"][0]
    assert "work_item_id" not in detail["offers"][0]
    summary = visibility.inquiry_view(db, inquiry, _ctx(db, users.seller), detail=False)
    assert summary["assignee_ids"] == assignees

    other = _client(users.other).get(f"/api/presales/inquiries/{inquiry.id}")
    assert other.status_code == 200, other.text
    shared = other.json()
    assert "assignee_ids" not in shared
    assert shared["work_items"] == []
    assert "reporter" not in shared["offers"][0]
    assert "reporter" in visibility.offer_view(db, offer, _ctx(db, users.buyer))
