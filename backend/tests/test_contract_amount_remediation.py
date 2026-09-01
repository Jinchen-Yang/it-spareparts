"""Safety contract for the forward-only f7a contract amount remediation."""

from __future__ import annotations

import copy
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal, engine
from app.services import maintenance_ledger
from scripts import remediate_contract_amount_inc_tax as remediation


def _alembic_cfg() -> AlembicConfig:
    backend_dir = Path(__file__).resolve().parents[1]
    cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    return cfg


def _seed_contract(
    db,
    *,
    contract_id: str = "remediation-contract-1",
    contract_no: str = "XS-REMEDIATION-1",
    amount_ex_tax: str = "100.00",
    amount_inc_tax: str | None = "113.00",
    version: int = 1,
) -> None:
    db.execute(text(
        "INSERT INTO sys_user (username, password_hash, role, is_active) "
        "VALUES ('remediation-operator', 'not-a-login-secret', 'admin', true)"
    ))
    db.execute(text(
        "INSERT INTO maintenance_project "
        "(project_id, project_code, display_name, lifecycle_status) VALUES "
        "('remediation-project-1', 'REMEDIATION-PROJECT-1', "
        "'合同含税额修复测试', 'ongoing')"
    ))
    db.execute(text(
        "INSERT INTO maintenance_project_contract "
        "(project_contract_id, project_id, contract_id, contract_no, "
        "contract_amount, amount_inc_tax, status_mapping_state, "
        "status_mapping_version, included_in_total, effective_from, source, "
        "version) VALUES "
        "(:contract_id, 'remediation-project-1', :contract_no, :contract_no, "
        "CAST(:amount_ex_tax AS NUMERIC(14, 2)), "
        "CAST(:amount_inc_tax AS NUMERIC(14, 2)), 'mapped', "
        "'remediation-test-v1', true, DATE '2026-01-01', "
        "'project_manager_xls_v1', :version)"
    ), {
        "contract_id": contract_id,
        "contract_no": contract_no,
        "amount_ex_tax": amount_ex_tax,
        "amount_inc_tax": amount_inc_tax,
        "version": version,
    })
    db.commit()


def _seed_sales(
    db,
    *,
    raw_order_id: str = "raw-remediation-sales-1",
    contract_no: str = "XS-REMEDIATION-1",
    amount_ex_tax: str = "100.00",
    tax_rate: str | None = "0.0600",
) -> None:
    batch_id = db.execute(text(
        "INSERT INTO sys_import_batch "
        "(filename, file_type, file_hash, status) VALUES "
        "(:filename, 'sales', :file_hash, 'success') RETURNING id"
    ), {
        "filename": f"{raw_order_id}.xlsx",
        "file_hash": (raw_order_id.encode().hex() + "0" * 64)[:64],
    }).scalar_one()
    db.execute(text(
        "INSERT INTO f_sales_order "
        "(raw_order_id, order_no, amount_ex_tax, tax_rate, data_status, "
        "import_batch_id) VALUES "
        "(:raw_order_id, :contract_no, CAST(:amount_ex_tax AS NUMERIC(14, 2)), "
        "CAST(:tax_rate AS NUMERIC(5, 4)), :active_status, :batch_id)"
    ), {
        "raw_order_id": raw_order_id,
        "contract_no": contract_no,
        "amount_ex_tax": amount_ex_tax,
        "tax_rate": tax_rate,
        "active_status": remediation.ACTIVE_SALES_STATUS,
        "batch_id": batch_id,
    })
    db.commit()


def _seed_ledger(
    db,
    *,
    batch_id: str = "ledger-remediation-batch-1",
    row_id: str = "ledger-remediation-row-1",
    contract_no: str = "XS-REMEDIATION-1",
    amount_inc_tax: str = "106.00",
) -> None:
    db.execute(text(
        "INSERT INTO maintenance_ledger_import_batch "
        "(batch_id, file_hash, filename, idempotency_key, source_kind, "
        "uploaded_by, status, applied_by, applied_at) VALUES "
        "(:batch_id, :file_hash, 'ledger-remediation.xlsx', :idempotency_key, "
        "'project_manager_xls_v1', 'remediation-operator', 'applied', "
        "'remediation-operator', TIMESTAMPTZ '2026-08-26 12:00:00+08')"
    ), {
        "batch_id": batch_id,
        "file_hash": "a" * 64,
        "idempotency_key": f"remediation:{batch_id}",
    })
    db.execute(text(
        "INSERT INTO maintenance_ledger_contract_row "
        "(row_id, batch_id, row_no, order_no, amount_inc_tax) VALUES "
        "(:row_id, :batch_id, 1, :contract_no, "
        "CAST(:amount_inc_tax AS NUMERIC(14, 2)))"
    ), {
        "row_id": row_id,
        "batch_id": batch_id,
        "contract_no": contract_no,
        "amount_inc_tax": amount_inc_tax,
    })
    db.commit()


