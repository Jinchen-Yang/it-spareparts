"""维保导出下载文件名：ASCII 响应头 + RFC 5987 UTF-8 文件名。"""
from urllib.parse import quote, unquote

import pytest
from fastapi.testclient import TestClient

from app import permissions
from app.auth import hash_password
from app.main import app
from app.models.system import SysUser


def _admin_client(db) -> TestClient:
    db.add(SysUser(
        username="maintenance_export_admin",
        role="admin",
        password_hash=hash_password("pw123456"),
        is_active=True,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "maintenance_export_admin", "password": "pw123456"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def _readonly_client(db) -> TestClient:
    db.add(SysUser(
        username="maintenance_export_readonly",
        role="readonly",
        password_hash=hash_password("pw123456"),
        is_active=True,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "maintenance_export_readonly", "password": "pw123456"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def _cost_blind_maintenance_client(db) -> TestClient:
    base = permissions.effective("readonly", None)
    overrides = {"page_maintenance": True, "data_purchase_cost": False}
    effective = permissions.effective_from_snapshot(base, overrides)
    db.add(SysUser(
        username="maintenance_export_cost_blind",
        role="readonly",
        password_hash=hash_password("pw123456"),
        is_active=True,
        template_code="readonly",
        template_version=1,
        template_perms=base,
        perm_overrides=overrides,
        permissions=effective,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "maintenance_export_cost_blind", "password": "pw123456"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def test_chinese_project_lines_csv_uses_ascii_header_and_utf8_filename(db):
    client = _admin_client(db)
    project = "华北核心网维保项目"

    response = client.get("/api/maintenance/lines/export", params={"project": project})

    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    disposition.encode("ascii")
    expected_name = f"maintenance_lines_{project}.csv"
    assert disposition == (
        'attachment; filename="maintenance_lines.csv"; '
        f"filename*=UTF-8''{quote(expected_name, safe='!#$&+-.^_`|~')}"
    )


def test_chinese_contract_workbook_uses_ascii_header_and_utf8_filename(db):
    client = _admin_client(db)
    contract = "北京联通核心网维保合同"

    response = client.get(
        "/api/maintenance/export-workbook",
        params={"contract": contract},
    )

    assert response.status_code == 200, response.text
    assert response.content[:2] == b"PK"
    disposition = response.headers["content-disposition"]
    disposition.encode("ascii")
    expected_name = f"project_workbook_{contract}.xlsx"
    assert disposition == (
        'attachment; filename="project_workbook.xlsx"; '
        f"filename*=UTF-8''{quote(expected_name, safe='!#$&+-.^_`|~')}"
    )


def test_project_filename_cannot_inject_headers_or_paths(db):
    client = _admin_client(db)
    project = "华北\r\nX-Injected: yes/../../escape"

    response = client.get("/api/maintenance/lines/export", params={"project": project})

    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    disposition.encode("ascii")
    assert "\r" not in disposition and "\n" not in disposition
    encoded_name = disposition.split("filename*=UTF-8''", 1)[1]
    decoded_name = unquote(encoded_name)
    assert decoded_name.startswith("maintenance_lines_华北_")
    assert not any(char in decoded_name for char in '\r\n/\\:*?"<>|')


def test_project_summary_csv_uses_same_dual_filename_contract(db):
    client = _admin_client(db)

    response = client.get("/api/maintenance/export", params={"lifecycle": "all"})

    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == (
        'attachment; filename="maintenance_projects.csv"; '
        "filename*=UTF-8''maintenance_projects.csv"
    )


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/maintenance/export", {"lifecycle": "all"}),
        ("/api/maintenance/lines/export", {"project": "中文项目"}),
        ("/api/maintenance/export-workbook", {"contract": "中文合同"}),
    ],
)
def test_export_endpoints_keep_anonymous_401_and_no_page_403(db, path, params):
    anonymous = TestClient(app)
    assert anonymous.get(path, params=params).status_code == 401

    readonly = _readonly_client(db)
    assert readonly.get(path, params=params).status_code == 403


def test_cost_blind_user_can_export_masked_csv_but_workbook_stays_403(db):
    client = _cost_blind_maintenance_client(db)

    csv_response = client.get(
        "/api/maintenance/lines/export",
        params={"project": "中文项目"},
    )
    workbook_response = client.get(
        "/api/maintenance/export-workbook",
        params={"contract": "中文合同"},
    )

    assert csv_response.status_code == 200, csv_response.text
    assert workbook_response.status_code == 403
    assert workbook_response.json()["detail"] == "无成本及利润查看权限，不能导出项目成本工作簿"
