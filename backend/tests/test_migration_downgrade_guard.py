"""人工池迁移的 downgrade 无损守卫回归。"""
import os

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text

from app.db import engine


_PREV = "b9e1f4a7c2d8"


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    return cfg


@pytest.fixture()
def preserve_pool_seq(migrated):
    with engine.begin() as conn:
        previous = conn.execute(text(
            "SELECT last_value, is_called FROM part_pool_group_id_seq"
        )).one()
    yield
    with engine.begin() as conn:
        conn.execute(
            text("SELECT setval('part_pool_group_id_seq', :last_value, :is_called)"),
            {"last_value": previous.last_value, "is_called": previous.is_called},
        )


def _cleanup_test_data() -> None:
    with engine.begin() as conn:
        # 仅使用新旧 schema 共有表；head 下策略历史由 pool FK CASCADE 删除。
        conn.execute(text("DELETE FROM part_pool_member"))
        conn.execute(text("DELETE FROM part_pool"))
        conn.execute(text("DELETE FROM dim_part WHERE pn_std LIKE 'DOWNGUARD-%'"))


def _pool_snapshot(group_id: int) -> tuple[tuple, list[tuple]]:
    """跨 downgrade 尝试比较完整人工元数据与成员维护字段。"""
    with engine.begin() as conn:
        pool = conn.execute(text(
            "SELECT name, description, status, source, version, member_count, "
            "created_by, updated_by, created_at, updated_at "
            "FROM part_pool WHERE group_id=:group_id"
        ), {"group_id": group_id}).one()
        members = conn.execute(text(
            "SELECT part_id, added_by, note, created_at, updated_at "
            "FROM part_pool_member WHERE group_id=:group_id ORDER BY part_id"
        ), {"group_id": group_id}).all()
    return tuple(pool), [tuple(member) for member in members]


def test_downgrade_rejects_manual_pool_and_preserves_schema_and_data(
    preserve_pool_seq, db
):
    """人工建池后降级会丢元数据，必须在任何 DDL 前失败即停。"""
    from app.models.dimensions import DimPart
    from app.services import pool_catalog

    parts = [DimPart(pn_std=f"DOWNGUARD-{suffix}") for suffix in ("A", "B")]
    db.add_all(parts)
    db.flush()
    created = pool_catalog.create_pool(
        db,
        name="人工池不可降级",
        description="必须完整保留",
        member_part_ids=[part.id for part in parts],
        operated_by="guard-owner",
    )
    group_id = created["group_id"]
    db.close()

    cfg = _cfg()
    try:
        with pytest.raises(Exception, match="迁移之后产生的业务数据"):
            alembic_command.downgrade(cfg, _PREV)

        with engine.begin() as conn:
            columns = {column["name"] for column in inspect(conn).get_columns("part_pool")}
            assert {"name", "description", "status", "source", "version", "created_by"} <= columns
            row = conn.execute(text(
                "SELECT name, description, status, source, version, created_by, updated_by "
                "FROM part_pool WHERE group_id=:group_id"
            ), {"group_id": group_id}).one()
            assert row == (
                "人工池不可降级", "必须完整保留", "active", "manual", 1,
                "guard-owner", "guard-owner",
            )
            assert conn.execute(text(
                "SELECT COUNT(*) FROM part_pool_member WHERE group_id=:group_id"
            ), {"group_id": group_id}).scalar_one() == 2
            assert conn.execute(text(
                "SELECT version_num FROM alembic_version"
            )).scalar_one() == "f2a7d9c3e6b1"
    finally:
        _cleanup_test_data()
        alembic_command.upgrade(cfg, "head")


@pytest.mark.parametrize("mutation", ["rename", "archive", "members"])
def test_downgrade_rejects_every_legacy_pool_business_mutation_and_preserves_data(
    preserve_pool_seq, db, mutation
):
    """改名/说明、归档、成员维护后的新语义都不能被旧 schema 静默吞掉。"""
    from app.models.dimensions import DimPart
    from app.models.inventory import PartPool, PartPoolMember
    from app.services import pool_catalog

    parts = [DimPart(pn_std=f"DOWNGUARD-{mutation}-{suffix}")
             for suffix in ("A", "B", "C")]
    db.add_all(parts)
    db.flush()
    group_id = int(db.execute(text("SELECT nextval('part_pool_group_id_seq')")).scalar_one())
    db.add(PartPool(
        group_id=group_id,
        name=f"互通池-{group_id}",
        status="active",
        source="legacy_generated",
        version=1,
        member_count=2,
    ))
    db.add_all([
        PartPoolMember(group_id=group_id, part_id=parts[0].id),
        PartPoolMember(group_id=group_id, part_id=parts[1].id),
    ])
    db.commit()

    if mutation == "rename":
        pool_catalog.update_pool(
            db,
            group_id=group_id,
            version=1,
            updates={"name": "迁移后改名", "description": "迁移后说明"},
            operated_by="guard-owner",
        )
    elif mutation == "archive":
        pool_catalog.archive_pool(
            db, group_id=group_id, version=1, operated_by="guard-owner"
        )
    else:
        pool_catalog.update_members(
            db,
            group_id=group_id,
            version=1,
            add_part_ids=[parts[2].id],
            operated_by="guard-owner",
        )
    expected = _pool_snapshot(group_id)
    db.close()

    cfg = _cfg()
    try:
        with pytest.raises(Exception, match="迁移之后产生的业务数据"):
            alembic_command.downgrade(cfg, _PREV)

        assert _pool_snapshot(group_id) == expected
        with engine.begin() as conn:
            assert conn.execute(text(
                "SELECT version_num FROM alembic_version"
            )).scalar_one() == "f2a7d9c3e6b1"
    finally:
        _cleanup_test_data()
        alembic_command.upgrade(cfg, "head")
