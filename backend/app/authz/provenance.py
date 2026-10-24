"""Server-owned lineage for Agent messages and files; never a client capability."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import RLock

from sqlalchemy.orm import Session

from app.authz import catalog, runtime, service
from app.authz.errors import authz_error
from app.authz.principal import VerifiedIdentity, load_principal


def unavailable():
    return authz_error(403, "AUTHZ_ARTIFACT_STALE", "该历史内容的授权来源已失效或无法确认，请按当前权限重新生成")


def _dependency_key(row):
    from app.authz.context import canonical_json
    return (row['resource_id'], canonical_json(row.get('object_ref')), row.get('entry_id') or '', row.get('branch') or '')


def _shape(value, *, allow_transient=False):
    from app.authz.context import ObjectRef
    from app.authz.principal import MAX_SAFE_INTEGER
    if (type(value) is not dict or set(value) != {
            "schema_version", "principal_id", "authorization_epoch", "catalog_version", "resources"}
            or type(value["schema_version"]) is not int or value["schema_version"] not in (1, 2)
            or type(value["principal_id"]) is not int or not 0 < value["principal_id"] <= MAX_SAFE_INTEGER
            or type(value["authorization_epoch"]) is not int or not 0 <= value["authorization_epoch"] <= MAX_SAFE_INTEGER
            or type(value["catalog_version"]) is not str or not value["catalog_version"]
            or type(value["resources"]) is not list):
        raise unavailable()
    seen = set()
    for row in value["resources"]:
        expected = {"resource_id", "actions", "grant_version"}
        if value['schema_version'] == 2:
            expected |= {'object_ref', 'authorization_revision', 'entry_id', 'branch'}
        if (type(row) is not dict or set(row) != expected
                or type(row["resource_id"]) is not str or not row["resource_id"]
                or type(row["grant_version"]) is not int or not 0 < row["grant_version"] <= MAX_SAFE_INTEGER
                or type(row["actions"]) is not list or not row["actions"]
                or any(type(a) is not str or not a for a in row["actions"])
                or len(set(row["actions"])) != len(row["actions"])):
            raise unavailable()
        try:
            definition = catalog.get_resource_definition(row['resource_id'])
            if not set(row['actions']) <= definition.actions:
                raise ValueError('Unknown dependency action')
            ref = ObjectRef.from_dict(row['object_ref']) if row.get('object_ref') is not None else None
        except (ValueError, TypeError):
            raise unavailable() from None
        if ref is not None:
            if (not definition.context_scopes or any(type(row[k]) is not str or not row[k]
                    for k in ('authorization_revision', 'entry_id', 'branch'))
                    or (ref.kind == 'operation_input' and not allow_transient)):
                raise unavailable()
        elif definition.context_scopes or any(row.get(k) is not None for k in ('authorization_revision', 'entry_id', 'branch')):
            # A contextual dependency cannot be downgraded to global schema 1.
            raise unavailable()
        key = _dependency_key(row)
        if key in seen:
            raise unavailable()
        seen.add(key)
    return value


def validate_provenance_shape(value: dict) -> dict:
    """Strict persisted format; transient input hashes cannot become old authority."""
    return deepcopy(_shape(value))


def validate_provenance(db: Session, identity: VerifiedIdentity, value: dict, *,
                        read_snapshot=None, locked_user=None) -> None:
    """Re-resolve actual dependencies and current policy in the caller's observation."""
    from app.authz.context import ObjectRef, read_snapshot as open_snapshot
    value = _shape(value)
    if any(row.get('object_ref') is not None for row in value['resources']) and read_snapshot is None and locked_user is None:
        # Never retain a read lock across LLM or file streaming. Callers that
        # materialize facts themselves pass their same short snapshot proof.
        with open_snapshot(lambda: Session(db.get_bind())) as (snapshot_db, proof):
            return validate_provenance(snapshot_db, identity, value, read_snapshot=proof)
    principal = service._read_principal(db, identity, read_snapshot=read_snapshot, locked_user=locked_user)
    if (principal is None or principal.id != value["principal_id"]
            or principal.authorization_epoch != value["authorization_epoch"]
            or catalog.CATALOG_VERSION != value["catalog_version"]):
        raise unavailable()
    with service.access_recorder(lambda access: None):
        for row in value["resources"]:
            ref = ObjectRef.from_dict(row['object_ref']) if row.get('object_ref') is not None else None
            if ref is not None:
                from app.authz.domain_contexts import revalidate_dependency
                current = revalidate_dependency(db, identity, ref, entry_id=row['entry_id'], branch=row['branch'],
                    actions_by_resource={row['resource_id']: tuple(row['actions'])},
                    read_snapshot=read_snapshot, locked_user=locked_user)
            for action in row['actions']:
                access = (current.require(ref, row['resource_id'], action) if ref is not None else
                    service.resource_access(db, identity, row['resource_id'], action,
                                            read_snapshot=read_snapshot, locked_user=locked_user))
                if (access.grant_version != row['grant_version'] or (ref is not None and
                        access.authorization_revision != row['authorization_revision'])):
                    raise unavailable()


