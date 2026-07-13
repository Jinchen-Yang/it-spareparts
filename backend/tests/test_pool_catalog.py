"""PoolCatalog 服务层：建池/编辑/成员/约束价/归档恢复 + 乐观锁 + 唯一性 + 审计。

覆盖规格 §11（并发与安全）/§13（约束价口径）/§15（模型语义）/§21-9（不经审核直接生效）。
"""
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.models.inventory import PartPoolMember, PartPoolPricePolicy
from app.models.system import SysAuditLog
from app.services import pool_catalog as svc


def _part(db, pn, brand=None, description=None):
    p = DimPart(pn_std=pn, brand=brand, description=description)
    db.add(p); db.flush()
    return p.id


def _pool(db, name, pns=None, **kw):
    """满足"有效池≥2成员"规则的测试池（《互通PN池》核心规则 5）。"""
    ids = [_part(db, pn) for pn in (pns or (f"{name}-A", f"{name}-B"))]
    kw.setdefault("operated_by", "t")
    return svc.create_pool(db, name=name, member_part_ids=ids, **kw)


def _audits(db, gid, action=None):
    stmt = select(SysAuditLog).where(SysAuditLog.entity_type == "part_pool",
                                     SysAuditLog.entity_id == gid)
    if action:
        stmt = stmt.where(SysAuditLog.action == action)
    return db.execute(stmt).scalars().all()


# ---------------------------------------------------------------- 建池

def test_create_pool_basic(db):
    a, b = _part(db, "CAT-A", brand="HP"), _part(db, "CAT-B")
    r = svc.create_pool(db, name="  4T SAS 硬盘池 ", description="客户不指定品牌可互换",
                        member_part_ids=[a, b], note="首建", operated_by="boss")
    assert r["name"] == "4T SAS 硬盘池"           # 去首尾空白
    assert r["source"] == "manual" and r["status"] == "active"
    assert r["version"] == 1 and r["member_count"] == 2
    assert r["created_by"] == "boss"
    logs = _audits(db, r["group_id"], "create")
    assert len(logs) == 1 and logs[0].operated_by == "boss" and logs[0].reason == "首建"
    assert sorted(logs[0].after_json["members"]) == ["CAT-A", "CAT-B"]


def test_create_pool_rejects_zero_and_one_member(db):
    """《互通PN池》核心规则 5：每个有效池至少包含两个 PN——0/1 成员建池一律 400，
    不存在"先建空壳后加成员"的路径（那需要引入非有效状态并另行评审）。"""
    with pytest.raises(svc.PoolCatalogError, match="至少包含 2 个"):
        svc.create_pool(db, name="空池", operated_by="t")
    with pytest.raises(svc.PoolCatalogError, match="至少包含 2 个"):
        svc.create_pool(db, name="空池2", member_part_ids=[], operated_by="t")
    a = _part(db, "CAT-ONLY")
    with pytest.raises(svc.PoolCatalogError, match="至少包含 2 个"):
        svc.create_pool(db, name="单成员池", member_part_ids=[a], operated_by="t")
    # 同一 part_id 重复给两次 = 去重后 1 个，同样拒绝
    with pytest.raises(svc.PoolCatalogError, match="至少包含 2 个"):
        svc.create_pool(db, name="重复成员池", member_part_ids=[a, a], operated_by="t")


def test_create_pool_validations(db):
    a = _part(db, "CAT-V")
    with pytest.raises(svc.PoolCatalogError, match="不能为空"):
        svc.create_pool(db, name="   ", operated_by="t")
    with pytest.raises(svc.PoolCatalogError, match="不存在"):
        svc.create_pool(db, name="幽灵成员", member_part_ids=[a, 999999], operated_by="t")


