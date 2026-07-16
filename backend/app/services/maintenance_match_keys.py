"""维保需求号正式精确匹配键。

这是成本引擎 A0 直配与只读归因报告的共同业务边界。行为刻意保留历史语义：
``None`` 和空字符串返回 ``None``，纯空白字符串因为原值 truthy 而返回空字符串，
其余文本去首尾空白并转大写。不要在此加入标点规整；宽松键只属于归因报告。
"""


def exact_match_key(value: str | None) -> str | None:
    return value.strip().upper() if value else None
