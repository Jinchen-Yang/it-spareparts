"""Legacy writers participate in shared revisions, audit and transaction boundaries."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.db import SessionLocal
from app.etl import loader
from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductSpec
from app.pn_catalog import api as catalog
from app.pn_catalog.dto import ActorContext, AuthorizationDecision, CommandContext, PatchProfile
from app.pn_catalog.models import AuditEvent
from app.services import master_data, master_edit, merge, spec_extract


def _part(db, pn, **values):
    part = DimPart(pn_std=pn, **values)
    db.add(part)
    db.commit()
    return part.id


def _versions(db, part_id):
    return catalog.read_versions(db, part_id)


def test_old_editor_detects_aba_and_keeps_the_entire_draft_atomic(db):
    created = master_edit.create_part(db, pn_std="LEGACY-ABA", brand="A", force=True)
    baseline = master_edit.get_part_edit_state(db, created["id"])
    master_edit.edit_part(db, pn_std="LEGACY-ABA", updates={"brand": "B"})
    master_edit.edit_part(db, pn_std="LEGACY-ABA", updates={"brand": "A"})
    with pytest.raises(master_edit.MasterEditConflict) as error:
        master_edit.edit_part(db, pn_std="LEGACY-ABA", base_snapshot=baseline,
                              updates={"brand": "C", "unit": "台"})
    assert error.value.detail["code"] == "field_conflict"
    final = master_edit.get_part_edit_state(db, created["id"])
    assert final["fields"]["brand"] == "A"
    assert final["fields"]["unit"] is None
    assert final["field_versions"]["brand"] > baseline["field_versions"]["brand"]


def test_deleted_spec_is_not_recreated_by_refresh_or_rebuild(db):
    created = master_edit.create_part(db, pn_std="LEGACY-SPEC", description="600GB SAS HDD", force=True)
    part_id = created["id"]
    master_edit.edit_part(db, pn_std="LEGACY-SPEC", updates={}, spec_updates={"capacity": None})
    deleted_revision = _versions(db, part_id)["field_versions"]["specs.capacity"]
    master_edit.edit_part(db, pn_std="LEGACY-SPEC", updates={"description": "2TB SAS HDD"})
    spec_extract.backfill_specs(db, rebuild=True)
    db.commit()
    assert db.scalar(select(ProductSpec.id).where(
        ProductSpec.part_id == part_id, ProductSpec.spec_key == "capacity")) is None
    assert _versions(db, part_id)["field_versions"]["specs.capacity"] == deleted_revision
    master_edit.edit_part(db, pn_std="LEGACY-SPEC", updates={}, spec_updates={
        "capacity": {"value": "2TB", "unit": "GB", "numeric_value": "2000"}})
    assert "capacity" not in _versions(db, part_id)["deleted_specs"]
    assert _versions(db, part_id)["field_versions"]["specs.capacity"] > deleted_revision


def test_deleting_absent_spec_retains_tombstone_before_first_auto_value(db):
    created = master_edit.create_part(db, pn_std="LEGACY-ABSENT", description="无容量资料", force=True)
    master_edit.edit_part(db, pn_std="LEGACY-ABSENT", updates={}, spec_updates={"capacity": None})
    master_edit.edit_part(db, pn_std="LEGACY-ABSENT", updates={"description": "4TB SAS HDD"})
    assert db.scalar(select(ProductSpec.id).where(
        ProductSpec.part_id == created["id"], ProductSpec.spec_key == "capacity")) is None
    assert "capacity" in _versions(db, created["id"])["deleted_specs"]


def test_excel_changes_and_audit_roll_back_with_caller_transaction(db):
    part_id = _part(db, "LEGACY-INGEST")
    rows = [{"pn_std": "LEGACY-INGEST", "pn_raw": "LEGACY-INGEST", "description": "600GB SAS HDD"}]
    attrs = loader._merge_part_attrs(rows, is_sales=False)
    loader._upsert_parts(db, attrs, is_sales=False)
    assert _versions(db, part_id)["profile_revision"] == 1
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1
    db.rollback()
    assert db.get(DimPart, part_id).description is None
    assert _versions(db, part_id)["profile_revision"] == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0
    loader._upsert_parts(db, attrs, is_sales=False)
    db.commit()
    first = _versions(db, part_id)
    loader._upsert_parts(db, attrs, is_sales=False)
    db.commit()
    assert _versions(db, part_id) == first
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_alias_ingest_updates_identity_without_profile_churn(db):
    part_id = _part(db, "LEGACY-ALIAS")
    rows = [{"pn_raw": "old spelling", "needs_review": False}]
    loader._upsert_aliases(db, rows, {"old spelling": (part_id, "LEGACY-ALIAS")})
    db.commit()
    first = _versions(db, part_id)
    assert first["identity_revision"] == 1
    assert first["profile_revision"] == 0
    loader._upsert_aliases(db, rows, {"old spelling": (part_id, "LEGACY-ALIAS")})
    db.commit()
    assert _versions(db, part_id) == first
    event = db.scalar(select(AuditEvent))
    assert event.actor["provenance"]["kind"] == "excel"
    assert "aliases" in event.changes


def test_classification_and_dictionary_binding_share_one_transaction(db):
    part_id = _part(db, "LEGACY-CLASSIFY", description="600GB SAS HDD")
    result = master_data.classify_backfill(db)
    assert result["parts_reclassified"] == 1
    assert db.get(DimPart, part_id).category_id is not None
    assert _versions(db, part_id)["profile_revision"] == 1
    db.rollback()
    assert db.get(DimPart, part_id).category_major is None
    assert db.get(DimPart, part_id).category_id is None
    assert _versions(db, part_id)["profile_revision"] == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_classification_rechecks_after_waiting_for_new_module_writer(db):
    part_id = _part(db, "LEGACY-CLASSIFY-RACE", description="600GB SAS HDD")
    context = CommandContext(request_id=uuid4(), actor=ActorContext(
        principal_id="test-user", client_id="itsp", executor_id="itsp", entrypoint="itsp_ui"))
    decision = AuthorizationDecision(readable_fields=frozenset({"*"}), writable_fields=frozenset({"*"}))
    catalog.patch_profile(db, part_id, PatchProfile(
        expected_identity_revision=0, field_versions={"category_major": 0, "category_minor": 0},
        changes={"category_major": "人工专用", "category_minor": "人工小类"}),
        context=context, authorize=lambda *_: decision)
    started = Queue()

    def classify():
        with SessionLocal() as other:
            other.execute(text("SET LOCAL lock_timeout = '10s'"))
            started.put(other.scalar(text("SELECT pg_backend_pid()")))
            result = master_data.classify_backfill(other)
            other.commit()
            return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(classify)
        try:
            pid = started.get(timeout=5)
            deadline = monotonic() + 5
            with SessionLocal() as monitor:
                while not monitor.scalar(text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": pid}):
                    assert monotonic() < deadline, "classifier never waited for actual parent lock"
                    sleep(.01)
            db.commit()
            assert future.result(timeout=15)["parts_reclassified"] == 0
        finally:
            db.rollback()
    db.expire_all()
    part = db.get(DimPart, part_id)
    assert (part.category_major, part.category_minor) == ("人工专用", "人工小类")


def test_merge_closure_revisions_and_atomic_host_rollback(db):
    a = _part(db, "LEGACY-MERGE-A")
    b = _part(db, "LEGACY-MERGE-B")
    c = _part(db, "LEGACY-MERGE-C")
    merge.merge_parts(db, "LEGACY-MERGE-A", "LEGACY-MERGE-B", "first", "tester")
    before = {pid: _versions(db, pid) for pid in (a, b, c)}
    merge.merge_parts_in_transaction(db, "LEGACY-MERGE-B", "LEGACY-MERGE-C", "second", "tester")
    assert all(_versions(db, pid)["identity_revision"] > before[pid]["identity_revision"] for pid in (a, b, c))
    db.rollback()
    assert db.get(DimPart, a).merged_into_id == b
    assert db.get(DimPart, b).status == "active"
    assert {pid: _versions(db, pid) for pid in (a, b, c)} == before


def test_alias_reassignment_rolls_back_identity_and_audit_together(db):
    a = _part(db, "LEGACY-REASSIGN-A")
    b = _part(db, "LEGACY-REASSIGN-B")
    alias = PartAlias(pn_raw="legacy raw", pn_std="LEGACY-REASSIGN-A", part_id=a,
                      source="auto", status="pending", needs_review=True)
    db.add(alias)
    db.commit()
    alias_id = alias.id
    master_data.review_alias_in_transaction(db, alias_id, "reassign", "LEGACY-REASSIGN-B", "tester")
    assert _versions(db, a)["identity_revision"] == 1
    assert _versions(db, b)["identity_revision"] == 1
    db.rollback()
    assert db.get(PartAlias, alias_id).part_id == a
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_import_keeps_one_alias_topology_until_caller_commit(db):
    a = _part(db, "LEGACY-TOPOLOGY-A")
    b = _part(db, "LEGACY-TOPOLOGY-B")
    alias = PartAlias(pn_raw="topology raw", pn_std="LEGACY-TOPOLOGY-A", part_id=a,
                      source="manual", status="active", needs_review=False)
    db.add(alias)
    db.commit()
    alias_id = alias.id
    rows = [{"pn_raw": "topology raw", "pn_std": "TOPOLOGY-RAW"}]
    first, _ = loader._resolve_line_parts(db, rows, is_sales=False)
    assert first["topology raw"][0] == a
    started = Queue()

    def reassign():
        with SessionLocal() as other:
            other.execute(text("SET LOCAL lock_timeout = '10s'"))
            started.put(other.scalar(text("SELECT pg_backend_pid()")))
            return master_data.review_alias(other, alias_id, "reassign", "LEGACY-TOPOLOGY-B", "tester")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reassign)
        try:
            pid = started.get(timeout=5)
            deadline = monotonic() + 5
            with SessionLocal() as monitor:
                while not monitor.scalar(text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": pid}):
                    assert monotonic() < deadline, "identity writer never waited for import topology"
                    sleep(.01)
            again, _ = loader._resolve_line_parts(db, rows, is_sales=False)
            assert again == first
            db.commit()
            assert future.result(timeout=15)["action"] == "reassign"
        finally:
            db.rollback()
    final, _ = loader._resolve_line_parts(db, rows, is_sales=False)
    assert final["topology raw"][0] == b


def test_merge_respects_manual_clear_for_dictionary_bindings(db):
    master_edit.create_part(db, pn_std="PROTECTED-SOURCE", brand="品牌甲", category_major="硬盘", force=True)
    target = master_edit.create_part(db, pn_std="PROTECTED-TARGET", force=True)
    master_edit.edit_part(db, pn_std="PROTECTED-TARGET", updates={"brand": None, "category_major": None})
    merge.merge_parts(db, "PROTECTED-SOURCE", "PROTECTED-TARGET", "test", "tester")
    db.expire_all()
    part = db.get(DimPart, target["id"])
    assert part.brand is None and part.brand_id is None
    assert part.category_major is None and part.category_id is None


def test_unmerge_does_not_erase_same_value_manual_confirmation(db):
    master_edit.create_part(db, pn_std="CONFIRMED-SOURCE", description="源描述", brand="源品牌", force=True)
    target_id = _part(db, "CONFIRMED-TARGET")
    result = merge.merge_parts(db, "CONFIRMED-SOURCE", "CONFIRMED-TARGET", "test", "tester")
    master_edit.edit_part(db, pn_std="CONFIRMED-TARGET", updates={"description": "源描述", "brand": "源品牌"})
    merge.unmerge(db, result["merge_log_id"], "tester")
    db.expire_all()
    target = db.get(DimPart, target_id)
    assert target.description == "源描述" and target.brand == "源品牌" and target.brand_id is not None


def test_import_respects_legacy_minor_only_lock_for_both_category_levels(db):
    part_id = _part(db, "PROTECTED-CATEGORY", category_major="人工一级", category_minor="人工二级",
                    locked_fields=["category_minor"])
    rows = [{"pn_std": "PROTECTED-CATEGORY", "category_major": "导入一级", "category_minor": "导入二级"}]
    loader._upsert_parts(db, loader._merge_part_attrs(rows, is_sales=True), is_sales=True)
    db.commit()
    part = db.get(DimPart, part_id)
    assert (part.category_major, part.category_minor) == ("人工一级", "人工二级")