def test_create_pool_rejects_merged_part(db):
    a = _part(db, "CAT-MG")
    target = _part(db, "CAT-MG-TARGET")
    db.execute(text("UPDATE dim_part SET status='merged', merged_into_id=:t WHERE id=:i"),
               {"i": a, "t": target})
    db.commit()
    with pytest.raises(svc.PoolCatalogError, match="已合并"):
        svc.create_pool(db, name="墓碑成员", member_part_ids=[a, target], operated_by="t")


def test_pn_in_active_pool_conflicts_with_pool_name(db):
    """PN 已属其他有效池 → 明确提示现有池（§11），冲突而非静默加入。"""
    a = _part(db, "CAT-DUP")
    b = _part(db, "CAT-DUP-B")
    c = _part(db, "CAT-DUP-C")
    svc.create_pool(db, name="老池", member_part_ids=[a, b], operated_by="t")
    with pytest.raises(svc.PoolConflictError) as ei:
        svc.create_pool(db, name="新池", member_part_ids=[a, c], operated_by="t")
    assert "老池" in str(ei.value) and "CAT-DUP" in str(ei.value)


# ---------------------------------------------------------------- 编辑 + 乐观锁

def test_update_pool_rename_and_version_bump(db):
    r = _pool(db, "旧名")
    r2 = svc.update_pool(db, group_id=r["group_id"], version=1,
                         updates={"name": "新名", "description": "说明"}, operated_by="u2")
    assert r2["name"] == "新名" and r2["version"] == 2 and r2["updated_by"] == "u2"
    assert _audits(db, r["group_id"], "update")


def test_update_pool_stale_version_conflicts(db):
    r = _pool(db, "并发池")
    svc.update_pool(db, group_id=r["group_id"], version=1, updates={"name": "先手"},
                    operated_by="a")
    with pytest.raises(svc.PoolConflictError, match="已被他人修改"):
        svc.update_pool(db, group_id=r["group_id"], version=1, updates={"name": "后手"},
                        operated_by="b")


def test_update_pool_not_found_returns_none(db):
    assert svc.update_pool(db, group_id=999999, version=1, updates={"name": "x"}) is None


def test_optimistic_lock_across_two_sessions(db):
    """双会话乐观锁：A、B 同时读 v1，A 保存后 B 携旧版本必须 409，不静默覆盖（§11）。"""
    r = _pool(db, "双会话池")
    db.commit()
    s1, s2 = SessionLocal(), SessionLocal()
    try:
        svc.update_pool(s1, group_id=r["group_id"], version=1, updates={"name": "A改"},
                        operated_by="a")
        with pytest.raises(svc.PoolConflictError):
            svc.update_pool(s2, group_id=r["group_id"], version=1, updates={"name": "B改"},
                            operated_by="b")
    finally:
        s1.close(); s2.close()


def test_part_advisory_lock_serializes_concurrent_joins(db):
    """同一 PN 的并发入池被事务级 advisory 锁串行化（提交后自动释放）。"""
    pid = _part(db, "CAT-LOCK")
    db.commit()
    s1, s2 = SessionLocal(), SessionLocal()
    try:
        svc._lock_parts(s1, [pid])          # s1 事务持锁
        got = s2.execute(text("SELECT pg_try_advisory_xact_lock(:ns, :pid)"),
                         {"ns": svc._PART_LOCK_NS, "pid": pid}).scalar()
        assert got is False                  # s2 拿不到 → 只能等 s1 提交后重查冲突
        s1.commit()                          # 释放
        got2 = s2.execute(text("SELECT pg_try_advisory_xact_lock(:ns, :pid)"),
                          {"ns": svc._PART_LOCK_NS, "pid": pid}).scalar()
        assert got2 is True
        s2.commit()
    finally:
        s1.close(); s2.close()


# ---------------------------------------------------------------- 成员维护