def _source_snapshot(
    *,
    amount_ex_tax: str = "100.00",
    version: int = 1,
) -> dict:
    amount = Decimal(amount_ex_tax)
    return remediation.make_f7_source_snapshot(
        backup_sha256="b" * 64,
        rows=[{
            "project_contract_id": "remediation-contract-1",
            "project_id": "remediation-project-1",
            "contract_no": "XS-REMEDIATION-1",
            "pre_f7_version": version,
            "pre_f7_contract_amount": format(amount, ".2f"),
            "pre_f7_amount_inc_tax": None,
            "f7_write_kind": "default_13_percent",
            "f7_tax_rate": "0.13",
            "f7_sales_row_id": None,
            "f7_sales_amount_ex_tax": None,
            "f7_written_amount_inc_tax": format(
                (amount * Decimal("1.13")).quantize(Decimal("0.01")),
                ".2f",
            ),
        }],
    )


def _bind(manifest: dict, *, source_snapshot: dict | None = None) -> dict:
    return remediation.bind_apply_manifest_to_f7_source(
        manifest,
        source_snapshot=source_snapshot or _source_snapshot(),
    )


def _sales_manifest(
    *,
    expected_version: int = 1,
    expected_current: str | None = "113.00",
    source_snapshot: dict | None = None,
) -> dict:
    return _bind({
        "schema_version": 1,
        "mode": "apply",
        "reason": "INC-123：逐行核验销售单明确税率",
        "rows": [{
            "project_contract_id": "remediation-contract-1",
            "expected_version": expected_version,
            "expected_current_amount_inc_tax": expected_current,
            "evidence": {
                "kind": "sales_explicit_tax",
                "raw_order_id": "raw-remediation-sales-1",
                "expected_amount_ex_tax": "100.00",
                "expected_tax_rate": "0.0600",
            },
        }],
    }, source_snapshot=source_snapshot)


def _incomplete_manifest(
    *,
    expected_current: str = "113.00",
    source_snapshot: dict | None = None,
) -> dict:
    return _bind({
        "schema_version": 1,
        "mode": "apply",
        "reason": "INC-123：无权威台账值且无明确销售税率",
        "rows": [{
            "project_contract_id": "remediation-contract-1",
            "expected_version": 1,
            "expected_current_amount_inc_tax": expected_current,
            "evidence": {
                "kind": "no_authoritative_evidence",
                "note": "来源缺失，恢复 NULL 并保持 incomplete",
            },
        }],
    }, source_snapshot=source_snapshot)


def _execute(
    db,
    manifest: dict,
    *,
    source_snapshot: dict | None = None,
) -> dict:
    digest = remediation.manifest_sha256(
        remediation.normalize_manifest(manifest)
    )
    return remediation.run_remediation(
        db,
        manifest=manifest,
        execute=True,
        operator="remediation-operator",
        confirm_manifest_sha256=digest,
        source_snapshot=source_snapshot or _source_snapshot(),
    )


def test_default_dry_run_is_read_only_and_reports_exact_sales_target(db):
    _seed_contract(db)
    _seed_sales(db)

    result = remediation.run_remediation(
        db, manifest=_sales_manifest(), source_snapshot=_source_snapshot())

    assert result["dry_run"] is True
    assert result["status"] == "ready"
    assert result["changes"] == [{
        "project_contract_id": "remediation-contract-1",
        "project_id": "remediation-project-1",
        "contract_no": "XS-REMEDIATION-1",
        "before_amount_inc_tax": "113.00",
        "after_amount_inc_tax": "106.00",
        "before_version": 1,
        "after_version": 2,
        "target_state": "authoritative",
        "evidence_kind": "sales_explicit_tax",
        "evidence_ref": "sales:raw-remediation-sales-1",
    }]
    row = db.execute(text(
        "SELECT amount_inc_tax, version FROM maintenance_project_contract "
        "WHERE project_contract_id = 'remediation-contract-1'"
    )).one()
    assert row == (Decimal("113.00"), 1)
    assert db.scalar(text(
        "SELECT count(*) FROM maintenance_contract_amount_remediation_run"
    )) == 0


