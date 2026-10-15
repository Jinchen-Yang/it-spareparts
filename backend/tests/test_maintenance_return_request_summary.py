"""方案 A 数量摘要的公开只读 Interface 契约。"""

from datetime import date
from decimal import Decimal
from threading import Event, Thread
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.db import engine

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine
from app.models.maintenance import FMaintenanceOrder
from app.models.master_data import ProductCategory
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_return_request_summary as summaries
from app.services.maintenance_return_receipts import register_receipt
from tests.test_maintenance_return_receipts_api import _wbdd
from tests.test_site_issue_v2_api import _project
from tests.test_maintenance_return_metrics import _issue, _receipt
from tests.test_maintenance_demand_safe_delete import _admin_client


def _demand(db, project, part, index, quantities, *, status="已生效"):
    order = _wbdd(db, project=project, order_no=f"WBDD-20261001-{index:04d}")
    order.data_status = status
    for line_no, quantity in enumerate(quantities, 1):
        db.add(FMaintenanceLine(
            raw_line_id=f"summary-{uuid4()}", order_id=order.id, part_id=part.id,
            line_no=line_no, qty=Decimal(quantity) if quantity is not None else None,
            is_active=True, import_batch_id=order.import_batch_id,
        ))
    db.commit()
    return order


@pytest.mark.parametrize("exemption_source", ["line_no_return", "project_default_no_return", "category_disk"])
def test_exempt_disks_remain_in_original_demand_and_receipts_without_status(db, exemption_source):
    project = _project(db, project_id=str(uuid4()))
    project.no_return_default = exemption_source == "project_default_no_return"
    category = ProductCategory(category_major="硬盘", category_minor="SSD")
    memory_category = ProductCategory(category_major="内存", category_minor="DDR4")
    db.add_all([category, memory_category])
    db.flush()
    disk = DimPart(pn_std="EXEMPT-DISK", category_id=category.id)
    other = DimPart(pn_std="NON-DISK", category_id=memory_category.id)
    db.add_all([disk, other])
    db.commit()
    order = _demand(db, project, disk, 1, ["5"])
    if exemption_source == "line_no_return":
        _issue(db, project, disk, "5", no_return=True, source_order_id=order.raw_order_id)
    db.add(FMaintenanceLine(
        raw_line_id=str(uuid4()), order_id=order.id, part_id=other.id,
        qty=Decimal("4"), is_active=True, import_batch_id=order.import_batch_id,
    ))
    _receipt(db, project, disk, "3", source_order_id=order.raw_order_id)
    _receipt(db, project, disk, "2", source_order_id=order.raw_order_id, source="rkd_import")
    _receipt(db, project, other, "1", source_order_id=order.raw_order_id)
    db.commit()
    result = summaries.summarize_demand_returns(
        db, source_order_ids=[order.raw_order_id], allowed_project_ids={project.project_id},
    )[order.raw_order_id].to_dict()
    assert result["demand_qty"] == "9.000"
    assert result["received_qty"] == "6.000"
    assert result["pending_qty"] == result["remaining_qty"] == "3.000"
    assert result["basis_state"] == "complete"
    assert result["reason_codes"] == []
    assert "return_status" not in result
    assert not any(key.startswith("excluded_exempt_disk_") for key in result)


