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
    # 仅使用新旧两版共有表，保证 RED 停在 _PREV 时也能清理；head 下 policy 由
    # part_pool 外键 ON DELETE CASCADE 一并删除。
    conn.execute(text("DELETE FROM part_pool_member"))
    conn.execute(text("DELETE FROM part_pool"))
    conn.execute(text("DELETE FROM dim_part WHERE pn_std LIKE 'MIGPOOL-%'"))


def test_legacy_pools_survive_migration_identically(preserve_pool_seq):
    """downgrade 到旧 schema → 造存量自动池（约 40 池 586 型号的縮影）→ upgrade head。

    所有历史池均满足"有效池≥2成员"；迁移逐池保持 ID 与成员集合不变。非法单成员
    存量由独立 fail-fast 用例覆盖，不得升级为 active。"""
    cfg = _cfg()
    with engine.begin() as conn:
        _cleanup(conn)   # 复合主键降级回单列主键前必须清池数据
    alembic_command.downgrade(cfg, _PREV)
    try:
        with engine.begin() as conn:
            part_ids = [conn.execute(text(
                "INSERT INTO dim_part (pn_std) VALUES (:pn) RETURNING id"),
                {"pn": f"MIGPOOL-{i}"}).scalar() for i in range(7)]
            # 三个存量池：11={p0,p1}、25={p2,p3,p4}、30={p5,p6}；ID 故意不连续，
            # 模拟退役空洞。
            legacy = {11: part_ids[:2], 25: part_ids[2:5], 30: part_ids[5:7]}
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
            undersized = conn.execute(text(
                "SELECT p.group_id FROM part_pool p "
                "LEFT JOIN part_pool_member m ON m.group_id = p.group_id "
                "WHERE p.status = 'active' "
                "GROUP BY p.group_id HAVING COUNT(m.part_id) < 2"
            )).all()
            assert undersized == [], "升级后数据库不得存在实际成员数 <2 的有效池"
            # §21-6：序列下一值严格大于所有历史 ID
            nxt = conn.execute(text("SELECT nextval('part_pool_group_id_seq')")).scalar()
            assert nxt > 30
        # 合法历史池升级后可正常改名。
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


def test_upgrade_rejects_legacy_single_member_pool_without_schema_or_data_changes(
        preserve_pool_seq):
    """旧 schema 存在单成员池时 upgrade 必须在任何 DDL 前失败，数据与 revision 原样。"""
    cfg = _cfg()
    gid = 31
    with engine.begin() as conn:
        _cleanup(conn)
    alembic_command.downgrade(cfg, _PREV)
    try:
        with engine.begin() as conn:
            part_id = conn.execute(text(
                "INSERT INTO dim_part (pn_std) VALUES ('MIGPOOL-SINGLE') RETURNING id"
            )).scalar()
            conn.execute(text(
                "INSERT INTO part_pool (group_id, member_count, needs_calibration, oversized) "
                "VALUES (:g, 1, false, false)"), {"g": gid})
            conn.execute(text(
                "INSERT INTO part_pool_member (part_id, group_id) VALUES (:p, :g)"),
                {"p": part_id, "g": gid})

        with pytest.raises(RuntimeError, match="有效池至少包含 2 个 PN"):
            alembic_command.upgrade(cfg, "head")

        with engine.begin() as conn:
            assert conn.execute(text(
                "SELECT version_num FROM alembic_version")).scalar() == _PREV
            added_columns = set(conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'part_pool' "
                "AND column_name IN ('name', 'status', 'source', 'version')"
            )).scalars())
            assert added_columns == set(), "fail-fast 前不得执行任何新增列 DDL"
            assert conn.execute(text(
                "SELECT member_count FROM part_pool WHERE group_id=:g"),
                {"g": gid}).scalar() == 1
            assert conn.execute(text(
                "SELECT part_id FROM part_pool_member WHERE group_id=:g"),
                {"g": gid}).scalar() == part_id
            assert conn.execute(text(
                "SELECT pn_std FROM dim_part WHERE id=:p"), {"p": part_id}).scalar() == \
                "MIGPOOL-SINGLE"
    finally:
        # RED（旧实现意外升级成功）和 GREEN（仍停在旧 schema）都能用这些共有列清理，
        # 随后恢复 head，避免污染共享测试库。
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM part_pool_member WHERE group_id=:g"), {"g": gid})
            conn.execute(text("DELETE FROM part_pool WHERE group_id=:g"), {"g": gid})
            conn.execute(text("DELETE FROM dim_part WHERE pn_std='MIGPOOL-SINGLE'"))
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
