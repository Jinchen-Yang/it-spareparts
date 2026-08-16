"""M3-3：三类账号 HTTP 矩阵 + 无侧信道三条硬规则（plan v1.3 §6.2）。"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services import maintenance_boss_board as board
from tests.boss_board_helpers import (
    assign,
    boss_client,
    import_wbdd,
    make_project,
    manager_client,
    no_access_client,
    set_costs,
)

_READ_PATHS = (
    "/api/maintenance/boss-board/health",
    "/api/maintenance/boss-board/summary",
    "/api/maintenance/boss-board/attention",
    "/api/maintenance/boss-board/projects",
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


def _numbers(payload) -> list:
    """递归收集 JSON 中所有数字（用于「无成本账号响应中没有金额」断言）。"""
    out = []
    if isinstance(payload, dict):
        for value in payload.values():
            out.extend(_numbers(value))
    elif isinstance(payload, list):
        for value in payload:
            out.extend(_numbers(value))
    elif isinstance(payload, (int, float)) and not isinstance(payload, bool):
        out.append(payload)
    return out


def _keyset(payload) -> set:
    return set(payload) if isinstance(payload, dict) else set()


# ---------- 三类账号 × 端点矩阵 ----------

def test_unauthenticated_is_401(db):
    anon = TestClient(app)
    for path in _READ_PATHS:
        assert anon.get(path).status_code == 401, path


def test_no_permission_account_is_403(db):
    client = no_access_client(db)
    for path in _READ_PATHS:
        assert client.get(path).status_code == 403, path


def test_boss_sees_all_endpoints(db, tmp_path):
    import_wbdd(db, tmp_path)
    client = boss_client(db)
    for path in _READ_PATHS:
        assert client.get(path).status_code == 200, path


def test_manager_scope_limited_to_own_projects(db, tmp_path):
    """M0-B「本人项目」案：经理只见自身范围；未归属桶不可见（无本人范围可言）。"""
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=2)
    assign(db, orders[0], proj)
    client = manager_client(db)
    body = client.get("/api/maintenance/boss-board/projects").json()
    ids = {r["project_id"] for r in body["rows"]}
    assert board.UNASSIGNED_BUCKET not in ids
    # 未分配给该经理的项目也不在范围内
    assert ids <= {proj.project_id}
    # 桶下钻 404（不暴露存在性）
    assert client.get(
        f"/api/maintenance/boss-board/projects/{board.UNASSIGNED_BUCKET}/orders"
    ).status_code == 404


def test_boss_sees_unassigned_bucket(db, tmp_path):
    import_wbdd(db, tmp_path)
    client = boss_client(db)
    ids = {r["project_id"]
           for r in client.get("/api/maintenance/boss-board/projects").json()["rows"]}
    assert board.UNASSIGNED_BUCKET in ids


# ---------- 无侧信道三条硬规则 ----------

def test_rule1_cost_fields_restricted_without_permission(db, tmp_path):
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, lines_per_order=2)
    assign(db, orders[0], proj)
    set_costs(db, amount="123.45")
    client = manager_client(db, username="no-cost-mgr", with_cost=False)
    summary = client.get("/api/maintenance/boss-board/summary").json()
    bundle = summary["known_apply_cost_inc_tax"]
    assert bundle["state"] == "restricted"
    assert bundle["value"] is None and bundle["as_of"] is None
    # 递归扫描：响应中不得出现成本金额
    assert 123.45 not in _numbers(summary)


def test_rule1_line_level_cost_and_source_restricted(db, tmp_path):
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, lines_per_order=1)
    assign(db, orders[0], proj)
    set_costs(db, source="pool_sales", amount="99.99")
    # 用「全范围但无成本」账号隔离成本维度：本人范围账号会先被 IDOR 范围校验挡在
    # 404（见 test_line_drilldown_enforces_project_scope_idor），验不到成本脱敏。
    client = boss_client(db, username="nocost-boss-lines", with_cost=False)
    lines = client.get(
        f"/api/maintenance/boss-board/orders/{orders[0].raw_order_id}/lines").json()
    row = lines["rows"][0]
    # 金额、取价来源、置信度同属成本组：一并 restricted（防经来源反推）
    for field in ("known_apply_cost_inc_tax", "cost_source", "confidence"):
        assert row[field]["state"] == "restricted", field
        assert row[field]["value"] is None
    assert "pool_sales" not in str(lines)
    assert 99.99 not in _numbers(lines)


def test_rule2_cost_sort_is_422_not_silent_downgrade(db, tmp_path):
    """静默降级会通过顺序泄露排名 → 必须显式 422。"""
    make_project(db)
    no_cost = manager_client(db, username="no-cost-mgr3", with_cost=False)
    resp = no_cost.get("/api/maintenance/boss-board/projects",
                       params={"sort": "known_cost"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "sort_requires_cost_permission"
    # 有成本权限的账号可用该排序
    assert boss_client(db).get("/api/maintenance/boss-board/projects",
                               params={"sort": "known_cost"}).status_code == 200


def test_rule2_search_endpoint_also_rejects_cost_sort(db):
    make_project(db)
    no_cost = manager_client(db, username="no-cost-mgr4", with_cost=False)
    resp = no_cost.post("/api/maintenance/boss-board/projects/search",
                        json={"q": "合成", "sort": "known_cost"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "sort_requires_cost_permission"


def test_rule3_response_shape_identical_with_and_without_cost(db, tmp_path):
    """restricted 与 ready 的键集合必须一致（防「字段存在性」侧信道）。"""
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, lines_per_order=1)
    assign(db, orders[0], proj)
    set_costs(db)
    # 只改变成本维度（范围同为全范围），确保比较的是「有无成本权限」而非范围差异
    with_cost = boss_client(db, username="cost-boss").get(
        "/api/maintenance/boss-board/summary").json()
    without = boss_client(db, username="nocost-boss", with_cost=False).get(
        "/api/maintenance/boss-board/summary").json()
    assert _keyset(with_cost) == _keyset(without)
    assert (_keyset(with_cost["known_apply_cost_inc_tax"])
            == _keyset(without["known_apply_cost_inc_tax"]))

    rows_with = boss_client(db, username="cost-boss2").get(
        "/api/maintenance/boss-board/projects").json()["rows"]
    rows_without = boss_client(db, username="nocost-boss2", with_cost=False).get(
        "/api/maintenance/boss-board/projects").json()["rows"]
    assert rows_with and rows_without
    assert _keyset(rows_with[0]) == _keyset(rows_without[0])
    assert (_keyset(rows_with[0]["known_apply_cost_inc_tax"])
            == _keyset(rows_without[0]["known_apply_cost_inc_tax"]))


def test_attention_has_no_cost_derived_items_without_permission(db):
    """M0-A 未拍板时队列为空；无成本账号同样不得出现成本派生条目。"""
    no_cost = manager_client(db, username="no-cost-mgr7", with_cost=False)
    body = no_cost.get("/api/maintenance/boss-board/attention").json()
    assert body["items"] == []


def test_line_drilldown_enforces_project_scope_idor(db, tmp_path):
    """IDOR 防回归（plan §6.2「越权 id → 404」）：

    PN 证据行下钻只按 source_order_id 取数，若不做范围校验，项目经理凭单据 ID
    即可读到他人项目、乃至未归属单的明细。此处锁死越权一律 404（不暴露存在性）。
    """
    mine, theirs = make_project(db, "我的项目"), make_project(db, "别人的项目")
    orders = import_wbdd(db, tmp_path, orders=3)
    assign(db, orders[0], mine)
    assign(db, orders[1], theirs)
    # orders[2] 保持未归属

    boss = boss_client(db, username="idor-boss")
    # 全范围账号可读全部三种
    for order in orders:
        assert boss.get(
            f"/api/maintenance/boss-board/orders/{order.raw_order_id}/lines"
        ).status_code == 200, order.raw_order_id

    manager = manager_client(db, username="idor-manager")
    # 本人范围账号：他人项目单据与未归属单据均 404
    assert manager.get(
        f"/api/maintenance/boss-board/orders/{orders[1].raw_order_id}/lines"
    ).status_code == 404
    assert manager.get(
        f"/api/maintenance/boss-board/orders/{orders[2].raw_order_id}/lines"
    ).status_code == 404
    # 不存在的单据同样 404（与越权不可区分）
    assert manager.get(
        "/api/maintenance/boss-board/orders/NO-SUCH-ORDER/lines"
    ).status_code == 404
