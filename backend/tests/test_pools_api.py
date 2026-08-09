"""/api/pools* 接口层：权限矩阵 × 乐观锁 409 × 价格治理脱敏 × 端到端闭环。

矩阵口径（规格 §12 + §24 工程门槛）：
- 读：登录即可（全员）；匿名 401。
- 池维护写：action_pool_manage（模板默认 boss；admin 恒通过；可对任意账号单独授权）。
- 约束价写：action_pool_set_policy（默认 boss/admin）。
- data_pool_price_governance=False 的账号：清单金额为 null，详情策略及历史整体隐藏。
"""
import json
from decimal import Decimal

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
        db.add(p)
        db.flush()
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

def _mk_parts(db, *pns):
    ids = []
    for pn in pns:
        p = DimPart(pn_std=pn)
        db.add(p)
        db.flush()
        ids.append(p.id)
    db.commit()
    return ids


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
    # boss / admin：可建池（≥2 成员）
    for role in ("boss", "admin"):
        c = _mk_client(db, f"ok_{role}", role)
        ids = _mk_parts(db, f"MTX-{role}-1", f"MTX-{role}-2")
        r = c.post("/api/pools", json={"name": f"{role}的池", "member_part_ids": ids})
        assert r.status_code == 200 and r.json()["source"] == "manual"


def test_create_pool_min_members_via_api(db):
    """复审阻塞 1：有效池至少 2 个 PN——0/1 成员建池 400（原先 0 成员会 200）。"""
    c = _mk_client(db, "boss_min", "boss")
    r0 = c.post("/api/pools", json={"name": "空池", "member_part_ids": []})
    assert r0.status_code == 400 and "至少包含 2 个" in r0.json()["detail"]
    (pid,) = _mk_parts(db, "MIN-API-1")
    r1 = c.post("/api/pools", json={"name": "单成员池", "member_part_ids": [pid]})
    assert r1.status_code == 400 and "至少包含 2 个" in r1.json()["detail"]
    p2, p3 = _mk_parts(db, "MIN-API-2", "MIN-API-3")
    duplicate_create = c.post(
        "/api/pools",
        json={"name": "重复成员池", "member_part_ids": [p2, p3, p2]},
    )
    assert duplicate_create.status_code == 400
    assert "成员列表包含重复 part_id" in duplicate_create.json()["detail"]
    # 成员删到 <2 同样 400，且集合不变
    created, ids = _seed_pool(db, name="下限API池", pns=("MIN-API-A", "MIN-API-B"))
    gid = created["group_id"]
    r2 = c.patch(f"/api/pools/{gid}/members",
                 json={"version": 1, "remove_part_ids": [ids[0]]})
    assert r2.status_code == 400 and "至少包含 2 个" in r2.json()["detail"]
    detail = c.get(f"/api/pools/{gid}").json()
    assert detail["member_count"] == 2 and detail["version"] == 1
    (fresh,) = _mk_parts(db, "MIN-API-FRESH")
    duplicate_update = c.patch(
        f"/api/pools/{gid}/members",
        json={"version": 1, "add_part_ids": [fresh, fresh]},
    )
    assert duplicate_update.status_code == 400
    assert "新增成员包含重复 part_id" in duplicate_update.json()["detail"]


def test_grant_pool_manage_to_readonly_user(db):
    """§12：成员维护可单独授权数据维护人员——readonly + action_pool_manage 可维护池，
    但 action_pool_set_policy 未授 → 约束价仍 403（两权限独立）。"""
    c = _mk_client(db, "dm1", "readonly", permissions={"action_pool_manage": True})
    ids = _mk_parts(db, "DM-1", "DM-2")
    r = c.post("/api/pools", json={"name": "数据维护建的池", "member_part_ids": ids})
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
    (fresh,) = _mk_parts(db, "API-FRESH")
    r = c.post("/api/pools", json={"name": "抢人池", "member_part_ids": [ids[0], fresh]})
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
    """data_pool_price_governance=False：详情不返回任何策略对象或历史元数据。

    不能只遮金额叶子：备注、设置人、录入口径和生效时间也会暴露隐藏约束。
    """
    created, _ = _seed_pool(db)
    gid = created["group_id"]
    boss = _mk_client(db, "boss6", "boss")
    boss.put(f"/api/pools/{gid}/price-policy",
             json={"version": 1,
                   "purchase_value": "725.66",
                   "purchase_basis": "inc_tax",
                   "sales_value": "973.45",
                   "note": "采购上限改为725.66，禁止向外泄漏"})

    blind = _mk_client(db, "blind1", "readonly",
                       permissions={"data_pool_price_governance": False})
    resp = blind.get("/api/pools").json()
    item = resp["items"][0]
    assert item["purchase_ceiling_ex_tax"] is None
    assert item["sales_floor_ex_tax"] is None
    detail = blind.get(f"/api/pools/{gid}").json()
    assert detail["purchase_ceiling_ex_tax"] is None
    assert detail["sales_floor_ex_tax"] is None
    assert detail["price_policy"] is None
    assert detail["price_policy_history"] == []
    serialized = json.dumps(detail, ensure_ascii=False)
    for secret in ("725.66", "973.45", "采购上限改为", "inc_tax",
                   "changed_by", "valid_from", "valid_to"):
        assert secret not in serialized
    # 复审非阻塞 1："无权限"必须有明确旗标，前端不允许与"未设置"都显示成 "--"
    assert resp["price_restricted"] is True and item["price_restricted"] is True
    assert detail["price_restricted"] is True

    seen = boss.get(f"/api/pools/{gid}").json()
    assert Decimal(str(seen["price_policy"]["purchase_input_value"])) == Decimal("725.66")
    assert seen["price_policy"]["purchase_input_basis"] == "inc_tax"
    assert seen["price_policy"]["note"] == "采购上限改为725.66，禁止向外泄漏"
    assert seen["price_restricted"] is False
    boss_list = boss.get("/api/pools").json()
    assert boss_list["price_restricted"] is False
    assert boss_list["items"][0]["price_restricted"] is False


