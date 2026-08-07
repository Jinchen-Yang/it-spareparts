"""Stable maintenance project master and contract aggregation API (#195)."""

import os
from datetime import date
from decimal import Decimal

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.auth import hash_password
from app.db import engine
from app.main import app
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.system import SysAccessLog, SysUser


def _token(db, *, username: str, role: str = "admin", permissions: dict | None = None) -> str:
    db.add(
        SysUser(
            username=username,
            role=role,
            is_active=True,
            password_hash=hash_password("synthetic-password-123"),
            permissions=permissions,
        )
    )
    db.commit()
    response = TestClient(app).post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _get(client: TestClient, path: str, token: str, **params):
    return client.get(
        path,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )


def _alembic_cfg() -> AlembicConfig:
    cfg = AlembicConfig(
        os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    )
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def test_stable_project_overview_sums_all_effective_contracts_to_the_cent(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000001",
        project_code="MAINT-SYNTH-001",
        display_name="合成项目甲",
        project_manager_id="manager-synth-1",
        lifecycle_status="active",
    )
    db.add(project)
    db.add_all(
        [
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000001",
                project_id=project.project_id,
                contract_id="contract-synth-a",
                contract_no="CONTRACT-SYNTH-A",
                contract_amount=Decimal("100.10"),
                contract_status="effective",
                status_mapping_state="mapped",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                source="synthetic-test",
            ),
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000002",
                project_id=project.project_id,
                contract_id="contract-synth-b",
                contract_no="CONTRACT-SYNTH-B",
                contract_amount=Decimal("200.20"),
                contract_status="effective",
                status_mapping_state="mapped",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                source="synthetic-test",
            ),
        ]
    )
    db.commit()
    token = _token(db, username="maint_project_admin")

    response = _get(
        TestClient(app),
        f"/api/maintenance/projects/stable/{project.project_id}",
        token,
        as_of="2026-08-08",
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["project"]["project_id"] == project.project_id
    assert payload["project"]["project_code"] == "MAINT-SYNTH-001"
    assert payload["effective_contract_count"] == 2
    assert Decimal(str(payload["total_contract_amount"])) == Decimal("300.30")
    assert [contract["contract_no"] for contract in payload["contracts"]] == [
        "CONTRACT-SYNTH-A",
        "CONTRACT-SYNTH-B",
    ]
    assert all(contract["is_effective"] for contract in payload["contracts"])
    assert payload["completeness"] == {"status": "complete", "issues": []}
    assert payload["as_of"] == "2026-08-08"
    assert payload["data_version"]


def test_missing_effective_contract_amount_fails_closed_with_reason(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000002",
        project_code="MAINT-SYNTH-002",
        display_name="合成项目乙",
        lifecycle_status="active",
    )
    db.add(project)
    db.add(
        MaintenanceProjectContract(
            project_contract_id="10000000-0000-4000-8000-000000000003",
            project_id=project.project_id,
            contract_id="contract-synth-missing",
            contract_no="CONTRACT-SYNTH-MISSING",
            contract_amount=None,
            contract_status="effective",
            status_mapping_state="mapped",
            included_in_total=True,
            effective_from=date(2026, 1, 1),
            source="synthetic-test",
        )
    )
    db.commit()
    token = _token(db, username="maint_project_missing_admin")

    response = _get(
        TestClient(app),
        f"/api/maintenance/projects/stable/{project.project_id}",
        token,
        as_of="2026-08-08",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_contract_amount"] is None
    assert payload["completeness"] == {
        "status": "incomplete",
        "issues": [
            {
                "code": "missing_contract_amount",
                "contract_ids": ["contract-synth-missing"],
            }
        ],
    }


def test_expired_or_excluded_relationships_remain_visible_but_do_not_enter_total(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000003",
        project_code="MAINT-SYNTH-003",
        display_name="合成项目丙",
        lifecycle_status="active",
    )
    db.add(project)
    db.add_all(
        [
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000004",
                project_id=project.project_id,
                contract_id="contract-synth-current",
                contract_no="CONTRACT-SYNTH-CURRENT",
                contract_amount=Decimal("400.00"),
                contract_status="effective",
                status_mapping_state="mapped",
                included_in_total=True,
                effective_from=date(2026, 8, 8),
                source="synthetic-test",
            ),
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000005",
                project_id=project.project_id,
                contract_id="contract-synth-expired",
                contract_no="CONTRACT-SYNTH-EXPIRED",
                contract_amount=Decimal("900.00"),
                contract_status="expired",
                status_mapping_state="mapped",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                effective_to=date(2026, 8, 8),
                source="synthetic-test",
            ),
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000006",
                project_id=project.project_id,
                contract_id="contract-synth-excluded",
                contract_no="CONTRACT-SYNTH-EXCLUDED",
                contract_amount=Decimal("800.00"),
                contract_status="effective",
                status_mapping_state="mapped",
                included_in_total=False,
                effective_from=date(2026, 1, 1),
                source="synthetic-test",
            ),
        ]
    )
    db.commit()
    token = _token(db, username="maint_project_interval_admin")

    response = _get(
        TestClient(app),
        f"/api/maintenance/projects/stable/{project.project_id}",
        token,
        as_of="2026-08-08",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["contract_count"] == 3
    assert payload["effective_contract_count"] == 1
    assert Decimal(str(payload["total_contract_amount"])) == Decimal("400.00")
    effective_by_id = {
        contract["contract_id"]: contract["is_effective"]
        for contract in payload["contracts"]
    }
    assert effective_by_id == {
        "contract-synth-current": True,
        "contract-synth-excluded": False,
        "contract-synth-expired": False,
    }


def test_unmapped_source_status_fails_closed_instead_of_guessing_display_text(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000004",
        project_code="MAINT-SYNTH-004",
        display_name="合成项目丁",
        lifecycle_status="active",
    )
    db.add(project)
    db.add(
        MaintenanceProjectContract(
            project_contract_id="10000000-0000-4000-8000-000000000007",
            project_id=project.project_id,
            contract_id="contract-synth-unmapped",
            contract_no="CONTRACT-SYNTH-UNMAPPED",
            contract_amount=Decimal("500.00"),
            contract_status="看起来已生效但未建映射",
            status_mapping_state="unmapped",
            included_in_total=True,
            effective_from=date(2026, 1, 1),
            source="synthetic-test",
        )
    )
    db.commit()
    token = _token(db, username="maint_project_unmapped_admin")

    response = _get(
        TestClient(app),
        f"/api/maintenance/projects/stable/{project.project_id}",
        token,
        as_of="2026-08-08",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_contract_amount"] is None
    assert payload["completeness"]["issues"] == [
        {
            "code": "unmapped_contract_status",
            "contract_ids": ["contract-synth-unmapped"],
        }
    ]


def test_overlapping_duplicate_contract_relationships_never_double_count(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000005",
        project_code="MAINT-SYNTH-005",
        display_name="合成项目戊",
        lifecycle_status="active",
    )
    db.add(project)
    db.add_all(
        [
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000008",
                project_id=project.project_id,
                contract_id="contract-synth-duplicate",
                contract_no="CONTRACT-SYNTH-DUPLICATE",
                contract_amount=Decimal("600.00"),
                contract_status="effective",
                status_mapping_state="mapped",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                source="synthetic-test-a",
            ),
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000009",
                project_id=project.project_id,
                contract_id="contract-synth-duplicate",
                contract_no="CONTRACT-SYNTH-DUPLICATE",
                contract_amount=Decimal("600.00"),
                contract_status="effective",
                status_mapping_state="mapped",
                included_in_total=True,
                effective_from=date(2026, 2, 1),
                source="synthetic-test-b",
            ),
        ]
    )
    db.commit()
    token = _token(db, username="maint_project_duplicate_admin")

    response = _get(
        TestClient(app),
        f"/api/maintenance/projects/stable/{project.project_id}",
        token,
        as_of="2026-08-08",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_contract_count"] == 2
    assert payload["total_contract_amount"] is None
    assert payload["completeness"]["issues"] == [
        {
            "code": "duplicate_effective_contract",
            "contract_ids": ["contract-synth-duplicate"],
        }
    ]


def test_exact_duplicate_relationship_identity_is_rejected_by_storage(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000006",
        project_code="MAINT-SYNTH-006",
        display_name="合成项目己",
        lifecycle_status="active",
    )
    db.add(project)
    common = {
        "project_id": project.project_id,
        "contract_id": "contract-synth-exact-duplicate",
        "contract_no": "CONTRACT-SYNTH-EXACT-DUPLICATE",
        "contract_amount": Decimal("700.00"),
        "contract_status": "effective",
        "status_mapping_state": "mapped",
        "included_in_total": True,
        "effective_from": date(2026, 1, 1),
        "source": "synthetic-test",
    }
    db.add_all(
        [
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000010",
                **common,
            ),
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000011",
                **common,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_cross_project_contract_conflict_fails_closed_and_is_auditable(db):
    projects = [
        MaintenanceProject(
            project_id="00000000-0000-4000-8000-000000000007",
            project_code="MAINT-SYNTH-007-A",
            display_name="合成共享合同项目甲",
            lifecycle_status="active",
        ),
        MaintenanceProject(
            project_id="00000000-0000-4000-8000-000000000008",
            project_code="MAINT-SYNTH-007-B",
            display_name="合成共享合同项目乙",
            lifecycle_status="active",
        ),
    ]
    db.add_all(projects)
    for index, project in enumerate(projects, 12):
        db.add(
            MaintenanceProjectContract(
                project_contract_id=f"10000000-0000-4000-8000-{index:012d}",
                project_id=project.project_id,
                contract_id="contract-synth-cross-project",
                contract_no="CONTRACT-SYNTH-CROSS-PROJECT",
                contract_amount=Decimal("800.00"),
                contract_status="effective",
                status_mapping_state="mapped",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                source="synthetic-test",
            )
        )
    db.commit()
    token = _token(db, username="maint_project_cross_admin")

    response = _get(
        TestClient(app),
        f"/api/maintenance/projects/stable/{projects[0].project_id}",
        token,
        as_of="2026-08-08",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_contract_amount"] is None
    assert payload["completeness"]["issues"] == [
        {
            "code": "cross_project_contract_conflict",
            "contract_ids": ["contract-synth-cross-project"],
        }
    ]


def test_same_display_name_projects_stay_independent_and_rename_keeps_relationships(db):
    first = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000009",
        project_code="MAINT-SYNTH-SAME-A",
        display_name="相同展示名称",
        lifecycle_status="active",
    )
    second = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000010",
        project_code="MAINT-SYNTH-SAME-B",
        display_name="相同展示名称",
        lifecycle_status="active",
    )
    db.add_all([first, second])
    for index, (project, amount) in enumerate(
        [(first, "900.00"), (second, "1000.00")],
        14,
    ):
        db.add(
            MaintenanceProjectContract(
                project_contract_id=f"10000000-0000-4000-8000-{index:012d}",
                project_id=project.project_id,
                contract_id=f"contract-synth-same-{index}",
                contract_no=f"CONTRACT-SYNTH-SAME-{index}",
                contract_amount=Decimal(amount),
                contract_status="effective",
                status_mapping_state="mapped",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                source="synthetic-test",
            )
        )
    db.commit()
    token = _token(db, username="maint_project_identity_admin")
    client = TestClient(app)

    directory = _get(
        client,
        "/api/maintenance/projects/stable",
        token,
        q="相同展示名称",
        as_of="2026-08-08",
    )
    assert directory.status_code == 200
    assert {row["project_id"] for row in directory.json()["rows"]} == {
        first.project_id,
        second.project_id,
    }
    code_search = _get(
        client,
        "/api/maintenance/projects/stable",
        token,
        q="maint-synth-same-a",
        as_of="2026-08-08",
    )
    assert [row["project_id"] for row in code_search.json()["rows"]] == [first.project_id]

    first_overview = _get(
        client,
        f"/api/maintenance/projects/stable/{first.project_id}",
        token,
        as_of="2026-08-08",
    ).json()
    second_overview = _get(
        client,
        f"/api/maintenance/projects/stable/{second.project_id}",
        token,
        as_of="2026-08-08",
    ).json()
    assert Decimal(str(first_overview["total_contract_amount"])) == Decimal("900.00")
    assert Decimal(str(second_overview["total_contract_amount"])) == Decimal("1000.00")

    first.display_name = "已更名但身份不变"
    first.version += 1
    db.commit()
    renamed = _get(
        client,
        f"/api/maintenance/projects/stable/{first.project_id}",
        token,
        as_of="2026-08-08",
    ).json()
    assert renamed["project"]["display_name"] == "已更名但身份不变"
    assert renamed["project"]["project_id"] == first.project_id
    assert renamed["contracts"][0]["contract_id"] == "contract-synth-same-14"


def test_contract_amount_permission_hides_lines_total_and_amount_completeness(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000011",
        project_code="MAINT-SYNTH-RESTRICTED",
        display_name="合成权限项目",
        lifecycle_status="active",
    )
    db.add(project)
    db.add_all(
        [
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000016",
                project_id=project.project_id,
                contract_id="contract-synth-visible-structure",
                contract_no="CONTRACT-SYNTH-VISIBLE-STRUCTURE",
                contract_amount=Decimal("1200.00"),
                contract_status="effective",
                status_mapping_state="mapped",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                source="synthetic-test",
            ),
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000017",
                project_id=project.project_id,
                contract_id="contract-synth-hidden-missing",
                contract_no="CONTRACT-SYNTH-HIDDEN-MISSING",
                contract_amount=None,
                contract_status="effective",
                status_mapping_state="mapped",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                source="synthetic-test",
            ),
        ]
    )
    db.commit()
    token = _token(db, username="maint_project_purchaser", role="purchaser")

    response = _get(
        TestClient(app),
        f"/api/maintenance/projects/stable/{project.project_id}",
        token,
        as_of="2026-08-08",
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total_contract_amount"] is None
    assert all(contract["contract_amount"] is None for contract in payload["contracts"])
    assert payload["completeness"] == {"status": "restricted", "issues": []}


def test_project_without_effective_contracts_returns_null_not_zero(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000012",
        project_code="MAINT-SYNTH-NO-CONTRACT",
        display_name="合成无合同项目",
        lifecycle_status="active",
    )
    db.add(project)
    db.commit()
    token = _token(db, username="maint_project_empty_admin")

    response = _get(
        TestClient(app),
        f"/api/maintenance/projects/stable/{project.project_id}",
        token,
        as_of="2026-08-08",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_contract_count"] == 0
    assert payload["total_contract_amount"] is None
    assert payload["completeness"]["issues"] == [
        {"code": "no_effective_contracts", "contract_ids": []}
    ]


def test_stable_routes_do_not_shadow_legacy_projects_api(db):
    token = _token(db, username="maint_project_legacy_admin")
    client = TestClient(app)

    legacy = _get(
        client,
        "/api/maintenance/projects",
        token,
        lifecycle="all",
    )
    anonymous_stable = client.get("/api/maintenance/projects/stable")

    assert legacy.status_code == 200, legacy.text
    assert {"rows", "as_of"} <= legacy.json().keys()
    assert anonymous_stable.status_code == 401


def test_stable_project_directory_and_overview_reads_are_access_logged(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000013",
        project_code="MAINT-SYNTH-AUDIT",
        display_name="合成审计项目",
        lifecycle_status="active",
    )
    db.add(project)
    db.commit()
    token = _token(db, username="maint_project_audit_admin")
    client = TestClient(app)

    assert _get(client, "/api/maintenance/projects/stable", token).status_code == 200
    assert _get(
        client,
        f"/api/maintenance/projects/stable/{project.project_id}",
        token,
    ).status_code == 200

    db.expire_all()
    actions = list(
        db.execute(
            select(SysAccessLog.action)
            .where(SysAccessLog.username == "maint_project_audit_admin")
            .where(
                SysAccessLog.action.in_(
                    ["stable_project_directory", "stable_project_overview"]
                )
            )
            .order_by(SysAccessLog.id)
        ).scalars()
    )
    assert actions == ["stable_project_directory", "stable_project_overview"]


def test_stable_project_queries_do_not_grow_with_contract_count(db):
    few = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000014",
        project_code="MAINT-SYNTH-QUERY-FEW",
        display_name="合成查询少合同",
        lifecycle_status="active",
    )
    many = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000015",
        project_code="MAINT-SYNTH-QUERY-MANY",
        display_name="合成查询多合同",
        lifecycle_status="active",
    )
    db.add_all([few, many])
    for project, count, offset in [(few, 1, 100), (many, 25, 200)]:
        for index in range(count):
            db.add(
                MaintenanceProjectContract(
                    project_contract_id=(
                        f"20000000-0000-4000-8000-{offset + index:012d}"
                    ),
                    project_id=project.project_id,
                    contract_id=f"contract-synth-query-{offset + index}",
                    contract_no=f"CONTRACT-SYNTH-QUERY-{offset + index}",
                    contract_amount=Decimal("10.00"),
                    contract_status="effective",
                    status_mapping_state="mapped",
                    included_in_total=True,
                    effective_from=date(2026, 1, 1),
                    source="synthetic-test",
                )
            )
    db.commit()
    token = _token(db, username="maint_project_query_admin")
    client = TestClient(app)

    def captured_get(path: str) -> tuple[object, list[str]]:
        statements: list[str] = []

        def before_cursor_execute(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        try:
            response = _get(client, path, token, as_of="2026-08-08")
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)
        return response, statements

    few_response, few_queries = captured_get(
        f"/api/maintenance/projects/stable/{few.project_id}"
    )
    many_response, many_queries = captured_get(
        f"/api/maintenance/projects/stable/{many.project_id}"
    )
    directory_response, directory_queries = captured_get(
        "/api/maintenance/projects/stable"
    )

    assert few_response.status_code == many_response.status_code == 200
    assert len(few_queries) == len(many_queries)
    assert sum("maintenance_project_contract" in query for query in few_queries) == 2
    assert sum("maintenance_project_contract" in query for query in many_queries) == 2
    assert directory_response.status_code == 200
    assert not any("maintenance_project_contract" in query for query in directory_queries)


def test_nonempty_stable_project_facts_block_destructive_schema_downgrade(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000016",
        project_code="MAINT-SYNTH-DOWNGRADE-GUARD",
        display_name="合成迁移保护项目",
        lifecycle_status="active",
    )
    db.add(project)
    db.commit()
    db.close()

    with pytest.raises(DBAPIError, match="downgrade blocked"):
        alembic_command.downgrade(_alembic_cfg(), "f1c8e4a7b2d9")

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "c6f2a8e9d4b1"
        )
        assert connection.scalar(
            text(
                "SELECT count(*) FROM maintenance_project "
                "WHERE project_id='00000000-0000-4000-8000-000000000016'"
            )
        ) == 1
