"""项目需求单 API 按页消费共享返还汇总，不复制核对公式。"""

import logging
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProjectUserAssignment
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysUser
from app.services import maintenance_boss_board
from app.services.maintenance_return_request_summary import ReturnSummaryError
from tests.boss_board_helpers import boss_client, client_for, make_project
from tests.test_maintenance_boss_board_perf import count_sql
from tests.test_maintenance_return_metrics import _receipt
from tests.test_maintenance_return_receipts_api import _wbdd


def _demand(db, project, part, index, quantity):
    order = _wbdd(db, project=project, order_no=f"WBDD-20261015-{index:04d}")
    db.add(FMaintenanceLine(
        raw_line_id=f"board-return-{uuid4()}", order_id=order.id,
        part_id=part.id, qty=Decimal(quantity) if quantity is not None else None,
        is_active=True, import_batch_id=order.import_batch_id,
    ))
    db.commit()
    return order


def test_project_orders_degrades_summary_error_without_losing_rows(
    db, monkeypatch, caplog,
):
    project = make_project(db, "项目需求单摘要降级")
    part = DimPart(pn_std="BOARD-RETURN-FALLBACK-RAM", category_major="内存")
    db.add(part)
    db.commit()
    _demand(db, project, part, 1, "5")
    _demand(db, project, part, 2, "2")
    client = boss_client(db)
    url = f"/api/maintenance/boss-board/projects/{project.project_id}/orders"
    normal = client.get(url)
    assert normal.status_code == 200, normal.text

    def unavailable_summary(*args, **kwargs):
        raise ReturnSummaryError("summary_unavailable")

    monkeypatch.setattr(
        maintenance_boss_board, "summarize_demand_returns", unavailable_summary,
    )
    # 测试迁移的 logging.fileConfig 会关闭已导入的应用 logger。
    monkeypatch.setattr(maintenance_boss_board.logger, "disabled", False)
    with caplog.at_level(logging.ERROR, logger=maintenance_boss_board.__name__):
        response = client.get(url)

    assert response.status_code == 200, response.text
    body = response.json()
    expected = normal.json()
    assert body["total"] == 2
    assert len(body["rows"]) == 2
    for actual_row, expected_row in zip(body["rows"], expected["rows"], strict=True):
        assert actual_row.pop("return_summary") is None
        assert actual_row.pop("return_summary_error") == "summary_unavailable"
        expected_row.pop("return_summary")
        assert actual_row == expected_row
    assert {key: value for key, value in body.items() if key != "rows"} == {
        key: value for key, value in expected.items() if key != "rows"
    }
    records = [record for record in caplog.records
               if record.name == maintenance_boss_board.__name__]
    assert len(records) == 1
    assert records[0].exc_info[0] is ReturnSummaryError
    assert "return summary unavailable" in records[0].getMessage()


def test_project_orders_exposes_serialized_return_summary_on_each_page(db):
    project = make_project(db, "项目需求单返还核对")
    other_project = make_project(db, "其他项目")
    part = DimPart(pn_std="BOARD-RETURN-RAM", category_major="内存")
    db.add(part)
    db.commit()
    partial = _demand(db, project, part, 1, "5")
    returned = _demand(db, project, part, 2, "2")
    incomplete = _demand(db, project, part, 3, None)
    _demand(db, other_project, part, 4, "99")
    _receipt(db, project, part, "2", source_order_id=partial.raw_order_id)
    _receipt(db, project, part, "3", source_order_id=returned.raw_order_id)
    db.commit()
    client = boss_client(db)
    url = f"/api/maintenance/boss-board/projects/{project.project_id}/orders"

    expected = [
        (partial, "5.000", "2.000", "3.000", "complete"),
        (returned, "2.000", "3.000", "0.000", "complete"),
        (incomplete, "0.000", "0.000", None, "incomplete"),
    ]
    for page, (order, demand, received, pending, basis) in enumerate(expected, 1):
        response = client.get(url, params={"page": page, "page_size": 1})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 3
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert row["source_order_id"] == order.raw_order_id
        summary = row["return_summary"]
        assert summary["source_order_id"] == order.raw_order_id
        assert summary["project_id"] == project.project_id
        assert summary["demand_qty"] == demand
        assert summary["received_qty"] == received
        assert summary["pending_qty"] == pending
        assert "return_status" not in summary
        assert summary["basis_state"] == basis
    assert summary["reason_codes"] == ["zero_demand_qty"]
    empty = client.get(url, params={"page": 4, "page_size": 1}).json()
    assert empty["rows"] == [] and empty["total"] == 3


