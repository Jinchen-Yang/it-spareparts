"""业务注册入口：通用模块只通过回调读取摘要和可见性。"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.security import UserContext


class WorkflowError(Exception):
    status_code = 422


class NotFound(WorkflowError):
    status_code = 404


class Forbidden(WorkflowError):
    status_code = 403


class Conflict(WorkflowError):
    status_code = 409

    def __init__(self, message: str, **current):
        super().__init__(message)
        self.detail = {"message": message, **current}


class InvalidTransition(WorkflowError):
    pass


@dataclass(frozen=True)
class Subject:
    subject_type: str
    subject_id: int


def _unfiltered(events, _ctx):
    return events


@dataclass(frozen=True)
class SubjectSpec:
    pool_permission: str
    supervisor_permission: str
    can_view_subject: Callable[[Session, int, UserContext], bool]
    summarize: Callable[[Session, Sequence[int], UserContext], dict[int, dict]]
    filter_events: Callable = _unfiltered
    pool_key: str | None = None
    validate_complete: Callable | None = None
    can_manage_subject: Callable | None = None
    access_permission: str | None = None
    badge_subject_ids: Callable | None = None
    filter_items: Callable | None = None


_specs: dict[str, SubjectSpec] = {}


def register(subject_type: str, spec: SubjectSpec) -> None:
    if not subject_type or len(subject_type) > 32:
        raise ValueError("subject_type 必须为 1–32 个字符")
    if spec.pool_key is not None and (not spec.pool_key or len(spec.pool_key) > 32):
        raise ValueError("pool_key 必须为 1–32 个字符")
    if subject_type in _specs:
        raise ValueError(f"subject_type 已注册: {subject_type}")
    # 同一个池的成员权限必须唯一，否则候选人接口无法判定成员资格。
    key = spec.pool_key or subject_type
    if any((other.pool_key or name) == key and other.pool_permission != spec.pool_permission
           for name, other in _specs.items()):
        raise ValueError("同一个 pool_key 必须使用相同的 pool_permission")
    _specs[subject_type] = spec


def unregister(subject_type: str) -> None:
    _specs.pop(subject_type, None)


def get_spec(subject_type: str) -> SubjectSpec:
    try:
        return _specs[subject_type]
    except KeyError:
        raise NotFound("未注册的 subject_type") from None


def registered() -> dict[str, SubjectSpec]:
    return dict(_specs)


def as_subject(subject) -> Subject:
    if isinstance(subject, Subject):
        result = subject
    elif isinstance(subject, tuple):
        result = Subject(*subject)
    else:
        result = Subject(subject.subject_type, subject.subject_id)
    get_spec(result.subject_type)
    if not isinstance(result.subject_id, int) or not 0 < result.subject_id < 2**63:
        raise InvalidTransition("subject_id 必须为正整数")
    return result
