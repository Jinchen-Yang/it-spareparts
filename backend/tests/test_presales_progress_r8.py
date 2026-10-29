"""旧行响应不能计入修改后的询价进度。"""

from sqlalchemy import select

from app.models.presales import PresalesLineResponse
from app.services.presales import common, inquiries, visibility
from tests.test_presales_r6_existing import r6_actors
from tests.test_presales_r6_responses import _pending, _reply


def test_skipped_line_must_be_handled_again_after_withdraw_and_edit(db, r6_actors):
    users = r6_actors
    inquiry, lines = _pending(db, users)
    line = lines[0]
    _reply(db, inquiry, users.buyer,
           [{"line_id": line.id, "mode": "skip", "reason": "原数量暂无货"}])
    contexts = [common.resolve_actor(db, viewer.id)[1]
                for viewer in (users.sales, users.buyer, users.boss)]
    for ctx in contexts:
        assert visibility.inquiry_view(db, inquiry, ctx)["progress"] == {
            "quoted_lines": 1, "line_count": 1, "effective_quoted_lines": 0}

    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
        lines=[{"line_key": line.line_key, "qty": 2}])
    response = db.scalar(select(PresalesLineResponse).where(PresalesLineResponse.line_id == line.id))
    assert response.line_version != line.version
    for ctx in contexts:
        for detail in (True, False):
            assert visibility.inquiry_view(db, inquiry, ctx, detail=detail)["progress"] == {
                "quoted_lines": 0, "line_count": 1, "effective_quoted_lines": 0}

    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    for ctx in contexts:
        assert visibility.inquiry_view(db, inquiry, ctx)["progress"] == {
            "quoted_lines": 0, "line_count": 1, "effective_quoted_lines": 0}
