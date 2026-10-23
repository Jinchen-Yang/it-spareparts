"""Independent S1 contract attacks through HTTP, real PostgreSQL and Cerbos.

No dependency overrides and no successful mocked authorization decisions. Startup
activation is explicit because the small HTTP app intentionally has no lifespan.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, event, func, select

from app.api import accounts, authorization, role_templates, sales_analytics
from app.auth import _make_token
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant, SysCustomerAssignment
from app.models.dimensions import DimCustomer
from app.models.system import SysAuditLog, SysUser
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.authz_pdp_helpers import local_cerbos_config


@pytest.fixture
def s1_client(db, monkeypatch, cerbos_url):
    from app.authz import runtime

    settings = get_settings()
    monkeypatch.setattr(settings, "authorization_mode", "unified_modules")
    monkeypatch.setattr(settings, "authz_enabled_resources", [])
    monkeypatch.setattr(settings, "authz_suspended_resources", [])
    monkeypatch.setattr(settings, "authz_sales_analysis_enabled", True)
    monkeypatch.setattr(settings, "authz_sales_analysis_suspended", False)
    monkeypatch.setattr(settings, "cerbos_url", cerbos_url)
    monkeypatch.setattr(settings, "cerbos_timeout_seconds", 2.0)
    runtime.activate_configured_resources(db.get_bind())
    app = FastAPI()
    for router in (authorization.router, accounts.router, role_templates.router,
                   sales_analytics.router):
        app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        yield client


def _user(db, username, role="readonly", **changes):
    user = SysUser(username=username, role=role, password_hash="unused-synthetic",
                   **changes)
    db.add(user)
    db.commit()
    return user


def _headers(user, **changes):
    claims = dict(role=user.role, sub=user.username, name=None,
                  authn="sys_user", token_version=user.token_version)
    claims.update(changes)
    token, _ = _make_token(**claims)
    return {"Authorization": f"Bearer {token}"}


def _json(response, status=200):
    assert response.status_code == status, response.text
    assert response.headers.get("cache-control") == "no-store"
    return response.json()


def _read(client, actor, target):
    return _json(client.get(f"/api/authz/users/{target.username}/modules",
                            headers=_headers(actor)))


def _configuration(*, actions=None, fields=None):
    return {"actions": ["read"] if actions is None else sorted(actions),
            "scope": "assigned_customers",
            "field_permissions": [] if fields is None else sorted(fields)}


def _command(snapshot, *, operation="replace_resource", configuration=None):
    update = {"resource_id": "sales_analysis",
              "expected_version": snapshot["resources"]["sales_analysis"]["version"],
              "operation": operation}
    if operation != "clear_override":
        update["configuration"] = configuration or _configuration()
    return {"schema_version": 1, "catalog_version": snapshot["catalog_version"],
            "expected_epoch": snapshot["authorization_epoch"], "updates": [update]}


def _write(client, actor, target, command):
    return client.put(f"/api/authz/users/{target.username}/modules",
                      headers=_headers(actor), json=command)


def _persisted(db, user):
    """Observe the whole transaction boundary, including invisible source state."""
    db.expire_all()
    db.refresh(user)
    grants = list(db.execute(select(
        SysAuthorizationGrant.resource, SysAuthorizationGrant.actions,
        SysAuthorizationGrant.scope, SysAuthorizationGrant.field_permissions,
        SysAuthorizationGrant.version,
    ).where(SysAuthorizationGrant.user_id == user.id).order_by(
        SysAuthorizationGrant.resource)))
    assignments = list(db.scalars(select(SysCustomerAssignment.customer_id).where(
        SysCustomerAssignment.user_id == user.id).order_by(
        SysCustomerAssignment.customer_id)))
    return deepcopy((user.token_version, user.authorization_profile, grants,
                     assignments, db.scalar(select(func.count()).select_from(SysAuditLog))))


def test_shared_identity_snapshot_denies_all_even_with_forged_admin_claims(db, s1_client):
    actor = _user(db, "independent-shared-name", "admin")
    headers = _headers(actor, fallback=True, perms={"page_sales_analysis": True})
    snapshot = _json(s1_client.get("/api/authz/me", headers=headers))
    assert snapshot["principal"] is None
    assert all(item["effect"] == "deny" for item in snapshot["actor_capabilities"].values())
    assert all(decision["effect"] == "deny" for resource in snapshot["resources"].values()
               for decision in resource["decisions"].values())
    assert snapshot["sales_analysis"]["read"] is False
    _json(s1_client.get("/api/authz/catalog", headers=headers), 403)
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant)) == 0


@pytest.mark.parametrize("mutation", ["epoch", "disabled", "deleted", "demoted"])
def test_live_identity_change_rejects_previously_valid_admin_command(db, s1_client, mutation):
    actor = _user(db, "independent-admin", "admin")
    target = _user(db, "independent-target")
    body = _command(_read(s1_client, actor, target))
    headers = _headers(actor)
    if mutation == "epoch":
        actor.token_version += 1
    elif mutation == "disabled":
        actor.is_active = False
    elif mutation == "deleted":
        actor.deleted_at = datetime.now(timezone.utc)
    else:
        actor.role = "readonly"
    db.commit()
    before = _persisted(db, target)
    response = s1_client.put(f"/api/authz/users/{target.username}/modules",
                             headers=headers, json=body)
    _json(response, 403 if mutation == "demoted" else 401)
    assert _persisted(db, target) == before


@pytest.mark.parametrize("bad_resource", ["purchase_analysis", "retired_pool_rebuild",
                                           "sales_analysis_typo"])
def test_invalid_second_resource_rolls_back_valid_first_resource(db, s1_client, bad_resource):
    actor = _user(db, "independent-admin", "admin")
    target = _user(db, "independent-target")
    body = _command(_read(s1_client, actor, target))
    bad = deepcopy(body["updates"][0])
    bad["resource_id"] = bad_resource
    body["updates"].append(bad)
    before = _persisted(db, target)
    error = _json(_write(s1_client, actor, target, body), 422)
    assert error["detail"]["committed"] is False
    assert _persisted(db, target) == before


def test_equal_override_and_clear_advance_cas_without_resurrecting_legacy(db, s1_client):
    actor = _user(db, "independent-admin", "admin")
    target = _user(db, "independent-target", permissions={"page_sales_analysis": True})
    initial = _read(s1_client, actor, target)
    first = _json(_write(s1_client, actor, target, _command(initial)))["result"]
    second = _json(_write(s1_client, actor, target, _command(first)))["result"]
    a, b = first["resources"]["sales_analysis"], second["resources"]["sales_analysis"]
    assert a["configuration"] == b["configuration"]
    assert b["personal_override"]["present"] is True
    assert b["version"] == a["version"] + 1
    assert second["authorization_epoch"] == first["authorization_epoch"] + 1
    cleared = _json(_write(s1_client, actor, target,
                           _command(second, operation="clear_override")))["result"]
    resource = cleared["resources"]["sales_analysis"]
    assert resource["configuration"]["actions"] == []
    assert resource["personal_override"] == {"present": False, "configuration": None}
    assert resource["source"] == "none"
    before = _persisted(db, target)
    _json(_write(s1_client, actor, target, _command(initial)), 409)
    assert _persisted(db, target) == before
    assert db.scalar(select(SysAuthorizationGrant.version).where(
        SysAuthorizationGrant.user_id == target.id)) == resource["version"]
    snapshot = _json(s1_client.get("/api/authz/me", headers=_headers(target)))
    assert snapshot["resources"]["sales_analysis"]["decisions"]["read"]["effect"] == "deny"


def test_customer_omitted_empty_and_invalid_are_distinct_atomic_commands(db, s1_client):
    actor = _user(db, "independent-admin", "admin")
    target = _user(db, "independent-target")
    customer = DimCustomer(name_raw="independent-customer")
    db.add(customer)
    db.commit()
    body = _command(_read(s1_client, actor, target))
    body["customer_assignments"] = {"ids": [customer.id]}
    first = _json(_write(s1_client, actor, target, body))["result"]
    second = _json(_write(s1_client, actor, target, _command(first)))["result"]
    assert second["customer_assignments"]["ids"] == [customer.id]
    invalid = _command(second)
    invalid["customer_assignments"] = {"ids": [customer.id + 1000000]}
    before = _persisted(db, target)
    _json(_write(s1_client, actor, target, invalid), 404)
    assert _persisted(db, target) == before
    empty = _command(second)
    empty["customer_assignments"] = {"ids": []}
    third = _json(_write(s1_client, actor, target, empty))["result"]
    assert third["customer_assignments"]["ids"] == []
    assert third["authorization_epoch"] == second["authorization_epoch"] + 1
    db.refresh(target)
    snapshot = _json(s1_client.get("/api/authz/me", headers=_headers(target)))
    assert snapshot["resources"]["sales_analysis"]["decisions"]["read"]["effect"] == "allow"
    assert snapshot["sales_analysis"]["customer_count"] == 0


def test_bulk_preview_is_invalidated_by_only_second_target_source_change(db, s1_client):
    actor = _user(db, "independent-admin", "admin")
    alice = _user(db, "independent-alice")
    bob = _user(db, "independent-bob")
    states = [_read(s1_client, actor, target) for target in (alice, bob)]
    body = {"schema_version": 1, "catalog_version": states[0]["catalog_version"],
            "dry_run": True,
            "targets": [{"user_id": target.id, "username": target.username,
                         "expected_epoch": state["authorization_epoch"]}
                        for target, state in zip((alice, bob), states)],
            "operation": {"kind": "replace_resources", "updates": [
                {"resource_id": "sales_analysis", "configuration": _configuration()}]}}
    preview = _json(s1_client.post("/api/accounts/bulk", headers=_headers(actor), json=body))
    assert preview["committed"] is False
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant)) == 0
    _json(_write(s1_client, actor, bob, _command(states[1])))
    before = [_persisted(db, target) for target in (alice, bob)]
    execute = {**preview["command"], "dry_run": False, "fingerprint": preview["fingerprint"]}
    _json(s1_client.post("/api/accounts/bulk", headers=_headers(actor), json=execute), 409)
    assert [_persisted(db, target) for target in (alice, bob)] == before


def test_cutover_survives_mode_and_enable_reversal(db, s1_client, monkeypatch):
    actor = _user(db, "independent-admin", "admin")
    target = _user(db, "independent-target", permissions={"page_sales_analysis": True})
    _json(_write(s1_client, actor, target, _command(_read(s1_client, actor, target))))
    db.refresh(target)
    headers = _headers(target)
    assert _json(s1_client.get("/api/authz/me", headers=headers))["sales_analysis"]["read"] is True
    monkeypatch.setattr(get_settings(), "authorization_mode", "legacy_compat")
    monkeypatch.setattr(get_settings(), "authz_sales_analysis_enabled", False)
    snapshot = _json(s1_client.get("/api/authz/me", headers=headers))
    assert snapshot["resources"]["sales_analysis"]["runtime_state"] == "suspended"
    assert snapshot["sales_analysis"]["read"] is False
    assert s1_client.get("/api/sales/analytics/options", headers=headers).status_code == 503
    legacy = {"expected_version": 1, "actions": ["read"], "scope": "all",
              "field_permissions": [], "customer_ids": []}
    _json(s1_client.put(f"/api/authz/users/{target.username}",
                        headers=_headers(actor), json=legacy), 409)


def test_real_pdp_disconnect_never_returns_partial_allow(db, s1_client, monkeypatch):
    actor = _user(db, "independent-admin", "admin")
    target = _user(db, "independent-target")
    _json(_write(s1_client, actor, target, _command(_read(s1_client, actor, target))))
    db.refresh(target)
    headers = _headers(target)
    assert _json(s1_client.get("/api/authz/me", headers=headers))["sales_analysis"]["read"] is True
    monkeypatch.setattr(get_settings(), "cerbos_url", "http://127.0.0.1:1")
    monkeypatch.setattr(get_settings(), "cerbos_timeout_seconds", .1)
    response = _json(s1_client.get("/api/authz/me", headers=headers), 503)
    assert "resources" not in response
    assert response["detail"]["code"] == "AUTHZ_ENGINE_UNAVAILABLE"
    assert response["detail"]["committed"] is False


@pytest.mark.parametrize("shape", ["dict", "user_context"])
def test_core_rejects_legacy_identity_shapes_without_database_or_pdp(db, shape):
    from app.authz.principal import load_principal
    from app.security import UserContext

    identity = ({"sub": "admin", "authn": "sys_user", "tv": 0} if shape == "dict"
                else UserContext(user_id="admin", role="admin", is_authenticated=True,
                                 authn="sys_user", token_version=0))
    with pytest.raises(HTTPException) as error:
        load_principal(db, identity)
    assert error.value.status_code == 401
    assert error.value.detail["code"] == "AUTHN_SESSION_INVALID"


def test_runtime_read_cannot_create_missing_activation_marker(db, s1_client):
    from app.authz import runtime
    from app.models.authorization import SysAuthorizationCutover

    db.execute(delete(SysAuthorizationCutover))
    db.commit()
    with pytest.raises(HTTPException) as error:
        runtime.runtime_states(db)
    assert error.value.status_code == 503
    assert db.scalar(select(func.count()).select_from(SysAuthorizationCutover)) == 0
    db.rollback()
    runtime.activate_configured_resources(db.get_bind())
    assert runtime.runtime_states(db)["sales_analysis"].runtime_state == "enabled"
    db.rollback()
    runtime.activate_configured_resources(db.get_bind())
    assert db.scalar(select(func.count()).select_from(SysAuthorizationCutover)) == 1


def test_two_real_http_writers_share_one_cas_winner(db, s1_client):
    actor = _user(db, "independent-admin", "admin")
    target = _user(db, "independent-target")
    initial = _read(s1_client, actor, target)
    commands = [_command(initial), _command(initial, configuration=_configuration(
        actions=["read", "export"], fields=["data_customer"]))]
    headers = _headers(actor)
    path = f"/api/authz/users/{target.username}/modules"
    barrier = Barrier(2, timeout=10)
    before_audit = db.scalar(select(func.count()).select_from(SysAuditLog))
    db.rollback()

    def send(body):
        barrier.wait()
        return s1_client.put(path, headers=headers, json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send, body) for body in commands]
        responses = [future.result(timeout=15) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409], [
        response.text for response in responses]
    for response in responses:
        _json(response, response.status_code)
    winner = next(response.json()["result"] for response in responses if response.status_code == 200)
    assert winner["authorization_epoch"] == initial["authorization_epoch"] + 1
    assert _read(s1_client, actor, target)["resources"] == winner["resources"]
    assert db.scalar(select(func.count()).select_from(SysAuditLog)) == before_audit + 1


def _synthetic_descriptor(definition):
    """Complete wire metadata for a test-only executable definition."""
    return {
        "id": definition.name, "label": definition.name,
        "migration_state": "implemented",
        "actions": [{"id": action, "label": action, "kind": "data",
                     "requires_actions": [required for source, required in definition.action_dependencies if source == action],
                     "requires_fields": [required for source, required in definition.action_field_dependencies if source == action]}
                    for action in sorted(definition.actions)],
        "scopes": [{"id": scope, "label": scope, "relationship_kind": None,
                    "requires_context": scope in definition.context_scopes}
                   for scope in sorted(definition.scopes)],
        "denial_scope": definition.denial_scope,
        "field_permissions": [{"id": field, "label": field,
                               "requires_fields": [required for source, required in definition.field_dependencies if source == field]}
                              for field in sorted(definition.field_permissions)],
        "surfaces": [], "legacy_sources": [], "notice": "Synthetic test resource only",
    }


@pytest.fixture
def two_resource_pdp(tmp_path, monkeypatch):
    """Synthetic registered resources, never enabling a real planned module."""
    from app.authz import catalog

    binary = os.environ.get("CERBOS_BINARY") or shutil.which("cerbos")
    if not binary:
        pytest.fail("Independent S1 PDP checks require CERBOS_BINARY (0.57.0)")
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True)
    assert "0.57.0" in version.stdout
    definitions = dict(catalog.RESOURCE_DEFINITIONS)
    policies = tmp_path / "policies"
    policies.mkdir()
    for resource, scope, field in (("s1_probe_a", "all", "cost"),
                                    ("s1_probe_b", "assigned_customers", "customer")):
        definitions[resource] = catalog.ResourceDefinition(
            resource, frozenset({"read", "export"}), frozenset({scope}),
            frozenset({field}), frozenset(), action_dependencies=(("export", "read"),),
            denial_scope=scope)
        policy = f"""apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: default
  resource: {resource}
  rules:
