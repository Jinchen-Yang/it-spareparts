"""询价列表在分页前搜索，并保持客户字段与记录的可见范围。"""

from datetime import datetime, timezone

import pytest

from app.services.presales import inquiries, visibility
from tests.test_presales_api_visibility import _client, _quoted, users  # noqa: F401


@pytest.mark.parametrize("term", ["xj", "aPi-pReSaLeS", "original-code", "line-description", "master-description", "customer-raw", "customer-normalized"])
def test_list_search_matches_each_public_field_and_customer_names(db, users, term):
    users.part.description = "MASTER-DESCRIPTION"
    users.customer.name_raw = "CUSTOMER-RAW"
    users.customer.name_normalized = "CUSTOMER-NORMALIZED"
    row = inquiries.create_draft(db, users.seller.id, customer_id=users.customer.id,
        lines=[{"part_id": users.part.id, "pn_raw": "ORIGINAL-CODE", "description": "LINE-DESCRIPTION"}])
    db.commit()
    response = _client(users.seller).get("/api/presales/inquiries", params={"q": term})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1 and data["items"][0]["id"] == row.id
    assert data["items"][0]["customer_name"] == "CUSTOMER-NORMALIZED"
    assert data["items"][0]["is_owner"] is True


def test_shared_inquiry_customer_is_null_and_never_a_search_side_channel(db, users):
    row, _ = _quoted(db, users)
    other = _client(users.other)
    visible = other.get("/api/presales/inquiries", params={"scope": "all", "q": users.part.pn_std}).json()
    assert visible["total"] == 1 and visible["items"][0]["id"] == row.id
    assert visible["items"][0]["customer_name"] is None
    assert visible["items"][0]["is_owner"] is False
    assert "customer_id" not in visible["items"][0]
    hidden = other.get("/api/presales/inquiries", params={"scope": "all", "q": users.customer.name_raw}).json()
    assert hidden["items"] == [] and hidden["total"] == 0
    owner = _client(users.seller).get("/api/presales/inquiries", params={"q": users.customer.name_raw}).json()
    assert owner["items"][0]["customer_name"] == users.customer.name_raw
    supervisor = _client(users.supervisor).get("/api/presales/inquiries", params={"scope": "all", "q": users.customer.name_raw}).json()
    assert supervisor["items"][0]["customer_name"] == users.customer.name_raw


def test_customer_permission_is_required_even_for_owner_or_supervisor(db, users):
    row, _ = _quoted(db, users)
    for user in (users.seller, users.supervisor):
        user.template_perms = {**user.template_perms, "data_customer": False}
    db.commit()
    for user in (users.seller, users.supervisor):
        client = _client(user)
        visible = client.get("/api/presales/inquiries", params={"scope": "all", "q": row.inquiry_no}).json()
        assert visible["items"][0]["customer_name"] is None
        hidden = client.get("/api/presales/inquiries", params={"scope": "all", "q": users.customer.name_raw}).json()
        assert hidden["total"] == 0 and hidden["items"] == []


def test_search_and_dates_preserve_server_sort_pagination_and_visibility(db, users):
    rows = []
    moments = ["2026-01-01T15:59:59+00:00", "2026-01-01T16:00:00+00:00",
               "2026-01-02T15:59:59.999999+00:00", "2026-01-02T16:00:00+00:00"]
    for index, moment in enumerate(moments):
        row = inquiries.create_draft(db, users.seller.id,
            lines=[{"pn_raw": "SEARCH-MATCH", "qty": 1}] * (index + 1))
        row.updated_at = datetime.fromisoformat(moment)
        rows.append(row)
    hidden = inquiries.create_draft(db, users.other.id, lines=[{"pn_raw": "SEARCH-MATCH"}])
    hidden.updated_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    db.commit()
    client = _client(users.seller)
    filters = {"q": "match", "date_from": "2026-01-02", "date_to": "2026-01-02",
               "scope": "all", "kind": "sales_inquiry", "status": "draft",
               "sort": "line_count", "dir": "desc", "page_size": 1}
    for page, expected in ((1, rows[2]), (2, rows[1])):
        response = client.get("/api/presales/inquiries", params={**filters, "page": page})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 2 and [row["id"] for row in data["items"]] == [expected.id]
    assert client.get("/api/presales/inquiries", params={"date_from": "2026-01-03"}).json()["total"] == 1
    assert client.get("/api/presales/inquiries", params={"date_to": "2026-01-01"}).json()["total"] == 1


