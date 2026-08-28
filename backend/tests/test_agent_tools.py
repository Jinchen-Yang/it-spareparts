"""智能体工具层单测：不依赖 LLM key，直接验证派发与返回结构（对本地真库）。"""
import pytest

from app import permissions, security
from app.agent import tools
from app.db import SessionLocal


@pytest.fixture(scope="module")
def db():
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture()
def ctx():
    return security.UserContext(user_id=None, role="phase1_full_access")


def test_search_parts_structure(db, ctx):
    r = tools.dispatch(db, "search_parts", {"query": "ST8000NM000A"}, ctx)
    assert "items" in r and "low_confidence" in r
    if r["items"]:
        it = r["items"][0]
        assert {"pn_std", "score", "match_reason"} <= set(it)


def test_overview_roundtrip(db, ctx):
    """search → overview 闭环：全景含全部板块且 JSON 可序列化（date 已转 str）。"""
    r = tools.dispatch(db, "search_parts", {"query": "ST8000NM000A"}, ctx)
    if not r["items"]:
        pytest.skip("真库无该型号")
    ov = tools.dispatch(db, "get_part_overview", {"pn_std": r["items"][0]["pn_std"]}, ctx)
    assert {"part", "purchases_recent", "sales_recent", "inventory",
            "profit_summary", "sales_velocity"} <= set(ov)
    assert "qty_sold_90d" in ov["sales_velocity"]


def test_overview_unknown_pn(db, ctx):
    r = tools.dispatch(db, "get_part_overview", {"pn_std": "不存在的PN-XX99"}, ctx)
    assert "error" in r


def test_profit_ranking_truncated(db, ctx):
    r = tools.dispatch(db, "get_profit_ranking", {"dimension": "salesperson"}, ctx)
    assert "rows" in r
    assert len(r["rows"]) <= 50


def test_unknown_tool(db, ctx):
    r = tools.dispatch(db, "rm_rf", {}, ctx)
    assert "error" in r


def test_empty_query_error(db, ctx):
    r = tools.dispatch(db, "search_parts", {"query": "  "}, ctx)
    assert "error" in r


# ── v1.5.0：数据层工具接入 + 技能剧本 ──

def _role_ctx(role, perms=None):
    return security.UserContext(user_id="u1", role=role, permissions=perms,
                                is_authenticated=True)


def test_schema_registry_consistent():
    """每个 TOOLS schema 都有对应 handler，反之亦然（防漏接线）。"""
    names = {t["function"]["name"] for t in tools.TOOLS}
    assert names == set(tools._REGISTRY)


def test_maintenance_lines_contract_separates_return_status_from_cost_offset():
    spec = next(
        item["function"] for item in tools.TOOLS
        if item["function"]["name"] == "get_maintenance_lines"
    )
    description = spec["description"]
    assert "returned_qty（已返数量）" in description
    assert "pending_return_qty（待返数量）" in description
    assert "不参与净量或成本核减" in description


def test_new_data_tools_smoke(db, ctx):
    r = tools.dispatch(db, "get_purchase_analysis", {"days": 7, "top": 5}, ctx)
    assert "kpi" in r and "rows" in r
    assert all("daily" not in row for row in r["rows"])      # 火花线数组已剥离
    r = tools.dispatch(db, "get_inventory", {"limit": 5}, ctx)
    assert "items" in r
    r = tools.dispatch(db, "get_cancellation_stats", {"granularity": "month"}, ctx)
    assert "rows" in r and "summary" in r


def test_maintenance_tools_page_gate(db):
    """维保成本工具与 API 同口径：无 page_maintenance 权限 → 拒绝；有 → 放行。"""
    import app.config as cfg
    old = cfg.ENABLE_RBAC
    cfg.ENABLE_RBAC = True
    try:
        sales = _role_ctx("sales", perms={"page_maintenance": False})
        for name, args in (("get_maintenance_board", {}),
                           ("get_maintenance_projects", {}),
                           ("get_maintenance_lines", {"project": "X"})):
            r = tools.dispatch(db, name, args, sales)
            assert "无权限" in r.get("error", ""), name
        purchaser = _role_ctx("purchaser", perms={"page_maintenance": True,
                                                  "data_purchase_cost": True,
                                                  "data_profit": True})
        r = tools.dispatch(db, "get_maintenance_board", {}, purchaser)
        assert "rows" in r and "status_counts" in r
    finally:
        cfg.ENABLE_RBAC = old


def test_skills_role_filtered(db):
    """技能按角色过滤：销售看不到老板速览/维保健康检查；get_skill 越权 → 报错。"""
    import app.config as cfg
    old = cfg.ENABLE_RBAC
    cfg.ENABLE_RBAC = True
    try:
        sales = _role_ctx("sales", perms={"page_maintenance": False})
        got = {s["skill"] for s in tools.dispatch(db, "list_skills", {}, sales)["skills"]}
        assert "sales_part_briefing" in got
        assert "boss_briefing" not in got and "maintenance_health_check" not in got
        r = tools.dispatch(db, "get_skill", {"skill": "boss_briefing"}, sales)
        assert "error" in r

        boss = _role_ctx("boss", perms=permissions.effective("boss", None))
        got = {s["skill"] for s in tools.dispatch(db, "list_skills", {}, boss)["skills"]}
        assert {"boss_briefing", "purchase_batch_planning",
                "maintenance_health_check"} <= got
        r = tools.dispatch(db, "get_skill", {"skill": "purchase_batch_planning"}, boss)
        assert "playbook" in r and "get_purchase_analysis" in r["playbook"]
    finally:
        cfg.ENABLE_RBAC = old
