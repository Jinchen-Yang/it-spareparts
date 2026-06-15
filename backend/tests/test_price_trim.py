"""平均销售价 / 成交参考价：只剔除 ¥0（无真实成交价），有售价即计入，不做离群裁剪。

用户口径：硬件成交价波动大，偏离中位数的也照算；唯独 ¥0（赠送/换货/录入0价）不计入均价。
逻辑在 part_overview._positive_priced。
"""
from decimal import Decimal as D

from app.services.part_overview import _positive_priced


def test_drops_zero_keeps_all_priced():
    rows = [(D(1), D("0")), (D(1), D("20000")), (D(2), D("21000")), (D(1), D("30000"))]
    prices = sorted(r[1] for r in _positive_priced(rows, 1))
    assert D("0") not in prices
    assert prices == [D("20000"), D("21000"), D("30000")]   # 大价差(>30%)也全保留


def test_drops_none_and_nonpositive():
    rows = [(D(1), None), (D(1), D("0")), (D(1), D("-5")), (D(1), D("100"))]
    assert [r[1] for r in _positive_priced(rows, 1)] == [D("100")]


def test_empty():
    assert _positive_priced([], 1) == []
