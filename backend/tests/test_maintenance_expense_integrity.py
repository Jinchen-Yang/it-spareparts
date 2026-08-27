"""Expense-integrity foundation: real-PostgreSQL coverage for K3-G.

Covers the b6e8d1f3a5c7 migration shape (minimal 2527/260 sample), the signed
basis-aware dual-tax invariant, the exact raw link (RESTRICT), historical
ownership resolution (0/1/>1 candidates, cross-project fail-closed), and the
pure sync helper semantics (create/no-op/change/move).
"""

import hashlib
import io
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import engine
from app.models.maintenance import FProjectExpense
from app.models.maintenance_project import MaintenanceProjectContract
from app.services import maintenance_expense_integrity as ei


_PRE = "a9c4e7b2d6f1"


def _workbook_data_version(project_id: str, revision: int) -> str:
    return hashlib.sha256(f"{project_id}:{revision}".encode("utf-8")).hexdigest()


def test_migration_locks_every_backfill_source_before_mutation():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/b6e8d1f3a5c7_maintenance_expense_integrity.py"
    ).read_text(encoding="utf-8")
    lock_position = migration.index("LOCK TABLE")
    mutation_position = migration.index("op.alter_column(")
    assert lock_position < mutation_position
    lock_section = migration[lock_position:mutation_position]
    lock_statements = (
        "LOCK TABLE maintenance_project_workbook_state",
        "LOCK TABLE maintenance_project, maintenance_project_contract",
        "LOCK TABLE maintenance_project_expense_attribution",
        "LOCK TABLE f_project_expense",
    )
    positions = [lock_section.index(statement) for statement in lock_statements]
    assert positions == sorted(positions)
    assert "IN EXCLUSIVE MODE" in lock_section
    assert "IN ACCESS EXCLUSIVE MODE" in lock_section
    assert "IN SHARE ROW EXCLUSIVE MODE" in lock_section


def test_migration_can_render_offline_release_sql():
    output = io.StringIO()
    cfg = _cfg()
    cfg.output_buffer = output

    alembic_command.upgrade(cfg, f"{_PRE}:b6e8d1f3a5c7", sql=True)

    rendered = output.getvalue()
    assert "LOCK TABLE maintenance_project_workbook_state" in rendered
    assert "sha256(convert_to(" in rendered
    assert "UPDATE maintenance_project_workbook_state" in rendered


def test_contract_metadata_declares_composite_expense_fk_target():
    assert "uq_maintenance_project_contract_project" in {
        constraint.name
        for constraint in MaintenanceProjectContract.__table__.constraints
    }


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic")
    )
    return cfg


def _seed_project(connection, project_id: str, code: str) -> None:
    connection.execute(
        text(
            "INSERT INTO maintenance_project "
            "(project_id, project_code, display_name, lifecycle_status) "
            "VALUES (:pid, :code, :name, 'ongoing')"
        ),
        {"pid": project_id, "code": code, "name": f"费用完整性{code}"},
    )


def _seed_contract(
    connection,
    contract_pk: str,
    project_id: str,
    contract_no: str,
    *,
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO maintenance_project_contract "
            "(project_contract_id, project_id, contract_id, contract_no, "
            "status_mapping_state, status_mapping_version, included_in_total, "
            "effective_from, effective_to, source) VALUES "
            "(:pk, :pid, :cid, :no, 'mapped', 'ei-test-v1', false, "
            ":from_, :to_, 'ei-test')"
        ),
        {
            "pk": contract_pk,
            "pid": project_id,
            "cid": f"contract-{contract_pk}",
            "no": contract_no,
            "from_": effective_from,
            "to_": effective_to,
        },
    )


def _seed_batch(connection, batch_id: int = 910001) -> int:
    connection.execute(
        text(
            "INSERT INTO sys_import_batch (id, filename, file_type, file_hash) "
            "VALUES (:id, 'ei-test.xlsx', 'sales', :hash)"
        ),
        {"id": batch_id, "hash": f"ei-hash-{batch_id}"},
    )
    return batch_id


