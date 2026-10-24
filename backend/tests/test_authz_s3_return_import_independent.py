"""Independent original-publication boundary, beyond the producer's RR check."""
import pytest
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.models.authorization import SysAuthorizationArtifact
from app.models.system import SysUser
from app.services import maintenance_return_import_authorization as protected
from tests.test_authz_s3_return_import import job, setup, upload, cerbos_url  # noqa: F401


@pytest.mark.parametrize('change,expected', [('epoch', 401), ('binding', 403)])
def test_original_revalidates_after_materialization_before_release(db, job, monkeypatch, change, expected):
    uid, _, _, _, client = job
    created = upload(client)
    assert created.status_code == 202, created.text
    batch_id = created.json()['batch_id']
    original = protected.read

    def rendered(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs['entry'] == protected.RAW and not kwargs.get('authorization_view'):
            with Session(db.get_bind()) as writer:
                if change == 'epoch':
                    writer.execute(update(SysUser).where(SysUser.id == uid).values(token_version=SysUser.token_version + 1))
                else:
                    writer.execute(delete(SysAuthorizationArtifact).where(
                        SysAuthorizationArtifact.domain_kind == protected.KIND,
                        SysAuthorizationArtifact.domain_id == batch_id))
                writer.commit()
        return result

    monkeypatch.setattr(protected, 'read', rendered)
    response = client.get(protected.BASE + '/' + batch_id + '/original')
    assert response.status_code == expected, 'Current reader or immutable original binding changed before byte publication'


def test_multipart_precondition_binds_the_actual_uploaded_bytes(db, job):
    import json
    from sqlalchemy import func, select
    from tests.test_return_receipt_import import workbook
    from app.services import maintenance_return_receipt_import as business
    _, _, _, _, client = job
    view = upload(client, workbook(qty='2'), params={'view': 'authorization'})
    assert view.status_code == 200, view.text
    context = view.json()['authorization_context']
    precondition = {key: context[key] for key in ('catalog_version', 'authorization_epoch', 'context')}
    response = client.post(protected.BASE,
        files={'file': ('return.xlsx', workbook(qty='3'))},
        data={'authorization_precondition': json.dumps(precondition)},
        headers={'Idempotency-Key': 'changed-return-original'})
    assert response.status_code == 409, 'Multipart precondition was silently ignored after input changed'
    assert db.scalar(select(func.count()).select_from(business.Batch)) == 0


def test_denied_raw_action_never_loads_project_notes_or_receipt_evidence(db, job):
    from sqlalchemy import event
    _, _, _, grant, client = job
    created = upload(client)
    assert created.status_code == 202, created.text
    batch_id = created.json()['batch_id']
    grant.actions = ['job_read']
    grant.version += 1
    db.commit()
    statements = []
    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().lower().startswith('select'):
            statements.append(statement.lower())
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        response = client.get(protected.BASE + '/' + batch_id + '/original')
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert response.status_code == 403, response.text
    # Identity-key predicates may read the registered head-id JSON path without
    # projecting its private evidence. Assert what is actually materialized.
    projections = [sql.split('from ', 1)[0] for sql in statements]
    forbidden = [sql for sql in projections if 'maintenance_project.project_note' in sql or
                 ('maintenance_rkd_return_line.source_payload' in sql)]
    assert not forbidden, '\n'.join(forbidden)
