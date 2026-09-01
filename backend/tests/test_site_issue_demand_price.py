"""领用成本解析：维保需求单价格为最强证据层（2026-08-23 用户口径）。

场景回归：大疆 01PE163——采购价窗口 947.31 vs 需求单 355.95，
需求单必须赢；无需求单价格时回退采购窗口；两边都没有保持 NULL。
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceDemandDeleteIntent,
    MaintenanceDemandTombstone,
)
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
from app.services import maintenance_cost
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


def _wbdd_price(db, *, project, pn, unit_ex, qty="1", return_qty=None,
                order_no=None, order_date=None):
    """造一条挂靠到 project 的 WBDD 需求单行，行成本单价 = unit_ex。

    qty/return_qty 控制净数量口径；order_no/order_date 控制同单匹配与
    "最新一单"挑选（2026-08-24 取价层修复回归用）。
    """
    batch = _batch(db, f"dem-{uuid.uuid4().hex[:6]}", "maintenance")
    rid = order_no or f"WBDD-DEM-{uuid.uuid4().hex[:8]}"
    loader.load(
        db,
        f.maintenance_result(
            {rid: f.maintenance_head(rid, order_no=rid,
                                     on=order_date or date(2026, 8, 1),
                                     project=project.display_name)},
            [f.maintenance_line(rid, f"{rid}-L1", pn, qty=qty,
                                return_qty=return_qty)],
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
    return line


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


def _append_wbdd_price_line(db, *, first_line, amount_ex, qty, suffix):
    """Append another priced line to the same demand order and PN."""
    line = FMaintenanceLine(
        raw_line_id=f"{first_line.raw_line_id}-{suffix}",
        order_id=first_line.order_id,
        line_no=(first_line.line_no or 1) + 1,
        part_id=first_line.part_id,
        pn_std=first_line.pn_std,
        pn_raw=first_line.pn_raw,
        qty=Decimal(qty),
        return_qty=Decimal("0"),
        cost_source="direct",
        cost_amount_ex_tax=Decimal(amount_ex),
        cost_amount_inc_tax=(Decimal(amount_ex) * Decimal("1.13")).quantize(
            Decimal("0.01")
        ),
        import_batch_id=first_line.import_batch_id,
        is_active=True,
    )
    db.add(line)
    db.commit()
    return line


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


def test_partial_return_no_longer_deflates_unit_price(db):
    """2026-08-24 修复：单价分母改净数量。

    需求行 qty=10、退 4、cost_amount_ex_tax=600（真实单价 100）——
    旧公式 600÷10=60 会把领用价压低四成，修复后必须是 100。
    """
    project = _project(db, "部分退货压价修复项目")
    part = _part(db, "PN-DEM-RET")
    _wbdd_price(db, project=project, pn="PN-DEM-RET", unit_ex="600",
                qty="10", return_qty="4")
    issue, line = _issue_line(db, project=project, part=part)
    mcc.resolve_lines(db, lines=[(issue.issue_date, line)])
    db.commit()
    db.refresh(line)
    assert line.cost_source == "maint_demand"
    assert line.unit_cost_ex_tax == Decimal("100.00")


def test_fully_returned_line_provides_no_price(db):
    """整行退净（净数量 0、成本 0）的需求行不能当价格证据——回退采购窗口。"""
    project = _project(db, "整行退净项目")
    part = _part(db, "PN-DEM-VOID")
    _wbdd_price(db, project=project, pn="PN-DEM-VOID", unit_ex="0",
                qty="2", return_qty="2")
    _purchase_price(db, part=part, unit_price="88.00",
                    order_date=date(2026, 8, 8))
    issue, line = _issue_line(db, project=project, part=part)
    mcc.resolve_lines(db, lines=[(issue.issue_date, line)])
    db.commit()
    db.refresh(line)
    assert line.cost_source == "purchase_window"
    assert line.unit_cost_ex_tax == Decimal("88.00")


def test_same_order_beats_latest_same_part(db):
    """同单同 PN 优先于"最新一单"兜底。

    领用单号 = 旧单 WBDD-OLD（价 200），项目里还有更新的 WBDD-NEW（价 999）
    ——旧公式一律取最新 999；修复后按单号命中本单 200。
    """
    project = _project(db, "同单优先项目")
    part = _part(db, "PN-DEM-SAME")
    _wbdd_price(db, project=project, pn="PN-DEM-SAME", unit_ex="200",
                order_no="WBDD-OLD", order_date=date(2026, 6, 1))
    _wbdd_price(db, project=project, pn="PN-DEM-SAME", unit_ex="999",
                order_no="WBDD-NEW", order_date=date(2026, 8, 5))
    issue, line = _issue_line(db, project=project, part=part)
    issue.issue_no = "WBDD-OLD"
    db.commit()
    mcc.resolve_lines(db, lines=[(issue.issue_date, line)])
    db.commit()
    db.refresh(line)
    assert line.cost_source == "maint_demand"
    assert line.unit_cost_ex_tax == Decimal("200.00")


def test_source_line_id_exact_link_wins(db):
    """领用行显式关联的需求行优先级最高（即使同项目最新一单更贵）。"""
    project = _project(db, "精确行关联项目")
    part = _part(db, "PN-DEM-EXACT")
    linked = _wbdd_price(db, project=project, pn="PN-DEM-EXACT", unit_ex="150",
                         order_no="WBDD-LINKED", order_date=date(2026, 5, 1))
    _wbdd_price(db, project=project, pn="PN-DEM-EXACT", unit_ex="777",
                order_no="WBDD-LATEST", order_date=date(2026, 8, 9))
    issue, line = _issue_line(db, project=project, part=part)
    line.source_order_id = "WBDD-LINKED"
    line.source_line_id = linked.raw_line_id
    db.commit()
    mcc.resolve_lines(db, lines=[(issue.issue_date, line)])
    db.commit()
    db.refresh(line)
    assert line.cost_source == "maint_demand"
    assert line.unit_cost_ex_tax == Decimal("150.00")


def test_resolve_line_single_path_also_uses_demand_layer(db):
    """单行路径（补价/重算入口）与批量路径同口径：需求单层必须生效。"""
    project = _project(db, "单行路径需求层项目")
    part = _part(db, "PN-DEM-SINGLE")
    _wbdd_price(db, project=project, pn="PN-DEM-SINGLE", unit_ex="355.95")
    _purchase_price(db, part=part, unit_price="947.31",
                    order_date=date(2026, 8, 8))
    issue, line = _issue_line(db, project=project, part=part)
    mcc.resolve_line(db, issue_date=issue.issue_date, line=line)
    db.commit()
    db.refresh(line)
    assert line.cost_source == "maint_demand"
    assert line.unit_cost_ex_tax == Decimal("355.95")


def test_same_order_same_part_uses_all_lines_weighted_with_stable_evidence(db):
    project = _project(db, "同单同型号加权项目")
    part = _part(db, "PN-DEM-WEIGHT")
    first = _wbdd_price(
        db,
        project=project,
        pn=part.pn_std,
        unit_ex="200",
        qty="2",
        order_no="WBDD-WEIGHT",
        order_date=date(2026, 8, 1),
    )
    second = _append_wbdd_price_line(
        db, first_line=first, amount_ex="300", qty="1", suffix="L2"
    )
    issue, line = _issue_line(db, project=project, part=part)
    issue.issue_no = "WBDD-WEIGHT"
    db.commit()

    mcc.resolve_lines(db, lines=[(issue.issue_date, line)])
    db.commit()
    db.refresh(line)

    # (200 + 300) / (2 + 1), rounded to cents.  Evidence identities must be
    # source-stable raw ids, never database PKs or the mutable part id.
    assert line.unit_cost_ex_tax == Decimal("166.67")
    assert line.reference_sample_count == 2
    assert line.reference_sample_ids == [
        f"maintenance-demand:{second.raw_line_id}",
        f"maintenance-demand:{first.raw_line_id}",
    ]


def test_exact_source_line_cannot_cross_project_ownership(db):
    project_a = _project(db, "需求证据项目A")
    project_b = _project(db, "需求证据项目B")
    part = _part(db, "PN-DEM-SCOPE")
    foreign = _wbdd_price(
        db, project=project_a, pn=part.pn_std, unit_ex="777",
        order_no="WBDD-A", order_date=date(2026, 8, 2),
    )
    _wbdd_price(
        db, project=project_b, pn=part.pn_std, unit_ex="123",
        order_no="WBDD-B", order_date=date(2026, 8, 3),
    )
    issue, line = _issue_line(db, project=project_b, part=part)
    line.source_line_id = foreign.raw_line_id
    db.commit()

    mcc.resolve_lines(db, lines=[(issue.issue_date, line)])
    db.commit()
    db.refresh(line)

    assert line.unit_cost_ex_tax == Decimal("123.00")
    assert line.reference_sample_ids != [
        f"maintenance-demand:{foreign.raw_line_id}"
    ]


def test_inactive_demand_header_is_not_price_evidence(db, monkeypatch):
    monkeypatch.setattr(mcc.config, "ACTIVE_STATUS_ONLY", True)
    project = _project(db, "无效需求单排除项目")
    part = _part(db, "PN-DEM-INACTIVE")
    demand = _wbdd_price(
        db, project=project, pn=part.pn_std, unit_ex="999",
        order_no="WBDD-INACTIVE", order_date=date(2026, 8, 2),
    )
    order = db.get(FMaintenanceOrder, demand.order_id)
    order.data_status = "已作废"
    _purchase_price(db, part=part, unit_price="88", order_date=date(2026, 8, 9))
    issue, line = _issue_line(db, project=project, part=part)

    mcc.resolve_lines(db, lines=[(issue.issue_date, line)])
    db.commit()
    db.refresh(line)
    assert line.cost_source == "purchase_window"
    assert line.unit_cost_ex_tax == Decimal("88.00")


def test_cutover_tombstone_excludes_demand_price(db, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "maintenance_cutover_enabled", True)
    project = _project(db, "墓碑需求单排除项目")
    part = _part(db, "PN-DEM-TOMBSTONE")
    demand = _wbdd_price(
        db, project=project, pn=part.pn_std, unit_ex="999",
        order_no="WBDD-TOMBSTONE", order_date=date(2026, 8, 2),
    )
    order = db.get(FMaintenanceOrder, demand.order_id)
    now = datetime.now(timezone.utc)
    intent = MaintenanceDemandDeleteIntent(
        intent_id=str(uuid.uuid4()),
        idempotency_key=f"site-price-{uuid.uuid4()}",
        request_digest="a" * 64,
        selection_digest="b" * 64,
        status="executed",
        reason="测试墓碑",
        operated_by="tester",
        header_count=1,
        line_count=1,
        created_at=now,
        expires_at=now,
    )
    db.add(intent)
    db.flush()
    db.add(MaintenanceDemandTombstone(
        source_order_id=order.raw_order_id,
        delete_intent_id=intent.intent_id,
        version_digest="c" * 64,
        deleted_by="tester",
        delete_reason="测试墓碑",
        deleted_at=now,
        version=1,
    ))
    _purchase_price(db, part=part, unit_price="66", order_date=date(2026, 8, 9))
    issue, line = _issue_line(db, project=project, part=part)

    mcc.resolve_lines(db, lines=[(issue.issue_date, line)])
    db.commit()
    db.refresh(line)
    assert line.cost_source == "purchase_window"
    assert line.unit_cost_ex_tax == Decimal("66.00")


def test_as_of_excludes_future_demand_evidence(db):
    project = _project(db, "需求证据快照项目")
    part = _part(db, "PN-DEM-ASOF")
    old = _wbdd_price(
        db, project=project, pn=part.pn_std, unit_ex="100",
        order_no="WBDD-ASOF-OLD", order_date=date(2026, 8, 1),
    )
    future = _wbdd_price(
        db, project=project, pn=part.pn_std, unit_ex="900",
        order_no="WBDD-ASOF-FUTURE", order_date=date(2026, 8, 9),
    )
    issue, line = _issue_line(db, project=project, part=part)

    mcc.resolve_lines(
        db,
        lines=[(issue.issue_date, line)],
        as_of=date(2026, 8, 5),
    )
    db.commit()
    db.refresh(line)
    assert line.unit_cost_ex_tax == Decimal("100.00")
    assert line.reference_sample_ids == [f"maintenance-demand:{old.raw_line_id}"]
    assert f"maintenance-demand:{future.raw_line_id}" not in line.reference_sample_ids


def test_global_cost_recompute_skips_archived_site_issue_projects(db):
    """Archived historical issues must not make every later import/recompute fail."""
    project = _project(db, "已归档领用历史项目")
    part = _part(db, "PN-DEM-ARCHIVED")
    _issue_line(db, project=project, part=part)
    project.is_active = False
    db.commit()

    result = maintenance_cost.recompute(db, commit=False)

    assert result["site_projects_repriced"] == 0
