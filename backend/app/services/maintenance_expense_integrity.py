"""Expense-integrity helpers: raw BXD line ↔ canonical attribution sync.

This module is the single home for the pure, synchronous building blocks of
expense integrity.  It deliberately contains **no** loader/master/roundtrip
wiring; those callers are layered on later.

Locking contract for write entries (callers, not this module, enforce it):

1. Before calling :func:`sync_attribution_from_raw` the caller must hold the
   project-scoped lock (``maintenance_project_workbook_state`` row,
   ``SELECT ... FOR UPDATE``) for every project it may touch.
2. Stable lock order across all expense write paths — always acquire in this
   order to avoid deadlocks::

       maintenance_project_workbook_state (project_id ASC)
       → maintenance_project (project_id ASC)
       → maintenance_project_contract
       → maintenance_project_expense_attribution
       → f_project_expense

3. Helpers here never ``commit()``/``rollback()`` and never bump
   ``maintenance_project_workbook_state.revision`` themselves; the caller
   decides the late state-lock/bump after inspecting
   :attr:`SyncResult.affected_project_ids`.

The approval axis (``status_mapping_state``/``normalized_status``) and the
ownership axis (``ownership_mapping_state``/``project_contract_id``) are
strictly separate: status mapping never touches ownership, ownership
resolution never reads approval state.
"""

from __future__ import annotations

import hashlib

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from app import config, tax_policy
from app.models.maintenance_project import MaintenanceProjectContract
from app.models.maintenance_project_operations import (
    MaintenanceProjectExpenseAttribution,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.maintenance import FProjectExpense


EXPENSE_ID_PREFIX = "bxd:"
EXPENSE_ID_MAX_LENGTH = 128
RAW_LINE_ID_MAX_LENGTH = 80

TAX_BASES = ("default_ex", "ex", "inc")
OWNERSHIP_STATES = ("mapped", "unmapped", "ambiguous")
# Version stamped by the b6e8d1f3a5c7 backfill and by ownership re-resolution.
OWNERSHIP_MAPPING_VERSION = "ownership-v1"

_AMOUNT_BOUND = Decimal("1000000000000")
_WHITESPACE_RE = re.compile(r"\s+")
_CONTRACT_PREFIX = "XSDD-"


class OwnershipConflictError(RuntimeError):
    """Fail-closed: the unique ownership candidate belongs to another project."""


class ExpenseIntegrityError(ValueError):
    """Raw expense facts are incomplete or violate the signed dual-tax shape."""


def expense_id_for(raw_line_id: str) -> str:
    """Canonical attribution primary key for one raw BXD line."""
    expense_id = f"{EXPENSE_ID_PREFIX}{raw_line_id}"
    if len(expense_id) > EXPENSE_ID_MAX_LENGTH:
        raise ExpenseIntegrityError(
            f"expense_id longer than {EXPENSE_ID_MAX_LENGTH}: {len(expense_id)}"
        )
    return expense_id


KEY_FAMILY_NATIVE = "native"        # 氚云原生「报销明细.数据ID」（UUID），§17.4 形态①
KEY_FAMILY_COMPOSITE = "composite"  # 单号#序号@合同域hash，形态②（旧导出视图无数据ID列）
KEY_FAMILY_CONTENT = "content"      # EXP:sha1(...)#n，形态③（既无数据ID也无单号/序号）


def raw_key_family(raw_line_id: str) -> str:
    """幂等键形态。同一笔报销单明细行在不同导出视图下会落成不同形态的键——
    2026-09-05 事故：批次 168 用无数据ID的旧视图落成复合键，客户新导出带数据ID，
    同一 (项目, 单号#序号) 出现两把键，守卫把它当真重复整批拒绝，且客户侧无解。"""
    if raw_line_id.startswith("EXP:"):
        return KEY_FAMILY_CONTENT
    if "#" in raw_line_id and "@" in raw_line_id:
        return KEY_FAMILY_COMPOSITE
    return KEY_FAMILY_NATIVE


def content_key_digest(*, xsdd: str | None, expense_date, amount, reason, person) -> str:
    """§17.4 形态③ 内容派生键的摘要。transform 与 loader 共用，保证按内容匹配旧键时
    与历史落库的键逐字节同源（amount 为 round_money 后的 Decimal，str() 即 '501.00'）。"""
    basis = "|".join([xsdd or "", expense_date.isoformat(), str(amount), reason or "", person or ""])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:36]


