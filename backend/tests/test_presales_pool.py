"""D-0052 询价报价共享池：谁都能询价报价、只锁单行、谁更新记录谁。"""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from threading import Barrier, Thread

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app import permissions
from app.db import engine
from app.models.presales import (
    PresalesInquiry, PresalesInquiryLine, PresalesLineResponse, PresalesOffer, PresalesOfferLineLink,
)
from app.models.system import SysUser
from app.services.presales import answers, common, inquiries, inquiry_workbook, offers, pool, quote_workbook, visibility
from app.services.workflow.registry import Conflict, Forbidden, InvalidTransition, NotFound
from app.models.workflow import SubjectEvent, WorkItem
from app.services.workflow import work_items
from tests.test_presales_api_visibility import _client, _ctx, users  # noqa: F401


def _inquiry(db, user, part, *, qty=1, customer=None, priority="normal"):
    result = pool.create_inquiry(db, _ctx(db, user), customer_id=customer.id if customer else None, priority=priority,
        lines=[{"part_id": part.id, "pn_raw": part.pn_std, "qty": Decimal(qty)}])
    db.commit()
    return db.scalar(select(PresalesInquiryLine).where(PresalesInquiryLine.inquiry_id == result["inquiry_id"]))


def _price(db, user, line, value, **extra):
    db.refresh(line)
    row = pool.set_price(db, line.id, _ctx(db, user), {"unit_price": Decimal(value), **extra},
                         expected_version=line.version)
    db.commit()
    return row


@contextmanager
def _selects():
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_create_has_no_work_item_and_anyone_quotes_then_overwrites_with_updater(db, users):
    line = _inquiry(db, users.seller, users.part)
    assert db.scalars(select(WorkItem).where(WorkItem.subject_type == "presales_inquiry",
                                             WorkItem.subject_id == line.inquiry_id)).all() == []
    # 销售也能报价；另一个人覆盖后只有一个当前价，更新人变化。
    first = _price(db, users.other, line, "100.50")
    assert (first["answer_state"], first["price"]["unit_price"], first["answered_by"]) == ("quoted", "100.50", "other")
    second = _price(db, users.buyer, line, "90")
    assert (second["price"]["unit_price"], second["answered_by"]) == ("90.00", "buyer")
    assert second["price"]["offer_id"] == first["price"]["offer_id"]
    assert len(db.scalars(select(PresalesOffer).where(PresalesOffer.line_id == line.id,
                                                       PresalesOffer.status == "published")).all()) == 1
    history = pool.history(db, line.id, _ctx(db, users.peer))
    assert [(row["actor"], Decimal(row["unit_price"])) for row in history if row["kind"] == "price"][:2] == [
        ("buyer", Decimal("90")), ("other", Decimal("100.50"))]


def test_stale_version_conflict_returns_latest_and_keeps_winner(db, users):
    line = _inquiry(db, users.seller, users.part)
    version = line.version
    _price(db, users.buyer, line, "100")
    with pytest.raises(Conflict) as caught:
        pool.set_price(db, line.id, _ctx(db, users.peer), {"unit_price": Decimal("80")}, expected_version=version)
    db.rollback()
    assert caught.value.detail["code"] == "version_conflict"
    assert caught.value.detail["latest"]["price"]["unit_price"] == "100.00"
    assert pool.line_view(db, line.id, _ctx(db, users.peer))["answered_by"] == "buyer"


