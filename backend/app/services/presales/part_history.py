"""型号全景的只读询价记录；准入、受众与钉住快照由售前输出边界负责。"""

from decimal import Decimal

from sqlalchemy import select

from app.models.presales import PresalesInquiry, PresalesInquiryLine
from app.services.presales import common, visibility


def list_lines(db, part_id, ctx, *, page=1, page_size=20):
    common.require(ctx, "page_presales")
    if part_id <= 0:
        raise ValueError("part_id 必须为正整数")
    inquiries = db.scalars(select(PresalesInquiry).where(
        PresalesInquiry.id.in_(select(PresalesInquiryLine.inquiry_id).where(
            PresalesInquiryLine.part_id == part_id, PresalesInquiryLine.status == "active")),
    ).order_by(PresalesInquiry.created_at.desc(), PresalesInquiry.id.desc()))
    result = []
    moment = common.now()
    for inquiry in inquiries:
        if not visibility.can_view_inquiry(db, inquiry, ctx):
            continue
        view = visibility.inquiry_view(db, inquiry, ctx)
        for line in view["lines"]:
            if line["part_id"] != part_id:
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
    return {"items": result[(page - 1) * page_size:page * page_size], "total": len(result),
            "page": page, "page_size": page_size}
