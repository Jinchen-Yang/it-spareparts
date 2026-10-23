"""Real authority crosses HTTP, durable job context, worker and live resource checks."""
# ruff: noqa: F811 -- imported pytest fixtures are resolved by argument name.

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.authz.pn_models import CatalogSubjectBinding
from app.db import engine
from app.models.authorization import SysAuthorizationGrant
from app.models.dimensions import DimPart
from app.models.system import SysUser
from app.pn_catalog import jobs, media
from app.pn_catalog.content_models import ContentCandidate
from app.pn_catalog.dto import CommandContext
from app.pn_catalog.job_models import CatalogJob
from app.pn_catalog.media_dto import CreateUpload, ExpectedRevision
from app.pn_catalog.media_models import Asset, UploadTicket
from app.services import pn_catalog_access as access, pn_catalog_identity as identity
from app.services.pn_catalog_runtime import original_actor_authorize
from app.services.pn_enrichment_execution import enrichment_handlers
from app.services.pn_media_execution import media_handlers
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_pn_catalog_access import authority, identity as verified  # noqa: F401
from tests.test_pn_catalog_authority_http import http, native, exchange  # noqa: F401
from tests.test_pn_catalog_enrichment import Provider, Cloud, Storage, research_command
from tests.test_pn_media_execution import FakeStorage, FakeCloud


def seed(db, pn="AUTH-JOB-1"):
    part = DimPart(pn_std=pn, description="A")
    db.add(part)
    db.commit()
    return part.id


def request(http, authority, client, method, path, data=None):
    body = json.dumps(data, separators=(",", ":")).encode() if data is not None else b""
    if client == "shop":
        headers, _ = exchange(http, authority[2], method, path, body)
    else:
        headers = native(authority[0]) | {"Idempotency-Key": str(uuid4())}
    if data is not None:
        headers["Content-Type"] = "application/json"
    return http.request(method, path, content=body, headers=headers)


