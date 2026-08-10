"""Independent database role/view posture self-check contract."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.agent.query_broker.environment import (
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
        reader=_role("agent_reader", login=True),
        guard_owner=_role("agent_guard_owner", login=False),
        view_owner=_role("agent_view_owner", login=False),
        protected_role_membership_edges=0,
        reader_can_temp=False,
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
        {"protected_role_membership_edges": 1},
        {"reader_can_temp": True},
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
