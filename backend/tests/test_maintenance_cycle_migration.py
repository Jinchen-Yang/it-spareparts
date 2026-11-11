"""坏件循环池 T1 迁移验收：三表结构/约束/索引、五页键默认 false、升降级单头。

对应任务书 §5.1–5.4、§11 T1：
- revision 血缘：新修订是冻结基线 98afc0d2157b 的加法子节点，链上单 head；
- 约束：非法工位、一单位两意图、qty≠1、delivering 无租约（及 withdrawn/closed/
  source 形状、to_shop 桶、SN 部分唯一）；
- 权限：五个页键对全部内置模板（含 admin）默认 false，运行时只认实名账号快照；
- downgrade 回旧 head 移除全部新增对象，再 upgrade 回新 head 可恢复（迁移回填 false）。

并发/唯一写冲突用例一律在**同一连接**内用 SAVEPOINT（``conn.begin_nested()``）断言：
失败只回滚到保存点，父事务继续。禁止在外层事务未提交时另开连接写子行——子行 FK
校验会对未提交父行取 KEY SHARE 锁并等待父事务，外层又在等内层返回，形成真实死锁
（2026-11-09 首次运行实测挂起 25+ 分钟的根因，已由 tech-manager 会话级定位）。
所有写事务都设 ``lock_timeout``/``statement_timeout``，避免再出现无界等待。

迁移测试家族惯例：downgrade/upgrade 会改变共享 schema，必须 try/finally 恢复 head，
否则后续用例的 conftest TRUNCATE 会撞到缺表（见 conftest 列槽位重建注释）。
"""

from __future__ import annotations

import json
import os

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from psycopg.errors import CheckViolation, UniqueViolation
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app import permissions
from app.db import engine

REVISION = "86f7486b9ed5"
PREVIOUS = "98afc0d2157b"

NEW_TABLES = (
    "maintenance_cycle_unit",
    "maintenance_cycle_event",
    "maintenance_cycle_sell_intent",
)
CYCLE_PAGE_KEYS = (
    "page_cycle_recycle",
    "page_cycle_test",
    "page_cycle_repair",
    "page_cycle_manage",
    "page_cycle_wip",
)
BUILTIN_ROLES = ("admin", "boss", "sales", "purchaser", "readonly")


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


def _ensure_head() -> None:
    """把测试库恢复到当前唯一 head（迁移测试 finally 用）。"""
    if _db_version() != _current_head():
        alembic_command.upgrade(_cfg(), "head")


def _set_timeouts(conn) -> None:
    conn.execute(text("SET LOCAL lock_timeout = '2000ms'"))
    conn.execute(text("SET LOCAL statement_timeout = '30000ms'"))


# ---------- 数据铺库 helper（全部在同一外层事务内）----------
def _insert_part(conn, pn: str = "PN-CYCLE-T1") -> int:
    return conn.execute(
        text("INSERT INTO dim_part (pn_std) VALUES (:pn) RETURNING id"),
        {"pn": pn},
    ).scalar_one()


def _insert_unit(conn, part_id: int, **overrides) -> int:
    row = {
        "part_id": part_id,
        "pn": "PN-CYCLE-T1",
        "station": "pool",
        "source_kind": "external",
        "created_by": "t1-test",
        "updated_by": "t1-test",
    }
    row.update(overrides)
    columns = ", ".join(row)
    params = ", ".join(f":{key}" for key in row)
    return conn.execute(
        text(
            f"INSERT INTO maintenance_cycle_unit ({columns}) VALUES ({params}) "
            "RETURNING id"
        ),
        row,
    ).scalar_one()


def _insert_closed_unit(conn, part_id: int, outcome: str = "to_shop",
                        bucket: str | None = "good") -> int:
    return conn.execute(
        text(
            "INSERT INTO maintenance_cycle_unit"
            " (part_id, pn, station, outcome, condition_bucket, source_kind,"
            "  created_by, updated_by, closed_at)"
            " VALUES (:part_id, 'PN-CYCLE-T1', 'closed', :outcome, :bucket,"
            "         'external', 't1-test', 't1-test', now())"
            " RETURNING id"
        ),
        {"part_id": part_id, "outcome": outcome, "bucket": bucket},
    ).scalar_one()


