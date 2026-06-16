"""行级异常标记的唯一判定处（决策树根，§6.8/§7.3）。

「零价 / 行金额与数量×单价不符」这套行级数值规则——连同容差 0.05——只在这里写一份：
- transform 落库（采购/销售初值）取它；
- profit 重算销售行时也取它，再在其上追加成本/业务维度分支（no_cost / neg_margin / excluded_*）。

此前规则在 transform 与 profit 各写一份、容差各填一个 0.05，改一处另一处会悄悄漂移。
现在 line_flags 是决策树的根，两层共用；上层只负责往根上挂自己的分支。

本模块刻意不依赖 pandas / DB / 业务配置——只看本行三个数，便于被任意层复用与单测。
"""
from decimal import Decimal

AMOUNT_TOL = Decimal("0.05")   # 行金额与 数量×单价 的允许误差（含税口径，分位四舍五入噪声）


def line_flags(qty, unit_price, line_amount) -> list[str]:
    """行级数值异常（不依赖成本/业务上下文）。

    None 一律视作「无此值」→ 不参与判定、不误标，与原 transform 口径一致。
    （profit 传入的 qty/unit_price 已 None→0 兜底，故 0 价仍会触发 zero_price。）
    """
    flags: list[str] = []
    if unit_price is not None and unit_price == 0:
        flags.append("zero_price")
    if (
        line_amount is not None
        and qty is not None
        and unit_price is not None
        and abs(line_amount - qty * unit_price) > AMOUNT_TOL
    ):
        flags.append("amount_mismatch")
    return flags