def test_concurrent_same_line_has_one_winner_and_other_lines_do_not_block(db, users):
    created = pool.create_inquiry(db, _ctx(db, users.seller), lines=[
        {"part_id": users.part.id, "pn_raw": users.part.pn_std, "qty": Decimal(1)},
        {"part_id": users.part.id, "pn_raw": users.part.pn_std, "qty": Decimal(2)}])
    db.commit()
    first, second = db.scalars(select(PresalesInquiryLine).where(
        PresalesInquiryLine.inquiry_id == created["inquiry_id"]).order_by(PresalesInquiryLine.seq)).all()
    first_id, first_version, second_id = first.id, first.version, second.id
    buyer_id, peer_id = users.buyer.id, users.peer.id
    barrier, outcomes = Barrier(2), []

    def attempt(user_id, value):
        with Session(engine) as session:
            barrier.wait()
            try:
                pool.set_price(session, first_id, common.resolve_actor(session, user_id)[1],
                               {"unit_price": Decimal(value)}, expected_version=first_version)
                session.commit()
                outcomes.append("ok")
            except Conflict:
                session.rollback()
                outcomes.append("conflict")

    threads = [Thread(target=attempt, args=args) for args in ((buyer_id, "10"), (peer_id, "20"))]
    [thread.start() for thread in threads]
    [thread.join(30) for thread in threads]
    assert sorted(outcomes) == ["conflict", "ok"]

    # 第一行的行锁未提交时，同单第二行可以立刻写入。
    with Session(engine) as holder, Session(engine) as other:
        holder.execute(select(PresalesInquiryLine).where(PresalesInquiryLine.id == first_id).with_for_update())
        other.execute(select(PresalesInquiry).where(PresalesInquiry.id == created["inquiry_id"]).with_for_update(nowait=True))
        other.rollback()
        other.execute(select(PresalesInquiryLine).where(PresalesInquiryLine.id == second_id).with_for_update(nowait=True))


def test_changing_return_defective_requires_confirm_then_voids_price(db, users):
    line = _inquiry(db, users.seller, users.part)
    _price(db, users.buyer, line, "100")
    db.refresh(line)
    with pytest.raises(Conflict) as caught:
        pool.edit_line(db, line.id, _ctx(db, users.other), {"return_defective": False}, expected_version=line.version)
    db.rollback()
    assert caught.value.detail["code"] == "price_reset_required"
    db.refresh(line)
    row = pool.edit_line(db, line.id, _ctx(db, users.other), {"return_defective": False},
                         expected_version=line.version, confirm_reset=True)
    db.commit()
    assert (row["answer_state"], row["return_defective"], row["price"]) == ("pending", False, None)
    voided = next(item for item in pool.history(db, line.id, _ctx(db, users.peer)) if item.get("status") == "stale")
    assert voided["reason"] == "询价条件已修改：坏件返回"
    # 只改备注不影响已有价格。
    _price(db, users.buyer, line, "120")
    db.refresh(line)
    row = pool.edit_line(db, line.id, _ctx(db, users.peer), {"notes": "急"}, expected_version=line.version)
    assert (row["answer_state"], row["notes"]) == ("quoted", "急")


@pytest.mark.parametrize("mode", ["skip", "reuse"])
def test_metadata_edits_preserve_only_the_current_published_response(db, users, mode):
    line = _inquiry(db, users.seller, users.part)
    pool.edit_line(db, line.id, users.seller.id, {"notes": "初始备注"}, expected_version=line.version)
    if mode == "reuse":
        source_line = _inquiry(db, users.seller, users.part)
        source = _price(db, users.buyer, source_line, "83")["price"]["offer_id"]
        offers.reply(db, line.inquiry_id, users.buyer.id, expected_version=1, line_responses=[
            {"line_id": line.id, "mode": "reuse", "ref_offer_id": source}])
    else:
        pool.skip(db, line.id, users.buyer.id, "型号停产", expected_version=line.version)
    version = line.version
    old = PresalesLineResponse(line_id=line.id, line_version=version - 1, created_by=users.peer.id,
        mode="skip", reason="旧版", is_draft=False)
    draft = PresalesLineResponse(line_id=line.id, line_version=version, created_by=users.seller.id,
        mode="skip", reason="未发布", is_draft=True)
    db.add_all([old, draft])
    db.commit()
    initial = pool.line_view(db, line.id, users.other.id)
    assert initial["answer_state"] == ("quoted" if mode == "reuse" else "skip")
    answer_fields = ("answer_state", "answer_reason", "answered_at", "answered_by", "price")
    for data in ({"notes": "新备注"}, {"expected_delivery": date(2027, 1, 1)}):
        updated = pool.edit_line(db, line.id, users.other.id, data, expected_version=line.version)
        assert [updated[key] for key in answer_fields] == [initial[key] for key in answer_fields]
    db.expire_all()
    assert line.version == version + 2
    assert (old.line_version, draft.line_version, draft.is_draft) == (version - 1, version, True)
    with pytest.raises(Conflict, match="作废"):
        pool.edit_line(db, line.id, users.other.id, {"qty": 2}, expected_version=line.version)
    reset = pool.edit_line(db, line.id, users.other.id, {"qty": 2}, expected_version=line.version,
                           confirm_reset=True)
    assert reset["answer_state"] == "pending" and reset["price"] is None