@pytest.mark.parametrize("reference", ["demand_line", "manual", "warehouse_line", "warehouse_with_inherited_line"])
def test_explicit_required_disk_overrides_defaults_for_demand_and_receipts(db, reference):
    project = _project(db, project_id=str(uuid4()))
    project.no_return_default = True
    category = ProductCategory(category_major="硬盘", category_minor="HDD")
    db.add(category)
    db.flush()
    disk = DimPart(pn_std="REQUIRED-DISK", category_id=category.id)
    db.add(disk)
    db.commit()
    order = _demand(db, project, disk, 1, ["5"])
    line = _issue(db, project, disk, "5", no_return=False,
                  source_order_id=None if reference == "manual" else order.raw_order_id)
    if reference == "manual":
        line.demand_order_no = order.order_no
    elif reference.startswith("warehouse"):
        line.source_line_id = "warehouse-shipment-line-1"
        if reference == "warehouse_with_inherited_line":
            inherited = _issue(db, project, disk, "5", source_order_id=order.raw_order_id)
            inherited.source_line_id = db.scalar(select(FMaintenanceLine.raw_line_id).where(
                FMaintenanceLine.order_id == order.id,
            ))
    else:
        line.source_line_id = db.scalar(select(FMaintenanceLine.raw_line_id).where(
            FMaintenanceLine.order_id == order.id,
        ))
    _receipt(db, project, disk, "2", source_order_id=order.raw_order_id)
    db.commit()
    result = summaries.summarize_demand_returns(
        db, source_order_ids=[order.raw_order_id], allowed_project_ids={project.project_id},
    )[order.raw_order_id].to_dict()
    assert result["demand_qty"] == "5.000"
    assert result["received_qty"] == "2.000"
    assert result["pending_qty"] == "3.000"
    assert result["basis_state"] == "complete"
    assert "return_status" not in result


def test_only_exempt_disks_keep_quantities_and_hide_cross_project_receipts(db):
    project = _project(db, project_id=str(uuid4()))
    old_project = _project(db, project_id=str(uuid4()))
    category = ProductCategory(category_major="硬盘", category_minor="SSD")
    db.add(category)
    db.flush()
    disk = DimPart(pn_std="ALL-EXEMPT", category_id=category.id)
    db.add(disk)
    db.commit()
    order = _demand(db, project, disk, 1, ["5"])
    _receipt(db, project, disk, "2", occurred_at=None, source_order_id=order.raw_order_id)
    db.commit()
    kwargs = dict(source_order_ids=[order.raw_order_id], allowed_project_ids={project.project_id})
    result = summaries.summarize_demand_returns(db, **kwargs)[order.raw_order_id].to_dict()
    assert result["demand_qty"] == "5.000"
    assert result["received_qty"] == result["undated_receipt_qty"] == "2.000"
    assert result["pending_qty"] == result["remaining_qty"] == "3.000"
    assert result["over_received_qty"] == "0.000"
    assert result["basis_state"] == "complete"
    assert result["reason_codes"] == ["undated_receipts"]
    assert "return_status" not in result
    readonly_engine = engine.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
    with Session(readonly_engine) as reader:
        payload = summaries.get_return_requests_outstanding_summary(
            reader, **kwargs, project_ids={project.project_id},
        ).to_dict()
    assert payload["summary"]["known_difference_qty"] == "3.000"
    assert payload["summary"]["unknown_order_count"] == 0
    assert payload["summary"]["incomplete"] is False
    _receipt(db, old_project, disk, "99", source_order_id=order.raw_order_id)
    db.commit()
    conflict = summaries.summarize_demand_returns(db, **kwargs)[order.raw_order_id].to_dict()
    assert conflict["basis_state"] == "ownership_conflict"
    assert conflict["received_qty"] is conflict["pending_qty"] is conflict["over_received_qty"] is None