def _seed_raw(
    connection,
    raw_line_id: str,
    *,
    batch_id: int = 910001,
    linked: str | None = None,
    expense_date: date = date(2026, 3, 1),
    status: str = "已结束",
    tax_basis: str = "default_ex",
    amount: Decimal,
) -> None:
    amount_ex, amount_inc = ei.dual_amounts(amount, tax_basis)
    raw_amount = amount_inc if tax_basis == "inc" else amount_ex
    connection.execute(
        text(
            "INSERT INTO f_project_expense "
            "(raw_line_id, bxd_no, line_no, data_status, expense_date, person, "
            "expense_type, fee_category, reason, linked_sales_order_no, amount, "
            "amount_ex_tax, amount_inc_tax, tax_basis, import_batch_id) VALUES "
            "(:rid, :bxd, 1, :status, :dt, '张三', '差旅费', '交通', '事由', "
            ":linked, :amount, :ex, :inc, :basis, :batch)"
        ),
        {
            "rid": raw_line_id,
            "bxd": f"BXD-{raw_line_id[:60]}",
            "status": status,
            "dt": expense_date,
            "linked": linked,
            "amount": raw_amount,
            "ex": amount_ex,
            "inc": amount_inc,
            "basis": tax_basis,
            "batch": batch_id,
        },
    )


def _seed_legacy_attribution(
    connection,
    expense_id: str,
    project_id: str,
    *,
    expense_ref: str,
    amount_ex: Decimal,
    amount_inc: Decimal,
) -> None:
    """Insert under the pre-integrity (a9c4e7b2d6f1) schema rules."""
    connection.execute(
        text(
            "INSERT INTO maintenance_project_expense_attribution "
            "(expense_id, project_id, expense_ref, expense_date, amount_ex_tax, "
            "amount_inc_tax, tax_rate_used, raw_status, status_mapping_state, "
            "normalized_status, status_mapping_version) VALUES "
            "(:eid, :pid, :ref, DATE '2026-03-01', :ex, :inc, 0.13, '已结束', "
            "'mapped', 'approved', 'backfill-v1')"
        ),
        {"eid": expense_id, "pid": project_id, "ref": expense_ref,
         "ex": amount_ex, "inc": amount_inc},
    )


def _attribution_row(connection, expense_id: str):
    return connection.execute(
        text(
            "SELECT project_id, project_contract_id, raw_expense_line_id, "
            "tax_basis, amount_ex_tax, amount_inc_tax, ownership_mapping_state, "
            "ownership_mapping_version "
            "FROM maintenance_project_expense_attribution WHERE expense_id = :eid"
        ),
        {"eid": expense_id},
    ).one()


