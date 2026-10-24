"""Immutable domain evidence and transaction-bound read observations.

These are server values, never request DTOs or reusable capability tokens.
Domain adapters resolve actual carriers/subjects; the shared service evaluates
their live grants. A revision is an optimistic precondition, not permission.
"""
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
from types import MappingProxyType
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.authz.errors import authz_error


def _freeze(value):
    if isinstance(value, Mapping):
        if any(type(k) is not str for k in value):
            raise ValueError('上下文键必须是字符串')
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError('上下文仅接受明确的 JSON 标量')


def plain(value):
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [plain(v) for v in value]
    return value


def canonical_json(value) -> str:
    return json.dumps(plain(_freeze(value)), sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True, slots=True)
class ObjectRef:
    kind: str
    id: str
    selector: Mapping = field(default_factory=dict, hash=False)

    def __post_init__(self):
        from app.authz.context_registry import CLOSED_KINDS
        if self.kind not in CLOSED_KINDS or type(self.id) is not str or not self.id or len(self.id) > 256:
            raise ValueError('未登记或无效的业务上下文')
        if not isinstance(self.selector, Mapping):
            raise ValueError('上下文选择器必须是对象')
        object.__setattr__(self, 'selector', _freeze(self.selector))

    def to_dict(self) -> dict:
        return {'kind': self.kind, 'id': self.id, 'selector': plain(self.selector)}

    @property
    def key(self) -> str:
        return canonical_json(self.to_dict())

    def __hash__(self):
        return hash(self.key)

    def wire(self) -> dict:
        if self.kind in ('project', 'collection'):
            return {'kind': self.kind, 'id': self.id}
        return {'kind': 'object', 'object_type': self.kind, 'id': self.id}

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {'kind', 'id', 'selector'}:
            raise ValueError('业务上下文格式无效')
        return cls(**value)


@dataclass(frozen=True, slots=True)
class PolicySubject:
    id: str
    attributes: Mapping

    def __post_init__(self):
        if type(self.id) is not str or not self.id or not isinstance(self.attributes, Mapping):
            raise ValueError('实际授权对象无效')
        if any(type(k) is not str or type(v) is not bool for k, v in self.attributes.items()):
            raise ValueError('对象关系必须是服务端布尔事实')
        if 'module_probe' in self.attributes:
            raise ValueError('对象关系不得覆盖模块探测标志')
        object.__setattr__(self, 'attributes', MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True)
class ResolvedContext:
    ref: ObjectRef
    authorization_revision: str
    policy_subjects: Mapping[str, tuple[PolicySubject, ...]]
    requirements: Mapping[str, tuple[str, ...]]
    entry_id: str
    branch: str = 'default'
    empty_target_set: bool = False
    slots: Mapping[str, tuple[ObjectRef, ...]] = field(default_factory=dict)

    def __post_init__(self):
        if (type(self.ref) is not ObjectRef or type(self.authorization_revision) is not str
                or not self.authorization_revision or type(self.entry_id) is not str or not self.entry_id
                or type(self.branch) is not str or not self.branch or type(self.empty_target_set) is not bool):
            raise ValueError('领域上下文不完整')
        requirements = {r: tuple(actions) for r, actions in self.requirements.items()}
        subjects = {r: tuple(values) for r, values in self.policy_subjects.items()}
        if not requirements or set(requirements) != set(subjects):
            raise ValueError('对象与动作必须逐资源配对')
        for resource, actions in requirements.items():
            if (type(resource) is not str or not actions or any(type(a) is not str for a in actions)
                    or len(actions) != len(set(actions))
                    or any(type(v) is not PolicySubject for v in subjects[resource])
                    or len({v.id for v in subjects[resource]}) != len(subjects[resource])):
                raise ValueError('领域上下文含无效或重复动作/对象')
            if not subjects[resource] and not self.empty_target_set and self.ref.kind != 'collection':
                raise ValueError('实际对象不能为空')
        if self.empty_target_set and self.ref.kind != 'operation_input':
            raise ValueError('空目标仅限真实命令输入')
        object.__setattr__(self, 'requirements', MappingProxyType(requirements))
        object.__setattr__(self, 'policy_subjects', MappingProxyType(subjects))
        slots = {key: tuple(refs) for key, refs in self.slots.items()}
        if any(type(key) is not str or not refs or any(type(ref) is not ObjectRef for ref in refs)
               for key, refs in slots.items()):
            raise ValueError('领域证据槽必须包含真实上下文')
        object.__setattr__(self, 'slots', MappingProxyType(slots))


def observation_id(db: Session) -> str:
    transaction = db.get_transaction()
    if transaction is None or not transaction.is_active:
        raise ValueError('上下文需要活动事务')
    old = db.info.get('_authorization_observation')
    if old is None or old[0] is not transaction:
        old = (transaction, uuid4().hex)
        db.info['_authorization_observation'] = old
    return old[1]


@dataclass(frozen=True, slots=True)
class ContextBatch:
    contexts: tuple[ResolvedContext, ...]
    observation_id: str

    def __post_init__(self):
        object.__setattr__(self, 'contexts', tuple(self.contexts))
        if (not self.contexts or any(type(c) is not ResolvedContext for c in self.contexts)
                or len({c.ref.key for c in self.contexts}) != len(self.contexts)
                or type(self.observation_id) is not str or not self.observation_id):
            raise ValueError('领域批次必须包含独立、完整的真实上下文')

    def validate_observation(self, db):
        if self.observation_id != observation_id(db):
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '业务上下文观察已失效，请重新加载')


def create_context_batch(db: Session, contexts) -> ContextBatch:
    return ContextBatch(tuple(contexts), observation_id(db))


_PROOF_SEAL = object()


class ReadSnapshotProof:
    __slots__ = ('_db', '_transaction', '_observation', '_seal')

    def __init__(self, db, transaction, seal):
        if seal is not _PROOF_SEAL:
            raise ValueError('一致读证明只能由只读会话创建')
        self._db, self._transaction, self._seal = db, transaction, seal
        self._observation = observation_id(db)

    def validate(self, db):
        if (self._seal is not _PROOF_SEAL or db is not self._db
                or db.get_transaction() is not self._transaction or not self._transaction.is_active
                or observation_id(db) != self._observation):
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '一致读事务已结束或不匹配')


@contextmanager
def read_snapshot(session_factory):
    """Short independent PostgreSQL snapshot; materialize DTOs inside the yield."""
    with session_factory() as db:
        if db.in_transaction():
            raise ValueError('一致读必须使用独立新会话')
        with db.begin() as transaction:
            db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            yield db, ReadSnapshotProof(db, transaction, _PROOF_SEAL)


def relation_revision(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
