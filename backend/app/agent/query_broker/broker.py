"""Planning facade: IR -> authz registry -> compiler -> AST gate."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.agent.query_broker.ast_guard import validate_compiled_sql
from app.agent.query_broker.compiler import CompiledQuery, compile_query
from app.agent.query_broker.ir import QueryIR
from app.agent.query_broker.registry import (
    AuthorizationSnapshot,
    AuthorizedQuery,
    authorize_query,
)


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_ir_ref: str
    authorized: AuthorizedQuery = Field(repr=False)
    compiled: CompiledQuery = Field(repr=False)


def build_query_plan(
    ir: QueryIR,
    authz: AuthorizationSnapshot,
    *,
    today: date | None = None,
) -> QueryPlan:
    authorized = authorize_query(ir, authz, today=today)
    compiled = compile_query(authorized)
    validate_compiled_sql(compiled)
    return QueryPlan(
        query_ir_ref=f"query-ir/{uuid4()}",
        authorized=authorized,
        compiled=compiled,
    )
