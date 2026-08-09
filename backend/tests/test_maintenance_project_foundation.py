"""Stable maintenance project master and contract aggregation API (#195)."""

import hashlib
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import permissions as permission_service
from app.auth import hash_password
from app.business_time import business_today
from app.db import engine
from app.main import app
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
)
from app.models.system import SysAccessLog, SysUser


def _token(
    db, *, username: str, role: str = "admin", permissions: dict | None = None
) -> str:
    account_scope = (
        {
            "template_code": "admin",
            "template_version": 1,
            "template_perms": permission_service.admin_account_defaults(),
            "perm_overrides": {"page_maintenance_beta": True},
        }
        if role == "admin" else {}
    )
    db.add(
        SysUser(
            username=username,
            role=role,
            is_active=True,
            password_hash=hash_password("synthetic-password-123"),
            permissions=permissions,
            **account_scope,
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


def _search_projects(client: TestClient, token: str, **body):
    return client.post(
        "/api/maintenance/projects/stable/search",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


def _alembic_cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
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
                status_mapping_version="contract-status-map-v1",
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
                status_mapping_version="contract-status-map-v1",
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
    assert {
        contract["status_mapping_version"] for contract in payload["contracts"]
    } == {"contract-status-map-v1"}

    amount_changed_without_version_bump = db.get(
        MaintenanceProjectContract,
        "10000000-0000-4000-8000-000000000001",
    )
    assert amount_changed_without_version_bump is not None
    amount_changed_without_version_bump.contract_amount = Decimal("101.10")
    db.commit()
    changed = _get(
        TestClient(app),
        f"/api/maintenance/projects/stable/{project.project_id}",
        token,
        as_of="2026-08-08",
    ).json()
    assert changed["data_version"] != payload["data_version"]
    assert Decimal(str(changed["total_contract_amount"])) == Decimal("301.30")


def test_directory_data_version_covers_as_of_and_all_filtered_pages(db):
    first = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000021",
        project_code="MAINT-VERSION-A-FIRST",
        display_name="版本标识第一页",
        lifecycle_status="active",
    )
    db.add(first)
    db.commit()
    token = _token(db, username="maint_project_directory_version_admin")
    client = TestClient(app)

    initial = _get(
        client,
        "/api/maintenance/projects/stable",
        token,
        page_size=1,
        as_of="2026-08-08",
    ).json()
    shifted_as_of = _get(
        client,
        "/api/maintenance/projects/stable",
        token,
        page_size=1,
        as_of="2026-08-09",
    ).json()
    assert shifted_as_of["rows"] == initial["rows"]
    assert shifted_as_of["total"] == initial["total"]
    assert shifted_as_of["data_version"] != initial["data_version"]

    after_page = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000022",
        project_code="MAINT-VERSION-Z-AFTER-PAGE",
        display_name="版本标识后续页",
        lifecycle_status="active",
        version=7,
    )
    db.add(after_page)
    db.commit()
    changed = _get(
        client,
        "/api/maintenance/projects/stable",
        token,
        page_size=1,
        as_of="2026-08-08",
    ).json()

    assert changed["rows"] == initial["rows"]
    assert changed["total"] == initial["total"] + 1
    assert changed["data_version"] != initial["data_version"]


def test_overview_data_version_covers_as_of_and_cross_project_relations(db):
    target = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000023",
        project_code="MAINT-VERSION-TARGET",
        display_name="版本标识目标项目",
        lifecycle_status="active",
    )
    target_relation = MaintenanceProjectContract(
        project_contract_id="10000000-0000-4000-8000-000000000023",
        project_id=target.project_id,
        contract_id="contract-version-cross-scope",
        contract_no="CONTRACT-VERSION-CROSS-SCOPE",
        contract_amount=Decimal("100.00"),
        contract_status="effective",
        status_mapping_state="mapped",
        status_mapping_version="contract-status-map-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        source="synthetic-test",
    )
    db.add_all([target, target_relation])
    db.commit()
    token = _token(db, username="maint_project_overview_version_admin")
    client = TestClient(app)
    path = f"/api/maintenance/projects/stable/{target.project_id}"

    initial = _get(client, path, token, as_of="2026-08-08").json()
    shifted_as_of = _get(client, path, token, as_of="2026-08-09").json()
    assert shifted_as_of["contracts"] == initial["contracts"]
    assert shifted_as_of["data_version"] != initial["data_version"]

    counterpart = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000024",
        project_code="MAINT-VERSION-COUNTERPART",
        display_name="版本标识对端项目",
        lifecycle_status="active",
    )
    counterpart_relation = MaintenanceProjectContract(
        project_contract_id="10000000-0000-4000-8000-000000000024",
        project_id=counterpart.project_id,
        contract_id=target_relation.contract_id,
        contract_no=target_relation.contract_no,
        contract_amount=Decimal("100.00"),
        contract_status="effective",
        status_mapping_state="mapped",
        status_mapping_version="contract-status-map-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        source="synthetic-test",
        version=4,
    )
    db.add_all([counterpart, counterpart_relation])
    db.commit()
    conflicted = _get(client, path, token, as_of="2026-08-08").json()

    assert conflicted["data_version"] != initial["data_version"]
    assert conflicted["completeness"]["issues"] == [
        {
            "code": "cross_project_contract_conflict",
            "contract_ids": [target_relation.contract_id],
        }
    ]

    counterpart_relation.version += 1
    db.commit()
    version_bumped = _get(client, path, token, as_of="2026-08-08").json()
    assert version_bumped["data_version"] == conflicted["data_version"]


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
            status_mapping_version="contract-status-map-v1",
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
                status_mapping_version="contract-status-map-v1",
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
                status_mapping_version="contract-status-map-v1",
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
                status_mapping_version="contract-status-map-v1",
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
            status_mapping_version="contract-status-map-v1",
            included_in_total=False,
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
        {"code": "no_effective_contracts", "contract_ids": []},
        {
            "code": "unmapped_contract_status",
            "contract_ids": ["contract-synth-unmapped"],
        },
    ]


