"""Storage invariants for maintenance collection reminders (K0 Task 1).

覆盖设计 §4.1/§4.2/§4.4/§5 与实施计划 Step 1.1：新 revision 血缘与单 head、
milestone 加法列与 DB 约束、三张新表（批次/绑定/操作账本）、operation
append-only、批次与绑定唯一键、存量回填、downgrade 只移除新增对象。
"""

from datetime import UTC, date, datetime
import os

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from psycopg.errors import CheckViolation

from app.db import engine


REVISION = "c8e2a4f6b1d3"
PREVIOUS = "d9f1a3c7e5b2"

MILESTONE_NEW_COLUMNS = (
    "date_precision",
    "collection_plan_import_batch_id",
    "follow_up_status",
    "follow_up_review_required",
    "follow_up_note",
    "followed_up_by",
    "followed_up_at",
)

NEW_TABLES = (
    "maintenance_collection_plan_import_batch",
    "maintenance_collection_plan_source_binding",
    "maintenance_collection_milestone_operation",
)


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def _current_head() -> str:
    return ScriptDirectory.from_config(_cfg()).get_current_head()


def _db_version() -> str:
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


# ---------- 种子辅助 ----------
def _seed_parents(db, *, prefix: str) -> int:
    """项目/合同/账号最小父数据；返回 sys_user.id。"""
    user_id = db.execute(
        text(
            "INSERT INTO sys_user (username, password_hash, role) "
            "VALUES (:username, 'unused', 'readonly') RETURNING id"
        ),
        {"username": f"{prefix}-user"},
    ).scalar_one()
    db.execute(
        text(
            "INSERT INTO maintenance_project "
            "(project_id, project_code, display_name, lifecycle_status) VALUES "
            "(:pid, :pcode, :pname, 'ongoing')"
        ),
        {
            "pid": f"{prefix}-project",
            "pcode": f"{prefix.upper()}-PROJECT",
            "pname": "合成回款提醒项目",
        },
    )
    db.execute(
        text(
            "INSERT INTO maintenance_project_contract "
            "(project_contract_id, project_id, contract_id, contract_no, contract_amount, "
            " contract_status, status_mapping_state, status_mapping_version, "
            " included_in_total, effective_from, source, version) "
            "VALUES (:pcid, :pid, :cid, :cno, 100000, 'active', 'mapped', "
            " 'synthetic-v1', true, '2026-01-01', 'synthetic-test', 1)"
        ),
        {
            "pcid": f"{prefix}-contract",
            "pid": f"{prefix}-project",
            "cid": f"{prefix}-contract-id",
            "cno": f"XS-{prefix.upper()}",
        },
    )
    db.commit()
    return user_id


def _seed_manager_batch(db, user_id: int, *, batch_id: str) -> str:
    db.execute(
        text(
            "INSERT INTO maintenance_manager_upload_batch "
            "(batch_id, owner_user_id, report_month, protocol_version, template_version, "
            " export_id, file_sha256, file_size, operation_key, semantic_hash, "
            " scope_version, data_version, status, plan_json, issues_json, created_by, "
            " created_at, expires_at) "
            "VALUES (:batch_id, :owner_user_id, '2026-08-01', 'v3', 'tpl', 'export-1', "
            " :sha256, 100, :operation_key, :semantic_hash, :scope_version, "
            " :data_version, 'valid', '{}'::jsonb, '[]'::jsonb, 'synthetic-test', now(), "
            " now() + interval '24 hours')"
        ),
        {
            "batch_id": batch_id,
            "owner_user_id": user_id,
            "sha256": "a" * 64,
            "operation_key": f"operation-{batch_id}",
            "semantic_hash": "b" * 64,
            "scope_version": "c" * 64,
            "data_version": "d" * 64,
        },
    )
    db.commit()
    return batch_id


def _seed_import_batch(db, user_id: int, *, batch_id: str) -> str:
    db.execute(
        text(
            "INSERT INTO maintenance_collection_plan_import_batch "
            "(batch_id, owner_user_id, contract_version, file_sha256, file_size, "
            " original_filename, storage_key, operation_key, semantic_hash, data_version, "
            " apply_payload_hash, version, status, plan_json, issues_json, result_json, "
            " created_by, created_at, expires_at, applied_by, applied_at) "
            "VALUES (:batch_id, :owner_user_id, 'project-manager-xls-v1', :sha256, 1024, "
            " 'synthetic.xls', :storage_key, :operation_key, :semantic_hash, "
            " :data_version, NULL, 1, 'valid', '{}'::jsonb, '[]'::jsonb, NULL, "
            " 'synthetic-test', now(), now() + interval '24 hours', NULL, NULL)"
        ),
        {
            "batch_id": batch_id,
            "owner_user_id": user_id,
            "sha256": "1" * 64,
            "storage_key": f"storage-{batch_id}",
            "operation_key": f"operation-{batch_id}",
            "semantic_hash": "2" * 64,
            "data_version": "3" * 64,
        },
    )
    db.commit()
    return batch_id


def _insert_milestone(
    db,
    *,
    milestone_id: str,
    project_id: str,
    contract_id: str,
    sequence: int = 1,
    source: str = "direct_api",
    source_batch_id: str | None = None,
    collection_plan_import_batch_id: str | None = None,
    date_precision: str = "day",
    follow_up_status: str = "pending",
    follow_up_review_required: bool = False,
    followed_up_by: int | None = None,
    followed_up_at: datetime | None = None,
    planned_date: date = date(2026, 9, 1),
    planned_amount: float = 25000.00,
) -> None:
    db.execute(
        text(
            "INSERT INTO maintenance_collection_milestone "
            "(milestone_id, project_id, project_contract_id, sequence, planned_date, "
            " planned_amount, completeness_state, source, source_batch_id, "
            " collection_plan_import_batch_id, date_precision, follow_up_status, "
            " follow_up_review_required, follow_up_note, followed_up_by, "
            " followed_up_at, version) "
            "VALUES (:milestone_id, :project_id, :project_contract_id, :sequence, "
            " :planned_date, :planned_amount, 'complete', :source, :source_batch_id, "
            " :collection_plan_import_batch_id, :date_precision, :follow_up_status, "
            " :follow_up_review_required, NULL, :followed_up_by, :followed_up_at, 1)"
        ),
        {
            "milestone_id": milestone_id,
            "project_id": project_id,
            "project_contract_id": contract_id,
            "sequence": sequence,
            "planned_date": planned_date,
            "planned_amount": planned_amount,
            "source": source,
            "source_batch_id": source_batch_id,
            "collection_plan_import_batch_id": collection_plan_import_batch_id,
            "date_precision": date_precision,
            "follow_up_status": follow_up_status,
            "follow_up_review_required": follow_up_review_required,
            "followed_up_by": followed_up_by,
            "followed_up_at": followed_up_at,
        },
    )