def test_search_ignores_deleted_lines_and_keeps_like_wildcards_literal(db, users):
    row = inquiries.create_draft(db, users.seller.id, lines=[{"pn_raw": "100%_RAW\\CODE"}, {"pn_raw": "DELETED-SECRET"}])
    removed = inquiries.lines_for(db, row.id)[1]
    removed.status = "deleted"
    db.commit()
    client = _client(users.seller)
    for term in ("%_", "\\CODE"):
        result = client.get("/api/presales/inquiries", params={"q": term}).json()
        assert result["total"] == 1 and result["items"][0]["id"] == row.id
    for term in ("DELETED-SECRET", "' OR 1=1 --"):
        result = client.get("/api/presales/inquiries", params={"q": term}).json()
        assert result["items"] == [] and result["total"] == 0


@pytest.mark.parametrize("params", [
    {"date_from": "2026-02-02", "date_to": "2026-01-01"},
    {"date_from": "invalid"}, {"date_to": "2026-02-30"}, {"q": "x" * 257},
])
def test_search_parameters_are_validated(users, params):
    assert _client(users.seller).get("/api/presales/inquiries", params=params).status_code == 422


def test_only_visible_page_serializes_offers_while_total_and_order_remain_accurate(db, users, monkeypatch):
    from sqlalchemy import event
    from app.db import engine

    visible = []
    for index in range(5):
        # 隐藏草稿穿插在排序中，不能先 SQL limit 再做权限筛选。
        inquiries.create_draft(db, users.other.id, lines=[{"pn_raw": f"HIDDEN-{index}"}])
        row = inquiries.create_draft(db, users.seller.id, lines=[{"pn_raw": f"VISIBLE-{index}"}])
        visible.append(row.id)
    db.commit()
    rendered, offer_reads = [], []
    original = visibility.inquiry_view
    def serialize(connection, inquiry, ctx, **kwargs):
        rendered.append(inquiry.id)
        return original(connection, inquiry, ctx, **kwargs)
    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "from presales_offer" in statement.lower():
            offer_reads.append(statement)
    monkeypatch.setattr(visibility, "inquiry_view", serialize)
    event.listen(engine, "before_cursor_execute", observe)
    try:
        client = _client(users.seller)
        params = {"scope": "all", "sort": "inquiry_no", "dir": "asc", "page": 2, "page_size": 2}
        response = client.get("/api/presales/inquiries", params=params)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 5 and [row["id"] for row in data["items"]] == visible[2:4]
        assert rendered == visible[2:4]
        page_queries = len(offer_reads)
        assert page_queries > 0
        for index in range(10):
            inquiries.create_draft(db, users.seller.id, lines=[{"pn_raw": f"LATER-{index}"}])
            inquiries.create_draft(db, users.other.id, lines=[{"pn_raw": f"LATER-HIDDEN-{index}"}])
        db.commit()
        rendered.clear()
        offer_reads.clear()
        data = client.get("/api/presales/inquiries", params=params).json()
        assert data["total"] == 15 and [row["id"] for row in data["items"]] == visible[2:4]
        assert rendered == visible[2:4] and len(offer_reads) == page_queries
        rendered.clear()
        offer_reads.clear()
        data = client.get("/api/presales/inquiries", params={**params, "page": 100}).json()
        assert data["total"] == 15 and data["items"] == []
        assert rendered == [] and offer_reads == []
    finally:
        event.remove(engine, "before_cursor_execute", observe)
