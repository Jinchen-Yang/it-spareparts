"""Independent transport and metadata-only bootstrap boundaries across workflows."""
import io
import json
import zipfile

from sqlalchemy import event, func, select
from app.models.system import SysImportBatch
from tests.test_authz_s3_document_imports import imports, setup, cerbos_url  # noqa: F401
from tests.test_authz_s3_wbdd import wbdd, differences, post  # noqa: F401
from tests.test_authz_s3_source_cancellation import cancellation  # noqa: F401


def _precondition(response):
    assert response.status_code == 200, response.text
    context = response.json()['authorization_context']
    return {key: context[key] for key in ('catalog_version', 'authorization_epoch', 'context')}


def _changed_file(data):
    output = io.BytesIO(data)
    with zipfile.ZipFile(output, 'a') as archive:
        archive.comment = b'changed actual canonical input'
    return output.getvalue()


def test_doc_and_ckd_multipart_precondition_is_not_silently_ignored(db, imports):
    _, _, client, data, preview, _, Model, _, _ = imports
    pre = _precondition(client.post(preview, params={'view': 'authorization'},
        files={'file': ('input.xlsx', data)}, headers={'Idempotency-Key': 'independent-original-file'}))
    result = client.post(preview, files={'file': ('input.xlsx', _changed_file(data))},
        data={'authorization_precondition': json.dumps(pre)}, headers={'Idempotency-Key': 'independent-new-file'})
    assert result.status_code == 409, result.text
    assert db.scalar(select(func.count()).select_from(Model)) == 0


def test_wbdd_multipart_precondition_is_not_silently_ignored(db, wbdd):
    _, _, client, data = wbdd
    pre = _precondition(post(client, data, {'view': 'authorization'}))
    result = client.post('/api/maintenance/wbdd-imports', files={'file': ('input.xlsx', _changed_file(data))},
        data={'authorization_precondition': json.dumps(pre)}, headers={'Idempotency-Key': 'independent-new-wbdd'})
    assert result.status_code == 409, result.text
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 0


def _only_metadata(db, call):
    projections = []
    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().lower().startswith('select'):
            projections.append(statement.lower().split('from ', 1)[0])
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        result = call()
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert result.status_code == 200, result.text
    forbidden = [sql for sql in projections if any(column in sql for column in (
        'f_maintenance_order.end_customer', 'f_maintenance_order.receiver_phone',
        'f_maintenance_line.cost_amount', 'f_maintenance_line.unit_cost'))]
    assert not forbidden, '\n'.join(forbidden)


def test_source_cancellation_authview_reads_only_real_relationship_metadata(db, cancellation):
    _, _, client, _, _, detail, _ = cancellation
    _only_metadata(db, lambda: client.get(detail, params={'view': 'authorization'}))


def test_wbdd_reconcile_authview_reads_only_real_relationship_metadata(db, differences):
    from tests.test_maintenance_wbdd_sync_api import _body
    _, _, client, _, _, _ = differences
    latest = client.get('/api/maintenance/wbdd-imports/latest/missing')
    assert latest.status_code == 200, latest.text
    value = latest.json()
    _only_metadata(db, lambda: client.post(f"/api/maintenance/wbdd-imports/{value['batch_id']}/reconcile",
        params={'view': 'authorization'}, json=_body(value)))