def test_update_members_add_remove_syncs_count(db):
    a, b, c = _part(db, "CAT-M1"), _part(db, "CAT-M2"), _part(db, "CAT-M3")
    r = svc.create_pool(db, name="成员池", member_part_ids=[a, b], operated_by="t")
    r2 = svc.update_members(db, group_id=r["group_id"], version=1,
                            add_part_ids=[c], remove_part_ids=[a], operated_by="t")
    assert r2["member_count"] == 2 and r2["version"] == 2
    left = set(db.scalars(select(PartPoolMember.part_id)
                          .where(PartPoolMember.group_id == r["group_id"])).all())
    assert left == {b, c}
    log = _audits(db, r["group_id"], "members")[0]
    assert log.after_json["added"] == ["CAT-M3"] and log.after_json["removed"] == ["CAT-M1"]


def test_update_members_validations(db):
    a, extra, b = _part(db, "CAT-M4"), _part(db, "CAT-M4X"), _part(db, "CAT-M5")
    r = svc.create_pool(db, name="校验池", member_part_ids=[a, extra], operated_by="t")
    with pytest.raises(svc.PoolCatalogError, match="没有要增删"):
        svc.update_members(db, group_id=r["group_id"], version=1)
    with pytest.raises(svc.PoolCatalogError, match="同时增删"):
        svc.update_members(db, group_id=r["group_id"], version=1,
                           add_part_ids=[a], remove_part_ids=[a])
    with pytest.raises(svc.PoolCatalogError, match="重复加入"):
        svc.update_members(db, group_id=r["group_id"], version=1, add_part_ids=[a])
    with pytest.raises(svc.PoolCatalogError, match="不是本池成员"):
        svc.update_members(db, group_id=r["group_id"], version=1, remove_part_ids=[b])


def test_update_members_rejects_below_two_and_keeps_set_intact(db):
    """成员调整后终态 <2 → 整体拒绝：任何删除都不落库，成员集合/计数/版本原样。"""
    a, b, c = _part(db, "CAT-MIN1"), _part(db, "CAT-MIN2"), _part(db, "CAT-MIN3")
    r = svc.create_pool(db, name="下限池", member_part_ids=[a, b, c], operated_by="t")
    gid = r["group_id"]

    def _members():
        return set(db.scalars(select(PartPoolMember.part_id)
                              .where(PartPoolMember.group_id == gid)).all())

    # 剩 1 个：拒绝且未删除任何成员
    with pytest.raises(svc.PoolCatalogError, match="至少包含 2 个"):
        svc.update_members(db, group_id=gid, version=1, remove_part_ids=[a, b],
                           operated_by="t")
    db.rollback()
    assert _members() == {a, b, c}
    # 剩 0 个：同样拒绝
    with pytest.raises(svc.PoolCatalogError, match="至少包含 2 个"):
        svc.update_members(db, group_id=gid, version=1, remove_part_ids=[a, b, c],
                           operated_by="t")
    db.rollback()
    assert _members() == {a, b, c}
    detail = svc.get_pool(db, gid)
    assert detail["member_count"] == 3 and detail["version"] == 1
    assert not _audits(db, gid, "members")   # 失败的调整不留成员审计
    # 有增有删、终态仍 ≥2 → 正常放行（回归保护）
    d = _part(db, "CAT-MIN4")
    ok = svc.update_members(db, group_id=gid, version=1,
                            add_part_ids=[d], remove_part_ids=[a, b], operated_by="t")
    assert ok["member_count"] == 2 and _members() == {c, d}


def test_update_members_cross_pool_conflict(db):
    a, a2 = _part(db, "CAT-X1"), _part(db, "CAT-X1B")
    b, b2 = _part(db, "CAT-X2"), _part(db, "CAT-X2B")
    svc.create_pool(db, name="甲池", member_part_ids=[a, a2], operated_by="t")
    r = svc.create_pool(db, name="乙池", member_part_ids=[b, b2], operated_by="t")
    with pytest.raises(svc.PoolConflictError, match="甲池"):
        svc.update_members(db, group_id=r["group_id"], version=1, add_part_ids=[a],
                           operated_by="t")


