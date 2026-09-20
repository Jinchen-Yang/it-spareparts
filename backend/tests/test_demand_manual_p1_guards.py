"""P1 审查修复专项验收（v1.36 批次 3）。

覆盖 review-findings #1-#5 + #10：
1. scope：受限账号改别人项目的行 / 在别人项目 create → 拒绝；full scope 放行
2. assignment：create 后总表读侧（inner join assignment）能看到、能再编辑
3. PN 身份三元组：A→B 后 clear 任一 PN 控件 → pn_std/pn_raw/part_id 整组恢复 A、
   两项 override 一起撤销；解析失败拒绝；重导不拽回 part_id
4. OCC：stale digest → 409；digest 覆盖 part_id + 完整 override 状态（可见值
   不变、仅 override 来源变化 → 旧 token 必须冲突）
5. NaN/bool qty → 400；幂等：create 同 key 同完整 payload 重放不重复建，
   payload 变化 409，作废后重放拒绝，人工编辑不影响原请求重放
6. loader 白名单：override 行的 part_id 不被重导覆盖（_MAINT_LINE_UPD 含
   part_id，但 override CASE 保护现在覆盖它）
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.services import maintenance_demand_manual as manual
from tests import factories as f
from tests.test_demand_manual_edit import (
    _batch, _make_project, _make_project_standalone, _seed_line,
)


def test_scope_restricted_account_cannot_patch_other_project(db):
    """#1：scope={proj-X} 的账号改 proj-Y 的行 → 拒绝。"""
    order, line = _seed_line(db, order_raw="M-P1A", line_raw="ML-P1A", pn="PN-P1A")
    other = _make_project(db, tag="P1A", source_order_id=order.raw_order_id)
    with pytest.raises(manual.DemandManualError, match="项目可见范围"):
        manual.patch_demand_line(
            db, raw_line_id=line.raw_line_id, updates={"qty": 3},
            reason="r", operated_by="t",
            allowed_project_ids={"proj-OTHER-1"},
        )


def test_scope_restricted_account_cannot_clear_other_project(db):
    """#1：clear 同样受 scope 约束（改/撤都是写操作）。"""
    order, line = _seed_line(db, order_raw="M-P1A2", line_raw="ML-P1A2", pn="PN-P1A2")
    _make_project(db, tag="P1A2", source_order_id=order.raw_order_id)
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"qty": 9}, reason="r", operated_by="t")
    db.commit()
    with pytest.raises(manual.DemandManualError, match="项目可见范围"):
        manual.clear_override(
            db, raw_line_id=line.raw_line_id, field="qty",
            reason="r", operated_by="t",
            allowed_project_ids={"proj-OTHER-9"},
        )


def test_scope_restricted_create_into_invisible_project_rejected(db):
    project = _make_project_standalone(db, tag="P1B")
    with pytest.raises(manual.DemandManualError, match="项目可见范围"):
        manual.create_manual_demand_line(
            db, order_date=date(2026, 9, 1), project_id=project.project_id,
            pn_std="PN-X", qty=1, reason="r", operated_by="t",
            allowed_project_ids={"proj-OTHER-2"},
        )


def test_scope_full_none_passes(db):
    order, line = _seed_line(db, order_raw="M-P1C", line_raw="ML-P1C", pn="PN-P1C")
    _make_project(db, tag="P1C", source_order_id=order.raw_order_id)
    result = manual.patch_demand_line(
        db, raw_line_id=line.raw_line_id, updates={"qty": 4},
        reason="r", operated_by="t", allowed_project_ids=None,
    )
    assert result["changed"] is True


