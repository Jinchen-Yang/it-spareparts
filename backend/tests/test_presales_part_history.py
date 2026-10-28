"""型号询价记录与采购发起人展示复用现有售前权限出口。"""

from datetime import timedelta
from decimal import Decimal

from app.services.presales import common, inquiries, offers, part_history, visibility
from tests.test_presales_api_visibility import _client, _quoted, users  # noqa: F401


def test_part_history_uses_visibility_before_pagination_and_never_leaks_private_rows(db, users):
    visible, offer = _quoted(db, users)
    private, _ = _quoted(db, users, whole={"mode": "private"})
    draft = inquiries.create_draft(db, users.seller.id, lines=[{"part_id": users.part.id}])
    removed = inquiries.create_draft(db, users.other.id, lines=[{"part_id": users.part.id}])
    inquiries.lines_for(db, removed.id)[0].status = "deleted"
    offer.is_tax_inclusive = True
    db.commit()
    response = _client(users.other).get(f"/api/presales/parts/{users.part.id}/inquiries", params={"page_size": 1})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total"] == 1
    row = result["items"][0]
    assert row["inquiry_id"] == visible.id and row["inquiry_id"] not in {private.id, draft.id, removed.id}
    assert row["created_by_name"] == "seller"
    assert row["has_quote"] is True and Decimal(row["qty"]) == 2
    assert row["lowest_valid_quotes"] == [{"unit_price": "50.25", "is_tax_inclusive": True}]
    assert "customer_id" not in row and "supplier_id" not in row
    assert _client(users.other).get(f"/api/presales/parts/{users.part.id}/inquiries", params={"page": 2, "page_size": 1}).json()["items"] == []


def test_part_history_requires_presales_permission(users):
    response = _client(users.denied).get(f"/api/presales/parts/{users.part.id}/inquiries")
    assert response.status_code == 403


def test_part_history_pages_multiple_inquiries_before_building_details(db, users, monkeypatch):
    visible = [_quoted(db, users)[0] for _ in range(4)]
    _quoted(db, users, whole={"mode": "private"})
    # 同一询价的多行也占独立分页位置。
    inquiry = inquiries.create_draft(db, users.seller.id, lines=[
        {"part_id": users.part.id, "qty": 3}, {"part_id": users.part.id, "qty": 4}])
    inquiries.submit(db, inquiry.id, users.seller.id, inquiry.version)
    db.commit()
    detail_ids = []
    original = visibility.inquiry_view

    def view(db, inquiry, ctx):
        detail_ids.append(inquiry.id)
        return original(db, inquiry, ctx)

    monkeypatch.setattr(visibility, "inquiry_view", view)
    ctx = common.resolve_actor(db, users.other.id)[1]
    first = part_history.list_lines(db, users.part.id, ctx, page=1, page_size=1)
    assert first["total"] == 6
    assert first["items"][0]["inquiry_id"] == inquiry.id
    assert detail_ids == [inquiry.id]
    detail_ids.clear()
    second = part_history.list_lines(db, users.part.id, ctx, page=2, page_size=1)
    assert second["total"] == 6
    assert [row["inquiry_id"] for row in second["items"]] == [inquiry.id]
    assert len({row["line_id"] for row in first["items"] + second["items"]}) == 2
    assert detail_ids == [inquiry.id]
    detail_ids.clear()
    combined = part_history.list_lines(db, users.part.id, ctx, page=1, page_size=2)
    assert [row["line_id"] for row in combined["items"]] == [row["line_id"] for row in first["items"] + second["items"]]
    assert detail_ids == [inquiry.id]
    detail_ids.clear()
    following = part_history.list_lines(db, users.part.id, ctx, page=2, page_size=2)
    assert [row["inquiry_id"] for row in following["items"]] == [visible[-1].id, visible[-2].id]
    assert detail_ids == [visible[-1].id, visible[-2].id]
    detail_ids.clear()
    assert part_history.list_lines(db, users.part.id, ctx, page=10, page_size=2)["items"] == []
    assert detail_ids == []


def test_part_history_ignores_expired_quotes_and_keeps_tax_bases_separate(db, users):
    row, offer = _quoted(db, users)
    offer.is_tax_inclusive = True
    line = inquiries.lines_for(db, row.id)[0]
    second = offers.save_draft(db, row.id, line.id, users.peer.id,
        {"unit_price": "40", "is_tax_inclusive": False, "validity_days": 3}, expected_version=row.version)
    offers.reply(db, row.id, users.peer.id, expected_version=row.version, on_existing="new")
    db.commit()
    path = f"/api/presales/parts/{users.part.id}/inquiries"
    result = _client(users.seller).get(path).json()["items"][0]
    assert {quote["is_tax_inclusive"] for quote in result["lowest_valid_quotes"]} == {True, False}
    offer.valid_until = common.now() - timedelta(seconds=1)
    second.valid_until = common.now() - timedelta(seconds=1)
    db.commit()
    result = _client(users.seller).get(path).json()["items"][0]
    assert result["has_quote"] is True and result["lowest_valid_quotes"] == []


def test_sales_creator_name_is_returned_for_assigned_buyer_and_work_summary(db, users):
    row, _ = _quoted(db, users)
    client = _client(users.buyer)
    detail = client.get(f"/api/presales/inquiries/{row.id}").json()
    assert detail["created_by_name"] == "seller"
    assert "created_by" not in detail
    queue = client.get("/api/work-items", params={"tab": "done", "subject_type": "presales_inquiry"}).json()
    assert queue["items"][0]["summary"]["created_by_name"] == "seller"
    timeline = _client(users.seller).get(f"/api/subjects/presales_inquiry/{row.id}/events").json()
    assert any(event["kind_label"] == "提交询价" for event in timeline["items"])
    assert all(event["kind_label"] and "." not in event["kind_label"] for event in timeline["items"])


def test_proactive_creator_name_does_not_bypass_hidden_reporter(db, users):
    row = inquiries.create_draft(db, users.buyer.id, kind="proactive_offer", lines=[{"part_id": users.part.id}])
    line = inquiries.lines_for(db, row.id)[0]
    offers.save_draft(db, row.id, line.id, users.buyer.id,
        {"unit_price": "88", "reporter_audience": {"mode": "private"}}, expected_version=row.version)
    offers.publish_proactive(db, row.id, users.buyer.id, expected_version=row.version, notice_user_ids=[users.other.id])
    db.commit()
    detail = _client(users.other).get(f"/api/presales/inquiries/{row.id}").json()
    assert "created_by_name" not in detail and "created_by" not in detail
    history = _client(users.other).get(f"/api/presales/parts/{users.part.id}/inquiries").json()["items"][0]
    assert "created_by_name" not in history
    queue = _client(users.other).get("/api/work-items", params={"tab": "assigned", "subject_type": "presales_offer_notice"}).json()
    assert "created_by_name" not in queue["items"][0]["summary"]