def _expect_violation(db, fn) -> None:
    with pytest.raises(DBAPIError):
        fn()
    db.rollback()


def _expect_evidence_check_violation(db, fn) -> None:
    """证据 CHECK 靶：必须是 ck_maintenance_collection_plan_import_batch_applied_evidence
    的 CheckViolation，而不是任意 DBAPIError。"""
    with pytest.raises(DBAPIError) as exc_info:
        fn()
    orig = exc_info.value.orig
    assert isinstance(orig, CheckViolation), f"期望 CheckViolation，实际 {type(orig).__name__}"
    assert (
        orig.diag.constraint_name
        == "ck_maintenance_collection_plan_import_batch_applied_evidence"
    ), "必须命中具名证据 CHECK"
    db.rollback()


# ---------- 1. revision 血缘与单 head ----------
def test_new_revision_is_additive_child_of_frozen_baseline_and_single_head():
    script = ScriptDirectory.from_config(_cfg())
    revisions = {revision.revision: revision for revision in script.walk_revisions()}
    assert REVISION in revisions, "新 revision c8e2a4f6b1d3 尚未创建"
    assert revisions[REVISION].down_revision == PREVIOUS
    heads = set(script.get_heads())
    assert len(heads) == 1, "必须只有一个 head"
    # 冻结基线必须仍在新 head 的祖先链上（后续加法迁移不得重写历史）
    ancestor_revisions = {
        revision.revision
        for revision in script.walk_revisions(base="base", head=heads.pop())
    }
    assert REVISION in ancestor_revisions, "冻结基线 c8e2a4f6b1d3 必须仍是新 head 的祖先"


# ---------- 2. milestone 加法列 / FK / 索引 ----------
def test_milestone_gains_follow_up_columns_with_fks_and_review_index(db):
    inspector = inspect(db.get_bind())
    columns = {
        column["name"]: column
        for column in inspector.get_columns("maintenance_collection_milestone")
    }
    for name in MILESTONE_NEW_COLUMNS:
        assert name in columns, f"milestone 缺少新列 {name}"
    column_types = {name: columns[name]["type"] for name in MILESTONE_NEW_COLUMNS}
    assert isinstance(column_types["date_precision"], String)
    assert isinstance(column_types["collection_plan_import_batch_id"], String)
    assert isinstance(column_types["follow_up_status"], String)
    assert isinstance(column_types["follow_up_review_required"], Boolean)
    assert isinstance(column_types["follow_up_note"], String)
    assert isinstance(column_types["followed_up_by"], Integer)
    followed_up_at_type = column_types["followed_up_at"]
    assert isinstance(followed_up_at_type, DateTime)
    assert followed_up_at_type.timezone is True, "followed_up_at 必须是时区感知时间"

    fks = {
        tuple(constraint["constrained_columns"]): constraint["referred_table"]
        for constraint in inspector.get_foreign_keys("maintenance_collection_milestone")
    }
    assert fks[("collection_plan_import_batch_id",)] == (
        "maintenance_collection_plan_import_batch"
    )
    assert fks[("followed_up_by",)] == "sys_user"

    indexes = {
        tuple(index["column_names"])
        for index in inspector.get_indexes("maintenance_collection_milestone")
    }
    assert (
        "project_id",
        "follow_up_status",
        "planned_date",
        "sequence",
    ) in indexes, "缺少 (project_id, follow_up_status, planned_date, sequence) 索引"


def test_new_tables_have_exact_frozen_columns(db):
    """三张新表的列集合必须与设计 §4.1/§4.2/§4.4 与计划 Step 1.3 完全一致（不额外加列）。"""
    expected = {
        "maintenance_collection_plan_import_batch": {
            "batch_id", "owner_user_id", "contract_version", "file_sha256",
            "file_size", "original_filename", "storage_key", "operation_key",
            "semantic_hash", "data_version", "apply_payload_hash", "version",
            "status", "plan_json", "issues_json", "result_json", "created_by",
            "created_at", "expires_at", "applied_by", "applied_at",
        },
        "maintenance_collection_plan_source_binding": {
            "binding_id", "source_system", "external_order_no", "project_id",
            "project_contract_id", "binding_status", "reviewed_by", "reviewed_at",
            "version", "created_at", "updated_at",
        },
        "maintenance_collection_milestone_operation": {
            "operation_id", "milestone_id", "action", "idempotency_key",
            "expected_version", "result_version", "payload_hash", "before_payload",
            "after_payload", "result_json", "reason", "actor_user_id", "created_at",
        },
    }
    inspector = inspect(db.get_bind())
    for table, expected_columns in expected.items():
        actual = {column["name"] for column in inspector.get_columns(table)}
        assert actual == expected_columns, f"{table} 列集合必须与冻结合同一致"


# ---------- 3. 存量节点回填 ----------
def test_existing_milestone_rows_backfilled_and_preserved(db):
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, PREVIOUS)
    try:
        with engine.begin() as connection:
            user_id = connection.execute(
                text(
                    "INSERT INTO sys_user (username, password_hash, role) "
                    "VALUES ('backfill-user', 'unused', 'readonly') RETURNING id"
                )
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO maintenance_project "
                    "(project_id, project_code, display_name, lifecycle_status) VALUES "
                    "('backfill-project', 'BACKFILL-PROJECT', '合成回填项目', 'ongoing')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_project_contract "
                    "(project_contract_id, project_id, contract_id, contract_no, "
                    " contract_amount, contract_status, status_mapping_state, "
                    " status_mapping_version, included_in_total, effective_from, "
                    " source, version) "
                    "VALUES ('backfill-contract', 'backfill-project', "
                    " 'backfill-contract-id', 'XS-BACKFILL', 100000, 'active', "
                    " 'mapped', 'synthetic-v1', true, '2026-01-01', "
                    " 'synthetic-test', 1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_manager_upload_batch "
                    "(batch_id, owner_user_id, report_month, protocol_version, "
                    " template_version, export_id, file_sha256, file_size, "
                    " operation_key, semantic_hash, scope_version, data_version, "
                    " status, plan_json, issues_json, created_by, created_at, expires_at) "
                    "VALUES ('backfill-batch', :user_id, '2026-08-01', 'v3', 'tpl', "
                    " 'export-1', repeat('a', 64), 100, 'backfill-op-key', "
                    " repeat('b', 64), repeat('c', 64), repeat('d', 64), 'valid', "
                    " '{}'::jsonb, '[]'::jsonb, 'synthetic-test', now(), "
                    " now() + interval '24 hours')"
                ),
                {"user_id": user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_collection_milestone "
                    "(milestone_id, project_id, project_contract_id, sequence, "
                    " planned_date, planned_amount, completeness_state, source, "
                    " source_batch_id, version) VALUES "
                    "('backfill-milestone-mgr', 'backfill-project', "
                    " 'backfill-contract', 1, '2026-09-01', 25000.00, 'complete', "
                    " 'manager_workbook_v3', 'backfill-batch', 1), "
                    "('backfill-milestone-direct', 'backfill-project', "
                    " 'backfill-contract', 2, '2026-10-01', 30000.00, 'complete', "
                    " 'direct_api', NULL, 1)"
                )
            )
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            rows = {
                row.milestone_id: row
                for row in connection.execute(
                    text(
                        "SELECT milestone_id, planned_date, date_precision, "
                        "follow_up_status, follow_up_review_required, followed_up_by, "
                        "followed_up_at, collection_plan_import_batch_id "
                        "FROM maintenance_collection_milestone "
                        "WHERE milestone_id IN "
                        "('backfill-milestone-mgr', 'backfill-milestone-direct')"
                    )
                )
            }
        assert set(rows) == {"backfill-milestone-mgr", "backfill-milestone-direct"}
        for row in rows.values():
            assert row.date_precision == "day", "存量节点必须回填 date_precision=day"
            assert row.follow_up_status == "pending", "存量节点必须回填 follow_up_status=pending"
            assert row.follow_up_review_required is False, "存量节点必须回填 review_required=false"
            assert row.followed_up_by is None
            assert row.followed_up_at is None
            assert row.collection_plan_import_batch_id is None
        assert rows["backfill-milestone-mgr"].planned_date == date(2026, 9, 1)
        assert rows["backfill-milestone-direct"].planned_date == date(2026, 10, 1)
    finally:
        alembic_command.upgrade(cfg, "head")