def _insert_intent(conn, unit_id: int, part_id: int, **overrides) -> str:
    row = {
        "unit_id": unit_id,
        "part_id": part_id,
        "qty": 1,
        "condition_bucket": "good",
        "state": "pending",
    }
    row.update(overrides)
    columns = ", ".join(row)
    params = ", ".join(f":{key}" for key in row)
    return str(
        conn.execute(
            text(
                "INSERT INTO maintenance_cycle_sell_intent"
                " (delivery_id, " + columns + ")"
                " VALUES (gen_random_uuid(), " + params + ")"
                " RETURNING delivery_id"
            ),
            row,
        ).scalar_one()
    )


def _insert_delivering_intent(conn, unit_id: int, part_id: int) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO maintenance_cycle_sell_intent"
                " (delivery_id, unit_id, part_id, qty, condition_bucket, state,"
                "  lease_token, lease_until)"
                " VALUES (gen_random_uuid(), :unit_id, :part_id, 1, 'good',"
                "         'delivering', gen_random_uuid(),"
                "         now() + interval '60 seconds')"
                " RETURNING delivery_id"
            ),
            {"unit_id": unit_id, "part_id": part_id},
        ).scalar_one()
    )


def _insert_pending_with_lease(conn, unit_id: int, part_id: int) -> None:
    conn.execute(
        text(
            "INSERT INTO maintenance_cycle_sell_intent"
            " (delivery_id, unit_id, part_id, qty, condition_bucket, state,"
            "  lease_token, lease_until)"
            " VALUES (gen_random_uuid(), :unit_id, :part_id, 1, 'good',"
            "         'pending', gen_random_uuid(), now() + interval '60 seconds')"
        ),
        {"unit_id": unit_id, "part_id": part_id},
    )


def _withdraw_to_other_station(conn, unit_id: int) -> None:
    conn.execute(
        text(
            "UPDATE maintenance_cycle_unit"
            " SET withdrawn_at = now(), station = 'testing'"
            " WHERE id = :unit_id"
        ),
        {"unit_id": unit_id},
    )


def _seed_receipt(conn, project_id: str = "proj-cycle-t1") -> str:
    conn.execute(
        text(
            "INSERT INTO maintenance_project"
            " (project_id, project_code, display_name, lifecycle_status)"
            " VALUES (:pid, :code, :name, 'ongoing')"
        ),
        {"pid": project_id, "code": "T1-CYCLE", "name": "T1 循环池测试项目"},
    )
    receipt_id = "rkd-cycle-t1"
    conn.execute(
        text(
            "INSERT INTO maintenance_rkd_return_line"
            " (rkd_line_id, project_id, source, head_no, source_ref, pn, qty)"
            " VALUES (:rid, :pid, 'manual', 'HEAD-T1', 'REF-T1', 'PN-CYCLE-T1', 1)"
        ),
        {"rid": receipt_id, "pid": project_id},
    )
    return receipt_id


# ---------- 断言 helper：同一连接 SAVEPOINT，失败只回滚到保存点 ----------
def _expect_check_violation(conn, constraint_name: str, action) -> None:
    with pytest.raises(DBAPIError) as exc_info:
        with conn.begin_nested():
            action(conn)
    orig = exc_info.value.orig
    assert isinstance(orig, CheckViolation), f"期望 CheckViolation，实际 {type(orig).__name__}"
    assert orig.diag.constraint_name == constraint_name, (
        f"期望命中 {constraint_name}，实际 {orig.diag.constraint_name}"
    )


def _expect_unique_violation(conn, constraint_name: str, action) -> None:
    with pytest.raises(DBAPIError) as exc_info:
        with conn.begin_nested():
            action(conn)
    orig = exc_info.value.orig
    assert isinstance(orig, UniqueViolation), f"期望 UniqueViolation，实际 {type(orig).__name__}"
    assert orig.diag.constraint_name == constraint_name, (
        f"期望命中 {constraint_name}，实际 {orig.diag.constraint_name}"
    )


