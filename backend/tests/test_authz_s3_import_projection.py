"""Whole-import bootstraps return metadata and opaque digests, never raw evidence."""
from sqlalchemy import event

from tests.test_authz_s3_document_imports import imports, setup, cerbos_url  # noqa: F401
from tests.test_maintenance_ckd_import import wbdd_project, _ckd_workbook_bytes, _HEAD_MAINT  # noqa: F401
from tests.test_maintenance_doc_import import _doc_workbook, _RETURN_HEAD, _RETURN_LINE
from app.authz import runtime
from app.config import get_settings


FORBIDDEN_COLUMNS = {'raw_json', 'report_json', 'total_amount', 'unit_cost', 'cost_amount',
                     'end_customer', 'receiver_phone', 'project_note', 'remark_raw'}


def test_unauthorized_batch_bootstrap_materializes_only_metadata_and_opaque_digest(db, imports):
    _, _, client, data, preview, read, _, _, _ = imports
    created = client.post(preview, files={'file': ('input.xlsx', data)},
                          headers={'Idempotency-Key': 'metadata-only-source'})
    assert created.status_code == 200, created.text
    columns = []

    def capture(_conn, cursor, statement, _params, _context, _many):
        if statement.lstrip().lower().startswith('select') and cursor.description:
            columns.extend(item.name for item in cursor.description)

    event.listen(db.get_bind(), 'after_cursor_execute', capture)
    try:
        response = client.get(read + created.json()['batch_id'], params={'view': 'authorization'})
    finally:
        event.remove(db.get_bind(), 'after_cursor_execute', capture)
    assert response.status_code == 200, response.text
    decisions = response.json()['authorization_context']['resources']
    assert all(item['decisions']['read']['effect'] == 'deny' for item in decisions.values())
    assert not FORBIDDEN_COLUMNS & set(columns), columns


def test_denied_apply_resolves_actual_parent_without_loading_whole_evidence(db, imports, wbdd_project, monkeypatch):
    _, grant, client, _, preview, read, _, _, kind = imports
    if kind == 'document_import_batch':
        preview = preview.replace('bxd_expense', 'return_order')
        data = _doc_workbook(sheet_title='Sheet1', head_headers=_RETURN_HEAD, line_headers=_RETURN_LINE,
            rows=[{'head': ['维保返件', '其他退回', '备件', '2026-08-03', '合成客户', 'CKD测试项目', '',
                           'WBDD-20260702-0014', '', '广州仓', '已生效', '', 'RKN-002', 'HEAD-2'],
                   'lines': [['', '02311AYV', '', '坏品', '', '', '5', 'LID-1', '1', '']]}])
    else:
        data = _ckd_workbook_bytes(rows=[{'head': _HEAD_MAINT, 'lines': []}])
    created = client.post(preview, files={'file': ('input.xlsx', data)},
                          headers={'Idempotency-Key': 'metadata-only-target'})
    assert created.status_code == 200, created.text
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', [grant.resource, grant.resource + '_targets'])
    runtime.activate_configured_resources(db.get_bind())
    columns = []

    def capture(_conn, cursor, statement, _params, _context, _many):
        if statement.lstrip().lower().startswith('select') and cursor.description:
            columns.extend(item.name for item in cursor.description)

    event.listen(db.get_bind(), 'after_cursor_execute', capture)
    try:
        response = client.post(read + created.json()['batch_id'] + '/apply', params={'view': 'authorization'})
    finally:
        event.remove(db.get_bind(), 'after_cursor_execute', capture)
    assert response.status_code == 200, response.text
    decisions = response.json()['authorization_context']['resources']
    assert decisions[grant.resource + '_targets']['decisions']['apply']['effect'] == 'deny'
    assert 'project_id' in columns and 'source_order_id' in columns
    assert not FORBIDDEN_COLUMNS & set(columns), columns