def test_mixed_same_disk_requirements_do_not_change_whole_order_quantities(db):
    project = _project(db, project_id=str(uuid4()))
    category = ProductCategory(category_major="硬盘", category_minor="SSD")
    db.add(category)
    db.flush()
    disk = DimPart(pn_std="MIXED-DISK", category_id=category.id)
    db.add(disk)
    db.commit()
    order = _demand(db, project, disk, 1, ["1", "9"])
    required = _issue(db, project, disk, "1", no_return=False, source_order_id=order.raw_order_id)
    required.source_line_id = db.scalar(select(FMaintenanceLine.raw_line_id).where(
        FMaintenanceLine.order_id == order.id, FMaintenanceLine.line_no == 1,
    ))
    _receipt(db, project, disk, "9", source_order_id=order.raw_order_id)
    db.commit()
    result = summaries.summarize_demand_returns(
        db, source_order_ids=[order.raw_order_id], allowed_project_ids={project.project_id},
    )[order.raw_order_id].to_dict()
    assert result["demand_qty"] == "10.000"
    assert result["received_qty"] == "9.000"
    assert result["pending_qty"] == "1.000"
    assert result["over_received_qty"] == "0.000"
    assert result["basis_state"] == "complete"
    assert result["reason_codes"] == []
    assert "return_status" not in result


def test_line_exempt_disk_and_unclassified_part_both_count_in_whole_order(db):
    project = _project(db, project_id=str(uuid4()))
    project.display_name = "硬盘免返提醒，不作为判定来源"
    category = ProductCategory(category_major="硬盘", category_minor="SSD")
    db.add(category)
    db.flush()
    disk = DimPart(pn_std="LINE-EXEMPT", category_id=category.id)
    text_only = DimPart(pn_std="TEXT-ONLY", category_major="硬盘")
    db.add_all([disk, text_only])
    db.commit()
    order = _demand(db, project, disk, 1, ["2"])
    _issue(db, project, disk, "2", no_return=True, source_order_id=order.raw_order_id)
    db.add(FMaintenanceLine(
        raw_line_id=str(uuid4()), order_id=order.id, part_id=text_only.id,
        qty=Decimal("3"), is_active=True, import_batch_id=order.import_batch_id,
    ))
    _receipt(db, project, disk, "1", source_order_id=order.raw_order_id)
    db.commit()
    result = summaries.summarize_demand_returns(
        db, source_order_ids=[order.raw_order_id], allowed_project_ids={project.project_id},
    )[order.raw_order_id]
    assert result.demand_qty == Decimal("5")
    assert result.received_qty == Decimal("1")
    assert result.pending_qty == Decimal("4")


@pytest.mark.parametrize(
    "disk,quantities,demand,basis,reason",
    [
        (False, [None, "3"], "3.000", "complete", []),
        (True, [None, "3"], "3.000", "complete", []),
        (False, [None], "0.000", "incomplete", ["zero_demand_qty"]),
        (True, [None], "0.000", "incomplete", ["zero_demand_qty"]),
    ],
)
def test_null_demand_quantities_count_as_zero_including_exempt_disks(
    db, disk, quantities, demand, basis, reason,
):
    project = _project(db, project_id=str(uuid4()))
    category = ProductCategory(category_major="硬盘" if disk else "内存", category_minor="NULL-TEST")
    db.add(category)
    db.flush()
    part = DimPart(pn_std="NULL-QUANTITY", category_id=category.id)
    db.add(part)
    db.commit()
    order = _demand(db, project, part, 1, quantities)
    result = summaries.summarize_demand_returns(
        db, source_order_ids=[order.raw_order_id], allowed_project_ids={project.project_id},
    )[order.raw_order_id].to_dict()
    assert result["demand_qty"] == demand
    assert result["received_qty"] == "0.000"
    assert result["basis_state"] == basis
    assert result["reason_codes"] == reason
    if basis == "complete":
        assert result["pending_qty"] == "3.000"
        assert result["over_received_qty"] == "0.000"
    else:
        assert result["pending_qty"] is result["over_received_qty"] is None
    assert "return_status" not in result


