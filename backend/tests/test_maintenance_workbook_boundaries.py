"""Shared workbook boundaries: real ownership, eligibility, and transaction contracts."""
import ast
from importlib.util import resolve_name
import json
from pathlib import Path
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal
from typing import get_type_hints
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.models.maintenance import FMaintenanceLine, FProjectExpense, MaintenanceManualCostOverride
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import (
    MaintenanceProjectExpenseAttribution, MaintenanceProjectOperationAudit,
    MaintenanceSiteIssueDisplayCounter,
)
from app.services import maintenance_expense_collection_workbook as ec
from app.services import maintenance_project_master_workbook as master
from app.services import maintenance_workbook_audit as audit
from app.services import maintenance_workbook_cost_writes as costs
from app.services import maintenance_workbook_fact_writes as facts
from app.services import maintenance_workbook_queries as queries
from app.services import maintenance_workbook_scope as scope
from app.services import maintenance_workbook_site_identity as identity
from app.services.maintenance_workbook_plans import CostRefill, WorkbookError
from tests.test_maintenance_expense_collection_workbook import _expense
from tests.test_maintenance_project_master_v2_editable import _make_project_with_line


_EXPORTS = {
    queries: ('_current_contracts', '_current_contract_by_no', '_v2_editable_contracts'),
    scope: ('_expected_expense_ids', '_expected_part_ids', '_expected_site_ids',
            '_v2_assert_entity_scope', '_v2_recheck_row_guards'),
    costs: ('_merge_manual_cost_to_line', '_recompute_line_amounts'),
    audit: ('_write_audit',),
    identity: ('_v2_duplicate_issue_no', '_v2_check_site_issue_numbers'),
    facts: ('_ensure_contract_for_xsdd_apply', '_cascade_void_site_lines',
            '_v2_allocate_site_display_no'),
}


def _local_dependency_paths(roots):
    backend = Path(__file__).resolve().parents[1]
    paths = {name: (name,) for name in roots}
    pending = list(roots)
    visited = set()
    while pending:
        name = pending.pop(0)
        source = backend / (name.replace('.', '/') + '.py')
        package = backend / name.replace('.', '/') / '__init__.py'
        source = package if package.exists() else source
        if name in visited or not source.exists():
            continue
        visited.add(name)
        imports = ['.'.join(name.split('.')[:length])
                   for length in range(1, len(name.split('.')))]
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                parent = name if source.name == '__init__.py' else name.rsplit('.', 1)[0]
                imported = resolve_name('.' * node.level + (node.module or ''), parent) \
                    if node.level else node.module or ''
                imports.extend((imported, *(imported + '.' + alias.name for alias in node.names)))
        for imported in imports:
            if (imported == 'app' or imported.startswith('app.')) and imported not in paths:
                paths[imported] = (*paths[name], imported)
                pending.append(imported)
    return {name: paths[name] for name in visited}


def test_shared_boundaries_do_not_depend_on_master_or_http_adapters():
    forbidden = ('app.api', 'app.main',
                 'app.services.maintenance_project_master_workbook',
                 'app.services.maintenance_boss_board',
                 'app.services.maintenance_site_issue_manual')
    for module in _EXPORTS:
        paths = _local_dependency_paths([module.__name__])
        assert not {name: path for name, path in paths.items() if name.startswith(forbidden)}
    # Scope still uses demand visibility, and fact writes still claim stable
    # project identity. These are known legacy dependencies, not query facades.
    for module in (queries, costs, audit, identity):
        paths = _local_dependency_paths([module.__name__])
        assert 'app.services.maintenance_expense_collection_workbook' not in paths
        assert 'app.services.maintenance_project_operations' not in paths
    assert scope._project_expenses is queries.project_expenses
    assert facts._current_contract_by_no is queries._current_contract_by_no
    assert facts._write_audit is audit._write_audit


