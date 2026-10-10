"""Explorer links follow actual project access without narrowing aggregate facts."""
from datetime import date, datetime, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import event, select

from app import config
from app.config import get_settings
from app.models.maintenance_project import MaintenanceProjectUserAssignment
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysUser
from tests.boss_board_helpers import PASSWORD, client_for
from tests.test_maintenance_analytics import _line, _part, _project
from tests.test_maintenance_analytics_filters import _set_order

URL = "/api/maintenance/analytics/explorer"


def _demand(db, project, part, qty="3"):
    return _line(
        db, project, part, order_no=f"WBDD-LINK-{uuid.uuid4().hex}",
        order_date=date(2026, 9, 1), qty=Decimal(qty), cost_inc=Decimal(qty) * 10,
    )


def _client(db, username="links-reader", role="readonly", scoped=False):
    return client_for(db, username=username, role=role, overrides={
        "page_maintenance": True, "data_purchase_cost": True,
        "data_customer": True, "own_maintenance_projects_only": scoped,
    })


def _assignment(db, project, username, responsibility="primary_manager", archived=False):
    user = db.scalar(select(SysUser).where(SysUser.username == username))
    assignment = MaintenanceProjectUserAssignment(
        assignment_id=str(uuid.uuid4()), project_id=project.project_id,
        responsibility_type=responsibility, user_id=user.id, version=1,
        assigned_by="test", assignment_reason="test",
        archived_at=datetime.now(timezone.utc) if archived else None,
        archived_by="test" if archived else None,
        archive_reason="test" if archived else None,
    )
    db.add(assignment)
    db.commit()
    return assignment


def _explore(client, **params):
    response = client.get(URL, params={
        "range": "all", "metric": "effective", "dimension": "project", **params,
    })
    assert response.status_code == 200, response.text
    return response.json()


def _surfaces(payload):
    return {
        "rows": payload["rows"], "chart_rows": payload["chart_rows"],
        "focus.row": [payload["focus"]["row"]] if payload["focus"]["row"] else [],
        "focus.rows": payload["focus"]["rows"],
        "matrix.rows": payload["matrix"]["rows"],
        "matrix.columns": payload["matrix"]["columns"],
    }


def _assert_links(payload, permitted):
    for surface, rows in _surfaces(payload).items():
        for row in rows:
            assert row["can_open_project"] is (row["project_id"] in permitted), (surface, row)


def _facts(value):
    """Ignore request snapshot time and link capability when comparing business data."""
    if isinstance(value, dict):
        return {key: _facts(item) for key, item in value.items()
                if key not in {"can_open_project", "as_of"}}
    if isinstance(value, list):
        return [_facts(item) for item in value]
    return value


def test_summary_visibility_does_not_grant_detail_and_assignment_keeps_facts(db):
    project = _project(db, "LINK-SUMMARY")
    part = _part(db, "LINK-PN")
    _demand(db, project, part)
    db.commit()
    client = _client(db)

    denied = _explore(client)
    assert denied["total"] == 1
    assert Decimal(denied["summary"]["effective_qty"]) == 3
    assert Decimal(denied["summary"]["cost_inc"]) == 30
    _assert_links(denied, set())
    detail_url = f"/api/maintenance/projects/stable/{project.project_id}"
    assert client.get(detail_url).status_code == 403

    _assignment(db, project, "links-reader")
    allowed = _explore(client)
    _assert_links(allowed, {project.project_id})
    assert client.get(detail_url).status_code == 200
    assert _facts(allowed) == _facts(denied)


@pytest.mark.parametrize("grant, scoped", [
    ("owner", False), ("viewer", False), ("sales", False), ("source_sales", False),
    ("archived_owner", False), ("archived_viewer", False), ("boss", False), ("admin", False),
    ("owner", True), ("viewer", True), ("sales", True),
])
def test_flags_match_real_detail_access_for_each_permission_branch(db, grant, scoped):
    project = _project(db, "LINK-GRANT")
    part = _part(db, "LINK-GRANT-PN")
    line = _demand(db, project, part)
    db.commit()
    client = _client(db, role=grant if grant in {"boss", "admin"} else "readonly", scoped=scoped)
    if grant in {"owner", "viewer", "archived_owner", "archived_viewer"}:
        _assignment(db, project, "links-reader",
                    responsibility="viewer" if "viewer" in grant else "primary_manager",
                    archived=grant.startswith("archived_"))
    if grant in {"sales", "source_sales"}:
        user = db.scalar(select(SysUser).where(SysUser.username == "links-reader"))
        user.salesperson_name = "链接销售"
        project.salesperson = "链接销售" if grant == "sales" else "另一个销售"
        _set_order(db, line, salesperson="链接销售")
        db.commit()
        login = client.post("/api/auth/login", json={"username": user.username, "password": PASSWORD})
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"

    permitted = grant in {"owner", "viewer", "sales", "boss", "admin"}
    payload = _explore(client)
    _assert_links(payload, {project.project_id} if permitted else set())
    detail = client.get(f"/api/maintenance/projects/stable/{project.project_id}")
    assert detail.status_code == (200 if permitted else 403), detail.text
    assert Decimal(payload["summary"]["effective_qty"]) == 3


