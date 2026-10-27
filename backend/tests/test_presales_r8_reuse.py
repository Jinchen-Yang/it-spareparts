"""旧沿用缺少版本时，追加回复必须要求采购重新明确选择。"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.presales import PresalesLineResponse
from app.services.presales import inquiries, offers
from app.services.workflow import work_items
from app.services.workflow.registry import InvalidTransition
from tests.test_presales_r6_existing import _publish, r6_actors
from tests.test_presales_r6_responses import _item, _pending, _reply, _response_view


def test_appending_another_line_requires_explicit_reselection_of_unpinned_reuse(db, r6_actors):
    users = r6_actors
    _, _, _, source = _publish(db, users)
    inquiry, lines = _pending(db, users)
    old_line = lines[0]
    reuse = {"line_id": old_line.id, "mode": "reuse", "ref_offer_id": source.id}
    _reply(db, inquiry, users.buyer, [reuse])
    response = db.scalar(select(PresalesLineResponse).where(
        PresalesLineResponse.line_id == old_line.id,
        PresalesLineResponse.created_by == users.buyer.id))
    # 模拟迁移后仍然不知道原始沿用版本的旧记录。
    response.ref_offer_revision_no = None
    db.flush()
    offers.update_offer(db, source.id, users.buyer.id, {"unit_price": "80.00"},
                        expected_version=source.version)
    assert source.revision_no == 2
    inquiries.append_lines(db, inquiry.id, users.sales.id, inquiry.version,
                           [{"part_id": users.other_part.id}])
    added_line = inquiries.lines_for(db, inquiry.id)[-1]
    skip = {"line_id": added_line.id, "mode": "skip", "reason": "新增行无货"}
    task = _item(db, inquiry, users, users.buyer)

    with pytest.raises(InvalidTransition, match="历史版本不可用，请重新选择"), db.begin_nested():
        work_items.complete(db, task.id, users.buyer.id, expected_version=task.version)
    with pytest.raises(InvalidTransition, match="每行必须"), db.begin_nested():
        _reply(db, inquiry, users.buyer, [skip])
    assert response.ref_offer_revision_no is None
    assert _response_view(db, inquiry, users.sales, old_line.id)["expired"] is True
    assert task.status == "pending"
    assert db.scalar(select(PresalesLineResponse).where(
        PresalesLineResponse.line_id == added_line.id)) is None

    # 显式重选仍须校验当前报价，不能通过重发同一 ID 绕过失效检查。
    with pytest.raises(InvalidTransition, match="沿用报价已失效"), db.begin_nested():
        offers.withdraw(db, source.id, users.buyer.id, "报价有误", expected_version=source.version)
        _reply(db, inquiry, users.buyer, [reuse, skip])
    assert response.ref_offer_revision_no is None
    _reply(db, inquiry, users.buyer, [reuse, skip])
    assert response.ref_offer_revision_no == source.revision_no == 2
    assert _response_view(db, inquiry, users.sales, old_line.id)["offer"]["unit_price"] == Decimal("80.00")
    assert task.status == "done"
