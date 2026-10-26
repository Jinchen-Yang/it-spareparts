"""通用待办的数据库契约：迁移往返、ORM 零漂移、并发约束和事件只追加。"""
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import BigInteger, Integer, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models.system import SysUser
from app.models.workflow import SubjectEvent, WorkItem
from tests.conftest import _alembic_cfg

_REVISION = "e8a2c4f6b1d3"
_PREVIOUS = "d6f8a0b2c4e6"


def test_workflow_revision_extends_the_single_main_head():
    scripts = ScriptDirectory.from_config(_alembic_cfg())
    assert scripts.get_revision(_REVISION).down_revision == _PREVIOUS
    # 后续通用模块迁移会接在本迁移之后，只要求迁移链仍是单 head 且包含本迁移。
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert _REVISION in {r.revision for r in scripts.walk_revisions(heads[0], 'base')}


def test_workflow_upgrade_downgrade_and_zero_orm_drift(db):
    engine = db.get_bind()
    db.close()
    cfg = _alembic_cfg()
    try:
        command.downgrade(cfg, _PREVIOUS)
        with engine.connect() as connection:
            assert not {"work_item", "subject_event"} & set(inspect(connection).get_table_names())
            assert connection.scalar(text(
                "SELECT to_regprocedure('reject_subject_event_mutation()')",
            )) is None
        command.upgrade(cfg, "head")
        # 与 CI 一样，真实执行 upgrade head 后再 check，全 metadata 参与比较。
        command.check(cfg)
        with engine.connect() as connection:
            inspector = inspect(connection)
            columns = {column["name"]: column for column in inspector.get_columns("work_item")}
            assert isinstance(columns["id"]["type"], BigInteger)
            assert "nextval" in columns["id"]["default"]
            assert isinstance(columns["subject_id"]["type"], BigInteger)
            assert columns["subject_type"]["type"].length == 32
            assert columns["pool_key"]["type"].length == 32
            assert columns["paused"]["nullable"] is False
            for key in ("assignee_id", "claimed_by", "created_by"):
                assert isinstance(columns[key]["type"], Integer)
                assert not isinstance(columns[key]["type"], BigInteger)
                assert columns[key]["nullable"] is True
            indexes = {index["name"]: index for index in inspector.get_indexes("work_item")}
            assert indexes["ix_work_item_assignee_status"]["column_names"] == ["assignee_id", "status"]
            assert indexes["ix_work_item_pool_status"]["column_names"] == ["pool_key", "status"]
            assert "assignee_id IS NULL" in str(
                indexes["ix_work_item_pool_status"]["dialect_options"]["postgresql_where"],
            )
            assert indexes["ix_work_item_subject"]["column_names"] == ["subject_type", "subject_id"]
            assert indexes["uq_work_item_open_subject_assignee"]["unique"]
            assert inspector.get_indexes("subject_event")[0]["column_names"] == [
                "subject_type", "subject_id", "id",
            ]
            assert {
                tuple(foreign_key["constrained_columns"]): foreign_key["referred_table"]
                for foreign_key in inspector.get_foreign_keys("work_item")
            } == {
                ("assignee_id",): "sys_user", ("claimed_by",): "sys_user", ("created_by",): "sys_user",
            }
            event_columns = {
                column["name"]: column for column in inspector.get_columns("subject_event")
            }
            assert isinstance(event_columns["id"]["type"], BigInteger)
            assert "nextval" in event_columns["id"]["default"]
            assert isinstance(event_columns["actor"]["type"], Integer)
            assert not isinstance(event_columns["actor"]["type"], BigInteger)
            assert event_columns["actor"]["nullable"] is True
            assert event_columns["kind"]["type"].length == 48
            assert {
                tuple(foreign_key["constrained_columns"]): foreign_key["referred_table"]
                for foreign_key in inspector.get_foreign_keys("subject_event")
            } == {("actor",): "sys_user", ("work_item_id",): "work_item"}
    finally:
        command.upgrade(cfg, "head")


@pytest.mark.parametrize("mutation", [
    "UPDATE subject_event SET kind = 'changed' WHERE id = :id",
    "DELETE FROM subject_event WHERE id = :id",
])
def test_subject_event_rejects_updates_and_deletes(db, mutation):
    event = SubjectEvent(subject_type="demo", subject_id=1, kind="created", payload={"keep": True})
    db.add(event)
    db.flush()
    with pytest.raises(DBAPIError, match="subject_event is append-only"):
        with db.begin_nested():
            db.execute(text(mutation), {"id": event.id})
    db.expire(event)
    assert event.kind == "created"
    assert event.payload == {"keep": True}


def test_workflow_only_one_open_item_per_subject_and_assignee(db):
    users = [SysUser(username=f"workflow-schema-{i}", password_hash="unused", role="readonly")
             for i in range(2)]
    db.add_all(users)
    db.flush()
    item = WorkItem(subject_type="demo", subject_id=1, pool_key="demo", assignee_id=users[0].id)
    db.add(item)
    db.flush()
    for status in ("pending", "in_progress"):
        with pytest.raises(IntegrityError, match="uq_work_item_open_subject_assignee"):
            with db.begin_nested():
                db.add(WorkItem(
                    subject_type="demo", subject_id=1, pool_key="demo",
                    assignee_id=users[0].id, status=status,
                ))
                db.flush()
    # 其他处理人、其他主体类型和已结束记录不占用这个唯一名额。
    db.add_all([
        WorkItem(subject_type="demo", subject_id=1, pool_key="demo", assignee_id=users[1].id),
        WorkItem(subject_type="other", subject_id=1, pool_key="demo", assignee_id=users[0].id),
        WorkItem(subject_type="demo", subject_id=1, pool_key="demo", assignee_id=users[0].id, status="done"),
    ])
    db.flush()
    item.status = "done"
    db.flush()
    reopened = WorkItem(subject_type="demo", subject_id=1, pool_key="demo", assignee_id=users[0].id)
    db.add(reopened)
    db.flush()
    assert reopened.version == 1
    assert reopened.paused is False


@pytest.mark.parametrize("values, constraint", [
    ({"status": "unknown"}, "ck_work_item_status"),
    ({"version": 0}, "ck_work_item_version"),
    ({"assignee_id": 2147483647}, "work_item_assignee_id_fkey"),
])
def test_workflow_database_rejects_invalid_states_and_account_references(db, values, constraint):
    with pytest.raises(IntegrityError, match=constraint):
        with db.begin_nested():
            db.add(WorkItem(subject_type="demo", subject_id=1, pool_key="demo", **values))
            db.flush()
