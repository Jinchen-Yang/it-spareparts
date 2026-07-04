"""解除替代关系（DELETE /substitutes）：删直连边 + 闭包自动重算 + 审计 + 权限 + 幂等。"""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import hash_password
from app.main import app
from app.models.dimensions import DimPart
from app.models.inventory import PartSubstitute
from app.models.system import SysAuditLog, SysUser
from app.services import part_overview, substitute


def _parts(db, names):
    parts = {}
    for pn in names:
        p = DimPart(pn_std=pn)
        db.add(p)
        parts[pn] = p
    db.flush()
    return parts


def _sub_pns(db, pn):
    p = db.scalar(select(DimPart).where(DimPart.pn_std == pn))
    return {s["pn_std"] for s in part_overview._substitutes(db, p.id)}


def test_remove_direct_edge_and_closure_recompute(db):
    parts = _parts(db, ["CENTER", "SUB1", "SUB2"])
    substitute.add_substitute(db, "CENTER", "SUB1", None, operated_by="admin")
    substitute.add_substitute(db, "CENTER", "SUB2", None, operated_by="admin")
    # 删前：SUB1 经 CENTER 与 SUB2 间接互通
    assert _sub_pns(db, "SUB1") == {"CENTER", "SUB2"}

    r = substitute.remove_substitute(db, "CENTER", "SUB1", operated_by="admin")
    assert r["deleted"] is True
    # 删后：CENTER 只剩 SUB2；SUB1 彻底孤立（闭包读时重算，无残留）
    assert _sub_pns(db, "CENTER") == {"SUB2"}
    assert _sub_pns(db, "SUB1") == set()
    # 行确实没了
    assert db.query(PartSubstitute).count() == 1


def test_remove_writes_audit(db):
    _parts(db, ["A1", "B1"])
    substitute.add_substitute(db, "A1", "B1", "打错了", operated_by="admin")
    substitute.remove_substitute(db, "A1", "B1", operated_by="admin")
    rows = db.query(SysAuditLog).filter(SysAuditLog.entity_type == "substitute",
                                        SysAuditLog.action == "delete").all()
    assert len(rows) == 1
    assert rows[0].before_json["pn_a"] in ("A1", "B1")
    assert rows[0].operated_by == "admin"


def test_remove_order_independent(db):
    """pn_a/pn_b 顺序无关（内部按 a<b 规范序定位）。"""
    _parts(db, ["X9", "Y9"])
    substitute.add_substitute(db, "X9", "Y9", None, operated_by="admin")
    r = substitute.remove_substitute(db, "Y9", "X9", operated_by="admin")   # 反序
    assert r["deleted"] is True
    assert db.query(PartSubstitute).count() == 0


def test_remove_oneway_edge(db):
    _parts(db, ["O1", "O2"])
    substitute.add_substitute(db, "O1", "O2", None, operated_by="admin", direction="one_way")
    assert substitute.remove_substitute(db, "O1", "O2", operated_by="admin")["deleted"] is True
    assert db.query(PartSubstitute).count() == 0


def test_remove_nonexistent_is_idempotent(db):
    _parts(db, ["N1", "N2"])
    r = substitute.remove_substitute(db, "N1", "N2", operated_by="admin")
    assert r["deleted"] is False


def test_remove_indirect_pair_not_deleted(db):
    """间接互通（经中心）的两 spoke 之间无直连边 → deleted=False，不误删中心边。"""
    parts = _parts(db, ["HUB", "S1", "S2"])
    substitute.add_substitute(db, "HUB", "S1", None, operated_by="admin")
    substitute.add_substitute(db, "HUB", "S2", None, operated_by="admin")
    r = substitute.remove_substitute(db, "S1", "S2", operated_by="admin")   # 无直连
    assert r["deleted"] is False
    assert db.query(PartSubstitute).count() == 2      # 两条中心边都在


def test_remove_unknown_part_errors(db):
    _parts(db, ["K1"])
    try:
        substitute.remove_substitute(db, "K1", "GHOST", operated_by="admin")
        assert False, "应抛 SubstituteError"
    except substitute.SubstituteError as e:
        assert "不存在" in str(e)


# ---------- API 层：权限 ----------
def _admin_client(db):
    db.add(SysUser(username="admin", role="admin", password_hash=hash_password("adminpw")))
    db.commit()
    c = TestClient(app)
    tok = c.post("/api/auth/login", json={"username": "admin", "password": "adminpw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def test_api_admin_can_delete(db):
    _parts(db, ["AP1", "AP2"])
    substitute.add_substitute(db, "AP1", "AP2", None, operated_by="admin")
    c = _admin_client(db)
    r = c.delete("/api/substitutes", params={"pn_a": "AP1", "pn_b": "AP2"})
    assert r.status_code == 200 and r.json()["deleted"] is True


def test_api_non_admin_forbidden(db):
    _parts(db, ["FP1", "FP2"])
    substitute.add_substitute(db, "FP1", "FP2", None, operated_by="admin")
    db.add(SysUser(username="sales1", role="sales", password_hash=hash_password("pw123456")))
    db.commit()
    tok = TestClient(app).post("/api/auth/login",
                               json={"username": "sales1", "password": "pw123456"}).json()["token"]
    r = TestClient(app).delete("/api/substitutes", params={"pn_a": "FP1", "pn_b": "FP2"},
                               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
    assert db.query(PartSubstitute).count() == 1      # 未被删
