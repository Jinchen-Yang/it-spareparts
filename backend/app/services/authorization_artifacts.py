"""Stable domain/file bindings. A sidecar is evidence, never an access capability.

Producers call these primitives in their existing domain transaction, after the
complete domain lock set. Readers materialize both authorization and bytes in a
single short read snapshot. External publication markers are written *before*
entering a producer transaction, never while holding its actor lock.
"""
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import re
from types import MappingProxyType
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz.context import ObjectRef, ReadSnapshotProof, canonical_json, plain, read_snapshot
from app.authz.errors import authz_error
from app.authz.principal import MAX_SAFE_INTEGER, VerifiedIdentity, load_principal, resume_task_identity
from app.authz import provenance as lineage, runtime, service
from app.models.authorization import SysAuthorizationArtifact, SysAuthorizationMigrationReceipt

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")


def unavailable():
    return authz_error(403, 'AUTHZ_ARTIFACT_STALE', '文件来源或完整内容授权无法确认，请按当前权限重新生成')


def conflict():
    return authz_error(409, 'AUTHZ_ARTIFACT_CONFLICT', '任务代次、输入或已发布产物发生变化，请重新读取')


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise unavailable()
    return value


def _digest(value):
    if type(value) is not str or not _SHA.fullmatch(value):
        raise unavailable()
    return value


def _name(value):
    if type(value) is not str or not _NAME.fullmatch(value):
        raise unavailable()
    return value


def _freeze(value):
    # Validate JSON first, rejecting non-finite numbers and unsupported values.
    canonical_json(value)
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class ArtifactTarget:
    kind: str
    id: str
    generation: int

    def __post_init__(self):
        _name(self.kind)
        if type(self.id) is not str or not self.id or len(self.id) > 128:
            raise unavailable()
        _integer(self.generation, 1)


@dataclass(frozen=True, slots=True)
class ArtifactSubject:
    owner_user_id: int
    enqueued_epoch: int

    def __post_init__(self):
        _integer(self.owner_user_id, 1)
        _integer(self.enqueued_epoch)


@dataclass(frozen=True, slots=True)
class IdempotentOperation:
    namespace: str
    key: str

    def __post_init__(self):
        _name(self.namespace)
        if type(self.key) is not str or not self.key or len(self.key) > 128:
            raise unavailable()


@dataclass(frozen=True, slots=True)
class ArtifactDomainState:
    """Loaded from the actual domain row; loaders must never infer old usernames."""
    target: ArtifactTarget
    ref: ObjectRef
    input_digest: str
    owner_user_id: int | None

    def __post_init__(self):
        if type(self.target) is not ArtifactTarget or type(self.ref) is not ObjectRef:
            raise unavailable()
        _digest(self.input_digest)
        if self.owner_user_id is not None:
            _integer(self.owner_user_id, 1)


