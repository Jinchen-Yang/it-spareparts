"""售前询价 REST 接口；服务持有业务规则，接口持有提交与错误映射。"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.audience import audience_user
from app.db import get_db
from app.models.dimensions import DimCustomer, DimPart
from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer
from app.security import UserContext
from app.services import price_reference
from app.services.presales import binding, candidates, common, inquiries, library, offers, visibility, part_history
from app.services.presales.registration import register_subjects
from app.services.resale_price_reference import PriceReferenceUnavailable
from app.services.workflow.registry import WorkflowError

router = APIRouter(prefix="/presales", tags=["presales"])
register_subjects()


def presales_user(viewer: UserContext = Depends(audience_user)):
    try:
        common.require(viewer, "page_presales")
    except WorkflowError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return viewer


Db = Annotated[Session, Depends(get_db)]
Ctx = Annotated[UserContext, Depends(presales_user)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AudienceInput(Input):
    mode: Literal["public", "private", "include", "exclude"] = "public"
    user_ids: list[str] = Field(default_factory=list)
    group_ids: list[int] = Field(default_factory=list)


class VersionInput(Input):
    expected_version: int = Field(ge=1, le=2**31 - 1)


class CustomValue(Input):
    field_def_id: int | None = Field(default=None, gt=0)
    new_name: str | None = Field(default=None, min_length=1, max_length=32)
    value: str | bool | int | float | None = None
    rule: AudienceInput | int | None = None


class LineInput(Input):
    line_key: str | None = Field(default=None, min_length=1, max_length=64)
    part_id: int | None = Field(default=None, gt=0)
    pn_raw: str | None = Field(default=None, max_length=256)
    description: str | None = None
    qty: Decimal = Field(default=Decimal("1"), gt=0, max_digits=14, decimal_places=3)
    require_exact_pn: bool = False
    ref_offer_id: int | None = Field(default=None, gt=0)
    notes: str | None = None
    notes_audience: AudienceInput | None = None
    expected_delivery: date | None = None
    field_values: list[CustomValue] | None = None


class CreateInquiry(Input):
    kind: Literal["sales_inquiry", "proactive_offer"] = "sales_inquiry"
    customer_id: int | None = Field(default=None, gt=0)
    notes: str | None = None
    lines: list[LineInput] = Field(default_factory=list, max_length=200)
    assignee_ids: list[int] = Field(default_factory=list)
    offer_audience: AudienceInput | None = None
    notes_audience: AudienceInput | None = None
    field_values: list[CustomValue] | None = None


class UpdateInquiry(VersionInput):
    customer_id: int | None = Field(default=None, gt=0)
    notes: str | None = None
    lines: list[LineInput] | None = Field(default=None, max_length=200)
    assignee_ids: list[int] | None = None
    offer_audience: AudienceInput | None = None
    notes_audience: AudienceInput | None = None
    field_values: list[CustomValue] | None = None


class AppendLines(VersionInput):
    lines: list[LineInput] = Field(min_length=1, max_length=200)


class CloseInquiry(VersionInput):
    outcome: Literal["won", "lost", "cancelled"]
    won_offer_id: int | None = Field(default=None, gt=0)


class OfferData(Input):
    unit_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    is_tax_inclusive: bool | None = None
    no_stock: bool = False
    qty_available: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=3)
    stock_location: str | None = Field(default=None, max_length=256)
    lead_time: str | None = Field(default=None, max_length=256)
    source_key: str = Field(default="", max_length=128)
    supplier_id: int | None = Field(default=None, gt=0)
    validity_days: int = Field(default=1, ge=1, le=36500)
    notes: str | None = None
    supplier_audience: AudienceInput | None = None
    reporter_audience: AudienceInput | None = None
    notes_audience: AudienceInput | None = None


class SaveOffer(OfferData, VersionInput):
    offer_id: int | None = Field(default=None, gt=0)
    work_item_id: int | None = Field(default=None, gt=0)


class UpdateOffer(OfferData, VersionInput):
    pass


class WithdrawOffer(VersionInput):
    reason: str = Field(min_length=1)


class ExistingChoice(Input):
    on_existing: Literal["update", "new"]
    existing_offer_id: int | None = Field(default=None, gt=0)


class PublishChoices(VersionInput):
    on_existing: Literal["update", "new"] | None = None
    existing_offer_id: int | None = Field(default=None, gt=0)
    existing_choices: dict[int, ExistingChoice] | None = None


class LineResponseInput(Input):
    line_id: int = Field(gt=0)
    mode: Literal["quote", "reuse", "skip"]
    ref_offer_id: int | None = Field(default=None, gt=0)
    reason: str | None = None


class ReplyInput(PublishChoices):
    work_item_id: int | None = Field(default=None, gt=0)
    expected_work_item_version: int | None = Field(default=None, ge=1)
    line_responses: list[LineResponseInput] | None = Field(default=None, max_length=200)


class PublishInput(PublishChoices):
    notice_user_ids: list[int] | None = None


class BindInput(VersionInput):
    part_id: int = Field(gt=0)


class CreatePartInput(VersionInput):
    pn_std: str = Field(min_length=1, max_length=256)
    description: str | None = None
    brand: str | None = None
    category_major: str | None = None
    category_minor: str | None = None
    manufacturer: str | None = None
    summary: str | None = None
    machine_or_part: str | None = None
    unit: str | None = None


class ReferenceInput(Input):
    part_ids: list[int] = Field(default_factory=list, max_length=200)
    inquiry_id: int | None = Field(default=None, gt=0)


def _call(db, fn, *, write=False):
    try:
        result = fn()
        if write:
            db.commit()
        return result
    except WorkflowError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, jsonable_encoder(getattr(exc, "detail", str(exc)))) from exc
    except (ValueError, PermissionError) as exc:
        db.rollback()
        raise HTTPException(getattr(exc, "status_code", 403 if isinstance(exc, PermissionError) else 422),
                            str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "记录已变化或关联数据不可用，请刷新后重试") from exc
    except PriceReferenceUnavailable as exc:
        db.rollback()
        raise HTTPException(503, "参考价暂不可用") from exc


def _inquiry_action(db, ctx, fn):
    return _call(db, lambda: visibility.inquiry_view(db, fn(), ctx), write=True)


@router.get("/customers")
def search_customers(db: Db, ctx: Ctx, q: str = Query(default="", max_length=256),
                     limit: int = Query(20, ge=1, le=100)):
    return _call(db, lambda: candidates.customers(db, ctx, q, limit=limit))


@router.get("/suppliers")
def search_suppliers(db: Db, ctx: Ctx, q: str = Query(default="", max_length=256),
                     limit: int = Query(20, ge=1, le=100)):
    return _call(db, lambda: candidates.suppliers(db, ctx, q, limit=limit))


@router.get("/inquiries")
def list_inquiries(db: Db, ctx: Ctx,
    kind: Literal["sales_inquiry", "proactive_offer"] | None = None,
    scope: Literal["mine", "shared", "all"] = "mine",
    status: Literal["draft", "submitted", "revising", "closed"] | None = None,
    q: str = Query(default="", max_length=256),
    date_from: date | None = None, date_to: date | None = None,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    sort: Literal["inquiry_no", "line_count", "status", "updated_at", "created_at"] = "updated_at",
    dir: Literal["asc", "desc"] = "desc"):
    def read():
        user, _ = common.resolve_actor(db, ctx)
        if date_from and date_to and date_from > date_to:
            raise ValueError("date_from 不能晚于 date_to")
        # 仅允许不依赖受众私密字段的排序键；先 SQL 排序，再沿用可见性过滤和分页。
        line_count = select(func.count(PresalesInquiryLine.id)).where(
            PresalesInquiryLine.inquiry_id == PresalesInquiry.id,
            PresalesInquiryLine.status == "active",
        ).correlate(PresalesInquiry).scalar_subquery()
        columns = {
            "inquiry_no": PresalesInquiry.inquiry_no,
            "line_count": line_count,
            "status": case(
                (PresalesInquiry.status == "draft", 0),
                (PresalesInquiry.status == "revising", 1),
                (PresalesInquiry.status == "submitted", 2),
                (PresalesInquiry.status == "closed", 3),
            ),
            "updated_at": PresalesInquiry.updated_at,
            "created_at": PresalesInquiry.created_at,
        }
        column = columns[sort]
        order = column.asc() if dir == "asc" else column.desc()
        customer_name = func.coalesce(func.nullif(DimCustomer.name_normalized, ""), DimCustomer.name_raw)
        query = select(PresalesInquiry, customer_name).outerjoin(
            DimCustomer, DimCustomer.id == PresalesInquiry.customer_id,
        ).order_by(order.nulls_last(), PresalesInquiry.id.desc())
        if scope == "shared":
            query = query.where(PresalesInquiry.created_by != user.id)
        if kind:
            query = query.where(PresalesInquiry.kind == kind)
        if status:
            query = query.where(PresalesInquiry.status == status)
        if q.strip():
            pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            matches = [PresalesInquiry.inquiry_no.ilike(pattern, escape="\\"),
                select(PresalesInquiryLine.id).outerjoin(
                    DimPart, DimPart.id == PresalesInquiryLine.part_id,
                ).where(
                    PresalesInquiryLine.inquiry_id == PresalesInquiry.id,
                    PresalesInquiryLine.status == "active",
                    or_(DimPart.pn_std.ilike(pattern, escape="\\"),
                        DimPart.description.ilike(pattern, escape="\\"),
                        PresalesInquiryLine.pn_raw.ilike(pattern, escape="\\"),
                        PresalesInquiryLine.description.ilike(pattern, escape="\\")),
                ).correlate(PresalesInquiry).exists()]
            from app.services.workflow import work_items
            if work_items.has_permission(ctx, "data_customer"):
                # 与询价输出相同：客户字段只向单据所有人或主管开放。
                customer_match = or_(DimCustomer.name_raw.ilike(pattern, escape="\\"),
                                     DimCustomer.name_normalized.ilike(pattern, escape="\\"))
                if not common.bypass(ctx):
                    customer_match = and_(PresalesInquiry.created_by == user.id, customer_match)
                matches.append(customer_match)
            query = query.where(or_(*matches))
        zone = ZoneInfo("Asia/Shanghai")
        if date_from:
            query = query.where(PresalesInquiry.updated_at >= datetime.combine(date_from, time.min, zone))
        if date_to:
            if date_to < date.max:
                query = query.where(PresalesInquiry.updated_at < datetime.combine(date_to + timedelta(days=1), time.min, zone))
            else:
                query = query.where(PresalesInquiry.updated_at <= datetime.combine(date_to, time.max, zone))
        items = []
        for inquiry, name in db.execute(query):
            if not visibility.can_view_inquiry(db, inquiry, ctx):
                continue
            if scope == "mine" and inquiry.created_by != user.id:
                from app.services.workflow import work_items
                from app.services.workflow.registry import Subject
                if not work_items.subject_participation(db, Subject("presales_inquiry", inquiry.id), ctx)["assigned"]:
                    continue
            view = visibility.inquiry_view(db, inquiry, ctx, detail=False)
            view["customer_name"] = name if "customer_id" in view else None
            view["is_owner"] = inquiry.created_by == user.id
            items.append(view)
        return {"items": items[(page - 1) * page_size:page * page_size], "total": len(items),
                "page": page, "page_size": page_size}
    return _call(db, read)


@router.post("/inquiries", status_code=201)
def create_inquiry(body: CreateInquiry, db: Db, ctx: Ctx):
    return _inquiry_action(db, ctx, lambda: inquiries.create_draft(
        db, ctx, **body.model_dump(exclude_unset=True)))


@router.get("/inquiries/{inquiry_id}")
def get_inquiry(inquiry_id: int, db: Db, ctx: Ctx):
    return _call(db, lambda: visibility.inquiry_view(db, common.get_inquiry(db, inquiry_id), ctx))


@router.patch("/inquiries/{inquiry_id}")
def update_inquiry(inquiry_id: int, body: UpdateInquiry, db: Db, ctx: Ctx):
    data = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    return _inquiry_action(db, ctx, lambda: inquiries.update_draft(
        db, inquiry_id, ctx, body.expected_version, **data))


@router.delete("/inquiries/{inquiry_id}", status_code=204)
def delete_inquiry(inquiry_id: int, body: VersionInput, db: Db, ctx: Ctx):
    _call(db, lambda: inquiries.delete_draft(db, inquiry_id, ctx, body.expected_version), write=True)
    return Response(status_code=204)


@router.post("/inquiries/{inquiry_id}/submit")
def submit_inquiry(inquiry_id: int, body: VersionInput, db: Db, ctx: Ctx):
    return _inquiry_action(db, ctx, lambda: inquiries.submit(db, inquiry_id, ctx, body.expected_version))


@router.post("/inquiries/{inquiry_id}/withdraw")
def withdraw_inquiry(inquiry_id: int, body: VersionInput, db: Db, ctx: Ctx):
    return _inquiry_action(db, ctx, lambda: inquiries.withdraw(db, inquiry_id, ctx, body.expected_version))


@router.post("/inquiries/{inquiry_id}/resubmit")
def resubmit_inquiry(inquiry_id: int, body: VersionInput, db: Db, ctx: Ctx):
    return _inquiry_action(db, ctx, lambda: inquiries.resubmit(db, inquiry_id, ctx, body.expected_version))


@router.post("/inquiries/{inquiry_id}/lines")
def append_lines(inquiry_id: int, body: AppendLines, db: Db, ctx: Ctx):
    lines = body.model_dump(exclude_unset=True)["lines"]
    return _inquiry_action(db, ctx, lambda: inquiries.append_lines(
        db, inquiry_id, ctx, body.expected_version, lines))


@router.post("/inquiries/{inquiry_id}/close")
def close_inquiry(inquiry_id: int, body: CloseInquiry, db: Db, ctx: Ctx):
    return _inquiry_action(db, ctx, lambda: inquiries.close(db, inquiry_id, ctx,
        body.expected_version, outcome=body.outcome, won_offer_id=body.won_offer_id))


@router.post("/inquiries/{inquiry_id}/copy", status_code=201)
def copy_inquiry(inquiry_id: int, body: VersionInput, db: Db, ctx: Ctx):
    return _inquiry_action(db, ctx, lambda: inquiries.copy_as_new(db, inquiry_id, ctx, body.expected_version))


@router.post("/inquiries/{inquiry_id}/lines/{line_id}/offers")
def save_offer(inquiry_id: int, line_id: int, body: SaveOffer, db: Db, ctx: Ctx):
    data = body.model_dump(exclude_unset=True, exclude={"expected_version", "offer_id", "work_item_id"})
    return _call(db, lambda: visibility.offer_view(db, offers.save_draft(
        db, inquiry_id, line_id, ctx, data, expected_version=body.expected_version,
        offer_id=body.offer_id, work_item_id=body.work_item_id), ctx), write=True)


@router.post("/inquiries/{inquiry_id}/reply")
def reply(inquiry_id: int, body: ReplyInput, db: Db, ctx: Ctx):
    return _call(db, lambda: [visibility.offer_view(db, row, ctx) for row in offers.reply(
        db, inquiry_id, ctx, **body.model_dump())], write=True)


@router.post("/inquiries/{inquiry_id}/publish")
def publish(inquiry_id: int, body: PublishInput, db: Db, ctx: Ctx):
    return _call(db, lambda: [visibility.offer_view(db, row, ctx) for row in offers.publish_proactive(
        db, inquiry_id, ctx, **body.model_dump())], write=True)


@router.patch("/offers/{offer_id}")
def update_offer(offer_id: int, body: UpdateOffer, db: Db, ctx: Ctx):
    data = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    return _call(db, lambda: visibility.offer_view(db, offers.update_offer(
        db, offer_id, ctx, data, expected_version=body.expected_version), ctx), write=True)


@router.post("/offers/{offer_id}/withdraw")
def withdraw_offer(offer_id: int, body: WithdrawOffer, db: Db, ctx: Ctx):
    return _call(db, lambda: visibility.offer_view(db, offers.withdraw(
        db, offer_id, ctx, body.reason, expected_version=body.expected_version), ctx), write=True)


@router.get("/offers/{offer_id}/revisions")
def offer_revisions(offer_id: int, db: Db, ctx: Ctx):
    return _call(db, lambda: visibility.revision_views(db, offer_id, ctx))


@router.get("/lines/{line_id}/part-check")
def check_part(line_id: int, db: Db, ctx: Ctx, pn_std: str | None = None):
    return _call(db, lambda: binding.check(db, line_id, ctx, pn_std))


@router.post("/lines/{line_id}/bind-part")
def bind_part(line_id: int, body: BindInput, db: Db, ctx: Ctx):
    def apply():
        line = binding.bind(db, line_id, ctx, body.expected_version, part_id=body.part_id)
        return visibility.inquiry_view(db, common.get_inquiry(db, line.inquiry_id), ctx)
    return _call(db, apply, write=True)


@router.post("/lines/{line_id}/create-part")
def create_part(line_id: int, body: CreatePartInput, db: Db, ctx: Ctx):
    def apply():
        data = body.model_dump(exclude_unset=True, exclude={"expected_version"})
        line = binding.create_and_bind(db, line_id, ctx, body.expected_version, **data)
        return visibility.inquiry_view(db, common.get_inquiry(db, line.inquiry_id), ctx)
    return _call(db, apply, write=True)


def _part_ids(value):
    try:
        ids = list(dict.fromkeys(int(part) for part in value.split(",") if part.strip()))
    except ValueError as exc:
        raise HTTPException(422, "part_ids 必须为逗号分隔的正整数") from exc
    if len(ids) > 200 or any(part <= 0 for part in ids):
        raise HTTPException(422, "最多提交 200 个有效 part_id")
    return ids


@router.get("/library")
def get_library(db: Db, ctx: Ctx, part_ids: str = ""):
    return _call(db, lambda: library.batch(db, _part_ids(part_ids), ctx))


@router.get("/library/search")
def search_library(db: Db, ctx: Ctx, q: str = Query(default="", max_length=256),
                   limit: int = Query(20, ge=1, le=100)):
    return _call(db, lambda: library.search(db, q, ctx, limit=limit))


@router.post("/reference")
def reference(body: ReferenceInput, db: Db, ctx: Ctx):
    def read():
        common.require(ctx, "action_presales_quote")
        part_ids = body.part_ids
        owner_id = None
        if body.inquiry_id is not None:
            inquiry = common.get_inquiry(db, body.inquiry_id)
            if not visibility.can_view_inquiry(db, inquiry, ctx):
                raise HTTPException(403, "无权查看询价参考")
            owner_id = inquiry.created_by
            part_ids = list(db.scalars(select(PresalesInquiryLine.part_id).where(
                PresalesInquiryLine.inquiry_id == inquiry.id, PresalesInquiryLine.status == "active",
                PresalesInquiryLine.part_id.is_not(None))))
        prices = price_reference.batch(db, part_ids, ctx)
        return {"reference": prices, "offers": library.batch(db, part_ids, ctx, owner_id=owner_id),
                "gallery": [{"part_id": part_id} for part_id in prices]}
    return _call(db, read)


@router.get("/parts/{part_id}/inquiries")
def part_inquiries(part_id: int, db: Db, ctx: Ctx,
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    return _call(db, lambda: part_history.list_lines(db, part_id, ctx, page=page, page_size=page_size))
