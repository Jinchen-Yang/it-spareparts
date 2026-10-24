"""Explicit transaction ownership against separate PostgreSQL sessions."""
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.maintenance_project import MaintenanceProjectContract
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot, MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookState,
)
from app.services import maintenance_expense_collection_workbook as ec
from app.services import maintenance_project_operations as operations
from tests.test_maintenance_expense_collection_workbook import project  # noqa: F401


def _plan(db, project):
    contract = db.scalar(select(MaintenanceProjectContract).where(
        MaintenanceProjectContract.project_id == project.project_id))
    return ec.WorkbookPlan(project.project_id, (), (ec.CollectionOp(
        "CREATE", contract.project_contract_id, contract.contract_no,
        date(2026, 1, 1), Decimal("10.00"), "receipt", "test",
    ),))


def _counts(db, project_id):
    return tuple(db.scalar(select(func.count()).select_from(model).where(
        model.project_id == project_id)) for model in (
            MaintenanceCollectionSnapshot, MaintenanceProjectOperationAudit))


def test_standalone_commits_facts_audit_and_one_revision(db, project):
    plan = _plan(db, project)
    result = ec.apply_and_commit(db, plan, operated_by="test", import_batch_id=str(uuid4()))
    assert "_operating_fact_changed" not in result
    assert result["collection_creates"] == 1
    with Session(db.get_bind()) as observer:
        assert _counts(observer, plan.project_id) == (1, 1)
        assert observer.get(MaintenanceProjectWorkbookState, plan.project_id).revision == 1
    ec.apply_and_commit(db, plan, operated_by="test", import_batch_id=str(uuid4()))
    with Session(db.get_bind()) as observer:
        assert _counts(observer, plan.project_id) == (1, 1)
        assert observer.get(MaintenanceProjectWorkbookState, plan.project_id).revision == 1


def test_joined_facts_and_audit_rollback_with_outer_transaction(db, project):
    plan = _plan(db, project)
    state = operations.get_or_create_workbook_state(db, project_id=plan.project_id, lock=True)
    result = ec.apply_in_transaction(db, plan, operated_by="test", import_batch_id=str(uuid4()),
                                    workbook_state=state)
    db.flush()
    assert result.operating_fact_changed is True
    assert state.revision == 0
    assert "_operating_fact_changed" not in result.receipt
    assert _counts(db, plan.project_id) == (1, 1)
    with Session(db.get_bind()) as observer:
        assert _counts(observer, plan.project_id) == (0, 0)
    db.rollback()
    with Session(db.get_bind()) as observer:
        assert _counts(observer, plan.project_id) == (0, 0)


@pytest.mark.parametrize("commit", [False, True])
@pytest.mark.parametrize("bump_revision", [False, True])
def test_legacy_flag_combinations_keep_original_ownership(db, project, commit, bump_revision):
    plan = _plan(db, project)
    result = ec.apply(db, plan, operated_by="test", import_batch_id=str(uuid4()),
                      commit=commit, bump_revision=bump_revision, track_change=True)
    assert result["_operating_fact_changed"] is True
    state = db.get(MaintenanceProjectWorkbookState, plan.project_id)
    assert state.revision == int(bump_revision)
    db.flush()
    with Session(db.get_bind()) as observer:
        assert _counts(observer, plan.project_id) == ((1, 1) if commit else (0, 0))


def test_joined_requires_explicit_project_state_before_writing(db, project):
    plan = _plan(db, project)
    with pytest.raises(ec.WorkbookError, match="并发状态") as error:
        ec.apply_in_transaction(db, plan, operated_by="test", import_batch_id=str(uuid4()),
                                workbook_state=None)
    assert error.value.code == "invalid_plan"
    assert _counts(db, plan.project_id) == (0, 0)
