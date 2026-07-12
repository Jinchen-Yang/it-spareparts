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
    """downgrade 到旧 schema → 造存量自动池（约 40 池 586 型号的縮影）→ upgrade head。"""
    cfg = _cfg()
    with engine.begin() as conn:
        _cleanup(conn)   # 复合主键降级回单列主键前必须清池数据
    alembic_command.downgrade(cfg, _PREV)
    try:
        with engine.begin() as conn:
            part_ids = [conn.execute(text(
                "INSERT INTO dim_part (pn_std) VALUES (:pn) RETURNING id"),
                {"pn": f"MIGPOOL-{i}"}).scalar() for i in range(5)]
            # 两个存量池：11={p0,p1}、25={p2,p3,p4}（ID 故意不连续，模拟退役空洞）
            legacy = {11: part_ids[:2], 25: part_ids[2:]}
            for gid, members in legacy.items():
                conn.execute(text(
                    "INSERT INTO part_pool (group_id, member_count, needs_calibration, oversized) "
                    "VALUES (:g, :n, false, false)"), {"g": gid, "n": len(members)})
                for pid in members:
                    conn.execute(text(
                        "INSERT INTO part_pool_member (part_id, group_id) VALUES (:p, :g)"),
                        {"p": pid, "g": gid})
            conn.execute(text("SELECT setval('part_pool_group_id_seq', 25, true)"))

        alembic_command.upgrade(cfg, "head")

        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT group_id, name, status, source, version, member_count, "
                "       created_at, updated_at FROM part_pool ORDER BY group_id")).all()
            assert [r.group_id for r in rows] == [11, 25], "历史池 ID 必须逐一保留"
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
            assert nxt > 25
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
    db.add(p); db.flush()
    created = pool_catalog.create_pool(db, name="唯一性池", member_part_ids=[p.id],
                                       operated_by="t")
    gid = created["group_id"]
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            for _ in range(2):   # 绕过服务层直插两条“当前策略”
                conn.execute(text(
                    "INSERT INTO part_pool_price_policy (group_id, purchase_ceiling_ex_tax) "
                    "VALUES (:g, 100)"), {"g": gid})
