"""维保出库成本引擎 + 导入识别/转换测试（docs/维保出库成本核算-开发方案.md §14）。

取价瀑布五层各一例、含税优先、追溯上限、占位 PN 排除、退货冲抵、起算日作用域、
幂等与 upsert 不冲成本、文件识别与列名容差、transform 基础路径。
"""
from datetime import date
from decimal import Decimal
import uuid

import pandas as pd
import pytest
from sqlalchemy import select

from app.etl import loader, mapping
from app.etl.transform import transform
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceManualCostOverride,
)
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch
from app.services import (
    maintenance_cost,
    maintenance_cost_quality,
    maintenance_workbook_renderer,
)
from tests import factories as f


@pytest.fixture()
def batch(db):
    b = SysImportBatch(
        filename="t.xlsx",
        file_type="maintenance",
        file_hash="hm1",
        status="success",
    )
    db.add(b)
    db.flush()
    return b


def _load_purchases(db, b, orders, lines):
    loader.load(db, f.purchase_result(orders, lines), b.id, date(2026, 6, 1))


def _load_maintenance(db, b, orders, lines, mode="skip"):
    loader.load(db, f.maintenance_result(orders, lines), b.id, date(2026, 6, 1), mode=mode)


def _line(db, raw_line_id) -> FMaintenanceLine:
    return db.execute(select(FMaintenanceLine)
                      .where(FMaintenanceLine.raw_line_id == raw_line_id)).scalar_one()


# ---------- 瀑布五层 ----------

