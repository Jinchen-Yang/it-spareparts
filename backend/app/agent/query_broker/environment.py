"""Independent Agent database configuration and fail-closed posture probe."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.agent.query_broker.errors import QueryBrokerError
from app.agent.query_broker.registry import DATASETS

EXPECTED_READER = "agent_reader"
EXPECTED_GUARD_OWNER = "agent_guard_owner"
EXPECTED_VIEW_OWNER = "agent_view_owner"


@dataclass(frozen=True, slots=True)
class AgentDatabaseSettings:
    enabled: bool = False
    agent_database_url: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class RolePosture:
    name: str
    can_login: bool
    inherit: bool
    superuser: bool
    create_db: bool
    create_role: bool
    replication: bool
    bypass_rls: bool


@dataclass(frozen=True, slots=True)
class ViewPosture:
    name: str
    owner: str
    security_barrier: bool
    reader_select: bool


@dataclass(frozen=True, slots=True)
class ProbeSnapshot:
    current_user: str
    session_user: str
    reader: RolePosture
    guard_owner: RolePosture
    view_owner: RolePosture
    protected_role_membership_edges: int
    reader_can_temp: bool
    reader_can_create_database: bool
    reader_can_create_public: bool
    reader_has_agent_schema_usage: bool
    reader_can_create_agent_schema: bool
    guard_owner_name: str
    guard_rls_enabled: bool
    guard_rls_forced: bool
    guard_policy_count: int
    reader_can_select_guard: bool
    views: tuple[ViewPosture, ...]
    forbidden_relation_privileges: int
    forbidden_sequence_privileges: int
    catalog_contract_verified: bool


def validate_dsn_separation(
    settings: AgentDatabaseSettings,
    main_database_url: str,
) -> None:
    """Validate only identity/topology; passwords are never formatted or logged."""

    if not settings.enabled:
        raise QueryBrokerError("QUERY_BROKER_DISABLED")
    if not settings.agent_database_url.strip():
        raise QueryBrokerError("AGENT_DSN_MISSING")
    try:
        agent = make_url(settings.agent_database_url)
        main = make_url(main_database_url)
    except (ValueError, TypeError):
        raise QueryBrokerError("AGENT_DSN_INVALID") from None
    if agent.drivername != "postgresql+psycopg":
        raise QueryBrokerError("AGENT_DSN_INVALID")
    if agent.username == main.username:
        raise QueryBrokerError("AGENT_DSN_REUSES_APP_IDENTITY")
    if agent.username != EXPECTED_READER:
        raise QueryBrokerError("AGENT_READER_IDENTITY_INVALID")
    if (
        not agent.host
        or not agent.database
        or agent.query
        or agent.normalized_query
    ):
        raise QueryBrokerError("AGENT_DSN_INVALID")


def create_agent_engine(settings: AgentDatabaseSettings, main_database_url: str) -> Engine:
    validate_dsn_separation(settings, main_database_url)
    try:
        return create_engine(
            settings.agent_database_url,
            pool_pre_ping=True,
            future=True,
            pool_size=2,
            max_overflow=0,
            pool_timeout=5,
            pool_recycle=300,
            echo=False,
            connect_args={
                "connect_timeout": 5,
                "application_name": "it_data_query_broker",
            },
        )
    except Exception:  # noqa: BLE001 - constructor details may contain the DSN
        raise QueryBrokerError("QUERY_BROKER_UNAVAILABLE") from None


def evaluate_probe(snapshot: ProbeSnapshot) -> None:
    """Evaluate a value-only posture snapshot without exposing probe SQL details."""

    safe_non_login_owner = lambda role, expected: (
        role.name == expected
        and not role.can_login
        and not role.inherit
        and not role.superuser
        and not role.create_db
        and not role.create_role
        and not role.replication
        and not role.bypass_rls
    )
    reader_safe = (
        snapshot.current_user == EXPECTED_READER
        and snapshot.session_user == EXPECTED_READER
        and snapshot.reader.name == EXPECTED_READER
        and snapshot.reader.can_login
        and not snapshot.reader.inherit
        and not snapshot.reader.superuser
        and not snapshot.reader.create_db
        and not snapshot.reader.create_role
        and not snapshot.reader.replication
        and not snapshot.reader.bypass_rls
        and snapshot.protected_role_membership_edges == 0
        and not snapshot.reader_can_temp
        and not snapshot.reader_can_create_database
        and not snapshot.reader_can_create_public
        and snapshot.reader_has_agent_schema_usage
        and not snapshot.reader_can_create_agent_schema
    )
    guard_safe = (
        safe_non_login_owner(snapshot.guard_owner, EXPECTED_GUARD_OWNER)
        and snapshot.guard_owner_name == EXPECTED_GUARD_OWNER
        and snapshot.guard_rls_enabled
        and snapshot.guard_rls_forced
        and snapshot.guard_policy_count >= 1
        and not snapshot.reader_can_select_guard
    )
    views = {view.name: view for view in snapshot.views}
    expected_views = set(DATASETS)
    views_safe = set(views) == expected_views and all(
        views[name].owner == EXPECTED_VIEW_OWNER
        and views[name].security_barrier
        and views[name].reader_select
        for name in expected_views
    )
    owner_safe = safe_non_login_owner(snapshot.view_owner, EXPECTED_VIEW_OWNER)
    if not (
        reader_safe
        and guard_safe
        and views_safe
        and owner_safe
        and snapshot.forbidden_relation_privileges == 0
        and snapshot.forbidden_sequence_privileges == 0
        and snapshot.catalog_contract_verified
    ):
        raise QueryBrokerError("QUERY_BROKER_UNAVAILABLE")


def _role(row: dict[str, Any]) -> RolePosture:
    return RolePosture(
        name=row["rolname"],
        can_login=row["rolcanlogin"],
        inherit=row["rolinherit"],
        superuser=row["rolsuper"],
        create_db=row["rolcreatedb"],
        create_role=row["rolcreaterole"],
        replication=row["rolreplication"],
        bypass_rls=row["rolbypassrls"],
    )


class AgentDatabaseProbe:
    """Short-lived live probe; every execution rechecks the DB security posture."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def ensure_ready(self) -> None:
        tx = None
        try:
            with self._engine.connect() as connection:
                tx = connection.begin()
                connection.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                connection.exec_driver_sql("SET LOCAL statement_timeout = '2000ms'")
                connection.exec_driver_sql("SET LOCAL lock_timeout = '200ms'")
                connection.exec_driver_sql(
                    "SET LOCAL idle_in_transaction_session_timeout = '3000ms'"
                )
                identity = connection.execute(text(
                    "SELECT current_user AS current_user, session_user AS session_user"
                )).mappings().one()
                role_rows = connection.execute(text(
                    "SELECT rolname, rolcanlogin, rolinherit, rolsuper, rolcreatedb, "
                    "rolcreaterole, rolreplication, rolbypassrls FROM pg_catalog.pg_roles "
                    "WHERE rolname IN ('agent_reader','agent_guard_owner','agent_view_owner')"
                )).mappings().all()
                roles = {row["rolname"]: _role(dict(row)) for row in role_rows}
                if set(roles) != {EXPECTED_READER, EXPECTED_GUARD_OWNER, EXPECTED_VIEW_OWNER}:
                    raise QueryBrokerError("QUERY_BROKER_UNAVAILABLE")
                membership_edges = connection.execute(text(
                    "SELECT count(*) FROM pg_catalog.pg_auth_members m "
                    "JOIN pg_catalog.pg_roles member ON member.oid=m.member "
                    "JOIN pg_catalog.pg_roles granted ON granted.oid=m.roleid "
                    "WHERE member.rolname IN ('agent_reader','agent_guard_owner','agent_view_owner') "
                    "OR granted.rolname IN ('agent_reader','agent_guard_owner','agent_view_owner')"
                )).scalar_one()
                privileges = connection.execute(text(
                    "SELECT has_database_privilege(current_user,current_database(),'TEMP') AS can_temp, "
                    "has_database_privilege(current_user,current_database(),'CREATE') AS can_create_database, "
                    "has_schema_privilege(current_user,'public','CREATE') AS can_create_public, "
                    "has_schema_privilege(current_user,'agent_semantic','USAGE') AS agent_usage, "
                    "has_schema_privilege(current_user,'agent_semantic','CREATE') AS agent_create"
                )).mappings().one()
                guard = connection.execute(text(
                    "SELECT owner.rolname AS owner, c.relrowsecurity AS rls_enabled, "
                    "c.relforcerowsecurity AS rls_forced, "
                    "has_table_privilege(current_user,c.oid,'SELECT') AS reader_select "
                    "FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "JOIN pg_catalog.pg_roles owner ON owner.oid=c.relowner "
                    "WHERE n.nspname='agent_semantic' AND c.relname='dataset_guard' "
                    "AND c.relkind='r'"
                )).mappings().one_or_none()
                if guard is None:
                    raise QueryBrokerError("QUERY_BROKER_UNAVAILABLE")
                guard_policy_count = connection.execute(text(
                    "SELECT count(*) FROM pg_catalog.pg_policy p "
                    "JOIN pg_catalog.pg_class c ON c.oid=p.polrelid "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='agent_semantic' AND c.relname='dataset_guard'"
                )).scalar_one()
                view_rows = connection.execute(text(
                    "SELECT c.relname AS name, owner.rolname AS owner, "
                    "COALESCE('security_barrier=true'=ANY(c.reloptions),false) AS security_barrier, "
                    "has_table_privilege(current_user,c.oid,'SELECT') AS reader_select "
                    "FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "JOIN pg_catalog.pg_roles owner ON owner.oid=c.relowner "
                    "WHERE n.nspname='agent_semantic' AND c.relkind='v'"
                )).mappings().all()
                forbidden_relations = connection.execute(text(
                    "SELECT count(*) FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE c.relkind IN ('r','p','v','m','f') "
                    "AND n.nspname NOT IN ('pg_catalog','information_schema') "
                    "AND ((NOT (n.nspname='agent_semantic' AND c.relname IN "
                    "('part_catalog_v1','purchase_activity_v1','sales_market_month_v1')) "
                    "AND has_table_privilege(current_user,c.oid,'SELECT')) "
                    "OR has_table_privilege(current_user,c.oid,'INSERT') "
                    "OR has_table_privilege(current_user,c.oid,'UPDATE') "
                    "OR has_table_privilege(current_user,c.oid,'DELETE') "
                    "OR has_table_privilege(current_user,c.oid,'TRUNCATE') "
                    "OR has_table_privilege(current_user,c.oid,'REFERENCES') "
                    "OR has_table_privilege(current_user,c.oid,'TRIGGER'))"
                )).scalar_one()
                forbidden_sequences = connection.execute(text(
                    "SELECT count(*) FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE c.relkind='S' AND n.nspname NOT IN ('pg_catalog','information_schema') AND ("
                    "has_sequence_privilege(current_user,c.oid,'USAGE') "
                    "OR has_sequence_privilege(current_user,c.oid,'SELECT') "
                    "OR has_sequence_privilege(current_user,c.oid,'UPDATE'))"
                )).scalar_one()
                snapshot = ProbeSnapshot(
                    current_user=identity["current_user"],
                    session_user=identity["session_user"],
                    reader=roles[EXPECTED_READER],
                    guard_owner=roles[EXPECTED_GUARD_OWNER],
                    view_owner=roles[EXPECTED_VIEW_OWNER],
                    protected_role_membership_edges=int(membership_edges),
                    reader_can_temp=bool(privileges["can_temp"]),
                    reader_can_create_database=bool(privileges["can_create_database"]),
                    reader_can_create_public=bool(privileges["can_create_public"]),
                    reader_has_agent_schema_usage=bool(privileges["agent_usage"]),
                    reader_can_create_agent_schema=bool(privileges["agent_create"]),
                    guard_owner_name=guard["owner"],
                    guard_rls_enabled=bool(guard["rls_enabled"]),
                    guard_rls_forced=bool(guard["rls_forced"]),
                    guard_policy_count=int(guard_policy_count),
                    reader_can_select_guard=bool(guard["reader_select"]),
                    views=tuple(
                        ViewPosture(
                            name=row["name"],
                            owner=row["owner"],
                            security_barrier=bool(row["security_barrier"]),
                            reader_select=bool(row["reader_select"]),
                        )
                        for row in view_rows
                    ),
                    forbidden_relation_privileges=int(forbidden_relations),
                    forbidden_sequence_privileges=int(forbidden_sequences),
                    # This slice intentionally ships no semantic-view migration.
                    # Exact policy/view definition hashes therefore have no
                    # trusted release manifest yet and live enablement remains
                    # impossible rather than trusting only object names/flags.
                    catalog_contract_verified=False,
                )
                evaluate_probe(snapshot)
                tx.rollback()
                tx = None
        except QueryBrokerError:
            if tx is not None:
                tx.rollback()
            raise
        except (SQLAlchemyError, KeyError, TypeError, ValueError):
            if tx is not None:
                tx.rollback()
            raise QueryBrokerError("QUERY_BROKER_UNAVAILABLE") from None