"""
        for action in ("read", "export"):
            policy += f"""    - actions: [{action}]
      effect: EFFECT_ALLOW
      roles: [registered_account]
      condition:
        match:
          expr: '"{action}" in P.attr.grants.{resource}.actions'
      output:
        when:
          ruleActivated: >-
            {{"resource": "{resource}", "scope": P.attr.grants.{resource}.scope,
             "version": P.attr.grants.{resource}.version,
             "field_permissions": P.attr.grants.{resource}.field_permissions}}
"""
        (policies / f"{resource}.yaml").write_text(policy)
    monkeypatch.setattr(catalog, "RESOURCE_DEFINITIONS", definitions)
    monkeypatch.setattr(catalog, "_DESCRIPTORS", catalog.resource_descriptors() + [
        _synthetic_descriptor(definitions[name]) for name in ("s1_probe_a", "s1_probe_b")])
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        http_port, grpc_port, debug_port = [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
    source = Path(__file__).resolve().parents[2] / "deploy/cerbos/config.yaml"
    config = local_cerbos_config(
        source.read_text(), http_port=http_port, grpc_port=grpc_port, policies=policies,
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config)
    url = f"http://127.0.0.1:{http_port}"
    log_path = tmp_path / "cerbos.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([binary, "server", "--config", str(config_path),
                                    "--debug-listen-addr", f"127.0.0.1:{debug_port}"],
                                   stdout=log, stderr=subprocess.STDOUT,
                                   env={**os.environ, "CERBOS_NO_TELEMETRY": "1"})
        try:
            for _ in range(100):
                assert process.poll() is None, log_path.read_text()
                try:
                    with urlopen(f"{url}/_cerbos/health", timeout=.2) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    time.sleep(.05)
            else:
                pytest.fail("Synthetic-resource Cerbos did not become healthy")
            monkeypatch.setattr(get_settings(), "cerbos_url", url)
            monkeypatch.setattr(get_settings(), "cerbos_timeout_seconds", 2.0)
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.mark.parametrize("corruption", [None, "missing", "duplicate", "wrong_kind", "wrong_scope"])
def test_real_batch_pairs_heterogeneous_grants_and_rejects_corrupt_results(
    db, two_resource_pdp, monkeypatch, corruption,
):
    from cerbos.sdk.client import CerbosClient
    from app.authz import engine

    user = _user(db, "independent-multi-resource")
    rows = [SysAuthorizationGrant(user_id=user.id, resource="s1_probe_a", scope="all",
                                  actions=["read", "export"], field_permissions=["cost"], version=3),
            SysAuthorizationGrant(user_id=user.id, resource="s1_probe_b", scope="assigned_customers",
                                  actions=["read"], field_permissions=["customer"], version=7)]
    db.add_all(rows)
    db.commit()
    grants = {row.resource: engine.Grant(tuple(row.actions), row.scope,
                                       tuple(row.field_permissions), row.version, resource=row.resource)
              for row in rows}
    original = CerbosClient.check_resources
    calls = []

    def observed(client, *args, **kwargs):
        # Successful responses always come from the live process. Deliberate
        # transport corruption only tests fail-closed response validation.
        response = original(client, *args, **kwargs)
        calls.append(response)
        if corruption == "missing":
            response.results.pop()
        elif corruption == "duplicate":
            response.results[1] = deepcopy(response.results[0])
        elif corruption == "wrong_kind":
            response.results[0].resource.kind = "s1_probe_wrong"
        elif corruption == "wrong_scope":
            response.results[0].outputs[0].val["scope"] = "unrelated_scope"
        return response

    monkeypatch.setattr(CerbosClient, "check_resources", observed)
    attributes = {resource: {} for resource in grants}
    requested = {resource: ("read", "export") for resource in grants}
    if corruption:
        with pytest.raises(engine.AuthorizationUnavailable):
            engine.check_resources(user.id, grants, attributes, requested)
    else:
        decisions = engine.check_resources(user.id, grants, attributes, requested)
        assert decisions["s1_probe_a"].allowed_actions == {"read", "export"}
        assert decisions["s1_probe_a"].field_permissions == {"cost"}
        assert decisions["s1_probe_b"].allowed_actions == {"read"}
        assert decisions["s1_probe_b"].field_permissions == {"customer"}
    assert len(calls) == 1


def test_multi_resource_snapshot_batches_sql_and_pdp_without_object_enumeration(
    db, two_resource_pdp, monkeypatch,
):
    from cerbos.sdk.client import CerbosClient
    from fastapi.security import HTTPAuthorizationCredentials
    from app.authz import catalog, runtime, service
    from app.authz.principal import current_verified_identity

    monkeypatch.setattr(runtime, "RESOURCE_DEFINITIONS", catalog.RESOURCE_DEFINITIONS)
    settings = get_settings()
    monkeypatch.setattr(settings, "authorization_mode", "unified_modules")
    monkeypatch.setattr(settings, "authz_enabled_resources", ["s1_probe_a", "s1_probe_b"])
    monkeypatch.setattr(settings, "authz_suspended_resources", [])
    monkeypatch.setattr(settings, "authz_sales_analysis_enabled", False)
    monkeypatch.setattr(settings, "authz_sales_analysis_suspended", False)
    runtime.activate_configured_resources(db.get_bind())
    user = _user(db, "independent-snapshot")
    db.add(SysAuthorizationGrant(user_id=user.id, resource="s1_probe_a", actions=["read"],
                                 scope="all", field_permissions=["cost"], version=1))
    db.commit()
    token = _headers(user)["Authorization"].removeprefix("Bearer ")
    identity = current_verified_identity(HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=token), db)
    original = CerbosClient.check_resources
    checks = []
    statements = []

    def record_check(client, *args, **kwargs):
        result = original(client, *args, **kwargs)
        checks.append(len(result.results))
        return result

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    monkeypatch.setattr(CerbosClient, "check_resources", record_check)
    event.listen(db.get_bind(), "before_cursor_execute", record_sql)
    try:
        one = service.my_snapshot(db, identity)
        first_statements = list(statements)
        # Release reader SHARE before modifying the test fixture in its own tx.
        db.rollback()
        db.add(SysAuthorizationGrant(user_id=user.id, resource="s1_probe_b", actions=["read"],
                                     scope="assigned_customers", field_permissions=["customer"], version=1))
        db.commit()
        statements.clear()
        two = service.my_snapshot(db, identity)
        second_statements = list(statements)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", record_sql)
    assert checks == [1, 2]
    assert len(first_statements) == len(second_statements)
    for queries in (first_statements, second_statements):
        assert sum("from sys_authorization_grant" in query for query in queries) == 1
        assert sum("from sys_authorization_cutover" in query for query in queries) == 1
        assert not any("sys_customer_assignment" in query or "maintenance_project" in query
                       for query in queries)
    assert one["resources"]["s1_probe_b"]["decisions"]["read"]["effect"] == "deny"
    assert two["resources"]["s1_probe_a"]["field_permissions"] == ["cost"]
    assert two["resources"]["s1_probe_b"]["field_permissions"] == ["customer"]
    assert two["resources"]["s1_probe_a"]["scope_kind"] == "all"
    assert two["resources"]["s1_probe_b"]["scope_kind"] == "assigned_customers"


@pytest.mark.parametrize("bad_operation", [[], {}, True, None])
def test_non_string_operation_is_structured_422_without_mutation(db, s1_client, bad_operation):
    actor = _user(db, "independent-admin", "admin")
    target = _user(db, "independent-target")
    body = _command(_read(s1_client, actor, target))
    body["updates"][0]["operation"] = bad_operation
    before = _persisted(db, target)
    response = _json(_write(s1_client, actor, target, body), 422)
    assert response["detail"]["committed"] is False
    assert response["detail"]["field_errors"]
    assert _persisted(db, target) == before


def test_account_create_security_branch_cannot_choose_arbitrary_initial_role(db, s1_client):
    actor = _user(db, "independent-admin", "admin")
    catalog = _json(s1_client.get("/api/authz/catalog", headers=_headers(actor)))
    body = {"schema_version": 1, "catalog_version": catalog["catalog_version"],
            "profile": {"username": "independent-new"}, "authorization": {"kind": "none"},
            "security_change": {"role": "boss"}, "dry_run": True}
    response = _json(s1_client.post("/api/accounts", headers=_headers(actor), json=body), 422)
    assert response["detail"]["committed"] is False
    assert db.scalar(select(SysUser.id).where(SysUser.username == "independent-new")) is None


def _apply_template(client, actor, target, template, *, sync=False):
    state = _read(client, actor, target)
    code = template["template"]["code"]
    path = f"/api/authz/templates/{code}/sync" if sync else "/api/accounts/bulk"
    body = {"schema_version": 1, "catalog_version": state["catalog_version"], "dry_run": True,
            "targets": [{"user_id": target.id, "username": target.username,
                         "expected_epoch": state["authorization_epoch"]}],
            "operation": {"kind": "sync_template" if sync else "apply_template", "template_code": code,
                          "expected_template_version": template["template"]["version"],
                          "override_policy": "preserve"}}
    preview = _json(client.post(path, headers=_headers(actor), json=body))
    result = _json(client.post(path, headers=_headers(actor), json={
        **preview["command"], "dry_run": False, "fingerprint": preview["fingerprint"]}))
    return result["results"][0]


def test_saved_template_clear_and_latest_sync_preserve_equal_and_deny_overrides(db, s1_client):
    actor = _user(db, "independent-admin", "admin")
    target = _user(db, "independent-target")
    catalog_version = _read(s1_client, actor, target)["catalog_version"]
    a = _configuration(fields=["data_customer"])
    b = _configuration(actions=["read", "export"], fields=["data_customer", "data_purchase_cost"])
    created = _json(s1_client.post("/api/role-templates", headers=_headers(actor), json={
        "schema_version": 1, "catalog_version": catalog_version,
        "metadata": {"code": "independent-template", "name": "独立快照"},
        "authorization": {"kind": "explicit", "modules": {"sales_analysis": a}},
    }), 201)["result"]
    inherited = _apply_template(s1_client, actor, target, created)
    overridden = _json(_write(s1_client, actor, target,
                              _command(inherited, configuration=a)))["result"]
    assert overridden["resources"]["sales_analysis"]["personal_override"]["present"] is True
    updated = _json(s1_client.put("/api/authz/templates/independent-template/modules",
                                  headers=_headers(actor), json={
        "schema_version": 1, "catalog_version": catalog_version,
        "expected_version": created["template"]["version"],
        "updates": [{"resource_id": "sales_analysis", "operation": "replace_resource", "configuration": b}],
    }))["result"]
    after_template = _read(s1_client, actor, target)
    assert after_template["authorization_epoch"] == overridden["authorization_epoch"]
    assert after_template["resources"] == overridden["resources"]
    assert after_template["template"]["stale"] is True
    old_clear = _json(_write(s1_client, actor, target,
                             _command(after_template, operation="clear_override")))["result"]
    assert old_clear["resources"]["sales_analysis"]["configuration"] == a
    overridden_again = _json(_write(s1_client, actor, target,
                                    _command(old_clear, configuration=a)))["result"]
    synced = _apply_template(s1_client, actor, target, updated, sync=True)
    resource = synced["resources"]["sales_analysis"]
    assert resource["configuration"] == a
    assert resource["personal_override"] == {"present": True, "configuration": a}
    assert resource["template_snapshot"]["configuration"] == b
    assert synced["authorization_epoch"] == overridden_again["authorization_epoch"] + 1
    cleared = _json(_write(s1_client, actor, target,
                           _command(synced, operation="clear_override")))["result"]
    assert cleared["resources"]["sales_analysis"]["configuration"] == b
    denial = _configuration(actions=[])
    _json(_write(s1_client, actor, target,
                 _command(cleared, operation="revoke_resource", configuration=denial)))
    deny_synced = _apply_template(s1_client, actor, target, updated, sync=True)
    denied = deny_synced["resources"]["sales_analysis"]
    assert denied["configuration"] == denial
    assert denied["personal_override"] == {"present": True, "configuration": denial}


@pytest.mark.parametrize("legacy_key,legacy_value", [("permissions", {}), ("overrides", {}),
                                                     ("keys", []), ("role", None)])
def test_profile_plus_legacy_field_never_partially_saves(db, s1_client, legacy_key, legacy_value):
    actor = _user(db, "independent-admin", "admin")
    target = _user(db, "independent-target", display_name="before")
    state = _read(s1_client, actor, target)
    before = _persisted(db, target)
    body = {"schema_version": 1, "catalog_version": state["catalog_version"],
            "profile": {"display_name": "must not persist"}, legacy_key: legacy_value}
    error = _json(s1_client.put(f"/api/accounts/{target.username}",
                                headers=_headers(actor), json=body), 422)
    assert error["detail"]["committed"] is False
    assert _persisted(db, target) == before
    assert target.display_name == "before"


def test_context_scoped_resource_cannot_turn_module_probe_into_object_access(
    db, two_resource_pdp, monkeypatch,
):
    from dataclasses import replace
    from fastapi.security import HTTPAuthorizationCredentials
    from app.authz import catalog, runtime, service
    from app.authz.principal import current_verified_identity

    resource = "s1_probe_b"
    definitions = dict(catalog.RESOURCE_DEFINITIONS)
    definitions[resource] = replace(definitions[resource], context_scopes=frozenset({"assigned_customers"}))
    monkeypatch.setattr(catalog, "RESOURCE_DEFINITIONS", definitions)
    monkeypatch.setattr(runtime, "RESOURCE_DEFINITIONS", definitions)
    monkeypatch.setattr(catalog, "_DESCRIPTORS", [
        _synthetic_descriptor(definitions[resource]) if item["id"] == resource else item
        for item in catalog.resource_descriptors()])
    settings = get_settings()
    monkeypatch.setattr(settings, "authorization_mode", "unified_modules")
    monkeypatch.setattr(settings, "authz_enabled_resources", [resource])
    monkeypatch.setattr(settings, "authz_suspended_resources", [])
    monkeypatch.setattr(settings, "authz_sales_analysis_enabled", False)
    monkeypatch.setattr(settings, "authz_sales_analysis_suspended", False)
    runtime.activate_configured_resources(db.get_bind())
    user = _user(db, "independent-context")
    db.add(SysAuthorizationGrant(user_id=user.id, resource=resource, actions=["read"],
                                 scope="assigned_customers", field_permissions=["customer"], version=1))
    db.commit()
    token = _headers(user)["Authorization"].removeprefix("Bearer ")
    identity = current_verified_identity(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), db)
    snapshot = service.my_snapshot(db, identity)
    assert snapshot["resources"][resource]["decisions"]["read"]["effect"] == "requires_context"
    with pytest.raises(HTTPException) as error:
        service.resource_access(db, identity, resource, "read")
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "AUTHZ_CONTEXT_REQUIRED"


@pytest.mark.parametrize("delegated", [False, True])
def test_ordinary_account_cannot_reset_credentials_via_legacy_management_gate(db, s1_client, delegated):
    admin = _user(db, "independent-admin", "admin")
    actor = _user(db, "independent-ordinary", permissions={
        "page_accounts": delegated, "action_account_manage": delegated})
    target = _user(db, "independent-target")
    state = _read(s1_client, admin, target)
    body = {"schema_version": 1, "catalog_version": state["catalog_version"],
            "expected_epoch": state["authorization_epoch"], "password": "synthetic-never-saved"}
    before = _persisted(db, target)
    response = _json(s1_client.put(f"/api/accounts/{target.username}/password",
                                   headers=_headers(actor), json=body), 403)
    assert response["detail"]["code"] == "AUTHZ_NAMED_ADMIN_REQUIRED"
    assert response["detail"]["committed"] is False
    assert _persisted(db, target) == before


def test_seed_eligibility_migration_labels_only_existing_rows_and_keeps_old_snapshots(db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from app.models.system import SysRoleTemplate

    backend = Path(__file__).parents[1]
    config = Config(str(backend / 'alembic.ini'))
    config.set_main_option('script_location', str(backend / 'alembic'))
    db.rollback()
    command.downgrade(config, 'f1a3c5e7b9d2')
    try:
        db.execute(text("""INSERT INTO sys_user
            (username,password_hash,role,template_perms,perm_overrides)
            VALUES ('eligibility-old-empty','unused','readonly',
                    '{"page_profit": true}'::jsonb,'{"data_customer": false}'::jsonb)"""))
        db.execute(text("""INSERT INTO sys_role_template
            (code,name,base_role,permissions,is_system,is_active,version)
            VALUES ('eligibility-old-template','Old empty canonical','readonly',
                    '{"page_profit": true}'::jsonb,false,true,4)"""))
        db.commit()
        command.upgrade(config, 'head')
        old = db.scalar(select(SysUser).where(SysUser.username == 'eligibility-old-empty'))
        template = db.scalar(select(SysRoleTemplate).where(SysRoleTemplate.code == 'eligibility-old-template'))
        assert old.authorization_legacy_seed_eligible is True
        assert template.authorization_legacy_seed_eligible is True
        assert old.template_perms == {'page_profit': True}
        assert old.perm_overrides == {'data_customer': False}
        assert old.token_version == 0
        assert old.authorization_profile['resource_overrides'] == {}
        assert template.permissions == {'page_profit': True}
        assert template.authorization_bundle == {} and template.version == 4
        db.execute(text("""INSERT INTO sys_user (username,password_hash,role)
            VALUES ('eligibility-new-raw','unused','readonly')"""))
        db.execute(text("""INSERT INTO sys_role_template
            (code,name,base_role,permissions,is_system,is_active,version)
            VALUES ('eligibility-new-raw','New raw','readonly','{}'::jsonb,false,true,1)"""))
        db.commit()
        assert db.scalar(select(SysUser.authorization_legacy_seed_eligible).where(
            SysUser.username == 'eligibility-new-raw')) is False
        assert db.scalar(select(SysRoleTemplate.authorization_legacy_seed_eligible).where(
            SysRoleTemplate.code == 'eligibility-new-raw')) is False
    finally:
        db.rollback()
        command.upgrade(config, 'head')


def test_canonical_empty_creation_and_legacy_template_copy_never_inherit_seed_eligibility(db, s1_client):
    from app.models.system import SysRoleTemplate

    actor = _user(db, 'eligibility-admin', 'admin')
    state = _read(s1_client, actor, actor)
    common = {'schema_version': 1, 'catalog_version': state['catalog_version']}
    created = _json(s1_client.post('/api/accounts', headers=_headers(actor), json={
        **common, 'profile': {'username': 'canonical-empty', 'password': 'synthetic-password'},
        'authorization': {'kind': 'none'}}), 201)
    user = db.scalar(select(SysUser).where(SysUser.username == 'canonical-empty'))
    assert user.authorization_legacy_seed_eligible is False
    assert user.authorization_profile['resource_overrides'] == {}
    old = SysRoleTemplate(code='legacy-eligible', name='Legacy', base_role='boss',
                          permissions={'page_profit': True}, authorization_bundle={},
                          authorization_legacy_seed_eligible=True, version=9)
    db.add(old)
    db.commit()
    copied = _json(s1_client.post('/api/role-templates', headers=_headers(actor), json={
        **common, 'metadata': {'code': 'canonical-copy', 'name': 'Canonical empty copy'},
        'authorization': {'kind': 'copy_template', 'code': old.code, 'expected_version': 9}}), 201)
    template = db.scalar(select(SysRoleTemplate).where(SysRoleTemplate.code == 'canonical-copy'))
    assert template.authorization_legacy_seed_eligible is False
    assert template.authorization_bundle == {}
    assert template.permissions == {} and template.base_role == 'readonly'
    assert 'authorization_legacy_seed_eligible' not in str(created)
    assert 'authorization_legacy_seed_eligible' not in str(copied)
    assert old.authorization_legacy_seed_eligible is True


@pytest.mark.parametrize('eligible', [False, True])
def test_seed_eligibility_is_neither_runtime_allow_nor_rewritten_by_canonical_updates(db, s1_client, eligible):
    actor = _user(db, 'eligibility-runtime-admin', 'admin')
    user = _user(db, 'eligibility-runtime-target', authorization_legacy_seed_eligible=eligible,
                 template_perms={'page_sales_analysis': True})
    snapshot = _json(s1_client.get('/api/authz/me', headers=_headers(user)))
    assert snapshot['resources']['sales_analysis']['decisions']['read']['effect'] == 'deny'
    state = _read(s1_client, actor, user)
    _json(_write(s1_client, actor, user, _command(state)))
    db.refresh(user)
    assert user.authorization_legacy_seed_eligible is eligible
    allowed = _json(s1_client.get('/api/authz/me', headers=_headers(user)))
    assert allowed['resources']['sales_analysis']['decisions']['read']['effect'] == 'allow'


@pytest.mark.parametrize('where', ['root', 'profile', 'template_metadata'])
def test_canonical_client_cannot_choose_legacy_seed_eligibility(db, s1_client, where):
    actor = _user(db, 'eligibility-injection-admin', 'admin')
    state = _read(s1_client, actor, actor)
    body = {'schema_version': 1, 'catalog_version': state['catalog_version'],
            'profile': {'username': 'eligibility-injected', 'password': 'synthetic-password'},
            'authorization': {'kind': 'none'}}
    path = '/api/accounts'
    if where == 'root':
        body['authorization_legacy_seed_eligible'] = True
    elif where == 'profile':
        body['profile']['authorization_legacy_seed_eligible'] = True
    else:
        path = '/api/role-templates'
        body.pop('profile')
        body['metadata'] = {'code': 'eligibility-injected', 'name': 'Injected',
                            'authorization_legacy_seed_eligible': True}
        body['authorization'] = {'kind': 'explicit', 'modules': {}}
    _json(s1_client.post(path, headers=_headers(actor), json=body), 422)
    assert db.scalar(select(func.count()).select_from(SysUser).where(
        SysUser.username == 'eligibility-injected')) == 0
    from app.models.system import SysRoleTemplate
    assert db.scalar(select(func.count()).select_from(SysRoleTemplate).where(
        SysRoleTemplate.code == 'eligibility-injected')) == 0


@pytest.mark.parametrize('kind', ['account', 'template'])
def test_canonical_origin_without_cutover_cannot_be_erased_by_schema_downgrade(db, s1_client, monkeypatch, kind):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect
    from app.models.authorization import SysAuthorizationCutover
    from app.models.system import SysRoleTemplate

    monkeypatch.setattr(get_settings(), 'authz_sales_analysis_enabled', False)
    db.execute(delete(SysAuthorizationCutover))
    db.commit()
    actor = _user(db, 'eligibility-downgrade-admin', 'admin', authorization_legacy_seed_eligible=True)
    state = _read(s1_client, actor, actor)
    common = {'schema_version': 1, 'catalog_version': state['catalog_version']}
    if kind == 'account':
        _json(s1_client.post('/api/accounts', headers=_headers(actor), json={
            **common, 'profile': {'username': 'downgrade-canonical', 'password': 'synthetic-password'},
            'authorization': {'kind': 'none'}}), 201)
        model, key = SysUser, SysUser.username == 'downgrade-canonical'
    else:
        _json(s1_client.post('/api/role-templates', headers=_headers(actor), json={
            **common, 'metadata': {'code': 'downgrade-canonical', 'name': 'Empty canonical'},
            'authorization': {'kind': 'explicit', 'modules': {}}}), 201)
        model, key = SysRoleTemplate, SysRoleTemplate.code == 'downgrade-canonical'
    assert db.scalar(select(func.count()).select_from(SysAuthorizationCutover)) == 0
    assert db.scalar(select(model.authorization_legacy_seed_eligible).where(key)) is False
    backend = Path(__file__).parents[1]
    config = Config(str(backend / 'alembic.ini'))
    config.set_main_option('script_location', str(backend / 'alembic'))
    db.rollback()
    try:
        with pytest.raises(RuntimeError):
            command.downgrade(config, 'f1a3c5e7b9d2')
        assert 'authorization_legacy_seed_eligible' in {
            c['name'] for c in inspect(db.get_bind()).get_columns(model.__tablename__)}
        assert db.scalar(select(model.authorization_legacy_seed_eligible).where(key)) is False
    finally:
        db.rollback()
        command.upgrade(config, 'head')


def test_downgrade_waits_for_uncommitted_canonical_origin_before_running_guards(db):
    """A real writer commits while downgrade waits; the later guard sees false."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect, text

    backend = Path(__file__).parents[1]
    config = Config(str(backend / 'alembic.ini'))
    config.set_main_option('script_location', str(backend / 'alembic'))
    db.rollback()
    engine = db.get_bind()
    # Isolate S1's own guard from foreign-key locks in later PN revisions.
    command.downgrade(config, 'a7c2e9f4b6d1')
    try:
        with ThreadPoolExecutor(max_workers=1) as executor, engine.connect() as writer:
            transaction = writer.begin()
            writer_pid = writer.scalar(text('SELECT pg_backend_pid()'))
            starting_revision = writer.scalar(text('SELECT version_num FROM alembic_version'))
            writer.execute(text("""INSERT INTO sys_user (username,password_hash,role,
                authorization_legacy_seed_eligible) VALUES
                ('concurrent-canonical-origin','unused','readonly',false)"""))
            future = executor.submit(command.downgrade, config, 'f1a3c5e7b9d2')
            try:
                deadline = time.monotonic() + 10
                blocked_query = None
                while time.monotonic() < deadline:
                    writer.execute(text('SELECT pg_stat_clear_snapshot()'))
                    blocked_query = writer.scalar(text("""SELECT query FROM pg_stat_activity
                        WHERE datname=current_database() AND pid <> pg_backend_pid()
                          AND :writer_pid = ANY(pg_blocking_pids(pid))
                          AND wait_event_type='Lock' LIMIT 1"""), {'writer_pid': writer_pid})
                    if blocked_query:
                        break
                    if future.done():
                        pytest.fail(f'Downgrade completed before waiting for origin writer: {future.exception()}')
                    time.sleep(.01)
                assert blocked_query is not None, 'No real PostgreSQL lock wait observed'
                assert 'LOCK TABLE sys_authorization_cutover' in blocked_query
                assert 'ACCESS EXCLUSIVE' in blocked_query
                transaction.commit()
                with pytest.raises(RuntimeError, match='non-legacy authorization origin'):
                    future.result(timeout=10)
            finally:
                if transaction.is_active:
                    transaction.rollback()
                try:
                    future.result(timeout=10)
                except RuntimeError:
                    pass
        assert 'authorization_legacy_seed_eligible' in {
            column['name'] for column in inspect(engine).get_columns('sys_user')}
        assert db.scalar(text("SELECT authorization_legacy_seed_eligible FROM sys_user "
                              "WHERE username='concurrent-canonical-origin'")) is False
        assert db.scalar(text('SELECT version_num FROM alembic_version')) == starting_revision
    finally:
        db.rollback()
        command.upgrade(config, 'head')
