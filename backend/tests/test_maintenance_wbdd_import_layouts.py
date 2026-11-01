"""WBDD 命名字段校验、旧双布局兼容、无明细单头保留与展示列落库。

全部使用合成 fixture（import-field-contract §9.2：禁真实业务数据）。
"""
from datetime import date

import pytest
from sqlalchemy import select

from app.etl import loader, mapping, pipeline, reader, transform
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.system import SysImportBatch
from app.services import maintenance_wbdd_import as wbdd
from tests import factories as f
from tests.wbdd_fixtures import (
    COLUMNS_90,
    COLUMNS_91,
    make_rows,
    write_workbook,
)


# ---------- 布局探测（纯列名，零 IO） ----------

def test_layout_91_compatibility_label():
    assert wbdd.validate_wbdd_layout(COLUMNS_91) == "91"


def test_layout_90_compatibility_label():
    assert wbdd.validate_wbdd_layout(COLUMNS_90) == "90"


def test_missing_anchor_column_rejected():
    cols = [c for c in COLUMNS_91 if c != "需求明细.数据ID(不可修改)"]
    with pytest.raises(wbdd.WbddImportError) as exc:
        wbdd.validate_wbdd_layout(cols)
    assert exc.value.code == "layout_unknown"


def test_optional_columns_can_be_added_or_removed():
    assert wbdd.validate_wbdd_layout(COLUMNS_91 + ["地址操作"]) == "name"
    assert wbdd.validate_wbdd_layout([c for c in COLUMNS_90 if c != "收货地址"]) == "name"


def test_head_and_detail_columns_can_move_without_changing_identity():
    cols = list(COLUMNS_91)
    i_head = cols.index("维保工单")
    i_line = cols.index("需求明细.备注")
    cols[i_head], cols[i_line] = cols[i_line], cols[i_head]
    assert wbdd.validate_wbdd_layout(cols) == "91"


def test_duplicate_headers_rejected():
    with pytest.raises(wbdd.WbddImportError) as exc:
        wbdd.validate_wbdd_layout(COLUMNS_91 + ["需求单号"])
    assert exc.value.code == "layout_unknown"


def test_blank_header_cells_follow_reader_compatibility():
    assert wbdd.validate_wbdd_layout(COLUMNS_91 + ["", ""]) == "name"


def test_mapping_counts_match_plan():
    """plan v1.3 §3 核对式：头 48=14+34，明细 35=7+28。"""
    assert len(mapping.MAINTENANCE_HEAD) == 48
    assert len(mapping.MAINTENANCE_LINE) == 35
    assert len(mapping.MAINTENANCE_HEAD_DISPLAY_FIELDS) == 34
    assert len(mapping.MAINTENANCE_LINE_DISPLAY_FIELDS) == 28


# ---------- 端到端：合成文件走通用管线 ----------

def test_present_fields_reflect_canonical_columns_not_values(tmp_path):
    """D-0050：present_fields 由规范化后的列集合决定，空值不改变“已提供”。"""
    cols = [c for c in COLUMNS_91
            if c not in ("仓储中心", "需求明细.个别调整发货仓")]
    rows = make_rows(orders=1, lines_per_order=1)
    path = write_workbook(str(tmp_path / "present.xlsx"), cols, rows)
    df, file_type = reader.read_excel(path)
    assert file_type == mapping.MAINTENANCE
    result = transform.transform(df, mapping.MAINTENANCE)
    assert result.present_fields is not None
    assert "storage_center" not in result.present_fields["head"]
    assert "adjust_warehouse_flag" not in result.present_fields["line"]
    assert "adjust_warehouse_raw" not in result.present_fields["line"]
    # 列存在但值为空仍是“已提供”（空白按既有规则覆盖，不是缺列）
    assert "receiver" in result.present_fields["head"]
    assert "purchaser2_raw" in result.present_fields["head"]   # 裸「采购人员」已规范化
    # 旧调用方直接构造 TransformResult 时 presence 缺省为 None（全量更新语义）
    assert transform.TransformResult(file_type=mapping.MAINTENANCE).present_fields is None


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


def test_missing_optional_columns_keep_values_but_present_blank_clears(db, tmp_path):
    """D-0050：源列缺失保留旧值；源列存在但空白仍按既有规则覆盖为 NULL。"""
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["仓储中心"] = "旧仓储中心"
    rows[0]["需求明细.调整仓库"] = "旧调整仓"
    rows[0]["需求明细.个别调整发货仓"] = "华南仓"
    _run(db, tmp_path, COLUMNS_91, rows, name="first.xlsx")

    missing = [c for c in COLUMNS_91
               if c not in ("仓储中心", "需求明细.调整仓库",
                             "需求明细.个别调整发货仓",
                             "客户名称", "项目名")]
    batch = _run(db, tmp_path, missing, make_rows(orders=1, lines_per_order=1),
                 name="missing.xlsx")
    db.expire_all()
    order = db.execute(select(FMaintenanceOrder)).scalars().one()
    line = db.execute(select(FMaintenanceLine)).scalars().one()
    assert order.storage_center == "旧仓储中心"
    assert line.adjust_warehouse == "旧调整仓"
    assert line.adjust_warehouse_raw == "华南仓"
    # 派生列按源列缺失保留：customer_id/project_std 不得因源列缺失变 NULL
    assert order.customer_id is not None
    assert order.project_std == "合成项目A"
    # 缺列不得被误记为语义变化 / 触发 workbook revision bump
    assert batch.report_json["orders_semantic_updated"] == 0
    assert batch.report_json["fact_rows_semantic_updated"] == 0
    assert batch.report_json["workbook_projects_bumped"] == 0

    blank = make_rows(orders=1, lines_per_order=1)
    blank[0]["仓储中心"] = ""
    blank[0]["需求明细.调整仓库"] = ""
    blank[0]["需求明细.个别调整发货仓"] = ""
    _run(db, tmp_path, COLUMNS_91, blank, name="blank.xlsx")
    db.expire_all()
    order = db.execute(select(FMaintenanceOrder)).scalars().one()
    line = db.execute(select(FMaintenanceLine)).scalars().one()
    assert order.storage_center is None
    assert line.adjust_warehouse is None
    assert line.adjust_warehouse_raw is None