def raw_backed_key(attr) -> str | None:
    """归因对应的事实行键；手工 create_expense 的独立归因（无 raw）返回 None——它们
    不是任何幂等键形态，不能参与接管/跳过判定（否则一条手填归因就能作废导入事实）。"""
    key = attr.raw_expense_line_id or raw_line_id_from_expense_id(attr.expense_id)
    return key or None


def duplicate_identity_verdict(existing_key: str, incoming_key: str) -> str:
    """同项目同 单号#序号 下两把不同的键该怎么办。

    takeover     既有是遗留形态、来行是原生 UUID：同一业务行换了导出视图，原生键接管
    keep_native  既有是原生、来行是遗留：原生键权威，来行是旧视图的重复导出，跳过不降级
                 ——否则月度大导出与手工薄来回传会让键形态反复横跳
    conflict     同形态不同键：真重复（如报销单退回重提换了数据ID），守卫本来就要防
    """
    ex, inc = raw_key_family(existing_key), raw_key_family(incoming_key)
    legacy = {KEY_FAMILY_COMPOSITE, KEY_FAMILY_CONTENT}
    if inc == KEY_FAMILY_NATIVE and ex in legacy:
        return "takeover"
    if ex == KEY_FAMILY_NATIVE and inc in legacy:
        return "keep_native"
    return "conflict"


def raw_line_id_from_expense_id(expense_id: str) -> str | None:
    """Inverse of :func:`expense_id_for`; None for standalone attributions."""
    if not expense_id.startswith(EXPENSE_ID_PREFIX):
        return None
    return expense_id[len(EXPENSE_ID_PREFIX):]


def normalize_contract_no(value: str | None) -> str:
    """Normalize a contract/order number for historical ownership matching.

    Trimmed, all whitespace removed, uppercased, and without a leading
    ``XSDD-`` prefix — raw lines carry both ``XSDD-20221008-0165`` and the
    bare ``20221008-0165`` form.  Keep in sync with the SQL expression in
    migration b6e8d1f3a5c7.
    """
    if not value:
        return ""
    normalized = _WHITESPACE_RE.sub("", value).upper()
    if normalized.startswith(_CONTRACT_PREFIX):
        normalized = normalized[len(_CONTRACT_PREFIX):]
    return normalized


def dual_amounts(amount: Decimal | str | int, tax_basis: str) -> tuple[Decimal, Decimal]:
    """Return (amount_ex_tax, amount_inc_tax) for one signed input amount.

    ``default_ex``/``ex`` treat the input as ex-tax; ``inc`` as inc-tax.
    Rounding matches PostgreSQL ``round(numeric, 2)`` (ties away from zero),
    so negative amounts round symmetrically.
    """
    value = Decimal(str(amount))
    if abs(value) >= _AMOUNT_BOUND:
        raise ExpenseIntegrityError(f"amount out of signed range: {value}")
    if tax_basis in ("default_ex", "ex"):
        amount_ex = tax_policy.round_money(value)
        return amount_ex, tax_policy.inc_from_ex(amount_ex)
    if tax_basis == "inc":
        amount_inc = tax_policy.round_money(value)
        return tax_policy.ex_from_inc(amount_inc), amount_inc
    raise ExpenseIntegrityError(f"unknown tax_basis: {tax_basis!r}")


def map_expense_status(raw_status: str | None) -> tuple[str, str]:
    """Approval axis only: raw workflow status → (state, normalized_status).

    Never touches ownership.  Unknown statuses fail closed to
    ``('unmapped', 'unknown')`` instead of being guessed.
    """
    status = raw_status or ""
    if status in ("已作废", "作废"):
        return "mapped", "void"
    if status == config.MAINT_EXPENSE_ACTIVE_STATUS:
        return "mapped", "approved"
    return "unmapped", "unknown"


@dataclass(frozen=True)
class OwnershipCandidate:
    project_contract_id: str
    project_id: str


@dataclass(frozen=True)
class OwnershipResolution:
    """Ownership-axis outcome for one expense at one historical date."""

    state: str  # mapped | unmapped | ambiguous
    project_contract_id: str | None
    contract_project_id: str | None
    candidates: tuple[OwnershipCandidate, ...] = ()


