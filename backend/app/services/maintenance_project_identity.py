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
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import MetaData, Table, and_, case, exists, func, or_, select, text, union_all, update
from sqlalchemy.orm import Session

from app import config
from app.business_time import business_today
from app.db import Base
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceDemandTombstone,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAlias,
    MaintenanceProjectAuditLog,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
    MaintenanceProjectXsdd,
)
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectWorkbookOperation,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.maintenance_warehouse import MaintenanceWarehouseDocumentLink
from app.models.system import SysUser
from app.services import project_names
from app.services import maintenance_project_operations as operations


class XsddProjectConflict(Exception):
    """One normalized XSDD already has evidence for another/multiple projects."""


class XsddExactDedupeConflict(Exception):
    """The locked facts no longer match a reviewed exact-duplicate plan."""


class XsddProjectMergeConflict(Exception):
    """A reviewed project-container merge plan is stale or unsafe to apply."""


_XSDD_IDENTITY_RE = re.compile(r"^[0-9]{8}-[0-9]{3,4}$")
_ASCII_WHITESPACE_RE = re.compile(r"[ \t\n\r\f\v]+")
_PEER_ALIAS_SOURCES = frozenset({"xsdd_container_merge"})
_SALES_ALIAS_SOURCE_PREFIX = "sales_order_import:"


def sales_alias_source(value: str | None) -> str:
    """Embed the reviewed XSDD reference in an alias provenance value."""

    xsdd_norm = normalize_xsdd(value)
    if not xsdd_norm:
        raise XsddProjectConflict("销售项目名称缺少有效 XSDD 来源")
    return f"{_SALES_ALIAS_SOURCE_PREFIX}{xsdd_norm}"


def _peer_source_candidate(source: str) -> str | None:
    if source in _PEER_ALIAS_SOURCES:
        return ""
    if source.startswith(_SALES_ALIAS_SOURCE_PREFIX):
        xsdd_norm = normalize_xsdd(source.removeprefix(_SALES_ALIAS_SOURCE_PREFIX))
        return xsdd_norm or None
    return None

# This is deliberately explicit.  A schema migration which adds another
# direct maintenance_project FK must update this reviewed list; merge apply
# fails closed when a non-empty, unreviewed table is discovered at runtime.
_SUPPORTED_PROJECT_FK_TABLES = frozenset({
    "business_file_download_audit",
    "maintenance_acceptance_checklist_batch",
    "maintenance_acceptance_deliverable",
    "maintenance_acceptance_operation",
    "maintenance_bad_return",
    "maintenance_bad_return_command",
    "maintenance_bad_salvage",
    "maintenance_collection_milestone",
    "maintenance_collection_plan_source_binding",
    "maintenance_collection_snapshot",
    "maintenance_doc_head_row",
    "maintenance_front_stock",
    "maintenance_front_stock_ledger",
    "maintenance_historical_cost_baseline",
    "maintenance_inventory_opening_balance",
    "maintenance_manager_upload_batch_project",
    "maintenance_migration_discrepancy",
    "maintenance_migration_event",
    "maintenance_project_alias",
    "maintenance_project_audit_log",
    "maintenance_project_contract",
    "maintenance_project_cutover_plan",
    "maintenance_project_expense_attribution",
    "maintenance_project_operation_audit",
    "maintenance_project_user_assignment",
    "maintenance_project_workbook_operation",
    "maintenance_project_workbook_state",
    "maintenance_project_workbook_validation",
    "maintenance_project_xsdd",
    "maintenance_return_obligation",
    "maintenance_rkd_return_line",
    "maintenance_service_period",
    "maintenance_site_issue",
    "maintenance_site_issue_command",
    "maintenance_site_issue_delivery_source",
    "maintenance_site_issue_return_event",
    "maintenance_source_order_assignment",
    "replenishment_application",
    "replenishment_cart_draft",
})

_SPECIAL_PROJECT_FK_TABLES = frozenset({
    "business_file_download_audit",
    "maintenance_acceptance_operation",
    "maintenance_bad_return_command",
    "maintenance_migration_event",
    "maintenance_project_alias",
    "maintenance_project_audit_log",
    "maintenance_project_contract",
    "maintenance_project_operation_audit",
    "maintenance_project_user_assignment",
    "maintenance_project_workbook_operation",
    "maintenance_project_workbook_state",
    "maintenance_project_workbook_validation",
    "maintenance_project_xsdd",
    "maintenance_site_issue_command",
    "maintenance_site_issue_return_event",
    "maintenance_source_order_assignment",
    "replenishment_application",
})

_ALLOWED_ARCHIVED_SOURCE_RELATION_TABLES = frozenset({
    "business_file_download_audit",
    "maintenance_acceptance_operation",
    "maintenance_bad_return_command",
    "maintenance_migration_event",
    "maintenance_project_audit_log",
    "maintenance_project_operation_audit",
    "maintenance_project_user_assignment",
    "maintenance_project_workbook_operation",
    "maintenance_project_workbook_state",
    "maintenance_project_workbook_validation",
    "maintenance_site_issue_command",
    "maintenance_site_issue_return_event",
    "maintenance_source_order_assignment",
    "replenishment_application",
})

_GENERIC_REPARENT_TABLES = frozenset({
    "maintenance_acceptance_checklist_batch",
    "maintenance_acceptance_deliverable",
    "maintenance_bad_return",
    "maintenance_bad_salvage",
    "maintenance_collection_milestone",
    "maintenance_collection_plan_source_binding",
    "maintenance_collection_snapshot",
    "maintenance_doc_head_row",
    "maintenance_front_stock",
    "maintenance_front_stock_ledger",
    "maintenance_historical_cost_baseline",
    "maintenance_inventory_opening_balance",
    "maintenance_manager_upload_batch_project",
    "maintenance_migration_discrepancy",
    "maintenance_project_cutover_plan",
    "maintenance_project_expense_attribution",
    "maintenance_return_obligation",
    "maintenance_rkd_return_line",
    "maintenance_service_period",
    "maintenance_site_issue",
    "maintenance_site_issue_delivery_source",
    "replenishment_cart_draft",
})

if _SUPPORTED_PROJECT_FK_TABLES != (
    _GENERIC_REPARENT_TABLES | _SPECIAL_PROJECT_FK_TABLES
):
    raise RuntimeError("maintenance project merge FK classification is incomplete")
if not _ALLOWED_ARCHIVED_SOURCE_RELATION_TABLES <= _SPECIAL_PROJECT_FK_TABLES:
    raise RuntimeError("maintenance project merge residual classification is invalid")

# Project-scoped identities which would collapse after every member project_id
# is rewritten to the canonical id.  These are not duplicate-deletion rules:
# any collision is a hard stop because both rows must be preserved.
_PROJECT_SCOPED_UNIQUE_SPECS: tuple[tuple[str, tuple[str, ...], str | None], ...] = (
    ("maintenance_acceptance_deliverable", ("deliverable_type",), None),
    ("maintenance_bad_salvage", ("idempotency_key",), None),
    ("maintenance_front_stock", ("part_id", "warehouse_name"), None),
    ("maintenance_manager_upload_batch_project", ("batch_id",), None),
    ("maintenance_project_cutover_plan", ("run_id",), None),
    ("maintenance_project_expense_attribution", ("expense_ref",), None),
    ("maintenance_service_period", (), None),
    ("maintenance_site_issue", ("issue_no",), None),
    ("replenishment_cart_draft", ("owner_user_id",), None),
)


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
        (normalized.op("~")(r"^[0-9]{8}-[0-9]{3,4}$"), normalized),
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
            FMaintenanceOrder.data_status == config.ACTIVE_STATUS,
            ~exists(select(1).where(
                MaintenanceDemandTombstone.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceDemandTombstone.restored_at.is_(None),
            )),
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


def _xsdd_owner_evidence(
    db: Session,
    xsdd_norm: str,
) -> tuple[MaintenanceProjectXsdd | None, set[str], set[str]]:
    mapped = db.get(MaintenanceProjectXsdd, xsdd_norm)
    contract_ids = set(db.scalars(
        select(MaintenanceProjectContract.project_id).where(
            normalized_xsdd_sql(MaintenanceProjectContract.contract_no) == xsdd_norm
        )
    ))
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
            normalized_xsdd_sql(FMaintenanceOrder.linked_sales_order_no) == xsdd_norm,
            FMaintenanceOrder.data_status == config.ACTIVE_STATUS,
            ~exists(select(1).where(
                MaintenanceDemandTombstone.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceDemandTombstone.restored_at.is_(None),
            )),
        )
    ))
    return mapped, contract_ids, assignment_ids


def resolve_sales_xsdd_project(db: Session, value: str | None) -> str | None:
    """Resolve a sales-side project without treating WBDD as owner evidence."""

    xsdd_norm = normalize_xsdd(value)
    if not xsdd_norm:
        return None
    mapped, contract_ids, assignment_ids = _xsdd_owner_evidence(db, xsdd_norm)
    owner_ids = set(contract_ids)
    if mapped is not None:
        owner_ids.add(mapped.project_id)
    if len(owner_ids) > 1 or (owner_ids and assignment_ids - owner_ids):
        raise XsddProjectConflict(
            f"XSDD {value} 已关联多个项目，需先完成历史归并预检"
        )
    if not owner_ids and assignment_ids:
        raise XsddProjectConflict(
            f"XSDD {value} 只有 WBDD 归属、没有销售合同 owner，需先修复"
        )
    return next(iter(owner_ids), None)


