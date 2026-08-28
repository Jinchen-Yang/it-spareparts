"""Canonical XSDD ownership and project display aliases.

Business invariant: one normalized XSDD belongs to exactly one maintenance
project; one project may still own several XSDDs. Historical splits use a
deterministic canonical candidate and only exact visible-business duplicates
may be physically removed with audit.
"""

from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import and_, case, func, select, union_all
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAlias,
    MaintenanceProjectAuditLog,
    MaintenanceProjectContract,
    MaintenanceProjectXsdd,
)
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectWorkbookOperation,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import project_names


class XsddProjectConflict(Exception):
    """One normalized XSDD already has evidence for another/multiple projects."""


class XsddExactDedupeConflict(Exception):
    """The locked facts no longer match a reviewed exact-duplicate plan."""


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


def _business_fingerprint(values: dict) -> str:
    """Stable exact-business fingerprint; technical provenance is excluded upstream."""

    payload = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _contract_business_values(row: MaintenanceProjectContract) -> dict:
    return {
        "contract_no": normalize_xsdd(row.contract_no) or row.contract_no.strip().upper(),
        "contract_id": row.contract_id,
        "contract_amount": (
            str(row.contract_amount) if row.contract_amount is not None else None
        ),
        "amount_inc_tax": (
            str(row.amount_inc_tax) if row.amount_inc_tax is not None else None
        ),
        "contract_status": row.contract_status,
        "status_mapping_state": row.status_mapping_state,
        "included_in_total": row.included_in_total,
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
    }


def _collection_business_values(
    row: MaintenanceCollectionSnapshot,
    *,
    contract: MaintenanceProjectContract,
) -> dict:
    """Visible receipt fact; source/import/version are technical provenance."""

    return {
        "contract_no": normalize_xsdd(contract.contract_no) or contract.contract_no.strip().upper(),
        "contract_effective_from": contract.effective_from.isoformat(),
        "report_month": row.report_month.isoformat(),
        "cumulative_amount": str(row.cumulative_amount),
        "status": row.status,
        "receipt_reference": row.receipt_reference,
        "remark": row.remark,
    }


def _exact_duplicate_clusters(
    rows,
    *,
    project_id_for,
    entity_id_for,
    values_for,
    canonical_project_id: str,
) -> list[dict]:
    grouped: dict[str, list[tuple[object, dict]]] = {}
    for row in rows:
        values = values_for(row)
        grouped.setdefault(_business_fingerprint(values), []).append((row, values))
    out: list[dict] = []
    for fingerprint, items in grouped.items():
        project_ids = {project_id_for(row) for row, _values in items}
        if len(items) < 2 or len(project_ids) < 2:
            continue
        ordered = sorted(
            items,
            key=lambda item: (
                project_id_for(item[0]) != canonical_project_id,
                entity_id_for(item[0]),
            ),
        )
        survivor = ordered[0][0]
        out.append({
            "fingerprint": fingerprint,
            "business_values": ordered[0][1],
            "survivor_id": entity_id_for(survivor),
            "duplicate_ids": [entity_id_for(row) for row, _values in ordered[1:]],
            "project_ids": sorted(project_ids),
        })
    return sorted(out, key=lambda item: item["fingerprint"])


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
    contract_rows = list(db.execute(
        select(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_id.in_(project_ids or {""}))
        .order_by(
            MaintenanceProjectContract.project_id,
            MaintenanceProjectContract.contract_no,
            MaintenanceProjectContract.project_contract_id,
        )
    ).scalars())
    contract_by_id = {row.project_contract_id: row for row in contract_rows}
    for row in contract_rows:
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
    collection_rows = list(db.execute(
        select(MaintenanceCollectionSnapshot)
        .where(MaintenanceCollectionSnapshot.project_id.in_(project_ids or {""}))
        .order_by(
            MaintenanceCollectionSnapshot.project_id,
            MaintenanceCollectionSnapshot.report_month,
            MaintenanceCollectionSnapshot.collection_id,
        )
    ).scalars())
    for row in collection_rows:
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
        mapped = db.get(MaintenanceProjectXsdd, xsdd_norm)
        if mapped is not None and mapped.project_id in member_ids:
            canonical_project_id = mapped.project_id
        else:
            canonical_project_id = sorted(
                member_ids,
                key=lambda project_id: (
                    not projects[project_id].is_active,
                    -int(active_order_counts.get(project_id, 0)),
                    -sum((
                        int(contract_counts.get(project_id, 0)),
                        int(collection_counts.get(project_id, 0)),
                        int(milestone_counts.get(project_id, 0)),
                        int(expense_counts.get(project_id, 0)),
                    )),
                    projects[project_id].created_at,
                    project_id,
                ),
            )[0]
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
            "canonical_project_id": canonical_project_id,
            "canonical_rule": "mapped_owner_else_active_orders_facts_created_at_id_v1",
            "requires_human_decision": False,
            "exact_duplicate_candidates": {
                "contracts": _exact_duplicate_clusters(
                    [row for row in contract_rows if row.project_id in member_ids],
                    project_id_for=lambda row: row.project_id,
                    entity_id_for=lambda row: row.project_contract_id,
                    values_for=_contract_business_values,
                    canonical_project_id=canonical_project_id,
                ),
                "collections": _exact_duplicate_clusters(
                    [row for row in collection_rows if row.project_id in member_ids],
                    project_id_for=lambda row: row.project_id,
                    entity_id_for=lambda row: row.collection_id,
                    values_for=lambda row: _collection_business_values(
                        row,
                        contract=contract_by_id[row.project_contract_id],
                    ),
                    canonical_project_id=canonical_project_id,
                ),
            },
            "projects": members,
        })
    return {
        "mode": "read_only_preview",
        "conflict_count": len(rows),
        "project_count": len(project_ids),
        "conflicts": rows,
    }


