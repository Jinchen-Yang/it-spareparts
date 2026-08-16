"""M1-1/M1-2/M1-3：WBDD 双布局探测、按段防御、无明细单头保留、展示列落库（plan v1.3 §2.2）。

全部使用合成 fixture（import-field-contract §9.2：禁真实业务数据）。
"""
import pytest
from sqlalchemy import select

from app.etl import mapping, pipeline
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.services import maintenance_wbdd_import as wbdd
from tests.wbdd_fixtures import (
    COLUMNS_90,
    COLUMNS_91,
    make_rows,
    write_workbook,
)


# ---------- 布局探测（纯列名，零 IO） ----------

def test_layout_91_detected_by_anchor_position():
    assert wbdd.validate_wbdd_layout(COLUMNS_91) == "91"


def test_layout_90_detected_by_anchor_position():
    assert wbdd.validate_wbdd_layout(COLUMNS_90) == "90"


def test_missing_anchor_column_rejected():
    cols = [c for c in COLUMNS_91 if c != "需求明细.数据ID(不可修改)"]
    with pytest.raises(wbdd.WbddImportError) as exc:
        wbdd.validate_wbdd_layout(cols)
    assert exc.value.code == "layout_unknown"


def test_unexpected_width_rejected():
    with pytest.raises(wbdd.WbddImportError) as exc:
        wbdd.validate_wbdd_layout(COLUMNS_91 + ["多余列"])   # 92 列
    assert exc.value.code == "layout_unknown"


def test_head_field_in_line_segment_rejected():
    # 把头字段「维保工单」挪进明细段（与明细段一列互换），列数不变
    cols = list(COLUMNS_91)
    i_head = cols.index("维保工单")
    i_line = cols.index("需求明细.备注")
    cols[i_head], cols[i_line] = cols[i_line], cols[i_head]
    with pytest.raises(wbdd.WbddImportError) as exc:
        wbdd.validate_wbdd_layout(cols)
    assert exc.value.code == "segment_mismatch"


def test_mapping_counts_match_plan():
    """plan v1.3 §3 核对式：头 48=14+34，明细 35=7+28。"""
    assert len(mapping.MAINTENANCE_HEAD) == 48
    assert len(mapping.MAINTENANCE_LINE) == 35
    assert len(mapping.MAINTENANCE_HEAD_DISPLAY_FIELDS) == 34
    assert len(mapping.MAINTENANCE_LINE_DISPLAY_FIELDS) == 28


# ---------- 端到端：合成文件走通用管线 ----------

def _run(db, tmp_path, columns, rows, name="wbdd.xlsx"):
    path = write_workbook(str(tmp_path / name), columns, rows)
    batch = pipeline.run_import(db, path, name, uploaded_by="tester", mode="upsert")
    db.commit()
    return batch


def test_91_layout_full_ingest_with_display_columns(db, tmp_path):
    batch = _run(db, tmp_path, COLUMNS_91, make_rows(orders=2, lines_per_order=2))
    assert batch.file_type == "maintenance"
    orders = db.execute(select(FMaintenanceOrder)).scalars().all()
    lines = db.execute(select(FMaintenanceLine)).scalars().all()
    assert len(orders) == 2 and len(lines) == 4
    o = next(x for x in orders if x.raw_order_id == "SYN-O001")
    # 展示补全列逐类抽查（str/qty/bool/date/原样文本）
    assert o.maintainer_raw == "合成负责人"
    assert o.head_demand_qty == 3 and o.head_shipped_qty == 2
    assert o.change_warehouse_flag is False          # 「否」→ False
    assert o.accept_generic_flag is True             # 「是」→ True（91 列独有）
    assert str(o.supply_deadline) == "2026-08-01"
    assert o.purchaser2_raw == "合成采购人员"          # 裸「采购人员」经 canonicalize 归一
    assert o.express_no2 == "SF001#"
    assert o.created_at_raw == "2026-07-15 10:00"
    ln = next(x for x in lines if x.raw_line_id == "SYN-L001-1")
    assert ln.whole_or_part == "备件"                 # 斜杠单列
    assert ln.line_image_urls == "http://line/att"    # 斜杠单列
    assert ln.supplied_qty == 2 and ln.pending_supply_qty == 1
    assert ln.consumed_qty is None                    # 空「领用数量」→ NULL 非 0
    assert ln.warehouse_stock_raw == "总仓:5;分仓A:2"


def test_90_layout_ingest_accept_generic_null(db, tmp_path):
    batch = _run(db, tmp_path, COLUMNS_90, make_rows(orders=2, lines_per_order=1))
    assert batch.file_type == "maintenance"
    orders = db.execute(select(FMaintenanceOrder)).scalars().all()
    assert len(orders) == 2
    assert all(o.accept_generic_flag is None for o in orders)   # 90 列无此列 → NULL
    assert all(o.maintainer_raw == "合成负责人" for o in orders)


def test_headless_orders_preserved(db, tmp_path):
    """有单头无明细：单头入库、明细 0 行、计入 headless 计数，不再是错误行。"""
    batch = _run(db, tmp_path, COLUMNS_91,
                 make_rows(orders=1, lines_per_order=2, headless=3))
    report = batch.report_json
    assert report["headless_orders"] == 3
    assert set(report["headless_order_ids_sample"]) == {"SYN-H001", "SYN-H002", "SYN-H003"}
    orders = db.execute(select(FMaintenanceOrder)).scalars().all()
    lines = db.execute(select(FMaintenanceLine)).scalars().all()
    assert len(orders) == 4 and len(lines) == 2
    headless = next(o for o in orders if o.raw_order_id == "SYN-H001")
    assert headless.order_no == "WBDD-2026H001"
    # 无明细单头不产生 missing_raw_id 错误
    assert not any(e["error_type"] == "missing_raw_id"
                   for e in report.get("errors_preview", []))


def test_display_bad_values_null_plus_issue_count(db, tmp_path):
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["是否变仓库"] = "也许"            # 非 是/否 → NULL + issue
    rows[0]["供货期限"] = "不是日期"          # 解析失败 → NULL + issue
    rows[0]["需求明细.已供数量"] = "abc"      # 数量坏值 → NULL + issue
    batch = _run(db, tmp_path, COLUMNS_91, rows)
    o = db.execute(select(FMaintenanceOrder)).scalars().one()
    ln = db.execute(select(FMaintenanceLine)).scalars().one()
    assert o.change_warehouse_flag is None and o.supply_deadline is None
    assert ln.supplied_qty is None
    assert batch.report_json["rows_display_issue"] == 3
    # 坏展示值不阻断行：事实行照常入库
    assert ln.qty == 3


def test_reupload_refreshes_display_but_not_cost(db, tmp_path):
    """快照重传幂等：展示列覆盖刷新；成本回填列（recompute 独占）不被导入触碰。"""
    _run(db, tmp_path, COLUMNS_91, make_rows(orders=1, lines_per_order=1))
    ln = db.execute(select(FMaintenanceLine)).scalars().one()
    # 预置一个成本值模拟 recompute 结果
    ln.unit_cost = 100
    ln.cost_source = "manual"
    db.commit()
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["收货人"] = "新收货人"
    _run(db, tmp_path, COLUMNS_91, rows, name="wbdd2.xlsx")
    o = db.execute(select(FMaintenanceOrder)).scalars().one()
    ln = db.execute(select(FMaintenanceLine)).scalars().one()
    assert o.receiver == "新收货人"                      # 展示列已刷新
    assert ln.unit_cost == 100 and ln.cost_source == "manual"  # 成本列未被触碰