def test_policy_coverage_permission_and_filter_anti_inference(db):
    """DEV-07：无治理可见权限拿不到覆盖数字，也不能用缺失筛选反推。"""
    created, _ = _seed_pool(db, name="覆盖率权限池", pns=("COV-A", "COV-B"))
    boss = _mk_client(db, "coverage_boss", "boss")
    set_result = boss.put(
        f"/api/pools/{created['group_id']}/price-policy",
        json={"version": 1, "purchase_value": "100"},
    )
    assert set_result.status_code == 200

    visible = boss.get("/api/pools").json()
    assert visible["coverage_restricted"] is False
    assert visible["coverage"] == {
        "active_pool_count": 1,
        "purchase_set_count": 1,
        "purchase_missing_count": 0,
        "sales_set_count": 0,
        "sales_missing_count": 1,
        "both_set_count": 0,
    }
    assert boss.get("/api/pools?policy_missing=sales").json()["total"] == 1

    blind = _mk_client(
        db, "coverage_blind", "readonly",
        permissions={"data_pool_price_governance": False},
    )
    hidden = blind.get("/api/pools")
    assert hidden.status_code == 200
    assert hidden.json()["coverage_restricted"] is True
    assert hidden.json()["coverage"] is None
    denied = blind.get("/api/pools?policy_missing=sales")
    assert denied.status_code == 403
    assert "约束价" in denied.json()["detail"]


# ---------------------------------------------------------------- 可写必可读（复审阻塞 4）

def test_set_policy_requires_governance_read(db):
    """action_pool_set_policy=True 但 data_pool_price_governance=False 的历史脏组合：
    接口层兜底 403——绝不允许在看不见现值的情况下改约束价。"""
    created, _ = _seed_pool(db, name="兜底池", pns=("GOV-A", "GOV-B"))
    gid = created["group_id"]
    boss = _mk_client(db, "gov_boss", "boss")
    boss.put(f"/api/pools/{gid}/price-policy",
             json={"version": 1, "purchase_value": "100", "sales_value": "90"})
    # 直接落库构造脏组合（账号管理接口已拒绝保存这种组合，见下一个测试）
    dirty = _mk_client(db, "gov_dirty", "readonly",
                       permissions={"action_pool_set_policy": True,
                                    "data_pool_price_governance": False})
    r = dirty.put(f"/api/pools/{gid}/price-policy",
                  json={"version": 2, "purchase_value": "1"})
    assert r.status_code == 403 and "查看权限" in r.json()["detail"]
    # 约束价原样未动
    seen = boss.get(f"/api/pools/{gid}").json()
    assert Decimal(str(seen["purchase_ceiling_ex_tax"])) == Decimal("100.00")
    assert Decimal(str(seen["sales_floor_ex_tax"])) == Decimal("90.00")


