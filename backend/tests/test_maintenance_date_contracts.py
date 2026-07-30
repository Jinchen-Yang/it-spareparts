"""维保数据页和 CSV 的统一日期范围公开契约。"""

from __future__ import annotations

import pytest

from app.services import maintenance_cost
from tests.test_maintenance_export_headers import _admin_client


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"date_from": "2026-07-01"}, "date_from 与 date_to 必须同时提供"),
        ({"date_to": "2026-07-31"}, "date_from 与 date_to 必须同时提供"),
        (
            {"date_from": "2026-07-31", "date_to": "2026-07-01"},
            "date_from 不能晚于 date_to",
        ),
    ],
)
@pytest.mark.parametrize(
    "path",
    [
        "/api/maintenance/projects",
        "/api/maintenance/export",
        "/api/maintenance/board",
        "/api/maintenance/board/export",
    ],
)
def test_maintenance_data_and_csv_routes_reject_invalid_date_pairs_before_building(
    db,
    monkeypatch,
    path,
    params,
    detail,
):
    calls = 0

    def forbidden_builder(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("invalid date pair must not reach a data builder")

    monkeypatch.setattr(maintenance_cost, "projects_aggregate", forbidden_builder)
    monkeypatch.setattr(maintenance_cost, "board", forbidden_builder)
    client = _admin_client(db)

    response = client.get(path, params=params)

    assert response.status_code == 422
    assert response.json()["detail"] == detail
    assert calls == 0
