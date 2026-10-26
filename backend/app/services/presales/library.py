"""有效报价的实时视图；检索权重和角标使用同一受众过滤出口。"""

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


def batch(db, part_ids, viewer, *, now=None):
    from app.services.presales.visibility import offer_views

    _, ctx = common.resolve_actor(db, viewer)
    common.require(ctx, "page_presales")
    ids = list(dict.fromkeys(part_ids))
    result = {part_id: [] for part_id in ids}
    if not ids:
        return result
    for view in offer_views(db, _active(db, ids, now=now), ctx):
        result[view["part_id"]].append(view)
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
