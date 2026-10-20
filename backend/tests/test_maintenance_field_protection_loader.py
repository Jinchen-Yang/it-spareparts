"""D-34: 原件上传只覆盖未人工保护的字段，并回执真实生效值。"""
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, MaintenanceManualCostOverride
from app.models.system import SysAuditLog, SysImportBatch
from tests import factories as f


def _load(db, lines, *, order_id="field-protection-order", audit_overwrites=True,
          operated_by="field-protection-test"):
    batch = SysImportBatch(
        filename="field-protection.xlsx", file_type="maintenance",
        file_hash=uuid4().hex, status="success",
    )
    db.add(batch)
    db.flush()
    transformed = f.maintenance_result({order_id: f.maintenance_head(order_id)}, lines)
    report = loader.load(
        db, transformed, batch.id, date(2026, 10, 20), mode="upsert",
        operated_by=operated_by, audit_overwrites=audit_overwrites,
    )
    db.commit()
    return report


def _source_line(raw_id="source-line", *, order_id="field-protection-order", **changes):
    return {
        **f.maintenance_line(order_id, raw_id, "PN-SOURCE", qty="2"),
        **changes,
    }


def _current_line(db, raw_id="source-line"):
    return db.scalar(select(FMaintenanceLine).where(FMaintenanceLine.raw_line_id == raw_id))


def _line_audits(db, line):
    return list(db.scalars(select(SysAuditLog).where(
        SysAuditLog.entity_type == "import_overwrite", SysAuditLog.entity_id == line.id,
        SysAuditLog.reason.contains(FMaintenanceLine.__tablename__),
    ).order_by(SysAuditLog.id)))


def test_note_protection_allows_source_quantity_and_receipt_matches_effective_audit(db):
    _load(db, [_source_line(line_note="原备注")])
    line = _current_line(db)
    line.line_note = "人工核对备注"
    line.manual_override = {
        "line_note": {"value": "人工核对备注", "source_value": "原备注"},
    }
    db.commit()

    report = _load(db, [_source_line(qty=Decimal("7"), line_note="新原件备注")])

    db.refresh(line)
    assert line.qty == Decimal("7")
    assert line.line_note == "人工核对备注"
    assert report["fact_rows_semantic_updated"] == 1
    assert report["fact_rows_unchanged"] == 0
    assert report["fact_rows_protected"] == 1
    assert report["protected_field_counts"] == {"line_note": 1}
    assert report["fact_rows_legacy_review"] == 0
    audit = _line_audits(db, line)[-1]
    assert Decimal(audit.after_json["qty"]) == Decimal("7")
    assert audit.after_json["line_note"] == "人工核对备注"


@pytest.mark.parametrize("protected_member", ["pn_std", "pn_raw", "part_id"])
def test_single_pn_ledger_member_protects_identity_group_without_freezing_quantity(
    db, protected_member,
):
    _load(db, [_source_line()])
    line = _current_line(db)
    replacement = DimPart(pn_std="PN-MANUAL", status="active")
    db.add(replacement)
    db.flush()
    old_part_id = line.part_id
    line.part_id = replacement.id
    line.pn_std = line.pn_raw = "PN-MANUAL"
    # 历史网页可能只留下一个 PN 成员的账本；不要求先做历史转换。
    line.manual_override = {
        protected_member: {
            "value": replacement.id if protected_member == "part_id" else "PN-MANUAL",
            "source_value": old_part_id if protected_member == "part_id" else "PN-SOURCE",
        },
    }
    db.commit()

    report = _load(db, [_source_line(qty=Decimal("9"))])

    db.refresh(line)
    assert (line.pn_std, line.pn_raw, line.part_id, line.qty) == (
        "PN-MANUAL", "PN-MANUAL", replacement.id, Decimal("9"),
    )
    assert report["protected_field_counts"] == {"part_id": 1, "pn_std": 1, "pn_raw": 1}
    assert report["fact_rows_semantic_updated"] == 1
    assert report["fact_rows_protected"] == 1
    audit = _line_audits(db, line)[-1]
    assert (audit.after_json["pn_std"], audit.after_json["pn_raw"],
            audit.after_json["part_id"]) == ("PN-MANUAL", "PN-MANUAL", replacement.id)


@pytest.mark.parametrize("ledger", [{}, {"line_note": {"value": "历史人工备注"}}])
def test_legacy_whole_row_protection_survives_later_partial_ledger(db, ledger):
    _load(db, [_source_line()])
    line = _current_line(db)
    line.edited_source = "workbook_manual"
    line.qty = Decimal("6")
    line.line_note = "历史人工备注"
    line.manual_override = ledger
    db.commit()

    report = _load(db, [_source_line(qty=Decimal("9"), line_note="新原件备注")])

    db.refresh(line)
    assert (line.qty, line.line_note, line.edited_source) == (
        Decimal("6"), "历史人工备注", "workbook_manual",
    )
    assert report["fact_rows_semantic_updated"] == 0
    assert report["fact_rows_unchanged"] == 1
    assert report["fact_rows_legacy_review"] == 1
    assert report["protected_field_counts"] == {"qty": 1, "line_note": 1}
    assert _line_audits(db, line) == []