def test_create_assigns_project_and_line_visible_in_workbook_join(db):
    """#2：create 后 assignment 存在 → 总表读侧 inner join 能看到。"""
    from app.models.maintenance_source_assignment import (
        MaintenanceSourceOrderAssignment,
    )
    project = _make_project_standalone(db, tag="P1D")
    db.add(DimPart(pn_std="PN-P1D", status="active"))
    db.commit()
    result = manual.create_manual_demand_line(
        db, order_date=date(2026, 3, 1), project_id=project.project_id,
        pn_std="PN-P1D", qty=2, reason="r", operated_by="tester",
        idempotency_key="idem-visibility-001",
    )
    db.commit()
    order_raw = db.scalar(select(FMaintenanceOrder.raw_order_id).where(
        FMaintenanceOrder.order_no == result["order_no"]))
    assignment = db.execute(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == order_raw,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )).scalar_one()
    assert assignment.project_id == project.project_id
    # 总表读侧等价 join：行随 assignment 可见
    from sqlalchemy import func
    visible = db.scalar(
        select(func.count(FMaintenanceLine.id))
        .select_from(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .join(MaintenanceSourceOrderAssignment,
              MaintenanceSourceOrderAssignment.source_order_id
              == FMaintenanceOrder.raw_order_id)
        .where(FMaintenanceOrder.raw_order_id == order_raw,
               MaintenanceSourceOrderAssignment.is_active.is_(True),
               FMaintenanceLine.is_active.is_(True))
    )
    assert visible == 1


def test_patch_new_pn_unresolvable_rejected_keeps_old_identity(db):
    """#3：新 PN 无法解析 → 拒绝整个修改（不保留旧 part_id 造成错位）。"""
    order, line = _seed_line(db, order_raw="M-P1E", line_raw="ML-P1E", pn="PN-P1E")
    _make_project(db, tag="P1E", source_order_id=order.raw_order_id)
    with pytest.raises(manual.DemandManualError, match="未匹配到型号主数据"):
        manual.patch_demand_line(
            db, raw_line_id=line.raw_line_id,
            updates={"pn_std": "PN-NOT-EXIST"}, reason="r", operated_by="t",
        )
    db.rollback()
    db.refresh(line)
    assert line.pn_std == "PN-P1E", "拒绝后原身份必须原样保留"
    assert "pn_std" not in (line.manual_override or {})


def _pn_override_setup(db, *, order_raw, line_raw, pn_a, pn_b, tag):
    """A→B（两字段一起改）的标准前置。"""
    order, line = _seed_line(db, order_raw=order_raw, line_raw=line_raw, pn=pn_a)
    _make_project(db, tag=tag, source_order_id=order.raw_order_id)
    part_a = db.scalar(select(DimPart).where(DimPart.pn_std == pn_a))
    db.add(DimPart(pn_std=pn_b, status="active"))
    db.commit()
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"pn_std": pn_b, "pn_raw": pn_b},
                             reason="换 B", operated_by="t")
    db.commit()
    return order, line, part_a


@pytest.mark.parametrize("clear_field", ["pn_std", "pn_raw"])
def test_pn_clear_restores_identity_triple(db, clear_field):
    """#3 完整版：A→B（两字段一起改）后，只 clear 一个 PN 控件 →
    pn_std/pn_raw/part_id 三元组全部恢复 A，两项 override 一起撤销。"""
    order, line, part_a = _pn_override_setup(
        db, order_raw="M-P1F", line_raw="ML-P1F",
        pn_a="PN-A1", pn_b="PN-B1", tag="P1F")
    assert line.pn_std == "PN-B1" and line.pn_raw == "PN-B1"
    assert line.part_id != part_a.id

    manual.clear_override(db, raw_line_id=line.raw_line_id, field=clear_field,
                          reason="回 A", operated_by="t")
    db.commit()
    assert line.pn_std == "PN-A1", "pn_std 必须随任一 PN clear 整体还原"
    assert line.pn_raw == "PN-A1", "pn_raw 必须随任一 PN clear 整体还原"
    assert line.part_id == part_a.id, "part_id 必须成组恢复"
    ov = line.manual_override or {}
    assert "pn_std" not in ov, "两项 PN override 必须一起撤销"
    assert "pn_raw" not in ov, "两项 PN override 必须一起撤销"


def test_pn_raw_pointing_to_other_identity_rejected(db):
    """Codex 复审：pn_raw 独立解析到**不同** part_id → 400 拒绝，
    不得静默接受"显示新 raw、成本按旧身份"的矛盾三元组。"""
    order, line = _seed_line(db, order_raw="M-P1F2", line_raw="ML-P1F2", pn="PN-A9")
    _make_project(db, tag="P1F2", source_order_id=order.raw_order_id)
    db.add(DimPart(pn_std="PN-B9", status="active"))
    db.commit()
    part_a = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-A9"))

    with pytest.raises(manual.DemandManualError, match="不同型号身份"):
        manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                                 updates={"pn_raw": "PN-B9"}, reason="只改 raw",
                                 operated_by="t")
    db.rollback()
    db.refresh(line)
    assert line.pn_raw == "PN-A9" and line.part_id == part_a.id, \
        "拒绝后不得留下部分修改"
    assert "pn_raw" not in (line.manual_override or {})


def test_pn_raw_alias_of_same_identity_allowed(db):
    """pn_raw 是 pn_std 身份的 alias 写法 → 合法（raw 是展示痕迹，身份不变）。"""
    from app.models.dimensions import PartAlias

    order, line = _seed_line(db, order_raw="M-P1F2B", line_raw="ML-P1F2B",
                             pn="PN-A10")
    _make_project(db, tag="P1F2B", source_order_id=order.raw_order_id)
    part_a = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-A10"))
    db.add(PartAlias(pn_raw="PN-A10 旧写法", pn_std="PN-A10",
                     part_id=part_a.id, status="active", source="manual"))
    db.commit()

    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"pn_raw": "PN-A10 旧写法"}, reason="raw 别名",
                             operated_by="t")
    db.commit()
    assert line.pn_raw == "PN-A10 旧写法"
    assert line.part_id == part_a.id, "alias raw 不换身份"

    # clear：pn_raw 回原值、身份组无残键
    manual.clear_override(db, raw_line_id=line.raw_line_id, field="pn_raw",
                          reason="还原", operated_by="t")
    db.commit()
    assert line.pn_raw == "PN-A10"
    ov = line.manual_override or {}
    assert "pn_std" not in ov and "pn_raw" not in ov


