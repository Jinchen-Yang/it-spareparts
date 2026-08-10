"""Bounded read-only PostgreSQL execution and server-only Query Evidence."""

from __future__ import annotations

import hashlib
import json
import math
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text

from app.agent.query_broker.ast_guard import validate_compiled_sql
from app.agent.query_broker.broker import QueryPlan
from app.agent.query_broker.compiler import compile_query
from app.agent.query_broker.errors import QueryBrokerError
from app.agent.query_broker.frozen import FrozenDict, deep_thaw
from app.agent.query_broker.registry import (
    REGISTRY_VERSION,
    AuthorizationSnapshot,
    AuthorizedQuery,
    FieldSpec,
    authorize_query,
)

_HARD_MAX_STATEMENT_MS = 2000
_HARD_MAX_LOCK_MS = 200
_HARD_MAX_IDLE_MS = 3000
_SET_CONTEXT_SQL = """SELECT set_config('app.agent_subject', :subject, true),
       set_config('app.agent_tenant', :tenant, true),
       set_config('app.agent_dataset', :dataset, true),
       set_config('app.agent_page_permissions', :page_permissions, true),
       set_config('app.agent_data_permissions', :data_permissions, true),
       set_config('app.agent_own_only', :own_only, true),
       set_config('app.agent_row_subject', :row_subject, true),
       set_config('app.agent_authz_version', :authz_version, true),
       set_config('app.agent_registry_version', :registry_version, true)"""


@dataclass(frozen=True, slots=True)
class QueryBudget:
    statement_timeout_ms: int = 2000
    lock_timeout_ms: int = 200
    idle_transaction_timeout_ms: int = 3000
    max_root_total_cost: int = 50_000
    max_plan_rows: int = 100_000
    max_estimated_scan_bytes: int = 32 * 1024 * 1024
    max_result_columns: int = 16
    max_result_rows: int = 200
    max_result_bytes: int = 256 * 1024
    max_cell_bytes: int = 8 * 1024
    # One row at a time prevents a server-side cursor from materializing up to
    # N oversized text cells before the per-cell/JSON budget can reject them.
    fetch_batch_rows: int = 1

    def __post_init__(self) -> None:
        bounded = {
            "statement_timeout_ms": (self.statement_timeout_ms, _HARD_MAX_STATEMENT_MS),
            "lock_timeout_ms": (self.lock_timeout_ms, _HARD_MAX_LOCK_MS),
            "idle_transaction_timeout_ms": (self.idle_transaction_timeout_ms, _HARD_MAX_IDLE_MS),
            "max_root_total_cost": (self.max_root_total_cost, 50_000),
            "max_plan_rows": (self.max_plan_rows, 100_000),
            "max_estimated_scan_bytes": (self.max_estimated_scan_bytes, 32 * 1024 * 1024),
            "max_result_columns": (self.max_result_columns, 16),
            "max_result_rows": (self.max_result_rows, 200),
            "max_result_bytes": (self.max_result_bytes, 256 * 1024),
            "max_cell_bytes": (self.max_cell_bytes, 8 * 1024),
            "fetch_batch_rows": (self.fetch_batch_rows, 1),
        }
        for value, hard_max in bounded.values():
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= hard_max:
                raise ValueError("QueryBudget values must be positive integers within hard maxima")


def _transaction_settings(budget: QueryBudget) -> tuple[str, ...]:
    """Identifiers are static; only prevalidated bounded integers are rendered."""

    return (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        # Resolve built-ins before any application namespace.  Physical views
        # are still schema-qualified; this removes an avoidable function /
        # aggregate shadowing surface if the schema ACL ever drifts.
        "SET LOCAL search_path = pg_catalog, agent_semantic",
        "SET LOCAL row_security = on",
        f"SET LOCAL statement_timeout = '{budget.statement_timeout_ms}ms'",
        f"SET LOCAL lock_timeout = '{budget.lock_timeout_ms}ms'",
        f"SET LOCAL idle_in_transaction_session_timeout = '{budget.idle_transaction_timeout_ms}ms'",
        "SET LOCAL work_mem = '4MB'",
        "SET LOCAL temp_file_limit = 0",
        "SET LOCAL max_parallel_workers_per_gather = 0",
    )


class ExecutionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: UUID
    task_ref: str = Field(min_length=1, max_length=128, repr=False)
    step_ref: str = Field(min_length=1, max_length=128, repr=False)
    provider_profile_ref: str = Field(min_length=1, max_length=128, repr=False)
    planned_authz: AuthorizationSnapshot = Field(repr=False)


class SealedEvidence(BaseModel):
    """Server-only envelope.  Generic serialization excludes the envelope bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    evidence_ref: str = Field(repr=False)
    evidence_digest: str = Field(repr=False)
    envelope: FrozenDict = Field(exclude=True, repr=False)

    @field_validator("envelope", mode="before")
    @classmethod
    def _freeze_envelope(cls, value: Mapping[str, Any] | FrozenDict) -> FrozenDict:
        return value if isinstance(value, FrozenDict) else FrozenDict(value)


class QueryExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    result_ref: str
    dataset: str
    output_fields: tuple[str, ...]
    rows: tuple[FrozenDict, ...] = Field(repr=False)
    row_count: int
    truncated: bool
    caveats: tuple[str, ...]
    sealed_evidence: SealedEvidence = Field(exclude=True, repr=False)

    @field_validator("rows", mode="before")
    @classmethod
    def _freeze_rows(cls, value: Any) -> tuple[FrozenDict, ...]:
        return tuple(item if isinstance(item, FrozenDict) else FrozenDict(item) for item in value)

    def model_payload(self) -> dict[str, Any]:
        """The only supported model-facing projection; no digest/MAC/authz/SQL."""

        return {
            "result_ref": self.result_ref,
            "dataset": self.dataset,
            "fields": list(self.output_fields),
            "rows": [deep_thaw(row) for row in self.rows],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "caveats": list(self.caveats),
        }


class EvidenceSealer(Protocol):
    def seal(self, *, purpose: str, payload: dict[str, Any]) -> SealedEvidence: ...


class EnvironmentProbe(Protocol):
    def ensure_ready(self) -> None: ...


AuthorityLoader = Callable[[str], AuthorizationSnapshot | None]
TelemetrySink = Callable[[dict[str, Any]], None]


def _emit(
    sink: TelemetrySink | None,
    *,
    dataset: str,
    rows: int,
    cols: int,
    started: float,
    status: str,
    code: str,
) -> None:
    if sink is None:
        return
    event = {
        "dataset": dataset,
        "rows": rows,
        "cols": cols,
        "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
        "status": status,
        "code": code,
    }
    try:
        sink(event)
    except Exception:  # noqa: BLE001 - telemetry must not alter query outcome
        return


def _parse_explain(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raise QueryBrokerError("QUERY_EXECUTION_FAILED") from None
    if (
        not isinstance(raw, list)
        or len(raw) != 1
        or not isinstance(raw[0], dict)
        or not isinstance(raw[0].get("Plan"), dict)
    ):
        raise QueryBrokerError("QUERY_EXECUTION_FAILED")
    return raw[0]["Plan"]


def _check_explain_budget(raw: Any, budget: QueryBudget) -> None:
    root = _parse_explain(raw)
    root_cost = root.get("Total Cost")
    if not isinstance(root_cost, (int, float)) or isinstance(root_cost, bool):
        raise QueryBrokerError("QUERY_EXECUTION_FAILED")
    if not math.isfinite(float(root_cost)):
        raise QueryBrokerError("QUERY_EXECUTION_FAILED")
    if root_cost > budget.max_root_total_cost:
        raise QueryBrokerError("QUERY_PLAN_COST_EXCEEDED")

    stack: list[tuple[dict[str, Any], int]] = [(root, 0)]
    nodes = 0
    estimated_bytes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > 10_000 or depth > 64:
            raise QueryBrokerError("QUERY_EXECUTION_FAILED")
        rows = node.get("Plan Rows", 0)
        width = node.get("Plan Width", 0)
        if (
            not isinstance(rows, (int, float))
            or not isinstance(width, (int, float))
            or isinstance(width, bool)
            or not math.isfinite(float(rows))
            or not math.isfinite(float(width))
            or rows < 0
            or width < 0
        ):
            raise QueryBrokerError("QUERY_EXECUTION_FAILED")
        if rows > budget.max_plan_rows:
            raise QueryBrokerError("QUERY_PLAN_ROWS_EXCEEDED")
        estimated_bytes += math.ceil(rows * width)
        if estimated_bytes > budget.max_estimated_scan_bytes:
            raise QueryBrokerError("QUERY_PLAN_BYTES_EXCEEDED")
        children = node.get("Plans", [])
        if not isinstance(children, list) or any(not isinstance(child, dict) for child in children):
            raise QueryBrokerError("QUERY_EXECUTION_FAILED")
        stack.extend((child, depth + 1) for child in children)


def _normalize_cell(value: Any, field: FieldSpec, budget: QueryBudget) -> tuple[Any, bool]:
    if value is None:
        # Null business facts remain explicit null; permission-hidden fields
        # never reach this stage because Registry rejects their use.
        return None, False
    if field.value_type == "string" and isinstance(value, str):
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise QueryBrokerError("QUERY_RESULT_INVALID")
        if len(value.encode("utf-8", errors="strict")) > budget.max_cell_bytes:
            # Never silently rewrite business facts.  Views must provide bounded
            # semantic text; drift is an invalid result, not a shortened value.
            raise QueryBrokerError("QUERY_RESULT_INVALID")
        return value, False
    if field.value_type == "integer":
        if isinstance(value, bool):
            raise QueryBrokerError("QUERY_RESULT_INVALID")
        if isinstance(value, Decimal):
            if not value.is_finite() or value != value.to_integral_value():
                raise QueryBrokerError("QUERY_RESULT_INVALID")
            value = int(value)
        if not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
            raise QueryBrokerError("QUERY_RESULT_INVALID")
        return value, False
    if field.value_type == "decimal":
        if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
            raise QueryBrokerError("QUERY_RESULT_INVALID")
        numeric = value if isinstance(value, Decimal) else Decimal(str(value))
        if not numeric.is_finite() or abs(numeric) > Decimal("1e18"):
            raise QueryBrokerError("QUERY_RESULT_INVALID")
        rendered = str(numeric)
        if len(rendered.encode()) > budget.max_cell_bytes:
            raise QueryBrokerError("QUERY_RESULT_INVALID")
        return rendered, False
    if field.value_type == "date" and isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat(), False
    raise QueryBrokerError("QUERY_RESULT_INVALID")


def _public_shape(
    *,
    result_ref: str,
    dataset: str,
    output_fields: tuple[str, ...],
    rows: list[dict[str, Any]],
    truncated: bool,
    caveats: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "result_ref": result_ref,
        "dataset": dataset,
        "fields": list(output_fields),
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "caveats": list(caveats),
    }


def _json_bytes(value: Any) -> bytes:
    # Budget against the conservative standard JSON representation (including
    # separators), not only an optimized compact encoder a future API may not use.
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
    ).encode()


class QueryExecutor:
    def __init__(
        self,
        *,
        engine: Any,
        authority_loader: AuthorityLoader,
        environment_probe: EnvironmentProbe,
        evidence_sealer: EvidenceSealer,
        telemetry_sink: TelemetrySink | None = None,
        budget: QueryBudget | None = None,
    ):
        self._engine = engine
        self._authority_loader = authority_loader
        self._environment_probe = environment_probe
        self._evidence_sealer = evidence_sealer
        self._telemetry_sink = telemetry_sink
        self._budget = budget or QueryBudget()

    def _reload_authority(
        self,
        plan: QueryPlan,
        context: ExecutionContext,
    ) -> tuple[AuthorizationSnapshot, AuthorizedQuery]:
        planned = context.planned_authz
        if planned.fingerprint() != plan.authorized.authz_fingerprint:
            raise QueryBrokerError("AUTHORIZATION_CHANGED")
        try:
            current = self._authority_loader(planned.subject)
        except Exception:  # noqa: BLE001 - authority backend details are secret
            raise QueryBrokerError("AUTHORITY_UNAVAILABLE") from None
        if current is None:
            raise QueryBrokerError("AUTHORIZATION_CHANGED")
        if current.fingerprint() != planned.fingerprint():
            raise QueryBrokerError("AUTHORIZATION_CHANGED")

        # Re-run the full registry and compiler under current authority.  Exact
        # equality prevents Plan-time hidden-field or policy-version drift.
        refreshed = authorize_query(plan.authorized.ir, current)
        refreshed_compiled = compile_query(refreshed)
        validate_compiled_sql(refreshed_compiled)
        if refreshed != plan.authorized:
            raise QueryBrokerError("AUTHORIZATION_CHANGED")
        # The value-free fingerprint is useful for telemetry but deliberately
        # does not commit low-entropy filter values.  Authorization therefore
        # requires exact in-memory equality with a fresh server compilation.
        if refreshed_compiled != plan.compiled:
            raise QueryBrokerError("AUTHORIZATION_CHANGED")
        return current, refreshed

    def execute(
        self,
        plan: QueryPlan,
        context: ExecutionContext,
    ) -> QueryExecutionResult:
        started = time.monotonic()
        dataset = plan.authorized.dataset_name
        rows_count = 0
        columns_count = len(plan.compiled.output_fields)
        try:
            current, _refreshed = self._reload_authority(plan, context)
            # Revocation is checked against the authoritative identity store
            # before even the Agent DB posture probe opens a connection.
            self._environment_probe.ensure_ready()
            validate_compiled_sql(plan.compiled)
            if columns_count > self._budget.max_result_columns:
                raise QueryBrokerError("QUERY_RESULT_INVALID")
            result = self._execute_read_only(plan, context, current)
            rows_count = result.row_count
            _emit(
                self._telemetry_sink,
                dataset=dataset,
                rows=rows_count,
                cols=columns_count,
                started=started,
                status="ok",
                code="QUERY_COMPLETED",
            )
            return result
        except QueryBrokerError as exc:
            _emit(
                self._telemetry_sink,
                dataset=dataset,
                rows=rows_count,
                cols=columns_count,
                started=started,
                status="error",
                code=exc.code,
            )
            raise
        except Exception:  # noqa: BLE001 - never surface driver/data details
            _emit(
                self._telemetry_sink,
                dataset=dataset,
                rows=0,
                cols=columns_count,
                started=started,
                status="error",
                code="QUERY_EXECUTION_FAILED",
            )
            raise QueryBrokerError("QUERY_EXECUTION_FAILED") from None

    def _execute_read_only(
        self,
        plan: QueryPlan,
        context: ExecutionContext,
        current: AuthorizationSnapshot,
    ) -> QueryExecutionResult:
        transaction = None
        try:
            with self._engine.connect() as connection:
                transaction = connection.begin()
                for statement in _transaction_settings(self._budget):
                    connection.exec_driver_sql(statement)
                page_permissions = sorted(
                    permission for permission in current.permissions if permission.startswith("page_")
                )
                data_permissions = sorted(
                    permission for permission in current.permissions if permission.startswith("data_")
                )
                connection.execute(text(_SET_CONTEXT_SQL), {
                    "subject": current.subject,
                    "tenant": current.tenant_id,
                    "dataset": plan.authorized.dataset_name,
                    "page_permissions": ",".join(page_permissions),
                    "data_permissions": ",".join(data_permissions),
                    "own_only": "true" if current.own_customers_only else "false",
                    "row_subject": current.row_subject if current.own_customers_only else "",
                    "authz_version": str(current.authz_version),
                    "registry_version": REGISTRY_VERSION,
                })
                explain = connection.execute(
                    text(f"EXPLAIN (FORMAT JSON) {plan.compiled.sql}"),
                    plan.compiled.params,
                ).scalar_one()
                _check_explain_budget(explain, self._budget)
                stream_connection = connection.execution_options(
                    stream_results=True,
                    max_row_buffer=self._budget.fetch_batch_rows,
                )
                raw_result = stream_connection.execute(
                    text(plan.compiled.sql),
                    plan.compiled.params,
                ).mappings()
                result_ref = f"query-result/{context.execution_id}"
                rows, truncated = self._collect_rows(raw_result, plan, result_ref)
                transaction.rollback()
                transaction = None
        finally:
            if transaction is not None:
                try:
                    transaction.rollback()
                except Exception:  # noqa: BLE001, S110
                    pass

        while True:
            public = _public_shape(
                result_ref=result_ref,
                dataset=plan.authorized.dataset_name,
                output_fields=plan.compiled.output_fields,
                rows=rows,
                truncated=truncated,
                caveats=plan.authorized.caveats,
            )
            if len(_json_bytes(public)) <= self._budget.max_result_bytes:
                break
            if not rows:
                raise QueryBrokerError("QUERY_RESULT_INVALID")
            rows.pop()
            truncated = True

        result_digest = hashlib.sha256(_json_bytes(public)).hexdigest()
        evidence_payload = {
            "schema_version": "query-evidence/v1",
            "execution_id": str(context.execution_id),
            "task_ref": context.task_ref,
            "step_ref": context.step_ref,
            "dataset": plan.authorized.dataset_name,
            "semantic_version": plan.authorized.dataset.semantic_version,
            "registry_version": REGISTRY_VERSION,
            "registry_fingerprint": plan.authorized.registry_fingerprint,
            "compiler_version": plan.compiled.compiler_version,
            "query_ir_ref": plan.query_ir_ref,
            "isolation": "read-only-repeatable-read",
            "authz_version": current.authz_version,
            "tenant_scope": current.tenant_id,
            "row_subject": current.row_subject if current.own_customers_only else None,
            "row_predicate_version": "dataset-guard/v1",
            "own_customers_only": current.own_customers_only,
            "provider_profile_ref": context.provider_profile_ref,
            "budget": {
                "statement_timeout_ms": self._budget.statement_timeout_ms,
                "lock_timeout_ms": self._budget.lock_timeout_ms,
                "max_root_total_cost": self._budget.max_root_total_cost,
                "max_plan_rows": self._budget.max_plan_rows,
                "max_estimated_scan_bytes": self._budget.max_estimated_scan_bytes,
                "max_result_rows": self._budget.max_result_rows,
                "max_result_bytes": self._budget.max_result_bytes,
                "max_cell_bytes": self._budget.max_cell_bytes,
            },
            "result_count": len(rows),
            "truncated": truncated,
            "result_digest": result_digest,
            "caveats": list(plan.authorized.caveats),
        }
        try:
            sealed = self._evidence_sealer.seal(
                purpose="query.evidence",
                payload=evidence_payload,
            )
        except Exception:  # noqa: BLE001 - no envelope/MAC detail escapes
            raise QueryBrokerError("EVIDENCE_SEAL_FAILED") from None
        return QueryExecutionResult(
            result_ref=result_ref,
            dataset=plan.authorized.dataset_name,
            output_fields=plan.compiled.output_fields,
            rows=tuple(rows),
            row_count=len(rows),
            truncated=truncated,
            caveats=plan.authorized.caveats,
            sealed_evidence=sealed,
        )

    def _collect_rows(
        self,
        result: Any,
        plan: QueryPlan,
        result_ref: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        expected = plan.compiled.output_fields
        requested = min(plan.authorized.ir.limit, self._budget.max_result_rows)
        rows: list[dict[str, Any]] = []
        truncated = False
        empty_shape = _public_shape(
            result_ref=result_ref,
            dataset=plan.authorized.dataset_name,
            output_fields=expected,
            rows=[],
            truncated=False,
            caveats=plan.authorized.caveats,
        )
        if len(_json_bytes(empty_shape)) > self._budget.max_result_bytes:
            raise QueryBrokerError("QUERY_RESULT_INVALID")
        while len(rows) <= requested:
            remaining_with_sentinel = requested + 1 - len(rows)
            batch = result.fetchmany(min(self._budget.fetch_batch_rows, remaining_with_sentinel))
            if not batch:
                break
            stop_fetch = False
            for raw in batch:
                if len(rows) >= requested:
                    truncated = True
                    stop_fetch = True
                    break
                mapping = dict(raw)
                if set(mapping) != set(expected):
                    raise QueryBrokerError("QUERY_RESULT_INVALID")
                row: dict[str, Any] = {}
                for name in expected:
                    field = plan.authorized.dataset.fields[name]
                    value, cell_truncated = _normalize_cell(mapping[name], field, self._budget)
                    row[name] = value
                    truncated = truncated or cell_truncated
                candidate = _public_shape(
                    result_ref=result_ref,
                    dataset=plan.authorized.dataset_name,
                    output_fields=expected,
                    rows=[*rows, row],
                    truncated=truncated,
                    caveats=plan.authorized.caveats,
                )
                if len(_json_bytes(candidate)) > self._budget.max_result_bytes:
                    truncated = True
                    stop_fetch = True
                    break
                rows.append(row)
            if stop_fetch:
                break
        return rows, truncated
