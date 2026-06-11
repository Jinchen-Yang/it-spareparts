"""智能体工具层单测：不依赖 LLM key，直接验证派发与返回结构（对本地真库）。"""
import pytest

from app import security
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