def test_migration_links_mirrors_and_resolves_ownership(db):
    """Minimal 2527/260-shaped sample: exact link → mirror → ownership."""
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, _PRE)
    try:
        with engine.begin() as connection:
            _seed_project(connection, "ei-mig-p1", "EI-MIG-P1")
            _seed_contract(
                connection,
                "ei-mig-c1",
                "ei-mig-p1",
                "XSDD-20221008-0165",
                effective_from=date(2026, 3, 2),
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_project_workbook_state "
                    "(project_id, revision, data_version) "
                    "VALUES ('ei-mig-p1', 0, :data_version)"
                ),
                {"data_version": _workbook_data_version("ei-mig-p1", 0)},
            )
            _seed_batch(connection)
            # Unique candidate, same project → mapped.
            # Legacy attribution carries 03-01, while the raw source carries
            # 03-02.  Ownership must use the rebuilt raw date and hit c1.
            _seed_raw(connection, "EI-MIG-R1", linked="20221008-0165",
                      expense_date=date(2026, 3, 2), amount=Decimal("100"))
            # No candidate → unmapped.
            _seed_raw(connection, "EI-MIG-R2", linked="EI-NO-SUCH-CONTRACT",
                      expense_date=date(2026, 3, 2), status="已作废",
                      amount=Decimal("50"))
            # Negative raw mirrored after the unsigned checks are relaxed.
            _seed_raw(connection, "EI-MIG-R3", linked="XSDD-20221008-0165",
                      expense_date=date(2026, 3, 3), amount=Decimal("-100"))
            # inc basis mirrored (raw keeps 1.00 inc / 0.88 ex).
            _seed_raw(connection, "EI-MIG-R4", linked="XSDD-20221008-0165",
                      expense_date=date(2026, 3, 4), amount=Decimal("1.00"),
                      tax_basis="inc")
            _seed_legacy_attribution(
                connection, "bxd:EI-MIG-R1", "ei-mig-p1",
                expense_ref="BXD-EI-MIG-R1#1",
                amount_ex=Decimal("100"), amount_inc=Decimal("113"),
            )
            _seed_legacy_attribution(
                connection, "bxd:EI-MIG-R2", "ei-mig-p1",
                expense_ref="BXD-EI-MIG-R2#1",
                amount_ex=Decimal("50"), amount_inc=Decimal("56.50"),
            )
            # Stale values under the old schema; the mirror must replace them.
            _seed_legacy_attribution(
                connection, "bxd:EI-MIG-R3", "ei-mig-p1",
                expense_ref="BXD-EI-MIG-R3#1",
                amount_ex=Decimal("100"), amount_inc=Decimal("113"),
            )
            _seed_legacy_attribution(
                connection, "bxd:EI-MIG-R4", "ei-mig-p1",
                expense_ref="BXD-EI-MIG-R4#1",
                amount_ex=Decimal("1.00"), amount_inc=Decimal("1.13"),
            )
            _seed_legacy_attribution(
                connection, "manual:EI-MIG-1", "ei-mig-p1",
                expense_ref="MANUAL-EI-MIG-1",
                amount_ex=Decimal("10"), amount_inc=Decimal("11.30"),
            )

        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            r1 = _attribution_row(connection, "bxd:EI-MIG-R1")
            assert tuple(r1) == (
                "ei-mig-p1", "ei-mig-c1", "EI-MIG-R1", "default_ex",
                Decimal("100.00"), Decimal("113.00"), "mapped", "ownership-v1",
            )
            r2 = _attribution_row(connection, "bxd:EI-MIG-R2")
            assert tuple(r2) == (
                "ei-mig-p1", None, "EI-MIG-R2", "default_ex",
                Decimal("50.00"), Decimal("56.50"), "unmapped", "ownership-v1",
            )
            r3 = _attribution_row(connection, "bxd:EI-MIG-R3")
            assert tuple(r3) == (
                "ei-mig-p1", "ei-mig-c1", "EI-MIG-R3", "default_ex",
                Decimal("-100.00"), Decimal("-113.00"), "mapped", "ownership-v1",
            )
            r4 = _attribution_row(connection, "bxd:EI-MIG-R4")
            assert tuple(r4) == (
                "ei-mig-p1", "ei-mig-c1", "EI-MIG-R4", "inc",
                Decimal("0.88"), Decimal("1.00"), "mapped", "ownership-v1",
            )
            standalone = _attribution_row(connection, "manual:EI-MIG-1")
            assert tuple(standalone) == (
                "ei-mig-p1", None, None, "default_ex",
                Decimal("10.00"), Decimal("11.30"), "unmapped", None,
            )
            mirrored = connection.execute(
                text(
                    "SELECT expense_date, applicant, category, expense_reason, "
                    "raw_status, status_mapping_state, normalized_status, "
                    "status_mapping_version, version "
                    "FROM maintenance_project_expense_attribution "
                    "WHERE expense_id = 'bxd:EI-MIG-R2'"
                )
            ).one()
            assert tuple(mirrored) == (
                date(2026, 3, 2),
                "张三",
                "交通",
                "事由",
                "已作废",
                "mapped",
                "void",
                "expense-integrity-v1",
                2,
            )
            assert tuple(connection.execute(
                text(
                    "SELECT revision, data_version "
                    "FROM maintenance_project_workbook_state "
                    "WHERE project_id = 'ei-mig-p1'"
                )
            ).one()) == (
                1,
                _workbook_data_version("ei-mig-p1", 1),
            )
            constraints = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE conrelid = "
                        "'maintenance_project_expense_attribution'::regclass"
                    )
                )
            }
            assert {
                "uq_maintenance_project_expense_raw_line",
                "fk_maintenance_project_expense_raw_line",
                "fk_maintenance_project_expense_contract_project",
                "ck_maintenance_project_expense_tax_basis",
                "ck_maintenance_project_expense_ownership_state",
                "ck_maintenance_project_expense_raw_mapped_contract",
                "ck_maintenance_project_expense_dual_tax_amounts",
            } <= constraints
    finally:
        alembic_command.upgrade(cfg, "head")