def test_present_fields_none_is_legacy_full_update_empty_set_preserves(db, tmp_path):
    """D-0050：present_fields=None 保留旧全量更新；空集合表示“源列全缺失”只更新批号。

    直接构造 TransformResult 的旧调用方（无 presence 元数据）不能被改成“全部保留”
    或“全部覆盖”；空 frozenset 才是明确的源列缺失。
    """
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["仓储中心"] = "旧仓储中心"
    rows[0]["需求明细.个别调整发货仓"] = "华南仓"
    _run(db, tmp_path, COLUMNS_91, rows, name="seed.xlsx")

    def _load(present_fields, name):
        result = f.maintenance_result(
            {"SYN-O001": f.maintenance_head("SYN-O001", order_no="WBDD-20260001")},
            [f.maintenance_line("SYN-O001", "SYN-L001-1", "PN-SYN-0011")],
        )
        result.orders["SYN-O001"]["storage_center"] = None
        result.lines[0]["adjust_warehouse_raw"] = None
        result.present_fields = present_fields
        batch = SysImportBatch(filename=name, file_type="maintenance",
                               file_hash=name)
        db.add(batch)
        db.flush()
        loader.load(db, result, batch.id, date(2026, 7, 20), mode="upsert")
        db.commit()

    _load(None, "legacy.xlsx")
    db.expire_all()
    order = db.execute(select(FMaintenanceOrder)).scalars().one()
    line = db.execute(select(FMaintenanceLine)).scalars().one()
    assert order.storage_center is None                 # 旧调用方仍全量更新
    assert line.adjust_warehouse_raw is None

    order.storage_center = "旧仓储中心"
    line.adjust_warehouse_raw = "华南仓"
    db.commit()
    _load({"head": frozenset(), "line": frozenset()}, "missing.xlsx")
    db.expire_all()
    order = db.execute(select(FMaintenanceOrder)).scalars().one()
    line = db.execute(select(FMaintenanceLine)).scalars().one()
    assert order.storage_center == "旧仓储中心"          # 空集合=源列缺失 → 保留
    assert line.adjust_warehouse_raw == "华南仓"


def test_adjust_warehouse_text_kept_raw_not_guessed_as_boolean(db, tmp_path):
    """D-0050：仓库名保存原文；布尔列只在明确是/否时置值。"""
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["需求明细.个别调整发货仓"] = "华南仓"
    batch = _run(db, tmp_path, COLUMNS_91, rows)
    line = db.execute(select(FMaintenanceLine)).scalars().one()
    assert line.adjust_warehouse_flag is None
    assert line.adjust_warehouse_raw == "华南仓"
    # D-0050：仓库名是合法原文，不再按坏布尔累计展示告警
    assert batch.report_json["rows_display_issue"] == 0

    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["需求明细.个别调整发货仓"] = "是"
    _run(db, tmp_path, COLUMNS_91, rows, name="yes.xlsx")
    db.expire_all()
    line = db.execute(select(FMaintenanceLine)).scalars().one()
    assert line.adjust_warehouse_flag is True
    assert line.adjust_warehouse_raw == "是"


def test_proven_truncation_fields_keep_full_source_text(db, tmp_path):
    """D-0050：退返旧件/收货人/电话/快递单号不再静默切片。"""
    long_text = {
        "需求明细.退返旧件": "退" * 24,
        "收货人": "收" * 76,
        "收货人电话": "1" * 56,
        "快递单号": "E" * 188,
        "快递单号#": "F" * 188,
    }
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0].update(long_text)
    _run(db, tmp_path, COLUMNS_91, rows)
    order = db.execute(select(FMaintenanceOrder)).scalars().one()
    line = db.execute(select(FMaintenanceLine)).scalars().one()
    assert order.receiver == long_text["收货人"]
    assert order.receiver_phone == long_text["收货人电话"]
    assert order.express_no == long_text["快递单号"]
    assert order.express_no2 == long_text["快递单号#"]
    assert line.return_old_part == long_text["需求明细.退返旧件"]


def test_overlong_core_identity_reports_field_and_skips_order(db, tmp_path):
    """D-0050：核心身份超长必须明确报错；唯一工单被拒后整批失败，不以 0 行冒充成功。"""
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["数据ID(不可修改)"] = "O" * 65
    path = write_workbook(str(tmp_path / "wbdd.xlsx"), COLUMNS_91, rows)
    with pytest.raises(reader.ReaderError) as exc_info:
        pipeline.run_import(db, path, "wbdd.xlsx", uploaded_by="tester", mode="upsert")
    db.commit()

    assert exc_info.value.code == "no_importable_rows"
    batch = db.scalar(select(SysImportBatch).order_by(SysImportBatch.id.desc()))
    assert batch.status == "failed"
    assert batch.rows_total == 1 and batch.rows_error == 1
    assert "均未形成可导入事实" in batch.report_json["error"]
    assert db.execute(select(FMaintenanceOrder)).scalars().all() == []
    assert db.execute(select(FMaintenanceLine)).scalars().all() == []
    errors = batch.report_json["errors_preview"]
    too_long = [item for item in errors if item["error_type"] == "field_too_long"]
    assert too_long, errors
    detail = too_long[0]["detail"]
    assert "维保单数据ID" in detail and "64" in detail and "65" in detail