# ---------- 4. milestone 跟进状态约束 ----------
def test_milestone_follow_up_state_constraints(db):
    prefix = "state"
    user_id = _seed_parents(db, prefix=prefix)
    pid, pcid = f"{prefix}-project", f"{prefix}-contract"
    handled_at = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)

    _insert_milestone(
        db,
        milestone_id="state-pending-ok",
        project_id=pid,
        contract_id=pcid,
        sequence=1,
    )
    db.commit()

    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="state-handled-missing-actor",
            project_id=pid,
            contract_id=pcid,
            sequence=2,
            follow_up_status="handled",
            followed_up_at=handled_at,
        ),
    )
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="state-handled-missing-time",
            project_id=pid,
            contract_id=pcid,
            sequence=3,
            follow_up_status="handled",
            followed_up_by=user_id,
        ),
    )
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="state-pending-with-actor",
            project_id=pid,
            contract_id=pcid,
            sequence=4,
            follow_up_status="pending",
            followed_up_by=user_id,
        ),
    )
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="state-pending-review",
            project_id=pid,
            contract_id=pcid,
            sequence=5,
            follow_up_review_required=True,
        ),
    )
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="state-bad-precision",
            project_id=pid,
            contract_id=pcid,
            sequence=6,
            date_precision="week",
        ),
    )
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="state-bad-status",
            project_id=pid,
            contract_id=pcid,
            sequence=7,
            follow_up_status="done",
        ),
    )
    _insert_milestone(
        db,
        milestone_id="state-handled-ok",
        project_id=pid,
        contract_id=pcid,
        sequence=8,
        follow_up_status="handled",
        followed_up_by=user_id,
        followed_up_at=handled_at,
        follow_up_review_required=True,
    )
    db.commit()


# ---------- 5. source 与两个批次 FK 三分支互斥 ----------
def test_milestone_source_batch_exclusivity(db):
    prefix = "excl"
    user_id = _seed_parents(db, prefix=prefix)
    pid, pcid = f"{prefix}-project", f"{prefix}-contract"
    manager_batch = _seed_manager_batch(db, user_id, batch_id=f"{prefix}-manager-batch")
    import_batch = _seed_import_batch(db, user_id, batch_id=f"{prefix}-import-batch")

    _insert_milestone(
        db,
        milestone_id="excl-direct-ok",
        project_id=pid,
        contract_id=pcid,
        sequence=1,
    )
    db.commit()
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="excl-direct-manager",
            project_id=pid,
            contract_id=pcid,
            sequence=2,
            source="direct_api",
            source_batch_id=manager_batch,
        ),
    )
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="excl-direct-import",
            project_id=pid,
            contract_id=pcid,
            sequence=3,
            source="direct_api",
            collection_plan_import_batch_id=import_batch,
        ),
    )
    _insert_milestone(
        db,
        milestone_id="excl-manager-ok",
        project_id=pid,
        contract_id=pcid,
        sequence=4,
        source="manager_workbook_v3",
        source_batch_id=manager_batch,
    )
    db.commit()
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="excl-manager-missing",
            project_id=pid,
            contract_id=pcid,
            sequence=5,
            source="manager_workbook_v3",
        ),
    )
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="excl-manager-both",
            project_id=pid,
            contract_id=pcid,
            sequence=6,
            source="manager_workbook_v3",
            source_batch_id=manager_batch,
            collection_plan_import_batch_id=import_batch,
        ),
    )
    _insert_milestone(
        db,
        milestone_id="excl-xls-ok",
        project_id=pid,
        contract_id=pcid,
        sequence=7,
        source="project_manager_xls_v1",
        collection_plan_import_batch_id=import_batch,
        date_precision="month",
    )
    db.commit()
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="excl-xls-missing",
            project_id=pid,
            contract_id=pcid,
            sequence=8,
            source="project_manager_xls_v1",
        ),
    )
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="excl-xls-both",
            project_id=pid,
            contract_id=pcid,
            sequence=9,
            source="project_manager_xls_v1",
            source_batch_id=manager_batch,
            collection_plan_import_batch_id=import_batch,
        ),
    )
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id="excl-bad-source",
            project_id=pid,
            contract_id=pcid,
            sequence=10,
            source="legacy_xls",
        ),
    )