# ---------------------------------------------------------------- 约束价

def test_policy_ex_tax_passthrough_and_inc_tax_conversion(db):
    """统一未税入库：未税原值、含税 ÷1.13（§13/§26-3），原始录入值与口径保留。"""
    r = _pool(db, "价池")
    out = svc.set_price_policy(db, group_id=r["group_id"], version=1,
                               purchase_value=Decimal("113"), purchase_basis="inc_tax",
                               sales_value=Decimal("973.45"), sales_basis="ex_tax",
                               note="首次设置", operated_by="boss")
    pol = out["price_policy"]
    assert pol["purchase_ceiling_ex_tax"] == Decimal("100.00")     # 113 ÷ 1.13
    assert pol["purchase_input_value"] == Decimal("113")
    assert pol["purchase_input_basis"] == "inc_tax"
    assert pol["sales_floor_ex_tax"] == Decimal("973.45")          # 未税原值
    assert pol["sales_input_basis"] == "ex_tax"
    assert pol["changed_by"] == "boss"
    assert out["version"] == 2


def test_policy_history_close_and_insert(db):
    """修改=关闭旧行+插入新行，不覆盖历史（§15.3）；每池仅一条当前策略。"""
    r = _pool(db, "历史池")
    gid = r["group_id"]
    svc.set_price_policy(db, group_id=gid, version=1,
                         purchase_value=Decimal("100"), operated_by="a")
    svc.set_price_policy(db, group_id=gid, version=2,
                         purchase_value=Decimal("120"), note="上调", operated_by="b")
    rows = db.execute(select(PartPoolPricePolicy)
                      .where(PartPoolPricePolicy.group_id == gid)
                      .order_by(PartPoolPricePolicy.id)).scalars().all()
    assert len(rows) == 2
    assert rows[0].valid_to is not None and rows[1].valid_to is None
    assert rows[1].purchase_ceiling_ex_tax == Decimal("120.00")
    detail = svc.get_pool(db, gid)
    assert detail["price_policy"]["purchase_ceiling_ex_tax"] == Decimal("120.00")
    assert len(detail["price_policy_history"]) == 2


def test_policy_single_side_set_keeps_other_side(db):
    """单侧更新（复审阻塞 4）：只改一侧时另一侧三字段（未税值/原始录入值/口径）原样保留，
    普通 None **不是**清空。"""
    r = _pool(db, "单侧池")
    gid = r["group_id"]
    svc.set_price_policy(db, group_id=gid, version=1,
                         purchase_value=Decimal("113"), purchase_basis="inc_tax",
                         sales_value=Decimal("973.45"), sales_basis="ex_tax", operated_by="t")
    # 只改采购：sales 三字段保持
    out = svc.set_price_policy(db, group_id=gid, version=2,
                               purchase_value=Decimal("90"), operated_by="t")
    pol = out["price_policy"]
    assert pol["purchase_ceiling_ex_tax"] == Decimal("90.00")
    assert pol["purchase_input_basis"] == "ex_tax"
    assert pol["sales_floor_ex_tax"] == Decimal("973.45")
    assert pol["sales_input_value"] == Decimal("973.45")
    assert pol["sales_input_basis"] == "ex_tax"
    # 只改销售：purchase 三字段保持
    out2 = svc.set_price_policy(db, group_id=gid, version=3,
                                sales_value=Decimal("1130"), sales_basis="inc_tax",
                                operated_by="t")
    pol2 = out2["price_policy"]
    assert pol2["sales_floor_ex_tax"] == Decimal("1000.00")
    assert pol2["purchase_ceiling_ex_tax"] == Decimal("90.00")
    assert pol2["purchase_input_value"] == Decimal("90")
    assert pol2["purchase_input_basis"] == "ex_tax"
    # 每次都是关旧行插新行
    assert len(svc.get_pool(db, gid)["price_policy_history"]) == 3


