"""v1.36 未发布迁移的降级守卫（review #10）。

b5e8d1a7c3f9（返还 SN）/c8f2b5d9a4e7（需求行 override 账本）是本版新增的
additive 迁移：数据存在时降级必须拒绝（RuntimeError → DBAPIError），不许
静默丢弃已登记 SN 或已保护的手工覆盖。d9a4c6e2f8b1（长凭据）守卫已有，
此处经真实降级链路一并回归。

所有用例 finally 强制 upgrade head：失败的 downgrade 不能把旧 schema
留给同库后续测试（run1 的连锁失败即此因）。
"""

import os
from datetime import date as _d
from uuid import uuid4

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app.etl import loader
from app.models.maintenance_project import MaintenanceProject
from tests import factories as f
from tests.test_demand_manual_edit import _batch


_B5 = "b5e8d1a7c3f9"
_C8 = "c8f2b5d9a4e7"
_B5_PREV = "e3a7b9c2d4f6"  # b5 的 down_revision
_C8_PREV = "b5e8d1a7c3f9"  # c8 的 down_revision（要跑 c8.downgrade 须降到它）


def _alembic_cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def _restore_head(db) -> None:
    """失败兜底：无论测试怎么挂，都把库升回 head 并结束当前事务。"""
    db.rollback()
    alembic_command.upgrade(_alembic_cfg(), "head")
    db.rollback()


def _seed_wbdd_line(db, *, order_raw, line_raw):
    """走真实 loader 建 WBDD 行（保证 schema 完整、依赖齐全）。"""
    orders = {order_raw: f.maintenance_head(order_raw, on=_d(2026, 3, 1))}
    lines = [f.maintenance_line(order_raw, line_raw, "PN-MIG")]
    loader.load(db, f.maintenance_result(orders, lines), _batch(db),
                _d(2026, 8, 1), mode="skip")
    db.commit()


def _seed_rkd_return(db, *, tag, sn_json):
    db.add(MaintenanceProject(
        project_id=f"proj-mig-{tag}", display_name=f"迁移{tag}",
        project_code=f"PC-MIG{tag}", lifecycle_status="ongoing",
    ))
    db.flush()
    db.execute(
        text("INSERT INTO maintenance_rkd_return_line "
             "(rkd_line_id, project_id, source, head_no, source_ref, pn, qty, "
             " serial_numbers, created_by) "
             "VALUES (CAST(:rid AS varchar(36)), CAST(:pid AS varchar(36)), "
             " 'manual', CAST(:head AS varchar(64)), CAST(:ref AS varchar(96)), "
             " 'PN-MIG', 2, CAST(:sn AS jsonb), 't')"),
        {"rid": str(uuid4()), "pid": f"proj-mig-{tag}",
         "head": f"BHR-MIG-{tag}", "ref": f"REF-{tag}", "sn": sn_json},
    )
    db.commit()


def test_b5_downgrade_refused_when_serial_numbers_recorded(db):
    """已登记 SN 的返还行存在 → 降 b5 拒绝（列不丢）。"""
    try:
        _seed_wbdd_line(db, order_raw="M-MIG1", line_raw="ML-MIG1")
        _seed_rkd_return(db, tag="1", sn_json='["SN-1", "SN-2"]')

        # alembic 在事务外直抛 RuntimeError（不经 SQLAlchemy 包装）
        with pytest.raises(RuntimeError, match="downgrade refused"):
            alembic_command.downgrade(_alembic_cfg(), _B5_PREV)

        cols = {c["name"] for c in inspect(db.get_bind())
                .get_columns("maintenance_rkd_return_line")}
        assert "serial_numbers" in cols, "拒绝降级后 SN 列必须仍在"
    finally:
        _restore_head(db)


def test_b5_downgrade_allowed_when_no_serial_numbers(db):
    """无 SN 数据 → 降级放行（守卫只拦有数据的降级，不拦空列降级）。"""
    try:
        _seed_wbdd_line(db, order_raw="M-MIG2", line_raw="ML-MIG2")
        _seed_rkd_return(db, tag="2", sn_json="[]")

        alembic_command.downgrade(_alembic_cfg(), _B5_PREV)

        cols = {c["name"] for c in inspect(db.get_bind())
                .get_columns("maintenance_rkd_return_line")}
        assert "serial_numbers" not in cols
    finally:
        _restore_head(db)


def test_c8_downgrade_refused_when_override_present(db):
    """override 账本非空 → c8.downgrade 拒绝（手工保护不丢）。

    注意目标必须是 _C8_PREV（=b5）：降级到 c8 只会跑 d9.downgrade，
    c8 守卫根本不执行。
    """
    try:
        _seed_wbdd_line(db, order_raw="M-MIG3", line_raw="ML-MIG3")
        db.execute(
            text("UPDATE f_maintenance_line SET manual_override = "
                 "'{\"qty\": {\"value\": \"9.000\", \"source_value\": \"2.000\"}}'::jsonb "
                 "WHERE raw_line_id = 'ML-MIG3'"))
        db.commit()

        with pytest.raises(RuntimeError, match="downgrade refused"):
            alembic_command.downgrade(_alembic_cfg(), _C8_PREV)

        cols = {c["name"] for c in inspect(db.get_bind())
                .get_columns("f_maintenance_line")}
        assert "manual_override" in cols, "拒绝降级后 override 列必须仍在"
    finally:
        _restore_head(db)


def test_c8_downgrade_allowed_when_no_overrides(db):
    """override 全空 → 降级放行。"""
    try:
        _seed_wbdd_line(db, order_raw="M-MIG4", line_raw="ML-MIG4")

        alembic_command.downgrade(_alembic_cfg(), _C8_PREV)

        cols = {c["name"] for c in inspect(db.get_bind())
                .get_columns("f_maintenance_line")}
        assert "manual_override" not in cols
    finally:
        _restore_head(db)
