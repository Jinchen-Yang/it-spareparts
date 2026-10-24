"""No mounted entry may silently escape a declared resource/platform/retired family.

This is a route drift gate, not evidence that metadata authorizes requests.
Behavioral PG/PDP and HTTP denial suites remain independently required.
"""
import json
from pathlib import Path
from collections import Counter

from app.authz.boundaries import BOUNDARY_ENTRY_CONTRACTS, boundary_descriptors, validate_boundaries
from app.authz.catalog import RESOURCE_DEFINITIONS, resource_descriptors
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.control_access import ACCOUNT_DATA_ENTRY_CONTRACTS
from app.authz.import_contract import HTTP_ENTRIES, WORKER
from app.main import app


def noncontext_inventory():
    value = json.loads(Path(__file__).with_name('fixtures').joinpath('authz_noncontext_http_inventory.json').read_text())
    assert value['schema_version'] == 1
    rows = value['entries']
    assert len({r['entry_id'] for r in rows}) == len(rows)
    assert Counter(r['authority_kind'] for r in rows) == {'ordinary_resource':88,'retired':12}
    return {r['entry_id']:r for r in rows}


def test_every_actual_mount_has_one_closed_authority_or_explicit_profile_security_branch():
    actual = {}
    for route in app.routes:
        for method in getattr(route, 'methods', ()):
            entry = method+' '+route.path
            assert entry not in actual, f'duplicate mounted authority: {entry}'
            actual[entry] = route
    http_contexts = {k:v for k,v in ENTRY_CONTRACTS.items() if k.split(' ',1)[0] in {'GET','POST','PUT','PATCH','DELETE','HEAD'}}
    other = noncontext_inventory()
    groups = [set(http_contexts), set(BOUNDARY_ENTRY_CONTRACTS), set(ACCOUNT_DATA_ENTRY_CONTRACTS), set(other)]
    expected = set.union(*groups)
    assert set(actual) == expected, {'unclassified':sorted(set(actual)-expected),'unmounted':sorted(expected-set(actual))}
    intersections = {key for key in expected if sum(key in group for group in groups)>1}
    assert intersections == {'PUT /api/accounts/{username}'}
    assert ACCOUNT_DATA_ENTRY_CONTRACTS['PUT /api/accounts/{username}']['branch'] == 'profile'
    assert BOUNDARY_ENTRY_CONTRACTS['PUT /api/accounts/{username}']['branch'] == 'security_change'
    for entry, contract in other.items():
        endpoint = actual[entry].endpoint
        assert endpoint.__module__ == contract['module'], entry
        assert endpoint.__name__ == contract['handler'], entry
    assert len(actual) == 398


def test_final_vocabulary_has_no_unexplained_planned_resources_or_boundary_grants():
    assert Counter(r['migration_state'] for r in resource_descriptors()) == {'implemented':107,'retired':5}
    assert validate_boundaries(RESOURCE_DEFINITIONS)
    retired = {r['id'] for r in resource_descriptors() if r['migration_state']=='retired'}
    assert {'data_import','maintenance_source_document','maintenance_ai_assist'} <= retired
    assert len(boundary_descriptors()) == 10
    for contract in ACCOUNT_DATA_ENTRY_CONTRACTS.values():
        assert contract['action'] in RESOURCE_DEFINITIONS[contract['resource']].actions
    for entry in HTTP_ENTRIES:
        assert entry in ENTRY_CONTRACTS
    assert WORKER in ENTRY_CONTRACTS
    assert {'TOOL agent.write_excel','TOOL agent.write_report'} <= ENTRY_CONTRACTS.keys()