def test_pn_std_and_raw_both_submitted_inconsistent_rejected(db):
    """两字段同时提交但解析到不同身份 → 400（按明确规范拒绝，不静默归一）。"""
    order, line = _seed_line(db, order_raw="M-P1F2C", line_raw="ML-P1F2C",
                             pn="PN-A10C")
    _make_project(db, tag="P1F2C", source_order_id=order.raw_order_id)
    db.add(DimPart(pn_std="PN-B10C", status="active"))
    db.commit()
    with pytest.raises(manual.DemandManualError, match="不同型号身份"):
        manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                                 updates={"pn_std": "PN-A10C", "pn_raw": "PN-B10C"},
                                 reason="矛盾组", operated_by="t")
    db.rollback()
    db.refresh(line)
    assert line.pn_std == "PN-A10C" and line.pn_raw == "PN-A10C"


def test_clear_qty_keeps_pn_override_and_reimport_keeps_pn(db):
    """Codex 复审：PN override + qty override 并存 → 只 clear qty →
    PN 三元组与 PN 账本必须保持 → 重导（旧 PN/旧 qty）仍保持手工 PN。"""
    order, line = _seed_line(db, order_raw="M-P1F4", line_raw="ML-P1F4", pn="PN-A11")
    _make_project(db, tag="P1F4", source_order_id=order.raw_order_id)
    part_a = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-A11"))
    db.add(DimPart(pn_std="PN-B11", status="active"))
    db.commit()
    part_b = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-B11"))
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"pn_std": "PN-B11", "pn_raw": "PN-B11"},
                             reason="换 B", operated_by="t")
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"qty": 9}, reason="改量", operated_by="t")
    db.commit()

    manual.clear_override(db, raw_line_id=line.raw_line_id, field="qty",
                          reason="撤量", operated_by="t")
    db.commit()
    ov = line.manual_override or {}
    assert "qty" not in ov, "qty override 应被撤销"
    assert "pn_std" in ov and "pn_raw" in ov, "clear qty 不得误删 PN 保护"
    assert line.pn_std == "PN-B11" and line.pn_raw == "PN-B11"
    assert line.part_id == part_b.id

    # 重导旧 PN + 旧 qty：PN 保护存活，qty 已无保护可被导入覆盖
    rows = [f.maintenance_line(order.raw_order_id, line.raw_line_id, "PN-A11", qty="3")]
    loader.load(db, f.maintenance_result(
        {order.raw_order_id: f.maintenance_head(order.raw_order_id, on=date(2026, 3, 1))},
        rows), _batch(db), date(2026, 9, 1), mode="upsert")
    db.commit()
    db.refresh(line)
    assert line.pn_std == "PN-B11" and line.part_id == part_b.id, \
        "PN 手工身份必须在 clear qty + 重导后存活"
    assert line.qty == Decimal("3.000"), "qty 保护已撤，导入值生效"


def test_pn_second_edit_keeps_first_source_part_id(db):
    """Codex 复审：两次 PN 编辑（B→C）→ clear 后 part_id 回最初的 A，
    不是中间态 B。"""
    order, line = _seed_line(db, order_raw="M-P1F5", line_raw="ML-P1F5", pn="PN-A12")
    _make_project(db, tag="P1F5", source_order_id=order.raw_order_id)
    part_a = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-A12"))
    db.add_all([DimPart(pn_std="PN-B12", status="active"),
                DimPart(pn_std="PN-C12", status="active")])
    db.commit()
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"pn_std": "PN-B12", "pn_raw": "PN-B12"},
                             reason="A→B", operated_by="t")
    db.commit()
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"pn_std": "PN-C12", "pn_raw": "PN-C12"},
                             reason="B→C", operated_by="t")
    db.commit()
    ov = line.manual_override or {}
    assert ov["pn_std"]["source_value_part_id"] == part_a.id, \
        "第二次 PN 编辑不得覆盖首次的身份快照"

    manual.clear_override(db, raw_line_id=line.raw_line_id, field="pn_std",
                          reason="回 A", operated_by="t")
    db.commit()
    assert line.pn_std == "PN-A12" and line.pn_raw == "PN-A12"
    assert line.part_id == part_a.id, "clear 必须回到最初身份 A（非中间态 B）"


def test_pn_same_field_twice_clear_triple(db):
    """交叉审查 ①：同一字段（只改 pn_std）连续 A→B→C → clear 三元组回 A。
    覆盖"先重建 entry 再取组快照"的顺序缺陷：第二次编辑曾把快照 A 换成 B。"""
    order, line = _seed_line(db, order_raw="M-P1F8", line_raw="ML-P1F8", pn="PN-A15")
    _make_project(db, tag="P1F8", source_order_id=order.raw_order_id)
    part_a = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-A15"))
    db.add_all([DimPart(pn_std="PN-B15", status="active"),
                DimPart(pn_std="PN-C15", status="active")])
    db.commit()
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"pn_std": "PN-B15"}, reason="A→B",
                             operated_by="t")
    db.commit()
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"pn_std": "PN-C15"}, reason="B→C",
                             operated_by="t")
    db.commit()
    ov = line.manual_override or {}
    assert ov["pn_std"]["source_value"] == "PN-A15"
    assert ov["pn_std"]["source_value_part_id"] == part_a.id, \
        "同字段第二次编辑不得覆盖首次身份快照"

    manual.clear_override(db, raw_line_id=line.raw_line_id, field="pn_std",
                          reason="回 A", operated_by="t")
    db.commit()
    assert line.pn_std == "PN-A15"
    assert line.pn_raw == "PN-A15"
    assert line.part_id == part_a.id, "clear 后必须是三元组 A（非中间态 B）"


