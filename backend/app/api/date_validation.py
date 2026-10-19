"""HTTP 日期查询的共用边界校验；不捕获业务层的普通 ValueError。"""

from datetime import date

from fastapi import HTTPException, Query


def validate_date_order(
    date_from: date | None,
    date_to: date | None,
    *,
    from_name: str = "date_from",
    to_name: str = "date_to",
) -> None:
    """允许单边范围和同一天；两个边界都有值时必须按时间递增。"""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, f"{from_name} 不能晚于 {to_name}")


def validate_date_query(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> None:
    validate_date_order(date_from, date_to)


def validate_from_to_query(
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
) -> None:
    validate_date_order(date_from, date_to, from_name="from", to_name="to")