@pytest.mark.parametrize("action", ["reset", "skip", "reprice"])
def test_detaching_multiple_price_sources_keeps_only_associated_revisions(db, users, action):
    target = _inquiry(db, users.seller, users.part)
    ref_line = _inquiry(db, users.seller, users.part)
    reused_line = _inquiry(db, users.seller, users.part)
    private_line = _inquiry(db, users.seller, users.part)
    _price(db, users.peer, ref_line, "8")  # 此时本行尚未引用。
    ref = _price(db, users.buyer, ref_line, "11")["price"]["offer_id"]
    reused = _price(db, users.buyer, reused_line, "22")["price"]["offer_id"]
    private = _price(db, users.buyer, private_line, "999")["price"]["offer_id"]
    # 历史数据可以同时带有销售引用、已沿用答复和另一位采购的未提交选择。
    target.ref_offer_id, target.ref_offer_revision_no = ref, 2
    db.add_all([
        PresalesLineResponse(line_id=target.id, line_version=target.version, created_by=users.buyer.id,
            mode="reuse", ref_offer_id=reused, ref_offer_revision_no=1, is_draft=False),
        PresalesLineResponse(line_id=target.id, line_version=target.version, created_by=users.seller.id,
            mode="reuse", ref_offer_id=private, ref_offer_revision_no=1, is_draft=True),
    ])
    answers.refresh(db, [target.id])
    _price(db, users.peer, reused_line, "23")
    assert pool.line_view(db, target.id, users.other.id)["price"]["unit_price"] == "23.00"

    def history_prices():
        return sorted((Decimal(row["unit_price"]), row["offer_id"], row["revision_no"], row["actor"], row["status"])
                      for row in pool.history(db, target.id, users.other.id) if row["kind"] == "price")

    expected = [(Decimal("11"), ref, 2, "buyer", "published"), (Decimal("22"), reused, 1, "buyer", "published"),
                (Decimal("23"), reused, 2, "peer", "published")]
    assert history_prices() == expected
    if action == "reset":
        pool.edit_line(db, target.id, users.other.id, {"qty": 2}, expected_version=target.version, confirm_reset=True)
    elif action == "skip":
        pool.skip(db, target.id, users.other.id, "停止采购", expected_version=target.version)
    else:
        # 已提交后关单仍可更新价格，并各自保留本行的源修订。
        inquiry = db.get(PresalesInquiry, target.inquiry_id)
        inquiries.close(db, inquiry.id, users.seller.id, inquiry.version, outcome="lost")
        new = _price(db, users.other, target, "40")["price"]["offer_id"]
        expected.append((Decimal("40"), new, 1, "other", "published"))
    _price(db, users.buyer, reused_line, "300")
    _price(db, users.buyer, ref_line, "400")
    assert history_prices() == expected


@pytest.mark.parametrize("missing_pins", [False, True])
def test_moved_quote_history_follows_each_line_then_stops_at_detachment(db, users, missing_pins):
    old = _inquiry(db, users.seller, users.part)
    offer_id = _price(db, users.buyer, old, "51")["price"]["offer_id"]
    other = _inquiry(db, users.seller, users.part)
    header = db.get(PresalesInquiry, other.inquiry_id)
    offers.save_draft(db, header.id, other.id, users.buyer.id, {"unit_price": "52"},
                      expected_version=header.version)
    offers.reply(db, header.id, users.buyer.id, expected_version=header.version,
                 on_existing="update", existing_offer_id=offer_id)
    link = db.get(PresalesOfferLineLink, (old.id, offer_id))
    assert link.offer_revision_no == 1 and old.current_offer_id == offer_id
    if missing_pins:
        link.offer_revision_no = None
        saved = db.scalar(select(PresalesLineResponse).where(PresalesLineResponse.line_id == old.id))
        saved.ref_offer_revision_no = None
        last_line = _inquiry(db, users.seller, users.part)
        newest = _price(db, users.peer, last_line, "60")["price"]["offer_id"]
        old.ref_offer_id, old.ref_offer_revision_no = newest, 1
        answers.refresh(db, [old.id])
        assert old.current_offer_id == newest
    db.commit()
    pool.skip(db, old.id, users.other.id, "撤销这次修改", expected_version=old.version)
    db.rollback()
    assert db.scalar(select(SubjectEvent.id).where(SubjectEvent.kind == "line.price_unlinked")) is None
    assert db.get(PresalesOfferLineLink, (old.id, offer_id)) is not None
    pool.skip(db, old.id, users.other.id, "不再询价", expected_version=old.version)
    _price(db, users.peer, other, "53")

    def prices(line):
        return [Decimal(row["unit_price"]) for row in pool.history(db, line.id, users.other.id)
                if row["kind"] == "price"]
    assert prices(old) == ([Decimal("60"), Decimal("51")] if missing_pins else [Decimal("52"), Decimal("51")])
    assert prices(other) == [Decimal("53"), Decimal("52")]


