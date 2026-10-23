"""Independent regressions for manual auxiliary workflow boundaries."""

from sqlalchemy.orm import sessionmaker
import pytest

from app.db import engine
from app.pn_catalog import jobs, media
from app.pn_catalog.enrichment_dto import CreateBundle
from app.pn_catalog.errors import CatalogError
from app.services import pn_catalog_enrichment as commands
from tests.test_pn_catalog_enrichment import handlers, Storage
from tests.test_pn_catalog_media import context, allow, seed, create_ticket, add


def test_bundle_download_requires_original_submitter_even_when_other_actor_can_read_gallery(
    db,
):
    pid = seed(db)
    ticket = create_ticket(db, pid)
    add(db, pid, ticket)
    db.commit()
    snapshot = media.bundle_snapshot(
        db, pid, grade="good", context=context(), authorize=allow
    )
    commands.bundle(
        db,
        pid,
        CreateBundle(
            expected_identity_revision=0,
            expected_gallery_revision=snapshot["gallery_revision"],
            grade="good",
        ),
        context=context("account:42"),
        authorize=allow,
    )
    db.commit()
    result = jobs.run_once(
        sessionmaker(engine),
        worker_id="independent-review",
        queues=["enrichment"],
        handlers=handlers(),
    )
    assert result.status == "succeeded"
    other = context("account:99")
    # This actor can read and bundle the same PN, but does not own this job.
    media.bundle_snapshot(db, pid, grade="good", context=other, authorize=allow)
    with pytest.raises(CatalogError) as exc:
        commands.bundle_downloads(
            db, result.job_id, context=other, authorize=allow, storage=Storage()
        )
    assert exc.value.code == "auth.forbidden"


@pytest.mark.parametrize("action", ["job.read", "job.cancel", "job.retry"])
def test_auxiliary_job_adapter_denies_other_submitter_even_with_resource_grants(
    db, action
):
    from tests.test_pn_catalog_enrichment import research_command
    from uuid import UUID

    pid = seed(db)
    submitted = commands.research(
        db, pid, research_command(), context=context("account:42"), authorize=allow
    )
    db.commit()
    job = jobs.get_job(db, UUID(submitted["data"]["job_id"]))
    with pytest.raises(CatalogError) as exc:
        commands.authorize_job(db, context("account:99"), action, job, authorize=allow)
    assert exc.value.code == "auth.forbidden"


def test_expired_research_lease_after_provider_response_publishes_no_candidates(db):
    from datetime import timedelta
    from sqlalchemy import func, select
    from app.pn_catalog.content_models import ContentCandidate
    from app.pn_catalog.job_models import CatalogJob
    from tests.test_pn_catalog_enrichment import research_command, Provider, Cloud
    from app.services.pn_enrichment_execution import enrichment_handlers

    pid = seed(db)
    commands.research(db, pid, research_command(), context=context(), authorize=allow)
    db.commit()

    def expire():
        with sessionmaker(engine)() as other:
            row = other.scalar(
                select(CatalogJob).where(CatalogJob.kind == "enrichment.research")
            )
            row.lease_expires_at = other.scalar(
                select(func.clock_timestamp())
            ) - timedelta(seconds=1)
            other.commit()

    handler = enrichment_handlers(
        sessionmaker(engine),
        authorize=allow,
        storage=Storage(),
        cloud=Cloud(),
        provider_factory=lambda execution: Provider(execution, expire),
    )
    result = jobs.run_once(
        sessionmaker(engine),
        worker_id="expired-review",
        queues=["enrichment"],
        handlers=handler,
    )
    assert (
        result.status == "running"
    )  # expired lease can be reclaimed, never late-published
    assert db.scalar(select(func.count()).select_from(ContentCandidate)) == 0


def test_revoked_actor_after_provider_response_publishes_no_candidates(db):
    from sqlalchemy import func, select
    from app.pn_catalog.content_models import ContentCandidate
    from tests.test_pn_catalog_enrichment import research_command, Provider, Cloud
    from app.services.pn_enrichment_execution import enrichment_handlers

    pid = seed(db)
    revoked = False

    def checked(*args):
        if revoked:
            raise CatalogError("auth.forbidden", "revoked", status_code=403)
        return allow(*args)

    commands.research(db, pid, research_command(), context=context(), authorize=checked)
    db.commit()

    def revoke():
        nonlocal revoked
        revoked = True

    handler = enrichment_handlers(
        sessionmaker(engine),
        authorize=checked,
        storage=Storage(),
        cloud=Cloud(),
        provider_factory=lambda execution: Provider(execution, revoke),
    )
    result = jobs.run_once(
        sessionmaker(engine),
        worker_id="revoked-review",
        queues=["enrichment"],
        handlers=handler,
    )
    assert result.status == "failed" and result.error_code == "auth.forbidden"
    assert db.scalar(select(func.count()).select_from(ContentCandidate)) == 0
