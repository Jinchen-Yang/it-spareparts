"""全量编辑和共享池的来回修改必须立即遵守同一行价约束。"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.presales import PresalesLineResponse, PresalesOffer, PresalesOfferLineLink, PresalesOfferRevision
from app.models.workflow import SubjectEvent
from app.services.presales import answers, common, inquiries, offers, pool
from tests.test_presales_api_visibility import _client, users  # noqa: F401
from tests.test_presales_pool import _inquiry, _price


@pytest.fixture(autouse=True)
def effective_prices(monkeypatch):
    # 本组检查改行、解绑和修订范围。让合成报价在应用及数据库时钟下均已生效。
    monkeypatch.setattr(common, "now", lambda: datetime(2025, 1, 1, tzinfo=timezone.utc))


def _edit(client, path, entries, confirm=False):
    current = client.get(path).json()
    return client.patch(path, json={"expected_version": current["version"],
        "expected_line_versions": {r["line_key"]: r["version"] for r in current["lines"]},
        "lines": entries, **({"confirm_reset": True} if confirm else {})})


@pytest.mark.parametrize("new_line", [False, True])
@pytest.mark.parametrize("remove", [False, True])
def test_document_reset_is_effective_before_resubmit_and_never_revives_a_price(db, users, new_line, remove):
    line = _inquiry(db, users.seller, users.part)
    owner, viewer = _client(users.seller), _client(users.peer)
    path = f"/api/presales/inquiries/{line.inquiry_id}"
    first = owner.get(path).json()
    assert owner.post(path + "/withdraw", json={"expected_version": first["version"]}).status_code == 200
    items = [{"line_key": line.line_key, "qty": 1 if new_line else 2}, {"line_key": "spare", "pn_raw": "备用行"}]
    if new_line:
        items.append({"line_key": "new", "part_id": users.part.id, "qty": 2})
    edited = _edit(owner, path, items)
    assert edited.status_code == 200, edited.text
    target = edited.json()["lines"][-1 if new_line else 0]
    endpoint = f"/api/presales/pool/lines/{target['id']}"
    quoted = viewer.put(endpoint + "/price", json={"expected_version": target["version"], "unit_price": "100"})
    assert quoted.status_code == 200, quoted.text
    offer_id = quoted.json()["price"]["offer_id"]
    replacements = [{"line_key": r["line_key"]} for r in edited.json()["lines"] if r["id"] != target["id"]]
    if not remove:
        replacements.append({"line_key": target["line_key"], "qty": 1})
    rejected = _edit(owner, path, replacements)
    assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "price_reset_required"
    saved = _edit(owner, path, replacements, confirm=True)
    assert saved.status_code == 200, saved.text
    db.expire_all()
    assert db.get(PresalesOffer, offer_id).status == "stale"
    fetched = viewer.get("/api/presales/pool/lines?tab=all").json()["items"]
    public = next((item for item in fetched if item["line_id"] == target["id"]), None)
    if remove:
        assert public is None
    else:
        assert (Decimal(public["qty"]), public["answer_state"], public["price"]) == (1, "pending", None)
        assert any(h.get("unit_price") and Decimal(h["unit_price"]) == 100
                   for h in viewer.get(endpoint + "/history").json())
        fresh = viewer.put(endpoint + "/price", json={"expected_version": public["line_version"], "unit_price": "85"})
        assert fresh.status_code == 200
    result = owner.post(path + "/resubmit", json={"expected_version": saved.json()["version"]})
    assert result.status_code == 200, result.text
    if not remove:
        price = next(r for r in viewer.get("/api/presales/pool/lines?tab=all").json()["items"] if r["line_id"] == target["id"])
        assert Decimal(price["price"]["unit_price"]) == 85


@pytest.mark.parametrize("writer", ["pool", "document"])
def test_pending_external_link_detaches_on_condition_change_and_keeps_private_skip(db, users, writer):
    target = _inquiry(db, users.seller, users.part)
    source = _inquiry(db, users.peer, users.part)
    offer_id = _price(db, users.peer, source, "11")["price"]["offer_id"]
    offer = db.get(PresalesOffer, offer_id)
    target.ref_offer_id, target.ref_offer_revision_no = offer.id, offer.revision_no
    db.add(PresalesOfferLineLink(line_id=target.id, offer_id=offer.id, offer_revision_no=offer.revision_no))
    draft = PresalesLineResponse(line_id=target.id, created_by=users.buyer.id, line_version=target.version,
                                 is_draft=True, mode="skip", reason="仅个人")
    db.add(draft)
    answers.refresh(db, [target.id])
    assert target.answer_state == "quoted"
    offers.update_offer(db, offer.id, users.peer.id, {"no_stock": True, "unit_price": None, "notes": "暂时缺货"}, expected_version=offer.version)
    db.commit()
    assert target.answer_state == "pending"
    if writer == "document":
        inq = common.get_inquiry(db, target.inquiry_id)
        inquiries.withdraw(db, inq.id, users.seller.id, inq.version)
        inquiries.update_draft(db, inq.id, users.seller.id, inq.version, lines=[
            {"line_key": target.line_key, "qty": 2, "ref_offer_id": offer.id}])
    else:
        pool.edit_line(db, target.id, users.seller.id, {"qty": Decimal(2)}, expected_version=target.version)
    db.commit()
    assert target.ref_offer_id is None and not db.scalar(select(PresalesOfferLineLink.offer_id).where(
        PresalesOfferLineLink.line_id == target.id))
    _price(db, users.peer, source, "22")
    assert target.answer_state == "pending" and draft.is_draft and draft.reason == "仅个人"
    prices = {Decimal(h["unit_price"]) for h in pool.history(db, target.id, users.seller.id) if h.get("unit_price")}
    assert prices == {11}


def test_document_can_set_a_different_valid_reference_while_replacing_the_conditions(db, users):
    first = _inquiry(db, users.peer, users.part)
    old_id = _price(db, users.peer, first, "11")["price"]["offer_id"]
    second = _inquiry(db, users.peer, users.part)
    new_id = _price(db, users.buyer, second, "22")["price"]["offer_id"]
    owner = _client(users.seller)
    draft = owner.post("/api/presales/inquiries", json={"lines": [{
        "part_id": users.part.id, "ref_offer_id": old_id}]}).json()
    path = f"/api/presales/inquiries/{draft['id']}"
    submitted = owner.post(path + "/submit", json={"expected_version": draft["version"]})
    assert submitted.status_code == 200, submitted.text
    edited = owner.post(path + "/withdraw", json={"expected_version": submitted.json()["version"]}).json()
    line = edited["lines"][0]
    change = [{"line_key": line["line_key"], "qty": 2, "ref_offer_id": new_id}]
    saved = _edit(owner, path, change, confirm=True)
    assert saved.status_code == 200, saved.text
    assert saved.json()["lines"][0]["ref_offer_id"] == new_id
    db.expire_all()
    row = pool.line_view(db, line["id"], users.seller.id)
    assert row["price"]["offer_id"] == new_id and Decimal(row["price"]["unit_price"]) == 22
    _price(db, users.peer, first, "99")
    assert {Decimal(h["unit_price"]) for h in pool.history(db, line["id"], users.seller.id)
            if h.get("unit_price")} == {11, 22}


@pytest.mark.parametrize("action", ["resubmit", "replace"])
def test_pre_upgrade_revising_conditions_do_not_reactivate_echoed_old_reference(db, users, action):
    source = _inquiry(db, users.peer, users.part)
    ref = _price(db, users.peer, source, "35")["price"]["offer_id"]
    inquiry = inquiries.create_draft(db, users.seller.id, lines=[{"part_id": users.part.id, "ref_offer_id": ref}])
    inquiries.submit(db, inquiry.id, users.seller.id, inquiry.version, open_tasks=False)
    line = inquiries.lines_for(db, inquiry.id)[0]
    # 模拟旧版本撤回编辑后的实际存量，原引用仍挂着，只有第一次提交的条件快照。
    inquiry.status = "revising"
    line.qty, line.version = 9, line.version + 1
    db.commit()
    ctx = _client(users.seller)
    path = f"/api/presales/inquiries/{inquiry.id}"
    if action == "replace":
        saved = _edit(ctx, path, [{"line_key": line.line_key, "ref_offer_id": ref, "notes": "保留新数量"}])
        assert saved.status_code == 200, saved.text
    else:
        saved = ctx.post(path + "/resubmit", json={"expected_version": inquiry.version})
        assert saved.status_code == 200, saved.text
    db.refresh(line)
    assert line.answer_state == "pending" and line.ref_offer_id is None
    _price(db, users.peer, source, "46")
    assert line.answer_state == "pending"
    assert {Decimal(h["unit_price"]) for h in pool.history(db, line.id, users.seller.id) if h.get("unit_price")} == {35}


@pytest.mark.parametrize(("ref_state", "stored_qty"), [(None, "1E+1"), (None, "10.000"), (True, 10.0)])
def test_legacy_reference_and_equivalent_quantities_leave_all_priced_sources_alone(db, users, ref_state, stored_qty):
    reference_line = _inquiry(db, users.peer, users.part)
    ref_id = _price(db, users.peer, reference_line, "11")["price"]["offer_id"]
    linked_line = _inquiry(db, users.buyer, users.part)
    linked_id = _price(db, users.buyer, linked_line, "22")["price"]["offer_id"]
    line = _inquiry(db, users.seller, users.part, qty=10)
    own_id = _price(db, users.seller, line, "33")["price"]["offer_id"]
    inquiry = common.get_inquiry(db, line.inquiry_id)
    prior = dict(inquiries.quote_conditions(line), qty=stored_qty, legacy_extra="保留")
    del prior["return_defective"]  # D-0047 之前默认返回坏件。
    if ref_state is not None:
        prior["has_reference"] = ref_state
    line.ref_offer_id, line.ref_offer_revision_no = ref_id, 1
    db.add(PresalesOfferLineLink(line_id=line.id, offer_id=linked_id, offer_revision_no=1))
    inquiry.status = "revising"
    inquiry.submitted_snapshot = {"lines": {line.line_key: prior}}
    db.commit()
    owner = _client(users.seller)
    saved = owner.post(f"/api/presales/inquiries/{inquiry.id}/resubmit", json={"expected_version": inquiry.version})
    assert saved.status_code == 200, saved.text
    db.expire_all()
    assert (line.ref_offer_id, line.ref_offer_revision_no) == (ref_id, 1)
    assert db.get(PresalesOfferLineLink, (line.id, linked_id)).offer_revision_no == 1
    assert [(db.get(PresalesOffer, key).status, db.get(PresalesOffer, key).revision_no)
            for key in (own_id, ref_id, linked_id)] == [("published", 1)] * 3
    assert {Decimal(row["unit_price"]) for row in pool.history(db, line.id, users.seller.id)
            if row.get("unit_price")} == {11, 22, 33}
    assert db.scalar(select(SubjectEvent.id).where(SubjectEvent.kind == "line.price_unlinked")) is None
    assert db.scalar(select(SubjectEvent.payload).where(SubjectEvent.kind == "inquiry.resubmitted",
        SubjectEvent.subject_id == inquiry.id))["changed_line_keys"] == []


@pytest.mark.parametrize("complete", [True, False])
def test_changed_conditions_only_keep_complete_numerically_matching_legacy_price_proof(db, users, complete):
    line = _inquiry(db, users.seller, users.part, qty=10)
    key = _price(db, users.buyer, line, "45")["price"]["offer_id"]
    offer = db.get(PresalesOffer, key)
    revision = db.scalar(select(PresalesOfferRevision).where(PresalesOfferRevision.offer_id == key))
    conditions = dict(inquiries.quote_conditions(line), qty="10.000")
    if not complete:
        del conditions["return_defective"]  # 默认值只适用于旧单据基线，不能补作报价人的确认。
    snapshot = {**revision.snapshot, "revision_no": 2, "_inquiry": {**revision.snapshot["_inquiry"],
                                                                   "line_conditions": conditions}}
    db.add(PresalesOfferRevision(offer_id=key, revision_no=2, snapshot=snapshot, actor=users.buyer.id))
    offer.revision_no, offer.version = 2, offer.version + 1
    inquiry = common.get_inquiry(db, line.inquiry_id)
    inquiry.status = "revising"
    inquiry.submitted_snapshot = {"lines": {line.line_key: dict(inquiries.quote_conditions(line), qty="3")}}
    db.commit()
    saved = _client(users.seller).post(f"/api/presales/inquiries/{inquiry.id}/resubmit",
                                       json={"expected_version": inquiry.version})
    assert saved.status_code == 200, saved.text
    db.refresh(offer)
    assert offer.status == ("published" if complete else "stale")