def test_missing_pin_freezes_only_the_proven_current_revision_before_replacement(db, users):
    target = _inquiry(db, users.seller, users.part)
    unpinned = _inquiry(db, users.seller, users.part)
    current = _inquiry(db, users.seller, users.part)
    other = _price(db, users.buyer, unpinned, "19")["price"]["offer_id"]
    _price(db, users.buyer, current, "21")
    used = _price(db, users.peer, current, "22")["price"]["offer_id"]
    db.add_all([
        PresalesOfferLineLink(line_id=target.id, offer_id=other, offer_revision_no=None),
        PresalesLineResponse(line_id=target.id, line_version=target.version, created_by=users.buyer.id,
            mode="reuse", ref_offer_id=used, ref_offer_revision_no=None, is_draft=False),
    ])
    answers.refresh(db, [target.id])
    assert target.current_offer_id == used

    def prices():
        return [(r["offer_id"], r["revision_no"], Decimal(r["unit_price"]))
                for r in pool.history(db, target.id, users.other.id) if r["kind"] == "price"]
    assert prices() == [(used, 2, Decimal("22"))]
    replacement = _price(db, users.other, target, "40")["price"]["offer_id"]
    _price(db, users.buyer, current, "200")
    assert prices() == [(replacement, 1, Decimal("40")), (used, 2, Decimal("22"))]


def test_skip_then_quote_and_latest_same_pn_hint(db, users):
    line = _inquiry(db, users.seller, users.part)
    row = pool.skip(db, line.id, _ctx(db, users.buyer), "无货源", expected_version=line.version)
    db.commit()
    assert (row["answer_state"], row["answer_reason"], row["answered_by"]) == ("skip", "无货源", "buyer")
    quoted = _price(db, users.peer, line, "55")
    assert quoted["answer_state"] == "quoted" and quoted["answer_reason"] is None
    newer = _inquiry(db, users.other, users.part)
    hint = pool.line_view(db, newer.id, _ctx(db, users.buyer))["latest_same_pn"]
    assert (hint["unit_price"], hint["reporter"]) == ("55.00", "peer")


def test_customer_follows_data_permission_and_drafts_stay_private(db, users):
    line = _inquiry(db, users.seller, users.part, customer=users.customer)
    assert "customer_name" not in pool.line_view(db, line.id, _ctx(db, users.buyer))
    assert pool.line_view(db, line.id, _ctx(db, users.supervisor))["customer_name"] == "客户机密"
    draft = inquiries.create_draft(db, _ctx(db, users.seller), lines=[
        {"part_id": users.part.id, "pn_raw": users.part.pn_std, "qty": Decimal(1)}])
    db.commit()
    assert not visibility.can_view_inquiry(db, draft, _ctx(db, users.other))
    draft_line = db.scalar(select(PresalesInquiryLine).where(PresalesInquiryLine.inquiry_id == draft.id))
    with pytest.raises(NotFound):
        pool.line_view(db, draft_line.id, _ctx(db, users.seller))
    db.rollback()
    with pytest.raises(NotFound):
        pool.set_price(db, draft_line.id, _ctx(db, users.buyer), {"unit_price": Decimal(1)},
                       expected_version=draft_line.version)
    db.rollback()


def test_private_legacy_offer_is_public_and_no_page_permission_is_denied(db, users):
    rule = common.rule_id(db, {"mode": "private"}, _ctx(db, users.seller))
    inquiry = inquiries.create_draft(db, _ctx(db, users.seller), offer_audience={"mode": "private"}, lines=[
        {"part_id": users.part.id, "pn_raw": users.part.pn_std, "qty": Decimal(1)}])
    inquiries.submit(db, inquiry.id, _ctx(db, users.seller), inquiry.version, open_tasks=False)
    db.commit()
    assert rule is not None and visibility.can_view_inquiry(db, inquiry, _ctx(db, users.other))
    with pytest.raises(Forbidden):
        pool.counts(db, _ctx(db, users.denied))
    db.rollback()