@pytest.mark.parametrize("dimension", ["pn", "project", "customer", "salesperson", "business"])
def test_all_row_surfaces_and_pages_receive_independent_project_capabilities(db, dimension):
    projects = [_project(db, f"LINK-SURFACE-{n}") for n in range(3)]
    part = _part(db, "LINK-SURFACE-PN")
    for project, qty in zip(projects, ("9", "5", "1")):
        line = _demand(db, project, part, qty)
        _set_order(db, line, end_customer="共同客户", salesperson="共同销售")
    db.commit()
    client = _client(db)
    _assignment(db, projects[0], "links-reader")
    _assignment(db, projects[2], "links-reader", responsibility="viewer")
    params = {"dimension": dimension, "page_size": 1, "top_n": 2,
              "focus_page_size": 1, "focus_page": 2}
    if dimension == "project":
        # Page 3 is outside Top 2; selected project is independently forbidden.
        params.update(page=3, focus=projects[1].project_id)
    payload = _explore(client, **params)
    _assert_links(payload, {projects[0].project_id, projects[2].project_id})
    assert all(_surfaces(payload).values())
    if dimension == "project":
        assert payload["rows"][0]["project_id"] == projects[2].project_id
        assert payload["focus"]["row"]["can_open_project"] is False
    else:
        assert payload["focus"]["page"] == 2
        assert payload["focus"]["rows"][0]["can_open_project"] is False
        assert len(payload["matrix"]["columns"]) == 3
    assert Decimal(payload["summary"]["effective_qty"]) == 15


def test_unassigned_group_never_gets_a_project_link_even_for_admin(db):
    project = _project(db, "LINK-UNASSIGNED")
    part = _part(db, "LINK-UNASSIGNED-PN")
    _demand(db, project, part)
    source_assignment = db.scalar(select(MaintenanceSourceOrderAssignment))
    source_assignment.is_active = False
    source_assignment.version += 1
    source_assignment.archived_at = datetime.now(timezone.utc)
    source_assignment.archived_by = "test"
    db.commit()
    payload = _explore(_client(db, role="admin"))
    assert payload["rows"][0]["project_id"] is None
    _assert_links(payload, set())
    assert Decimal(payload["summary"]["effective_qty"]) == 3


def test_scoped_account_without_assignment_has_no_rows_or_project_links(db):
    project = _project(db, "LINK-EMPTY-SCOPE")
    part = _part(db, "LINK-EMPTY-SCOPE-PN")
    _demand(db, project, part)
    db.commit()
    client = _client(db, scoped=True)
    payload = _explore(client, focus=project.project_id)
    assert payload["total"] == 0
    assert all(not rows for rows in _surfaces(payload).values())
    assert payload["matrix"]["cells"] == []
    assert client.get(f"/api/maintenance/projects/stable/{project.project_id}").status_code == 403


@pytest.mark.parametrize("rbac_enabled", [True, False])
def test_disabled_project_feature_disables_links_without_disabling_statistics(db, monkeypatch, rbac_enabled):
    project = _project(db, "LINK-FLAG")
    part = _part(db, "LINK-FLAG-PN")
    _demand(db, project, part)
    db.commit()
    client = _client(db, role="admin")
    monkeypatch.setattr(config, "ENABLE_RBAC", rbac_enabled)
    enabled = _explore(client)
    _assert_links(enabled, {project.project_id})
    monkeypatch.setattr(get_settings(), "maintenance_boss_dashboard_enabled", False)
    disabled = _explore(client)
    _assert_links(disabled, set())
    assert client.get(f"/api/maintenance/projects/stable/{project.project_id}").status_code == 404
    assert _facts(enabled) == _facts(disabled)


def test_project_link_permissions_use_one_query_bounded_to_returned_ids(db):
    projects = [_project(db, f"LINK-BOUNDED-{n}") for n in range(12)]
    part = _part(db, "LINK-BOUNDED-PN")
    for n, project in enumerate(projects):
        _demand(db, project, part, str(n + 1))
    db.commit()
    client = _client(db)
    _assignment(db, projects[0], "links-reader")
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT maintenance_project.project_id \nFROM maintenance_project \nWHERE"):
            statements.append((statement, parameters))

    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        payload = _explore(client, page=12, page_size=1, top_n=2, focus=projects[1].project_id)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert len(statements) == 1
    returned_ids = {row["project_id"] for rows in _surfaces(payload).values()
                    for row in rows if row["project_id"]}
    queried_values = set(statements[0][1].values())
    assert {project.project_id for project in projects} & queried_values == returned_ids
    assert len(returned_ids) == 4
    _assert_links(payload, {projects[0].project_id})