# ---------- 1. revision 血缘与单 head ----------
def test_new_revision_is_additive_child_of_frozen_baseline_and_single_head():
    script = ScriptDirectory.from_config(_cfg())
    revisions = {revision.revision: revision for revision in script.walk_revisions()}
    assert REVISION in revisions, "新 revision 未创建"
    assert revisions[REVISION].down_revision == PREVIOUS, "父修订必须是冻结基线"
    heads = set(script.get_heads())
    assert len(heads) == 1, "迁移链必须只有一个 head"
    head = heads.pop()
    assert head == REVISION
    ancestors = {
        revision.revision
        for revision in script.walk_revisions(base="base", head=head)
    }
    assert PREVIOUS in ancestors, "冻结基线 98afc0d2157b 必须仍是新 head 的祖先"


# ---------- 2. 三表结构（列/约束/索引）----------
def test_new_tables_columns_constraints_and_indexes(db):
    inspector = inspect(db.get_bind())
    unit_columns = {
        column["name"]: column
        for column in inspector.get_columns("maintenance_cycle_unit")
    }
    assert "qty" not in unit_columns, "一行一件：单位表不得有 qty 列"
    assert "withdrawn_at" in unit_columns
    assert unit_columns["withdrawn_at"]["type"].timezone is True
    assert unit_columns["source_receipt_id"]["type"].length == 36
    assert unit_columns["station"]["default"] is not None

    unit_checks = {c["name"] for c in inspector.get_check_constraints("maintenance_cycle_unit")}
    assert {
        "ck_maintenance_cycle_unit_station",
        "ck_maintenance_cycle_unit_source_shape",
        "ck_maintenance_cycle_unit_closed_shape",
        "ck_maintenance_cycle_unit_withdrawn",
        "ck_maintenance_cycle_unit_shop_bucket",
        "ck_maintenance_cycle_unit_counters",
    } <= unit_checks

    unit_uniques = {
        c["name"] for c in inspector.get_unique_constraints("maintenance_cycle_unit")
    }
    assert "uq_maintenance_cycle_unit_uid" in unit_uniques

    unit_indexes = {
        index["name"]: index
        for index in inspector.get_indexes("maintenance_cycle_unit")
    }
    assert {"ix_maintenance_cycle_unit_station", "ix_maintenance_cycle_unit_part",
            "ix_maintenance_cycle_unit_receipt",
            "ux_maintenance_cycle_unit_receipt_sn"} <= set(unit_indexes)
    assert unit_indexes["ux_maintenance_cycle_unit_receipt_sn"]["unique"] is True

    event_columns = {
        column["name"] for column in inspector.get_columns("maintenance_cycle_event")
    }
    assert {"id", "unit_id", "event_type", "payload", "actor", "created_at"} <= event_columns
    event_indexes = {
        index["name"] for index in inspector.get_indexes("maintenance_cycle_event")
    }
    assert "ix_maintenance_cycle_event_unit" in event_indexes

    intent_columns = {
        column["name"] for column in inspector.get_columns("maintenance_cycle_sell_intent")
    }
    assert {
        "delivery_id", "unit_id", "part_id", "qty", "condition_bucket", "state",
        "attempts", "available_at", "lease_token", "lease_until",
        "receipt_status", "block_code", "last_error_code", "completed_at",
    } <= intent_columns
    intent_checks = {
        c["name"]
        for c in inspector.get_check_constraints("maintenance_cycle_sell_intent")
    }
    assert {
        "ck_maintenance_cycle_sell_qty",
        "ck_maintenance_cycle_sell_lease",
        "ck_maintenance_cycle_sell_state",
        "ck_maintenance_cycle_sell_receipt",
    } <= intent_checks
    intent_uniques = {
        c["name"]
        for c in inspector.get_unique_constraints("maintenance_cycle_sell_intent")
    }
    assert "uq_maintenance_cycle_sell_intent_unit" in intent_uniques
    intent_indexes = {
        index["name"]: index
        for index in inspector.get_indexes("maintenance_cycle_sell_intent")
    }
    assert {"ix_maintenance_cycle_sell_pending", "ix_maintenance_cycle_sell_lease",
            "ix_maintenance_cycle_sell_blocked"} <= set(intent_indexes)


# ---------- 3. 单位约束 ----------
def test_invalid_station_is_rejected(db):
    db.rollback()
    with engine.begin() as conn:
        _set_timeouts(conn)
        part_id = _insert_part(conn)
        _expect_check_violation(
            conn,
            "ck_maintenance_cycle_unit_station",
            lambda c: _insert_unit(c, part_id, station="bogus"),
        )


