"""D-34：自动解除采用最新合法原件值，保留人工覆盖与首次撤销快照。"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysAuditLog
from app.services import maintenance_field_protection as protection
from app.services import maintenance_project_master_workbook as master
from tests import factories as f
from tests.test_demand_manual_edit import _batch
from tests.test_maintenance_field_protection_api import (
    _admin_client,
    _make_project,
    _patch,
    _read_line,
    _seed_line,
    _upload,
)


def _upload_mode(db, order, line, *, mode="upsert", audit_overwrites=False, **changes):
    source = {
        **f.maintenance_line(order.raw_order_id, line.raw_line_id, "PN-E1", qty="2"),
        **changes,
    }
    report = loader.load(
        db, f.maintenance_result(
            {order.raw_order_id: f.maintenance_head(order.raw_order_id, on=date(2026, 3, 1))},
            [source],
        ), _batch(db), date(2026, 10, 20), mode=mode,
        audit_overwrites=audit_overwrites, operated_by="source-refresh-test",
    )
    db.commit()
    return report


def _refresh_audits(db, line):
    return list(db.scalars(select(SysAuditLog).where(
        SysAuditLog.entity_type == "maintenance_field_protection",
        SysAuditLog.entity_id == line.id,
        SysAuditLog.action == "source_snapshot_refresh",
    ).order_by(SysAuditLog.id)))


def _clear(client, order, line, field):
    response = client.post(
        f"/api/maintenance/demands/lines/{line.raw_line_id}/clear-override",
        json={
            "field_name": field, "reason": "撤销人工覆盖",
            "expected_digest": _read_line(client, order)["digest"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_auto_release_compares_latest_legal_source_after_protected_upload(db):
    order, line = _seed_line(db)
    _make_project(db, tag="LATEST-SOURCE", source_order_id=order.raw_order_id)
    client = _admin_client(db)

    _patch(client, order, line.raw_line_id, {"qty": 5})
    _upload(db, order, line, qty="9")
    protected = _read_line(client, order)
    assert protected["qty"] == "5.000"
    assert protected["protected_fields"] == ["qty"]
    assert Decimal(protected["manual_override"]["qty"]["source_value"]) == Decimal(9)

    former_source = _patch(client, order, line.raw_line_id, {"qty": 2})
    assert former_source["qty"] == "2.000"
    assert former_source["protected_fields"] == ["qty"]

    latest_source = _patch(client, order, line.raw_line_id, {"qty": 9})
    assert latest_source["qty"] == "9.000"
    assert latest_source["protected_fields"] == []
    assert latest_source["manual_override"] == {}

    _upload(db, order, line, qty="11")
    assert _read_line(client, order)["qty"] == "11.000"


@pytest.mark.parametrize("audit_overwrites", [False, True])
def test_source_refresh_retains_audit_and_repeated_source_is_noop(db, audit_overwrites):
    order, line = _seed_line(db)
    _make_project(db, tag="REFRESH-AUDIT", source_order_id=order.raw_order_id)
    client = _admin_client(db)
    manual = _patch(client, order, line.raw_line_id, {"qty": 5})
    original_entry = manual["manual_override"]["qty"]

    report = _upload_mode(db, order, line, qty="9", audit_overwrites=audit_overwrites)
    refreshed = _read_line(client, order)
    assert report["fact_rows_semantic_updated"] == 0
    assert report["fact_rows_unchanged"] == 1
    assert report["fact_rows_protected"] == 1
    assert refreshed["qty"] == "5.000"
    entry = refreshed["manual_override"]["qty"]
    assert entry["updated_by"] == original_entry["updated_by"]
    assert entry["updated_at"] == original_entry["updated_at"]
    assert Decimal(entry["original_source_value"]) == Decimal(2)
    audits = _refresh_audits(db, line)
    assert len(audits) == 1
    assert audits[0].operated_by == "source-refresh-test"
    assert Decimal(audits[0].before_json["manual_override"]["qty"]["source_value"]) == Decimal(2)
    assert Decimal(audits[0].after_json["manual_override"]["qty"]["source_value"]) == Decimal(9)
    assert audits[0].after_json["manual_override"]["qty"]["value"] == "5.000"

    repeated = _upload_mode(db, order, line, qty="9.000", audit_overwrites=audit_overwrites)
    assert repeated["fact_rows_semantic_updated"] == 0
    assert repeated["workbook_projects_bumped"] == 0
    assert _read_line(client, order)["digest"] == refreshed["digest"]
    assert len(_refresh_audits(db, line)) == 1


def test_explicit_clear_restores_first_quantity_after_source_refresh_and_another_edit(db):
    order, line = _seed_line(db)
    _make_project(db, tag="FIRST-SNAPSHOT", source_order_id=order.raw_order_id)
    client = _admin_client(db)
    _patch(client, order, line.raw_line_id, {"qty": 5})
    _upload(db, order, line, qty="9")
    _patch(client, order, line.raw_line_id, {"qty": 3})

    restored = _clear(client, order, line, "qty")
    assert restored["qty"] == "2.000"
    assert restored["protected_fields"] == []
    assert restored["source_reupload_required"] is True
    assert "首次" in restored["message"]
    _upload(db, order, line, qty="9")
    assert _read_line(client, order)["qty"] == "9.000"


def test_skip_preserves_source_snapshot_and_does_not_add_refresh_audit(db):
    order, line = _seed_line(db)
    _make_project(db, tag="SKIP-SOURCE", source_order_id=order.raw_order_id)
    client = _admin_client(db)
    before = _patch(client, order, line.raw_line_id, {"qty": 5})

    report = _upload_mode(db, order, line, mode="skip", qty="9")
    current = _read_line(client, order)
    assert report["fact_rows_skipped"] == 1
    assert current["qty"] == "5.000"
    assert current["manual_override"] == before["manual_override"]
    assert current["digest"] == before["digest"]
    assert _refresh_audits(db, line) == []
    assert _patch(client, order, line.raw_line_id, {"qty": 2})["protected_fields"] == []


def test_unchanged_source_does_not_enrich_ledger_or_bump_version(db):
    order, line = _seed_line(db)
    _make_project(db, tag="SOURCE-UNCHANGED", source_order_id=order.raw_order_id)
    client = _admin_client(db)
    before = _patch(client, order, line.raw_line_id, {"qty": 5})

    report = _upload_mode(db, order, line, qty="2.000")
    current = _read_line(client, order)
    assert current["manual_override"] == before["manual_override"]
    assert current["digest"] == before["digest"]
    assert report["workbook_projects_bumped"] == 0
    assert _refresh_audits(db, line) == []


def test_metadata_only_refresh_invalidates_page_digest_and_marks_workbook_revision_drift(db):
    order, line = _seed_line(db)
    project = _make_project(db, tag="REFRESH-VERSION", source_order_id=order.raw_order_id)
    client = _admin_client(db)
    before = _patch(client, order, line.raw_line_id, {"qty": 5})
    exported = master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_PARTS,),
    )
    db.commit()

    report = _upload_mode(db, order, line, qty="9")
    current = _read_line(client, order)
    assert current["qty"] == before["qty"]
    assert current["digest"] != before["digest"]
    assert report["fact_rows_semantic_updated"] == 0
    assert report["workbook_projects_bumped"] == 1
    stale = client.patch(f"/api/maintenance/demands/lines/{line.raw_line_id}", json={
        "updates": {"qty": 2}, "reason": "旧来源基线的修改",
        "expected_digest": before["digest"],
    })
    assert stale.status_code == 409, stale.text
    assert _read_line(client, order)["qty"] == "5.000"

    plan = master.validate_project_master_v2(
        db, project_id=project.project_id, data=exported,
    )
    result = master.apply_project_master_v2(
        db, plan, operated_by="unchanged-workbook", import_batch_id=str(uuid4()),
    )
    db.commit()
    assert result["revision_drift"] is True
    assert _read_line(client, order)["manual_override"] == current["manual_override"]


def test_pn_latest_source_releases_only_after_every_member_returns(db):
    order, line = _seed_line(db)
    _make_project(db, tag="LATEST-PN", source_order_id=order.raw_order_id)
    db.add_all([DimPart(pn_std="PN-MANUAL"), DimPart(pn_std="PN-LATEST")])
    db.commit()
    client = _admin_client(db)
    _patch(client, order, line.raw_line_id, {"pn_raw": "PN-MANUAL", "pn_std": "PN-MANUAL"})
    _upload(db, order, line, pn_raw="PN-LATEST", pn_std="PN-LATEST")

    refreshed = _read_line(client, order)
    assert refreshed["pn_std"] == "PN-MANUAL"
    assert refreshed["manual_override"]["pn_std"]["source_value"] == "PN-LATEST"
    assert refreshed["manual_override"]["pn_raw"]["source_value"] == "PN-LATEST"
    latest_id = db.scalar(select(DimPart.id).where(DimPart.pn_std == "PN-LATEST"))
    assert refreshed["manual_override"]["part_id"]["source_value"] == latest_id

    partial = _patch(client, order, line.raw_line_id, {"pn_std": "PN-LATEST"})
    assert partial["pn_raw"] == "PN-MANUAL"
    assert partial["part_id"] == latest_id
    assert partial["protected_fields"] == ["part_id", "pn_raw", "pn_std"]
    complete = _patch(client, order, line.raw_line_id, {"pn_raw": "PN-LATEST"})
    assert complete["protected_fields"] == []
    assert complete["manual_override"] == {}
    _upload(db, order, line, pn_raw="PN-AFTER-RELEASE", pn_std="PN-AFTER-RELEASE")
    assert _read_line(client, order)["pn_std"] == "PN-AFTER-RELEASE"


def test_explicit_pn_clear_keeps_first_group_after_latest_source_and_partial_edit(db):
    order, line = _seed_line(db)
    original_part_id = line.part_id
    _make_project(db, tag="FIRST-PN", source_order_id=order.raw_order_id)
    db.add_all([DimPart(pn_std="PN-MANUAL"), DimPart(pn_std="PN-LATEST")])
    db.commit()
    client = _admin_client(db)
    _patch(client, order, line.raw_line_id, {"pn_raw": "PN-MANUAL", "pn_std": "PN-MANUAL"})
    _upload(db, order, line, pn_raw="PN-LATEST", pn_std="PN-LATEST")
    _patch(client, order, line.raw_line_id, {"pn_std": "PN-LATEST"})

    restored = _clear(client, order, line, "pn_raw")
    assert (restored["pn_raw"], restored["pn_std"], restored["part_id"]) == (
        "PN-E1", "PN-E1", original_part_id,
    )
    assert restored["manual_override"] == {}
    assert restored["source_reupload_required"] is True


def test_historical_missing_pn_source_members_cannot_be_inferred_from_manual_values():
    line = SimpleNamespace(**{field: None for field in protection.DEMAND_FACT_FIELDS})
    line.raw_line_id = "source-with-incomplete-pn-history"
    line.edited_source = "wbdd"
    line.pn_std = "PN-MANUAL"
    line.pn_raw = "manual alias"
    line.part_id = 42
    line.manual_override = {
        "pn_std": {"value": "PN-MANUAL", "source_value": "PN-SOURCE"},
    }
    before = protection.snapshot_fields(line)
    line.pn_std = "PN-SOURCE"
    protection.record_manual_changes(
        line, before=before, operated_by="historical-review", source="page_manual",
    )
    assert protection.protected_fields(
        raw_line_id=line.raw_line_id, edited_source=line.edited_source,
        manual_override=line.manual_override,
    ) == protection.PN_IDENTITY_FIELDS

    # 再改 raw 后改回它的人工原值，也不能把未知历史来源补成已知来源。
    before = protection.snapshot_fields(line)
    line.pn_raw = "another manual alias"
    protection.record_manual_changes(
        line, before=before, operated_by="historical-review", source="page_manual",
    )
    before = protection.snapshot_fields(line)
    line.pn_raw = "manual alias"
    protection.record_manual_changes(
        line, before=before, operated_by="historical-review", source="page_manual",
    )
    assert protection.protected_fields(
        raw_line_id=line.raw_line_id, edited_source=line.edited_source,
        manual_override=line.manual_override,
    ) == protection.PN_IDENTITY_FIELDS


@pytest.mark.parametrize("action", ["return_to_latest", "explicit_clear"])
def test_source_upload_completes_historical_pn_group_without_fabricating_first_snapshot(db, action):
    order, line = _seed_line(db)
    original_part_id = line.part_id
    _make_project(db, tag="PN-PARTIAL-HISTORY", source_order_id=order.raw_order_id)
    replacement = DimPart(pn_std="PN-MANUAL")
    db.add(replacement)
    db.flush()
    line.pn_std = line.pn_raw = "PN-MANUAL"
    line.part_id = replacement.id
    line.manual_override = {
        "pn_std": {
            "value": "PN-MANUAL", "source_value": "PN-E1",
            "source_value_part_id": original_part_id,
        },
    }
    db.commit()
    client = _admin_client(db)
    _upload(db, order, line, pn_raw="PN-LATEST", pn_std="PN-LATEST")
    refreshed = _read_line(client, order)
    assert refreshed["protected_fields"] == ["part_id", "pn_raw", "pn_std"]
    assert refreshed["manual_override"]["pn_raw"]["source_value"] == "PN-LATEST"
    assert refreshed["manual_override"]["pn_raw"]["original_source_value_known"] is False

    if action == "return_to_latest":
        restored = _patch(client, order, line.raw_line_id, {
            "pn_raw": "PN-LATEST", "pn_std": "PN-LATEST",
        })
        assert restored["pn_raw"] == restored["pn_std"] == "PN-LATEST"
    else:
        restored = _clear(client, order, line, "pn_std")
        assert restored["pn_std"] == "PN-E1"
        assert restored["pn_raw"] == "PN-MANUAL"
        assert restored["part_id"] == original_part_id
    assert restored["protected_fields"] == []


def test_source_becoming_equal_to_manual_value_does_not_release_without_manual_change(db):
    order, line = _seed_line(db)
    _make_project(db, tag="SOURCE-EQUAL", source_order_id=order.raw_order_id)
    client = _admin_client(db)
    _patch(client, order, line.raw_line_id, {"qty": 5})
    _upload(db, order, line, qty="5")
    current = _read_line(client, order)
    assert current["protected_fields"] == ["qty"]
    assert Decimal(current["manual_override"]["qty"]["source_value"]) == Decimal(5)
    unchanged = _patch(client, order, line.raw_line_id, {"qty": "5.000"})
    assert unchanged["changed"] is False
    assert unchanged["manual_override"] == current["manual_override"]


@pytest.mark.parametrize("edited_source,raw_prefix", [
    ("workbook_manual", "legacy-"),
    ("page_manual", "manual-line:"),
    ("page_manual", "page-manual-"),
    ("workbook_manual", "workbook-create-"),
])
def test_source_refresh_does_not_change_legacy_or_manual_line_ledger(db, edited_source, raw_prefix):
    order, line = _seed_line(db)
    _make_project(db, tag="WHOLE-ROW", source_order_id=order.raw_order_id)
    line.raw_line_id = raw_prefix + "preserve-history"
    line.edited_source = edited_source
    line.qty = Decimal(5)
    original = {"qty": {"value": "5.000", "source_value": "2.000"}}
    line.manual_override = original
    db.commit()

    _upload(db, order, line, qty="9")
    db.refresh(line)
    assert line.qty == Decimal(5)
    assert line.manual_override == original
    assert _refresh_audits(db, line) == []


def test_explicit_pn_clear_does_not_invent_first_part_after_source_refresh_and_alias_edit(db):
    order, line = _seed_line(db)
    _make_project(db, tag="PN-UNKNOWN-FIRST-PART", source_order_id=order.raw_order_id)
    replacement = DimPart(pn_std="PN-MANUAL")
    db.add(replacement)
    db.flush()
    manual_part_id = replacement.id
    line.pn_std = line.pn_raw = "PN-MANUAL"
    line.part_id = manual_part_id
    # 历史证据只知道 std 曾为 A；不存在能证明首次 part_id 的快照。
    line.manual_override = {
        "pn_std": {"value": "PN-MANUAL", "source_value": "PN-E1"},
    }
    db.commit()
    client = _admin_client(db)

    _upload(db, order, line, pn_raw="PN-LATEST", pn_std="PN-LATEST")
    refreshed = _read_line(client, order)
    assert refreshed["manual_override"]["part_id"]["original_source_value_known"] is False
    latest_part_id = db.scalar(select(DimPart.id).where(DimPart.pn_std == "PN-LATEST"))
    assert refreshed["manual_override"]["part_id"]["source_value"] == latest_part_id
    edited = _patch(client, order, line.raw_line_id, {"pn_raw": "unresolved-manual-alias"})
    assert edited["part_id"] == manual_part_id

    restored = _clear(client, order, line, "pn_std")
    assert restored["pn_std"] == "PN-E1"
    assert restored["pn_raw"] == "unresolved-manual-alias"
    assert restored["part_id"] == manual_part_id
    assert restored["manual_override"] == {}
    assert restored["source_reupload_required"] is True


@pytest.mark.parametrize("edited_source,raw_prefix", [
    ("workbook_manual", "legacy-"),
    ("wbdd", "manual-line:"),
    ("wbdd", "page-manual-"),
    ("wbdd", "workbook-create-"),
])
def test_return_to_source_does_not_release_legacy_or_manual_whole_row(edited_source, raw_prefix):
    line = SimpleNamespace(**{field: None for field in protection.DEMAND_FACT_FIELDS})
    line.raw_line_id = raw_prefix + "manual-return"
    line.edited_source = edited_source
    line.qty = Decimal(5)
    line.manual_override = {"qty": {"value": "5.000", "source_value": "2.000"}}
    before = protection.snapshot_fields(line)
    line.qty = Decimal("2.000")

    changed = protection.record_manual_changes(
        line, before=before, operated_by="manual-return", source="page_manual",
    )

    assert changed == {"qty"}
    assert "qty" in line.manual_override
    assert protection.protected_fields(
        raw_line_id=line.raw_line_id, edited_source=line.edited_source,
        manual_override=line.manual_override,
    ) == protection.DEMAND_FACT_FIELDS