def find_ownership_candidates(
    db: Session,
    *,
    linked_sales_order_no: str | None,
    expense_date: date,
) -> tuple[OwnershipCandidate, ...]:
    """Contract versions whose normalized number matches and whose
    ``[effective_from, effective_to)`` window covers ``expense_date``."""
    normalized = normalize_contract_no(linked_sales_order_no)
    if not normalized:
        return ()
    rows = db.execute(
        select(
            MaintenanceProjectContract.project_contract_id,
            MaintenanceProjectContract.project_id,
            MaintenanceProjectContract.contract_no,
        ).where(
            MaintenanceProjectContract.effective_from <= expense_date,
            or_(
                MaintenanceProjectContract.effective_to.is_(None),
                MaintenanceProjectContract.effective_to > expense_date,
            ),
        )
    ).all()
    seen: dict[str, OwnershipCandidate] = {}
    for project_contract_id, project_id, contract_no in rows:
        if normalize_contract_no(contract_no) != normalized:
            continue
        seen.setdefault(
            project_contract_id,
            OwnershipCandidate(
                project_contract_id=project_contract_id, project_id=project_id
            ),
        )
    return tuple(seen.values())


def resolve_historical_ownership(
    db: Session,
    *,
    project_id: str,
    linked_sales_order_no: str | None,
    expense_date: date,
) -> OwnershipResolution:
    """Resolve the ownership axis for an expense attributed to ``project_id``.

    Exactly one candidate on the same project maps; zero candidates stay
    unmapped; multiple candidates are ambiguous (never guessed).  A unique
    candidate owned by a *different* project fails closed.
    """
    candidates = find_ownership_candidates(
        db,
        linked_sales_order_no=linked_sales_order_no,
        expense_date=expense_date,
    )
    if not candidates:
        return OwnershipResolution("unmapped", None, None)
    if len(candidates) > 1:
        return OwnershipResolution("ambiguous", None, None, candidates)
    candidate = candidates[0]
    if candidate.project_id != project_id:
        raise OwnershipConflictError(
            "unique ownership candidate "
            f"{candidate.project_contract_id} belongs to project "
            f"{candidate.project_id}, not {project_id}"
        )
    return OwnershipResolution(
        "mapped",
        candidate.project_contract_id,
        candidate.project_id,
        candidates,
    )


def expense_ref_for(raw: FProjectExpense) -> str:
    """Human-facing reference mirroring the existing loader convention."""
    if raw.bxd_no and raw.line_no is not None:
        return f"{raw.bxd_no}#{raw.line_no}"
    return raw.bxd_no or raw.raw_line_id


def raw_mirror_values(
    raw: FProjectExpense,
    *,
    ownership: OwnershipResolution,
    ownership_mapping_version: str = OWNERSHIP_MAPPING_VERSION,
    status_mapping_version: str,
) -> dict:
    """Mirror one raw BXD line into attribution field values.

    Pure: reads the raw object, writes nothing.  Both axes are populated
    independently — status from :func:`map_expense_status`, ownership from the
    given resolution.  Amounts are mirrored verbatim from the raw row (they
    already satisfy the signed dual-tax invariant on the raw side).
    """
    if (
        raw.amount_ex_tax is None
        or raw.amount_inc_tax is None
        or raw.expense_date is None
    ):
        raise ExpenseIntegrityError(
            f"raw expense {raw.raw_line_id} lacks date or dual-tax amounts"
        )
    if (
        abs(raw.amount_ex_tax) >= _AMOUNT_BOUND
        or abs(raw.amount_inc_tax) >= _AMOUNT_BOUND
    ):
        raise ExpenseIntegrityError(
            f"raw expense {raw.raw_line_id} amount out of signed range"
        )
    mapping_state, normalized_status = map_expense_status(raw.data_status)
    return {
        "project_contract_id": ownership.project_contract_id,
        "raw_expense_line_id": raw.raw_line_id,
        "expense_ref": expense_ref_for(raw),
        "expense_date": raw.expense_date,
        "applicant": raw.person,
        "category": raw.fee_category or raw.expense_type,
        "expense_reason": raw.reason,
        "tax_basis": raw.tax_basis,
        "amount_ex_tax": raw.amount_ex_tax,
        "amount_inc_tax": raw.amount_inc_tax,
        "tax_rate_used": raw.tax_rate_used,
        "raw_status": raw.data_status or "",
        "status_mapping_state": mapping_state,
        "normalized_status": normalized_status,
        "status_mapping_version": status_mapping_version,
        "ownership_mapping_state": ownership.state,
        "ownership_mapping_version": ownership_mapping_version,
    }