def test_closed_and_withdrawn_shape_constraints(db):
    db.rollback()
    with engine.begin() as conn:
        _set_timeouts(conn)
        part_id = _insert_part(conn)
        unit_id = _insert_unit(conn, part_id)
        # closed 当且仅当 outcome 与 closed_at 都有值：工位 closed 但两者都空 → 违反
        _expect_check_violation(
            conn,
            "ck_maintenance_cycle_unit_closed_shape",
            lambda c: _insert_unit(c, part_id, station="closed"),
        )
        # 撤回后工位必须回 pool 且 outcome/closed_at 为空
        _expect_check_violation(
            conn,
            "ck_maintenance_cycle_unit_withdrawn",
            lambda c: _withdraw_to_other_station(c, unit_id),
        )


def test_outcome_requires_condition_bucket(db):
    db.rollback()
    with engine.begin() as conn:
        _set_timeouts(conn)
        part_id = _insert_part(conn)
        # to_shop 且桶为空：closed 形状合法，只有桶检查失败（NULL 不得被 CHECK 放过）
        _expect_check_violation(
            conn,
            "ck_maintenance_cycle_unit_shop_bucket",
            lambda c: _insert_closed_unit(c, part_id, outcome="to_shop", bucket=None),
        )
        _insert_closed_unit(conn, part_id, outcome="to_shop", bucket="good")


def test_source_kind_shape(db):
    db.rollback()
    with engine.begin() as conn:
        _set_timeouts(conn)
        part_id = _insert_part(conn)
        receipt_id = _seed_receipt(conn)
        # receipt 来源必须有收货行外键（外键为 NULL → 只有形状 CHECK 失败）
        _expect_check_violation(
            conn,
            "ck_maintenance_cycle_unit_source_shape",
            lambda c: _insert_unit(c, part_id, source_kind="receipt"),
        )
        # external 必须为空；带一个真实存在的收货行 id 使外键本身合法，只留形状 CHECK
        _expect_check_violation(
            conn,
            "ck_maintenance_cycle_unit_source_shape",
            lambda c: _insert_unit(
                c, part_id, source_kind="external", source_receipt_id=receipt_id,
            ),
        )


# ---------- 4. 可售意图约束（同一事务 + SAVEPOINT）----------
def test_qty_must_be_one(db):
    db.rollback()
    with engine.begin() as conn:
        _set_timeouts(conn)
        part_id = _insert_part(conn)
        unit_id = _insert_unit(conn, part_id)
        for bad_qty in (2, 0, 0.5):
            _expect_check_violation(
                conn,
                "ck_maintenance_cycle_sell_qty",
                lambda c, qty=bad_qty: _insert_intent(c, unit_id, part_id, qty=qty),
            )
        _insert_intent(conn, unit_id, part_id, qty=1)


def test_one_unit_allows_only_one_sell_intent(db):
    db.rollback()
    with engine.begin() as conn:
        _set_timeouts(conn)
        part_id = _insert_part(conn)
        unit_id = _insert_unit(conn, part_id)
        _insert_intent(conn, unit_id, part_id)
        _expect_unique_violation(
            conn,
            "uq_maintenance_cycle_sell_intent_unit",
            lambda c: _insert_intent(c, unit_id, part_id),
        )


def test_delivering_requires_lease(db):
    db.rollback()
    with engine.begin() as conn:
        _set_timeouts(conn)
        part_id = _insert_part(conn)
        unit_id = _insert_unit(conn, part_id)
        _expect_check_violation(
            conn,
            "ck_maintenance_cycle_sell_lease",
            lambda c: _insert_intent(c, unit_id, part_id, state="delivering"),
        )
        # 等价关系反向也成立：pending 不允许带租约
        _expect_check_violation(
            conn,
            "ck_maintenance_cycle_sell_lease",
            lambda c: _insert_pending_with_lease(c, unit_id, part_id),
        )
        _insert_delivering_intent(conn, unit_id, part_id)


