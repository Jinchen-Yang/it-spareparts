"""DEV-15 migration lifecycle and fail-closed downgrade tests."""

import io
import json
import os
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from app import permissions as permission_service
from app.etl import loader
from app.models.maintenance import (
    FMaintenanceLine,
    FProjectExpense,
    MaintenanceContractWorkbookState,
    MaintenanceManualCostOverride,
    MaintenanceRoundtripOperation,
)
from app.models.system import SysImportBatch
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests import factories as f

_PREV = "e5f9a2b3c4d5"
_DEV15_HEAD = "f1c8e4a7b2d9"


def _cfg(*, output_buffer=None) -> AlembicConfig:
    cfg = AlembicConfig(
        os.path.join(os.path.dirname(__file__), "..", "alembic.ini"),
        output_buffer=output_buffer,
    )
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def _current_head() -> str:
    head = ScriptDirectory.from_config(_cfg()).get_current_head()
    assert head is not None
    return head


def _maintenance_line(db, suffix: str) -> tuple[SysImportBatch, FMaintenanceLine]:
    batch = SysImportBatch(
        filename=f"dev15-migration-{suffix}.xlsx",
        file_type="maintenance",
        file_hash=f"dev15-migration-{suffix}",
        status="success",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.maintenance_result(
            {
                f"M-{suffix}": f.maintenance_head(
                    f"M-{suffix}",
                    on=date(2026, 7, 1),
                    sales_order=f"XS-{suffix}",
                ),
            },
            [
                f.maintenance_line(
                    f"M-{suffix}",
                    f"ML-{suffix}",
                    f"PN-{suffix}",
                    qty="1",
                ),
            ],
        ),
        batch.id,
        date(2026, 7, 2),
    )
    line = db.scalar(
        select(FMaintenanceLine).where(
            FMaintenanceLine.raw_line_id == f"ML-{suffix}",
        )
    )
    assert line is not None
    return batch, line


def _assert_rejected(engine, sql: str, params: dict) -> None:
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(IntegrityError):
            connection.execute(text(sql), params)
        transaction.rollback()


def _business_setting_snapshot(db) -> dict:
    return dict(db.execute(text(
        """
        SELECT id,
               maintenance_project_profit_default_basis,
               purchase_display_basis,
               sales_display_basis,
               version,
               updated_by,
               updated_at
        FROM sys_business_setting
        WHERE id = 1
        """
    )).mappings().one())


def _cleanup_downgrade_guard_case(
    engine,
    *,
    artifacts: dict,
    setting_before: dict,
) -> None:
    """Delete only rows created by one parametrized downgrade-guard case."""
    with engine.begin() as connection:
        for table, artifact_id in (
            (
                "maintenance_roundtrip_operation",
                artifacts["operation_id"],
            ),
            (
                "maintenance_manual_cost_override",
                artifacts["manual_override_id"],
            ),
            ("f_project_expense", artifacts["expense_id"]),
        ):
            if artifact_id is not None:
                connection.execute(
                    text(f"DELETE FROM {table} WHERE id = :artifact_id"),
                    {"artifact_id": artifact_id},
                )

        if artifacts["state_contract_no"] is not None:
            connection.execute(
                text(
                    """
                    DELETE FROM maintenance_contract_workbook_state
                    WHERE contract_no = :contract_no
                    """
                ),
                {"contract_no": artifacts["state_contract_no"]},
            )

        connection.execute(
            text("DELETE FROM f_maintenance_line WHERE id = :line_id"),
            {"line_id": artifacts["line_id"]},
        )
        connection.execute(
            text("DELETE FROM f_maintenance_order WHERE id = :order_id"),
            {"order_id": artifacts["order_id"]},
        )
        connection.execute(
            text("DELETE FROM part_alias WHERE id = :alias_id"),
            {"alias_id": artifacts["alias_id"]},
        )
        connection.execute(
            text("DELETE FROM dim_part WHERE id = :part_id"),
            {"part_id": artifacts["part_id"]},
        )
        connection.execute(
            text("DELETE FROM dim_customer WHERE id = :customer_id"),
            {"customer_id": artifacts["customer_id"]},
        )

        if artifacts["roundtrip_batch_id"] is not None:
            connection.execute(
                text("DELETE FROM sys_import_batch WHERE id = :batch_id"),
                {"batch_id": artifacts["roundtrip_batch_id"]},
            )
        connection.execute(
            text("DELETE FROM sys_import_batch WHERE id = :batch_id"),
            {"batch_id": artifacts["batch_id"]},
        )

        connection.execute(
            text(
                """
                UPDATE sys_business_setting
                SET maintenance_project_profit_default_basis =
                        :maintenance_basis,
                    purchase_display_basis = :purchase_basis,
                    sales_display_basis = :sales_basis,
                    version = :version,
                    updated_by = :updated_by,
                    updated_at = :updated_at
                WHERE id = :setting_id
                """
            ),
            {
                "setting_id": setting_before["id"],
                "maintenance_basis":
                    setting_before[
                        "maintenance_project_profit_default_basis"
                    ],
                "purchase_basis":
                    setting_before["purchase_display_basis"],
                "sales_basis": setting_before["sales_display_basis"],
                "version": setting_before["version"],
                "updated_by": setting_before["updated_by"],
                "updated_at": setting_before["updated_at"],
            },
        )


