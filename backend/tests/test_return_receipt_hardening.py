"""Adversarial contracts for receipt transactions, ACL, durable audit and rollback."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import config
from app.api import maintenance_return_receipts as api
from app.db import SessionLocal, engine
from app.models.dimensions import DimPart
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project import MaintenanceProjectUserAssignment
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
from app.models.system import SysAuditLog, SysUser
from app.services import maintenance_return_receipts as receipts
from tests.conftest import _alembic_cfg
from tests.test_maintenance_return_receipts_api import _client, _wbdd
from tests.test_site_issue_v2_api import _project


def _create(db, project, **kwargs):
    result = receipts.register_receipt(
        db, project_id=project.project_id, pn="PN-TEST", qty=3,
        operated_by="synthetic-operator", **kwargs,
    )
    db.commit()
    return result


def _failed(db):
    db.expire_all()
    return db.scalars(select(SysAuditLog).where(
        SysAuditLog.entity_type == "return_receipt_attempt"
    ).order_by(SysAuditLog.id)).all()


def _grant(db, project_id, username):
    user = db.scalar(select(SysUser).where(SysUser.username == username))
    db.add(MaintenanceProjectUserAssignment(
        assignment_id=str(uuid4()), project_id=project_id,
        responsibility_type="viewer", user_id=user.id,
        assigned_by="test-admin", assignment_reason="scope test",
    ))
    db.commit()


@pytest.mark.parametrize("changes", [
    {"qty": 9, "condition": "invalid"},
    {"qty": 9, "wbdd_no": "missing"},
    {"qty": 9, "pn": "PN-OTHER", "part_id": -100},
    {"qty": 9, "note": "x" * 513},
    {"qty": 9, "occurred_at": "not-a-date"},
])
def test_failed_validation_does_not_dirty_or_flush_receipt(db, changes):
    project = _project(db, project_id=str(uuid4()))
    created = _create(db, project)
    with Session(engine, autoflush=True) as session:
        row = session.get(MaintenanceRkdReturnLine, created["receipt_id"])
        before = receipts._receipt_dict(row)
        unrelated = DimPart(pn_std="UNRELATED", description="caller work")
        session.add(unrelated)
        with pytest.raises(receipts.ReturnReceiptValidation):
            receipts.update_receipt(
                session, receipt_id=row.rkd_line_id, expected_version=1,
                updates=changes, reason="invalid correction", operated_by="operator",
            )
        assert receipts._receipt_dict(row) == before
        assert not session.is_modified(row)
        session.commit()
    db.expire_all()
    assert db.get(MaintenanceRkdReturnLine, created["receipt_id"]).qty == Decimal(3)
    assert db.scalar(select(DimPart.id).where(DimPart.pn_std == "UNRELATED"))
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit)) == 1


def test_explicit_null_clears_all_optional_fields_and_unassigned_filter(db):
    project = _project(db, project_id=str(uuid4()))
    order = _wbdd(db, project=project)
    db.add(DimPart(pn_std="PN-TEST", description="part"))
    db.commit()
    client = _client(db, username="clear-user")
    base = f"/api/maintenance/projects/stable/{project.project_id}/return-receipts"
    created = client.post(base, json={
        "pn": "PN-TEST", "qty": 3, "wbdd_no": order.order_no,
        "description": "desc", "condition": "坏品", "note": "note",
        "evidence_ref": "reference", "occurred_at": "2026-09-12T00:00:00Z",
    }).json()
    nullable = {key: None for key in (
        "wbdd_no", "part_id", "description", "condition", "note", "evidence_ref", "occurred_at"
    )}
    response = client.patch(f"/api/maintenance/return-receipts/{created['receipt_id']}",
                            json={"version": 1, "reason": "clear fields", **nullable})
    assert response.status_code == 200, response.text
    for field in nullable.keys() - {"wbdd_no"}:
        assert response.json()[field] is None
    assert response.json()["source_order_id"] is None
    client.post(base, json={"pn": "LINKED", "qty": 2, "wbdd_no": order.order_no})
    result = client.get(base, params={"unassigned": True}).json()
    assert result["total"] == 1
    assert result["items"][0]["receipt_id"] == created["receipt_id"]
    for field in ("qty", "pn", "project_id"):
        rejected = client.patch(f"/api/maintenance/return-receipts/{created['receipt_id']}",
                                json={"version": 2, "reason": "bad clear", field: None})
        assert rejected.status_code == 422


def test_idempotency_rejects_changed_command_date_void_and_transfer(db):
    project = _project(db, project_id=str(uuid4()))
    other = _project(db, project_id=str(uuid4()))
    created = _create(db, project, idempotency_key="stable-command")
    for changes in ({"note": "different"}, {"occurred_at": datetime(2026, 9, 12, tzinfo=UTC)}):
        with pytest.raises(receipts.ReturnReceiptConflict):
            _create(db, project, idempotency_key="stable-command", **changes)
        db.rollback()
    receipts.update_receipt(db, receipt_id=created["receipt_id"], expected_version=1,
                            updates={"qty": 4}, reason="correct", operated_by="operator")
    db.commit()
    assert _create(db, project, idempotency_key="stable-command")["qty"] == "4.000"
    receipts.update_receipt(db, receipt_id=created["receipt_id"], expected_version=2,
                            updates={"project_id": other.project_id}, reason="move", operated_by="operator")
    db.commit()
    with pytest.raises(receipts.ReturnReceiptConflict, match="转移"):
        _create(db, project, idempotency_key="stable-command")
    db.rollback()
    receipts.void_receipt(db, receipt_id=created["receipt_id"], expected_version=3,
                          reason="void", operated_by="operator")
    db.commit()
    with pytest.raises(receipts.ReturnReceiptConflict):
        _create(db, project, idempotency_key="stable-command")


def test_real_concurrent_create_same_key_inserts_one_row_and_one_audit(db):
    project = _project(db, project_id=str(uuid4()))
    project_id = project.project_id
    db.commit()
    start = Barrier(2)
    def register():
        with SessionLocal() as session:
            start.wait(timeout=10)
            result = receipts.register_receipt(session, project_id=project_id,
                pn="PN-CONCURRENT", qty=2, idempotency_key="same-key", operated_by="operator")
            session.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(register) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert len({row["receipt_id"] for row in results}) == 1
    assert sorted(row["replayed"] for row in results) == [False, True]
    assert db.scalar(select(func.count()).select_from(MaintenanceRkdReturnLine)) == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit)) == 1


@pytest.mark.parametrize("second_action", ["update", "void"])
def test_real_concurrent_cas_refreshes_preloaded_orm_identity(db, second_action):
    project = _project(db, project_id=str(uuid4()))
    created = _create(db, project)
    start = Barrier(2)
    def write(action):
        with SessionLocal() as session:
            # Both sessions deliberately cache version 1 before either writes.
            row = session.get(MaintenanceRkdReturnLine, created["receipt_id"])
            assert row.version == 1
            start.wait(timeout=10)
            try:
                if action == "void":
                    result = receipts.void_receipt(session, receipt_id=row.rkd_line_id,
                        expected_version=1, reason="concurrent void", operated_by="operator")
                else:
                    result = receipts.update_receipt(session, receipt_id=row.rkd_line_id,
                        expected_version=1, updates={"qty": 6}, reason="concurrent edit", operated_by="operator")
                session.commit()
                return result["version"]
            except receipts.ReturnReceiptConflict:
                session.rollback()
                return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, action) for action in ("update", second_action)]
        results = [future.result(timeout=15) for future in futures]
    assert sorted(map(str, results)) == ["2", "conflict"]
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit)) == 2


def test_order_change_lock_blocks_receipt_until_committed_then_revalidates(db):
    project = _project(db, project_id=str(uuid4()))
    order = _wbdd(db, project=project)
    project_id, order_id, order_no = project.project_id, order.id, order.order_no
    db.commit()
    started = Event()
    worker = {}
    with SessionLocal() as changing:
        changing.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                         {"key": config.DATA_CHANGE_ADVISORY_LOCK_KEY})
        changing.execute(text("UPDATE f_maintenance_order SET data_status='已作废' WHERE id=:id"), {"id": order_id})
        def register():
            with SessionLocal() as session:
                worker["pid"] = session.scalar(text("SELECT pg_backend_pid()"))
                started.set()
                with pytest.raises(receipts.ReturnReceiptValidation):
                    receipts.register_receipt(session, project_id=project_id, wbdd_no=order_no,
                        pn="TEST", qty=1, operated_by="operator")
                session.rollback()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(register)
            assert started.wait(timeout=5)
            deadline = monotonic() + 5
            blocked = False
            try:
                while monotonic() < deadline:
                    blocked = bool(db.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": worker["pid"]}))
                    if blocked:
                        break
                    sleep(0.01)
            finally:
                changing.commit()
            future.result(timeout=10)
            assert blocked, "receipt must wait on the source-writer advisory lock"
    assert db.scalar(select(func.count()).select_from(MaintenanceRkdReturnLine)) == 0


def test_failed_attempts_include_dependency_validation_conflict_and_durable_identity(db, monkeypatch):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="audit-admin")
    viewer = _client(db, username="audit-viewer", role="viewer", permissions={
        "page_maintenance": True, "action_maintenance_bad_return_manage": False})
    base = f"/api/maintenance/projects/stable/{project.project_id}/return-receipts"
    # Failure audit does not depend on best-effort access logging being enabled.
    monkeypatch.setattr(config, "ENABLE_ACCESS_LOG", False)
    assert viewer.post(base, json={"pn": "SENSITIVE-PN", "qty": 1}).status_code == 403
    assert client.post(base, json={"pn": "SENSITIVE-PN", "qty": True}).status_code == 422
    assert client.post(base, json={"pn": "SENSITIVE-PN", "qty": 1, "wbdd_no": "MISSING"}).status_code == 422
    created = client.post(base, json={"pn": "OK", "qty": 1}).json()
    assert client.patch(f"/api/maintenance/return-receipts/{created['receipt_id']}",
                        json={"version": 99, "qty": 2, "reason": "PRIVATE-REASON"}).status_code == 409
    audits = _failed(db)
    assert [r.after_json["status_code"] for r in audits] == [403, 422, 422, 409]
    assert [r.operated_by for r in audits] == ["audit-viewer", "audit-admin", "audit-admin", "audit-admin"]
    assert "SENSITIVE" not in str([r.after_json for r in audits])
    assert "PRIVATE" not in str([r.after_json for r in audits])
    assert "Bearer" not in str([r.after_json for r in audits])


def test_audit_storage_failure_returns_safe_503(db, monkeypatch):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="audit-failure")
    def unavailable(*args, **kwargs):
        raise RuntimeError("SECRET DATABASE PARAMETERS")
    monkeypatch.setattr(api, "_record_failed_attempt", unavailable)
    response = client.post(f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
                           json={"pn": "TEST", "qty": 0})
    assert response.status_code == 503
    assert "SECRET" not in response.text


def test_transfer_requires_both_project_scopes_and_history_does_not_leak(db):
    a = _project(db, project_id=str(uuid4()))
    b = _project(db, project_id=str(uuid4()))
    admin = _client(db, username="transfer-admin")
    perms = {"page_maintenance": True, "action_maintenance_bad_return_manage": True}
    only_a = _client(db, username="only-a", role="viewer", permissions=perms)
    only_b = _client(db, username="only-b", role="viewer", permissions=perms)
    _grant(db, a.project_id, "only-a")
    _grant(db, b.project_id, "only-b")
    created = admin.post(f"/api/maintenance/projects/stable/{a.project_id}/return-receipts",
        json={"pn": "TEST", "qty": 1, "note": "PRIVATE-A"}).json()
    url = f"/api/maintenance/return-receipts/{created['receipt_id']}"
    transfer = {"version": 1, "reason": "PRIVATE-A-TRANSFER", "project_id": b.project_id}
    assert only_a.patch(url, json=transfer).status_code == 403
    assert only_b.patch(url, json=transfer).status_code == 403
    assert admin.patch(url, json=transfer).status_code == 200
    assert only_a.get(url + "/audit").status_code == 403
    history = only_b.get(url + "/audit")
    assert history.status_code == 200
    assert history.json()["items"] == []
    assert len(admin.get(url + "/audit").json()["items"]) == 3


def test_downgrade_refuses_manual_facts_without_data_or_schema_loss(db):
    project = _project(db, project_id=str(uuid4()))
    created = _create(db, project)
    db.close()
    with pytest.raises(DBAPIError, match="downgrade refused"):
        command.downgrade(_alembic_cfg(), "c9e5a1b7d3f8")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT qty, source, version FROM maintenance_rkd_return_line WHERE rkd_line_id=:id"),
                           {"id": created["receipt_id"]}).one()
        assert tuple(row) == (Decimal(3), "manual", 1)
    command.upgrade(_alembic_cfg(), "head")


def test_legacy_recovery_reads_corrected_import_facts_without_counting_manual(db):
    from app.services.maintenance_recovery import recovery_summary
    from tests.test_maintenance_recovery import _seed_recovery
    seeded = _seed_recovery(db)
    project_id = seeded["project_id"]
    receipts.register_receipt(db, project_id=project_id, pn="MANUAL", qty=7,
                              condition="坏品", operated_by="operator")
    db.commit()
    assert receipts.receipt_summary(db, project_id=project_id)["project_total_qty"] == "8.000"
    assert recovery_summary(db, project_id)["bad_returned_total_qty"] == 1
    receipts.update_receipt(db, receipt_id="rc-line-1", expected_version=1,
        updates={"qty": 4}, reason="correct imported quantity", operated_by="operator")
    db.commit()
    assert recovery_summary(db, project_id)["bad_returned_total_qty"] == 4
    receipts.update_receipt(db, receipt_id="rc-line-1", expected_version=2,
        updates={"condition": "成品"}, reason="correct imported condition", operated_by="operator")
    db.commit()
    assert recovery_summary(db, project_id)["bad_returned_total_qty"] == 0
    assert receipts.receipt_summary(db, project_id=project_id)["project_total_qty"] == "11.000"
    receipts.void_receipt(db, receipt_id="rc-line-1", expected_version=3,
                          reason="void imported fact", operated_by="operator")
    db.commit()
    assert receipts.receipt_summary(db, project_id=project_id)["project_total_qty"] == "7.000"
    # Receipt corrections never mutate the separate front-stock balance.
    assert recovery_summary(db, project_id)["remaining_total_qty"] == 3


def test_stable_panel_gate_closes_receipt_routes_and_audits_rejection(db, monkeypatch):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="receipt-gate")
    monkeypatch.setattr(config.get_settings(), "maintenance_boss_dashboard_enabled", False)
    response = client.post(f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
                           json={"pn": "TEST", "qty": 1})
    assert response.status_code == 404
    assert _failed(db)[0].after_json["status_code"] == 404
    assert db.scalar(select(func.count()).select_from(MaintenanceRkdReturnLine)) == 0


def test_project_inactive_and_tombstoned_order_rejected(db):
    from app.models.maintenance import MaintenanceDemandDeleteIntent, MaintenanceDemandTombstone
    project = _project(db, project_id=str(uuid4()))
    order = _wbdd(db, project=project)
    project.is_active = False
    db.commit()
    with pytest.raises(receipts.ReturnReceiptValidation, match="停用"):
        _create(db, project)
    db.rollback()
    project.is_active = True
    db.commit()
    # An active source row with an active Beta tombstone is not selectable.
    intent = MaintenanceDemandDeleteIntent(
        intent_id=str(uuid4()), idempotency_key=str(uuid4()), operated_by="operator",
        request_digest="0" * 64, selection_digest="0" * 64, reason="delete test",
        header_count=1, line_count=0,
        created_at=datetime.now(UTC), expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    db.add(intent)
    db.flush()
    db.add(MaintenanceDemandTombstone(
        source_order_id=order.raw_order_id, delete_intent_id=intent.intent_id,
        version_digest="0" * 64, deleted_by="operator", delete_reason="delete test",
        deleted_at=datetime.now(UTC),
    ))
    db.commit()
    with pytest.raises(receipts.ReturnReceiptValidation):
        _create(db, project, wbdd_no=order.order_no)


def test_same_key_different_payload_concurrent_requests_conflict(db):
    project = _project(db, project_id=str(uuid4()))
    project_id = project.project_id
    db.commit()
    start = Barrier(2)
    def register(qty):
        with SessionLocal() as session:
            start.wait(timeout=10)
            try:
                result = receipts.register_receipt(session, project_id=project_id,
                    pn="TEST", qty=qty, idempotency_key="conflict-key", operated_by="operator")
                session.commit()
                return result["qty"]
            except receipts.ReturnReceiptConflict:
                session.rollback()
                return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(register, qty) for qty in (1, 2)]
        results = [future.result(timeout=15) for future in futures]
    assert results.count("conflict") == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceRkdReturnLine)) == 1


def test_failure_audit_never_trusts_body_or_fallback_username(db):
    from app.auth import _make_token
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="trusted-operator")
    base = f"/api/maintenance/projects/stable/{project.project_id}/return-receipts"
    assert client.post(base, json={"pn": "TEST", "qty": 1, "operated_by": "forged"}).status_code == 422
    token, _ = _make_token(role="admin", sub="forged-shared", name=None, fallback=True)
    client.headers["Authorization"] = f"Bearer {token}"
    assert client.post(base, json={"pn": "TEST", "qty": 1}).status_code == 403
    audits = _failed(db)
    assert [row.operated_by for row in audits] == ["trusted-operator", None]
    assert "forged" not in str([row.after_json for row in audits])


def test_explicit_integer_correction_resolves_fraction_review_retaining_evidence(db):
    from tests.test_maintenance_recovery import _seed_recovery
    _seed_recovery(db)
    row = db.get(MaintenanceRkdReturnLine, "rc-line-1")
    row.qty = Decimal("0.500")
    row.review_required = True
    row.source_payload = {"qty": "0.500", "test_result": "坏品"}
    db.commit()
    changed = receipts.update_receipt(db, receipt_id=row.rkd_line_id, expected_version=1,
        updates={"qty": 1}, reason="received one intact unit", operated_by="operator")
    db.commit()
    assert changed["review_required"] is False
    assert row.source_payload["qty"] == "0.500"
    audit = db.scalar(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_id == row.rkd_line_id))
    assert audit.before_json["review_required"] is True
    assert audit.after_json["review_required"] is False


def test_manual_registration_does_not_bind_merged_part_or_stale_orm_identity(db):
    project = _project(db, project_id=str(uuid4()))
    target = DimPart(pn_std="ACTIVE-CANONICAL")
    old = DimPart(pn_std="MERGED-SOURCE-PN")
    db.add_all([target, old])
    db.commit()
    old_id, target_id = old.id, target.id
    assert old.status == "active"  # Deliberately cache before concurrent merge.
    with SessionLocal() as merging:
        merging.execute(text("UPDATE dim_part SET status='merged', merged_into_id=:target WHERE id=:old"),
                        {"target": target_id, "old": old_id})
        merging.commit()
    with pytest.raises(receipts.ReturnReceiptValidation):
        receipts.register_receipt(db, project_id=project.project_id, pn="MERGED-SOURCE-PN",
                                  part_id=old_id, qty=1, operated_by="operator")
    db.rollback()
    result = receipts.register_receipt(db, project_id=project.project_id, pn="MERGED-SOURCE-PN",
                                       qty=1, operated_by="operator")
    db.commit()
    assert result["pn"] == "MERGED-SOURCE-PN"
    assert result["part_id"] is None