@pytest.mark.parametrize("zero_quantity", [None, "0"])
def test_zero_and_exempt_disk_lines_still_use_original_quantities(db, zero_quantity):
    project = _project(db, project_id=str(uuid4()))
    category = ProductCategory(category_major="硬盘", category_minor="SSD")
    db.add(category)
    db.flush()
    disk = DimPart(pn_std="ZERO-REQUIRED-DISK", category_id=category.id)
    db.add(disk)
    db.commit()
    order = _demand(db, project, disk, 1, [zero_quantity, "3"])
    required = _issue(db, project, disk, "1", no_return=False, source_order_id=order.raw_order_id)
    required.source_line_id = db.scalar(select(FMaintenanceLine.raw_line_id).where(
        FMaintenanceLine.order_id == order.id, FMaintenanceLine.line_no == 1,
    ))
    _receipt(db, project, disk, "2", source_order_id=order.raw_order_id)
    db.commit()
    readonly_engine = engine.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
    with Session(readonly_engine) as reader:
        payload = summaries.get_return_requests_outstanding_summary(
            reader, source_order_ids=[order.raw_order_id],
            allowed_project_ids={project.project_id}, project_ids=None,
        ).to_dict()
    result = payload["orders"][0]
    assert result["demand_qty"] == "3.000"
    assert result["received_qty"] == "2.000"
    assert result["pending_qty"] == "1.000"
    assert result["over_received_qty"] == "0.000"
    assert result["basis_state"] == "complete"
    assert result["reason_codes"] == []
    assert "return_status" not in result
    assert payload["summary"]["unknown_order_count"] == 0
    assert payload["summary"]["incomplete"] is False


def test_snapshot_counts_inactive_separately_and_never_reports_their_overage(db):
    project = _project(db, project_id=str(uuid4()))
    archived = _project(db, project_id=str(uuid4()))
    part = DimPart(pn_std="INACTIVE-COUNTS")
    db.add(part)
    db.commit()
    inactive = _demand(db, project, part, 1, ["1"], status="已取消")
    unassigned = _demand(db, project, part, 2, ["1"])
    assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == unassigned.raw_order_id,
    ))
    assignment.is_active = False
    assignment.version += 1
    assignment.archived_by = "test-fixture"
    assignment.archived_at = assignment.created_at
    archived_order = _demand(db, archived, part, 3, ["1"])
    archived.is_active = False
    incomplete = _demand(db, project, part, 4, [None])
    complete = _demand(db, project, part, 5, ["3"])
    _receipt(db, project, part, "8", source_order_id=inactive.raw_order_id)
    db.commit()
    ids = [order.raw_order_id for order in (inactive, unassigned, archived_order, incomplete, complete)]
    readonly_engine = engine.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
    with Session(readonly_engine) as reader:
        payload = summaries.get_return_requests_outstanding_summary(
            reader, source_order_ids=ids, allowed_project_ids=None, project_ids=None,
        ).to_dict()
    assert payload["contract_version"] == "v2.0-draft"
    assert payload["summary"] == {
        "known_difference_qty": "3.000", "unknown_order_count": 1,
        "inactive_order_count": 3, "incomplete": True,
    }
    by_id = {item["source_order_id"]: item for item in payload["orders"]}
    for order in (inactive, unassigned, archived_order, incomplete):
        assert by_id[order.raw_order_id]["over_received_qty"] is None


def test_issue_flags_and_ambiguous_display_number_never_change_order_quantities(db):
    project = _project(db, project_id=str(uuid4()))
    other_project = _project(db, project_id=str(uuid4()))
    category = ProductCategory(category_major="硬盘", category_minor="SSD")
    db.add(category)
    db.flush()
    disk = DimPart(pn_std="REQUIRED-SCOPE", category_id=category.id)
    db.add(disk)
    db.commit()
    first = _demand(db, project, disk, 1, ["5"])
    second = _demand(db, project, disk, 2, ["5"])
    duplicate = _demand(db, project, disk, 3, ["5"])
    duplicate.order_no = first.order_no
    stable = _issue(db, project, disk, "5", no_return=False, source_order_id=second.raw_order_id)
    stable.demand_order_no = first.order_no  # 稳定身份不能被展示号覆盖。
    _issue(db, other_project, disk, "5", no_return=False, source_order_id=first.raw_order_id)
    manual = _issue(db, project, disk, "5", no_return=False)
    manual.demand_order_no = first.order_no  # 同名多单不可猜配。
    db.commit()
    result = summaries.summarize_demand_returns(
        db, source_order_ids=[first.raw_order_id, second.raw_order_id, duplicate.raw_order_id],
        allowed_project_ids={project.project_id},
    )
    for order in (first, second, duplicate):
        assert result[order.raw_order_id].demand_qty == Decimal("5")
        assert result[order.raw_order_id].received_qty == Decimal("0")
        assert result[order.raw_order_id].pending_qty == Decimal("5")
        assert result[order.raw_order_id].basis_state == "complete"