def research(http, authority, client, pid):
    response = request(
        http,
        authority,
        client,
        "POST",
        f"/api/pn-catalog/v1/parts/{pid}/research",
        research_command().model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def run_research(mutate=None):
    handlers = enrichment_handlers(
        sessionmaker(engine),
        authorize=original_actor_authorize,
        storage=Storage(),
        cloud=Cloud(),
        provider_factory=lambda execution: Provider(execution, mutate),
    )
    return jobs.run_once(
        sessionmaker(engine),
        worker_id="authority-research",
        queues=["enrichment"],
        handlers=handlers,
    )


@pytest.mark.parametrize("client", ["itsp", "shop"])
def test_http_research_worker_and_receipt_use_original_authority(
    db, authority, http, client
):
    pid = seed(db)
    result = research(http, authority, client, pid)
    row = db.get(CatalogJob, UUID(result["job_id"]))
    assert row.context["actor"]["principal_id"] == f"account:{authority[0].id}"
    assert row.context["actor"]["authorization_epoch"] == 4
    assert row.context["actor"]["mapping_version"] == (1 if client == "shop" else None)
    db.rollback()
    completed = run_research()
    assert completed.status == "succeeded", completed
    assert db.scalar(select(func.count()).select_from(ContentCandidate)) == 2
    db.rollback()
    projected = request(
        http, authority, client, "GET", f"/api/pn-catalog/v1/jobs/{result['job_id']}"
    )
    assert projected.status_code == 200, projected.text
    assert projected.json()["data"]["status"] == "succeeded"
    assert (
        "payload" not in projected.text and "authorization_epoch" not in projected.text
    )


@pytest.mark.parametrize("revoke", ["epoch", "mapping", "grant"])
def test_actual_revocation_after_provider_response_prevents_late_candidates(
    db, authority, http, revoke
):
    pid = seed(db)
    uid, subject = authority[0].id, authority[2]
    research(http, authority, "shop", pid)
    db.rollback()

    def mutation():
        with Session(engine) as other:
            if revoke == "epoch":
                other.get(SysUser, uid).token_version += 1
            elif revoke == "mapping":
                binding = other.get(CatalogSubjectBinding, ("shop", subject))
                binding.active = False
                binding.version += 1
            else:
                grant = other.scalar(
                    select(SysAuthorizationGrant).where(
                        SysAuthorizationGrant.user_id == uid,
                        SysAuthorizationGrant.resource == "part_master",
                    )
                )
                grant.actions = ["read"]
                grant.version += 1
            other.commit()

    result = run_research(mutation)
    assert result.status == "failed", result
    assert result.error_code.startswith("auth.")
    assert db.scalar(select(func.count()).select_from(ContentCandidate)) == 0


def test_media_job_authority_owner_and_live_upload_scope(db, authority, http):
    pid = seed(db)
    resolved = access.resolve_native_access(db, verified(authority[0]))
    context = CommandContext(actor=resolved.actor, request_id=uuid4())
    upload = media.create_upload(
        db,
        CreateUpload(
            client_request_id=uuid4(),
            filename="test.jpg",
            size_bytes=100,
            content_type="image/jpeg",
            part_id=pid,
            expected_identity_revision=0,
        ),
        context=context,
        authorize=resolved.authorize,
    )
    upload_id = UUID(upload["data"]["upload_id"])
    media.complete_upload(
        db,
        upload_id,
        ExpectedRevision(expected_revision=1),
        context=context.model_copy(update={"request_id": uuid4()}),
        authorize=resolved.authorize,
    )
    db.commit()
    row = db.scalar(select(CatalogJob).where(CatalogJob.kind == "media.verify_upload"))
    path = f"/api/pn-catalog/v1/jobs/{row.id}"
    db.rollback()
    response = http.get(path, headers=native(authority[0]))
    assert response.status_code == 200, response.text
    # Give another account exactly the same actual grants: ownership still matters.
    for grant in list(
        db.scalars(
            select(SysAuthorizationGrant).where(
                SysAuthorizationGrant.user_id == authority[0].id
            )
        )
    ):
        db.add(
            SysAuthorizationGrant(
                user_id=authority[1].id,
                resource=grant.resource,
                actions=grant.actions,
                scope=grant.scope,
                field_permissions=grant.field_permissions,
                version=1,
            )
        )
    db.commit()
    denied = http.get(path, headers=native(authority[1]))
    assert denied.status_code == 403, denied.text
    db.rollback()
    handlers = media_handlers(
        storage=FakeStorage(),
        cloud=FakeCloud(),
        authorize=original_actor_authorize,
        session_factory=sessionmaker(engine),
    )
    result = jobs.run_once(
        sessionmaker(engine),
        worker_id="authority-media",
        queues=["media"],
        handlers=handlers,
    )
    assert result.status == "succeeded", result
    assert db.get(UploadTicket, upload_id).status == "ready"
    assert db.scalar(select(func.count()).select_from(Asset)) == 1


def merge(db, authority):
    source, target = seed(db, "AUTH-SOURCE"), seed(db, "AUTH-TARGET")
    resolved = access.resolve_native_access(db, verified(authority[0]))
    context = CommandContext(actor=resolved.actor, request_id=uuid4())
    preview = identity.preview(
        db,
        identity.IdentityPreview(
            operation="merge", source_part_id=source, target_part_id=target
        ),
        context=context,
        authorize=resolved.authorize,
    )
    command = identity.MergeIdentity(
        source_part_id=source,
        target_part_id=target,
        expected={
            key: identity.ExpectedPart(**value)
            for key, value in preview["expected"].items()
        },
    )
    result = identity.execute(
        db, command, context=context, authorize=resolved.authorize
    )
    db.commit()
    return result["data"]["recalculation_job"]["job_id"]


@pytest.mark.parametrize("revoke_operation", [False, True])
def test_identity_job_checks_actual_identity_resource_and_original_operation(
    db, authority, http, revoke_operation
):
    job_id = merge(db, authority)
    path = f"/api/pn-catalog/v1/jobs/{job_id}"
    response = http.get(path, headers=native(authority[0]))
    assert response.status_code == 200, response.text
    if revoke_operation:
        grant = db.scalar(
            select(SysAuthorizationGrant).where(
                SysAuthorizationGrant.user_id == authority[0].id,
                SysAuthorizationGrant.resource == "part_identity",
            )
        )
        grant.actions = [a for a in grant.actions if a != "merge"]
        grant.version += 1
        db.commit()
        denied = http.get(path, headers=native(authority[0]))
        assert denied.status_code == 403, denied.text
    db.rollback()
    result = jobs.run_once(
        sessionmaker(engine),
        worker_id="authority-identity",
        queues=["maintenance"],
        handlers={
            "identity.recalculate": identity.recalculation_handler(
                original_actor_authorize
            )
        },
        lease_seconds=600,
    )
    assert result.status == ("failed" if revoke_operation else "succeeded"), result
    if revoke_operation:
        assert result.error_code == "auth.forbidden"
