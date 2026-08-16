"""M4-1：四源 readiness/as_of 状态机（plan v1.3 §2.4）。

铁律 5：各源独立；**未导入显示 not_imported，绝不显示 0**。
"""
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

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


def _link_wbdd(db, order_no="WBDD-20260001", *, project_code="合成项目A"):
    """建立 wbdd_no → 项目 的完整链路（CKD 就绪的前提；无归属即未关联）。"""
    import uuid as _uuid

    from app.models.maintenance import FMaintenanceOrder
    from app.models.maintenance_project import MaintenanceProject
    from app.models.maintenance_source_assignment import (
        MaintenanceSourceOrderAssignment,
    )
    from app.models.system import SysImportBatch

    batch = SysImportBatch(filename="w.xlsx", file_type="maintenance",
                           file_hash="h" * 64, status="success")
    db.add(batch)
    db.flush()
    order = FMaintenanceOrder(
        raw_order_id=f"RAW-{_uuid.uuid4().hex[:8]}", order_no=order_no,
        order_date=date(2026, 7, 15), data_status="已生效",
        import_batch_id=batch.id)
    project = MaintenanceProject(
        project_id=str(_uuid.uuid4()), project_code=project_code,
        display_name=project_code, lifecycle_status="missing")
    db.add_all([order, project])
    db.flush()
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(_uuid.uuid4()), source_order_id=order.raw_order_id,
        project_id=project.project_id, is_active=True, version=1,
        created_by="tester"))
    db.commit()
    return project


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
    _link_wbdd(db)                       # 有归属才谈得上 ready
    _ckd_batch(db, order_date=date(2026, 7, 20))
    source = health.source_health(db)["sources"]["ckd"]
    assert source["readiness"] == "ready"
    assert source["as_of"] == "2026-07-20"
    assert source["unlinked_rows"] == 0


def test_ckd_partial_when_issue_rows(db):
    _link_wbdd(db)
    _ckd_batch(db, issue_rows=2)
    assert health.source_health(db)["sources"]["ckd"]["readiness"] == "partial"


def test_ckd_partial_when_wbdd_missing(db):
    """CKD 无项目列：无 WBDD 单号即无法归属 → partial + unlinked 计数。"""
    _link_wbdd(db)
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
    _link_wbdd(db)
    old = business_today() - timedelta(days=health.STALE_DAYS + 1)
    _ckd_batch(db, order_date=old)
    source = health.source_health(db)["sources"]["ckd"]
    assert source["readiness"] == "stale"
    assert source["as_of"] == old.isoformat()


def test_fresh_as_of_stays_ready(db):
    _link_wbdd(db)
    fresh = business_today() - timedelta(days=health.STALE_DAYS - 1)
    _ckd_batch(db, order_date=fresh)
    assert health.source_health(db)["sources"]["ckd"]["readiness"] == "ready"


def test_ckd_partial_when_wbdd_present_but_unassigned(db):
    """回归（blocker）：单号有值但尚未人工确认归属 → 事实落不到项目上。

    此前 unlinked 只数「wbdd_no IS NULL」，于是 readiness 误判 ready，看板把完全
    没有关联的实发渲染成一个自信的 0——正是铁律 5 要禁止的。
    """
    _ckd_batch(db, wbdd_no="WBDD-NOT-ASSIGNED")   # 未建归属
    source = health.source_health(db)["sources"]["ckd"]
    assert source["readiness"] == "partial"
    assert source["unlinked_rows"] == 1


def test_voided_ckd_head_not_counted_as_unlinked(db):
    """作废发货单不参与实发，也不该把来源拖成 partial。"""
    _link_wbdd(db)
    _ckd_batch(db, wbdd_no="WBDD-VOID")
    from app.models.maintenance_ckd_import import MaintenanceCkdHeadRow
    from sqlalchemy import update
    db.execute(update(MaintenanceCkdHeadRow)
               .where(MaintenanceCkdHeadRow.wbdd_no == "WBDD-VOID")
               .values(data_status_raw="已取消"))
    db.commit()
    source = health.source_health(db)["sources"]["ckd"]
    assert source["unlinked_rows"] == 0
    assert source["readiness"] == "ready"


def test_ignored_heads_do_not_count_as_unlinked(db):
    """导入 apply 主动忽略的头行不算「未关联」，否则 partial 永不消退。

    RKD 导出里绝大多数是采购入库/销售退货（非返件类），apply 记为 ignored_heads
    并按设计不解析项目。把它们算进 unlinked，一份完全正常的导出会永久停在
    partial + 一个很大的计数——这种不会消退的假警报会把 readiness 训练成噪音，
    真正的 partial 就没人看了。作废/草稿头同理。
    """
    _doc_batch(db, "rkd_inbound", project_id=None)          # 返件类未解析 → 真未关联
    batch = db.execute(
        select(MaintenanceDocImportBatch).where(
            MaintenanceDocImportBatch.doc_type == "rkd_inbound")
    ).scalars().one()
    db.add_all([
        # 非返件类：apply 直接 ignored_heads
        MaintenanceDocHeadRow(
            row_id=str(uuid.uuid4()), batch_id=batch.batch_id, row_no=2,
            raw_json={}, head_no="RKD-BUY", head_date=date(2026, 7, 25),
            category="采购入库", data_status="已生效", project_id=None),
        # 作废头：apply 同样 ignored_heads
        MaintenanceDocHeadRow(
            row_id=str(uuid.uuid4()), batch_id=batch.batch_id, row_no=3,
            raw_json={}, head_no="RKD-VOID", head_date=date(2026, 7, 25),
            category="维保拆旧返件", data_status="已取消", project_id=None),
    ])
    db.commit()
    source = health.source_health(db)["sources"]["rkd_inbound"]
    assert source["unlinked_rows"] == 1, "只有返件类且已生效的未解析头才算未关联"


def test_return_order_voided_head_not_counted_as_unlinked(db):
    _doc_batch(db, "return_order", project_id=None)
    batch = db.execute(
        select(MaintenanceDocImportBatch).where(
            MaintenanceDocImportBatch.doc_type == "return_order")
    ).scalars().one()
    db.add(MaintenanceDocHeadRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, row_no=2, raw_json={},
        head_no="RT-VOID", head_date=date(2026, 7, 25), category="维保拆旧返件",
        data_status="草稿", project_id=None))
    db.commit()
    assert health.source_health(db)["sources"]["return_order"]["unlinked_rows"] == 1