def test_execute_and_generated_rollback_are_guarded_and_audited(db):
    _seed_contract(db)
    _seed_sales(db)
    manifest = _sales_manifest()
    dry_run = remediation.run_remediation(
        db, manifest=manifest, source_snapshot=_source_snapshot())

    applied = remediation.run_remediation(
        db,
        manifest=manifest,
        execute=True,
        operator="remediation-operator",
        confirm_manifest_sha256=dry_run["manifest_sha256"],
        source_snapshot=_source_snapshot(),
    )
    assert applied["status"] == "applied"
    assert applied["dry_run"] is False
    assert applied["rollback_manifest_sha256"] == remediation.manifest_sha256(
        applied["rollback_manifest"]
    )
    assert applied["workbook_revision_changes"] == [{
        "project_id": "remediation-project-1",
        "before_revision": 0,
        "after_revision": 1,
        "after_data_version": remediation._workbook_data_version(
            "remediation-project-1", 1),
    }]
    row = db.execute(text(
        "SELECT amount_inc_tax, version FROM maintenance_project_contract "
        "WHERE project_contract_id = 'remediation-contract-1'"
    )).one()
    assert row == (Decimal("106.00"), 2)
    db.rollback()

    rolled_back = remediation.run_remediation(
        db,
        manifest=applied["rollback_manifest"],
        execute=True,
        operator="remediation-operator",
        confirm_manifest_sha256=applied["rollback_manifest_sha256"],
    )
    assert rolled_back["mode"] == "rollback"
    row = db.execute(text(
        "SELECT amount_inc_tax, version FROM maintenance_project_contract "
        "WHERE project_contract_id = 'remediation-contract-1'"
    )).one()
    assert row == (Decimal("113.00"), 3)
    assert db.scalar(text(
        "SELECT count(*) FROM maintenance_contract_amount_remediation_run"
    )) == 2
    assert db.scalar(text(
        "SELECT count(*) FROM maintenance_contract_amount_remediation_entry"
    )) == 2
    audits = db.execute(text(
        "SELECT action, before_json, after_json "
        "FROM maintenance_project_audit_log ORDER BY id"
    )).all()
    assert [row.action for row in audits] == [
        "remediate_contract_amount",
        "rollback_contract_amount",
    ]
    assert audits[0].before_json["amount_inc_tax"] == "113.00"
    assert audits[0].after_json["amount_inc_tax"] == "106.00"
    assert audits[1].after_json["amount_inc_tax"] == "113.00"
    rollback_state = db.scalar(text(
        "SELECT e.target_state "
        "FROM maintenance_contract_amount_remediation_entry e "
        "JOIN maintenance_contract_amount_remediation_run r "
        "ON r.run_id = e.run_id WHERE r.mode = 'rollback'"
    ))
    assert rollback_state == "restored"
    workbook_state = db.execute(text(
        "SELECT revision, data_version "
        "FROM maintenance_project_workbook_state "
        "WHERE project_id = 'remediation-project-1'"
    )).one()
    assert workbook_state.revision == 2
    assert workbook_state.data_version == remediation._workbook_data_version(
        "remediation-project-1", 2)


@pytest.mark.parametrize(
    ("new_version", "new_current"),
    [(2, "113.00"), (1, "112.99")],
)
def test_execute_refuses_stale_version_or_expected_current_atomically(
    db,
    new_version,
    new_current,
):
    _seed_contract(db)
    _seed_sales(db)
    manifest = _sales_manifest()
    db.execute(text(
        "UPDATE maintenance_project_contract "
        "SET version = :version, amount_inc_tax = CAST(:amount AS NUMERIC(14, 2)) "
        "WHERE project_contract_id = 'remediation-contract-1'"
    ), {"version": new_version, "amount": new_current})
    db.commit()

    with pytest.raises(remediation.RemediationError, match="manifest|版本|当前含税额"):
        _execute(db, manifest)

    row = db.execute(text(
        "SELECT amount_inc_tax, version FROM maintenance_project_contract "
        "WHERE project_contract_id = 'remediation-contract-1'"
    )).one()
    assert row == (Decimal(new_current), new_version)
    assert db.scalar(text(
        "SELECT count(*) FROM maintenance_contract_amount_remediation_run"
    )) == 0
    assert db.scalar(text(
        "SELECT count(*) FROM maintenance_project_audit_log"
    )) == 0