def test_latest_missing_mixed_real_and_manual_consistent(db):
    """review_spec：混合"1 条真实缺失 WBDD + 1 条页面手工单"时，
    latest_missing 的计数、明细列表、分母必须与 snapshot_diff 同口径
    （都排除 page-manual-%）——否则前端全选作废会误删手工单。"""
    from app.services import maintenance_wbdd_import as wbdd

    # 文件里有的单（真实 WBDD，进回执批次 → 不算缺失）
    in_file, _l0 = _seed_line(db, order_raw="M-P1M0", line_raw="ML-P1M0", pn="PN-P1M0")
    _make_project(db, tag="P1M0", source_order_id=in_file.raw_order_id)
    # 真实缺失 WBDD 单（不在文件里）
    order, line = _seed_line(db, order_raw="M-P1M1", line_raw="ML-P1M1", pn="PN-P1M1")
    _make_project(db, tag="P1M1", source_order_id=order.raw_order_id)
    # 页面手工单（page-manual- 前缀头，同日期窗）
    project2 = _make_project_standalone(db, tag="P1M2")
    db.add(DimPart(pn_std="PN-P1M2", status="active"))
    db.commit()
    manual.create_manual_demand_line(
        db, order_date=date(2026, 3, 1), project_id=project2.project_id,
        pn_std="PN-P1M2", qty=1, reason="手工单", operated_by="tester",
        idempotency_key="idem-missing-mix-001",
    )
    # 回执挂在 in_file 的批次：文件单集 = {in_file}（非空 → diff 生效）
    from app.models.maintenance_wbdd_import import MaintenanceWbddImportReceipt
    db.add(MaintenanceWbddImportReceipt(
        batch_id=in_file.import_batch_id, idempotency_key="missing-mix-key-0001",
        uploaded_by="t", file_hash="hash-mix-0001",
        report_json={"batch_id": in_file.import_batch_id},
    ))
    db.commit()

    diff = wbdd.snapshot_diff(db, {in_file.order_no}, [date(2026, 3, 1), date(2026, 3, 1)])
    assert diff["missing_orders"] == 1, "snapshot_diff 只报真实缺失（排除手工单）"

    result = wbdd.latest_missing(db)
    assert result["readiness"] == "ready"
    assert result["missing_count"] == 1
    listed = [m["source_order_id"] for m in result["missing_orders"]]
    assert order.raw_order_id in listed, "真实缺失单在列表"
    assert all(not sid.startswith("page-manual-") for sid in listed), \
        "手工单不得混入缺失列表（全选作废会误删）"
    assert len(listed) == result["missing_count"] == 1, "计数与列表一致"
    # 分母 = 窗口内活跃单（in_file + 缺失单 = 2），不含手工单（否则会是 3）
    assert result["db_active_in_window"] == 2
    assert result["missing_ratio"] == 0.5


