"""M3-2：看板七端点契约、六态信封、未归属桶（plan v1.3 §4.4/§4.5）。"""
import pytest

from app.config import get_settings
from app.services import maintenance_boss_board as board
from tests.boss_board_helpers import (
    add_ckd,
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


def test_health_returns_four_sources(db):
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/health").json()
    assert set(body["sources"]) == {"wbdd", "ckd", "return_order", "rkd_inbound"}
    assert all(s["readiness"] == "not_imported" for s in body["sources"].values())


def test_summary_window_defaults_and_envelopes(db, tmp_path):
    import_wbdd(db, tmp_path, orders=2, lines_per_order=2)
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/summary",
                      params={"from": "2026-01-01", "to": "2026-12-31"}).json()
    assert body["window"] == {"from": "2026-01-01", "to": "2026-12-31"}
    assert body["orders_ytd"]["state"] == "ready"
    assert body["orders_ytd"]["value"] == 2
    assert body["lines_ytd"]["value"] == 4
    # 环比基期：等长窗口紧邻前移
    assert body["prev_window"]["window"]["to"] == "2025-12-31"
    # 时间窗字段名不写死年份
    assert "y2026" not in str(body)


def test_summary_cost_bundle_five_fields(db, tmp_path):
    import_wbdd(db, tmp_path, orders=1, lines_per_order=2)
    set_costs(db, source="direct", amount="100.00")
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/summary",
                      params={"from": "2026-01-01", "to": "2026-12-31"}).json()
    bundle = body["known_apply_cost_inc_tax"]
    assert bundle["state"] == "ready"
    value = bundle["value"]
    assert set(value) == {"actual_amount", "estimated_amount", "known_amount",
                          "missing_lines", "coverage_pct", "quality"}
    assert float(value["actual_amount"]) == 200.0
    assert value["missing_lines"] == 0
    assert value["coverage_pct"] == 100.0
    assert value["quality"] == "actual_only"


def test_cost_bundle_incomplete_when_missing_prices(db, tmp_path):
    """缺价必须显式 incomplete / missing_lines，绝不按 0 计（铁律 5 精神）。"""
    import_wbdd(db, tmp_path, orders=1, lines_per_order=2)
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/summary",
                      params={"from": "2026-01-01", "to": "2026-12-31"}).json()
    value = body["known_apply_cost_inc_tax"]["value"]
    assert value["missing_lines"] == 2
    assert value["quality"] == "incomplete"
    assert value["coverage_pct"] == 0.0


def test_attention_is_empty_pending_m0a(db):
    """M0-A 未拍板：只搭框架，不预置内容（不替业务拍板）。"""
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/attention").json()
    assert body["items"] == []
    assert body["pending_decision"] == "M0-A"


def test_projects_list_includes_unassigned_bucket(db, tmp_path):
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=2, lines_per_order=1)
    assign(db, orders[0], proj)          # 一单归属，一单留未归属
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/projects").json()
    rows = body["rows"]
    bucket = next(r for r in rows if r["project_id"] == board.UNASSIGNED_BUCKET)
    assert bucket["orders_ytd"]["value"] == 1
    proj_row = next(r for r in rows if r["project_id"] == proj.project_id)
    assert proj_row["orders_ytd"]["value"] == 1
    assert proj_row["lifecycle"] == "ongoing"
    assert body["total"] == 1            # total 只数真实项目；桶是附加行


def test_project_row_fact_envelopes_not_imported_never_zero(db, tmp_path):
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path)
    assign(db, orders[0], proj)
    client = boss_client(db)
    row = next(r for r in client.get("/api/maintenance/boss-board/projects").json()["rows"]
               if r["project_id"] == proj.project_id)
    for field in ("shipped_qty", "returned_good_qty", "returned_bad_qty"):
        assert row[field]["state"] == "not_imported"
        assert row[field]["value"] is None       # 未导入绝不显示 0


def test_project_row_shipped_ready_after_ckd(db, tmp_path):
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path)
    assign(db, orders[0], proj)
    add_ckd(db, wbdd_no=orders[0].order_no, qty="7")
    client = boss_client(db)
    row = next(r for r in client.get("/api/maintenance/boss-board/projects").json()["rows"]
               if r["project_id"] == proj.project_id)
    assert row["shipped_qty"]["state"] == "ready"
    assert float(row["shipped_qty"]["value"]) == 7.0
    assert row["returned_good_qty"]["state"] == "not_imported"


def test_get_projects_with_q_rejects_and_search_works(db, tmp_path):
    make_project(db, code="平安银行整体维保")
    make_project(db, code="其他工程")
    client = boss_client(db)
    bad = client.get("/api/maintenance/boss-board/projects", params={"q": "平安"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "use_search_endpoint"
    found = client.post("/api/maintenance/boss-board/projects/search",
                        json={"q": "平安"}).json()
    assert [r["project_code"] for r in found["rows"]] == ["平安银行整体维保"]


def test_order_and_line_drilldown(db, tmp_path):
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=2)
    assign(db, orders[0], proj)
    set_costs(db, amount="50.00")
    client = boss_client(db)
    od = client.get(
        f"/api/maintenance/boss-board/projects/{proj.project_id}/orders").json()
    assert od["total"] == 1
    row = od["rows"][0]
    assert row["order_no"] == orders[0].order_no
    assert row["line_count"] == 2
    assert row["self_report"]["head_shipped_qty"] is not None
    # M4-4：无判定并排——响应不含任何 mismatch/diff 键
    assert "mismatch" not in str(row) and "diff" not in str(row)
    lines = client.get(
        f"/api/maintenance/boss-board/orders/{orders[0].raw_order_id}/lines").json()
    assert lines["total"] == 2
    line = lines["rows"][0]
    # 流转状态列原样（铁律 3）
    assert float(line["supplied_qty"]) == 2.0
    assert line["consumed_qty"] is None
    assert line["known_apply_cost_inc_tax"]["state"] == "ready"


def test_unassigned_bucket_drilldown(db, tmp_path):
    import_wbdd(db, tmp_path, orders=1)
    client = boss_client(db)
    body = client.get(
        f"/api/maintenance/boss-board/projects/{board.UNASSIGNED_BUCKET}/orders").json()
    assert body["total"] == 1


def test_flag_off_hides_all_board_endpoints(db):
    client = boss_client(db)
    settings = get_settings()
    settings.maintenance_boss_dashboard_enabled = False
    try:
        for path in ("/api/maintenance/boss-board/health",
                     "/api/maintenance/boss-board/summary",
                     "/api/maintenance/boss-board/attention",
                     "/api/maintenance/boss-board/projects"):
            assert client.get(path).status_code == 404, path
        assert client.post("/api/maintenance/boss-board/projects/search",
                           json={"q": "x"}).status_code == 404
        # 稳定端点不受影响
        assert client.get("/api/maintenance/projects").status_code == 200
    finally:
        settings.maintenance_boss_dashboard_enabled = True