@pytest.mark.parametrize("prefix", ["manual-line:", "page-manual-", "workbook-create-"])
def test_manually_created_identity_cannot_be_moved_or_overwritten_by_source(db, prefix):
    _load(db, [_source_line()])
    source = _current_line(db)
    raw_id = prefix + "1" * 20
    manual = FMaintenanceLine(
        raw_line_id=raw_id, order_id=source.order_id, line_no=8,
        part_id=source.part_id, pn_std=source.pn_std, pn_raw=source.pn_raw,
        qty=Decimal("4"), line_note="人工新建备注", edited_source="page_manual",
        is_active=True, import_batch_id=source.import_batch_id,
    )
    original_order_id = source.order_id
    original_batch_id = source.import_batch_id
    db.add(manual)
    db.commit()

    report = _load(db, [_source_line(
        raw_id, order_id="different-source-order", qty=Decimal("10"),
        line_note="来源不能接管人工行",
    )], order_id="different-source-order")

    db.refresh(manual)
    assert (manual.order_id, manual.line_no, manual.import_batch_id) == (
        original_order_id, 8, original_batch_id,
    )
    assert (manual.qty, manual.line_note, manual.edited_source) == (
        Decimal("4"), "人工新建备注", "page_manual",
    )
    assert report["fact_rows_semantic_updated"] == 0
    assert report["fact_rows_unchanged"] == 1
    assert report["fact_rows_protected"] == 1
    assert report["fact_rows_legacy_review"] == 0
    assert report["protected_field_counts"] == {
        "order_id": 1, "line_no": 1, "qty": 1, "line_note": 1,
    }
    assert _line_audits(db, manual) == []


def test_equal_protected_value_is_unchanged_without_false_protection_receipt(db):
    _load(db, [_source_line(line_note="人工备注")])
    line = _current_line(db)
    line.manual_override = {"line_note": {"value": "人工备注", "source_value": None}}
    db.commit()

    report = _load(db, [_source_line(line_note="人工备注")])

    assert report["fact_rows_updated"] == 1  # 保留兼容的同键计数。
    assert report["fact_rows_semantic_updated"] == 0
    assert report["fact_rows_unchanged"] == 1
    assert report["fact_rows_protected"] == 0
    assert report["protected_field_counts"] == {}
    assert _line_audits(db, line) == []


@pytest.mark.parametrize("audit_overwrites", [True, False])
def test_source_identity_change_retires_old_manual_price_with_same_transaction_audit(
    db, audit_overwrites,
):
    _load(db, [_source_line()])
    line = _current_line(db)
    override = MaintenanceManualCostOverride(
        line_id=line.id, unit_cost_ex_tax=Decimal("100"),
        unit_cost_inc_tax=Decimal("113"), reason="仅适用原 PN 的人工凭证",
        version=1, active=True, updated_by="original-human",
    )
    db.add(override)
    db.commit()

    _load(db, [_source_line(pn_std="PN-NEW", pn_raw="PN-NEW", qty=Decimal("5"))],
          audit_overwrites=audit_overwrites)

    db.refresh(override)
    assert override.active is False
    assert override.version == 2
    assert override.updated_by == "field-protection-test"
    assert override.unit_cost_ex_tax == Decimal("100")
    assert override.reason == "仅适用原 PN 的人工凭证"
    audit = db.scalar(select(SysAuditLog).where(
        SysAuditLog.entity_type == "maintenance_manual_cost_override",
        SysAuditLog.entity_id == override.id,
    ))
    assert audit is not None
    assert audit.before_json["active"] is True
    assert audit.after_json["active"] is False
    assert audit.operated_by == "field-protection-test"


@pytest.mark.parametrize("unchanged_identity", ["protected", "changed_back"])
def test_unchanged_final_identity_keeps_manual_price(db, monkeypatch, unchanged_identity):
    _load(db, [_source_line()])
    line = _current_line(db)
    override = MaintenanceManualCostOverride(
        line_id=line.id, unit_cost_ex_tax=Decimal("100"),
        unit_cost_inc_tax=Decimal("113"), reason="原 PN 的人工凭证",
        version=1, active=True, updated_by="original-human",
    )
    db.add(override)
    if unchanged_identity == "protected":
        line.manual_override = {"pn_std": {"value": "PN-SOURCE"}}
    db.commit()

    incoming = [_source_line(pn_std="PN-NEW", pn_raw="PN-NEW")]
    if unchanged_identity == "changed_back":
        # 原件重复原始身份跨 SQL chunk，最终又回到原身份：不误退役原价。
        incoming.append(_source_line())
        original_chunks = loader._chunks
        monkeypatch.setattr(loader, "_chunks", lambda rows, n=1: original_chunks(rows, n))
    _load(db, incoming)

    db.refresh(line)
    db.refresh(override)
    assert line.pn_std == "PN-SOURCE"
    assert (override.active, override.version, override.updated_by) == (
        True, 1, "original-human",
    )
    assert db.scalar(select(SysAuditLog.id).where(
        SysAuditLog.entity_type == "maintenance_manual_cost_override",
        SysAuditLog.entity_id == override.id,
    )) is None