def test_current_unmapped_relationship_fails_closed_even_when_not_included(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000017",
        project_code="MAINT-SYNTH-UNMAPPED-EXCLUDED",
        display_name="合成未映射排除项目",
        lifecycle_status="active",
    )
    db.add(project)
    db.add_all(
        [
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000018",
                project_id=project.project_id,
                contract_id="contract-synth-mapped-included",
                contract_no="CONTRACT-SYNTH-MAPPED-INCLUDED",
                contract_amount=Decimal("100.00"),
                contract_status="effective",
                status_mapping_state="mapped",
                status_mapping_version="contract-status-map-v1",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                source="synthetic-test",
            ),
            MaintenanceProjectContract(
                project_contract_id="10000000-0000-4000-8000-000000000019",
                project_id=project.project_id,
                contract_id="contract-synth-unmapped-excluded",
                contract_no="CONTRACT-SYNTH-UNMAPPED-EXCLUDED",
                contract_amount=Decimal("200.00"),
                contract_status="来源状态尚未映射",
                status_mapping_state="unmapped",
                status_mapping_version="contract-status-map-v1",
                included_in_total=False,
                effective_from=date(2026, 1, 1),
                source="synthetic-test",
            ),
        ]
    )
    db.commit()
    token = _token(db, username="maint_project_unmapped_excluded_admin")

    response = _get(
        TestClient(app),
        f"/api/maintenance/projects/stable/{project.project_id}",
        token,
        as_of="2026-08-08",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_contract_count"] == 1
    assert payload["total_contract_amount"] is None
    assert payload["completeness"]["issues"] == [
        {
            "code": "unmapped_contract_status",
            "contract_ids": ["contract-synth-unmapped-excluded"],
        }
    ]


@pytest.mark.parametrize(
    ("mapping_version", "mapping_state", "included"),
    [
        ("   ", "mapped", True),
        ("contract-status-map-v1", "unmapped", True),
    ],
    ids=["blank-mapping-version", "unmapped-cannot-enter-total"],
)
def test_mapping_provenance_constraints_fail_closed(
    db,
    mapping_version,
    mapping_state,
    included,
):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000026",
        project_code="MAINT-SYNTH-MAPPING-CONSTRAINT",
        display_name="合成映射约束项目",
        lifecycle_status="active",
    )
    db.add(project)
    db.commit()
    db.add(
        MaintenanceProjectContract(
            project_contract_id="10000000-0000-4000-8000-000000000026",
            project_id=project.project_id,
            contract_id="contract-synth-mapping-constraint",
            contract_no="CONTRACT-SYNTH-MAPPING-CONSTRAINT",
            contract_amount=Decimal("100.00"),
            contract_status="effective",
            status_mapping_state=mapping_state,
            status_mapping_version=mapping_version,
            included_in_total=included,
            effective_from=date(2026, 1, 1),
            source="synthetic-test",
        )
    )

    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


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
                status_mapping_version="contract-status-map-v1",
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
                status_mapping_version="contract-status-map-v1",
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
        "status_mapping_version": "contract-status-map-v1",
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
                status_mapping_version="contract-status-map-v1",
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


