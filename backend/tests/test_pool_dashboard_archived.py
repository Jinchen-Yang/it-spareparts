"""归档池必须退出老板经营分析（复审阻塞 2）。

归档池成员可再加入新有效池：若清单/总数/savings 排名/analyze 仍算归档池，
同一 PN 会在旧归档池与新有效池里被双份统计。归档档案的查看走管理接口
/api/pools?status=archived，与经营分析彻底分流。
"""
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.main import app
from app.models.dimensions import DimPart
from app.models.system import SysUser
from app.services import pool, pool_catalog


def _part(db, pn):
    p = DimPart(pn_std=pn)
    db.add(p); db.flush()
    return p.id


def _pool(db, name, pns):
    ids = [_part(db, pn) for pn in pns]
    return pool_catalog.create_pool(db, name=name, member_part_ids=ids, operated_by="t")


def _boss(db, username="arch_boss"):
    db.add(SysUser(username=username, role="boss",
                   password_hash=hash_password("pw123456")))
    db.commit()
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": "pw123456"})
    assert r.status_code == 200
    c.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return c


# ---------------------------------------------------------------- 服务层

def test_archived_pool_leaves_dashboard_list_total_and_sorts(db):
    """归档后：total 不计、两种排序（member_count / savings 全局排名）都不出现。"""
    a = _pool(db, "在营池", ("ARCH-A1", "ARCH-A2"))
    b = _pool(db, "退役池", ("ARCH-B1", "ARCH-B2", "ARCH-B3"))
    assert pool.list_pools(db)["total"] == 2
    pool_catalog.archive_pool(db, group_id=b["group_id"], version=1, operated_by="t")

    for sort in ("member_count", "savings"):
        out = pool.list_pools(db, sort=sort)
        assert out["total"] == 1, f"sort={sort}: 归档池不得计入 total"
        assert [i["group_id"] for i in out["items"]] == [a["group_id"]], (
            f"sort={sort}: 归档池不得出现在清单")


def test_archived_pool_analyze_returns_none(db):
    b = _pool(db, "退役分析池", ("ARCH-AN1", "ARCH-AN2"))
    gid = b["group_id"]
    assert pool.analyze(db, gid) is not None
    pool_catalog.archive_pool(db, group_id=gid, version=1, operated_by="t")
    assert pool.analyze(db, gid) is None   # 归档池不是当前经营池


def test_archived_member_in_new_pool_counted_once(db):
    """归档池成员加入新有效池后，看板只统计新池——同一 PN 绝不双份出现。"""
    shared = _part(db, "ARCH-SHARED")
    extra_old = _part(db, "ARCH-OLD")
    old = pool_catalog.create_pool(db, name="旧池", member_part_ids=[shared, extra_old],
                                   operated_by="t")
    pool_catalog.archive_pool(db, group_id=old["group_id"], version=1, operated_by="t")
    extra_new = _part(db, "ARCH-NEW")
    new = pool_catalog.create_pool(db, name="新池", member_part_ids=[shared, extra_new],
                                   operated_by="t")

    out = pool.list_pools(db, sort="savings")
    assert out["total"] == 1
    assert [i["group_id"] for i in out["items"]] == [new["group_id"]]
    # 全部在营池的成员并集里 shared 只出现一次（唯一入口是新池）
    d = pool.analyze(db, new["group_id"])
    assert sum(1 for m in d["members"] if m["pn_std"] == "ARCH-SHARED") == 1
    assert pool.analyze(db, old["group_id"]) is None


# ---------------------------------------------------------------- 接口层

def test_dashboard_pool_detail_archived_404_with_reason(db):
    b = _pool(db, "退役详情池", ("ARCH-D1", "ARCH-D2"))
    gid = b["group_id"]
    c = _boss(db)
    assert c.get(f"/api/dashboard/pool/{gid}").status_code == 200
    pool_catalog.archive_pool(db, group_id=gid, version=1, operated_by="t")
    r = c.get(f"/api/dashboard/pool/{gid}")
    assert r.status_code == 404
    assert "已归档" in r.json()["detail"]          # 明确不可参与当前分析，而非含糊"不存在"
    r2 = c.get("/api/dashboard/pool/999999")
    assert r2.status_code == 404 and "不存在" in r2.json()["detail"]


def test_dashboard_pools_endpoint_excludes_archived(db):
    a = _pool(db, "在营池2", ("ARCH-E1", "ARCH-E2"))
    b = _pool(db, "退役池2", ("ARCH-F1", "ARCH-F2"))
    pool_catalog.archive_pool(db, group_id=b["group_id"], version=1, operated_by="t")
    c = _boss(db, "arch_boss2")
    for sort in ("member_count", "savings"):
        body = c.get(f"/api/dashboard/pools?sort={sort}").json()
        assert body["total"] == 1
        assert [i["group_id"] for i in body["items"]] == [a["group_id"]]


def test_management_api_still_serves_archived_archive(db):
    """管理接口不受影响：?status=archived 仍能查归档档案，详情可读（成员/约束历史保留）。"""
    b = _pool(db, "档案池", ("ARCH-G1", "ARCH-G2"))
    gid = b["group_id"]
    pool_catalog.archive_pool(db, group_id=gid, version=1, operated_by="t")
    c = _boss(db, "arch_boss3")
    archived = c.get("/api/pools?status=archived").json()
    assert archived["total"] == 1
    assert archived["items"][0]["group_id"] == gid
    assert c.get("/api/pools?status=active").json()["total"] == 0
    detail = c.get(f"/api/pools/{gid}").json()
    assert detail["status"] == "archived"
    assert {m["pn_std"] for m in detail["members"]} == {"ARCH-G1", "ARCH-G2"}