# ---------- 6. 导入批次唯一键 ----------
def test_import_batch_owner_operation_key_and_storage_key_uniqueness(db):
    prefix = "batch"
    user_id = _seed_parents(db, prefix=prefix)
    other_id = db.execute(
        text(
            "INSERT INTO sys_user (username, password_hash, role) "
            "VALUES ('batch-other-user', 'unused', 'readonly') RETURNING id"
        )
    ).scalar_one()
    db.commit()
    _seed_import_batch(db, user_id, batch_id=f"{prefix}-one")

    def insert(batch_id: str, owner: int, operation_key: str, storage_key: str) -> None:
        db.execute(
            text(
                "INSERT INTO maintenance_collection_plan_import_batch "
                "(batch_id, owner_user_id, contract_version, file_sha256, file_size, "
                " original_filename, storage_key, operation_key, semantic_hash, "
                " data_version, version, status, plan_json, issues_json, created_by, "
                " created_at, expires_at) "
                "VALUES (:batch_id, :owner, 'project-manager-xls-v1', repeat('1', 64), "
                " 1024, 'synthetic.xls', :storage_key, :operation_key, repeat('2', 64), "
                " repeat('3', 64), 1, 'valid', '{}'::jsonb, '[]'::jsonb, "
                " 'synthetic-test', now(), now() + interval '24 hours')"
            ),
            {
                "batch_id": batch_id,
                "owner": owner,
                "operation_key": operation_key,
                "storage_key": storage_key,
            },
        )

    # 同一 owner + 同一 operation_key → 唯一键拒绝（并发相同 preview 收敛到同一批次）
    _expect_violation(
        db,
        lambda: insert(
            f"{prefix}-two",
            user_id,
            f"operation-{prefix}-one",
            f"storage-{prefix}-two",
        ),
    )
    # 不同 owner + 相同 operation_key → 允许
    insert(
        f"{prefix}-three",
        other_id,
        f"operation-{prefix}-one",
        f"storage-{prefix}-three",
    )
    db.commit()
    # storage_key 全局唯一
    _expect_violation(
        db,
        lambda: insert(
            f"{prefix}-four",
            other_id,
            f"operation-{prefix}-four",
            f"storage-{prefix}-three",
        ),
    )


# ---------- 7. 外部订单绑定 ----------
def test_source_binding_source_fixed_and_pair_unique(db):
    prefix = "binding"
    user_id = _seed_parents(db, prefix=prefix)
    pid, pcid = f"{prefix}-project", f"{prefix}-contract"

    def insert(
        binding_id: str,
        order_no: str,
        source_system: str = "project_manager_xls_v1",
        binding_status: str = "reviewed",
    ) -> None:
        db.execute(
            text(
                "INSERT INTO maintenance_collection_plan_source_binding "
                "(binding_id, source_system, external_order_no, project_id, "
                " project_contract_id, binding_status, reviewed_by, reviewed_at, "
                " version, created_at, updated_at) "
                "VALUES (:bid, :source_system, :order_no, :pid, :pcid, "
                " :binding_status, :reviewed_by, now(), 1, now(), now())"
            ),
            {
                "bid": binding_id,
                "source_system": source_system,
                "order_no": order_no,
                "pid": pid,
                "pcid": pcid,
                "binding_status": binding_status,
                "reviewed_by": user_id,
            },
        )

    insert("binding-one", "ORDER-001")
    db.commit()
    _expect_violation(
        db,
        lambda: insert("binding-two", "ORDER-001"),
    )
    insert("binding-three", "ORDER-002")
    db.commit()
    _expect_violation(
        db,
        lambda: insert("binding-four", "ORDER-003", source_system="direct_api"),
    )
    _expect_violation(
        db,
        lambda: insert("binding-five", "ORDER-004", binding_status="pending"),
    )


# ---------- 8. 操作账本：幂等键 / action / reason / append-only ----------
def test_operation_ledger_constraints_and_append_only(db):
    prefix = "op"
    user_id = _seed_parents(db, prefix=prefix)
    pid, pcid = f"{prefix}-project", f"{prefix}-contract"
    _insert_milestone(
        db,
        milestone_id=f"{prefix}-milestone",
        project_id=pid,
        contract_id=pcid,
        sequence=1,
    )
    db.commit()

    def insert(
        operation_id: str,
        idempotency_key: str,
        action: str = "handle",
        reason: str | None = None,
    ) -> None:
        db.execute(
            text(
                "INSERT INTO maintenance_collection_milestone_operation "
                "(operation_id, milestone_id, action, idempotency_key, "
                " expected_version, result_version, payload_hash, before_payload, "
                " after_payload, result_json, reason, actor_user_id, created_at) "
                "VALUES (:oid, :milestone_id, :action, :idempotency_key, 1, 1, "
                " repeat('a', 64), '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, "
                " :reason, :actor, now())"
            ),
            {
                "oid": operation_id,
                "milestone_id": f"{prefix}-milestone",
                "action": action,
                "idempotency_key": idempotency_key,
                "reason": reason,
                "actor": user_id,
            },
        )

    insert("op-one", "idempotency-key-op-one")
    db.commit()
    # idempotency_key 全局唯一
    _expect_violation(
        db,
        lambda: insert("op-two", "idempotency-key-op-one"),
    )
    # reschedule/reopen 必须有 reason；action 枚举受限
    insert("op-three", "idempotency-key-op-three", action="reschedule", reason="合成改期理由")
    db.commit()
    _expect_violation(
        db,
        lambda: insert("op-four", "idempotency-key-op-four", action="reschedule", reason=None),
    )
    _expect_violation(
        db,
        lambda: insert("op-five", "idempotency-key-op-five", action="reopen", reason=None),
    )
    _expect_violation(
        db,
        lambda: insert("op-six", "idempotency-key-op-six", action="delete"),
    )
    # DB trigger 拒绝 UPDATE / DELETE
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(
            text(
                "UPDATE maintenance_collection_milestone_operation "
                "SET result_version = 9 WHERE operation_id = 'op-one'"
            )
        )
    db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(
            text(
                "DELETE FROM maintenance_collection_milestone_operation "
                "WHERE operation_id = 'op-one'"
            )
        )
    db.rollback()


