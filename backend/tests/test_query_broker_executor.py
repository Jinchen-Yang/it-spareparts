"""Read-only execution, TOCTOU, budget and evidence boundaries."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.agent.query_broker.broker import build_query_plan
from app.agent.query_broker.egress import ProviderEgressSnapshot
from app.agent.query_broker.environment import (
    AgentDatabaseSettings,
    validate_dsn_separation,
)
from app.agent.query_broker.errors import QueryBrokerError
from app.agent.query_broker.executor import (
    ExecutionContext,
    QueryBudget,
    QueryExecutor,
    SealedEvidence,
)
from app.agent.query_broker.ir import QueryIR
from app.agent.query_broker.registry import DATASETS, AuthorizationSnapshot

TODAY = date(2026, 8, 10)
SENTINEL = "supplier-secret-canary"


def _authz(**overrides) -> AuthorizationSnapshot:
    values = {
        "subject": "user-17",
        "tenant_id": "tenant-a",
        "role": "purchaser",
        "permissions": frozenset({
            "page_chat",
            "page_parts",
            "page_purchases",
            "data_supplier",
            "data_purchase_cost",
        }),
        "authz_version": 11,
        "own_customers_only": False,
        "row_subject": None,
    }
    values.update(overrides)
    return AuthorizationSnapshot(**values)


def _egress(
    authz: AuthorizationSnapshot | None = None,
    **overrides,
) -> ProviderEgressSnapshot:
    authority = authz or _authz()
    values = {
        "profile_ref": "private-gpu/v1",
        "policy_version": 3,
        "policy_fingerprint": "b" * 64,
        "authz_fingerprint": authority.fingerprint(),
        "allowed_purposes": frozenset({"query.registry", "query.result"}),
        "allowed_field_refs": frozenset(
            f"{dataset.name}.{field.name}"
            for dataset in DATASETS.values()
            for field in dataset.fields.values()
        ),
        "allowed_sensitivities": frozenset({
            "business_confidential",
            "business_restricted",
        }),
    }
    values.update(overrides)
    return ProviderEgressSnapshot(**values)


def _plan(
    authz: AuthorizationSnapshot | None = None,
    egress: ProviderEgressSnapshot | None = None,
    **ir_overrides,
):
    body = {
        "version": "query-ir/v1",
        "dataset": "purchase_activity_v1",
        "time_range": {"start": TODAY - timedelta(days=30), "end": TODAY},
        "dimensions": ["day", "part_id"],
        "metrics": ["qty"],
        "filters": [],
        "order_by": [],
        "limit": 50,
    }
    body.update(ir_overrides)
    authority = authz or _authz()
    return build_query_plan(
        QueryIR.model_validate(body),
        authority,
        egress or _egress(authority),
        today=TODAY,
    )


class _Tx:
    def __init__(self):
        self.rolled_back = False

    def rollback(self):
        self.rolled_back = True


class _Result:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = list(rows or [])
        self._offset = 0
        self.fetch_sizes: list[int] = []

    def scalar_one(self):
        return self._scalar

    def fetchmany(self, size):
        self.fetch_sizes.append(size)
        batch = self._rows[self._offset:self._offset + size]
        self._offset += len(batch)
        return batch

    def mappings(self):
        return self


class _Connection:
    def __init__(self, *, rows=None, explain=None, fail_on_query=False):
        self.rows = list(rows or [])
        self.explain = explain or [{
            "Plan": {"Node Type": "Seq Scan", "Total Cost": 10, "Plan Rows": 10, "Plan Width": 32}
        }]
        self.fail_on_query = fail_on_query
        self.static_sql: list[str] = []
        self.executed: list[tuple[str, dict]] = []
        self.tx = _Tx()
        self.closed = False
        self.execution_options_seen: list[dict] = []
        self.data_result = _Result(rows=self.rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True

    def begin(self):
        return self.tx

    def exec_driver_sql(self, sql):
        self.static_sql.append(sql)

    def execution_options(self, **kwargs):
        self.execution_options_seen.append(kwargs)
        return self

    def execute(self, statement, params=None):
        sql = str(statement)
        values = dict(params or {})
        self.executed.append((sql, values))
        if sql.startswith("SELECT set_config"):
            return _Result(rows=[])
        if sql.startswith("EXPLAIN"):
            return _Result(scalar=self.explain)
        if self.fail_on_query:
            raise RuntimeError(f"driver leak {SENTINEL}")
        return self.data_result


class _Engine:
    def __init__(self, connection):
        self.connection = connection
        self.connect_count = 0

    def connect(self):
        self.connect_count += 1
        return self.connection


class _Probe:
    def __init__(self, *, ready=True):
        self.ready = ready
        self.calls = 0

    def ensure_ready(self):
        self.calls += 1
        if not self.ready:
            raise QueryBrokerError("QUERY_BROKER_UNAVAILABLE")


class _Sealer:
    def __init__(self):
        self.payloads: list[dict] = []

    def seal(self, *, purpose, payload):
        assert purpose == "query.evidence"
        self.payloads.append(payload)
        return SealedEvidence(
            evidence_ref=f"evidence/{payload['execution_id']}",
            evidence_digest="a" * 64,
            envelope={"header": {"purpose": purpose}, "payload": payload, "mac": "server-only"},
        )


def _context(authz=None, egress=None) -> ExecutionContext:
    authority = authz or _authz()
    return ExecutionContext(
        execution_id=uuid4(),
        task_ref="task/9ccf",
        step_ref="step/3",
        planned_authz=authority,
        planned_egress=egress or _egress(authority),
    )


def _executor(
    connection,
    *,
    current_authz=None,
    current_egress=None,
    probe=None,
    telemetry=None,
    budget=None,
):
    sealer = _Sealer()
    executor = QueryExecutor(
        engine=_Engine(connection),
        authority_loader=lambda subject: current_authz or _authz(subject=subject),
        egress_loader=lambda _profile: current_egress
        or _egress(current_authz or _authz()),
        environment_probe=probe or _Probe(),
        evidence_sealer=sealer,
        telemetry_sink=telemetry,
        budget=budget or QueryBudget(),
    )
    return executor, sealer


@pytest.mark.parametrize(
    "kwargs",
    [
        {"statement_timeout_ms": 2001},
        {"lock_timeout_ms": 0},
        {"idle_transaction_timeout_ms": -1},
        {"max_result_rows": 201},
        {"max_result_bytes": 256 * 1024 + 1},
        {"max_cell_bytes": 8 * 1024 + 1},
        {"fetch_batch_rows": 0},
        {"fetch_batch_rows": 2},
        {"fetch_batch_rows": True},
    ],
)
def test_query_budget_can_only_tighten_hard_limits(kwargs):
    with pytest.raises(ValueError):
        QueryBudget(**kwargs)


def test_tighter_timeout_budget_is_the_exact_executed_and_evidenced_value():
    connection = _Connection(rows=[])
    budget = QueryBudget(statement_timeout_ms=500, lock_timeout_ms=50,
                         idle_transaction_timeout_ms=800)
    executor, sealer = _executor(connection, budget=budget)
    executor.execute(_plan(), _context())
    assert "SET LOCAL statement_timeout = '500ms'" in connection.static_sql
    assert "SET LOCAL lock_timeout = '50ms'" in connection.static_sql
    assert "SET LOCAL idle_in_transaction_session_timeout = '800ms'" in connection.static_sql
    assert sealer.payloads[0]["budget"]["statement_timeout_ms"] == 500
    assert sealer.payloads[0]["budget"]["lock_timeout_ms"] == 50
    assert sealer.payloads[0]["budget"]["idle_transaction_timeout_ms"] == 800
    assert sealer.payloads[0]["budget"]["max_result_columns"] == 16
    assert sealer.payloads[0]["budget"]["fetch_batch_rows"] == 1


@pytest.mark.parametrize(
    "agent_url,main_url,code",
    [
        ("", "postgresql+psycopg://app:pw@db:5432/main", "AGENT_DSN_MISSING"),
        (
            "postgresql+psycopg://app:other@db:5432/main",
            "postgresql+psycopg://app:pw@db:5432/main",
            "AGENT_DSN_REUSES_APP_IDENTITY",
        ),
        (
            "postgresql+psycopg://reader:pw@db:5432/main",
            "postgresql+psycopg://app:pw@db:5432/main",
            "AGENT_READER_IDENTITY_INVALID",
        ),
        (
            "postgresql+psycopg://app:other@other-host:5432/other",
            "postgresql+psycopg://app:pw@db:5432/main",
            "AGENT_DSN_REUSES_APP_IDENTITY",
        ),
        (
            "postgresql+psycopg://agent_reader:pw@db:5432/main?options=-csearch_path%3Dpublic",
            "postgresql+psycopg://app:pw@db:5432/main",
            "AGENT_DSN_INVALID",
        ),
    ],
)
def test_agent_dsn_is_missing_or_separate_exact_reader(agent_url, main_url, code):
    with pytest.raises(QueryBrokerError) as exc:
        validate_dsn_separation(
            AgentDatabaseSettings(enabled=True, agent_database_url=agent_url),
            main_url,
        )
    assert exc.value.code == code


def test_disabled_flag_fails_before_any_dsn_detail():
    with pytest.raises(QueryBrokerError) as exc:
        validate_dsn_separation(
            AgentDatabaseSettings(
                enabled=False,
                agent_database_url="postgresql+psycopg://agent_reader:pw@db:5432/main",
            ),
            "postgresql+psycopg://app:pw@db:5432/main",
        )
    assert exc.value.code == "QUERY_BROKER_DISABLED"


def test_agent_dsn_password_is_redacted_from_settings_repr():
    settings = AgentDatabaseSettings(
        enabled=True,
        agent_database_url=f"postgresql+psycopg://agent_reader:{SENTINEL}@db:5432/main",
    )
    assert SENTINEL not in repr(settings)


def test_authority_revocation_happens_before_engine_connect_or_sql():
    planned = _authz()
    current = planned.model_copy(update={
        "permissions": planned.permissions - {"page_purchases"},
        "authz_version": 12,
    })
    connection = _Connection(rows=[])
    engine = _Engine(connection)
    executor = QueryExecutor(
        engine=engine,
        authority_loader=lambda _subject: current,
        egress_loader=lambda _profile: _egress(planned),
        environment_probe=_Probe(),
        evidence_sealer=_Sealer(),
    )
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(_plan(planned), _context(planned))
    assert exc.value.code == "AUTHORIZATION_CHANGED"
    assert engine.connect_count == 0
    assert connection.executed == []


def test_egress_snapshot_is_frozen_into_authorized_compiler_and_plan():
    authz = _authz()
    egress = _egress(authz)
    plan = _plan(authz, egress)
    assert plan.egress_snapshot == egress
    assert plan.authorized.egress_snapshot == egress
    assert plan.egress_fingerprint == egress.fingerprint()
    assert plan.authorized.egress_fingerprint == egress.fingerprint()
    assert plan.compiled.egress_fingerprint == egress.fingerprint()


def test_egress_policy_is_reloaded_and_exactly_compared_before_agent_db_connection():
    authz = _authz()
    planned = _egress(authz)
    changed = planned.model_copy(update={
        "policy_version": planned.policy_version + 1,
        "policy_fingerprint": "c" * 64,
    })
    connection = _Connection()
    engine = _Engine(connection)
    probe = _Probe()
    egress_calls: list[str] = []

    def load_egress(profile_ref):
        egress_calls.append(profile_ref)
        return changed

    executor = QueryExecutor(
        engine=engine,
        authority_loader=lambda _subject: authz,
        egress_loader=load_egress,
        environment_probe=probe,
        evidence_sealer=_Sealer(),
    )
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(_plan(authz, planned), _context(authz, planned))
    assert exc.value.code == "PROVIDER_EGRESS_CHANGED"
    assert egress_calls == ["private-gpu/v1"]
    assert probe.calls == 0
    assert engine.connect_count == 0


def test_egress_backend_failure_is_value_free_and_precedes_agent_db_connection():
    authz = _authz()
    planned = _egress(authz)
    engine = _Engine(_Connection())

    def unavailable(_profile_ref):
        raise RuntimeError(SENTINEL)

    executor = QueryExecutor(
        engine=engine,
        authority_loader=lambda _subject: authz,
        egress_loader=unavailable,
        environment_probe=_Probe(),
        evidence_sealer=_Sealer(),
    )
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(_plan(authz, planned), _context(authz, planned))
    assert exc.value.code == "PROVIDER_EGRESS_UNAVAILABLE"
    assert SENTINEL not in str(exc.value)
    assert engine.connect_count == 0


def test_same_type_parameter_substitution_is_denied_before_database_connect():
    plan = _plan(filters=[{"field": "pn_std", "operator": "eq", "value": "PN-A"}])
    poisoned_params = dict(plan.compiled.params)
    poisoned_params["filter_0"] = "PN-B"
    # Fingerprint intentionally stays equal because it is value-free.
    poisoned_compiled = plan.compiled.model_copy(update={"params": poisoned_params})
    assert poisoned_compiled.compiler_fingerprint == plan.compiled.compiler_fingerprint
    poisoned_plan = plan.model_copy(update={"compiled": poisoned_compiled})
    connection = _Connection()
    engine = _Engine(connection)
    executor = QueryExecutor(
        engine=engine,
        authority_loader=lambda subject: _authz(subject=subject),
        egress_loader=lambda _profile: _egress(),
        environment_probe=_Probe(),
        evidence_sealer=_Sealer(),
    )
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(poisoned_plan, _context())
    assert exc.value.code == "AUTHORIZATION_CHANGED"
    assert engine.connect_count == 0


def test_stale_or_tampered_authorized_semantics_are_denied_before_database():
    plan = _plan()
    poisoned_authorized = plan.authorized.model_copy(
        update={"caveats": (*plan.authorized.caveats, SENTINEL)}
    )
    poisoned_plan = plan.model_copy(update={"authorized": poisoned_authorized})
    connection = _Connection()
    engine = _Engine(connection)
    executor = QueryExecutor(
        engine=engine,
        authority_loader=lambda subject: _authz(subject=subject),
        egress_loader=lambda _profile: _egress(),
        environment_probe=_Probe(),
        evidence_sealer=_Sealer(),
    )
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(poisoned_plan, _context())
    assert exc.value.code == "AUTHORIZATION_CHANGED"
    assert engine.connect_count == 0


def test_compiled_parameter_mapping_is_immutable_and_repr_hides_filter_canary():
    plan = _plan(filters=[{"field": "pn_std", "operator": "eq", "value": SENTINEL}])
    with pytest.raises(TypeError):
        plan.compiled.params["filter_0"] = "changed"
    assert SENTINEL not in repr(plan)
    assert SENTINEL not in repr(plan.compiled)


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "tenant-b", "authz_version": 12},
        {"row_subject": "another-salesperson", "authz_version": 12},
        {"subject": "other-user", "authz_version": 12},
    ],
)
def test_scope_or_tenant_change_is_not_reused(change):
    planned = _authz(row_subject="salesperson-17")
    current = planned.model_copy(update=change)
    connection = _Connection()
    executor, _ = _executor(connection, current_authz=current)
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(_plan(planned), _context(planned))
    assert exc.value.code == "AUTHORIZATION_CHANGED"
    assert connection.executed == []


def test_read_only_transaction_and_context_are_installed_before_explain_and_query():
    connection = _Connection(rows=[{"day": TODAY, "part_id": 7, "qty": Decimal("2.5")}])
    executor, sealer = _executor(connection)
    result = executor.execute(_plan(), _context())

    assert connection.static_sql == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        "SET LOCAL search_path = pg_catalog, agent_semantic",
        "SET LOCAL row_security = on",
        "SET LOCAL statement_timeout = '2000ms'",
        "SET LOCAL lock_timeout = '200ms'",
        "SET LOCAL idle_in_transaction_session_timeout = '3000ms'",
        "SET LOCAL work_mem = '4MB'",
        "SET LOCAL max_parallel_workers_per_gather = 0",
    ]
    assert connection.executed[0][0].startswith("SELECT set_config")
    assert connection.executed[1][0].startswith("EXPLAIN")
    assert connection.executed[2][0].startswith("SELECT")
    context_params = connection.executed[0][1]
    assert context_params == {
        "subject": "user-17",
        "tenant": "tenant-a",
        "dataset": "purchase_activity_v1",
        "page_permissions": "page_chat,page_parts,page_purchases",
        "data_permissions": "data_purchase_cost,data_supplier",
        "own_only": "false",
        "row_subject": "",
        "authz_version": "11",
        "registry_version": "semantic-registry/v1",
    }
    assert connection.tx.rolled_back is True
    assert connection.closed is True
    assert result.rows == ({"day": TODAY.isoformat(), "part_id": 7, "qty": "2.5"},)
    assert sealer.payloads[0]["result_count"] == 1


def test_query_error_is_never_reported_as_empty_or_zero_and_driver_detail_is_hidden():
    events: list[dict] = []
    connection = _Connection(fail_on_query=True)
    executor, sealer = _executor(connection, telemetry=events.append)
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(_plan(), _context())
    assert exc.value.code == "QUERY_EXECUTION_FAILED"
    assert SENTINEL not in str(exc.value)
    assert sealer.payloads == []
    assert connection.tx.rolled_back is True
    assert events[-1]["status"] == "error"
    assert events[-1]["code"] == "QUERY_EXECUTION_FAILED"
    assert SENTINEL not in json.dumps(events)


@pytest.mark.parametrize(
    "plan,code",
    [
        ([{"Plan": {"Total Cost": 50001, "Plan Rows": 1, "Plan Width": 1}}], "QUERY_PLAN_COST_EXCEEDED"),
        ([{"Plan": {"Total Cost": 1, "Plan Rows": 100001, "Plan Width": 1}}], "QUERY_PLAN_ROWS_EXCEEDED"),
        ([{"Plan": {"Total Cost": 1, "Plan Rows": 100000, "Plan Width": 336}}], "QUERY_PLAN_BYTES_EXCEEDED"),
        ([{"Plan": {"Total Cost": 1, "Plan Width": 1}}], "QUERY_EXECUTION_FAILED"),
        ([{"Plan": {"Total Cost": 1, "Plan Rows": 1}}], "QUERY_EXECUTION_FAILED"),
    ],
)
def test_explain_budget_rejects_before_data_query(plan, code):
    connection = _Connection(explain=plan)
    executor, _ = _executor(connection)
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(_plan(), _context())
    assert exc.value.code == code
    assert len(connection.executed) == 2  # set_config + EXPLAIN; no data SELECT
    assert connection.tx.rolled_back is True


def test_row_and_byte_budgets_truncate_incrementally_without_overrun():
    rows = [
        {"day": TODAY, "part_id": index, "pn_std": "x" * 200, "qty": Decimal(1)}
        for index in range(201)
    ]
    connection = _Connection(rows=rows)
    executor, sealer = _executor(
        connection,
        budget=QueryBudget(max_result_bytes=10_000, max_cell_bytes=1024),
    )
    result = executor.execute(_plan(dimensions=["day", "part_id", "pn_std"], limit=200), _context())
    assert result.truncated is True
    assert 0 < len(result.rows) <= 200
    assert len(json.dumps(result.model_payload(), ensure_ascii=False).encode()) <= 10_000
    assert all(len(row["pn_std"].encode()) <= 1024 for row in result.rows)
    assert connection.data_result.fetch_sizes
    assert set(connection.data_result.fetch_sizes) == {1}
    assert sealer.payloads[0]["truncated"] is True


def test_oversized_string_cell_is_rejected_not_silently_rewritten():
    connection = _Connection(rows=[{
        "day": TODAY,
        "part_id": 1,
        "pn_std": "x" * 1025,
        "qty": Decimal(1),
    }])
    executor, sealer = _executor(
        connection,
        budget=QueryBudget(max_cell_bytes=1024),
    )
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(_plan(dimensions=["day", "part_id", "pn_std"]), _context())
    assert exc.value.code == "QUERY_RESULT_INVALID"
    assert sealer.payloads == []


@pytest.mark.parametrize("value", ["PN\x00SECRET", "PN\u202eSECRET", "PN\ud800SECRET"])
def test_result_unicode_controls_are_rejected_before_evidence(value):
    connection = _Connection(rows=[{
        "day": TODAY,
        "part_id": 1,
        "pn_std": value,
        "qty": Decimal(1),
    }])
    executor, sealer = _executor(connection)
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(_plan(dimensions=["day", "part_id", "pn_std"]), _context())
    assert exc.value.code == "QUERY_RESULT_INVALID"
    assert sealer.payloads == []


def test_unexpected_hidden_result_column_fails_closed_and_never_reaches_evidence():
    connection = _Connection(rows=[{
        "day": TODAY,
        "part_id": 1,
        "qty": Decimal(1),
        "supplier_secret": SENTINEL,
    }])
    events: list[dict] = []
    executor, sealer = _executor(connection, telemetry=events.append)
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(_plan(), _context())
    assert exc.value.code == "QUERY_RESULT_INVALID"
    assert sealer.payloads == []
    assert SENTINEL not in json.dumps(events)


def test_alias_correct_but_registry_type_wrong_result_is_rejected():
    connection = _Connection(rows=[{
        "day": TODAY,
        "part_id": 1,
        "qty": SENTINEL,
    }])
    events: list[dict] = []
    executor, sealer = _executor(connection, telemetry=events.append)
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(_plan(), _context())
    assert exc.value.code == "QUERY_RESULT_INVALID"
    assert sealer.payloads == []
    assert SENTINEL not in json.dumps(events)


def test_server_side_digest_envelope_and_authz_never_enter_model_payload():
    connection = _Connection(rows=[{"day": TODAY, "part_id": 1, "qty": Decimal(3)}])
    executor, sealer = _executor(connection)
    result = executor.execute(_plan(), _context())
    public = result.model_payload()
    public_text = json.dumps(public, ensure_ascii=False)
    assert UUID(public["result_ref"].removeprefix("query-result/"))
    assert "digest" not in public_text
    assert "mac" not in public_text
    assert "authz" not in public_text
    assert "tenant-a" not in public_text
    assert "user-17" not in public_text
    assert "private-gpu" not in public_text
    assert "business_restricted" not in public_text
    assert "SELECT" not in public_text
    assert result.sealed_evidence.envelope["mac"] == "server-only"
    assert sealer.payloads[0]["result_digest"]
    assert sealer.payloads[0]["query_ir_ref"].startswith("query-ir/")
    assert sealer.payloads[0]["provider_egress"] == _egress().evidence_binding()


def test_result_and_evidence_are_immutable_after_sealing_and_repr_is_redacted():
    connection = _Connection(rows=[{"day": TODAY, "part_id": 1, "qty": Decimal(3)}])
    executor, _ = _executor(connection)
    result = executor.execute(_plan(), _context())
    before = result.model_payload()
    with pytest.raises(TypeError):
        result.rows[0]["qty"] = SENTINEL
    with pytest.raises(TypeError):
        result.sealed_evidence.envelope["mac"] = SENTINEL
    assert result.model_payload() == before
    assert SENTINEL not in repr(result)
    assert "server-only" not in repr(result)
    assert "server-only" not in repr(result.sealed_evidence)
    assert "sealed_evidence" not in result.model_dump()


def test_telemetry_contains_only_value_free_shape():
    events: list[dict] = []
    plan = _plan(filters=[{"field": "pn_std", "operator": "eq", "value": SENTINEL}])
    connection = _Connection(rows=[{"day": TODAY, "part_id": 1, "qty": Decimal(3)}])
    executor, _ = _executor(connection, telemetry=events.append)
    executor.execute(plan, _context())
    blob = json.dumps(events, ensure_ascii=False)
    assert SENTINEL not in blob
    assert "purchase_activity_v1" in blob
    assert events[-1].keys() == {"dataset", "rows", "cols", "latency_ms", "status", "code"}


def test_environment_probe_failure_happens_after_authority_but_before_business_sql():
    authority_calls = 0

    def authority(_subject):
        nonlocal authority_calls
        authority_calls += 1
        return _authz()

    connection = _Connection()
    engine = _Engine(connection)
    executor = QueryExecutor(
        engine=engine,
        authority_loader=authority,
        egress_loader=lambda _profile: _egress(),
        environment_probe=_Probe(ready=False),
        evidence_sealer=_Sealer(),
    )
    with pytest.raises(QueryBrokerError) as exc:
        executor.execute(_plan(), _context())
    assert exc.value.code == "QUERY_BROKER_UNAVAILABLE"
    assert authority_calls == 1
    assert engine.connect_count == 0
