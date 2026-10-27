"""更新报价迁移归属后，原通知仍按最新报价人受众过滤。"""

import pytest
from sqlalchemy import delete

from app.models.presales import PresalesLineResponse
from app.services.presales import common, offers
from app.services.workflow import work_items
from app.services.workflow.registry import Subject
from tests.test_presales_r6_existing import _prepared, _publish, r6_actors


@pytest.mark.parametrize("whole", ["public", "private"])
def test_original_notice_rechecks_reporter_via_moved_offer_response(db, r6_actors, whole):
    users = r6_actors
    original, _, _ = _prepared(db, users)
    first = offers.publish_proactive(db, original.id, users.buyer.id,
        expected_version=original.version, notice_user_ids=[users.sales.id])[0]
    viewer = common.resolve_actor(db, users.sales.id)[1]
    before = work_items.visible_items(db, viewer, "assigned", "presales_offer_notice")
    assert before["items"][0]["created_by"] == users.buyer.id
    _, _, _, updated = _publish(db, users, whole={"mode": whole}, price="1.00",
        fields={"reporter_audience": {"mode": "private"}},
        on_existing="update", existing_offer_id=first.id)
    assert updated.id == first.id and updated.inquiry_id != original.id
    # 旧单报价关联独立于采购的行处理结果，不能依赖可被覆盖的 response。
    db.execute(delete(PresalesLineResponse).where(PresalesLineResponse.ref_offer_id == first.id))
    after = work_items.visible_items(db, viewer, "assigned", "presales_offer_notice")
    assert after["total"] == 1
    notice = after["items"][0]
    assert "created_by" not in notice and "created_by_name" not in notice
    owner = common.resolve_actor(db, users.buyer.id)[1]
    items = work_items.list_by_subject(db, Subject("presales_offer_notice", original.id), owner)
    own = work_items.serialize_items(db, items, users.buyer.id, owner)
    assert own[0]["created_by"] == users.buyer.id