# ---------- 9. 新 action 默认 false 回填 ----------
def test_new_actions_backfilled_false_in_templates_and_existing_accounts(db):
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, PREVIOUS)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO sys_user
                        (username, role, password_hash, template_code,
                         template_perms, permissions, perm_overrides)
                    VALUES
                        ('reminder-migration-boss', 'boss', 'unused', 'admin',
                         '{"sentinel": true}'::jsonb,
                         '{"sentinel": true}'::jsonb,
                         '{"action_maintenance_collection_follow_up": true}'::jsonb),
                        ('reminder-migration-admin', 'admin', 'unused', 'boss',
                         '{"sentinel": true}'::jsonb,
                         '{"sentinel": true}'::jsonb,
                         '{"action_maintenance_collection_plan_import": false}'::jsonb)
                    """
                )
            )
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            templates = {
                row.code: (row.follow_up, row.plan_import)
                for row in connection.execute(
                    text(
                        """
                        SELECT code,
                               (permissions->>'action_maintenance_collection_follow_up')::boolean
                                   AS follow_up,
                               (permissions->>'action_maintenance_collection_plan_import')::boolean
                                   AS plan_import
                        FROM sys_role_template
                        WHERE code IN ('admin', 'boss', 'sales', 'purchaser', 'readonly')
                        """
                    )
                )
            }
            users = {
                username: (follow_up, plan_import, has_follow_up_override, has_import_override)
                for username, follow_up, plan_import, has_follow_up_override, has_import_override
                in connection.execute(
                    text(
                        """
                        SELECT username,
                               (template_perms->>'action_maintenance_collection_follow_up')::boolean,
                               (template_perms->>'action_maintenance_collection_plan_import')::boolean,
                               perm_overrides ? 'action_maintenance_collection_follow_up',
                               perm_overrides ? 'action_maintenance_collection_plan_import'
                        FROM sys_user
                        WHERE username LIKE 'reminder-migration-%'
                        """
                    )
                )
            }
        assert templates == {
            "admin": (False, False),
            "boss": (False, False),
            "sales": (False, False),
            "purchaser": (False, False),
            "readonly": (False, False),
        }
        assert users == {
            "reminder-migration-boss": (False, False, False, False),
            "reminder-migration-admin": (False, False, False, False),
        }
    finally:
        alembic_command.upgrade(cfg, "head")


def test_new_actions_backfilled_false_in_custom_template_and_seeded_accounts(db):
    """回填必须覆盖自定义角色模板与无新键的存量账号（守卫，P1-5 硬化）。"""
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, PREVIOUS)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO sys_role_template
                        (code, name, base_role, permissions, is_system, is_active,
                         version, created_by)
                    VALUES
                        ('custom_viewer', '自定义只读', 'readonly',
                         '{"sentinel": true}'::jsonb, false, true, 1, 'synthetic-test')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO sys_user
                        (username, role, password_hash, template_code,
                         template_perms, permissions)
                    VALUES
                        ('reminder-custom-boss', 'boss', 'unused', 'custom_viewer',
                         '{"sentinel": true}'::jsonb, '{"sentinel": true}'::jsonb),
                        ('reminder-custom-readonly', 'readonly', 'unused', 'custom_viewer',
                         '{"sentinel": true}'::jsonb, '{"sentinel": true}'::jsonb)
                    """
                )
            )
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            custom = connection.execute(
                text(
                    """
                    SELECT (permissions->>'action_maintenance_collection_follow_up')::boolean
                               AS follow_up,
                           (permissions->>'action_maintenance_collection_plan_import')::boolean
                               AS plan_import
                    FROM sys_role_template WHERE code = 'custom_viewer'
                    """
                )
            ).one()
            users = {
                username: (follow_up, plan_import)
                for username, follow_up, plan_import in connection.execute(
                    text(
                        """
                        SELECT username,
                               (permissions->>'action_maintenance_collection_follow_up')::boolean,
                               (permissions->>'action_maintenance_collection_plan_import')::boolean
                        FROM sys_user
                        WHERE username IN ('reminder-custom-boss', 'reminder-custom-readonly')
                        """
                    )
                )
            }
        assert (custom.follow_up, custom.plan_import) == (False, False)
        assert users == {
            "reminder-custom-boss": (False, False),
            "reminder-custom-readonly": (False, False),
        }
    finally:
        alembic_command.upgrade(cfg, "head")


# ---------- 10. downgrade 只移除新增对象 ----------
def test_downgrade_removes_only_new_objects_and_keeps_existing_milestones(db):
    prefix = "downgrade"
    user_id = _seed_parents(db, prefix=prefix)
    pid, pcid = f"{prefix}-project", f"{prefix}-contract"
    _insert_milestone(
        db,
        milestone_id="downgrade-existing",
        project_id=pid,
        contract_id=pcid,
        sequence=1,
    )
    # 迁移前已存在的 manager_workbook_v3 节点同样必须原样保留（默认状态，
    # 不触发 P1-6 失败关闭路径；修复后仍应允许 downgrade 且保留本节点）。
    _seed_manager_batch(db, user_id, batch_id="downgrade-manager-batch")
    _insert_milestone(
        db,
        milestone_id="downgrade-manager-existing",
        project_id=pid,
        contract_id=pcid,
        sequence=2,
        source="manager_workbook_v3",
        source_batch_id="downgrade-manager-batch",
    )
    db.commit()
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, PREVIOUS)
    try:
        with engine.connect() as connection:
            for table in NEW_TABLES:
                assert connection.scalar(
                    text(f"SELECT to_regclass('{table}')")
                ) is None, f"downgrade 后 {table} 必须不存在"
            remaining_columns = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'maintenance_collection_milestone'"
                    )
                ).scalars()
            )
            for name in MILESTONE_NEW_COLUMNS:
                assert name not in remaining_columns, f"downgrade 后列 {name} 必须不存在"
            for milestone_id in ("downgrade-existing", "downgrade-manager-existing"):
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM maintenance_collection_milestone "
                            "WHERE milestone_id = :mid"
                        ),
                        {"mid": milestone_id},
                    )
                    == 1
                ), "downgrade 不得删除迁移前已存在的计划节点"
            assert connection.scalar(
                text(
                    "SELECT planned_date FROM maintenance_collection_milestone "
                    "WHERE milestone_id = 'downgrade-existing'"
                )
            ) == date(2026, 9, 1)
    finally:
        alembic_command.upgrade(cfg, "head")
    assert _db_version() == _current_head()


# ---------- 11. 修复靶 P1-5：批次应用证据 CHECK / 操作账本索引 / 硬化 ----------
def _insert_import_batch_with_evidence(
    db,
    *,
    batch_id: str,
    user_id: int,
    status: str,
    apply_payload_hash: str | None,
    result_json: str | None,
    applied_by: str | None,
    applied_at: datetime | None,
) -> None:
    """带任意应用证据字段的批次插入（P1-5 证据 CHECK 靶）。"""
    db.execute(
        text(
            "INSERT INTO maintenance_collection_plan_import_batch "
            "(batch_id, owner_user_id, contract_version, file_sha256, file_size, "
            " original_filename, storage_key, operation_key, semantic_hash, data_version, "
            " apply_payload_hash, version, status, plan_json, issues_json, result_json, "
            " created_by, created_at, expires_at, applied_by, applied_at) "
            "VALUES (:batch_id, :user_id, 'project-manager-xls-v1', repeat('1', 64), 1024, "
            " 'synthetic.xls', :storage_key, :operation_key, repeat('2', 64), "
            " repeat('3', 64), :apply_payload_hash, 1, :status, '{}'::jsonb, '[]'::jsonb, "
            " CAST(:result_json AS jsonb), 'synthetic-test', now(), now() + interval '24 hours', "
            " :applied_by, :applied_at)"
        ),
        {
            "batch_id": batch_id,
            "user_id": user_id,
            "storage_key": f"storage-{batch_id}",
            "operation_key": f"operation-{batch_id}",
            "apply_payload_hash": apply_payload_hash,
            "status": status,
            "result_json": result_json,
            "applied_by": applied_by,
            "applied_at": applied_at,
        },
    )


