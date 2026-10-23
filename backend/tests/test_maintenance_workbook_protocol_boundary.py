"""Protocol extraction contracts; these preserve current wire compatibility.

They do not ratify historical business policy or the pending Q1/Q3 behavior.
"""
import ast
import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import FrozenInstanceError
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import get_type_hints

import pytest

from app.services import maintenance_expense_collection_workbook as ec
from app.services import maintenance_project_master_workbook as master
from app.services import maintenance_workbook_plans as plans
from app.services import maintenance_workbook_protocol as protocol
from app.services import maintenance_workbook_values as values


_PURE_MODULES = {
    'app.services.maintenance_workbook_protocol',
    'app.services.maintenance_workbook_plans',
    'app.services.maintenance_workbook_values',
}


def test_protocol_import_closure_is_standard_library_only():
    """Fresh Python without site-packages must import all three neutral layers."""
    backend = Path(__file__).resolve().parents[1]
    script = f'''
import importlib, json, sys
from typing import get_type_hints
for name in {sorted(_PURE_MODULES)!r}:
    importlib.import_module(name)
plans = sys.modules["app.services.maintenance_workbook_plans"]
get_type_hints(plans.MasterPlan)
get_type_hints(plans.MasterV2Plan)
print(json.dumps(sorted(name for name in sys.modules if name == "app" or name.startswith("app."))))
'''
    result = subprocess.run(
        [sys.executable, '-S', '-c', script], cwd=backend,
        text=True, capture_output=True, check=True,
    )
    assert set(json.loads(result.stdout)) == {'app', 'app.services', *_PURE_MODULES}


def test_protocol_static_imports_include_local_and_package_initializers():
    """Also reject dormant imports inside functions, not only imports run above."""
    backend = Path(__file__).resolve().parents[1]
    paths = [backend / 'app/__init__.py', backend / 'app/services/__init__.py']
    paths.extend(backend / (name.replace('.', '/') + '.py') for name in _PURE_MODULES)
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert not node.level, f'Unreviewed relative import in {path.name}'
                imported = [node.module or '']
            else:
                continue
            for name in imported:
                assert name in _PURE_MODULES or name.split('.')[0] in sys.stdlib_module_names, (
                    path.name, name,
                )


@pytest.mark.parametrize('name', [
    'CostRefill', 'SourceOrderAssignmentChange', 'SiteReturnFlag', 'MasterPlan',
    'V2MilestoneChange', 'ContractAmountChange', 'ProjectNoteChange',
    'MasterV2Plan', 'V2RowGuard', '_SiteAddressPatch',
])
def test_master_compatibility_exports_keep_type_identity(name):
    assert getattr(master, name) is getattr(plans, name)


@pytest.mark.parametrize('name', ['WorkbookError', 'ExpenseUpdate', 'CollectionOp', 'WorkbookPlan'])
def test_expense_compatibility_exports_keep_type_identity(name):
    assert getattr(ec, name) is getattr(plans, name)


def test_shared_exception_still_matches_adapter_catches_and_retains_details():
    issues = [{'row': 2}]
    details = {'conflicts': ['entity-1']}
    with pytest.raises(ec.WorkbookError) as caught:
        raise master.WorkbookError('conflict', '当前数据已变化', issues, details=details)
    error = caught.value
    assert type(error) is plans.WorkbookError
    assert str(error) == error.message == '当前数据已变化'
    assert error.code == 'conflict'
    assert error.issues is issues and error.details is details
    first = plans.WorkbookError('first', 'one')
    second = plans.WorkbookError('second', 'two')
    first.issues.append({'row': 3})
    first.details['extra'] = True
    assert second.issues == [] and second.details == {}


def test_plan_annotations_resolve_to_neutral_types():
    assert get_type_hints(plans.MasterPlan)['inner'] == plans.WorkbookPlan | None
    annotations = get_type_hints(plans.MasterV2Plan)
    assert annotations['expense_updates'] == tuple[plans.ExpenseUpdate, ...]
    assert annotations['receipt_ops'] == tuple[plans.CollectionOp, ...]
    assert annotations['row_guards'] == tuple[plans.V2RowGuard, ...]


def test_existing_plan_mutability_and_summary_are_preserved():
    # A frozen outer plan never implied deep immutability. The per-instance
    # dictionary is intentionally mutable and must not become shared state.
    first = plans.MasterV2Plan('p', ())
    second = plans.MasterV2Plan('p', ())
    first.site_issue_rows['issue'] = [2]
    assert second.site_issue_rows == {}
    with pytest.raises(FrozenInstanceError):
        first.project_id = 'other'
    ops = tuple(plans.CollectionOp(op, 'c', 'XSDD', date(2026, 10, 1), None, None, None)
                for op in ('CREATE', 'UPDATE', 'VOID'))
    inner = plans.WorkbookPlan('p', (
        plans.ExpenseUpdate('e1'), plans.ExpenseUpdate('e2', is_create=True),
    ), ops)
    plan = plans.MasterPlan('p', inner=inner)
    assert plan.sheets is protocol.ALL_SHEETS
    assert plan.summary == {
        'cost_refills': 0, 'site_return_flags': 0, 'expense_creates': 1,
        'expense_updates': 1, 'collection_creates': 1,
        'collection_updates': 1, 'collection_voids': 1,
    }


