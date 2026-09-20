"""需求单页面直改/直建 API 层边界（v1.36 Phase E）。

覆盖：
- OCC 失败关闭：PATCH/clear 省略 expected_digest → 422（新接口未上线，
  无旧客户端须兼容——不允许绕过并发校验）；
- create 的 idempotency_key 必填（422）；
- scope：受限账号（own_maintenance_projects_only）跨项目 patch/clear/create
  全部 403/400 拒绝；full scope（admin）放行；
- create 后行在实际项目读侧（master workbook _assigned_lines 同构 join）
  可见；
- NaN/Infinity/超界字串 qty → 400（不是 500）；
- API GET 行 digest 与 service/模型侧 digest 一致。
"""

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.api import maintenance_demands as demands_api
from app.auth import hash_password
from app.models.dimensions import DimPart
from app.models.system import SysUser
from app.services import maintenance_demand_manual as manual
from tests import factories as f
from tests.test_demand_manual_edit import (
    _batch, _make_project, _make_project_standalone, _seed_line,
)

PW = "synthetic-password-123"


def _client(db, *, username: str, role: str, permissions: dict | None = None,
            ) -> TestClient:
    user = SysUser(username=username, role=role, display_name=f"合成 {username}",
                   password_hash=hash_password(PW), permissions=permissions)
    db.add(user)
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(demands_api.router, prefix="/api")
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": username, "password": PW})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _admin_client(db) -> TestClient:
    return _client(db, username="admin-api-e", role="admin")


def _scoped_client(db, *, owned: set[str]) -> TestClient:
    """own_maintenance_projects_only 开、可见集 = owned 的受限账号。"""
    return _client(
        db, username="sales-scoped-e", role="sales",
        permissions={"own_maintenance_projects_only": True,
                     "page_maintenance": True,
                     "action_maintenance_demand_manage": True},
    )


def test_patch_without_expected_digest_rejected_422(db):
    """OCC 失败关闭：省略 expected_digest 的 PATCH → 422，不落库。"""
    order, line = _seed_line(db, order_raw="M-API1", line_raw="ML-API1", pn="PN-API1")
    _make_project(db, tag="API1", source_order_id=order.raw_order_id)
    c = _admin_client(db)
    r = c.patch(f"/api/maintenance/demands/lines/{line.raw_line_id}",
                json={"updates": {"qty": 3}, "reason": "r"})
    assert r.status_code == 422, r.text
    db.refresh(line)
    assert line.qty is None or str(line.qty) == "2.000", "422 时不得落库任何修改"


def test_clear_without_expected_digest_rejected_422(db):
    order, line = _seed_line(db, order_raw="M-API2", line_raw="ML-API2", pn="PN-API2")
    _make_project(db, tag="API2", source_order_id=order.raw_order_id)
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"qty": 9}, reason="r", operated_by="t")
    db.commit()
    c = _admin_client(db)
    r = c.post(f"/api/maintenance/demands/lines/{line.raw_line_id}/clear-override",
               json={"field_name": "qty", "reason": "r"})
    assert r.status_code == 422, r.text


def test_create_without_idempotency_key_rejected_422(db):
    project = _make_project_standalone(db, tag="API3")
    db.add(DimPart(pn_std="PN-API3", status="active"))
    db.commit()
    c = _admin_client(db)
    r = c.post("/api/maintenance/demands/lines", json={
        "order_date": "2026-03-01", "project_id": project.project_id,
        "pn_std": "PN-API3", "qty": 1, "reason": "r",
    })
    assert r.status_code == 422, r.text


def _create_body(project_id: str, **over) -> dict:
    body = {
        "order_date": "2026-03-01", "project_id": project_id,
        "pn_std": "PN-API4", "qty": 2, "return_qty": 0,
        "reason": "r", "idempotency_key": "api-idem-0001",
    }
    body.update(over)
    return body


