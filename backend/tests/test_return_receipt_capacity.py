"""D-31 §1–4: public HTTP commands against real PostgreSQL."""
from uuid import uuid4

import pytest

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
from tests.test_maintenance_return_receipts_api import _client, _wbdd
from tests.test_site_issue_v2_api import _project


def _setup(db, *, qty=10):
    project = _project(db, project_id=str(uuid4()))
    order = _wbdd(db, project=project)
    part = DimPart(pn_std='CAP-PN')
    db.add(part)
    db.flush()
    db.add(FMaintenanceLine(part_id=part.id, raw_line_id=str(uuid4()), order_id=order.id,
                            import_batch_id=order.import_batch_id, qty=qty, is_active=True))
    db.commit()
    client = _client(db, username='capacity-user')
    return project, order, client


def _historical(db, project, order, qty, **extra):
    row = MaintenanceRkdReturnLine(
        rkd_line_id=str(uuid4()), project_id=project.project_id,
        source_order_id=order.raw_order_id if order else None,
        source='manual', head_no='历史合成', source_ref=f'manual:{uuid4()}',
        pn='OTHER-PN', qty=qty, line_status='active', version=1,
        created_by='synthetic', **extra,
    )
    db.add(row)
    db.commit()
    return row


def _base(project):
    return f'/api/maintenance/projects/stable/{project.project_id}'


def _create(client, project, order, qty):
    return client.post(f'{_base(project)}/return-receipts', json={
        'pn': 'NEW-PN', 'qty': qty, 'wbdd_no': order.order_no if order else None,
    })


def test_full_order_hidden_and_direct_create_rejected_without_business_write(db):
    project, order, client = _setup(db)
    _historical(db, project, order, 10)
    candidates = client.get(f'{_base(project)}/return-receipt-demands')
    assert candidates.status_code == 200
    assert candidates.json()['rows'] == []
    response = _create(client, project, order, 1)
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'return_capacity_exceeded'
    assert db.query(MaintenanceRkdReturnLine).count() == 1
    assert db.query(MaintenanceProjectOperationAudit).filter_by(entity_type='return_receipt').count() == 0


def _edit(client, row, **updates):
    return client.patch(f'/api/maintenance/return-receipts/{row.rkd_line_id}', json={
        'version': row.version, 'reason': '核对实收', **updates,
    })


def test_remaining_create_boundary_and_error_shape(db):
    project, order, client = _setup(db)
    _historical(db, project, order, 9)
    candidate = client.get(f'{_base(project)}/return-receipt-demands').json()['rows'][0]
    assert (candidate['demand_qty'], candidate['received_qty'], candidate['remaining_qty']) == (
        '10.000', '9.000', '1.000')
    rejected = _create(client, project, order, 2)
    assert rejected.status_code == 409, rejected.text
    detail = rejected.json()['detail']
    assert detail['details'] == dict(demand_order_id=order.raw_order_id, order_no=order.order_no,
                                     N='10.000', R='9.000', requested='2.000', remaining='1.000')
    assert '最多还能录入 1' in detail['message']
    assert _create(client, project, order, 1).status_code == 201
    assert client.get(f'{_base(project)}/return-receipt-demands').json()['total'] == 0


def test_historical_overage_retained_and_nonincreasing_edits_allowed(db):
    from app.services.maintenance_return_request_summary import summarize_demand_returns
    project, order, client = _setup(db)
    row = _historical(db, project, order, 12)
    summary = summarize_demand_returns(db, source_order_ids=[order.raw_order_id],
                                      allowed_project_ids=[project.project_id])[order.raw_order_id]
    assert (summary.demand_qty, summary.received_qty, summary.over_received_qty) == (10, 12, 2)
    db.commit()
    response = _create(client, project, order, 1)
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'return_capacity_exceeded'
    totals = client.get(f'{_base(project)}/return-receipt-summary').json()
    assert totals['project_total_qty'] == '12.000'
    assert totals['by_demand'][0]['qty'] == '12.000'
    records = client.get(f'{_base(project)}/return-receipts').json()['items']
    assert len(records) == 1
    assert records[0]['receipt_id'] == row.rkd_line_id
    assert records[0]['qty'] == '12.000' and records[0]['version'] == 1
    assert client.get(f'/api/maintenance/return-receipts/{row.rkd_line_id}/audit').json()['items'] == []
    assert _edit(client, row, qty=13).status_code == 409
    response = _edit(client, row, note='保留历史', condition='成品',
                     occurred_at='2026-10-19T02:00:00Z', serial_numbers=[f'SN-{i}' for i in range(12)])
    assert response.status_code == 200, response.text
    db.refresh(row)
    assert row.qty == 12 and row.version == 2
    response = _edit(client, row, qty=11, serial_numbers=[])
    assert response.status_code == 200, response.text
    db.refresh(row)
    response = client.post(f'/api/maintenance/return-receipts/{row.rkd_line_id}/void',
                           json={'version': row.version, 'reason': '核对后作废'})
    assert response.status_code == 200, response.text
    assert client.get(f'{_base(project)}/return-receipt-demands').json()['rows'][0]['remaining_qty'] == '10.000'


