"""c8f3d1a7e2b9 迁移验收：确定性回填、计数器对齐、upgrade/downgrade 形状。

回填口径（决策 0006）：仅 LY-AUTO-% 行，按 issue_date 分组、组内
created_at 再 issue_id 排序；展示号分配后永不变更，重复执行同序同值。
"""
import os
from datetime import date

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text

from app.db import engine
from app.services import maintenance_project_master_workbook as master

_PREV = "e7c1f9a3b5d2"
_THIS = "c8f3d1a7e2b9"

# 手工铺库：组内排序必须 created_at 优先、再 issue_id 定序（同一时间戳两行）。
_ISSUES = [
    # (issue_id, issue_no, issue_date, created_at)
    ("disp-backfill-a2", "LY-AUTO-bf-a2", "2026-09-10", "2026-09-10 08:00:00+08"),
    ("disp-backfill-a1", "LY-AUTO-bf-a1", "2026-09-10", "2026-09-10 07:00:00+08"),
    ("disp-backfill-a3", "LY-AUTO-bf-a3", "2026-09-10", "2026-09-10 07:00:00+08"),
    ("disp-backfill-b2", "LY-AUTO-bf-b2", "2026-09-11", "2026-09-11 09:00:00+08"),
    ("disp-backfill-b1", "LY-AUTO-bf-b1", "2026-09-11", "2026-09-11 09:00:00+08"),
    ("disp-backfill-user", "LY-20260910-0777", "2026-09-10", "2026-09-10 06:00:00+08"),
]

_EXPECTED_DISPLAY = {
    "disp-backfill-a1": "LY-20260910-0001",  # 同刻并列，issue_id 小者先
    "disp-backfill-a3": "LY-20260910-0002",
    "disp-backfill-a2": "LY-20260910-0003",  # created_at 更晚
    "disp-backfill-b1": "LY-20260911-0001",  # 插入顺序在 b2 之后仍按 id 定序
    "disp-backfill-b2": "LY-20260911-0002",
    "disp-backfill-user": None,  # 用户手填号不回填
}


def _alembic_cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def _seed_legacy_rows() -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO maintenance_project"
            " (project_id, project_code, display_name, lifecycle_status)"
            " VALUES ('disp-backfill-project', 'DISP-BACKFILL', '展示号回填项目', 'ongoing')"))
        for issue_id, issue_no, issue_day, created_at in _ISSUES:
            conn.execute(text(
                "INSERT INTO maintenance_site_issue"
                " (issue_id, project_id, issue_no, issue_date, raw_status,"
                "  status_mapping_state, normalized_status, status_mapping_version,"
                "  source, created_at)"
                " VALUES (:issue_id, 'disp-backfill-project', :issue_no, :issue_day,"
                "  '已确认', 'mapped', 'confirmed', 'workbook-manual-v1',"
                "  'site_issue_v2', :created_at)"),
                {"issue_id": issue_id, "issue_no": issue_no,
                 "issue_day": issue_day, "created_at": created_at})


def _read_backfill(conn) -> dict[str, str | None]:
    return dict(conn.execute(text(
        "SELECT issue_id, display_no FROM maintenance_site_issue"
        " WHERE issue_id LIKE 'disp-backfill-%'")).all())


def test_backfill_deterministic_and_counter_seeded(db):
    db.rollback()  # 让出夹具会话快照，迁移走独立连接
    cfg = _alembic_cfg()
    alembic_command.downgrade(cfg, _PREV)
    _seed_legacy_rows()
    alembic_command.upgrade(cfg, _THIS)

    with engine.connect() as conn:
        assert _read_backfill(conn) == _EXPECTED_DISPLAY
        # 用户手填号（即使长得像展示号格式）不回填。
        assert conn.execute(text(
            "SELECT display_no FROM maintenance_site_issue"
            " WHERE issue_id = 'disp-backfill-user'")).scalar_one() is None
        counters = dict(conn.execute(text(
            "SELECT issue_date, last_sequence FROM"
            " maintenance_site_issue_display_counter")).all())
        assert counters == {date(2026, 9, 10): 3, date(2026, 9, 11): 2}

    # 确定性：再降级再升级（display_no 属实只增不改），回填结果逐字节一致。
    alembic_command.downgrade(cfg, _PREV)
    alembic_command.upgrade(cfg, _THIS)
    with engine.connect() as conn:
        assert _read_backfill(conn) == _EXPECTED_DISPLAY

    # 计数器对齐回填最大值：下一次分配从 0004 起，绝不复用回填号。
    assert master._v2_allocate_site_display_no(
        db, date(2026, 9, 10)) == "LY-20260910-0004"
    db.rollback()


def test_upgrade_downgrade_shape(db):
    db.rollback()
    cfg = _alembic_cfg()

    def inspector():
        return inspect(engine)

    # head 状态：列、部分唯一索引与计数器表齐备。
    assert "display_no" in {
        column["name"] for column in inspector().get_columns("maintenance_site_issue")}
    assert "maintenance_site_issue_display_counter" in set(
        inspector().get_table_names())
    assert "ux_maintenance_site_issue_display_no" in {
        index["name"] for index in inspector().get_indexes("maintenance_site_issue")}

    # downgrade 只移除本迁移新增对象，issue_no 等既有值不动。
    alembic_command.downgrade(cfg, _PREV)
    assert "display_no" not in {
        column["name"] for column in inspector().get_columns("maintenance_site_issue")}
    assert "maintenance_site_issue_display_counter" not in set(
        inspector().get_table_names())

    alembic_command.upgrade(cfg, "head")
    assert "display_no" in {
        column["name"] for column in inspector().get_columns("maintenance_site_issue")}
