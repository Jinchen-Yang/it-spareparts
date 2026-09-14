"""Source immutability and the shared project-writer concurrency boundary."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event
from uuid import uuid4

import pytest
from app.api.maintenance_project_scope import resolve_visible_project_ids
from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.models.inventory import Inventory
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_front_stock import (
    MaintenanceFrontStock,
    MaintenanceFrontStockLedger,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysUser
from app.security import UserContext
from app.services import maintenance_front_stock
from app.services import maintenance_project_assignments as assignments
from app.services import maintenance_project_catalog as catalog
from sqlalchemy import select

from tests.test_maintenance_return_receipts_api import _wbdd
from tests.test_return_receipt_import import (
    Receipt,
    apply,
    imports,
    ledger,
    preview,
    workbook,
)
from tests.test_return_receipt_import import project as project


def test_import_does_not_rewrite_inventory_cost_or_wbdd_source_facts(db, project):
    order = _wbdd(db, project=project)
    part = DimPart(pn_std="MEMORY", description="same PN across facts")
    db.add(part)
    db.flush()
    db.add(
        FMaintenanceLine(
            raw_line_id=str(uuid4()),
            order_id=order.id,
            line_no=1,
            part_id=part.id,
            pn_std=part.pn_std,
            pn_raw=part.pn_std,
            qty=Decimal("42"),
            return_qty=Decimal("3"),
            unit_cost=Decimal("7"),
            cost_amount=Decimal("294"),
            cost_source="direct",
            cost_tax_basis="inc",
            is_active=True,
            import_batch_id=order.import_batch_id,
        )
    )
    db.add(
        Inventory(
            raw_inventory_id=str(uuid4()),
            part_id=part.id,
            pn_std=part.pn_std,
            warehouse="Synthetic warehouse",
            source_qty=15,
            manual_qty=19,
            is_qty_overridden=True,
            unit_cost=7,
            inventory_value=133,
            import_batch_id=order.import_batch_id,
        )
    )
    maintenance_front_stock.apply_movement(
        db,
        project_id=project.project_id,
        part_id=part.id,
        kind="shipment_in",
        source_type="f_maintenance_line",
        source_ref="synthetic-protected-stock",
        qty=Decimal("8"),
        warehouse_name="Synthetic front stock",
        operated_by="tester",
    )
    db.commit()
    protected = (
        Inventory,
        FMaintenanceOrder,
        FMaintenanceLine,
        MaintenanceSourceOrderAssignment,
        MaintenanceFrontStock,
        MaintenanceFrontStockLedger,
    )

    # Snapshot complete persisted rows, including costs, quantities, versions and
    # timestamps, rather than just the columns the implementation currently uses.
    def facts():
        return {
            model.__tablename__: [
                tuple(row) for row in db.execute(select(model.__table__))
            ]
            for model in protected
        }

    before = facts()
    batch = preview(db, workbook(qty="2", wbdd=order.order_no))
    apply(db, batch)
    correction = preview(db, workbook(qty="3", wbdd=order.order_no))
    apply(db, correction, confirm_changes=True, reason="receipt-only correction")
    cancelled = preview(db, workbook(qty="3", wbdd=order.order_no, status="已取消"))
    apply(db, cancelled, confirm_changes=True, reason="receipt-only cancellation")
    db.expire_all()
    assert facts() == before
    assert db.scalar(select(Receipt)).line_status == "voided"


def test_project_archive_waits_for_final_import_validation_and_commit(
    db, project, monkeypatch
):
    batch = preview(db, workbook())
    frozen = (
        batch.batch_id,
        batch.report_json["plan_hash"],
        batch.report_json["preview_token"],
    )
    project_id, version = project.project_id, project.version
    db.commit()
    validated, release, editor_started = Event(), Event(), Event()
    original = imports.build_plan

    def pause_after_final_validation(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("locked"):
            validated.set()
            assert release.wait(timeout=10)
        return result

    monkeypatch.setattr(imports, "build_plan", pause_after_final_validation)

    def importer():
        with SessionLocal() as session:
            return imports.apply(
                session,
                frozen[0],
                "tester",
                plan_hash=frozen[1],
                preview_token=frozen[2],
            )

    def archive():
        with SessionLocal() as session:
            editor_started.set()
            result = catalog.set_project_active(
                session,
                project_id=project_id,
                version=version,
                active=False,
                reason="archive after import",
                operated_by="tester",
            )
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(importer)
        assert validated.wait(timeout=5)
        editing = pool.submit(archive)
        assert editor_started.wait(timeout=5)
        try:
            with pytest.raises(TimeoutError):
                editing.result(timeout=0.3)
        finally:
            release.set()
        assert writing.result(timeout=10)["applied_lines"] == 1
        assert editing.result(timeout=10)["is_active"] is False


def test_scope_is_recomputed_after_waiting_for_project_writer(db, project, monkeypatch):
    """A scope provider must execute after state/project locks, not in the API's
    pre-lock argument evaluation. Use real catalog writing to hold that lock."""
    db.add(
        SysUser(
            username="tester",
            display_name="Synthetic scoped importer",
            role="readonly",
            password_hash="synthetic-unused-login-hash",
            is_active=True,
        )
    )
    db.flush()
    assignments.sync_project_viewers(
        db,
        project_id=project.project_id,
        usernames=["tester"],
        reason="grant import scope",
        operated_by="tester",
    )
    db.commit()
    ctx = UserContext(user_id="tester", role="readonly", is_authenticated=True)
    assert resolve_visible_project_ids(db, ctx) == {project.project_id}
    batch = preview(db, workbook())
    frozen = (
        batch.batch_id,
        batch.report_json["plan_hash"],
        batch.report_json["preview_token"],
    )
    project_id, version = project.project_id, project.version
    db.commit()
    before_lock, scope_called = Event(), Event()
    original = imports.build_plan

    def observe_unlocked(*args, **kwargs):
        result = original(*args, **kwargs)
        if not kwargs.get("locked"):
            before_lock.set()
        return result

    monkeypatch.setattr(imports, "build_plan", observe_unlocked)

    def worker():
        with SessionLocal() as session:

            def scope():
                scope_called.set()
                return resolve_visible_project_ids(session, ctx)

            try:
                imports.apply(
                    session,
                    frozen[0],
                    "tester",
                    plan_hash=frozen[1],
                    preview_token=frozen[2],
                    scope_provider=scope,
                )
            except imports.ImportError as exc:
                session.rollback()
                return exc.code

    with SessionLocal() as editor:
        catalog.update_project(
            editor,
            project_id=project_id,
            version=version,
            updates={"business_type": "备件维保"},
            reason="scope writer lock",
            operated_by="tester",
        )
        assignments.sync_project_viewers(
            editor,
            project_id=project_id,
            usernames=[],
            reason="revoke importer scope",
            operated_by="tester",
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            assert before_lock.wait(timeout=5)
            assert not scope_called.wait(timeout=0.3)
            editor.commit()
            assert future.result(timeout=10) == "permission_denied"
    assert scope_called.is_set()
    db.expire_all()
    assert db.scalar(select(Receipt)) is None


def test_possible_manual_duplicate_uses_business_day_and_explicit_separate_confirmation(
    db, project
):
    # 17:00 UTC on the 11th is 01:00 on the 12th in the business timezone.
    manual = ledger.register_receipt(
        db,
        project_id=project.project_id,
        pn="MEMORY",
        qty=2,
        occurred_at=datetime(2026, 9, 11, 17, tzinfo=UTC),
        operated_by="tester",
    )
    db.commit()
    original = ledger._receipt_dict(db.get(Receipt, manual["receipt_id"]))
    batch = preview(db, workbook())
    assert batch.report_json["possible_duplicates_count"] == 1
    hint = batch.report_json["rows"][0]["possible_duplicates"][0]
    assert (
        hint["receipt_id"] == manual["receipt_id"]
        and hint["receipt_date"] == "2026-09-12"
    )
    with pytest.raises(imports.ImportError) as raised:
        apply(db, batch, confirm_changes=True, reason="not duplicate confirmation")
    assert raised.value.code == "possible_duplicate_confirmation_required"
    db.rollback()
    assert apply(db, batch, confirm_possible_duplicates=True)["applied_lines"] == 1
    assert ledger._receipt_dict(db.get(Receipt, manual["receipt_id"])) == original
    assert len(list(db.scalars(select(Receipt)))) == 2


def test_missing_date_or_other_project_does_not_guess_manual_duplicate(db, project):
    from tests.test_site_issue_v2_api import _project

    other = _project(db, project_id=str(uuid4()))
    ledger.register_receipt(
        db, project_id=project.project_id, pn="MEMORY", qty=2, operated_by="tester"
    )
    ledger.register_receipt(
        db,
        project_id=other.project_id,
        pn="MEMORY",
        qty=2,
        occurred_at=datetime(2026, 9, 12, tzinfo=UTC),
        operated_by="tester",
    )
    db.commit()
    batch = preview(db, workbook())
    assert batch.report_json["possible_duplicates_count"] == 0
    assert not batch.report_json["rows"][0].get("possible_duplicates")
    assert apply(db, batch)["applied_lines"] == 1


def test_manual_candidate_change_invalidates_import_preview(db, project):
    manual = ledger.register_receipt(
        db,
        project_id=project.project_id,
        pn="MEMORY",
        qty=2,
        occurred_at=datetime(2026, 9, 12, tzinfo=UTC),
        operated_by="tester",
    )
    db.commit()
    batch = preview(db, workbook())
    ledger.update_receipt(
        db,
        receipt_id=manual["receipt_id"],
        expected_version=1,
        updates={"note": "review changed"},
        reason="manual review",
        operated_by="tester",
    )
    db.commit()
    with pytest.raises(imports.ImportError) as raised:
        apply(db, batch, confirm_possible_duplicates=True)
    assert raised.value.code == "stale_preview"
    db.rollback()
    assert len(list(db.scalars(select(Receipt)))) == 1


def test_frozen_manual_duplicate_hint_cannot_leak_after_project_transfer(db, project):
    from tests.test_site_issue_v2_api import _project

    other = _project(db, project_id=str(uuid4()))
    manual = ledger.register_receipt(
        db,
        project_id=project.project_id,
        pn="MEMORY",
        qty=2,
        occurred_at=datetime(2026, 9, 12, tzinfo=UTC),
        operated_by="tester",
    )
    db.commit()
    batch = preview(db, workbook())
    assert batch.report_json["possible_duplicates_count"] == 1
    ledger.update_receipt(
        db,
        receipt_id=manual["receipt_id"],
        expected_version=1,
        updates={"project_id": other.project_id},
        reason="move manual fact",
        operated_by="tester",
    )
    db.commit()
    with pytest.raises(imports.ImportError) as raised:
        imports.public_job(db, batch, allowed_project_ids={project.project_id})
    assert raised.value.code == "permission_denied"


def test_manual_registration_waits_until_import_candidate_snapshot_commits(
    db, project, monkeypatch
):
    batch = preview(db, workbook())
    frozen = (
        batch.batch_id,
        batch.report_json["plan_hash"],
        batch.report_json["preview_token"],
    )
    project_id = project.project_id
    db.commit()
    validated, release, manual_started = Event(), Event(), Event()
    original = imports.build_plan

    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("locked"):
            validated.set()
            assert release.wait(timeout=10)
        return result

    monkeypatch.setattr(imports, "build_plan", pause)

    def importer():
        with SessionLocal() as session:
            return imports.apply(
                session,
                frozen[0],
                "tester",
                plan_hash=frozen[1],
                preview_token=frozen[2],
            )

    def manual():
        with SessionLocal() as session:
            manual_started.set()
            result = ledger.register_receipt(
                session,
                project_id=project_id,
                pn="MEMORY",
                qty=2,
                occurred_at=datetime(2026, 9, 12, tzinfo=UTC),
                operated_by="tester",
            )
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(importer)
        assert validated.wait(timeout=5)
        recording = pool.submit(manual)
        assert manual_started.wait(timeout=5)
        try:
            with pytest.raises(TimeoutError):
                recording.result(timeout=0.3)
        finally:
            release.set()
        assert writing.result(timeout=10)["applied_lines"] == 1
        assert recording.result(timeout=10)["source"] == "manual"
    db.expire_all()
    assert len(list(db.scalars(select(Receipt)))) == 2


def test_import_waits_for_model_merge_and_does_not_bind_its_inactive_source(
    db, project, monkeypatch
):
    from app.services import merge

    source = DimPart(pn_std="MEMORY", status="active")
    target = DimPart(pn_std="MEMORY-CANONICAL", status="active")
    db.add_all([source, target])
    db.commit()
    batch = preview(db, workbook())
    frozen = (
        batch.batch_id,
        batch.report_json["plan_hash"],
        batch.report_json["preview_token"],
    )
    db.commit()
    merging, release, validated = Event(), Event(), Event()
    original_snapshot, original_plan = merge._part_snapshot, imports.build_plan

    def pause_merge(row):
        merging.set()
        assert release.wait(timeout=10)
        return original_snapshot(row)

    def observe_plan(*args, **kwargs):
        result = original_plan(*args, **kwargs)
        if kwargs.get("locked"):
            validated.set()
        return result

    monkeypatch.setattr(merge, "_part_snapshot", pause_merge)
    monkeypatch.setattr(imports, "build_plan", observe_plan)

    def merger():
        with SessionLocal() as session:
            return merge.merge_parts(
                session, "MEMORY", "MEMORY-CANONICAL", "merge before binding", "tester"
            )

    def importer():
        with SessionLocal() as session:
            return imports.apply(
                session,
                frozen[0],
                "tester",
                plan_hash=frozen[1],
                preview_token=frozen[2],
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        mutation = pool.submit(merger)
        assert merging.wait(timeout=5)
        importing = pool.submit(importer)
        assert validated.wait(timeout=5)
        try:
            with pytest.raises(TimeoutError):
                importing.result(timeout=0.3)
        finally:
            release.set()
        assert mutation.result(timeout=10)["target_pn"] == "MEMORY-CANONICAL"
        assert importing.result(timeout=10)["applied_lines"] == 1
    db.expire_all()
    assert db.get(DimPart, source.id).status == "merged"
    receipt = db.scalar(select(Receipt))
    assert receipt.pn == "MEMORY" and receipt.part_id is None
