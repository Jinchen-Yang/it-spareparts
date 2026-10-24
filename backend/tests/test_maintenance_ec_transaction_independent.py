"""Independent E1 probes: actual PostgreSQL writes, visibility and lock lifetime.

Only test-owned data is mutated. Failure rollback belongs to the outer owner.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from importlib import import_module
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.maintenance import FProjectExpense
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookOperation,
    MaintenanceProjectWorkbookState,
)
from app.models.system import SysImportBatch
from app.services import maintenance_expense_collection_workbook as ec
from app.services import maintenance_project_master_workbook as master
from app.services import maintenance_project_operations as operations
from tests.test_maintenance_expense_collection_workbook import project as _project_fixture

project = _project_fixture


MODELS = (FProjectExpense, MaintenanceProjectExpenseAttribution,
          MaintenanceCollectionSnapshot, MaintenanceProjectOperationAudit,
          SysImportBatch, MaintenanceProjectWorkbookOperation)
ZERO = (0, 0, 0, 0, 0, 0)
MIXED = (1, 1, 1, 1, 1, 0)


def _counts(db):
    return tuple(db.scalar(select(func.count()).select_from(model)) for model in MODELS)


def _patch_bump(monkeypatch, replacement, name="bump_locked_workbook_revision"):
    # The combination extracts the same state primitive into its own module.
    # Observe the implementation owner AND its compatibility re-export.
    owner = import_module(getattr(operations, name).__module__)
    monkeypatch.setattr(owner, name, replacement)
    if owner is not operations:
        monkeypatch.setattr(operations, name, replacement)


def _plan(db, project):
    contract = db.scalar(select(MaintenanceProjectContract).where(
        MaintenanceProjectContract.project_id == project.project_id))
    return ec.WorkbookPlan(project.project_id, (ec.ExpenseUpdate(
        raw_line_id="EXP:independent-e1", is_create=True,
        bxd_no="INDEPENDENT-E1", line_no=1, expense_date=date(2026, 1, 15),
        person="independent", expense_type="差旅", fee_category="交通",
        reason="transaction probe", contract_no=contract.contract_no,
        amount_ex_tax=Decimal("10.00"), amount_inc_tax=Decimal("11.30"),
        tax_basis="ex", data_status="已结束", remark="probe",
    ),), (ec.CollectionOp(
        "CREATE", contract.project_contract_id, contract.contract_no,
        date(2026, 1, 1), Decimal("100.00"), "independent", "probe",
    ),))


def _state(db, project_id):
    operations.get_or_create_workbook_state(db, project_id=project_id, lock=True)
    db.commit()
    return operations.get_or_create_workbook_state(db, project_id=project_id, lock=True)


def _assert_observer(db, expected, project_id, revision):
    with Session(db.get_bind()) as observer:
        assert observer.scalar(text("SELECT pg_backend_pid()")) != db.scalar(
            text("SELECT pg_backend_pid()"))
        assert _counts(observer) == expected
        assert observer.get(MaintenanceProjectWorkbookState, project_id).revision == revision


def _invoke_master(db, plan, version):
    if version == "v1":
        return master.apply(db, master.MasterPlan(plan.project_id, inner=plan),
                            operated_by="independent", import_batch_id=str(uuid4()))
    state = db.get(MaintenanceProjectWorkbookState, plan.project_id)
    return master.apply_project_master_v2(db, master.MasterV2Plan(
        project_id=plan.project_id,
        sheets=(master.V2_SHEET_EXPENSE, master.V2_SHEET_RECEIPTS),
        export_id=str(uuid4()), file_sha256="a" * 64,
        expected_workbook_revision=state.revision,
        expected_workbook_data_version=state.data_version,
        expense_updates=plan.expense_updates, receipt_ops=plan.collection_ops,
    ), operated_by="independent", import_batch_id=str(uuid4()))


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("late_failure", [False, True])
def test_real_master_owner_after_completed_inner_writes(db, project, monkeypatch,
                                                       version, late_failure):
    plan = _plan(db, project)
    _state(db, plan.project_id)
    bump_calls = []
    real_bump = operations.bump_locked_workbook_revision
    real_inner = ec.apply_in_transaction
    inner_completed = []

    def bump(session, *, state):
        bump_calls.append(state.project_id)
        return real_bump(session, state=state)

    def inner(session, *args, **kwargs):
        assert bump_calls == []
        result = real_inner(session, *args, **kwargs)
        session.flush()
        assert _counts(session) == MIXED
        assert bump_calls == [], "revision dedup must not hide an inner bump call"
        assert kwargs["workbook_state"].revision == 0
        _assert_observer(session, ZERO, plan.project_id, 0)
        inner_completed.append(True)
        return result

    _patch_bump(monkeypatch, bump)
    monkeypatch.setattr(ec, "apply_in_transaction", inner)
    expected = MIXED if version == "v1" else (1, 1, 1, 2, 1, 1)

    def fail_after_everything(session):
        session.flush()
        assert _counts(session) == expected
        assert session.get(MaintenanceProjectWorkbookState, plan.project_id).revision == 1
        _assert_observer(session, ZERO, plan.project_id, 0)
        raise RuntimeError("independent outer owner failure after real writes")

    if late_failure:
        event.listen(db, "before_commit", fail_after_everything)
        try:
            with pytest.raises(RuntimeError, match="outer owner failure"):
                _invoke_master(db, plan, version)
        finally:
            event.remove(db, "before_commit", fail_after_everything)
        db.rollback()  # HTTP/session owner, never an added joined responsibility.
        _assert_observer(db, ZERO, plan.project_id, 0)
    else:
        _invoke_master(db, plan, version)
        _assert_observer(db, expected, plan.project_id, 1)
    assert inner_completed == [True]
    assert bump_calls == [plan.project_id]


@pytest.mark.parametrize("entry", ["joined", "standalone"])
def test_real_late_business_failure_keeps_partial_writes_private_until_owner_rollback(
        db, project, entry):
    plan = _plan(db, project)
    plan = replace(plan, collection_ops=plan.collection_ops + (
        replace(plan.collection_ops[0], report_month=date(2026, 2, 1),
                cumulative_amount=Decimal("50.00")),))
    state = _state(db, plan.project_id)
    root_transaction = db.get_transaction()
    with pytest.raises(ec.WorkbookError) as caught:
        if entry == "joined":
            ec.apply_in_transaction(db, plan, operated_by="independent",
                                    import_batch_id=str(uuid4()), workbook_state=state)
        else:
            ec.apply_and_commit(db, plan, operated_by="independent",
                                import_batch_id=str(uuid4()))
    assert caught.value.code == "collection_not_monotonic"
    assert db.get_transaction() is root_transaction
    assert db.is_active
    assert _counts(db) == (1, 1, 2, 2, 1, 0)  # Guard ran after genuine SQL writes.
    assert state.revision == 0
    _assert_observer(db, ZERO, plan.project_id, 0)
    db.rollback()
    _assert_observer(db, ZERO, plan.project_id, 0)


@pytest.mark.parametrize("outer_already_bumped", [False, True])
def test_joined_never_invokes_bump_commit_rollback_or_savepoint(
        db, project, monkeypatch, outer_already_bumped):
    plan = _plan(db, project)
    state = _state(db, plan.project_id)
    if outer_already_bumped:
        operations.bump_locked_workbook_revision(db, state=state)

    def forbidden(*args, **kwargs):
        pytest.fail("joined used an outer-owned transaction operation")

    with monkeypatch.context() as scoped:
        for name in ("commit", "rollback", "begin_nested"):
            scoped.setattr(db, name, forbidden)
        _patch_bump(scoped, forbidden)
        _patch_bump(scoped, forbidden, "bump_workbook_revision")
        result = ec.apply_in_transaction(db, plan, operated_by="independent",
                                        import_batch_id=str(uuid4()), workbook_state=state)
        assert result.operating_fact_changed
        assert state.revision == int(outer_already_bumped)
        assert _counts(db) == MIXED
    db.rollback()
    _assert_observer(db, ZERO, plan.project_id, 0)


@pytest.mark.parametrize("target", ["state", "project", "raw", "attribution", "collection"])
def test_joined_holds_real_row_locks_until_owner_rollback(db, project, target):
    plan = _plan(db, project)
    ec.apply_and_commit(db, plan, operated_by="independent", import_batch_id=str(uuid4()))
    state = operations.get_or_create_workbook_state(db, project_id=plan.project_id, lock=True)
    changed = replace(plan,
        expense_updates=(replace(plan.expense_updates[0], amount_ex_tax=Decimal("20.00"),
                                 amount_inc_tax=Decimal("22.60")),),
        collection_ops=(replace(plan.collection_ops[0], cumulative_amount=Decimal("200.00")),))
    ec.apply_in_transaction(db, changed, operated_by="independent",
                            import_batch_id=str(uuid4()), workbook_state=state)
    models = {"state": MaintenanceProjectWorkbookState, "project": MaintenanceProject,
              "raw": FProjectExpense, "attribution": MaintenanceProjectExpenseAttribution,
              "collection": MaintenanceCollectionSnapshot}
    statement = select(models[target]).with_for_update(nowait=True)
    with Session(db.get_bind()) as contender:
        with pytest.raises(OperationalError) as caught:
            contender.execute(statement)
        assert caught.value.orig.sqlstate == "55P03"
        contender.rollback()
        assert contender.scalar(select(FProjectExpense.amount_ex_tax)) == Decimal("10.00")
        assert contender.scalar(select(MaintenanceProjectExpenseAttribution.amount_ex_tax)) == Decimal("10.00")
        assert contender.scalar(select(MaintenanceCollectionSnapshot.cumulative_amount)) == Decimal("100.00")
        db.rollback()
        assert contender.execute(statement).scalar_one() is not None
        contender.rollback()
    _assert_observer(db, MIXED, plan.project_id, 1)


@pytest.mark.parametrize("owner_commits", [False, True])
def test_competing_standalone_writer_waits_then_retries_cleanly(db, project, owner_commits):
    plan = _plan(db, project)
    state = _state(db, plan.project_id)
    ec.apply_in_transaction(db, plan, operated_by="independent",
                            import_batch_id=str(uuid4()), workbook_state=state)
    with Session(db.get_bind()) as contender:
        contender.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(OperationalError) as caught:
            ec.apply_and_commit(contender, plan, operated_by="contender",
                                import_batch_id=str(uuid4()))
        assert caught.value.orig.sqlstate == "55P03"
        contender.rollback()
        assert _counts(contender) == ZERO
        if owner_commits:
            operations.bump_locked_workbook_revision(db, state=state)
            db.commit()
        else:
            db.rollback()
        ec.apply_and_commit(contender, plan, operated_by="contender",
                            import_batch_id=str(uuid4()))
    _assert_observer(db, MIXED, plan.project_id, 1)


@pytest.mark.parametrize("variant", ["empty", "repeat", "void_again", "void_hint_create", "void_hint_update"])
def test_noop_and_void_receipts_do_not_call_bump(db, project, monkeypatch, variant):
    plan = replace(_plan(db, project), expense_updates=())
    ec.apply_and_commit(db, plan, operated_by="independent", import_batch_id=str(uuid4()))
    if variant.startswith("void"):
        void_plan = replace(plan, collection_ops=(replace(plan.collection_ops[0],
                            operation="VOID", cumulative_amount=None),))
        ec.apply_and_commit(db, void_plan, operated_by="independent", import_batch_id=str(uuid4()))
    if variant == "empty":
        plan = replace(plan, collection_ops=())
    elif variant == "void_again":
        plan = void_plan
    elif variant == "void_hint_update":
        plan = replace(plan, collection_ops=(replace(plan.collection_ops[0], operation="UPDATE"),))
    state = operations.get_or_create_workbook_state(db, project_id=plan.project_id, lock=True)
    before = _counts(db)
    revision = state.revision

    def forbidden(*args, **kwargs):
        pytest.fail("a receipt/no-op must not invoke revision bump")

    _patch_bump(monkeypatch, forbidden)
    result = ec.apply_in_transaction(db, plan, operated_by="independent",
                                    import_batch_id=str(uuid4()), workbook_state=state)
    assert not result.operating_fact_changed
    assert bool(result.receipt["voided_rows"]) == variant.startswith("void_hint")
    assert _counts(db) == before
    standalone = ec.apply_and_commit(db, plan, operated_by="independent", import_batch_id=str(uuid4()))
    assert bool(standalone["voided_rows"]) == variant.startswith("void_hint")
    _assert_observer(db, before, plan.project_id, revision)


@pytest.mark.parametrize("invalid", ["wrong_state", "inactive_project", "missing_project"])
def test_invalid_join_context_rejected_before_mixed_fact_writes(db, project, invalid):
    plan = _plan(db, project)
    state = _state(db, plan.project_id)
    if invalid == "wrong_state":
        state = MaintenanceProjectWorkbookState(project_id=str(uuid4()), revision=0, data_version="fake")
    elif invalid == "inactive_project":
        project.is_active = False
        db.commit()
    else:
        plan = replace(plan, project_id=str(uuid4()))
    with pytest.raises(ec.WorkbookError) as caught:
        ec.apply_in_transaction(db, plan, operated_by="independent",
                                import_batch_id=str(uuid4()), workbook_state=state)
    assert caught.value.code == ("invalid_plan" if invalid == "wrong_state" else "project_not_editable")
    assert _counts(db) == ZERO
    db.rollback()


@pytest.mark.parametrize("commit", [False, True])
@pytest.mark.parametrize("bump_revision", [False, True])
def test_legacy_mixed_facts_matrix_counts_bump_calls_not_only_revision(
        db, project, monkeypatch, commit, bump_revision):
    plan = _plan(db, project)
    state = _state(db, plan.project_id)
    calls = []
    real = operations.bump_locked_workbook_revision

    def counted(session, *, state):
        calls.append(state.project_id)
        return real(session, state=state)

    _patch_bump(monkeypatch, counted)
    result = ec.apply(db, plan, operated_by="independent", import_batch_id=str(uuid4()),
                      workbook_state=state, commit=commit, bump_revision=bump_revision)
    assert "_operating_fact_changed" not in result
    assert calls == ([plan.project_id] if bump_revision else [])
    assert _counts(db) == MIXED
    _assert_observer(db, MIXED if commit else ZERO, plan.project_id,
                     int(commit and bump_revision))
    if not commit:
        db.rollback()
        _assert_observer(db, ZERO, plan.project_id, 0)
