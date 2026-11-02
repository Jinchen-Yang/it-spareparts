"""D-0052 迁移：回填当前答复，取消待办并可由降级恢复。"""

import json
from decimal import Decimal

import pytest
from alembic import command
from alembic.script import ScriptDirectory
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


def test_upgrade_quarantines_unproven_legacy_answers_without_changing_business_records(db, users):
    cfg, engine = _alembic_cfg(), db.get_bind()
    actor, part = users.buyer.id, users.part.id
    db.commit()
    db.close()
    base, pool_head = "a3c5e7f9b2d4", "e5a7c9d1f3b6"
    review_ids = ScriptDirectory.from_config(cfg).get_revision(pool_head).module.REVIEW_LINE_IDS
    names = ("unchanged", "changed", "missing", "malformed", "signed", "skip", "private", "empty", "ref",
             "ref_only", "linked", "fallback", "unrelated")
    old = {"part_id": part, "pn_raw": "Q", "qty": "1", "require_exact_pn": False, "return_defective": True}
    current = {**old, "qty": "1E+1"}
    old_signatures = {name: old for name in names if name != "missing"}
    old_signatures["unchanged"] = {key: value for key, value in current.items() if key != "return_defective"}
    old_signatures["malformed"] = {**old, "qty": "1e99999999999999999999"}
    old_signatures.update({name: current for name in ("ref_only", "linked", "fallback", "unrelated")})
    tables = ("presales_inquiry", "presales_inquiry_line", "presales_offer", "presales_offer_revision",
              "presales_offer_line_link", "presales_line_response")

    def business_rows(conn):
        return {table: conn.scalars(text(f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY to_jsonb(t)::text")).all()
                for table in tables}

    command.downgrade(cfg, base)
    try:
        with engine.begin() as conn:
            header = conn.scalar(text(
                "INSERT INTO presales_inquiry (kind,inquiry_no,status,created_by,submitted_at,submitted_snapshot) "
                "VALUES ('sales_inquiry','PRE-D52-REVIEW','revising',:actor,now(),CAST(:snapshot AS jsonb)) RETURNING id"
            ), {"actor": actor, "snapshot": json.dumps({"lines": old_signatures})})
            lines = {name: conn.scalar(text(
                "INSERT INTO presales_inquiry_line (inquiry_id,line_key,seq,part_id,pn_raw,qty,version) "
                "VALUES (:h,:name,:seq,:part,'Q',10,2) RETURNING id"
            ), {"h": header, "name": name, "seq": seq, "part": part}) for seq, name in enumerate(names, 1)}
            offers = {}
            for name in ("unchanged", "changed", "missing", "malformed", "signed", "fallback", "unrelated"):
                offers[name] = conn.scalar(text(
                    "INSERT INTO presales_offer (inquiry_id,line_id,part_id,created_by,status,unit_price,valid_from,revision_no) "
                    "VALUES (:h,:line,:part,:actor,'published',41,'2021-01-01',1) RETURNING id"
                ), {"h": header, "line": lines[name], "part": part, "actor": actor})
                snap = {"id": offers[name], "line_id": lines[name], "part_id": part, "inquiry_id": header,
                        "status": "published", "valid_from": "2021-01-01", "unit_price": "41.00"}
                if name == "signed":
                    snap["_inquiry"] = {"line_conditions": {**current, "qty": "10.000"}}
                conn.execute(text(
                    "INSERT INTO presales_offer_revision (offer_id,revision_no,snapshot,actor) "
                    "VALUES (:offer,1,CAST(:snapshot AS jsonb),:actor)"
                ), {"offer": offers[name], "snapshot": json.dumps(snap), "actor": actor})
            for name in ("skip", "private"):
                conn.execute(text(
                    "INSERT INTO presales_line_response (line_id,created_by,line_version,mode,reason,is_draft) "
                    "VALUES (:line,:actor,2,'skip','旧答复',:draft)"
                ), {"line": lines[name], "actor": actor, "draft": name == "private"})
            for name, source in (("ref", "unchanged"), ("ref_only", "changed"), ("fallback", "changed")):
                conn.execute(text("UPDATE presales_inquiry_line SET ref_offer_id=:offer, ref_offer_revision_no=1 WHERE id=:id"),
                             {"offer": offers[source], "id": lines[name]})
            conn.execute(text("INSERT INTO presales_offer_line_link (line_id,offer_id,offer_revision_no) VALUES (:id,:offer,1)"),
                         {"id": lines["linked"], "offer": offers["changed"]})
            expected = {lines[name] for name in ("changed", "missing", "malformed", "skip", "ref")}
            assert set(conn.scalars(text(review_ids))) == expected
            original = business_rows(conn)

        command.upgrade(cfg, pool_head)
        for target in (pool_head, "head"):
            command.upgrade(cfg, target)
            with engine.connect() as conn:
                states = {row.id: row for row in conn.execute(text(
                    "SELECT id, answer_requires_review, answer_state, current_offer_id, answer_reason, answered_by, answered_at "
                    "FROM presales_inquiry_line WHERE inquiry_id=:h"), {"h": header})}
                assert {row.id for row in states.values() if row.answer_requires_review} == expected
                for name in ("changed", "missing", "malformed", "skip", "ref", "ref_only", "linked", "private", "empty"):
                    assert tuple(states[lines[name]][2:]) == ("pending", None, None, None, None)
                for name in ("unchanged", "signed", "fallback", "unrelated"):
                    assert (states[lines[name]].answer_state, states[lines[name]].current_offer_id) == ("quoted", offers[name])
            # 普通重算不能将旧价写回同一行及其引用行。
            from app.services.presales import answers
            answers.refresh(db, lines.values())
            db.commit()
            with engine.connect() as conn:
                assert not conn.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM presales_inquiry_line WHERE current_offer_id=:offer)"
                ), {"offer": offers["changed"]})
            db.close()
        command.downgrade(cfg, base)
        with engine.connect() as conn:
            assert "answer_requires_review" not in {col["name"] for col in inspect(conn).get_columns("presales_inquiry_line")}
            assert business_rows(conn) == original
    finally:
        db.close()
        command.upgrade(cfg, "head")
