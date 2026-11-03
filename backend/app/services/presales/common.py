"""售前业务共享的身份、锁、版本、事件和审计边界。"""

from datetime import datetime, timezone

from sqlalchemy import select

from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer
from app.models.system import SysAccessLog, SysUser
from app.security import UserContext
from app.services import audience
from app.services.workflow import events, work_items
from app.services.workflow.registry import Conflict, Forbidden, InvalidTransition, NotFound, Subject


def now():
    return datetime.now(timezone.utc)


def offer_effective(offer, moment=None):
    """D-0046：报价不设有效期。已发布、未标无货、有单价即一直有效，直到更新、撤回或标为失效。"""
    moment = moment or now()
    return (offer.status == "published" and not offer.no_stock and offer.unit_price is not None
            and offer.valid_from is not None and offer.valid_from <= moment)


def offer_source_eligible():
    """历史报价可以读取；其来源行待核对时不进入当前有效价、沿用或成交候选。"""
    return ~select(PresalesInquiryLine.id).where(
        PresalesInquiryLine.id == PresalesOffer.line_id, PresalesInquiryLine.answer_requires_review.is_(True)
    ).correlate(PresalesOffer).exists()


def resolve_actor(db, actor):
    return work_items.actor_context(db, actor)


def require(ctx, key):
    if not work_items.has_permission(ctx, key):
        raise Forbidden("无此操作权限")


def was_submitted(inquiry):
    """删除草稿也会关单，只有实际提交过的单据才进入共享范围。"""
    return inquiry.status != "draft" and inquiry.submitted_at is not None


def bypass(ctx):
    return ctx.role in ("admin", "boss")


def get_inquiry(db, inquiry_id):
    row = db.get(PresalesInquiry, inquiry_id)
    if row is None:
        raise NotFound("询价单不存在")
    return row


def check_version(row, expected_version):
    if type(expected_version) is not int or expected_version < 1:
        raise InvalidTransition("必须提供 expected_version")
    if row.version != expected_version:
        raise Conflict("记录已更新，请刷新后重试", version=row.version)


def lock_inquiry(db, inquiry_id, expected_version=None):
    row = db.scalar(select(PresalesInquiry).where(PresalesInquiry.id == inquiry_id)
                    .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise NotFound("询价单不存在")
    if expected_version is not None:
        check_version(row, expected_version)
    return row


def author_name(db, user_id):
    user = db.get(SysUser, user_id) if user_id is not None else None
    return user.username if user else ""


def rule_id(db, value, ctx, *, default=None):
    """业务写入只接受规则内容，避免借用他人的私有分组或规则。"""
    if value is None:
        value = {"mode": default} if default else None
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"mode", "user_ids", "group_ids"}:
        raise InvalidTransition("可见性规则格式无效")
    return audience.create_rule(db, value.get("mode", "public"), value.get("user_ids", []),
                                value.get("group_ids", []), ctx)


def visibility_rule_id(db, visible_to, ctx):
    if not isinstance(visible_to, list):
        raise InvalidTransition("可见人员必须是账号列表")
    return audience.create_rule(db, "include" if visible_to else "private", visible_to, [], ctx)


def event(db, inquiry, actor, kind, payload=None):
    from app.services.presales.registration import register_subjects
    register_subjects()
    user, _ = resolve_actor(db, actor)
    # 时间线只存非敏感标识，字段值由各业务读取接口按实时规则输出。
    return events.record(db, Subject("presales_inquiry", inquiry.id), kind, user.id, payload or {})


def audit(db, actor, action, entity_id):
    user, ctx = resolve_actor(db, actor)
    db.add(SysAccessLog(username=user.username, role=ctx.role, action=action,
                        resource="presales", detail={"id": entity_id}))
    db.flush()