def test_direct_hit_weighted(db, batch):
    """A0 直配：同 WBDD 同 PN 多行按数量加权；记来源采购单号。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", order_no="CGDD-1", on=date(2026, 3, 5),
                              source_type="维保需求", linked_maintenance_order_no="WBDD-1"),
    }, [
        f.purchase_line("P1", "PL1", "PN-A", qty="1", price="100"),
        f.purchase_line("P1", "PL2", "PN-A", qty="3", price="200"),
    ])
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", order_no="WBDD-1",
                                                           on=date(2026, 3, 10))},
                      [f.maintenance_line("M1", "ML1", "PN-A", qty="2")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["direct"] == 1
    ln = _line(db, "ML1")
    assert ln.cost_source == "direct"
    assert ln.unit_cost == Decimal("175.00")          # (100+600)/4
    assert ln.cost_amount == Decimal("350.00")
    assert ln.trace_months == 0
    assert ln.linked_purchase_order_no == "CGDD-1"
    assert ln.cost_tax_basis == "ex"                   # is_tax_inclusive=None → ex
    assert ln.cost_bucket == maintenance_cost_quality.COST_BUCKET_ACTUAL_EX


def test_month_avg(db, batch):
    """A2 当月加权：无直配/±7天窗口命中时取同 part 当月均价（跨采购单加权）。

    v2：两笔采购都放在出库日 ±7 天窗口之外（13/15 天），确保不落 window 层。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", on=date(2026, 3, 2)),
        "P2": f.purchase_head("P2", on=date(2026, 3, 30)),
    }, [
        f.purchase_line("P1", "PL1", "PN-B", qty="1", price="90"),
        f.purchase_line("P2", "PL2", "PN-B", qty="1", price="110"),
    ])
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", on=date(2026, 3, 15))},
                      [f.maintenance_line("M1", "ML1", "PN-B", qty="1")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["month_avg"] == 1
    ln = _line(db, "ML1")
    assert ln.cost_source == "month_avg"
    assert ln.unit_cost == Decimal("100.00")
    assert ln.price_month == "2026-03"
    assert ln.trace_months == 0


def test_three_month_reference_priority(db, batch):
    """前三层未命中后，3 个月内按本 PN 采购→销售历史补价。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", on=date(2026, 1, 10)),   # PN-C：3 月出库 → 追溯 2 个月
        "P2": f.purchase_head("P2", on=date(2025, 10, 10)),  # PN-D：5 个月前 → 超上限
    }, [
        f.purchase_line("P1", "PL1", "PN-C", qty="1", price="50"),
        f.purchase_line("P2", "PL2", "PN-D", qty="1", price="70"),
    ])
    sb = SysImportBatch(
        filename="s.xlsx",
        file_type="sales",
        file_hash="hs1",
        status="success",
    )
    db.add(sb)
    db.flush()
    loader.load(db, f.sales_result(
        {"S1": f.sales_head("S1", on=date(2026, 3, 5), business_type="备件销售")},
        [f.sales_line("S1", "SL1", "PN-D", qty="1", price="88")]), sb.id, date(2026, 6, 1))
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", on=date(2026, 3, 15))}, [
        f.maintenance_line("M1", "ML1", "PN-C", qty="1"),
        f.maintenance_line("M1", "ML2", "PN-D", qty="1"),
    ])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["trace_avg"] == 0 and stats["sales_ref"] == 0
    assert stats["purchase_history"] == 1 and stats["sales_history"] == 1
    c = _line(db, "ML1")
    assert (c.cost_source, c.trace_months, c.price_month) == (
        "purchase_history",
        2,
        "2026-01",
    )
    assert c.unit_cost == Decimal("50.00")
    assert c.cost_bucket == maintenance_cost_quality.COST_BUCKET_ESTIMATED_EX_LOW
    d = _line(db, "ML2")
    assert d.cost_source == "sales_history"  # 采购超 3 个月后使用 3 个月内销售
    assert d.unit_cost == Decimal("88.00")
    assert d.cost_tax_basis == "inc"  # 销售原始单价恒为含税，原始税率只留审计


def test_none_no_cost_flag(db, batch):
    """D 无成本：采购/销售皆无 → none + no_cost 标记。"""
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", on=date(2026, 3, 1))},
                      [f.maintenance_line("M1", "ML1", "PN-NONE", qty="1")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["none"] == 1
    ln = _line(db, "ML1")
    assert ln.cost_source == "none"
    assert ln.unit_cost is None and ln.cost_amount is None
    assert "no_cost" in ln.anomaly_flags


def test_legacy_contract_workbook_merges_unsynced_active_manual_override(
    db,
    batch,
):
    """历史 override 未镜像进主行时，旧合同工作簿仍展示同一份成本事实。"""
    contract = "XS-MANUAL-WORKBOOK"
    _load_maintenance(
        db,
        batch,
        {
            "M-MANUAL-WORKBOOK": f.maintenance_head(
                "M-MANUAL-WORKBOOK",
                order_no="WBDD-MANUAL-WORKBOOK",
                on=date(2026, 3, 1),
                sales_order=contract,
            ),
        },
        [
            f.maintenance_line(
                "M-MANUAL-WORKBOOK",
                "ML-MANUAL-WORKBOOK",
                "PN-MANUAL-WORKBOOK",
                qty="5",
                return_qty="2",
            ),
        ],
    )
    db.commit()
    maintenance_cost.recompute(db)
    line = _line(db, "ML-MANUAL-WORKBOOK")
    assert line.cost_source == "none"
    assert line.cost_amount is None
    db.add(MaintenanceManualCostOverride(
        line_id=line.id,
        unit_cost_ex_tax=Decimal("10.00"),
        unit_cost_inc_tax=Decimal("11.30"),
        reason="历史人工成本依据",
        active=True,
        updated_by="test",
    ))
    db.commit()

    data = maintenance_cost.contract_workbook_data(db, contract)

    # 读取链只合并事实视图，不偷偷改写生产主行。
    db.refresh(line)
    assert line.cost_source == "none"
    assert line.cost_amount is None
    assert data["doc_total"]["WBDD-MANUAL-WORKBOOK"] == Decimal("30.00")
    assert data["monthly_parts"]["2026-03"] == Decimal("30.00")
    assert data["cost_summary"]["actual_cost_ex"] == Decimal("30.00")
    assert data["cost_summary"]["known_cost_total"] == Decimal("30.00")
    assert data["cost_summary"]["cost_quality"] == "actual_only"
    assert data["dual_cost_summary"]["parts_cost_inc_tax"] == Decimal("33.90")
    assert data["dual_cost_summary"]["parts_cost_ex_tax"] == Decimal("30.00")
    assert data["line_cost_tiers"][line.id] == "actual"
    assert data["line_cost_display"][line.id] == {
        "tier": "actual",
        "inc_tier": "actual",
        "ex_tier": "actual",
        "source": "manual",
        "tax_basis": "ex",
        "confidence": "high",
        "unit_cost": Decimal("10.00"),
        "cost_amount": Decimal("30.00"),
        "unit_cost_inc_tax": Decimal("11.30"),
        "unit_cost_ex_tax": Decimal("10.00"),
        "cost_amount_inc_tax": Decimal("33.90"),
        "cost_amount_ex_tax": Decimal("30.00"),
        "anomaly_flags": ["has_return"],
        "manual_fallback": True,
    }
    detail = maintenance_cost.project_lines(db, "测试维保项目")["rows"][0]
    assert detail["cost_source"] == "manual"
    assert detail["cost_tax_basis"] == "ex"
    assert detail["cost_tier"] == "actual"
    assert detail["cost_amount"] == 30.0
    assert detail["cost_amount_inc_tax"] == 33.9
    assert detail["cost_amount_ex_tax"] == 30.0
    assert detail["anomaly_flags"] == ["has_return"]

    workbook = maintenance_workbook_renderer.render_contract_workbook(
        contract,
        data,
        lambda value: value,
    )
    try:
        row = workbook["备件明细-氚云"][2]
        assert row[12].value == 30
        assert row[16].value == 10
        assert row[17].value == 30
        assert row[18].value == 11.3
        assert row[19].value == 10
        assert row[20].value == 33.9
        assert row[21].value == 30
        assert row[22].value == "实际·人工回填"
        assert row[23].value == "高"
        assert row[26].value == "ex"
        assert row[27].value == "实际采购参考"
    finally:
        workbook.close()


# ---------- 口径细节 ----------

def test_tax_preference_inc_first(db, batch):
    """Q4：同一取价日含税/不含税并存 → 优先含税并标注；仅不含税 → ex。

    v2：日期近优先于税口径（window 层），税偏好只在同一取价日内取舍 → 两单同日验证。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", on=date(2026, 3, 2), is_tax_inclusive=True,
                              tax_rate=Decimal("0.13")),
        "P2": f.purchase_head("P2", on=date(2026, 3, 2)),
    }, [
        f.purchase_line("P1", "PL1", "PN-E", qty="1", price="113"),
        f.purchase_line("P2", "PL2", "PN-E", qty="1", price="100"),
        f.purchase_line("P2", "PL3", "PN-F", qty="1", price="60"),
    ])
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", on=date(2026, 3, 9))}, [
        f.maintenance_line("M1", "ML1", "PN-E", qty="1"),
        f.maintenance_line("M1", "ML2", "PN-F", qty="1"),
    ])
    db.commit()
    maintenance_cost.recompute(db)
    e = _line(db, "ML1")
    assert (e.unit_cost, e.cost_tax_basis) == (Decimal("113.00"), "inc")
    fl = _line(db, "ML2")
    assert (fl.unit_cost, fl.cost_tax_basis) == (Decimal("60.00"), "ex")