@dataclass(frozen=True, slots=True)
class ArtifactFormat:
    """Server-registered complete layout, including hidden/derived columns."""
    id: str
    purpose: str
    entry_id: str
    projection: Mapping
    inspect_projection: Callable[[bytes], Mapping]
    producer_entry_id: str | None = None
    producer_carrier_kind: str | None = None
    producer_ref: Callable[[Session, ArtifactDomainState], ObjectRef] | None = None

    def __post_init__(self):
        _name(self.id)
        _name(self.purpose)
        if type(self.entry_id) is not str or not self.entry_id:
            raise unavailable()
        if not isinstance(self.projection, Mapping) or not self.projection or not callable(self.inspect_projection):
            raise unavailable()
        if self.producer_entry_id is not None:
            if (type(self.producer_entry_id) is not str or not self.producer_entry_id
                    or type(self.producer_carrier_kind) is not str or not self.producer_carrier_kind
                    or not callable(self.producer_ref)):
                raise unavailable()
        elif self.producer_carrier_kind is not None or self.producer_ref is not None:
            raise unavailable()
        object.__setattr__(self, 'projection', _freeze(self.projection))

    def verify_content(self, content: bytes) -> None:
        if type(content) is not bytes:
            raise unavailable()
        observed = self.inspect_projection(content)
        if not isinstance(observed, Mapping) or canonical_json(observed) != canonical_json(self.projection):
            raise unavailable()

    @property
    def projection_digest(self):
        return sha256(canonical_json({'format_id': self.id, 'projection': self.projection,
            'consumer_entry_id': self.entry_id, 'producer_entry_id': self.producer_entry_id,
            'producer_carrier_kind': self.producer_carrier_kind}).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactDomainAdapter:
    load_state: Callable[[Session, ArtifactTarget], ArtifactDomainState]
    # Must call the registered whole-document gate, returning DocumentAccess.
    document_access: Callable[..., Any] | None
    formats: tuple[ArtifactFormat, ...]
    private: bool = True
    protocol: str = "authz_s3"

    def __post_init__(self):
        if (not callable(self.load_state)
                or (self.formats and not callable(self.document_access))
                or type(self.formats) is not tuple
                or any(type(item) is not ArtifactFormat for item in self.formats)
                or len({item.purpose for item in self.formats}) != len(self.formats)
                or type(self.private) is not bool
                or type(self.protocol) is not str
                or self.protocol not in ("authz_s2", "authz_s3", "authz_s4")):
            raise ValueError('Invalid artifact domain adapter')


_DOMAINS: dict[str, ArtifactDomainAdapter] = {}


def register_artifact_domain(kind: str, adapter: ArtifactDomainAdapter) -> None:
    """Application initialization only; never exposed as a request operation."""
    _name(kind)
    if type(adapter) is not ArtifactDomainAdapter or kind in _DOMAINS:
        raise ValueError('Artifact domain is invalid or already registered')
    _DOMAINS[kind] = adapter


def _adapter(target):
    if type(target) is not ArtifactTarget or target.kind not in _DOMAINS:
        raise unavailable()
    return _DOMAINS[target.kind]


def _state(db, target):
    state = _adapter(target).load_state(db, target)
    if type(state) is not ArtifactDomainState or state.target != target:
        raise unavailable()
    return state


def _format(adapter, purpose):
    for item in adapter.formats:
        if item.purpose == purpose:
            return item
    raise unavailable()


@dataclass(frozen=True, slots=True)
class ArtifactBinding:
    target: ArtifactTarget
    subject: ArtifactSubject
    input_digest: str
    operation: IdempotentOperation | None
    provenance: Mapping | None
    manifests: Mapping | None

    def __post_init__(self):
        if (type(self.target) is not ArtifactTarget or type(self.subject) is not ArtifactSubject
                or (self.operation is not None and type(self.operation) is not IdempotentOperation)
                or (self.provenance is not None and not isinstance(self.provenance, Mapping))
                or (self.manifests is not None and not isinstance(self.manifests, Mapping))):
            raise unavailable()
        _digest(self.input_digest)
        if self.provenance is not None:
            object.__setattr__(self, 'provenance', _freeze(self.provenance))
        if self.manifests is not None:
            object.__setattr__(self, 'manifests', _freeze(self.manifests))


def _binding(row):
    if (row.command_namespace is None) != (row.idempotency_key is None):
        raise unavailable()
    return ArtifactBinding(ArtifactTarget(row.domain_kind, row.domain_id, row.generation),
        ArtifactSubject(row.owner_user_id, row.enqueued_epoch), row.input_digest,
        IdempotentOperation(row.command_namespace, row.idempotency_key) if row.idempotency_key is not None else None,
        row.authorization_provenance, row.artifact_manifests)


def _row(db, target, *, lock=False):
    _adapter(target)
    query = select(SysAuthorizationArtifact).where(
        SysAuthorizationArtifact.domain_kind == target.kind,
        SysAuthorizationArtifact.domain_id == target.id).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        raise unavailable()
    if row.generation != target.generation:
        raise conflict()
    return row


def load_artifact_binding(db, *, target: ArtifactTarget, lock=False) -> ArtifactBinding:
    """Internal lookup only. The return value is not a download authorization."""
    row = _row(db, target, lock=lock)
    state = _state(db, target)
    if (state.input_digest != row.input_digest
            or (state.owner_user_id is not None and state.owner_user_id != row.owner_user_id)):
        raise unavailable()
    return _binding(row)


def _producer(db, identity, row, *, locked_user=None):
    principal = load_principal(db, identity, locked_user=locked_user)
    if (principal is None or principal.id != row.owner_user_id
            or principal.authorization_epoch != row.enqueued_epoch):
        raise unavailable()
    return principal


def _provenance(value, subject):
    value = lineage.validate_provenance_shape(plain(value))
    if (value['principal_id'] != subject.owner_user_id
            or value['authorization_epoch'] != subject.enqueued_epoch):
        raise unavailable()
    return value


def register_artifact_binding(db, identity, *, target: ArtifactTarget, input_digest: str,
        operation: IdempotentOperation | None = None, provenance: Mapping | None = None,
        locked_user=None) -> ArtifactBinding:
    """Register with the business write, without commit or independent transaction.

    The caller already holds the complete domain lock set and has authorized
    this producer operation. Unknown historical rows must not call this helper
    to claim ownership; they require an explicit new generation operation.
    """
    _digest(input_digest)
    if db.get(SysAuthorizationMigrationReceipt, (_adapter(target).protocol, 'artifact_protocol', 'global')) is None:
        raise authz_error(503, 'AUTHZ_ARTIFACT_PROTOCOL_REQUIRED', '产物协议尚未激活')
    state = _state(db, target)
    locked_user = locked_user or service.lock_actor(db, identity)
    principal = load_principal(db, identity, locked_user=locked_user)
    if (principal is None or state.input_digest != input_digest
            or (state.owner_user_id is not None and state.owner_user_id != principal.id)):
        raise unavailable()
    subject = ArtifactSubject(principal.id, principal.authorization_epoch)
    value = _provenance(provenance, subject) if provenance is not None else None
    if value is not None:
        lineage.validate_provenance(db, identity, value, locked_user=locked_user)
    prior = db.get(SysAuthorizationArtifact, (target.kind, target.id), populate_existing=True)
    if prior is not None:
        binding = load_artifact_binding(db, target=target)
        if (binding.subject != subject or binding.input_digest != input_digest
                or binding.operation != operation or plain(binding.provenance) != value):
            raise conflict()
        return binding
    row = SysAuthorizationArtifact(domain_kind=target.kind, domain_id=target.id,
        generation=target.generation, owner_user_id=principal.id,
        enqueued_epoch=principal.authorization_epoch, input_digest=input_digest,
        command_namespace=operation.namespace if operation else None,
        idempotency_key=operation.key if operation else None,
        authorization_provenance=value, artifact_manifests=None)
    db.add(row)
    db.flush()
    return _binding(row)


def begin_artifact_protocol(bind, *, protocol: str = "authz_s3") -> None:
    """Before any domain/actor transaction, protect external bytes from downgrade."""
    if protocol not in ('authz_s2', 'authz_s3', 'authz_s4'):
        raise ValueError('Unknown artifact protocol')
    runtime.mark_artifact_protocol_used(bind, protocol=protocol)


def stable_operation_key(identity_id: int, namespace: str, key: str) -> str:
    """Domain idempotency hashes use the stable actor ID, never an audit name."""
    _integer(identity_id, 1)
    operation = IdempotentOperation(namespace, key)
    return sha256(canonical_json({'schema_version': 1, 'owner_user_id': identity_id,
        'namespace': operation.namespace, 'key': operation.key}).encode()).hexdigest()


def resume_artifact_task(db, *, target: ArtifactTarget) -> VerifiedIdentity:
    binding = load_artifact_binding(db, target=target)
    identity = resume_task_identity(db, binding.subject.owner_user_id, binding.subject.enqueued_epoch)
    if binding.provenance is None:
        raise unavailable()
    lineage.validate_provenance(db, identity, plain(binding.provenance))
    return identity


def restart_artifact_task(db, identity, *, binding: ArtifactBinding, expected_generation: int,
        provenance: Mapping | None, locked_user=None) -> ArtifactBinding:
    if binding.target.generation != expected_generation:
        raise conflict()
    row = _row(db, binding.target, lock=True)
    # This explicit retry can capture a new epoch, but can never change owner.
    locked_user = locked_user or service.lock_actor(db, identity)
    principal = load_principal(db, identity, locked_user=locked_user)
    if principal is None or principal.id != row.owner_user_id:
        raise unavailable()
    next_target = ArtifactTarget(binding.target.kind, binding.target.id, expected_generation + 1)
    state = _state(db, next_target)  # Caller advanced the actual task in this transaction.
    if state.input_digest != row.input_digest or (state.owner_user_id is not None and state.owner_user_id != row.owner_user_id):
        raise unavailable()
    subject = ArtifactSubject(principal.id, principal.authorization_epoch)
    value = _provenance(provenance, subject) if provenance is not None else None
    if value is not None:
        lineage.validate_provenance(db, identity, value, locked_user=locked_user)
    row.generation = next_target.generation
    row.enqueued_epoch = principal.authorization_epoch
    row.authorization_provenance = value
    row.artifact_manifests = None
    db.flush()
    return _binding(row)


def _document(db, identity, state, descriptor, *, read_snapshot=None, locked_user=None, publishing=False):
    adapter = _adapter(state.target)
    entry_id = descriptor.producer_entry_id if publishing and descriptor.producer_entry_id is not None else descriptor.entry_id
    document = adapter.document_access(db, identity, state.ref, entry_id=entry_id,
        format_purpose=descriptor.purpose, read_snapshot=read_snapshot, locked_user=locked_user)
    # This gate must yield actual successful core decisions, not a boolean.
    if (document.ref != state.ref or not document.accesses
            or any(type(access) is not service.ResourceAccess for access in document.accesses)
            or type(document.authorization_revision) is not str or not document.authorization_revision
            or type(document.content_revision) is not str or not document.content_revision
            or type(document.content_manifest) is not tuple or not document.content_manifest
            or any(type(ref) is not ObjectRef for ref in document.content_manifest)
            or state.ref not in document.content_manifest):
        raise unavailable()
    if publishing and descriptor.producer_entry_id is not None:
        expected = descriptor.producer_ref(db, state)
        if (type(expected) is not ObjectRef or expected.kind != descriptor.producer_carrier_kind
                or expected not in document.content_manifest
                or any(access.entry_id != entry_id or access.object_ref != expected for access in document.accesses)):
            raise unavailable()
    principal = load_principal(db, identity, locked_user=locked_user)
    if principal is None or any(access.principal.id != principal.id or
            access.authorization_epoch != principal.authorization_epoch or
            access.catalog_version != service.CATALOG_VERSION for access in document.accesses):
        raise unavailable()
    if adapter.private and state.owner_user_id is not None and principal.id != state.owner_user_id:
        raise unavailable()
    return document


def _manifest(descriptor, subject, document, content: bytes):
    if type(content) is not bytes:
        raise unavailable()
    refs = [ref.to_dict() for ref in document.content_manifest]
    if len({canonical_json(ref) for ref in refs}) != len(refs):
        raise unavailable()
    return {'schema_version': 1, 'format_id': descriptor.id,
        'projection_digest': descriptor.projection_digest, 'content_sha256': sha256(content).hexdigest(),
        'producer': {'user_id': subject.owner_user_id, 'authorization_epoch': subject.enqueued_epoch},
        'content_dependencies': {'entry_id': descriptor.entry_id,
            'carrier': document.ref.to_dict(), 'content_revision': document.content_revision,
            'objects': refs}, 'complete': True}


def publish_artifact(db, identity, *, binding: ArtifactBinding, purpose: str,
        content: bytes, expected_generation: int, locked_user=None) -> Mapping:
    """Publish metadata in the domain transaction; never writes bytes or commits."""
    if binding.target.generation != expected_generation:
        raise conflict()
    row = _row(db, binding.target, lock=True)
    current = load_artifact_binding(db, target=binding.target)
    if current != binding:
        raise conflict()
    locked_user = locked_user or service.lock_actor(db, identity)
    _producer(db, identity, row, locked_user=locked_user)
    if current.provenance is None:
        raise unavailable()
    lineage.validate_provenance(db, identity, plain(current.provenance), locked_user=locked_user)
    state = _state(db, binding.target)
    descriptor = _format(_adapter(binding.target), purpose)
    descriptor.verify_content(content)
    document = _document(db, identity, state, descriptor, locked_user=locked_user, publishing=True)
    manifest = _manifest(descriptor, current.subject, document, content)
    prior = dict(row.artifact_manifests or {})
    if purpose in prior and prior[purpose] != manifest:
        raise conflict()
    prior[purpose] = manifest
    row.artifact_manifests = prior
    db.flush()
    return _freeze(manifest)


def require_artifact(db, identity, *, target: ArtifactTarget, purpose: str,
        read_snapshot: ReadSnapshotProof, authorization_precondition=None) -> Mapping:
    """Current reader, whole document, same observation. Never resume producer."""
    if type(read_snapshot) is not ReadSnapshotProof:
        raise unavailable()
    read_snapshot.validate(db)
    return _require_artifact(db, identity, target=target, purpose=purpose,
        read_snapshot=read_snapshot, authorization_precondition=authorization_precondition)


def require_locked_artifact(db, identity, *, target: ArtifactTarget, purpose: str,
        locked_user, authorization_precondition=None) -> Mapping:
    """Write replay only: caller holds the complete domain set then actor lock.

    Unlike a reader this cannot open another connection: it checks the same
    transaction that has verified the actual immutable receipt.
    """
    from app.models.system import SysUser
    if type(locked_user) is not SysUser or not db.in_transaction():
        raise ValueError('事务回放需要当前完整领域锁集与最终主体')
    return _require_artifact(db, identity, target=target, purpose=purpose,
        locked_user=locked_user, authorization_precondition=authorization_precondition)


def _require_artifact(db, identity, *, target, purpose, read_snapshot=None,
        locked_user=None, authorization_precondition=None):
    binding = load_artifact_binding(db, target=target)
    if binding.provenance is None or binding.manifests is None or purpose not in binding.manifests:
        raise unavailable()
    _provenance(binding.provenance, binding.subject)  # Shape only: reader may differ.
    principal = load_principal(db, identity, locked_user=locked_user)
    if principal is None or (_adapter(target).private and principal.id != binding.subject.owner_user_id):
        raise unavailable()
    state = _state(db, target)
    descriptor = _format(_adapter(target), purpose)
    document = _document(db, identity, state, descriptor, read_snapshot=read_snapshot, locked_user=locked_user)
    service.check_resource_precondition(authorization_precondition, document.accesses[0],
        object_ref=document.ref, authorization_revision=document.authorization_revision)
    manifest = plain(binding.manifests[purpose])
    expected = _manifest(descriptor, binding.subject, document, b'')
    if (type(manifest) is not dict or set(manifest) != set(expected)
            or any(manifest[key] != value for key, value in expected.items() if key != 'content_sha256')):
        raise unavailable()
    _digest(manifest['content_sha256'])
    return _freeze(manifest)


def read_artifact_bytes(session_factory, identity, *, target: ArtifactTarget,
        purpose: str, read_bytes: Callable[[Session, ArtifactTarget, str], bytes],
        authorization_precondition=None) -> bytes:
    """Return only bytes matching the complete manifest and current snapshot."""
    with read_snapshot(session_factory) as (db, proof):
        manifest = require_artifact(db, identity, target=target, purpose=purpose, read_snapshot=proof,
            authorization_precondition=authorization_precondition)
        content = read_bytes(db, target, purpose)
        if type(content) is not bytes or sha256(content).hexdigest() != manifest['content_sha256']:
            raise unavailable()
        return content
