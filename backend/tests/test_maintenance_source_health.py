"""M4-1：四源 readiness/as_of 状态机（plan v1.3 §2.4）。

铁律 5：各源独立；**未导入显示 not_imported，绝不显示 0**。
"""
import uuid
from datetime import date, datetime, timedelta, timezone

from app.business_time import business_today
from app.etl import pipeline
from app.models.maintenance_ckd_import import (
    MaintenanceCkdHeadRow,
    MaintenanceCkdImportBatch,
)
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
)
from app.services import maintenance_source_health as health
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook


def _ckd_batch(db, *, status="applied", issue_rows=0, order_date=None,
               wbdd_no="WBDD-20260001", category="维保供货"):
    batch = MaintenanceCkdImportBatch(
        batch_id=str(uuid.uuid4()), file_hash="h" * 64, filename="ckd.xlsx",
        idempotency_key=str(uuid.uuid4()), uploaded_by="tester",
        head_rows=1, line_rows=1, issue_rows=issue_rows, status=status,
        applied_by="tester" if status == "applied" else None,
        applied_at=datetime.now(timezone.utc) if status == "applied" else None,
    )
    db.add(batch)
    db.flush()
    db.add(MaintenanceCkdHeadRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, row_no=1,
        order_no="CKD-1", order_date=order_date or date(2026, 7, 20),
        category=category, wbdd_no=wbdd_no, data_status_raw="已生效",
    ))
    db.commit()
    return batch


def _doc_batch(db, doc_type, *, status="applied", issue_rows=0,
               head_date=None, project_id=None):
    batch = MaintenanceDocImportBatch(
        batch_id=str(uuid.uuid4()), doc_type=doc_type, file_hash="h" * 64,
        filename=f"{doc_type}.xlsx", idempotency_key=str(uuid.uuid4()),
        uploaded_by="tester", head_rows=1, line_rows=1, issue_rows=issue_rows,
        status=status,
        applied_by="tester" if status == "applied" else None,
        applied_at=datetime.now(timezone.utc) if status == "applied" else None,
    )
    db.add(batch)
    db.flush()
    db.add(MaintenanceDocHeadRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, row_no=1,
        raw_json={}, head_no="RKD-1",
        head_date=head_date or date(2026, 7, 25),
        category="维保拆旧返件", data_status="已生效", project_id=project_id,
    ))
    db.commit()
    return batch


def test_all_sources_not_imported_on_empty_db(db):
    result = health.source_health(db)
    for key in health.SOURCE_KEYS:
        source = result["sources"][key]
        assert source["readiness"] == "not_imported", key
        # 未导入绝不显示 0：计数键一律 None
        assert source["as_of"] is None and source["unlinked_rows"] is None, key


def test_wbdd_ready_after_import(db, tmp_path):
    path = write_workbook(str(tmp_path / "w.xlsx"), COLUMNS_91,
                          make_rows(orders=1, lines_per_order=1))
    pipeline.run_import(db, path, "w.xlsx", uploaded_by="tester", mode="upsert")
    db.commit()
    sources = health.source_health(db)["sources"]
    assert sources["wbdd"]["readiness"] == "ready"
    assert sources["wbdd"]["as_of"] == "2026-07-15"
    # 其余三源仍是未导入（源分离，铁律 5）
    assert sources["ckd"]["readiness"] == "not_imported"
    assert sources["return_order"]["readiness"] == "not_imported"
    assert sources["rkd_inbound"]["readiness"] == "not_imported"


def test_pending_batch_does_not_count_as_imported(db):
    _ckd_batch(db, status="pending")
    assert health.source_health(db)["sources"]["ckd"]["readiness"] == "not_imported"


def test_ckd_ready_and_as_of_from_head_rows(db):
    _ckd_batch(db, order_date=date(2026, 7, 20))
    source = health.source_health(db)["sources"]["ckd"]
    assert source["readiness"] == "ready"
    assert source["as_of"] == "2026-07-20"
    assert source["unlinked_rows"] == 0


def test_ckd_partial_when_issue_rows(db):
    _ckd_batch(db, issue_rows=2)
    assert health.source_health(db)["sources"]["ckd"]["readiness"] == "partial"


def test_ckd_partial_when_wbdd_missing(db):
    """CKD 无项目列：无 WBDD 单号即无法归属 → partial + unlinked 计数。"""
    _ckd_batch(db, wbdd_no=None)
    source = health.source_health(db)["sources"]["ckd"]
    assert source["readiness"] == "partial"
    assert source["unlinked_rows"] == 1


def test_doc_sources_partial_when_project_unresolved(db):
    _doc_batch(db, "rkd_inbound", project_id=None)
    _doc_batch(db, "return_order", project_id=None)
    sources = health.source_health(db)["sources"]
    assert sources["rkd_inbound"]["readiness"] == "partial"
    assert sources["rkd_inbound"]["unlinked_rows"] == 1
    assert sources["return_order"]["readiness"] == "partial"


def test_stale_is_display_state_not_stored(db):
    """as_of 距今 > 45 天（F2 默认）→ stale；仍带 as_of 与计数。"""
    old = business_today() - timedelta(days=health.STALE_DAYS + 1)
    _ckd_batch(db, order_date=old)
    source = health.source_health(db)["sources"]["ckd"]
    assert source["readiness"] == "stale"
    assert source["as_of"] == old.isoformat()


def test_fresh_as_of_stays_ready(db):
    fresh = business_today() - timedelta(days=health.STALE_DAYS - 1)
    _ckd_batch(db, order_date=fresh)
    assert health.source_health(db)["sources"]["ckd"]["readiness"] == "ready"
