"""M1-9：对账断言套件（plan v1.3 §2.2）——冻结合成快照下四条精确相等，不允许 ±2%。"""
from decimal import Decimal

from sqlalchemy import func, select

from app.etl import pipeline
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.services import maintenance_cost
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook


def _import(db, tmp_path, rows, name="wbdd.xlsx"):
    path = write_workbook(str(tmp_path / name), COLUMNS_91, rows)
    batch = pipeline.run_import(db, path, name, uploaded_by="tester", mode="upsert")
    db.commit()
    return batch


def test_counts_and_sums_reconcile_exactly(db, tmp_path):
    """①文件头/行计数=DB 计数（含 headless 单列）；②Σ需求数量/Σ需采数量 精确相等。"""
    rows = make_rows(orders=3, lines_per_order=2, headless=2)
    batch = _import(db, tmp_path, rows)
    report = batch.report_json

    file_head_count = 3 + 2                       # 有明细 3 单 + 无明细 2 单
    file_line_count = 3 * 2
    db_orders = db.execute(select(func.count(FMaintenanceOrder.id))).scalar_one()
    db_lines = db.execute(select(func.count(FMaintenanceLine.id))).scalar_one()
    assert db_orders == file_head_count
    assert db_lines == file_line_count
    assert report["orders_inserted"] == file_head_count
    assert report["fact_rows_inserted"] == file_line_count
    assert report["headless_orders"] == 2
    assert report["fact_rows_error"] == 0

    # Σ需求数量（明细级 qty=3/行）与 Σ需采数量（purchase_qty=2/行）精确对平
    sum_qty, sum_purchase = db.execute(
        select(func.sum(FMaintenanceLine.qty),
               func.sum(FMaintenanceLine.purchase_qty))
    ).one()
    assert sum_qty == Decimal(3 * 2 * 3)          # 6 行 × 3
    assert sum_purchase == Decimal(3 * 2 * 2)     # 6 行 × 2


def test_reupload_idempotent_counts_unchanged(db, tmp_path):
    """③幂等：同内容重传后全部计数不变（upsert 覆盖，不新增）。"""
    rows = make_rows(orders=2, lines_per_order=2, headless=1)
    _import(db, tmp_path, rows, name="first.xlsx")
    before = (
        db.execute(select(func.count(FMaintenanceOrder.id))).scalar_one(),
        db.execute(select(func.count(FMaintenanceLine.id))).scalar_one(),
        db.execute(select(func.sum(FMaintenanceLine.qty))).scalar_one(),
    )
    # 同内容、不同文件名（不同 hash 路径也允许 upsert 重放）
    batch2 = _import(db, tmp_path, rows, name="second.xlsx")
    after = (
        db.execute(select(func.count(FMaintenanceOrder.id))).scalar_one(),
        db.execute(select(func.count(FMaintenanceLine.id))).scalar_one(),
        db.execute(select(func.sum(FMaintenanceLine.qty))).scalar_one(),
    )
    assert before == after
    assert batch2.report_json["orders_updated"] == 3
    assert batch2.report_json["fact_rows_updated"] == 4


def test_recompute_touches_only_cost_columns(db, tmp_path):
    """④成本审计：recompute 只写成本回填列；展示列/事实列零字节变化。"""
    _import(db, tmp_path, make_rows(orders=2, lines_per_order=2))
    display_before = db.execute(
        select(FMaintenanceLine.raw_line_id, FMaintenanceLine.supplied_qty,
               FMaintenanceLine.line_note, FMaintenanceLine.qty,
               FMaintenanceOrder.receiver, FMaintenanceOrder.head_shipped_qty)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .order_by(FMaintenanceLine.raw_line_id)
    ).all()
    stats = maintenance_cost.recompute(db)
    assert stats["lines_in_scope"] == 4
    display_after = db.execute(
        select(FMaintenanceLine.raw_line_id, FMaintenanceLine.supplied_qty,
               FMaintenanceLine.line_note, FMaintenanceLine.qty,
               FMaintenanceOrder.receiver, FMaintenanceOrder.head_shipped_qty)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .order_by(FMaintenanceLine.raw_line_id)
    ).all()
    assert display_before == display_after
    # 无采购/销售参照的合成 PN → cost_source='none'，缺价不是 0（铁律 5 精神）
    sources = db.execute(select(FMaintenanceLine.cost_source)).scalars().all()
    assert all(s == "none" for s in sources)
    amounts = db.execute(select(FMaintenanceLine.cost_amount)).scalars().all()
    assert all(a is None for a in amounts)
