"""报价走势缓存：按上海变更日期重建，同一天每条报价取最后一次快照。"""

import argparse
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import delete, select, text

from app.models.presales import OfferPriceDaily, PresalesOfferRevision
from app.models.system import SysAccessLog
from app.services.presales.offers import SHANGHAI


def rebuild(db, *, since=None):
    """含税、不含税和未声明口径分别汇总；调用方负责提交事务。"""
    if isinstance(since, str):
        since = date.fromisoformat(since)
    if since is not None and not isinstance(since, date):
        raise ValueError("since 必须为 YYYY-MM-DD")
    # 两次维护重建不能各自先清空、后插入，统一串行到事务结束。
    db.execute(text("SELECT pg_advisory_xact_lock(734138903871923)"))
    revisions = db.scalars(select(PresalesOfferRevision).order_by(
        PresalesOfferRevision.created_at, PresalesOfferRevision.id,
    ))
    latest = {}
    for revision in revisions:
        day = revision.created_at.astimezone(SHANGHAI).date()
        if since is None or day >= since:
            latest[(day, revision.offer_id)] = revision.snapshot
    grouped = {}
    for (day, _), snapshot in latest.items():
        if (snapshot.get("status") != "published" or snapshot.get("no_stock")
                or snapshot.get("part_id") is None or snapshot.get("unit_price") is None):
            continue
        tax = snapshot.get("is_tax_inclusive")
        source = "tax_inclusive" if tax is True else "tax_exclusive" if tax is False else "unknown"
        key = (snapshot["part_id"], day, source)
        grouped.setdefault(key, []).append(Decimal(snapshot["unit_price"]))
    statement = delete(OfferPriceDaily)
    if since is not None:
        statement = statement.where(OfferPriceDaily.date >= since)
    db.execute(statement)
    for (part_id, day, source), prices in grouped.items():
        db.add(OfferPriceDaily(
            part_id=part_id, date=day, source=source, min=min(prices), max=max(prices),
            avg=(sum(prices) / len(prices)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            count=len(prices),
        ))
    db.flush()
    # 维护命令同样留系统审计，但不改变业务单据和报价版本。
    from app.services.presales.registration import register_subjects
    from app.services.workflow import events
    from app.services.workflow.registry import Subject
    register_subjects()
    for inquiry_id in sorted({snapshot["inquiry_id"] for snapshot in latest.values()
                              if snapshot.get("inquiry_id") is not None}):
        events.record(db, Subject("presales_inquiry", inquiry_id), "offer.daily_rebuilt", None,
                      {"since": since.isoformat() if since else None})
    db.add(SysAccessLog(username=None, role="system", action="presales.daily.rebuild",
                        resource="presales", detail={"since": since.isoformat() if since else None,
                                                      "count": len(grouped)}))
    db.flush()
    return len(grouped)


def main():
    parser = argparse.ArgumentParser(description="从只追加报价快照重建日汇总缓存")
    parser.add_argument("--rebuild", action="store_true", required=True)
    parser.add_argument("--since", type=date.fromisoformat)
    args = parser.parse_args()
    from app.db import SessionLocal
    with SessionLocal() as db:
        count = rebuild(db, since=args.since)
        db.commit()
    print(f"已重建 {count} 条报价日汇总")


if __name__ == "__main__":
    main()