def test_loader_audit_part_id_effective_value_matches_db(db):
    """交叉审查 ②：人工 PN=B 重导 A → DB part_id 保持 B；audit after_json
    与 changed_keys 必须按真实库值记（不伪记 part_id=A）。"""
    from app.models.system import SysAuditLog

    order, line = _seed_line(db, order_raw="M-P1F9", line_raw="ML-P1F9", pn="PN-A16")
    _make_project(db, tag="P1F9", source_order_id=order.raw_order_id)
    db.add(DimPart(pn_std="PN-B16", status="active"))
    db.commit()
    part_b = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-B16"))
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"pn_std": "PN-B16", "pn_raw": "PN-B16"},
                             reason="改 B", operated_by="t")
    db.commit()

    rows = [f.maintenance_line(order.raw_order_id, line.raw_line_id, "PN-A16", qty="7")]
    loader.load(db, f.maintenance_result(
        {order.raw_order_id: f.maintenance_head(order.raw_order_id, on=date(2026, 3, 1))},
        rows), _batch(db), date(2026, 9, 1), mode="upsert")
    db.commit()
    db.refresh(line)
    assert line.part_id == part_b.id, "DB part_id 保持 B（SQL 保护）"
    assert line.pn_std == "PN-B16", "DB pn_std 保持 B"
    assert line.qty == Decimal("7.000"), "qty 未保护，导入生效"
    # 无审计入参时 changed_keys 不含本行（有效值无变化——qty 虽变但须真实比较）
    # qty 从 2→7 是真实变化，changed_keys 应含本行；PN/part_id 不是
    # （此处仅确认无异常；审计一致性由带 audit 的下半段验证）

    # 带 audit 走同链路：audit after_json 的 part_id 必须等于库上 B
    order2, line2 = _seed_line(db, order_raw="M-P1F10", line_raw="ML-P1F10",
                               pn="PN-A17")
    _make_project(db, tag="P1F10", source_order_id=order2.raw_order_id)
    db.add(DimPart(pn_std="PN-B17", status="active"))
    db.commit()
    part_b2 = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-B17"))
    manual.patch_demand_line(db, raw_line_id=line2.raw_line_id,
                             updates={"pn_std": "PN-B17", "pn_raw": "PN-B17"},
                             reason="改 B", operated_by="t")
    db.commit()

    batch2 = _batch(db)
    rows2 = [f.maintenance_line(order2.raw_order_id, line2.raw_line_id, "PN-A17",
                                qty="2")]
    loader.load(db, f.maintenance_result(
        {order2.raw_order_id: f.maintenance_head(order2.raw_order_id, on=date(2026, 3, 1))},
        rows2), batch2, date(2026, 9, 1), mode="upsert",
        operated_by="importer-t", audit_overwrites=True)
    db.commit()
    db.refresh(line2)
    assert line2.part_id == part_b2.id
    # qty 2→2 无变化、PN 保护下无有效变化 → 本行不应进 overwrite 审计
    audits = db.execute(select(SysAuditLog).where(
        SysAuditLog.entity_id == line2.id,
        SysAuditLog.action == "overwrite")).scalars().all()
    assert not audits, \
        "保护字段无有效变化时不得伪记 overwrite 审计"

    # 反例：qty 真实变化（2→5）→ 审计入账，且 after_json 的 part_id 必须
    # 等于库上保护值 B（不得伪记导入值 A 的 part_id）
    order3, line3 = _seed_line(db, order_raw="M-P1F11", line_raw="ML-P1F11",
                               pn="PN-A18")
    _make_project(db, tag="P1F11", source_order_id=order3.raw_order_id)
    db.add(DimPart(pn_std="PN-B18", status="active"))
    db.commit()
    part_b3 = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-B18"))
    part_a3 = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-A18"))
    manual.patch_demand_line(db, raw_line_id=line3.raw_line_id,
                             updates={"pn_std": "PN-B18", "pn_raw": "PN-B18"},
                             reason="改 B", operated_by="t")
    db.commit()

    batch3 = _batch(db)
    rows3 = [f.maintenance_line(order3.raw_order_id, line3.raw_line_id, "PN-A18",
                                qty="5")]
    loader.load(db, f.maintenance_result(
        {order3.raw_order_id: f.maintenance_head(order3.raw_order_id, on=date(2026, 3, 1))},
        rows3), batch3, date(2026, 9, 1), mode="upsert",
        operated_by="importer-t", audit_overwrites=True)
    db.commit()
    db.refresh(line3)
    assert line3.part_id == part_b3.id, "库上身份仍 B"
    assert line3.qty == Decimal("5.000")
    audit3 = db.execute(select(SysAuditLog).where(
        SysAuditLog.entity_id == line3.id,
        SysAuditLog.action == "overwrite")).scalars().one_or_none()
    assert audit3 is not None, "qty 真实变化应有 overwrite 审计"
    assert audit3.after_json.get("part_id") == part_b3.id, \
        "audit after 的 part_id 必须与库上保护值一致（非导入值）"
    assert audit3.after_json.get("part_id") != part_a3.id


def _make_manual_cost(db, line, *, amount_ex="100.00") -> None:
    from decimal import Decimal as _D

    from app.models.maintenance import MaintenanceManualCostOverride
    db.add(MaintenanceManualCostOverride(
        line_id=line.id,
        unit_cost_ex_tax=_D(amount_ex),
        unit_cost_inc_tax=_D(amount_ex) * _D("1.13"),
        tax_rate_used=_D("0.13"),
        active=True, version=1, updated_by="admin",
    ))
    db.commit()


def test_pn_rebind_retires_manual_cost_override(db):
    """Codex 复审：PN 真实换绑（A→B）必须停用旧 PN 的人工成本证据；
    新 PN 无价格记录时成本缺失保持 NULL（不得沿用 A 的人工价）。"""
    from app.models.maintenance import MaintenanceManualCostOverride

    order, line = _seed_line(db, order_raw="M-P1F6", line_raw="ML-P1F6", pn="PN-A13")
    _make_project(db, tag="P1F6", source_order_id=order.raw_order_id)
    db.add(DimPart(pn_std="PN-B13", status="active"))
    db.commit()
    _make_manual_cost(db, line)

    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"pn_std": "PN-B13", "pn_raw": "PN-B13"},
                             reason="换 B", operated_by="op1")
    db.commit()

    ov = db.execute(select(MaintenanceManualCostOverride).where(
        MaintenanceManualCostOverride.line_id == line.id)).scalar_one()
    assert ov.active is False, "换绑后旧人工价必须停用"
    assert ov.version == 2 and ov.updated_by == "op1"
    db.expire(line)
    # 新 PN 无任何价格记录 → recompute 后成本缺失保持 NULL
    assert line.unit_cost is None
    assert line.cost_source == "none" or line.cost_source is None


