"""WBDD 上传回执区分同键命中与有效业务字段实际更新。"""
from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from app.etl import loader
from app.models.maintenance import FMaintenanceLine
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysAuditLog, SysImportBatch
from app.services import maintenance_project_operations as ops
from tests import factories as f


def _load(db, lines, *, mode="upsert"):
    batch = SysImportBatch(
        filename="feedback.xlsx", file_type="maintenance",
        file_hash=uuid4().hex, status="success",
    )
    db.add(batch)
    db.flush()
    result = f.maintenance_result(
        {"feedback-order": f.maintenance_head("feedback-order")}, lines,
    )
    report = loader.load(
        db, result, batch.id, date(2026, 10, 15), mode=mode,
        operated_by="feedback-test", audit_overwrites=True,
    )
    db.commit()
    return report


def _line(raw_id, *, qty="2", description=None):
    return f.maintenance_line(
        "feedback-order", raw_id, "PN-FEEDBACK", qty=qty,
        description=description,
    )


def test_loader_counts_actual_changes_unchanged_and_blocked_fields(db):
    _load(db, [_line("changed"), _line("unchanged"), _line("protected")])
    protected = db.scalar(select(FMaintenanceLine).where(
        FMaintenanceLine.raw_line_id == "protected",
    ))
    protected.qty = Decimal("7")
    protected.manual_override = {"qty": {"value": "7", "source_value": "2"}}
    db.commit()

    report = _load(db, [
        _line("changed", qty="3"),
        _line("unchanged"),
        _line("protected", qty="5"),
        _line("new"),
    ])

    assert report["fact_rows_inserted"] == 1
    assert report["fact_rows_updated"] == 3  # 旧协议：既有同键行数
    assert report["fact_rows_skipped"] == 0
    assert report["fact_rows_semantic_updated"] == 1
    assert report["fact_rows_unchanged"] == 2
    assert report["fact_rows_protected"] == 1
    assert report["protected_field_counts"] == {"qty": 1}
    assert report["fact_rows_legacy_review"] == 0
    db.refresh(protected)
    assert protected.qty == Decimal("7")


def test_protection_can_overlap_real_update_and_audit_records_effective_values(db):
    _load(db, [_line("protected-and-changed")])
    line = db.scalar(select(FMaintenanceLine))
    line.qty = Decimal("7")
    line.manual_override = {"qty": {"value": "7", "source_value": "2"}}
    project = MaintenanceProject(
        project_id="feedback-project", project_code="FEEDBACK",
        display_name="上传反馈项目", lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id="feedback-assignment", project_id=project.project_id,
        source_order_id="feedback-order", is_active=True, created_by="test",
    ))
    state = ops.get_or_create_workbook_state(db, project_id=project.project_id)
    base_revision = state.revision
    db.commit()

    report = _load(db, [_line("protected-and-changed", qty="5", description="源描述更新")])

    assert report["fact_rows_semantic_updated"] == 1
    assert report["fact_rows_unchanged"] == 0
    assert report["fact_rows_protected"] == 1
    assert report["protected_field_counts"] == {"qty": 1}
    assert report["workbook_projects_bumped"] == 1
    db.refresh(state)
    assert state.revision == base_revision + 1
    db.refresh(line)
    assert line.qty == Decimal("7")
    assert line.description == "源描述更新"
    audit = db.scalar(select(SysAuditLog).where(
        SysAuditLog.entity_type == "import_overwrite",
        SysAuditLog.entity_id == line.id,
    ).order_by(SysAuditLog.id.desc()))
    assert Decimal(audit.after_json["qty"]) == Decimal("7")
    assert audit.after_json["description"] == "源描述更新"

    # 已有保护标记但源值相同，没有实际挡住字段，也不产生新审计或 bump。
    noop = _load(db, [_line("protected-and-changed", qty="7", description="源描述更新")])
    assert noop["fact_rows_updated"] == 1
    assert noop["fact_rows_semantic_updated"] == 0
    assert noop["fact_rows_unchanged"] == 1
    assert noop["fact_rows_protected"] == 0
    assert noop["protected_field_counts"] == {}
    assert noop["workbook_projects_bumped"] == 0
    db.refresh(state)
    assert state.revision == base_revision + 1
    assert db.scalars(select(SysAuditLog).where(
        SysAuditLog.entity_type == "import_overwrite",
        SysAuditLog.entity_id == line.id,
    )).all() == [audit]


