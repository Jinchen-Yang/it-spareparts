"""库存出入流水（§7.6）：transform 方向/单据类型解析、loader 幂等、动态在库、对账、整机库。"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import func, select

from app.etl import loader, transform
from app.models.dimensions import DimPart
from app.models.inventory import Inventory, InventoryMovement
from app.models.sales import FSalesLine
from app.models.system import SysImportBatch
from app.services import inventory as inv_svc
from app.services import profit
from tests import factories as f


@pytest.fixture()
def batch(db):
    b = SysImportBatch(filename="ledger.xlsx", file_type="stock_ledger", file_hash="hmv1")
    db.add(b)
    db.flush()
    return b


def _import(db, batch, rows):
    res = loader.load(db, f.movement_result(rows), batch.id, date(2026, 6, 1))
    db.commit()
    return res


# ---------- transform：列名 → 方向/类型 ----------
def test_transform_in_out_columns():
    df = pd.DataFrame([
        {"流水ID": "M1", "单据日期": "2026-01-05", "单据类型": "收货入库单",
         "单据编号": "RK001", "产品名称(PN)": "PN-A", "仓库": "总仓",
         "入库数量": 10, "出库数量": None, "单价": 50},
        {"流水ID": "M2", "单据日期": "2026-01-06", "单据类型": "销售出库单",
         "单据编号": "CK001", "产品名称(PN)": "PN-A", "仓库": "总仓",
         "入库数量": None, "出库数量": 3, "单价": None},
    ])
    res = transform.transform(df, "stock_ledger")
    assert len(res.movements) == 2 and not res.errors
    m1, m2 = res.movements
    assert m1["doc_type"] == "receipt" and m1["direction"] == 1 and m1["qty"] == Decimal("10")
    assert m1["unit_price"] == Decimal("50")
    assert m2["doc_type"] == "issue" and m2["direction"] == -1 and m2["qty"] == Decimal("3")


def test_transform_signed_quantity_and_stocktake():
    df = pd.DataFrame([
        {"流水ID": "M1", "单据日期": "2026-01-05", "单据类型": "调拨出库",
         "产品名称(PN)": "PN-A", "仓库": "总仓", "数量": -4},
        {"流水ID": "M2", "单据日期": "2026-01-10", "单据类型": "盘点单",
         "产品名称(PN)": "PN-A", "仓库": "总仓", "数量": 8},
        {"流水ID": "M3", "单据日期": "2026-01-11", "单据类型": "直发出库",
         "产品名称(PN)": "PN-A", "仓库": "总仓", "数量": 2},
    ])
    res = transform.transform(df, "stock_ledger")
    by = {m["raw_movement_id"]: m for m in res.movements}
    assert by["M1"]["doc_type"] == "transfer_out" and by["M1"]["direction"] == -1
    assert by["M2"]["doc_type"] == "stocktake" and by["M2"]["is_absolute"] is True \
        and by["M2"]["direction"] == 0 and by["M2"]["qty"] == Decimal("8")
    assert by["M3"]["doc_type"] == "direct_ship" and by["M3"]["direction"] == 0


def test_detect_file_type_ledger():
    from app.etl import mapping
    assert mapping.detect_file_type(["流水ID", "产品名称(PN)", "仓库", "数量"]) == "stock_ledger"
    assert mapping.detect_file_type(["单据类型", "仓库", "入库数量", "产品名称(PN)"]) == "stock_ledger"
    # 不与库存快照冲突
    assert mapping.detect_file_type(["产品库存ID", "库存数量", "产品名称(PN)"]) == "inventory"


# ---------- loader：建档、幂等 ----------
def test_movement_load_creates_part_and_rows(db, batch):
    _import(db, batch, [
        f.movement_row("M1", "PN-A", direction=1, qty="10"),
        f.movement_row("M2", "PN-A", direction=-1, qty="3", doc_type="issue"),
    ])
    assert db.scalar(select(DimPart).where(DimPart.pn_std == "PN-A")) is not None
    assert db.scalar(select(func.count()).select_from(InventoryMovement)) == 2


def test_movement_load_idempotent(db, batch):
    res1 = _import(db, batch, [f.movement_row("M1", "PN-A", qty="10")])
    assert res1["fact_rows_inserted"] == 1
    res2 = _import(db, batch, [f.movement_row("M1", "PN-A", qty="10"),
                               f.movement_row("M2", "PN-A", qty="5")])
    assert res2["fact_rows_inserted"] == 1 and res2["fact_rows_skipped"] == 1
    assert db.scalar(select(func.count()).select_from(InventoryMovement)) == 2


# ---------- 动态在库：时间回放 ----------
def _pid(db, pn):
    return db.scalar(select(DimPart.id).where(DimPart.pn_std == pn))


def test_compute_onhand_in_minus_out(db, batch):
    _import(db, batch, [
        f.movement_row("M1", "PN-A", direction=1, qty="10", on=date(2026, 1, 1)),
        f.movement_row("M2", "PN-A", direction=-1, qty="3", on=date(2026, 1, 2)),
        f.movement_row("M3", "PN-A", direction=1, qty="5", on=date(2026, 1, 3)),
    ])
    oh = inv_svc.compute_onhand(db)
    assert oh[(_pid(db, "PN-A"), "总仓")] == Decimal("12")   # 10 - 3 + 5


def test_compute_onhand_stocktake_resets(db, batch):
    _import(db, batch, [
        f.movement_row("M1", "PN-A", direction=1, qty="10", on=date(2026, 1, 1)),
        f.movement_row("M2", "PN-A", is_absolute=True, qty="8", doc_type="stocktake",
                       direction=0, on=date(2026, 1, 5)),
        f.movement_row("M3", "PN-A", direction=-1, qty="2", on=date(2026, 1, 6)),
    ])
    oh = inv_svc.compute_onhand(db)
    assert oh[(_pid(db, "PN-A"), "总仓")] == Decimal("6")    # 盘点重置为8，再 -2


def test_compute_onhand_direct_ship_and_warehouses(db, batch):
    _import(db, batch, [
        f.movement_row("M1", "PN-A", direction=1, qty="10", warehouse="A仓"),
        f.movement_row("M2", "PN-A", direction=0, qty="4", doc_type="direct_ship",
                       warehouse="A仓"),                       # 直发不动结存
        f.movement_row("M3", "PN-A", direction=1, qty="7", warehouse="B仓"),
    ])
    oh = inv_svc.compute_onhand(db)
    pid = _pid(db, "PN-A")
    assert oh[(pid, "A仓")] == Decimal("10") and oh[(pid, "B仓")] == Decimal("7")


def test_compute_onhand_as_of(db, batch):
    _import(db, batch, [
        f.movement_row("M1", "PN-A", direction=1, qty="10", on=date(2026, 1, 1)),
        f.movement_row("M2", "PN-A", direction=-1, qty="3", on=date(2026, 2, 1)),
    ])
    oh = inv_svc.compute_onhand(db, as_of=date(2026, 1, 15))
    assert oh[(_pid(db, "PN-A"), "总仓")] == Decimal("10")   # 2月那笔不计


def test_part_ledger_running_balance(db, batch):
    _import(db, batch, [
        f.movement_row("M1", "PN-A", direction=1, qty="10", on=date(2026, 1, 1)),
        f.movement_row("M2", "PN-A", direction=-1, qty="4", on=date(2026, 1, 2)),
    ])
    led = inv_svc.part_ledger(db, _pid(db, "PN-A"))
    assert [r["balance"] for r in led] == [10.0, 6.0]
    assert led[1]["signed_qty"] == -4.0


# ---------- 对账：流水 vs 快照 ----------
def test_reconcile_match_diff_and_oneside(db, batch):
    # 流水：PN-A 在库 7；PN-B 在库 5（仅流水，无快照）
    _import(db, batch, [
        f.movement_row("M1", "PN-A", direction=1, qty="10"),
        f.movement_row("M2", "PN-A", direction=-1, qty="3"),
        f.movement_row("M3", "PN-B", direction=1, qty="5"),
    ])
    # 快照：PN-A=7（吻合），PN-C=9（仅快照，无流水）
    loader.load(db, f.inventory_result([
        f.inventory_row("INV-A", "PN-A", qty="7"),
        f.inventory_row("INV-C", "PN-C", qty="9"),
    ]), batch.id, date(2026, 6, 1))
    db.commit()

    rec = inv_svc.reconcile(db, only_diff=False)
    by_pn = {r["pn_std"]: r for r in rec["rows"]}
    assert by_pn["PN-A"]["status"] == "match" and by_pn["PN-A"]["diff"] == 0.0
    assert by_pn["PN-B"]["status"] == "ledger_only" and by_pn["PN-B"]["computed_qty"] == 5.0
    assert by_pn["PN-C"]["status"] == "snapshot_only" and by_pn["PN-C"]["snapshot_qty"] == 9.0

    only_diff = inv_svc.reconcile(db, only_diff=True)
    assert "PN-A" not in {r["pn_std"] for r in only_diff["rows"]}   # 一致行被隐藏
    assert only_diff["summary"]["match"] == 1


def test_reconcile_flags_missing_movements(db, batch):
    # 快照 100，但流水只记到 80 → 差异 -20（流水不全的典型）
    _import(db, batch, [f.movement_row("M1", "PN-A", direction=1, qty="80")])
    loader.load(db, f.inventory_result([f.inventory_row("INV-A", "PN-A", qty="100")]),
                batch.id, date(2026, 6, 1))
    db.commit()
    rec = inv_svc.reconcile(db, only_diff=True)
    row = next(r for r in rec["rows"] if r["pn_std"] == "PN-A")
    assert row["status"] == "diff" and row["diff"] == -20.0


# ---------- P1：流水接入成本回放 ----------
def _seed_purchase_sales(db, fhash, sales_lines):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash=fhash)
    db.add(b)
    db.flush()
    loader.load(db, f.purchase_result(
        {"P1": f.purchase_head("P1", on=date(2026, 1, 1))},
        [f.purchase_line("P1", "PL1", "PN-X", qty="5", price="100")]), b.id, date(2026, 6, 1))
    so = {f"S{i}": f.sales_head(f"S{i}", on=on) for i, (_, _, on) in enumerate(sales_lines, 1)}
    sl = [f.sales_line(f"S{i}", lid, "PN-X", qty=qty, price="150")
          for i, (lid, qty, _) in enumerate(sales_lines, 1)]
    loader.load(db, f.sales_result(so, sl), b.id, date(2026, 6, 1))
    db.commit()
    return b


def test_return_movement_feeds_sale_cost(db):
    # 采购5@100；2/1卖5吃光；3/1卖3（无货）
    _seed_purchase_sales(db, "hp1", [("SL1", "5", date(2026, 2, 1)),
                                     ("SL2", "3", date(2026, 3, 1))])
    profit.recompute(db)
    sl2 = db.scalar(select(FSalesLine).where(FSalesLine.raw_line_id == "SL2"))
    assert sl2.cost_source == "fallback" and sl2.cost_moving_avg == Decimal("100.00")

    # 加退货返库 5@60（2/10）→ 3/1 那笔吃到退货层 @60，computed
    bm = SysImportBatch(filename="ledger.xlsx", file_type="stock_ledger", file_hash="hl1")
    db.add(bm)
    db.flush()
    loader.load(db, f.movement_result([
        f.movement_row("RM1", "PN-X", direction=1, qty="5", doc_type="return_in",
                       unit_price="60", on=date(2026, 2, 10))]), bm.id, date(2026, 6, 1))
    db.commit()
    profit.recompute(db)
    db.expire_all()
    sl2 = db.scalar(select(FSalesLine).where(FSalesLine.raw_line_id == "SL2"))
    assert sl2.cost_source == "computed"
    assert sl2.cost_moving_avg == Decimal("60.00") and sl2.cost_fifo == Decimal("60.00")


def test_receipt_movement_not_double_counted(db):
    # 采购5@100；3/1卖8（采购仅够5，3件兜底）
    _seed_purchase_sales(db, "hp2", [("SL2", "8", date(2026, 3, 1))])
    bm = SysImportBatch(filename="ledger.xlsx", file_type="stock_ledger", file_hash="hl2")
    db.add(bm)
    db.flush()
    # receipt 100@999——采购已覆盖收货，若被误计会让 SL2 变 computed@异常价；正确应仍兜底
    loader.load(db, f.movement_result([
        f.movement_row("RM1", "PN-X", direction=1, qty="100", doc_type="receipt",
                       unit_price="999", on=date(2026, 2, 1))]), bm.id, date(2026, 6, 1))
    db.commit()
    profit.recompute(db)
    sl2 = db.scalar(select(FSalesLine).where(FSalesLine.raw_line_id == "SL2"))
    assert sl2.cost_source == "fallback"
    assert sl2.cost_moving_avg != Decimal("999.00")


# ---------- P2：单据语义 / 整机库 / 调拨 ----------
@pytest.mark.parametrize("raw,expect_type,expect_dir,expect_abs", [
    ("调拨入库单", "transfer_in", 1, False),
    ("调拨出库单", "transfer_out", -1, False),
    ("组装入库单", "assembly_in", 1, False),
    ("组装领料单", "assembly_out", -1, False),
    ("退货返库单", "return_in", 1, False),
    ("采购入库单", "receipt", 1, False),
    ("销售出库单", "issue", -1, False),
    ("直发出库单", "direct_ship", 0, False),
    ("盘点单", "stocktake", 0, True),
    ("盘盈单", "stocktake_gain", 1, False),
    ("盘亏单", "stocktake_loss", -1, False),
])
def test_doc_type_vocabulary_priority(raw, expect_type, expect_dir, expect_abs):
    from app.etl import mapping
    doc_type, direction, is_abs = mapping.resolve_doc_type(raw)
    assert doc_type == expect_type and is_abs == expect_abs
    # 直发/盘点的最终方向由 transform 归零；此处校验词表默认方向
    if expect_type not in ("stocktake",):
        assert direction == expect_dir


def test_ledger_kind_machine_detection():
    df = pd.DataFrame([
        {"流水ID": "M1", "单据类型": "整机入库", "产品名称(PN)": "JI-1",
         "仓库": "整机仓", "入库数量": 1, "整机/备件": "整机"},
        {"流水ID": "M2", "单据类型": "收货入库", "产品名称(PN)": "PN-A",
         "仓库": "总仓", "入库数量": 5, "整机/备件": "备件"},
    ])
    res = transform.transform(df, "stock_ledger")
    by = {m["raw_movement_id"]: m for m in res.movements}
    assert by["M1"]["ledger_kind"] == "machine"
    assert by["M2"]["ledger_kind"] == "part"


def test_transfer_net_zero_at_part_level(db, batch):
    # 同一 part 从 A 仓调出 4、调入 B 仓 4：part 级总量不变，仓库级此消彼长
    _import(db, batch, [
        f.movement_row("M0", "PN-A", direction=1, qty="10", warehouse="A仓"),
        f.movement_row("M1", "PN-A", direction=-1, qty="4", doc_type="transfer_out", warehouse="A仓"),
        f.movement_row("M2", "PN-A", direction=1, qty="4", doc_type="transfer_in", warehouse="B仓"),
    ])
    oh = inv_svc.compute_onhand(db)
    pid = _pid(db, "PN-A")
    assert oh[(pid, "A仓")] == Decimal("6") and oh[(pid, "B仓")] == Decimal("4")
    # part 级合计 = 10（调拨不增减总量）
    assert sum(v for (p, _w), v in oh.items() if p == pid) == Decimal("10")


def test_counterpart_warehouse_recorded(db, batch):
    _import(db, batch, [
        f.movement_row("M1", "PN-A", direction=-1, qty="3", doc_type="transfer_out",
                       warehouse="A仓", counterpart_warehouse="B仓"),
    ])
    m = db.scalar(select(InventoryMovement).where(InventoryMovement.raw_movement_id == "M1"))
    assert m.counterpart_warehouse == "B仓" and m.doc_type == "transfer_out"


def test_merge_repoints_movements(db, batch):
    """合并 part 后，流水跟随 repoint 到目标（与采购/销售/库存同口径），在库归并不丢。"""
    from app.services import merge
    _import(db, batch, [
        f.movement_row("M1", "PN-SRC", direction=1, qty="5"),
        f.movement_row("M2", "PN-TGT", direction=1, qty="3"),
    ])
    merge.merge_parts(db, "PN-SRC", "PN-TGT", "同款", "tester")
    db.expire_all()
    tgt = db.scalar(select(DimPart.id).where(DimPart.pn_std == "PN-TGT"))
    m1 = db.scalar(select(InventoryMovement).where(InventoryMovement.raw_movement_id == "M1"))
    assert m1.part_id == tgt
    oh = inv_svc.compute_onhand(db)
    assert oh[(tgt, "总仓")] == Decimal("8")     # 5 + 3 归并到目标


def test_list_movements_filter_by_kind(db, batch):
    _import(db, batch, [
        f.movement_row("M1", "PN-A", qty="5", ledger_kind="part"),
        f.movement_row("M2", "JI-1", qty="1", ledger_kind="machine", warehouse="整机仓"),
    ])
    machines = inv_svc.list_movements(db, None, None, None, 1, 50, ledger_kind="machine")
    assert machines["total"] == 1 and machines["items"][0]["pn_std"] == "JI-1"


def test_assembly_out_consumes_stock_for_cost(db):
    # 采购5@100；先组装领料3（2/1）；3/1卖3 → 仅剩2件有货 → 1件兜底
    _seed_purchase_sales(db, "hp3", [("SL2", "3", date(2026, 3, 1))])
    bm = SysImportBatch(filename="ledger.xlsx", file_type="stock_ledger", file_hash="hl3")
    db.add(bm)
    db.flush()
    loader.load(db, f.movement_result([
        f.movement_row("AM1", "PN-X", direction=-1, qty="3", doc_type="assembly_out",
                       on=date(2026, 2, 1))]), bm.id, date(2026, 6, 1))
    db.commit()
    profit.recompute(db)
    sl2 = db.scalar(select(FSalesLine).where(FSalesLine.raw_line_id == "SL2"))
    # 5采购−3组装领料=2可用，卖3 → 2件@100 + 1件兜底@100 → 仍兜底标记
    assert sl2.cost_source == "fallback"
