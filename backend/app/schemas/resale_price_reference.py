"""商城销售参考价响应 DTO（GET /integrations/resale/parts/price-reference）。

字段与商城消费契约同构；不包含客户、单号、人员、销售行／订单 ID、原始证据或
税率明细，响应只经本白名单序列化。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReferenceWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date


class ReferencePrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["sale"]
    amount: Decimal
    currency: Literal["CNY"]
    occurred_at: datetime
    source: Literal["itsp销售均价"]
    tax_basis: Literal["inc_tax", "ex_tax"]
    sample_count: int = Field(ge=0)
    order_count: int = Field(ge=0)
    window: ReferenceWindow
    latest_sale_date: date
    oldest_sale_date: date
    nominal_sample_count: int = Field(ge=0)


class SalesAverageCalculation(BaseModel):
    """本次实际执行口径；全部字段由生效 Settings 与聚合结果构建。"""

    model_config = ConfigDict(extra="forbid")

    method: Literal["time_decay_qty_weighted", "time_decay_line_weighted"]
    decay: Literal["exponential_half_life"]
    window_days: int = Field(ge=1, le=366)
    half_life_days: Decimal
    date_basis: Literal["order_date"]
    timezone: Literal["Asia/Shanghai"]
    window: ReferenceWindow
    order_statuses: list[str]
    counts_revenue_only: bool
    positive_qty_only: Literal[True]
    include_zero_prices: bool
    exclude_confirmed_source_error: bool
    sales_unit_price_basis: Literal["inc_tax"]
    tax_policy: Literal["order_rate_or_13pct"]
    fallback_tax_rate: Decimal
    currency: Literal["CNY"]
    sample_count: int = Field(ge=0)
    order_count: int = Field(ge=0)
    latest_sale_date: date | None
    oldest_sale_date: date | None


class ResalePriceReferenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pn: str
    prices: list[ReferencePrice]
    reference_only: Literal[True]
    calculation: SalesAverageCalculation
