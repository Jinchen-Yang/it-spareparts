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
    """铁律 3 锁死：聚合白名单与流转状态列交集为空。

    唯一豁免：`PROCURED_QTY_COLUMNS`（库房发货＋直采直发）——业务
    2026-08-16 在 REQUIREMENTS #41 明文指定「维保备件采购数」就是这两列之和。
    铁律 3 原文枚举的是「已采/待供/待返/领用」，这两列不在其中。本用例把豁免
    锁死为**恰好这两列**：任何人日后想再豁免第三列，这里会红。
    """
    assert board.PROCURED_QTY_COLUMNS == {"warehouse_shipped_qty", "direct_ship_qty"}
    assert board.AGGREGATE_SOURCE_COLUMNS & board.STATUS_ONLY_COLUMNS == frozenset()
    # 白名单确实覆盖了需求侧数量与成本列
    assert {"qty", "return_qty", "cost_amount_inc_tax"} <= board.AGGREGATE_SOURCE_COLUMNS
    # 状态列域＝mapping 定义减去被明文豁免的两列
    assert (set(mapping.MAINTENANCE_LINE_DISPLAY_FIELDS) - board.PROCURED_QTY_COLUMNS
            <= board.STATUS_ONLY_COLUMNS)
    # 铁律 3 原文点名的四类，以及头级自报列，永远不得入聚合
    for status_col in ("supplied_qty", "consumed_qty", "purchased_qty",
                       "pending_return_qty", "pending_supply_qty", "returned_qty",
                       "head_shipped_qty"):
        assert status_col not in board.AGGREGATE_SOURCE_COLUMNS
        assert status_col in board.STATUS_ONLY_COLUMNS


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


def test_aggregation_sql_never_references_status_columns():
    """铁律 3 的**真闸门**：编译聚合 SQL，断言其中不出现任何流转状态列。

    只断言 AGGREGATE_SOURCE_COLUMNS 与状态列集合不相交是不够的——那只证明两个
    常量不重叠，不证明真实查询遵守白名单。这里把每条聚合语句编译成 SQL 文本逐一
    检查，任何人日后在聚合里引用 supplied_qty/consumed_qty 之类都会立刻红。
    """
    from sqlalchemy.dialects import postgresql

    from app.services import maintenance_boss_facts as facts

    statements = {
        "ckd": facts._applied_ckd_lines(),
        "return_order": facts._applied_return_lines(),
        "rkd": facts._applied_rkd_lines(),
    }
    for name, stmt in statements.items():
        sql = str(stmt.compile(dialect=postgresql.dialect(),
                               compile_kwargs={"literal_binds": True}))
        for column in board.STATUS_ONLY_COLUMNS:
            assert column not in sql, f"{name} 聚合 SQL 引用了流转状态列 {column}"


def test_cost_aggregation_sql_never_references_status_columns(db):
    """成本五件套聚合同样不得引用状态列（成本只看 cost_* 与 qty）。"""
    from sqlalchemy.dialects import postgresql

    stmt_columns = board._cost_columns()
    for expr in stmt_columns:
        sql = str(expr.compile(dialect=postgresql.dialect(),
                               compile_kwargs={"literal_binds": True}))
        for column in board.STATUS_ONLY_COLUMNS:
            assert column not in sql, f"成本聚合引用了流转状态列 {column}"