@pytest.mark.parametrize(
    "requested_active",
    [True, False],
    ids=[
        "active-project-reads-inactive-conflict",
        "inactive-project-reads-active-conflict",
    ],
)
def test_cross_project_conflict_includes_inactive_project_relationships(
    db,
    requested_active,
):
    requested = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000018",
        project_code="MAINT-SYNTH-CROSS-REQUESTED",
        display_name="合成跨项目请求方",
        lifecycle_status="active" if requested_active else "inactive",
        is_active=requested_active,
    )
    counterpart = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000019",
        project_code="MAINT-SYNTH-CROSS-COUNTERPART",
        display_name="合成跨项目对端",
        lifecycle_status="inactive" if requested_active else "active",
        is_active=not requested_active,
    )
    db.add_all([requested, counterpart])
    for index, project in enumerate([requested, counterpart], 20):
        db.add(
            MaintenanceProjectContract(
                project_contract_id=f"10000000-0000-4000-8000-{index:012d}",
                project_id=project.project_id,
                contract_id="contract-synth-cross-inactive",
                contract_no="CONTRACT-SYNTH-CROSS-INACTIVE",
                contract_amount=Decimal("850.00"),
                contract_status="effective",
                status_mapping_state="mapped",
                status_mapping_version="contract-status-map-v1",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                source="synthetic-test",
            )
        )
    db.commit()
    token = _token(
        db,
        username=f"maint_project_cross_inactive_{requested_active}",
    )

    response = _get(
        TestClient(app),
        f"/api/maintenance/projects/stable/{requested.project_id}",
        token,
        as_of="2026-08-08",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_contract_amount"] is None
    assert payload["completeness"]["issues"] == [
        {
            "code": "cross_project_contract_conflict",
            "contract_ids": ["contract-synth-cross-inactive"],
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
                status_mapping_version="contract-status-map-v1",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                source="synthetic-test",
            )
        )
    db.commit()
    token = _token(db, username="maint_project_identity_admin")
    client = TestClient(app)

    directory = _search_projects(
        client,
        token,
        q="相同展示名称",
        as_of="2026-08-08",
    )
    assert directory.status_code == 200
    assert {row["project_id"] for row in directory.json()["rows"]} == {
        first.project_id,
        second.project_id,
    }
    code_search = _search_projects(
        client,
        token,
        q="maint-synth-same-a",
        as_of="2026-08-08",
    )
    assert [row["project_id"] for row in code_search.json()["rows"]] == [
        first.project_id
    ]

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


def test_directory_contains_search_uses_trigram_indexes_without_changing_semantics(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000020",
        project_code="MAINT-INDEX-NEEDLE-001",
        display_name="合成搜索索引中文目标",
        lifecycle_status="active",
    )
    db.add(project)
    db.commit()
    token = _token(db, username="maint_project_search_index_admin")
    client = TestClient(app)

    captured: list[tuple[str, object]] = []

    def capture_directory_sql(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture_directory_sql)
    try:
        code_search = _search_projects(
            client,
            token,
            q="needle",
            include_inactive=True,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_directory_sql)
    name_search = _search_projects(
        client,
        token,
        q="索引中文",
    )

    assert code_search.status_code == name_search.status_code == 200
    assert [row["project_id"] for row in code_search.json()["rows"]] == [
        project.project_id
    ]
    assert [row["project_id"] for row in name_search.json()["rows"]] == [
        project.project_id
    ]

    db.execute(text("ANALYZE maintenance_project"))
    db.execute(text("SET LOCAL enable_seqscan = off"))
    db.execute(text("SET LOCAL enable_indexscan = off"))
    directory_sql, directory_parameters = next(
        (statement, parameters)
        for statement, parameters in captured
        if "ORDER BY maintenance_project.project_code" in statement
    )
    assert " ILIKE " in directory_sql
    assert "lower(maintenance_project.project_code)" not in directory_sql
    plan = "\n".join(
        db.connection()
        .exec_driver_sql(
            f"EXPLAIN (FORMAT TEXT) {directory_sql}",
            directory_parameters,
        )
        .scalars()
    )
    assert "ix_maintenance_project_code_trgm" in plan, plan
    assert "ix_maintenance_project_display_name_trgm" in plan, plan


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
                status_mapping_version="contract-status-map-v1",
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
                status_mapping_version="contract-status-map-v1",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                source="synthetic-test",
            ),
        ]
    )
    db.commit()
    username = "maint_project_purchaser"
    token = _token(
        db,
        username=username,
        role="purchaser",
        permissions={"page_maintenance_beta": True},
    )
    user_id = db.scalar(select(SysUser.id).where(SysUser.username == username))
    assert user_id is not None
    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id="20000000-0000-4000-8000-000000000011",
            project_id=project.project_id,
            responsibility_type="primary_manager",
            user_id=user_id,
            source_manager_text="合成权限负责人",
            assigned_by="synthetic-test",
            assignment_reason="验证显式项目范围内的合同金额脱敏",
        )
    )
    db.commit()

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
    assert all(contract["version"] is None for contract in payload["contracts"])
    assert payload["completeness"] == {"status": "restricted", "issues": []}

    hidden_relation = db.get(
        MaintenanceProjectContract,
        "10000000-0000-4000-8000-000000000016",
    )
    assert hidden_relation is not None
    hidden_relation.contract_amount = Decimal("987654.32")
    hidden_relation.version += 1
    db.commit()
    after_hidden_amount_change = _get(
        TestClient(app),
        f"/api/maintenance/projects/stable/{project.project_id}",
        token,
        as_of="2026-08-08",
    ).json()
    assert after_hidden_amount_change["data_version"] == payload["data_version"]
    assert after_hidden_amount_change["contracts"] == payload["contracts"]


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


