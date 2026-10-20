"""采购分析三口径与名义占比的**独立手算**测试（编码检察官 #398 #4，2026-10-18）。

与 test_purchase_analytics_api.py 的对拍测试互补：那里的等价性测试复用了实现的
构件（``_order_amount_exprs`` / ``order_amounts_reference``），只能证明「重构后与
参照实现一致」；本文件**不 import 实现的任何构件**，期望值全部按 D-39 §6 与
2026-10-18 金臣名义侧裁定手工推导后写死——实现若改错口径，这里直接红。

手算规则（单头，O 为订单）：
- 税率：标了（≠0）用标的，否则 13%；实付 = 含税列原值（None 不计）。
- 原始侧由「是否含税」决定：含税单含税额为原值、不含税 = round(含税/(1+率), 2)；
  非含税单不含税额为原值、含税 = round(不含税×(1+率), 2)；权威侧缺失回退另一列。
- 名义侧：未标税率时**实际被推算**的那侧为名义（含税单 → 不含税侧 "ex"；其余 →
  含税侧 "inc"）；已标税率 → None。历史缺口（权威侧列空、只录入另一侧）会把
  推算方向翻转回权威侧（H9/H10）；两侧金额列皆空 → None（H11）。
  名义占比 = 名义侧金额之和 ÷ 同侧合计（手算见各断言旁注释）。

行级（分类筛选）：行金额跟随单头原始侧，另一侧按同一税率换算；名义侧按单据
标记的**结构性规则**（含税单 → "ex"，其余 → "inc"），不受单头金额列缺口影响
——H9/H10 两种口径方向相反即为此意。
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models import DimPart, FPurchaseLine, FPurchaseOrder
from app.models.system import SysImportBatch
from tests.boss_board_helpers import client_for

D = Decimal


def _batch(db: Session, name: str) -> SysImportBatch:
    b = SysImportBatch(filename=f"{name}.xlsx", file_type="purchase",
                       file_hash=f"handcalc-{name}", uploaded_by="handcalc",
                       status="success")
    db.add(b)
    db.flush()
    return b


# (key, is_inc, rate, amount_ex_col, amount_inc_col, line_amount, purchaser)
# 档位：13% / 6% / 0.6% / 0 / NULL × 含税/不含税（0 与 NULL 各覆盖两种标记）
# H9/H10 = 历史缺口（权威侧列空、只录入另一侧，推算方向翻转）；H11 = 双侧皆空
HANDCALC_ORDERS = [
    ("H1", True,  "0.13",  D("1000"), D("1130"), D("1130"), "手算采购员"),
    ("H2", False, "0.13",  D("1000"), D("1130"), D("1000"), "已标税率采购员"),
    ("H3", True,  "0.06",  None,      D("1060"), D("1060"), "手算采购员"),
    ("H4", False, "0.006", D("1000"), None,      D("1000"), "手算采购员"),
    ("H5", False, "0",     D("2000"), None,      D("2000"), "手算采购员"),
    ("H6", False, None,    D("3000"), None,      D("3000"), "手算采购员"),
    ("H7", True,  "0",     None,      D("2260"), D("2260"), "手算采购员"),
    ("H8", None,  None,    D("500"),  D("500"),  D("500"),  "手算采购员"),
    ("H9", True,  None,    D("1000"), None,      D("1130"), "手算采购员"),
    ("H10", False, "0",    None,      D("2260"), D("2260"), "手算采购员"),
    ("H11", False, None,   None,      None,      D("500"),  "手算采购员"),
]


@pytest.fixture()
def handcalc_ds(db: Session):
    b = _batch(db, "handcalc")
    part = DimPart(pn_std="PN-HANDCALC", category_major="电器件")
    db.add(part)
    db.flush()
    pos = {}
    for key, is_inc, rate, ex, inc, line_amt, purchaser in HANDCALC_ORDERS:
        po = FPurchaseOrder(
            raw_order_id=key, order_no=f"CG-{key}", order_date=date(2026, 7, 1),
            purchaser=purchaser, source_type="销售订单", source_type_raw="销售订单",
            is_tax_inclusive=is_inc, tax_rate=rate,
            amount_ex_tax=ex, amount_inc_tax=inc, data_status="已生效",
            import_batch_id=b.id)
        db.add(po)
        db.flush()
        db.add(FPurchaseLine(
            raw_line_id=f"{key}-L1", order_id=po.id, line_no=1, part_id=part.id,
            pn_std=part.pn_std, qty=D("1"), unit_price=line_amt, line_amount=line_amt,
            import_batch_id=b.id))
        pos[key] = po
    db.commit()
    c = client_for(db, username="handcalc-boss", role="boss")
    return c, pos


def _summary(c, **params):
    r = c.get("/api/purchases/analytics/summary", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _detail(c, order_id):
    r = c.get(f"/api/purchases/analytics/orders/{order_id}")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- 单头口径手算

# 每单 (不含税, 含税, 实付, nominal_side)：
#   H1 含税13%：原值 1130；不含税 1130/1.13 = 1000.00 → (1000.00, 1130, 1130, None)
#   H2 不含税13%：原值 1000；含税 1000×1.13 = 1130.00 → (1000, 1130.00, 1130, None)
#   H3 含税6%、不含税列缺失：原值 1060；不含税 1060/1.06 = 1000.00 → (1000.00, 1060, 1060, None)
#   H4 不含税0.6%：原值 1000；含税 1000×1.006 = 1006.00 → (1000, 1006.00, None, None)
#   H5 不含税0税率→13%：原值 2000；含税 2260.00（名义）→ (2000, 2260.00, None, "inc")
#   H6 不含税NULL税率→13%：原值 3000；含税 3390.00（名义）→ (3000, 3390.00, None, "inc")
#   H7 含税0税率→13%：原值 2260；不含税 2260/1.13 = 2000.00（名义）→ (2000.00, 2260, 2260, "ex")
#   H8 标记未标注→按不含税单：原值 500（两列同值）；含税 565.00（名义）→ (500, 565.00, 500, "inc")
#   H9 历史缺口（含税单缺含税额）：录入不含税 1000；含税 1000×1.13 = 1130.00
#       才是推算侧 → (1000, 1130.00, None, "inc")  ← 方向翻转：含税单却标 "inc"
#   H10 历史缺口（不含税单缺不含税额、税率0→13%）：录入含税 2260；不含税
#       2260/1.13 = 2000.00 才是推算侧 → (2000.00, 2260, 2260, "ex")  ← 翻转
#   H11 双侧金额列皆空：金额全 None，无从谈名义 → (None, None, None, None)
ORDER_EXPECTED = {
    "H1": ("1000.00", "1130.00", "1130", None),
    "H2": ("1000.00", "1130.00", "1130", None),
    "H3": ("1000.00", "1060.00", "1060", None),
    "H4": ("1000.00", "1006.00", None, None),
    "H5": ("2000.00", "2260.00", None, "inc"),
    "H6": ("3000.00", "3390.00", None, "inc"),
    "H7": ("2000.00", "2260.00", "2260", "ex"),
    "H8": ("500.00", "565.00", "500", "inc"),
    "H9": ("1000.00", "1130.00", None, "inc"),
    "H10": ("2000.00", "2260.00", "2260", "ex"),
    "H11": (None, None, None, None),
}


def _approx_or_none(value, expected):
    # 金额精确到分，禁用随合计金额放大的相对容差，不能放过一分钱的差异。
    return (value == pytest.approx(float(expected), rel=0, abs=0.005)
            if expected is not None else value is None)


def test_handcalc_order_detail_amounts(handcalc_ds):
    c, pos = handcalc_ds
    for key, (ex, inc, paid, side) in ORDER_EXPECTED.items():
        d = _detail(c, pos[key].id)
        assert _approx_or_none(d["amount_ex"], ex), key
        assert _approx_or_none(d["amount_inc"], inc), key
        assert _approx_or_none(d["amount_paid"], paid), key
        assert d["nominal_side"] == side, key


def test_handcalc_order_mode_summary_totals(handcalc_ds):
    """合计（单头口径）：
    inc = 1130+1130+1060+1006+2260+3390+2260+565 +H9 1130 +H10 2260 = 16191.00
    ex  = 1000+1000+1000+1000+2000+3000+2000+500 +H9 1000 +H10 2000 = 14500.00
    paid = 1130+1130+1060+2260+500 +H10 2260 = 8340（含税列缺失的单不计：
          H4/H5/H6/H9/H11）
    nominal_inc 分子（实际推算含税侧）= H5 2260+H6 3390+H8 565+H9 1130 = 7345
        → 7345/16191 = 0.4536
    nominal_ex  分子（实际推算不含税侧）= H7 2000+H10 2000 = 4000
        → 4000/14500 = 0.2759（H10 缺口翻转：录入的含税 2260 不再被计入）"""
    c, _pos = handcalc_ds
    kpi = _summary(c)["kpi"]
    assert kpi["orders"] == 11
    assert kpi["amount_inc"] == pytest.approx(16191.00)
    assert kpi["amount_ex"] == pytest.approx(14500.00)
    assert kpi["amount_paid"] == pytest.approx(8340.00)
    assert kpi["nominal_inc_share"] == pytest.approx(0.4536)
    assert kpi["nominal_ex_share"] == pytest.approx(0.2759)


# ------------------------------------------------------------- 行级口径手算

# 行金额（单行/单）跟随单头原始侧：含税单行不含税 = round(行/(1+率),2)，
# 不含税单行含税 = round(行×(1+率),2)；名义侧按**结构性规则**（标记决定，与
# 单头金额列缺口无关）：
#   H1 行1130 → (1000.00, 1130.00)   H2 行1000 → (1000, 1130.00)
#   H3 行1060 → (1000.00, 1060.00)   H4 行1000 → (1000, 1006.00)
#   H5 行2000 → (2000, 2260.00 名义) H6 行3000 → (3000, 3390.00 名义)
#   H7 行2260 → (2000.00 名义, 2260) H8 行500  → (500, 565.00 名义)
#   H9 行1130（含税单，缺含税列照样按标记走含税侧）→ (1000.00 名义, 1130)
#   H10 行2260（不含税单，缺不含税列照样按标记走不含税侧）→ (2260, 2553.80 名义)
#   H11 行500 → (500, 565.00 名义)
# 合计：inc = 1130+1130+1060+1006+2260+3390+2260+565+1130+2553.80+565 = 17049.80
#       ex  = 1000+1000+1000+1000+2000+3000+2000+500+1000+2260+500 = 15260.00
#       paid = 全部行金额 = 1130+1000+1060+1000+2000+3000+2260+500
#              +1130+2260+500 = 15840.00
# nominal_inc 分子（结构性 "inc"：H5/H6/H8/H10/H11）= 2260+3390+565+2553.80+565
#     = 9333.80 → 9333.80/17049.80 = 0.5474（H10 行级与单头口径方向相反，见 H10 注）
# nominal_ex  分子（"ex"：H7/H9）= 2000+1000 = 3000 → 3000/15260 = 0.1966
def test_handcalc_line_mode_summary_totals(handcalc_ds):
    c, _pos = handcalc_ds
    data = _summary(c, category=["电器件"])
    assert data["basis_mode"] == "line"
    kpi = data["kpi"]
    assert kpi["orders"] == 11 and kpi["lines"] == 11
    assert kpi["amount_inc"] == pytest.approx(17049.80)
    assert kpi["amount_ex"] == pytest.approx(15260.00)
    assert kpi["amount_paid"] == pytest.approx(15840.00)
    assert kpi["nominal_inc_share"] == pytest.approx(0.5474)
    assert kpi["nominal_ex_share"] == pytest.approx(0.1966)


def test_handcalc_line_mode_per_order_rows(handcalc_ds):
    c, _pos = handcalc_ds
    r = c.get("/api/purchases/analytics/orders",
              params={"category": ["电器件"], "page_size": 20})
    assert r.status_code == 200, r.text
    by_no = {i["order_no"]: i for i in r.json()["items"]}
    # H7 含税 0 税率：行不含税 = 2260/1.13 = 2000.00（名义侧 ex）；实付 = 行金额 2260
    assert by_no["CG-H7"]["amount_ex"] == pytest.approx(2000.00)
    assert by_no["CG-H7"]["amount_inc"] == pytest.approx(2260.00)
    assert by_no["CG-H7"]["amount_paid"] == pytest.approx(2260.00)
    assert by_no["CG-H7"]["nominal_side"] == "ex"
    # H6 NULL 税率不含税单：行含税 = 3000×1.13 = 3390.00（名义侧 inc）；实付 = 行金额
    assert by_no["CG-H6"]["amount_inc"] == pytest.approx(3390.00)
    assert by_no["CG-H6"]["amount_ex"] == pytest.approx(3000.00)
    assert by_no["CG-H6"]["amount_paid"] == pytest.approx(3000.00)
    assert by_no["CG-H6"]["nominal_side"] == "inc"
    # H9/H10 历史缺口单：行级名义侧按**标记结构**（与单头口径的翻转方向相反）——
    # H9 含税单（缺含税列）行不含税 = 1130/1.13 = 1000.00 是推算侧 → "ex"，
    # 而其单头口径 nominal_side = "inc"（明细接口，见 ORDER_EXPECTED）
    assert by_no["CG-H9"]["amount_ex"] == pytest.approx(1000.00)
    assert by_no["CG-H9"]["amount_inc"] == pytest.approx(1130.00)
    assert by_no["CG-H9"]["nominal_side"] == "ex"
    # H10 不含税单（缺不含税列）行含税 = 2260×1.13 = 2553.80 是推算侧 → "inc"，
    # 而其单头口径 nominal_side = "ex"
    assert by_no["CG-H10"]["amount_ex"] == pytest.approx(2260.00)
    assert by_no["CG-H10"]["amount_inc"] == pytest.approx(2553.80)
    assert by_no["CG-H10"]["nominal_side"] == "inc"
    # H11 双侧金额列皆空：行级金额仍可算（行金额在），名义侧按结构 → "inc"
    assert by_no["CG-H11"]["amount_ex"] == pytest.approx(500.00)
    assert by_no["CG-H11"]["amount_inc"] == pytest.approx(565.00)
    assert by_no["CG-H11"]["nominal_side"] == "inc"


# ------------------------------------------------------------- 名义占比边界

def test_handcalc_share_denominator_zero_or_missing(handcalc_ds):
    """占比边界：分子为 0 但分母存在 → 0.0（H2 已标税率，筛它时无名义金额）；
    分母为 0（空区间）→ null，不得出现 0/0 或 NaN。"""
    c, _pos = handcalc_ds
    only_h2 = _summary(c, purchaser=["已标税率采购员"])
    assert only_h2["kpi"]["orders"] == 1
    assert only_h2["kpi"]["amount_inc"] == pytest.approx(1130.00)
    assert only_h2["kpi"]["nominal_inc_share"] == pytest.approx(0.0)
    assert only_h2["kpi"]["nominal_ex_share"] == pytest.approx(0.0)
    # 空区间：H 系列都在 07-01，06-30 单日窗口排除全部 → 合计 0 → 占比 null
    empty = _summary(c, date_from="2026-06-30", date_to="2026-06-30")
    assert empty["kpi"]["orders"] == 0
    assert empty["kpi"]["nominal_inc_share"] is None
    assert empty["kpi"]["nominal_ex_share"] is None


# ------------------------------------------------------------- 完整税率 / 缺列矩阵

# 每个税率 × 标记分别列出「双列都有 / 仅不含税列 / 仅含税列 / 行级」的独立期望：
# (不含税, 含税, 实付, nominal_side, nominal_inc_share, nominal_ex_share)。
# 单头源列固定 ex=1000 / inc=1130，行金额固定 1130；6% 与 0.6% 的两列故意
# 不相容，确保实现尊重权威侧，而不是碰巧拿到正确的另一列。除法手算到分：
# 1130/1.06=1066.04；1130/1.006=1123.26。此处不调用服务或税率函数推导期望。
COMPLETE_MATRIX = [
    ("13-inc", "0.13", True,
     ("1000", "1130", "1130", None, 0.0, 0.0),
     ("1000", "1130", None, None, 0.0, 0.0),
     ("1000", "1130", "1130", None, 0.0, 0.0),
     ("1000", "1130", "1130", None, 0.0, 0.0)),
    ("13-ex", "0.13", False,
     ("1000", "1130", "1130", None, 0.0, 0.0),
     ("1000", "1130", None, None, 0.0, 0.0),
     ("1000", "1130", "1130", None, 0.0, 0.0),
     ("1130", "1276.90", "1130", None, 0.0, 0.0)),
    ("13-null", "0.13", None,
     ("1000", "1130", "1130", None, 0.0, 0.0),
     ("1000", "1130", None, None, 0.0, 0.0),
     ("1000", "1130", "1130", None, 0.0, 0.0),
     ("1130", "1276.90", "1130", None, 0.0, 0.0)),
    ("6-inc", "0.06", True,
     ("1066.04", "1130", "1130", None, 0.0, 0.0),
     ("1000", "1060", None, None, 0.0, 0.0),
     ("1066.04", "1130", "1130", None, 0.0, 0.0),
     ("1066.04", "1130", "1130", None, 0.0, 0.0)),
    ("6-ex", "0.06", False,
     ("1000", "1060", "1130", None, 0.0, 0.0),
     ("1000", "1060", None, None, 0.0, 0.0),
     ("1066.04", "1130", "1130", None, 0.0, 0.0),
     ("1130", "1197.80", "1130", None, 0.0, 0.0)),
    ("6-null", "0.06", None,
     ("1000", "1060", "1130", None, 0.0, 0.0),
     ("1000", "1060", None, None, 0.0, 0.0),
     ("1066.04", "1130", "1130", None, 0.0, 0.0),
     ("1130", "1197.80", "1130", None, 0.0, 0.0)),
    ("point6-inc", "0.006", True,
     ("1123.26", "1130", "1130", None, 0.0, 0.0),
     ("1000", "1006", None, None, 0.0, 0.0),
     ("1123.26", "1130", "1130", None, 0.0, 0.0),
     ("1123.26", "1130", "1130", None, 0.0, 0.0)),
    ("point6-ex", "0.006", False,
     ("1000", "1006", "1130", None, 0.0, 0.0),
     ("1000", "1006", None, None, 0.0, 0.0),
     ("1123.26", "1130", "1130", None, 0.0, 0.0),
     ("1130", "1136.78", "1130", None, 0.0, 0.0)),
    ("point6-null", "0.006", None,
     ("1000", "1006", "1130", None, 0.0, 0.0),
     ("1000", "1006", None, None, 0.0, 0.0),
     ("1123.26", "1130", "1130", None, 0.0, 0.0),
     ("1130", "1136.78", "1130", None, 0.0, 0.0)),
    ("zero-inc", "0", True,
     ("1000", "1130", "1130", "ex", 0.0, 1.0),
     ("1000", "1130", None, "inc", 1.0, 0.0),
     ("1000", "1130", "1130", "ex", 0.0, 1.0),
     ("1000", "1130", "1130", "ex", 0.0, 1.0)),
    ("zero-ex", "0", False,
     ("1000", "1130", "1130", "inc", 1.0, 0.0),
     ("1000", "1130", None, "inc", 1.0, 0.0),
     ("1000", "1130", "1130", "ex", 0.0, 1.0),
     ("1130", "1276.90", "1130", "inc", 1.0, 0.0)),
    ("zero-null", "0", None,
     ("1000", "1130", "1130", "inc", 1.0, 0.0),
     ("1000", "1130", None, "inc", 1.0, 0.0),
     ("1000", "1130", "1130", "ex", 0.0, 1.0),
     ("1130", "1276.90", "1130", "inc", 1.0, 0.0)),
    ("null-inc", None, True,
     ("1000", "1130", "1130", "ex", 0.0, 1.0),
     ("1000", "1130", None, "inc", 1.0, 0.0),
     ("1000", "1130", "1130", "ex", 0.0, 1.0),
     ("1000", "1130", "1130", "ex", 0.0, 1.0)),
    ("null-ex", None, False,
     ("1000", "1130", "1130", "inc", 1.0, 0.0),
     ("1000", "1130", None, "inc", 1.0, 0.0),
     ("1000", "1130", "1130", "ex", 0.0, 1.0),
     ("1130", "1276.90", "1130", "inc", 1.0, 0.0)),
    ("null-null", None, None,
     ("1000", "1130", "1130", "inc", 1.0, 0.0),
     ("1000", "1130", None, "inc", 1.0, 0.0),
     ("1000", "1130", "1130", "ex", 0.0, 1.0),
     ("1130", "1276.90", "1130", "inc", 1.0, 0.0)),
]
NO_HEADER_AMOUNTS = (None, None, None, None, None, None)


def _matrix_order(db, batch, part, key, *, rate, is_inc, ex, inc, line_amount):
    po = FPurchaseOrder(
        raw_order_id=key, order_no=f"CG-{key}", order_date=date(2026, 7, 2),
        purchaser=key, source_type="销售订单", source_type_raw="销售订单",
        is_tax_inclusive=is_inc, tax_rate=D(rate) if rate is not None else None,
        amount_ex_tax=D(ex) if ex is not None else None,
        amount_inc_tax=D(inc) if inc is not None else None,
        data_status="已生效", import_batch_id=batch.id)
    db.add(po)
    db.flush()
    db.add(FPurchaseLine(
        raw_line_id=f"{key}-L1", order_id=po.id, line_no=1, part_id=part.id,
        pn_std=part.pn_std, qty=D("1"), unit_price=D(line_amount),
        line_amount=D(line_amount), import_batch_id=batch.id))
    return po.id


@pytest.fixture()
def complete_matrix_ds(db: Session):
    """一次建 60 单；每单独立采购员分组，保留逐格占比断言，避免 60 次建库/清表。"""
    batch = _batch(db, "complete-matrix")
    part = DimPart(pn_std="PN-MATRIX", category_major="矩阵类别")
    db.add(part)
    db.flush()
    expected = {}
    for label, rate, is_inc, both, only_ex, only_inc, line in COMPLETE_MATRIX:
        for state, ex, inc, header in (
            ("both", "1000", "1130", both),
            ("only-ex", "1000", None, only_ex),
            ("only-inc", None, "1130", only_inc),
            ("neither", None, None, NO_HEADER_AMOUNTS),
        ):
            key = f"M-{label}-{state}"
            order_id = _matrix_order(
                db, batch, part, key, rate=rate, is_inc=is_inc,
                ex=ex, inc=inc, line_amount="1130")
            expected[key] = (order_id, header, line)
    db.commit()
    return client_for(db, username="complete-matrix-boss", role="boss"), expected


def _assert_matrix_amounts(actual, expected, case):
    for field, value in zip(("amount_ex", "amount_inc", "amount_paid"), expected[:3]):
        assert _approx_or_none(actual[field], value), (case, field, actual[field], value)


def _assert_matrix_group(actual, expected, case):
    _assert_matrix_amounts(actual, expected, case)
    assert actual["orders"] == 1 and actual["lines"] == 1, case
    assert actual["nominal_inc_share"] == expected[4], case
    assert actual["nominal_ex_share"] == expected[5], case


def test_handcalc_complete_rate_flag_and_missing_column_matrix(complete_matrix_ds):
    """SQL 列表/分组和 Python 明细均经公开 API，对独立字面量核对完整 60 格。"""
    client, expected = complete_matrix_ds
    order_response = client.get("/api/purchases/analytics/orders", params={"page_size": 200})
    line_response = client.get(
        "/api/purchases/analytics/orders", params={"page_size": 200, "category": ["矩阵类别"]})
    assert order_response.status_code == line_response.status_code == 200
    orders = {row["purchaser"]: row for row in order_response.json()["items"]}
    lines = {row["purchaser"]: row for row in line_response.json()["items"]}
    order_summary = _summary(client, group_by="purchaser")
    line_summary = _summary(client, group_by="purchaser", category=["矩阵类别"])
    order_groups = {row["key"]: row for row in order_summary["groups"]}
    line_groups = {row["key"]: row for row in line_summary["groups"]}
    assert set(orders) == set(lines) == set(order_groups) == set(line_groups) == set(expected)
    assert len(expected) == 60
    for key, (order_id, header, line) in expected.items():
        _assert_matrix_amounts(orders[key], header, (key, "单头列表"))
        assert orders[key]["nominal_side"] == header[3], key
        _assert_matrix_amounts(lines[key], line, (key, "行级列表"))
        assert lines[key]["nominal_side"] == line[3], key
        _assert_matrix_group(order_groups[key], header, (key, "单头分组"))
        _assert_matrix_group(line_groups[key], line, (key, "行级分组"))
        response = client.get(
            f"/api/purchases/analytics/orders/{order_id}", params={"category": ["矩阵类别"]})
        assert response.status_code == 200, (key, response.text)
        detail = response.json()
        _assert_matrix_amounts(detail, header, (key, "单头明细"))
        assert detail["nominal_side"] == header[3], key
        _assert_matrix_amounts(detail["category_matched"], line, (key, "匹配行合计"))
        assert len(detail["lines"]) == 1
        assert _approx_or_none(detail["lines"][0]["line_amount_ex"], line[0]), key
        assert _approx_or_none(detail["lines"][0]["line_amount_inc"], line[1]), key

    # 五档全矩阵手算：两档未标税率的单头名义分子 inc=11300、ex=8000；
    # 行级分子 inc=20430.40、ex=8000。单头双空不能稀释名义值，行金额仍计入。
    assert order_summary["kpi"]["orders"] == line_summary["kpi"]["orders"] == 60
    _assert_matrix_amounts(order_summary["kpi"], ("45757.20", "49880", "33900"), "单头 KPI")
    assert order_summary["kpi"]["nominal_inc_share"] == pytest.approx(0.2265)
    assert order_summary["kpi"]["nominal_ex_share"] == pytest.approx(0.1748)
    _assert_matrix_amounts(line_summary["kpi"], ("65957.20", "71922.24", "67800"), "行级 KPI")
    assert line_summary["kpi"]["nominal_inc_share"] == pytest.approx(0.2841)
    assert line_summary["kpi"]["nominal_ex_share"] == pytest.approx(0.1213)


def test_handcalc_zero_authoritative_amount_never_falls_back(db):
    """权威列 0 是真实录入，不因 falsy 回退另一列；名义侧保留而零分母占比为空。"""
    batch = _batch(db, "zero-authoritative")
    part = DimPart(pn_std="PN-ZERO-AUTHORITY", category_major="零值类别")
    db.add(part)
    db.flush()
    expected = {}
    for key, rate, is_inc, ex, inc, result in (
        ("Z-inc-zero", "0", True, "1000", "0", ("0", "0", "0", "ex", None, None)),
        ("Z-inc-null", None, True, "1000", "0", ("0", "0", "0", "ex", None, None)),
        ("Z-ex-zero", "0", False, "0", "1130", ("0", "0", "1130", "inc", None, None)),
        ("Z-ex-null", None, False, "0", "1130", ("0", "0", "1130", "inc", None, None)),
        ("Z-null-zero", "0", None, "0", "1130", ("0", "0", "1130", "inc", None, None)),
        ("Z-null-null", None, None, "0", "1130", ("0", "0", "1130", "inc", None, None)),
    ):
        order_id = _matrix_order(
            db, batch, part, key, rate=rate, is_inc=is_inc,
            ex=ex, inc=inc, line_amount="0")
        expected[key] = (order_id, result)
    db.commit()
    client = client_for(db, username="zero-authoritative-boss", role="boss")
    response = client.get("/api/purchases/analytics/orders")
    assert response.status_code == 200, response.text
    orders = {row["purchaser"]: row for row in response.json()["items"]}
    summary = _summary(client, group_by="purchaser")
    groups = {row["key"]: row for row in summary["groups"]}
    assert set(orders) == set(groups) == set(expected)
    for key, (order_id, result) in expected.items():
        _assert_matrix_amounts(orders[key], result, key)
        assert orders[key]["nominal_side"] == result[3], key
        detail = _detail(client, order_id)
        _assert_matrix_amounts(detail, result, key)
        assert detail["nominal_side"] == result[3], key
        _assert_matrix_group(groups[key], result, key)
    line_summary = _summary(client, group_by="purchaser", category=["零值类别"])
    assert len(line_summary["groups"]) == 6
    for group in line_summary["groups"]:
        _assert_matrix_group(group, ("0", "0", "0", None, None, None), group["key"])
