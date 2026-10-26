"""待建档询价行：查重、复用主数据和事务内建档。"""

from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.presales import PresalesInquiryLine
from app.services import field_pool, master_edit
from app.services.presales import common, inquiries
from app.services.workflow import work_items
from app.services.workflow.registry import Conflict, Forbidden, InvalidTransition, NotFound


def _access(db, line_id, actor, *, lock=False, expected_version=None):
    user, ctx = common.resolve_actor(db, actor)
    common.require(ctx, "page_presales")
    common.require(ctx, "action_presales_bind_part")
    common.require(ctx, "page_master_data")
    line = db.get(PresalesInquiryLine, line_id)
    if line is None:
        raise NotFound("询价行不存在")
    inquiry = common.lock_inquiry(db, line.inquiry_id) if lock else common.get_inquiry(db, line.inquiry_id)
    if lock:
        line = db.scalar(select(PresalesInquiryLine).where(PresalesInquiryLine.id == line_id)
                         .with_for_update().execution_options(populate_existing=True))
        common.check_version(line, expected_version)
    if line.status != "active" or inquiry.status == "closed":
        raise InvalidTransition("询价行已删除或已关单")
    allowed = common.bypass(ctx)
    if inquiry.kind == "proactive_offer":
        allowed = allowed or inquiry.created_by == user.id
        if inquiry.status != "draft":
            raise InvalidTransition("主动报价只能在草稿阶段建档")
    else:
        if inquiry.status != "submitted":
            raise InvalidTransition("请在询价提交后由处理人建档")
        items = work_items.list_by_subject(db, inquiries.subject(inquiry), user.id)
        allowed = allowed or any(item.assignee_id == user.id and item.status in work_items.ACTIVE
                                 and not item.paused for item in items)
    if not allowed:
        raise Forbidden("只有本单处理人或主管可以建档，请先领单")
    return inquiry, line, user, ctx


def check(db, line_id, actor, pn_std=None):
    _, line, _, _ = _access(db, line_id, actor)
    return {"near_duplicates": master_edit.find_near_duplicates(db, pn_std or line.pn_raw)}


def _unbound(line):
    if line.part_id is not None:
        raise Conflict("该询价行已经绑定主数据，请刷新", part_id=line.part_id, version=line.version)


def _apply(db, inquiry, line, part, user, ctx, kind):
    line.part_id = part.id
    if not line.description:
        line.description = part.description
    line.version += 1
    line.updated_at = common.now()
    # 建档只是补齐主数据身份，不属于销售修改 PN；同步提交基线避免下次备注修改误伤报价。
    snapshot = dict(inquiry.submitted_snapshot or {})
    baseline = dict(snapshot.get("lines", {}))
    if line.line_key in baseline:
        baseline[line.line_key] = {**baseline[line.line_key], "part_id": part.id}
        snapshot["lines"] = baseline
        inquiry.submitted_snapshot = snapshot
    inquiry.version += 1
    inquiry.updated_at = common.now()
    field_pool.template_for(db, inquiries.SUBJECT_TYPE, "line", ctx, anchor=f"part:{part.id}")
    db.flush()
    common.event(db, inquiry, user.id, kind, {"line_id": line.id, "part_id": part.id})
    common.audit(db, user.id, kind, inquiry.id)
    return line


def bind(db, line_id, actor, expected_version, *, part_id):
    inquiry, line, user, ctx = _access(db, line_id, actor, lock=True, expected_version=expected_version)
    _unbound(line)
    if type(part_id) is not int or part_id < 1:
        raise InvalidTransition("型号 ID 无效")
    part = db.get(DimPart, part_id)
    if part is None or part.status != "active":
        raise InvalidTransition("型号不存在或已合并")
    # 绑定也复用统一查重入口，确保两条处理路径采用同一套候选口径。
    master_edit.find_near_duplicates(db, line.pn_raw)
    return _apply(db, inquiry, line, part, user, ctx, "inquiry.part_bound")


def create_and_bind(db, line_id, actor, expected_version, *, pn_std=None, **master_fields):
    inquiry, line, user, ctx = _access(db, line_id, actor, lock=True, expected_version=expected_version)
    _unbound(line)
    if set(master_fields) - set(master_edit.EDITABLE_FIELDS):
        raise InvalidTransition("建档包含未知字段；不允许跳过查重")
    pn = pn_std or line.pn_raw
    duplicates = master_edit.find_near_duplicates(db, pn)
    if duplicates:
        raise Conflict("已有相同或近似型号，请绑定已有 PN", near_duplicates=duplicates)
    try:
        result = master_edit.create_part_in_transaction(db, pn_std=pn, operated_by=user.username,
                                                        force=False, **master_fields)
    except master_edit.MasterEditError as exc:
        raise InvalidTransition(str(exc)) from exc
    if not result.get("created"):
        raise Conflict("已有相同或近似型号，请绑定已有 PN", near_duplicates=result.get("near_duplicates", []))
    part = db.get(DimPart, result["id"])
    return _apply(db, inquiry, line, part, user, ctx, "inquiry.part_created")
