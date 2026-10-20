"""SQL selector vs the frozen pre-pushdown implementation on real PostgreSQL."""
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_demands
from app.services import maintenance_return_receipts as ledger
from tests.return_receipt_candidates_reference import legacy_memory_candidates
from tests.test_return_receipt_capacity import _base, _historical, _setup
from tests.test_site_issue_v2_api import _project


def test_sql_pages_match_frozen_memory_selector_across_semantics(db):
    project, initial, client = _setup(db)
    foreign = _project(db, project_id=str(uuid4()))
    parts = [DimPart(pn_std='FILTER-MASTER'), DimPart(pn_std='OTHER-MASTER')]
    db.add_all(parts)
    db.flush()
    # Cross the former 500/501 boundary; keep repeated display numbers.
    orders = [FMaintenanceOrder(
        raw_order_id=f'sql-candidate-{i:04}', order_no=f'SQL-{i // 2:04}',
        data_status='已生效', import_batch_id=initial.import_batch_id,
        order_date=date(2026, 10, 1) if i % 2 else None,
    ) for i in range(516)]
    db.add_all(orders)
    db.flush()
    for i, order in enumerate(orders):
        db.add(MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid4()), source_order_id=order.raw_order_id,
            project_id=project.project_id, is_active=True, created_by='synthetic',
        ))
        db.add(FMaintenanceLine(
            raw_line_id=str(uuid4()), order_id=order.id, part_id=parts[0].id,
            import_batch_id=order.import_batch_id, qty=10, is_active=True,
            pn_raw='FILTER-RAW', pn_std='FILTER-STD',
        ))
    db.commit()
    # Helpers commit only to this process-owned database, never another port.
    current = _historical(db, project, initial, 2)
    _historical(db, project, initial, 3)
    for i, order in enumerate(orders):
        if i >= 25:
            # Available rows straddle the historical aggregation batch boundary.
            db.add(MaintenanceRkdReturnLine(
                rkd_line_id=str(uuid4()), project_id=project.project_id,
                source_order_id=order.raw_order_id, source='manual', head_no='合成',
                source_ref=f'manual:{uuid4()}', pn='ANY',
                qty=9 if i in (498, 499, 500, 501, 515) else 10,
                line_status='active', created_by='synthetic',
            ))
    db.commit()
    lines = {line.order_id: line for line in db.scalars(select(FMaintenanceLine))}
    # All branches of _summary and selector validity, on this same dataset.
    lines[orders[0].id].is_active = False                 # no active lines
    lines[orders[1].id].qty = -1                         # negative, even if sum > 0
    db.add(FMaintenanceLine(raw_line_id=str(uuid4()), order_id=orders[1].id,
                            part_id=parts[1].id, import_batch_id=initial.import_batch_id,
                            qty=20, is_active=True))
    lines[orders[2].id].qty = None                       # all NULL -> zero
    lines[orders[3].id].qty = 0
    lines[orders[4].id].qty = Decimal('NaN')
    db.commit()
    invalid_self = _historical(db, project, orders[5], 'NaN')
    finite_with_nan_other = _historical(db, project, orders[5], 2)
    conflict = _historical(db, project, orders[6], 2)
    _historical(db, foreign, orders[6], 1)
    for index, status in [(7, '已取消'), (8, '草稿'), (9, '进行中'), (10, None)]:
        orders[index].data_status = status
    # NULL+positive demand; inactive lines and void receipts have no effect.
    lines[orders[11].id].qty = None
    db.add_all([FMaintenanceLine(
        raw_line_id=str(uuid4()), order_id=orders[11].id, part_id=parts[1].id,
        import_batch_id=initial.import_batch_id, qty=qty, is_active=active,
        pn_raw='CROSS-LINE' if active else 'INACTIVE-ONLY',
    ) for qty, active in [(4, True), (100, False)]])
    db.commit()
    _historical(db, project, orders[11], '1.125')          # undated receipt
    ignored = _historical(db, foreign, orders[11], 100)   # void foreign != conflict
    ignored.line_status = 'voided'
    ignored.voided_by = 'synthetic'
    ignored.voided_at = datetime.now(UTC)
    ignored.void_reason = '合成已作废返件'
    tombstone_receipt = _historical(db, project, orders[12], 2)
    maintenance_demands.void_fast(db, source_order_ids=[orders[12].raw_order_id],
                                 reason='合成 SQL 等价测试', operated_by='synthetic',
                                 idempotency_key='candidate-tombstone')
    db.commit()
    # Retain active ownership under a tombstone too: both gates must be applied.
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid4()), source_order_id=orders[12].raw_order_id,
        project_id=project.project_id, is_active=True, created_by='synthetic',
    ))
    # Undo soft-disabled lines only; tombstone continues to make the order invalid.
    lines[orders[12].id].is_active = True
    missing_assignment = _historical(db, project, orders[13], 2)
    reassigned = _historical(db, project, orders[14], 2)
    assignments = {a.source_order_id: a for a in db.scalars(select(MaintenanceSourceOrderAssignment))}
    for i in (13, 14):
        a = assignments[orders[i].raw_order_id]
        a.is_active = False
        a.version += 1
        a.archived_by = 'synthetic'
        a.archived_at = datetime.now(UTC)
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid4()), source_order_id=orders[14].raw_order_id,
        project_id=foreign.project_id, is_active=True, created_by='synthetic',
    ))
    db.commit()
    full_self = _historical(db, project, orders[15], 10)
    over_self = _historical(db, project, orders[16], 12)
    _historical(db, project, orders[17], 9, source_payload={'category': 'anything'}, test_result='成品')
    unlinked = _historical(db, project, None, 500)         # never occupies an order
    cancelled_self = _historical(db, project, orders[7], 2)
    zero_self = _historical(db, project, orders[3], 2)
    incomplete_currents = [_historical(db, project, orders[i], 2) for i in (0, 1, 2, 4)]

    def compare(*, receipt=None, q=None, part_id=None, sizes=(2,)):
        options = dict(project_id=project.project_id, capacity_only=True,
                       receipt_id=receipt.rkd_line_id if receipt else None, q=q, part_id=part_id)
        expected = legacy_memory_candidates(db, page=1, page_size=10000, **options)
        db.commit()
        for size in sizes:
            # Includes first, full final, partial final, and past-end pages.
            for page in range(1, (expected['total'] + size - 1) // size + 2):
                response = client.get(f'{_base(project)}/return-receipt-demands', params={
                    key: value for key, value in dict(page=page, page_size=size, q=q,
                        part_id=part_id, receipt_id=options['receipt_id']).items() if value is not None
                })
                assert response.status_code == 200, response.text
                assert response.json() == dict(
                    rows=expected['rows'][(page-1)*size:page*size], total=expected['total'],
                    page=page, page_size=size,
                ), (options, page, size)
        return expected

    expected = compare(sizes=(1, 2, 7, 200))
    rows = {r['source_order_id']: r for r in expected['rows']}
    assert all(orders[i].raw_order_id not in rows for i in range(11))
    assert rows[orders[11].raw_order_id]['demand_qty'] == '4.000'
    assert rows[orders[11].raw_order_id]['received_qty'] == '1.125'
    assert rows[orders[11].raw_order_id]['remaining_qty'] == '2.875'
    for q in (' SQL-0250 ', 'FILTER-RAW', 'FILTER-STD', 'FILTER-MASTER', '%', 'INACTIVE-ONLY'):
        compare(q=q)
    cross = compare(q='CROSS-LINE', part_id=parts[0].id)
    assert [r['source_order_id'] for r in cross['rows']] == [orders[11].raw_order_id]
    compare(part_id=parts[1].id)
    compare(part_id=2147483647)
    for receipt in (current, invalid_self, finite_with_nan_other, conflict, tombstone_receipt,
                    missing_assignment, reassigned, full_self, over_self, cancelled_self, zero_self, unlinked, *incomplete_currents):
        compare(receipt=receipt)
        compare(receipt=receipt, q='no-match', part_id=2147483647, sizes=(1,))
    project.is_active = False
    db.commit()
    assert compare()['total'] == 0
    compare(receipt=current)
    empty = _project(db, project_id=str(uuid4()))
    response = client.get(f'{_base(empty)}/return-receipt-demands')
    assert response.status_code == 200 and response.json()['rows'] == [] and response.json()['total'] == 0


@pytest.mark.parametrize('size', [0, 201])
def test_candidate_page_size_bounds(db, size):
    project, order, client = _setup(db)
    assert client.get(f'{_base(project)}/return-receipt-demands', params={'page_size':size}).status_code == 422


def test_committed_occupancy_changes_candidates_on_other_connection(db):
    project, order, client = _setup(db)
    _historical(db, project, order, 9)
    project_id, order_id, order_no = project.project_id, order.raw_order_id, order.order_no
    db.commit()
    with SessionLocal() as reader, SessionLocal() as writer:
        assert reader.scalar(text('SELECT pg_backend_pid()')) != writer.scalar(text('SELECT pg_backend_pid()'))
        args = dict(project_id=project_id, page=1, page_size=1, capacity_only=True)
        assert ledger.demand_candidates(reader, **args)['total'] == 1
        ledger.register_receipt(writer, project_id=project_id, wbdd_no=order_no,
                                pn='OTHER-CONNECTION', qty=1, operated_by='synthetic')
        # An uncommitted occupancy does not leak into the independent reader.
        assert ledger.demand_candidates(reader, **args)['total'] == 1
        writer.commit()
        result = ledger.demand_candidates(reader, **args)
        assert result['rows'] == [] and result['total'] == 0
        with pytest.raises(ledger.ReturnReceiptCapacityConflict) as caught:
            ledger.lock_receipt_demands(reader, {order_id})
            ledger.check_receipt_capacity(reader, project_id=project_id,
                                          source_order_id=order_id, requested=Decimal(1))
        assert caught.value.code == 'return_capacity_exceeded'
        assert caught.value.details['R'] == '10.000' and caught.value.details['remaining'] == '0.000'
