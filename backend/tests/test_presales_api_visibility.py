"""售前 HTTP 与受众反例矩阵，覆盖字段旁路、历史、待办和搜索输出。"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app import permissions
from app.auth import _make_token
from app.main import app
from app.models.dimensions import DimCustomer, DimPart, DimSupplier
from app.models.presales import PresalesOffer, PresalesOfferRevision
from app.models.system import SysAccessLog, SysUser
from app.services import audience
from app.services.presales import common, inquiries, library, offers, visibility
from app.services.presales.registration import register_subjects
from app.services.workflow import events, work_items
from app.services.workflow.registry import InvalidTransition, Subject


@pytest.fixture()
def users(db):
    register_subjects()
    users = {}
    for name, role in (("seller", "sales"), ("other", "sales"), ("buyer", "purchaser"),
                       ("peer", "purchaser"), ("supervisor", "readonly"), ("denied", "readonly")):
        graph = permissions.effective(role, None)
        if name in {"seller", "other"}:
            graph["data_supplier"] = True
        if name == "supervisor":
            graph.update(page_presales=True, action_presales_supervise=True,
                         data_supplier=True, data_customer=True)
        row = SysUser(username=f"api_{name}", role=role, password_hash="测试不使用口令",
                      display_name=name, template_perms=graph, perm_overrides={})
        db.add(row)
        users[name] = row
    part = DimPart(pn_std="API-PRESALES-PN", description="接口测试")
    supplier = DimSupplier(name_raw="供应商机密")
    customer = DimCustomer(name_raw="客户机密")
    db.add_all([part, supplier, customer])
    db.commit()
    return SimpleNamespace(**users, part=part, supplier=supplier, customer=customer)


def _ctx(db, user):
    return common.resolve_actor(db, user.id)[1]


def _client(user):
    token, _ = _make_token(user.role, user.username, user.display_name, authn="sys_user")
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + token
    return client


def _quoted(db, users, *, rule=None, whole=None):
    inquiry = inquiries.create_draft(db, users.seller.id, customer_id=users.customer.id,
        lines=[{"part_id": users.part.id, "qty": 2}],
        assignee_ids=[users.buyer.id, users.peer.id], offer_audience=whole)
    inquiries.submit(db, inquiry.id, users.seller.id, inquiry.version)
    data = {"unit_price": "50.25", "supplier_id": users.supplier.id,
            "notes": "采购机密", "stock_location": "北京"}
    if rule:
        data.update(supplier_audience=rule, reporter_audience=rule, notes_audience=rule)
    offer = offers.save_draft(db, inquiry.id, inquiries.lines_for(db, inquiry.id)[0].id,
                             users.buyer.id, data, expected_version=inquiry.version)
    offers.reply(db, inquiry.id, users.buyer.id,
        expected_version=inquiry.version, on_existing="new")
    db.commit()
    return inquiry, offer


def test_generic_work_item_complete_cannot_bypass_per_line_reply_validation(db, users):
    inquiry = inquiries.create_draft(db, users.seller.id, lines=[{"part_id": users.part.id}],
                                     assignee_ids=[users.buyer.id])
    inquiries.submit(db, inquiry.id, users.seller.id, inquiry.version)
    item = work_items.list_by_subject(db, Subject("presales_inquiry", inquiry.id), users.buyer.id)[0]
    with pytest.raises(InvalidTransition, match="每行"):
        work_items.complete(db, item.id, users.buyer.id, item.version)
    assert item.status == "pending"


def test_pool_deadlock_at_commit_preserves_price_and_version(db, users):
    inquiry, offer = _quoted(db, users)
    line = inquiries.lines_for(db, inquiry.id)[0]
    url = f"/api/presales/pool/lines/{line.id}/price"
    original_version, original_revision = line.version, offer.revision_no
    client = _client(users.peer)
    change = {"expected_version": original_version, "unit_price": "97.50"}

    def deadlock(session):
        session.execute(text("DO $$ BEGIN RAISE EXCEPTION 'concurrent update' "
                             "USING ERRCODE = '40P01'; END $$"))

    event.listen(Session, "before_commit", deadlock, once=True)
    try:
        response = client.put(url, json=change)
    finally:
        event.remove(Session, "before_commit", deadlock)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "concurrent_write"
    db.refresh(line)
    db.refresh(offer)
    assert (line.version, offer.revision_no, offer.unit_price) == (original_version, original_revision, Decimal("50.25"))
    saved = client.put(url, json=change)
    assert saved.status_code == 200, saved.text
    assert saved.json()["price"]["unit_price"] == "97.50"


def test_revision_api_reads_pre_pool_snapshots_without_rewriting_them(db, users):
    _, offer = _quoted(db, users)
    previous = db.scalar(select(PresalesOfferRevision).where(PresalesOfferRevision.offer_id == offer.id))
    offer.revision_no += 1
    legacy = dict(previous.snapshot, revision_no=offer.revision_no)
    legacy.pop("updated_by")
    legacy.pop("price_basis")
    revision = PresalesOfferRevision(offer_id=offer.id, revision_no=offer.revision_no,
        snapshot=legacy, actor=users.buyer.id, changed_fields=["unit_price"])
    db.add(revision)
    db.commit()
    response = _client(users.other).get(f"/api/presales/offers/{offer.id}/revisions")
    assert response.status_code == 200
    snapshot = response.json()[-1]["snapshot"]
    assert snapshot["reporter"]["id"] == users.buyer.id
    assert snapshot["unit_price"] == legacy["unit_price"] and snapshot["price_basis"] == "sale"
    db.refresh(revision)
    assert revision.snapshot == legacy


def test_http_two_buyers_reply_compare_close_and_audit(db, users):
    seller, buyer, peer = (_client(user) for user in (users.seller, users.buyer, users.peer))
    response = seller.post("/api/presales/inquiries", json={
        "customer_id": users.customer.id, "lines": [{"part_id": users.part.id, "qty": "2"}],
        "assignee_ids": [users.buyer.id, users.peer.id],
        "field_values": [{"new_name": "交货要求", "value": "工作日"}],
    })
    assert response.status_code == 201, response.text
    inquiry = response.json()
    assert inquiry["field_values"][0]["name"] == "交货要求"
    path = f"/api/presales/inquiries/{inquiry['id']}"
    response = seller.post(path + "/submit", json={"expected_version": inquiry["version"]})
    assert response.status_code == 200, response.text
    inquiry = response.json()
    first_offer = None
    for client, price in ((buyer, "25.50"), (peer, "24.50")):
        quote = client.post(path + f"/lines/{inquiry['lines'][0]['id']}/offers", json={
            "expected_version": inquiry["version"], "unit_price": price})
        assert quote.status_code == 200, quote.text
        first_offer = first_offer or quote.json()["id"]
        reply = client.post(path + "/reply", json={"expected_version": inquiry["version"]})
        assert reply.status_code == 200, reply.text
    detail = seller.get(path).json()
    assert len(detail["offers"]) == 2 and all(item["status"] == "done" for item in detail["work_items"])
    closed = seller.post(path + "/close", json={"expected_version": detail["version"],
                                               "outcome": "won", "won_offer_id": first_offer})
    assert closed.status_code == 200 and closed.json()["status"] == "closed", closed.text
    copied = seller.post(path + "/copy", json={"expected_version": closed.json()["version"]})
    assert copied.status_code == 201 and copied.json()["status"] == "draft", copied.text
    assert copied.json()["lines"][0]["line_key"] != inquiry["lines"][0]["line_key"]
    actions = set(db.scalars(select(SysAccessLog.action).where(SysAccessLog.resource == "presales")))
    assert {"inquiry.created", "inquiry.submitted", "presales.offer.replied", "inquiry.closed"} <= actions


def test_http_anonymous_denied_and_conflict_never_mutates(db, users):
    assert TestClient(app).get("/api/presales/inquiries").status_code == 401
    assert _client(users.denied).get("/api/presales/inquiries").status_code == 403
    client = _client(users.seller)
    response = client.post("/api/presales/inquiries", json={"lines": [{"pn_raw": "待建档原文"}]})
    assert response.status_code == 201, response.text
    inquiry = response.json()
    path = f"/api/presales/inquiries/{inquiry['id']}"
    conflict = client.patch(path, json={"expected_version": inquiry["version"] + 1, "notes": "不应写入"})
    assert conflict.status_code == 409, conflict.text
    assert client.get(path).json()["notes"] is None
    assert client.patch(path, json={"notes": "缺少版本"}).status_code == 422
    assert client.get("/api/presales/library?part_ids=-1").status_code == 422


def test_http_proactive_notice_reference_library_and_revisions(db, users):
    buyer = _client(users.buyer)
    response = buyer.post("/api/presales/inquiries", json={"kind": "proactive_offer",
        "lines": [{"part_id": users.part.id, "field_values": [{"new_name": "质保", "value": "一年"}]}]})
    assert response.status_code == 201, response.text
    inquiry = response.json()
    path = f"/api/presales/inquiries/{inquiry['id']}"
    quote = buyer.post(path + f"/lines/{inquiry['lines'][0]['id']}/offers", json={
        "expected_version": inquiry["version"], "unit_price": "88.88", "is_tax_inclusive": True})
    assert quote.status_code == 200, quote.text
    published = buyer.post(path + "/publish", json={"expected_version": inquiry["version"],
                                                   "notice_user_ids": [users.other.id]})
    assert published.status_code == 200, published.text
    offer = published.json()[0]
    quote = buyer.patch(f"/api/presales/offers/{offer['id']}", json={
        "expected_version": offer["version"], "unit_price": "90.00"})
    assert quote.status_code == 200 and quote.json()["revision_no"] == 2, quote.text
    history = buyer.get(f"/api/presales/offers/{offer['id']}/revisions")
    assert history.status_code == 200 and len(history.json()) == 2, history.text
    library_response = _client(users.other).get(f"/api/presales/library?part_ids={users.part.id}")
    assert library_response.status_code == 200 and len(library_response.json()[str(users.part.id)]) == 1
    notices = _client(users.other).get("/api/work-items?subject_type=presales_offer_notice")
    assert notices.status_code == 200 and notices.json()["total"] == 1, notices.text
    refs = buyer.post("/api/presales/reference", json={"inquiry_id": inquiry["id"]})
    assert refs.status_code == 200, refs.text
    assert refs.json()["gallery"] == [{"part_id": users.part.id}]
    assert str(users.part.id) in refs.json()["reference"]
    assert buyer.post(f"/api/presales/offers/{offer['id']}/withdraw", json={
        "expected_version": quote.json()["version"], "reason": "报价错误"}).status_code == 200
    assert not _client(users.other).get(f"/api/presales/library?part_ids={users.part.id}").json()[str(users.part.id)]


def test_supervisor_can_manage_team_fields_and_public_groups(db, users):
    client = _client(users.supervisor)
    defined = client.post("/api/fields/defs", json={"subject_type": "presales_inquiry",
        "level": "line", "name": "团队质保", "owner_scope": "team"})
    assert defined.status_code == 201, defined.text
    group = client.post("/api/audience/groups", json={"scope": "public", "name": "售前采购组",
                                                    "user_ids": [users.buyer.username]})
    assert group.status_code == 201, group.text


def test_http_binding_check_then_existing_bind_and_create_new_are_versioned(db, users):
    seller, buyer = _client(users.seller), _client(users.buyer)
    response = seller.post("/api/presales/inquiries", json={"assignee_ids": [users.buyer.id],
        "lines": [{"pn_raw": users.part.pn_std}, {"pn_raw": "COMPLETELY-NEW-76095842"}]})
    assert response.status_code == 201, response.text
    inquiry = response.json()
    response = seller.post(f"/api/presales/inquiries/{inquiry['id']}/submit", json={
        "expected_version": inquiry["version"]})
    assert response.status_code == 200, response.text
    lines = response.json()["lines"]
    path = f"/api/presales/lines/{lines[0]['id']}"
    checked = buyer.get(path + "/part-check")
    assert checked.status_code == 200 and checked.json()["near_duplicates"], checked.text
    reject = buyer.post(path + "/create-part", json={"expected_version": lines[0]["version"],
                                                    "pn_std": users.part.pn_std})
    assert reject.status_code == 409, reject.text
    bound = buyer.post(path + "/bind-part", json={"expected_version": lines[0]["version"],
                                                  "part_id": users.part.id})
    assert bound.status_code == 200 and bound.json()["lines"][0]["part_id"] == users.part.id, bound.text
    repeat = buyer.post(path + "/bind-part", json={"expected_version": lines[0]["version"],
                                                   "part_id": users.part.id})
    assert repeat.status_code == 409, repeat.text
    created = buyer.post(f"/api/presales/lines/{lines[1]['id']}/create-part", json={
        "expected_version": lines[1]["version"], "pn_std": lines[1]["pn_raw"],
        "description": "新建型号说明", "brand": "测试品牌"})
    assert created.status_code == 200 and created.json()["lines"][1]["part_id"] is not None, created.text


def test_referenced_proactive_offer_keeps_identity_when_renewed(db, users):
    source = inquiries.create_draft(db, users.buyer.id, kind="proactive_offer",
                                     lines=[{"part_id": users.part.id}])
    source_line = inquiries.lines_for(db, source.id)[0]
    offer = offers.save_draft(db, source.id, source_line.id, users.buyer.id,
        {"unit_price": "10"}, expected_version=source.version)
    offers.publish_proactive(db, source.id, users.buyer.id, expected_version=source.version)
    target = inquiries.create_draft(db, users.seller.id, lines=[{
        "part_id": users.part.id, "ref_offer_id": offer.id}])
    inquiries.submit(db, target.id, users.seller.id, target.version)
    assert work_items.list_by_subject(db, Subject("presales_inquiry", target.id), users.seller.id) == []
    offers.update_offer(db, offer.id, users.buyer.id, {"unit_price": "12"},
                        expected_version=offer.version)
    assert inquiries.lines_for(db, target.id)[0].ref_offer_id == offer.id
    visible = library.batch(db, [users.part.id], _ctx(db, users.seller))[users.part.id][0]
    assert visible["id"] == offer.id and visible["revision_no"] == 2
    assert visible["unit_price"] == Decimal("12")