def apply_exact_collection_dedupe(
    db: Session,
    *,
    xsdd: str,
    operated_by: str,
) -> dict:
    """Delete only exact visible-business duplicate collection snapshots.

    This is the first guarded write step of project-container consolidation.
    It never sums money and never chooses between differing facts.  The caller
    owns commit/rollback so the deletion and audit remain one transaction.
    """

    from app import config

    xsdd_norm = normalize_xsdd(xsdd)
    if not xsdd_norm:
        raise XsddExactDedupeConflict("XSDD 格式无效")
    clean_operator = str(operated_by or "").strip()
    if not clean_operator:
        raise XsddExactDedupeConflict("操作人不能为空")

    db.execute(select(func.pg_advisory_xact_lock(
        config.DATA_CHANGE_ADVISORY_LOCK_KEY
    )))
    lock_xsdd_identities(db, [xsdd_norm])
    manifest = preview_historical_conflicts(db)
    conflict = next(
        (
            row for row in manifest["conflicts"]
            if row["xsdd_norm"] == xsdd_norm
        ),
        None,
    )
    if conflict is None:
        return {
            "xsdd_norm": xsdd_norm,
            "canonical_project_id": None,
            "deleted_collection_ids": [],
            "repointed_operation_count": 0,
        }

    member_ids = sorted(
        member["project_id"] for member in conflict["projects"]
    )
    locked_projects = list(db.scalars(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id.in_(member_ids))
        .order_by(MaintenanceProject.project_id)
        .with_for_update()
    ))
    if [row.project_id for row in locked_projects] != member_ids:
        raise XsddExactDedupeConflict("归并项目集合已变化，请重新预览")

    candidate_clusters = conflict["exact_duplicate_candidates"]["collections"]
    candidate_ids = sorted({
        entity_id
        for cluster in candidate_clusters
        for entity_id in [cluster["survivor_id"], *cluster["duplicate_ids"]]
    })
    locked_snapshots = {
        row.collection_id: row
        for row in db.scalars(
            select(MaintenanceCollectionSnapshot)
            .where(MaintenanceCollectionSnapshot.collection_id.in_(candidate_ids or {""}))
            .order_by(MaintenanceCollectionSnapshot.collection_id)
            .with_for_update()
        )
    }
    contract_ids = {
        row.project_contract_id for row in locked_snapshots.values()
    }
    locked_contracts = {
        row.project_contract_id: row
        for row in db.scalars(
            select(MaintenanceProjectContract)
            .where(MaintenanceProjectContract.project_contract_id.in_(contract_ids or {""}))
            .order_by(MaintenanceProjectContract.project_contract_id)
            .with_for_update()
        )
    }

    deleted_ids: list[str] = []
    audit_deleted: list[dict] = []
    repointed_operations = 0
    for cluster in candidate_clusters:
        survivor_id = cluster["survivor_id"]
        survivor = locked_snapshots.get(survivor_id)
        if survivor is None:
            raise XsddExactDedupeConflict("exact duplicate survivor 已变化")
        expected_fingerprint = cluster["fingerprint"]
        survivor_contract = locked_contracts.get(survivor.project_contract_id)
        if (
            survivor_contract is None
            or _business_fingerprint(_collection_business_values(
                survivor,
                contract=survivor_contract,
            )) != expected_fingerprint
        ):
            raise XsddExactDedupeConflict("exact duplicate survivor 字段已变化")
        for duplicate_id in cluster["duplicate_ids"]:
            duplicate = locked_snapshots.get(duplicate_id)
            duplicate_contract = (
                locked_contracts.get(duplicate.project_contract_id)
                if duplicate is not None else None
            )
            if (
                duplicate is None
                or duplicate_contract is None
                or _business_fingerprint(_collection_business_values(
                    duplicate,
                    contract=duplicate_contract,
                )) != expected_fingerprint
            ):
                raise XsddExactDedupeConflict(
                    f"exact duplicate {duplicate_id} 字段已变化"
                )
            repointed_operations += db.query(MaintenanceProjectWorkbookOperation).filter(
                MaintenanceProjectWorkbookOperation.entity_id == duplicate_id
            ).update(
                {MaintenanceProjectWorkbookOperation.entity_id: survivor_id},
                synchronize_session=False,
            )
            audit_deleted.append({
                "collection_id": duplicate.collection_id,
                "project_id": duplicate.project_id,
                "project_contract_id": duplicate.project_contract_id,
                **_collection_business_values(
                    duplicate,
                    contract=duplicate_contract,
                ),
            })
            deleted_ids.append(duplicate_id)
            db.delete(duplicate)

    canonical_project_id = conflict["canonical_project_id"]
    if deleted_ids:
        db.add(MaintenanceProjectAuditLog(
            project_id=canonical_project_id,
            entity_type="project",
            entity_id=canonical_project_id,
            action="xsdd_exact_dedupe",
            before_json={"deleted_collections": audit_deleted},
            after_json={
                "xsdd_norm": xsdd_norm,
                "survivor_collection_ids": [
                    cluster["survivor_id"] for cluster in candidate_clusters
                ],
                "repointed_operation_count": repointed_operations,
            },
            reason="同一 XSDD 项目容器归并：删除完整业务指纹一致的重复实收回款",
            operated_by=clean_operator[:64],
        ))
        db.flush()
    return {
        "xsdd_norm": xsdd_norm,
        "canonical_project_id": canonical_project_id,
        "deleted_collection_ids": deleted_ids,
        "repointed_operation_count": repointed_operations,
    }