def test_no_authoritative_source_clears_guess_and_marks_incomplete(db):
    _seed_contract(db)

    result = _execute(db, _incomplete_manifest())

    assert result["changes"][0]["after_amount_inc_tax"] is None
    assert result["changes"][0]["target_state"] == "incomplete"
    row = db.execute(text(
        "SELECT amount_inc_tax, version FROM maintenance_project_contract "
        "WHERE project_contract_id = 'remediation-contract-1'"
    )).one()
    assert row == (None, 2)
    receipt = db.execute(text(
        "SELECT target_state, evidence_kind, evidence_snapshot "
        "FROM maintenance_contract_amount_remediation_entry"
    )).one()
    assert receipt.target_state == "incomplete"
    assert receipt.evidence_kind == "no_authoritative_evidence"
    assert receipt.evidence_snapshot["completeness_state"] == "incomplete"


def test_incomplete_is_refused_when_explicit_sales_evidence_is_unambiguous(db):
    _seed_contract(db)
    _seed_sales(db)

    with pytest.raises(remediation.RemediationError) as caught:
        remediation.run_remediation(
            db,
            manifest=_incomplete_manifest(),
            source_snapshot=_source_snapshot(),
        )

    assert caught.value.code == "authoritative_evidence_exists"
    row = db.execute(text(
        "SELECT amount_inc_tax, version FROM maintenance_project_contract "
        "WHERE project_contract_id = 'remediation-contract-1'"
    )).one()
    assert row == (Decimal("113.00"), 1)


def test_unresolved_active_sales_row_blocks_sales_write_and_allows_incomplete(db):
    _seed_contract(db)
    _seed_sales(db)
    _seed_sales(
        db,
        raw_order_id="raw-remediation-sales-unknown-tax",
        tax_rate=None,
    )

    with pytest.raises(remediation.RemediationError) as caught:
        remediation.run_remediation(
            db,
            manifest=_sales_manifest(),
            source_snapshot=_source_snapshot(),
        )
    assert caught.value.code == "ambiguous_sales_evidence"

    result = remediation.run_remediation(
        db,
        manifest=_incomplete_manifest(),
        source_snapshot=_source_snapshot(),
    )
    assert result["changes"][0]["target_state"] == "incomplete"
    assert result["changes"][0]["after_amount_inc_tax"] is None


def test_sales_write_fails_closed_when_canonical_ex_tax_amount_conflicts(db):
    _seed_contract(
        db,
        amount_ex_tax="200.00",
        amount_inc_tax="226.00",
    )
    _seed_sales(db, amount_ex_tax="100.00", tax_rate="0.0600")
    source = _source_snapshot(amount_ex_tax="200.00")
    manifest = _sales_manifest(
        expected_current="226.00", source_snapshot=source)

    with pytest.raises(remediation.RemediationError) as caught:
        remediation.run_remediation(
            db, manifest=manifest, source_snapshot=source)
    assert caught.value.code == "contract_sales_amount_conflict"

    incomplete = remediation.run_remediation(
        db,
        manifest=_incomplete_manifest(
            expected_current="226.00", source_snapshot=source),
        source_snapshot=source,
    )
    assert incomplete["changes"][0]["target_state"] == "incomplete"


def test_ledger_amount_must_match_latest_applied_row_exactly(db):
    _seed_contract(db)
    _seed_ledger(db)
    manifest = _bind({
        "schema_version": 1,
        "mode": "apply",
        "reason": "INC-123：以最新已应用台账原值修复",
        "rows": [{
            "project_contract_id": "remediation-contract-1",
            "expected_version": 1,
            "expected_current_amount_inc_tax": "113.00",
            "evidence": {
                "kind": "ledger_amount_inc_tax",
                "batch_id": "ledger-remediation-batch-1",
                "row_id": "ledger-remediation-row-1",
                "expected_amount_inc_tax": "106.00",
            },
        }],
    })

    result = remediation.run_remediation(
        db, manifest=manifest, source_snapshot=_source_snapshot())

    assert result["changes"][0]["after_amount_inc_tax"] == "106.00"
    manifest["rows"][0]["evidence"]["expected_amount_inc_tax"] = "107.00"
    with pytest.raises(remediation.RemediationError) as caught:
        remediation.run_remediation(
            db, manifest=manifest, source_snapshot=_source_snapshot())
    assert caught.value.code == "ambiguous_ledger_evidence"


def test_manifest_rejects_float_money_and_duplicate_json_keys(tmp_path):
    manifest = _sales_manifest()
    manifest["rows"][0]["expected_current_amount_inc_tax"] = 113.0
    with pytest.raises(remediation.RemediationError) as caught:
        remediation.normalize_manifest(manifest)
    assert caught.value.code == "invalid_manifest"

    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1,"mode":"apply",'
        '"reason":"x","rows":[]}',
        encoding="utf-8",
    )
    with pytest.raises(remediation.RemediationError) as caught:
        remediation.load_manifest(path)
    assert caught.value.code == "duplicate_json_key"


