"""M3-4：看板 SQL 计数门（plan v1.3 §2.5）——查询数与项目规模无关（禁 N+1）。

模仿 tests/test_maintenance_roundtrip_performance.py 的 _count_sql 计数法。
"""
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest
from sqlalchemy import event

from app.config import get_settings
from app.db import engine
from app.models.maintenance_project import MaintenanceProject
from tests.boss_board_helpers import assign, boss_client, import_wbdd, make_project


@pytest.fixture(autouse=True)
def _flag_on():
    settings = get_settings()
    original = settings.maintenance_boss_dashboard_enabled
    settings.maintenance_boss_dashboard_enabled = True
    try:
        yield
    finally:
        settings.maintenance_boss_dashboard_enabled = original


@dataclass
class _SqlCount:
    selects: int = 0
    total: int = 0
    statements: list = field(default_factory=list)


@contextmanager
def count_sql():
    counter = _SqlCount()

    def before(conn, cursor, statement, parameters, context, executemany):
        verb = statement.strip().split(None, 1)[0].upper() if statement.strip() else ""
        counter.total += 1
        if verb == "SELECT":
            counter.selects += 1
        counter.statements.append(verb)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", before)


def _seed_projects(db, n: int, prefix: str) -> list[MaintenanceProject]:
    projects = [
        MaintenanceProject(project_id=str(uuid.uuid4()),
                           project_code=f"{prefix}-{i:04d}",
                           display_name=f"{prefix}-{i:04d}",
                           lifecycle_status="ongoing")
        for i in range(n)
    ]
    db.add_all(projects)
    db.commit()
    return projects


def test_projects_list_query_count_is_independent_of_project_count(db, tmp_path):
    """100 vs 1000 项目：同一页 page_size 下 SELECT 数完全相同（O(1)，禁 N+1）。"""
    _seed_projects(db, 100, "P100")
    client = boss_client(db)
    with count_sql() as small:
        resp = client.get("/api/maintenance/boss-board/projects",
                          params={"page_size": 20})
    assert resp.status_code == 200
    small_selects = small.selects

    _seed_projects(db, 900, "P900")
    with count_sql() as large:
        resp = client.get("/api/maintenance/boss-board/projects",
                          params={"page_size": 20})
    assert resp.status_code == 200
    assert len(resp.json()["rows"]) <= 21          # 20 项目 + 未归属桶
    assert large.selects == small_selects, (small_selects, large.selects)
    assert large.selects <= 40


def test_projects_list_query_count_is_independent_of_page_size(db, tmp_path):
    """同一数据集下，page_size 20 与 100 的 SELECT 数相同（逐行无额外查询）。"""
    _seed_projects(db, 200, "PS")
    client = boss_client(db)
    with count_sql() as small:
        client.get("/api/maintenance/boss-board/projects", params={"page_size": 20})
    with count_sql() as big:
        client.get("/api/maintenance/boss-board/projects", params={"page_size": 100})
    assert big.selects == small.selects, (small.selects, big.selects)


def test_drilldown_query_counts_bounded(db, tmp_path):
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=5, lines_per_order=4)
    for order in orders:
        assign(db, order, proj)
    client = boss_client(db)
    with count_sql() as od:
        resp = client.get(
            f"/api/maintenance/boss-board/projects/{proj.project_id}/orders",
            params={"page_size": 20})
    assert resp.status_code == 200 and resp.json()["total"] == 5
    # 单据行的逐单成本/行数查询与页大小成正比，但每单常数条，设上限防回归
    assert od.selects <= 30, od.selects
    with count_sql() as ln:
        resp = client.get(
            f"/api/maintenance/boss-board/orders/{orders[0].raw_order_id}/lines",
            params={"page_size": 20})
    assert resp.status_code == 200 and resp.json()["total"] == 4
    assert ln.selects <= 12, ln.selects


def test_summary_and_health_query_counts_bounded(db, tmp_path):
    import_wbdd(db, tmp_path, orders=3, lines_per_order=2)
    client = boss_client(db)
    with count_sql() as summary:
        client.get("/api/maintenance/boss-board/summary")
    assert summary.selects <= 20, summary.selects
    with count_sql() as health:
        client.get("/api/maintenance/boss-board/health")
    assert health.selects <= 20, health.selects