def test_stable_project_foundation_rejects_unsupported_write_methods(db):
    project = MaintenanceProject(
        project_id="00000000-0000-4000-8000-000000000025",
        project_code="MAINT-SYNTH-READ-ONLY",
        display_name="合成只读项目",
        lifecycle_status="active",
    )
    db.add(project)
    db.commit()
    token = _token(db, username="maint_project_read_only_admin")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    for method in ["put", "delete"]:
        directory = client.request(
            method.upper(),
            "/api/maintenance/projects/stable",
            headers=headers,
            json={},
        )
        overview = client.request(
            method.upper(),
            f"/api/maintenance/projects/stable/{project.project_id}",
            headers=headers,
            json={},
        )
        assert directory.status_code == 405
        assert overview.status_code == 405


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

    search = project.display_name
    searched = client.post(
        "/api/maintenance/projects/stable/search",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "q": search,
            "page": 1,
            "page_size": 50,
            "include_inactive": False,
        },
    )
    assert searched.status_code == 200, searched.text
    assert (
        _get(
            client,
            f"/api/maintenance/projects/stable/{project.project_id}",
            token,
        ).status_code
        == 200
    )

    db.expire_all()
    rows = list(
        db.scalars(
            select(SysAccessLog)
            .where(SysAccessLog.username == "maint_project_audit_admin")
            .where(
                SysAccessLog.action.in_(
                    ["stable_project_directory", "stable_project_overview"]
                )
            )
            .order_by(SysAccessLog.id)
        )
    )
    assert [row.action for row in rows] == [
        "stable_project_directory",
        "stable_project_overview",
    ]
    assert rows[0].detail == {
        "searched": True,
        "include_inactive": False,
        "as_of": str(business_today()),
    }
    serialized = json.dumps(rows[0].detail, ensure_ascii=False, sort_keys=True)
    assert search not in serialized
    assert hashlib.sha256(search.encode()).hexdigest() not in serialized
    assert search.encode().hex() not in serialized


