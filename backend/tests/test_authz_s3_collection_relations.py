"""Generated collection content must not outlive its real parent relationships."""
from time import perf_counter
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import event, func, select, update

from app.authz import catalog, service
from app.authz.provenance import AccessCollector, validate_provenance
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceSiteIssue
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_analysis_access as analysis
from tests.test_authz_s3_analysis import analysis_client  # noqa: F401
from tests.test_authz_s3_workspace import workspace, grant  # noqa: F401
from tests.test_authz_s3_projects import canonical  # noqa: F401
from tests.test_authz_s3_project_foundation import projects  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_maintenance_analytics import _part
from tests.test_maintenance_return_metrics import _issue, _receipt


@pytest.mark.parametrize('relation', ['source_assignment', 'issue_parent', 'receipt_parent'])
def test_generated_collection_rejects_real_parent_reassignment_without_actor_change(
        db, analysis_client, projects, relation):
    actor, identity, ids = projects
    manager = db.get(MaintenanceProject, ids['manager'])
    part = _part(db, 'S3-CONTENT-RELATION')
    issue_line = _issue(db, manager, part, '3')
    receipt = _receipt(db, manager, part, '2')
    db.commit()
    grant(db, actor, 'maintenance_analysis', ['read_pn_ranking'])
    collector = AccessCollector(actor.id, actor.token_version, catalog.CATALOG_VERSION)
    with service.access_recorder(collector.record):
        payload = analysis.read(db, identity, endpoint='pn-ranking', params={'range': 'all', 'sort': 'pn'})
    saved = collector.snapshot()
    assert saved['schema_version'] == 2 and len(saved['resources']) == 1
    assert payload['total'] == 2
    validate_provenance(db, identity, saved)
    epoch, project_version = actor.token_version, manager.version
    if relation == 'source_assignment':
        row = db.scalar(select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.project_id == manager.project_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True)))
        db.execute(update(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.assignment_id == row.assignment_id).values(
            is_active=False, version=row.version + 1, archived_at=func.now(), archived_by='reparent-test'))
        db.add(MaintenanceSourceOrderAssignment(assignment_id=str(uuid4()),
            source_order_id=row.source_order_id, project_id=ids['other'], created_by='reparent-test'))
    elif relation == 'issue_parent':
        issue = db.get(MaintenanceSiteIssue, issue_line.issue_id)
        issue.project_id, issue.version = ids['other'], issue.version + 1
    else:
        receipt.project_id, receipt.version = ids['other'], receipt.version + 1
    db.commit()
    assert actor.token_version == epoch and manager.version == project_version
    with pytest.raises(HTTPException) as rejected:
        validate_provenance(db, identity, saved)
    assert rejected.value.status_code == 403
    assert rejected.value.detail['code'] == 'AUTHZ_ARTIFACT_STALE'
    # Regeneration under current SQL scopes is still permitted.
    fresh = AccessCollector(actor.id, actor.token_version, catalog.CATALOG_VERSION)
    with service.access_recorder(fresh.record):
        analysis.read(db, identity, endpoint='pn-ranking', params={'range': 'all', 'sort': 'pn'})
    validate_provenance(db, identity, fresh.snapshot())
    assert fresh.snapshot()['resources'][0]['authorization_revision'] != saved['resources'][0]['authorization_revision']


def test_relation_fingerprint_is_one_metadata_only_sql_statement(db, analysis_client, projects):
    from sqlalchemy.orm import Session
    from app.authz.context import read_snapshot
    from app.services import maintenance_authorization as domain
    actor, identity, _ = projects
    grant(db, actor, 'maintenance_analysis', ['read_filter_options'])
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    started = perf_counter()
    try:
        with read_snapshot(lambda: Session(db.get_bind())) as (snapshot, proof):
            domain.resolve_entry_context(snapshot, identity,
                entry_id='GET /api/maintenance/analytics/filter-options', params={}, read_snapshot=proof)
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    elapsed = perf_counter() - started
    fingerprints = [q for q in statements if 'string_agg' in q]
    assert len(fingerprints) == 1
    sql = fingerprints[0]
    for table in ['maintenance_source_order_assignment', 'maintenance_site_issue',
                  'maintenance_rkd_return_line', 'maintenance_project_expense_attribution',
                  'maintenance_project_contract', 'maintenance_collection_snapshot']:
        assert table in sql
    for hidden in ['.quantity', '.qty', '.contract_amount', '.cumulative_amount', '.amount_inc_tax',
                   '.description', '.evidence_ref', '.receipt_reference', '.remark']:
        assert hidden not in sql
    print(f'collection relationship fingerprint: one SQL, fixture elapsed={elapsed:.6f}s')


def test_relationship_fingerprint_explain_with_40000_real_relation_rows(db, projects):
    """Record measured scale evidence, without a flaky wall-clock assertion."""
    import json
    from pathlib import Path
    from sqlalchemy import text
    from sqlalchemy.dialects import postgresql
    from app.services.maintenance_authorization_relations import collection_relation_statement
    _, _, ids = projects
    part = _part(db, 'S3-FINGERPRINT-SCALE')
    db.execute(text("""
        INSERT INTO maintenance_site_issue
          (issue_id, project_id, issue_no, issue_date, raw_status, status_mapping_state,
           normalized_status, status_mapping_version)
        SELECT 's3-scale-issue-' || n, :project_id, 'SCALE-' || n, DATE '2026-01-01',
          'confirmed', 'mapped', 'confirmed', 'scale-v1' FROM generate_series(1, 20000) n
    """), {'project_id': ids['manager']})
    db.execute(text("""
        INSERT INTO maintenance_site_issue_line
          (issue_line_id, issue_id, line_no, part_id, pn, quantity, algorithm_version)
        SELECT 's3-scale-line-' || n, 's3-scale-issue-' || n, 1, :part_id, 'S3-SCALE', 1,
          'scale-v1' FROM generate_series(1, 20000) n
    """), {'part_id': part.id})
    db.commit()
    statement = collection_relation_statement('maintenance_analysis_projects')
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
    started = perf_counter()
    plan = db.scalar(text('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + compiled))[0]
    elapsed = perf_counter() - started
    def nodes(node):
        yield node
        for child in node.get('Plans', []):
            yield from nodes(child)
    scans = {row.get('Relation Name'): row['Actual Rows'] for row in nodes(plan['Plan']) if row.get('Relation Name')}
    assert scans['maintenance_site_issue'] >= 20000
    assert scans['maintenance_site_issue_line'] >= 20000
    assert plan['Plan']['Actual Rows'] == 1
    evidence = {'synthetic_relation_rows': 40000, 'client_elapsed_seconds': elapsed,
                'planning_ms': plan['Planning Time'], 'execution_ms': plan['Execution Time'],
                'table_scan_rows': scans, 'explain': plan}
    Path('/tmp/itsp-s3-relationship-scale.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print('relationship scale evidence: ' + json.dumps({k:v for k,v in evidence.items() if k != 'explain'}))
