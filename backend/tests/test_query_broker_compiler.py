"""Deterministic compiler and SQLGlot second-gate tests.

Raw SQL appears only in direct unit tests of the *internal* AST guard.  No
runtime/model-facing request type accepts it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.agent.query_broker.ast_guard import validate_compiled_sql
from app.agent.query_broker.compiler import (
    CompiledQuery,
    compile_query,
    compute_compiler_fingerprint,
)
from app.agent.query_broker.egress import ProviderEgressSnapshot
from app.agent.query_broker.errors import QueryBrokerError
from app.agent.query_broker.ir import QueryIR
from app.agent.query_broker.registry import DATASETS, AuthorizationSnapshot, authorize_query

TODAY = date(2026, 8, 10)


def _full_month_range(months: int = 3) -> dict[str, date]:
    end = date(TODAY.year, TODAY.month, 1) - timedelta(days=1)
    start_index = end.year * 12 + end.month - 1 - (months - 1)
    return {
        "start": date(start_index // 12, start_index % 12 + 1, 1),
        "end": end,
    }


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
    authz: AuthorizationSnapshot,
    **overrides,
) -> ProviderEgressSnapshot:
    values = {
        "profile_ref": "private-gpu/v1",
        "policy_version": 3,
        "policy_fingerprint": "b" * 64,
        "authz_fingerprint": authz.fingerprint(),
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


def _compile(body: dict, authz: AuthorizationSnapshot | None = None):
    ir = QueryIR.model_validate(body)
    authority = authz or _authz()
    authorized = authorize_query(ir, authority, _egress(authority), today=TODAY)
    return compile_query(authorized)


def _replace_with_internally_consistent_sql(
    compiled: CompiledQuery,
    sql: str,
) -> CompiledQuery:
    """Exercise the AST gate, not merely the preceding fingerprint gate."""

    fingerprint = compute_compiler_fingerprint(
        dataset_name=compiled.dataset_name,
        view_schema=compiled.view_schema,
        view_name=compiled.view_name,
        sql=sql,
        params=compiled.params,
        output_fields=compiled.output_fields,
        allowed_columns=compiled.allowed_columns,
        registry_fingerprint=compiled.registry_fingerprint,
        authz_fingerprint=compiled.authz_fingerprint,
        egress_fingerprint=compiled.egress_fingerprint,
    )
    return compiled.model_copy(update={
        "sql": sql,
        "compiler_fingerprint": fingerprint,
    })


def _purchase_body(**overrides) -> dict:
    body = {
        "version": "query-ir/v1",
        "dataset": "purchase_activity_v1",
        "time_range": {"start": TODAY - timedelta(days=30), "end": TODAY},
        "dimensions": ["month", "part_id", "pn_std"],
        "metrics": ["qty", "amount_ex_tax", "weighted_unit_price_ex_tax"],
        "filters": [
            {"field": "pn_std", "operator": "eq", "value": "PN' OR 1=1 --"},
            {"field": "source_type", "operator": "in", "value": ["销售订单", "指定采购"]},
        ],
        "order_by": [{"field": "amount_ex_tax", "direction": "desc"}],
        "limit": 50,
    }
    body.update(overrides)
    return body


def test_compiler_uses_only_static_identifiers_and_bound_values():
    compiled = _compile(_purchase_body())
    assert compiled.sql.count("SELECT") == 1
    assert 'FROM "agent_semantic"."purchase_activity_v1"' in compiled.sql
    assert "PN' OR 1=1" not in compiled.sql
    assert "销售订单" not in compiled.sql
    assert compiled.params["filter_0"] == "PN' OR 1=1 --"
    assert compiled.params["filter_1_0"] == "销售订单"
    assert compiled.params["result_limit"] == 51
    assert compiled.output_fields == (
        "month",
        "part_id",
        "pn_std",
        "qty",
        "amount_ex_tax",
        "weighted_unit_price_ex_tax",
    )
    validate_compiled_sql(compiled)


def test_json_iso_date_filter_is_bound_as_a_typed_date():
    body = _purchase_body(filters=[{
        "field": "day",
        "operator": "eq",
        "value": TODAY.isoformat(),
    }])
    compiled = _compile(body)
    assert compiled.params["filter_0"] == TODAY
    validate_compiled_sql(compiled)


def test_compiler_output_is_byte_stable_for_same_authorized_ir():
    left = _compile(_purchase_body())
    right = _compile(_purchase_body())
    assert left.sql == right.sql
    assert left.params == right.params
    assert left.compiler_fingerprint == right.compiler_fingerprint


def test_compiler_fingerprint_commits_value_free_egress_snapshot():
    authz = _authz()
    ir = QueryIR.model_validate(_purchase_body())
    first = compile_query(authorize_query(ir, authz, _egress(authz), today=TODAY))
    changed = compile_query(authorize_query(
        ir,
        authz,
        _egress(authz, policy_version=4, policy_fingerprint="c" * 64),
        today=TODAY,
    ))
    assert first.egress_fingerprint != changed.egress_fingerprint
    assert first.compiler_fingerprint != changed.compiler_fingerprint


def test_multiple_weighted_metrics_receive_unique_bound_zero_parameters():
    body = _purchase_body(metrics=[
        "weighted_unit_price_inc_tax",
        "weighted_unit_price_ex_tax",
    ], order_by=[])
    compiled = _compile(body)
    assert compiled.params["metric_zero_weighted_unit_price_inc_tax"] == 0
    assert compiled.params["metric_zero_weighted_unit_price_ex_tax"] == 0
    assert compiled.sql.count(":metric_zero_weighted_unit_price_inc_tax") == 1
    assert compiled.sql.count(":metric_zero_weighted_unit_price_ex_tax") == 1
    validate_compiled_sql(compiled)


def test_own_sales_query_always_has_server_owned_k_anonymity_predicate():
    authz = _authz(
        role="sales",
        permissions=frozenset({"page_chat", "page_parts"}),
        own_customers_only=True,
        row_subject="salesperson-17",
    )
    compiled = _compile(
        {
            "version": "query-ir/v1",
            "dataset": "sales_market_month_v1",
            "time_range": _full_month_range(),
            "dimensions": ["month", "part_id"],
            "metrics": ["sales_qty", "sales_order_count"],
            "filters": [],
            "order_by": [],
            "limit": 200,
        },
        authz,
    )
    assert '"sales_order_count" >= :scope_k_min' in compiled.sql
    assert compiled.params["scope_k_min"] == 3
    assert compiled.params["result_limit"] == 201
    assert "salesperson-17" not in compiled.sql
    assert "salesperson-17" not in compiled.params.values()
    validate_compiled_sql(compiled)


def test_part_catalog_is_single_view_direct_projection():
    compiled = _compile({
        "version": "query-ir/v1",
        "dataset": "part_catalog_v1",
        "time_range": None,
        "dimensions": ["part_id", "pn_std", "description", "pool_name"],
        "metrics": [],
        "filters": [{"field": "brand", "operator": "eq", "value": "HPE"}],
        "order_by": [{"field": "pn_std", "direction": "asc"}],
        "limit": 20,
    })
    assert "GROUP BY" not in compiled.sql
    assert "JOIN" not in compiled.sql
    assert compiled.params["filter_0"] == "HPE"
    validate_compiled_sql(compiled)


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM "agent_semantic"."part_catalog_v1"',
        'SELECT "part_id" FROM "agent_semantic"."part_catalog_v1"; SELECT 1',
        'WITH x AS (SELECT 1) SELECT "part_id" FROM "agent_semantic"."part_catalog_v1"',
        'SELECT "part_id" FROM "agent_semantic"."part_catalog_v1" UNION SELECT 1',
        'SELECT p."part_id" FROM "agent_semantic"."part_catalog_v1" p JOIN pg_catalog.pg_roles r ON TRUE',
        'SELECT (SELECT current_user) FROM "agent_semantic"."part_catalog_v1"',
        'SELECT pg_sleep(1) FROM "agent_semantic"."part_catalog_v1"',
        'SELECT current_setting(\'server_version\') FROM "agent_semantic"."part_catalog_v1"',
        'SELECT set_config(\'search_path\', \'public\', true) FROM "agent_semantic"."part_catalog_v1"',
        'SELECT "part_id" INTO TEMP leaked FROM "agent_semantic"."part_catalog_v1"',
        'SELECT "part_id" FROM "public"."part_catalog_v1"',
        'SELECT "part_id" FROM "pg_catalog"."pg_roles"',
        'SELECT "part_id" FROM "agent_semantic"."part_catalog_v1" FOR UPDATE',
        'COPY (SELECT 1) TO PROGRAM \'id\'',
        'SET ROLE agent_view_owner',
        'DELETE FROM "agent_semantic"."part_catalog_v1"',
        'SELECT "part_id" FROM "agent_semantic"."part_catalog_v1" -- comment',
        'SELECT "part_id" FROM "agent_semantic"."part_catalog_v1" /* comment */',
        'SELECT "part_id" FROM "agent_semantic"."part_catalog_v1"\x00',
        # Cyrillic small a in the schema name.
        'SELECT "part_id" FROM "аgent_semantic"."part_catalog_v1"',
    ],
)
def test_internal_ast_gate_rejects_attack_fixtures(sql):
    trusted = _compile({
        "version": "query-ir/v1",
        "dataset": "part_catalog_v1",
        "time_range": None,
        "dimensions": ["part_id"],
        "metrics": [],
        "filters": [],
        "order_by": [],
        "limit": 10,
    })
    poisoned = _replace_with_internally_consistent_sql(trusted, sql)
    with pytest.raises(QueryBrokerError) as exc:
        validate_compiled_sql(poisoned)
    assert exc.value.code == "COMPILED_SQL_REJECTED"


def test_ast_gate_rejects_unknown_column_and_function_even_on_allowlisted_view():
    trusted = _compile({
        "version": "query-ir/v1",
        "dataset": "part_catalog_v1",
        "time_range": None,
        "dimensions": ["part_id"],
        "metrics": [],
        "filters": [],
        "order_by": [],
        "limit": 10,
    })
    for sql in (
        'SELECT "secret" FROM "agent_semantic"."part_catalog_v1"',
        'SELECT md5("part_id"::text) FROM "agent_semantic"."part_catalog_v1"',
    ):
        with pytest.raises(QueryBrokerError) as exc:
            validate_compiled_sql(_replace_with_internally_consistent_sql(trusted, sql))
        assert exc.value.code == "COMPILED_SQL_REJECTED"


def test_ast_gate_rejects_compiler_fingerprint_tampering():
    trusted = _compile(_purchase_body())
    with pytest.raises(QueryBrokerError) as exc:
        validate_compiled_sql(
            trusted.model_copy(update={"compiler_fingerprint": "0" * 64})
        )
    assert exc.value.code == "COMPILED_SQL_REJECTED"
