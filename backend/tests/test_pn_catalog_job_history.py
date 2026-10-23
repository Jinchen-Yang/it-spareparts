"""PostgreSQL paging: owner prefilter, bounded scans, tie ordering and byte budget."""
# ruff: noqa: F811 -- imported pytest fixtures are resolved by argument name.

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.pn_catalog import jobs
from app.pn_catalog.dto import ActorContext, AuthorizationDecision, CommandContext
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.job_models import CatalogJob
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_pn_catalog_access import authority  # noqa: F401
from tests.test_pn_catalog_authority_http import http, native  # noqa: F401
from tests.test_pn_catalog_authority_jobs import research, seed


def context(owner="account:1"):
    return CommandContext(
        actor=ActorContext(
            principal_id=owner,
            client_id="itsp",
            executor_id="test",
            entrypoint="itsp_ui",
        ),
        request_id=uuid4(),
    )


def insert(
    db,
    number,
    *,
    ctx=None,
    kind="enrichment.research",
    status="pending",
    result=None,
    part_id=None,
    at=None,
    resource_id="1",
):
    ctx = ctx or context()
    identifier = UUID(int=number + 1)
    row = CatalogJob(
        id=identifier,
        kind=kind,
        queue="enrichment",
        resource_type=kind,
        resource_id=resource_id,
        part_id=part_id,
        resource_generation=0,
        input_revision=0,
        deduplication_key=str(identifier),
        payload={},
        payload_hash="a" * 64,
        context=ctx.model_dump(mode="json"),
        status=status,
        revision=1,
        result=result,
        created_at=at or datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db.add(row)
    return str(identifier)


def allow(db, ctx, action, job):
    assert (
        db.get(CatalogJob, job.job_id).context["actor"]["principal_id"]
        == ctx.actor.principal_id
    )
    if job.kind == "hidden":
        raise CatalogError("auth.forbidden", "not visible", status_code=403)
    return AuthorizationDecision(frozenset({"*"}), frozenset(), {})


def page(db, **kwargs):
    return jobs.list_jobs_authorized(
        db,
        context=kwargs.pop("context", context()),
        authorize_job=allow,
        cursor_secret=b"test-page-key",
        **kwargs,
    )


def test_owner_sql_prefilter_tie_order_and_status_pages(db):
    expected = [insert(db, n) for n in range(5)]
    for n in range(100, 150):
        insert(db, n, ctx=context("account:2"))
    insert(db, 200, status="cancelled")
    db.commit()
    found, cursor = [], None
    while True:
        value = page(db, statuses=["pending"], limit=2, cursor=cursor)
        found += [row["job_id"] for row in value["data"]]
        cursor = value["meta"]["next_cursor"]
        if cursor is None:
            break
    assert found == list(reversed(expected))
    db.execute(text("SET LOCAL enable_seqscan = off"))
    statement = str(
        select(CatalogJob.id)
        .where(CatalogJob.context["actor"]["principal_id"].astext == "account:1")
        .order_by(CatalogJob.created_at.desc(), CatalogJob.id.desc())
        .limit(20)
        .compile(db.get_bind(), compile_kwargs={"literal_binds": True})
    )
    plan = "\n".join(row[0] for row in db.execute(text("EXPLAIN " + statement)))
    assert "ix_pn_catalog_job_owner_created" in plan


def test_scan_budget_returns_continuation_even_for_empty_authorized_page(db):
    visible = insert(db, 1)
    for n in range(10, 215):
        insert(db, n, kind="hidden")
    db.commit()
    first = page(db, limit=1)
    assert first["data"] == [] and first["meta"]["next_cursor"]
    second = page(db, limit=1, cursor=first["meta"]["next_cursor"])
    assert [row["job_id"] for row in second["data"]] == [visible]
    assert second["meta"]["next_cursor"] is None


def test_page_byte_budget_preserves_every_full_result(db):
    expected = [
        insert(db, n, status="succeeded", result={"text": "中" * 20000})
        for n in range(20)
    ]
    db.commit()
    found, cursor, pages = [], None, 0
    while True:
        result = page(db, limit=50, cursor=cursor)
        assert all(row["result"]["text"] == "中" * 20000 for row in result["data"])
        found += [row["job_id"] for row in result["data"]]
        cursor, pages = result["meta"]["next_cursor"], pages + 1
        if cursor is None:
            break
        assert pages <= 4
    assert found == list(reversed(expected)) and pages >= 3


def test_cursor_binding_and_tampering_cannot_cross_owner_or_filter(db):
    insert(db, 1)
    insert(db, 2)
    db.commit()
    cursor = page(db, limit=1)["meta"]["next_cursor"]
    for kwargs in (
        {"statuses": ["pending"]},
        {"context": context("account:2")},
        {"cursor": "a" + cursor[1:]},
        {"cursor": "bad"},
    ):
        with pytest.raises(CatalogError) as caught:
            page(db, **({"cursor": cursor} | kwargs))
        assert caught.value.code == "pagination.invalid_cursor"


def test_real_http_owner_and_part_filter_uses_current_job_authorizer(
    db, authority, http
):
    pid, other = seed(db), seed(db, "AUTH-JOB-OTHER")
    result = research(http, authority, "itsp", pid)
    path = "/api/pn-catalog/v1/jobs"
    response = http.get(
        path, params={"part_id": pid, "status": "pending"}, headers=native(authority[0])
    )
    assert response.status_code == 200, response.text
    assert [row["job_id"] for row in response.json()["data"]] == [result["job_id"]]
    assert (
        http.get(path, params={"part_id": other}, headers=native(authority[0])).json()[
            "data"
        ]
        == []
    )
    assert http.get(path, headers=native(authority[1])).json()["data"] == []
    invalid = http.get(
        path,
        params={"part_id": pid, "inbox_id": str(uuid4())},
        headers=native(authority[0]),
    )
    assert invalid.status_code == 422


def test_inbox_history_filters_recognition_kind_and_resource(db):
    inbox = uuid4()
    expected = insert(db, 1, kind="enrichment.recognize", resource_id=str(inbox))
    insert(db, 2, kind="enrichment.recognize", resource_id=str(uuid4()))
    insert(db, 3, kind="media.verify_upload", resource_id=str(inbox))
    db.commit()
    assert [row["job_id"] for row in page(db, inbox_id=inbox)["data"]] == [expected]