def test_policy_explicit_unset_only_clears_target_side(db):
    """显式 unset 才清空、且只清目标侧；两侧都 keep = 无操作 → 400；
    unset 留历史行（可追溯谁清的），审计准确记录每侧 set/unset/keep。"""
    r = _pool(db, "清空池")
    gid = r["group_id"]
    svc.set_price_policy(db, group_id=gid, version=1,
                         purchase_value=Decimal("50"), sales_value=Decimal("40"),
                         operated_by="t")
    # 显式清采购，销售 keep
    out = svc.set_price_policy(db, group_id=gid, version=2, purchase_op="unset",
                               note="清采购", operated_by="t")
    pol = out["price_policy"]
    assert pol["purchase_ceiling_ex_tax"] is None and pol["purchase_input_basis"] is None
    assert pol["sales_floor_ex_tax"] == Decimal("40.00")
    # 显式清销售（采购已是未设置，keep 继续保持未设置）
    out2 = svc.set_price_policy(db, group_id=gid, version=3, sales_op="unset",
                                operated_by="t")
    assert out2["price_policy"]["sales_floor_ex_tax"] is None
    assert out2["price_policy"]["purchase_ceiling_ex_tax"] is None
    assert len(svc.get_pool(db, gid)["price_policy_history"]) == 3
    # 审计逐侧记录 set/unset/keep
    ops = [(log.after_json["purchase_op"], log.after_json["sales_op"])
           for log in _audits(db, gid, "set_policy")]
    assert ops == [("set", "set"), ("unset", "keep"), ("keep", "unset")]
    # 两侧都 keep → 明确报错，不产生无意义历史行
    with pytest.raises(svc.PoolCatalogError, match="keep"):
        svc.set_price_policy(db, group_id=gid, version=4, operated_by="t")
    # unset 同时又给值 → 400
    with pytest.raises(svc.PoolCatalogError, match="二选一"):
        svc.set_price_policy(db, group_id=gid, version=4, purchase_op="unset",
                             purchase_value=Decimal("1"), operated_by="t")


def test_policy_validations(db):
    r = _pool(db, "校验价池")
    with pytest.raises(svc.PoolCatalogError, match="大于 0"):
        svc.set_price_policy(db, group_id=r["group_id"], version=1,
                             purchase_value=Decimal("0"))
    with pytest.raises(svc.PoolCatalogError, match="口径"):
        svc.set_price_policy(db, group_id=r["group_id"], version=1,
                             purchase_value=Decimal("1"), purchase_basis="with_tax")
    with pytest.raises(svc.PoolConflictError):
        svc.set_price_policy(db, group_id=r["group_id"], version=99,
                             purchase_value=Decimal("1"))


# ---------------------------------------------------------------- 归档 / 恢复

def test_archive_keeps_members_and_frees_pns(db):
    """归档保留成员集合；归档池成员可加入新有效池（复合主键语义）。"""
    a, b = _part(db, "CAT-AR"), _part(db, "CAT-AR-B")
    c = _part(db, "CAT-AR-C")
    r = svc.create_pool(db, name="退役池", member_part_ids=[a, b], operated_by="t")
    r2 = svc.archive_pool(db, group_id=r["group_id"], version=1, operated_by="t")
    assert r2["status"] == "archived" and r2["version"] == 2
    left = set(db.scalars(select(PartPoolMember.part_id)
                          .where(PartPoolMember.group_id == r["group_id"])).all())
    assert left == {a, b}   # 成员还在
    r3 = svc.create_pool(db, name="接盘池", member_part_ids=[a, c], operated_by="t")
    assert r3["member_count"] == 2    # 同一 PN 可入新有效池