def test_migration_fails_closed_on_unique_cross_project_candidate(db):
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, _PRE)
    try:
        with engine.begin() as connection:
            _seed_project(connection, "ei-mig-xp1", "EI-MIG-XP1")
            _seed_project(connection, "ei-mig-xp2", "EI-MIG-XP2")
            _seed_contract(connection, "ei-mig-xc1", "ei-mig-xp1", "XSDD-20990101-0001")
            _seed_batch(connection, 910002)
            _seed_raw(connection, "EI-MIG-XR1", batch_id=910002,
                      linked="20990101-0001", amount=Decimal("10"))
            # The attribution sits on xp2 but its only candidate is xp1's.
            _seed_legacy_attribution(
                connection, "bxd:EI-MIG-XR1", "ei-mig-xp2",
                expense_ref="BXD-EI-MIG-XR1#1",
                amount_ex=Decimal("10"), amount_inc=Decimal("11.30"),
            )
        with pytest.raises(DBAPIError, match="different project"):
            alembic_command.upgrade(cfg, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM maintenance_project_expense_attribution "
                    "WHERE expense_id = 'bxd:EI-MIG-XR1'"
                )
            )
            connection.execute(
                text("DELETE FROM f_project_expense WHERE raw_line_id = 'EI-MIG-XR1'")
            )
        alembic_command.upgrade(cfg, "head")
    finally:
        alembic_command.upgrade(cfg, "head")


def test_expense_id_128_fits_80_char_raw_line(db):
    raw_line_id = "R" * 80
    with engine.begin() as connection:
        _seed_project(connection, "ei-width-p1", "EI-WIDTH-P1")
        _seed_batch(connection, 910003)
        _seed_raw(connection, raw_line_id, batch_id=910003, amount=Decimal("1"))
    expense_id = ei.expense_id_for(raw_line_id)
    assert len(expense_id) == 84
    db.add(
        ei.MaintenanceProjectExpenseAttribution(
            expense_id=expense_id,
            project_id="ei-width-p1",
            raw_expense_line_id=raw_line_id,
            expense_ref="BXD-WIDTH#1",
            expense_date=date(2026, 3, 1),
            tax_basis="default_ex",
            amount_ex_tax=Decimal("1.00"),
            amount_inc_tax=Decimal("1.13"),
            raw_status="已结束",
            status_mapping_state="mapped",
            normalized_status="approved",
            status_mapping_version="ei-test-v1",
            ownership_mapping_state="unmapped",
            ownership_mapping_version="ownership-v1",
            version=1,
        )
    )
    db.flush()
    assert ei.raw_line_id_from_expense_id(expense_id) == raw_line_id


def test_dual_tax_check_is_basis_aware_and_signed(db):
    with engine.begin() as connection:
        _seed_project(connection, "ei-tax-p1", "EI-TAX-P1")

    def _insert(connection, expense_id, basis, ex, inc):
        connection.execute(
            text(
                "INSERT INTO maintenance_project_expense_attribution "
                "(expense_id, project_id, expense_ref, expense_date, tax_basis, "
                "amount_ex_tax, amount_inc_tax, tax_rate_used, raw_status, "
                "status_mapping_state, normalized_status, status_mapping_version, "
                "ownership_mapping_state) VALUES "
                "(:eid, 'ei-tax-p1', :ref, DATE '2026-03-01', :basis, :ex, :inc, "
                "0.13, '已结束', 'mapped', 'approved', 'ei-test-v1', 'unmapped')"
            ),
            {"eid": expense_id, "ref": f"REF-{expense_id}", "basis": basis,
             "ex": ex, "inc": inc},
        )

    # inc basis keeps 1.00 inc / 0.88 ex (not re-derived as 1.13).
    with engine.begin() as connection:
        _insert(connection, "ei-tax-inc-ok", "inc", Decimal("0.88"), Decimal("1.00"))
    # default_ex and negative signed amounts.
    with engine.begin() as connection:
        _insert(connection, "ei-tax-neg-ok", "default_ex",
                Decimal("-100"), Decimal("-113"))
    with engine.begin() as connection:
        _insert(connection, "ei-tax-ex-ok", "ex", Decimal("100"), Decimal("113"))

    for expense_id, basis, ex, inc in (
        ("ei-tax-inc-bad", "inc", Decimal("0.89"), Decimal("1.00")),
        ("ei-tax-ex-bad", "default_ex", Decimal("100"), Decimal("114")),
        ("ei-tax-neg-pair-bad", "default_ex", Decimal("-100"), Decimal("113")),
        ("ei-tax-low-bound", "default_ex", Decimal("-1000000000000"), Decimal("0")),
        ("ei-tax-high-bound", "default_ex", Decimal("1000000000000"), Decimal("0")),
        ("ei-tax-basis-bad", "gross", Decimal("1"), Decimal("1.13")),
    ):
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                _insert(connection, expense_id, basis, ex, inc)


