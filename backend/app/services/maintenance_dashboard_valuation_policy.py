"""维保数据看板坏件估价系数政策（R33 裁定，版本化配置）。

R33（金臣 2026-10-21 15:11「按推荐就行」）第 1 条：
坏件/坏品/故障 20%、废品 5%、成品 100%；件况未知（未填/其他）不估价；
先上线、页面标注「估算」，系数做成带版本的配置。

本模块是代码内不可变配置：响应与估价导出携带 ``policy_version``；调整系数时
新增版本并保持旧口径可追溯，不写库、不加迁移。估价只是经营试算
（PN 交易参考×假设折算；非变卖收入），不进成本/利润/库存事实。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

POLICY_VERSION = "mdv-2026-10-21-r33"
EFFECTIVE_FROM = date(2026, 10, 21)

# 件况 → 折算系数。RKD_RETURN_TEST_RESULTS 之外的「成品」返件按 100% 另列。
CONDITION_FACTORS: dict[str, Decimal] = {
    "坏件": Decimal("0.20"),
    "坏品": Decimal("0.20"),
    "故障": Decimal("0.20"),
    "废品": Decimal("0.05"),
    "成品": Decimal("1.00"),
}

# 未填或其他任意文本都视为件况未知：不估价（待估），绝不算成 0。
UNKNOWN_CONDITIONS: tuple[None | str, ...] = (None, "", "其他")

# 少于该张数的不同订单样本标「样本少，低置信」，仍可进入已估部分。
LOW_SAMPLE_ORDER_COUNT = 2

# 参考价来源优先级（R33 第 7 条 / Q5）：同 PN 采购参考优先，无合格采购样本回退销售。
PRICE_PRIORITY = "purchase_first_sales_fallback"

NOTES = "PN 交易参考×假设折算；非变卖收入"


def factor_for(test_result: str | None) -> Decimal | None:
    """件况 → 系数；未知件况返回 None（不估价）。"""
    if test_result is None or test_result.strip() in ("", "其他"):
        return None
    return CONDITION_FACTORS.get(test_result.strip())


def is_bad_condition(test_result: str | None) -> bool:
    """坏件实收口径：现有 RKD_RETURN_TEST_RESULTS（坏品/坏件/故障/废品）。"""
    from app.config import RKD_RETURN_TEST_RESULTS

    return test_result is not None and test_result.strip() in RKD_RETURN_TEST_RESULTS


def policy_payload() -> dict:
    """响应/导出携带的政策说明（仅金额可见用户）。"""
    return {
        "policy_version": POLICY_VERSION,
        "effective_from": EFFECTIVE_FROM.isoformat(),
        "condition_factors": {
            name: format(factor, "f") for name, factor in CONDITION_FACTORS.items()
        },
        "unknown_condition_rule": "件况未知（未填/其他）不估价",
        "low_sample_order_count": LOW_SAMPLE_ORDER_COUNT,
        "price_priority": PRICE_PRIORITY,
        "notes": NOTES,
    }
