"""M3-5：对账恒等式（plan v1.3 §2.5）——精确相等，不允许 ±2%。"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.etl import mapping
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.services import maintenance_boss_board as board
from tests.boss_board_helpers import (
    assign,
    boss_client,
    import_wbdd,
    make_project,
    set_costs,
)


@pytest.fixture(autouse=True)
def _flag_on():
    settings = get_settings()
    original = settings.maintenance_boss_dashboard_enabled
    settings.maintenance_boss_dashboard_enabled = True
    try:
        yield
    finally:
        settings.maintenance_boss_dashboard_enabled = original


def test_status_columns_never_enter_aggregation(db):
    """铁律 3 锁死：聚合白名单与 28 流转状态列（+头级自报四列）交集为空。"""
    assert board.AGGREGATE_SOURCE_COLUMNS & board.STATUS_ONLY_COLUMNS == frozenset()
    # 白名单确实覆盖了需求侧数量与成本列
    assert {"qty", "return_qty", "cost_amount_inc_tax"} <= board.AGGREGATE_SOURCE_COLUMNS
    # 状态列域与 mapping 定义一致
    assert set(mapping.MAINTENANCE_LINE_DISPLAY_FIELDS) <= board.STATUS_ONLY_COLUMNS
    for status_col in ("supplied_qty", "consumed_qty", "purchased_qty",
                       "pending_return_qty", "head_shipped_qty"):
        assert status_col not in board.AGGREGATE_SOURCE_COLUMNS


def test_projects_plus_unassigned_equals_global_cohort(db, tmp_path):
    """母集恒等式：Σ各项目 + 未归属桶 = 全局有效 WBDD 母集（精确相等）。"""
    proj_a, proj_b = make_project(db, "项目A"), make_project(db, "项目B")
    orders = import_wbdd(db, tmp_path, orders=5, lines_per_order=2)
    assign(db, orders[0], proj_a)
    assign(db, orders[1], proj_a)
    assign(db, orders[2], proj_b)
    # orders[3], orders[4] 留未归属
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/projects",
                      params={"from": "2026-01-01", "to": "2026-12-31"}).json()
    sum_orders = sum(r["orders_ytd"]["value"] for r in body["rows"])
    sum_lines = sum(r["lines_ytd"]["value"] for r in body["rows"])
    global_orders = db.execute(
        select(func.count(FMaintenanceOrder.id))).scalar_one()
    global_lines = db.execute(
        select(func.count(FMaintenanceLine.id))).scalar_one()
    assert sum_orders == global_orders == 5
    assert sum_lines == global_lines == 10
    bucket = next(r for r in body["rows"]
                  if r["project_id"] == board.UNASSIGNED_BUCKET)
    assert bucket["orders_ytd"]["value"] == 2


def test_summary_counts_match_direct_window_count(db, tmp_path):
    import_wbdd(db, tmp_path, orders=3, lines_per_order=2)
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/summary",
                      params={"from": "2026-01-01", "to": "2026-12-31"}).json()
    lo, hi = date(2026, 1, 1), date(2026, 12, 31)
    direct_orders = db.execute(
        select(func.count(FMaintenanceOrder.id))
        .where(FMaintenanceOrder.order_date.between(lo, hi))
    ).scalar_one()
    direct_lines = db.execute(
        select(func.count(FMaintenanceLine.id))
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .where(FMaintenanceOrder.order_date.between(lo, hi))
    ).scalar_one()
    assert body["orders_ytd"]["value"] == direct_orders == 3
    assert body["lines_ytd"]["value"] == direct_lines == 6


def test_cost_bundle_internal_identities(db, tmp_path):
    """成本五件套内部恒等：actual+estimated=known；三类行数之和=总行数；
    coverage_pct 口径与 maintenance_cost.py:972-974 一致。"""
    import_wbdd(db, tmp_path, orders=2, lines_per_order=2)
    lines = db.execute(select(FMaintenanceLine)).scalars().all()
    # 2 实际 / 1 估算 / 1 缺价
    lines[0].cost_source, lines[0].cost_amount_inc_tax = "direct", Decimal("100")
    lines[1].cost_source, lines[1].cost_amount_inc_tax = "window", Decimal("50")
    lines[2].cost_source, lines[2].cost_amount_inc_tax = "pool_sales", Decimal("25")
    lines[3].cost_source, lines[3].cost_amount_inc_tax = "none", None
    db.commit()
    client = boss_client(db)
    value = client.get("/api/maintenance/boss-board/summary",
                       params={"from": "2026-01-01", "to": "2026-12-31"}
                       ).json()["known_apply_cost_inc_tax"]["value"]
    actual = Decimal(str(value["actual_amount"]))
    estimated = Decimal(str(value["estimated_amount"]))
    known = Decimal(str(value["known_amount"]))
    assert actual == Decimal("150")
    assert estimated == Decimal("25")
    assert actual + estimated == known                     # 恒等式 1
    assert value["missing_lines"] == 1                      # 恒等式 2（3 已知 + 1 缺）
    assert value["coverage_pct"] == round(3 / 4 * 100, 1)   # 恒等式 3
    assert value["quality"] == "incomplete"                 # 缺价 → 已知下限语义


def test_project_cost_sums_equal_global_cost(db, tmp_path):
    """项目级成本之和（含未归属桶）= 全局成本，精确相等。"""
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=3, lines_per_order=1)
    assign(db, orders[0], proj)
    set_costs(db, source="direct", amount="100.00")
    client = boss_client(db)
    rows = client.get("/api/maintenance/boss-board/projects",
                      params={"from": "2026-01-01", "to": "2026-12-31"}
                      ).json()["rows"]
    per_project = sum(Decimal(str(r["known_apply_cost_inc_tax"]["value"]["known_amount"]))
                      for r in rows)
    summary = client.get("/api/maintenance/boss-board/summary",
                         params={"from": "2026-01-01", "to": "2026-12-31"}).json()
    global_known = Decimal(
        str(summary["known_apply_cost_inc_tax"]["value"]["known_amount"]))
    assert per_project == global_known == Decimal("300.00")