def test_restore_conflict_when_member_taken(db):
    """恢复时成员已被其他有效池占用 → 409 列出占用池，不静默抢占。"""
    a, x1 = _part(db, "CAT-RS"), _part(db, "CAT-RS-X1")
    x2, x3 = _part(db, "CAT-RS-X2"), _part(db, "CAT-RS-X3")
    r1 = svc.create_pool(db, name="一号池", member_part_ids=[a, x1], operated_by="t")
    svc.archive_pool(db, group_id=r1["group_id"], version=1, operated_by="t")
    r2 = svc.create_pool(db, name="二号池", member_part_ids=[a, x2, x3], operated_by="t")
    with pytest.raises(svc.PoolConflictError, match="二号池"):
        svc.restore_pool(db, group_id=r1["group_id"], version=2, operated_by="t")
    # 二号池让出该 PN（余 2 个成员，仍满足下限）后恢复成功
    svc.update_members(db, group_id=r2["group_id"], version=1, remove_part_ids=[a],
                       operated_by="t")
    r1b = svc.restore_pool(db, group_id=r1["group_id"], version=2, operated_by="t")
    assert r1b["status"] == "active"


def test_archived_pool_rejects_edits(db):
    a, b = _part(db, "CAT-FZ"), _part(db, "CAT-FZ-B")
    r = svc.create_pool(db, name="冻结池", member_part_ids=[a, b], operated_by="t")
    svc.archive_pool(db, group_id=r["group_id"], version=1, operated_by="t")
    with pytest.raises(svc.PoolCatalogError, match="已归档"):
        svc.update_pool(db, group_id=r["group_id"], version=2, updates={"name": "x"})
    with pytest.raises(svc.PoolCatalogError, match="已归档"):
        svc.update_members(db, group_id=r["group_id"], version=2, remove_part_ids=[a])
    with pytest.raises(svc.PoolCatalogError, match="已归档"):
        svc.set_price_policy(db, group_id=r["group_id"], version=2,
                             purchase_value=Decimal("1"))
    with pytest.raises(svc.PoolCatalogError, match="已是归档"):
        svc.archive_pool(db, group_id=r["group_id"], version=2)


# ---------------------------------------------------------------- 清单搜索

def test_list_pools_search_and_status_filter(db):
    a = _part(db, "ST4000NM0035", brand="Seagate", description="4T SAS 3.5 硬盘")
    a2 = _part(db, "ST4000NM0055", brand="Seagate")
    b = _part(db, "HUS726T4TALS", brand="HGST")
    b2 = _part(db, "HUS726T6TALS", brand="HGST")
    r1 = svc.create_pool(db, name="4T SAS 硬盘池", member_part_ids=[a, a2], operated_by="t")
    r2 = svc.create_pool(db, name="内存池", description="服务器内存",
                         member_part_ids=[b, b2], operated_by="t")
    svc.archive_pool(db, group_id=r2["group_id"], version=1, operated_by="t")

    assert [i["group_id"] for i in svc.list_pools(db)["items"]] == [r1["group_id"]]
    assert svc.list_pools(db, status="archived")["items"][0]["group_id"] == r2["group_id"]
    assert svc.list_pools(db, status="all")["total"] == 2
    # 按池名 / 成员 PN / 品牌 / 描述搜索（§11）
    assert svc.list_pools(db, q="SAS", status="all")["total"] == 1
    assert svc.list_pools(db, q="ST4000", status="all")["items"][0]["group_id"] == r1["group_id"]
    assert svc.list_pools(db, q="HGST", status="all")["items"][0]["group_id"] == r2["group_id"]
    assert svc.list_pools(db, q="服务器内存", status="all")["items"][0]["group_id"] == r2["group_id"]


def test_list_pools_carries_current_policy(db):
    r = _pool(db, "带价清单池")
    svc.set_price_policy(db, group_id=r["group_id"], version=1,
                         purchase_value=Decimal("725.66"), operated_by="t")
    item = svc.list_pools(db)["items"][0]
    assert item["purchase_ceiling_ex_tax"] == Decimal("725.66")
    assert item["sales_floor_ex_tax"] is None