def test_create_then_line_visible_in_project_read(db):
    """#2 API 全链：create → assignment → master workbook 读侧可见 → GET 行。"""
    from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
    from app.models.maintenance_source_assignment import (
        MaintenanceSourceOrderAssignment,
    )
    project = _make_project_standalone(db, tag="API4")
    db.add(DimPart(pn_std="PN-API4", status="active"))
    db.commit()
    c = _admin_client(db)
    r = c.post("/api/maintenance/demands/lines", json=_create_body(project.project_id))
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["order_no"].startswith("PAGE-")

    order_raw = db.scalar(select(FMaintenanceOrder.raw_order_id).where(
        FMaintenanceOrder.order_no == created["order_no"]))
    # master workbook 读侧同构 join（_assigned_lines 的核心谓词）
    from sqlalchemy import func
    visible = db.scalar(
        select(func.count(FMaintenanceLine.id))
        .select_from(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .join(MaintenanceSourceOrderAssignment,
              MaintenanceSourceOrderAssignment.source_order_id
              == FMaintenanceOrder.raw_order_id)
        .where(FMaintenanceOrder.raw_order_id == order_raw,
               MaintenanceSourceOrderAssignment.is_active.is_(True),
               FMaintenanceLine.is_active.is_(True))
    )
    assert visible == 1

    # GET 行列表：digest 与 service 侧一致
    r2 = c.get(f"/api/maintenance/demands/orders/{order_raw}/lines")
    assert r2.status_code == 200, r2.text
    items = r2.json()["items"]
    assert len(items) == 1
    line = db.execute(select(FMaintenanceLine).where(
        FMaintenanceLine.raw_line_id == items[0]["raw_line_id"])).scalar_one()
    assert items[0]["digest"] == manual._digest(manual._line_snapshot(line))

    # API PATCH 带正确 digest 成功；返回新 digest
    token = items[0]["digest"]
    r3 = c.patch(f"/api/maintenance/demands/lines/{line.raw_line_id}",
                 json={"updates": {"qty": 5}, "reason": "r",
                       "expected_digest": token})
    assert r3.status_code == 200, r3.text
    assert r3.json()["qty"] == "5.000"
    new_token = r3.json()["digest"]
    assert new_token != token

    # 旧 token 再改 → 409
    r4 = c.patch(f"/api/maintenance/demands/lines/{line.raw_line_id}",
                 json={"updates": {"qty": 6}, "reason": "r",
                       "expected_digest": token})
    assert r4.status_code == 409, r4.text


def test_scoped_account_cross_project_writes_rejected(db):
    """#1 API：受限账号（只见 proj-OWNED）对别的项目 patch/clear/create 全拒。"""
    order, line = _seed_line(db, order_raw="M-API5", line_raw="ML-API5", pn="PN-API5")
    _make_project(db, tag="API5", source_order_id=order.raw_order_id)
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id,
                             updates={"qty": 9}, reason="r", operated_by="t")
    db.commit()
    c = _scoped_client(db, owned={"proj-OWNED-API"})

    r_patch = c.patch(f"/api/maintenance/demands/lines/{line.raw_line_id}",
                      json={"updates": {"qty": 3}, "reason": "r",
                            "expected_digest": "0" * 64})
    assert r_patch.status_code == 403, r_patch.text

    r_clear = c.post(
        f"/api/maintenance/demands/lines/{line.raw_line_id}/clear-override",
        json={"field_name": "qty", "reason": "r", "expected_digest": "0" * 64})
    assert r_clear.status_code == 403, r_clear.text

    other_project = _make_project_standalone(db, tag="API5B")
    db.add(DimPart(pn_std="PN-API5B", status="active"))
    db.commit()
    r_create = c.post("/api/maintenance/demands/lines", json=_create_body(
        other_project.project_id, idempotency_key="api-idem-cross-0001"))
    assert r_create.status_code in (400, 403), r_create.text

    db.refresh(line)
    assert str(line.qty) == "9.000", "受限账号的三类写都不得落库"