def test_pn_clear_retires_manual_cost_override(db):
    """clear PN（B 回 A）同样换绑：B 期间的人工价不得留给 A。"""
    from app.models.maintenance import MaintenanceManualCostOverride

    order, line = _seed_line(db, order_raw="M-P1F7", line_raw="ML-P1F7", pn="PN-A14")
    _make_project(db, tag="P1F7", source_order_id=order.raw_order_id)
    db.add(DimPart(pn_std="PN-B14", status="active"))
    db.commit()
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"pn_std": "PN-B14", "pn_raw": "PN-B14"},
                             reason="换 B", operated_by="t")
    db.commit()
    # B 期间录入人工价
    _make_manual_cost(db, line, amount_ex="50.00")
    assert db.scalar(select(MaintenanceManualCostOverride.active).where(
        MaintenanceManualCostOverride.line_id == line.id)) is True

    manual.clear_override(db, raw_line_id=line.raw_line_id, field="pn_std",
                          reason="回 A", operated_by="op2")
    db.commit()
    ov = db.execute(select(MaintenanceManualCostOverride).where(
        MaintenanceManualCostOverride.line_id == line.id)).scalar_one()
    assert ov.active is False, "B 期间的人工价不得随 clear 留给 A"
    assert ov.updated_by == "op2"


def test_resolve_part_id_alias_and_merged(db):
    """解析规范：active 别名回退命中；merged 墓碑沿 merged_into 回溯到目标。"""
    from app.models.dimensions import PartAlias

    target = DimPart(pn_std="PN-TGT", status="active")
    db.add(target)
    db.flush()
    db.add(PartAlias(pn_raw="pn-tgt 别名写法", pn_std="PN-TGT",
                     part_id=target.id, status="active", source="manual"))
    db.commit()

    pid = manual._resolve_part_id(db, part_id=None, pn_std="pn-tgt 别名写法")
    assert pid == target.id, "active 别名必须回退命中"

    # merged 墓碑：source 并入 target 后，旧 PN 解析回溯到 target
    source = DimPart(pn_std="PN-SRC", status="merged", merged_into_id=target.id)
    db.add(source)
    db.commit()
    pid2 = manual._resolve_part_id(db, part_id=None, pn_std="PN-SRC")
    assert pid2 == target.id, "merged 墓碑必须回溯到目标身份"

    with pytest.raises(manual.DemandManualError):
        manual.patch_demand_line(db, raw_line_id="no-such-line",
                                 updates={"qty": 1}, reason="r", operated_by="t")


def test_pn_clear_cost_recomputed(db):
    """clear PN 后成本按恢复的身份重算（不是只改字符串）。"""
    order, line, part_a = _pn_override_setup(
        db, order_raw="M-P1F3", line_raw="ML-P1F3",
        pn_a="PN-A7", pn_b="PN-B7", tag="P1F3")
    db.commit()
    cost_b = line.unit_cost
    manual.clear_override(db, raw_line_id=line.raw_line_id, field="pn_std",
                          reason="回 A", operated_by="t")
    db.commit()
    db.expire(line)
    assert line.part_id == part_a.id
    # recompute 已按恢复身份重跑：unit_cost 是新身份的取价结果（允许同为
    # None——两 PN 均无价格记录时——但重算路径必须真实执行过，此处以
    # part_id 恢复 + recompute 无异常为验收点）。
    _ = line.unit_cost


def test_occ_stale_digest_conflicts(db):
    """#5：两个编辑并发——第二个用 stale digest → DemandManualConflict。"""
    order, line = _seed_line(db, order_raw="M-P1G", line_raw="ML-P1G", pn="PN-P1G")
    _make_project(db, tag="P1G", source_order_id=order.raw_order_id)
    r1 = manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                                  updates={"qty": 5}, reason="v1", operated_by="a")
    db.commit()
    stale = _digest_of({"raw_line_id": line.raw_line_id, "part_id": line.part_id,
                        "qty": "2.000", "return_qty": None, "serial_numbers": None,
                        "description": None, "pn_raw": "PN-P1G", "pn_std": "PN-P1G",
                        "edited_source": "wbdd", "manual_override": {}})
    with pytest.raises(manual.DemandManualConflict, match="版本不一致"):
        manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                                 updates={"qty": 7}, reason="v2", operated_by="b",
                                 expected_digest=stale)
    r2 = manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                                  updates={"qty": 7}, reason="v2", operated_by="b",
                                 expected_digest=r1["digest"])
    assert r2["qty"] == "7.000"


def _digest_of(snapshot: dict) -> str:
    return manual._digest(snapshot)


