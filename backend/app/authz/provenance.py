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


def _shape(value):
    if (type(value) is not dict or set(value) != {
            "schema_version", "principal_id", "authorization_epoch", "catalog_version", "resources"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["principal_id"]) is not int or value["principal_id"] <= 0
            or type(value["authorization_epoch"]) is not int or value["authorization_epoch"] < 0
            or type(value["catalog_version"]) is not str or not value["catalog_version"]
            or type(value["resources"]) is not list):
        raise unavailable()
    seen = set()
    for row in value["resources"]:
        if (type(row) is not dict or set(row) != {"resource_id", "actions", "grant_version"}
                or type(row["resource_id"]) is not str or not row["resource_id"]
                or row["resource_id"] in seen or type(row["grant_version"]) is not int
                or row["grant_version"] < 1 or type(row["actions"]) is not list
                or not row["actions"] or any(type(a) is not str or not a for a in row["actions"])
                or len(set(row["actions"])) != len(row["actions"])):
            raise unavailable()
        seen.add(row["resource_id"])
    return value


def validate_provenance(db: Session, identity: VerifiedIdentity, value: dict) -> None:
    """Revalidate live identity and every actual dependency, including PDP health."""
    value = _shape(value)
    principal = load_principal(db, identity)
    if (principal is None or principal.id != value["principal_id"]
            or principal.authorization_epoch != value["authorization_epoch"]
            or catalog.CATALOG_VERSION != value["catalog_version"]):
        raise unavailable()
    # Validation itself is not another data read and must not extend the collector.
    with service.access_recorder(lambda access: None):
        for row in value["resources"]:
            for action in row["actions"]:
                access = service.resource_access(db, identity, row["resource_id"], action)
                if access.grant_version != row["grant_version"]:
                    raise unavailable()


class AccessCollector:
    """Copy immutable decisions; conflicting versions fail instead of overwriting."""
    def __init__(self, principal_id: int, authorization_epoch: int, catalog_version: str):
        self._value = {"schema_version": 1, "principal_id": principal_id,
                       "authorization_epoch": authorization_epoch,
                       "catalog_version": catalog_version, "resources": []}
        _shape(self._value)
        self._lock = RLock()
        self.record_count = 0

    def snapshot(self) -> dict:
        with self._lock:
            return deepcopy(self._value)

    def inherit(self, value: dict) -> None:
        value = _shape(value)
        with self._lock:
            if any(value[k] != self._value[k] for k in (
                    "principal_id", "authorization_epoch", "catalog_version")):
                raise unavailable()
            rows = {r["resource_id"]: deepcopy(r) for r in self._value["resources"]}
            for row in value["resources"]:
                prior = rows.get(row["resource_id"])
                if prior and prior["grant_version"] != row["grant_version"]:
                    raise unavailable()
                rows[row["resource_id"]] = {**row, "actions": sorted(set(row["actions"]) |
                    set(prior["actions"] if prior else []))}
            self._value["resources"] = [rows[k] for k in sorted(rows)]

    def record(self, access: service.ResourceAccess) -> None:
        if type(access) is not service.ResourceAccess or access.object_id is not None:
            # Object lineage needs its domain relationship validator (S3), never epoch substitution.
            raise unavailable()
        with self._lock:
            self.record_count += 1
        self.inherit({"schema_version": 1, "principal_id": access.principal.id,
                      "authorization_epoch": access.authorization_epoch,
                      "catalog_version": access.catalog_version,
                      "resources": [{"resource_id": access.resource, "actions": [access.action],
                                     "grant_version": access.grant_version}]})


class AgentAccess:
    """One execution's identity/lineage, with short independent validation sessions."""
    def __init__(self, bind, identity: VerifiedIdentity | None):
        self.bind, self.identity = bind, identity
        with Session(bind) as db:
            self.authoritative = runtime.canonical_management_required(db)
            self.collector = None
            if self.authoritative:
                principal = load_principal(db, identity)
                if principal is None:
                    raise unavailable()
                self.collector = AccessCollector(principal.id, principal.authorization_epoch,
                                                 catalog.CATALOG_VERSION)

    def check(self) -> None:
        with Session(self.bind) as db:
            required = runtime.canonical_management_required(db)
            if required and not self.authoritative:
                raise unavailable()  # An in-flight legacy run cannot survive a cutover.
            if self.authoritative:
                validate_provenance(db, self.identity, self.collector.snapshot())

    def inherit(self, value: dict) -> None:
        if self.authoritative:
            with Session(self.bind) as db:
                validate_provenance(db, self.identity, value)
            self.collector.inherit(value)

    def snapshot(self):
        return self.collector.snapshot() if self.collector else None

    def mark_used(self) -> None:
        if self.authoritative:
            runtime.mark_artifact_protocol_used(self.bind)
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
