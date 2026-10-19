"""子资源读取须区分父实体不存在与父实体存在但尚无子记录。"""

import uuid

from fastapi.testclient import TestClient
import pytest

from app import permissions
from app.auth import _make_token
from app.main import app
from app.models.dimensions import DimPart
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectUserAssignment,
)
from app.models.system import SysUser


PROJECT_READS = (
    "expense-rows",
    "master-workbook/collection-plan",
    "acceptance/checklist",
    "acceptance/checklist/template",
    "purchases",
)
MISSING_PROJECT_IDS = ("0", "-1", "abc", "00000000-0000-0000-0000-000000000000")


def _client(db, *, role="admin"):
    effective = permissions.effective(role, None)
    if role == "readonly":
        effective.update(
            page_maintenance=True, page_maintenance_beta=True, data_profit=True,
        )
    user = SysUser(
        username=f"parent-read-{role}", display_name="合成读取用户", role=role,
        password_hash="unused", permissions=effective, is_active=True,
    )
    db.add(user)
    db.commit()
    token, _ = _make_token(
        role, user.username, user.display_name, perms=effective, authn="sys_user",
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.headers["Authorization"] = f"Bearer {token}"
    return client, user


def _project(db, *, project_id=None, is_active=True):
    project = MaintenanceProject(
        project_id=project_id or str(uuid.uuid4()), project_code="EMPTY-PARENT",
        display_name="无子记录的合成项目", lifecycle_status="ongoing", is_active=is_active,
    )
    db.add(project)
    db.commit()
    return project


def _url(project_id, endpoint):
    return f"/api/maintenance/projects/stable/{project_id}/{endpoint}"


def _assert_empty_project_read(response, endpoint):
    assert response.status_code == 200, response.text
    if endpoint.endswith("/template"):
        assert response.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert response.content.startswith(b"PK")
    elif endpoint == "acceptance/checklist":
        assert response.json() == {"current": None, "history": []}
    else:
        body = response.json()
        assert body["purchases" if endpoint == "purchases" else "rows"] == []
        assert body["total"] == 0


@pytest.mark.parametrize("endpoint", PROJECT_READS)
@pytest.mark.parametrize("role", ["admin", "readonly"])
def test_project_child_reads_missing_parent_return_404(db, endpoint, role):
    client, _ = _client(db, role=role)
    for project_id in MISSING_PROJECT_IDS:
        response = client.get(_url(project_id, endpoint))
        assert response.status_code == 404, response.text
        assert "项目不存在" in str(response.json()["detail"])


@pytest.mark.parametrize("endpoint", PROJECT_READS)
@pytest.mark.parametrize("legacy_identity", [False, True])
def test_project_child_reads_existing_empty_parent_return_200(db, endpoint, legacy_identity):
    client, _ = _client(db)
    project = _project(db, project_id="legacy-stable-id" if legacy_identity else None)
    _assert_empty_project_read(client.get(_url(project.project_id, endpoint)), endpoint)


@pytest.mark.parametrize("endpoint", PROJECT_READS)
def test_project_child_reads_authorized_owner_empty_parent_return_200(db, endpoint):
    client, user = _client(db, role="readonly")
    project = _project(db)
    db.add(MaintenanceProjectUserAssignment(
        assignment_id=str(uuid.uuid4()), project_id=project.project_id,
        user_id=user.id, responsibility_type="primary_manager", assigned_by="test",
        assignment_reason="合成读取权限测试",
    ))
    db.commit()
    _assert_empty_project_read(client.get(_url(project.project_id, endpoint)), endpoint)


@pytest.mark.parametrize("endpoint", PROJECT_READS)
def test_project_child_reads_preserve_scope_denial(db, endpoint):
    client, _ = _client(db, role="readonly")
    project = _project(db)
    response = client.get(_url(project.project_id, endpoint))
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("endpoint", PROJECT_READS)
def test_project_child_reads_existing_inactive_parent_is_not_missing(db, endpoint):
    client, _ = _client(db)
    project = _project(db, is_active=False)
    _assert_empty_project_read(client.get(_url(project.project_id, endpoint)), endpoint)


@pytest.mark.parametrize("part_id", (*MISSING_PROJECT_IDS, "2147483647", "2147483648", "9" * 100))
def test_part_edit_history_missing_parent_returns_404(db, part_id):
    client, _ = _client(db)
    response = client.get(f"/api/parts/master/{part_id}/edit-history")
    assert response.status_code == 404, response.text
    assert "型号不存在" in str(response.json()["detail"])


@pytest.mark.parametrize("merged", [False, True])
def test_part_edit_history_existing_without_manual_history_returns_empty(db, merged):
    client, _ = _client(db)
    target = DimPart(pn_std="MERGE-TARGET")
    db.add(target)
    db.flush()
    part = DimPart(
        pn_std="EMPTY-HISTORY", status="merged" if merged else "active",
        merged_into_id=target.id if merged else None,
    )
    db.add(part)
    db.commit()
    response = client.get(f"/api/parts/master/{part.id}/edit-history")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "history_basis": "manual_audit", "items": [], "next_before_id": None,
    }


@pytest.mark.parametrize("params", [{"limit": "abc"}, {"limit": 0}, {"before_id": 0}])
def test_part_edit_history_query_validation_still_returns_422(db, params):
    client, _ = _client(db)
    part = DimPart(pn_std="HISTORY-QUERY-VALIDATION")
    db.add(part)
    db.commit()
    response = client.get(f"/api/parts/master/{part.id}/edit-history", params=params)
    assert response.status_code == 422, response.text


def test_part_edit_history_parent_check_preserves_authentication_and_permissions(db):
    client, _ = _client(db, role="sales")
    endpoint = "/api/parts/master/abc/edit-history"
    assert client.get(endpoint).status_code == 403
    anonymous = TestClient(app, raise_server_exceptions=False)
    assert anonymous.get(endpoint).status_code == 401


@pytest.mark.parametrize("endpoint", PROJECT_READS)
def test_project_child_reads_authenticate_before_parent_lookup(db, endpoint):
    client, user = _client(db, role="readonly")
    # 页面准入不通过时，不暴露父实体存在性。
    user.permissions = {**user.permissions, "page_maintenance": False}
    db.commit()
    token, _ = _make_token(
        user.role, user.username, user.display_name,
        perms=user.permissions, authn="sys_user",
    )
    client.headers["Authorization"] = f"Bearer {token}"
    anonymous = TestClient(app, raise_server_exceptions=False)
    project = _project(db)
    for project_id in (project.project_id, MISSING_PROJECT_IDS[-1]):
        assert anonymous.get(_url(project_id, endpoint)).status_code == 401
        assert client.get(_url(project_id, endpoint)).status_code == 403