def test_remediation_receipts_are_database_append_only(db):
    _seed_contract(db)
    _execute(db, _incomplete_manifest())
    run_id = db.scalar(text(
        "SELECT run_id FROM maintenance_contract_amount_remediation_run"
    ))
    db.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE maintenance_contract_amount_remediation_run "
                "SET reason = 'rewritten' WHERE run_id = :run_id"
            ), {"run_id": run_id})
    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.execute(text(
                "DELETE FROM maintenance_contract_amount_remediation_entry "
                "WHERE run_id = :run_id"
            ), {"run_id": run_id})
    with engine.connect() as connection:
        assert connection.scalar(text(
            "SELECT count(*) FROM maintenance_contract_amount_remediation_entry "
            "WHERE run_id = :run_id"
        ), {"run_id": run_id}) == 1


def test_commit_ack_loss_is_unknown_not_reported_as_rolled_back(db):
    _seed_contract(db)
    _seed_sales(db)
    manifest = _sales_manifest()
    digest = remediation.manifest_sha256(
        remediation.normalize_manifest(manifest)
    )

    class CommitAckLostSession:
        def __init__(self, inner):
            self.inner = inner
            self.rollback_calls = 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def commit(self):
            self.inner.commit()
            raise ConnectionError("synthetic commit acknowledgement loss")

        def rollback(self):
            self.rollback_calls += 1
            return self.inner.rollback()

    wrapped = CommitAckLostSession(db)
    with pytest.raises(remediation.CommitOutcomeUnknown) as caught:
        remediation.run_remediation(
            wrapped,
            manifest=manifest,
            execute=True,
            operator="remediation-operator",
            confirm_manifest_sha256=digest,
            source_snapshot=_source_snapshot(),
        )

    assert caught.value.manifest_hash == digest
    assert wrapped.rollback_calls == 0
    with engine.connect() as connection:
        stored = connection.execute(text(
            "SELECT run_id, manifest_sha256 "
            "FROM maintenance_contract_amount_remediation_run "
            "WHERE manifest_sha256 = :digest"
        ), {"digest": digest}).one()
        contract = connection.execute(text(
            "SELECT amount_inc_tax, version FROM maintenance_project_contract "
            "WHERE project_contract_id = 'remediation-contract-1'"
        )).one()
    assert stored.run_id == caught.value.run_id
    assert stored.manifest_sha256 == digest
    assert contract == (Decimal("106.00"), 2)
    with SessionLocal() as reconciliation_db:
        reconciliation_db.execute(text(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        receipt = remediation.reconcile_manifest_run(
            reconciliation_db,
            manifest_hash=digest,
        )
        reconciliation_db.rollback()
    assert receipt["status"] == "applied"
    assert receipt["run_id"] == caught.value.run_id
    assert receipt["source_algorithm_sha256"] == remediation.F7_ALGORITHM_SHA256
    assert receipt["f7_affected_set_sha256"] == _source_snapshot()[
        "affected_set_sha256"
    ]
    assert receipt["partition"]["preserved_set_sha256"] == manifest[
        "partition"
    ]["preserved_set_sha256"]
    assert receipt["partition"]["changed_set_sha256"] == manifest[
        "partition"
    ]["changed_set_sha256"]
    assert receipt["rollback_manifest_sha256"] == remediation.manifest_sha256(
        receipt["rollback_manifest"])


def test_withdrawn_f7_revision_cannot_guess_tax_on_fresh_install():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/f7a3d2c8e6b1_contract_total_inc_tax_backfill.py"
    ).read_text(encoding="utf-8")

    assert "1.13" not in migration
    assert "0.13" not in migration
    assert "UPDATE maintenance_project_contract" not in migration
    assert "coalesce(tax_rate" not in migration


def test_source_export_replays_old_f7_same_contract_number_sibling_exactly(db):
    db.rollback()
    cfg = _alembic_cfg()
    alembic_command.downgrade(cfg, remediation.F7_PREDECESSOR_REVISION)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO maintenance_project "
                "(project_id, project_code, display_name, lifecycle_status) VALUES "
                "('f7-source-project', 'F7-SOURCE', 'f7 source snapshot', 'ongoing')"
            ))
            connection.execute(text(
                "INSERT INTO maintenance_project_contract "
                "(project_contract_id, project_id, contract_id, contract_no, "
                "contract_amount, amount_inc_tax, status_mapping_state, "
                "status_mapping_version, included_in_total, effective_from, "
                "source, version) VALUES "
                "('f7-source-with-ex', 'f7-source-project', 'F7-A', 'XS-SAME', "
                "100.00, NULL, 'mapped', 'test-v1', true, DATE '2026-01-01', "
                "'test', 1), "
                "('f7-source-null-ex', 'f7-source-project', 'F7-B', 'XS-SAME', "
                "NULL, NULL, 'mapped', 'test-v1', true, DATE '2026-02-01', "
                "'test', 1)"
            ))
            batch_id = connection.scalar(text(
                "INSERT INTO sys_import_batch "
                "(filename, file_type, file_hash, status) VALUES "
                "('f7-source-sales.xlsx', 'sales', :file_hash, 'success') "
                "RETURNING id"
            ), {"file_hash": "e" * 64})
            connection.execute(text(
                "INSERT INTO f_sales_order "
                "(raw_order_id, order_no, amount_ex_tax, tax_rate, data_status, "
                "import_batch_id) VALUES "
                "('f7-source-sales-row', 'XS-SAME', 100.00, 0.0600, "
                ":active_status, :batch_id)"
            ), {
                "active_status": remediation.ACTIVE_SALES_STATUS,
                "batch_id": batch_id,
            })
        with SessionLocal() as source_db:
            snapshot = remediation.export_f7_source_snapshot(
                source_db,
                backup_sha256="f" * 64,
            )
            source_db.rollback()
        assert snapshot["affected_count"] == 2
        by_id = {
            row["project_contract_id"]: row for row in snapshot["rows"]
        }
        assert by_id["f7-source-with-ex"]["f7_written_amount_inc_tax"] == "106.00"
        sibling = by_id["f7-source-null-ex"]
        assert sibling["pre_f7_contract_amount"] is None
        assert sibling["f7_sales_amount_ex_tax"] == "100.00"
        assert sibling["f7_written_amount_inc_tax"] == "106.00"

        # The rewritten migration body is intentionally empty: upgrading the
        # restored database records f7/a9 but does not replay either old write.
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            amounts = connection.execute(text(
                "SELECT project_contract_id, amount_inc_tax "
                "FROM maintenance_project_contract "
                "WHERE project_id = 'f7-source-project' "
                "ORDER BY project_contract_id"
            )).all()
        assert amounts == [
            ("f7-source-null-ex", None),
            ("f7-source-with-ex", None),
        ]
    finally:
        alembic_command.upgrade(cfg, "head")


