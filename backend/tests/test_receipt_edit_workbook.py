"""D-0051: signed receipt edits retain facts, capacity, scope and first receipts."""
import io
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
from app.services import maintenance_receipt_workbook as workbook
from app.services import maintenance_return_receipts as receipts
from tests.test_maintenance_beta_gate import _client
from tests.test_maintenance_return_receipts_api import _wbdd
from tests.test_site_issue_v2_api import _project


def seed(db, qty=2, **kwargs):
    project = _project(db, project_id=str(uuid4()))
    row = receipts.register_receipt(db, project_id=project.project_id, pn="PN-TEST", qty=qty,
                                    operated_by="synthetic", **kwargs)
    db.commit()
    return project, row


def edit(data, callback):
    wb = load_workbook(io.BytesIO(data)); callback(wb)
    out = io.BytesIO(); wb.save(out); wb.close(); return out.getvalue()


def change(wb, row=2, col=7, value=3):
    wb[workbook.SHEET].cell(row, col, value)
    wb[workbook.SHEET].cell(row, 12, "合成更正")


def apply_service(db, project, data):
    preview = workbook.preview(db, data=data, project_id=project.project_id, operator="synthetic", authorize=lambda: None)
    result = workbook.apply(db, data=data, project_id=project.project_id, operator="synthetic",
                            digest=preview["digest"], authorize=lambda: None)
    db.commit()
    return preview, result


def test_preview_rolls_back_facts_and_audit_then_apply_replays_first_result(db):
    project, receipt = seed(db)
    data = edit(workbook.build(db, project_id=project.project_id), change)
    count = db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit))
    preview = workbook.preview(db, data=data, project_id=project.project_id, operator="synthetic", authorize=lambda: None)
    assert preview["updates"] == 1
    assert preview["rows"][0]["changes"][0]["after"] == "3"
    row = db.get(MaintenanceRkdReturnLine, receipt["receipt_id"], populate_existing=True)
    assert row.qty == 2 and row.version == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit)) == count
    args = dict(data=data, project_id=project.project_id, operator="synthetic", digest=preview["digest"], authorize=lambda: None)
    result = workbook.apply(db, **args); db.commit()
    assert result["updates"] == 1 and not result["replayed"]
    again = workbook.apply(db, **args); db.commit()
    assert again == {**result, "replayed": True}
    db.refresh(row)
    assert row.qty == 3 and row.version == 2
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit)) == count + 2
    repreview = workbook.preview(db, data=data, project_id=project.project_id, operator="synthetic", authorize=lambda: None)
    assert repreview["replayed"] is True


@pytest.mark.parametrize("typed_fields", [(), (7,), (9,), (7, 9)])
def test_same_value_numeric_and_datetime_cells_are_noop_without_reason(db, typed_fields):
    original_time = datetime(2026, 10, 31, 4, 5, 6, 123456, tzinfo=timezone.utc)
    project, receipt = seed(db, occurred_at=original_time)
    data = workbook.build(db, project_id=project.project_id)
    def reenter_same_values(wb):
        ws = wb[workbook.SHEET]
        # Export uses literal text. A normal save alone does not convert it;
        # retyping/pasting an equivalent numeric/date value may do so.
        assert isinstance(ws.cell(2, 7).value, str)
        assert ws.cell(2, 9).value == "2026-10-31 12:05:06"
        if 7 in typed_fields: ws.cell(2, 7, 2)
        if 9 in typed_fields: ws.cell(2, 9, datetime(2026, 10, 31, 12, 5, 6))
    data = edit(data, reenter_same_values)
    if not typed_fields:
        saved = load_workbook(io.BytesIO(data))
        try:
            assert isinstance(saved[workbook.SHEET].cell(2, 7).value, str)
            assert isinstance(saved[workbook.SHEET].cell(2, 9).value, str)
        finally:
            saved.close()
    preview, result = apply_service(db, project, data)
    assert preview["updates"] == preview["voids"] == result["updates"] == result["voids"] == 0
    assert preview["rows"] == result["rows"] == []
    row = db.get(MaintenanceRkdReturnLine, receipt["receipt_id"], populate_existing=True)
    assert row.qty == 2 and row.version == 1
    # Equivalent displayed seconds must not overwrite stored sub-second facts.
    assert row.occurred_at == original_time


