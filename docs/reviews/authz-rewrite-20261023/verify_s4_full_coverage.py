"""Audit current mounts and executable vocabulary; never a permission authority.

Writes only this review directory. Existing test-file references are evidence
links, not a claim that static discovery ran their assertions or a browser.
"""
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'backend'))
from app.main import app
from app.authz.catalog import RESOURCE_DEFINITIONS, resource_descriptors, validate_catalog, CATALOG_VERSION
from app.authz.boundaries import BOUNDARY_ENTRY_CONTRACTS, boundary_descriptors
from app.authz.control_access import ACCOUNT_DATA_ENTRY_CONTRACTS
from app.authz.context_registry import ENTRY_CONTRACTS, AUTHORIZATION_VIEWS
from app.authz.import_contract import HTTP_ENTRIES, WORKER
from app.agent.tools import _REGISTRY as AGENT_TOOLS


def read(name):
    return json.loads((HERE / name).read_text())


def test_links(paths):
    output = []
    for path in paths:
        file = ROOT / path
        assert file.is_file(), path
        tree = ast.parse(file.read_text())
        output.append({'file': path, 'functions': [node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_')],
            'meaning': 'persistent_assertion_source_not_a_new_execution'})
    return output


def execution_sites():
    """Find real task launch/offload callsites, including nested worker closures."""
    launch_methods = {'Thread', 'ThreadPoolExecutor', 'ProcessPoolExecutor', 'create_task',
        'run_in_executor', 'run_sync', 'add_task', 'run_in_threadpool', 'parse_in_threadpool', 'run_worker'}
    rows = []
    for path in sorted((ROOT / 'backend/app').rglob('*.py')):
        tree = ast.parse(path.read_text())
        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.names = []
            def visit_FunctionDef(self, node):
                self.names.append(node.name)
                self.generic_visit(node)
                self.names.pop()
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_Call(self, node):
                name = (node.func.attr if isinstance(node.func, ast.Attribute) else
                    node.func.id if isinstance(node.func, ast.Name) else None)
                if name in launch_methods:
                    rows.append({'file':str(path.relative_to(ROOT)), 'line':node.lineno,
                        'function':'.'.join(self.names), 'call':ast.unparse(node.func),
                        'target_expression':ast.unparse(node.args[0]) if node.args else None,
                        'keywords':{k.arg:ast.unparse(k.value) for k in node.keywords if k.arg == 'target'}})
                self.generic_visit(node)
        Visitor().visit(tree)
    return rows


def main():
    validate_catalog()
    baseline = {r['entry_id']: r for r in read('s4-http-entry-baseline.json')['entries']}
    noncontext = json.loads((ROOT / 'backend/tests/fixtures/authz_noncontext_http_inventory.json').read_text())
    other = {r['entry_id']: r for r in noncontext['entries']}
    project = read('s3-project-read-finance-implementation-coverage.json')['entries']
    workflow = read('s3-workflow-implementation-coverage.json')['entries']
    s3 = {r['entry_id']:r for r in [*project, *workflow]}
    assert len(s3) == len(project) + len(workflow)
    mounted = {}
    for route in app.routes:
        for method in getattr(route, 'methods', ()):
            key = method + ' ' + route.path
            assert key not in mounted, key
            mounted[key] = route
    http_contracts = {k:v for k,v in ENTRY_CONTRACTS.items() if k.split(' ',1)[0] in {'GET','HEAD','POST','PUT','PATCH','DELETE'}}
    declared = set(http_contracts) | set(other) | set(BOUNDARY_ENTRY_CONTRACTS) | set(ACCOUNT_DATA_ENTRY_CONTRACTS)
    assert set(mounted) == declared, {'undeclared':sorted(set(mounted)-declared), 'unmounted':sorted(declared-set(mounted))}
    rows = []
    for key, route in sorted(mounted.items()):
        old = baseline.get(key, {})
        kind = 'ordinary_resource'
        if key in BOUNDARY_ENTRY_CONTRACTS:
            kind = 'platform_boundary'
        if key in other and other[key]['authority_kind'] == 'retired':
            kind = 'retired_endpoint'
        if key in ACCOUNT_DATA_ENTRY_CONTRACTS and key in BOUNDARY_ENTRY_CONTRACTS:
            kind = 'explicit_profile_security_body_branches'
        stage = old.get('stage', 'S4_platform')
        semantic_status = ('prior_exact_CI_evidence_current_integration_pending' if stage in ('S2','S3') else
            'local_implementation_independent_and_exact_CI_pending')
        evidence = old.get('evidence')
        if stage == 'S4_import':
            evidence = 's4-import-implementation-review.md; backend/tests/test_authz_s4_import.py'
        elif stage == 'S4_platform':
            evidence = 's4-platform-implementation-independent-review.md; backend/tests/test_authz_s4_platform_independent.py'
            semantic_status = 'local_cross_author_platform_review_passed_exact_CI_pending'
        elif stage == 'S4_agent':
            evidence = 's4-agent-implementation-review.md; s4-astra-platform-frontend-cross-review.md; backend/tests/test_authz_s4_agent.py; backend/tests/test_authz_s4_agent_cutover_independent.py; backend/tests/test_authz_s4_legacy_file_publication_independent.py'
            semantic_status = 'local_cross_author_agent_review_passed_exact_CI_pending'
        residual = old.get('legacy_residual_disposition', 'closed_live_namedadmin_no_ordinary_grant')
        if stage == 'S4_import':
            residual = ('legacy_handler_only_formal_no_view_and_all_identified_sources_never_cutover; '
                        'canonical_views_never_fall_back; persistent_resource_and_authz_s4_guards')
        row = {'entry_id':key, 'module':route.endpoint.__module__, 'handler':route.endpoint.__name__,
            'stage':stage, 'authority_kind':kind, 'semantic_evidence_status':semantic_status,
            'evidence':evidence, 'legacy_residual_disposition':residual}
        if key in http_contracts:
            row['exact_context_contract'] = http_contracts[key]
            if not http_contracts[key]['branches']:
                assert key == 'POST /api/maintenance/projects/stable'
                row['authority_kind'] = 'fixed_business_denial'
                row['canonical_contract'] = 'D05_source_import_creation_only_zero_project_alias_audit_writes'
            else:
                assert key in AUTHORIZATION_VIEWS, key
        if key in BOUNDARY_ENTRY_CONTRACTS:
            row['boundary_contract'] = BOUNDARY_ENTRY_CONTRACTS[key]
        if key in ACCOUNT_DATA_ENTRY_CONTRACTS:
            row['ordinary_data_contract'] = ACCOUNT_DATA_ENTRY_CONTRACTS[key]
        if key in s3:
            row['prior_stage_semantic_ledger'] = s3[key]
        rows.append(row)
    descriptors = resource_descriptors()
    resources = []
    surfaces = []
    for descriptor in descriptors:
        rid = descriptor['id']
        if descriptor['migration_state'] == 'implemented':
            definition = RESOURCE_DEFINITIONS[rid]
            assert {a['id'] for a in descriptor['actions']} == set(definition.actions)
            assert {f['id'] for f in descriptor['field_permissions']} == set(definition.field_permissions)
        else:
            assert descriptor['migration_state'] == 'retired'
            assert not descriptor['actions'] and not descriptor['surfaces']
        resources.append({**{key:descriptor[key] for key in ('id','label','migration_state','actions','scopes','field_permissions')}, 'notice':descriptor.get('notice')})
        for surface in descriptor['surfaces']:
            row = {'resource_id':rid, **surface}
            row['classification'] = ('dormant_no_route' if not surface['page_routes'] else
                'route_entry' if surface.get('route_entry') else 'embedded_only')
            surfaces.append(row)
    assert len({s['id'] for s in surfaces}) == len(surfaces)
    tool_bindings = {
        'search_parts':'part_access.read_part_search', 'get_part_overview':'part_access.read_part_overview',
        'lookup_prices_bulk':'part_access.read_part_search + read_quick_pricing',
        'get_profit_ranking':'profit.read_profit', 'list_recent_purchases':'purchase_query.read_recent_purchases',
        'get_purchase_analysis':'purchase_analysis.read_analysis', 'get_inventory':'inventory.read_dynamic',
        'get_cancellation_stats':'purchase_analysis.read_cancellation_stats',
        'get_maintenance_board':'maintenance_cost_access.read / BOARD',
        'get_maintenance_projects':'maintenance_cost_access.read / PROJECTS',
        'get_maintenance_lines':'maintenance_cost_access.read / LINES',
        'inspect_file':'agent_file.preview + actual content sources',
        'read_file_rows':'agent_file.preview + actual content sources',
        'read_document':'agent_file.preview + actual content sources',
        'write_excel':'TOOL agent.write_excel / agent_file.generate + base sources',
        'write_report':'TOOL agent.write_report / agent_file.generate',
        'list_skills':'static skill descriptions under current execution gate',
        'get_skill':'static skill descriptions under current execution gate',
    }
    assert set(AGENT_TOOLS) == set(tool_bindings)
    nonhttp = [{'entry_id':key, 'contract':value} for key,value in ENTRY_CONTRACTS.items() if key not in http_contracts]
    assert {r['entry_id'] for r in nonhttp} == {'TOOL agent.write_excel','TOOL agent.write_report',WORKER}
    evidence = test_links([
        'backend/tests/test_authz_s4_complete_inventory.py', 'backend/tests/test_authz_s4_import.py',
        'backend/tests/test_authz_s4_migration.py', 'backend/tests/test_authz_s4_platform_independent.py',
        'backend/tests/test_authz_s4_platform.py', 'backend/tests/test_authz_s4_machine_boundaries.py',
        'backend/tests/test_authz_s4_domain_contexts.py', 'backend/tests/test_authz_s4_agent.py',
        'backend/tests/test_authz_s4_agent_files.py', 'backend/tests/test_authz_s4_agent_lifecycle.py',
        'backend/tests/test_authz_s4_artifact_protocol.py', 'backend/tests/test_authz_s4_stream_protocol.py',
        'backend/tests/test_authz_s4_return_worker_independent.py',
        'backend/tests/test_authz_s4_legacy_worker_cutover.py',
    ])
    background = read('s4-background-entry-reconciliation.json')
    for entry in background['entries']:
        assert (ROOT / entry['file']).is_file()
        entry['persistent_tests'] = test_links(entry['tests'])
    sites = execution_sites()
    launch_adapters = {
        ('api/chat_sessions.py', 'chat_stream'): 'chat_session_generation',
        ('api/imports.py', '_legacy_upload_batch'): 'legacy_import_job',
        ('api/maintenance.py', '_close_roundtrip_form'): 'roundtrip_temp_ownership',
        ('api/maintenance.py', '_parse_and_save_roundtrip_upload'): 'roundtrip_temp_ownership',
        ('api/maintenance.py', '_wait_for_roundtrip_import_worker'): 'global_roundtrip_worker',
        ('api/maintenance_acceptance_checklist.py', 'preview_checklist'): 'input_parse_offloads',
        ('api/maintenance_ai_fallback.py', 'propose_ai_mapping'): 'input_parse_offloads',
        ('api/maintenance_ckd_import.py', 'preview_ckd_import'): 'input_parse_offloads',
        ('api/maintenance_doc_import.py', 'preview_doc_import'): 'input_parse_offloads',
        ('api/maintenance_ledger.py', 'preview_ledger_import'): 'input_parse_offloads',
        ('api/maintenance_dashboard.py', '_guard_export_stream.chunks'): 'dashboard_download_checks',
        ('api/maintenance_manager_workbooks.py', '_run_validation_worker'): 'manager_workbook_validate',
        ('api/maintenance_manager_workbooks.py', 'validate_manager_workbook_upload'): 'manager_workbook_validate',
        ('api/maintenance_project_workbooks.py', '_run_project_workbook_validation_worker'): 'project_workbook_validate',
        ('api/maintenance_project_workbooks.py', 'validate_project_workbook_upload'): 'project_workbook_validate',
        ('api/maintenance_return_receipt_import.py', 'upload'): 'return_import_dispatch',
        ('api/maintenance_return_receipt_import.py', 'retry'): 'return_import_dispatch',
        ('api/maintenance_return_receipts.py', 'get_route_handler.audited'): 'return_failure_audit',
        ('api/maintenance_warehouse.py', 'preview_warehouse_import'): 'warehouse_offload',
        ('api/maintenance_warehouse.py', 'apply_warehouse_import'): 'warehouse_offload',
        ('services/authorization_downloads.py', 'guard_stream.chunks'): 'guarded_download_checks',
        ('services/import_safety.py', 'parse_in_threadpool'): 'input_parse_offloads',
        ('services/source_import_access.py', 'enqueue_job'): 'canonical_import_job',
        ('services/workbook_tasks.py', 'run_worker'): 'workbook_executor',
    }
    assert {(r['file'].removeprefix('backend/app/'), r['function']) for r in sites} == set(launch_adapters), 'Execution launch function drift requires semantic review'
    for site in sites:
        site['adapter_id'] = launch_adapters[(site['file'].removeprefix('backend/app/'),site['function'])]
        site['source_sha256'] = hashlib.sha256((ROOT/site['file']).read_bytes()).hexdigest()
    assert {r['adapter_id'] for r in sites} == {r['id'] for r in background['entries']}
    operator_scripts = sorted(str(p.relative_to(ROOT)) for p in (ROOT / 'backend/scripts').glob('*.py'))
    operator_scripts.append('backend/app/seed_users.py')
    value = {'schema_version':1, 'status':'actual_mount_and_vocabulary_reconciled_not_final_acceptance',
        'source_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'catalog_version':CATALOG_VERSION, 'working_tree_is_uncommitted':True,
        'counts':{'http':len(rows),'http_by_stage':dict(Counter(r['stage'] for r in rows)),
            'http_by_authority':dict(Counter(r['authority_kind'] for r in rows)),
            'registered_nonhttp':len(nonhttp),'actual_agent_tools':len(tool_bindings),
            'execution_adapters':len(background['entries']), 'execution_launch_sites':len(sites),
            'offline_operator_scripts':len(operator_scripts),
            'modules_by_state':dict(Counter(r['migration_state'] for r in resources)),
            'boundaries':len(boundary_descriptors()),
            'resource_action_pairs':sum(len(r['actions']) for r in resources),
            'resource_field_pairs':sum(len(r['field_permissions']) for r in resources),
            'surfaces':len(surfaces),'surfaces_by_classification':dict(Counter(r['classification'] for r in surfaces))},
        'http_entries':rows, 'registered_nonhttp_entries':nonhttp,
        'actual_tool_bindings':tool_bindings, 'resources':resources,'boundaries':boundary_descriptors(),
        'background_execution':background, 'execution_launch_sites':sites,
        'offline_operator_scripts':{'classification':'local_build_maintenance_operator_tools_not_employee_api',
            'paths':operator_scripts, 'automatic_scheduler_found':False,
            'limit':'No installed host cron/systemd or production job inventory was queried; this is repository-only.'},
        'surfaces':surfaces,'persistent_tests':evidence,
        'remaining_acceptance':[
            'exact final commit full backend and frontend CI including prior stages',
            'real browser cross-resource disabled/partial/context/CAS/revocation user journeys',
            'cross-channel HTTP/Agent/background/artifact replay equivalence acceptance',
            'migration/rollback deployment rehearsal and code-cost/residual review',
            'user unified acceptance; no production permission inferred'],
        'limits':[
            'Route equality and action/field closure are structural, not semantic authorization proof.',
            'No lifespan, worker execution, production database or browser is started by this script.',
            'S2/S3 exact CI references are historical evidence, not a new current-tree run.',
            'A dormant surface has no route; it is not counted as a user-tested reachable UI.',
            'Only 2 TOOL and 1 WORKER are new context entries; 18 actual tool dispatches remain separately enumerated.',
            'All repository task/thread launch sites and 14 semantic execution adapters are separately reconciled; 3 new contracts do not imply only 3 actual workers.',
            'Control, public, framework and service boundaries never become assignable employee Grants.']}
    out=HERE/'s4-full-coverage-reconciliation.json'
    out.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'counts':value['counts'],'sha256':hashlib.sha256(out.read_bytes()).hexdigest()},ensure_ascii=False))

if __name__ == '__main__':
    main()
