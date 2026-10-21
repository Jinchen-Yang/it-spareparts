"""R34 估价政策（D-0024）：去 20%/5%/30 天旧预设，比例缺省 50% 可调。

旧 R33 政策（坏件 0.20/废品 0.05/成品 1.00 预设系数 + 30 天窗口）必须移除；
Sunny 01:20 修正（金臣 00:51）：缺省 = 50%，20%/30% 仅可选，显式 0 保留为 0。
"""

from decimal import Decimal

import pytest

from app.services import maintenance_dashboard_valuation_policy as policy


# ---------- 价值保留比例解析 ----------

def test_parse_ratio_none_and_empty():
    assert policy.parse_retention_ratio(None) is None
    assert policy.parse_retention_ratio("") is None
    assert policy.parse_retention_ratio("  ") is None


@pytest.mark.parametrize("raw,expected", [
    ("0.5", Decimal("0.5")), ("0", Decimal("0")), ("1", Decimal("1")),
    ("0.30", Decimal("0.30")), ("0.05", Decimal("0.05")),
    ("0.3333", Decimal("0.3333")),
])
def test_parse_ratio_valid(raw, expected):
    assert policy.parse_retention_ratio(raw) == expected


@pytest.mark.parametrize("raw", ["1.01", "-0.1", "2", "abc", "NaN", "Infinity", "-"])
def test_parse_ratio_rejects_out_of_range_or_invalid(raw):
    with pytest.raises(policy.PolicyInputError) as raised:
        policy.parse_retention_ratio(raw)
    assert raised.value.code == "invalid_retention_ratio"


def test_resolve_ratio_defaults_to_half_blank_included():
    """Sunny 01:20：未提供/空白 → 默认 0.5（HTTP 缺省口径）。"""
    assert policy.DEFAULT_RETENTION_RATIO == Decimal("0.5")
    assert policy.resolve_retention_ratio(None) == Decimal("0.5")
    assert policy.resolve_retention_ratio("") == Decimal("0.5")
    assert policy.resolve_retention_ratio("   ") == Decimal("0.5")


@pytest.mark.parametrize("raw,expected", [
    ("0", Decimal("0")), ("0.3", Decimal("0.3")), ("0.5", Decimal("0.5")),
    ("0.25", Decimal("0.25")), ("1", Decimal("1")),
])
def test_resolve_ratio_keeps_explicit_values_including_zero(raw, expected):
    """显式值原样保留；显式 0 绝不能被 `or 0.5` 吞成默认。"""
    assert policy.resolve_retention_ratio(raw) == expected


@pytest.mark.parametrize("raw", ["1.5", "-0.1", "abc", "NaN", "Infinity"])
def test_resolve_ratio_still_rejects_invalid(raw):
    with pytest.raises(policy.PolicyInputError) as raised:
        policy.resolve_retention_ratio(raw)
    assert raised.value.code == "invalid_retention_ratio"


def test_quick_options_half_is_default_others_selectable():
    """快捷项 50%（即默认）/30%/20% 仅供选择入口，无全局写入。"""
    assert policy.RETENTION_QUICK_OPTIONS == (
        Decimal("0.5"), Decimal("0.3"), Decimal("0.2"),
    )
    assert policy.RETENTION_QUICK_OPTIONS[0] == policy.DEFAULT_RETENTION_RATIO


# ---------- 件况分类（保留 R33 语义，去预设系数） ----------

@pytest.mark.parametrize("test_result", ["坏件", "坏品", "故障", "废品"])
def test_classify_bad_conditions(test_result):
    assert policy.classify_line(test_result) == "bad"


def test_classify_finished():
    assert policy.classify_line("成品") == "finished"


@pytest.mark.parametrize("test_result", [None, "", "  ", "其他", "随便写的"])
def test_classify_unknown(test_result):
    assert policy.classify_line(test_result) == "unknown"


def test_line_factor_bad_uses_ratio_finished_full_value():
    """坏品类行 × 用户比例；成品行全值（不折算）；未知件况不估价。"""
    assert policy.line_factor("bad", Decimal("0.3")) == Decimal("0.3")
    assert policy.line_factor("bad", Decimal("0")) == Decimal("0")
    assert policy.line_factor("finished", Decimal("0.3")) == Decimal("1")
    assert policy.line_factor("unknown", Decimal("0.3")) is None


def test_line_factor_bad_without_ratio_is_awaiting_not_zero():
    """无比例时坏件行不给系数（待试算），绝不当 0（HTTP 层已按 0.5 缺省）。"""
    assert policy.line_factor("bad", None) is None
    assert policy.line_factor("finished", None) == Decimal("1")


# ---------- 政策载荷 ----------

def test_policy_payload_r34_shape_without_ratio():
    """ratio=None（服务直调）载荷仍可构造；HTTP 层不会传 None。"""
    payload = policy.policy_payload(None)
    assert payload["policy_version"] == "mdv-2026-10-22-r34"
    assert payload["retention_ratio"] is None
    assert payload["window_days"] == 7
    assert payload["window_rule"] == "含统计结束日的 7 个上海日历日"
    assert payload["half_life_days"] == "7"
    assert "试算" in payload["notes"]
    assert payload["three_domain_rule"] == "采购/销售/领用已计成本并列，不相加"


def test_policy_payload_declares_half_default():
    """载荷声明缺省 50%、比例非必填、20/30 仅可选。"""
    payload = policy.policy_payload(policy.DEFAULT_RETENTION_RATIO)
    assert payload["retention_ratio"] == "0.5"
    assert payload["retention_ratio_required"] is False
    assert payload["retention_ratio_default"] == "0.5"
    assert payload["quick_options"] == ["0.5", "0.3", "0.2"]
    assert "缺省 50%" in payload["retention_rule"]
    assert "不写全局政策或历史成本" in payload["retention_rule"]


def test_policy_payload_echoes_ratio():
    payload = policy.policy_payload(Decimal("0.3"))
    assert payload["retention_ratio"] == "0.3"
    assert payload["quick_options"] == ["0.5", "0.3", "0.2"]
    # 显式 0 回显 "0"，不被吞成默认。
    assert policy.policy_payload(Decimal("0"))["retention_ratio"] == "0"


def test_old_r33_defaults_are_gone():
    """R34-03：旧预设系数与 factor_for 不得残留（删除守卫的静态锚点）。"""
    assert not hasattr(policy, "CONDITION_FACTORS")
    assert not hasattr(policy, "factor_for")
    assert not hasattr(policy, "EFFECTIVE_FROM")