def test_unbound_line_cannot_be_priced(db, users):
    created = pool.create_inquiry(db, _ctx(db, users.seller), lines=[{"pn_raw": "未建档-1", "qty": Decimal(1)}])
    db.commit()
    line = db.scalar(select(PresalesInquiryLine).where(PresalesInquiryLine.inquiry_id == created["inquiry_id"]))
    with pytest.raises(InvalidTransition):
        pool.set_price(db, line.id, _ctx(db, users.buyer), {"unit_price": Decimal(1)}, expected_version=line.version)
    db.rollback()


def test_list_query_count_is_constant_and_urgent_first(db, users):
    for index in range(3):
        _inquiry(db, users.seller, users.part, qty=index + 1)
    client = _client(users.buyer)
    with _selects() as small:
        assert client.get("/api/presales/pool/lines", params={"tab": "pending"}).status_code == 200
    for index in range(30):
        _inquiry(db, users.other, users.part, qty=index + 1)
    urgent = _inquiry(db, users.other, users.part, priority="urgent")
    with _selects() as large:
        response = client.get("/api/presales/pool/lines", params={"tab": "pending"})
    assert response.status_code == 200
    body = response.json()
    assert len(large) == len(small)
    assert body["items"][0]["line_id"] == urgent.id
    assert body["counts"]["pending"] == body["total"] == 34


def test_http_flow_create_price_edit_history_and_search(db, users):
    seller, buyer = _client(users.seller), _client(users.buyer)
    created = seller.post("/api/presales/pool/inquiries", json={"priority": "urgent", "lines": [
        {"part_id": users.part.id, "pn_raw": users.part.pn_std, "qty": "2", "return_defective": False}]})
    assert created.status_code == 201
    row = buyer.get("/api/presales/pool/lines", params={"q": "API-PRESALES"}).json()["items"][0]
    assert (row["requester"], row["return_defective"], row["priority"]) == ("seller", False, "urgent")
    priced = buyer.put(f"/api/presales/pool/lines/{row['line_id']}/price",
                       json={"expected_version": row["line_version"], "unit_price": "88.5"})
    assert priced.status_code == 200 and priced.json()["price"]["unit_price"] == "88.50"
    stale = seller.patch(f"/api/presales/pool/lines/{row['line_id']}",
                         json={"expected_version": row["line_version"], "return_defective": True})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    reset = seller.patch(f"/api/presales/pool/lines/{row['line_id']}", json={
        "expected_version": priced.json()["line_version"], "return_defective": True, "confirm_reset": True})
    assert reset.status_code == 200 and reset.json()["answer_state"] == "pending"
    kinds = {item["kind"] for item in seller.get(f"/api/presales/pool/lines/{row['line_id']}/history").json()}
    assert kinds == {"price", "line.edited"}
    assert buyer.get("/api/presales/pool/counts").json()["pending"] >= 1


def test_price_only_update_keeps_other_fields(db, users):
    seller = _client(users.seller)
    line = _inquiry(db, users.seller, users.part)
    first = seller.put(f"/api/presales/pool/lines/{line.id}/price", json={
        "expected_version": line.version, "unit_price": "10", "lead_time": "3天", "stock_location": "上海",
        "qty_available": "5", "notes": "原备注", "supplier_id": users.supplier.id, "price_basis": "cost",
        "is_tax_inclusive": False}).json()
    assert first["price"]["supplier_name"] == users.supplier.name_raw
    second = seller.put(f"/api/presales/pool/lines/{line.id}/price", json={
        "expected_version": first["line_version"], "unit_price": "11"}).json()
    keys = ("lead_time", "stock_location", "qty_available", "notes", "supplier_id", "supplier_name", "price_basis", "is_tax_inclusive")
    assert second["price"]["unit_price"] == "11.00"
    assert {key: second["price"][key] for key in keys} == {key: first["price"][key] for key in keys}
    # 显式传空才清空。
    cleared = seller.put(f"/api/presales/pool/lines/{line.id}/price", json={
        "expected_version": second["line_version"], "unit_price": "11", "lead_time": None}).json()
    assert cleared["price"]["lead_time"] is None and cleared["price"]["stock_location"] == "上海"