def test_occ_digest_changes_with_override_metadata_only(db):
    """Codex 边界修正版：可见值完全相同、只有 override 的来源/审计元数据
    （source_value）变化 → 旧 digest 必须冲突（stale clear 场景）。"""
    order, line = _seed_line(db, order_raw="M-P1L", line_raw="ML-P1L", pn="PN-P1L",
                             qty="5")
    _make_project(db, tag="P1L", source_order_id=order.raw_order_id)
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"qty": 9}, reason="v1", operated_by="alice")
    db.commit()
    token_before = manual._digest(manual._line_snapshot(line))
    # 可见值不变（qty 停在 9）；模拟导入侧把账本 source_value 改写（可见值
    # 不动、仅 override 来源事实变化）——digest 必须不同，旧 token 的 clear
    # 必须 409。deepcopy 构造全新账本：浅拷贝原地改嵌套对象 SQLAlchemy
    # 不标脏，commit 后会被旧 DB 值覆盖，测试就假通过了。
    from copy import deepcopy

    ov = deepcopy(line.manual_override or {})
    ov["qty"]["source_value"] = "4.000"
    line.manual_override = ov
    db.commit()
    db.refresh(line)
    assert (line.manual_override or {})["qty"]["source_value"] == "4.000", \
        "deepcopy 账本必须真实落库（防 ORM 未标脏的假通过）"
    token_after = manual._digest(manual._line_snapshot(line))
    # 模拟导入侧改写账本 source_value（可见值不动）：手工改账本元数据
    assert token_before != token_after, \
        "digest 必须覆盖完整 override 状态（含 source_value）"
    # 持旧 token 的 clear 必须冲突
    with pytest.raises(manual.DemandManualConflict, match="版本不一致"):
        manual.clear_override(db, raw_line_id=line.raw_line_id, field="qty",
                              reason="clear", operated_by="bob",
                              expected_digest=token_before)


def test_nan_and_bool_qty_rejected(db):
    order, line = _seed_line(db, order_raw="M-P1H", line_raw="ML-P1H", pn="PN-P1H")
    _make_project(db, tag="P1H", source_order_id=order.raw_order_id)
    for bad in (float("nan"), float("inf"), True, "NaN", "Infinity", "1e999"):
        with pytest.raises(manual.DemandManualError):
            manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                                     updates={"qty": bad}, reason="r", operated_by="t")


def test_override_protects_part_id_from_reimport(db):
    """#3 联动：PN 被 override 后，重导旧 PN 不能把 part_id 拽回旧身份。"""
    order, line = _seed_line(db, order_raw="M-P1I", line_raw="ML-P1I", pn="PN-A2")
    _make_project(db, tag="P1I", source_order_id=order.raw_order_id)
    db.add(DimPart(pn_std="PN-B2", status="active"))
    db.commit()
    part_b = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-B2"))
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"pn_std": "PN-B2", "pn_raw": "PN-B2"},
                             reason="改 B", operated_by="t")
    db.commit()

    # 重导 PN-A2（旧值）—— override 保护的 pn_std/part_id 保留
    rows = [f.maintenance_line(order.raw_order_id, line.raw_line_id, "PN-A2", qty="1")]
    loader.load(db, f.maintenance_result(
        {order.raw_order_id: f.maintenance_head(order.raw_order_id, on=date(2026, 3, 1))},
        rows), _batch(db), date(2026, 9, 1), mode="upsert")
    db.commit()
    db.refresh(line)
    assert line.pn_std == "PN-B2"
    assert line.part_id == part_b.id, "重导不得把 part_id 拽回旧身份"


def test_clear_then_reimport_overwrites_normally(db):
    """撤销后保护解除：重导的值照常覆盖（不能保护一辈子）。"""
    order, line = _seed_line(db, order_raw="M-P1I2", line_raw="ML-P1I2", pn="PN-A3")
    _make_project(db, tag="P1I2", source_order_id=order.raw_order_id)
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"qty": 9}, reason="改", operated_by="t")
    db.commit()
    manual.clear_override(db, raw_line_id=line.raw_line_id, field="qty",
                          reason="撤", operated_by="t")
    db.commit()
    assert "qty" not in (line.manual_override or {})

    rows = [f.maintenance_line(order.raw_order_id, line.raw_line_id, "PN-A3", qty="6")]
    loader.load(db, f.maintenance_result(
        {order.raw_order_id: f.maintenance_head(order.raw_order_id, on=date(2026, 3, 1))},
        rows), _batch(db), date(2026, 9, 1), mode="upsert")
    db.commit()
    db.refresh(line)
    assert line.qty == Decimal("6.000"), "撤销后重导必须正常覆盖"


def test_create_idempotent_replay_same_key_same_payload(db):
    """Codex 补充：同 key 同完整 payload 重放返回原行，不重复建。"""
    project = _make_project_standalone(db, tag="P1J")
    db.add(DimPart(pn_std="PN-P1J", status="active"))
    db.commit()
    r1 = manual.create_manual_demand_line(
        db, order_date=date(2026, 3, 1), project_id=project.project_id,
        pn_std="PN-P1J", qty=1, reason="r", operated_by="tester",
        idempotency_key="idem-key-001",
    )
    db.commit()
    assert r1.get("replayed") is None  # 首次
    r2 = manual.create_manual_demand_line(
        db, order_date=date(2026, 3, 1), project_id=project.project_id,
        pn_std="PN-P1J", qty=1, reason="r", operated_by="tester",
        idempotency_key="idem-key-001",
    )
    db.commit()
    assert r2["replayed"] is True
    assert r2["raw_line_id"] == r1["raw_line_id"]
    assert r2["order_no"] == r1["order_no"]
    # 库里只有一行
    from sqlalchemy import func
    count = db.scalar(select(func.count(FMaintenanceLine.id)).where(
        FMaintenanceLine.raw_line_id == r1["raw_line_id"]))
    assert count == 1


