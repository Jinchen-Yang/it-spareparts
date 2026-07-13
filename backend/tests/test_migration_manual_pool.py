"""迁移验收（互通PN池价格分析 §21）：真跑 Alembic downgrade→upgrade，证明
存量自动池升级为人工池后 **ID、成员数、成员集合完全一致**，且回填值正确：
- name='互通池-{ID}'、source='legacy_generated'、status='active'、version=1；
- created_at 回填自 updated_at（非空）；
- 序列下一值严格大于所有历史 group_id（§21-6）。
沿用 test_migration_pool_seq 的范式：走 alembic_command 真执行 revision，
序列状态用 preserve fixture 恢复（Postgres 序列不随事务回滚）。
"""
import os

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from app.db import engine

_PREV = "b9e1f4a7c2d8"   # 人工池迁移 f2a7d9c3e6b1 的上一版


def _cfg():
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    return cfg


@pytest.fixture()
def preserve_pool_seq(migrated):
    with engine.begin() as conn:
        prev = conn.execute(text(
            "SELECT last_value, is_called FROM part_pool_group_id_seq")).one()
    yield
    with engine.begin() as conn:
        conn.execute(text("SELECT setval('part_pool_group_id_seq', :lv, :ic)"),
                     {"lv": prev.last_value, "ic": prev.is_called})


def _cleanup(conn):
    conn.execute(text("DELETE FROM part_pool_price_policy"))
    conn.execute(text("DELETE FROM part_pool_member"))
    conn.execute(text("DELETE FROM part_pool"))
    conn.execute(text("DELETE FROM dim_part WHERE pn_std LIKE 'MIGPOOL-%'"))


def test_legacy_pools_survive_migration_identically(preserve_pool_seq):
    """downgrade 到旧 schema → 造存量自动池（约 40 池 586 型号的縮影）→ upgrade head。

    含一个 1 成员的历史池（gid 30）：运行时"有效池≥2成员"只约束 create/update_members
    写路径，迁移回填的历史池不经该校验、必须零变化通过（复审阻塞 1-7）。"""
    cfg = _cfg()
    with engine.begin() as conn:
        _cleanup(conn)   # 复合主键降级回单列主键前必须清池数据
    alembic_command.downgrade(cfg, _PREV)
    try:
        with engine.begin() as conn:
            part_ids = [conn.execute(text(
                "INSERT INTO dim_part (pn_std) VALUES (:pn) RETURNING id"),
                {"pn": f"MIGPOOL-{i}"}).scalar() for i in range(6)]
            # 三个存量池：11={p0,p1}、25={p2,p3,p4}、30={p5}（ID 故意不连续，模拟退役空洞；
            # 30 是 1 成员池，验证迁移不受运行时最小成员数校验影响）
            legacy = {11: part_ids[:2], 25: part_ids[2:5], 30: part_ids[5:]}
            for gid, members in legacy.items():
                conn.execute(text(
                    "INSERT INTO part_pool (group_id, member_count, needs_calibration, oversized) "
                    "VALUES (:g, :n, false, false)"), {"g": gid, "n": len(members)})
                for pid in members:
                    conn.execute(text(
                        "INSERT INTO part_pool_member (part_id, group_id) VALUES (:p, :g)"),
                        {"p": pid, "g": gid})
            conn.execute(text("SELECT setval('part_pool_group_id_seq', 30, true)"))

        alembic_command.upgrade(cfg, "head")

        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT group_id, name, status, source, version, member_count, "
                "       created_at, updated_at FROM part_pool ORDER BY group_id")).all()
            assert [r.group_id for r in rows] == [11, 25, 30], "历史池 ID 必须逐一保留"
            for r in rows:
                assert r.name == f"互通池-{r.group_id}"          # 默认名回填
                assert r.source == "legacy_generated"            # 来源标记
                assert r.status == "active" and r.version == 1
                assert r.created_at is not None and r.created_at == r.updated_at
            for gid, members in legacy.items():
                got = {row.part_id for row in conn.execute(text(
                    "SELECT part_id FROM part_pool_member WHERE group_id=:g"), {"g": gid})}
                assert got == set(members), f"池 {gid} 成员集合必须完全一致"
                cnt = conn.execute(text(
                    "SELECT member_count FROM part_pool WHERE group_id=:g"), {"g": gid}).scalar()
                assert cnt == len(members)
            # §21-6：序列下一值严格大于所有历史 ID
            nxt = conn.execute(text("SELECT nextval('part_pool_group_id_seq')")).scalar()
            assert nxt > 30
        # 1 成员历史池日常维护不被最小成员数校验卡死：改名可用；
        # 补足成员到 ≥2 也可用（唯一被拒的是把有效池改到 <2）
        from app.db import SessionLocal
        from app.services import pool_catalog
        s = SessionLocal()
        try:
            renamed = pool_catalog.update_pool(s, group_id=30, version=1,
                                               updates={"name": "整理后的历史池"},
                                               operated_by="t")
            assert renamed["name"] == "整理后的历史池"
        finally:
            s.close()
    finally:
        with engine.begin() as conn:
            _cleanup(conn)
        alembic_command.upgrade(cfg, "head")