def test_applied_batch_requires_all_apply_evidence(db):
    """status='applied' 必须四证据齐备（apply_payload_hash/result_json/applied_by/
    applied_at 任一缺失即拒绝；设计 §4.4 应用证据）。

    当前批次表没有任何应用证据 CHECK，四个"缺失"插入全部成功，本测试当前红。
    """
    prefix = "evidence"
    user_id = _seed_parents(db, prefix=prefix)
    evidence = dict(
        apply_payload_hash="e" * 64,
        result_json='{"applied": true}',
        applied_by="synthetic-admin",
        applied_at=datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
    )
    # 完整 applied 行 → 接受（守卫）
    _insert_import_batch_with_evidence(
        db, batch_id=f"{prefix}-complete", user_id=user_id,
        status="applied", **evidence,
    )
    db.commit()
    # 四个证据逐一缺失 → 必须拒绝（当前红）
    for field in evidence:
        missing = {**evidence, field: None}
        _expect_evidence_check_violation(
            db,
            lambda missing=missing: _insert_import_batch_with_evidence(
                db, batch_id=f"{prefix}-missing-{field}", user_id=user_id,
                status="applied", **missing,
            ),
        )


def test_non_applied_batch_rejects_completed_apply_evidence(db):
    """非 applied 状态携带完整应用证据必须拒绝（P1-5：当前无 CHECK，本测试红）。"""
    prefix = "evidence-nonapplied"
    user_id = _seed_parents(db, prefix=prefix)
    evidence = dict(
        apply_payload_hash="f" * 64,
        result_json='{"applied": true}',
        applied_by="synthetic-admin",
        applied_at=datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
    )
    for status in ("valid", "error", "expired"):
        _expect_evidence_check_violation(
            db,
            lambda status=status: _insert_import_batch_with_evidence(
                db, batch_id=f"{prefix}-{status}", user_id=user_id,
                status=status, **evidence,
            ),
        )


def test_operation_ledger_has_milestone_created_index(db):
    """操作账本必须按 (milestone_id, created_at) 索引（P1-5：当前缺失，本测试红）。"""
    inspector = inspect(db.get_bind())
    indexes = {
        tuple(index["column_names"])
        for index in inspector.get_indexes("maintenance_collection_milestone_operation")
    }
    assert ("milestone_id", "created_at") in indexes


def test_idempotency_key_globally_unique_across_milestones(db):
    """同一幂等键跨节点也必须唯一（设计 §4.2 全局唯一，守卫）。"""
    prefix = "idemfix"
    user_id = _seed_parents(db, prefix=prefix)
    pid, pcid = f"{prefix}-project", f"{prefix}-contract"
    _insert_milestone(
        db, milestone_id=f"{prefix}-m1", project_id=pid, contract_id=pcid, sequence=1,
    )
    _insert_milestone(
        db, milestone_id=f"{prefix}-m2", project_id=pid, contract_id=pcid, sequence=2,
    )
    db.commit()

    def insert(milestone_id: str, operation_id: str) -> None:
        db.execute(
            text(
                "INSERT INTO maintenance_collection_milestone_operation "
                "(operation_id, milestone_id, action, idempotency_key, expected_version, "
                " result_version, payload_hash, before_payload, after_payload, result_json, "
                " reason, actor_user_id, created_at) "
                "VALUES (:oid, :milestone_id, 'handle', 'shared-key-across-milestones', 1, 1, "
                " repeat('a', 64), '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, NULL, :actor, now())"
            ),
            {"oid": operation_id, "milestone_id": milestone_id, "actor": user_id},
        )

    insert(f"{prefix}-m1", f"{prefix}-op-one")
    db.commit()
    _expect_violation(db, lambda: insert(f"{prefix}-m2", f"{prefix}-op-two"))


def test_new_tables_column_types_nullability_fks_and_no_cascade(db):
    """三张新表与 milestone 新增列的精确类型/可空性/FK 目标/无级联删除（守卫）。"""
    inspector = inspect(db.get_bind())

    batch_cols = {
        c["name"]: c
        for c in inspector.get_columns("maintenance_collection_plan_import_batch")
    }
    batch_id_type = batch_cols["batch_id"]["type"]
    assert isinstance(batch_id_type, String) and batch_id_type.length == 64
    assert batch_cols["batch_id"]["nullable"] is False
    assert isinstance(batch_cols["owner_user_id"]["type"], Integer)
    assert batch_cols["owner_user_id"]["nullable"] is False
    assert isinstance(batch_cols["file_size"]["type"], BigInteger)
    assert batch_cols["file_size"]["nullable"] is False
    assert isinstance(batch_cols["plan_json"]["type"], JSONB)
    assert batch_cols["plan_json"]["nullable"] is True
    assert isinstance(batch_cols["issues_json"]["type"], JSONB)
    assert batch_cols["issues_json"]["nullable"] is False
    assert isinstance(batch_cols["result_json"]["type"], JSONB)
    assert batch_cols["result_json"]["nullable"] is True
    assert batch_cols["apply_payload_hash"]["nullable"] is True
    assert batch_cols["applied_by"]["nullable"] is True
    assert batch_cols["applied_at"]["nullable"] is True
    for name in ("created_at", "expires_at"):
        ts_type = batch_cols[name]["type"]
        assert isinstance(ts_type, DateTime) and ts_type.timezone is True
        assert batch_cols[name]["nullable"] is False

    binding_cols = {
        c["name"]: c
        for c in inspector.get_columns("maintenance_collection_plan_source_binding")
    }
    assert isinstance(binding_cols["reviewed_by"]["type"], Integer)
    assert binding_cols["reviewed_by"]["nullable"] is False
    for name in ("project_id", "project_contract_id"):
        assert isinstance(binding_cols[name]["type"], String)
        assert binding_cols[name]["nullable"] is False
    reviewed_at_type = binding_cols["reviewed_at"]["type"]
    assert isinstance(reviewed_at_type, DateTime) and reviewed_at_type.timezone is True
    assert binding_cols["reviewed_at"]["nullable"] is False

    op_cols = {
        c["name"]: c
        for c in inspector.get_columns("maintenance_collection_milestone_operation")
    }
    assert isinstance(op_cols["milestone_id"]["type"], String)
    assert op_cols["milestone_id"]["nullable"] is False
    assert isinstance(op_cols["actor_user_id"]["type"], Integer)
    assert op_cols["actor_user_id"]["nullable"] is False
    assert isinstance(op_cols["reason"]["type"], Text)
    assert op_cols["reason"]["nullable"] is True
    op_created_at_type = op_cols["created_at"]["type"]
    assert isinstance(op_created_at_type, DateTime) and op_created_at_type.timezone is True
    assert op_cols["created_at"]["nullable"] is False

    expected_fks = {
        "maintenance_collection_plan_import_batch": {("owner_user_id",): "sys_user"},
        "maintenance_collection_plan_source_binding": {
            ("reviewed_by",): "sys_user",
            ("project_id",): "maintenance_project",
            ("project_contract_id",): "maintenance_project_contract",
        },
        "maintenance_collection_milestone_operation": {
            ("milestone_id",): "maintenance_collection_milestone",
            ("actor_user_id",): "sys_user",
        },
        "maintenance_collection_milestone": {
            ("collection_plan_import_batch_id",): "maintenance_collection_plan_import_batch",
            ("followed_up_by",): "sys_user",
        },
    }
    for table, expected in expected_fks.items():
        actual = {
            tuple(fk["constrained_columns"]): fk["referred_table"]
            for fk in inspector.get_foreign_keys(table)
        }
        for constrained, referred in expected.items():
            assert actual.get(constrained) == referred, (
                f"{table} 缺少 FK {constrained} → {referred}"
            )
        # 任何 FK 都不得声明 ondelete CASCADE（审计/证据表必须防误删）
        for fk in inspector.get_foreign_keys(table):
            assert fk["options"].get("ondelete") != "CASCADE", (
                f"{table} 的 FK {fk['constrained_columns']} 不得级联删除"
            )


