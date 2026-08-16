"""M4-2/M4-4：项目＋PN 聚合读模型与自报并排（plan v1.3 §2.4）。"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.etl import pipeline
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_ckd_import import (
    MaintenanceCkdHeadRow,
    MaintenanceCkdImportBatch,
    MaintenanceCkdLineRow,
)
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
    MaintenanceDocLineRow,
    MaintenanceRkdReturnLine,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_boss_facts as facts
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook


def _project(db, code="合成项目A") -> MaintenanceProject:
    proj = MaintenanceProject(
        project_id=str(uuid.uuid4()), project_code=code, display_name=code,
        lifecycle_status="missing")
    db.add(proj)
    db.commit()
    return proj


def _wbdd(db, tmp_path, *, project="合成项目A", orders=1):
    path = write_workbook(str(tmp_path / f"{uuid.uuid4().hex}.xlsx"), COLUMNS_91,
                          make_rows(orders=orders, lines_per_order=1, project=project))
    pipeline.run_import(db, path, "w.xlsx", uploaded_by="tester", mode="upsert")
    db.commit()
    return db.execute(select(FMaintenanceOrder)).scalars().all()


def _assign(db, order, project):
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid.uuid4()), source_order_id=order.raw_order_id,
        project_id=project.project_id, is_active=True, version=1,
        created_by="tester"))
    db.commit()


def _ckd(db, *, wbdd_no, pn="PN-SYN-0011", qty="4", category="维保供货",
         data_status="已生效", ckd_no="CKD-1", line_id=None, row_no=1):
    batch = MaintenanceCkdImportBatch(
        batch_id=str(uuid.uuid4()), file_hash="h" * 64, filename="ckd.xlsx",
        idempotency_key=str(uuid.uuid4()), uploaded_by="tester",
        head_rows=1, line_rows=1, issue_rows=0, status="applied",
        applied_by="tester", applied_at=datetime.now(timezone.utc))
    db.add(batch)
    db.flush()
    head = MaintenanceCkdHeadRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, row_no=1,
        order_no=ckd_no, order_date=date(2026, 7, 20), category=category,
        wbdd_no=wbdd_no, data_status_raw=data_status)
    db.add(head)
    db.flush()
    db.add(MaintenanceCkdLineRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, head_row_id=head.row_id,
        row_no=row_no, pn_raw=pn, out_qty=Decimal(qty), data_id_raw=line_id))
    db.commit()


def _return_order(db, *, project_id, pn="PN-SYN-0011", qty="1", test_result="成品"):
    batch = MaintenanceDocImportBatch(
        batch_id=str(uuid.uuid4()), doc_type="return_order", file_hash="h" * 64,
        filename="ret.xlsx", idempotency_key=str(uuid.uuid4()), uploaded_by="tester",
        head_rows=1, line_rows=1, issue_rows=0, status="applied",
        applied_by="tester", applied_at=datetime.now(timezone.utc))
    db.add(batch)
    db.flush()
    head = MaintenanceDocHeadRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, row_no=1, raw_json={},
        head_no="RET-1", head_date=date(2026, 7, 26), category="维保返件",
        data_status="已生效", project_id=project_id)
    db.add(head)
    db.flush()
    db.add(MaintenanceDocLineRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, head_row_id=head.row_id,
        row_no=1, raw_json={}, line_key="L1", pn=pn, qty=Decimal(qty),
        test_result=test_result))
    db.commit()


def _rkd(db, *, project_id, pn="PN-SYN-0011", qty="2"):
    batch = MaintenanceDocImportBatch(
        batch_id=str(uuid.uuid4()), doc_type="rkd_inbound", file_hash="h" * 64,
        filename="rkd.xlsx", idempotency_key=str(uuid.uuid4()), uploaded_by="tester",
        head_rows=1, line_rows=1, issue_rows=0, status="applied",
        applied_by="tester", applied_at=datetime.now(timezone.utc))
    db.add(batch)
    db.flush()
    head = MaintenanceDocHeadRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, row_no=1, raw_json={},
        head_no="RKD-1", head_date=date(2026, 7, 28), category="维保拆旧返件",
        data_status="已生效", project_id=project_id)
    db.add(head)
    db.flush()
    db.add(MaintenanceRkdReturnLine(
        rkd_line_id=str(uuid.uuid4()), batch_id=batch.batch_id,
        head_row_id=head.row_id, project_id=project_id, head_no="RKD-1",
        source_ref=f"rkd:{uuid.uuid4().hex}", pn=pn, qty=Decimal(qty),
        test_result="坏品"))
    db.commit()


def test_three_sources_aggregate_by_project_and_pn(db, tmp_path):
    proj = _project(db)
    order = _wbdd(db, tmp_path)[0]
    _assign(db, order, proj)
    _ckd(db, wbdd_no=order.order_no, qty="4")
    _return_order(db, project_id=proj.project_id, qty="1")
    _rkd(db, project_id=proj.project_id, qty="2")

    result = facts.project_pn_facts(db)
    assert set(result) == {proj.project_id}
    pn_map = result[proj.project_id]
    assert set(pn_map) == {"PN-SYN-0011"}
    assert pn_map["PN-SYN-0011"] == {
        "shipped": Decimal("4.000"),
        "returned_good": Decimal("1.000"),
        "returned_bad": Decimal("2.000"),
    }


def test_return_order_bad_lines_are_not_good_returns(db, tmp_path):
    """铁律 5：返库单只有成品算未用件收回；坏品属 RKD 口径，不进 returned_good。"""
    proj = _project(db)
    _return_order(db, project_id=proj.project_id, qty="5", test_result="坏品")
    result = facts.project_pn_facts(db)
    assert result == {} or result[proj.project_id]["PN-SYN-0011"]["returned_good"] is None


def test_ckd_without_assignment_is_not_attributed(db, tmp_path):
    """CKD 无项目名列：WBDD 未归属 → 不摊进任何项目（进 unlinked，不丢不猜）。"""
    _project(db)
    order = _wbdd(db, tmp_path)[0]
    _ckd(db, wbdd_no=order.order_no)     # 未 assign
    assert facts.project_pn_facts(db) == {}


def test_non_maintenance_ckd_category_excluded(db, tmp_path):
    proj = _project(db)
    order = _wbdd(db, tmp_path)[0]
    _assign(db, order, proj)
    _ckd(db, wbdd_no=order.order_no, category="销售出库")
    assert facts.project_pn_facts(db) == {}


def test_project_totals_roll_up_pn_level(db, tmp_path):
    proj = _project(db)
    order = _wbdd(db, tmp_path)[0]
    _assign(db, order, proj)
    # 同一出库单的两行：序号必须不同（真实导出如此），否则按业务键 (单号,序号)
    # 会被正确地判为同一行的重传
    _ckd(db, wbdd_no=order.order_no, pn="PN-A", qty="3", row_no=1)
    _ckd(db, wbdd_no=order.order_no, pn="PN-B", qty="2", ckd_no="CKD-2", row_no=1)
    totals = facts.project_totals(db)
    assert totals[proj.project_id]["shipped"] == Decimal("5.000")
    assert totals[proj.project_id]["returned_good"] is None   # 未导入不是 0


def test_self_report_and_facts_side_by_side_without_verdict(db, tmp_path):
    """M4-4：自报四列与事实并排；响应中**不存在**任何 mismatch/diff 键（铁律 3）。"""
    proj = _project(db)
    order = _wbdd(db, tmp_path)[0]
    _assign(db, order, proj)
    _ckd(db, wbdd_no=order.order_no, qty="99")   # 与自报「已发货数量=2」显著不同
    payload = facts.order_self_report_and_facts(db, source_order_id=order.raw_order_id)
    assert payload["self_report"]["head_shipped_qty"] == Decimal("2.000")
    assert payload["facts"]["shipped"] == Decimal("99.000")
    flat = str(payload)
    assert "mismatch" not in flat and "diff" not in flat
    assert not any("mismatch" in k or "diff" in k for k in payload)


def test_line_evidence_returns_status_columns_verbatim(db, tmp_path):
    """铁律 3：流转状态列原样返回，空值保持 None（不补 0、不标注）。"""
    order = _wbdd(db, tmp_path)[0]
    rows = facts.line_evidence(db, source_order_id=order.raw_order_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["supplied_qty"] == Decimal("2.000")
    assert row["pending_supply_qty"] == Decimal("1.000")
    assert row["consumed_qty"] is None          # fixture 中「领用数量」为空
    assert row["return_old_part"] == "是"


def test_duplicate_order_no_does_not_double_count_shipments(db, tmp_path):
    """扇出防回归：order_no 无唯一约束，同号双单不得让实发翻倍。

    f_maintenance_order.order_no 只有普通索引（幂等键是 raw_order_id）。若氚云导出
    出现同一需求单号的两个数据ID 且都有活跃归属，朴素 join 会把 CKD 明细数量重复
    计入——「精确对平不允许 ±2%」下这是硬缺陷。
    """
    proj = _project(db)
    order = _wbdd(db, tmp_path)[0]
    _assign(db, order, proj)
    # 同一 order_no 的第二个数据ID（更正单/重复导出），同样归属该项目
    twin = FMaintenanceOrder(
        raw_order_id="SYN-O001-TWIN", order_no=order.order_no,
        order_date=order.order_date, project_raw=order.project_raw,
        project_std=order.project_std, data_status="已生效",
        import_batch_id=order.import_batch_id,
    )
    db.add(twin)
    db.commit()
    _assign(db, twin, proj)

    _ckd(db, wbdd_no=order.order_no, qty="4")
    totals = facts.project_totals(db)
    # 实发必须仍是 4，而不是 8
    assert totals[proj.project_id]["shipped"] == Decimal("4.000")


def test_ambiguous_order_no_across_projects_is_excluded_not_guessed(db, tmp_path):
    """歧义 fail-closed：同号映射到两个不同项目时整体排除，绝不静默挑一个摊数。"""
    proj_a, proj_b = _project(db, "项目A"), _project(db, "项目B")
    order = _wbdd(db, tmp_path)[0]
    _assign(db, order, proj_a)
    twin = FMaintenanceOrder(
        raw_order_id="SYN-O001-TWIN2", order_no=order.order_no,
        order_date=order.order_date, project_raw=order.project_raw,
        project_std=order.project_std, data_status="已生效",
        import_batch_id=order.import_batch_id,
    )
    db.add(twin)
    db.commit()
    _assign(db, twin, proj_b)

    _ckd(db, wbdd_no=order.order_no, qty="9")
    totals = facts.project_totals(db)
    # 两个项目都不得拿到这 9（宁可计未关联，也不猜）
    assert totals.get(proj_a.project_id, {}).get("shipped") is None
    assert totals.get(proj_b.project_id, {}).get("shipped") is None


def test_voided_shipment_is_not_counted_as_actual_delivery(db, tmp_path):
    """作废/草稿发货单不是实发事实——与导入 apply 的「只放行已生效」判定同形。"""
    proj = _project(db)
    order = _wbdd(db, tmp_path)[0]
    _assign(db, order, proj)
    _ckd(db, wbdd_no=order.order_no, qty="6", data_status="已取消")
    assert facts.project_totals(db).get(proj.project_id, {}).get("shipped") is None


def test_same_shipment_in_two_applied_batches_is_not_double_counted(db, tmp_path):
    """跨批次去重：同一发货明细出现在两个 applied 批次（换幂等键重传/周期重叠）
    时不得翻倍——front_stock 账本按 source_ref 幂等，读侧必须与之一致。"""
    proj = _project(db)
    order = _wbdd(db, tmp_path)[0]
    _assign(db, order, proj)
    _ckd(db, wbdd_no=order.order_no, qty="5", ckd_no="CKD-DUP", line_id="L-1")
    _ckd(db, wbdd_no=order.order_no, qty="5", ckd_no="CKD-DUP", line_id="L-1")
    assert facts.project_totals(db)[proj.project_id]["shipped"] == Decimal("5.000")