def test_raw_fk_restrict_and_standalone_attribution(db):
    with engine.begin() as connection:
        _seed_project(connection, "ei-fk-p1", "EI-FK-P1")
        _seed_batch(connection, 910004)
        _seed_raw(connection, "EI-FK-R1", batch_id=910004, amount=Decimal("2"))
        connection.execute(
            text(
                "INSERT INTO maintenance_project_expense_attribution "
                "(expense_id, project_id, raw_expense_line_id, expense_ref, "
                "expense_date, tax_basis, amount_ex_tax, amount_inc_tax, "
                "tax_rate_used, raw_status, status_mapping_state, "
                "normalized_status, status_mapping_version, "
                "ownership_mapping_state) VALUES "
                "('bxd:EI-FK-R1', 'ei-fk-p1', 'EI-FK-R1', 'BXD-EI-FK-R1#1', "
                "DATE '2026-03-01', 'default_ex', 2, 2.26, 0.13, '已结束', "
                "'mapped', 'approved', 'ei-test-v1', 'unmapped')"
            )
        )
    # ON DELETE RESTRICT: the raw line cannot disappear under an attribution.
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM f_project_expense WHERE raw_line_id = 'EI-FK-R1'")
            )
    # Standalone attribution: raw FK stays NULL.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO maintenance_project_expense_attribution "
                "(expense_id, project_id, expense_ref, expense_date, tax_basis, "
                "amount_ex_tax, amount_inc_tax, tax_rate_used, raw_status, "
                "status_mapping_state, normalized_status, status_mapping_version, "
                "ownership_mapping_state) VALUES "
                "('manual:EI-FK-1', 'ei-fk-p1', 'MANUAL-EI-FK-1', "
                "DATE '2026-03-01', 'default_ex', 3, 3.39, 0.13, '已结束', "
                "'mapped', 'approved', 'ei-test-v1', 'unmapped')"
            )
        )
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT raw_expense_line_id FROM "
                "maintenance_project_expense_attribution "
                "WHERE expense_id = 'manual:EI-FK-1'"
            )
        ).scalar_one() is None


def test_raw_backed_mapped_requires_contract_and_project_consistency(db):
    with engine.begin() as connection:
        _seed_project(connection, "ei-guard-p1", "EI-GUARD-P1")
        _seed_project(connection, "ei-guard-p2", "EI-GUARD-P2")
        _seed_contract(connection, "ei-guard-c1", "ei-guard-p1", "XSDD-20500101-0007")
        _seed_batch(connection, 910005)
        _seed_raw(connection, "EI-GUARD-R1", batch_id=910005, amount=Decimal("4"))

    insert_sql = (
        "INSERT INTO maintenance_project_expense_attribution "
        "(expense_id, project_id, project_contract_id, raw_expense_line_id, "
        "expense_ref, expense_date, tax_basis, amount_ex_tax, amount_inc_tax, "
        "tax_rate_used, raw_status, status_mapping_state, normalized_status, "
        "status_mapping_version, ownership_mapping_state) VALUES "
        "(:eid, :pid, :cid, :rid, :ref, DATE '2026-03-01', 'default_ex', 4, "
        "4.52, 0.13, '已结束', 'mapped', 'approved', 'ei-test-v1', :ostate)"
    )
    # raw-backed mapped without a contract is rejected.
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(insert_sql),
                {"eid": "bxd:EI-GUARD-R1", "pid": "ei-guard-p1", "cid": None,
                 "rid": "EI-GUARD-R1", "ref": "BXD-EI-GUARD-R1#1",
                 "ostate": "mapped"},
            )
    # composite FK: attribution project must equal the contract's project
    # (deferred FK raises at commit).
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(insert_sql),
                {"eid": "manual:EI-GUARD-X", "pid": "ei-guard-p2",
                 "cid": "ei-guard-c1", "rid": None, "ref": "MANUAL-EI-GUARD-X",
                 "ostate": "unmapped"},
            )
    # mapped with the contract of the same project is accepted.
    with engine.begin() as connection:
        connection.execute(
            text(insert_sql),
            {"eid": "bxd:EI-GUARD-R1", "pid": "ei-guard-p1", "cid": "ei-guard-c1",
             "rid": "EI-GUARD-R1", "ref": "BXD-EI-GUARD-R1#1", "ostate": "mapped"},
        )