def test_a9_downgrade_refuses_to_erase_nonempty_remediation_receipt(db):
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/a9c4e7b2d6f1_contract_amount_remediation_guard.py"
    ).read_text(encoding="utf-8")
    assert "SET LOCAL lock_timeout = '5s'" in migration
    assert "IN ACCESS EXCLUSIVE MODE" in migration

    _seed_contract(db)
    _execute(db, _incomplete_manifest())
    db.rollback()

    # a9 is no longer necessarily the Alembic head.  The protected downgrade
    # may first traverse later transactional revisions; when a9 refuses to
    # erase its append-only receipt, PostgreSQL rolls that whole migration
    # transaction back.  The observable invariant is therefore "head remains
    # exactly where it started", not "version becomes a9".
    with engine.connect() as connection:
        original_revision = connection.scalar(text(
            "SELECT version_num FROM alembic_version"
        ))

    with pytest.raises(DBAPIError, match="downgrade refused"):
        alembic_command.downgrade(_alembic_cfg(), remediation.F7_REVISION)

    with engine.connect() as connection:
        assert connection.scalar(text(
            "SELECT version_num FROM alembic_version"
        )) == original_revision
        assert connection.scalar(text(
            "SELECT count(*) FROM maintenance_contract_amount_remediation_run"
        )) == 1


def test_cli_defaults_to_dry_run_and_execute_requires_hash_confirmation():
    parsed = remediation._parser().parse_args(["--manifest", "repair.json"])
    assert parsed.execute is False
    payload = _sales_manifest()

    class ConfirmationOnlySession:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("database must not be touched before confirmation")

        def rollback(self):
            raise ConnectionError("synthetic rollback cleanup failure")

    with pytest.raises(remediation.RemediationError) as caught:
        remediation.run_remediation(
            ConfirmationOnlySession(),
            manifest=payload,
            execute=True,
            operator="remediation-operator",
            confirm_manifest_sha256="0" * 64,
            source_snapshot=_source_snapshot(),
        )
    assert caught.value.code == "manifest_confirmation_mismatch"


