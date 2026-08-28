"""Canonical XSDD ownership and project display aliases.

Business invariant: one normalized XSDD belongs to exactly one maintenance
project; one project may still own several XSDDs.  Existing ambiguous evidence
is never guessed or silently rewritten.
"""

from __future__ import annotations

import re
from uuid import uuid4

from sqlalchemy import and_, case, func, select, union_all
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAlias,
    MaintenanceProjectContract,
    MaintenanceProjectXsdd,
)
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectExpenseAttribution,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import project_names


class XsddProjectConflict(Exception):
    """One normalized XSDD already has evidence for another/multiple projects."""


_XSDD_IDENTITY_RE = re.compile(r"^[0-9]{8}-[0-9]{4}$")
_ASCII_WHITESPACE_RE = re.compile(r"[ \t\n\r\f\v]+")


def normalize_xsdd(value: str | None) -> str:
    """The single Python normalization entrypoint for XSDD project identity."""

    # Keep this byte-for-byte compatible with PostgreSQL ``\s`` rather than
    # Python's broader Unicode ``\s``.  Exotic whitespace remains invalid on
    # both sides instead of creating a Python-only identity that the trigger
    # cannot protect.
    normalized = _ASCII_WHITESPACE_RE.sub("", str(value or "")).upper()
    if normalized.startswith("XSDD-"):
        normalized = normalized[5:]
    return normalized if _XSDD_IDENTITY_RE.fullmatch(normalized) else ""


def normalized_xsdd_sql(column):
    """PostgreSQL expression kept identical to :func:`normalize_xsdd`."""

    normalized = func.regexp_replace(
        func.upper(func.regexp_replace(func.btrim(column), r"\s+", "", "g")),
        "^XSDD-",
        "",
    )
    return case(
        (normalized.op("~")(r"^[0-9]{8}-[0-9]{4}$"), normalized),
        else_="",
    )


def lock_xsdd_identities(db: Session, values) -> list[str]:
    """Serialize absent/present XSDD claims before workbook/project row locks."""

    identities = sorted({normalize_xsdd(value) for value in values if normalize_xsdd(value)})
    for identity in identities:
        db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(
            f"maintenance-project-xsdd:{identity}", 0,
        ))))
    return identities


def evidence_project_ids(db: Session, xsdd_norm: str) -> set[str]:
    """All current assignment/contract evidence for one normalized XSDD."""

    if not xsdd_norm:
        return set()
    assignment_ids = set(db.scalars(
        select(MaintenanceSourceOrderAssignment.project_id)
        .select_from(FMaintenanceOrder)
        .join(
            MaintenanceSourceOrderAssignment,
            and_(
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            ),
        )
        .where(
            FMaintenanceOrder.linked_sales_order_no.is_not(None),
            normalized_xsdd_sql(FMaintenanceOrder.linked_sales_order_no) == xsdd_norm,
        )
    ))
    # ``contract_id`` is an internal relation id and may retain a synthetic
    # ``xsdd-*`` fallback after the business contract number changes.  Only
    # contract_no is business evidence, matching migration backfill/trigger.
    contract_ids = set(db.scalars(
        select(MaintenanceProjectContract.project_id).where(
            normalized_xsdd_sql(MaintenanceProjectContract.contract_no) == xsdd_norm
        )
    ))
    return assignment_ids | contract_ids


def resolve_xsdd_project(db: Session, value: str | None) -> str | None:
    """Resolve one canonical owner; ambiguous historical evidence fails closed."""

    xsdd_norm = normalize_xsdd(value)
    if not xsdd_norm:
        return None
    mapped = db.get(MaintenanceProjectXsdd, xsdd_norm)
    evidence = evidence_project_ids(db, xsdd_norm)
    if mapped is not None:
        if evidence - {mapped.project_id}:
            raise XsddProjectConflict(
                f"XSDD {value} 已关联多个项目，需先完成历史归并预检"
            )
        return mapped.project_id
    if len(evidence) > 1:
        raise XsddProjectConflict(
            f"XSDD {value} 已关联多个项目，需先完成历史归并预检"
        )
    return next(iter(evidence), None)


