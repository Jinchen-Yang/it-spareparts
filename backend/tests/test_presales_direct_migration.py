"""独立报价迁移：旧生产头往返及已有新业务时拒绝破坏性降级。"""
import pytest
from alembic import command
from sqlalchemy import inspect, text

from app.services.presales import offers
from tests.conftest import _alembic_cfg
from tests.test_presales_r6_existing import r6_actors


def test_direct_schema_roundtrip_to_production_head_and_back(db):
    cfg, engine = _alembic_cfg(), db.get_bind()
    db.close()
    try:
        command.downgrade(cfg, "d6e8f0a2b4c6")
        with engine.connect() as connection:
            columns = {row["name"]: row for row in inspect(connection).get_columns("presales_offer")}
            assert columns["inquiry_id"]["nullable"] is False and columns["line_id"]["nullable"] is False
            assert "offer_audience_rule_id" not in columns and "price_basis" not in columns
        command.upgrade(cfg, "head")
        command.check(cfg)
        with engine.connect() as connection:
            columns = {row["name"]: row for row in inspect(connection).get_columns("presales_offer")}
            assert columns["inquiry_id"]["nullable"] is True and columns["line_id"]["nullable"] is True
            assert "offer_audience_rule_id" in columns and "price_basis" in columns
    finally:
        command.upgrade(cfg, "head")


def test_downgrade_with_standalone_quote_is_refused_without_losing_quote_or_snapshot(db, r6_actors):
    u = r6_actors
    quote = offers.direct_offer(db, u.buyer.id, u.part.id, {"unit_price": "123.45"})
    quote_id = quote.id
    db.commit()
    engine, cfg = db.get_bind(), _alembic_cfg()
    db.close()
    with engine.connect() as connection:
        previous_head = connection.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(cfg, "e9b3d5f7a1c4")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == previous_head
        assert connection.scalar(text("SELECT count(*) FROM presales_offer WHERE id=:id AND inquiry_id IS NULL AND line_id IS NULL"), {"id": quote_id}) == 1
        assert connection.scalar(text("SELECT count(*) FROM presales_offer_revision WHERE offer_id=:id"), {"id": quote_id}) == 1
    command.check(cfg)
