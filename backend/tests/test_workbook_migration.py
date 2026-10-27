"""通用工作簿操作表迁移往返与 ORM 零漂移。"""
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.models.workbook import WorkbookOperation
from tests.conftest import _alembic_cfg


def test_workbook_migration_extends_current_head():
    scripts = ScriptDirectory.from_config(_alembic_cfg())
    assert scripts.get_revision("c4e6f8a0b2d5").down_revision == "b2d4f6a8c0e1"
    # 后续迁移会接在本迁移之后：只要求迁移链单 head 且包含本迁移。
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert "c4e6f8a0b2d5" in {r.revision for r in scripts.walk_revisions('base', heads[0])}


def test_workbook_upgrade_downgrade_and_zero_drift(db):
    engine = db.get_bind()
    db.close()
    cfg = _alembic_cfg()
    try:
        command.downgrade(cfg, "b2d4f6a8c0e1")
        assert "workbook_operation" not in inspect(engine).get_table_names()
        command.upgrade(cfg, "head")
        command.check(cfg)
        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("workbook_operation")}
        assert set(columns) == {"id", "schema_name", "subject_type", "subject_id", "export_id", "file_sha256",
                                "actor", "status", "plan", "response", "created_at", "applied_at"}
        assert "nextval" in columns["id"]["default"]
        assert inspector.get_unique_constraints("workbook_operation")[0]["column_names"] == [
            "export_id", "file_sha256", "actor",
        ]
    finally:
        command.upgrade(cfg, "head")


def test_workbook_database_unique_file_actor_and_status_constraints(db):
    values = dict(schema_name="demo", export_id="export", file_sha256="f" * 64, actor="owner", plan={})
    db.add(WorkbookOperation(**values))
    db.flush()
    with pytest.raises(IntegrityError, match="uq_workbook_operation_file_actor"):
        with db.begin_nested():
            db.add(WorkbookOperation(**values))
            db.flush()
    with pytest.raises(IntegrityError, match="ck_workbook_operation_status"):
        with db.begin_nested():
            db.add(WorkbookOperation(**{**values, "actor": "other", "status": "unknown"}))
            db.flush()