def validate_agent_provenance(db, identity, value, *, read_snapshot=None, locked_user=None, _seen=None):
    """Agent file references also bind actual immutable filesystem bytes."""
    if read_snapshot is None and locked_user is None:
        from app.authz.context import read_snapshot as open_snapshot
        with open_snapshot(lambda: Session(db.get_bind())) as (snapshot_db, proof):
            return validate_agent_provenance(snapshot_db, identity, value, read_snapshot=proof, _seen=_seen)
    validate_provenance(db, identity, value, read_snapshot=read_snapshot, locked_user=locked_user)
    seen = set() if _seen is None else _seen
    for dependency in value['resources']:
        ref = dependency.get('object_ref')
        if ref is None or ref['kind'] != 'agent_file':
            continue
        fid = ref['id']
        if fid in seen:
            raise unavailable()
        seen.add(fid)
        try:
            from app.services import agent_file_authorization as files
            from app.services import authorization_artifacts as artifacts
            from app.authz.context import plain
            from hashlib import sha256
            header, _ = files.metadata(db, fid)
            binding = artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget(files.KIND, fid, 1))
            # This dependency's full current preview/download has already passed.
            meta = files._metadata(fid, header)
            path = files.paths(fid, header['ext'])
            if path.is_symlink() or sha256(path.read_bytes()).hexdigest() != header['bytes_sha256']:
                raise unavailable()
            source = plain(binding.provenance)
            if source != meta['authorization_provenance']:
                raise unavailable()
            validate_agent_provenance(db, identity, source, read_snapshot=read_snapshot,
                                      locked_user=locked_user, _seen=seen)
        except OSError:
            raise unavailable() from None
        finally:
            seen.remove(fid)


def _as_schema2(value):
    value = deepcopy(value)
    if value['schema_version'] == 1:
        value['schema_version'] = 2
        for row in value['resources']:
            row.update(object_ref=None, authorization_revision=None, entry_id=None, branch=None)
    return value


class AccessCollector:
    """Keep actual carrier + entry; never collapse different objects or parents."""
    def __init__(self, principal_id: int, authorization_epoch: int, catalog_version: str):
        self._value = {"schema_version": 1, "principal_id": principal_id,
                       "authorization_epoch": authorization_epoch,
                       "catalog_version": catalog_version, "resources": []}
        _shape(self._value)
        self._lock = RLock()
        self.record_count = 0

    def snapshot(self) -> dict:
        with self._lock:
            return deepcopy(_shape(self._value))

    def inherit(self, value: dict) -> None:
        self._inherit(value, allow_transient=False)

    def _inherit(self, value: dict, *, allow_transient):
        value = _shape(value, allow_transient=allow_transient)
        with self._lock:
            if any(value[k] != self._value[k] for k in (
                    "principal_id", "authorization_epoch", "catalog_version")):
                raise unavailable()
            if value['schema_version'] == 2 or self._value['schema_version'] == 2:
                self._value, value = _as_schema2(self._value), _as_schema2(value)
            rows = {_dependency_key(r): deepcopy(r) for r in self._value['resources']}
            for row in value['resources']:
                key = _dependency_key(row)
                prior = rows.get(key)
                if prior and (prior['grant_version'] != row['grant_version'] or
                              prior.get('authorization_revision') != row.get('authorization_revision')):
                    raise unavailable()
                rows[key] = {**row, 'actions': sorted(set(row['actions']) | set(prior['actions'] if prior else []))}
            self._value['resources'] = [rows[k] for k in sorted(rows)]

    def record(self, access: service.ResourceAccess) -> None:
        if type(access) is not service.ResourceAccess or (access.object_id is not None and access.object_ref is None):
            raise unavailable()
        ref = access.object_ref
        row = {'resource_id': access.resource, 'actions': [access.action], 'grant_version': access.grant_version}
        if ref is not None:
            row.update(object_ref=ref.to_dict(), authorization_revision=access.authorization_revision,
                       entry_id=access.entry_id, branch=access.context_branch)
        self._inherit({'schema_version': 2 if ref is not None else 1, 'principal_id': access.principal.id,
                       'authorization_epoch': access.authorization_epoch, 'catalog_version': access.catalog_version,
                       'resources': [row]}, allow_transient=True)
        with self._lock:
            self.record_count += 1

    def bind_transient(self, source_ref, current: service.ContextAccessSet) -> None:
        """Replace actual input obligations only after a persisted carrier is checked."""
        from app.authz.context import ObjectRef
        if type(source_ref) is not ObjectRef or source_ref.kind != 'operation_input' or type(current) is not service.ContextAccessSet:
            raise unavailable()
        with self._lock:
            rows = [r for r in self._value['resources'] if r.get('object_ref') == source_ref.to_dict()]
            if not rows or any(c.ref.kind == 'operation_input' for c in current.batch.contexts):
                raise unavailable()
            replacements = []
            for row in rows:
                for action in row['actions']:
                    matches = [a for a in current.accesses.values() if a.resource == row['resource_id'] and a.action == action]
                    if not matches:
                        raise unavailable()
                    replacements.extend(matches)
            old = deepcopy(self._value)
            try:
                self._value['resources'] = [r for r in self._value['resources'] if r not in rows]
                for access in replacements:
                    self.record(access)
            except Exception:
                self._value = old
                raise