def test_policy_current_uniqueness_enforced_by_db(preserve_pool_seq, db):
    """部分唯一索引：同一池第二条 valid_to IS NULL 的当前策略被 DB 拒绝（并发兜底）。"""
    from sqlalchemy.exc import IntegrityError

    from app.models.dimensions import DimPart
    from app.services import pool_catalog

    p = DimPart(pn_std="MIGPOOL-UQ")
    p2 = DimPart(pn_std="MIGPOOL-UQ2")
    db.add_all([p, p2]); db.flush()
    created = pool_catalog.create_pool(db, name="唯一性池", member_part_ids=[p.id, p2.id],
                                       operated_by="t")
    gid = created["group_id"]
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            for _ in range(2):   # 绕过服务层直插两条“当前策略”
                conn.execute(text(
                    "INSERT INTO part_pool_price_policy (group_id, purchase_ceiling_ex_tax) "
                    "VALUES (:g, 100)"), {"g": gid})


def test_alembic_check_no_drift(migrated):
    """ORM 元数据与迁移链零漂移（复审阻塞 5）：alembic check 必须报
    "No new upgrade operations detected"，否则抛 AutogenerateDiffsDetected。
    CI 另有独立的 upgrade head + alembic check 步骤，双保险。"""
    alembic_command.check(_cfg())


def test_downgrade_guard_stops_on_post_migration_data(preserve_pool_seq, db):
    """downgrade 数据丢失守卫（复审第六节）：存在约束价历史 / 一 PN 多池数据时
    失败即停——事务回滚、什么都不删，明确指引"生产回滚恢复数据库备份"。"""
    from decimal import Decimal

    from app.models.dimensions import DimPart
    from app.services import pool_catalog

    a = DimPart(pn_std="MIGPOOL-G1"); b = DimPart(pn_std="MIGPOOL-G2")
    db.add_all([a, b]); db.flush()
    created = pool_catalog.create_pool(db, name="守卫池", member_part_ids=[a.id, b.id],
                                       operated_by="t")
    gid = created["group_id"]
    pool_catalog.set_price_policy(db, group_id=gid, version=1,
                                  purchase_value=Decimal("100"), operated_by="t")
    db.close()   # 释放行锁，让 alembic 连接不被阻塞

    cfg = _cfg()
    try:
        with pytest.raises(Exception, match="恢复迁移前的数据库备份"):
            alembic_command.downgrade(cfg, _PREV)
        # 失败即停 = 不删任何数据：约束价历史与池成员原样，schema 仍在 head
        with engine.begin() as conn:
            n_policy = conn.execute(text(
                "SELECT COUNT(*) FROM part_pool_price_policy WHERE group_id=:g"),
                {"g": gid}).scalar()
            assert n_policy == 1, "守卫中止的 downgrade 不得删除约束价历史"
            n_member = conn.execute(text(
                "SELECT COUNT(*) FROM part_pool_member WHERE group_id=:g"),
                {"g": gid}).scalar()
            assert n_member == 2
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert version != _PREV, "downgrade 必须整体回滚，版本不得落到旧版"
    finally:
        with engine.begin() as conn:
            _cleanup(conn)
        alembic_command.upgrade(cfg, "head")