def claim_xsdd_project(
    db: Session,
    *,
    value: str | None,
    project_id: str,
    source: str,
) -> str | None:
    """Claim an XSDD for ``project_id`` or reject conflicting evidence."""

    xsdd_norm = normalize_xsdd(value)
    if not xsdd_norm:
        return None
    lock_xsdd_identities(db, [xsdd_norm])
    resolved = resolve_xsdd_project(db, xsdd_norm)
    if resolved is not None and resolved != project_id:
        raise XsddProjectConflict(
            f"XSDD {value} 已归属于其他项目，不能再次拆分"
        )
    mapped = db.get(MaintenanceProjectXsdd, xsdd_norm)
    if mapped is None:
        db.add(MaintenanceProjectXsdd(
            xsdd_norm=xsdd_norm,
            project_id=project_id,
            source=source,
        ))
        db.flush()
    return xsdd_norm


def record_alias(
    db: Session,
    *,
    project_id: str,
    alias_name: str | None,
    source: str,
) -> None:
    """Idempotently retain a human-facing name without changing identity."""

    clean = " ".join(str(alias_name or "").split())
    if not clean:
        return
    alias_key = project_names.display_name_identity(clean)
    exists = db.scalar(select(MaintenanceProjectAlias.alias_id).where(
        MaintenanceProjectAlias.project_id == project_id,
        MaintenanceProjectAlias.alias_key == alias_key,
    ))
    if exists is not None:
        return
    db.add(MaintenanceProjectAlias(
        alias_id=str(uuid4()),
        project_id=project_id,
        alias_name=clean,
        alias_key=alias_key,
        source=source,
    ))
    db.flush()


def aliases_by_project(db: Session, project_ids: list[str]) -> dict[str, list[str]]:
    if not project_ids:
        return {}
    rows = db.execute(
        select(
            MaintenanceProjectAlias.project_id,
            MaintenanceProjectAlias.alias_name,
        )
        .where(MaintenanceProjectAlias.project_id.in_(project_ids))
        .order_by(MaintenanceProjectAlias.project_id, MaintenanceProjectAlias.alias_name)
    ).all()
    out: dict[str, list[str]] = {}
    for project_id, alias_name in rows:
        bucket = out.setdefault(project_id, [])
        if alias_name not in bucket:
            bucket.append(alias_name)
    return out


