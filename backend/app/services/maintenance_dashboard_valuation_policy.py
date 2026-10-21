"""维保数据看板 R34 估价政策（D-0024）：比例默认 50%，可调（Sunny 01:20 修正）。

R34（2026-10-22）裁定，覆盖 R33 旧口径；同日 01:20 修正覆盖早期「无默认」段：
- 去旧预设：不再有坏件 20%／废品 5%／成品 100% 的固定系数，窗口从 30 天
  缩到含统计结束日的 7 个上海日历日（商城 30 天参考政策不受影响）。
- 「价值保留比例」取值 [0,1]，**缺省 = 50%**（金臣 00:51：给他等值 50%，
  等值可自行设定）；20%／30% 仅作为可选项。显式 0 合法且必须保留为 0
  （不得用 ``ratio or 0.5`` 吞 0）。比例是本次试算参数，不写全局政策或
  历史成本。
- 三域并列（R34-05）：采购优先、销售回退、领用已计成本独立视角；
  三域估值不相加、不混均价、不暗换。
- 件况语义保留：未知件况（未填/其他/任意文本）不估价（待估，绝不算 0）；
  成品行按参考全值另列，不混入坏件主卡。

估价只是经营试算（PN 交易参考×假设折算；非变卖收入），不进成本/利润/库存事实。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.config import RKD_RETURN_TEST_RESULTS

POLICY_VERSION = "mdv-2026-10-22-r34"

#: Sunny 01:20 修正：缺省 50%，可调；20/30 仅可选（金臣 00:51 原话裁定）。
DEFAULT_RETENTION_RATIO: Decimal = Decimal("0.5")

#: 快捷选择入口（50% 即默认值本身；30%/20% 仅可选项，无全局写入）。
RETENTION_QUICK_OPTIONS: tuple[Decimal, ...] = (
    Decimal("0.5"), Decimal("0.3"), Decimal("0.2"),
)

#: 成品返件按参考全值另列（R33 语义保留：成品估值不混入坏件主卡）。
FINISHED_GOODS_FACTOR = Decimal("1")

#: 少于该张数的不同订单样本标「样本少，低置信」，仍可进入已估部分。
LOW_SAMPLE_ORDER_COUNT = 2

#: 参考价来源优先级（R34-05）：采购优先，缺合格采购样本回退销售并标明；
#: 领用已计成本只作独立视角，不暗替市场参考。
PRICE_PRIORITY = "purchase_first_sales_fallback_cost_parallel"

NOTES = "估算／试算，非变卖收入；系数变化只改变试算，不改变任何业务成本、库存或已收货数量"

THREE_DOMAIN_RULE = "采购/销售/领用已计成本并列，不相加"


class PolicyInputError(Exception):
    """试算参数非法（API 层转 422）。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def parse_retention_ratio(raw: str | None) -> Decimal | None:
    """解析价值保留比例：None/空白 → None；否则必须为 [0,1] 的有限小数。"""
    if raw is None or not raw.strip():
        return None
    try:
        ratio = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise PolicyInputError("invalid_retention_ratio") from exc
    if not ratio.is_finite() or ratio < 0 or ratio > 1:
        raise PolicyInputError("invalid_retention_ratio")
    return ratio


def resolve_retention_ratio(raw: str | None) -> Decimal:
    """HTTP 层缺省解析：未提供/空白 → 默认 50%；显式 0 保留为 0。

    禁止 ``parse(...) or 0.5`` 写法——那会把显式 0 吞成默认值。
    非法/非有限/越界仍抛 PolicyInputError（API 层转 422）。
    """
    ratio = parse_retention_ratio(raw)
    if ratio is None:
        return DEFAULT_RETENTION_RATIO
    return ratio


def is_bad_condition(test_result: str | None) -> bool:
    """坏件实收口径（R33 语义保留）：RKD_RETURN_TEST_RESULTS（坏品/坏件/故障/废品）。"""
    return classify_line(test_result) == "bad"


def classify_line(test_result: str | None) -> str:
    """件况 → bad/finished/unknown；未知件况不估价（R33 语义保留）。"""
    if test_result is None or not test_result.strip() or test_result.strip() == "其他":
        return "unknown"
    stripped = test_result.strip()
    if stripped in RKD_RETURN_TEST_RESULTS:
        return "bad"
    if stripped == "成品":
        return "finished"
    return "unknown"


def line_factor(classification: str, ratio: Decimal | None) -> Decimal | None:
    """分类+比例 → 行折算系数；无比例时坏品行为 None（待试算，绝不当 0）。

    HTTP 层已按缺省 50% 解析（``resolve_retention_ratio``），None 仅在服务
    直调（库语义）时出现；显式 0 是合法折算系数 0，不是缺省。
    """
    if classification == "unknown":
        return None
    if classification == "finished":
        return FINISHED_GOODS_FACTOR
    return ratio  # bad：比例缺省 → 待试算


def policy_payload(retention_ratio: Decimal | None) -> dict:
    """响应/导出携带的政策说明（仅金额可见用户）；试算参数始终伴随结果。"""
    return {
        "policy_version": POLICY_VERSION,
        "retention_ratio": (
            format(retention_ratio, "f") if retention_ratio is not None else None
        ),
        "retention_ratio_required": False,
        "retention_ratio_default": format(DEFAULT_RETENTION_RATIO, "f"),
        "quick_options": [format(option, "f") for option in RETENTION_QUICK_OPTIONS],
        "retention_rule": (
            "价值保留比例 = 参考价×比例；缺省 50% 可调（20%/30% 仅可选），"
            "本次试算参数，不写全局政策或历史成本"
        ),
        "window_days": 7,
        "window_rule": "含统计结束日的 7 个上海日历日",
        "half_life_days": "7",
        "weighting": "数量×时间衰减（半衰期 7 天），双税分别计算",
        "three_domain_rule": THREE_DOMAIN_RULE,
        "unknown_condition_rule": "件况未知（未填/其他/任意文本）不估价",
        "finished_goods_rule": "成品按参考全值另列，不混入坏件主卡",
        "low_sample_order_count": LOW_SAMPLE_ORDER_COUNT,
        "price_priority": PRICE_PRIORITY,
        "notes": NOTES,
    }