class AgentAccess:
    """One execution's identity/lineage, with short independent validation sessions."""
    def __init__(self, bind, identity: VerifiedIdentity | None):
        self.bind, self.identity = bind, identity
        self.execution_contracts = ()
        self.execution_wire = None
        with Session(bind) as db:
            self.authoritative = runtime.canonical_management_required(db)
            self.collector = None
            if self.authoritative:
                principal = load_principal(db, identity)
                if principal is None:
                    raise unavailable()
                self.collector = AccessCollector(principal.id, principal.authorization_epoch,
                                                 catalog.CATALOG_VERSION)

    def check_sources(self) -> None:
        with Session(self.bind) as db:
            required = runtime.canonical_management_required(db)
            if required and not self.authoritative:
                raise unavailable()  # An in-flight legacy run cannot survive a cutover.
            if self.authoritative:
                validate_agent_provenance(db, self.identity, self.collector.snapshot())

    def bind_execution(self, contract, *, precondition=None):
        from app.services.agent_authorization import ExecutionContract
        if type(contract) is not ExecutionContract:
            raise unavailable()
        if not self.authoritative:
            return
        # Execution checks never become historical data obligations.
        with service.access_recorder(lambda _: None):
            accesses, ref = contract.authorize(self.bind, self.identity, precondition=precondition)
        self.execution_contracts = (contract,)
        self.execution_wire = accesses.wire(ref)

    def check(self) -> None:
        self.check_sources()
        if self.authoritative:
            if not self.execution_contracts:
                raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '缺少本次 Agent 执行合同')
            with service.access_recorder(lambda _: None):
                for contract in self.execution_contracts:
                    contract.authorize(self.bind, self.identity)

    def check_locked(self, db, actor):
        if not self.authoritative:
            return
        if not self.execution_contracts:
            raise unavailable()
        with service.access_recorder(lambda _: None):
            for contract in self.execution_contracts:
                contract.authorize_in(db, self.identity, locked_user=actor)
        validate_agent_provenance(db, self.identity, self.snapshot(), locked_user=actor)

    @contextmanager
    def command(self, contract):
        """A named nested command; keep the parent run contract and all data reads."""
        from app.services.agent_authorization import ExecutionContract
        if type(contract) is not ExecutionContract:
            raise unavailable()
        prior = self.execution_contracts
        self.execution_contracts = (*prior, contract)
        try:
            self.check()
            yield
            self.check()
        finally:
            self.execution_contracts = prior

    def inherit(self, value: dict) -> None:
        if self.authoritative:
            with Session(self.bind) as db:
                validate_agent_provenance(db, self.identity, value)
            self.collector.inherit(value)

    def snapshot(self):
        return self.collector.snapshot() if self.collector else None

    def mark_used(self) -> None:
        if self.authoritative:
            runtime.mark_artifact_protocol_used(self.bind, protocol='authz_s4')
            self.check()


_CURRENT: ContextVar[AgentAccess | None] = ContextVar("agent_access", default=None)


@contextmanager
def execution_access(access: AgentAccess):
    """Install inside the actual worker thread, not in its parent's context."""
    token = _CURRENT.set(access)
    try:
        with service.access_recorder(access.collector.record if access.collector else lambda _: None):
            access.check()
            yield access
    finally:
        _CURRENT.reset(token)


def current_access() -> AgentAccess | None:
    return _CURRENT.get()


def file_access() -> AgentAccess | None:
    """Direct file primitives cannot bypass the HTTP/tool wrappers in new authority."""
    access = current_access()
    if access is not None:
        access.check()
        return access if access.authoritative else None
    from app.db import engine
    with Session(engine) as db:
        if runtime.canonical_management_required(db):
            raise unavailable()
    return None
