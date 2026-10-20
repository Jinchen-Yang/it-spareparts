"""已上线商城参考价消费者 DTO 的冻结副本（2026-10-20）。

来源：本地 wt-shop-priceref-itsp/backend/app/services/price_reference.py，
与用户给定的已上线字段契约核对，仅保留纯 Pydantic 模型；未访问线上系统。
不依赖提供方 schema，避免生产者与测试同时增加字段后失去 extra=forbid 保护。
更新时须明确消费者版本；本次采购扩展不得改动其字段集合与校验行为。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TaxBasis = Literal["inc_tax", "ex_tax"]


class ReferenceWindow(BaseModel):
    """销售均价统计窗口，闭区间。"""

    model_config = ConfigDict(extra="forbid")
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> "ReferenceWindow":
        if self.start > self.end:
            raise ValueError("参考价统计窗口起始日期不能晚于结束日期")
        return self


class ReferencePrice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["purchase", "sale"]
    amount: Decimal = Field(ge=0, max_digits=18, decimal_places=4)
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    occurred_at: datetime
    source: str = Field(max_length=256)
    # 销售均价扩展均为可选：旧生产者不返回时按原逐笔价格展示，不推断税基或样本。
    tax_basis: TaxBasis | None = None
    sample_count: int | None = Field(default=None, ge=0, strict=True)
    order_count: int | None = Field(default=None, ge=0, strict=True)
    window: ReferenceWindow | None = None
    latest_sale_date: date | None = None
    oldest_sale_date: date | None = None
    nominal_sample_count: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def consistent_sample_metadata(self) -> "ReferencePrice":
        if (
            self.sample_count is not None
            and self.order_count is not None
            and self.order_count > self.sample_count
        ):
            raise ValueError("订单数不能大于样本行数")
        if (
            self.sample_count is not None
            and self.nominal_sample_count is not None
            and self.nominal_sample_count > self.sample_count
        ):
            raise ValueError("名义样本数不能大于样本行数")
        if self.window is not None:
            for day in (self.latest_sale_date, self.oldest_sale_date):
                if day is not None and not self.window.start <= day <= self.window.end:
                    raise ValueError("销售日期必须位于统计窗口内")
        if (
            self.oldest_sale_date is not None
            and self.latest_sale_date is not None
            and self.oldest_sale_date > self.latest_sale_date
        ):
            raise ValueError("最旧销售日不能晚于最近销售日")
        return self


class SalesAverageCalculation(BaseModel):
    """金臣 2026-10-19 09:33Z 口径：30 天窗口 + 指数时间衰减。

    顶层 calculation 可选，仅用于兼容旧逐笔价格响应；新 it-spareparts 必须完整填写。
    """

    model_config = ConfigDict(extra="forbid")
    method: Literal["time_decay_qty_weighted", "time_decay_line_weighted"]
    decay: Literal["exponential_half_life"]
    window_days: int = Field(ge=1, le=366, strict=True)
    half_life_days: Decimal = Field(gt=0, le=3650)
    date_basis: Literal["order_date"]
    timezone: Literal["Asia/Shanghai"]
    window: ReferenceWindow
    order_statuses: list[str] = Field(min_length=1, max_length=20)
    counts_revenue_only: bool
    positive_qty_only: bool
    include_zero_prices: bool
    exclude_confirmed_source_error: bool
    sales_unit_price_basis: Literal["inc_tax"]
    tax_policy: Literal["order_rate_or_13pct"]
    fallback_tax_rate: Decimal = Field(ge=0, lt=1)
    currency: Literal["CNY"]
    sample_count: int = Field(ge=0, strict=True)
    order_count: int = Field(ge=0, strict=True)
    latest_sale_date: date | None = None
    oldest_sale_date: date | None = None

    @model_validator(mode="after")
    def consistent_counts_and_dates(self) -> "SalesAverageCalculation":
        if self.order_count > self.sample_count:
            raise ValueError("订单数不能大于样本行数")
        for day in (self.latest_sale_date, self.oldest_sale_date):
            if day is not None and not self.window.start <= day <= self.window.end:
                raise ValueError("销售日期必须位于统计窗口内")
        if (
            self.oldest_sale_date is not None
            and self.latest_sale_date is not None
            and self.oldest_sale_date > self.latest_sale_date
        ):
            raise ValueError("最旧销售日不能晚于最近销售日")
        if self.sample_count == 0:
            if (
                self.order_count != 0
                or self.latest_sale_date is not None
                or self.oldest_sale_date is not None
            ):
                raise ValueError("无销售样本时计数必须为 0 且不能返回销售日期")
        elif self.latest_sale_date is None or self.oldest_sale_date is None:
            raise ValueError("有销售样本时必须提供最近和最旧销售日")
        return self


class PriceReferenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pn: str
    prices: list[ReferencePrice] = Field(max_length=100)
    reference_only: Literal[True] = True
    calculation: SalesAverageCalculation | None = None

    @model_validator(mode="after")
    def consistent_sales_average(self) -> "PriceReferenceResult":
        calculation = self.calculation
        if calculation is None:
            # 旧生产者没有销售均价口径，保持原逐笔价格契约，不追加推断。
            return self
        sale_prices = [price for price in self.prices if price.kind == "sale"]
        if not sale_prices:
            if calculation.sample_count != 0 or calculation.order_count != 0:
                raise ValueError("无销售价格时样本数与订单数必须为 0")
            if calculation.latest_sale_date is not None or calculation.oldest_sale_date is not None:
                raise ValueError("无销售价格时不能返回销售日期")
            return self
        seen_tax_bases: set[str] = set()
        for price in sale_prices:
            if any(
                getattr(price, name) is None
                for name in (
                    "tax_basis",
                    "sample_count",
                    "order_count",
                    "window",
                    "latest_sale_date",
                    "oldest_sale_date",
                    "nominal_sample_count",
                )
            ):
                raise ValueError("销售均价条目必须包含完整口径字段")
            if price.tax_basis in seen_tax_bases:
                raise ValueError("同一响应的销售均价税基必须不同")
            seen_tax_bases.add(price.tax_basis)
            if (
                price.sample_count != calculation.sample_count
                or price.order_count != calculation.order_count
                or price.window != calculation.window
                or price.latest_sale_date != calculation.latest_sale_date
                or price.oldest_sale_date != calculation.oldest_sale_date
            ):
                raise ValueError("销售均价条目与 calculation 口径不一致")
        if seen_tax_bases != {"inc_tax", "ex_tax"}:
            raise ValueError("销售均价必须同时包含含税与不含税两条")
        return self
