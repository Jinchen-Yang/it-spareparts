"""旧版公开不报从行答复迁入历史；未发布的原因始终不可见。"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.presales import PresalesInquiryLine, PresalesLineResponse
from app.models.workflow import SubjectEvent, WorkItem
from app.services.presales import answers, inquiries, offers
from tests.test_presales_api_visibility import _client, users  # noqa: F401
from tests.test_presales_pool import _inquiry


def _skips(client, line_id):
    result = client.get(f"/api/presales/pool/lines/{line_id}/history")
    assert result.status_code == 200, result.text
    return [(row["actor"], row["reason"], datetime.fromisoformat(row["at"])) for row in result.json()
            if row["kind"] == "line.skipped"]


@pytest.mark.parametrize("replace", ["price", "conditions", "skip"])
def test_replacing_legacy_skips_keeps_each_published_response_once(db, users, replace):
    line = _inquiry(db, users.seller, users.part)
    client = _client(users.other)
    url = f"/api/presales/pool/lines/{line.id}"
    meta = client.patch(url, json={"expected_version": 1, "notes": "原单备注"})
    assert meta.status_code == 200
    db.refresh(line)
    at = datetime(2026, 1, 5, tzinfo=timezone.utc)
    task = WorkItem(subject_type="presales_inquiry", subject_id=line.inquiry_id, pool_key="presales_inquiry",
                    assignee_id=users.buyer.id, status="done")
    db.add(task)
    db.flush()
    db.add_all([
        PresalesLineResponse(line_id=line.id, line_version=line.version - 1, created_by=users.buyer.id,
                             work_item_id=task.id, mode="skip", reason="无法供货", created_at=at),
        PresalesLineResponse(line_id=line.id, line_version=line.version, created_by=users.peer.id,
                             mode="skip", reason="另一供应商无现货", created_at=at + timedelta(minutes=1)),
        PresalesLineResponse(line_id=line.id, line_version=line.version, created_by=users.other.id,
                             mode="skip", reason="旧共享池不报", created_at=at + timedelta(minutes=2)),
        PresalesLineResponse(line_id=line.id, line_version=line.version, created_by=users.seller.id,
                             mode="skip", reason="私稿未发送", is_draft=True, created_at=at),
        # 升级前的共享池事件缺少 response_id；同一行同一作者既可能重报相同原因，也可能已有准确事件。
        SubjectEvent(subject_type="presales_inquiry", subject_id=line.inquiry_id, kind="line.skipped",
                     actor=users.buyer.id, created_at=at - timedelta(days=1),
                     payload={"line_id": line.id, "reason": "无法供货"}),
        SubjectEvent(subject_type="presales_inquiry", subject_id=line.inquiry_id, kind="line.skipped",
                     actor=users.other.id, created_at=at + timedelta(minutes=2),
                     payload={"line_id": line.id, "reason": "旧共享池不报"}),
    ])
    answers.refresh(db, [line.id])
    db.commit()
    before = [("other", "旧共享池不报", at + timedelta(minutes=2)),
              ("peer", "另一供应商无现货", at + timedelta(minutes=1)),
              ("buyer", "无法供货", at), ("buyer", "无法供货", at - timedelta(days=1))]
    prior_to_write = _skips(client, line.id)
    version = {"expected_version": line.version}
    if replace == "price":
        replaced = client.put(url + "/price", json={**version, "unit_price": 31})
    elif replace == "conditions":
        replaced = client.patch(url, json={**version, "qty": 2, "confirm_reset": True})
    else:
        replaced = client.post(url + "/skip", json={**version, "reason": "旧共享池不报"})
    assert replaced.status_code == 200, replaced.text
    live = _skips(client, line.id)
    if replace == "skip":
        assert live[0][:2] == ("other", "旧共享池不报")
        assert live[1:] == before
    else:
        assert live == before
    assert prior_to_write == before
    # 旧版本重试失败、随后成功重报，都不能重复复制历史。
    assert client.put(url + "/price", json={**version, "unit_price": 52}).status_code == 409
    assert client.put(url + "/price", json={"expected_version": replaced.json()["line_version"],
                                            "unit_price": 52}).status_code == 200
    assert _skips(client, line.id) == live
    stored = db.scalars(select(SubjectEvent).where(SubjectEvent.kind == "line.skipped")).all()
    assert len(stored) == len(live)
    assert "私稿未发送" not in [event.payload["reason"] for event in stored]
    assert db.scalars(select(PresalesLineResponse.reason).where(PresalesLineResponse.is_draft.is_(True))).all() == [
        "私稿未发送"]


def test_deleted_never_submitted_draft_does_not_publish_legacy_skip(db, users):
    draft = inquiries.create_draft(db, users.seller.id, lines=[{"part_id": users.part.id}])
    line = db.scalars(select(PresalesInquiryLine).where(PresalesInquiryLine.inquiry_id == draft.id)).one()
    db.add(PresalesLineResponse(line_id=line.id, line_version=line.version, created_by=users.buyer.id, mode="skip",
                                is_draft=False, reason="未提交单的旧数据"))
    inquiries.delete_draft(db, draft.id, users.seller.id, draft.version)
    answers.archive_published_skips(db, [line.id])
    db.commit()
    result = _client(users.other).get(f"/api/presales/pool/lines/{line.id}/history")
    assert result.status_code == 404
    assert db.scalars(select(SubjectEvent).where(SubjectEvent.kind == "line.skipped")).all() == []


def test_replacing_own_legacy_skip_with_an_unpublished_decision_keeps_only_public_history(db, users):
    line = _inquiry(db, users.seller, users.part)
    at = datetime(2026, 1, 5, tzinfo=timezone.utc)
    db.add(PresalesLineResponse(line_id=line.id, line_version=line.version, created_by=users.buyer.id,
                                mode="skip", reason="已发布的原因", created_at=at))
    db.flush()
    offers._response(db, line, users.buyer.id, None, "skip", reason="仍是采购个人的草稿", is_draft=True)
    db.commit()
    assert _skips(_client(users.other), line.id) == [("buyer", "已发布的原因", at)]