def test_placeholder_pn_excluded_from_pool(db, batch):
    """「一批备件」打包占位不进价格池（2,333 万实测教训）。"""
    _load_purchases(db, batch,
                    {"P1": f.purchase_head("P1", on=date(2026, 3, 2))},
                    [f.purchase_line("P1", "PL1", "一批备件", qty="1", price="999999")])
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", on=date(2026, 3, 9))},
                      [f.maintenance_line("M1", "ML1", "一批备件", qty="1")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["none"] == 1
    assert _line(db, "ML1").unit_cost is None


def test_return_qty_offsets_cost(db, batch):
    """退货冲抵：cost_amount=(qty-return_qty)×单价，标 has_return；退超发按 0 计。"""
    _load_purchases(db, batch, {"P1": f.purchase_head("P1", on=date(2026, 3, 2))},
                    [f.purchase_line("P1", "PL1", "PN-G", qty="1", price="100")])
    _load_maintenance(db, batch, {"M1": f.maintenance_head("M1", on=date(2026, 3, 9))}, [
        f.maintenance_line("M1", "ML1", "PN-G", qty="5", return_qty="2"),
        f.maintenance_line("M1", "ML2", "PN-G", qty="1", return_qty="3"),
    ])
    db.commit()
    maintenance_cost.recompute(db)
    a = _line(db, "ML1")
    assert a.cost_amount == Decimal("300.00") and "has_return" in a.anomaly_flags
    b = _line(db, "ML2")
    assert b.cost_amount == Decimal("0.00")


def test_start_date_scope(db, batch):
    """起算日（2024-01-01）前的出库不计价：cost_source 恒 NULL，区别于 none。"""
    _load_purchases(db, batch, {"P1": f.purchase_head("P1", on=date(2023, 12, 5))},
                    [f.purchase_line("P1", "PL1", "PN-H", qty="1", price="100")])
    _load_maintenance(db, batch,
                      {"M1": f.maintenance_head("M1", on=date(2023, 12, 20))},
                      [f.maintenance_line("M1", "ML1", "PN-H", qty="1")])
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["out_of_scope"] == 1 and stats["lines_in_scope"] == 0
    ln = _line(db, "ML1")
    assert ln.cost_source is None and ln.unit_cost is None


def test_recompute_idempotent_and_upsert_preserves_cost(db, batch):
    """重复 recompute 结果稳定；维保文件 upsert 重导不冲成本字段（白名单排除）。"""
    orders = {"M1": f.maintenance_head("M1", order_no="WBDD-9", on=date(2026, 3, 9))}
    lines = [f.maintenance_line("M1", "ML1", "PN-I", qty="2")]
    _load_purchases(db, batch, {"P1": f.purchase_head("P1", on=date(2026, 3, 2))},
                    [f.purchase_line("P1", "PL1", "PN-I", qty="1", price="42")])
    _load_maintenance(db, batch, orders, lines)
    db.commit()
    s1 = maintenance_cost.recompute(db)
    first = (_line(db, "ML1").unit_cost, _line(db, "ML1").cost_source)
    # upsert 重导同一批明细：成本回填字段必须保留
    _load_maintenance(db, batch, orders, lines, mode="upsert")
    db.commit()
    ln = _line(db, "ML1")
    assert (ln.unit_cost, ln.cost_source) == first
    s2 = maintenance_cost.recompute(db)
    assert s1 == s2
    assert (_line(db, "ML1").unit_cost, _line(db, "ML1").cost_source) == first


def test_bounded_recompute_only_invalidates_requested_lines(db, batch):
    """工作簿单行改 PN 不能顺带重写其他项目/其他行的派生成本。"""
    _load_purchases(db, batch, {
        "P1": f.purchase_head("P1", on=date(2026, 3, 2)),
    }, [
        f.purchase_line("P1", "PL1", "PN-BOUND-A", qty="1", price="40"),
        f.purchase_line("P1", "PL2", "PN-BOUND-B", qty="1", price="80"),
    ])
    _load_maintenance(db, batch, {
        "M1": f.maintenance_head("M1", on=date(2026, 3, 9)),
        "M2": f.maintenance_head("M2", on=date(2026, 3, 9)),
    }, [
        f.maintenance_line("M1", "ML-BOUND-A", "PN-BOUND-A", qty="1"),
        f.maintenance_line("M2", "ML-BOUND-B", "PN-BOUND-B", qty="1"),
    ])
    db.commit()
    maintenance_cost.recompute(db)
    target = _line(db, "ML-BOUND-A")
    unrelated = _line(db, "ML-BOUND-B")

    target.unit_cost = Decimal("999.00")
    target.cost_amount = Decimal("999.00")
    unrelated.unit_cost = Decimal("777.00")
    unrelated.cost_amount = Decimal("777.00")
    db.commit()

    stats = maintenance_cost.recompute(db, line_ids={target.id})
    db.expire_all()
    assert stats["lines_in_scope"] == 1
    assert _line(db, "ML-BOUND-A").unit_cost == Decimal("40.00")
    assert _line(db, "ML-BOUND-B").unit_cost == Decimal("777.00")


# ---------- 项目聚合 ----------

def test_projects_aggregate_shared_contract(db, batch):
    """项目聚合：税口径分列小计、来源分布、共用合同标记（Q5）。"""
    sb = SysImportBatch(
        filename="s.xlsx",
        file_type="sales",
        file_hash="hs2",
        status="success",
    )
    db.add(sb)
    db.flush()
    loader.load(db, f.sales_result(
        {"S1": {**f.sales_head("S1", order_no="XSDD-1", on=date(2026, 1, 5),
                               business_type="整体维保"),
                "amount_ex_tax": Decimal("1000"), "tax_rate": Decimal("0.06")}},
        [f.sales_line("S1", "SL1", "整体维保专用", qty="1", price="1060")]),
        sb.id, date(2026, 6, 1))
    _load_purchases(db, batch, {"P1": f.purchase_head("P1", on=date(2026, 3, 2))},
                    [f.purchase_line("P1", "PL1", "PN-J", qty="1", price="100")])
    _load_maintenance(db, batch, {
        "M1": f.maintenance_head("M1", on=date(2026, 3, 9), project="项目甲",
                                 sales_order="XSDD-1"),
        "M2": f.maintenance_head("M2", on=date(2026, 3, 12), project="项目乙",
                                 sales_order="XSDD-1"),
    }, [
        f.maintenance_line("M1", "ML1", "PN-J", qty="2"),
        f.maintenance_line("M2", "ML2", "PN-J", qty="1"),
        f.maintenance_line("M2", "ML3", "PN-NOPRICE", qty="1"),
    ])
    db.commit()
    maintenance_cost.recompute(db)
    data = maintenance_cost.projects_aggregate(db, lifecycle="all")
    rows = {r["project"]: r for r in data["rows"]}
    # 原始采购价是未税实际价；成本域按既有统一 13% 业务政策确定性生成双口径，
    # 因而两侧都是 actual，不能再用 0 伪装成“没有含税成本”。合同额不复用此规则。
    assert rows["项目甲"]["cost_ex"] == 200.0
    assert rows["项目甲"]["cost_inc"] == 226.0
    assert rows["项目甲"]["parts_cost_ex_tax_quality"] == "actual_only"
    assert rows["项目甲"]["parts_cost_inc_tax_quality"] == "actual_only"
    assert rows["项目甲"]["actual_cost_inc"] == 226.0
    assert rows["项目甲"]["estimated_cost_inc"] == 0.0
    assert rows["项目甲"]["coverage_pct"] == 100.0
    assert rows["项目乙"]["by_source"]["none"] == 1
    assert rows["项目乙"]["coverage_pct"] == 50.0
    # 同一 XSDD 挂两个项目 → 共用标记；无当前合同台账不得从销售未税额猜含税。
    assert rows["项目甲"]["contract_shared"] and rows["项目乙"]["contract_shared"]
    assert rows["项目甲"]["contract_amount"] is None
    assert rows["项目甲"]["contract_incomplete"] is True


def test_projects_aggregate_reads_live_current_contract_amount(db, batch):
    _load_maintenance(db, batch, {
        "M1": f.maintenance_head(
            "M1", on=date(2026, 3, 9), project="合同改单项目",
            sales_order="XSDD-LIVE-CONTRACT",
        ),
    }, [f.maintenance_line("M1", "ML-LIVE-CONTRACT", "PN-LIVE", qty="1")])
    order = db.scalar(select(FMaintenanceOrder).where(FMaintenanceOrder.order_no == "M1"))
    project = MaintenanceProject(
        project_id=str(uuid.uuid4()), project_code="LIVE-CONTRACT",
        display_name="合同改单项目", lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    relation = MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()), project_id=project.project_id,
        contract_id="LIVE-CONTRACT-ID", contract_no="XSDD-LIVE-CONTRACT",
        amount_inc_tax=Decimal("123.45"), included_in_total=True,
        status_mapping_state="mapped", status_mapping_version="v1",
        effective_from=date(2026, 1, 1), source="ledger", version=1,
    )
    db.add_all([
        relation,
        MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid.uuid4()), project_id=project.project_id,
            source_order_id=order.raw_order_id, is_active=True,
            created_by="test",
        ),
    ])
    db.commit()

    first = maintenance_cost.projects_aggregate(db, lifecycle="all")["rows"][0]
    assert first["contract_amount_inc_tax"] == 123.45
    assert first["contract_amount_basis"] == "inc_tax"
    assert first["contract_amount"] == 123.45
    relation.amount_inc_tax = Decimal("456.78")
    db.commit()
    second = maintenance_cost.projects_aggregate(db, lifecycle="all")["rows"][0]
    assert second["contract_amount_inc_tax"] == 456.78
    assert second["contract_amount_basis"] == "inc_tax"
    assert second["contract_amount"] == 456.78


