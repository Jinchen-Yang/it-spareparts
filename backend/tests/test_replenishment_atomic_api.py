"""Public contracts for project-bound atomic replenishment (#260)."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import BytesIO
from threading import Barrier, Event
from uuid import uuid4

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app import permissions
from app.auth import hash_password
from app.config import get_settings
from app.db import engine
from app.main import app
from app.models.dimensions import DimPart
from app.models.inventory import PartPool, PartPoolMember, PartPoolPricePolicy
from app.models.maintenance_project import MaintenanceProject
from app.models.replenishment import (
    ReplenishmentApplication,
    ReplenishmentApplicationLine,
    ReplenishmentApplicationVersion,
    ReplenishmentAuditEvent,
)
from app.models.system import SysUser
from app.services import pool_price_analysis, replenishment_screening


_PASSWORD = "synthetic-replenishment-atomic-password"


def _admin_client(db, *, username: str) -> TestClient:
    template = permissions.effective("admin", None)
    template["page_replenishment_beta"] = False
    db.add(
        SysUser(
            username=username,
            password_hash=hash_password(_PASSWORD),
            role="admin",
            display_name="补库原子提交管理员",
            salesperson_name="销售甲",
            is_active=True,
            template_code="admin",
            template_version=1,
            template_perms=template,
            perm_overrides={"page_replenishment_beta": True},
        )
    )
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _sales_client(
    db, *, username: str, salesperson_name: str | None
) -> TestClient:
    template = permissions.effective("sales", None)
    template["page_replenishment_beta"] = False
    db.add(
        SysUser(
            username=username,
            password_hash=hash_password(_PASSWORD),
            role="sales",
            display_name="补库原子提交销售",
            salesperson_name=salesperson_name,
            is_active=True,
            template_code="sales",
            template_version=1,
            template_perms=template,
            perm_overrides={
                "page_replenishment_beta": True,
                "action_replenishment_create": True,
                "data_pool_price_governance": True,
            },
        )
    )
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _project(db, *, salesperson: str = "销售甲") -> MaintenanceProject:
    project = MaintenanceProject(
        project_id=str(uuid4()),
        project_code=f"REPL-{uuid4().hex[:8].upper()}",
        display_name="补库原子提交合成项目",
        salesperson=salesperson,
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add(project)
    db.commit()
    return project


def _get(client: TestClient, path: str):
    settings = get_settings()
    original = settings.replenishment_beta_enabled
    try:
        settings.replenishment_beta_enabled = True
        return client.get(path)
    finally:
        settings.replenishment_beta_enabled = original


def _post(client: TestClient, payload: dict):
    settings = get_settings()
    original = settings.replenishment_beta_enabled
    try:
        settings.replenishment_beta_enabled = True
        return client.post("/api/replenishment-beta/applications", json=payload)
    finally:
        settings.replenishment_beta_enabled = original


def test_named_admin_atomically_submits_one_project_cart(db):
    project = _project(db)
    part = DimPart(pn_std="REPL-ATOMIC-PN-001", status="active")
    db.add(part)
    db.commit()
    client = _admin_client(db, username="replenishment_atomic_admin")

    response = _post(
        client,
        {
            "client_request_id": str(uuid4()),
            "project_id": project.project_id,
            "request_note": "仅用于合成公开接口测试",
            "lines": [
                {"part_id": part.id, "quantity": 2, "special_note": None}
            ],
        },
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["project"]["project_id"] == project.project_id
    assert payload["status"] == "submitted"
    assert payload["workflow_mode"] == "system_screening"
    assert payload["stage"] == "screening_complete"
    assert payload["versions"][0]["status"] == "submitted"
    assert payload["versions"][0]["lines"][0]["quantity"] == 2
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplication)) == 1
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationVersion)) == 1
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationLine)) == 1
    audit = db.scalar(select(ReplenishmentAuditEvent))
    assert audit is not None
    assert audit.after_json["project_id"] == project.project_id


def test_atomic_submission_reuses_one_price_fact_snapshot(db, monkeypatch):
    project = _project(db)
    part = DimPart(pn_std="REPL-ONE-FACT-SNAPSHOT", status="active")
    db.add(part)
    db.commit()
    client = _admin_client(db, username="replenishment_one_fact_admin")
    calls = []
    snapshots = [
        {
            part.id: {
                "purchase": {
                    "weighted_avg": 25.0,
                    "total_qty": 2.0,
                    "order_count": 1,
                    "line_count": 1,
                    "latest_date": "2026-08-17",
                },
                "sales": None,
            }
        },
        {part.id: {"purchase": None, "sales": None}},
    ]

    def changing_price_facts(*_args, **_kwargs):
        calls.append(1)
        return snapshots[min(len(calls) - 1, 1)]

    monkeypatch.setattr(
        pool_price_analysis, "aggregate_part_price_facts", changing_price_facts
    )
    response = _post(
        client,
        {
            "client_request_id": str(uuid4()),
            "project_id": project.project_id,
            "request_note": None,
            "lines": [{"part_id": part.id, "quantity": 1, "special_note": None}],
        },
    )

    assert response.status_code == 201, response.text
    line = response.json()["versions"][0]["lines"][0]
    activity = next(
        check
        for check in line["screening"]["checks"]
        if check["key"] == "recent_activity"
    )
    assert len(calls) == 1
    assert line["purchase"]["order_count"] == 1
    assert activity["detail"]["purchase_samples"] == 1


def test_capabilities_exposes_system_screening_without_review_action(db):
    client = _admin_client(db, username="replenishment_capabilities_admin")

    response = _get(client, "/api/replenishment-beta/capabilities")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["can_review"] is False
    assert payload["workflow_mode"] == "system_screening"
    assert payload["stage"] == "screening_complete"
    assert "人工审批" not in payload["data_contract"]


def test_admin_project_picker_lists_all_and_only_active_projects(db):
    active = _project(db)
    db.add(
        MaintenanceProject(
            project_id=str(uuid4()),
            project_code="REPL-INACTIVE",
            display_name="停用项目",
            salesperson="销售乙",
            lifecycle_status="completed",
            is_active=False,
        )
    )
    db.commit()
    client = _admin_client(db, username="replenishment_project_admin")

    response = _get(client, "/api/replenishment-beta/projects")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [
            {
                "project_id": active.project_id,
                "project_code": active.project_code,
                "display_name": active.display_name,
            }
        ]
    }


def test_sales_project_picker_uses_exact_salesperson_mapping(db):
    exact = _project(db, salesperson="销售甲")
    db.add_all(
        [
            MaintenanceProject(
                project_id=str(uuid4()),
                project_code="REPL-OTHER-SALES",
                display_name="其他销售项目",
                salesperson="销售乙",
                lifecycle_status="ongoing",
                is_active=True,
            ),
            MaintenanceProject(
                project_id=str(uuid4()),
                project_code="REPL-WHITESPACE-MISMATCH",
                display_name="空格不匹配项目",
                salesperson="销售甲 ",
                lifecycle_status="ongoing",
                is_active=True,
            ),
        ]
    )
    db.commit()
    client = _sales_client(
        db, username="replenishment_project_sales", salesperson_name="销售甲"
    )

    response = _get(client, "/api/replenishment-beta/projects")

    assert response.status_code == 200, response.text
    assert [item["project_id"] for item in response.json()["items"]] == [
        exact.project_id
    ]


def test_sales_without_mapping_sees_no_projects_and_submit_fails_closed(db):
    project = _project(db, salesperson="销售甲")
    part = DimPart(pn_std="REPL-UNMAPPED-SALES-PN", status="active")
    db.add(part)
    db.commit()
    client = _sales_client(
        db, username="replenishment_unmapped_sales", salesperson_name=None
    )
    payload = {
        "client_request_id": str(uuid4()),
        "project_id": project.project_id,
        "request_note": None,
        "lines": [{"part_id": part.id, "quantity": 1, "special_note": None}],
    }

    listing = _get(client, "/api/replenishment-beta/projects")
    existing = _post(client, payload)
    missing = _post(client, {**payload, "project_id": str(uuid4())})

    assert listing.status_code == 200
    assert listing.json() == {"items": []}
    assert existing.status_code == missing.status_code == 404
    assert existing.json() == missing.json()
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplication)) == 0


def test_same_owner_and_request_replays_without_duplicate_business_rows(db):
    project = _project(db)
    part = DimPart(pn_std="REPL-IDEMPOTENT-PN", status="active")
    db.add(part)
    db.commit()
    client = _admin_client(db, username="replenishment_idempotent_admin")
    payload = {
        "client_request_id": str(uuid4()),
        "project_id": project.project_id,
        "request_note": "顺序重试",
        "lines": [{"part_id": part.id, "quantity": 3, "special_note": None}],
    }

    first = _post(client, payload)
    second = _post(client, payload)

    assert first.status_code == second.status_code == 201
    assert first.json()["idempotent"] is False
    assert second.json()["idempotent"] is True
    assert first.json()["application_id"] == second.json()["application_id"]
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplication)) == 1
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationVersion)) == 1
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationLine)) == 1
    assert db.scalar(select(func.count()).select_from(ReplenishmentAuditEvent)) == 1


def test_same_owner_and_request_with_different_payload_is_conflict(db):
    project = _project(db)
    part = DimPart(pn_std="REPL-IDEMPOTENCY-CONFLICT-PN", status="active")
    db.add(part)
    db.commit()
    client = _admin_client(db, username="replenishment_conflict_admin")
    request_id = str(uuid4())
    payload = {
        "client_request_id": request_id,
        "project_id": project.project_id,
        "request_note": None,
        "lines": [{"part_id": part.id, "quantity": 1, "special_note": None}],
    }

    first = _post(client, payload)
    changed = _post(
        client,
        {
            **payload,
            "lines": [{"part_id": part.id, "quantity": 2, "special_note": None}],
        },
    )

    assert first.status_code == 201
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "idempotency_conflict"
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplication)) == 1
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationVersion)) == 1
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationLine)) == 1
    assert db.scalar(select(func.count()).select_from(ReplenishmentAuditEvent)) == 1


def test_validation_failure_leaves_zero_business_rows(db):
    project = _project(db)
    part = DimPart(pn_std="REPL-DUPLICATE-PN", status="active")
    db.add(part)
    db.commit()
    client = _admin_client(db, username="replenishment_atomic_failure_admin")

    response = _post(
        client,
        {
            "client_request_id": str(uuid4()),
            "project_id": project.project_id,
            "request_note": None,
            "lines": [
                {"part_id": part.id, "quantity": 1, "special_note": None},
                {"part_id": part.id, "quantity": 2, "special_note": None},
            ],
        },
    )

    assert response.status_code == 409
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplication)) == 0
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationVersion)) == 0
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationLine)) == 0
    assert db.scalar(select(func.count()).select_from(ReplenishmentAuditEvent)) == 0


def test_fractional_quantity_is_rejected_before_any_business_write(db):
    project = _project(db)
    part = DimPart(pn_std="REPL-FRACTIONAL-PN", status="active")
    db.add(part)
    db.commit()
    client = _admin_client(db, username="replenishment_fractional_admin")

    response = _post(
        client,
        {
            "client_request_id": str(uuid4()),
            "project_id": project.project_id,
            "request_note": None,
            "lines": [
                {"part_id": part.id, "quantity": 1.5, "special_note": None}
            ],
        },
    )

    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplication)) == 0
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationVersion)) == 0
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationLine)) == 0
    assert db.scalar(select(func.count()).select_from(ReplenishmentAuditEvent)) == 0


def test_concurrent_same_owner_and_request_returns_one_application(db):
    project = _project(db)
    part = DimPart(pn_std="REPL-CONCURRENT-PN", status="active")
    db.add(part)
    db.commit()
    authenticated = _admin_client(db, username="replenishment_concurrent_admin")
    clients = [
        TestClient(app, raise_server_exceptions=False),
        TestClient(app, raise_server_exceptions=False),
    ]
    for client in clients:
        client.headers["Authorization"] = authenticated.headers["Authorization"]
    payload = {
        "client_request_id": str(uuid4()),
        "project_id": project.project_id,
        "request_note": "并发重试",
        "lines": [{"part_id": part.id, "quantity": 4, "special_note": None}],
    }
    barrier = Barrier(2)

    def submit(client: TestClient):
        barrier.wait()
        return _post(client, payload)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(submit, clients))
    finally:
        for client in clients:
            client.close()

    assert [response.status_code for response in responses] == [201, 201]
    assert len({response.json()["application_id"] for response in responses}) == 1
    assert sorted(response.json()["idempotent"] for response in responses) == [False, True]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplication)) == 1
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationVersion)) == 1
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplicationLine)) == 1
    assert db.scalar(select(func.count()).select_from(ReplenishmentAuditEvent)) == 1


def test_atomic_submit_locks_project_and_part_eligibility(db, monkeypatch):
    project = _project(db)
    part = DimPart(pn_std="REPL-ELIGIBILITY-LOCK-PN", status="active")
    db.add(part)
    db.commit()
    client = _admin_client(db, username="replenishment_eligibility_lock_admin")
    entered_screening = Event()
    release_screening = Event()
    original_screen = replenishment_screening.screen

    def blocked_screen(*args, **kwargs):
        entered_screening.set()
        assert release_screening.wait(timeout=5)
        return original_screen(*args, **kwargs)

    def update_times_out(statement: str, params: dict) -> bool:
        try:
            with engine.begin() as connection:
                connection.execute(text("SET LOCAL lock_timeout = '200ms'"))
                connection.execute(text(statement), params)
        except DBAPIError:
            return True
        return False

    monkeypatch.setattr(replenishment_screening, "screen", blocked_screen)
    payload = {
        "client_request_id": str(uuid4()),
        "project_id": project.project_id,
        "lines": [{"part_id": part.id, "quantity": 1, "special_note": None}],
    }
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_post, client, payload)
        assert entered_screening.wait(timeout=5)
        try:
            project_locked = update_times_out(
                "UPDATE maintenance_project SET is_active = false "
                "WHERE project_id = :project_id",
                {"project_id": project.project_id},
            )
            part_locked = update_times_out(
                "UPDATE dim_part SET status = 'merged' WHERE id = :part_id",
                {"part_id": part.id},
            )
        finally:
            release_screening.set()
        response = future.result(timeout=10)

    assert project_locked is True
    assert part_locked is True
    assert response.status_code == 201, response.text


def test_retired_mutation_and_derived_routes_return_410_without_writes(db):
    client = _admin_client(db, username="replenishment_retired_routes_admin")
    application_id = str(uuid4())
    line_id = str(uuid4())
    version_id = str(uuid4())
    requests = [
        (
            "PATCH",
            f"/api/replenishment-beta/applications/{application_id}",
            {"json": {"expected_version": 1, "warehouse": None, "request_note": None}},
        ),
        (
            "POST",
            f"/api/replenishment-beta/applications/{application_id}/lines",
            {"json": {"expected_version": 1, "part_id": 1, "quantity": 1}},
        ),
        (
            "PATCH",
            f"/api/replenishment-beta/applications/{application_id}/lines/{line_id}",
            {"json": {"expected_version": 1, "part_id": 1, "quantity": 1}},
        ),
        (
            "DELETE",
            f"/api/replenishment-beta/applications/{application_id}/lines/{line_id}"
            "?expected_version=1",
            {},
        ),
        (
            "POST",
            f"/api/replenishment-beta/applications/{application_id}/submit",
            {"json": {"expected_version": 1}},
        ),
        (
            "POST",
            f"/api/replenishment-beta/applications/{application_id}/revision",
            {"json": {"expected_version": 1}},
        ),
        (
            "POST",
            f"/api/replenishment-beta/applications/{application_id}/review-results",
            {
                "json": {
                    "version_id": version_id,
                    "content_digest": "a" * 64,
                    "idempotency_key": "retired-review-key",
                    "decisions": [
                        {"line_id": line_id, "decision": "approved", "reason": None}
                    ],
                }
            },
        ),
        (
            "GET",
            f"/api/replenishment-beta/applications/{application_id}"
            "/exports/manual-review.xlsx",
            {},
        ),
        (
            "GET",
            f"/api/replenishment-beta/applications/{application_id}"
            "/exports/wbdd-subset.xlsx",
            {},
        ),
        ("GET", f"/api/replenishment-beta/applications/{application_id}/evidence", {}),
        (
            "GET",
            f"/api/replenishment-beta/applications/{application_id}"
            "/exports/purchase-list.xlsx",
            {},
        ),
    ]

    settings = get_settings()
    original = settings.replenishment_beta_enabled
    try:
        settings.replenishment_beta_enabled = True
        responses = [
            client.request(method, path, **kwargs)
            for method, path, kwargs in requests
        ]
    finally:
        settings.replenishment_beta_enabled = original

    assert [response.status_code for response in responses] == [410] * len(requests)
    assert db.scalar(select(func.count()).select_from(ReplenishmentApplication)) == 0
    assert db.scalar(select(func.count()).select_from(ReplenishmentAuditEvent)) == 0


def test_retired_review_route_exposes_no_decision_request_contract(db):
    client = _admin_client(db, username="replenishment_retired_schema_admin")

    schema = client.get("/openapi.json").json()

    operation = schema["paths"][
        "/api/replenishment-beta/applications/{application_id}/review-results"
    ]["post"]
    assert "requestBody" not in operation
    assert "ReviewDecision" not in schema["components"]["schemas"]
    assert "ReviewWrite" not in schema["components"]["schemas"]


def test_retired_routes_do_not_validate_removed_workflow_inputs(db):
    client = _admin_client(db, username="replenishment_retired_inputs_admin")
    application_id = str(uuid4())
    line_id = str(uuid4())
    requests = [
        ("PATCH", f"/api/replenishment-beta/applications/{application_id}"),
        ("POST", f"/api/replenishment-beta/applications/{application_id}/lines"),
        (
            "PATCH",
            f"/api/replenishment-beta/applications/{application_id}/lines/{line_id}",
        ),
        (
            "DELETE",
            f"/api/replenishment-beta/applications/{application_id}/lines/{line_id}",
        ),
        ("POST", f"/api/replenishment-beta/applications/{application_id}/submit"),
        ("POST", f"/api/replenishment-beta/applications/{application_id}/revision"),
        (
            "POST",
            f"/api/replenishment-beta/applications/{application_id}/review-results",
        ),
    ]

    settings = get_settings()
    original = settings.replenishment_beta_enabled
    try:
        settings.replenishment_beta_enabled = True
        responses = [
            client.request(method, path, json={}) for method, path in requests
        ]
    finally:
        settings.replenishment_beta_enabled = original

    assert [response.status_code for response in responses] == [410] * len(requests)


def test_system_screening_export_is_frozen_and_read_only(db, monkeypatch):
    project = _project(db)
    part = DimPart(
        pn_std="REPL-FROZEN-EXPORT-PN", description="提交时描述", status="active"
    )
    db.add(part)
    db.flush()
    pool = PartPool(
        group_id=980000 + part.id,
        name="补库冻结池",
        status="active",
        source="manual",
        member_count=1,
    )
    db.add(pool)
    db.flush()
    db.add(PartPoolMember(group_id=pool.group_id, part_id=part.id))
    policy = PartPoolPricePolicy(
        group_id=pool.group_id,
        sales_floor_ex_tax=Decimal("88.00"),
        sales_input_value=Decimal("88.00"),
        sales_input_basis="ex_tax",
        valid_to=None,
        changed_by="tester",
    )
    db.add(policy)
    db.commit()
    client = _admin_client(db, username="replenishment_frozen_export_admin")
    created = _post(
        client,
        {
            "client_request_id": str(uuid4()),
            "project_id": project.project_id,
            "request_note": None,
            "lines": [{"part_id": part.id, "quantity": 1, "special_note": None}],
        },
    )
    assert created.status_code == 201, created.text
    audit_count = db.scalar(select(func.count()).select_from(ReplenishmentAuditEvent))

    policy.sales_floor_ex_tax = Decimal("777.00")
    part.description = "提交后被修改的实时描述"
    db.commit()

    def reject_live_query(*_args, **_kwargs):
        raise AssertionError("export must not query mutable screening sources")

    monkeypatch.setattr(replenishment_screening, "screen", reject_live_query)
    monkeypatch.setattr(replenishment_screening, "latest_sales_history", reject_live_query)
    monkeypatch.setattr(replenishment_screening, "pool_floor_prices", reject_live_query)
    response = _get(
        client,
        f"/api/replenishment-beta/applications/{created.json()['application_id']}"
        "/exports/system-screening.xlsx",
    )

    assert response.status_code == 200, response.text
    rows = [
        [cell.value for cell in row]
        for row in load_workbook(BytesIO(response.content)).active.iter_rows()
    ]
    headers, body = rows[1], rows[2]
    assert body[headers.index("产品描述")] == "提交时描述"
    assert body[headers.index("池内最低价(未税)")] == 88.0
    assert "批准" not in rows[0][0]
    assert "驳回" not in rows[0][0]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ReplenishmentAuditEvent)) == audit_count


def test_legacy_unbound_history_cannot_export_system_screening(db):
    username = "replenishment_legacy_export_admin"
    client = _admin_client(db, username=username)
    db.execute(
        text(
            "ALTER TABLE replenishment_application "
            "DISABLE TRIGGER trg_replenishment_project_binding"
        )
    )
    try:
        db.execute(
            text(
                "INSERT INTO replenishment_application "
                "(application_id, application_no, owner_username, "
                "is_legacy_project_unbound, status) VALUES "
                "('legacy-export-app', 'BLK-LEGACY-EXPORT', :owner, true, 'submitted')"
            ),
            {"owner": username},
        )
    finally:
        db.execute(
            text(
                "ALTER TABLE replenishment_application "
                "ENABLE TRIGGER trg_replenishment_project_binding"
            )
        )
    db.execute(
        text(
            "INSERT INTO replenishment_application_version "
            "(version_id, application_id, version_no, status, warehouse, "
            "content_digest, created_by, submitted_by, submitted_at) VALUES "
            "('legacy-export-version', 'legacy-export-app', 1, 'submitted', "
            "'历史仓', :digest, :owner, :owner, now())"
        ),
        {"digest": "a" * 64, "owner": username},
    )
    db.commit()

    response = _get(
        client,
        "/api/replenishment-beta/applications/legacy-export-app/"
        "exports/system-screening.xlsx",
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "legacy_project_unbound"