@pytest.mark.parametrize(
    "search",
    [
        "GET-URL-稳定项目搜索敏感词",
        "GET-URL-LONG-PRIVATE-SENTINEL-" + "x" * 256,
    ],
)
def test_stable_project_directory_rejects_get_query_search_without_audit(db, search):
    token = _token(db, username="maint_project_get_query_admin")
    client = TestClient(app)

    response = _get(
        client,
        "/api/maintenance/projects/stable",
        token,
        q=search,
    )

    assert response.status_code == 422
    assert search not in response.text
    db.expire_all()
    assert (
        db.scalar(
            select(SysAccessLog.id).where(
                SysAccessLog.username == "maint_project_get_query_admin",
                SysAccessLog.action == "stable_project_directory",
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "search",
    [
        "",
        "POST-BODY-LONG-PRIVATE-SENTINEL-" + "x" * 128,
    ],
)
def test_stable_project_directory_rejects_invalid_post_search_without_reflection(
    db, search
):
    token = _token(db, username="maint_project_invalid_post_search_admin")
    client = TestClient(app)

    response = _search_projects(client, token, q=search)

    assert response.status_code == 422
    if search:
        assert search not in response.text
        assert "POST-BODY-LONG-PRIVATE-SENTINEL" not in response.text
    db.expire_all()
    assert (
        db.scalar(
            select(SysAccessLog.id).where(
                SysAccessLog.username == "maint_project_invalid_post_search_admin",
                SysAccessLog.action == "stable_project_directory",
            )
        )
        is None
    )


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
                    status_mapping_version="contract-status-map-v1",
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

        def before_cursor_execute(
            _conn, _cursor, statement, _parameters, _context, _many
        ):
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
    assert not any(
        "maintenance_project_contract" in query for query in directory_queries
    )


def test_empty_foundation_schema_downgrade_and_upgrade_rebuilds_full_contract(db):
    db.close()
    cfg = _alembic_cfg()
    migration_source = (
        Path(__file__).parents[1]
        / "alembic/versions/c6f2a8e9d4b1_maintenance_project_contract_foundation.py"
    ).read_text(encoding="utf-8")
    lock_start = migration_source.index(
        "LOCK TABLE maintenance_project_contract, maintenance_project"
    )
    assert lock_start < migration_source.index("IN ACCESS EXCLUSIVE MODE")
    assert lock_start < migration_source.index("DO $migration$")
    explicit_indexes = {
        "ix_maintenance_project_active_code",
        "ix_maintenance_project_code_trgm",
        "ix_maintenance_project_display_name",
        "ix_maintenance_project_display_name_trgm",
        "ix_maintenance_project_contract_contract",
        "ix_maintenance_project_contract_effective",
        "ix_maintenance_project_contract_project",
    }
    key_constraints = {
        "ck_maintenance_project_version",
        "maintenance_project_pkey",
        "maintenance_project_project_code_key",
        "ck_maintenance_project_contract_status_mapping",
        "ck_maintenance_project_contract_mapping_version",
        "ck_maintenance_project_contract_unmapped_excluded",
        "ck_maintenance_project_contract_interval",
        "ck_maintenance_project_contract_amount",
        "ck_maintenance_project_contract_version",
        "maintenance_project_contract_project_id_fkey",
        "maintenance_project_contract_pkey",
        "uq_maintenance_project_contract_identity",
    }

    try:
        alembic_command.downgrade(cfg, "f1c8e4a7b2d9")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "f1c8e4a7b2d9"
            )
            assert connection.execute(
                text(
                    "SELECT to_regclass(name) FROM "
                    "(VALUES ('maintenance_project'), "
                        "('maintenance_project_contract'), "
                        "('maintenance_project_audit_log')) AS tables(name)"
                    )
                ).scalars().all() == [None, None, None]

        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory.from_config(cfg).get_current_head()
            )
            assert connection.execute(
                text(
                    "SELECT to_regclass(name) FROM "
                    "(VALUES ('maintenance_project'), "
                        "('maintenance_project_contract'), "
                        "('maintenance_project_audit_log')) AS tables(name)"
                )
            ).scalars().all() == [
                "maintenance_project",
                "maintenance_project_contract",
                "maintenance_project_audit_log",
            ]
            index_definitions = dict(
                connection.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE schemaname = current_schema() "
                        "AND tablename IN "
                        "('maintenance_project', 'maintenance_project_contract')"
                    )
                ).all()
            )
            assert explicit_indexes <= index_definitions.keys()
            assert (
                "USING gin (project_code gin_trgm_ops)"
                in index_definitions["ix_maintenance_project_code_trgm"]
            )
            assert (
                "USING gin (display_name gin_trgm_ops)"
                in index_definitions["ix_maintenance_project_display_name_trgm"]
            )
            constraints = set(
                connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid IN "
                        "('maintenance_project'::regclass, "
                        "'maintenance_project_contract'::regclass)"
                    )
                ).scalars()
            )
            assert key_constraints <= constraints
            dual_tax_columns = set(
                connection.execute(
                    text(
                        "SELECT table_name || '.' || column_name "
                        "FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND ("
                        "(table_name = 'maintenance_site_issue_line' AND "
                        "column_name IN ('unit_cost_ex_tax', 'unit_cost_inc_tax', "
                        "'cost_amount_ex_tax', 'cost_amount_inc_tax')) OR "
                        "(table_name = 'maintenance_project_expense_attribution' AND "
                        "column_name = 'amount_inc_tax'))"
                    )
                ).scalars()
            )
            assert dual_tax_columns == {
                "maintenance_site_issue_line.unit_cost_ex_tax",
                "maintenance_site_issue_line.unit_cost_inc_tax",
                "maintenance_site_issue_line.cost_amount_ex_tax",
                "maintenance_site_issue_line.cost_amount_inc_tax",
                "maintenance_project_expense_attribution.amount_inc_tax",
            }
            strict_status_constraints = dict(
                connection.execute(
                    text(
                        "SELECT conname, convalidated FROM pg_constraint "
                        "WHERE conname IN ("
                        "'ck_maintenance_site_issue_unmapped_unknown', "
                        "'ck_maintenance_project_expense_unmapped_unknown')"
                    )
                ).all()
            )
            assert strict_status_constraints == {
                "ck_maintenance_site_issue_unmapped_unknown": False,
                "ck_maintenance_project_expense_unmapped_unknown": False,
            }
    finally:
        alembic_command.upgrade(cfg, "head")


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
    with engine.connect() as connection:
        versions_before = set(
            connection.scalars(text("SELECT version_num FROM alembic_version"))
        )

    try:
        with pytest.raises(DBAPIError, match="downgrade blocked"):
            alembic_command.downgrade(_alembic_cfg(), "f1c8e4a7b2d9")

        with engine.connect() as connection:
            assert set(
                connection.scalars(text("SELECT version_num FROM alembic_version"))
            ) == versions_before
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM maintenance_project "
                        "WHERE project_id='00000000-0000-4000-8000-000000000016'"
                    )
                )
                == 1
            )
    finally:
        # 幂等恢复；原子迁移已保证保护检查失败时数据库仍停留在完整 head。
        alembic_command.upgrade(_alembic_cfg(), "head")
