"""型号询价记录与采购发起人展示复用现有售前权限出口。"""

from datetime import timedelta
from decimal import Decimal

from app.services.presales import common, inquiries, offers, part_history, visibility
from tests.test_presales_api_visibility import _client, _quoted, users  # noqa: F401


def test_part_history_requires_presales_permission(users):
    response = _client(users.denied).get(f"/api/presales/parts/{users.part.id}/inquiries")
    assert response.status_code == 403


def test_part_history_includes_legacy_private_records_but_excludes_drafts_and_deleted_lines(db, users):
    public, _ = _quoted(db, users)
    private, _ = _quoted(db, users, whole={"mode": "private"})
    inquiries.create_draft(db, users.seller.id, lines=[{"part_id": users.part.id}])
    removed = inquiries.create_draft(db, users.other.id, lines=[{"part_id": users.part.id}])
    inquiries.lines_for(db, removed.id)[0].status = "deleted"
    db.commit()

    client = _client(users.other)
    path = f"/api/presales/parts/{users.part.id}/inquiries"
    for page, inquiry in enumerate((private, public), start=1):
        response = client.get(path, params={"page": page, "page_size": 1})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["total"] == 2
        row, = result["items"]
        assert row["inquiry_id"] == inquiry.id and row["created_by_name"] == "seller"
        assert row["has_quote"] is True and Decimal(row["qty"]) == 2
        assert row["lowest_valid_quotes"] == [{"unit_price": "50.25", "is_tax_inclusive": True}]
        assert "customer_id" not in row and "supplier_id" not in row
    assert client.get(path, params={"page": 3, "page_size": 1}).json()["items"] == []


def test_part_history_pages_multiple_inquiries_before_building_details(db, users, monkeypatch):
    public = [_quoted(db, users)[0] for _ in range(4)]
    private, _ = _quoted(db, users, whole={"mode": "private"})
    inquiry = inquiries.create_draft(db, users.seller.id, lines=[
        {"part_id": users.part.id, "qty": 3}, {"part_id": users.part.id, "qty": 4}])
    inquiries.submit(db, inquiry.id, users.seller.id, inquiry.version)
    db.commit()
    lines = inquiries.lines_for(db, inquiry.id)
    next_page = [inquiries.lines_for(db, row.id)[0] for row in (private, public[-1])]
    detail_ids = []
    original = visibility.inquiry_view

    def view(db, inquiry, ctx):
        detail_ids.append(inquiry.id)
        return original(db, inquiry, ctx)

    monkeypatch.setattr(visibility, "inquiry_view", view)
    ctx = common.resolve_actor(db, users.other.id)[1]
    for page, size, expected_lines, expected_details in [
        (1, 1, lines[:1], [inquiry.id]),
        (2, 1, lines[1:], [inquiry.id]),
        (1, 2, lines, [inquiry.id]),
        (2, 2, next_page, [private.id, public[-1].id]),
        (5, 2, [], []),
    ]:
        detail_ids.clear()
        result = part_history.list_lines(db, users.part.id, ctx, page=page, page_size=size)
        assert result["total"] == 7
        assert [(row["inquiry_id"], row["line_id"]) for row in result["items"]] == [
            (line.inquiry_id, line.id) for line in expected_lines]
        assert detail_ids == expected_details


def test_sales_creator_and_event_labels_are_visible_to_pool_reader(db, users):
    row, _ = _quoted(db, users)
    client = _client(users.other)
    detail = client.get(f"/api/presales/inquiries/{row.id}").json()
    assert detail["created_by_name"] == "seller"
    # 旧入口生成的已完成待办仍可查到发起人。
    completed = _client(users.buyer).get("/api/work-items", params={
        "tab": "done", "subject_type": "presales_inquiry"}).json()["items"]
    assert next(item for item in completed if item["subject_id"] == row.id)["summary"]["created_by_name"] == "seller"
    timeline = client.get(f"/api/subjects/presales_inquiry/{row.id}/events").json()
    assert any(event["kind_label"] == "提交询价" for event in timeline["items"])
    assert all(event["kind_label"] and "." not in event["kind_label"] for event in timeline["items"])


def test_legacy_proactive_quote_creator_is_visible_in_detail_and_history(db, users):
    row = inquiries.create_draft(db, users.buyer.id, kind="proactive_offer", lines=[{"part_id": users.part.id}])
    line = inquiries.lines_for(db, row.id)[0]
    offers.save_draft(db, row.id, line.id, users.buyer.id,
        {"unit_price": "88", "reporter_audience": {"mode": "private"}}, expected_version=row.version)
    offers.publish_proactive(db, row.id, users.buyer.id, expected_version=row.version)
    db.commit()
    client = _client(users.other)
    detail = client.get(f"/api/presales/inquiries/{row.id}").json()
    assert detail["created_by_name"] == "buyer"
    assert detail["offers"][0]["reporter"] == {"id": users.buyer.id, "name": "buyer"}
    historical, = client.get(f"/api/presales/parts/{users.part.id}/inquiries").json()["items"]
    assert (historical["line_id"], historical["created_by_name"]) == (line.id, "buyer")


def test_part_history_ignores_unusable_quotes_and_keeps_tax_bases_separate(db, users):
    row, offer = _quoted(db, users)
    offer.is_tax_inclusive = True
    line = inquiries.lines_for(db, row.id)[0]
    second = offers.save_draft(db, row.id, line.id, users.peer.id,
        {"unit_price": "40", "is_tax_inclusive": False}, expected_version=row.version)
    offers.reply(db, row.id, users.peer.id, expected_version=row.version, on_existing="new")
    db.commit()
    path = f"/api/presales/parts/{users.part.id}/inquiries"
    result = _client(users.seller).get(path).json()["items"][0]
    assert {quote["is_tax_inclusive"] for quote in result["lowest_valid_quotes"]} == {True, False}
    # D-0046：已停用的 valid_until 不再让报价失效；标无货等状态变化才会。
    offer.valid_until = common.now() - timedelta(days=30)
    second.valid_until = common.now() - timedelta(days=30)
    db.commit()
    result = _client(users.seller).get(path).json()["items"][0]
    assert {quote["is_tax_inclusive"] for quote in result["lowest_valid_quotes"]} == {True, False}
    offer.no_stock = second.no_stock = True
    db.commit()
    result = _client(users.seller).get(path).json()["items"][0]
    assert result["has_quote"] is True and result["lowest_valid_quotes"] == []