def test_dev15_upgrade_constraints_and_clean_round_trip(db):
    batch, line = _maintenance_line(db, "CLEAN")
    db.commit()
    engine = db.get_bind()
    batch_id = batch.id
    line_id = line.id
    db.close()
    cfg = _cfg()

    try:
        alembic_command.downgrade(cfg, _PREV)
        with engine.begin() as connection:
            # The predecessor schema already supports the maintenance display
            # basis and optimistic-lock version. A non-default legacy value is
            # losslessly representable after downgrade and must not be mistaken
            # for a DEV-15-only write.
            connection.execute(
                text(
                    """
                    UPDATE sys_business_setting
                    SET maintenance_project_profit_default_basis = 'inc',
                        version = 7
                    WHERE id = 1
                    """
                )
            )

        alembic_command.upgrade(cfg, _DEV15_HEAD)
        with engine.connect() as connection:
            assert {
                "maintenance_manual_cost_override",
                "maintenance_contract_workbook_state",
                "maintenance_roundtrip_operation",
            } <= set(inspect(connection).get_table_names())
            success_index = next(
                index
                for index in inspect(connection).get_indexes("sys_import_batch")
                if index["name"] == "ux_batch_success_hash"
            )
            assert success_index["column_names"] == ["file_type", "file_hash"]

        # Explicit inc-basis input is valid when the inverse 13% result is
        # cent-rounded; the transaction is intentionally rolled back so the
        # following clean downgrade remains lossless.
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(
                text(
                    """
                    INSERT INTO f_project_expense
                        (raw_line_id, data_status, linked_sales_order_no,
                         amount, amount_ex_tax, amount_inc_tax, tax_basis,
                         tax_rate_used, import_batch_id)
                    VALUES
                        ('EXP-DEV15-INC', '已结束', 'XS-CLEAN',
                         1, 0.88, 1, 'inc', 0.13, :batch_id)
                    """
                ),
                {"batch_id": batch_id},
            )
            transaction.rollback()

        expense_insert = """
            INSERT INTO f_project_expense
                (raw_line_id, data_status, linked_sales_order_no,
                 amount, amount_ex_tax, amount_inc_tax, tax_basis,
                 tax_rate_used, import_batch_id)
            VALUES
                (:raw_id, '已结束', 'XS-CLEAN',
                 :amount, :amount_ex, :amount_inc, :basis, 0.13, :batch_id)
        """
        _assert_rejected(
            engine,
            expense_insert,
            {
                "raw_id": "EXP-DEV15-ONE-SIDED",
                "amount": Decimal("100"),
                "amount_ex": None,
                "amount_inc": None,
                "basis": "default_ex",
                "batch_id": batch_id,
            },
        )
        _assert_rejected(
            engine,
            expense_insert,
            {
                "raw_id": "EXP-DEV15-RAW-EX-MISMATCH",
                "amount": Decimal("999"),
                "amount_ex": Decimal("0.50"),
                "amount_inc": Decimal("0.57"),
                "basis": "ex",
                "batch_id": batch_id,
            },
        )
        _assert_rejected(
            engine,
            expense_insert,
            {
                "raw_id": "EXP-DEV15-RAW-INC-MISMATCH",
                "amount": Decimal("0.88"),
                "amount_ex": Decimal("0.88"),
                "amount_inc": Decimal("1.00"),
                "basis": "inc",
                "batch_id": batch_id,
            },
        )
        _assert_rejected(
            engine,
            expense_insert,
            {
                "raw_id": "EXP-DEV15-MISMATCH",
                "amount": Decimal("100"),
                "amount_ex": Decimal("100"),
                "amount_inc": Decimal("112.99"),
                "basis": "default_ex",
                "batch_id": batch_id,
            },
        )
        # Python Decimal's implicit default is half-even, while PostgreSQL
        # numeric round is half-away-from-zero. The persisted relation must
        # reject the old half-even result at an exact .005 midpoint.
        _assert_rejected(
            engine,
            expense_insert,
            {
                "raw_id": "EXP-DEV15-HALF-EVEN",
                "amount": Decimal("0.50"),
                "amount_ex": Decimal("0.50"),
                "amount_inc": Decimal("0.56"),
                "basis": "default_ex",
                "batch_id": batch_id,
            },
        )
        _assert_rejected(
            engine,
            """
            INSERT INTO maintenance_manual_cost_override
                (line_id, unit_cost_ex_tax, unit_cost_inc_tax,
                 tax_rate_used, reason)
            VALUES (:line_id, 100, 112.99, 0.13, 'invalid')
            """,
            {"line_id": line_id},
        )
        _assert_rejected(
            engine,
            """
            INSERT INTO maintenance_manual_cost_override
                (line_id, unit_cost_ex_tax, unit_cost_inc_tax,
                 tax_rate_used, reason)
            VALUES (:line_id, 2.50, 2.82, 0.13, 'old half-even value')
            """,
            {"line_id": line_id},
        )

        alembic_command.downgrade(cfg, _PREV)
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    """
                    SELECT maintenance_project_profit_default_basis, version
                    FROM sys_business_setting
                    WHERE id = 1
                    """
                )
            ).one() == ("inc", 7)
            columns = {
                column["name"]
                for column in inspect(connection).get_columns("f_project_expense")
            }
            assert {
                "amount_ex_tax",
                "amount_inc_tax",
                "tax_basis",
                "tax_rate_used",
            }.isdisjoint(columns)
            assert "maintenance_manual_cost_override" not in inspect(
                connection
            ).get_table_names()
            assert "maintenance_roundtrip_operation" not in inspect(
                connection
            ).get_table_names()
            legacy_success_index = next(
                index
                for index in inspect(connection).get_indexes("sys_import_batch")
                if index["name"] == "ux_batch_success_hash"
            )
            assert legacy_success_index["column_names"] == ["file_hash"]

        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == _current_head()
    finally:
        alembic_command.upgrade(cfg, "head")