def test_legacy_review_excludes_manually_created_identity(db):
    _load(db, [_line("source-workbook"), _line("manual-line:created")])
    for line in db.scalars(select(FMaintenanceLine)):
        line.edited_source = "workbook_manual"
        line.qty = Decimal("8")
        line.description = "人工描述"
    db.commit()

    report = _load(db, [_line("source-workbook"), _line("manual-line:created")])

    assert report["fact_rows_updated"] == 2
    assert report["fact_rows_semantic_updated"] == 0
    assert report["fact_rows_unchanged"] == 2
    assert report["fact_rows_protected"] == 2
    assert report["protected_field_counts"] == {"qty": 2, "description": 2}
    assert report["fact_rows_legacy_review"] == 1


def test_skip_counts_existing_rows_without_claiming_updates_or_protection(db):
    _load(db, [_line("known")])
    line = db.scalar(select(FMaintenanceLine))
    line.qty = Decimal("7")
    line.manual_override = {"qty": {"value": "7", "source_value": "2"}}
    db.commit()

    report = _load(db, [_line("known", qty="5"), _line("new")], mode="skip")

    assert report["import_mode"] == "skip"
    assert report["fact_rows_inserted"] == 1
    assert report["fact_rows_updated"] == 0
    assert report["fact_rows_skipped"] == 1
    assert report["fact_rows_semantic_updated"] == 0
    assert report["fact_rows_unchanged"] == 0
    assert report["fact_rows_protected"] == 0
    assert report["protected_field_counts"] == {}
    assert report["fact_rows_legacy_review"] == 0
    db.refresh(line)
    assert line.qty == Decimal("7")


def test_pn_identity_group_protection_is_counted_without_false_changes(db):
    _load(db, [_line("pn-protected")])
    line = db.scalar(select(FMaintenanceLine))
    from app.models.dimensions import DimPart

    replacement = DimPart(pn_std="PN-MANUAL", status="active")
    db.add(replacement)
    db.flush()
    line.pn_std = line.pn_raw = "PN-MANUAL"
    line.part_id = replacement.id
    line.manual_override = {"pn_std": {"value": "PN-MANUAL", "source_value": "PN-FEEDBACK"},
                            "pn_raw": {"value": "PN-MANUAL", "source_value": "PN-FEEDBACK"}}
    db.commit()

    report = _load(db, [_line("pn-protected")])

    assert report["fact_rows_semantic_updated"] == 0
    assert report["fact_rows_unchanged"] == 1
    assert report["fact_rows_protected"] == 1
    assert report["protected_field_counts"] == {"part_id": 1, "pn_std": 1, "pn_raw": 1}
    db.refresh(line)
    assert line.part_id == replacement.id
    assert line.pn_std == "PN-MANUAL"
    assert db.scalars(select(SysAuditLog).where(
        SysAuditLog.entity_type == "import_overwrite",
        SysAuditLog.entity_id == line.id,
    )).all() == []


def test_upload_response_and_replay_keep_feedback_and_missing_lines_separate(db, tmp_path):
    from tests.test_maintenance_wbdd_import_api import _client, _upload
    from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook

    client = _client(db, username="feedback-uploader", role="admin")
    rows = make_rows(orders=1, lines_per_order=2)
    for row in rows:
        row["需求单号"] = "WBDD-20261015-0001"
        row["销售订单"] = ""
    first = write_workbook(str(tmp_path / "first.xlsx"), COLUMNS_91, rows)
    response = _upload(client, first)
    assert response.status_code == 200, response.text
    line = db.scalar(select(FMaintenanceLine).where(
        FMaintenanceLine.raw_line_id == "SYN-L001-1",
    ))
    line.qty = Decimal("7")
    line.manual_override = {"qty": {"value": "7", "source_value": "3"}}
    db.commit()
    rows[0]["需求明细.需求数量"] = "5"
    second = write_workbook(str(tmp_path / "second.xlsx"), COLUMNS_91, rows[:1])

    response = _upload(client, second, key="feedback-replay")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["fact_rows_updated"] == 1
    assert payload["fact_rows_semantic_updated"] == 0
    assert payload["fact_rows_unchanged"] == 1
    assert payload["fact_rows_protected"] == 1
    assert payload["protected_field_counts"] == {"qty": 1}
    assert payload["snapshot_diff"]["missing_orders"] == 0
    assert payload["snapshot_diff"]["missing_lines"] == 1
    assert "_wbdd_source_snapshot" not in payload
    replay = _upload(client, second, key="feedback-replay")
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**payload, "replayed": True}