def test_void_accepts_equivalent_numeric_and_datetime_cells(db):
    original_time = datetime(2026, 10, 31, 4, 5, 6, 123456, tzinfo=timezone.utc)
    project, receipt = seed(db, occurred_at=original_time)
    def void(wb):
        ws = wb[workbook.SHEET]
        ws.cell(2, 7, 2)
        ws.cell(2, 9, datetime(2026, 10, 31, 12, 5, 6))
        ws.cell(2, 6, "作废")
        ws.cell(2, 12, "重复登记")
    preview, result = apply_service(db, project, edit(workbook.build(db, project_id=project.project_id), void))
    assert preview["updates"] == result["updates"] == 0
    assert preview["voids"] == result["voids"] == 1
    assert preview["rows"][0]["fields"] == []
    row = db.get(MaintenanceRkdReturnLine, receipt["receipt_id"], populate_existing=True)
    assert row.line_status == "voided" and row.version == 2
    assert row.qty == 2 and row.occurred_at == original_time


@pytest.mark.parametrize("new_time, expected_time", [
    (datetime(2026, 10, 31, 12, 5, 7), datetime(2026, 10, 31, 4, 5, 7, tzinfo=timezone.utc)),
    ("2026-10-31 12:05:06.000001", datetime(2026, 10, 31, 4, 5, 6, 1, tzinfo=timezone.utc)),
])
def test_real_quantity_and_time_edits_preserve_precision(db, new_time, expected_time):
    project, receipt = seed(db, occurred_at=datetime(2026, 10, 31, 4, 5, 6, tzinfo=timezone.utc))
    def update(wb):
        change(wb, value=3)
        wb[workbook.SHEET].cell(2, 9, new_time)
    preview, result = apply_service(db, project, edit(workbook.build(db, project_id=project.project_id), update))
    assert preview["updates"] == result["updates"] == 1
    assert preview["rows"][0]["fields"] == ["qty", "occurred_at"]
    row = db.get(MaintenanceRkdReturnLine, receipt["receipt_id"], populate_existing=True)
    assert row.qty == 3 and row.version == 2 and row.occurred_at == expected_time


def test_fractional_quantity_is_not_rounded_into_equivalent_integer(db):
    project, _ = seed(db)
    data = edit(workbook.build(db, project_id=project.project_id),
                lambda wb: change(wb, value="2.00000000000000000001"))
    with pytest.raises(workbook.ReceiptWorkbookError, match="实收数量必须为正整数"):
        workbook.parse(data, project_id=project.project_id)


@pytest.mark.parametrize("column", [3, 11])
def test_numeric_coercion_does_not_apply_to_readonly_or_editable_text(db, column):
    project, receipt = seed(db, note="00123")
    row = db.get(MaintenanceRkdReturnLine, receipt["receipt_id"])
    row.pn = "00123"
    db.commit()
    data = edit(workbook.build(db, project_id=project.project_id),
                lambda wb: change(wb, col=column, value=123))
    with pytest.raises(workbook.ReceiptWorkbookError):
        workbook.parse(data, project_id=project.project_id)
    assert row.pn == row.note == "00123" and row.version == 1


def test_metadata_allows_formatted_empty_rows_with_signed_keys_unchanged(db):
    project, _ = seed(db)
    def add_empty_rows(wb):
        ms = wb[workbook.META_SHEET]
        ms.insert_rows(2)
        ms.cell(2, 1).number_format = "@"
        ms.cell(workbook.MAX_META_ROWS, 2).number_format = "@"
    data = edit(workbook.build(db, project_id=project.project_id), add_empty_rows)
    assert workbook.parse(data, project_id=project.project_id)["operations"] == []


@pytest.mark.parametrize("kind", [
    "duplicate", "unknown", "value_without_key", "whitespace", "missing", "numeric_value",
    "extra_column", "too_many_rows", "extreme_dimension",
])
def test_metadata_padding_does_not_allow_extra_content_or_unbounded_dimensions(db, kind):
    project, _ = seed(db)
    def tamper(wb):
        ms = wb[workbook.META_SHEET]
        if kind == "duplicate": ms.append([ms.cell(1, 1).value, ms.cell(1, 2).value])
        elif kind == "unknown": ms.append(["unknown", "value"])
        elif kind == "value_without_key": ms.cell(6, 2, "value")
        elif kind == "whitespace": ms.cell(6, 1, " ")
        elif kind == "missing": ms.delete_rows(1)
        elif kind == "numeric_value": ms.cell(1, 2, 1)
        elif kind == "extra_column": ms.cell(1, 3).number_format = "@"
        elif kind == "too_many_rows": ms.cell(workbook.MAX_META_ROWS + 1, 2).number_format = "@"
        elif kind == "extreme_dimension": ms.cell(1048576, 2).number_format = "@"
    with pytest.raises(workbook.ReceiptWorkbookError):
        workbook.parse(edit(workbook.build(db, project_id=project.project_id), tamper), project_id=project.project_id)


