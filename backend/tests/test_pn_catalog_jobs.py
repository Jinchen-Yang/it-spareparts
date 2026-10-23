"""Durable queue contracts against PostgreSQL, including concurrent claim/reclaim."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.db import engine
from app.models.dimensions import DimPart
from app.pn_catalog.dto import ActorContext, CommandContext
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.job_dto import JobSpec
from app.pn_catalog.job_models import CatalogJob, CatalogJobEvent
from app.pn_catalog import jobs


def context():
    return CommandContext(actor=ActorContext(principal_id="account:42", client_id="itsp",
                          executor_id="itsp-api", entrypoint="itsp_ui"), request_id=uuid4())


def spec(**changes):
    return JobSpec(kind="test.prepare", queue="media", resource_type="gallery_entry",
                   resource_id=str(uuid4()), resource_generation=1, input_revision=3,
                   deduplication_key=str(uuid4()), **changes)


def enqueue_claim(db, **changes):
    item = jobs.enqueue(db, spec(**changes), context=context())
    db.commit()
    lease = jobs.claim(db, worker_id="worker:1", queues=["media"])
    db.commit()
    assert lease.job_id == item.job_id
    return lease


def expire(db, lease):
    db.execute(text("UPDATE pn_catalog_job SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": lease.job_id})
    db.commit()


def verify(_db, lease):
    return lease.resource_generation, lease.input_revision


def test_enqueue_deduplication_and_atomic_rollback(db):
    request = spec()
    first = jobs.enqueue(db, request, context=context())
    assert jobs.enqueue(db, request, context=context()).job_id == first.job_id
    with pytest.raises(CatalogError, match="输入已改变"):
        jobs.enqueue(db, request.model_copy(update={"input_revision": 9}), context=context())
    db.rollback()
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 0
    assert db.scalar(select(func.count()).select_from(CatalogJobEvent)) == 0


def test_concurrent_claims_skip_locked_jobs(db):
    expected = {jobs.enqueue(db, spec(), context=context()).job_id for _ in range(2)}
    db.commit()
    barrier = Barrier(2)
    def worker(index):
        with Session(engine) as session:
            lease = jobs.claim(session, worker_id=f"worker:{index}", queues=["media"])
            barrier.wait(timeout=5)  # Both hold their claims simultaneously.
            session.commit()
            return lease.job_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        actual = set(pool.map(worker, range(2)))
    assert actual == expected


def test_reclaimed_lease_rejects_stale_publication_and_heartbeat(db):
    old = enqueue_claim(db)
    expire(db, old)
    current = jobs.claim(db, worker_id="worker:2", queues=["media"])
    db.commit()
    assert current.attempt == old.attempt + 1
    assert current.lease_epoch > old.lease_epoch and current.lease_token != old.lease_token
    called = []
    with pytest.raises(CatalogError) as caught:
        jobs.complete(db, old, verify_resource=verify, publish=lambda *_: called.append(True))
    assert caught.value.code == "job.lease_lost" and not called
    db.rollback()
    with pytest.raises(CatalogError):
        jobs.heartbeat(db, old)
    db.rollback()
    result = jobs.complete(db, current, verify_resource=verify, publish=lambda *_: {"asset_id": "new"})
    assert result.status == "succeeded" and result.result == {"asset_id": "new"}


@pytest.mark.parametrize("field,value", [("worker_id", "intruder"), ("lease_token", uuid4()),
    ("lease_epoch", 99), ("attempt", 99), ("resource_generation", 999), ("input_revision", 999)])
def test_all_fence_axes_are_checked(db, field, value):
    lease = enqueue_claim(db)
    forged = lease.model_copy(update={field: value})
    with pytest.raises(CatalogError) as caught:
        jobs.complete(db, forged, verify_resource=verify, publish=lambda *_: pytest.fail("published"))
    assert caught.value.code == "job.lease_lost"


def test_cancel_and_explicit_retry_preserve_logical_job_inputs(db):
    old = enqueue_claim(db)
    cancelled = jobs.cancel(db, old.job_id, context=context())
    db.commit()
    assert cancelled.status == "cancelled"
    retry = jobs.retry(db, old.job_id, expected_attempt=1, expected_retry_count=0, context=context())
    db.commit()
    assert retry.job_id == old.job_id and retry.retry_count == 1 and retry.attempt == 1
    new = jobs.claim(db, worker_id="worker:2", queues=["media"])
    db.commit()
    assert new.attempt == 2 and new.payload == old.payload
    assert (new.resource_generation, new.input_revision) == (old.resource_generation, old.input_revision)
    with pytest.raises(CatalogError):
        jobs.complete(db, old, verify_resource=verify, publish=lambda *_: pytest.fail("published"))
    db.rollback()
    with pytest.raises(CatalogError) as caught:
        jobs.retry(db, old.job_id, expected_attempt=1, expected_retry_count=0, context=context())
    assert caught.value.code == "job.retry_conflict"


def test_live_resource_generation_and_revision_guard_publication(db):
    lease = enqueue_claim(db)
    current = jobs.complete(db, lease, verify_resource=lambda *_: (2, 3),
                            publish=lambda *_: pytest.fail("stale publication"))
    assert current.status == "stale" and current.error_code == "job.resource_changed"


def test_expired_without_reclaim_cannot_publish(db):
    lease = enqueue_claim(db)
    expire(db, lease)
    with pytest.raises(CatalogError) as caught:
        jobs.complete(db, lease, verify_resource=verify, publish=lambda *_: pytest.fail("published"))
    assert caught.value.code == "job.lease_lost"


def test_retry_limit_and_explicit_new_cycle(db):
    lease = enqueue_claim(db, max_attempts=1)
    failed = jobs.fail(db, lease, code="oss.unavailable", retryable=True, delay_seconds=0)
    db.commit()
    assert failed.status == "failed" and failed.attempt == 1
    assert jobs.claim(db, worker_id="other", queues=["media"]) is None
    jobs.retry(db, lease.job_id, expected_attempt=1, expected_retry_count=0, context=context())
    db.commit()
    assert jobs.claim(db, worker_id="other", queues=["media"]).attempt == 2


def test_crashed_final_attempt_becomes_terminal(db):
    lease = enqueue_claim(db, max_attempts=1)
    expire(db, lease)
    assert jobs.claim(db, worker_id="other", queues=["media"]) is None
    db.commit()
    assert jobs.get_job(db, lease.job_id).error_code == "job.attempts_exhausted"


def test_network_stage_has_no_open_db_connection_and_failure_rolls_back_publish(db):
    part = DimPart(pn_std="JOB-RESOURCE", description="before")
    db.add(part)
    db.commit()
    part_id = part.id
    db.rollback()  # release lazy reload transaction before checking pool state
    job = jobs.enqueue(db, spec(), context=context())
    db.commit()
    observed = []
    def prepare(_lease):
        observed.append(engine.pool.checkedout())
        return {"file": "prepared"}
    def publish(session, _lease, result):
        session.get(DimPart, part_id).description = result["file"]
        session.flush()
        raise jobs.JobExecutionFailure("test.transient", delay_seconds=0)
    handler = jobs.JobHandler(prepare, verify, publish)
    result = jobs.run_once(sessionmaker(engine), worker_id="runner", queues=["media"],
                           handlers={"test.prepare": handler})
    assert observed == [0]
    assert result.job_id == job.job_id and result.status == "pending"
    assert db.get(DimPart, part_id, populate_existing=True).description == "before"


def test_job_inputs_and_events_are_immutable_in_database(db):
    lease = enqueue_claim(db)
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE pn_catalog_job SET input_revision=5 WHERE id=:id"), {"id": lease.job_id})
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM pn_catalog_job_event WHERE job_id=:id"), {"id": lease.job_id})
    db.rollback()
    assert db.scalar(select(func.count()).select_from(CatalogJobEvent)) == 2


def test_public_cancel_retry_guards_and_receipt_replay(db):
    from app.pn_catalog.dto import AuthorizationDecision
    from app.pn_catalog.job_dto import CancelJob, RetryJob
    from app.pn_catalog.models import CommandReceipt
    def authorize(*args):
        return AuthorizationDecision(frozenset({"*"}), frozenset({"*"}), {"scope": "test-resource"})
    lease = enqueue_claim(db)
    view = jobs.get_job(db, lease.job_id)
    ctx = context()
    cancel = CancelJob(expected_revision=view.revision, expected_attempt=view.attempt,
                       expected_retry_count=view.retry_count)
    first = jobs.cancel_job(db, lease.job_id, cancel, context=ctx, authorize_job=authorize)
    db.commit()
    second = jobs.cancel_job(db, lease.job_id, cancel, context=ctx, authorize_job=authorize)
    assert second["meta"]["replayed"] is True
    assert first["meta"]["audit_id"] == second["meta"]["audit_id"]
    current = jobs.get_job(db, lease.job_id)
    jobs.retry_job(db, lease.job_id, RetryJob(expected_revision=current.revision,
        expected_attempt=current.attempt, expected_retry_count=current.retry_count),
        context=context(), authorize_job=authorize)
    db.commit()
    # Lost-response replay does not cancel the new explicit retry cycle.
    replay = jobs.cancel_job(db, lease.job_id, cancel, context=ctx, authorize_job=authorize)
    assert replay["data"]["status"] == "pending" and replay["meta"]["replayed"]
    with pytest.raises(CatalogError) as conflict:
        jobs.cancel_job(db, lease.job_id, cancel, context=context(), authorize_job=authorize)
    assert conflict.value.code == "job.version_conflict"
    db.rollback()
    def revoked(*args):
        raise CatalogError("auth.forbidden", "revoked", status_code=403)
    with pytest.raises(CatalogError):
        jobs.cancel_job(db, lease.job_id, cancel, context=ctx, authorize_job=revoked)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 2
    visible = jobs.get_job_authorized(db, lease.job_id, context=context(), authorize_job=authorize)["data"]
    assert not ({"payload", "context", "lease_token", "lease_owner", "payload_hash"} & set(visible))


def test_job_migration_preserves_existing_data_and_refuses_durable_work_loss(db):
    from alembic import command as alembic_command
    from tests.conftest import _alembic_cfg
    db.add(DimPart(pn_std="PRE-QUEUE"))
    db.commit()
    alembic_command.downgrade(_alembic_cfg(), "a3c6e9f2b5d8")
    alembic_command.upgrade(_alembic_cfg(), "head")
    assert db.scalar(select(DimPart.pn_std)) == "PRE-QUEUE"
    item = jobs.enqueue(db, spec(), context=context())
    db.commit()
    with pytest.raises(RuntimeError, match="durable work"):
        alembic_command.downgrade(_alembic_cfg(), "a3c6e9f2b5d8")
    assert jobs.get_job(db, item.job_id).status == "pending"


@pytest.mark.parametrize("failure", ["revoked", "generation"])
def test_runner_preflight_blocks_provider_io_after_revocation_or_resource_change(db, failure):
    jobs.enqueue(db, spec(), context=context())
    db.commit()
    def verifier(session, lease):
        if failure == "revoked":
            raise CatalogError("auth.forbidden", "撤权", status_code=403)
        return lease.resource_generation + 1, lease.input_revision
    handler = jobs.JobHandler(lambda _: pytest.fail("provider I/O before current authorization"),
                              verifier, lambda *_: pytest.fail("publication"))
    result = jobs.run_once(sessionmaker(engine), worker_id="preflight", queues=["media"],
                           handlers={"test.prepare": handler})
    assert result.status == ("failed" if failure == "revoked" else "stale")
