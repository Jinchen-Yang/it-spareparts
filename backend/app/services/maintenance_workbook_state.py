"""Workbook revisions and ordered state/project locks in the caller's transaction.

These helpers do not authorize, refresh existing ORM identities, or commit.
Workflows retain their own earlier locks and post-lock freshness checks. Even
get_or_create(lock=False) can INSERT; it is not a read-only query. Nested
savepoint rollback is not a supported revision-deduplication boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState


def workbook_data_version(project_id: str, revision: int) -> str:
    return hashlib.sha256(f"{project_id}:{revision}".encode("utf-8")).hexdigest()


def get_or_create_workbook_state(
    db: Session,
    *,
    project_id: str,
    lock: bool = False,
) -> MaintenanceProjectWorkbookState:
    """Return the project concurrency row, creating revision zero idempotently."""

    db.execute(
        insert(MaintenanceProjectWorkbookState)
        .values(
            project_id=project_id,
            revision=0,
            data_version=workbook_data_version(project_id, 0),
        )
        .on_conflict_do_nothing(index_elements=["project_id"])
    )
    statement = select(MaintenanceProjectWorkbookState).where(
        MaintenanceProjectWorkbookState.project_id == project_id
    )
    if lock:
        statement = statement.with_for_update()
    return db.execute(statement).scalar_one()


def lock_workbook_states(
    db: Session,
    *,
    project_ids: Iterable[str | None],
) -> dict[str, MaintenanceProjectWorkbookState]:
    """Create and lock project workbook states in one canonical order.

    Callers must invoke this before locking project/order/assignment/detail
    facts.  Lock one row at a time: ``ORDER BY`` on a multi-row ``FOR UPDATE``
    query does not itself guarantee the physical row-lock acquisition order.
    """

    states: dict[str, MaintenanceProjectWorkbookState] = {}
    for project_id in sorted(
        {str(value).strip() for value in project_ids if str(value or "").strip()}
    ):
        states[project_id] = get_or_create_workbook_state(
            db,
            project_id=project_id,
            lock=True,
        )
    return states


def bump_workbook_revision(
    db: Session,
    *,
    project_id: str,
) -> MaintenanceProjectWorkbookState:
    """Bump once inside the caller's transaction after an operating-fact write."""

    state = get_or_create_workbook_state(db, project_id=project_id, lock=True)
    return bump_locked_workbook_revision(db, state=state)


def bump_locked_workbook_revision(
    db: Session,
    *,
    state: MaintenanceProjectWorkbookState,
) -> MaintenanceProjectWorkbookState:
    """Bump a locked state at most once per root Session transaction.

    ``Session.info`` outlives a commit, so the registry is paired with the
    actual root ``SessionTransaction`` object.  A later transaction receives a
    fresh set even when the same Session instance is reused.
    """

    transaction = db.get_transaction()
    registry_key = "_maintenance_workbook_revision_bumps"
    registry = db.info.get(registry_key)
    if (
        not isinstance(registry, tuple)
        or len(registry) != 2
        or registry[0] is not transaction
    ):
        registry = (transaction, set())
        db.info[registry_key] = registry
    bumped_project_ids = registry[1]
    if state.project_id in bumped_project_ids:
        return state

    state.revision += 1
    state.data_version = workbook_data_version(state.project_id, state.revision)
    db.flush()
    bumped_project_ids.add(state.project_id)
    return state


def _project(db: Session, project_id: str) -> MaintenanceProject | None:
    return db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )


def lock_project_for_fact_write(
    db: Session, project_id: str
) -> MaintenanceProject | None:
    """Use one global lock order: workbook state, then the concrete fact row."""

    exists = db.scalar(
        select(MaintenanceProject.project_id).where(
            MaintenanceProject.project_id == project_id
        )
    )
    if exists is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    return _project(db, project_id)
