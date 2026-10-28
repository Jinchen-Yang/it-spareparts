"""型号全景的只读询价记录；准入、受众与钉住快照由售前输出边界负责。"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.models.presales import PresalesInquiry, PresalesInquiryLine
from app.services.presales import common, visibility


def list_lines(db, part_id, ctx, *, page=1, page_size=20):
    common.require(ctx, "page_presales")
    if part_id <= 0:
        raise ValueError("part_id 必须为正整数")
    # 受众与工作流准入继续走唯一 visibility 出口；候选只读准入字段和行 ID。
    candidates = db.execute(select(PresalesInquiry, PresalesInquiryLine.id).join(
        PresalesInquiryLine, PresalesInquiryLine.inquiry_id == PresalesInquiry.id,
    ).where(PresalesInquiryLine.part_id == part_id, PresalesInquiryLine.status == "active").options(
        load_only(PresalesInquiry.id, PresalesInquiry.created_by, PresalesInquiry.status,
                  PresalesInquiry.submitted_at, PresalesInquiry.kind, PresalesInquiry.offer_audience_rule_id),
    ).order_by(PresalesInquiry.created_at.desc(), PresalesInquiry.id.desc(),
               PresalesInquiryLine.seq, PresalesInquiryLine.id))
    allowed = {}
    visible = []
    for inquiry, line_id in candidates:
        if inquiry.id not in allowed:
            allowed[inquiry.id] = visibility.can_view_inquiry(db, inquiry, ctx)
        if allowed[inquiry.id]:
            visible.append((inquiry, line_id))
    current_page = visible[(page - 1) * page_size:page * page_size]
    # 当前页一次补齐单头字段，避免 load_only 的逐字段延迟加载。
    page_ids = {inquiry.id for inquiry, _ in current_page}
    page_inquiries = {inquiry.id: inquiry for inquiry in db.scalars(select(PresalesInquiry).where(
        PresalesInquiry.id.in_(page_ids)))} if current_page else {}
    views = {}
    result = []
    moment = common.now()
    for inquiry, line_id in current_page:
        if inquiry.id not in views:
            views[inquiry.id] = visibility.inquiry_view(db, page_inquiries[inquiry.id], ctx)
        view = views[inquiry.id]
        for line in view["lines"]:
            if line["id"] != line_id:
                continue
            # 本行的历史关联优先，不能用同 ID 的当前价格替换钉住版本。
            pinned = {offer["id"]: offer for offer in line.get("linked_offers", [])}
            if line.get("ref_offer"):
                pinned[line["ref_offer"]["id"]] = line["ref_offer"]
            for response in view.get("line_responses", []):
                if response["line_id"] == line["id"] and not response.get("is_draft") and response.get("offer"):
                    pinned[response["offer"]["id"]] = response["offer"]
            quotes = list(pinned.values()) + [offer for offer in view["offers"]
                if offer["line_id"] == line["id"] and offer["id"] not in pinned]
            valid = [offer for offer in quotes if offer["status"] == "published"
                     and not offer.get("original_withdrawn") and not offer.get("expired")
                     and not offer["no_stock"] and offer["unit_price"] is not None
                     and offer["valid_from"] is not None and offer["valid_from"] <= moment
                     and offer["valid_until"] is not None and offer["valid_until"] > moment]
            # 各税口径分别取最低，避免把含税与未税直接比较。
            minimum = []
            for basis in (True, False, None):
                group = [offer for offer in valid if offer["is_tax_inclusive"] is basis]
                if group:
                    minimum.append({"unit_price": str(min(Decimal(str(offer["unit_price"])) for offer in group)),
                                    "is_tax_inclusive": basis})
            row = {"line_id": line["id"], "inquiry_id": inquiry.id,
                   "created_at": view["created_at"], "qty": line["qty"], "status": view["status"],
                   "has_quote": any(offer["status"] == "published" for offer in quotes),
                   "lowest_valid_quotes": minimum}
            if "created_by_name" in view:
                row["created_by_name"] = view["created_by_name"]
            result.append(row)
    return {"items": result, "total": len(visible),
            "page": page, "page_size": page_size}