def test_summary_uses_whole_order_quantities_and_all_received_conditions(db):
    project = _project(db, project_id=str(uuid4()))
    project.no_return_default = True
    order = _wbdd(db, project=project)
    order.head_demand_qty = Decimal("99")
    order.head_returned_qty = Decimal("88")
    part = DimPart(pn_std="SUMMARY-DEMAND-PN")
    db.add(part)
    db.flush()
    for qty in ("2.250", "1.250"):
        db.add(FMaintenanceLine(
            raw_line_id=f"summary-{uuid4()}", order_id=order.id, part_id=part.id,
            qty=Decimal(qty), return_qty=Decimal("1.000"), is_active=True,
            import_batch_id=order.import_batch_id,
        ))
    db.commit()
    for quantity, condition in ((1, "坏品"), (2, "成品")):
        register_receipt(db, project_id=project.project_id, pn="ANOTHER-PN",
                         qty=quantity, condition=condition, wbdd_no=order.order_no,
                         operated_by="summary-fixture")
    register_receipt(db, project_id=project.project_id, pn="UNASSIGNED", qty=7,
                     operated_by="summary-fixture")
    db.commit()

    result = summaries.summarize_demand_returns(
        db, source_order_ids=[order.raw_order_id], allowed_project_ids={project.project_id},
    )[order.raw_order_id]
    assert result.demand_qty == Decimal("3.500")
    assert result.received_qty == Decimal("3.000")
    assert result.pending_qty == Decimal("0.500")
    assert result.remaining_qty == Decimal("0.500")
    assert not hasattr(result, "return_status")
    assert result.basis_state == "complete"
    assert result.to_dict()["demand_qty"] == "3.500"


def test_snapshot_requires_real_readonly_repeatable_read_and_separates_unassigned(db):
    project = _project(db, project_id=str(uuid4()))
    order = _wbdd(db, project=project)
    part = DimPart(pn_std="SNAPSHOT-PN")
    db.add(part)
    db.flush()
    db.add(FMaintenanceLine(
        raw_line_id=f"snapshot-{uuid4()}", order_id=order.id, part_id=part.id,
        qty=Decimal("5"), import_batch_id=order.import_batch_id, is_active=True,
    ))
    register_receipt(db, project_id=project.project_id, pn="SNAPSHOT-PN", qty=2,
                     wbdd_no=order.order_no, operated_by="summary-fixture")
    register_receipt(db, project_id=project.project_id, pn="UNASSIGNED", qty=3,
                     operated_by="summary-fixture")
    db.commit()
    source_id, project_id = order.raw_order_id, project.project_id
    with pytest.raises(summaries.ReturnSummaryError) as error:
        summaries.get_return_requests_outstanding_summary(
            db, source_order_ids=[source_id], allowed_project_ids={project_id}, project_ids=None,
        )
    assert error.value.code == "summary_snapshot_required"
    readonly_engine = engine.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
    with Session(readonly_engine) as reader:
        result = summaries.get_return_requests_outstanding_summary(
            reader, source_order_ids=[source_id], allowed_project_ids={project_id}, project_ids=None,
        ).to_dict()
        assert result["summary"] == {
            "known_difference_qty": "3.000", "unknown_order_count": 0,
            "inactive_order_count": 0, "incomplete": False,
        }
        assert result["unassigned_by_project"] == [{"project_id": project_id, "received_qty": "3.000"}]
        assert result["orders"][0]["pending_qty"] == "3.000"
        assert "known_pending_qty" not in result["summary"]
        assert "return_status" not in result["orders"][0]
        empty = summaries.get_return_requests_outstanding_summary(
            reader, source_order_ids=[], allowed_project_ids={project_id}, project_ids=None,
        ).to_dict()
        assert empty["orders"] == []
        assert empty["unassigned_by_project"] == result["unassigned_by_project"]
        for allowed, selected in ((set(), None), ({project_id}, set()), (set(), {project_id})):
            hidden = summaries.get_return_requests_outstanding_summary(
                reader, source_order_ids=[source_id], allowed_project_ids=allowed, project_ids=selected,
            ).to_dict()
            assert hidden["orders"] == []
            assert hidden["unassigned_by_project"] == []