def test_edit_preserves_current_order_and_excludes_self(db):
    project, order, client = _setup(db)
    row = _historical(db, project, order, 6)
    _historical(db, project, order, 4)
    candidates = client.get(f'{_base(project)}/return-receipt-demands', params={
        'receipt_id': row.rkd_line_id, 'q': 'no-match',
    }).json()
    assert candidates['total'] == 1
    assert candidates['rows'][0]['is_current'] is True
    assert candidates['rows'][0]['remaining_qty'] == '0.000'
    assert candidates['rows'][0]['max_qty'] == '6.000'
    assert _edit(client, row, qty=7).status_code == 409
    assert _edit(client, row, qty=6, wbdd_no=order.order_no).status_code == 200
    db.refresh(row)
    assert _edit(client, row, qty=5).status_code == 200


def test_linked_increase_to_capacity_after_rejection_keeps_original_version(db):
    project, order, client = _setup(db)
    created = _create(client, project, order, 9)
    assert created.status_code == 201, created.text
    receipt = created.json()
    path = f"/api/maintenance/return-receipts/{receipt['receipt_id']}"
    command = {'version': receipt['version'], 'reason': '核对实收后补足'}

    rejected = client.patch(path, json={**command, 'qty': 11})
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()['detail']['code'] == 'return_capacity_exceeded'
    records = client.get(f'{_base(project)}/return-receipts').json()['items']
    assert len(records) == 1
    assert records[0]['qty'] == '9.000'
    assert records[0]['version'] == receipt['version']
    assert records[0]['source_order_id'] == order.raw_order_id
    assert [item['action'] for item in client.get(f'{path}/audit').json()['items']] == ['create']

    accepted = client.patch(path, json={**command, 'qty': 10})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['qty'] == '10.000'
    assert accepted.json()['version'] == receipt['version'] + 1
    assert accepted.json()['source_order_id'] == order.raw_order_id
    totals = client.get(f'{_base(project)}/return-receipt-summary').json()
    assert totals['project_total_qty'] == '10.000'
    assert totals['by_demand'][0]['qty'] == '10.000'
    assert client.get(f'{_base(project)}/return-receipt-demands').json()['total'] == 0
    audits = client.get(f'{path}/audit').json()['items']
    assert [item['action'] for item in audits] == ['create', 'update']
    assert audits[1]['before_json']['qty'] == '9.000'
    assert audits[1]['after_json']['qty'] == '10.000'


def test_change_link_checks_target_and_rolls_back_both_sides(db):
    project, order, client = _setup(db)
    target = _wbdd(db, project=project, order_no='WBDD-TARGET', demand_qty=10)
    row = _historical(db, project, order, 3)
    _historical(db, project, target, 9)
    response = _edit(client, row, wbdd_no=target.order_no, qty=2, note='不得半写')
    assert response.status_code == 409, response.text
    db.refresh(row)
    assert row.source_order_id == order.raw_order_id and row.qty == 3 and row.version == 1
    assert row.note is None
    totals = client.get(f'{_base(project)}/return-receipt-summary').json()['by_demand']
    assert {item['source_order_id']: item['qty'] for item in totals} == {
        order.raw_order_id: '3.000', target.raw_order_id: '9.000',
    }
    assert db.query(MaintenanceProjectOperationAudit).filter_by(entity_type='return_receipt').count() == 0
    assert _edit(client, row, wbdd_no=target.order_no, qty=1).status_code == 200


def test_unlinked_independent_but_link_to_full_rejected(db):
    project, order, client = _setup(db)
    _historical(db, project, order, 10)
    created = _create(client, project, None, 50)
    assert created.status_code == 201
    row = db.get(MaintenanceRkdReturnLine, created.json()['receipt_id'])
    assert _edit(client, row, wbdd_no=order.order_no).status_code == 409
    db.refresh(row)
    assert row.source_order_id is None and row.qty == 50
    assert _edit(client, row, qty=60).status_code == 200