@pytest.mark.parametrize("kind", ["pn", "version", "identity", "baseline", "signature", "delete", "duplicate", "formula", "new"])
def test_tampering_is_rejected_before_write(db, kind):
    project, row = seed(db)
    data = workbook.build(db, project_id=project.project_id)
    def mutate(wb):
        ws = wb[workbook.SHEET]
        if kind == "pn": ws.cell(2, 3, "FORGED")
        elif kind == "version": ws.cell(2, 2, 4)
        elif kind == "identity": ws.cell(2, 1, str(uuid4()))
        elif kind == "baseline": wb[workbook.BASE_SHEET].cell(1, 1, "[]")
        elif kind == "signature": wb[workbook.META_SHEET].cell(5, 2, "forged")
        elif kind == "delete": ws.delete_rows(2)
        elif kind == "duplicate": ws.append([cell.value for cell in ws[2]])
        elif kind == "formula": ws.cell(2, 7, "=1+2")
        elif kind == "new": ws.append([str(uuid4()), 1, "NEW", "manual", "", "保留", 1, "", "", "", "", "新建"])
    with pytest.raises(workbook.ReceiptWorkbookError):
        workbook.parse(edit(data, mutate), project_id=project.project_id)
    assert db.get(MaintenanceRkdReturnLine, row["receipt_id"]).version == 1


def test_signatures_bind_project_actor_and_file_and_require_preview(db):
    project, _ = seed(db)
    data = edit(workbook.build(db, project_id=project.project_id), change)
    with pytest.raises(workbook.ReceiptWorkbookError):
        workbook.parse(data, project_id=str(uuid4()))
    preview = workbook.preview(db, data=data, project_id=project.project_id, operator="synthetic", authorize=lambda: None)
    args = dict(data=data, project_id=project.project_id, operator="synthetic", authorize=lambda: None)
    with pytest.raises(receipts.ReturnReceiptConflict):
        workbook.apply(db, **args, digest=workbook.parse(data, project_id=project.project_id)["digest"])
    with pytest.raises(receipts.ReturnReceiptConflict):
        workbook.apply(db, **{**args, "operator": "other"}, digest=preview["digest"])
    with pytest.raises(receipts.ReturnReceiptConflict):
        workbook.apply(db, **{**args, "data": edit(data, lambda w: change(w, value=4))}, digest=preview["digest"])
    token = json.loads(preview["digest"]); token["actor"] = "other"
    with pytest.raises(receipts.ReturnReceiptConflict):
        workbook.apply(db, **{**args, "operator": "other"}, digest=json.dumps(token))


def test_stale_preview_fails_without_changing_any_row(db):
    project, receipt = seed(db)
    data = edit(workbook.build(db, project_id=project.project_id), change)
    preview = workbook.preview(db, data=data, project_id=project.project_id, operator="synthetic", authorize=lambda: None)
    receipts.update_receipt(db, receipt_id=receipt["receipt_id"], expected_version=1,
        updates={"note": "网页新值"}, reason="并发", operated_by="synthetic")
    db.commit()
    with pytest.raises(receipts.ReturnReceiptConflict):
        workbook.apply(db, data=data, project_id=project.project_id, operator="synthetic", digest=preview["digest"], authorize=lambda: None)
    db.rollback()
    row = db.get(MaintenanceRkdReturnLine, receipt["receipt_id"], populate_existing=True)
    assert row.qty == 2 and row.note == "网页新值" and row.version == 2


def test_capacity_applies_reductions_before_increases_and_explicit_void(db):
    project = _project(db, project_id=str(uuid4()))
    demand = _wbdd(db, project=project, demand_qty=10)
    for qty in [6, 4]:
        receipts.register_receipt(db, project_id=project.project_id, pn="PN", qty=qty,
                                  wbdd_no=demand.order_no, operated_by="synthetic")
    db.commit()
    def transfer(wb):
        ws = wb[workbook.SHEET]
        # Increase whichever row is first in Excel; reduce the later row.
        for r, delta in [(2, 1), (3, -1)]:
            change(wb, row=r, value=int(Decimal(ws.cell(r, 7).value)) + delta)
    data = edit(workbook.build(db, project_id=project.project_id), transfer)
    _, result = apply_service(db, project, data)
    assert result["updates"] == 2
    assert db.scalar(select(func.sum(MaintenanceRkdReturnLine.qty))) == 10
    def void(wb):
        wb[workbook.SHEET].cell(2, 6, "作废"); wb[workbook.SHEET].cell(2, 12, "重复登记")
    _, result = apply_service(db, project, edit(workbook.build(db, project_id=project.project_id), void))
    assert result["voids"] == 1