def test_dev15_upgrade_refuses_unaudited_legacy_expenses(db):
    engine = db.get_bind()
    db.close()
    cfg = _cfg()
    batch_id = None

    try:
        alembic_command.downgrade(cfg, _PREV)
        with engine.begin() as connection:
            batch_id = connection.execute(
                text(
                    """
                    INSERT INTO sys_import_batch
                        (filename, file_type, file_hash, status)
                    VALUES
                        ('legacy-expense.xlsx', 'expense',
                         'dev15-unaudited-legacy-expense', 'success')
                    RETURNING id
                    """
                )
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO f_project_expense
                        (raw_line_id, data_status, linked_sales_order_no,
                         amount, import_batch_id)
                    VALUES
                        ('EXP-DEV15-UNAUDITED', '已结束',
                         'XS-DEV15-UNAUDITED', 113, :batch_id)
                    """
                ),
                {"batch_id": batch_id},
            )

        with pytest.raises(
            DBAPIError,
            match="historical project expenses require "
            "archived-workbook tax-basis audit and replay",
        ):
            alembic_command.upgrade(cfg, _DEV15_HEAD)

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == _PREV
            assert connection.execute(
                text(
                    """
                    SELECT amount
                    FROM f_project_expense
                    WHERE raw_line_id = 'EXP-DEV15-UNAUDITED'
                    """
                )
            ).scalar_one() == Decimal("113.00")
            columns = {
                column["name"]
                for column in inspect(connection).get_columns(
                    "f_project_expense"
                )
            }
            assert "tax_basis" not in columns
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    DELETE FROM f_project_expense
                    WHERE raw_line_id = 'EXP-DEV15-UNAUDITED'
                    """
                )
            )
            if batch_id is not None:
                connection.execute(
                    text(
                        """
                        DELETE FROM sys_import_batch
                        WHERE id = :batch_id
                        """
                    ),
                    {"batch_id": batch_id},
                )
        alembic_command.upgrade(cfg, "head")


