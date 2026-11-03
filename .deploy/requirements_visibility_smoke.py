"""Read-only production projections for the combined requirements/visibility release."""
import json

from sqlalchemy import select, text

from app.db import SessionLocal
from app.models.presales import PresalesInquiry, PresalesOffer
from app.models.system import SysUser
from app.services.presales import pool, visibility
from app.services.workflow import work_items


def main():
    checks = ["migration", "excel_fidelity_columns", "inquiry_sql_and_record_acl", "offer_acl", "pool_pagination"]
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout='15s'"))
        if db.scalar(text("SHOW transaction_read_only")) != "on":
            raise RuntimeError("read-only transaction required")
        if db.scalar(text("SELECT version_num FROM alembic_version")) != "98afc0d2157b":
            raise RuntimeError("unexpected database head")
        # ORM projection checks D0050's expanded columns against the deployed database.
        from app.models.maintenance import FMaintenanceOrder
        db.scalars(select(FMaintenanceOrder).limit(1)).first()
        users = list(db.scalars(select(SysUser).where(SysUser.is_active.is_(True), SysUser.deleted_at.is_(None))))
        checked = 0
        inquiries = list(db.scalars(select(PresalesInquiry).order_by(PresalesInquiry.id.desc()).limit(60)))
        for user in users:
            user, ctx = work_items.actor_context(db, user.id)
            if not work_items.has_permission(ctx, "page_presales"):
                continue
            allowed = set(db.scalars(select(PresalesInquiry.id).where(
                PresalesInquiry.id.in_([row.id for row in inquiries]), visibility.inquiry_access_filter(db, ctx))))
            for inquiry in inquiries:
                if visibility.can_view_inquiry(db, inquiry, ctx) != (inquiry.id in allowed):
                    raise RuntimeError("SQL and record visibility disagree")
                submitted = inquiry.status != "draft" and inquiry.submitted_at is not None
                required = (inquiry.created_by == user.id or user.role in {"admin", "boss"}
                    or (submitted and (inquiry.kind == "proactive_offer"
                                       or work_items.has_permission(ctx, "action_presales_quote"))))
                if required and inquiry.id not in allowed:
                    raise RuntimeError("author, administrator or quoter lost required access")
                if inquiry.id in allowed and inquiry.submitted_at is None and inquiry.created_by != user.id and user.role not in {"admin", "boss"}:
                    raise RuntimeError("another user's unsubmitted draft is visible")
            offers = list(db.scalars(select(PresalesOffer).where(visibility.offer_access_filter(db, ctx)).limit(30)))
            for offer in offers:
                if offer.inquiry_id and not visibility.can_view_inquiry(db, offer.inquiry_id, ctx):
                    raise RuntimeError("offer source is inaccessible")
            result = pool.list_lines(db, ctx, tab="all", page=1, page_size=20)
            for row in result["items"]:
                if not visibility.can_view_inquiry(db, row["inquiry_id"], ctx):
                    raise RuntimeError("pool returned an inaccessible inquiry")
            checked += 1
        if not checked:
            raise RuntimeError("no active presales identities checked")
        db.rollback()
    print(json.dumps({"status": "passed", "mode": "readonly_database_projection", "checks": checks}))


if __name__ == "__main__":
    main()
