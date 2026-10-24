"""Read-only implementation inventory; route metadata is never semantic proof.

Imports the real application without lifespan or DB I/O. Joins all 228 live
entries and 11 retired entries to mounted handlers, concrete dependencies,
current function calls and separately maintained semantic evidence ledgers.
Use --require-complete only after every author and independent reviewer closes
those ledgers. The original source hashes stay in the design specification.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import inspect
import json
import logging
import re
import sys
import textwrap
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
logging.disable(logging.CRITICAL)
from app.main import app
from app.authz.context_registry import ENTRY_CONTRACTS, AUTHORIZATION_VIEWS


def location(fn):
    try:
        return {'file': str(Path(inspect.getsourcefile(fn)).resolve().relative_to(ROOT)),
                'line': inspect.getsourcelines(fn)[1],
                'callable': fn.__module__ + '.' + fn.__qualname__}
    except (TypeError, ValueError, OSError):
        return {'callable': getattr(fn, '__name__', type(fn).__name__)}


def dependencies(dep):
    record = location(dep.call)
    # Only explicit public resource IDs; never arbitrary closure/session data.
    values = {}
    if inspect.isfunction(dep.call):
        for key, cell in zip(dep.call.__code__.co_freevars, dep.call.__closure__ or ()):
            value = cell.cell_contents
            if key in {'resources', 'resource', 'resource_id'} and isinstance(value, (str, tuple, list)):
                if isinstance(value, str) or all(isinstance(x, str) for x in value):
                    values[key] = value
    if values: record['registered_resources'] = values
    record['dependencies'] = [dependencies(child) for child in dep.dependencies]
    return record


def calls(fn):
    source = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(source)
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call): continue
        expr = node.func
        target = fn.__globals__.get(expr.id) if isinstance(expr, ast.Name) else None
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            parent = fn.__globals__.get(expr.value.id)
            if inspect.ismodule(parent): target = getattr(parent, expr.attr, None)
        if inspect.isfunction(target) and target.__module__.startswith('app.'):
            result.append({**location(target), 'expression': ast.unparse(expr)})
    return sorted({(x['callable'], x['expression']): x for x in result}.values(), key=lambda x: x['callable'])


def main(strict=False):
    spec = json.loads((HERE / 's3-execution-spec.json').read_text())
    project = json.loads((HERE / 's3-project-read-finance-implementation-coverage.json').read_text())
    workflow = json.loads((HERE / 's3-workflow-implementation-coverage.json').read_text())
    ledgers = {e['entry_id']: e for e in project['entries'] + workflow['entries']}
    assert len(ledgers) == len(project['entries']) + len(workflow['entries'])
    live = {e['entry_id']: e for e in spec['entries']}
    assert len(live) == 228 and len(set(ledgers) - set(live)) == 11
    routes = {}
    for route in app.routes:
        if not hasattr(route, 'dependant'): continue
        for method in route.methods:
            key = method + ' ' + route.path
            assert key not in routes, key
            routes[key] = route
    rows = []
    errors = []
    pending = []
    for eid in sorted(ledgers):
        ledger = ledgers[eid]
        route = routes.get(eid)
        if route is None:
            errors.append({'entry_id': eid, 'error': 'missing_actual_mounted_route'})
            continue
        fn = route.endpoint
        row = {'entry_id': eid, 'classification': 'live' if eid in live else 'retired',
               'actual_handler': location(fn), 'actual_dependencies': dependencies(route.dependant),
               'direct_function_calls': calls(fn), 'semantic_evidence': ledger,
               'metadata_is_not_runtime_proof': True}
        if eid in live:
            assert eid in ENTRY_CONTRACTS
            row['registered_requirements'] = {k: v['requirements'] for k,v in ENTRY_CONTRACTS[eid]['branches'].items()}
            row['has_registered_authorization_view'] = eid in AUTHORIZATION_VIEWS
            if not (ledger['status'].startswith('verified') or ledger['status'] == 'semantic_tests_passed'):
                pending.append({'entry_id': eid, 'status': ledger['status']})
        else:
            assert eid not in ENTRY_CONTRACTS
            source = inspect.getsource(fn)
            if not ('410' in source or 'retired' in source):
                errors.append({'entry_id': eid, 'error': 'retired_handler_needs_explicit_review'})
        rows.append(row)
    frozen = {s['file']: s['sha256'] for s in spec['supporting_sources']}
    frozen.update({e['source']['file']: e['source']['sha256'] for e in spec['entries']})
    drift = [{'file': file, 'frozen_sha256': old,
              'current_sha256': hashlib.sha256((ROOT / file).read_bytes()).hexdigest()}
             for file,old in sorted(frozen.items()) if hashlib.sha256((ROOT/file).read_bytes()).hexdigest() != old]
    result = {'status': 'inventory_only_pending_semantic_closure' if pending or errors else 'inventory_and_declared_semantic_ledger_closed_not_ci',
              'live_entries': len(live), 'retired_entries': 11, 'actual_mounted_matches': len(rows),
              'pending': pending, 'structural_errors': errors, 'source_changes': drift,
              'limits': ['AST calls do not prove authorization or branch dominance.',
                         'Semantic results are linked author/reviewer evidence, not rerun by this script.',
                         'Design hash drift is recorded; original frozen hashes are never replaced.',
                         'This is neither full CI nor production proof.'], 'entries': rows}
    (HERE / 's3-implementation-inventory.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in result.items() if k not in {'entries','source_changes'}}, ensure_ascii=False, indent=2))
    if errors or (strict and pending): raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-complete', action='store_true')
    main(parser.parse_args().require_complete)
