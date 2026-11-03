"""D-0052 迁移：回填当前答复，取消待办并可由降级恢复。"""

from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import inspect, text

from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer
from app.models.workflow import WorkItem
from app.services.presales import common
from tests.conftest import _alembic_cfg
from tests.test_presales_api_visibility import users  # noqa: F401


def test_upgrade_backfills_answers_and_downgrade_restores_work_items(db, users):
    moment = common.now()
    inquiry = PresalesInquiry(kind="sales_inquiry", inquiry_no="MIG000000001", status="submitted", created_by=users.seller.id,
                              submitted_at=moment)
    db.add(inquiry)
    db.flush()
    lines = {}
    for seq, name in enumerate(("multi", "stale", "pending"), 1):
        lines[name] = PresalesInquiryLine(inquiry_id=inquiry.id, line_key=name, seq=seq, part_id=users.part.id, pn_raw="P",
                                          qty=Decimal(1), version=1)
        db.add(lines[name])
    db.flush()
    db.add_all([
        PresalesOffer(inquiry_id=inquiry.id, line_id=lines["multi"].id, part_id=users.part.id, created_by=users.buyer.id,
                      status="published", unit_price=Decimal(10), valid_from=moment.replace(year=2020), revision_no=1),
        PresalesOffer(inquiry_id=inquiry.id, line_id=lines["multi"].id, part_id=users.part.id, created_by=users.peer.id,
                      status="published", unit_price=Decimal(20), valid_from=moment.replace(year=2021), revision_no=1),
        PresalesOffer(inquiry_id=inquiry.id, line_id=lines["stale"].id, part_id=users.part.id, created_by=users.peer.id,
                      status="withdrawn", unit_price=Decimal(30), valid_from=moment.replace(year=2021), revision_no=1),
        WorkItem(subject_type="presales_inquiry", subject_id=inquiry.id, pool_key="presales_inquiry", status="in_progress",
                 assignee_id=users.buyer.id, last_reason="原因", created_by=users.seller.id),
        WorkItem(subject_type="presales_inquiry", subject_id=inquiry.id, pool_key="presales_inquiry", status="done",
                 created_by=users.seller.id),
        WorkItem(subject_type="other", subject_id=1, pool_key="other", status="cancelled",
                 last_reason="D-0052|pending|人工填写"),
    ])
    db.commit()
    ids = {name: line.id for name, line in lines.items()}
    cfg, engine = _alembic_cfg(), db.get_bind()
    peer_id = users.peer.id
    db.close()
    try:
        command.downgrade(cfg, "a3c5e7f9b2d4")
        with engine.begin() as connection:
            assert "answer_state" not in {row["name"] for row in inspect(connection).get_columns("presales_inquiry_line")}
            assert "updated_by" not in {row["name"] for row in inspect(connection).get_columns("presales_offer")}
            assert {row[0] for row in connection.execute(text(
                "SELECT status FROM work_item WHERE subject_type = 'presales_inquiry'"))} == {"in_progress", "done"}
            restored = connection.execute(text(
                "SELECT last_reason, closed_at FROM work_item WHERE status = 'in_progress'")).one()
            assert tuple(restored) == ("原因", None)
        command.upgrade(cfg, "e5a7c9d1f3b6")
        with engine.begin() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "e5a7c9d1f3b6"
            assert "adjust_warehouse_raw" not in {
                col["name"] for col in inspect(connection).get_columns("f_maintenance_line")
            }
            rows = {row.id: row for row in connection.execute(text(
                "SELECT l.id, l.answer_state, o.unit_price, l.answered_by FROM presales_inquiry_line l "
                "LEFT JOIN presales_offer o ON o.id = l.current_offer_id"))}
            # 多个有效价取最新；只剩撤回价的行回到待报价。
            assert (rows[ids["multi"]].answer_state, rows[ids["multi"]].unit_price, rows[ids["multi"]].answered_by) == (
                "quoted", Decimal("20.00"), peer_id)
            assert rows[ids["stale"]].answer_state == rows[ids["pending"]].answer_state == "pending"
            statuses = connection.execute(text(
                "SELECT status, last_reason FROM work_item WHERE subject_type = 'presales_inquiry' ORDER BY id")).all()
            assert statuses[0] == ("cancelled", "D-0052|in_progress|原因")
            assert statuses[1][0] == "done"

        # 从精选售前发布回退，只恢复迁移所取消的工作项，原报价也必须保留。
        command.downgrade(cfg, "a3c5e7f9b2d4")
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT status, last_reason, closed_at FROM work_item WHERE subject_type = 'presales_inquiry' ORDER BY id"
            )).all() == [("in_progress", "原因", None), ("done", None, None)]
            assert connection.execute(text(
                "SELECT unit_price FROM presales_offer ORDER BY unit_price"
            )).scalars().all() == [Decimal("10"), Decimal("20"), Decimal("30")]
            assert connection.execute(text(
                "SELECT status, last_reason FROM work_item WHERE subject_type = 'other'"
            )).one() == ("cancelled", "D-0052|pending|人工填写")
        command.upgrade(cfg, "e5a7c9d1f3b6")
        with engine.begin() as connection:
            # 已发布共享池上的新答复不能在后续合入其他迁移时被历史回填重置。
            connection.execute(text(
                "UPDATE presales_inquiry_line SET answer_state = 'skip', answer_reason = '该配件停产' WHERE id = :id"
            ), {"id": ids["pending"]})
        command.upgrade(cfg, "head")
        command.check(cfg)
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT answer_state, answer_reason FROM presales_inquiry_line WHERE id = :id"
            ), {"id": ids["pending"]}).one() == ("skip", "该配件停产")
    finally:
        command.upgrade(cfg, "head")


def test_upgrade_refuses_existing_presales_cancellation_with_reserved_reason(db):
    cfg, engine = _alembic_cfg(), db.get_bind()
    db.close()
    command.downgrade(cfg, "a3c5e7f9b2d4")
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO work_item (subject_type, subject_id, pool_key, status, last_reason) "
                "VALUES ('presales_offer_notice', 1, 'presales', 'cancelled', 'D-0052|pending|人工填写')"))
        with pytest.raises(RuntimeError, match="回滚标记"):
            command.upgrade(cfg, "e5a7c9d1f3b6")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "a3c5e7f9b2d4"
            assert connection.execute(text("SELECT status, last_reason FROM work_item")).one() == (
                "cancelled", "D-0052|pending|人工填写")
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM work_item WHERE subject_type = 'presales_offer_notice' AND subject_id = 1"))
        command.upgrade(cfg, "head")
