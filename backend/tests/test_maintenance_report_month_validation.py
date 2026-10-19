"""项目经理月度工作簿拒绝不存在的日历月份，保留空值默认月份。"""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api import maintenance_manager_workbooks as api
from tests.test_maintenance_manager_workbooks_api import _client


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/maintenance/project-manager/workbooks/v3/status"),
        ("GET", "/api/maintenance/project-manager/workbooks/v3"),
        ("POST", "/api/maintenance/project-manager/workbooks/v3/validate"),
    ],
)
def test_manager_workbook_invalid_month_returns_422_before_adapter(db, monkeypatch, method, path):
    def forbidden_adapter(*_args, **_kwargs):
        pytest.fail("无效月份不得构造工作簿适配器")

    monkeypatch.setattr(api, "_adapter", forbidden_adapter)
    monkeypatch.setattr(api, "business_today", lambda: date(2026, 10, 19))
    authenticated, _user = _client(db, username="invalid-month-admin")
    with TestClient(authenticated.app, raise_server_exceptions=False) as client:
        client.headers.update(authenticated.headers)
        for month in ("0000-01", "０００１-01", "abc", "2026-00", "2026-13", "2026-11"):
            response = client.request(method, path, params={"report_month": month})

            assert response.status_code == 422, (method, path, month, response.text)
            assert response.json()["detail"]


@pytest.mark.parametrize("month", [None, "", "  ", "2026-10", "2024-02"])
def test_manager_workbook_month_defaults_and_valid_months(db, monkeypatch, month):
    seen = []

    class Adapter:
        def status(self, report_month):
            seen.append(report_month)
            return {"report_month": report_month.isoformat()}

    monkeypatch.setattr(api, "_adapter", lambda *_args, **_kwargs: Adapter())
    monkeypatch.setattr(api, "business_today", lambda: date(2026, 10, 19))
    client, _user = _client(db, username="valid-month-admin")

    response = client.get(
        "/api/maintenance/project-manager/workbooks/v3/status",
        params={} if month is None else {"report_month": month},
    )

    assert response.status_code == 200, response.text
    expected = date(2024, 2, 1) if month == "2024-02" else date(2026, 10, 1)
    assert seen == [expected]
