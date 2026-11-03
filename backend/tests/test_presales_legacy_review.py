"""生产旧库的撤回改单在第一次共享池读取前就必须隔离未经重新确认的答复。"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import select, text

from app.models.presales import PresalesOffer, PresalesOfferRevision
from app.services.presales import answers, common, inquiries
from tests.conftest import _alembic_cfg
from tests.test_presales_api_visibility import _client, _quoted, users  # noqa: F401


@pytest.fixture(autouse=True)
def effective_prices(monkeypatch):
    monkeypatch.setattr(common, "now", lambda: datetime(2025, 1, 1, tzinfo=timezone.utc))


def test_first_pool_read_close_and_explicit_price_after_real_production_schema_upgrade(db, users):
    inquiry = inquiries.create_draft(db, users.seller.id, lines=[{"part_id": users.part.id, "qty": 2}])
    inquiries.submit(db, inquiry.id, users.seller.id, inquiry.version, open_tasks=False)
    line = inquiries.lines_for(db, inquiry.id)[0]
    actor, buyer = _client(users.seller), _client(users.buyer)
    doc_id, line_id, old_revision = inquiry.id, line.id, 1
    part_id, buyer_id = users.part.id, users.buyer.id
    db.commit()
    engine, cfg = db.get_bind(), _alembic_cfg()
    db.close()
    try:
        command.downgrade(cfg, "a3c5e7f9b2d4")
        with engine.begin() as conn:
            conn.execute(text("UPDATE presales_inquiry SET status='revising', version=version+1 WHERE id=:id"), {"id": doc_id})
            conn.execute(text("UPDATE presales_inquiry_line SET qty=7, version=version+1 WHERE id=:id"), {"id": line_id})
            old_offer = conn.scalar(text("""INSERT INTO presales_offer
                (inquiry_id,line_id,part_id,unit_price,is_tax_inclusive,created_by,status,valid_from,revision_no)
                VALUES (:doc,:line,:part,50.25,true,:buyer,'published','2024-12-01',1) RETURNING id"""),
                {"doc": doc_id, "line": line_id, "part": part_id, "buyer": buyer_id})
            conn.execute(text("""INSERT INTO presales_offer_revision (offer_id,revision_no,snapshot,changed_fields,actor)
                SELECT o.id,1,to_jsonb(o) || jsonb_build_object('unit_price',o.unit_price::text,'_inquiry',
                    jsonb_build_object('id',i.id,'kind',i.kind,'created_by',i.created_by,'status','submitted',
                    'offer_audience_rule_id',NULL,'submitted_at',i.submitted_at)),'["unit_price"]'::jsonb,o.created_by
                FROM presales_offer o JOIN presales_inquiry i ON i.id=o.inquiry_id WHERE o.id=:id"""), {"id": old_offer})
            original = conn.scalar(text("SELECT snapshot FROM presales_offer_revision WHERE offer_id=:id AND revision_no=:n"),
                                   {"id": old_offer, "n": old_revision})
        command.upgrade(cfg, "e5a7c9d1f3b6")
        path = f"/api/presales/inquiries/{doc_id}"
        row_path = f"/api/presales/pool/lines/{line_id}"
        def read_pool():
            response = buyer.get("/api/presales/pool/lines?tab=all")
            assert response.status_code == 200, response.text
            return next(r for r in response.json()["items"] if r["line_id"] == line_id)
        initial = read_pool()
        assert (initial["answer_requires_review"], initial["answer_state"], initial["price"],
                initial["latest_same_pn"]) == (True, "pending", None, None)
        detail = actor.get(path).json()
        assert detail["progress"]["effective_quoted_lines"] == 0
        assert detail["line_answers"] == [{"line_id": line_id, "state": "pending", "responded": False,
                                          "answer_requires_review": True}]
        history_offer = next(o for o in detail["offers"] if o["id"] == old_offer)
        assert history_offer["expired"] and Decimal(str(history_offer["unit_price"])) == Decimal("50.25")
        assert buyer.get(f"/api/presales/library?part_ids={part_id}").json()[str(part_id)] == []
        cannot_reuse = actor.post("/api/presales/inquiries", json={"lines": [{"part_id": part_id, "ref_offer_id": old_offer}]})
        assert cannot_reuse.status_code == 422
        another = buyer.post("/api/presales/pool/inquiries", json={"lines": [{"part_id": part_id, "pn_raw": "API-PRESALES-PN"}]})
        assert another.status_code == 201, another.text
        rows = buyer.get("/api/presales/pool/lines?tab=all").json()["items"]
        fresh_line = next(r for r in rows if r["inquiry_id"] == another.json()["inquiry_id"])
        assert not fresh_line["answer_requires_review"] and fresh_line["latest_same_pn"] is None
        chosen = {"expected_version": detail["version"], "outcome": "won", "won_offer_id": old_offer,
                  "expected_offer_revision_no": old_revision}
        assert actor.post(path + "/close", json=chosen).status_code in {409, 422}
        assert buyer.patch(f"/api/presales/offers/{old_offer}", json={
            "expected_version": history_offer["version"], "notes": "只换备注"}).status_code == 409
        answers.refresh_inquiry(db, doc_id, bump_versions=True)
        db.commit()
        assert read_pool()["price"] is None
        assert db.get(PresalesOffer, old_offer).status == "published"

        notes = buyer.patch(row_path, json={"expected_version": initial["line_version"], "notes": "待重新询价"})
        assert notes.status_code == 200 and notes.json()["answer_requires_review"]
        assert notes.json()["price"] is None
        # 正常用户主动重新填价，原报价及其历史绝不能变成新条件的可用来源。
        saved = buyer.put(row_path + "/price", json={"expected_version": notes.json()["line_version"], "unit_price": "86.50"})
        assert saved.status_code == 200, saved.text
        current = saved.json()
        assert not current["answer_requires_review"] and current["answer_state"] == "quoted"
        assert current["price"]["offer_id"] != old_offer and Decimal(current["price"]["unit_price"]) == Decimal("86.50")
        assert actor.post(path + "/close", json=chosen).status_code in {409, 422}
        close = actor.post(path + "/close", json={**chosen, "won_offer_id": current["price"]["offer_id"],
                                                  "expected_offer_revision_no": current["price"]["revision_no"]})
        assert close.status_code == 200, close.text
        assert Decimal(str(close.json()["won_offer"]["unit_price"])) == Decimal("86.50")
        db.expire_all()
        assert db.get(PresalesOffer, old_offer).status == "stale"
        assert db.scalar(select(PresalesOfferRevision.snapshot).where(
            PresalesOfferRevision.offer_id == old_offer, PresalesOfferRevision.revision_no == old_revision)) == original
    finally:
        db.rollback()
        db.close()
        command.upgrade(cfg, "head")


def test_held_link_cannot_adopt_an_otherwise_valid_external_quote(db, users):
    _, offer = _quoted(db, users)
    inquiry = inquiries.create_draft(db, users.seller.id, lines=[{"part_id": users.part.id, "ref_offer_id": offer.id}])
    inquiries.submit(db, inquiry.id, users.seller.id, inquiry.version, open_tasks=False)
    line = inquiries.lines_for(db, inquiry.id)[0]
    line.answer_requires_review = True
    answers.refresh(db, [line.id])
    db.commit()
    owner = _client(users.seller)
    path = f"/api/presales/inquiries/{inquiry.id}"
    detail = owner.get(path).json()
    assert detail["line_answers"][0]["state"] == "pending"
    assert detail["lines"][0]["ref_offer"]["expired"]
    chosen = {"expected_version": detail["version"], "outcome": "won", "won_offer_id": offer.id,
              "expected_offer_revision_no": offer.revision_no}
    assert owner.post(path + "/close", json=chosen).status_code == 422
    # 外部来源本身已有明确答复；同单新增一个真正有效的引用仍可成交。
    assert owner.get(f"/api/presales/library?part_ids={users.part.id}").json()[str(users.part.id)]
    appended = owner.post(path + "/lines", json={"expected_version": detail["version"],
        "lines": [{"part_id": users.part.id, "ref_offer_id": offer.id}]})
    assert appended.status_code == 200, appended.text
    closed = owner.post(path + "/close", json={**chosen, "expected_version": appended.json()["version"]})
    assert closed.status_code == 200, closed.text


def test_offline_win_without_adopted_price_does_not_turn_held_price_into_facts(db, users):
    inquiry, old = _quoted(db, users)
    line = inquiries.lines_for(db, inquiry.id)[0]
    line.answer_requires_review = True
    answers.refresh(db, [line.id])
    db.commit()
    done = _client(users.seller).post(f"/api/presales/inquiries/{inquiry.id}/close",
                                     json={"expected_version": inquiry.version, "outcome": "won"})
    assert done.status_code == 200, done.text
    assert done.json()["won_offer_id"] is None and done.json()["won_offer_revision_no"] is None
    assert db.get(PresalesOffer, old.id).status == "published"


@pytest.mark.parametrize("resolve", ["skip", "conditions"])
def test_explicit_resolution_does_not_release_the_old_price_as_a_candidate(db, users, resolve):
    inquiry, old = _quoted(db, users)
    line = inquiries.lines_for(db, inquiry.id)[0]
    line.answer_requires_review = True
    answers.refresh(db, [line.id])
    db.commit()
    buyer = _client(users.buyer)
    endpoint = f"/api/presales/pool/lines/{line.id}"
    version = {"expected_version": line.version}
    if resolve == "skip":
        response = buyer.post(endpoint + "/skip", json={**version, "reason": "按新数量无法供货"})
    else:
        assert buyer.patch(endpoint, json={**version, "qty": 8}).status_code == 409
        response = buyer.patch(endpoint, json={**version, "qty": 8, "confirm_reset": True})
    assert response.status_code == 200, response.text
    row = response.json()
    assert not row["answer_requires_review"] and row["price"] is None
    assert row["answer_state"] == ("skip" if resolve == "skip" else "pending")
    db.refresh(old)
    assert old.status == "stale"