def preview_historical_conflicts(db: Session) -> dict:
    """Read-only XSDD conflict manifest; never chooses or mutates a survivor."""

    assignment_evidence = (
        select(
            normalized_xsdd_sql(FMaintenanceOrder.linked_sales_order_no).label(
                "xsdd_norm"
            ),
            MaintenanceSourceOrderAssignment.project_id.label("project_id"),
        )
        .select_from(FMaintenanceOrder)
        .join(
            MaintenanceSourceOrderAssignment,
            and_(
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            ),
        )
        .where(FMaintenanceOrder.linked_sales_order_no.is_not(None))
    )
    contract_evidence = select(
        normalized_xsdd_sql(MaintenanceProjectContract.contract_no).label(
            "xsdd_norm"
        ),
        MaintenanceProjectContract.project_id.label("project_id"),
    ).where(MaintenanceProjectContract.contract_no.is_not(None))
    evidence = union_all(assignment_evidence, contract_evidence).cte(
        "xsdd_project_evidence"
    )
    conflicts = (
        select(evidence.c.xsdd_norm)
        .where(evidence.c.xsdd_norm != "")
        .group_by(evidence.c.xsdd_norm)
        .having(func.count(func.distinct(evidence.c.project_id)) > 1)
        .cte("xsdd_conflicts")
    )
    memberships = db.execute(
        select(
            evidence.c.xsdd_norm,
            evidence.c.project_id,
        )
        .join(conflicts, conflicts.c.xsdd_norm == evidence.c.xsdd_norm)
        .group_by(evidence.c.xsdd_norm, evidence.c.project_id)
        .order_by(evidence.c.xsdd_norm, evidence.c.project_id)
    ).all()
    project_ids = sorted({project_id for _, project_id in memberships})
    projects = {
        row.project_id: row
        for row in db.scalars(
            select(MaintenanceProject).where(
                MaintenanceProject.project_id.in_(project_ids or {""})
            )
        )
    }
    aliases = aliases_by_project(db, project_ids)

    active_order_counts = dict(db.execute(
        select(
            MaintenanceSourceOrderAssignment.project_id,
            func.count(),
        )
        .where(
            MaintenanceSourceOrderAssignment.project_id.in_(project_ids or {""}),
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    ).all())
    contract_counts = dict(db.execute(
        select(MaintenanceProjectContract.project_id, func.count())
        .where(MaintenanceProjectContract.project_id.in_(project_ids or {""}))
        .group_by(MaintenanceProjectContract.project_id)
    ).all())
    collection_counts = dict(db.execute(
        select(MaintenanceCollectionSnapshot.project_id, func.count())
        .where(MaintenanceCollectionSnapshot.project_id.in_(project_ids or {""}))
        .group_by(MaintenanceCollectionSnapshot.project_id)
    ).all())
    milestone_counts = dict(db.execute(
        select(MaintenanceCollectionMilestone.project_id, func.count())
        .where(MaintenanceCollectionMilestone.project_id.in_(project_ids or {""}))
        .group_by(MaintenanceCollectionMilestone.project_id)
    ).all())
    expense_counts = dict(db.execute(
        select(MaintenanceProjectExpenseAttribution.project_id, func.count())
        .where(MaintenanceProjectExpenseAttribution.project_id.in_(project_ids or {""}))
        .group_by(MaintenanceProjectExpenseAttribution.project_id)
    ).all())

    contracts_by_project: dict[str, list[dict]] = {}
    for row in db.execute(
        select(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_id.in_(project_ids or {""}))
        .order_by(
            MaintenanceProjectContract.project_id,
            MaintenanceProjectContract.contract_no,
            MaintenanceProjectContract.project_contract_id,
        )
    ).scalars():
        contracts_by_project.setdefault(row.project_id, []).append({
            "project_contract_id": row.project_contract_id,
            "contract_no": row.contract_no,
            "amount_inc_tax": (
                str(row.amount_inc_tax) if row.amount_inc_tax is not None else None
            ),
            "source": row.source,
            "effective_from": row.effective_from.isoformat(),
            "effective_to": (
                row.effective_to.isoformat() if row.effective_to else None
            ),
        })
    collections_by_project: dict[str, list[dict]] = {}
    for row in db.execute(
        select(MaintenanceCollectionSnapshot)
        .where(MaintenanceCollectionSnapshot.project_id.in_(project_ids or {""}))
        .order_by(
            MaintenanceCollectionSnapshot.project_id,
            MaintenanceCollectionSnapshot.report_month,
            MaintenanceCollectionSnapshot.collection_id,
        )
    ).scalars():
        collections_by_project.setdefault(row.project_id, []).append({
            "collection_id": row.collection_id,
            "project_contract_id": row.project_contract_id,
            "report_month": row.report_month.isoformat(),
            "cumulative_amount": str(row.cumulative_amount),
            "status": row.status,
            "receipt_reference": row.receipt_reference,
            "version": row.version,
        })

    grouped: dict[str, list[str]] = {}
    for xsdd_norm, project_id in memberships:
        grouped.setdefault(xsdd_norm, []).append(project_id)
    rows: list[dict] = []
    for xsdd_norm, member_ids in grouped.items():
        members = []
        for project_id in member_ids:
            project = projects[project_id]
            members.append({
                "project_id": project_id,
                "project_code": project.project_code,
                "display_name": project.display_name,
                "aliases": aliases.get(project_id, []),
                "is_active": project.is_active,
                "created_at": project.created_at.isoformat(),
                "fact_counts": {
                    "active_orders": int(active_order_counts.get(project_id, 0)),
                    "contracts": int(contract_counts.get(project_id, 0)),
                    "collections": int(collection_counts.get(project_id, 0)),
                    "milestones": int(milestone_counts.get(project_id, 0)),
                    "expenses": int(expense_counts.get(project_id, 0)),
                },
                "contracts": contracts_by_project.get(project_id, []),
                "collections": collections_by_project.get(project_id, []),
            })
        rows.append({
            "xsdd_norm": xsdd_norm,
            "canonical_project_id": None,
            "requires_human_decision": True,
            "projects": members,
        })
    return {
        "mode": "read_only_preview",
        "conflict_count": len(rows),
        "project_count": len(project_ids),
        "conflicts": rows,
    }