def test_projects_aggregate_marks_missing_contract_fact_incomplete(db, batch):
    _load_maintenance(db, batch, {
        "M-NO-CONTRACT": f.maintenance_head(
            "M-NO-CONTRACT", on=date(2026, 3, 9),
            project="无合同事实项目", sales_order=None,
        ),
    }, [f.maintenance_line(
        "M-NO-CONTRACT", "ML-NO-CONTRACT", "PN-NO-CONTRACT", qty="1",
    )])
    db.commit()

    row = maintenance_cost.projects_aggregate(db, lifecycle="all")["rows"][0]
    assert row["contract_amount_inc_tax"] is None
    assert row["contract_amount_basis"] == "inc_tax"
    assert row["contract_amount"] is None
    assert row["contract_incomplete"] is True


def test_projects_aggregate_hides_partial_amount_from_explicit_and_alias_fields(
    db, batch, monkeypatch,
):
    """已知小计不能伪装成完整合同总额；新旧字段保持同一失败关闭语义。"""
    from app.services import maintenance_boss_board

    test_projects_aggregate_reads_live_current_contract_amount(db, batch)
    project = db.scalar(select(MaintenanceProject))
    monkeypatch.setattr(
        maintenance_boss_board,
        "_card_contracts",
        lambda _db, _project_ids: {
            project.project_id: {
                "contract_nos": ["XSDD-LIVE-CONTRACT"],
                "amount_inc_tax": Decimal("456.78"),
                "contract_shared": False,
                "contract_incomplete": True,
            }
        },
    )

    row = maintenance_cost.projects_aggregate(db, lifecycle="all")["rows"][0]
    assert row["contract_amount_inc_tax"] is None
    assert row["contract_amount_basis"] == "inc_tax"
    assert row["contract_amount"] is None
    assert row["contract_incomplete"] is True