def test_protocol_versions_and_shared_constants_remain_distinct():
    assert master.PROTOCOL_VERSION == protocol.PROJECT_MASTER_PROTOCOL_VERSION == 'project-master-v1'
    assert ec.PROTOCOL_VERSION == protocol.EXPENSE_COLLECTION_PROTOCOL_VERSION == 'expense-collection-v1'
    assert master.SHEET_EXPENSE is ec.SHEET_EXPENSE is protocol.SHEET_EXPENSE
    assert master.SHEET_COLLECTION is ec.SHEET_COLLECTION is protocol.SHEET_COLLECTION
    assert master._META_SHEET is ec._META_SHEET is protocol._META_SHEET
    assert master.V2_PART_HEADERS is protocol.V2_PART_HEADERS
    assert master.V2_PART_EDITABLE is protocol.V2_PART_EDITABLE
    assert ec._EXPENSE_HEADERS is protocol._EXPENSE_HEADERS


@pytest.mark.parametrize(('source', 'expected'), [
    (None, ''), ('', ''), ('  PN  ', '  PN  '),
    (True, 'True'), (False, 'False'), (1, '1'), (1.0, '1'),
    (Decimal('1.000'), '1'), (Decimal('-0.00'), '-0'),
    (date(2026, 10, 23), '2026-10-23'),
    (datetime(2026, 10, 23, 23, 59), '2026-10-23'),
])
def test_wire_hash_value_retains_excel_round_trip_distinctions(source, expected):
    assert values._v2_hash_value(source) == expected


def test_hash_and_numeric_codec_compatibility_vectors():
    assert values._v2_hash([
        None, '', Decimal('1.00'), 1.0, True, False,
        date(2026, 10, 23), datetime(2026, 10, 23, 23, 59), ' 中文 ',
    ]) == 'b73f3634747d2741c76db1b87fac9c468eb0b12db0e8186d6df7385f7e31e4e3'
    assert values._v2_hash([False]) != values._v2_hash([0])
    assert values._num(None) == '' and values._num(Decimal('0')) == 0.0
    assert values._global_cost_decimal(None) is None
    with localcontext() as context:
        context.rounding = ROUND_HALF_EVEN
        assert values._global_cost_decimal(Decimal('1.005')) == '1.00'
        context.rounding = ROUND_HALF_UP
        assert values._global_cost_decimal(Decimal('1.005')) == '1.01'


def test_explicit_key_metadata_signature_covers_hidden_identity_and_scope():
    metadata = {'project_id': '项目-1', 'parts_row_ids': '1,2',
                'included_sheets': '03_备件明细', 'metadata_hmac': 'ignored'}
    signed = values._v2_metadata_signature(metadata, b'unit-test-only')
    assert signed == '855a942644aea50bdef88803dcfa97488d4c9d826abd1b1fccab5e001c7e8637'
    assert values._v2_metadata_signature(dict(reversed(list(metadata.items()))), b'unit-test-only') == signed
    assert values._v2_metadata_signature({**metadata, 'metadata_hmac': 'other'}, b'unit-test-only') == signed
    for key in ('project_id', 'parts_row_ids', 'included_sheets'):
        assert values._v2_metadata_signature({**metadata, key: metadata[key] + '!'}, b'unit-test-only') != signed
    assert values._v2_metadata_signature({**metadata, 'new_scope': 'bound'}, b'unit-test-only') != signed
    assert values._v2_metadata_signature(metadata, b'another-key') != signed


@pytest.mark.parametrize('size', [0, 30000, 30001, 300001])
def test_metadata_chunking_preserves_unicode_and_numeric_suffixes(size):
    text = '数' * size
    chunks = values._chunk_meta_value('parts_base_hashes', text)
    assert ''.join(value for _, value in chunks) == text
    assert all(len(value) <= 30000 for _, value in chunks)
    assert [key for key, _ in chunks] == [
        'parts_base_hashes',
        *(f'parts_base_hashes@{number}' for number in range(2, len(chunks) + 1)),
    ]


def test_existing_lossy_decoders_keep_their_specific_tolerance():
    assert values._encode_base_hashes({'b': '2', 'a': '1'}) == 'a:1,b:2'
    assert values._decode_base_hashes('bad, a:1 ,x:y:2,a:3,:empty,z:') == {
        'a': '3', 'x:y': '2', '': 'empty', 'z': '',
    }
    assert values._decode_base_hashes(None) == {}
    assert values._encode_row_ids(['b', 'a', 'a']) == 'a,a,b'
    assert values._decode_row_ids('a, a,,b,') == {'a', ' a', 'b'}
    assert values._decode_row_ids(None) == set()


def test_auto_identity_retains_export_scope_duplicate_occurrences_and_order_independence():
    def identities(rows, *, project='project', export='export'):
        occurrences = defaultdict(int)
        result = []
        for pn in rows:
            result.append(values._v2_auto_site_no(
                project, export, pn, date(2026, 10, 23), None, 'WBDD1',
                Decimal('2.00'), False, None, occurrences,
            ))
        return result, occurrences

    ordered, occurrences = identities(['PN-1', 'PN-2', 'PN-1'])
    reordered, _ = identities(['PN-2', 'PN-1', 'PN-1'])
    assert ordered[0] == 'LY-AUTO-10ecce463d711a281c81-7bfee23ce4c8a5f5a06c72f2'
    assert sorted(ordered) == sorted(reordered)
    assert len(set(ordered)) == 3 and sorted(occurrences.values()) == [1, 2]
    assert identities(['PN-1'], project='other')[0][0] != ordered[0]
    assert identities(['PN-1'], export='other')[0][0] != ordered[0]


def test_cell_and_example_detection_keep_missing_blank_and_zero_distinct():
    row = (0, '', None, ' 【示例】PN ')
    index = {'zero': 0, 'blank': 1, 'null': 2, 'missing_cell': 10}
    assert values._cell(row, index, 'zero') == 0
    assert values._cell(row, index, 'blank') == ''
    assert values._cell(row, index, 'null') is None
    assert values._cell(row, index, 'missing_cell') is None
    assert values._cell(row, index, 'missing_column') is None
    assert values._is_example_row(row)
    assert not values._is_example_row((0, None, '正常示例文字'))