def test_invalid_later_row_rolls_back_preview_all_changes(db):
    project, first = seed(db)
    receipts.register_receipt(db, project_id=project.project_id, pn="PN2", qty=2,
                              serial_numbers=["S1", "S2"], operated_by="synthetic")
    db.commit()
    def invalid(wb):
        for r in [2, 3]: change(wb, row=r, value=3)
    data = edit(workbook.build(db, project_id=project.project_id), invalid)
    with pytest.raises(receipts.ReturnReceiptValidation):
        workbook.preview(db, data=data, project_id=project.project_id, operator="synthetic", authorize=lambda: None)
    db.commit()
    rows = db.scalars(select(MaintenanceRkdReturnLine)).all()
    assert all(r.qty == 2 and r.version == 1 for r in rows)


def test_replay_rechecks_current_authorization(db):
    project, _ = seed(db)
    data = edit(workbook.build(db, project_id=project.project_id), change)
    preview, _ = apply_service(db, project, data)
    def revoked(): raise HTTPException(403, "revoked")
    with pytest.raises(HTTPException):
        workbook.apply(db, data=data, project_id=project.project_id, operator="synthetic", digest=preview["digest"], authorize=revoked)


def test_concurrent_same_file_returns_one_first_receipt(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.db import SessionLocal
    project, receipt = seed(db)
    project_id = project.project_id
    data = edit(workbook.build(db, project_id=project_id), change)
    preview = workbook.preview(db, data=data, project_id=project_id, operator="synthetic", authorize=lambda: None)
    db.rollback()
    start = Barrier(2)
    def apply():
        with SessionLocal() as session:
            start.wait(timeout=10)
            result = workbook.apply(session, data=data, project_id=project_id, operator="synthetic",
                digest=preview["digest"], authorize=lambda: None)
            session.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(apply) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(result["replayed"] for result in results) == [False, True]
    assert results[0]["rows"] == results[1]["rows"]
    assert db.get(MaintenanceRkdReturnLine, receipt["receipt_id"], populate_existing=True).version == 2
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_type == workbook.ENTITY)) == 1


def test_api_stable_gate_permissions_preview_and_atomic_failure(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "maintenance_beta_enabled", False)
    monkeypatch.setattr(get_settings(), "maintenance_boss_dashboard_enabled", True)
    project, _ = seed(db)
    client = _client(db, username="workbook-admin", role="admin")
    url = f"/api/maintenance/projects/stable/{project.project_id}/return-receipt-workbook"
    response = client.get(url + ".xlsx"); assert response.status_code == 200, response.text
    data = edit(response.content, change)
    files = {"file": ("edit.xlsx", data)}
    assert client.post(url + "/apply", files=files).status_code == 422
    preview = client.post(url + "/validate", files=files)
    assert preview.status_code == 200, preview.text
    done = client.post(url + "/apply", files=files, data={"digest": preview.json()["digest"]})
    assert done.status_code == 200, done.text
    other = _client(db, username="workbook-readonly", overrides={"page_maintenance": True})
    assert other.get(url + ".xlsx").status_code == 403
    assert other.post(url + "/validate", files=files).status_code == 403
    outside = _client(db, username="workbook-outside", overrides={
        "page_maintenance": True, "action_maintenance_bad_return_manage": True})
    assert outside.get(url + ".xlsx").status_code == 403
    assert outside.post(url + "/validate", files=files).status_code == 403
    prior_scope_token = workbook._confirmation(workbook.parse(data, project_id=project.project_id), "workbook-outside")
    assert outside.post(url + "/apply", files=files, data={"digest": prior_scope_token}).status_code == 403
    monkeypatch.setattr(get_settings(), "maintenance_boss_dashboard_enabled", False)
    assert client.get(url + ".xlsx").status_code == 404