def test_projects_aggregate_scoped_sales_cannot_traverse_project_contracts(
    db, batch, monkeypatch,
):
    from app import config, permissions
    from app.security import UserContext

    test_projects_aggregate_reads_live_current_contract_amount(db, batch)
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    graph = permissions.effective("sales", None)
    graph.update({
        "own_customers_only": True,
        "data_profit": True,
        "data_purchase_cost": True,
    })
    rows = maintenance_cost.projects_aggregate(
        db,
        lifecycle="all",
        user_ctx=UserContext(
            user_id="own-sales",
            role="sales",
            salesperson_name="测试销售",
            permissions=graph,
            is_authenticated=True,
        ),
    )["rows"]
    assert len(rows) == 1
    assert rows[0]["contract_amount_inc_tax"] is None
    assert rows[0]["contract_amount_basis"] == "inc_tax"
    assert rows[0]["contract_amount"] is None
    assert rows[0]["contract_incomplete"] is None


def test_contract_amounts_fail_closed_for_cross_project_shared_relation(db, batch):
    from app.models.maintenance import MaintenanceContractWorkbookState

    projects = [
        MaintenanceProject(
            project_id=str(uuid.uuid4()), project_code=f"SHARED-{index}",
            display_name=f"共享项目{index}", lifecycle_status="ongoing",
        )
        for index in range(2)
    ]
    db.add_all(projects)
    db.flush()
    for index, project in enumerate(projects):
        db.add(MaintenanceProjectContract(
            project_contract_id=str(uuid.uuid4()), project_id=project.project_id,
            contract_id="SHARED-CONTRACT-ID", contract_no="XSDD-SHARED-CANONICAL",
            amount_inc_tax=Decimal("1000.00"), included_in_total=True,
            status_mapping_state="mapped", status_mapping_version="v1",
            effective_from=date(2026, 1, 1), source="ledger", version=1,
        ))
    db.commit()

    assert maintenance_cost._contract_amounts(
        db, ["XSDD-SHARED-CANONICAL"]
    ) == {}
    _load_maintenance(db, batch, {
        "M-SHARED": f.maintenance_head(
            "M-SHARED", on=date(2026, 3, 9), project="共享项目0",
            sales_order="XSDD-SHARED-CANONICAL",
        ),
    }, [f.maintenance_line("M-SHARED", "ML-SHARED", "PN-SHARED", qty="1")])
    line = _line(db, "ML-SHARED")
    line.cost_source = "direct"
    line.cost_tax_basis = "inc"
    line.cost_amount = Decimal("10")
    line.cost_amount_inc_tax = Decimal("10")
    line.cost_amount_ex_tax = Decimal("8.85")
    db.add(MaintenanceContractWorkbookState(
        contract_no="XSDD-SHARED-CANONICAL",
        expense_snapshot_complete=True,
        expense_complete_through=date(2099, 1, 1),
        updated_by="test",
    ))
    db.commit()

    row = maintenance_cost.board(db, lifecycle="all")["rows"][0]
    assert row["budget"] is None
    assert row["decision_status"] == "no_budget"


