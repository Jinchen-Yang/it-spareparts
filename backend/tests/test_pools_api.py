"""/api/pools* 接口层：权限矩阵 × 乐观锁 409 × 价格治理脱敏 × 端到端闭环。

矩阵口径（规格 §12 + §24 工程门槛）：
- 读：登录即可（全员）；匿名 401。
- 池维护写：action_pool_manage（模板默认 boss；admin 恒通过；可对任意账号单独授权）。
- 约束价写：action_pool_set_policy（默认 boss/admin）。
- data_pool_price_governance=False 的账号：约束价字段（含原始录入值）全为 null。
"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.main import app
from app.models.dimensions import DimPart
from app.models.system import SysUser
from app.services import pool_catalog as svc


def _mk_client(db, username, role, permissions=None, password="pw123456"):
    db.add(SysUser(username=username, role=role, permissions=permissions,
                   password_hash=hash_password(password)))
    db.commit()
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    c.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return c


def _seed_pool(db, name="矩阵池", pns=("API-A", "API-B")):
    ids = []
    for pn in pns:
        p = DimPart(pn_std=pn)
        db.add(p); db.flush()
        ids.append(p.id)
    created = svc.create_pool(db, name=name, member_part_ids=ids, operated_by="seed")
    return created, ids


# ---------------------------------------------------------------- 读权限

def test_read_requires_login(db):
    _seed_pool(db)
    anon = TestClient(app)
    assert anon.get("/api/pools").status_code == 401
    for role in ("admin", "boss", "sales", "purchaser", "readonly"):
        c = _mk_client(db, f"r_{role}", role)
        r = c.get("/api/pools")
        assert r.status_code == 200, f"{role} 应可读池清单"
        assert r.json()["total"] == 1
        gid = r.json()["items"][0]["group_id"]
        assert c.get(f"/api/pools/{gid}").status_code == 200


def test_detail_404(db):
    c = _mk_client(db, "u404", "boss")
    assert c.get("/api/pools/999999").status_code == 404


# ---------------------------------------------------------------- 写权限矩阵

def test_write_permission_matrix(db):
    created, _ = _seed_pool(db)
    gid, ver = created["group_id"], created["version"]
    # 无池维护权限的业务角色：全部写操作 403
    for role in ("sales", "purchaser", "readonly"):
        c = _mk_client(db, f"w_{role}", role)
        assert c.post("/api/pools", json={"name": "越权池"}).status_code == 403
        assert c.patch(f"/api/pools/{gid}", json={"version": ver, "name": "x"}).status_code == 403
        assert c.patch(f"/api/pools/{gid}/members",
                       json={"version": ver, "add_part_ids": []}).status_code == 403
        assert c.put(f"/api/pools/{gid}/price-policy",
                     json={"version": ver, "purchase_value": "1"}).status_code == 403
        assert c.post(f"/api/pools/{gid}/archive", json={"version": ver}).status_code == 403
    # 匿名：401
    anon = TestClient(app)
    assert anon.post("/api/pools", json={"name": "匿名池"}).status_code == 401
    # boss / admin：可建池
    for role in ("boss", "admin"):
        c = _mk_client(db, f"ok_{role}", role)
        r = c.post("/api/pools", json={"name": f"{role}的池"})
        assert r.status_code == 200 and r.json()["source"] == "manual"


def test_grant_pool_manage_to_readonly_user(db):
    """§12：成员维护可单独授权数据维护人员——readonly + action_pool_manage 可维护池，
    但 action_pool_set_policy 未授 → 约束价仍 403（两权限独立）。"""
    c = _mk_client(db, "dm1", "readonly", permissions={"action_pool_manage": True})
    r = c.post("/api/pools", json={"name": "数据维护建的池"})
    assert r.status_code == 200
    gid, ver = r.json()["group_id"], r.json()["version"]
    assert c.patch(f"/api/pools/{gid}", json={"version": ver, "name": "改名"}).status_code == 200
    assert c.put(f"/api/pools/{gid}/price-policy",
                 json={"version": ver + 1, "purchase_value": "10"}).status_code == 403


def test_boss_sets_policy_via_api(db):
    created, _ = _seed_pool(db)
    c = _mk_client(db, "boss1", "boss")
    r = c.put(f"/api/pools/{created['group_id']}/price-policy",
              json={"version": 1, "purchase_value": "113", "purchase_basis": "inc_tax",
                    "sales_value": "973.45", "sales_basis": "ex_tax", "note": "早会定的"})
    assert r.status_code == 200
    body = r.json()
    assert Decimal(str(body["purchase_ceiling_ex_tax"])) == Decimal("100.00")
    assert Decimal(str(body["sales_floor_ex_tax"])) == Decimal("973.45")


# ---------------------------------------------------------------- 冲突语义

def test_stale_version_409_via_api(db):
    created, _ = _seed_pool(db)
    gid = created["group_id"]
    c = _mk_client(db, "boss2", "boss")
    assert c.patch(f"/api/pools/{gid}", json={"version": 1, "name": "先手"}).status_code == 200
    r = c.patch(f"/api/pools/{gid}", json={"version": 1, "name": "后手"})
    assert r.status_code == 409 and "已被他人修改" in r.json()["detail"]


def test_member_conflict_409_via_api(db):
    created, ids = _seed_pool(db)
    c = _mk_client(db, "boss3", "boss")
    r = c.post("/api/pools", json={"name": "抢人池", "member_part_ids": [ids[0]]})
    assert r.status_code == 409 and "矩阵池" in r.json()["detail"]


def test_bad_request_400(db):
    c = _mk_client(db, "boss4", "boss")
    assert c.post("/api/pools", json={"name": "   "}).status_code == 400
    created, _ = _seed_pool(db)
    r = c.put(f"/api/pools/{created['group_id']}/price-policy",
              json={"version": 1, "purchase_value": "-5"})
    assert r.status_code == 400


def test_archive_restore_roundtrip_via_api(db):
    created, ids = _seed_pool(db)
    gid = created["group_id"]
    c = _mk_client(db, "boss5", "boss")
    r = c.post(f"/api/pools/{gid}/archive", json={"version": 1, "note": "先停用"})
    assert r.status_code == 200 and r.json()["status"] == "archived"
    r2 = c.post(f"/api/pools/{gid}/restore", json={"version": 2})
    assert r2.status_code == 200 and r2.json()["status"] == "active"
    # 已是有效再恢复 → 400（幂等错误提示而非静默成功）
    assert c.post(f"/api/pools/{gid}/restore", json={"version": 3}).status_code == 400


# ---------------------------------------------------------------- 价格治理脱敏

def test_price_governance_masking(db):
    """data_pool_price_governance=False：清单/详情的约束价与原始录入值全为 null，
    有权限账号看到真实值（防止靠管理页反推约束金额，§12）。"""
    created, _ = _seed_pool(db)
    gid = created["group_id"]
    boss = _mk_client(db, "boss6", "boss")
    boss.put(f"/api/pools/{gid}/price-policy",
             json={"version": 1, "purchase_value": "725.66", "sales_value": "973.45"})

    blind = _mk_client(db, "blind1", "readonly",
                       permissions={"data_pool_price_governance": False})
    item = blind.get("/api/pools").json()["items"][0]
    assert item["purchase_ceiling_ex_tax"] is None
    assert item["sales_floor_ex_tax"] is None
    detail = blind.get(f"/api/pools/{gid}").json()
    assert detail["purchase_ceiling_ex_tax"] is None
    assert detail["price_policy"]["purchase_input_value"] is None
    for h in detail["price_policy_history"]:
        assert h["purchase_ceiling_ex_tax"] is None and h["sales_floor_ex_tax"] is None

    seen = boss.get(f"/api/pools/{gid}").json()
    assert Decimal(str(seen["purchase_ceiling_ex_tax"])) == Decimal("725.66")


# ---------------------------------------------------------------- 权限注册表接线

def test_permission_registry_wiring(db):
    """四个新权限进 ALL_KEYS/_VALID/模板/_full；guest（匿名兜底）绝无池写权限；
    sanitize 不丢新 key；账号管理 _meta 返回 action_keys 桶。"""
    from app import permissions

    for key in ("page_pool_analysis", "data_pool_price_governance",
                "action_pool_manage", "action_pool_set_policy"):
        assert key in permissions.ALL_KEYS and key in permissions.LABELS
    # admin/boss 恒有池写权限（_full 纳入 action）
    for role in ("admin", "boss"):
        eff = permissions.effective(role, None)
        assert eff["action_pool_manage"] is True and eff["action_pool_set_policy"] is True
    # 其余角色（含 guest 匿名兜底）默认无写权限；分析/价格治理全实名角色开
    for role in ("sales", "purchaser", "readonly", "guest"):
        eff = permissions.effective(role, None)
        assert eff["action_pool_manage"] is False
        assert eff["action_pool_set_policy"] is False
        assert eff["page_pool_analysis"] is True
        assert eff["data_pool_price_governance"] is True
    # sanitize 白名单收录新 key（否则管理页授权会被静默丢弃）
    assert permissions.sanitize({"action_pool_manage": 1, "bogus": True}) == {
        "action_pool_manage": True}
    # 账号管理元数据带 action 桶（前端权限抽屉渲染依据）
    admin = _mk_client(db, "meta_admin", "admin")
    meta = admin.get("/api/accounts/_meta").json()
    assert meta["action_keys"] == ["action_pool_manage", "action_pool_set_policy"]


# ---------------------------------------------------------------- 端到端闭环

def test_full_management_flow(db):
    """管理页闭环：建池→改名→调成员→设约束→归档→恢复，每步用上一步返回的 version。"""
    p1 = DimPart(pn_std="FLOW-1"); p2 = DimPart(pn_std="FLOW-2")
    db.add_all([p1, p2]); db.commit()
    c = _mk_client(db, "admin_flow", "admin")

    r = c.post("/api/pools", json={"name": "闭环池", "member_part_ids": [p1.id],
                                   "note": "建池"})
    assert r.status_code == 200
    gid, ver = r.json()["group_id"], r.json()["version"]

    r = c.patch(f"/api/pools/{gid}", json={"version": ver, "description": "闭环说明"})
    assert r.status_code == 200; ver = r.json()["version"]

    r = c.patch(f"/api/pools/{gid}/members",
                json={"version": ver, "add_part_ids": [p2.id]})
    assert r.status_code == 200 and r.json()["member_count"] == 2
    ver = r.json()["version"]

    r = c.put(f"/api/pools/{gid}/price-policy",
              json={"version": ver, "purchase_value": "100"})
    assert r.status_code == 200; ver = r.json()["version"]

    r = c.post(f"/api/pools/{gid}/archive", json={"version": ver})
    assert r.status_code == 200; ver = r.json()["version"]
    r = c.post(f"/api/pools/{gid}/restore", json={"version": ver})
    assert r.status_code == 200

    detail = c.get(f"/api/pools/{gid}").json()
    assert detail["member_count"] == 2 and detail["status"] == "active"
    assert {m["pn_std"] for m in detail["members"]} == {"FLOW-1", "FLOW-2"}
    assert Decimal(str(detail["price_policy"]["purchase_ceiling_ex_tax"])) == Decimal("100.00")
