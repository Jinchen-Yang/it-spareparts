"""老板看板所有日期查询共享 HTTP 校验，保留有效空窗口。"""

import pytest

from app.models.inventory import PartPool
from tests.test_maintenance_export_headers import _admin_client


PATHS = [
    "/api/dashboard/price-discipline-summary",
    "/api/dashboard/kpi",
    "/api/dashboard/sales",
    "/api/dashboard/purchase-orders",
    "/api/dashboard/trend",
    "/api/dashboard/part-ranking",
    "/api/dashboard/pools",
    "/api/dashboard/pool/1",
]


@pytest.fixture()
def client(db):
    db.add(PartPool(group_id=1, name="日期校验空池"))
    db.commit()
    return _admin_client(db)


@pytest.mark.parametrize("path", PATHS)
def test_dashboard_rejects_reversed_date_range(client, path):
    response = client.get(
        path,
        params={"date_from": "2100-12-31", "date_to": "2000-01-01"},
    )

    assert response.status_code == 422, response.text
    assert "date_from" in response.json()["detail"]
    assert "date_to" in response.json()["detail"]


@pytest.mark.parametrize("path", PATHS)
def test_dashboard_rejects_malformed_and_empty_dates(client, path):
    for field in ("date_from", "date_to"):
        for value in ("abc", "", " ", "2026-02-30", "0000-01-01"):
            response = client.get(path, params={field: value})

            assert response.status_code == 422, (path, field, value, response.text)
            assert any(
                error["loc"] == ["query", field]
                for error in response.json()["detail"]
            )


@pytest.mark.parametrize("path", PATHS)
def test_dashboard_accepts_equal_dates_and_default_window(client, path):
    for params in (
        {},
        {"date_from": "2024-02-29", "date_to": "2024-02-29"},
    ):
        response = client.get(path, params=params)

        assert response.status_code == 200, (path, params, response.text)


@pytest.mark.parametrize("path", PATHS)
def test_dashboard_preserves_endpoint_specific_one_sided_windows(client, path):
    for params in ({"date_from": "2024-02-29"}, {"date_to": "2024-02-29"}):
        response = client.get(path, params=params)

        expected = 422 if path.endswith("price-discipline-summary") else 200
        assert response.status_code == expected, (path, params, response.text)