def test_original_milestone_constraints_preserved_after_upgrade(db):
    """upgrade 后原有唯一键与四个 CHECK 必须保留（守卫）。"""
    inspector = inspect(db.get_bind())
    unique_names = {
        c["name"]
        for c in inspector.get_unique_constraints("maintenance_collection_milestone")
    }
    assert "uq_maintenance_collection_milestone_contract_sequence" in unique_names
    check_names = {
        c["name"]
        for c in inspector.get_check_constraints("maintenance_collection_milestone")
    }
    for name in (
        "ck_maintenance_collection_milestone_sequence",
        "ck_maintenance_collection_milestone_amount",
        "ck_maintenance_collection_milestone_completeness",
        "ck_maintenance_collection_milestone_state_fields",
    ):
        assert name in check_names, f"upgrade 后里程碑约束 {name} 必须保留"


def test_migration_sets_lock_timeout():
    """迁移文件必须在升级与降级路径设置 lock_timeout（守卫）。"""
    path = os.path.join(
        os.path.dirname(__file__), "..", "alembic", "versions",
        "c8e2a4f6b1d3_maintenance_collection_reminders.py",
    )
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    assert "SET LOCAL lock_timeout = '5s'" in source


def test_downgrade_locks_collection_reminder_tables_before_fail_closed_guards():
    """downgrade 检查新功能数据前必须显式锁表，避免 check-then-drop 竞态。"""
    path = os.path.join(
        os.path.dirname(__file__), "..", "alembic", "versions",
        "c8e2a4f6b1d3_maintenance_collection_reminders.py",
    )
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    downgrade_source = source.split("def downgrade() -> None:", 1)[1]
    first_guard = downgrade_source.index(
        "SELECT 1 FROM maintenance_collection_plan_import_batch"
    )
    lock_position = downgrade_source.find("LOCK TABLE")

    assert lock_position != -1, "downgrade 必须在 fail-closed guard 前显式 LOCK TABLE"
    assert lock_position < first_guard, "LOCK TABLE 必须先于任何新功能数据 guard SELECT"
    lock_block = downgrade_source[lock_position:first_guard]
    assert "ACCESS EXCLUSIVE MODE" in lock_block
    for table in (
        "maintenance_collection_milestone",
        "maintenance_collection_milestone_operation",
        "maintenance_collection_plan_source_binding",
        "maintenance_collection_plan_import_batch",
    ):
        assert table in lock_block, f"downgrade lock 缺少 {table}"


def test_orm_exports_new_models_with_defaults():
    """ORM 导出三张新表模型，且 milestone 新列带 Python 侧默认值（守卫）。"""
    from app.models import (
        MaintenanceCollectionMilestone,
        MaintenanceCollectionMilestoneOperation,
        MaintenanceCollectionPlanImportBatch,
        MaintenanceCollectionPlanSourceBinding,
    )
    assert (
        MaintenanceCollectionPlanImportBatch.__tablename__
        == "maintenance_collection_plan_import_batch"
    )
    assert (
        MaintenanceCollectionPlanSourceBinding.__tablename__
        == "maintenance_collection_plan_source_binding"
    )
    assert (
        MaintenanceCollectionMilestoneOperation.__tablename__
        == "maintenance_collection_milestone_operation"
    )
    columns = MaintenanceCollectionMilestone.__table__.c
    assert columns.date_precision.default.arg == "day"
    assert columns.follow_up_status.default.arg == "pending"
    assert columns.follow_up_review_required.default.arg is False


def test_milestone_source_xor_remaining_combinations_rejected(db):
    """source/batch FK 三分支互斥的剩余组合（守卫；既有测试覆盖其他组合）。"""
    prefix = "xorfix"
    user_id = _seed_parents(db, prefix=prefix)
    pid, pcid = f"{prefix}-project", f"{prefix}-contract"
    manager_batch = _seed_manager_batch(db, user_id, batch_id=f"{prefix}-manager-batch")
    import_batch = _seed_import_batch(db, user_id, batch_id=f"{prefix}-import-batch")
    # manager 来源只给导入批次（source_batch_id 为空）→ 拒绝
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id=f"{prefix}-mgr-import",
            project_id=pid,
            contract_id=pcid,
            sequence=1,
            source="manager_workbook_v3",
            collection_plan_import_batch_id=import_batch,
        ),
    )
    # XLS 来源只给 manager 批次（无导入批次）→ 拒绝
    _expect_violation(
        db,
        lambda: _insert_milestone(
            db,
            milestone_id=f"{prefix}-xls-manager",
            project_id=pid,
            contract_id=pcid,
            sequence=2,
            source="project_manager_xls_v1",
            source_batch_id=manager_batch,
            date_precision="month",
        ),
    )