def test_new_price_without_tax_or_basis_uses_defaults(db, users):
    line = _inquiry(db, users.seller, users.part)
    row = _client(users.buyer).put(f"/api/presales/pool/lines/{line.id}/price", json={
        "expected_version": line.version, "unit_price": "10"}).json()
    assert (row["price"]["is_tax_inclusive"], row["price"]["price_basis"]) == (True, "sale")


def test_candidates_follow_data_permission_not_action_permission(db, users):
    seller, buyer = _client(users.seller), _client(users.buyer)
    for client in (seller, buyer):
        assert client.get("/api/presales/suppliers").status_code == 200
        assert client.post("/api/presales/reference", json={"part_ids": [users.part.id]}).status_code == 200
    assert seller.get("/api/presales/customers").status_code == 200
    # 采购默认没有客户数据权限：这是「客户名看权限」，不是动作权限。
    assert buyer.get("/api/presales/customers").status_code == 403
    users.buyer.template_perms = {**users.buyer.template_perms, "data_customer": True}
    db.commit()
    assert _client(users.buyer).get("/api/presales/customers").status_code == 200
    assert _client(users.denied).get("/api/presales/suppliers").status_code == 403


def test_default_sales_without_data_supplier_sees_supplier_in_pool(db, users):
    graph = permissions.effective("sales", None)
    assert graph["data_supplier"] is False
    plain = SysUser(username="pool_plain_sales", role="sales", password_hash="测试不使用口令", display_name="普通销售",
                    template_perms=graph, perm_overrides={})
    db.add(plain)
    db.commit()
    line = _inquiry(db, users.seller, users.part)
    _price(db, users.buyer, line, "10", supplier_id=users.supplier.id)
    client = _client(plain)
    assert client.get("/api/presales/suppliers").status_code == 200
    row = client.get("/api/presales/pool/lines", params={"tab": "quoted"}).json()["items"][0]
    assert row["price"]["supplier_id"] == users.supplier.id
    assert row["price"]["supplier_name"] == users.supplier.name_raw


def test_legacy_reply_skip_reaches_pool_answer(db, users):
    draft = inquiries.create_draft(db, _ctx(db, users.seller), lines=[
        {"part_id": users.part.id, "pn_raw": users.part.pn_std, "qty": Decimal(1)}])
    inquiries.submit(db, draft.id, _ctx(db, users.seller), draft.version)
    db.commit()
    item = work_items.subject_items(db, inquiries.subject(draft), users.buyer.id)[0]
    work_items.claim(db, item.id, users.buyer.id, item.version)
    db.commit()
    line = db.scalar(select(PresalesInquiryLine).where(PresalesInquiryLine.inquiry_id == draft.id))
    db.refresh(draft)
    offers.reply(db, draft.id, _ctx(db, users.buyer), expected_version=draft.version,
                 line_responses=[{"line_id": line.id, "mode": "skip", "reason": "无货源"}])
    db.commit()
    db.refresh(line)
    assert (line.answer_state, line.answer_reason) == ("skip", "无货源")


def test_draft_count_only_for_owner(db, users):
    inquiries.create_draft(db, _ctx(db, users.seller), lines=[{"pn_raw": "草稿行", "qty": Decimal(1)}])
    db.commit()
    assert pool.counts(db, _ctx(db, users.seller))["drafts"] == 1
    assert pool.counts(db, _ctx(db, users.other))["drafts"] == 0


def test_skip_reason_survives_in_history_after_repricing(db, users):
    line = _inquiry(db, users.seller, users.part)
    pool.skip(db, line.id, _ctx(db, users.buyer), "停产", expected_version=line.version)
    db.commit()
    _price(db, users.peer, line, "9")
    entry = next(item for item in pool.history(db, line.id, _ctx(db, users.other)) if item["kind"] == "line.skipped")
    assert (entry["reason"], entry["actor"]) == ("停产", "buyer")