def test_dev15_upgrade_normalizes_legacy_json_null_permission_payloads(db):
    """Production legacy rows store JSON ``null``, not SQL NULL or objects."""
    engine = db.get_bind()
    user_id = db.execute(
        text(
            """
            INSERT INTO sys_user
                (username, role, password_hash, template_perms,
                 permissions, perm_overrides)
            VALUES
                ('dev15-json-null-user', 'sales', 'not-used',
                 '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)
            RETURNING id
            """
        )
    ).scalar_one()
    db.commit()
    db.close()
    cfg = _cfg()

    try:
        alembic_command.downgrade(cfg, _PREV)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE sys_user
                    SET permissions = 'null'::jsonb,
                        perm_overrides = 'null'::jsonb
                    WHERE id = :user_id
                    """
                ),
                {"user_id": user_id},
            )

        alembic_command.upgrade(cfg, _DEV15_HEAD)
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT jsonb_typeof(permissions),
                           jsonb_typeof(perm_overrides),
                           jsonb_typeof(template_perms),
                           permissions
                               ? 'action_maintenance_roundtrip_apply',
                           permissions
                               ->> 'action_maintenance_roundtrip_apply',
                           template_perms
                               ->> 'action_maintenance_roundtrip_apply',
                           perm_overrides
                               ? 'action_maintenance_roundtrip_apply'
                    FROM sys_user
                    WHERE id = :user_id
                    """
                ),
                {"user_id": user_id},
            ).one()
            assert row == (
                "object",
                "object",
                "object",
                True,
                "false",
                "false",
                False,
            )
    finally:
        alembic_command.upgrade(cfg, "head")
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM sys_user WHERE id = :user_id"),
                {"user_id": user_id},
            )


@pytest.mark.parametrize(
    "role",
    ["admin", "boss", "sales", "purchaser", "readonly", "unknown-role"],
)
def test_dev15_legacy_permissions_json_null_normalization_is_equivalent(role):
    """Replacing a legacy JSON null with only the new role default is neutral."""
    normalized = {
        "action_maintenance_roundtrip_apply": role in ("admin", "boss"),
    }
    assert permission_service.effective(
        role,
        normalized,
    ) == permission_service.effective(role, None)