def test_accounts_reject_writable_but_unreadable_combo(db):
    """账号管理保存时拒绝"能设约束价但看不见约束价"的组合（建号与改权限都拦）。"""
    admin = _mk_client(db, "combo_admin", "admin")
    bad = {"action_pool_set_policy": True, "data_pool_price_governance": False}
    r = admin.post("/api/accounts", json={
        "username": "combo_u1", "password": "pw123456", "role": "readonly",
        "permissions": bad})
    assert r.status_code == 400 and "必须能查看" in r.json()["detail"]
    # 合法组合可以建号
    ok = admin.post("/api/accounts", json={
        "username": "combo_u2", "password": "pw123456", "role": "readonly",
        "permissions": {"action_pool_set_policy": True}})
    assert ok.status_code == 201
    # 改权限改出非法组合同样 400，且原权限不变
    r2 = admin.put("/api/accounts/combo_u2", json={"permissions": bad})
    assert r2.status_code == 400
    eff = next(u for u in admin.get("/api/accounts").json()
               if u["username"] == "combo_u2")["permissions"]
    assert eff["action_pool_set_policy"] is True
    assert eff["data_pool_price_governance"] is True


def test_policy_single_side_and_unset_via_api(db):
    """PUT /price-policy 单侧语义：缺省=keep、显式 unset 才清空；旧式
    {purchase_value: X, sales_value: null} 请求不再清掉销售侧。"""
    created, _ = _seed_pool(db, name="单侧API池", pns=("SIDE-A", "SIDE-B"))
    gid = created["group_id"]
    c = _mk_client(db, "side_boss", "boss")
    r = c.put(f"/api/pools/{gid}/price-policy",
              json={"version": 1, "purchase_value": "100", "sales_value": "90"})
    assert r.status_code == 200

    # 危险请求形状（阻塞 4 的原始事故）：只想改采购，sales_value 传了 null
    r2 = c.put(f"/api/pools/{gid}/price-policy",
               json={"version": 2, "purchase_value": "80", "sales_value": None})
    assert r2.status_code == 200
    pol = r2.json()["price_policy"]
    assert Decimal(str(pol["purchase_ceiling_ex_tax"])) == Decimal("80.00")
    assert Decimal(str(pol["sales_floor_ex_tax"])) == Decimal("90.00")   # 保住了

    # 显式清空销售侧
    r3 = c.put(f"/api/pools/{gid}/price-policy",
               json={"version": 3, "sales_unset": True})
    assert r3.status_code == 200
    pol3 = r3.json()["price_policy"]
    assert pol3["sales_floor_ex_tax"] is None
    assert Decimal(str(pol3["purchase_ceiling_ex_tax"])) == Decimal("80.00")

    # 两侧都没改 → 400；带旧 version → 409
    assert c.put(f"/api/pools/{gid}/price-policy",
                 json={"version": 4}).status_code == 400
    assert c.put(f"/api/pools/{gid}/price-policy",
                 json={"version": 1, "purchase_value": "70"}).status_code == 409


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
    assert meta["action_keys"] == ["action_pool_manage", "action_pool_set_policy",
                                   "action_account_manage", "action_data_quality_review",
                                   "action_maintenance_roundtrip_apply",
                                   "action_maintenance_project_manage",
                                   "action_maintenance_demand_delete",
                                   "action_maintenance_site_issue_manage"]


# ---------------------------------------------------------------- 端到端闭环

def test_full_management_flow(db):
    """管理页闭环：建池→改名→调成员→设约束→归档→恢复，每步用上一步返回的 version。"""
    p1, p2, p3 = _mk_parts(db, "FLOW-1", "FLOW-2", "FLOW-3")
    c = _mk_client(db, "admin_flow", "admin")

    r = c.post("/api/pools", json={"name": "闭环池", "member_part_ids": [p1, p2],
                                   "note": "建池"})
    assert r.status_code == 200
    gid, ver = r.json()["group_id"], r.json()["version"]

    r = c.patch(f"/api/pools/{gid}", json={"version": ver, "description": "闭环说明"})
    assert r.status_code == 200
    ver = r.json()["version"]

    r = c.patch(f"/api/pools/{gid}/members",
                json={"version": ver, "add_part_ids": [p3]})
    assert r.status_code == 200 and r.json()["member_count"] == 3
    ver = r.json()["version"]

    r = c.put(f"/api/pools/{gid}/price-policy",
              json={"version": ver, "purchase_value": "100"})
    assert r.status_code == 200
    ver = r.json()["version"]

    r = c.post(f"/api/pools/{gid}/archive", json={"version": ver})
    assert r.status_code == 200
    ver = r.json()["version"]
    r = c.post(f"/api/pools/{gid}/restore", json={"version": ver})
    assert r.status_code == 200

    detail = c.get(f"/api/pools/{gid}").json()
    assert detail["member_count"] == 3 and detail["status"] == "active"
    assert {m["pn_std"] for m in detail["members"]} == {"FLOW-1", "FLOW-2", "FLOW-3"}
    assert Decimal(str(detail["price_policy"]["purchase_ceiling_ex_tax"])) == Decimal("100.00")
