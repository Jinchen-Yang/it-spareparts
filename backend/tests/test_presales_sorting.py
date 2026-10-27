"""列表排序必须先于可见性分页，白名单不能接受 SQL 或受众私密字段。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.presales import inquiries
from tests.test_presales_api_visibility import _client, users  # noqa: F401


@pytest.fixture()
def sorted_inquiries(db, users):
    rows = []
    for index, count in enumerate((3, 1, 2)):
        row = inquiries.create_draft(db, users.seller.id,
            lines=[{"part_id": users.part.id, "qty": 1} for _ in range(count)])
        row.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
        row.updated_at = row.created_at
        rows.append(row)
    db.commit()
    return rows


@pytest.mark.parametrize("sort,asc", [
    ("line_count", [1, 2, 0]), ("updated_at", [0, 1, 2]),
    ("created_at", [0, 1, 2]), ("inquiry_no", [0, 1, 2]),
])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_sort_across_pages(db, users, sorted_inquiries, sort, asc, direction):
    client = _client(users.seller)
    expected = asc if direction == "asc" else list(reversed(asc))
    actual = []
    for page in range(1, 4):
        result = client.get("/api/presales/inquiries", params={
            "sort": sort, "dir": direction, "page": page, "page_size": 1,
        })
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["total"] == 3
        actual.append(data["items"][0]["id"])
    assert actual == [sorted_inquiries[index].id for index in expected]


def test_default_and_status_rank(db, users, sorted_inquiries):
    first, second, third = sorted_inquiries
    inquiries.submit(db, first.id, users.seller.id, first.version)
    inquiries.submit(db, third.id, users.seller.id, third.version)
    inquiries.close(db, third.id, users.seller.id, third.version, outcome="cancelled")
    db.commit()
    client = _client(users.seller)
    data = client.get("/api/presales/inquiries", params={"sort": "status", "dir": "asc"}).json()
    assert [row["id"] for row in data["items"]] == [second.id, first.id, third.id]
    default = client.get("/api/presales/inquiries").json()
    explicit = client.get("/api/presales/inquiries", params={"sort": "updated_at", "dir": "desc"}).json()
    assert default == explicit


@pytest.mark.parametrize("params", [
    {"sort": "updated_at desc; DROP TABLE presales_inquiry"},
    {"sort": "customer_id"}, {"sort": "supplier_id"}, {"dir": "sideways"},
])
def test_sort_whitelist(users, params):
    assert _client(users.seller).get("/api/presales/inquiries", params=params).status_code == 422


def test_sort_preserves_visibility_and_stable_ties(db, users, sorted_inquiries):
    for row in sorted_inquiries:
        row.updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.commit()
    data = _client(users.seller).get("/api/presales/inquiries", params={"sort": "updated_at", "dir": "asc"}).json()
    assert [row["id"] for row in data["items"]] == sorted((row.id for row in sorted_inquiries), reverse=True)
    hidden = _client(users.other).get("/api/presales/inquiries", params={"scope": "all", "sort": "line_count"}).json()
    assert hidden["items"] == [] and hidden["total"] == 0