# ---------- 5. SN 部分唯一（同一收货行、未撤回）----------
def test_serial_number_partial_unique_per_receipt(db):
    db.rollback()
    with engine.begin() as conn:
        _set_timeouts(conn)
        part_id = _insert_part(conn)
        receipt_id = _seed_receipt(conn)
        first = _insert_unit(
            conn, part_id, source_kind="receipt", source_receipt_id=receipt_id,
            serial_number="SN-T1-001",
        )
        # 同收货行同 SN 的第二个未撤回单位必须被拒（savepoint 回滚，父事务继续）
        _expect_unique_violation(
            conn,
            "ux_maintenance_cycle_unit_receipt_sn",
            lambda c: _insert_unit(
                c, part_id, source_kind="receipt",
                source_receipt_id=receipt_id, serial_number="SN-T1-001",
            ),
        )
        # 撤回后释放 SN；空 SN 不受部分唯一约束影响
        conn.execute(
            text(
                "UPDATE maintenance_cycle_unit SET withdrawn_at = now() "
                "WHERE id = :unit_id"
            ),
            {"unit_id": first},
        )
        _insert_unit(
            conn, part_id, source_kind="receipt", source_receipt_id=receipt_id,
            serial_number="SN-T1-001",
        )
        for _ in range(2):
            _insert_unit(
                conn, part_id, source_kind="receipt",
                source_receipt_id=receipt_id, serial_number=None,
            )


# ---------- 6. 五页键默认 false（代码层）----------
def test_cycle_page_keys_registered_and_fail_closed_by_default():
    for key in CYCLE_PAGE_KEYS:
        assert key in permissions.PAGE_KEYS
        assert key in permissions.CYCLE_PAGE_KEYS
        assert key in permissions.ACCOUNT_SCOPED_BETA_PAGE_KEYS
        assert key in permissions.LABELS
        meta = permissions.PERMISSION_META[key]
        for field in ("label", "summary", "can", "cannot", "typical",
                      "sensitivity", "risk"):
            assert meta.get(field), f"{key} 缺业务语言字段 {field}"

    # _full() 全开图与全部内置模板：五个键一律 false（含 admin）
    full = permissions._full()
    for key in CYCLE_PAGE_KEYS:
        assert full[key] is False
        for role in BUILTIN_ROLES:
            assert permissions.ROLE_TEMPLATES[role].get(key, False) is False, (
                f"{role} 模板的 {key} 必须默认 false"
            )
        for role in BUILTIN_ROLES:
            assert permissions.effective(role, None)[key] is False

    # 权限中心矩阵：五个键出现在「页面入口」组
    page_group = next(group for group in permissions.UI_GROUPS if group["key"] == "page")
    assert set(CYCLE_PAGE_KEYS) <= set(page_group["keys"])


def test_admin_requires_explicit_account_snapshot_for_cycle_pages():
    class _User:
        role = "admin"
        permissions = None
        template_perms = None
        perm_overrides = None

    no_snapshot = permissions.effective_for_user(_User())
    for key in CYCLE_PAGE_KEYS:
        assert no_snapshot[key] is False, "admin 无账号快照时循环页键必须失败关闭"
        assert permissions.page_permission_allowed(
            role="admin", permission_map=no_snapshot, page_key=key
        ) is False

    granted = _User()
    granted.template_perms = {key: True for key in CYCLE_PAGE_KEYS}
    granted.perm_overrides = {}
    effective = permissions.effective_for_user(granted)
    for key in CYCLE_PAGE_KEYS:
        assert effective[key] is True
        assert permissions.page_permission_allowed(
            role="admin", permission_map=effective, page_key=key
        ) is True


# ---------- 7. 迁移回填：DB 模板全 false ----------
def test_migrated_db_templates_default_cycle_pages_false(db):
    with engine.connect() as conn:
        rows = dict(
            conn.execute(text("SELECT code, permissions FROM sys_role_template")).all()
        )
    assert set(BUILTIN_ROLES) <= set(rows)
    for role in BUILTIN_ROLES:
        for key in CYCLE_PAGE_KEYS:
            assert rows[role].get(key) is False, f"{role} 模板 {key} 必须为 false"