def test_reproducible_builder_partitions_preserved_and_cleared_then_executes(db):
    _seed_contract(db, amount_inc_tax="106.00")
    _seed_sales(db)
    db.execute(text(
        "INSERT INTO maintenance_project_contract "
        "(project_contract_id, project_id, contract_id, contract_no, "
        "contract_amount, amount_inc_tax, status_mapping_state, "
        "status_mapping_version, included_in_total, effective_from, source, version) "
        "VALUES ('remediation-contract-2', 'remediation-project-1', "
        "'XS-REMEDIATION-2', 'XS-REMEDIATION-2', 200.00, 226.00, 'mapped', "
        "'remediation-test-v1', true, DATE '2026-01-01', "
        "'project_manager_xls_v1', 1)"
    ))
    db.commit()
    source = remediation.make_f7_source_snapshot(
        backup_sha256="c" * 64,
        rows=[
            {
                "project_contract_id": "remediation-contract-1",
                "project_id": "remediation-project-1",
                "contract_no": "XS-REMEDIATION-1",
                "pre_f7_version": 1,
                "pre_f7_contract_amount": "100.00",
                "pre_f7_amount_inc_tax": None,
                "f7_write_kind": "default_13_percent",
                "f7_tax_rate": "0.13",
                "f7_sales_row_id": None,
                "f7_sales_amount_ex_tax": None,
                "f7_written_amount_inc_tax": "113.00",
            },
            {
                "project_contract_id": "remediation-contract-2",
                "project_id": "remediation-project-1",
                "contract_no": "XS-REMEDIATION-2",
                "pre_f7_version": 1,
                "pre_f7_contract_amount": "200.00",
                "pre_f7_amount_inc_tax": None,
                "f7_write_kind": "default_13_percent",
                "f7_tax_rate": "0.13",
                "f7_sales_row_id": None,
                "f7_sales_amount_ex_tax": None,
                "f7_written_amount_inc_tax": "226.00",
            },
        ],
    )

    manifest = remediation.build_apply_manifest(
        db,
        source_snapshot=source,
        reason="INC-123：pre-f7 备份与当前事实双人复核",
    )
    db.rollback()

    assert manifest["partition"] == {
        "affected_count": 2,
        "preserved_count": 1,
        "authoritative_corrected_count": 0,
        "cleared_count": 1,
        "changed_count": 1,
        "preserved_set_sha256": manifest["partition"]["preserved_set_sha256"],
        "changed_set_sha256": manifest["partition"]["changed_set_sha256"],
    }
    dry_run = remediation.run_remediation(
        db,
        manifest=manifest,
        source_snapshot=source,
    )
    assert dry_run["partition"]["affected_count"] == 2
    assert [row["project_contract_id"] for row in dry_run["preserved_rows"]] == [
        "remediation-contract-1"
    ]
    applied = remediation.run_remediation(
        db,
        manifest=manifest,
        source_snapshot=source,
        execute=True,
        operator="remediation-operator",
        confirm_manifest_sha256=dry_run["manifest_sha256"],
    )
    assert applied["status"] == "applied"
    rows = db.execute(text(
        "SELECT project_contract_id, amount_inc_tax, version "
        "FROM maintenance_project_contract "
        "ORDER BY project_contract_id"
    )).all()
    assert rows == [
        ("remediation-contract-1", Decimal("106.00"), 1),
        ("remediation-contract-2", None, 2),
    ]
    run = db.execute(text(
        "SELECT source_snapshot_sha256, source_backup_sha256, "
        "source_algorithm_sha256, f7_affected_set_sha256, "
        "preserved_set_sha256, changed_set_sha256, "
        "f7_affected_count, preserved_count, authoritative_corrected_count, "
        "cleared_count, row_count "
        "FROM maintenance_contract_amount_remediation_run"
    )).one()
    assert run == (
        remediation.f7_source_snapshot_sha256(source),
        "c" * 64,
        remediation.F7_ALGORITHM_SHA256,
        source["affected_set_sha256"],
        manifest["partition"]["preserved_set_sha256"],
        manifest["partition"]["changed_set_sha256"],
        2,
        1,
        0,
        1,
        1,
    )


def test_forged_no_evidence_clear_cannot_claim_a_post_f7_manual_value():
    source = _source_snapshot()
    manifest = _incomplete_manifest()
    manifest["rows"][0]["expected_version"] = 2
    manifest["rows"][0]["expected_current_amount_inc_tax"] = "120.00"

    with pytest.raises(remediation.RemediationError) as caught:
        remediation._validate_f7_binding(
            remediation.normalize_manifest(manifest),
            source_snapshot=source,
        )

    assert caught.value.code == "post_f7_manual_change"


