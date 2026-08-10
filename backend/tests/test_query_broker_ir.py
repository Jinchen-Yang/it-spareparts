"""Query Broker v1 input and semantic-registry security contract."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.agent.query_broker.errors import QueryBrokerError
from app.agent.query_broker.ir import QueryIR
from app.agent.query_broker.registry import (
    DATASETS,
    REGISTRY_POLICY_FINGERPRINT,
    AuthorizationSnapshot,
    authorize_query,
    dataset_registry_fingerprint,
)

TODAY = date(2026, 8, 10)


def _full_month_range(months: int = 3) -> dict[str, date]:
    end = date(TODAY.year, TODAY.month, 1) - timedelta(days=1)
    start_index = end.year * 12 + end.month - 1 - (months - 1)
    start = date(start_index // 12, start_index % 12 + 1, 1)
    return {"start": start, "end": end}


def _authz(**overrides) -> AuthorizationSnapshot:
    values = {
        "subject": "user-17",
        "tenant_id": "it-data",
        "role": "purchaser",
        "permissions": frozenset({
            "page_chat",
            "page_parts",
            "page_purchases",
            "data_supplier",
            "data_purchase_cost",
        }),
        "authz_version": 7,
        "own_customers_only": False,
        "row_subject": None,
    }
    values.update(overrides)
    return AuthorizationSnapshot(**values)


def _purchase_ir(**overrides) -> QueryIR:
    values = {
        "version": "query-ir/v1",
        "dataset": "purchase_activity_v1",
        "time_range": {
            "start": TODAY - timedelta(days=30),
            "end": TODAY,
        },
        "dimensions": ["day", "part_id"],
        "metrics": ["qty"],
        "filters": [],
        "order_by": [{"field": "qty", "direction": "desc"}],
        "limit": 50,
    }
    if ("dimensions" in overrides or "metrics" in overrides) and "order_by" not in overrides:
        values["order_by"] = []
    values.update(overrides)
    return QueryIR.model_validate(values)


@pytest.mark.parametrize(
    "extra",
    [
        {"sql": "select * from dim_part"},
        {"query": "WITH leaked AS (SELECT 1) SELECT * FROM leaked"},
        {"cte": "x"},
        {"offset": 1},
        {"having": {"qty": 1}},
        {"alias": "leak"},
    ],
)
def test_ir_has_no_raw_sql_or_expression_escape_hatch(extra):
    body = _purchase_ir().model_dump(mode="json") | extra
    with pytest.raises(ValidationError):
        QueryIR.model_validate(body)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda body: body.update(dataset="purchase_activity_v1; DROP TABLE dim_part"),
        lambda body: body.update(dimensions=["day", "part_id --"]),
        lambda body: body.update(metrics=["qty\x00"]),
        lambda body: body.update(filters=[{"field": "pn_std", "operator": "eq", "value": "A\u202eB"}]),
        lambda body: body.update(filters=[{"field": "pn_std", "operator": "eq", "value": "\ud800"}]),
    ],
)
def test_ir_rejects_identifier_and_unicode_control_smuggling(mutator):
    body = _purchase_ir().model_dump(mode="python")
    mutator(body)
    with pytest.raises((ValidationError, UnicodeEncodeError)):
        QueryIR.model_validate(body)


def test_injection_shaped_filter_value_is_data_not_sql():
    ir = _purchase_ir(filters=[{
        "field": "pn_std",
        "operator": "eq",
        "value": "PN' OR 1=1 /* canary */ --",
    }])
    authorized = authorize_query(ir, _authz(), today=TODAY)
    assert authorized.ir.filters[0].value.endswith("--")


@pytest.mark.parametrize(
    "field,permission",
    [
        ("supplier_name", "data_supplier"),
        ("source_channel", "data_supplier"),
        ("amount_inc_tax", "data_purchase_cost"),
        ("weighted_unit_price_ex_tax", "data_purchase_cost"),
    ],
)
@pytest.mark.parametrize("position", ["select", "filter", "order"])
def test_hidden_fields_are_structurally_rejected_everywhere(field, permission, position):
    authz = _authz(permissions=_authz().permissions - {permission})
    kwargs: dict = {}
    if position == "select":
        kwargs["dimensions" if field in {"supplier_name", "source_channel"} else "metrics"] = [field]
        if "dimensions" in kwargs:
            kwargs["metrics"] = ["qty"]
    elif position == "filter":
        kwargs["filters"] = [{"field": field, "operator": "eq", "value": 1}]
    else:
        selected_key = "dimensions" if field in {"supplier_name", "source_channel"} else "metrics"
        kwargs[selected_key] = [field]
        kwargs["order_by"] = [{"field": field, "direction": "asc"}]
    with pytest.raises(QueryBrokerError) as exc:
        authorize_query(_purchase_ir(**kwargs), authz, today=TODAY)
    assert exc.value.code == "FIELD_NOT_VISIBLE"


def test_dataset_page_permissions_are_all_required_before_compilation():
    authz = _authz(permissions=_authz().permissions - {"page_purchases"})
    with pytest.raises(QueryBrokerError) as exc:
        authorize_query(_purchase_ir(), authz, today=TODAY)
    assert exc.value.code == "DATASET_NOT_VISIBLE"


@pytest.mark.parametrize(
    "time_range,code",
    [
        (None, "TIME_RANGE_REQUIRED"),
        ({"start": TODAY - timedelta(days=367), "end": TODAY}, "TIME_RANGE_TOO_WIDE"),
        ({"start": TODAY, "end": TODAY - timedelta(days=1)}, "TIME_RANGE_INVALID"),
        ({"start": TODAY, "end": TODAY + timedelta(days=1)}, "TIME_RANGE_IN_FUTURE"),
    ],
)
def test_activity_time_range_is_bounded(time_range, code):
    with pytest.raises(QueryBrokerError) as exc:
        authorize_query(_purchase_ir(time_range=time_range), _authz(), today=TODAY)
    assert exc.value.code == code


def test_registry_rejects_unknown_fields_and_metric_kind_confusion():
    with pytest.raises(QueryBrokerError) as unknown:
        authorize_query(_purchase_ir(dimensions=["day", "secret_column"]), _authz(), today=TODAY)
    assert unknown.value.code == "UNKNOWN_FIELD"

    with pytest.raises(QueryBrokerError) as wrong_kind:
        authorize_query(_purchase_ir(dimensions=["qty"], metrics=[]), _authz(), today=TODAY)
    assert wrong_kind.value.code == "FIELD_KIND_MISMATCH"


def test_purchase_order_count_requires_formal_part_dimension():
    with pytest.raises(QueryBrokerError) as exc:
        authorize_query(
            _purchase_ir(dimensions=["month"], metrics=["purchase_order_count"]),
            _authz(),
            today=TODAY,
        )
    assert exc.value.code == "REQUIRED_DIMENSION_MISSING"


def test_filter_value_and_operator_must_match_registry_type():
    with pytest.raises(QueryBrokerError) as wrong_type:
        authorize_query(
            _purchase_ir(filters=[{"field": "part_id", "operator": "eq", "value": "17"}]),
            _authz(),
            today=TODAY,
        )
    assert wrong_type.value.code == "FILTER_TYPE_INVALID"

    with pytest.raises(QueryBrokerError) as wrong_op:
        authorize_query(
            _purchase_ir(filters=[{"field": "pn_std", "operator": "gt", "value": "PN-1"}]),
            _authz(),
            today=TODAY,
        )
    assert wrong_op.value.code == "FILTER_OPERATOR_NOT_ALLOWED"


@pytest.mark.parametrize("value", [
    pytest.param(2**63, id="signed64-high"),
    pytest.param(-(2**63) - 1, id="signed64-low"),
    pytest.param(10**5000, id="huge-integer"),
    pytest.param(1e308, id="huge-float"),
    pytest.param(-1e308, id="huge-negative-float"),
])
def test_numeric_filter_values_have_pre_db_magnitude_budget(value):
    body = _purchase_ir().model_dump(mode="python")
    body["filters"] = [{"field": "part_id", "operator": "eq", "value": value}]
    with pytest.raises(ValidationError):
        QueryIR.model_validate(body)


def test_boolean_is_not_accepted_as_integer_field_value():
    ir = _purchase_ir(filters=[{"field": "part_id", "operator": "eq", "value": True}])
    with pytest.raises(QueryBrokerError) as exc:
        authorize_query(ir, _authz(), today=TODAY)
    assert exc.value.code == "FILTER_TYPE_INVALID"


def test_own_sales_scope_requires_server_row_subject_and_fixed_grain():
    body = {
        "version": "query-ir/v1",
        "dataset": "sales_market_month_v1",
        "time_range": _full_month_range(),
        "dimensions": ["month", "part_id"],
        "metrics": ["sales_qty", "sales_order_count"],
        "filters": [],
        "order_by": [],
        "limit": 50,
    }
    authz = _authz(
        role="sales",
        permissions=frozenset({"page_chat", "page_parts"}),
        own_customers_only=True,
        row_subject=None,
    )
    with pytest.raises(QueryBrokerError) as missing_subject:
        authorize_query(QueryIR.model_validate(body), authz, today=TODAY)
    assert missing_subject.value.code == "ROW_SUBJECT_REQUIRED"

    scoped = authz.model_copy(update={"row_subject": "salesperson-7"})
    authorized = authorize_query(QueryIR.model_validate(body), scoped, today=TODAY)
    assert authorized.k_anonymity_threshold == 3

    body["dimensions"] = ["month", "part_id", "brand"]
    with pytest.raises(QueryBrokerError) as grain:
        authorize_query(QueryIR.model_validate(body), scoped, today=TODAY)
    assert grain.value.code == "SALES_ORDER_COUNT_GRAIN_INVALID"


def test_part_catalog_has_no_cost_or_raw_pn_fields():
    body = {
        "version": "query-ir/v1",
        "dataset": "part_catalog_v1",
        "time_range": None,
        "dimensions": ["part_id", "pn_raw"],
        "metrics": [],
        "filters": [],
        "order_by": [],
        "limit": 50,
    }
    with pytest.raises(QueryBrokerError) as exc:
        authorize_query(QueryIR.model_validate(body), _authz(), today=TODAY)
    assert exc.value.code == "UNKNOWN_FIELD"


def test_sales_month_dataset_requires_complete_calendar_months_and_max_twelve():
    body = {
        "version": "query-ir/v1",
        "dataset": "sales_market_month_v1",
        "time_range": _full_month_range(12),
        "dimensions": ["month", "part_id"],
        "metrics": ["sales_qty"],
        "filters": [],
        "order_by": [],
        "limit": 50,
    }
    authz = _authz(permissions=frozenset({"page_chat", "page_parts"}))
    authorized = authorize_query(QueryIR.model_validate(body), authz, today=TODAY)
    assert any("当前未结束月份不进入" in caveat for caveat in authorized.caveats)

    body["time_range"] = _full_month_range(13)
    with pytest.raises(QueryBrokerError) as too_wide:
        authorize_query(QueryIR.model_validate(body), authz, today=TODAY)
    assert too_wide.value.code == "TIME_RANGE_TOO_WIDE"

    valid = _full_month_range(3)
    body["time_range"] = {"start": valid["start"] + timedelta(days=1), "end": valid["end"]}
    with pytest.raises(QueryBrokerError) as partial:
        authorize_query(QueryIR.model_validate(body), authz, today=TODAY)
    assert partial.value.code == "TIME_RANGE_INVALID"

    body["time_range"] = {"start": TODAY.replace(day=1), "end": TODAY}
    with pytest.raises(QueryBrokerError) as current_partial_month:
        authorize_query(QueryIR.model_validate(body), authz, today=TODAY)
    assert current_partial_month.value.code == "TIME_RANGE_INVALID"


def test_ir_shape_budgets_are_enforced_before_registry_or_db():
    body = _purchase_ir().model_dump(mode="python")
    body["filters"] = [
        {"field": "pn_std", "operator": "in", "value": [f"PN-{n}" for n in range(51)]}
    ]
    with pytest.raises(ValidationError):
        QueryIR.model_validate(body)


def test_semantic_registry_is_deeply_immutable_and_fingerprint_stable():
    before = dataset_registry_fingerprint("part_catalog_v1")
    with pytest.raises(TypeError):
        DATASETS["evil_v1"] = DATASETS["part_catalog_v1"]
    with pytest.raises(TypeError):
        DATASETS["part_catalog_v1"].fields["secret"] = DATASETS["part_catalog_v1"].fields["part_id"]
    assert dataset_registry_fingerprint("part_catalog_v1") == before
    assert len(REGISTRY_POLICY_FINGERPRINT) == 64

    body = _purchase_ir().model_dump(mode="python")
    body["limit"] = 201
    with pytest.raises(ValidationError):
        QueryIR.model_validate(body)