def test_normalize_contract_no_and_dual_amounts():
    assert ei.normalize_contract_no(" XSDD-20221008-0165 ") == "20221008-0165"
    assert ei.normalize_contract_no("xsdd-20221008-0165") == "20221008-0165"
    assert ei.normalize_contract_no("XSDD-20221008 0165") == "202210080165"
    assert ei.normalize_contract_no(None) == ""
    assert ei.normalize_contract_no("") == ""

    assert ei.dual_amounts(Decimal("100"), "default_ex") == (
        Decimal("100.00"), Decimal("113.00"))
    assert ei.dual_amounts(Decimal("100"), "ex") == (
        Decimal("100.00"), Decimal("113.00"))
    assert ei.dual_amounts(Decimal("1.00"), "inc") == (
        Decimal("0.88"), Decimal("1.00"))
    assert ei.dual_amounts(Decimal("-100"), "ex") == (
        Decimal("-100.00"), Decimal("-113.00"))
    assert ei.dual_amounts(Decimal("-113"), "inc") == (
        Decimal("-100.00"), Decimal("-113.00"))
    with pytest.raises(ei.ExpenseIntegrityError):
        ei.dual_amounts(Decimal("1"), "gross")
    with pytest.raises(ei.ExpenseIntegrityError):
        ei.dual_amounts(Decimal("1000000000000"), "ex")


def test_map_expense_status_is_approval_axis_only():
    assert ei.map_expense_status("已结束") == ("mapped", "approved")
    assert ei.map_expense_status("已作废") == ("mapped", "void")
    assert ei.map_expense_status("作废") == ("mapped", "void")
    assert ei.map_expense_status("审批中") == ("unmapped", "unknown")
    assert ei.map_expense_status(None) == ("unmapped", "unknown")


def test_resolve_historical_ownership_candidate_counts(db):
    with engine.begin() as connection:
        _seed_project(connection, "ei-own-p1", "EI-OWN-P1")
        _seed_project(connection, "ei-own-p2", "EI-OWN-P2")
        _seed_contract(connection, "ei-own-c1", "ei-own-p1", "XSDD-20221008-0165")
        # Overlapping window, same normalized number → second candidate.
        _seed_contract(
            connection, "ei-own-c2", "ei-own-p1", "20221008-0165",
            effective_from=date(2026, 2, 1), effective_to=date(2026, 4, 1),
        )
        # Same number, other project → unique cross-project candidate once the
        # two p1 contracts are out of window.
        _seed_contract(
            connection, "ei-own-c3", "ei-own-p2", "xsdd-20221008-0165",
            effective_from=date(2026, 4, 1), effective_to=None,
        )

    # 0 candidates → unmapped.
    resolution = ei.resolve_historical_ownership(
        db, project_id="ei-own-p1", linked_sales_order_no="NO-SUCH",
        expense_date=date(2026, 3, 1))
    assert resolution.state == "unmapped"
    assert resolution.project_contract_id is None

    # >1 candidate → ambiguous, never guessed.
    resolution = ei.resolve_historical_ownership(
        db, project_id="ei-own-p1", linked_sales_order_no="20221008-0165",
        expense_date=date(2026, 3, 1))
    assert resolution.state == "ambiguous"
    assert {c.project_contract_id for c in resolution.candidates} == {
        "ei-own-c1", "ei-own-c2"}

    # effective_to is exclusive: at 2026-04-01 only c1 and c3 remain windows…
    # c1 (open-ended) and c3 both match → ambiguous.
    resolution = ei.resolve_historical_ownership(
        db, project_id="ei-own-p1", linked_sales_order_no="20221008-0165",
        expense_date=date(2026, 4, 1))
    assert resolution.state == "ambiguous"

    # Exactly one candidate on the same project → mapped.
    resolution = ei.resolve_historical_ownership(
        db, project_id="ei-own-p1", linked_sales_order_no="XSDD-20221008-0165",
        expense_date=date(2026, 1, 15))
    assert resolution.state == "mapped"
    assert resolution.project_contract_id == "ei-own-c1"

    # Exactly one candidate owned by another project → fail closed.
    with pytest.raises(ei.OwnershipConflictError):
        ei.resolve_historical_ownership(
            db, project_id="ei-own-p2", linked_sales_order_no="XSDD-20221008-0165",
            expense_date=date(2026, 1, 15))


