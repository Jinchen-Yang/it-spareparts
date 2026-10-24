"""Real bootstrap reads do not materialize full facts before their PDP decision."""
from contextlib import contextmanager

import re

from sqlalchemy import event, select

from tests.test_authz_s3_warehouse import warehouse, applied, post_file, set_actions, protected, Ambiguity  # noqa:F401
from tests.test_authz_s3_replenishment import replenishment, setup, grant, submit  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401


@contextmanager
def projected_sql(db):
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(' '.join(statement.lower().split()))
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        yield statements
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)


def directly_projects(statements, table, columns):
    return any(re.search(r'(?:select |, )'+re.escape(table)+r'\.'+re.escape(column)+r'(?: as \w+)?(?=,| from )', sql)
               for sql in statements for column in columns)


def test_denied_warehouse_replay_never_materializes_receipt_json(db, warehouse):
    client, uid, content = warehouse
    plan, _, fields = applied(client, content)
    fields.pop('authorization_precondition')
    set_actions(db, uid, protected.IMPORT, ['preview'])
    with projected_sql(db) as statements:
        response = post_file(client, 'warehouse-imports/'+plan['import_id']+'/apply', content, fields=fields)
    assert response.status_code == 403, response.text
    assert not directly_projects(statements, 'maintenance_warehouse_import_batch', ['result_json']), statements
    assert not directly_projects(statements, 'maintenance_warehouse_audit_event', ['after_json']), statements


def test_denied_warehouse_ambiguity_never_materializes_candidate_evidence(db, warehouse):
    client, uid, content = warehouse
    applied(client, content)
    item = db.scalar(select(Ambiguity).where(Ambiguity.ambiguity_type == 'controlled_attachment'))
    body = {'version': item.version, 'reason': 'actual evidence review', 'decision': 'acknowledge'}
    path = '/api/maintenance/warehouse-ambiguities/'+item.ambiguity_id+'/resolve'
    set_actions(db, uid, protected.WAREHOUSE, ['read_ambiguities'])
    with projected_sql(db) as statements:
        response = client.post(path, json=body)
    assert response.status_code == 403, response.text
    assert not directly_projects(statements, 'maintenance_warehouse_ambiguity', ['candidates_json', 'evidence_json']), statements


def test_denied_replenishment_read_never_materializes_application_snapshot(db, replenishment):
    uid, pid, part, client = replenishment
    grant(db, uid, 'replenishment_project_commands', ['submit'])
    created = submit(client, pid, part)
    assert created.status_code == 201, created.text
    with projected_sql(db) as statements:
        response = client.get('/api/replenishment-beta/applications/'+created.json()['application_id'])
    assert response.status_code == 403, response.text
    assert not directly_projects(statements, 'replenishment_application', ['project_name_snapshot', 'salesperson_name_snapshot']), statements


def test_denied_warehouse_preview_resolves_part_identity_without_part_facts(db, warehouse):
    client, uid, content = warehouse
    set_actions(db, uid, protected.IMPORT, ['apply'])
    with projected_sql(db) as statements:
        response = post_file(client, 'warehouse-imports/preview', content)
    assert response.status_code == 403, response.text
    assert not directly_projects(statements, 'dim_part', ['description', 'category_major', 'category_minor']), statements