# ---------- 12. 修复靶 P1-6：downgrade 失败关闭（存在新功能状态必须拒绝） ----------
def test_downgrade_blocked_by_import_batch_preview(db):
    """存在导入批次（新功能状态）时 downgrade 必须失败关闭（P1-6）。

    当前 downgrade 静默删除新表，本测试当前红；实现后必须 RuntimeError 且
    数据库保持 c8e2a4f6b1d3、批次行原样保留。
    """
    prefix = "block-batch"
    user_id = _seed_parents(db, prefix=prefix)
    _seed_import_batch(db, user_id, batch_id=f"{prefix}-preview")
    db.close()
    cfg = _cfg()
    try:
        with pytest.raises(RuntimeError):
            alembic_command.downgrade(cfg, PREVIOUS)
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == _current_head()
            ), "downgrade 失败后数据库必须仍在 c8e2a4f6b1d3"
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM maintenance_collection_plan_import_batch "
                        "WHERE batch_id = :bid"
                    ),
                    {"bid": f"{prefix}-preview"},
                )
                == 1
            ), "导入批次行必须原样保留"
    finally:
        # 无论 pytest.raises 是否按预期失败（当前实现红：downgrade 成功），
        # 都必须把数据库恢复到 head，避免污染后续用例。
        alembic_command.upgrade(cfg, "head")
    assert _db_version() == _current_head()


def test_downgrade_blocked_by_source_binding(db):
    """存在外部订单绑定（新功能状态）时 downgrade 必须失败关闭（P1-6 红）。"""
    prefix = "block-binding"
    user_id = _seed_parents(db, prefix=prefix)
    pid, pcid = f"{prefix}-project", f"{prefix}-contract"
    db.execute(
        text(
            "INSERT INTO maintenance_collection_plan_source_binding "
            "(binding_id, source_system, external_order_no, project_id, "
            " project_contract_id, binding_status, reviewed_by, reviewed_at, "
            " version, created_at, updated_at) "
            "VALUES (:bid, 'project_manager_xls_v1', 'ORDER-BLOCK', :pid, :pcid, "
            " 'reviewed', :reviewed_by, now(), 1, now(), now())"
        ),
        {"bid": f"{prefix}-binding", "pid": pid, "pcid": pcid, "reviewed_by": user_id},
    )
    db.commit()
    db.close()
    cfg = _cfg()
    try:
        with pytest.raises(RuntimeError):
            alembic_command.downgrade(cfg, PREVIOUS)
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == _current_head()
            ), "downgrade 失败后数据库必须仍在 c8e2a4f6b1d3"
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM maintenance_collection_plan_source_binding "
                        "WHERE binding_id = :bid"
                    ),
                    {"bid": f"{prefix}-binding"},
                )
                == 1
            ), "绑定行必须原样保留"
    finally:
        # 无论 pytest.raises 是否按预期失败（当前实现红：downgrade 成功），
        # 都必须把数据库恢复到 head，避免污染后续用例。
        alembic_command.upgrade(cfg, "head")
    assert _db_version() == _current_head()


def test_downgrade_blocked_by_xls_milestone_and_operation(db):
    """存在 XLS 里程碑或操作账本（新功能状态）时 downgrade 必须失败关闭（P1-6 红）。"""
    prefix = "block-xls"
    user_id = _seed_parents(db, prefix=prefix)
    pid, pcid = f"{prefix}-project", f"{prefix}-contract"
    import_batch = _seed_import_batch(db, user_id, batch_id=f"{prefix}-import")
    _insert_milestone(
        db,
        milestone_id=f"{prefix}-milestone",
        project_id=pid,
        contract_id=pcid,
        sequence=1,
        source="project_manager_xls_v1",
        collection_plan_import_batch_id=import_batch,
        date_precision="month",
    )
    db.commit()
    db.execute(
        text(
            "INSERT INTO maintenance_collection_milestone_operation "
            "(operation_id, milestone_id, action, idempotency_key, expected_version, "
            " result_version, payload_hash, before_payload, after_payload, result_json, "
            " reason, actor_user_id, created_at) "
            "VALUES (:oid, :milestone_id, 'handle', 'block-xls-op-key-1234', 1, 1, "
            " repeat('a', 64), '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, NULL, :actor, now())"
        ),
        {"oid": f"{prefix}-op", "milestone_id": f"{prefix}-milestone", "actor": user_id},
    )
    db.commit()
    db.close()
    cfg = _cfg()
    try:
        with pytest.raises(RuntimeError):
            alembic_command.downgrade(cfg, PREVIOUS)
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == _current_head()
            ), "downgrade 失败后数据库必须仍在 c8e2a4f6b1d3"
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM maintenance_collection_milestone "
                        "WHERE milestone_id = :mid"
                    ),
                    {"mid": f"{prefix}-milestone"},
                )
                == 1
            ), "XLS 里程碑行必须原样保留"
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM maintenance_collection_milestone_operation "
                        "WHERE operation_id = :oid"
                    ),
                    {"oid": f"{prefix}-op"},
                )
                == 1
            ), "操作账本行必须原样保留"
    finally:
        # 无论 pytest.raises 是否按预期失败（当前实现红：downgrade 成功），
        # 都必须把数据库恢复到 head，避免污染后续用例。
        alembic_command.upgrade(cfg, "head")
    assert _db_version() == _current_head()


def test_downgrade_blocked_by_non_default_follow_up_state(db):
    """存在非默认跟进状态（handled/复核标记/处理人）时 downgrade 必须失败关闭
    （P1-6 红）；行与跟进字段必须原样保留。"""
    prefix = "block-handled"
    user_id = _seed_parents(db, prefix=prefix)
    pid, pcid = f"{prefix}-project", f"{prefix}-contract"
    handled_at = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)
    _insert_milestone(
        db,
        milestone_id=f"{prefix}-milestone",
        project_id=pid,
        contract_id=pcid,
        sequence=1,
        follow_up_status="handled",
        follow_up_review_required=True,
        followed_up_by=user_id,
        followed_up_at=handled_at,
    )
    db.commit()
    db.execute(
        text(
            "UPDATE maintenance_collection_milestone SET follow_up_note = :note "
            "WHERE milestone_id = :mid"
        ),
        {"note": "合成已跟进备注", "mid": f"{prefix}-milestone"},
    )
    db.commit()
    db.close()
    cfg = _cfg()
    try:
        with pytest.raises(RuntimeError):
            alembic_command.downgrade(cfg, PREVIOUS)
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == _current_head()
            ), "downgrade 失败后数据库必须仍在 c8e2a4f6b1d3"
            row = connection.execute(
                text(
                    "SELECT follow_up_status, follow_up_review_required, followed_up_by, "
                    "followed_up_at, follow_up_note FROM maintenance_collection_milestone "
                    "WHERE milestone_id = :mid"
                ),
                {"mid": f"{prefix}-milestone"},
            ).one()
            assert row.follow_up_status == "handled"
            assert row.follow_up_review_required is True
            assert row.followed_up_by == user_id
            assert row.followed_up_at == handled_at
            assert row.follow_up_note == "合成已跟进备注"
    finally:
        # 无论 pytest.raises 是否按预期失败（当前实现红：downgrade 成功），
        # 都必须把数据库恢复到 head，避免污染后续用例。
        alembic_command.upgrade(cfg, "head")
    assert _db_version() == _current_head()