def test_deleted_unsubmitted_draft_does_not_become_visible_to_other_sales(db, users):
    draft = inquiries.create_draft(db, users.seller.id, notes="未提交草稿", lines=[{"part_id": users.part.id}])
    line = db.scalar(select(PresalesInquiryLine).where(PresalesInquiryLine.inquiry_id == draft.id))
    inquiries.delete_draft(db, draft.id, users.seller.id, draft.version)
    db.commit()
    actor = _ctx(db, users.other)
    assert not visibility.can_view_inquiry(db, draft, actor)
    response = _client(users.other).get("/api/presales/inquiries?scope=all")
    assert response.status_code == 200 and response.json()["total"] == 0
    assert pool.list_lines(db, actor, tab="all")["total"] == pool.counts(db, actor)["all"] == 0
    for reader in (pool.line_view, pool.history):
        with pytest.raises(NotFound):
            reader(db, line.id, actor)
    with pytest.raises(NotFound):
        pool.set_price(db, line.id, actor, {"unit_price": Decimal(1)}, expected_version=line.version)
    with pytest.raises(NotFound):
        pool.skip(db, line.id, actor, "不报", expected_version=line.version)


def test_shared_answer_preserves_private_drafts_and_hides_unpublished_revisions(db, users):
    line = _inquiry(db, users.seller, users.part)
    author, colleague = _ctx(db, users.buyer), _ctx(db, users.peer)
    private = PresalesOffer(inquiry_id=line.inquiry_id, line_id=line.id, part_id=line.part_id,
        created_by=users.buyer.id, status="draft", unit_price=Decimal("987.65"), source_key="私稿")
    decision = PresalesLineResponse(line_id=line.id, line_version=line.version, created_by=users.buyer.id,
        mode="skip", reason="未提交原因", is_draft=True)
    db.add_all([private, decision])
    db.flush()
    # 旧流程可能留下从未发布的失效报价修订；其价格没有进入共享历史。
    historic = PresalesOffer(inquiry_id=line.inquiry_id, line_id=line.id, part_id=line.part_id,
        created_by=users.buyer.id, status="draft", unit_price=Decimal("999"), source_key="旧私稿")
    db.add(historic)
    db.flush()
    offers.mark_stale(db, historic, users.buyer.id, "旧流程")
    _price(db, users.peer, line, "12")
    pool.skip(db, line.id, colleague, "已停产", expected_version=line.version)
    db.commit()
    assert db.get(PresalesLineResponse, decision.id).is_draft
    assert (private.status, private.revision_no) == ("draft", 0)
    assert visibility.offer_view(db, historic, author) is not None
    for offer in (private, historic):
        assert visibility.offer_view(db, offer, colleague) is None
    prices = [Decimal(row["unit_price"]) for row in pool.history(db, line.id, colleague) if row["kind"] == "price"]
    assert prices and set(prices) == {Decimal("12")}
    # 同一人确认不报可替换自己的草稿决定，不违反每人每行唯一约束。
    pool.skip(db, line.id, author, "本人确认", expected_version=line.version)
    db.commit()
    assert (decision.is_draft, decision.reason) == (False, "本人确认")


@pytest.mark.parametrize("key", ["qty", "return_defective", "require_exact_pn"])
def test_condition_cannot_be_null(db, users, key):
    line = _inquiry(db, users.seller, users.part)
    response = _client(users.seller).patch(f"/api/presales/pool/lines/{line.id}", json={"expected_version": line.version, key: None})
    assert response.status_code == 422


def test_revising_inquiry_is_editable_and_exportable_by_anyone_but_draft_is_private(db, users):
    draft = inquiries.create_draft(db, _ctx(db, users.seller), lines=[
        {"part_id": users.part.id, "pn_raw": users.part.pn_std, "qty": Decimal(1)}])
    db.commit()
    with pytest.raises(Forbidden):
        inquiry_workbook._editable_inquiry(db, draft.id, _ctx(db, users.other))
    db.rollback()
    inquiries.submit(db, draft.id, _ctx(db, users.seller), draft.version, open_tasks=False)
    inquiries.withdraw(db, draft.id, _ctx(db, users.seller), draft.version)
    db.commit()
    assert inquiry_workbook._editable_inquiry(db, draft.id, _ctx(db, users.other)).id == draft.id
    assert quote_workbook.export_quote_sheet(db, [draft.id], _ctx(db, users.buyer))
    columns = {column.key for column in quote_workbook.QUOTE_SCHEMA.sheets[0].columns}
    assert not columns & {"on_existing", "existing_offer_id"}
