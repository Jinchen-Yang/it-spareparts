"""Independent W2a ownership counterexamples; optional in-memory fault variants."""
import inspect
import os
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal
from app.models.maintenance import FProjectExpense
from app.models.maintenance_project_operations import (
    MaintenanceProjectExpenseAttribution, MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_workbook_queries as queries
from app.services import maintenance_workbook_scope as scope
from app.services import maintenance_workbook_site_identity as identity
from app.services.maintenance_workbook_plans import MasterV2Plan, SiteReturnFlag, V2RowGuard, WorkbookError
from app.services.maintenance_workbook_protocol import V2_EXPENSE_BASE_FIELDS, V2_SHEET_EXPENSE
from app.services.maintenance_workbook_rows import _v2_expense_row_values
from app.services.maintenance_workbook_values import _v2_row_base_hash
from tests.test_maintenance_expense_collection_workbook import _expense
from tests.test_maintenance_project_master_v2_editable import _make_project_with_line, _site_issue, _append_site_line
from tests.test_maintenance_workbook_boundaries import _project, _contract


@pytest.fixture(autouse=True)
def _optional_fault_variant(monkeypatch):
    """Never edit production files; opt-in mutants must fail the same assertions."""
    variant = os.environ.get('W2A_OWNERSHIP_MUTANT')
    if not variant:
        return
    if variant == 'crossed-attribution':
        module, name = queries, 'project_expenses'
        source = inspect.getsource(getattr(module, name))
        source = source[:source.index('    return [\n')] + '    return [expense for expense, attribution in rows]\n'
    elif variant == 'stale-expense':
        module, name = scope, '_v2_assert_entity_scope'
        source = inspect.getsource(getattr(module, name)).replace(
            '.execution_options(populate_existing=True)', '')
    elif variant == 'partial-merge-lock':
        original = scope._v2_assert_entity_scope
        def partial(db, **kwargs):
            kwargs['lock_site_merge_scope'] = False
            return original(db, **kwargs)
        monkeypatch.setattr(scope, '_v2_assert_entity_scope', partial)
        return
    elif variant == 'void-parent-visible':
        module, name = scope, '_expected_site_ids'
        source = inspect.getsource(getattr(module, name)).replace(
            'MaintenanceSiteIssue.normalized_status != "void",', '')
    else:
        raise AssertionError(f'Unknown deliberate mutant: {variant}')
    namespace = dict(vars(module))
    exec(compile(source, f'<w2a-mutant:{variant}>', 'exec'), namespace)
    monkeypatch.setattr(module, name, namespace[name])
    if name == 'project_expenses':
        monkeypatch.setattr(scope, '_project_expenses', namespace[name])


def _distinct_expense(db, raw_id, contract_no):
    _expense(db, raw_line_id=raw_id, contract_no=contract_no)
    expense = db.scalar(select(FProjectExpense).where(FProjectExpense.raw_line_id == raw_id))
    expense.bxd_no = f'BXD-{raw_id}'
    attribution = db.get(MaintenanceProjectExpenseAttribution, f'bxd:{raw_id}')
    attribution.expense_ref = f'BXD-{raw_id}#1'
    db.commit()
    return expense, attribution


def test_two_valid_contract_sets_do_not_authorize_crossed_expense_pairs(db):
    """Both SQL IN sets pass; exact sales-number/relation pairing still fails."""
    project = _project(db, 'pair-owner')
    first = _contract(db, project, 'PAIR-A')
    second = _contract(db, project, 'PAIR-B')
    db.commit()
    expense_a, attribution_a = _distinct_expense(db, 'pair-a', 'PAIR-A')
    expense_b, attribution_b = _distinct_expense(db, 'pair-b', 'PAIR-B')
    assert {row.raw_line_id for row in queries.project_expenses(db, project.project_id)} == {'pair-a', 'pair-b'}
    attribution_a.project_contract_id = second.project_contract_id
    attribution_b.project_contract_id = first.project_contract_id
    db.commit()
    assert queries.project_expenses(db, project.project_id) == []
    with pytest.raises(WorkbookError, match='报销') as caught:
        scope._v2_assert_entity_scope(db, project_id=project.project_id,
            expense_ids={'pair-a', 'pair-b'}, lock_rows=True)
    assert caught.value.code == 'expense_not_in_project'


def test_expense_lock_refreshes_retained_identity_before_guard(db):
    project = _project(db, 'fresh-owner')
    _contract(db, project, 'FRESH-A')
    db.commit()
    retained, attribution = _distinct_expense(db, 'retained', 'FRESH-A')
    raw_id, project_id = retained.raw_line_id, project.project_id
    baseline = _v2_row_base_hash(_v2_expense_row_values(retained, ''), V2_EXPENSE_BASE_FIELDS)
    old_reason = retained.reason
    plan = MasterV2Plan(project_id=project_id, sheets=(V2_SHEET_EXPENSE,), row_guards=(
        V2RowGuard(sheet=V2_SHEET_EXPENSE, entity_id=raw_id, row_label='2', digest=baseline),))
    with SessionLocal() as peer:
        row = peer.scalar(select(FProjectExpense).where(FProjectExpense.raw_line_id == raw_id))
        row.reason = 'another committed request'
        peer.commit()
    assert retained.reason == old_reason  # keep a strong reference to the stale ORM row
    scope._v2_assert_entity_scope(db, project_id=project_id, expense_ids={raw_id}, lock_rows=True)
    with pytest.raises(WorkbookError) as caught:
        scope._v2_recheck_row_guards(db, plan)
    assert caught.value.code == 'row_conflicts'
    assert caught.value.issues[0]['reason'] == 'server_changed_since_validate'
    assert retained.reason == 'another committed request'
    assert not db.dirty and not db.new


def test_merge_scope_locks_unrequested_inactive_lines_but_not_another_project(db):
    project, part, _, _ = _make_project_with_line(db)
    _, requested = _site_issue(db, project, part, issue_no='REQUESTED')
    hidden_issue, _ = _site_issue(db, project, part, issue_no='UNTOUCHED')
    inactive = _append_site_line(db, hidden_issue, part)
    inactive.is_active = False
    other = _project(db, 'unrelated-lock-owner')
    db.commit()
    _, foreign = _site_issue(db, other, part, issue_no='UNRELATED')
    inactive_id, foreign_id = inactive.issue_line_id, foreign.issue_line_id
    scope._v2_assert_entity_scope(db, project_id=project.project_id,
        site_ids={requested.issue_line_id}, lock_rows=True, lock_site_merge_scope=True)
    # A different transaction is able to lock unrelated project facts.
    with SessionLocal() as peer:
        peer.execute(text('SET LOCAL lock_timeout = \'1s\''))
        assert peer.scalar(select(MaintenanceSiteIssueLine.issue_line_id).where(
            MaintenanceSiteIssueLine.issue_line_id == foreign_id).with_for_update(nowait=True)) == foreign_id
        peer.rollback()
        with pytest.raises(OperationalError) as caught:
            peer.execute(select(MaintenanceSiteIssueLine.issue_line_id).where(
                MaintenanceSiteIssueLine.issue_line_id == inactive_id).with_for_update(nowait=True))
        assert caught.value.orig.sqlstate == '55P03'
        peer.rollback()


def test_parent_void_after_export_removes_still_active_child_from_scope(db):
    project, part, _, _ = _make_project_with_line(db)
    parent, child = _site_issue(db, project, part)
    project_id, parent_id, child_id = project.project_id, parent.issue_id, child.issue_line_id
    assert child_id in scope._expected_site_ids(db, project_id)
    with SessionLocal() as peer:
        peer.get(MaintenanceSiteIssue, parent_id).normalized_status = 'void'
        peer.commit()
    assert parent.normalized_status == 'confirmed'
    with pytest.raises(WorkbookError) as caught:
        scope._v2_assert_entity_scope(db, project_id=project_id, site_ids={child_id}, lock_rows=True)
    assert caught.value.code == 'site_line_not_in_project'
    assert child.is_active  # the parent predicate, not a coincidental child flag, rejects it


def test_locked_xsdd_evidence_tracks_current_assignment_not_matching_name(db):
    project, _, order, line = _make_project_with_line(db)
    other = _project(db, 'same-display-name-owner')
    other.display_name = project.display_name
    db.commit()
    assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == order.raw_order_id))
    project_id, other_id, raw_order_id, line_id = project.project_id, other.project_id, order.raw_order_id, line.id
    contract_no = order.linked_sales_order_no
    with SessionLocal() as peer:
        row = peer.scalar(select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.source_order_id == raw_order_id))
        row.is_active = False
        row.version += 1
        row.archived_at = datetime.now(timezone.utc)
        row.archived_by = 'peer'
        peer.flush()
        peer.add(MaintenanceSourceOrderAssignment(assignment_id=str(uuid4()),
            source_order_id=raw_order_id, project_id=other_id, created_by='peer', is_active=True))
        peer.commit()
    assert assignment.project_id == project_id
    assert scope._v2_assert_entity_scope(db, project_id=project_id,
        linked_sales_order_nos={contract_no}, lock_rows=True) == set()
    assert db.scalar(select(MaintenanceSourceOrderAssignment.project_id).where(
        MaintenanceSourceOrderAssignment.source_order_id == raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True))) == other_id
    with pytest.raises(WorkbookError) as caught:
        scope._v2_assert_entity_scope(db, project_id=project_id,
            part_ids={line_id}, lock_rows=True)
    assert caught.value.code == 'line_not_in_project'


def test_existing_final_number_merge_does_not_authorize_new_identity_collision(db):
    project, part, _, _ = _make_project_with_line(db)
    first, first_line = _site_issue(db, project, part, issue_no='OLD-A')
    second, second_line = _site_issue(db, project, part, issue_no='OLD-B')
    existing = [SiteReturnFlag(issue_line_id=line.issue_line_id, no_return=None,
        issue_id=issue.issue_id, issue_no='FINAL', issue_date=issue.issue_date, row_no=i)
        for i, (issue, line) in enumerate(((first, first_line), (second, second_line)), 2)]
    headers = identity._v2_check_site_issue_numbers(db, project.project_id, existing)
    assert set(headers) == {first.issue_id, second.issue_id}
    # New automatic identity must remain distinct even when two existing identities can merge.
    new = SiteReturnFlag(issue_line_id=str(uuid4()), no_return=None, is_create=True,
        issue_no='FINAL', issue_date=date(2026, 8, 10), row_no=4)
    with pytest.raises(WorkbookError) as caught:
        identity._v2_check_site_issue_numbers(db, project.project_id, [*existing, new])
    assert caught.value.code == 'duplicate_issue_no'
    assert {item['row_no'] for item in caught.value.details['rows']} == {2, 3, 4}