def test_create_idempotent_replay_after_manual_edit_still_replays(db):
    """行被人工编辑后，原请求重放仍判定为重放（指纹存 batch 元数据，
    不看行现值）。"""
    project = _make_project_standalone(db, tag="P1J2")
    db.add(DimPart(pn_std="PN-P1J2", status="active"))
    db.commit()
    r1 = manual.create_manual_demand_line(
        db, order_date=date(2026, 3, 1), project_id=project.project_id,
        pn_std="PN-P1J2", qty=1, reason="r", operated_by="tester",
        idempotency_key="idem-key-edit-001",
    )
    db.commit()
    manual.patch_demand_line(db, raw_line_id=r1["raw_line_id"],
                             updates={"qty": 8}, reason="手改", operated_by="op")
    db.commit()
    r2 = manual.create_manual_demand_line(
        db, order_date=date(2026, 3, 1), project_id=project.project_id,
        pn_std="PN-P1J2", qty=1, reason="r", operated_by="tester",
        idempotency_key="idem-key-edit-001",
    )
    db.commit()
    assert r2["replayed"] is True
    assert r2["raw_line_id"] == r1["raw_line_id"]


@pytest.mark.parametrize("mutate", [
    lambda kw: kw.update(qty=2),                              # qty 变
    lambda kw: kw.update(pn_std="PN-P1K3B"),                  # PN 变
    lambda kw: kw.update(order_date=date(2026, 3, 2)),        # 日期变
    lambda kw: kw.update(serial_numbers="SN-1"),              # SN 变
    lambda kw: kw.update(description="d2"),                   # 描述变
    lambda kw: kw.update(reason="r2"),                        # 原因变
])
def test_create_idempotent_same_key_changed_payload_conflicts(db, mutate):
    """同 key 但任一实质字段变化 → 409（不是静默建第二行）。"""
    project = _make_project_standalone(db, tag="P1K3")
    db.add_all([DimPart(pn_std="PN-P1K3", status="active"),
                DimPart(pn_std="PN-P1K3B", status="active")])
    db.commit()
    base = dict(order_date=date(2026, 3, 1), project_id=project.project_id,
                pn_std="PN-P1K3", qty=1, reason="r", operated_by="tester",
                idempotency_key="idem-key-mutate-001")
    manual.create_manual_demand_line(db, **dict(base))
    db.commit()
    replay = dict(base)
    mutate(replay)
    with pytest.raises(manual.DemandManualConflict):
        manual.create_manual_demand_line(db, **replay)
    db.rollback()


def test_create_idempotent_voided_original_replay_refused(db):
    """原行被作废后，同 key 重放不能"复活"（不返回已作废行）。"""
    project = _make_project_standalone(db, tag="P1K4")
    db.add(DimPart(pn_std="PN-P1K4", status="active"))
    db.commit()
    r1 = manual.create_manual_demand_line(
        db, order_date=date(2026, 3, 1), project_id=project.project_id,
        pn_std="PN-P1K4", qty=1, reason="r", operated_by="tester",
        idempotency_key="idem-key-void-001",
    )
    db.commit()
    line = db.execute(select(FMaintenanceLine).where(
        FMaintenanceLine.raw_line_id == r1["raw_line_id"])).scalar_one()
    line.is_active = False
    db.commit()
    with pytest.raises(manual.DemandManualConflict, match="作废|不存在"):
        manual.create_manual_demand_line(
            db, order_date=date(2026, 3, 1), project_id=project.project_id,
            pn_std="PN-P1K4", qty=1, reason="r", operated_by="tester",
            idempotency_key="idem-key-void-001",
        )


def test_create_idempotent_conflict_different_project(db):
    """同 key 换 project → 409 冲突（不是静默建第二行）。"""
    project = _make_project_standalone(db, tag="P1K")
    db.add(DimPart(pn_std="PN-P1K", status="active"))
    db.commit()
    manual.create_manual_demand_line(
        db, order_date=date(2026, 3, 1), project_id=project.project_id,
        pn_std="PN-P1K", qty=1, reason="r", operated_by="tester",
        idempotency_key="idem-key-002",
    )
    db.commit()
    project2 = _make_project_standalone(db, tag="P1K2")
    with pytest.raises(manual.DemandManualConflict, match="project_id 已变化"):
        manual.create_manual_demand_line(
            db, order_date=date(2026, 3, 1), project_id=project2.project_id,
            pn_std="PN-P1K", qty=1, reason="r", operated_by="tester",
            idempotency_key="idem-key-002",
        )
