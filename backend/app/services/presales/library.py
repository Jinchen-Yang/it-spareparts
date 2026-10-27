"""有效报价的实时视图；检索权重和角标使用同一受众过滤出口。"""

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from app.models.presales import PresalesOffer
from app.services import part_search
from app.services.presales import common


def _active(db, part_ids=None, *, now=None):
    moment = now or common.now()
    query = select(PresalesOffer).where(
        PresalesOffer.status == "published", PresalesOffer.no_stock.is_(False),
        PresalesOffer.part_id.is_not(None), PresalesOffer.unit_price.is_not(None),
        PresalesOffer.valid_from <= moment, PresalesOffer.valid_until > moment,
    )
    if part_ids is not None:
        query = query.where(PresalesOffer.part_id.in_(part_ids))
    return list(db.scalars(query.order_by(PresalesOffer.part_id,
                                         PresalesOffer.unit_price, PresalesOffer.id)))


def _price_key(view):
    # 不换算税基；含税、未税、未知分别排序。
    tax = view.get("is_tax_inclusive")
    return (0 if tax is True else 1 if tax is False else 2, view["unit_price"], view["id"])


def batch(db, part_ids, viewer, *, now=None, owner_id=None):
    from app.services.presales.visibility import _offer_batch

    _, ctx = common.resolve_actor(db, viewer)
    common.require(ctx, "page_presales")
    ids = list(dict.fromkeys(part_ids))
    result = {part_id: [] for part_id in ids}
    if not ids:
        return result
    active = _active(db, ids, now=now)
    owner_views = None
    if owner_id is not None:
        _, owner_ctx = common.resolve_actor(db, owner_id)
        owner_views = {offer.id: view for offer, view in _offer_batch(db, active, owner_ctx, fields=True)}
        active = [offer for offer in active if offer.id in owner_views]
    grouped = defaultdict(list)
    for offer, view in _offer_batch(db, active, ctx, fields=True):
        if owner_views is not None and "reporter" not in owner_views[offer.id]:
            view.pop("reporter", None)
        grouped[(offer.part_id, offer.created_by)].append((offer, view))
    floor = datetime.min.replace(tzinfo=timezone.utc)
    for (part_id, _), rows in grouped.items():
        rows.sort(key=lambda item: (item[0].valid_from or floor, item[0].id), reverse=True)
        main = rows[0][1]
        main["more"] = sorted([view for _, view in rows[1:]], key=_price_key)
        result[part_id].append(main)
    for rows in result.values():
        rows.sort(key=_price_key)
    return result


def boost_part_ids(db, q, viewer=None):
    """缺少查看者时不给权重，避免调用方意外泄露私密报价的存在。"""
    from app.services.presales.visibility import visible_offers

    if viewer is None:
        return []
    _, ctx = common.resolve_actor(db, viewer)
    common.require(ctx, "page_presales")
    return list(dict.fromkeys(offer.part_id for offer in visible_offers(db, _active(db), ctx)))


def search(db, q, viewer, *, limit=20):
    boost = boost_part_ids(db, q, viewer)
    rows = part_search.search(db, q, filters={"boost_part_ids": boost}, limit=limit)
    offers = batch(db, [row["part_id"] for row in rows], viewer)
    return [{**row, "offers": offers.get(row["part_id"], []),
             "offer_count": len(offers.get(row["part_id"], []))} for row in rows]