# ---------- 8. 升降级：downgrade 移除 / upgrade 回填 ----------
def test_downgrade_to_frozen_baseline_removes_everything_and_upgrade_restores(db):
    db.rollback()
    cfg = _cfg()
    try:
        alembic_command.downgrade(cfg, PREVIOUS)
        assert _db_version() == PREVIOUS
        inspector = inspect(engine)
        for table in NEW_TABLES:
            assert not inspector.has_table(table), f"downgrade 后不应存在 {table}"
        with engine.begin() as conn:
            _set_timeouts(conn)
            permissions_json = conn.execute(
                text("SELECT permissions FROM sys_role_template WHERE code = 'admin'")
            ).scalar_one()
            assert all(key not in permissions_json for key in CYCLE_PAGE_KEYS)
            # 模拟旧库遗留显式 true：upgrade 必须重新回填 false
            conn.execute(
                text(
                    "UPDATE sys_role_template SET permissions = permissions || "
                    "CAST(:blob AS jsonb) WHERE code = 'admin'"
                ),
                {"blob": json.dumps({"page_cycle_test": True})},
            )

        alembic_command.upgrade(cfg, "head")
        assert _db_version() == REVISION
        inspector = inspect(engine)
        for table in NEW_TABLES:
            assert inspector.has_table(table), f"upgrade 后应恢复 {table}"
        with engine.connect() as conn:
            permissions_json = conn.execute(
                text("SELECT permissions FROM sys_role_template WHERE code = 'admin'")
            ).scalar_one()
            assert all(permissions_json[key] is False for key in CYCLE_PAGE_KEYS)
    finally:
        _ensure_head()


# ---------- 9. F-01：NULL template_perms 旧账号不因迁移制造快照/丢权限 ----------
def test_null_template_perms_legacy_account_not_snapshotted_and_keeps_permissions(db):
    """复盘 F-01：旧账号 template_perms IS NULL 时，升级不得创建「只有五键的快照」。

    期望：template_perms 保持 NULL（继续走角色模板/legacy 图回退）；legacy
    permissions 仅合并五键 false 并保留原有键；降级只删五键、不制造 '{}'。
    """
    from types import SimpleNamespace

    db.rollback()
    cfg = _cfg()
    try:
        alembic_command.downgrade(cfg, PREVIOUS)
        with engine.begin() as conn:
            _set_timeouts(conn)
            conn.execute(
                text(
                    "INSERT INTO sys_user"
                    " (username, role, display_name, password_hash, is_active,"
                    "  template_perms, perm_overrides, permissions)"
                    " VALUES ('f01-legacy-null', 'sales', '旧账号', 'x', true,"
                    "         NULL, NULL, CAST(:p AS jsonb))"
                ),
                {"p": json.dumps({"page_parts": True})},
            )

        alembic_command.upgrade(cfg, "head")
        assert _db_version() == REVISION
        with engine.connect() as conn:
            template_perms, perm_overrides, legacy = conn.execute(
                text(
                    "SELECT template_perms, perm_overrides, permissions"
                    " FROM sys_user WHERE username = 'f01-legacy-null'"
                )
            ).one()
        assert template_perms is None, "升级不得为 NULL 快照账号创建 template_perms"
        assert perm_overrides is None
        assert legacy["page_parts"] is True, "旧账号原有权限不得丢失"
        for key in CYCLE_PAGE_KEYS:
            assert legacy[key] is False, f"legacy 图必须写入 {key}=false"

        # 用真实权限函数断言有效权限：原有 page_parts 保留、五循环键失败关闭
        user = SimpleNamespace(
            role="sales",
            permissions=legacy,
            template_perms=None,
            perm_overrides=None,
            is_active=True,
        )
        effective = permissions.effective_for_user(user)
        assert effective["page_parts"] is True
        for key in CYCLE_PAGE_KEYS:
            assert effective[key] is False

        # 降级：只删五键，不制造 '{}' 快照，legacy 图回到原状
        alembic_command.downgrade(cfg, PREVIOUS)
        assert _db_version() == PREVIOUS
        with engine.connect() as conn:
            template_perms2, legacy2 = conn.execute(
                text(
                    "SELECT template_perms, permissions FROM sys_user"
                    " WHERE username = 'f01-legacy-null'"
                )
            ).one()
        assert template_perms2 is None
        assert legacy2 == {"page_parts": True}
    finally:
        _ensure_head()
