"""Real queued dispatcher must not read business reports before task authority."""
import re

import pytest
from sqlalchemy import event, func, select

from app.models.authorization import SysAuthorizationArtifact
from app.models.system import SysUser
from app.services import maintenance_return_import_authorization as protected
from app.services import maintenance_return_receipt_import as business
from tests.test_authz_s3_return_import import job, setup, upload, cerbos_url  # noqa: F401


@pytest.mark.parametrize('revocation', ['epoch', 'grant'])
def test_public_worker_dispatcher_rejects_before_materializing_report(db, job, monkeypatch, revocation):
    uid, _, _, grant, client = job
    dispatcher = business.run_job
    # Leave a real submitted generation queued, then exercise its actual public
    # dispatcher (not a direct call to the protected implementation).
    monkeypatch.setattr(business, 'run_job', lambda *_args: None)
    submitted = upload(client)
    assert submitted.status_code == 202, submitted.text
    batch_id = submitted.json()['batch_id']
    if revocation == 'epoch':
        db.get(SysUser, uid).token_version += 1
    else:
        grant.actions = ['job_read']
        grant.version += 1
    db.commit()

    calls = []
    original = protected.run_job
    def delegate(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(protected, 'run_job', delegate)
    statements = []
    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().lower().startswith('select'):
            statements.append(statement.lower())
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        dispatcher(batch_id, 1)
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)

    assert calls == [(batch_id, 1)]
    # Narrow protocol/owner/generation JSON paths and opaque SQL hashes are
    # allowed. A direct report_json projection materializes unrelated facts.
    forbidden = []
    table = business.Batch.__tablename__
    for sql in statements:
        projection = sql.split('from ', 1)[0]
        if re.search(rf'\b{table}\.report_json\s+(?:as\s+\w+)?\s*(?:,|$)', projection):
            forbidden.append(sql)
    assert not forbidden, '\n'.join(forbidden)
    assert db.get(business.Batch, batch_id, populate_existing=True).report_json['status'] == 'queued'
    for model in (business.Head, business.Line, business.Receipt):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact).where(
        SysAuthorizationArtifact.domain_kind == protected.PLAN_KIND)) == 0
