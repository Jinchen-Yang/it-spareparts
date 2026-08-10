"""SQLGlot second gate for server-compiled SQL.

This module never parses model/user SQL.  It assumes the deterministic compiler
has produced a query and independently rejects structural drift before the DB.
Database role/view/RLS isolation remains the final security boundary.
"""

from __future__ import annotations

import hmac
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.agent.query_broker.compiler import (
    COMPILER_VERSION,
    MAX_COMPILED_SQL_BYTES,
    CompiledQuery,
    compute_compiler_fingerprint,
)
from app.agent.query_broker.errors import QueryBrokerError
from app.agent.query_broker.registry import DATASETS, dataset_registry_fingerprint

PINNED_SQLGLOT_VERSION = "30.13.0"
_SAFE_PARAMETER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_HEX = re.compile(r"^[0-9a-f]{64}$")

_BANNED_NODE_TYPES = tuple(
    node
    for node in (
        getattr(exp, name, None)
        for name in (
            "Alter",
            "Analyze",
            "Attach",
            "Cache",
            "Call",
            "Command",
            "Commit",
            "Copy",
            "Create",
            "CTE",
            "Delete",
            "Describe",
            "Detach",
            "Drop",
            "Execute",
            "Grant",
            "Insert",
            "Intersect",
            "Into",
            "Join",
            "Lateral",
            "LoadData",
            "Lock",
            "Merge",
            "Pragma",
            "Prepare",
            "Revoke",
            "Rollback",
            "Set",
            "Show",
            "Star",
            "Subquery",
            "Transaction",
            "TruncateTable",
            "Union",
            "Uncache",
            "Update",
            "Use",
            "Window",
            "With",
        )
    )
    if node is not None
)
_ALLOWED_NODE_TYPES = {
    exp.Select,
    exp.Alias,
    exp.Limit,
    exp.From,
    exp.Where,
    exp.Group,
    exp.Order,
    exp.Ordered,
    exp.Column,
    exp.Identifier,
    exp.Placeholder,
    exp.Table,
    exp.Sum,
    exp.Min,
    exp.Max,
    exp.Nullif,
    exp.Div,
    exp.And,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.In,
}


def _reject() -> None:
    raise QueryBrokerError("COMPILED_SQL_REJECTED")


def _verify_compiler_fingerprint(compiled: CompiledQuery) -> None:
    if compiled.compiler_version != COMPILER_VERSION:
        _reject()
    if not _SAFE_HEX.fullmatch(compiled.compiler_fingerprint):
        _reject()
    expected = compute_compiler_fingerprint(
        dataset_name=compiled.dataset_name,
        view_schema=compiled.view_schema,
        view_name=compiled.view_name,
        sql=compiled.sql,
        params=compiled.params,
        output_fields=compiled.output_fields,
        allowed_columns=compiled.allowed_columns,
        registry_fingerprint=compiled.registry_fingerprint,
        authz_fingerprint=compiled.authz_fingerprint,
        egress_fingerprint=compiled.egress_fingerprint,
    )
    if not hmac.compare_digest(expected, compiled.compiler_fingerprint):
        _reject()


def validate_compiled_sql(compiled: CompiledQuery) -> None:
    """Reject anything outside the compiler's single-view SELECT grammar."""

    if sqlglot.__version__ != PINNED_SQLGLOT_VERSION:
        _reject()
    _verify_compiler_fingerprint(compiled)
    dataset = DATASETS.get(compiled.dataset_name)
    if dataset is None:
        _reject()
    if (
        compiled.view_schema != dataset.view_schema
        or compiled.view_name != dataset.view_name
        or compiled.registry_fingerprint != dataset_registry_fingerprint(dataset.name)
        or not _SAFE_HEX.fullmatch(compiled.authz_fingerprint)
        or not _SAFE_HEX.fullmatch(compiled.egress_fingerprint)
    ):
        _reject()

    sql = compiled.sql
    if (
        not sql
        or len(sql.encode()) > MAX_COMPILED_SQL_BYTES
        or not sql.isascii()
        or "\x00" in sql
        or "--" in sql
        or "/*" in sql
        or "*/" in sql
    ):
        _reject()
    try:
        roots = sqlglot.parse(sql, read="postgres")
    except (ParseError, ValueError, RecursionError):
        _reject()
    if len(roots) != 1 or not isinstance(roots[0], exp.Select):
        _reject()
    root = roots[0]
    nodes = list(root.walk())
    if sum(isinstance(node, exp.Select) for node in nodes) != 1:
        _reject()
    if any(isinstance(node, _BANNED_NODE_TYPES) for node in nodes):
        _reject()
    if any(getattr(node, "comments", None) for node in nodes):
        _reject()
    if any(isinstance(node, (exp.Literal, exp.Boolean, exp.Anonymous)) for node in nodes):
        _reject()

    # Exact AST vocabulary.  This is intentionally narrower than a generic
    # "read-only SELECT" grammar and therefore rejects newly introduced
    # SQLGlot node types until reviewed.
    if any(type(node) not in _ALLOWED_NODE_TYPES for node in nodes):
        _reject()

    tables = list(root.find_all(exp.Table))
    if len(tables) != 1:
        _reject()
    table = tables[0]
    if (
        table.catalog
        or table.db != dataset.view_schema
        or table.name != dataset.view_name
        or table.alias
    ):
        _reject()

    allowed_columns = set(compiled.allowed_columns)
    if not allowed_columns or any(
        not _SAFE_PARAMETER.fullmatch(name) for name in allowed_columns
    ):
        _reject()
    if not allowed_columns.issubset(dataset.allowed_internal_columns):
        _reject()
    for column in root.find_all(exp.Column):
        if column.table or column.db or column.catalog or column.name not in allowed_columns:
            _reject()
    for identifier in root.find_all(exp.Identifier):
        if identifier.args.get("quoted") is not True:
            _reject()

    projections = root.expressions
    if len(projections) != len(compiled.output_fields):
        _reject()
    aliases: list[str] = []
    for projection in projections:
        if not isinstance(projection, exp.Alias) or not projection.alias:
            _reject()
        aliases.append(projection.alias)
    if tuple(aliases) != compiled.output_fields:
        _reject()

    placeholders = list(root.find_all(exp.Placeholder))
    placeholder_names = {placeholder.name for placeholder in placeholders}
    if (
        len(placeholder_names) != len(placeholders)
        or placeholder_names != set(compiled.params)
        or any(not _SAFE_PARAMETER.fullmatch(name) for name in placeholder_names)
    ):
        _reject()
    limit = root.args.get("limit")
    if (
        not isinstance(limit, exp.Limit)
        or not isinstance(limit.expression, exp.Placeholder)
        or limit.expression.name != "result_limit"
    ):
        _reject()
    result_limit = compiled.params.get("result_limit")
    if isinstance(result_limit, bool) or not isinstance(result_limit, int):
        _reject()
    if not 2 <= result_limit <= 201:
        _reject()