def test_backup_and_partition_tampering_are_rejected_before_database_access():
    source = _source_snapshot()
    manifest = _incomplete_manifest()
    other_backup = copy.deepcopy(source)
    other_backup["backup_sha256"] = "d" * 64

    with pytest.raises(remediation.RemediationError) as caught:
        remediation._validate_f7_binding(
            remediation.normalize_manifest(manifest),
            source_snapshot=other_backup,
        )
    assert caught.value.code == "source_snapshot_mismatch"

    tampered_partition = copy.deepcopy(manifest)
    tampered_partition["partition"]["cleared_count"] = 0
    with pytest.raises(remediation.RemediationError) as caught:
        remediation._validate_f7_binding(
            remediation.normalize_manifest(tampered_partition),
            source_snapshot=source,
        )
    assert caught.value.code == "source_partition_mismatch"


def test_execute_waits_on_ledger_batch_before_taking_workbook_state(db):
    """Real PostgreSQL interleave proves batch/advisory/state cannot invert.

    A ledger-side transaction owns batch -> contract advisory -> workbook
    state.  Remediation must wait at its batch envelope, without already owning
    workbook state, and then complete after the ledger-side transaction exits.
    """

    apply_source = inspect.getsource(maintenance_ledger.apply_batch)
    assert apply_source.index("_lock_contract_evidence_identities") < apply_source.index(
        "_lock_target_projects"
    )

    _seed_contract(db)
    _seed_ledger(db)
    db.execute(text(
        "INSERT INTO maintenance_project_workbook_state "
        "(project_id, revision, data_version) VALUES "
        "('remediation-project-1', 0, :data_version)"
    ), {
        "data_version": remediation._workbook_data_version(
            "remediation-project-1", 0),
    })
    db.commit()
    manifest = _bind({
        "schema_version": 1,
        "mode": "apply",
        "reason": "INC-LOCK：真实 PostgreSQL 锁序交错验证",
        "rows": [{
            "project_contract_id": "remediation-contract-1",
            "expected_version": 1,
            "expected_current_amount_inc_tax": "113.00",
            "evidence": {
                "kind": "ledger_amount_inc_tax",
                "batch_id": "ledger-remediation-batch-1",
                "row_id": "ledger-remediation-row-1",
                "expected_amount_inc_tax": "106.00",
            },
        }],
    })
    digest = remediation.manifest_sha256(manifest)

    db.execute(text("SET LOCAL lock_timeout = '5s'"))
    db.execute(text("SET LOCAL statement_timeout = '10s'"))
    db.execute(text(
        "SELECT batch_id FROM maintenance_ledger_import_batch "
        "WHERE batch_id = 'ledger-remediation-batch-1' FOR UPDATE"
    )).one()
    remediation_reached_batch_envelope = threading.Event()

    def execute_remediation() -> dict:
        with SessionLocal() as session:
            connection = session.connection()

            def observe_batch_envelope(
                _conn, _cursor, statement, _parameters, _context, _executemany,
            ):
                normalized = " ".join(statement.lower().split())
                if (
                    "from maintenance_ledger_import_batch b" in normalized
                    and "for share of b" in normalized
                ):
                    remediation_reached_batch_envelope.set()

            event.listen(
                connection,
                "before_cursor_execute",
                observe_batch_envelope,
            )
            try:
                return remediation.run_remediation(
                    session,
                    manifest=manifest,
                    execute=True,
                    operator="remediation-operator",
                    confirm_manifest_sha256=digest,
                    source_snapshot=_source_snapshot(),
                )
            finally:
                event.remove(
                    connection,
                    "before_cursor_execute",
                    observe_batch_envelope,
                )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(execute_remediation)
        assert remediation_reached_batch_envelope.wait(timeout=10)
        assert not future.done()

        # This is the ledger apply-side order.  If remediation had taken state
        # before waiting for the batch, this real row lock would time out or
        # deadlock instead of succeeding.
        maintenance_ledger._lock_contract_evidence_identities(
            db,
            {"XS-REMEDIATION-1"},
        )
        state = db.execute(text(
            "SELECT revision FROM maintenance_project_workbook_state "
            "WHERE project_id = 'remediation-project-1' FOR UPDATE"
        )).one()
        assert state.revision == 0
        db.commit()

        applied = future.result(timeout=20)

    assert applied["status"] == "applied"
    db.expire_all()
    row = db.execute(text(
        "SELECT amount_inc_tax, version FROM maintenance_project_contract "
        "WHERE project_contract_id = 'remediation-contract-1'"
    )).one()
    assert row == (Decimal("106.00"), 2)