def test_formula_ignores_void_lines_and_receipts_null_is_zero_and_all_sources_count(db):
    project, order, client = _setup(db, qty=4)
    part_id = db.query(FMaintenanceLine).filter_by(order_id=order.id).one().part_id
    for qty, active in [(6, True), (None, True), (99, False)]:
        db.add(FMaintenanceLine(raw_line_id=str(uuid4()), order_id=order.id, part_id=part_id,
                                import_batch_id=order.import_batch_id, qty=qty, is_active=active,
                                return_qty=5))
    db.commit()
    _historical(db, project, order, 5, test_result='坏品')
    from tests.test_maintenance_return_metrics import _receipt
    part = db.get(DimPart, part_id)
    _receipt(db, project, part, '4', source='rkd_import', condition='成品',
             source_order_id=order.raw_order_id)
    _receipt(db, project, part, '100', status='voided', source_order_id=order.raw_order_id)
    db.commit()
    candidate = client.get(f'{_base(project)}/return-receipt-demands').json()['rows'][0]
    assert (candidate['demand_qty'], candidate['received_qty'], candidate['remaining_qty']) == ('10.000','9.000','1.000')
    assert _create(client, project, order, 2).status_code == 409
    assert _create(client, project, order, 1).status_code == 201


@pytest.mark.parametrize('qty', [None, 0, -1, 'NaN'])
def test_unknown_base_denies_increase_but_allows_same_owner_decrease_and_note(db, qty):
    project, order, client = _setup(db, qty=qty)
    row = _historical(db, project, order, 3)
    result = _create(client, project, order, 1)
    assert result.status_code == 409, result.text
    assert result.json()['detail']['code'] == 'return_capacity_unknown'
    assert _edit(client, row, note='待核对').status_code == 200
    db.refresh(row)
    assert _edit(client, row, qty=2).status_code == 200
    assert client.get(f'{_base(project)}/return-receipt-demands').json()['total'] == 0


def test_ownership_anomaly_blocks_new_occupancy_without_leaking_foreign_qty(db):
    project, order, client = _setup(db)
    other = _project(db, project_id=str(uuid4()))
    _historical(db, other, order, 1)
    response = _create(client, project, order, 1)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'return_capacity_unknown'
    assert response.json()['detail']['details']['R'] is None
    assert client.get(f'{_base(project)}/return-receipt-demands').json()['total'] == 0


def test_full_capacity_idempotent_replay_remains_successful(db):
    project, order, client = _setup(db, qty=1)
    payload = {'pn':'PN', 'qty':1, 'wbdd_no':order.order_no, 'idempotency_key':'retry-full'}
    first = client.post(f'{_base(project)}/return-receipts', json=payload)
    assert first.status_code == 201
    second = client.post(f'{_base(project)}/return-receipts', json=payload)
    assert second.status_code == 201 and second.json()['replayed'] is True
    assert second.json()['receipt_id'] == first.json()['receipt_id']
    assert db.query(MaintenanceRkdReturnLine).count() == 1


def test_candidate_filters_before_pagination_and_keeps_site_issue_query(db):
    from app.api.maintenance_project_operations import stable_site_issue_router
    project, order, client = _setup(db)
    _historical(db, project, order, 10)
    available = _wbdd(db, project=project, order_no='Z-AVAILABLE', demand_qty=2)
    response = client.get(f'{_base(project)}/return-receipt-demands', params={'page_size':1})
    assert response.json()['total'] == 1
    assert response.json()['rows'][0]['source_order_id'] == available.raw_order_id
    client.app.include_router(stable_site_issue_router, prefix='/api')
    response = client.get(f'/api/maintenance/site-issues/projects/{project.project_id}/demand-candidates')
    assert response.status_code == 200, response.text
    assert order.raw_order_id in {item['source_order_id'] for item in response.json()['rows']}


def test_edit_context_cannot_use_another_projects_receipt(db):
    project, order, client = _setup(db)
    other = _project(db, project_id=str(uuid4()))
    row = _historical(db, other, None, 20)
    response = client.get(f'{_base(project)}/return-receipt-demands', params={'receipt_id':row.rkd_line_id})
    assert response.status_code == 404