def test_snapshot_project_filter_restricts_orders_as_well_as_unassigned(db):
    first = _project(db, project_id=str(uuid4()))
    second = _project(db, project_id=str(uuid4()))
    part = DimPart(pn_std="SNAPSHOT-SCOPE")
    db.add(part)
    db.commit()
    order = _demand(db, first, part, 1, ["5"])
    _receipt(db, second, part, "7")
    db.commit()
    source_id, first_id, second_id = order.raw_order_id, first.project_id, second.project_id
    readonly_engine = engine.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
    with Session(readonly_engine) as reader:
        result = summaries.get_return_requests_outstanding_summary(
            reader, source_order_ids=[source_id], allowed_project_ids={first_id, second_id}, project_ids={second_id},
        ).to_dict()
        assert result["orders"] == []  # 不能因 source_order_ids 显式传入就越过项目筛选。
        assert result["unassigned_by_project"] == [{"project_id": second_id, "received_qty": "7.000"}]
        denied = summaries.get_return_requests_outstanding_summary(
            reader, source_order_ids=[source_id], allowed_project_ids={first_id}, project_ids={second_id},
        ).to_dict()
        assert denied["orders"] == denied["unassigned_by_project"] == []


def test_snapshot_keeps_one_real_pg_version_across_501_orders_and_unassigned(db):
    project = _project(db, project_id=str(uuid4()))
    part = DimPart(pn_std="SNAPSHOT-CHUNK")
    db.add(part)
    db.commit()
    seed = _wbdd(db, project=project)
    batch_id, project_id, part_id = seed.import_batch_id, project.project_id, part.id
    orders = [FMaintenanceOrder(raw_order_id=f"chunk-{index:04d}",
                               order_no=f"WBDD-20261002-{index:04d}", data_status="已生效",
                               import_batch_id=batch_id) for index in range(501)]
    db.add_all(orders)
    db.flush()
    lines = [FMaintenanceLine(raw_line_id=f"chunk-line-{index}", order_id=order.id,
                             part_id=part_id, qty=Decimal("1"), is_active=True,
                             import_batch_id=batch_id) for index, order in enumerate(orders)]
    db.add_all(lines)
    db.add_all([MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid4()), source_order_id=order.raw_order_id,
        project_id=project_id, is_active=True, created_by="snapshot-fixture",
    ) for order in orders])
    unassigned = _receipt(db, project, part, "3")
    db.commit()
    ids = [order.raw_order_id for order in orders]
    last_line_id, receipt_id = lines[-1].id, unassigned.rkd_line_id
    reader_between_batches, writer_finished = Event(), Event()
    writer_errors = []

    def write_next_version():
        try:
            if not reader_between_batches.wait(60):
                raise AssertionError("reader never reached first batch")
            from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
            with Session(engine) as writer:
                writer.get(FMaintenanceLine, last_line_id).qty = Decimal("4")
                writer.get(MaintenanceRkdReturnLine, receipt_id).qty = Decimal("9")
                writer.commit()
        except BaseException as error:
            writer_errors.append(error)
        finally:
            writer_finished.set()

    readonly_engine = engine.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
    def batch_boundary(_conn, cursor, *_args):
        if cursor.rowcount == 500 and not reader_between_batches.is_set():
            reader_between_batches.set()
            assert writer_finished.wait(60), "writer did not commit between batches"

    writer = Thread(target=write_next_version)
    event.listen(readonly_engine, "after_cursor_execute", batch_boundary)
    writer.start()
    try:
        with Session(readonly_engine) as reader:
            snapshot = summaries.get_return_requests_outstanding_summary(
                reader, source_order_ids=ids, allowed_project_ids={project_id}, project_ids=None,
            ).to_dict()
        assert writer_errors == []
        assert len(snapshot["orders"]) == 501
        assert snapshot["summary"]["known_difference_qty"] == "501.000"
        assert snapshot["unassigned_by_project"] == [{"project_id": project_id, "received_qty": "3.000"}]
        fresh = summaries.summarize_demand_returns(db, source_order_ids=[ids[-1]], allowed_project_ids={project_id})
        assert fresh[ids[-1]].demand_qty == Decimal("4")
    finally:
        event.remove(readonly_engine, "after_cursor_execute", batch_boundary)
        reader_between_batches.set()
        writer.join(timeout=60)
        assert not writer.is_alive()