def test_fresh_import_of_shared_boundaries_needs_no_database_connection():
    script = f'''
import importlib, json, sys
from sqlalchemy.engine import Engine
def unexpected_connection(*args, **kwargs):
    raise AssertionError('boundary import attempted a database connection')
Engine.connect = unexpected_connection
for name in {[module.__name__ for module in _EXPORTS]!r}:
    importlib.import_module(name)
print(json.dumps(sorted(sys.modules)))
'''
    loaded = json.loads(subprocess.run([sys.executable, '-c', script],
                        cwd=Path(__file__).resolve().parents[1],
                        check=True, capture_output=True, text=True).stdout)
    assert 'app.services.maintenance_project_master_workbook' not in loaded
    assert not any(name.startswith('app.api.') for name in loaded)


@pytest.mark.parametrize(('module', 'name'), [
    (module, name) for module, names in _EXPORTS.items() for name in names
] + [(queries, '_current_unique_contracts'), (queries, 'project_expenses')])
def test_legacy_exports_keep_identity_and_the_new_module_owns_execution(module, name):
    adapter = ec if name in ('_current_unique_contracts', 'project_expenses') else master
    function = getattr(module, name)
    assert getattr(adapter, name) is function
    assert function.__globals__ is vars(module)
    # Annotation names must remain resolvable after moving module ownership.
    get_type_hints(function)


def _project(db, code):
    project = MaintenanceProject(project_id=str(uuid4()), project_code=code,
                                 display_name=code, lifecycle_status='ongoing')
    db.add(project)
    db.flush()
    return project


def _contract(db, project, number, *, included=True, starts=date(2026, 1, 1), ends=None):
    contract = MaintenanceProjectContract(
        project_contract_id=str(uuid4()), project_id=project.project_id,
        contract_id=str(uuid4()), contract_no=number, amount_inc_tax=Decimal('113'),
        included_in_total=included, status_mapping_state='mapped',
        status_mapping_version='boundary-test', effective_from=starts,
        effective_to=ends, source='ledger', version=1,
    )
    db.add(contract)
    db.flush()
    return contract


def test_relationship_amount_editing_and_expense_ownership_are_distinct(db, monkeypatch):
    today = date(2026, 10, 23)
    monkeypatch.setattr(queries, 'business_today', lambda: today)
    project = _project(db, 'boundary-owner')
    other = _project(db, 'boundary-other')
    shared = _contract(db, project, 'SHARED')
    excluded = _contract(db, project, 'EXCLUDED', included=False)
    _contract(db, project, 'EXPIRED', ends=today)
    _contract(db, project, 'FUTURE', starts=today + timedelta(days=1))
    db.commit()
    _expense(db, raw_line_id='shared-expense', contract_no='SHARED')
    # The legacy helper seeds one fixed BXD reference; use distinct source
    # identities when creating two expenses in the same project.
    db.scalar(select(FProjectExpense).where(
        FProjectExpense.raw_line_id == 'shared-expense')).bxd_no = 'BXD-SHARED'
    db.get(MaintenanceProjectExpenseAttribution, 'bxd:shared-expense').expense_ref = 'BXD-SHARED#1'
    db.commit()
    _expense(db, raw_line_id='excluded-expense', contract_no='EXCLUDED')
    # Even an excluded relation in another project blocks expense ownership;
    # it does not prevent the existing amount-editing rule from choosing one
    # included current relationship in this project.
    _contract(db, other, 'SHARED', included=False)
    db.commit()

    current = queries._current_contracts(db, project.project_id)
    assert current == [excluded, shared]
    assert queries._v2_editable_contracts(db, project.project_id, current) == [shared]
    assert queries._current_unique_contracts(db, project.project_id) == {'EXCLUDED': excluded}
    assert [expense.raw_line_id for expense in queries.project_expenses(db, project.project_id)] == ['excluded-expense']
    assert queries._current_unique_contracts(db, project.project_id, contract_nos={'SHARED'}) == {}
    with pytest.raises(WorkbookError) as caught:
        scope._v2_assert_entity_scope(db, project_id=project.project_id,
                                     expense_ids={'shared-expense'}, lock_rows=True)
    assert caught.value.code == 'expense_not_in_project'


