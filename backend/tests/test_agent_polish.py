"""Agent 加固 PR-3（品味与一致性）：profit 数据层越权兜底(TOOLS-3) +
文件存在性 oracle(TOOLS-4) + 工具描述/clamp 单一常量(TOOLS-5)。"""
import pytest

from app import permissions, security
from app.agent import tools
from app.services import profit


def _ctx(role: str, uid: str = "u") -> security.UserContext:
    return security.UserContext(user_id=uid, role=role,
                                permissions=permissions.effective(role, None))


# ---------- TOOLS-3：profit.aggregate 数据层兜底 ----------
def test_aggregate_blocks_scoped_sales_sensitive_dims(db):
    scoped = _ctx("sales")   # own_customers_only=True → is_scoped_sales
    for dim in ("salesperson", "customer"):
        with pytest.raises(PermissionError):
            profit.aggregate(db, dim, None, None, False, scoped)
    # part 维度只是行情、不暴露同事经营，受限销售放行
    assert "rows" in profit.aggregate(db, "part", None, None, False, scoped)


def test_aggregate_allows_admin_all_dims(db):
    admin = _ctx("admin")   # own_customers_only=False → 不受兜底影响
    for dim in ("part", "salesperson", "customer"):
        assert "rows" in profit.aggregate(db, dim, None, None, False, admin)


# ---------- TOOLS-4：文件不存在按"无权"处理，关存在性 oracle ----------
def test_owns_denies_nonexistent_file_for_scoped_user():
    sales = security.UserContext(user_id="liu", role="sales")
    # 不存在的 file_id：受限用户拿到的是"无权"（False），与"非本人"不可区分
    assert tools._owns(sales, "deadbeef0001") is False
    # 普通工具入口 owner-only；admin 也不能拿不存在/他人的 file_id 绕过。
    assert tools._owns(security.UserContext(user_id="a", role="admin"), "deadbeef0001") is False


# ---------- TOOLS-5：工具描述与 clamp 引用同一常量，防文字漂移 ----------
def test_tool_limit_descriptions_match_constants():
    fns = {t["function"]["name"]: t["function"] for t in tools.TOOLS}
    props = lambda name: fns[name]["parameters"]["properties"]
    assert str(tools._SEARCH_LIMIT_MAX) in props("search_parts")["limit"]["description"]
    assert str(tools._READ_ROWS_MAX) in props("read_file_rows")["max_rows"]["description"]
    assert str(tools._RECENT_DAYS_MAX) in props("list_recent_purchases")["days"]["description"]
    assert str(tools._RECENT_LIMIT_MAX) in props("list_recent_purchases")["limit"]["description"]
    assert str(tools._BULK_MAX) in fns["lookup_prices_bulk"]["description"]
    assert str(tools._RANK_ROWS) in fns["get_profit_ranking"]["description"]