def resolve_contract_xsdd_owner(db: Session, value: str | None) -> str | None:
    """Return the unique contract-backed owner; WBDD/mapping alone is insufficient."""

    xsdd_norm = normalize_xsdd(value)
    if not xsdd_norm:
        return None
    mapped, contract_ids, assignment_ids = _xsdd_owner_evidence(db, xsdd_norm)
    if mapped is not None:
        mapped_project = db.get(MaintenanceProject, mapped.project_id)
        if mapped_project is None or not mapped_project.is_active:
            raise XsddProjectConflict(
                f"XSDD {value} 的 canonical owner 项目已停用或不存在"
            )
    if len(contract_ids) > 1:
        raise XsddProjectConflict(
            f"XSDD {value} 存在多个销售合同 owner，需先完成历史归并预检"
        )
    owner = next(iter(contract_ids), None)
    if owner is not None and mapped is None:
        raise XsddProjectConflict(
            f"XSDD {value} 有销售合同但缺少 canonical owner 映射，需先修复"
        )
    if mapped is not None and owner is not None and mapped.project_id != owner:
        raise XsddProjectConflict(
            f"XSDD {value} 的映射与销售合同 owner 冲突"
        )
    if owner is None and assignment_ids:
        raise XsddProjectConflict(
            f"XSDD {value} 只有 WBDD 归属、没有销售合同 owner，需先修复"
        )
    if owner is not None and assignment_ids - {owner}:
        raise XsddProjectConflict(
            f"XSDD {value} 的 WBDD 归属与销售合同 owner 冲突"
        )
    return owner


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
) -> bool:
    """Idempotently retain a human-facing name without changing identity."""

    clean = " ".join(str(alias_name or "").split())
    if not clean:
        return False
    alias_key = project_names.display_name_identity(clean)
    exists = db.scalar(select(MaintenanceProjectAlias).where(
        MaintenanceProjectAlias.project_id == project_id,
        MaintenanceProjectAlias.alias_key == alias_key,
    ))
    if exists is not None:
        # Later sales/XSDD evidence may upgrade an old generic alias into a
        # proven peer name.  Never downgrade a proven source.
        if (
            _peer_source_candidate(source) is not None
            and _peer_source_candidate(exists.source) is None
        ):
            exists.source = source
            db.flush()
            return True
        return False
    db.add(MaintenanceProjectAlias(
        alias_id=str(uuid4()),
        project_id=project_id,
        alias_name=clean,
        alias_key=alias_key,
        source=source,
    ))
    db.flush()
    return True


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


def peer_names_by_project(db: Session, project_ids: list[str]) -> dict[str, list[str]]:
    """Names whose provenance proves they describe the same XSDD owner."""

    if not project_ids:
        return {}
    aliases = list(db.execute(
        select(
            MaintenanceProjectAlias.project_id,
            MaintenanceProjectAlias.alias_name,
            MaintenanceProjectAlias.alias_key,
            MaintenanceProjectAlias.source,
        )
        .where(MaintenanceProjectAlias.project_id.in_(project_ids))
        .order_by(MaintenanceProjectAlias.project_id, MaintenanceProjectAlias.alias_name)
    ))
    referenced_xsdds = {
        xsdd_norm
        for _project_id, _alias_name, _alias_key, source in aliases
        if (xsdd_norm := _peer_source_candidate(source))
    }
    mapped_pairs: set[tuple[str, str]] = set()
    contract_pairs: set[tuple[str, str]] = set()
    if referenced_xsdds:
        mapped_pairs = {
            (project_id, xsdd_norm)
            for project_id, xsdd_norm in db.execute(
                select(
                    MaintenanceProjectXsdd.project_id,
                    MaintenanceProjectXsdd.xsdd_norm,
                ).where(
                    MaintenanceProjectXsdd.xsdd_norm.in_(sorted(referenced_xsdds))
                )
            )
        }
        contract_norm = normalized_xsdd_sql(
            MaintenanceProjectContract.contract_no
        ).label("xsdd_norm")
        contract_pairs = {
            (project_id, xsdd_norm)
            for project_id, xsdd_norm in db.execute(
                select(MaintenanceProjectContract.project_id, contract_norm).where(
                    contract_norm.in_(sorted(referenced_xsdds))
                )
            )
        }
    out: dict[str, list[str]] = {}
    for project_id, alias_name, _alias_key, source in aliases:
        # ``source_order`` has no source_ref column, so it cannot prove which
        # WBDD row supplied an old alias.  Keep it secondary instead of
        # guessing from equal text.  Sales import and XSDD container merge are
        # the two provenance paths that do establish the same XSDD identity.
        xsdd_norm = _peer_source_candidate(source)
        if xsdd_norm is None:
            continue
        if xsdd_norm and (
            (project_id, xsdd_norm) not in mapped_pairs
            or (project_id, xsdd_norm) not in contract_pairs
        ):
            # A source string is only a claim.  The current canonical map and
            # sales contract must both prove the same project before UI may
            # promote this alias to a peer name.
            continue
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


