"""Shared, read-only candidate matching; scores rank results, never bind identity.

This file is also shipped to it-spareparts as services/resale_catalog_matching.py.
Only * and ? are wildcards. SQL %, _ and backslash remain literal characters.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import and_, case, or_


def search_terms(query: str) -> list[str]:
    return query.strip().split()


def like_pattern(term: str) -> str:
    return (
        "%"
        + "".join(
            "%" if char == "*" else "_" if char == "?" else "\\" + char if char in "%_\\" else char
            for char in term
        )
        + "%"
    )


def search_condition(columns: Sequence[Any], query: str) -> Any:
    return and_(
        *(
            or_(*(column.ilike(like_pattern(term), escape="\\") for column in columns))
            for term in search_terms(query)
        )
    )


def search_order(columns: Sequence[Any], query: str) -> Any:
    """Keep whole-value/prefix hits ahead of substring rows before SQL LIMIT."""
    term = query.strip()
    escaped = "".join("\\" + c if c in "%_\\" else c for c in term)
    conditions = []
    for index, column in enumerate(columns):
        conditions.extend(
            [
                (column.ilike(escaped, escape="\\"), index * 3),
                (column.ilike(escaped + "%", escape="\\"), index * 3 + 1),
            ]
        )
    return case(*conditions, else_=len(columns) * 3)


def candidate_match(query: str, fields: Mapping[str, str | None]) -> dict[str, Any] | None:
    terms = search_terms(query)
    if not terms or not any(re.sub(r"[*?]", "", term) for term in terms):
        return None
    values = {key: value.strip().casefold() for key, value in fields.items() if value}

    # fnmatch treats [] specially; escape it so it agrees with SQL's * / ? contract.
    def matches(term: str, value: str) -> bool:
        pattern = "".join("[[]" if c == "[" else c for c in term.casefold())
        return fnmatch.fnmatchcase(value, "*" + pattern + "*")

    if not all(any(matches(term, value) for value in values.values()) for term in terms):
        return None
    query_value = query.strip().casefold()
    wildcard = any(c in query for c in "*?")
    for field, value in values.items():
        identity = field in {"pn", "sn", "alias", "internal", "self_code", "dossier"}
        if value == query_value:
            return {"matchKind": "exact", "matchField": field, "score": 100 if identity else 85}
    if wildcard:
        field = next(
            (key for key, value in values.items() if all(matches(term, value) for term in terms)),
            "keywords",
        )
        return {"matchKind": "wildcard", "matchField": field, "score": 75}
    if len(terms) > 1:
        return {"matchKind": "keywords", "matchField": "keywords", "score": 65}
    for field, value in values.items():
        if value.startswith(query_value):
            return {
                "matchKind": "prefix",
                "matchField": field,
                "score": 90 if field in {"pn", "sn", "internal", "self_code"} else 70,
            }
    field = next(key for key, value in values.items() if query_value in value)
    return {
        "matchKind": "contains",
        "matchField": field,
        "score": 80 if field in {"pn", "sn", "internal", "self_code"} else 60,
    }


def ranked(items: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            -item["score"],
            item.get("pnStd", ""),
            item.get("dossierNo", ""),
            item.get("dossierId", ""),
        ),
    )[:limit]