def test_full_occ_chain_get_patch_conflict_clear(db):
    """发布阻断 P1 回归：真实 API 链路 GET → PATCH 成功 → 原 GET token 二次
    409 → GET 新 token → clear 成功。GET 的 digest 必须与 PATCH OCC 输入
    同源（canonical _line_snapshot），否则前端首改即 409。"""
    order, line = _seed_line(db, order_raw="M-API7", line_raw="ML-API7", pn="PN-API7")
    _make_project(db, tag="API7", source_order_id=order.raw_order_id)
    c = _admin_client(db)

    got = c.get(f"/api/maintenance/demands/orders/{order.raw_order_id}/lines")
    assert got.status_code == 200, got.text
    token1 = got.json()["items"][0]["digest"]
    assert token1 == manual._digest(manual._line_snapshot(line)), \
        "GET digest 必须与 service canonical snapshot 同源"

    r1 = c.patch(f"/api/maintenance/demands/lines/{line.raw_line_id}",
                 json={"updates": {"qty": 4}, "reason": "v1",
                       "expected_digest": token1})
    assert r1.status_code == 200, r1.text
    assert r1.json()["qty"] == "4.000"
    token2 = r1.json()["digest"]
    assert token2 != token1

    # 原 GET token 重放 → 409
    r2 = c.patch(f"/api/maintenance/demands/lines/{line.raw_line_id}",
                 json={"updates": {"qty": 5}, "reason": "v2",
                       "expected_digest": token1})
    assert r2.status_code == 409, r2.text

    # 重新 GET 拿新 token → clear 成功，返回又一个新的 digest
    got2 = c.get(f"/api/maintenance/demands/orders/{order.raw_order_id}/lines")
    token3 = got2.json()["items"][0]["digest"]
    assert token3 == token2, "无并发时 GET token 应与上次写返回一致"
    r3 = c.post(f"/api/maintenance/demands/lines/{line.raw_line_id}/clear-override",
                json={"field_name": "qty", "reason": "回原值",
                      "expected_digest": token3})
    assert r3.status_code == 200, r3.text
    assert r3.json()["qty"] == "2.000"
    assert r3.json()["digest"] != token3


def test_create_returns_canonical_digest_and_replay_matches(db):
    """create 首创与幂等重放都回 canonical digest，可直接用于后续 PATCH。"""
    project = _make_project_standalone(db, tag="API8")
    db.add(DimPart(pn_std="PN-API8", status="active"))
    db.commit()
    c = _admin_client(db)
    r1 = c.post("/api/maintenance/demands/lines", json=_create_body(
        project.project_id, pn_std="PN-API8", idempotency_key="api-idem-digest-01"))
    assert r1.status_code == 201, r1.text
    token = r1.json().get("digest")
    assert token, "create 首创必须返回 digest"

    # 同 key 同 payload 重放也带 digest
    r2 = c.post("/api/maintenance/demands/lines", json=_create_body(
        project.project_id, pn_std="PN-API8", idempotency_key="api-idem-digest-01"))
    assert r2.status_code == 201 and r2.json()["replayed"] is True
    assert r2.json()["digest"] == token, "重放 digest 必须与首创一致"

    # 该 token 可直接 PATCH（GET/写侧 digest 同源的端到端证据）
    r3 = c.patch(f"/api/maintenance/demands/lines/{r1.json()['raw_line_id']}",
                 json={"updates": {"qty": 6}, "reason": "r",
                       "expected_digest": token})
    assert r3.status_code == 200, r3.text
    assert r3.json()["qty"] == "6.000"


def test_noop_patch_returns_digest(db):
    """no-op patch（值未变）changed=False 但必须带当前 digest。"""
    order, line = _seed_line(db, order_raw="M-API9", line_raw="ML-API9", pn="PN-API9")
    _make_project(db, tag="API9", source_order_id=order.raw_order_id)
    c = _admin_client(db)
    token = manual._digest(manual._line_snapshot(line))
    r = c.patch(f"/api/maintenance/demands/lines/{line.raw_line_id}",
                json={"updates": {"qty": 2}, "reason": "同值",
                      "expected_digest": token})  # qty 已是 2.000 → no-op
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is False
    assert r.json()["digest"] == token, "no-op digest 应与请求 token 一致"


def test_nan_and_oversized_qty_strings_rejected_not_500(db):
    """字串 NaN/Infinity/超界 → 400 业务校验（不是 500/截断）。"""
    order, line = _seed_line(db, order_raw="M-API6", line_raw="ML-API6", pn="PN-API6")
    _make_project(db, tag="API6", source_order_id=order.raw_order_id)
    c = _admin_client(db)
    token = manual._digest(manual._line_snapshot(line))
    for bad in ("NaN", "Infinity", "-Infinity", "1e999", "abc"):
        r = c.patch(f"/api/maintenance/demands/lines/{line.raw_line_id}",
                    json={"updates": {"qty": bad}, "reason": "r",
                          "expected_digest": token})
        assert r.status_code == 400, f"{bad!r} → {r.status_code} {r.text}"
