"""Batch projections retain current grants, bounded work and independent revisions."""

# ruff: noqa: F811
import json

import pytest
from sqlalchemy import event, func, select

from app.models.dimensions import DimPart
from app.pn_catalog import api
from app.pn_catalog.attribute_models import AttributeState, AttributeValue
from app.pn_catalog.bulk_read import get_snapshots
from app.pn_catalog.content_models import ConditionContent
from app.pn_catalog.dto import AuthorizationDecision
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.http_bulk import SnapshotListResponse
from app.pn_catalog.models import AuditEvent, CommandReceipt, PartState
from tests.test_pn_catalog_access import authority, cerbos_url  # noqa: F401
from tests.test_pn_catalog_attributes import allow, context
from tests.test_pn_catalog_authority_http import exchange, http, native  # noqa: F401


def seed(db, count=1, *, large=False):
    ids = []
    for i in range(count):
        part = DimPart(
            pn_std=f"BULK-{i}", description=f"description-{i}", brand="brand"
        )
        db.add(part)
        db.flush()
        ids.append(part.id)
        db.add(
            PartState(
                part_id=part.id,
                identity_revision=2,
                profile_revision=4,
                field_versions={"description": 3},
                fill_status="confirmed",
                fill_status_revision=7,
            )
        )
        db.add(
            ConditionContent(
                part_id=part.id,
                grade="good",
                title="good title",
                description="good description",
                revision=5,
                field_versions={"title": 2, "description": 4},
            )
        )
        db.add(AttributeState(part_id=part.id, revision=8))
        db.flush()
        db.add(
            AttributeValue(
                part_id=part.id,
                key="custom",
                value="字" * 14000 if large else None,
                version=6,
            )
        )
    db.commit()
    return ids


def test_batch_queries_are_constant_and_projections_match_individual_reads(db):
    ids = seed(db, 20)
    statements, actions = [], []

    def count(_conn, _cursor, statement, *_rest):
        statements.append(statement)

    def authorize(*args):
        actions.append(args[2])
        return allow(*args)

    event.listen(db.get_bind(), "before_cursor_execute", count)
    try:
        result = get_snapshots(
            db, ids + [999999], cursor=None, context=context(), authorize=authorize
        )
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", count)
    assert len(statements) == 6
    assert actions == ["list", "read_content", "read_attributes"]
    assert all(
        not sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for sql in statements
    )
    assert result["data"][-1]["state"] == "not_found"
    first = result["data"][0]
    assert (
        first["profile"]
        == api.get_part(db, ids[0], context=context(), authorize=allow)["data"]
    )
    assert (
        first["content_good"]
        == api.get_content(db, ids[0], "good", context=context(), authorize=allow)[
            "data"
        ]
    )
    assert (
        first["attributes"]
        == api.get_attributes(db, ids[0], context=context(), authorize=allow)["data"]
    )
    assert first["attributes"]["items"]["custom"]["value"] is None
    assert first["profile"]["fill_status_revision"] == 7
    SnapshotListResponse.model_validate(result)
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_current_field_grants_do_not_turn_hidden_values_into_missing_values(db):
    ids = seed(db)

    def restricted(_db, _ctx, action, _pid):
        if action == "read_attributes":
            raise CatalogError("auth.forbidden", "denied", status_code=403)
        fields = {"brand"} if action == "list" else {"content.good.title"}
        return AuthorizationDecision(
            readable_fields=frozenset(fields), metadata={"scope": "all"}
        )

    value = get_snapshots(
        db, ids, cursor=None, context=context(), authorize=restricted
    )["data"][0]
    assert value["profile"]["profile"] == {"brand": "brand"}
    assert (
        "description" not in value["content_good"]
        and "status" not in value["content_good"]
    )
    assert value["content_good"]["title"] == "good title"
    assert value["content_bad"] is None and value["attributes"] is None
    assert value["unavailable_resources"] == ["content.bad", "attributes"]
    assert value["readable_fields"] == ["brand", "content.good.title"]

    def unavailable(_db, _ctx, action, _pid):
        if action == "read_attributes":
            raise CatalogError("auth.unavailable", "offline", status_code=503)
        return allow()

    with pytest.raises(CatalogError) as error:
        get_snapshots(db, ids, cursor=None, context=context(), authorize=unavailable)
    assert error.value.status_code == 503


def test_response_byte_budget_pages_without_omissions_and_binds_cursor(db):
    ids = seed(db, 30, large=True)
    ctx = context()
    result = get_snapshots(db, ids, cursor=None, context=ctx, authorize=allow)
    first_cursor = result["meta"]["next_cursor"]
    assert first_cursor is not None and len(result["data"]) < 30
    seen = []
    while True:
        assert len(json.dumps(result, ensure_ascii=False).encode()) < 512 * 1024
        seen.extend(row["part_id"] for row in result["data"])
        cursor = result["meta"]["next_cursor"]
        if cursor is None:
            break
        result = get_snapshots(db, ids, cursor=cursor, context=ctx, authorize=allow)
    assert seen == ids
    with pytest.raises(CatalogError):
        get_snapshots(db, ids[:-1], cursor=first_cursor, context=ctx, authorize=allow)
    different = ctx.model_copy(
        update={"actor": ctx.actor.model_copy(update={"principal_id": "account:other"})}
    )
    with pytest.raises(CatalogError):
        get_snapshots(db, ids, cursor=first_cursor, context=different, authorize=allow)


def test_http_native_and_shop_delegation_use_the_typed_read_route(db, authority, http):
    ids = seed(db)
    path = "/api/pn-catalog/v1/parts/snapshots"
    native_response = http.post(
        path, json={"part_ids": ids}, headers=native(authority[0])
    )
    assert native_response.status_code == 200, native_response.text
    body = json.dumps({"part_ids": ids}).encode()
    headers, _ = exchange(http, authority[2], "POST", path, body)
    response = http.post(
        path, content=body, headers={**headers, "content-type": "application/json"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["part_id"] == ids[0]
    assert (
        response.json()["data"][0]["profile"]["profile"]
        == native_response.json()["data"][0]["profile"]["profile"]
    )
