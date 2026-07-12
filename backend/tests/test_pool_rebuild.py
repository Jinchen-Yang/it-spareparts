"""人工池唯一真值回归（Slice 1，互通PN池价格分析 §24 合并门槛）。

本文件原测自动重算 rebuild——Slice 1 起自动重算已删除，改测三条不变量：
1. pool.rebuild 写路径不存在（全仓不再有任何自动写池代码路径）；
2. POST /dashboard/pool/rebuild 恒 410 Gone（admin）/403（非 admin），权限面不扩大；
3. 替代关系（part_substitute）增删**不会**改池——池、成员只随 pool_catalog 变化。
新池 group_id 仍取持久序列：单调递增、退役（归档）ID 永不复用。
"""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import hash_password
from app.main import app
from app.models.dimensions import DimPart
from app.models.inventory import PartPool, PartPoolMember, PartSubstitute
from app.models.system import SysUser
from app.services import pool, pool_catalog, substitute


def _part(db, pn):
    p = DimPart(pn_std=pn)
    db.add(p); db.flush()
    return p.id


def _pool_snapshot(db):
    pools = {p.group_id: (p.name, p.status, p.member_count)
             for p in db.execute(select(PartPool)).scalars()}
    members = set(db.execute(
        select(PartPoolMember.group_id, PartPoolMember.part_id)).all())
    return pools, members


def _client(db, username, role, password="pw123456"):
    db.add(SysUser(username=username, role=role, password_hash=hash_password(password)))
    db.commit()
    c = TestClient(app)
    tok = c.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def test_rebuild_write_path_removed():
    """服务层不再暴露 rebuild——自动重算不能写池（§24：自动重算不能写池）。"""
    assert not hasattr(pool, "rebuild")


def test_rebuild_endpoint_410_for_admin(db):
    c = _client(db, "admin1", "admin")
    r = c.post("/api/dashboard/pool/rebuild")
    assert r.status_code == 410
    assert "人工维护" in r.json()["detail"]


def test_rebuild_endpoint_403_for_non_admin(db):
    c = _client(db, "sales1", "sales")
    r = c.post("/api/dashboard/pool/rebuild")
    assert r.status_code == 403


def test_substitute_changes_never_touch_pools(db):
    """替代关系增删不改池：人工池是唯一真值（§23 功能正确性第 1 条）。"""
    a, b, c_ = _part(db, "MT-A"), _part(db, "MT-B"), _part(db, "MT-C")
    pool_catalog.create_pool(db, name="人工池AB", member_part_ids=[a, b], operated_by="t")
    before = _pool_snapshot(db)

    # 走 service 正路增删替代关系（曾经的池边来源）
    substitute.add_substitute(db, "MT-B", "MT-C", None, operated_by="t")
    assert _pool_snapshot(db) == before
    substitute.remove_substitute(db, "MT-B", "MT-C", operated_by="t")
    assert _pool_snapshot(db) == before

    # 直插已生效双向边（旧 rebuild 的成池条件）同样不影响
    db.add(PartSubstitute(part_id_a=min(a, c_), part_id_b=max(a, c_),
                          status="active", direction="both", substitute_type="same_spec"))
    db.commit()
    assert _pool_snapshot(db) == before


def test_new_pool_ids_monotonic_never_reused(db):
    """group_id 单调递增；归档（退役）池的 ID 不会被新池复用。"""
    a, b = _part(db, "SEQ-A"), _part(db, "SEQ-B")
    db.commit()
    p1 = pool_catalog.create_pool(db, name="池一", member_part_ids=[a], operated_by="t")
    p2 = pool_catalog.create_pool(db, name="池二", member_part_ids=[b], operated_by="t")
    assert p2["group_id"] > p1["group_id"]
    pool_catalog.archive_pool(db, group_id=p2["group_id"], version=p2["version"], operated_by="t")
    p3 = pool_catalog.create_pool(db, name="池三", operated_by="t")
    assert p3["group_id"] > p2["group_id"]