def test_unknown_over_received_inactive_and_cross_project_facts_remain_distinct(db):
    project = _project(db, project_id=str(uuid4()))
    old_project = _project(db, project_id=str(uuid4()))
    part = DimPart(pn_std="MATRIX-PN")
    db.add(part)
    db.commit()
    cases = [
        ("no_rows", [], "已生效"), ("nullable", [None, "3"], "已生效"),
        ("negative", ["-1", "3"], "已生效"), ("invalid", ["NaN", "3"], "已生效"),
        ("zero", ["0"], "已生效"),
        ("over", ["2"], "已生效"), ("pending", ["4"], "已生效"),
        ("inactive", ["4"], "已取消"), ("conflict", ["4"], "已生效"),
    ]
    orders = {name: _demand(db, project, part, index, quantities, status=status)
              for index, (name, quantities, status) in enumerate(cases, 1)}
    _receipt(db, project, part, "3", source_order_id=orders["over"].raw_order_id)
    _receipt(db, project, part, "1", source_order_id=orders["pending"].raw_order_id,
             source="rkd_import", condition="成品", occurred_at=None)
    _receipt(db, project, part, "99", source_order_id=orders["pending"].raw_order_id, status="voided")
    _receipt(db, old_project, part, "123", source_order_id=orders["conflict"].raw_order_id)
    db.commit()
    source_ids = [item.raw_order_id for item in orders.values()]
    result = summaries.summarize_demand_returns(
        db, source_order_ids=source_ids, allowed_project_ids={project.project_id},
    )
    def item(name):
        return result[orders[name].raw_order_id]

    for name, reason in (("no_rows", "no_active_lines"),
                         ("negative", "negative_demand_qty"), ("invalid", "invalid_demand_qty"),
                         ("zero", "zero_demand_qty")):
        assert not hasattr(item(name), "return_status")
        assert item(name).pending_qty is None
        assert reason in item(name).reason_codes
    assert item("nullable").demand_qty == Decimal("3")
    assert item("nullable").pending_qty == Decimal("3")
    assert item("nullable").reason_codes == ()
    assert item("negative").demand_qty is item("invalid").demand_qty is None
    assert item("over").received_qty == Decimal("3")
    assert item("over").over_received_qty == Decimal("1")
    assert item("over").pending_qty == Decimal("0")
    assert item("pending").pending_qty == Decimal("3")  # A 超收不能抵 B 欠返。
    assert item("pending").undated_receipt_qty == Decimal("1")
    assert item("pending").basis_state == "complete"
    assert item("inactive").basis_state == "inactive"
    assert item("inactive").pending_qty is item("inactive").over_received_qty is None
    assert item("conflict").basis_state == "ownership_conflict"
    for field in ("received_qty", "pending_qty", "remaining_qty", "over_received_qty",
                  "undated_receipt_qty"):
        assert getattr(item("conflict"), field) is None
    assert summaries.summarize_demand_returns(
        db, source_order_ids=source_ids, allowed_project_ids={old_project.project_id},
    ) == {}


