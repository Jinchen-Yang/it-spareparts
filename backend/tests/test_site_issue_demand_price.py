"""领用成本解析：维保需求单价格为最强证据层（2026-08-23 用户口径）。

场景回归：大疆 01PE163——采购价窗口 947.31 vs 需求单 355.95，
需求单必须赢；无需求单价格时回退采购窗口；两边都没有保持 NULL。
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.maintenance_source_assignment import (
    MaintenanceSourceOrderAssignment,
)
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.system import SysImportBatch
from app.services import maintenance_consumption_cost as mcc
from tests import factories as f


def _project(db, name):
    proj = MaintenanceProject(project_id=str(uuid.uuid4()), project_code=name,
                              display_name=name, lifecycle_status="ongoing")
    db.add(proj)
    db.commit()
    return proj


def _part(db, pn):
    part = DimPart(pn_std=pn)
    db.add(part)
    db.flush()
    return part


def _batch(db, name, file_type):
    batch = SysImportBatch(
        filename=f"synthetic-{name}.xlsx", file_type=file_type,
        file_hash=uuid.uuid4().hex.ljust(64, "0"), status="success")
    db.add(batch)
    db.flush()
    return batch


def _wbdd_price(db, *, project, pn, unit_ex):
    """造一条挂靠到 project 的 WBDD 需求单行，行成本单价 = unit_ex。"""
    batch = _batch(db, f"dem-{uuid.uuid4().hex[:6]}", "maintenance")
    rid = f"WBDD-DEM-{uuid.uuid4().hex[:8]}"
    loader.load(
        db,
        f.maintenance_result(
            {rid: f.maintenance_head(rid, order_no=rid, project=project.display_name)},
            [f.maintenance_line(rid, f"{rid}-L1", pn, qty="1")],
        ),
        batch.id, date(2026, 8, 1), mode="upsert",
    )
    order = db.scalar(select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id == rid))
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid.uuid4()), source_order_id=rid,
        project_id=project.project_id, is_active=True, version=1,
        created_by="tester"))
    line = db.scalar(select(FMaintenanceLine).where(
        FMaintenanceLine.order_id == order.id))
    line.cost_source = "direct"
    line.cost_amount_ex_tax = Decimal(unit_ex)
    line.cost_amount_inc_tax = (Decimal(unit_ex) * Decimal("1.13")).quantize(
        Decimal("0.01"))
    db.commit()


def _purchase_price(db, *, part, unit_price, order_date):
    batch = _batch(db, f"po-{uuid.uuid4().hex[:6]}", "purchase")
    order = FPurchaseOrder(
        raw_order_id=f"PO-{uuid.uuid4().hex[:8]}",
        order_no=f"PO-{uuid.uuid4().hex[:6]}",
        order_date=order_date, data_status="已生效",
        is_tax_inclusive=False, import_batch_id=batch.id)
    db.add(order)
    db.flush()
    db.add(FPurchaseLine(
        raw_line_id=f"POL-{uuid.uuid4().hex[:8]}", order_id=order.id, line_no=1,
        part_id=part.id, pn_std=part.pn_std,
        qty=Decimal("1"), unit_price=Decimal(unit_price),
        import_batch_id=batch.id))
    db.commit()


def _issue_line(db, *, project, part):
    issue = MaintenanceSiteIssue(
        issue_id=str(uuid.uuid4()), project_id=project.project_id,
        issue_no=f"ISS-{uuid.uuid4().hex[:6]}", issue_date=date(2026, 8, 10),
        raw_status="已生效", status_mapping_state="mapped",
        normalized_status="confirmed", status_mapping_version="t",
        source="legacy")
    db.add(issue)
    db.flush()
    line = MaintenanceSiteIssueLine(
        issue_line_id=str(uuid.uuid4()), issue_id=issue.issue_id,
        line_no=1, part_id=part.id, pn=part.pn_std, quantity=Decimal("1"),
        algorithm_version="fixture", tax_rate_used=Decimal("0.13"),
        is_active=True)
    db.add(line)
    db.commit()
    return issue, line


def test_demand_price_beats_purchase_window(db):
    """需求单单价 355.95 必须压过采购窗口 947.31（大疆 01PE163 回归）。"""
    project = _project(db, "需求单价格优先项目")
    part = _part(db, "PN-DEM-001")
    _wbdd_price(db, project=project, pn="PN-DEM-001", unit_ex="355.95")
    _purchase_price(db, part=part, unit_price="947.31",
                    order_date=date(2026, 8, 8))

    issue, line = _issue_line(db, project=project, part=part)
    mcc.resolve_lines(db, lines=[(issue.issue_date, line)])
    db.commit()
    db.refresh(line)
    assert line.cost_source == "maint_demand"
    assert line.unit_cost_ex_tax == Decimal("355.95")
    assert line.cost_amount_inc_tax == Decimal("402.22")  # 355.95 × 1 × 1.13


def test_falls_back_to_purchase_window_without_demand(db):
    project = _project(db, "无需求单回退项目")
    part = _part(db, "PN-DEM-002")
    _purchase_price(db, part=part, unit_price="120.00",
                    order_date=date(2026, 8, 9))

    issue, line = _issue_line(db, project=project, part=part)
    mcc.resolve_lines(db, lines=[(issue.issue_date, line)])
    db.commit()
    db.refresh(line)
    assert line.cost_source == "purchase_window"
    assert line.unit_cost_ex_tax == Decimal("120.00")


def test_no_evidence_anywhere_stays_null(db):
    """两边都没有 → NULL（不知道≠0，铁律 5）。"""
    project = _project(db, "全无证据项目")
    part = _part(db, "PN-DEM-003")
    issue, line = _issue_line(db, project=project, part=part)
    mcc.resolve_lines(db, lines=[(issue.issue_date, line)])
    db.commit()
    db.refresh(line)
    assert line.cost_source is None
    assert line.cost_amount_inc_tax is None
