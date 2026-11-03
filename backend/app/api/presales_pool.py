"""询价报价共享池接口（D-0052）。"""

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import Field, model_validator

from app.api.presales import Ctx, Db, Input, _call
from app.services.presales import pool

router = APIRouter(prefix="/presales/pool", tags=["presales"])


class PoolLine(Input):
    part_id: int | None = Field(default=None, gt=0)
    pn_raw: str = Field(min_length=1, max_length=256)
    description: str | None = None
    qty: Decimal = Field(default=Decimal("1"), gt=0, max_digits=14, decimal_places=3)
    require_exact_pn: bool = False
    return_defective: bool = True
    notes: str | None = None
    expected_delivery: date | None = None


class CreateInquiry(Input):
    customer_id: int | None = Field(default=None, gt=0)
    priority: Literal["normal", "urgent"] = "normal"
    notes: str | None = None
    visible_to: list[str] = Field(default_factory=list)
    lines: list[PoolLine] = Field(min_length=1, max_length=200)


class SetPrice(Input):
    expected_version: int = Field(ge=1, le=2**31 - 1)
    unit_price: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    is_tax_inclusive: bool | None = None
    price_basis: Literal["sale", "cost"] = "sale"
    lead_time: str | None = Field(default=None, max_length=256)
    qty_available: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=3)
    stock_location: str | None = Field(default=None, max_length=256)
    supplier_id: int | None = Field(default=None, gt=0)
    notes: str | None = None


class SkipLine(Input):
    expected_version: int = Field(ge=1, le=2**31 - 1)
    reason: str = Field(min_length=1, max_length=500)


class EditLine(Input):
    expected_version: int = Field(ge=1, le=2**31 - 1)
    confirm_reset: bool = False
    qty: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=3)
    return_defective: bool | None = None
    require_exact_pn: bool | None = None
    notes: str | None = None
    expected_delivery: date | None = None

    @model_validator(mode="after")
    def _conditions_not_null(self):
        for key in ("qty", "return_defective", "require_exact_pn"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} 不能为空")
        return self


@router.get("/lines")
def list_lines(db: Db, ctx: Ctx, tab: Literal["pending", "quoted", "skip", "mine", "all"] = "pending",
               q: str = Query(default="", max_length=256), page: int = Query(1, ge=1),
               page_size: int = Query(50, ge=1, le=200)):
    return _call(db, lambda: pool.list_lines(db, ctx, tab=tab, q=q, page=page, page_size=page_size))


@router.get("/counts")
def counts(db: Db, ctx: Ctx):
    return _call(db, lambda: pool.counts(db, ctx))


@router.post("/inquiries", status_code=201)
def create_inquiry(body: CreateInquiry, db: Db, ctx: Ctx):
    data = body.model_dump(exclude_unset=True)
    return _call(db, lambda: pool.create_inquiry(db, ctx, **data), write=True)


@router.put("/lines/{line_id}/price")
def set_price(line_id: int, body: SetPrice, db: Db, ctx: Ctx):
    # 只写本次传入的字段：只改单价不能清空交期、货源和备注。
    data = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    return _call(db, lambda: pool.set_price(db, line_id, ctx, data, expected_version=body.expected_version), write=True)


@router.post("/lines/{line_id}/skip")
def skip(line_id: int, body: SkipLine, db: Db, ctx: Ctx):
    return _call(db, lambda: pool.skip(db, line_id, ctx, body.reason, expected_version=body.expected_version), write=True)


@router.patch("/lines/{line_id}")
def edit_line(line_id: int, body: EditLine, db: Db, ctx: Ctx):
    data = body.model_dump(exclude_unset=True, exclude={"expected_version", "confirm_reset"})
    return _call(db, lambda: pool.edit_line(db, line_id, ctx, data, expected_version=body.expected_version,
                                            confirm_reset=body.confirm_reset), write=True)


@router.get("/lines/{line_id}/history")
def history(line_id: int, db: Db, ctx: Ctx):
    return _call(db, lambda: pool.history(db, line_id, ctx))
