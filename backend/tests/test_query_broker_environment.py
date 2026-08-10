"""Independent database role/view posture self-check contract."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.agent.query_broker.environment import (
    AgentDatabaseProbe,
    ProbeSnapshot,
    RolePosture,
    ViewPosture,
    evaluate_probe,
)
from app.agent.query_broker.errors import QueryBrokerError


def _role(name: str, *, login: bool) -> RolePosture:
    return RolePosture(
        name=name,
        can_login=login,
        inherit=False,
        superuser=False,
        create_db=False,
        create_role=False,
        replication=False,
        bypass_rls=False,
    )


def _safe() -> ProbeSnapshot:
    return ProbeSnapshot(
        current_user="agent_reader",
        session_user="agent_reader",
        transaction_read_only=True,
        transaction_isolation_repeatable_read=True,
        row_security_on=True,
        search_path_pinned=True,
        reader=_role("agent_reader", login=True),
        guard_owner=_role("agent_guard_owner", login=False),
        view_owner=_role("agent_view_owner", login=False),
        protected_role_membership_edges=0,
        reader_can_temp=False,
        reader_temp_file_limit_zero=True,
        reader_can_create_database=False,
        reader_can_create_public=False,
        reader_has_agent_schema_usage=True,
        reader_can_create_agent_schema=False,
        guard_owner_name="agent_guard_owner",
        guard_rls_enabled=True,
        guard_rls_forced=True,
        guard_policy_count=1,
        reader_can_select_guard=False,
        views=tuple(
            ViewPosture(
                name=name,
                owner="agent_view_owner",
                security_barrier=True,
                reader_select=True,
            )
            for name in (
                "part_catalog_v1",
                "purchase_activity_v1",
                "sales_market_month_v1",
            )
        ),
        forbidden_relation_privileges=0,
        forbidden_sequence_privileges=0,
        catalog_contract_verified=True,
    )


def test_safe_probe_snapshot_is_accepted():
    evaluate_probe(_safe())


@pytest.mark.parametrize(
    "change",
    [
        {"current_user": "app"},
        {"session_user": "app"},
        {"transaction_read_only": False},
        {"transaction_isolation_repeatable_read": False},
        {"row_security_on": False},
        {"search_path_pinned": False},
        {"protected_role_membership_edges": 1},
        {"reader_can_temp": True},
        {"reader_temp_file_limit_zero": False},
        {"reader_can_create_database": True},
        {"reader_can_create_public": True},
        {"reader_has_agent_schema_usage": False},
        {"reader_can_create_agent_schema": True},
        {"guard_rls_enabled": False},
        {"guard_rls_forced": False},
        {"guard_policy_count": 0},
        {"reader_can_select_guard": True},
        {"forbidden_relation_privileges": 1},
        {"forbidden_sequence_privileges": 1},
        {"catalog_contract_verified": False},
    ],
)
def test_any_role_rls_or_actual_privilege_drift_fails_closed(change):
    with pytest.raises(QueryBrokerError) as exc:
        evaluate_probe(replace(_safe(), **change))
    assert exc.value.code == "QUERY_BROKER_UNAVAILABLE"


def test_unverified_catalog_contract_is_an_absolute_release_blocker():
    with pytest.raises(QueryBrokerError) as exc:
        evaluate_probe(replace(_safe(), catalog_contract_verified=False))
    assert exc.value.code == "QUERY_BROKER_UNAVAILABLE"


def test_view_owner_security_barrier_and_exact_set_are_all_required():
    safe = _safe()
    bad_owner = replace(safe.views[0], owner="agent_guard_owner")
    missing_barrier = replace(safe.views[1], security_barrier=False)
    for views in (
        (bad_owner, *safe.views[1:]),
        (safe.views[0], missing_barrier, safe.views[2]),
        safe.views[:-1],
    ):
        with pytest.raises(QueryBrokerError) as exc:
            evaluate_probe(replace(safe, views=tuple(views)))
        assert exc.value.code == "QUERY_BROKER_UNAVAILABLE"


@pytest.mark.parametrize(
    "role_field,change",
    [
        ("reader", {"inherit": True}),
        ("reader", {"bypass_rls": True}),
        ("guard_owner", {"can_login": True}),
        ("guard_owner", {"inherit": True}),
        ("view_owner", {"superuser": True}),
        ("view_owner", {"create_role": True}),
    ],
)
def test_reader_and_both_owners_have_exact_non_privileged_posture(role_field, change):
    safe = _safe()
    role = replace(getattr(safe, role_field), **change)
    with pytest.raises(QueryBrokerError) as exc:
        evaluate_probe(replace(safe, **{role_field: role}))
    assert exc.value.code == "QUERY_BROKER_UNAVAILABLE"


class _ProbeResult:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def one(self):
        return self.value

    def one_or_none(self):
        return self.value

    def all(self):
        return self.value

    def scalar_one(self):
        return self.value


class _ProbeConnection:
    def __init__(self):
        self.queries: list[str] = []

    def begin(self):
        raise AssertionError("borrowed-connection probe must not begin a transaction")

    def exec_driver_sql(self, sql):
        raise AssertionError(f"borrowed-connection probe must not SET: {sql}")

    def execute(self, statement):
        sql = str(statement)
        self.queries.append(sql)
        if "current_setting('temp_file_limit')" in sql:
            return _ProbeResult({
                "current_user": "agent_reader",
                "session_user": "agent_reader",
                "transaction_read_only": True,
                "transaction_isolation_repeatable_read": True,
                "row_security_on": True,
                "search_path_pinned": True,
                "temp_file_limit_zero": True,
            })
        if "FROM pg_catalog.pg_roles" in sql and "WHERE rolname" in sql:
            return _ProbeResult([
                {
                    "rolname": name,
                    "rolcanlogin": name == "agent_reader",
                    "rolinherit": False,
                    "rolsuper": False,
                    "rolcreatedb": False,
                    "rolcreaterole": False,
                    "rolreplication": False,
                    "rolbypassrls": False,
                }
                for name in ("agent_reader", "agent_guard_owner", "agent_view_owner")
            ])
        if "pg_auth_members" in sql:
            return _ProbeResult(0)
        if "has_database_privilege" in sql:
            return _ProbeResult({
                "can_temp": False,
                "can_create_database": False,
                "can_create_public": False,
                "agent_usage": True,
                "agent_create": False,
            })
        if "c.relname='dataset_guard'" in sql and "relrowsecurity" in sql:
            return _ProbeResult({
                "owner": "agent_guard_owner",
                "rls_enabled": True,
                "rls_forced": True,
                "reader_select": False,
            })
        if "FROM pg_catalog.pg_policy" in sql:
            return _ProbeResult(1)
        if "c.relkind='v'" in sql:
            return _ProbeResult([
                {
                    "name": name,
                    "owner": "agent_view_owner",
                    "security_barrier": True,
                    "reader_select": True,
                }
                for name in (
                    "part_catalog_v1",
                    "purchase_activity_v1",
                    "sales_market_month_v1",
                )
            ])
        if "c.relkind IN" in sql or "c.relkind='S'" in sql:
            return _ProbeResult(0)
        raise AssertionError(f"unexpected probe SQL: {sql}")


def test_live_probe_only_reads_borrowed_connection_and_catalog_functions_are_qualified():
    connection = _ProbeConnection()
    probe = AgentDatabaseProbe()
    with pytest.raises(QueryBrokerError) as exc:
        probe.ensure_ready(connection)
    # The semantic catalog contract remains deliberately false in this slice.
    assert exc.value.code == "QUERY_BROKER_UNAVAILABLE"
    joined = "\n".join(connection.queries)
    for function in (
        "current_setting(",
        "current_database(",
        "has_database_privilege(",
        "has_schema_privilege(",
        "has_table_privilege(",
        "has_sequence_privilege(",
        "count(",
    ):
        assert joined.count(function) == joined.count(f"pg_catalog.{function}")