def test_two_real_connections_compete_for_last_unit(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import text
    from app.db import SessionLocal
    from app.services import maintenance_return_receipts as receipts
    project, order, client = _setup(db)
    _historical(db, project, order, 9)
    project_id, order_no = project.project_id, order.order_no
    db.commit()
    barrier = Barrier(2)
    def submit():
        with SessionLocal() as session:
            pid = session.scalar(text('SELECT pg_backend_pid()'))
            session.execute(text("SET LOCAL lock_timeout='10s'"))
            barrier.wait(timeout=10)
            try:
                receipts.register_receipt(session, project_id=project_id, wbdd_no=order_no,
                                          pn='CONCURRENT', qty=1, operated_by='synthetic')
                session.commit()
                return pid, 'ok'
            except receipts.ReturnReceiptCapacityConflict as exc:
                session.rollback()
                return pid, exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        results = [f.result(timeout=20) for f in futures]
    assert len({pid for pid, _ in results}) == 2
    assert sorted(outcome for _, outcome in results) == ['ok', 'return_capacity_exceeded']
    assert client.get(f'{_base(project)}/return-receipt-summary').json()['project_total_qty'] == '10.000'
    assert db.query(MaintenanceProjectOperationAudit).filter_by(entity_type='return_receipt').count() == 1


def test_edit_current_order_is_first_page_even_after_assignment_disappears(db):
    from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
    project, order, client = _setup(db)
    row = _historical(db, project, order, 5)
    _wbdd(db, project=project, order_no='AAA-FIRST', demand_qty=10)
    assignment = db.query(MaintenanceSourceOrderAssignment).filter_by(
        source_order_id=order.raw_order_id, is_active=True).one()
    from datetime import UTC, datetime
    assignment.is_active = False
    assignment.version += 1
    assignment.archived_by = 'synthetic'
    assignment.archived_at = datetime.now(UTC)
    db.commit()
    response = client.get(f'{_base(project)}/return-receipt-demands', params={
        'receipt_id': row.rkd_line_id, 'page_size': 1,
    })
    assert response.status_code == 200, response.text
    current = response.json()['rows'][0]
    assert current['source_order_id'] == order.raw_order_id and current['is_current']
    assert current['received_qty'] is None
    assert current['max_qty'] == '5.000'
    assert _edit(client, row, qty=4, wbdd_no=order.order_no).status_code == 200
    db.refresh(row)
    assert _edit(client, row, qty=6).status_code == 409


def test_nonfinite_historical_receipt_is_unknown_and_never_turns_into_capacity(db):
    from app.services.maintenance_return_request_summary import summarize_demand_returns
    project, order, client = _setup(db)
    row = _historical(db, project, order, 'NaN')
    summaries = summarize_demand_returns(db, source_order_ids=[order.raw_order_id],
                                         allowed_project_ids=[project.project_id])
    summary = summaries[order.raw_order_id]
    assert summary.basis_state == 'incomplete' and summary.received_qty is None
    assert 'invalid_receipt_qty' in summary.reason_codes
    db.commit()
    response = _create(client, project, order, 1)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'return_capacity_unknown'
    assert client.get(f'{_base(project)}/return-receipt-demands').json()['total'] == 0
    current = client.get(f'{_base(project)}/return-receipt-demands', params={'receipt_id':row.rkd_line_id})
    assert current.status_code == 200 and current.json()['rows'][0]['max_qty'] is None
    assert _edit(client, row, note='历史非法数量待核对').status_code == 200
    db.refresh(row)
    response = client.post(f'/api/maintenance/return-receipts/{row.rkd_line_id}/void',
                           json={'version':row.version, 'reason':'核实后作废'})
    assert response.status_code == 200


def test_invalid_existing_link_allows_note_but_blocks_increase_and_relink(db):
    project, order, client = _setup(db)
    row = _historical(db, project, order, 3)
    order.data_status = '已取消'
    db.commit()
    # D-31 §3: keep the historical identity; a remark adds no occupancy.
    response = _edit(client, row, note='原单失效，保留实收凭据')
    assert response.status_code == 200, response.text
    db.refresh(row)
    previous_version = row.version
    response = _edit(client, row, qty=4)
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'return_capacity_unknown'
    unlinked = _historical(db, project, None, 1)
    response = _edit(client, unlinked, wbdd_no=order.order_no)
    assert response.status_code == 422, response.text
    assert '已失效' in response.json()['detail']
    db.refresh(row)
    db.refresh(unlinked)
    assert row.source_order_id == order.raw_order_id and row.qty == 3
    assert row.version == previous_version and unlinked.source_order_id is None
