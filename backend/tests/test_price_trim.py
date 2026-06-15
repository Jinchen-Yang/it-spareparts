"""成交价均价 / 参考价的离群裁剪：剔除 ¥0 与偏离中位数 ±30% 的异常价。

用户实证：销售历史里有 ¥0（赠送/换货/录入0价）和个别异常高价被算进了"平均销售价"，
要求 ¥0 不计入均价、偏离 30% 的也不计入。逻辑在 part_overview._trim_outliers。
"""
from decimal import Decimal as D

from app.services.part_overview import _median, _trim_outliers


def test_median():
    assert _median([D(1), D(3), D(2)]) == D(2)
    assert _median([D(10), D(20)]) == D(15)
    assert _median([]) is None


def test_trim_excludes_zero_price():
    rows = [(D(1), D("0")), (D(1), D("20000")), (D(2), D("21000")), (D(1), D("19000"))]
    prices = [r[1] for r in _trim_outliers(rows, 1, 0.30)]
    assert D("0") not in prices
    assert D("20000") in prices and D("21000") in prices


def test_trim_excludes_30pct_outlier():
    # 中位数 21000，±30% 带 = [14700, 27300]，30000 超出应被裁
    rows = [(D(1), D("19200")), (D(1), D("21000")), (D(1), D("30000"))]
    prices = sorted(r[1] for r in _trim_outliers(rows, 1, 0.30))
    assert prices == [D("19200"), D("21000")]


def test_trim_small_sample_only_drops_zero():
    # 去 ¥0 后只剩 2 条（<3）→ 不做中位数裁剪，两条都留
    rows = [(D(1), D("0")), (D(1), D("30000")), (D(1), D("19200"))]
    prices = sorted(r[1] for r in _trim_outliers(rows, 1, 0.30))
    assert prices == [D("19200"), D("30000")]


def test_trim_none_and_all_kept():
    assert _trim_outliers([], 1, 0.30) == []
    rows = [(D(1), D("100")), (D(1), D("101")), (D(1), D("99"))]   # 都在带内
    assert len(_trim_outliers(rows, 1, 0.30)) == 3
