"""Read model for stable maintenance projects and their contract portfolio."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.security import UserContext, is_field_hidden
from app.services import maintenance_project_assignments


def _payload_token(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    user_ctx: UserContext | None = None,
) -> dict:
    filters = []
    if user_ctx is not None and maintenance_project_assignments.resolve_owner_scope(
        user_ctx,
        None,
    ) == "me":
        # 2026-08-21 行键收敛：own_maintenance_projects_only 开 → 负责人∪销售并集；
        # 未开键维持 #205 挂靠负责人口径
        from app.security import is_scoped_maintenance

        if is_scoped_maintenance(user_ctx):
            from app.services.maintenance_project_assignments import (
                maintenance_scope_project_ids,
            )

            allowed = maintenance_scope_project_ids(db, user_ctx) or set()
            filters.append(MaintenanceProject.project_id.in_(allowed or {""}))
        else:
            filters.append(
                maintenance_project_assignments.owned_project_condition(user_ctx)
            )
    if not include_inactive:
        filters.append(MaintenanceProject.is_active.is_(True))
    if q_text and (search := q_text.strip()):
        filters.append(
            or_(
                MaintenanceProject.project_code.icontains(search, autoescape=True),
                MaintenanceProject.display_name.icontains(search, autoescape=True),
            )
        )

    filtered_facts = list(
        db.execute(
            select(
                MaintenanceProject.project_id,
                MaintenanceProject.project_code,
                MaintenanceProject.display_name,
                MaintenanceProject.project_manager_id,
                MaintenanceProject.lifecycle_status,
                MaintenanceProject.is_active,
                MaintenanceProject.version,
            )
            .where(*filters)
            .order_by(MaintenanceProject.project_id)
        ).all()
    )
    total = len(filtered_facts)
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
            # 维保期限主数据（#39/#51），与 overview/catalog 的键集一致
            "period_from": project.period_from.isoformat() if project.period_from else None,
            "period_to": project.period_to.isoformat() if project.period_to else None,
            "lifecycle_status": project.lifecycle_status,
            "is_active": project.is_active,
            "version": project.version,
        }
        for project in projects
    ]
    payload = {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "as_of": as_of.isoformat(),
    }
    payload["data_version"] = _payload_token(
        {
            "as_of": payload["as_of"],
            "projects": [tuple(fact) for fact in filtered_facts],
        }
    )
    return payload


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

    cross_relationships: list[tuple[str, str, str]] = []
    if effective_ids:
        cross_relationships = [
            (contract_id, project_contract_id, related_project_id)
            for contract_id, project_contract_id, related_project_id in db.execute(
                select(
                    MaintenanceProjectContract.contract_id,
                    MaintenanceProjectContract.project_contract_id,
                    MaintenanceProjectContract.project_id,
                )
                .where(
                    MaintenanceProjectContract.contract_id.in_(effective_ids),
                    MaintenanceProjectContract.included_in_total.is_(True),
                    MaintenanceProjectContract.effective_from <= as_of,
                    or_(
                        MaintenanceProjectContract.effective_to.is_(None),
                        MaintenanceProjectContract.effective_to > as_of,
                    ),
                )
                .order_by(
                    MaintenanceProjectContract.contract_id,
                    MaintenanceProjectContract.project_id,
                    MaintenanceProjectContract.project_contract_id,
                )
            ).all()
        ]

    projects_by_contract: dict[str, set[str]] = {}
    for contract_id, _relationship_id, related_project_id in cross_relationships:
        projects_by_contract.setdefault(contract_id, set()).add(related_project_id)
    cross_project_conflicts = {
        contract_id
        for contract_id, project_ids in projects_by_contract.items()
        if len(project_ids) > 1
    }

    payload = project_overview_from_facts(
        project=project,
        contracts=contracts,
        cross_project_conflicts=cross_project_conflicts,
        as_of=as_of,
        user_ctx=user_ctx,
    )
    if payload is not None:
        # 项目级可见账号（2026-08-25）：面板「编辑基本信息」回显用
        # （挂在 project 子对象上，与前端 proj.visible_usernames 对齐）
        from app.services import maintenance_project_assignments as assignments

        payload["project"]["visible_usernames"] = [
            row["username"]
            for row in assignments.project_viewers(db, project_id=project_id)
        ]
    return payload


def project_overview_from_facts(
    *,
    project: MaintenanceProject,
    contracts: list[MaintenanceProjectContract],
    cross_project_conflicts: set[str],
    as_of: date,
    user_ctx: UserContext,
) -> dict:
    """Build one project overview from already-batched ORM facts.

    Both the detail read and operations directory call this pure assembler so
    contract completeness and visibility cannot drift between the two views.
    """

    contracts = sorted(
        contracts,
        key=lambda row: (row.contract_no, row.effective_from, row.project_contract_id),
    )
    effective = [relation for relation in contracts if _is_effective(relation, as_of)]

    issues: list[dict] = []
    if not effective:
        issues.append({"code": "no_effective_contracts", "contract_ids": []})

    repeated = sorted(
        contract_id
        for contract_id, count in Counter(row.contract_id for row in effective).items()
        if count > 1
    )
    if repeated:
        issues.append(
            {"code": "duplicate_effective_contract", "contract_ids": repeated}
        )

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
        {
            row.contract_id
            for row in effective
            if row.amount_inc_tax is None and row.contract_amount is None
        }
    )
    if missing_amount:
        issues.append(
            {"code": "missing_contract_amount", "contract_ids": missing_amount}
        )

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
        # 2026-08-26 客户拍板：合同总额一律显示含税。台账导入的
        # contract_amount 是对账后未税额（模型注释），回退时按 13% 归一
        # （CONTEXT.md 双税口径）补成含税，不再把未税数当含税展示。
        total_amount = sum(
            (
                relation.amount_inc_tax
                if relation.amount_inc_tax is not None
                else (
                    (relation.contract_amount * Decimal("1.13")).quantize(
                        Decimal("0.01"))
                    if relation.contract_amount is not None
                    else Decimal("0.00")
                )
                for relation in effective
            ),
            start=Decimal("0.00"),
        )
        completeness = {"status": "complete", "issues": []}

    project_payload = {
        "project_id": project.project_id,
        "project_code": project.project_code,
        "display_name": project.display_name,
        "project_manager_id": project.project_manager_id,
        # 维保期限主数据（#39/#51）：面板显示与编辑回读都走这份 payload
        "period_from": project.period_from.isoformat() if project.period_from else None,
        "period_to": project.period_to.isoformat() if project.period_to else None,
        "lifecycle_status": project.lifecycle_status,
        "is_active": project.is_active,
        "version": project.version,
    }
    contract_payload = [
        {
            "project_contract_id": relation.project_contract_id,
            "contract_id": relation.contract_id,
            "contract_no": relation.contract_no,
            "contract_amount": (
                None
                if amount_restricted
                else (
                    relation.amount_inc_tax
                    if relation.amount_inc_tax is not None
                    else (
                        (relation.contract_amount * Decimal("1.13")).quantize(
                            Decimal("0.01"))
                        if relation.contract_amount is not None
                        else None
                    )
                )
            ),
            "contract_amount_basis": "inc_tax",
            "contract_status": relation.contract_status,
            "status_mapping_state": relation.status_mapping_state,
            "status_mapping_version": relation.status_mapping_version,
            "included_in_total": relation.included_in_total,
            "effective_from": relation.effective_from.isoformat(),
            "effective_to": (
                relation.effective_to.isoformat() if relation.effective_to else None
            ),
            "is_effective": _is_effective(relation, as_of),
            "source": relation.source,
            "version": None if amount_restricted else relation.version,
        }
        for relation in contracts
    ]
    payload = {
        "project": project_payload,
        "contracts": contract_payload,
        "contract_count": len(contracts),
        "effective_contract_count": len(effective),
        "total_contract_amount": total_amount,
        "completeness": completeness,
        "as_of": as_of.isoformat(),
    }
    payload["data_version"] = _payload_token(payload)
    return payload