# ---------- 导入识别 / 转换 ----------

_MAINT_COLS = [
    "数据ID(不可修改)", "需求单号", "制单日期", "销售订单", "项目名", "客户名称",
    "需求类型", "业务类型", "销售人员", "出库仓库(必填)", "维保起始日期", "维保终止日期",
    "数据状态", "需求明细.数据ID(不可修改)", "需求明细.序号", "需求明细.需供货产品",
    "需求明细.产品描述", "需求明细.需求数量", "需求明细.退货数量", "需求明细.发货SN",
]


def test_detect_maintenance_and_no_regression():
    assert mapping.detect_file_type(_MAINT_COLS) == "maintenance"
    # 列名容差：注解后缀变体经 canonicalize 后仍可识别
    variants = [c.replace("(必填)", "") for c in _MAINT_COLS]
    assert mapping.detect_file_type(mapping.canonicalize_columns(variants)) == "maintenance"
    # 原有类型零回归
    assert mapping.detect_file_type(["采购单号(必填)", "明细.产品名称(必填)"]) == "purchase"
    assert mapping.detect_file_type(["订单编号(必填)", "业务类型#"]) == "sales"
    assert mapping.detect_file_type(["产品库存ID", "库存数量"]) == "inventory"


def test_transform_maintenance_basics():
    """转换：头/行字段落位、项目名剥「预交付-」、草稿单空 PN 走软错误。"""
    rows = [
        {"数据ID(不可修改)": "RID1", "需求单号": "WBDD-1", "制单日期": "2026-03-01",
         "销售订单": "XSDD-1", "项目名": "预交付-某项目20260101", "客户名称": "客户A",
         "需求类型": "报修供货", "业务类型": "备件维保", "销售人员": "张三",
         "出库仓库(必填)": "北京成品仓", "维保起始日期": None, "维保终止日期": None,
         "数据状态": "已生效",
         "需求明细.数据ID(不可修改)": "LID1", "需求明细.序号": 1,
         "需求明细.需供货产品": "PN-1", "需求明细.产品描述": "硬盘",
         "需求明细.需求数量": 3, "需求明细.退货数量": 1, "需求明细.发货SN": "SN1"},
        {"数据ID(不可修改)": "RID2", "需求单号": "WBDD-2", "制单日期": "2026-03-02",
         "销售订单": None, "项目名": None, "客户名称": None,
         "需求类型": None, "业务类型": None, "销售人员": None,
         "出库仓库(必填)": None, "维保起始日期": None, "维保终止日期": None,
         "数据状态": "草稿",
         "需求明细.数据ID(不可修改)": "LID2", "需求明细.序号": 1,
         "需求明细.需供货产品": None, "需求明细.产品描述": None,
         "需求明细.需求数量": None, "需求明细.退货数量": None, "需求明细.发货SN": None},
    ]
    res = transform(pd.DataFrame(rows), mapping.MAINTENANCE)
    assert res.rows_total == 2 and len(res.lines) == 1
    head = res.orders["RID1"]
    assert head["project_std"] == "某项目20260101"      # 剥前缀
    assert head["project_raw"] == "预交付-某项目20260101"
    ln = res.lines[0]
    assert (ln["qty"], ln["return_qty"]) == (Decimal("3"), Decimal("1"))
    # 草稿单空 PN → 软错误（不计入 rows_error 口径由 loader 判定）
    assert res.errors and res.errors[0].error_type == "empty_pn_inactive"


def test_loader_report_counts(db, batch):
    """loader 报告：维保头/行入库计数与幂等 skip。"""
    orders = {"M1": f.maintenance_head("M1", on=date(2026, 3, 9))}
    lines = [f.maintenance_line("M1", "ML1", "PN-K", qty="1")]
    r1 = loader.load(db, f.maintenance_result(orders, lines), batch.id, date(2026, 6, 1))
    assert (r1["orders_inserted"], r1["fact_rows_inserted"]) == (1, 1)
    r2 = loader.load(db, f.maintenance_result(orders, lines), batch.id, date(2026, 6, 1))
    assert (r2["fact_rows_inserted"], r2["fact_rows_skipped"]) == (0, 1)
    n = db.execute(select(FMaintenanceOrder)).scalars().all()
    assert len(n) == 1