def _orm_raw(db, raw_line_id: str) -> FProjectExpense:
    return db.scalar(
        select(FProjectExpense).where(FProjectExpense.raw_line_id == raw_line_id)
    )


def test_sync_attribution_create_noop_change_and_move(db):
    with engine.begin() as connection:
        _seed_project(connection, "ei-sync-p1", "EI-SYNC-P1")
        _seed_project(connection, "ei-sync-p2", "EI-SYNC-P2")
        _seed_contract(connection, "ei-sync-c1", "ei-sync-p1", "XSDD-20221008-0165")
        _seed_contract(connection, "ei-sync-c2", "ei-sync-p2", "XSDD-20990202-0002")
        _seed_batch(connection, 910006)
        _seed_raw(connection, "EI-SYNC-R1", batch_id=910006,
                  linked="XSDD-20221008-0165", amount=Decimal("100"))

    raw = _orm_raw(db, "EI-SYNC-R1")

    # create
    result = ei.sync_attribution_from_raw(
        db, raw=raw, project_id="ei-sync-p1", status_mapping_version="ei-sync-v1")
    assert result.created is True
    assert result.affected_project_ids == {"ei-sync-p1"}
    attribution = db.get(
        ei.MaintenanceProjectExpenseAttribution, "bxd:EI-SYNC-R1")
    assert attribution.ownership_mapping_state == "mapped"
    assert attribution.project_contract_id == "ei-sync-c1"
    assert attribution.raw_expense_line_id == "EI-SYNC-R1"
    assert attribution.normalized_status == "approved"
    assert attribution.version == 1

    # no-op: same facts → no version bump, empty affected set.
    result = ei.sync_attribution_from_raw(
        db, raw=raw, project_id="ei-sync-p1", status_mapping_version="ei-sync-v1")
    assert result.changed is False
    assert result.changed_fields == {}
    assert result.affected_project_ids == set()
    assert attribution.version == 1

    # amount change → semantic diff + version bump.
    raw.amount = Decimal("200")
    raw.amount_ex_tax = Decimal("200")
    raw.amount_inc_tax = Decimal("226")
    result = ei.sync_attribution_from_raw(
        db, raw=raw, project_id="ei-sync-p1", status_mapping_version="ei-sync-v1")
    assert set(result.changed_fields) == {"amount_ex_tax", "amount_inc_tax"}
    assert attribution.version == 2

    # approval-axis change leaves ownership untouched.
    raw.data_status = "已作废"
    result = ei.sync_attribution_from_raw(
        db, raw=raw, project_id="ei-sync-p1", status_mapping_version="ei-sync-v1")
    assert set(result.changed_fields) == {"raw_status", "normalized_status"}
    assert attribution.normalized_status == "void"
    assert attribution.ownership_mapping_state == "mapped"
    assert attribution.project_contract_id == "ei-sync-c1"
    assert attribution.version == 3

    # move: the raw line now points at p2's contract → mapped move, both
    # projects reported.
    raw.linked_sales_order_no = "20990202-0002"
    result = ei.sync_attribution_from_raw(
        db, raw=raw, project_id="ei-sync-p2", status_mapping_version="ei-sync-v1")
    assert result.affected_project_ids == {"ei-sync-p1", "ei-sync-p2"}
    assert attribution.project_id == "ei-sync-p2"
    assert attribution.project_contract_id == "ei-sync-c2"
    assert attribution.ownership_mapping_state == "mapped"
    assert attribution.version == 4

    # unique candidate of another project fails closed and writes nothing.
    raw.linked_sales_order_no = "XSDD-20221008-0165"
    with pytest.raises(ei.OwnershipConflictError):
        ei.sync_attribution_from_raw(
            db, raw=raw, project_id="ei-sync-p2",
            status_mapping_version="ei-sync-v1")