#: Fields whose semantics define "the attribution still equals the raw fact".
SYNCED_FIELDS = (
    "project_contract_id",
    "raw_expense_line_id",
    "expense_ref",
    "expense_date",
    "applicant",
    "category",
    "expense_reason",
    "tax_basis",
    "amount_ex_tax",
    "amount_inc_tax",
    "tax_rate_used",
    "raw_status",
    "status_mapping_state",
    "normalized_status",
    "status_mapping_version",
    "ownership_mapping_state",
    "ownership_mapping_version",
)


def _semantic_equal(field_name: str, old, new) -> bool:
    """Field-semantics comparison: money by numeric value, the rest exact."""
    if old is None or new is None:
        return old is None and new is None
    if field_name in ("amount_ex_tax", "amount_inc_tax", "tax_rate_used"):
        return Decimal(str(old)) == Decimal(str(new))
    return old == new


def semantic_diff(
    attribution: MaintenanceProjectExpenseAttribution, values: dict
) -> dict[str, tuple]:
    """Changed synced fields as ``{field: (old, new)}``; empty means no-op."""
    return {
        name: (getattr(attribution, name), values[name])
        for name in SYNCED_FIELDS
        if name in values
        and not _semantic_equal(name, getattr(attribution, name), values[name])
    }


@dataclass
class SyncResult:
    """Outcome of one raw → attribution sync (no commit performed)."""

    expense_id: str
    created: bool
    changed_fields: dict[str, tuple] = field(default_factory=dict)
    old_project_id: str | None = None
    new_project_id: str | None = None

    @property
    def changed(self) -> bool:
        return self.created or bool(self.changed_fields)

    @property
    def affected_project_ids(self) -> set[str]:
        """Old/new projects whose expense facts changed (workbook invalidation
        and audit are the caller's job)."""
        return {
            pid
            for pid in (self.old_project_id, self.new_project_id)
            if pid is not None
        }


def sync_attribution_from_raw(
    db: Session,
    *,
    raw: FProjectExpense,
    project_id: str,
    status_mapping_version: str,
    ownership_mapping_version: str = OWNERSHIP_MAPPING_VERSION,
) -> SyncResult:
    """Synchronize the canonical attribution for one raw BXD line.

    Creates the row when absent, otherwise applies a semantic diff and bumps
    ``version`` only on real change.  Ownership is re-resolved historically;
    a unique candidate owned by another project fails closed
    (:class:`OwnershipConflictError`).  Flushes but never commits/rolls back;
    the caller holds the project lock and owns the transaction (see module
    docstring for the lock order).
    """
    if raw.expense_date is None:
        raise ExpenseIntegrityError(
            f"raw expense {raw.raw_line_id} lacks expense_date"
        )
    expense_id = expense_id_for(raw.raw_line_id)
    existing = db.get(MaintenanceProjectExpenseAttribution, expense_id)
    ownership = resolve_historical_ownership(
        db,
        project_id=project_id,
        linked_sales_order_no=raw.linked_sales_order_no,
        expense_date=raw.expense_date,
    )
    values = raw_mirror_values(
        raw,
        ownership=ownership,
        ownership_mapping_version=ownership_mapping_version,
        status_mapping_version=status_mapping_version,
    )
    if existing is None:
        db.add(
            MaintenanceProjectExpenseAttribution(
                expense_id=expense_id,
                project_id=project_id,
                version=1,
                **values,
            )
        )
        db.flush()
        return SyncResult(
            expense_id=expense_id,
            created=True,
            changed_fields=dict(values),
            new_project_id=project_id,
        )
    old_project_id = existing.project_id
    moved = old_project_id != project_id
    changed = semantic_diff(existing, values)
    for name, (_old, new) in changed.items():
        setattr(existing, name, new)
    if moved:
        # The composite FK keeps a moved row consistent with its contract.
        changed["project_id"] = (old_project_id, project_id)
        existing.project_id = project_id
    if changed:
        existing.version += 1
        db.flush()
    return SyncResult(
        expense_id=expense_id,
        created=False,
        changed_fields=changed,
        old_project_id=old_project_id if changed else None,
        new_project_id=project_id if changed else None,
    )
