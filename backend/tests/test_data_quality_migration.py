"""DEV-05A 迁移真执行：纯新增、模板补键、空表往返与有数据 fail-stop。"""
import os

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from app.db import engine

_PREV = "a9c5e2f7d4b1"
_HEAD = "d5a7c9e1f3b6"


def _cfg():
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    return cfg


def test_empty_downgrade_upgrade_preserves_existing_facts_and_permission_defaults(db):
    cfg = _cfg()
    with engine.begin() as conn:
        before = {
            table: conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            for table in ("f_purchase_line", "f_sales_line", "inventory", "part_pool")
        }
        # 自定义模板缺新键，验证升级失败关闭；admin 则默认打开。
        conn.execute(text(
            "INSERT INTO sys_role_template "
            "(code,name,base_role,permissions,is_system,is_active,version) "
            "VALUES ('custom_dq','数据维护自定义','readonly','{}'::jsonb,false,true,1)"
        ))

    alembic_command.downgrade(cfg, _PREV)
    try:
        with engine.begin() as conn:
            assert conn.execute(text("SELECT to_regclass('fact_data_quality_issue')")).scalar() is None
            perms = conn.execute(text(
                "SELECT code, permissions FROM sys_role_template "
                "WHERE code IN ('admin','custom_dq') ORDER BY code"
            )).all()
            assert all("action_data_quality_review" not in row.permissions for row in perms)

        alembic_command.upgrade(cfg, "head")
        with engine.begin() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == _HEAD
            assert conn.execute(text("SELECT to_regclass('fact_data_quality_issue')")).scalar()
            values = dict(conn.execute(text(
                "SELECT code, (permissions->>'action_data_quality_review')::boolean "
                "FROM sys_role_template WHERE code IN ('admin','custom_dq')"
            )).all())
            assert values == {"admin": True, "custom_dq": False}
            after = {
                table: conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
                for table in before
            }
            assert after == before
    finally:
        alembic_command.upgrade(cfg, "head")


def test_nonempty_issue_table_blocks_downgrade(db):
    with engine.begin() as conn:
        batch_id = conn.execute(text(
            "INSERT INTO sys_import_batch "
            "(filename,file_type,file_hash,status,rows_total,rows_inserted,rows_skipped,rows_error,rows_inactive) "
            "VALUES ('m.xlsx','purchase','dq-mig','success',0,0,0,0,0) RETURNING id"
        )).scalar()
        part_id = conn.execute(text(
            "INSERT INTO dim_part (pn_std) VALUES ('DQ-MIG-PN') RETURNING id"
        )).scalar()
        conn.execute(text(
            "INSERT INTO fact_data_quality_issue "
            "(side,line_id,part_id,import_batch_id,rule_code,rule_version,evidence,"
            " source_fingerprint,status,detected_by,version) "
            "VALUES ('purchase',999,:part,:batch,'r','1','{}'::jsonb,'fp','open','test',1)"
        ), {"part": part_id, "batch": batch_id})

    with pytest.raises(Exception, match="not empty"):
        alembic_command.downgrade(_cfg(), _PREV)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == _HEAD
        assert conn.execute(text("SELECT COUNT(*) FROM fact_data_quality_issue")).scalar() == 1
