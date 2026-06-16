"""Agent 加固 PR-1：dispatch 异常脱敏(TOOLS-1) + chat 路由后端准入(HUB-1) + 脱敏覆盖护栏(TOOLS-2)。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from app import permissions, security
from app.agent import tools
from app.auth import hash_password
from app.main import app
from app.models.system import SysImportBatch, SysUser
from app.services import part_overview, profit, purchase_query
from tests import factories as f


# ---------- TOOLS-1：内部异常不外泄 ----------
def test_dispatch_sanitizes_internal_exception(monkeypatch):
    """工具内部抛异常(如 SQLAlchemyError 带 SQL+表名) → dispatch 只回固定脱敏文案，
    原始 SQL/表名/异常类型一律不进回灌结果。"""
    def boom(db, args, ctx):
        raise ProgrammingError("SELECT supplier_name FROM dim_supplier WHERE id=1",
                               {}, Exception("relation dim_supplier leaked"))
    monkeypatch.setitem(tools._REGISTRY, "search_parts", boom)
    ctx = security.UserContext(user_id=None, role="phase1_full_access")
    r = tools.dispatch(None, "search_parts", {"query": "x"}, ctx)

    assert r["error"] == "工具执行失败，请换个方式或稍后重试"
    assert r["kind"] == "internal" and r["retriable"] is True
    blob = str(r)
    for leak in ("SELECT", "dim_supplier", "ProgrammingError", "relation"):
        assert leak not in blob, f"内部细节泄漏: {leak}"


def test_dispatch_keeps_business_error(db):
    """业务错(工具显式 return error 文案)原样回灌——模型据此自恢复，不被脱敏吞掉。"""
    ctx = security.UserContext(user_id=None, role="phase1_full_access")
    r = tools.dispatch(db, "search_parts", {"query": "   "}, ctx)
    assert r.get("error") and "kind" not in r   # 业务错无 internal 标记


# ---------- HUB-1：page_chat 后端准入 ----------
def _login(c, db, username, perms=None):
    db.add(SysUser(username=username, role="sales",
                   password_hash=hash_password("pw123456"), permissions=perms))
    db.commit()
    return c.post("/api/auth/login",
                  json={"username": username, "password": "pw123456"}).json()["token"]


def test_chat_routes_require_page_chat(db):
    c = TestClient(app)
    no_chat = _login(c, db, "nochat", {"page_chat": False})
    yes_chat = _login(c, db, "haschat")
    H = lambda t: {"Authorization": f"Bearer {t}"}

    # 无 page_chat：会话接口 + 无状态 chat 接口都 403（前端藏菜单≠后端拦接口）
    assert c.get("/api/agent/sessions", headers=H(no_chat)).status_code == 403
    assert c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "hi"}]},
                  headers=H(no_chat)).status_code == 403
    # 有 page_chat：会话列表放行
    assert c.get("/api/agent/sessions", headers=H(yes_chat)).status_code == 200


def test_every_agent_route_carries_page_chat_gate():
    """结构红线：挂在 /api/agent* 下的每条路由都必须带 require_page 依赖。
    防将来有人新加 agent 端点(或挪到别的 router)漏掉后端准入——stream/attach/cancel/
    upload/download/preview 等都靠 router 级依赖覆盖，这里把"无端点逃逸"钉死。"""
    routes = [r for r in app.routes if getattr(r, "path", "").startswith("/api/agent")]
    assert routes, "未发现 /api/agent* 路由"
    for r in routes:
        quals = [getattr(d.call, "__qualname__", "") for d in r.dependant.dependencies]
        assert any("require_page" in q for q in quals), f"{r.path} 缺 page_chat 后端准入"


# ---------- TOOLS-2：敏感派生键必须已在 FIELD_GROUPS 登记（脱敏才生效）----------
# 脱敏靠精确 key 匹配(security._mask)，服务层新增成本/毛利/供应商派生键若忘登记即漏脱敏。
# 本测试遍历各财务工具的真实产出键，凡命中敏感族就断言已登记——把"差一字即漏"变成红线。
_SENSITIVE = ("cost", "margin", "profit", "supplier")
_PURCHASE_PRICE = ("purchase_price", "purchase_avg", "purchase_min", "purchase_max")
# 非金额值/或对销售本就可见的键：不要求登记（带说明）。新增真敏感金额键不在此列即被拦。
_ALLOW = {
    "cost_source",      # 成本来源枚举(computed/fallback/none)，非金额
    "cost_method",      # 计价法名(moving_avg/fifo)，非金额
    "no_cost",          # 统计计数(无成本行数)，非金额
    "revenue_costed",   # 营收口径(已配到成本的营收，毛利分母)，单独不可反推成本
    "profit_summary",   # 全景的利润板块容器键，其子键(avg_purchase_cost/gross_margin…)已各自登记
}


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    if key in _ALLOW or "sale" in k:   # sale_* 系售价聚合，销售本就可见、刻意不在脱敏组
        return False
    return any(s in k for s in _SENSITIVE) or any(p in k for p in _PURCHASE_PRICE) or key == "unit_price"


def _collect_keys(node, out: set):
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(k)
            _collect_keys(v, out)
    elif isinstance(node, list):
        for x in node:
            _collect_keys(x, out)


def test_sensitive_tool_keys_are_registered_for_masking(db):
    # 最小时间线：让财务工具吐出全部派生键
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hc")
    db.add(b)
    db.flush()
    loader_orders = {"P1": f.purchase_head("P1", on=date(2026, 1, 5))}
    loader_lines = [f.purchase_line("P1", "PL1", "PN-X", qty="10", price="80")]
    from app.etl import loader
    loader.load(db, f.purchase_result(loader_orders, loader_lines), b.id, date(2026, 6, 1))
    loader.load(db, f.sales_result({"S1": f.sales_head("S1", on=date(2026, 2, 1))},
                                   [f.sales_line("S1", "SL1", "PN-X", qty="5", price="150")]),
                b.id, date(2026, 6, 1))
    db.commit()
    profit.recompute(db)

    # 全量上下文：不脱敏，看到所有原始派生键
    admin = security.UserContext(user_id="t", role="admin",
                                 permissions=permissions.effective("admin", None))
    keys: set = set()
    _collect_keys(part_overview.get_overview(db, "PN-X", admin), keys)
    _collect_keys(part_overview.quick_pricing(db, "PN-X"), keys)
    _collect_keys(profit.aggregate(db, "part", None, None, False, admin), keys)
    _collect_keys(purchase_query.recent_purchases(db, admin, days=365, page=1, page_size=20), keys)

    reverse = security._FIELD_TO_GROUP
    unregistered = sorted(k for k in keys if _is_sensitive(k) and k not in reverse)
    assert not unregistered, (
        f"敏感派生键未在 config.FIELD_GROUPS 登记，将漏脱敏: {unregistered}。"
        "请补登到对应字段组(purchase_cost/profit_amount/profit_rate/supplier_info)。")