def test_pipeline_feedback_waits_for_concurrent_manual_edit(db, tmp_path):
    """两个真实 PG Session：源上传取全局锁后必须按已提交人工值算回执。"""
    from concurrent.futures import ThreadPoolExecutor
    import threading

    from sqlalchemy import event, text

    from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
    from app.db import SessionLocal
    from app.etl import pipeline
    from app.services import maintenance_demand_manual as manual
    from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook

    rows = make_rows(orders=1, lines_per_order=1)
    path = write_workbook(str(tmp_path / "initial.xlsx"), COLUMNS_91, rows)
    pipeline.run_import(db, path, "initial.xlsx", mode="upsert")
    project = MaintenanceProject(
        project_id="concurrent-feedback", project_code="CONCURRENT-FEEDBACK",
        display_name="并发反馈项目", lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id="concurrent-feedback-assignment", project_id=project.project_id,
        source_order_id="SYN-O001", is_active=True, created_by="test",
    ))
    state = ops.get_or_create_workbook_state(db, project_id=project.project_id)
    db.commit()
    base_revision = state.revision
    rows[0]["需求明细.需求数量"] = "5"
    changed = write_workbook(str(tmp_path / "changed.xlsx"), COLUMNS_91, rows)
    attempted_data_lock = threading.Event()
    acquired_data_lock = threading.Event()

    # 页面真实命令取得 DATA_CHANGE/state/order/line 锁并写保护，暂不提交。
    manual.patch_demand_line(
        db, raw_line_id="SYN-L001-1", updates={"qty": "7"},
        reason="并发人工修正", operated_by="feedback-editor",
    )

    def import_concurrently():
        with SessionLocal() as session:
            connection = session.connection()
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            connection.execute(text("SET LOCAL statement_timeout = '10s'"))

            def observe_data_lock(_conn, _cursor, statement, parameters, _context, _many):
                if "pg_advisory_xact_lock" in statement and DATA_CHANGE_ADVISORY_LOCK_KEY in parameters.values():
                    attempted_data_lock.set()

            def observe_acquired_lock(_conn, _cursor, statement, parameters, _context, _many):
                if "pg_advisory_xact_lock" in statement and DATA_CHANGE_ADVISORY_LOCK_KEY in parameters.values():
                    acquired_data_lock.set()

            event.listen(connection, "before_cursor_execute", observe_data_lock)
            event.listen(connection, "after_cursor_execute", observe_acquired_lock)
            try:
                batch = pipeline.run_import(session, changed, "changed.xlsx", mode="upsert")
                report = dict(batch.report_json)
                session.commit()
                return report
            finally:
                event.remove(connection, "before_cursor_execute", observe_data_lock)
                event.remove(connection, "after_cursor_execute", observe_acquired_lock)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(import_concurrently)
        try:
            assert attempted_data_lock.wait(timeout=5)
            assert not acquired_data_lock.wait(timeout=0.25)
            assert not pending.done()
        finally:
            db.commit()
        report = pending.result(timeout=15)

    assert report["fact_rows_updated"] == 1
    assert report["fact_rows_semantic_updated"] == 0
    assert report["fact_rows_unchanged"] == 1
    assert report["fact_rows_protected"] == 1
    assert report["protected_field_counts"] == {"qty": 1}
    assert report["workbook_projects_bumped"] == 0
    db.refresh(state)
    assert state.revision == base_revision + 1  # 只由人工编辑推进，导入不重复 bump
    line = db.scalar(select(FMaintenanceLine))
    assert line.qty == Decimal("7")
