"""Read model for stable maintenance projects and their contract portfolio."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.security import UserContext, is_field_hidden


def _version_token(projects: list[MaintenanceProject]) -> str:
    facts: list[str] = []
    for project in projects:
        facts.append(f"project:{project.project_id}:{project.version}")
        for relation in sorted(
            project.contracts,
            key=lambda row: row.project_contract_id,
        ):
            facts.append(
                f"contract:{relation.project_contract_id}:{relation.version}"
            )
    return hashlib.sha256("|".join(facts).encode("utf-8")).hexdigest()


def _is_current(relation: MaintenanceProjectContract, as_of: date) -> bool:
    return bool(
        relation.effective_from <= as_of
        and (relation.effective_to is None or as_of < relation.effective_to)
    )


def _is_effective(relation: MaintenanceProjectContract, as_of: date) -> bool:
    return bool(relation.included_in_total and _is_current(relation, as_of))


def project_directory(
    db: Session,
    *,
    q_text: str | None,
    page: int,
    page_size: int,
    include_inactive: bool,
    as_of: date,
) -> dict:
    filters = []
    if not include_inactive:
        filters.append(MaintenanceProject.is_active.is_(True))
    if q_text and (search := q_text.strip()):
        filters.append(
            or_(
                MaintenanceProject.project_code.icontains(search, autoescape=True),
                MaintenanceProject.display_name.icontains(search, autoescape=True),
            )
        )

    total = db.scalar(select(func.count()).select_from(MaintenanceProject).where(*filters)) or 0
    projects = list(
        db.execute(
            select(MaintenanceProject)
            .where(*filters)
            .order_by(MaintenanceProject.project_code, MaintenanceProject.project_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars()
    )
    rows = [
        {
            "project_id": project.project_id,
            "project_code": project.project_code,
            "display_name": project.display_name,
            "project_manager_id": project.project_manager_id,
            "lifecycle_status": project.lifecycle_status,
            "is_active": project.is_active,
            "version": project.version,
        }
        for project in projects
    ]
    # Directory does not load contracts; its token covers only the returned identities.
    token_facts = [f"{row['project_id']}:{row['version']}" for row in rows]
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "as_of": as_of.isoformat(),
        "data_version": hashlib.sha256("|".join(token_facts).encode()).hexdigest(),
    }


def project_overview(
    db: Session,
    project_id: str,
    *,
    as_of: date,
    user_ctx: UserContext,
) -> dict | None:
    project = db.execute(
        select(MaintenanceProject)
        .options(selectinload(MaintenanceProject.contracts))
        .where(MaintenanceProject.project_id == project_id)
    ).scalar_one_or_none()
    if project is None:
        return None

    contracts = sorted(
        project.contracts,
        key=lambda row: (row.contract_no, row.effective_from, row.project_contract_id),
    )
    effective = [relation for relation in contracts if _is_effective(relation, as_of)]
    effective_ids = sorted({relation.contract_id for relation in effective})

    cross_project_conflicts: set[str] = set()
    if effective_ids:
        cross_project_conflicts = set(
            db.execute(
                select(MaintenanceProjectContract.contract_id)
                .where(
                    MaintenanceProjectContract.contract_id.in_(effective_ids),
                    MaintenanceProjectContract.included_in_total.is_(True),
                    MaintenanceProjectContract.effective_from <= as_of,
                    or_(
                        MaintenanceProjectContract.effective_to.is_(None),
                        MaintenanceProjectContract.effective_to > as_of,
                    ),
                )
                .group_by(MaintenanceProjectContract.contract_id)
                .having(func.count(func.distinct(MaintenanceProjectContract.project_id)) > 1)
            ).scalars()
        )

    issues: list[dict] = []
    if not effective:
        issues.append({"code": "no_effective_contracts", "contract_ids": []})

    repeated = sorted(
        contract_id
        for contract_id, count in Counter(row.contract_id for row in effective).items()
        if count > 1
    )
    if repeated:
        issues.append({"code": "duplicate_effective_contract", "contract_ids": repeated})

    unmapped = sorted(
        {
            row.contract_id
            for row in contracts
            if _is_current(row, as_of) and row.status_mapping_state != "mapped"
        }
    )
    if unmapped:
        issues.append({"code": "unmapped_contract_status", "contract_ids": unmapped})

    missing_amount = sorted(
        {row.contract_id for row in effective if row.contract_amount is None}
    )
    if missing_amount:
        issues.append({"code": "missing_contract_amount", "contract_ids": missing_amount})

    if cross_project_conflicts:
        issues.append(
            {
                "code": "cross_project_contract_conflict",
                "contract_ids": sorted(cross_project_conflicts),
            }
        )

    amount_restricted = is_field_hidden(user_ctx, "contract_amount")
    if amount_restricted:
        total_amount: Decimal | None = None
        completeness = {"status": "restricted", "issues": []}
    elif issues:
        total_amount = None
        completeness = {"status": "incomplete", "issues": issues}
    else:
        total_amount = sum(
            (relation.contract_amount for relation in effective),
            start=Decimal("0.00"),
        )
        completeness = {"status": "complete", "issues": []}

    return {
        "project": {
            "project_id": project.project_id,
            "project_code": project.project_code,
            "display_name": project.display_name,
            "project_manager_id": project.project_manager_id,
            "lifecycle_status": project.lifecycle_status,
            "is_active": project.is_active,
            "version": project.version,
        },
        "contracts": [
            {
                "project_contract_id": relation.project_contract_id,
                "contract_id": relation.contract_id,
                "contract_no": relation.contract_no,
                "contract_amount": None if amount_restricted else relation.contract_amount,
                "contract_status": relation.contract_status,
                "status_mapping_state": relation.status_mapping_state,
                "included_in_total": relation.included_in_total,
                "effective_from": relation.effective_from.isoformat(),
                "effective_to": (
                    relation.effective_to.isoformat() if relation.effective_to else None
                ),
                "is_effective": _is_effective(relation, as_of),
                "source": relation.source,
                "version": relation.version,
            }
            for relation in contracts
        ],
        "contract_count": len(contracts),
        "effective_contract_count": len(effective),
        "total_contract_amount": total_amount,
        "completeness": completeness,
        "as_of": as_of.isoformat(),
        "data_version": _version_token([project]),
    }
