"""只读接口的输入错误返回 HTTP 客户端错误，而非 500。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.test_maintenance_export_headers import _admin_client


@pytest.fixture
def client(db):
    authenticated = _admin_client(db)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.headers.update(authenticated.headers)
        yield client


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/maintenance/spare-part-lines.xlsx", {"from": "abc"}),
        (
            "/api/maintenance/boss-board/projects",
            {"from": "2100-12-31", "to": "2000-01-01"},
        ),
    ],
)
def test_reported_invalid_date_queries_return_422(client, path, params):
    response = client.get(path, params=params)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


DATE_ROUTES = [
    ("/api/maintenance/spare-part-lines.xlsx", "from", "to", {"range": "custom"}),
    ("/api/maintenance/boss-board/projects", "from", "to", {}),
    ("/api/maintenance/boss-board/summary", "from", "to", {}),
    *[
        (f"/api/maintenance/analytics/{path}", "date_from", "date_to", {"range": "custom"})
        for path in ("pn-ranking", "spend-trend", "explorer")
    ],
]


@pytest.mark.parametrize("path,start,end,extra", DATE_ROUTES)
@pytest.mark.parametrize("bad", ["abc", "", "2026-02-30", "0000-01-01"])
@pytest.mark.parametrize("side", ["start", "end"])
def test_invalid_date_is_a_located_validation_error(client, path, start, end, extra, bad, side):
    field = start if side == "start" else end
    params = {**extra, start: "2026-07-01", end: "2026-07-01", field: bad}
    response = client.get(path, params=params)
    assert response.status_code == 422, response.text
    errors = response.json()["detail"]
    assert any(error["loc"] == ["query", field] and error["msg"] for error in errors)


@pytest.mark.parametrize("path,start,end,extra", DATE_ROUTES)
@pytest.mark.parametrize("use_custom", [True, False])
def test_reversed_dates_rejected_even_with_a_preset(client, path, start, end, extra, use_custom):
    response = client.get(path, params={
        **(extra if use_custom else {}), start: "2100-12-31", end: "2000-01-01",
    })
    assert response.status_code == 422, response.text
    assert "不能晚于" in response.json()["detail"]


@pytest.mark.parametrize("path,start,end,extra", DATE_ROUTES)
def test_equal_dates_and_default_queries_remain_valid(client, path, start, end, extra):
    for params in ({**extra, start: "2026-07-01", end: "2026-07-01"}, {}):
        response = client.get(path, params=params)
        assert response.status_code == 200, response.text
        if path.endswith(".xlsx"):
            assert response.content.startswith(b"PK")


@pytest.mark.parametrize("path", ["projects", "summary"])
@pytest.mark.parametrize("params", [{"from": "2100-12-31"}, {"to": "2000-01-01"}])
def test_board_validates_range_after_defaulting_missing_boundary(client, path, params):
    response = client.get(f"/api/maintenance/boss-board/{path}", params=params)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "from 不能晚于 to"


@pytest.mark.parametrize("path,start,end,extra", [DATE_ROUTES[0], *DATE_ROUTES[3:]])
@pytest.mark.parametrize("side", ["start", "end", "neither"])
def test_custom_range_still_requires_both_dates(client, path, start, end, extra, side):
    params = dict(extra)
    if side != "neither":
        params[start if side == "start" else end] = "2026-07-01"
    response = client.get(path, params=params)
    assert response.status_code == 422, response.text
    assert "同时" in str(response.json()["detail"])


def test_summary_rejects_window_whose_comparison_period_underflows(client):
    response = client.get("/api/maintenance/boss-board/summary", params={
        "from": "0001-01-01", "to": "0001-01-01",
    })
    assert response.status_code == 422, response.text
    assert "环比" in response.json()["detail"]


def test_maintenance_lines_reject_reversed_dates_and_preserve_one_sided_ranges(client):
    path = "/api/maintenance/lines"
    response = client.get(path, params={
        "project": "合成空项目", "date_from": "2100-12-31", "date_to": "2000-01-01",
    })
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "date_from 不能晚于 date_to"
    for params in (
        {"date_from": "2026-07-01"},
        {"date_to": "2026-07-01"},
        {"date_from": "2026-07-01", "date_to": "2026-07-01"},
    ):
        assert client.get(path, params={"project": "合成空项目", **params}).status_code == 200