def test_export_retains_literal_formula_like_text_and_empty_roundtrip(db):
    project, _ = seed(db, note="=SUM(1,2)")
    data = workbook.build(db, project_id=project.project_id)
    wb = load_workbook(io.BytesIO(data), data_only=False)
    assert wb[workbook.SHEET].cell(2, 11).data_type == "s"
    assert wb[workbook.SHEET].protection.sheet is True
    assert wb[workbook.SHEET].cell(2, 1).protection.locked is True
    assert wb[workbook.SHEET].cell(2, 11).protection.locked is False
    assert workbook.parse(data, project_id=project.project_id)["operations"] == []
    empty = _project(db, project_id=str(uuid4()))
    assert workbook.parse(workbook.build(db, project_id=empty.project_id), project_id=empty.project_id)["operations"] == []


def test_apply_capacity_drift_rolls_back_prior_valid_row_and_audits(db, monkeypatch):
    from app.config import get_settings
    from app.models.maintenance import FMaintenanceLine
    monkeypatch.setattr(get_settings(), "maintenance_boss_dashboard_enabled", True)
    project, first = seed(db)
    demand = _wbdd(db, project=project, demand_qty=10)
    linked = receipts.register_receipt(db, project_id=project.project_id, pn="LINKED", qty=2,
        wbdd_no=demand.order_no, operated_by="synthetic")
    db.commit()
    def changes(wb):
        for r in range(2, 4):
            if wb[workbook.SHEET].cell(r, 1).value == first["receipt_id"]:
                change(wb, row=r, col=11, value="must roll back")
            else:
                change(wb, row=r, value=3)
    data = edit(workbook.build(db, project_id=project.project_id), changes)
    client = _client(db, username="atomic-admin", role="admin")
    url = f"/api/maintenance/projects/stable/{project.project_id}/return-receipt-workbook"
    files = {"file": ("edit.xlsx", data)}
    preview = client.post(url + "/validate", files=files)
    assert preview.status_code == 200, preview.text
    line = db.scalar(select(FMaintenanceLine).where(FMaintenanceLine.order_id == demand.id))
    line.qty = 2
    db.commit()
    audit_count = db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit))
    response = client.post(url + "/apply", files=files, data={"digest": preview.json()["digest"]})
    assert response.status_code in (409, 422), response.text
    db.expire_all()
    first_row = db.get(MaintenanceRkdReturnLine, first["receipt_id"])
    linked_row = db.get(MaintenanceRkdReturnLine, linked["receipt_id"])
    assert first_row.version == linked_row.version == 1
    assert first_row.note is None and linked_row.qty == 2
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit)) == audit_count


@pytest.mark.parametrize("revocation, expected", [("action", 403), ("inactive", 401), ("token", 401)])
@pytest.mark.parametrize("replay", [False, True])
def test_lock_wait_rechecks_current_account_before_first_apply_or_replay(db, monkeypatch, revocation, expected, replay):
    from app.config import get_settings
    from app.db import SessionLocal
    from app.models.system import SysUser
    from tests.test_return_receipt_hardening import _grant
    monkeypatch.setattr(get_settings(), "maintenance_boss_dashboard_enabled", True)
    project, receipt = seed(db)
    username = "lock-wait-operator"
    client = _client(db, username=username, overrides={
        "page_maintenance": True, "action_maintenance_bad_return_manage": True})
    _grant(db, project.project_id, username)
    data = edit(workbook.build(db, project_id=project.project_id), change)
    url = f"/api/maintenance/projects/stable/{project.project_id}/return-receipt-workbook"
    files = {"file": ("edit.xlsx", data)}
    preview = client.post(url + "/validate", files=files)
    assert preview.status_code == 200, preview.text
    confirmation = {"digest": preview.json()["digest"]}
    if replay:
        applied = client.post(url + "/apply", files=files, data=confirmation)
        assert applied.status_code == 200, applied.text
    original = receipts.lock_receipt_projects
    revoked = False
    def revoke_after_lock(session, project_ids):
        nonlocal revoked
        original(session, project_ids)
        if not revoked:
            revoked = True
            with SessionLocal() as updating:
                user = updating.scalar(select(SysUser).where(SysUser.username == username))
                if revocation == "action":
                    user.perm_overrides = {**user.perm_overrides, "action_maintenance_bad_return_manage": False}
                elif revocation == "inactive":
                    user.is_active = False
                else:
                    user.token_version += 1
                updating.commit()
    monkeypatch.setattr(receipts, "lock_receipt_projects", revoke_after_lock)
    denied = client.post(url + "/apply", files=files, data=confirmation)
    assert denied.status_code == expected, denied.text
    db.expire_all()
    row = db.get(MaintenanceRkdReturnLine, receipt["receipt_id"])
    assert row.version == (2 if replay else 1)