def _canonical_manifest_value(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {"length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if isinstance(value, dict):
        return {
            str(key): _canonical_manifest_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_manifest_value(item) for item in value]
    return str(value)


def _rows_digest(rows) -> dict:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        mapping = row._mapping if hasattr(row, "_mapping") else row
        payload = {
            str(key): _canonical_manifest_value(value)
            for key, value in mapping.items()
        }
        digest.update(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return {"row_count": count, "sha256": digest.hexdigest()}


def _project_fk_catalog() -> dict[str, object]:
    # Ensure the registry is complete even when this service is imported by a
    # focused maintenance script rather than through the FastAPI app factory.
    import app.models  # noqa: F401

    return {
        table.name: table
        for table in Base.metadata.tables.values()
        if any(
            fk.target_fullname == "maintenance_project.project_id"
            for column in table.columns
            for fk in column.foreign_keys
        )
    }


def _database_project_fk_catalog(db: Session) -> list[tuple[str, str, str]]:
    """Read the live PostgreSQL FK graph, not only the ORM snapshot."""

    rows = db.execute(text(
        "SELECT child_ns.nspname AS table_schema, child.relname AS table_name, "
        "       child_col.attname AS column_name "
        "FROM pg_constraint constraint_row "
        "JOIN pg_class child ON child.oid = constraint_row.conrelid "
        "JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace "
        "JOIN pg_class parent ON parent.oid = constraint_row.confrelid "
        "JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace "
        "JOIN unnest(constraint_row.conkey) WITH ORDINALITY "
        "     AS child_key(attnum, ordinality) ON true "
        "JOIN unnest(constraint_row.confkey) WITH ORDINALITY "
        "     AS parent_key(attnum, ordinality) "
        "     ON parent_key.ordinality = child_key.ordinality "
        "JOIN pg_attribute child_col "
        "     ON child_col.attrelid = child.oid "
        "    AND child_col.attnum = child_key.attnum "
        "JOIN pg_attribute parent_col "
        "     ON parent_col.attrelid = parent.oid "
        "    AND parent_col.attnum = parent_key.attnum "
        "WHERE constraint_row.contype = 'f' "
        "  AND parent_ns.nspname = current_schema() "
        "  AND child_ns.nspname = current_schema() "
        "  AND parent.relname = 'maintenance_project' "
        "  AND parent_col.attname = 'project_id' "
        "ORDER BY child_ns.nspname, child.relname, child_col.attname"
    )).all()
    return [(str(schema), str(table), str(column)) for schema, table, column in rows]


def _reflected_fk_table(db: Session, *, schema: str, table_name: str):
    orm_table = _project_fk_catalog().get(table_name)
    # The test harness and some installations put the application tables in a
    # dedicated current_schema().  ORM tables are intentionally unqualified
    # and therefore resolve through that same search_path.
    if orm_table is not None:
        return orm_table
    return Table(
        table_name,
        MetaData(),
        schema=schema,
        autoload_with=db.get_bind(),
    )


def _ordered_table_rows(db: Session, table, predicate) -> list:
    primary_key = list(table.primary_key.columns)
    statement = select(table).where(predicate)
    if primary_key:
        statement = statement.order_by(*primary_key)
    return list(db.execute(statement))


def _merge_inventory(db: Session, member_ids: list[str]) -> dict:
    """Hash every known direct/indirect fact used by project consolidation."""

    catalog = _project_fk_catalog()
    inventory: dict[str, dict] = {}
    project_table = MaintenanceProject.__table__
    inventory[project_table.name] = _rows_digest(_ordered_table_rows(
        db,
        project_table,
        project_table.c.project_id.in_(member_ids),
    ))
    for table_name in sorted(catalog):
        table = catalog[table_name]
        if table_name == "maintenance_project_workbook_state":
            # Lock acquisition creates a revision-0 state for a legacy project.
            # Treat that as semantically identical to an absent row so the lock
            # envelope itself cannot invalidate a just-reviewed manifest.
            revisions = dict(db.execute(
                select(table.c.project_id, table.c.revision).where(
                    table.c.project_id.in_(member_ids)
                )
            ).all())
            inventory[table_name] = _rows_digest([
                {"project_id": project_id, "revision": int(revisions.get(project_id, 0))}
                for project_id in member_ids
            ])
            continue
        inventory[table_name] = _rows_digest(_ordered_table_rows(
            db,
            table,
            table.c.project_id.in_(member_ids),
        ))

    source_order_ids = sorted(set(db.scalars(
        select(MaintenanceSourceOrderAssignment.source_order_id).where(
            MaintenanceSourceOrderAssignment.project_id.in_(member_ids)
        )
    )))
    order_table = FMaintenanceOrder.__table__
    order_rows = _ordered_table_rows(
        db,
        order_table,
        order_table.c.raw_order_id.in_(source_order_ids or {""}),
    )
    inventory[order_table.name] = _rows_digest(order_rows)
    order_ids = [row._mapping["id"] for row in order_rows]
    line_table = FMaintenanceLine.__table__
    inventory[line_table.name] = _rows_digest(_ordered_table_rows(
        db,
        line_table,
        line_table.c.order_id.in_(order_ids or {-1}),
    ))

    warehouse_table = MaintenanceWarehouseDocumentLink.__table__
    inventory[f"{warehouse_table.name}:project_target"] = _rows_digest(
        _ordered_table_rows(
            db,
            warehouse_table,
            and_(
                warehouse_table.c.target_type == "maintenance_project",
                warehouse_table.c.target_id.in_(member_ids),
            ),
        )
    )
    return dict(sorted(inventory.items()))


def _manifest_hash(*, conflict: dict, inventory: dict) -> str:
    payload = {
        "xsdd_norm": conflict["xsdd_norm"],
        "canonical_project_id": conflict["canonical_project_id"],
        "canonical_rule": conflict["canonical_rule"],
        "requires_human_decision": conflict["requires_human_decision"],
        "member_project_ids": sorted(
            row["project_id"] for row in conflict["projects"]
        ),
        "exact_duplicate_candidates": conflict["exact_duplicate_candidates"],
        "inventory": inventory,
    }
    return _business_fingerprint(payload)


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
        .where(
            FMaintenanceOrder.linked_sales_order_no.is_not(None),
            FMaintenanceOrder.data_status == config.ACTIVE_STATUS,
            ~exists(select(1).where(
                MaintenanceDemandTombstone.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceDemandTombstone.restored_at.is_(None),
            )),
        )
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
        .select_from(MaintenanceSourceOrderAssignment)
        .join(
            FMaintenanceOrder,
            FMaintenanceOrder.raw_order_id
            == MaintenanceSourceOrderAssignment.source_order_id,
        )
        .where(
            MaintenanceSourceOrderAssignment.project_id.in_(project_ids or {""}),
            MaintenanceSourceOrderAssignment.is_active.is_(True),
            FMaintenanceOrder.data_status == config.ACTIVE_STATUS,
            ~exists(select(1).where(
                MaintenanceDemandTombstone.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceDemandTombstone.restored_at.is_(None),
            )),
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
        contract_owner_project_ids = sorted({
            row.project_id
            for row in contract_rows
            if normalize_xsdd(row.contract_no) == xsdd_norm
        })
        unique_contract_owner = (
            contract_owner_project_ids[0]
            if len(contract_owner_project_ids) == 1
            else None
        )
        mapping_contract_conflict = bool(
            unique_contract_owner is not None
            and mapped is not None
            and mapped.project_id != unique_contract_owner
        )
        if unique_contract_owner is not None:
            canonical_project_id = unique_contract_owner
            canonical_rule = "unique_contract_owner_v1"
        elif mapped is not None and mapped.project_id in member_ids:
            canonical_project_id = mapped.project_id
            canonical_rule = "mapped_owner_else_active_orders_facts_created_at_id_v1"
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
            canonical_rule = "mapped_owner_else_active_orders_facts_created_at_id_v1"
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
        conflict = {
            "xsdd_norm": xsdd_norm,
            "canonical_project_id": canonical_project_id,
            "canonical_rule": canonical_rule,
            "mapped_project_id": mapped.project_id if mapped is not None else None,
            "contract_owner_project_ids": contract_owner_project_ids,
            # No automatic or reviewed merge may silently overturn an
            # existing map which disagrees with the unique sales-contract
            # owner.  That inconsistency needs an explicit repair path.
            "requires_human_decision": mapping_contract_conflict,
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
        }
        inventory = _merge_inventory(db, sorted(member_ids))
        conflict["merge_inventory"] = inventory
        conflict["manifest_hash"] = _manifest_hash(
            conflict=conflict,
            inventory=inventory,
        )
        rows.append(conflict)
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


def _project_unique_collision_check(
    db: Session,
    *,
    member_ids: list[str],
) -> None:
    catalog = _project_fk_catalog()
    for table_name, key_names, predicate in _PROJECT_SCOPED_UNIQUE_SPECS:
        table = catalog[table_name]
        key_columns = [table.c[name] for name in key_names]
        statement = (
            select(*key_columns, func.count().label("row_count"))
            .where(table.c.project_id.in_(member_ids))
        )
        for column in key_columns:
            statement = statement.where(column.is_not(None))
        if predicate:
            statement = statement.where(text(predicate))
        if key_columns:
            statement = statement.group_by(*key_columns)
        statement = statement.having(func.count() > 1).limit(1)
        collision = db.execute(statement).first()
        if collision is not None:
            raise XsddProjectMergeConflict(
                f"{table_name} 的项目级唯一事实归并后冲突，不能自动删除或覆盖"
            )


def _parse_user_assignment_resolution(
    db: Session,
    *,
    member_ids: list[str],
    canonical_project_id: str,
    resolution: dict | None,
) -> dict:
    active = list(db.scalars(
        select(MaintenanceProjectUserAssignment)
        .where(
            MaintenanceProjectUserAssignment.project_id.in_(member_ids),
            MaintenanceProjectUserAssignment.archived_at.is_(None),
        )
        .order_by(MaintenanceProjectUserAssignment.assignment_id)
    ))
    canonical_active = [
        row for row in active if row.project_id == canonical_project_id
    ]
    source_active = [
        row for row in active if row.project_id != canonical_project_id
    ]
    if not source_active and resolution is None:
        return {
            "keep_assignment_ids": sorted(row.assignment_id for row in canonical_active),
            "archive_assignment_ids": [],
            "create_on_canonical": [],
        }
    if not isinstance(resolution, dict):
        raise XsddProjectMergeConflict(
            "源项目存在 active 用户关系，必须显式提交归档/重建方案"
        )
    active_by_id = {row.assignment_id: row for row in active}
    keep_ids = sorted({
        str(value).strip()
        for value in resolution.get("keep_assignment_ids", [])
        if str(value).strip()
    })
    archive_ids = sorted({
        str(value).strip()
        for value in resolution.get("archive_assignment_ids", [])
        if str(value).strip()
    })
    if set(keep_ids) != {row.assignment_id for row in canonical_active}:
        raise XsddProjectMergeConflict(
            "keep_assignment_ids 必须精确覆盖 canonical 现有 active 用户关系"
        )
    if set(archive_ids) != {row.assignment_id for row in source_active}:
        raise XsddProjectMergeConflict(
            "archive_assignment_ids 必须精确覆盖 source 全部 active 用户关系"
        )
    raw_creates = resolution.get("create_on_canonical", [])
    if not isinstance(raw_creates, list):
        raise XsddProjectMergeConflict("create_on_canonical 必须为数组")
    creates: list[dict] = []
    seen_sources: set[str] = set()
    for item in raw_creates:
        if not isinstance(item, dict):
            raise XsddProjectMergeConflict("create_on_canonical 元素格式无效")
        source_assignment_id = str(item.get("source_assignment_id") or "").strip()
        source = active_by_id.get(source_assignment_id)
        responsibility_type = str(item.get("responsibility_type") or "").strip()
        try:
            user_id = int(item.get("user_id"))
        except (TypeError, ValueError) as exc:
            raise XsddProjectMergeConflict("create_on_canonical user_id 无效") from exc
        if (
            source is None
            or source_assignment_id not in archive_ids
            or source_assignment_id in seen_sources
            or responsibility_type != source.responsibility_type
            or user_id != source.user_id
        ):
            raise XsddProjectMergeConflict(
                "canonical 新关系必须逐条继承被归档 source 的角色和账号"
            )
        user = db.get(SysUser, user_id)
        if user is None or not user.is_active:
            raise XsddProjectMergeConflict("不能在 canonical 新建停用或不存在的账号关系")
        seen_sources.add(source_assignment_id)
        creates.append({
            "source_assignment_id": source_assignment_id,
            "responsibility_type": responsibility_type,
            "user_id": user_id,
            "source_manager_text": source.source_manager_text,
        })

    resulting = [
        (row.responsibility_type, row.user_id) for row in canonical_active
    ] + [(row["responsibility_type"], row["user_id"]) for row in creates]
    primary_count = sum(kind == "primary_manager" for kind, _user_id in resulting)
    viewer_users = [user_id for kind, user_id in resulting if kind == "viewer"]
    if primary_count > 1 or len(viewer_users) != len(set(viewer_users)):
        raise XsddProjectMergeConflict("用户关系方案在 canonical 上产生唯一键冲突")
    resulting_identities = set(resulting)
    users = {
        row.id: row
        for row in db.scalars(select(SysUser).where(
            SysUser.id.in_({row.user_id for row in source_active} or {-1})
        ))
    }
    lost_active_permissions = [
        row.assignment_id
        for row in source_active
        if users.get(row.user_id) is not None
        and users[row.user_id].is_active
        and (row.responsibility_type, row.user_id) not in resulting_identities
    ]
    if lost_active_permissions:
        raise XsddProjectMergeConflict(
            "active 账号的 source 权限必须在 canonical 原样保留: "
            f"{sorted(lost_active_permissions)}"
        )
    return {
        "keep_assignment_ids": keep_ids,
        "archive_assignment_ids": archive_ids,
        "create_on_canonical": creates,
    }


def _unsupported_project_fk_rows(
    db: Session,
    *,
    source_project_ids: list[str],
) -> list[str]:
    unsupported: list[str] = []
    orm_catalog = _project_fk_catalog()
    for schema, table_name, column_name in _database_project_fk_catalog(db):
        if (
            table_name in _GENERIC_REPARENT_TABLES
            or table_name in _SPECIAL_PROJECT_FK_TABLES
        ):
            if table_name in orm_catalog and column_name == "project_id":
                continue
        table = _reflected_fk_table(db, schema=schema, table_name=table_name)
        exists = db.scalar(
            select(func.count())
            .select_from(table)
            .where(table.c[column_name].in_(source_project_ids))
        )
        if int(exists or 0) > 0:
            unsupported.append(f"{schema}.{table_name}.{column_name}")
    return unsupported


def _remaining_source_fk_rows(
    db: Session,
    *,
    source_project_ids: list[str],
) -> dict[str, int]:
    remaining: dict[str, int] = {}
    for schema, table_name, column_name in _database_project_fk_catalog(db):
        if table_name in _ALLOWED_ARCHIVED_SOURCE_RELATION_TABLES:
            continue
        table = _reflected_fk_table(db, schema=schema, table_name=table_name)
        count = int(db.scalar(
            select(func.count())
            .select_from(table)
            .where(table.c[column_name].in_(source_project_ids))
        ) or 0)
        if count:
            remaining[f"{schema}.{table_name}.{column_name}"] = count
    return remaining


def _validate_source_xsdd_scope(
    db: Session,
    *,
    source_project_ids: list[str],
    member_ids: list[str],
) -> None:
    """A source container may not silently absorb a second conflict graph."""

    contract_values = db.scalars(
        select(MaintenanceProjectContract.contract_no).where(
            MaintenanceProjectContract.project_id.in_(source_project_ids)
        )
    )
    assignment_values = db.scalars(
        select(FMaintenanceOrder.linked_sales_order_no)
        .join(
            MaintenanceSourceOrderAssignment,
            and_(
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            ),
        )
        .where(
            MaintenanceSourceOrderAssignment.project_id.in_(source_project_ids),
        )
    )
    identities = {
        normalized
        for value in [*contract_values, *assignment_values]
        if (normalized := normalize_xsdd(value))
    }
    member_set = set(member_ids)
    for identity in sorted(identities):
        outside = evidence_project_ids(db, identity) - member_set
        if outside:
            raise XsddProjectMergeConflict(
                f"源项目还参与 XSDD {identity} 的另一冲突图 {sorted(outside)}，"
                "必须合并预览后统一处理"
            )


def _xsdd_identities_for_projects(db: Session, project_ids: list[str]) -> set[str]:
    values = list(db.scalars(
        select(MaintenanceProjectContract.contract_no).where(
            MaintenanceProjectContract.project_id.in_(project_ids)
        )
    ))
    values.extend(db.scalars(
        select(FMaintenanceOrder.linked_sales_order_no)
        .join(
            MaintenanceSourceOrderAssignment,
            and_(
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            ),
        )
        .where(MaintenanceSourceOrderAssignment.project_id.in_(project_ids))
    ))
    values.extend(db.scalars(
        select(MaintenanceProjectXsdd.xsdd_norm).where(
            MaintenanceProjectXsdd.project_id.in_(project_ids)
        )
    ))
    return {
        identity for value in values if (identity := normalize_xsdd(value))
    }


def _parse_contract_resolution(
    db: Session,
    *,
    conflict: dict,
    resolution: dict | None,
) -> dict:
    """Validate the only permitted business decision before any merge write."""

    member_ids = sorted(row["project_id"] for row in conflict["projects"])
    xsdd_norm = conflict["xsdd_norm"]
    today = business_today()
    contracts = list(db.scalars(
        select(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_id.in_(member_ids))
        .order_by(MaintenanceProjectContract.project_contract_id)
    ))
    contracts_by_id = {row.project_contract_id: row for row in contracts}
    xsdd_contracts = [
        row for row in contracts if normalize_xsdd(row.contract_no) == xsdd_norm
    ]
    current = [
        row for row in xsdd_contracts
        if row.effective_from <= today
        and (row.effective_to is None or row.effective_to > today)
    ]
    if len(current) <= 1 and resolution is None:
        return {
            "current_project_contract_id": (
                current[0].project_contract_id if current else None
            ),
            "archive_contracts": [],
            "collection_contract_repoints": [],
        }
    if not isinstance(resolution, dict):
        raise XsddProjectMergeConflict(
            f"XSDD {xsdd_norm} 当前存在多条生效合同，必须显式提交消歧方案"
        )

    current_id = str(resolution.get("current_project_contract_id") or "").strip()
    current_row = contracts_by_id.get(current_id)
    if (
        current_row is None
        or current_row not in current
        or not current_row.included_in_total
    ):
        raise XsddProjectMergeConflict("指定 current 合同必须已生效且计入合同总额")

    raw_archives = resolution.get("archive_contracts")
    if not isinstance(raw_archives, list):
        raise XsddProjectMergeConflict("archive_contracts 必须为数组")
    archives: list[dict] = []
    archive_ids: set[str] = set()
    for item in raw_archives:
        if not isinstance(item, dict):
            raise XsddProjectMergeConflict("archive_contracts 元素格式无效")
        contract_id = str(item.get("project_contract_id") or "").strip()
        row = contracts_by_id.get(contract_id)
        if row is None or row not in current or contract_id == current_id:
            raise XsddProjectMergeConflict("归档合同不属于本次待消歧 current 集合")
        if contract_id in archive_ids:
            raise XsddProjectMergeConflict("归档合同重复")
        try:
            effective_to = date.fromisoformat(str(item.get("effective_to") or ""))
        except ValueError as exc:
            raise XsddProjectMergeConflict("归档合同 effective_to 格式无效") from exc
        if effective_to <= row.effective_from or effective_to > today:
            raise XsddProjectMergeConflict(
                "归档合同 effective_to 必须晚于起始日且不晚于业务日"
            )
        archive_ids.add(contract_id)
        archives.append({
            "project_contract_id": contract_id,
            "effective_to": effective_to,
        })
    expected_archives = {
        row.project_contract_id for row in current if row.project_contract_id != current_id
    }
    if archive_ids != expected_archives:
        raise XsddProjectMergeConflict("消歧方案必须覆盖除 current 外全部生效合同")

    raw_repoints = resolution.get("collection_contract_repoints", [])
    if not isinstance(raw_repoints, list):
        raise XsddProjectMergeConflict("collection_contract_repoints 必须为数组")
    collections = list(db.scalars(
        select(MaintenanceCollectionSnapshot)
        .where(MaintenanceCollectionSnapshot.project_id.in_(member_ids))
        .order_by(MaintenanceCollectionSnapshot.collection_id)
    ))
    collections_by_id = {row.collection_id: row for row in collections}
    duplicate_ids = {
        duplicate_id
        for cluster in conflict["exact_duplicate_candidates"]["collections"]
        for duplicate_id in cluster["duplicate_ids"]
    }
    repoints: list[dict] = []
    repointed_ids: set[str] = set()
    for item in raw_repoints:
        if not isinstance(item, dict):
            raise XsddProjectMergeConflict("collection_contract_repoints 元素格式无效")
        collection_id = str(item.get("collection_id") or "").strip()
        target_id = str(item.get("target_project_contract_id") or "").strip()
        collection = collections_by_id.get(collection_id)
        if (
            collection is None
            or collection_id in duplicate_ids
            or collection_id in repointed_ids
            or target_id != current_id
        ):
            raise XsddProjectMergeConflict("回款重指方案包含无效或将被删除的记录")
        source_contract = contracts_by_id[collection.project_contract_id]
        if (
            normalize_xsdd(source_contract.contract_no)
            != normalize_xsdd(current_row.contract_no)
            or source_contract.effective_from != current_row.effective_from
        ):
            raise XsddProjectMergeConflict("回款只能重指到同合同号、同起始日的 current 合同")
        repointed_ids.add(collection_id)
        repoints.append({
            "collection_id": collection_id,
            "target_project_contract_id": target_id,
        })

    required_repoints = {
        cluster["survivor_id"]
        for cluster in conflict["exact_duplicate_candidates"]["collections"]
        if collections_by_id[cluster["survivor_id"]].project_contract_id in archive_ids
    }
    if not required_repoints.issubset(repointed_ids):
        raise XsddProjectMergeConflict(
            "exact 回款 survivor 仍指向将归档合同，必须显式重指 current 合同"
        )

    projected: set[tuple[str, date]] = set()
    target_by_collection = {
        row["collection_id"]: row["target_project_contract_id"] for row in repoints
    }
    for collection in collections:
        if collection.collection_id in duplicate_ids:
            continue
        key = (
            target_by_collection.get(
                collection.collection_id,
                collection.project_contract_id,
            ),
            collection.report_month,
        )
        if key in projected:
            raise XsddProjectMergeConflict("回款重指后同合同同月份出现非 exact 冲突")
        projected.add(key)

    return {
        "current_project_contract_id": current_id,
        "archive_contracts": archives,
        "collection_contract_repoints": repoints,
    }


def _dedupe_exact_collections_locked(db: Session, *, conflict: dict) -> dict:
    candidate_clusters = conflict["exact_duplicate_candidates"]["collections"]
    candidate_ids = sorted({
        entity_id
        for cluster in candidate_clusters
        for entity_id in [cluster["survivor_id"], *cluster["duplicate_ids"]]
    })
    snapshots = {
        row.collection_id: row
        for row in db.scalars(
            select(MaintenanceCollectionSnapshot)
            .where(MaintenanceCollectionSnapshot.collection_id.in_(candidate_ids or {""}))
            .order_by(MaintenanceCollectionSnapshot.collection_id)
            .with_for_update()
        )
    }
    contract_ids = {row.project_contract_id for row in snapshots.values()}
    contracts = {
        row.project_contract_id: row
        for row in db.scalars(
            select(MaintenanceProjectContract)
            .where(MaintenanceProjectContract.project_contract_id.in_(contract_ids or {""}))
            .order_by(MaintenanceProjectContract.project_contract_id)
            .with_for_update()
        )
    }
    deleted_ids: list[str] = []
    deleted_payloads: list[dict] = []
    repointed_operations = 0
    for cluster in candidate_clusters:
        survivor = snapshots.get(cluster["survivor_id"])
        survivor_contract = (
            contracts.get(survivor.project_contract_id) if survivor is not None else None
        )
        if (
            survivor is None
            or survivor_contract is None
            or _business_fingerprint(_collection_business_values(
                survivor,
                contract=survivor_contract,
            )) != cluster["fingerprint"]
        ):
            raise XsddProjectMergeConflict("exact 回款 survivor 已漂移")
        for duplicate_id in cluster["duplicate_ids"]:
            duplicate = snapshots.get(duplicate_id)
            duplicate_contract = (
                contracts.get(duplicate.project_contract_id) if duplicate is not None else None
            )
            if (
                duplicate is None
                or duplicate_contract is None
                or _business_fingerprint(_collection_business_values(
                    duplicate,
                    contract=duplicate_contract,
                )) != cluster["fingerprint"]
            ):
                raise XsddProjectMergeConflict(f"exact 回款 {duplicate_id} 已漂移")
            repointed_operations += db.query(MaintenanceProjectWorkbookOperation).filter(
                MaintenanceProjectWorkbookOperation.entity_id == duplicate_id
            ).update(
                {MaintenanceProjectWorkbookOperation.entity_id: survivor.collection_id},
                synchronize_session=False,
            )
            deleted_payloads.append({
                "collection_id": duplicate.collection_id,
                "project_id": duplicate.project_id,
                "project_contract_id": duplicate.project_contract_id,
                **_collection_business_values(duplicate, contract=duplicate_contract),
            })
            deleted_ids.append(duplicate.collection_id)
            db.delete(duplicate)
    db.flush()
    return {
        "deleted_collection_ids": deleted_ids,
        "deleted_collections": deleted_payloads,
        "repointed_operation_count": repointed_operations,
    }


def _merged_contract_id(
    *,
    original: str,
    project_contract_id: str,
    occupied: set[tuple[str, date]],
    effective_from: date,
) -> str:
    suffix = hashlib.sha256(project_contract_id.encode("utf-8")).hexdigest()[:12]
    stem = original[: max(1, 64 - len(suffix) - 3)]
    candidate = f"{stem}~m-{suffix}"
    counter = 1
    while (candidate, effective_from) in occupied:
        counter_suffix = f"-{counter}"
        candidate = f"{stem[:64-len(suffix)-3-len(counter_suffix)]}~m-{suffix}{counter_suffix}"
        counter += 1
    occupied.add((candidate, effective_from))
    return candidate


def _apply_locked_project_merge(
    db: Session,
    *,
    conflict: dict,
    resolution: dict,
    user_assignment_resolution: dict,
    locked_projects: dict[str, MaintenanceProject],
    locked_states: dict,
    operated_by: str,
    merge_batch_id: str,
) -> dict:
    xsdd_norm = conflict["xsdd_norm"]
    canonical_id = conflict["canonical_project_id"]
    member_ids = sorted(row["project_id"] for row in conflict["projects"])
    source_ids = [project_id for project_id in member_ids if project_id != canonical_id]
    canonical = locked_projects[canonical_id]
    source_projects = [locked_projects[project_id] for project_id in source_ids]

    dedupe = _dedupe_exact_collections_locked(db, conflict=conflict)

    # Preserve every visible project name.  Existing alias identities are
    # repointed; only duplicate alias identities are removed.
    for project in [canonical, *source_projects]:
        record_alias(
            db,
            project_id=canonical_id,
            alias_name=project.display_name,
            source="xsdd_container_merge",
        )
    canonical_alias_keys = set(db.scalars(
        select(MaintenanceProjectAlias.alias_key).where(
            MaintenanceProjectAlias.project_id == canonical_id
        )
    ))
    deleted_alias_ids: list[str] = []
    source_aliases = list(db.scalars(
        select(MaintenanceProjectAlias)
        .where(MaintenanceProjectAlias.project_id.in_(source_ids))
        .order_by(MaintenanceProjectAlias.alias_id)
        .with_for_update()
    ))
    for alias in source_aliases:
        if alias.alias_key in canonical_alias_keys:
            deleted_alias_ids.append(alias.alias_id)
            db.delete(alias)
        else:
            alias.project_id = canonical_id
            canonical_alias_keys.add(alias.alias_key)
    db.flush()

    now = datetime.now(timezone.utc)
    archived_user_assignments: list[dict] = []
    archive_user_assignment_ids = user_assignment_resolution[
        "archive_assignment_ids"
    ]
    if archive_user_assignment_ids:
        manager_rows = list(db.scalars(
            select(MaintenanceProjectUserAssignment)
            .where(MaintenanceProjectUserAssignment.assignment_id.in_(
                archive_user_assignment_ids
            ))
            .order_by(MaintenanceProjectUserAssignment.assignment_id)
            .with_for_update()
        ))
        if [row.assignment_id for row in manager_rows] != archive_user_assignment_ids:
            raise XsddProjectMergeConflict("项目用户关系已漂移")
        for assignment in manager_rows:
            if assignment.archived_at is not None:
                raise XsddProjectMergeConflict("项目用户关系已漂移")
            archived_user_assignments.append({
                "assignment_id": assignment.assignment_id,
                "project_id": assignment.project_id,
                "responsibility_type": assignment.responsibility_type,
                "user_id": assignment.user_id,
            })
            assignment.archived_at = now
            assignment.archived_by = operated_by[:64]
            assignment.archive_reason = "同一 XSDD 项目容器归并：归档 source 用户关系"
            assignment.version += 1
        db.flush()

    created_user_assignments: list[dict] = []
    for item in user_assignment_resolution["create_on_canonical"]:
        assignment = MaintenanceProjectUserAssignment(
            assignment_id=str(uuid4()),
            project_id=canonical_id,
            responsibility_type=item["responsibility_type"],
            user_id=item["user_id"],
            source_manager_text=item["source_manager_text"],
            version=1,
            assigned_at=now,
            assigned_by=operated_by[:64],
            assignment_reason="同一 XSDD 项目容器归并：在 canonical 保留原权限语义",
        )
        db.add(assignment)
        db.flush()
        created_user_assignments.append({
            "source_assignment_id": item["source_assignment_id"],
            "assignment_id": assignment.assignment_id,
            "project_id": canonical_id,
            "responsibility_type": assignment.responsibility_type,
            "user_id": assignment.user_id,
            "version": 1,
        })

    # ``is_active`` is the assignment generation boundary.  Visibility of the
    # WBDD fact (data_status/tombstone) is independent: a hidden current
    # generation must move with the container or a later reveal would still
    # claim the archived source project and fail the contract-backed guard.
    active_source_assignments = list(db.scalars(
        select(MaintenanceSourceOrderAssignment)
        .join(
            FMaintenanceOrder,
            FMaintenanceOrder.raw_order_id
            == MaintenanceSourceOrderAssignment.source_order_id,
        )
        .where(
            MaintenanceSourceOrderAssignment.project_id.in_(source_ids),
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
        .order_by(MaintenanceSourceOrderAssignment.assignment_id)
        .with_for_update()
    ))
    archived_source_assignments: list[dict] = []
    for assignment in active_source_assignments:
        before = {
            "assignment_id": assignment.assignment_id,
            "source_order_id": assignment.source_order_id,
            "project_id": assignment.project_id,
            "is_active": True,
            "version": assignment.version,
        }
        assignment.is_active = False
        assignment.version += 1
        assignment.archived_by = operated_by[:64]
        assignment.archived_at = now
        after = {
            **before,
            "is_active": False,
            "version": assignment.version,
            "archived_by": assignment.archived_by,
            "archived_at": now.isoformat(),
        }
        archived_source_assignments.append({"before": before, "after": after})
        db.add(MaintenanceProjectAuditLog(
            project_id=assignment.project_id,
            entity_type="source_order_assignment",
            entity_id=assignment.assignment_id,
            action="reassign_out",
            before_json=before,
            after_json=after,
            reason="同一 XSDD 项目容器归并：归档 source WBDD 归属代次",
            operated_by=operated_by[:64],
        ))
    db.flush()

    # Source WBDD generations are now archived, so the reviewed canonical
    # project is the sole active WBDD owner.  Claim the missing map before
    # staging a source contract: the database contract-removal guard requires
    # that map to prove the surviving owner during the intermediate state.
    mapped = db.get(MaintenanceProjectXsdd, xsdd_norm)
    if mapped is None:
        mapped = MaintenanceProjectXsdd(
            xsdd_norm=xsdd_norm,
            project_id=canonical_id,
            source="xsdd_container_merge",
        )
        db.add(mapped)
    elif mapped.project_id != canonical_id:
        raise XsddProjectMergeConflict(
            "归并中的 XSDD mapping 与已验证 canonical owner 冲突"
        )
    else:
        mapped.source = "xsdd_container_merge"
    db.flush()

    contracts = list(db.scalars(
        select(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_id.in_(member_ids))
        .order_by(MaintenanceProjectContract.project_contract_id)
        .with_for_update()
    ))
    staged_contract_nos: dict[str, str] = {}
    for contract in contracts:
        if contract.project_id in source_ids and normalize_xsdd(contract.contract_no):
            staged_contract_nos[contract.project_contract_id] = contract.contract_no
            contract.contract_no = (
                "merge-stage-"
                + hashlib.sha256(
                    f"{merge_batch_id}:{contract.project_contract_id}".encode("utf-8")
                ).hexdigest()[:36]
            )
    db.flush()

    initial_xsdd_result = db.execute(
        update(MaintenanceProjectXsdd)
        .where(MaintenanceProjectXsdd.project_id.in_(source_ids))
        .values(project_id=canonical_id, source="xsdd_container_merge")
    )

    current_id = resolution.get("current_project_contract_id")
    archive_by_id = {
        item["project_contract_id"]: item for item in resolution["archive_contracts"]
    }
    for contract in contracts:
        archive = archive_by_id.get(contract.project_contract_id)
        if archive is not None:
            contract.included_in_total = False
            contract.effective_to = archive["effective_to"]
            contract.version += 1
    db.flush()

    duplicate_collection_ids = set(dedupe["deleted_collection_ids"])
    for item in resolution["collection_contract_repoints"]:
        if item["collection_id"] in duplicate_collection_ids:
            raise XsddProjectMergeConflict("不能重指已删除的 exact 回款")
        db.execute(
            update(MaintenanceCollectionSnapshot)
            .where(MaintenanceCollectionSnapshot.collection_id == item["collection_id"])
            .values(project_contract_id=item["target_project_contract_id"])
        )

    occupied: set[tuple[str, date]] = set()
    rekeyed_contracts: list[dict] = []
    ordered_contracts = sorted(
        contracts,
        key=lambda row: (
            row.project_contract_id != current_id,
            row.project_id != canonical_id,
            row.project_contract_id,
        ),
    )
    for contract in ordered_contracts:
        identity = (contract.contract_id, contract.effective_from)
        if identity not in occupied:
            occupied.add(identity)
            continue
        previous = contract.contract_id
        contract.contract_id = _merged_contract_id(
            original=previous,
            project_contract_id=contract.project_contract_id,
            occupied=occupied,
            effective_from=contract.effective_from,
        )
        contract.version += 1
        rekeyed_contracts.append({
            "project_contract_id": contract.project_contract_id,
            "before_contract_id": previous,
            "after_contract_id": contract.contract_id,
        })
    db.flush()

    affected_by_table: dict[str, int] = {}
    catalog = _project_fk_catalog()
    for table_name in sorted(_GENERIC_REPARENT_TABLES):
        table = catalog[table_name]
        result = db.execute(
            update(table)
            .where(table.c.project_id.in_(source_ids))
            .values(project_id=canonical_id)
        )
        affected_by_table[table_name] = int(result.rowcount or 0)

    contract_result = db.execute(
        update(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_id.in_(source_ids))
        .values(project_id=canonical_id)
    )
    affected_by_table["maintenance_project_contract"] = int(
        contract_result.rowcount or 0
    )
    affected_by_table["maintenance_project_xsdd"] = int(
        initial_xsdd_result.rowcount or 0
    )
    alias_result = db.execute(
        update(MaintenanceProjectAlias)
        .where(MaintenanceProjectAlias.project_id.in_(source_ids))
        .values(project_id=canonical_id)
    )
    affected_by_table["maintenance_project_alias"] = int(alias_result.rowcount or 0)

    db.flush()
    for project_contract_id, contract_no in staged_contract_nos.items():
        db.execute(
            update(MaintenanceProjectContract)
            .where(
                MaintenanceProjectContract.project_contract_id == project_contract_id
            )
            .values(contract_no=contract_no)
        )
    created_source_assignments: list[dict] = []
    for archived in active_source_assignments:
        assignment = MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid4()),
            source_order_id=archived.source_order_id,
            project_id=canonical_id,
            is_active=True,
            version=1,
            created_by=operated_by[:64],
        )
        db.add(assignment)
        db.flush()
        after = {
            "assignment_id": assignment.assignment_id,
            "source_order_id": assignment.source_order_id,
            "project_id": canonical_id,
            "is_active": True,
            "version": 1,
        }
        created_source_assignments.append({
            "source_assignment_id": archived.assignment_id,
            **after,
        })
        db.add(MaintenanceProjectAuditLog(
            project_id=canonical_id,
            entity_type="source_order_assignment",
            entity_id=assignment.assignment_id,
            action="assign",
            before_json=None,
            after_json=after,
            reason="同一 XSDD 项目容器归并：建立 canonical WBDD 归属代次",
            operated_by=operated_by[:64],
        ))
    affected_by_table["maintenance_source_order_assignment:new_generation"] = len(
        created_source_assignments
    )
    db.flush()
    moved_source_order_ids = {
        assignment.source_order_id for assignment in active_source_assignments
    }
    if moved_source_order_ids:
        from app.services import maintenance_warehouse

        maintenance_warehouse.reconcile_project_assignment_links(
            db,
            operated_by=operated_by[:64],
            reason="同一 XSDD 项目容器归并：按 canonical WBDD 归属重建仓库项目链接",
            source_order_ids=moved_source_order_ids,
        )
    affected_by_table[
        "maintenance_warehouse_document_link:reconciled_source_orders"
    ] = len(moved_source_order_ids)
    db.flush()

    archived_projects: list[dict] = []
    for project in source_projects:
        before = {
            "project_id": project.project_id,
            "display_name": project.display_name,
            "is_active": project.is_active,
            "version": project.version,
        }
        project.is_active = False
        project.version += 1
        archived_projects.append({
            "before": before,
            "after": {
                **before,
                "is_active": False,
                "version": project.version,
            },
        })
        db.add(MaintenanceProjectAuditLog(
            project_id=project.project_id,
            entity_type="project",
            entity_id=project.project_id,
            action="xsdd_merge_source_archive",
            before_json=before,
            after_json={
                **before,
                "is_active": False,
                "version": project.version,
                "canonical_project_id": canonical_id,
                "merge_batch_id": merge_batch_id,
            },
            reason="同一 XSDD 项目容器归并：归档 source 容器并保留历史 provenance",
            operated_by=operated_by[:64],
        ))

    operations.bump_locked_workbook_revision(db, state=locked_states[canonical_id])
    db.flush()

    active_source_assignment_count = int(db.scalar(
        select(func.count())
        .select_from(MaintenanceSourceOrderAssignment)
        .join(
            FMaintenanceOrder,
            FMaintenanceOrder.raw_order_id
            == MaintenanceSourceOrderAssignment.source_order_id,
        )
        .where(
            MaintenanceSourceOrderAssignment.project_id.in_(source_ids),
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
    ) or 0)
    active_source_user_count = int(db.scalar(
        select(func.count())
        .select_from(MaintenanceProjectUserAssignment)
        .where(
            MaintenanceProjectUserAssignment.project_id.in_(source_ids),
            MaintenanceProjectUserAssignment.archived_at.is_(None),
        )
    ) or 0)
    if active_source_assignment_count or active_source_user_count:
        raise XsddProjectMergeConflict(
            "归并后源项目仍有 active 关系代次: "
            f"source_orders={active_source_assignment_count}, "
            f"users={active_source_user_count}"
        )
    remaining_source_rows = _remaining_source_fk_rows(
        db,
        source_project_ids=source_ids,
    )
    if remaining_source_rows:
        raise XsddProjectMergeConflict(
            f"归并后仍有源项目外键未迁移: {remaining_source_rows}"
        )
    active_source_warehouse_links = list(db.scalars(
        select(MaintenanceWarehouseDocumentLink.link_id)
        .where(
            MaintenanceWarehouseDocumentLink.target_type
            == "maintenance_project",
            MaintenanceWarehouseDocumentLink.target_id.in_(source_ids),
            MaintenanceWarehouseDocumentLink.status == "active",
        )
        .order_by(MaintenanceWarehouseDocumentLink.link_id)
    ))
    if active_source_warehouse_links:
        raise XsddProjectMergeConflict(
            "归并后仍有 active 仓库项目链接指向源项目，必须先通过不可变链接代次重建: "
            f"{active_source_warehouse_links}"
        )
    staged_remaining = int(db.scalar(
        select(func.count())
        .select_from(MaintenanceProjectContract)
        .where(
            MaintenanceProjectContract.project_contract_id.in_(
                list(staged_contract_nos) or {""}
            ),
            MaintenanceProjectContract.contract_no.like("merge-stage-%"),
        )
    ) or 0)
    if staged_remaining:
        raise XsddProjectMergeConflict("归并后存在 staging 合同号，事务已拒绝")
    mapped = db.get(MaintenanceProjectXsdd, xsdd_norm)
    if mapped is None or mapped.project_id != canonical_id:
        raise XsddProjectMergeConflict("归并后 XSDD canonical mapping 不成立")
    current_xsdd_contracts = list(db.scalars(
        select(MaintenanceProjectContract).where(
            MaintenanceProjectContract.project_id == canonical_id,
            normalized_xsdd_sql(MaintenanceProjectContract.contract_no) == xsdd_norm,
            MaintenanceProjectContract.effective_from <= business_today(),
            (
                MaintenanceProjectContract.effective_to.is_(None)
                | (MaintenanceProjectContract.effective_to > business_today())
            ),
        )
    ))
    if len(current_xsdd_contracts) > 1:
        raise XsddProjectMergeConflict("归并后仍存在多条 current 合同")

    audit_payload = {
        "merge_batch_id": merge_batch_id,
        "xsdd_norm": xsdd_norm,
        "manifest_hash": conflict["manifest_hash"],
        "canonical_project_id": canonical_id,
        "source_project_ids": source_ids,
        "archived_projects": archived_projects,
        "deleted_exact_collections": dedupe["deleted_collections"],
        "deleted_alias_ids": deleted_alias_ids,
        "source_order_assignment_resolution": {
            "archived_source_generations": archived_source_assignments,
            "created_canonical_generations": created_source_assignments,
        },
        "contract_resolution": {
            "current_project_contract_id": current_id,
            "archive_contracts": [
                {
                    "project_contract_id": item["project_contract_id"],
                    "effective_to": item["effective_to"].isoformat(),
                }
                for item in resolution["archive_contracts"]
            ],
            "collection_contract_repoints": resolution[
                "collection_contract_repoints"
            ],
        },
        "user_assignment_resolution": {
            "keep_assignment_ids": user_assignment_resolution[
                "keep_assignment_ids"
            ],
            "archived_assignments": archived_user_assignments,
            "created_on_canonical": created_user_assignments,
        },
        "rekeyed_internal_contract_ids": rekeyed_contracts,
        "affected_by_table": affected_by_table,
    }
    db.add(MaintenanceProjectAuditLog(
        project_id=canonical_id,
        entity_type="project",
        entity_id=canonical_id,
        action="xsdd_container_merge",
        before_json={
            "member_project_ids": member_ids,
            "manifest_hash": conflict["manifest_hash"],
        },
        after_json=audit_payload,
        reason="同一 XSDD 历史项目容器原子归并",
        operated_by=operated_by[:64],
    ))
    db.flush()
    return audit_payload


def apply_historical_project_merge_batch(
    db: Session,
    *,
    plans: list[dict],
    operated_by: str,
) -> dict:
    """Atomically CAS-check, then merge a reviewed set of disjoint XSDDs.

    The caller owns commit/rollback.  Every selected manifest is frozen and
    validated after the complete lock envelope is held and before the first
    write, so an earlier group cannot invalidate a later group's own CAS.
    """

    from app import config

    clean_operator = str(operated_by or "").strip()
    if not clean_operator:
        raise XsddProjectMergeConflict("操作人不能为空")
    if not plans:
        raise XsddProjectMergeConflict("归并计划不能为空")

    normalized_plans: list[dict] = []
    seen_xsdds: set[str] = set()
    occupied_projects: set[str] = set()
    for raw_plan in plans:
        if not isinstance(raw_plan, dict):
            raise XsddProjectMergeConflict("归并计划格式无效")
        xsdd_norm = normalize_xsdd(str(raw_plan.get("xsdd") or ""))
        expected_hash = str(raw_plan.get("expected_manifest_hash") or "").strip()
        canonical_id = str(
            raw_plan.get("expected_canonical_project_id") or ""
        ).strip()
        member_ids = sorted({
            str(value).strip()
            for value in raw_plan.get("expected_member_project_ids", [])
            if str(value).strip()
        })
        if (
            not xsdd_norm
            or len(expected_hash) != 64
            or not canonical_id
            or canonical_id not in member_ids
            or len(member_ids) < 2
        ):
            raise XsddProjectMergeConflict("归并计划缺少严格 OCC 字段")
        if xsdd_norm in seen_xsdds:
            raise XsddProjectMergeConflict("同一 XSDD 不能在批次中重复")
        overlap = occupied_projects.intersection(member_ids)
        if overlap:
            raise XsddProjectMergeConflict(
                f"批次内项目集合交叉，必须先拆解重做预览: {sorted(overlap)}"
            )
        seen_xsdds.add(xsdd_norm)
        occupied_projects.update(member_ids)
        normalized_plans.append({
            "xsdd_norm": xsdd_norm,
            "expected_manifest_hash": expected_hash,
            "expected_canonical_project_id": canonical_id,
            "expected_member_project_ids": member_ids,
            "contract_resolution": raw_plan.get("contract_resolution"),
            "user_assignment_resolution": raw_plan.get(
                "user_assignment_resolution"
            ),
        })

    db.execute(select(func.pg_advisory_xact_lock(
        config.DATA_CHANGE_ADVISORY_LOCK_KEY
    )))
    all_project_ids = sorted(occupied_projects)
    all_xsdd_identities = set(seen_xsdds) | _xsdd_identities_for_projects(
        db,
        all_project_ids,
    )
    lock_xsdd_identities(db, sorted(all_xsdd_identities))
    locked_states = operations.lock_workbook_states(
        db,
        project_ids=all_project_ids,
    )
    locked_projects: dict[str, MaintenanceProject] = {}
    for project_id in all_project_ids:
        project = db.scalar(
            select(MaintenanceProject)
            .where(MaintenanceProject.project_id == project_id)
            .with_for_update()
        )
        if project is None:
            raise XsddProjectMergeConflict("归并项目集合已变化，请重新预览")
        locked_projects[project_id] = project

    preview = preview_historical_conflicts(db)
    conflicts_by_xsdd = {
        item["xsdd_norm"]: item for item in preview["conflicts"]
    }
    frozen: list[tuple[dict, dict, dict]] = []
    for plan in normalized_plans:
        conflict = conflicts_by_xsdd.get(plan["xsdd_norm"])
        if conflict is None or conflict["requires_human_decision"]:
            raise XsddProjectMergeConflict("历史冲突已变化或不能自动归并")
        actual_members = sorted(
            row["project_id"] for row in conflict["projects"]
        )
        if (
            conflict["manifest_hash"] != plan["expected_manifest_hash"]
            or conflict["canonical_project_id"]
            != plan["expected_canonical_project_id"]
            or actual_members != plan["expected_member_project_ids"]
        ):
            raise XsddProjectMergeConflict("归并 manifest 已漂移，请重新预览")
        source_ids = [
            value for value in actual_members
            if value != conflict["canonical_project_id"]
        ]
        unsupported = _unsupported_project_fk_rows(
            db,
            source_project_ids=source_ids,
        )
        if unsupported:
            raise XsddProjectMergeConflict(
                f"发现未审查的项目外键事实表: {unsupported}"
            )
        _validate_source_xsdd_scope(
            db,
            source_project_ids=source_ids,
            member_ids=actual_members,
        )
        user_assignment_resolution = _parse_user_assignment_resolution(
            db,
            member_ids=actual_members,
            canonical_project_id=conflict["canonical_project_id"],
            resolution=plan["user_assignment_resolution"],
        )
        _project_unique_collision_check(
            db,
            member_ids=actual_members,
        )
        resolution = _parse_contract_resolution(
            db,
            conflict=conflict,
            resolution=plan["contract_resolution"],
        )
        frozen.append((conflict, resolution, user_assignment_resolution))

    merge_batch_id = str(uuid4())
    results = [
        _apply_locked_project_merge(
            db,
            conflict=conflict,
            resolution=resolution,
            user_assignment_resolution=user_assignment_resolution,
            locked_projects=locked_projects,
            locked_states=locked_states,
            operated_by=clean_operator,
            merge_batch_id=merge_batch_id,
        )
        for conflict, resolution, user_assignment_resolution in frozen
    ]
    return {
        "merge_batch_id": merge_batch_id,
        "merged_group_count": len(results),
        "groups": results,
    }


def apply_historical_project_merge(
    db: Session,
    *,
    xsdd: str,
    expected_manifest_hash: str,
    expected_canonical_project_id: str,
    expected_member_project_ids: list[str],
    operated_by: str,
    contract_resolution: dict | None = None,
    user_assignment_resolution: dict | None = None,
) -> dict:
    """Single-group convenience wrapper around the atomic batch primitive."""

    result = apply_historical_project_merge_batch(
        db,
        plans=[{
            "xsdd": xsdd,
            "expected_manifest_hash": expected_manifest_hash,
            "expected_canonical_project_id": expected_canonical_project_id,
            "expected_member_project_ids": expected_member_project_ids,
            "contract_resolution": contract_resolution,
            "user_assignment_resolution": user_assignment_resolution,
        }],
        operated_by=operated_by,
    )
    return result["groups"][0]


def auto_merge_sales_xsdd_conflicts(
    db: Session,
    *,
    incoming_amount_inc_tax_by_xsdd: dict[str, Decimal],
    operated_by: str,
) -> dict:
    """Merge only incoming, contract-backed XSDD splits during sales prelock.

    This deliberately delegates every write and OCC check to the reviewed
    batch primitive.  When historical current contracts disagree, the active
    incoming sales row may select one existing contract only by an exact,
    unique gross-amount match on the already chosen canonical project.  Every
    other current contract is archived, never deleted.  Source user relations
    are copied one-for-one; the reviewed parser still rejects any primary or
    viewer uniqueness collision before the first write.
    """

    incoming = {
        normalized: Decimal(amount)
        for value, amount in incoming_amount_inc_tax_by_xsdd.items()
        if (normalized := normalize_xsdd(value))
    }
    if not incoming:
        return {"merged_group_count": 0, "groups": []}

    preview = preview_historical_conflicts(db)
    relevant = [
        conflict
        for conflict in preview["conflicts"]
        if conflict["xsdd_norm"] in incoming
    ]
    if not relevant:
        return {"merged_group_count": 0, "groups": []}

    plans: list[dict] = []
    for conflict in relevant:
        contract_owners = conflict.get("contract_owner_project_ids") or []
        canonical_id = conflict["canonical_project_id"]
        canonical = next(
            (
                project
                for project in conflict["projects"]
                if project["project_id"] == canonical_id
            ),
            None,
        )
        if conflict.get("requires_human_decision") or canonical is None or not canonical[
            "is_active"
        ]:
            raise XsddProjectMergeConflict(
                f"XSDD {conflict['xsdd_norm']} 不能按唯一销售合同 owner 自动归并"
            )

        member_ids = sorted(
            project["project_id"] for project in conflict["projects"]
        )
        today = business_today()
        current_contracts = list(db.scalars(
            select(MaintenanceProjectContract)
            .where(
                MaintenanceProjectContract.project_id.in_(member_ids),
                normalized_xsdd_sql(MaintenanceProjectContract.contract_no)
                == conflict["xsdd_norm"],
                MaintenanceProjectContract.effective_from <= today,
                or_(
                    MaintenanceProjectContract.effective_to.is_(None),
                    MaintenanceProjectContract.effective_to > today,
                ),
            )
            .order_by(MaintenanceProjectContract.project_contract_id)
        ))
        matching_contracts = [
            contract
            for contract in current_contracts
            if contract.status_mapping_state == "mapped"
            and contract.included_in_total
            and contract.amount_inc_tax == incoming[conflict["xsdd_norm"]]
        ]

        contract_resolution = None
        if len(contract_owners) == 1 and contract_owners == [canonical_id]:
            # Preserve the original safe path when the contract-backed owner
            # is already unique.  Only a genuinely ambiguous current set needs
            # the sales amount to choose a survivor.
            if len(current_contracts) > 1:
                if len(matching_contracts) != 1:
                    raise XsddProjectMergeConflict(
                        f"XSDD {conflict['xsdd_norm']} 不能按唯一销售合同 owner 自动归并："
                        "incoming 含税额未唯一匹配 current 合同"
                    )
        else:
            if (
                len(matching_contracts) != 1
                or matching_contracts[0].project_id != canonical_id
            ):
                raise XsddProjectMergeConflict(
                    f"XSDD {conflict['xsdd_norm']} 不能按唯一销售合同 owner 自动归并："
                    "incoming 含税额未唯一匹配 locked canonical 合同"
                )

        if len(current_contracts) > 1:
            current_contract = matching_contracts[0]
            non_authoritative_contracts = [
                contract
                for contract in current_contracts
                if contract.project_contract_id
                != current_contract.project_contract_id
            ]
            if any(
                contract.effective_from >= today
                for contract in non_authoritative_contracts
            ):
                raise XsddProjectMergeConflict(
                    f"XSDD {conflict['xsdd_norm']} 的非权威 current 合同起始日"
                    "不早于业务日，不能无损归档"
                )
            archived_contract_ids = {
                contract.project_contract_id
                for contract in non_authoritative_contracts
            }
            survivor_ids = {
                cluster["survivor_id"]
                for cluster in conflict["exact_duplicate_candidates"]["collections"]
            }
            survivor_rows = list(db.scalars(
                select(MaintenanceCollectionSnapshot).where(
                    MaintenanceCollectionSnapshot.collection_id.in_(
                        sorted(survivor_ids or {""})
                    )
                )
            ))
            contract_resolution = {
                "current_project_contract_id": (
                    current_contract.project_contract_id
                ),
                "archive_contracts": [
                    {
                        "project_contract_id": contract.project_contract_id,
                        "effective_to": today.isoformat(),
                    }
                    for contract in non_authoritative_contracts
                ],
                "collection_contract_repoints": [
                    {
                        "collection_id": collection.collection_id,
                        "target_project_contract_id": (
                            current_contract.project_contract_id
                        ),
                    }
                    for collection in survivor_rows
                    if collection.project_contract_id in archived_contract_ids
                ],
            }

        active_users = list(db.scalars(
            select(MaintenanceProjectUserAssignment)
            .where(
                MaintenanceProjectUserAssignment.project_id.in_(member_ids),
                MaintenanceProjectUserAssignment.archived_at.is_(None),
            )
            .order_by(MaintenanceProjectUserAssignment.assignment_id)
        ))
        canonical_users = [
            assignment
            for assignment in active_users
            if assignment.project_id == canonical_id
        ]
        source_users = [
            assignment
            for assignment in active_users
            if assignment.project_id != canonical_id
        ]
        user_assignment_resolution = None
        if source_users:
            users_by_id = {
                user.id: user
                for user in db.scalars(select(SysUser).where(
                    SysUser.id.in_({assignment.user_id for assignment in active_users})
                ))
            }
            invalid_user_ids = sorted({
                assignment.user_id
                for assignment in active_users
                if users_by_id.get(assignment.user_id) is None
                or not users_by_id[assignment.user_id].is_active
            })
            if invalid_user_ids:
                raise XsddProjectMergeConflict(
                    "active 项目用户关系指向停用或不存在账号，不能自动归并: "
                    f"{invalid_user_ids}"
                )
            canonical_identities = {
                (assignment.responsibility_type, assignment.user_id)
                for assignment in canonical_users
            }
            source_by_identity: dict[
                tuple[str, int], MaintenanceProjectUserAssignment
            ] = {}
            for assignment in source_users:
                source_by_identity.setdefault(
                    (assignment.responsibility_type, assignment.user_id),
                    assignment,
                )
            user_assignment_resolution = {
                "keep_assignment_ids": [
                    assignment.assignment_id for assignment in canonical_users
                ],
                "archive_assignment_ids": [
                    assignment.assignment_id for assignment in source_users
                ],
                "create_on_canonical": [
                    {
                        "source_assignment_id": assignment.assignment_id,
                        "responsibility_type": assignment.responsibility_type,
                        "user_id": assignment.user_id,
                    }
                    for identity, assignment in source_by_identity.items()
                    if identity not in canonical_identities
                ],
            }
        plans.append({
            "xsdd": conflict["xsdd_norm"],
            "expected_manifest_hash": conflict["manifest_hash"],
            "expected_canonical_project_id": canonical_id,
            "expected_member_project_ids": member_ids,
            "contract_resolution": contract_resolution,
            "user_assignment_resolution": user_assignment_resolution,
        })

    return apply_historical_project_merge_batch(
        db,
        plans=plans,
        operated_by=operated_by,
    )
