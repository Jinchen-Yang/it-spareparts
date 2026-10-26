"""只追加的通用时间线，过滤发生在分页和序列化之前。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.workflow import SubjectEvent
from app.services.workflow.registry import Forbidden, InvalidTransition, as_subject, get_spec


def record(db: Session, subject, kind: str, actor: int | None, payload: dict,
           work_item_id: int | None = None) -> SubjectEvent:
    subject = as_subject(subject)
    if not kind or len(kind) > 48:
        raise InvalidTransition("事件 kind 必须为 1–48 个字符")
    event = SubjectEvent(subject_type=subject.subject_type, subject_id=subject.subject_id,
                         kind=kind, actor=actor, payload=payload, work_item_id=work_item_id)
    db.add(event)
    db.flush()
    return event


def list_events(db: Session, subject, ctx) -> list[SubjectEvent]:
    subject = as_subject(subject)
    spec = get_spec(subject.subject_type)
    if not spec.can_view_subject(db, subject.subject_id, ctx):
        raise Forbidden("无权查看该对象的时间线")
    events = db.scalars(select(SubjectEvent).where(
        SubjectEvent.subject_type == subject.subject_type,
        SubjectEvent.subject_id == subject.subject_id,
    ).order_by(SubjectEvent.created_at.desc(), SubjectEvent.id.desc())).all()
    return list(spec.filter_events(events, ctx))
