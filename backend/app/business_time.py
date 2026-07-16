"""统一业务日口径。

生产容器可能使用 UTC；所有面向甲方的“今天”必须按中国标准时间判断，
不能依赖宿主机或容器的本地时区。
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def business_today(now: datetime | None = None) -> date:
    """返回中国标准时间下的业务日期；可注入时刻以测试跨日边界。"""
    current = now or datetime.now(BUSINESS_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BUSINESS_TZ)
    return current.astimezone(BUSINESS_TZ).date()
