"""End-to-end and adversarial contracts for the standard RKD return channel."""

import io
from collections import Counter
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_doc_import import (
    MaintenanceDocImportBatch as Batch,
)
from app.models.maintenance_doc_import import (
    MaintenanceRkdReturnLine as Receipt,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit as Audit,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch, SysRawFile
from app.services import maintenance_return_receipt_import as imports
from app.services import maintenance_return_receipts as ledger
from openpyxl import Workbook
from sqlalchemy import func, select

from tests.test_maintenance_return_receipts_api import _wbdd
from tests.test_site_issue_v2_api import _project


def workbook(
    *,
    qty="2",
    condition="成品",
    status="已生效",
    project="TEST-PROJECT",
    wbdd="",
    machine=False,
    extra=None,
    line_id="DETAIL-1",
    head_id="HEAD-1",
    category="维保拆旧返件",
):
    headers = list(imports._HEAD_FIELDS) + list(imports._LINE_FIELDS)
    wb = Workbook()
    ws = wb.active
    ws.append([f"F00{i:05}" for i in range(len(headers))])
    ws.append(headers)
    raw = {
        "数据ID(不可修改)": head_id,
        "入库单号": "RKD-20260912-0001",
        "入库日期": "2026-09-12",
        "入库类别": category,
        "入库备件/整机": "整机" if machine else "备件",
        "数据状态": status,
        "项目名称": project,
        "维保需求单": wbdd,
        "整机PN": "SERVER",
        "整机描述": "Server",
        "整机测试结果": condition,
        "备件明细.备件PN": "MEMORY",
        "备件明细.备件描述": "Memory",
        "备件明细.入库数量": qty,
        "备件明细.测试结果": condition,
        "备件明细.数据ID(不可修改)": line_id,
        "备件明细.序号": "1",
    }
    ws.append([raw.get(h) for h in headers])
    for row in extra or []:
        ws.append([row.get(h) for h in headers])
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


@pytest.fixture
def project(db):
    project = _project(db, project_id=str(uuid4()))
    project.project_code = project.display_name = "TEST-PROJECT"
    db.commit()
    return project


def preview(db, data, key=None):
    batch, created = imports.enqueue(
        db, data, "synthetic.xlsx", "tester", key or str(uuid4())
    )
    assert created
    imports.run_job(batch.batch_id, 1)
    db.expire_all()
    batch = db.get(Batch, batch.batch_id)
    assert batch.report_json["status"] == "ready", batch.report_json.get("error")
    return batch


def apply(db, batch, **kwargs):
    return imports.apply(
        db,
        batch.batch_id,
        "tester",
        plan_hash=batch.report_json["plan_hash"],
        preview_token=batch.report_json["preview_token"],
        **kwargs,
    )


@pytest.mark.parametrize("condition", ["成品", "坏品", "废品", "", "其他件况"])
def test_all_conditions_archive_apply_and_identical_reupload(db, project, condition):
    data = workbook(condition=condition)
    batch = preview(db, data)
    archive = db.scalar(select(SysRawFile))
    assert Path(archive.storage_path).read_bytes() == data
    assert batch.report_json["summary"]["create"] == 1
    assert apply(db, batch)["applied_lines"] == 1
    again = preview(db, data)
    assert again.report_json["summary"]["unchanged"] == 1
    assert apply(db, again)["applied_lines"] == 0
    assert db.scalar(select(func.count()).select_from(Receipt)) == 1
    assert db.scalar(select(func.count()).select_from(Audit)) == 1
    with pytest.raises(imports.ImportError):
        apply(db, batch)


def test_old_stock_project_only_and_fractional_quantity(db, project):
    batch = preview(db, workbook(qty="1.250", category="旧库退返"))
    assert batch.report_json["rows"][0]["review_required"]
    apply(db, batch)
    row = db.scalar(select(Receipt))
    assert row.qty == Decimal("1.250") and row.review_required
    assert row.source_order_id is None


def test_machine_parent_counts_one_components_preserved(db, project):
    batch = preview(
        db,
        workbook(
            machine=True,
            extra=[
                {
                    "备件明细.备件PN": "DISK",
                    "备件明细.入库数量": "6",
                    "备件明细.数据ID(不可修改)": "DETAIL-2",
                    "备件明细.序号": "2",
                }
            ],
        ),
    )
    rows = batch.report_json["rows"]
    assert [r["kind"] for r in rows] == ["machine", "component", "component"]
    assert all(r["parent_row_key"] == rows[0]["row_key"] for r in rows[1:])
    assert apply(db, batch)["applied_lines"] == 1
    assert db.scalar(select(func.sum(Receipt.qty))) == Decimal(1)


def test_machine_component_correction_updates_evidence_without_extra_count(db, project):
    first = preview(db, workbook(machine=True))
    apply(db, first)
    changed = preview(
        db,
        workbook(
            machine=True,
            extra=[
                {
                    "备件明细.备件PN": "DISK",
                    "备件明细.入库数量": "6",
                    "备件明细.数据ID(不可修改)": "SECOND",
                    "备件明细.序号": "2",
                },
            ],
        ),
    )
    assert changed.report_json["summary"]["change"] == 1
    apply(db, changed, confirm_changes=True, reason="component correction")
    row = db.scalar(select(Receipt))
    assert row.batch_id == changed.batch_id
    assert row.qty == 1 and row.version == 2
    assert len(row.source_payload["source_metadata"]["components"]) == 2


def test_wbdd_assignment_and_conflicting_source_project(db, project):
    order = _wbdd(db, project=project)
    batch = preview(db, workbook(project="", wbdd=order.order_no))
    apply(db, batch)
    assert db.scalar(select(Receipt)).source_order_id == order.raw_order_id
    old_label = preview(
        db, workbook(project="unrecognized", wbdd=order.order_no, line_id="OTHER")
    )
    assert old_label.report_json["summary"]["create"] == 1
    other_project = _project(db, project_id=str(uuid4()))
    conflict = preview(
        db,
        workbook(
            project=other_project.project_code, wbdd=order.order_no, line_id="OTHER"
        ),
    )
    assert conflict.report_json["summary"]["pending"] == 1
    with pytest.raises(imports.ImportError, match="整批拒绝"):
        apply(db, conflict)


def test_changed_source_requires_explicit_correction_with_history(db, project):
    apply(db, preview(db, workbook()))
    batch = preview(db, workbook(qty="3"))
    assert batch.report_json["summary"]["change"] == 1
    with pytest.raises(imports.ImportError, match="明确确认"):
        apply(db, batch)
    db.rollback()
    apply(db, batch, confirm_changes=True, reason="source correction")
    row = db.scalar(select(Receipt))
    assert row.qty == 3 and row.version == 2
    assert db.scalar(select(func.count()).select_from(Audit)) == 2
    latest = db.scalar(select(Audit).order_by(Audit.id.desc()))
    assert latest.after_json["source_evidence"]["batch_id"] == batch.batch_id
    assert latest.before_json["source_evidence"]["batch_id"] != batch.batch_id


def test_old_workbook_preserves_manual_edit_and_void_cannot_revive(db, project):
    data = workbook()
    apply(db, preview(db, data))
    row = db.scalar(select(Receipt))
    ledger.update_receipt(
        db,
        receipt_id=row.rkd_line_id,
        expected_version=1,
        updates={"qty": 9},
        reason="manual",
        operated_by="tester",
    )
    db.commit()
    batch = preview(db, data)
    assert batch.report_json["summary"]["unchanged"] == 1
    apply(db, batch)
    assert db.get(Receipt, row.rkd_line_id).qty == 9
    ledger.void_receipt(
        db,
        receipt_id=row.rkd_line_id,
        expected_version=2,
        reason="void",
        operated_by="tester",
    )
    db.commit()
    blocked = preview(db, data)
    assert blocked.report_json["summary"]["invalid"] == 1


def test_stale_preview_after_manual_edit_is_zero_write(db, project):
    apply(db, preview(db, workbook()))
    pending = preview(db, workbook(qty="3"))
    row = db.scalar(select(Receipt))
    ledger.update_receipt(
        db,
        receipt_id=row.rkd_line_id,
        expected_version=1,
        updates={"qty": 8},
        reason="manual",
        operated_by="tester",
    )
    db.commit()
    with pytest.raises(imports.ImportError) as raised:
        apply(db, pending, confirm_changes=True, reason="correction")
    assert raised.value.code == "stale_preview"
    db.rollback()
    assert db.get(Receipt, row.rkd_line_id).qty == 8


def test_duplicate_native_id_conflict_rejects_whole_batch(db, project):
    batch = preview(
        db,
        workbook(
            extra=[
                {
                    "备件明细.备件PN": "MEMORY",
                    "备件明细.入库数量": "7",
                    "备件明细.数据ID(不可修改)": "DETAIL-1",
                    "备件明细.序号": "2",
                }
            ]
        ),
    )
    assert batch.report_json["summary"]["invalid"] == 2
    with pytest.raises(imports.ImportError):
        apply(db, batch)
    assert db.scalar(select(func.count()).select_from(Receipt)) == 0


def test_queued_cancel_retry_and_old_worker_generation_cannot_finish(db, project):
    batch, _ = imports.enqueue(db, workbook(), "test.xlsx", "tester", "cancel-key-1")
    imports.control(db, batch.batch_id, "cancel")
    imports.run_job(batch.batch_id, 1)
    assert imports._load(db, batch.batch_id).report_json["status"] == "cancelled"
    batch = imports.control(db, batch.batch_id, "retry")
    generation = batch.report_json["generation"]
    imports.run_job(batch.batch_id, generation)
    assert imports._load(db, batch.batch_id).report_json["status"] == "ready"


def test_idempotency_key_different_file_rejected_and_scope_fail_closed(db, project):
    data = workbook()
    batch, _ = imports.enqueue(db, data, "test.xlsx", "tester", "idempotent-1")
    same, created = imports.enqueue(db, data, "renamed.xlsx", "tester", "idempotent-1")
    assert same.batch_id == batch.batch_id and not created
    with pytest.raises(imports.ImportError) as raised:
        imports.enqueue(db, workbook(qty="7"), "test.xlsx", "tester", "idempotent-1")
    assert raised.value.code == "idempotency_conflict"
    db.rollback()
    imports.run_job(batch.batch_id, 1)
    batch = imports._load(db, batch.batch_id)
    with pytest.raises(imports.ImportError) as raised:
        imports.public_job(db, batch, allowed_project_ids=set())
    assert raised.value.status == 403
    with pytest.raises(imports.ImportError) as raised:
        apply(db, batch, allowed_project_ids=set())
    assert raised.value.status == 403


def test_new_sequence_without_parent_rejects_cross_document_inheritance():
    data = workbook(
        extra=[
            {
                "备件明细.备件PN": "SECOND",
                "备件明细.入库数量": "1",
                "备件明细.数据ID(不可修改)": "OTHER",
                "备件明细.序号": "1",
            }
        ]
    )
    with pytest.raises(imports.ImportError, match="缺少主单身份"):
        imports.parse_standard(data)


def test_optional_real_standard_workbook_isolated_parse_and_apply(db, monkeypatch):
    """Opt-in real export; counts only are emitted, no source rows copied to repo."""
    import os

    filename = os.getenv("RETURN_RECEIPT_SAMPLE")
    if not filename:
        pytest.skip("Set RETURN_RECEIPT_SAMPLE for the local private standard export")
    parsed = imports.parse_standard(Path(filename).read_bytes())
    assert parsed["head_rows"] == 12986
    assert parsed["line_rows"] == 107651
    effective = [h for h in parsed["heads"] if h["raw"]["数据状态"] == "已生效"]
    assert sum(h["raw"]["入库类别"] == "维保拆旧返件" for h in effective) == 315
    assert sum(h["raw"]["入库类别"] == "旧库退返" for h in effective) == 13
    # Source labels seed *only this isolated test database*. Production import
    # requires existing independently managed project/order assignments.
    source_batch = SysImportBatch(
        filename="synthetic-dependencies.xlsx",
        file_type="maintenance",
        file_hash=uuid4().hex,
        status="success",
    )
    db.add(source_batch)
    db.flush()
    project_map, order_map = {}, {}
    # A head can omit its project label while sharing a WBDD with another head.
    # Seed that demand's explicit nonempty label, not a synthetic blank fallback.
    demand_names = {}
    for h in effective:
        order_no = imports._clean(h["raw"]["维保需求单"], imports._WBDD_RE)
        if order_no and h["raw"]["项目名称"]:
            demand_names.setdefault(order_no, h["raw"]["项目名称"])
    for h in effective:
        raw = h["raw"]
        order_no = imports._clean(raw["维保需求单"], imports._WBDD_RE)
        name = (
            raw["项目名称"]
            or demand_names.get(order_no)
            or "Synthetic project from assigned demand"
        )
        if name not in project_map:
            project = MaintenanceProject(
                project_id=str(uuid4()),
                project_code=f"SYNTH-{len(project_map)}",
                display_name=name,
                lifecycle_status="ongoing",
                is_active=True,
            )
            db.add(project)
            db.flush()
            project_map[name] = project
        if order_no and order_no not in order_map:
            order = FMaintenanceOrder(
                raw_order_id=str(uuid4()),
                order_no=order_no,
                data_status="已生效",
                import_batch_id=source_batch.id,
            )
            db.add(order)
            db.flush()
            db.add(
                MaintenanceSourceOrderAssignment(
                    assignment_id=str(uuid4()),
                    project_id=project_map[name].project_id,
                    source_order_id=order.raw_order_id,
                    is_active=True,
                    created_by="synthetic-tester",
                )
            )
            order_map[order_no] = order
    db.commit()
    monkeypatch.setattr(imports, "parse_standard", lambda _data: parsed)
    batch = preview(db, Path(filename).read_bytes())
    problems = Counter(
        row["reason"]
        for row in batch.report_json["rows"]
        if row["action"] in ("invalid", "pending")
    )
    # Copying every export label into a separate canonical project creates two
    # conflicts: the same WBDD appears under different source project labels.
    # This is NOT proof of conflicting production assignments (not consulted).
    assert sum(problems.values()) == 2, dict(problems)
    with pytest.raises(imports.ImportError, match="整批拒绝"):
        apply(db, batch)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Receipt)) == 0
    # Explicitly govern the isolated reference data: current WBDD assignments
    # stay authoritative; legacy display labels are retired, not source bytes.
    project_only_names = {
        h["raw"]["项目名称"] for h in effective if not h["raw"]["维保需求单"]
    }
    for name, project in project_map.items():
        if name not in project_only_names:
            project.display_name = "Governed test project " + project.project_code
    db.commit()
    batch = imports.control(db, batch.batch_id, "retry")
    imports.run_job(batch.batch_id, batch.report_json["generation"])
    batch = imports._load(db, batch.batch_id)
    problems = Counter(
        row["reason"]
        for row in batch.report_json["rows"]
        if row["action"] in ("invalid", "pending")
    )
    assert not problems, dict(problems)
    expected_count = sum(
        1 if h["raw"]["入库备件/整机"] == "整机" else len(h["lines"]) for h in effective
    )
    assert apply(db, batch)["applied_lines"] == expected_count
    expected_qty = sum(
        Decimal(1)
        if h["raw"]["入库备件/整机"] == "整机"
        else sum(Decimal(l["raw"]["备件明细.入库数量"]) for l in h["lines"])
        for h in effective
    )
    assert db.scalar(select(func.sum(Receipt.qty))) == expected_qty
    assert db.scalar(select(func.count()).select_from(Audit)) == expected_count
    imports.verify_archive(batch.report_json["storage_path"], batch.file_hash)
    assert (
        imports.hashlib.sha256(Path(filename).read_bytes()).hexdigest()
        == batch.file_hash
    )
    print(
        {
            "real_sample_heads": parsed["head_rows"],
            "real_sample_details": parsed["line_rows"],
            "effective_return_heads": len(effective),
            "initial_reference_conflict_rows": 2,
            "before_governance_receipt_rows": 0,
            "after_reference_governance_receipt_rows": expected_count,
            "after_reference_governance_quantity": str(expected_qty),
            "archive_bytes_unchanged": True,
        }
    )