@pytest.mark.parametrize("sentinel_kind", ["sql_null", "json_null"])
def test_dev15_migration_preserves_legacy_template_fallback(
    db,
    sentinel_kind,
):
    """Both NULL encodings must keep role + legacy-permissions fallback."""
    engine = db.get_bind()
    user_id = db.execute(
        text(
            """
            INSERT INTO sys_user
                (username, role, password_hash, template_perms,
                 permissions, perm_overrides)
            VALUES
                (:username, 'sales', 'not-used',
                 '{}'::jsonb,
                 '{"page_inventory": false}'::jsonb,
                 '{}'::jsonb)
            RETURNING id
            """
        ),
        {"username": f"dev15-{sentinel_kind}-template"},
    ).scalar_one()
    db.commit()
    db.close()
    cfg = _cfg()

    try:
        alembic_command.downgrade(cfg, _PREV)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE sys_user
                    SET template_perms = CASE
                            WHEN :is_sql_null THEN NULL::jsonb
                            ELSE 'null'::jsonb
                        END,
                        perm_overrides = CASE
                            WHEN :is_sql_null THEN NULL::jsonb
                            ELSE 'null'::jsonb
                        END
                    WHERE id = :user_id
                    """
                ),
                {
                    "is_sql_null": sentinel_kind == "sql_null",
                    "user_id": user_id,
                },
            )

        def snapshot():
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT role, template_perms, permissions, perm_overrides,
                               template_perms IS NULL AS template_sql_null,
                               template_perms = 'null'::jsonb
                                   AS template_json_null,
                               perm_overrides IS NULL AS overrides_sql_null,
                               perm_overrides = 'null'::jsonb
                                   AS overrides_json_null
                        FROM sys_user
                        WHERE id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).mappings().one()
            effective = permission_service.effective_for_user(
                SimpleNamespace(
                    role=row["role"],
                    template_perms=row["template_perms"],
                    permissions=row["permissions"],
                    perm_overrides=row["perm_overrides"],
                )
            )
            markers = (
                row["template_sql_null"],
                row["template_json_null"],
                row["overrides_sql_null"],
                row["overrides_json_null"],
            )
            return row, effective, markers

        before_row, before_effective, before_markers = snapshot()
        expected_markers = (
            (True, None, True, None)
            if sentinel_kind == "sql_null"
            else (False, True, False, True)
        )
        assert before_markers == expected_markers

        alembic_command.upgrade(cfg, _DEV15_HEAD)
        upgraded_row, upgraded_effective, upgraded_markers = snapshot()
        assert upgraded_markers == expected_markers
        assert upgraded_effective == before_effective
        assert upgraded_row["permissions"]["page_inventory"] is False
        assert (
            upgraded_row["permissions"][
                "action_maintenance_roundtrip_apply"
            ]
            is False
        )

        alembic_command.downgrade(cfg, _PREV)
        downgraded_row, downgraded_effective, downgraded_markers = snapshot()
        assert downgraded_markers == expected_markers
        assert downgraded_effective == before_effective
        assert downgraded_row["permissions"] == before_row["permissions"]
    finally:
        alembic_command.upgrade(cfg, "head")
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM sys_user WHERE id = :user_id"),
                {"user_id": user_id},
            )


@pytest.mark.parametrize(
    ("target", "invalid_payload", "expected_type"),
    [
        ("role_template.permissions", [], "array"),
        ("user.template_perms", "invalid", "string"),
        ("user.permissions", 1, "number"),
        ("user.perm_overrides", True, "boolean"),
    ],
)
def test_dev15_upgrade_rejects_non_object_permission_payloads(
    db,
    target,
    invalid_payload,
    expected_type,
):
    engine = db.get_bind()
    user_id = db.execute(
        text(
            """
            INSERT INTO sys_user
                (username, role, password_hash, template_perms,
                 permissions, perm_overrides)
            VALUES
                ('dev15-json-array-user', 'sales', 'not-used',
                 '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)
            RETURNING id
            """
        )
    ).scalar_one()
    db.commit()
    db.close()
    cfg = _cfg()
    column = target.removeprefix("user.")
    original = None

    try:
        alembic_command.downgrade(cfg, _PREV)
        with engine.begin() as connection:
            if target == "role_template.permissions":
                original = connection.execute(
                    text(
                        """
                        SELECT permissions
                        FROM sys_role_template
                        WHERE code = 'sales'
                        """
                    )
                ).scalar_one()
                connection.execute(
                    text(
                        """
                        UPDATE sys_role_template
                        SET permissions = CAST(:invalid_payload AS jsonb)
                        WHERE code = 'sales'
                        """
                    ),
                    {"invalid_payload": json.dumps(invalid_payload)},
                )
            else:
                original = connection.execute(
                    text(f"SELECT {column} FROM sys_user WHERE id = :user_id"),
                    {"user_id": user_id},
                ).scalar_one()
                connection.execute(
                    text(
                        f"""
                        UPDATE sys_user
                        SET {column} = CAST(:invalid_payload AS jsonb)
                        WHERE id = :user_id
                        """
                    ),
                    {
                        "invalid_payload": json.dumps(invalid_payload),
                        "user_id": user_id,
                    },
                )

        with pytest.raises(
            DBAPIError,
            match="permission JSONB payload must be an object",
        ):
            alembic_command.upgrade(cfg, _DEV15_HEAD)

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == _PREV
            if target == "role_template.permissions":
                actual_type = connection.execute(
                    text(
                        """
                        SELECT jsonb_typeof(permissions)
                        FROM sys_role_template
                        WHERE code = 'sales'
                        """
                    )
                ).scalar_one()
            else:
                actual_type = connection.execute(
                    text(
                        f"""
                        SELECT jsonb_typeof({column})
                        FROM sys_user
                        WHERE id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one()
            assert actual_type == expected_type
    finally:
        with engine.begin() as connection:
            if target == "role_template.permissions" and original is not None:
                connection.execute(
                    text(
                        """
                        UPDATE sys_role_template
                        SET permissions = CAST(:original AS jsonb)
                        WHERE code = 'sales'
                        """
                    ),
                    {"original": json.dumps(original)},
                )
            elif original is not None:
                connection.execute(
                    text(
                        f"""
                        UPDATE sys_user
                        SET {column} = CAST(:original AS jsonb)
                        WHERE id = :user_id
                        """
                    ),
                    {
                        "original": json.dumps(original),
                        "user_id": user_id,
                    },
                )
        alembic_command.upgrade(cfg, "head")
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM sys_user WHERE id = :user_id"),
                {"user_id": user_id},
            )