def test_archived_project_with_orders_stays_in_mother_set(db, tmp_path):
    """归档项目仍带单时不得从列表消失，否则母集恒等式无声崩掉。

    归档（is_active=False）是业务动作，并不会顺带停用单据归属。此前列表按
    is_active 过滤，于是这些单既不在项目行、也进不了未归属桶（归属还是活跃的）
    ——总数会因为有人归档了一个项目而凭空变小，正是 §6.2 恒等式要防的事。
    """
    from app.models.maintenance_project import MaintenanceProject

    live, archived = make_project(db, "在营项目"), make_project(db, "归档项目")
    orders = import_wbdd(db, tmp_path, orders=3, lines_per_order=1)
    assign(db, orders[0], live)
    assign(db, orders[1], archived)
    # orders[2] 未归属
    db.get(MaintenanceProject, archived.project_id).is_active = False
    db.commit()

    client = boss_client(db, username="archive-boss")
    body = client.get("/api/maintenance/boss-board/projects",
                      params={"from": "2026-01-01", "to": "2026-12-31"}).json()
    rows = {r["project_id"]: r for r in body["rows"]}
    assert archived.project_id in rows, "归档但仍带单的项目被滤掉了"
    assert rows[archived.project_id]["is_archived"] is True
    assert rows[live.project_id]["is_archived"] is False
    # 恒等式：Σ项目 + 未归属桶 = 全局母集
    total_orders = sum(r["orders_ytd"]["value"] for r in body["rows"])
    summary = client.get("/api/maintenance/boss-board/summary",
                         params={"from": "2026-01-01", "to": "2026-12-31"}).json()
    assert total_orders == summary["orders_ytd"]["value"] == 3


def test_empty_archived_project_stays_hidden(db, tmp_path):
    """已经空掉的归档项目照旧隐藏——恒等式不需要它，列表也不该被它撑乱。"""
    from app.models.maintenance_project import MaintenanceProject

    empty = make_project(db, "空归档项目")
    db.get(MaintenanceProject, empty.project_id).is_active = False
    db.commit()
    rows = boss_client(db, username="archive-boss2").get(
        "/api/maintenance/boss-board/projects").json()["rows"]
    assert empty.project_id not in {r["project_id"] for r in rows}


def test_project_row_keyset_is_identical_for_bucket_and_projects(db, tmp_path):
    """桶行与项目行键集必须一致（新增 is_archived 后的形状回归）。"""
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=2)
    assign(db, orders[0], proj)
    rows = boss_client(db, username="shape-boss").get(
        "/api/maintenance/boss-board/projects").json()["rows"]
    bucket = next(r for r in rows if r["project_id"] == board.UNASSIGNED_BUCKET)
    real = next(r for r in rows if r["project_id"] == proj.project_id)
    assert set(bucket) == set(real)
    assert "is_archived" in real
    # 只比顶层键不够：rows 是同构数组，同名字段的**信封内部**键集也得一致，
    # 否则按数组统一取数的调用方会在桶行上拿到 undefined（partial 带 unlinked，
    # not_imported 不带，就是此前的差异）。
    for field in board.FACT_FIELDS:
        assert set(bucket[field]) == set(real[field]), field
        assert "unlinked" in bucket[field], field


def test_fact_envelope_keyset_is_uniform_across_states(db, tmp_path):
    """源为 partial 时，项目行带 unlinked、桶行也必须带（同构数组）。"""
    import uuid
    from datetime import datetime, timezone

    from app.models.maintenance_doc_import import (
        MaintenanceDocHeadRow,
        MaintenanceDocImportBatch,
    )

    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=2)
    assign(db, orders[0], proj)
    # 造一个「已应用但项目未解析」的返库单头 → return_order 进 partial
    batch = MaintenanceDocImportBatch(
        batch_id=str(uuid.uuid4()), doc_type="return_order", file_hash="h" * 64,
        filename="rt.xlsx", idempotency_key=str(uuid.uuid4()), uploaded_by="t",
        head_rows=1, line_rows=1, issue_rows=0, status="applied",
        applied_by="t", applied_at=datetime.now(timezone.utc))
    db.add(batch)
    db.flush()
    db.add(MaintenanceDocHeadRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, row_no=1, raw_json={},
        head_no="RT-1", head_date=date(2026, 7, 25), category="维保拆旧返件",
        data_status="已生效", project_id=None))
    db.commit()

    rows = boss_client(db, username="uniform-boss").get(
        "/api/maintenance/boss-board/projects").json()["rows"]
    bucket = next(r for r in rows if r["project_id"] == board.UNASSIGNED_BUCKET)
    real = next(r for r in rows if r["project_id"] == proj.project_id)
    assert real["returned_good_qty"]["state"] == "partial"
    assert set(bucket["returned_good_qty"]) == set(real["returned_good_qty"])
