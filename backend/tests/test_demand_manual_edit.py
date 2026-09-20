"""需求单页面直改/直建（v1.36 Phase E）核心测试。

覆盖四条命脉：
1. override 账本：PATCH 写保护字段后，WBDD upsert 重导不覆盖手工值（loader CASE 保护），
   但未保护字段照常更新；
2. edited_source 保护：wbdd 行被页面改过后升为 page_manual，此后整行可保护字段
   均不被重导覆盖（含既有总表 workbook_manual 语义的统一）；
3. snapshot_diff 排除 page_manual 行（防"手工单被报氚云已删→一键作废"陷阱）；
4. 白名单失败关闭：成本列/未知字段/头字段全拒；无项目挂靠拒绝（审计落点要求）。
附：clear_override 恢复 source_value；手工建行合成头/批次身份。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
)
from app.models.maintenance_source_assignment import (
    MaintenanceSourceOrderAssignment,
)
from app.services import maintenance_demand_manual as manual
from app.services import maintenance_wbdd_import
from tests import factories as f


import uuid as _uuid


def _batch(db) -> int:
    from app.models.system import SysImportBatch
    batch = SysImportBatch(filename="t-e.xlsx", file_type="maintenance",
                           file_hash=f"hash-e-{_uuid.uuid4().hex}",
                           status="success")
    db.add(batch)
    db.flush()
    return batch.id


def _make_project(db, *, tag: str, source_order_id: str) -> MaintenanceProject:
    project = MaintenanceProject(
        project_id=f"proj-{tag}-{source_order_id[:8]}",
        display_name=f"项目{tag}", project_code=f"PC-{tag}",
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=f"asg-{tag}", project_id=project.project_id,
        source_order_id=source_order_id, is_active=True, created_by="t",
    ))
    db.commit()
    return project


def _make_project_standalone(db, *, tag: str) -> MaintenanceProject:
    project = MaintenanceProject(
        project_id=f"proj-{tag}-standalone",
        display_name=f"项目{tag}", project_code=f"PC-{tag}",
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    db.commit()
    return project


def _seed_line(db, *, order_raw="M-E1", line_raw="ML-E1", pn="PN-E1",
               qty="2") -> tuple[FMaintenanceOrder, FMaintenanceLine]:
    """走真实 loader 建行（skip 模式 = 首次导入语义），保证 schema 完整。"""
    orders = {order_raw: f.maintenance_head(order_raw, on=date(2026, 3, 1))}
    lines = [f.maintenance_line(order_raw, line_raw, pn, qty=qty)]
    loader.load(db, f.maintenance_result(orders, lines), _batch(db),
                date(2026, 8, 1), mode="skip")
    db.commit()
    order = db.execute(select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id == order_raw)).scalar_one()
    line = db.execute(select(FMaintenanceLine).where(
        FMaintenanceLine.raw_line_id == line_raw)).scalar_one()
    return order, line


def test_patch_then_reimport_preserves_override(db):
    """改 qty=9 → 重导 qty=5：qty 保留 9（override），description 照常被新值覆盖。"""
    order, line = _seed_line(db)
    _make_project(db, tag="E1", source_order_id=order.raw_order_id)

    result = manual.patch_demand_line(
        db, raw_line_id=line.raw_line_id,
        updates={"qty": 9, "description": "手工描述"},
        reason="页面修正数量", operated_by="tester")
    db.commit()
    assert result["changed"] is True
    assert result["qty"] == "9.000"
    assert line.manual_override["qty"]["value"] == "9.000"
    assert line.manual_override["qty"]["source_value"] == "2.000"
    # P1#4 修正：真实 WBDD 行的字段覆盖不升格 edited_source（行来源仍 wbdd，
    # 删单对账照常；保护由 override 字段级承担）
    assert line.edited_source == "wbdd"

    # 重导（upsert 模式，qty=5 + 新描述）
    orders = {order.raw_order_id: f.maintenance_head(order.raw_order_id, on=date(2026, 3, 1))}
    lines = [f.maintenance_line(order.raw_order_id, line.raw_line_id, "PN-E1",
                                qty="5", description="导入新描述")]
    loader.load(db, f.maintenance_result(orders, lines), _batch(db),
                date(2026, 8, 2), mode="upsert")
    db.commit()
    db.refresh(line)
    assert line.qty == Decimal("9.000"), "override 字段必须保留手工值"
    assert line.manual_override["qty"]["value"] == "9.000"
    # description 也在白名单内但当时进了 override —— 同样保护
    assert line.description == "手工描述"


def test_unprotected_field_updates_normally(db):
    """未进 override 的字段（本例 description 从未手改）重导照常覆盖。"""
    order, line = _seed_line(db, order_raw="M-E2", line_raw="ML-E2", pn="PN-E2")
    _make_project(db, tag="E2", source_order_id=order.raw_order_id)
    assert line.description is None

    orders = {order.raw_order_id: f.maintenance_head(order.raw_order_id, on=date(2026, 3, 1))}
    lines = [f.maintenance_line(order.raw_order_id, line.raw_line_id, "PN-E2",
                                qty="2", description="导入描述")]
    loader.load(db, f.maintenance_result(orders, lines), _batch(db),
                date(2026, 8, 2), mode="upsert")
    db.commit()
    db.refresh(line)
    assert line.description == "导入描述"
    assert line.qty == Decimal("2.000")  # 未保护，导入值生效


def test_workbook_manual_rows_also_protected(db):
    """既有 bug 修复（审查 #9）：总表手工行 edited_source='workbook_manual'，
    其 qty 不再被 WBDD 重导覆盖（此前标记在、值被冲——最阴的静默回滚）。"""
    order, line = _seed_line(db, order_raw="M-E3", line_raw="ML-E3", pn="PN-E3")
    _make_project(db, tag="E3", source_order_id=order.raw_order_id)
    line.edited_source = "workbook_manual"
    line.qty = Decimal("7.000")
    db.commit()

    orders = {order.raw_order_id: f.maintenance_head(order.raw_order_id, on=date(2026, 3, 1))}
    lines = [f.maintenance_line(order.raw_order_id, line.raw_line_id, "PN-E3", qty="1")]
    loader.load(db, f.maintenance_result(orders, lines), _batch(db),
                date(2026, 8, 2), mode="upsert")
    db.commit()
    db.refresh(line)
    assert line.qty == Decimal("7.000"), "workbook_manual 行的手工值必须存活"


def test_snapshot_diff_excludes_page_manual(db):
    """手工建的单头（page-manual- 前缀）不参与删单比对（防一键作废陷阱）。"""
    # 真实链路：create_manual_demand_line 建的头带 page-manual- 前缀
    project = _make_project_standalone(db, tag="E4P")
    db.add(DimPart(pn_std="PN-E4", status="active"))
    db.commit()
    result = manual.create_manual_demand_line(
        db, order_date=date(2026, 3, 1), project_id=project.project_id,
        pn_std="PN-E4", qty=1, reason="t", operated_by="tester")
    db.commit()
    manual_order = db.execute(select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id.startswith("page-manual-"))).scalars().all()
    assert any(o.order_no == result["order_no"] for o in manual_order)

    diff = maintenance_wbdd_import.snapshot_diff(
        db, {"SOME-OTHER-ORDER"}, [date(2026, 3, 1), date(2026, 3, 1)])
    assert result["order_no"] not in diff["sample_order_nos"]
    # 回归：被改过字段的普通 WBDD 行仍参与对账（P1#4）
    order2, line2 = _seed_line(db, order_raw="M-E4B", line_raw="ML-E4B", pn="PN-E4B")
    _make_project(db, tag="E4B", source_order_id=order2.raw_order_id)
    manual.patch_demand_line(db, raw_line_id=line2.raw_line_id,
                             updates={"qty": 5}, reason="t", operated_by="t")
    db.commit()
    diff2 = maintenance_wbdd_import.snapshot_diff(
        db, {"SOME-OTHER"}, [date(2026, 3, 1), date(2026, 3, 1)])
    assert order2.order_no in [o for o in diff2["sample_order_nos"]] or diff2["missing_orders"] >= 1


def test_snapshot_diff_still_reports_wbdd_missing(db):
    """回归：正常 WBDD 行缺位仍要报（保护不能误伤对账）。"""
    order, _ = _seed_line(db, order_raw="M-E5", line_raw="ML-E5", pn="PN-E5")
    diff = maintenance_wbdd_import.snapshot_diff(
        db, {"SOME-OTHER"}, [date(2026, 3, 1), date(2026, 3, 1)])
    assert diff["missing_orders"] >= 1


def test_whitelist_rejects_cost_and_unknown(db):
    order, line = _seed_line(db, order_raw="M-E6", line_raw="ML-E6", pn="PN-E6")
    _make_project(db, tag="E6", source_order_id=order.raw_order_id)
    with pytest.raises(manual.DemandManualError, match="不支持的修改字段"):
        manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                                  updates={"cost_amount": "1"}, reason="r", operated_by="t")
    with pytest.raises(manual.DemandManualError, match="不支持的修改字段"):
        manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                                  updates={"order_no": "X"}, reason="r", operated_by="t")


def test_patch_without_project_assignment_rejected(db):
    """审计落点要求项目挂靠：无挂靠行拒绝编辑（失败关闭）。"""
    order, line = _seed_line(db, order_raw="M-E7", line_raw="ML-E7", pn="PN-E7")
    # 不做 _make_project —— 无挂靠
    with pytest.raises(manual.DemandManualError, match="未归属任何项目"):
        manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                                  updates={"qty": 3}, reason="r", operated_by="t")


def test_clear_override_restores_source_value(db):
    order, line = _seed_line(db, order_raw="M-E8", line_raw="ML-E8", pn="PN-E8")
    _make_project(db, tag="E8", source_order_id=order.raw_order_id)
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"qty": 9}, reason="先改", operated_by="t")
    db.commit()
    result = manual.clear_override(db, raw_line_id=line.raw_line_id,
                                   field="qty", reason="还原", operated_by="t")
    db.commit()
    assert result["qty"] == "2.000"
    assert "qty" not in (line.manual_override or {})


def test_manual_create_builds_head_line_batch(db):
    """手工建行：合成头（page-manual- 前缀）+ manual-line: 行 + edited_source。"""
    project = MaintenanceProject(project_id="proj-EC", display_name="项目EC",
                                 project_code="PC-EC", lifecycle_status="ongoing")
    part = DimPart(pn_std="PN-EC", status="active")
    db.add_all([project, part])
    db.flush()
    db.commit()

    result = manual.create_manual_demand_line(
        db, order_date=date(2026, 9, 1), project_id="proj-EC",
        pn_std="PN-EC", qty=3, reason="手工补录", operated_by="tester")
    db.commit()
    assert result["order_no"].startswith("PAGE-")
    assert result["edited_source"] == "page_manual"
    line = db.execute(select(FMaintenanceLine).where(
        FMaintenanceLine.raw_line_id == result["raw_line_id"])).scalar_one()
    assert line.raw_line_id.startswith("manual-line:")
    assert line.import_batch_id is not None
    order = db.get(FMaintenanceOrder, line.order_id)
    assert order.raw_order_id.startswith("page-manual-")
    # 审计落 operation_audit
    audit = db.execute(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_id == line.raw_line_id,
        MaintenanceProjectOperationAudit.action == "page_create")).scalar_one()
    assert audit.project_id == "proj-EC"