def test_canonical_expense_attribution_cannot_be_replaced_by_matching_sales_number(db):
    project = _project(db, 'boundary-attribution')
    original = _contract(db, project, 'ATTRIBUTED')
    unrelated = _contract(db, project, 'DIFFERENT')
    db.commit()
    _expense(db, raw_line_id='canonical-expense', contract_no='ATTRIBUTED')
    assert scope._expected_expense_ids(db, project.project_id) == ['canonical-expense']
    attribution = db.get(MaintenanceProjectExpenseAttribution, 'bxd:canonical-expense')
    attribution.project_contract_id = unrelated.project_contract_id
    db.commit()
    assert queries._current_contract_by_no(db, project.project_id, 'ATTRIBUTED') is original
    assert queries.project_expenses(db, project.project_id) == []
    with pytest.raises(WorkbookError) as caught:
        scope._v2_assert_entity_scope(db, project_id=project.project_id,
                                     expense_ids={'canonical-expense'}, lock_rows=True)
    assert caught.value.code == 'expense_not_in_project'


def test_duplicate_current_relationship_remains_ambiguous(db):
    project = _project(db, 'boundary-ambiguous')
    _contract(db, project, 'DUPLICATE')
    _contract(db, project, 'DUPLICATE')
    db.commit()
    with pytest.raises(WorkbookError) as caught:
        queries._current_contract_by_no(db, project.project_id, 'DUPLICATE', lock=True)
    assert caught.value.code == 'contract_ambiguous'
    assert queries._current_unique_contracts(db, project.project_id) == {}


def test_cost_audit_and_display_allocation_roll_back_in_the_callers_transaction(db):
    project, _part, _order, line = _make_project_with_line(
        db, unit_cost=None, cost_source='none')
    project_id, line_id = project.project_id, line.id
    allocated_on = date(2026, 10, 23)
    commits = []
    listener = lambda *_: commits.append('commit')
    event.listen(db, 'after_commit', listener)
    try:
        assert costs._merge_manual_cost_to_line(db,
            CostRefill(line_id, Decimal('5'), Decimal('5.65'), '人工证据'), operated_by='tester')
        audit._write_audit(db, project_id=project_id, entity_type='part', entity_id=str(line_id),
                          action='UPDATE', operated_by='tester', reason='同事务核验',
                          after={'unit_cost_ex_tax': '5'})
        assert facts._v2_allocate_site_display_no(db, allocated_on) == 'LY-20261023-0001'
        db.flush()
        assert db.get(FMaintenanceLine, line_id).cost_amount_ex_tax == Decimal('10')
        assert db.scalar(select(MaintenanceProjectOperationAudit.id)) is not None
        assert not commits
        db.rollback()
    finally:
        event.remove(db, 'after_commit', listener)
    assert db.get(FMaintenanceLine, line_id).unit_cost_ex_tax is None
    assert db.scalar(select(MaintenanceManualCostOverride).where(
        MaintenanceManualCostOverride.line_id == line_id)) is None
    assert db.scalar(select(MaintenanceProjectOperationAudit.id)) is None
    assert db.get(MaintenanceSiteIssueDisplayCounter, allocated_on) is None
    # A rolled-back allocation never consumes the project's next display number.
    assert facts._v2_allocate_site_display_no(db, allocated_on) == 'LY-20261023-0001'


def test_manual_evidence_does_not_replace_existing_automatic_cost(db):
    _project_row, _part, _order, line = _make_project_with_line(db, cost_source='direct')
    original = (line.unit_cost_ex_tax, line.unit_cost_inc_tax,
                line.cost_amount_ex_tax, line.cost_amount_inc_tax, line.cost_source)
    assert costs._merge_manual_cost_to_line(db,
        CostRefill(line.id, Decimal('7'), Decimal('7.91'), '保留人工证据'), operated_by='tester')
    db.flush()
    assert (line.unit_cost_ex_tax, line.unit_cost_inc_tax,
            line.cost_amount_ex_tax, line.cost_amount_inc_tax, line.cost_source) == original
    evidence = db.scalar(select(MaintenanceManualCostOverride).where(
        MaintenanceManualCostOverride.line_id == line.id))
    assert evidence.unit_cost_ex_tax == Decimal('7')
    assert evidence.reason == '保留人工证据'
