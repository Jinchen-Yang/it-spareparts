"""氚云项目导入服务契约：预览零写入、应用幂等、冲突 409（PR5B）。"""

import io
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import permissions
from app.auth import hash_password
from app.main import app
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_import import (
    MaintenanceProjectImportBatch,
    MaintenanceProjectSourceLink,
)
from app.models.system import SysUser


_PASSWORD = "synthetic-password-123"


@pytest.fixture(autouse=True)
def _maintenance_beta_enabled(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "maintenance_beta_enabled", True)


def _admin_client(db) -> TestClient:
    db.add(
        SysUser(
            username="import_admin",
            role="admin",
            display_name="合成导入管理员",
            password_hash=hash_password(_PASSWORD),
            template_code="admin",
            template_version=1,
            template_perms={
                **permissions.admin_account_defaults(),
                "page_maintenance_beta": True,
            },
        )
    )
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "import_admin", "password": _PASSWORD},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client




def test_invalid_content_fails_without_project_writes(db):
    """Non-workbook content surfaces an error and writes no projects."""
    from app.services import maintenance_project_imports as svc

    result = svc.preview_import(
        db,
        file_content=b"SYNTHETIC-NOT-XLS",
        filename="tritium-sample.xls",
        operated_by="import_admin",
    )
    assert result["status"] == "error"
    assert list(db.scalars(select(MaintenanceProject)).all()) == []


def test_import_sample_fixture_parses(db):
    """The committed de-identified tritium fixture parses to preview rows."""
    import os
    from app.services import maintenance_project_imports as svc
    fixture = os.path.join(
        os.path.dirname(__file__), "fixtures", "tritium_project_sample.xls"
    )
    if not os.path.exists(fixture):
        pytest.skip("tritium fixture not committed")
    with open(fixture, "rb") as fh:
        content = fh.read()
    result = svc.preview_import(
        db, file_content=content, filename="tritium-sample.xls",
        operated_by="import_admin",
    )
    assert result["status"] == "preview"
    assert result["row_count"] >= 3
    # full rows must be stored in preview_json (not truncated)
    batch = db.get(MaintenanceProjectImportBatch, result["import_id"])
    assert len(batch.preview_json["new_projects"]) == result["new_count"]


def test_apply_twice_is_conflict_free_and_updates(db):
    """Apply creates projects; a second preview of the same XSDDs marks them
    updated, and applying that batch updates rather than duplicates."""
    import os
    from app.services import maintenance_project_imports as svc
    fixture = os.path.join(
        os.path.dirname(__file__), "fixtures", "tritium_project_sample.xls"
    )
    if not os.path.exists(fixture):
        pytest.skip("tritium fixture not committed")
    with open(fixture, "rb") as fh:
        content = fh.read()

    first = svc.preview_import(
        db, file_content=content, filename="tritium-sample.xls",
        operated_by="import_admin",
    )
    applied = svc.apply_import(db, first["import_id"], "import_admin")
    assert applied["created"] == first["new_count"]

    before = len(list(db.scalars(select(MaintenanceProject)).all()))

    second = svc.preview_import(
        db, file_content=content, filename="tritium-sample.xls",
        operated_by="import_admin",
    )
    assert second["new_count"] == 0
    assert second["updated_count"] == first["new_count"]
    reapplied = svc.apply_import(db, second["import_id"], "import_admin")
    assert reapplied["created"] == 0
    assert reapplied["updated"] == first["new_count"]

    after = len(list(db.scalars(select(MaintenanceProject)).all()))
    assert after == before

    links = list(db.scalars(select(MaintenanceProjectSourceLink)).all())
    assert len(links) == first["new_count"]


def test_preview_makes_zero_project_writes(db):
    import os
    from app.services import maintenance_project_imports as svc
    fixture = os.path.join(
        os.path.dirname(__file__), "fixtures", "tritium_project_sample.xls"
    )
    if not os.path.exists(fixture):
        pytest.skip("tritium fixture not committed")
    with open(fixture, "rb") as fh:
        content = fh.read()
    svc.preview_import(
        db, file_content=content, filename="tritium-sample.xls",
        operated_by="import_admin",
    )
    assert list(db.scalars(select(MaintenanceProject)).all()) == []