def test_project_orders_summary_stays_bounded_to_page_with_over_500_orders(db):
    project = make_project(db, "超过单批上限的项目")
    part = DimPart(pn_std="BOARD-RETURN-BATCH-RAM", category_major="内存")
    db.add(part)
    db.commit()
    seed = _demand(db, project, part, 0, "1")
    orders = [FMaintenanceOrder(
        raw_order_id=f"board-batch-{uuid4()}",
        order_no=f"WBDD-20261015-{index:04d}", data_status="已生效",
        import_batch_id=seed.import_batch_id,
    ) for index in range(1, 501)]
    db.add_all(orders)
    db.flush()
    db.add_all([FMaintenanceLine(
        raw_line_id=f"board-batch-line-{uuid4()}", order_id=order.id,
        part_id=part.id, qty=Decimal("1"), is_active=True,
        import_batch_id=seed.import_batch_id,
    ) for order in orders])
    db.add_all([MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid4()), source_order_id=order.raw_order_id,
        project_id=project.project_id, is_active=True, created_by="board-test",
    ) for order in orders])
    db.commit()
    client = boss_client(db)
    url = f"/api/maintenance/boss-board/projects/{project.project_id}/orders"

    with count_sql() as small:
        small_response = client.get(url, params={"page_size": 1})
    assert small_response.status_code == 200, small_response.text
    with count_sql() as large:
        first_response = client.get(url, params={"page_size": 200})
    assert first_response.status_code == 200, first_response.text
    assert large.total == small.total  # 页内单数增加不增加数据库往返次数。
    bodies = [first_response.json()]
    for page in (2, 3):
        response = client.get(url, params={"page": page, "page_size": 200})
        assert response.status_code == 200, response.text
        bodies.append(response.json())
    assert [len(body["rows"]) for body in bodies] == [200, 200, 101]
    assert all(body["total"] == 501 for body in bodies)
    returned_ids = set()
    for body in bodies:
        for row in body["rows"]:
            returned_ids.add(row["source_order_id"])
            summary = row["return_summary"]
            assert summary["source_order_id"] == row["source_order_id"]
            assert summary["demand_qty"] == "1.000"
            assert summary["received_qty"] == "0.000"
            assert summary["pending_qty"] == "1.000"
            assert "return_status" not in summary
    assert len(returned_ids) == 501
    assert client.get(url, params={"page_size": 501}).status_code == 422


def test_project_orders_summary_preserves_scope_and_unassigned_unknown_state(db):
    mine = make_project(db, "范围内项目")
    theirs = make_project(db, "范围外项目")
    part = DimPart(pn_std="BOARD-SUMMARY-SCOPE-RAM", category_major="内存")
    db.add(part)
    db.commit()
    own_order = _demand(db, mine, part, 1, "4")
    _demand(db, theirs, part, 2, "99")
    unassigned = FMaintenanceOrder(
        raw_order_id=f"board-unassigned-{uuid4()}", order_no="WBDD-20261015-0003",
        data_status="已生效", import_batch_id=own_order.import_batch_id,
    )
    db.add(unassigned)
    db.flush()
    db.add(FMaintenanceLine(
        raw_line_id=f"board-unassigned-line-{uuid4()}", order_id=unassigned.id,
        part_id=part.id, qty=Decimal("7"), is_active=True,
        import_batch_id=own_order.import_batch_id,
    ))
    db.commit()
    scoped = client_for(db, username="board-summary-scoped", overrides={
        "page_maintenance": True, "own_maintenance_projects_only": True,
    })
    user = db.scalar(select(SysUser).where(SysUser.username == "board-summary-scoped"))
    db.add(MaintenanceProjectUserAssignment(
        assignment_id=str(uuid4()), project_id=mine.project_id,
        responsibility_type="primary_manager", user_id=user.id,
        source_manager_text=user.username, assigned_by="board-test",
        assignment_reason="合成范围夹具", version=1,
    ))
    db.commit()
    base = "/api/maintenance/boss-board/projects"
    response = scoped.get(f"{base}/{mine.project_id}/orders")
    assert response.status_code == 200, response.text
    own = response.json()["rows"][0]
    assert own["source_order_id"] == own_order.raw_order_id
    assert own["return_summary"]["pending_qty"] == "4.000"
    denied = [scoped.get(f"{base}/{project_id}/orders")
              for project_id in (theirs.project_id, "unassigned", "nonexistent")]
    assert all(response.status_code == 404 for response in denied)
    assert denied[0].json() == denied[1].json() == denied[2].json()

    global_response = boss_client(db).get(f"{base}/unassigned/orders")
    assert global_response.status_code == 200, global_response.text
    summary = global_response.json()["rows"][0]["return_summary"]
    assert summary["source_order_id"] == unassigned.raw_order_id
    assert summary["basis_state"] == "inactive"
    assert summary["pending_qty"] is None
    assert "return_status" not in summary
    assert "unassigned_order" in summary["reason_codes"]
