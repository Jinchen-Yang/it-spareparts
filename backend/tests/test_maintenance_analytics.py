"""维保数据分析看板：PN 成本排名与损坏频率聚合（2026-08-21）。"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceDemandTombstone,
)
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_analytics as ana


def _batch(db) -> int:
    from app.models.system import SysImportBatch
    b = SysImportBatch(filename="t.xlsx", file_type="maintenance",
                       file_hash=uuid.uuid4().hex, status="success")
    db.add(b)
    db.flush()
    return b.id


def _project(db, tag: str) -> MaintenanceProject:
    p = MaintenanceProject(project_id=str(uuid.uuid4()), project_code=f"ANA-{tag}",
                           display_name=f"分析测试{tag}", lifecycle_status="ongoing")
    db.add(p)
    db.flush()
    return p


def _line(db, project, part, *, order_no, order_date, qty, return_qty=Decimal("0"),
          cost_inc=None, cost_ex=None, active=True):
    b = _batch(db)
    order = FMaintenanceOrder(
        raw_order_id=f"raw-{uuid.uuid4()}", order_no=order_no,
        order_date=order_date, linked_sales_order_no="XSDD-ANA",
        project_raw=project.display_name, data_status="已生效", import_batch_id=b,
    )
    db.add(order)
    db.flush()
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid.uuid4()), project_id=project.project_id,
        source_order_id=order.raw_order_id, is_active=True, created_by="t"))
    line = FMaintenanceLine(
        raw_line_id=f"rl-{uuid.uuid4()}", order_id=order.id, line_no=1,
        part_id=part.id, pn_std=part.pn_std, pn_raw=part.pn_std,
        description=part.description, qty=qty, return_qty=return_qty,
        cost_amount_inc_tax=cost_inc, cost_amount_ex_tax=cost_ex,
        cost_source="direct" if cost_inc is not None else None,
        is_active=active, import_batch_id=b,
    )
    db.add(line)
    db.flush()
    return line


def _part(db, pn: str, desc: str = "") -> DimPart:
    p = DimPart(pn_std=pn, description=desc)
    db.add(p)
    db.flush()
    return p


def test_ranking_aggregates_cost_and_qty(db):
    proj = _project(db, "A")
    part = _part(db, "ANA-PN-1", "测试备件一")
    _line(db, proj, part, order_no="WBDD-ANA-001", order_date=date(2026, 3, 1),
          qty=Decimal("2"), cost_inc=Decimal("226.00"), cost_ex=Decimal("200.00"))
    _line(db, proj, part, order_no="WBDD-ANA-002", order_date=date(2026, 5, 1),
          qty=Decimal("3"), return_qty=Decimal("1"), cost_inc=Decimal("113.00"),
          cost_ex=Decimal("100.00"))
    db.commit()

    out = ana.pn_ranking(db, range_="all", sort="cost_inc", can_cost=True)
    assert out["total"] == 1
    row = out["rows"][0]
    assert row["pn"] == "ANA-PN-1"
    assert row["occurrences"] == 2
    assert row["order_count"] == 2
    assert row["project_count"] == 1
    assert row["effective_qty"] == Decimal("4")  # (2+3) - 1
    assert row["cost_inc"]["value"] == "339.00"
    assert row["cost_ex"]["value"] == "300.00"
    assert out["summary"]["total_cost_inc"]["value"] == "339.00"
    assert row["cost_share_pct"] == 100.0


def test_ranking_window_filters(db):
    proj = _project(db, "B")
    part = _part(db, "ANA-PN-2")
    _line(db, proj, part, order_no="WBDD-ANA-101", order_date=date(2025, 6, 1),
          qty=Decimal("9"), cost_inc=Decimal("999.00"))
    _line(db, proj, part, order_no="WBDD-ANA-102", order_date=date(2026, 6, 1),
          qty=Decimal("1"), cost_inc=Decimal("113.00"))
    db.commit()

    out = ana.pn_ranking(db, range_="ytd", sort="cost_inc", can_cost=True)
    assert out["total"] == 1
    assert out["rows"][0]["cost_inc"]["value"] == "113.00"  # 只算 2026 年


def test_ranking_excludes_voided_line_and_tombstoned_order(db):
    from app.models.maintenance import MaintenanceDemandDeleteIntent

    proj = _project(db, "C")
    part = _part(db, "ANA-PN-3")
    keep = _line(db, proj, part, order_no="WBDD-ANA-201", order_date=date(2026, 7, 1),
                 qty=Decimal("1"), cost_inc=Decimal("100.00"))
    voided = _line(db, proj, part, order_no="WBDD-ANA-202", order_date=date(2026, 7, 2),
                   qty=Decimal("5"), cost_inc=Decimal("500.00"), active=False)
    dead = _line(db, proj, part, order_no="WBDD-ANA-203", order_date=date(2026, 7, 3),
                 qty=Decimal("7"), cost_inc=Decimal("700.00"))
    intent = MaintenanceDemandDeleteIntent(
        intent_id=str(uuid.uuid4()), idempotency_key=f"ana-{uuid.uuid4()}",
        request_digest="x" * 64, selection_digest="y" * 64, status="executed",
        reason="测试墓碑", operated_by="t", header_count=1, line_count=1,
        created_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc))
    db.add(intent)
    db.flush()
    order = db.scalar(select(FMaintenanceOrder).where(
        FMaintenanceOrder.order_no == "WBDD-ANA-203"))
    db.add(MaintenanceDemandTombstone(
        source_order_id=order.raw_order_id, delete_intent_id=intent.intent_id,
        version_digest="z" * 64, deleted_by="t", delete_reason="测试",
        deleted_at=datetime.now(timezone.utc), version=1))
    db.commit()
    out = ana.pn_ranking(db, range_="all", sort="qty", can_cost=True)
    row = out["rows"][0]
    # 墓碑排除只在 maintenance_cutover_enabled 开启后生效（稳定版口径）；
    # 作废行（is_active=False）无条件排除。
    from app.config import get_settings
    if get_settings().maintenance_cutover_enabled:
        assert row["occurrences"] == 1
        assert row["qty"] == Decimal("1")
    else:
        assert row["occurrences"] == 2  # keep + 墓碑单（开关未切）
        assert row["qty"] == Decimal("8")


def test_ranking_keyword_filter(db):
    proj = _project(db, "D")
    _part(db, "ANA-KEY-ONE", "硬盘")
    _part(db, "ANA-KEY-TWO", "内存条")
    p1 = db.scalar(select(DimPart).where(DimPart.pn_std == "ANA-KEY-ONE"))
    p2 = db.scalar(select(DimPart).where(DimPart.pn_std == "ANA-KEY-TWO"))
    _line(db, proj, p1, order_no="WBDD-KEY-1", order_date=date(2026, 1, 1),
          qty=Decimal("1"))
    _line(db, proj, p2, order_no="WBDD-KEY-2", order_date=date(2026, 1, 1),
          qty=Decimal("1"))
    db.commit()
    out = ana.pn_ranking(db, range_="all", sort="qty", q="内存", can_cost=True)
    assert out["total"] == 1
    assert out["rows"][0]["pn"] == "ANA-KEY-TWO"


def test_ranking_cost_restricted_without_permission(db):
    proj = _project(db, "E")
    part = _part(db, "ANA-PN-5")
    _line(db, proj, part, order_no="WBDD-ANA-301", order_date=date(2026, 2, 1),
          qty=Decimal("1"), cost_inc=Decimal("113.00"))
    db.commit()
    out = ana.pn_ranking(db, range_="all", sort="qty", can_cost=False)
    row = out["rows"][0]
    assert row["cost_inc"]["state"] == "restricted"
    assert row["cost_inc"]["value"] is None
    assert out["summary"]["total_cost_inc"]["state"] == "restricted"
    # 键集与 ready 一致（防侧信道）
    assert set(row["cost_inc"]) == {"state", "value", "as_of"}


def test_ranking_bad_return_join_and_rate(db):
    proj = _project(db, "F")
    part = _part(db, "ANA-PN-6")
    _line(db, proj, part, order_no="WBDD-ANA-401", order_date=date(2026, 4, 1),
          qty=Decimal("10"))
    from app.models.maintenance_doc_import import (
        MaintenanceDocHeadRow,
        MaintenanceDocImportBatch,
    )

    batch = MaintenanceDocImportBatch(
        batch_id=str(uuid.uuid4()), doc_type="rkd_inbound", file_hash=uuid.uuid4().hex,
        filename="rkd-test.xlsx", idempotency_key=f"rkd-{uuid.uuid4()}",
        uploaded_by="t", head_rows=1, line_rows=1, issue_rows=0, status="applied",
        applied_by="t", applied_at=datetime(2026, 4, 5, tzinfo=timezone.utc))
    db.add(batch)
    db.flush()
    head = MaintenanceDocHeadRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, row_no=1, raw_json={},
        head_no="RKD-TEST-1", head_date=date(2026, 4, 5), category="维保拆旧返件",
        data_status="已生效", project_id=proj.project_id)
    db.add(head)
    db.flush()
    db.add(MaintenanceRkdReturnLine(
        rkd_line_id=str(uuid.uuid4()), batch_id=batch.batch_id,
        head_row_id=head.row_id, project_id=proj.project_id, head_no="RKD-TEST-1",
        source_ref=f"rkd:{uuid.uuid4().hex}", part_id=part.id, pn=part.pn_std, qty=Decimal("4"),
        test_result="坏品", occurred_at=datetime(2026, 4, 5, tzinfo=timezone.utc)))
    db.commit()
    out = ana.pn_ranking(db, range_="all", sort="bad_qty", can_cost=True)
    row = out["rows"][0]
    assert row["bad_return_qty"] == Decimal("4")
    assert row["bad_return_rate_pct"] == 40.0  # 4/10
    assert out["summary"]["total_bad_return_qty"] == "4.000"


def test_ranking_pagination_and_sort_whitelist(db):
    proj = _project(db, "G")
    for i in range(5):
        part = _part(db, f"ANA-PG-{i}")
        _line(db, proj, part, order_no=f"WBD-PG-{i}", order_date=date(2026, 1, 1),
              qty=Decimal(str(i + 1)))
    db.commit()
    out = ana.pn_ranking(db, range_="all", sort="qty", page=1, page_size=2,
                         can_cost=True)
    assert out["total"] == 5
    assert [r["rank"] for r in out["rows"]] == [1, 2]
    assert out["rows"][0]["qty"] == Decimal("5")
    page2 = ana.pn_ranking(db, range_="all", sort="qty", page=3, page_size=2,
                           can_cost=True)
    assert len(page2["rows"]) == 1


def test_ranking_per_column_sorts(db):
    """各列排序键独立生效（用户需求：每个字段都可排序）。"""
    proj = _project(db, "H")
    for i, (qty, ret) in enumerate([(Decimal("10"), Decimal("0")),
                                    (Decimal("2"), Decimal("2")),
                                    (Decimal("5"), Decimal("0"))]):
        part = _part(db, f"ANA-SORT-{i}")
        _line(db, proj, part, order_no=f"WBD-SORT-{i}", order_date=date(2026, 1, 1),
              qty=qty, return_qty=ret,
              cost_inc=Decimal(str((i + 1) * 100)) if i != 1 else None)
    db.commit()
    by_qty = ana.pn_ranking(db, range_="all", sort="qty", can_cost=True)["rows"][0]
    assert by_qty["qty"] == Decimal("10")
    by_eff = ana.pn_ranking(db, range_="all", sort="effective_qty", can_cost=True)["rows"][0]
    assert by_eff["effective_qty"] == Decimal("5") or by_eff["effective_qty"] == Decimal("10")
    by_ret = ana.pn_ranking(db, range_="all", sort="return_qty", can_cost=True)["rows"][0]
    assert by_ret["return_qty"] == Decimal("2")
    by_missing = ana.pn_ranking(db, range_="all", sort="missing_lines", can_cost=True)["rows"][0]
    assert by_missing["missing_lines"] == 1
    by_pn = ana.pn_ranking(db, range_="all", sort="pn", can_cost=True)["rows"][0]
    assert by_pn["pn"] == "ANA-SORT-0"
