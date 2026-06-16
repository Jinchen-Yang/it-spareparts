"""行级异常标记决策树根（app/etl/anomaly.py）：唯一规则 + transform/profit 共用证明（PR-B）。

旧实现 zero_price/amount_mismatch 与容差 0.05 在 transform 与 profit 各写一份（穿透层）。
现在统一到 anomaly.line_flags：root 只看本行数值，profit 在其上挂成本/业务分支。
"""
from datetime import date
from decimal import Decimal

import pandas as pd
from sqlalchemy import select

from app.etl import anomaly, loader, mapping, transform
from app.models.purchase import FPurchaseLine
from app.models.sales import FSalesLine
from app.models.system import SysImportBatch
from app.services import profit
from tests import factories as f


# ---------- 决策树根：纯函数规则 ----------
def test_zero_price_only_when_exactly_zero():
    assert anomaly.line_flags(Decimal(2), Decimal(0), Decimal(0)) == ["zero_price"]
    assert "zero_price" not in anomaly.line_flags(Decimal(2), Decimal("0.01"), Decimal("0.02"))


def test_none_price_not_flagged():
    # None=「无此值」，不误判（采购行价缺失不应等同于 0 价）
    assert anomaly.line_flags(Decimal(2), None, Decimal(20)) == []


def test_amount_mismatch_respects_tolerance():
    # |line_amount - qty*price| > 0.05 才标
    assert anomaly.line_flags(Decimal(2), Decimal(10), Decimal(25)) == ["amount_mismatch"]
    assert anomaly.line_flags(Decimal(2), Decimal(10), Decimal("20.04")) == []   # 容差内
    assert anomaly.line_flags(Decimal(2), Decimal(10), Decimal("20.05")) == []   # 边界：严格大于
    assert anomaly.line_flags(Decimal(2), Decimal(10), Decimal("20.06")) == ["amount_mismatch"]


def test_none_line_amount_not_mismatch():
    assert anomaly.line_flags(Decimal(2), Decimal(10), None) == []


def test_zero_price_and_mismatch_can_coexist():
    # 0 价 + 行金额非 0 → 两个根级 flag 都出
    assert anomaly.line_flags(Decimal(2), Decimal(0), Decimal(10)) == ["zero_price", "amount_mismatch"]


# ---------- transform 采购路径：root 落库且为权威（profit 不碰采购行）----------
def _purchase_df(rows: list[dict]) -> pd.DataFrame:
    """rows 用内部字段名；按 mapping 反查中文表头构造 DataFrame。"""
    hm = mapping.MAPPINGS[mapping.PURCHASE]["head"]
    lm = mapping.MAPPINGS[mapping.PURCHASE]["line"]
    cn = {**{v: k for k, v in hm.items()}, **{v: k for k, v in lm.items()}}  # internal -> 中文
    return pd.DataFrame([{cn[k]: v for k, v in r.items()} for r in rows])


def test_transform_purchase_uses_root():
    df = _purchase_df([{
        "raw_order_id": "O1", "order_no": "O1", "order_date": "2026-01-01",
        "data_status": "已生效", "raw_line_id": "L1", "line_no": 1,
        "pn_raw": "PN-A", "qty": 2, "unit_price": 0, "line_amount": 10,
    }])
    res = transform.transform(df, mapping.PURCHASE)
    assert not res.errors, res.errors
    assert res.lines[0]["anomaly_flags"] == ["zero_price", "amount_mismatch"]


def test_purchase_flags_persist_through_loader(db):
    df = _purchase_df([{
        "raw_order_id": "O9", "order_no": "O9", "order_date": "2026-01-01",
        "data_status": "已生效", "raw_line_id": "L9", "line_no": 1,
        "pn_raw": "PN-B", "qty": 3, "unit_price": 7, "line_amount": 999,
    }])
    res = transform.transform(df, mapping.PURCHASE)
    b = SysImportBatch(filename="p.xlsx", file_type="purchase", file_hash="hP")
    db.add(b)
    db.flush()
    loader.load(db, res, b.id, date(2026, 6, 1))
    db.commit()
    pl = db.scalar(select(FPurchaseLine).where(FPurchaseLine.raw_line_id == "L9"))
    assert "amount_mismatch" in pl.anomaly_flags   # profit 不碰采购行 → transform 的 root 为权威


# ---------- profit 销售路径：在 root 之上挂成本/业务分支 ----------
def test_profit_sales_uses_root_then_branches(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hS")
    db.add(b)
    db.flush()
    loader.load(db, f.purchase_result(
        {"P1": f.purchase_head("P1", on=date(2026, 1, 1))},
        [f.purchase_line("P1", "PL1", "PN-A", qty="10", price="80")]), b.id, date(2026, 6, 1))

    zero = f.sales_line("S1", "SL_ZERO", "PN-A", qty="2", price="0")   # 0 价 → root: zero_price
    mism = f.sales_line("S1", "SL_MISM", "PN-A", qty="2", price="100")
    mism["line_amount"] = Decimal("500")                              # 故意不符 → root: amount_mismatch
    loader.load(db, f.sales_result({"S1": f.sales_head("S1", on=date(2026, 2, 1))},
                                   [zero, mism]), b.id, date(2026, 6, 1))
    db.commit()

    profit.recompute(db)
    db.expire_all()
    flags_zero = db.scalar(select(FSalesLine.anomaly_flags).where(FSalesLine.raw_line_id == "SL_ZERO"))
    flags_mism = db.scalar(select(FSalesLine.anomaly_flags).where(FSalesLine.raw_line_id == "SL_MISM"))
    assert "zero_price" in flags_zero            # 来自 root
    assert "amount_mismatch" in flags_mism       # 来自 root
