"""Independent actual selection, historical receipt and parent-race checks."""
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.authorization import SysAuthorizationArtifact
from app.models.maintenance_project import (
    MaintenanceProject, MaintenanceProjectContract, MaintenanceProjectUserAssignment,
)
from app.models.maintenance_project_operations import MaintenanceCollectionReceipt
from app.models.system import SysImportBatch
from app.services import maintenance_batch_transfer_authorization as protected
from app.services import maintenance_bulk_import as business
from tests.test_authz_s3_batch_transfer import transfer, body, apply  # noqa:F401
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401
from tests.test_maintenance_bulk_import import _ordinary_sales_workbook


def _preview_files(transfer, paths):
    return transfer[0].post(protected.BASE+'/preview', headers=transfer[1], files=[
        ('files', (path.name, path.read_bytes(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))
        for path in paths])


def _two_existing_projects(db, transfer, tmp_path):
    paths = []
    for label in ('A', 'B'):
        paths.append(Path(_ordinary_sales_workbook(tmp_path,
            order_no=f'XSDD-INDEPENDENT-{label}', raw_order_id=f'INDEPENDENT-{label}',
            project_name=f'Independent selection {label}')))
    first = _preview_files(transfer, paths)
    assert first.status_code == 200, first.text
    created = apply(transfer, body(first))
    assert created.status_code == 200, created.text
    ids = {label: db.scalar(select(MaintenanceProject.project_id).where(
        MaintenanceProject.display_name == f'Independent selection {label}')) for label in ('A', 'B')}
    assert all(ids.values()) and ids['A'] != ids['B']
    for label in ('A', 'B'):
        _ordinary_sales_workbook(tmp_path, order_no=f'XSDD-INDEPENDENT-{label}',
            raw_order_id=f'INDEPENDENT-{label}', project_name=f'Independent selection {label}',
            order_amount='226', amount_ex_tax='200', tax_amount='26')
    response = _preview_files(transfer, paths)
    assert response.status_code == 200, response.text
    assert {r['matched_project_id'] for r in response.json()['rows'] if r['row_status']=='ready'} == set(ids.values())
    return response, ids


def test_selected_project_is_complete_contract_without_unselected_plan_leak(db, transfer, tmp_path):
    response, ids = _two_existing_projects(db, transfer, tmp_path)
    actor, grant = transfer[2], transfer[3]
    db.add(MaintenanceProjectUserAssignment(assignment_id=str(uuid4()), project_id=ids['A'],
        user_id=actor.id, responsibility_type='viewer', version=1,
        assigned_by='independent', assignment_reason='selected A only'))
    grant.scope='assigned_projects'; grant.actions=['apply']; grant.version += 1
    db.commit()
    payload = body(response)
    denied = apply(transfer, payload)
    assert denied.status_code == 403, denied.text
    assert db.get(SysImportBatch, response.json()['preview_id']).status == 'processing'
    payload['row_keys'] = [r['row_key'] for r in response.json()['rows']
        if r['matched_project_id']==ids['A'] and r['row_status']=='ready']
    allowed = apply(transfer, payload)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()['applied'] == 1
    amounts = dict(db.execute(select(MaintenanceProjectContract.project_id,
        MaintenanceProjectContract.amount_inc_tax).where(MaintenanceProjectContract.project_id.in_(ids.values()))).all())
    assert amounts == {ids['A']: Decimal('226'), ids['B']: Decimal('113')}
    replay = apply(transfer, payload)
    assert replay.status_code == 200, replay.text
    assert replay.json() == allowed.json()
    grant.actions=[]; grant.version += 1; db.commit()
    assert apply(transfer, payload).status_code == 403


def test_real_apply_receipt_publication_failure_preserves_preview_and_rolls_back_rows(db, transfer, tmp_path, monkeypatch):
    response, ids = _two_existing_projects(db, transfer, tmp_path)
    batch_id=response.json()['preview_id']
    archives = [(Path(item['storage_path']), Path(item['storage_path']).read_bytes())
        for item in db.get(SysImportBatch, batch_id).report_json['archives']]
    before_count=db.scalar(select(func.count()).select_from(SysAuthorizationArtifact))
    db.rollback()
    original=protected.artifacts.register_artifact_binding
    def publish_then_fail(*args, **kwargs):
        result=original(*args, **kwargs)
        if kwargs['target'].kind == protected.RECEIPT:
            assert db.get(SysAuthorizationArtifact,(protected.RECEIPT,str(batch_id))) is not None
            raise RuntimeError('actual transfer receipt published then failed')
        return result
    monkeypatch.setattr(protected.artifacts,'register_artifact_binding',publish_then_fail)
    with pytest.raises(RuntimeError, match='actual transfer receipt published then failed'):
        apply(transfer, body(response))
    db.expire_all()
    assert db.get(SysImportBatch,batch_id).status == 'processing'
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact)) == before_count
    amounts=list(db.scalars(select(MaintenanceProjectContract.amount_inc_tax).where(
        MaintenanceProjectContract.project_id.in_(ids.values()))))
    assert amounts == [Decimal('113'), Decimal('113')]
    for path, content in archives:
        assert path.read_bytes() == content


def test_ruling_contract_reparent_after_capture_invalidates_precondition(db, transfer, monkeypatch):
    client, headers, _, grant, _, _, ids=transfer
    contract_id=str(uuid4())
    contract=MaintenanceProjectContract(project_contract_id=contract_id, project_id=ids['manager'],
        contract_no=business.normalize_order_no('XSDD-INDEPENDENT-RULING'), contract_id='independent-ruling',
        amount_inc_tax=Decimal('1000'), version=1, status_mapping_state='mapped',
        status_mapping_version='test-v1', effective_from=date(2026,1,1), source='independent')
    db.add(contract); db.flush()
    receipt=MaintenanceCollectionReceipt(project_contract_id=contract_id,
        contract_no=contract.contract_no, receipt_no='INDEPENDENT-SK', receipt_date=date(2026,1,1),
        actual_amount=Decimal('100'), is_active=True, created_by='independent')
    db.add(receipt); grant.actions=['receipt_ruling']; grant.version+=1; db.commit()
    receipt_id=receipt.id
    payload={'contract_no':'XSDD-INDEPENDENT-RULING','receipt_no':'INDEPENDENT-SK',
        'receipt_date':'2026-01-01','actual_amount':'120','reason':'actual parent revision'}
    view=client.post(protected.BASE+'/receipt-rulings',headers=headers,json=payload,params={'view':'authorization'})
    assert view.status_code==200,view.text
    context=view.json()['authorization_context']
    payload['authorization_precondition']={key:context[key] for key in ('catalog_version','authorization_epoch','context')}
    original=protected.TransferAuthorization.capture
    def capture_then_reparent(permit, transaction):
        original(permit, transaction)
        if permit.entry_id==protected.RULING:
            with Session(db.get_bind()) as editor:
                editor.execute(update(MaintenanceProjectContract).where(
                    MaintenanceProjectContract.project_contract_id==contract_id).values(
                        project_id=ids['other'], version=2))
                editor.commit()
    monkeypatch.setattr(protected.TransferAuthorization,'capture',capture_then_reparent)
    response=client.post(protected.BASE+'/receipt-rulings',headers=headers,json=payload)
    assert response.status_code==409,response.text
    db.expire_all()
    assert db.get(MaintenanceCollectionReceipt,receipt_id).is_active
    assert db.get(MaintenanceCollectionReceipt,receipt_id).actual_amount==Decimal('100')
    assert db.scalar(select(func.count()).select_from(MaintenanceCollectionReceipt))==1