def test_batch_is_one_select_does_not_autoflush_and_empty_scope_never_widens(db):
    project = _project(db, project_id=str(uuid4()))
    part = DimPart(pn_std="ONE-SELECT")
    db.add(part)
    db.commit()
    first = _demand(db, project, part, 1, ["1"])
    second = _demand(db, project, part, 2, ["2"])
    ids = [first.raw_order_id, second.raw_order_id, first.raw_order_id]
    project_id = project.project_id
    statements = []
    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        db.autoflush = True
        project.display_name = "未提交草稿，不应被汇总写入"
        result = summaries.summarize_demand_returns(db, source_order_ids=ids, allowed_project_ids={project_id})
        assert len(result) == 2
        assert len(statements) == 1
        assert not any(statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for statement in statements)
        assert project in db.dirty
        statements.clear()
        assert summaries.summarize_demand_returns(db, source_order_ids=[], allowed_project_ids=None) == {}
        assert summaries.summarize_demand_returns(db, source_order_ids=ids, allowed_project_ids=set()) == {}
        for kwargs, code in (({"source_order_ids": [str(i) for i in range(501)]}, "summary_batch_too_large"),
                             ({"source_order_ids": "not-a-collection"}, "invalid_summary_request"),
                             ({"source_order_ids": ids, "as_of": date(2026, 10, 1)}, "historical_as_of_not_supported")):
            with pytest.raises(summaries.ReturnSummaryError) as error:
                summaries.summarize_demand_returns(db, allowed_project_ids=None, **kwargs)
            assert error.value.code == code
        assert statements == []
    finally:
        db.autoflush = False
        event.remove(engine, "before_cursor_execute", capture)


def test_search_and_detail_enrich_without_changing_delete_or_line_digest(db):
    project = _project(db, project_id=str(uuid4()))
    part = DimPart(pn_std="HTTP-SUMMARY")
    db.add(part)
    db.commit()
    order = _demand(db, project, part, 1, ["5"])
    source_id, project_id = order.raw_order_id, project.project_id
    client = _admin_client(db)
    row = client.post("/api/maintenance/demands/search", json={"q": order.order_no}).json()["items"][0]
    assert row["return_summary"]["demand_qty"] == "5.000"
    before_digest = row["version_digest"]
    lines_url = f"/api/maintenance/demands/orders/{source_id}/lines"
    detail = client.get(lines_url).json()
    assert detail["return_summary"] == row["return_summary"]
    line_digest = detail["items"][0]["digest"]
    register_receipt(db, project_id=project_id, pn="CROSS-PN", qty=5,
                     wbdd_no=order.order_no, operated_by="summary-fixture")
    db.commit()
    after = client.post("/api/maintenance/demands/search", json={"q": order.order_no}).json()["items"][0]
    assert after["return_summary"]["received_qty"] == "5.000"
    assert after["return_summary"]["pending_qty"] == "0.000"
    assert "return_status" not in row["return_summary"]
    assert "return_status" not in after["return_summary"]
    assert after["version_digest"] == before_digest
    after_detail = client.get(lines_url).json()
    assert after_detail["items"][0]["digest"] == line_digest
    assert after_detail["return_summary"] == after["return_summary"]