def test_dev15_downgrade_rejects_non_object_permission_payloads(db):
    engine = db.get_bind()
    user_id = db.execute(
        text(
            """
            INSERT INTO sys_user
                (username, role, password_hash, template_perms,
                 permissions, perm_overrides)
            VALUES
                ('dev15-json-array-downgrade', 'sales', 'not-used',
                 '{}'::jsonb, '{}'::jsonb, '[]'::jsonb)
            RETURNING id
            """
        )
    ).scalar_one()
    db.commit()
    db.close()
    cfg = _cfg()
    with engine.connect() as connection:
        starting_version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert starting_version == _current_head()

    try:
        with pytest.raises(DBAPIError, match="downgrade blocked"):
            alembic_command.downgrade(cfg, _PREV)

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == starting_version
            assert connection.execute(
                text(
                    """
                    SELECT jsonb_typeof(perm_overrides)
                    FROM sys_user
                    WHERE id = :user_id
                    """
                ),
                {"user_id": user_id},
            ).scalar_one() == "array"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM sys_user WHERE id = :user_id"),
                {"user_id": user_id},
            )
        alembic_command.upgrade(cfg, "head")


@pytest.mark.parametrize(
    ("case", "preservation_sql"),
    [
        (
            "manual",
            "SELECT count(*) FROM maintenance_manual_cost_override",
        ),
        (
            "state",
            "SELECT count(*) FROM maintenance_contract_workbook_state",
        ),
        (
            "operation",
            "SELECT count(*) FROM maintenance_roundtrip_operation",
        ),
        (
            "roundtrip",
            "SELECT count(*) FROM sys_import_batch "
            "WHERE file_type='maint_roundtrip' AND status='success'",
        ),
        (
            "setting",
            "SELECT count(*) FROM sys_business_setting "
            "WHERE version=2 AND sales_display_basis='both'",
        ),
        (
            "expense",
            "SELECT count(*) FROM f_project_expense "
            "WHERE raw_line_id='EXP-DEV15-BLOCK'",
        ),
        (
            "expense_default_ex",
            "SELECT count(*) FROM f_project_expense "
            "WHERE raw_line_id='EXP-DEV15-DEFAULT-EX-BLOCK'",
        ),
    ],
)
def test_dev15_downgrade_guard_preserves_each_new_business_fact(
    db,
    case,
    preservation_sql,
):
    setting_before = _business_setting_snapshot(db)
    batch, line = _maintenance_line(db, f"BLOCK-{case}")
    order_identity = db.execute(text(
        """
        SELECT customer_id
        FROM f_maintenance_order
        WHERE id = :order_id
        """
    ), {"order_id": line.order_id}).mappings().one()
    alias_id = db.execute(text(
        """
        SELECT id
        FROM part_alias
        WHERE pn_raw = :pn_raw
          AND part_id = :part_id
        """
    ), {"pn_raw": line.pn_raw, "part_id": line.part_id}).scalar_one()
    artifacts = {
        "batch_id": batch.id,
        "line_id": line.id,
        "order_id": line.order_id,
        "part_id": line.part_id,
        "customer_id": order_identity["customer_id"],
        "alias_id": alias_id,
        "manual_override_id": None,
        "operation_id": None,
        "state_contract_no": None,
        "roundtrip_batch_id": None,
        "expense_id": None,
    }
    if case == "manual":
        manual_override = MaintenanceManualCostOverride(
            line_id=line.id,
            unit_cost_ex_tax=Decimal("100"),
            unit_cost_inc_tax=Decimal("113"),
            reason="confirmed",
            updated_by="migration-test",
        )
        db.add(manual_override)
        db.flush()
        artifacts["manual_override_id"] = manual_override.id
    elif case == "state":
        state = MaintenanceContractWorkbookState(
            contract_no="XS-DEV15-BLOCK",
            revision=1,
            expense_snapshot_complete=False,
        )
        db.add(state)
        artifacts["state_contract_no"] = state.contract_no
    elif case == "operation":
        operation = MaintenanceRoundtripOperation(
            export_id="00000000-0000-0000-0000-000000000001",
            sheet_code="04_报销明细",
            client_row_id="00000000-0000-0000-0000-000000000002",
            operation="CREATE",
            payload_hash="a" * 64,
            result_json={"status": "applied"},
            import_batch_id=batch.id,
            applied_by="migration-test",
        )
        db.add(operation)
        db.flush()
        artifacts["operation_id"] = operation.id
    elif case == "roundtrip":
        roundtrip_batch = SysImportBatch(
            filename="roundtrip.xlsx",
            file_type="maint_roundtrip",
            file_hash="dev15-roundtrip-success",
            status="success",
        )
        db.add(roundtrip_batch)
        db.flush()
        artifacts["roundtrip_batch_id"] = roundtrip_batch.id
    elif case == "setting":
        db.execute(text(
            """
            UPDATE sys_business_setting
            SET sales_display_basis='both', version=2
            WHERE id=1
            """
        ))
    elif case in {"expense", "expense_default_ex"}:
        is_default_ex = case == "expense_default_ex"
        expense = FProjectExpense(
            raw_line_id=(
                "EXP-DEV15-DEFAULT-EX-BLOCK"
                if is_default_ex
                else "EXP-DEV15-BLOCK"
            ),
            linked_sales_order_no="XS-DEV15-BLOCK",
            data_status="已结束",
            amount=Decimal("1"),
            amount_ex_tax=Decimal("1") if is_default_ex else Decimal("0.88"),
            amount_inc_tax=Decimal("1.13") if is_default_ex else Decimal("1"),
            tax_basis="default_ex" if is_default_ex else "inc",
            import_batch_id=batch.id,
        )
        db.add(expense)
        db.flush()
        artifacts["expense_id"] = expense.id
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(case)
    db.commit()

    engine = db.get_bind()
    db.close()
    cfg = _cfg()
    with engine.connect() as connection:
        starting_version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert starting_version == _current_head()
    try:
        with pytest.raises(DBAPIError, match="downgrade blocked"):
            alembic_command.downgrade(cfg, _PREV)

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == starting_version
            assert connection.execute(text(preservation_sql)).scalar_one() == 1
    finally:
        # A rejected PostgreSQL transactional migration stays at head. Keep the
        # cleanup safe if that invariant ever regresses by restoring head first.
        alembic_command.upgrade(cfg, "head")
        _cleanup_downgrade_guard_case(
            engine,
            artifacts=artifacts,
            setting_before=setting_before,
        )

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == _current_head()
        assert connection.execute(
            text(
                """
                SELECT maintenance_project_profit_default_basis,
                       purchase_display_basis,
                       sales_display_basis,
                       version,
                       updated_by,
                       updated_at
                FROM sys_business_setting
                WHERE id = 1
                """
            )
        ).one() == (
            setting_before[
                "maintenance_project_profit_default_basis"
            ],
            setting_before["purchase_display_basis"],
            setting_before["sales_display_basis"],
            setting_before["version"],
            setting_before["updated_by"],
            setting_before["updated_at"],
        )


def test_dev15_downgrade_guard_renders_in_offline_sql():
    output = io.StringIO()
    alembic_command.downgrade(
        _cfg(output_buffer=output),
        f"{_DEV15_HEAD}:{_PREV}",
        sql=True,
    )
    rendered = output.getvalue()
    assert "DO $migration$" in rendered
    assert "downgrade blocked" in rendered
