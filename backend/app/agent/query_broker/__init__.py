"""Fail-closed Text2SQL Query Broker v1 kernel.

This package is intentionally not registered as an Agent tool yet.  Issue
#224 depends on the durable task/capability control plane and independently
owned PostgreSQL semantic views; until those gates exist the feature remains
disabled and unreachable from model input.
"""

from app.agent.query_broker.broker import QueryPlan, build_query_plan
from app.agent.query_broker.errors import QueryBrokerError
from app.agent.query_broker.ir import QueryIR

__all__ = ["QueryBrokerError", "QueryIR", "QueryPlan", "build_query_plan"]
