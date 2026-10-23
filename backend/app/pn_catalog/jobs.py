"""Durable at-least-once work with leased, fenced publication.

Functions never commit. A claimant commits its short claim transaction before doing
network I/O. Completion locks/checks the resource first, then the job, and publishes
only after checking the lease token, epoch, expiry and immutable input versions.
"""
import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from .dto import AuthorizationDecision, CommandContext
from .errors import CatalogError
from .job_dto import CancelJob, JobLease, JobSpec, JobView, RetryJob
from .job_models import CatalogJob, CatalogJobEvent

ResourceVerifier = Callable[[Session, JobLease], tuple[int, int] | None]
Publisher = Callable[[Session, JobLease], dict | None]


def _bounded_json(value: dict, *, limit: int = 65536) -> str:
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CatalogError("job.invalid_payload", "任务内容不是有效 JSON", status_code=422) from exc
    if len(result.encode()) > limit:
        raise CatalogError("job.invalid_payload", "任务内容过大", status_code=422)
    return result


def _now(db: Session):
    # Transaction timestamps can be old after waiting for a resource lock.
    return db.scalar(select(func.clock_timestamp()))


def _view(job: CatalogJob) -> JobView:
    return JobView(job_id=job.id, kind=job.kind, queue=job.queue, status=job.status,
        resource_type=job.resource_type, resource_id=job.resource_id, part_id=job.part_id,
        resource_generation=job.resource_generation, input_revision=job.input_revision,
        attempt=job.attempt, retry_count=job.retry_count, revision=job.revision, max_attempts=job.max_attempts,
        available_at=job.available_at, created_at=job.created_at, updated_at=job.updated_at,
        finished_at=job.finished_at, error_code=job.error_code, result=job.result)


def _lease(job: CatalogJob) -> JobLease:
    return JobLease(job_id=job.id, kind=job.kind, queue=job.queue,
        resource_type=job.resource_type, resource_id=job.resource_id, part_id=job.part_id,
        resource_generation=job.resource_generation, input_revision=job.input_revision,
        payload=job.payload, context=job.context, worker_id=job.lease_owner,
        lease_token=job.lease_token, lease_epoch=job.lease_epoch,
        lease_expires_at=job.lease_expires_at, attempt=job.attempt)


def _event(db: Session, job: CatalogJob, event_type: str, *, context: dict | None = None,
           worker_id: str | None = None, code: str | None = None, state_change: bool = True) -> None:
    if state_change:
        job.revision += 1
    db.add(CatalogJobEvent(job_id=job.id, event_type=event_type, status=job.status,
        attempt=job.attempt, lease_epoch=job.lease_epoch, worker_id=worker_id,
        code=code, context=context or {}))
    db.flush()


def _locked_job(db: Session, job_id: UUID) -> CatalogJob:
    job = db.scalar(select(CatalogJob).where(CatalogJob.id == job_id).with_for_update()
                    .execution_options(populate_existing=True))
    if job is None:
        raise CatalogError("job.not_found", "任务不存在", status_code=404)
    return job


def _clear_lease(job: CatalogJob) -> None:
    job.lease_token = job.lease_owner = job.lease_expires_at = None


def _fence(db: Session, job: CatalogJob, lease: JobLease):
    now = _now(db)
    if (job.status != "running" or job.lease_owner != lease.worker_id
            or job.lease_token != lease.lease_token or job.lease_epoch != lease.lease_epoch
            or job.attempt != lease.attempt or job.lease_expires_at is None
            or job.lease_expires_at <= now
            or job.resource_generation != lease.resource_generation
            or job.input_revision != lease.input_revision):
        raise CatalogError("job.lease_lost", "任务执行权已失效，不能发布旧结果")
    return now


def enqueue(db: Session, spec: JobSpec, *, context: CommandContext) -> JobView:
    """Host-authorized enqueue; resource write, audit and enqueue share one transaction."""
    encoded = _bounded_json(spec.model_dump(mode="json"),
                            limit={"enrichment.bundle": 512 * 1024, "enrichment.research": 128 * 1024}.get(spec.kind, 65536))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    lock = int.from_bytes(hashlib.sha256(
        f"pn-job:{spec.kind}:{spec.deduplication_key}".encode()).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    existing = db.scalar(select(CatalogJob).where(CatalogJob.kind == spec.kind,
        CatalogJob.deduplication_key == spec.deduplication_key))
    if existing is not None:
        if existing.payload_hash != digest:
            raise CatalogError("job.idempotency_mismatch", "同一任务标识的输入已改变")
        return _view(existing)
    job = CatalogJob(**spec.model_dump(exclude={"deduplication_key"}),
        deduplication_key=spec.deduplication_key, payload_hash=digest,
        context=context.model_dump(mode="json"))
    db.add(job)
    db.flush()
    _event(db, job, "enqueued", context=context.model_dump(mode="json"))
    return _view(job)


def claim(db: Session, *, worker_id: str, queues: Iterable[str],
          kinds: Iterable[str] | None = None, lease_seconds: int = 60) -> JobLease | None:
    """Claim/reclaim without resource locks. Commit before contacting external services."""
    queue_names = sorted(set(queues))
    kind_names = sorted(set(kinds)) if kinds is not None else None
    if not worker_id or len(worker_id) > 128 or not queue_names or len(queue_names) > 20:
        raise CatalogError("job.invalid_worker", "任务执行者或队列无效", status_code=422)
    if not 1 <= lease_seconds <= 3600:
        raise CatalogError("job.invalid_lease", "租约时长无效", status_code=422)
    for _ in range(100):
        now = _now(db)
        eligible = or_(and_(CatalogJob.status == "pending", CatalogJob.available_at <= now),
                       and_(CatalogJob.status == "running", CatalogJob.lease_expires_at <= now))
        query = select(CatalogJob).where(CatalogJob.queue.in_(queue_names), eligible)
        if kind_names is not None:
            query = query.where(CatalogJob.kind.in_(kind_names))
        job = db.scalar(query.order_by(CatalogJob.available_at, CatalogJob.created_at, CatalogJob.id)
                        .with_for_update(skip_locked=True).limit(1)
                        .execution_options(populate_existing=True))
        if job is None:
            return None
        now = _now(db)
        if job.attempt_in_cycle >= job.max_attempts:
            job.status, job.error_code = "failed", "job.attempts_exhausted"
            job.finished_at = job.updated_at = now
            _clear_lease(job)
            _event(db, job, "exhausted", worker_id=worker_id, code=job.error_code)
            continue
        reclaimed = job.status == "running"
        job.status = "running"
        job.attempt += 1
        job.attempt_in_cycle += 1
        job.lease_epoch += 1
        job.lease_token = uuid4()
        job.lease_owner = worker_id
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.updated_at = now
        job.error_code = None
        _event(db, job, "reclaimed" if reclaimed else "claimed", worker_id=worker_id)
        return _lease(job)
    # A bounded sweep cannot monopolize the queue; callers commit exhausted transitions.
    return None


def heartbeat(db: Session, lease: JobLease, *, lease_seconds: int = 60) -> JobLease:
    if not 1 <= lease_seconds <= 3600:
        raise CatalogError("job.invalid_lease", "租约时长无效", status_code=422)
    job = _locked_job(db, lease.job_id)
    now = _fence(db, job, lease)
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.updated_at = now
    db.flush()
    return _lease(job)


def complete(db: Session, lease: JobLease, *, verify_resource: ResourceVerifier,
             publish: Publisher) -> JobView:
    """Verifier locks resource and checks live permissions; publisher performs DB work only."""
    current = verify_resource(db, lease)
    job = _locked_job(db, lease.job_id)
    now = _fence(db, job, lease)
    if current != (job.resource_generation, job.input_revision):
        job.status, job.error_code = "stale", "job.resource_changed"
        job.finished_at = job.updated_at = now
        _clear_lease(job)
        _event(db, job, "stale", worker_id=lease.worker_id, code=job.error_code)
        return _view(job)
    result = publish(db, lease) or {}
    _bounded_json(result)
    now = _fence(db, job, lease)
    job.status, job.result = "succeeded", result
    job.finished_at = job.updated_at = now
    job.error_code = None
    _clear_lease(job)
    _event(db, job, "succeeded", worker_id=lease.worker_id)
    return _view(job)


def fail(db: Session, lease: JobLease, *, code: str, retryable: bool,
         delay_seconds: int = 30) -> JobView:
    if not code or len(code) > 128 or not 0 <= delay_seconds <= 86400:
        raise CatalogError("job.invalid_failure", "任务失败代码或等待时长无效", status_code=422)
    job = _locked_job(db, lease.job_id)
    now = _fence(db, job, lease)
    retry = retryable and job.attempt_in_cycle < job.max_attempts
    job.status = "pending" if retry else "failed"
    job.error_code = code
    job.updated_at = now
    job.available_at = now + timedelta(seconds=delay_seconds) if retry else now
    job.finished_at = None if retry else now
    _clear_lease(job)
    _event(db, job, "retry_scheduled" if retry else "failed", worker_id=lease.worker_id, code=code)
    return _view(job)


def cancel(db: Session, job_id: UUID, *, context: CommandContext) -> JobView:
    job = _locked_job(db, job_id)
    if job.status == "cancelled":
        return _view(job)
    if job.status in {"succeeded", "stale"}:
        raise CatalogError("job.not_cancellable", "任务已经结束，不能取消")
    job.status, job.error_code = "cancelled", "job.cancelled"
    job.lease_epoch += 1
    job.finished_at = job.updated_at = _now(db)
    _clear_lease(job)
    _event(db, job, "cancelled", context=context.model_dump(mode="json"))
    return _view(job)


def retry(db: Session, job_id: UUID, *, expected_attempt: int, expected_retry_count: int,
          context: CommandContext) -> JobView:
    job = _locked_job(db, job_id)
    if (job.status not in {"failed", "cancelled"} or job.attempt != expected_attempt
            or job.retry_count != expected_retry_count):
        raise CatalogError("job.retry_conflict", "任务状态已变化，请重新核对")
    job.status, job.error_code, job.finished_at = "pending", None, None
    job.retry_count += 1
    job.attempt_in_cycle = 0
    job.lease_epoch += 1
    job.available_at = job.updated_at = _now(db)
    _clear_lease(job)
    _event(db, job, "retried", context=context.model_dump(mode="json"))
    return _view(job)


def get_job(db: Session, job_id: UUID) -> JobView:
    job = db.get(CatalogJob, job_id, populate_existing=True)
    if job is None:
        raise CatalogError("job.not_found", "任务不存在", status_code=404)
    return _view(job)


@dataclass(frozen=True)
class JobHandler:
    prepare: Callable[[JobLease], object]
    verify_resource: ResourceVerifier
    publish: Callable[[Session, JobLease, object], dict | None]


class JobExecutionFailure(Exception):
    def __init__(self, code: str, *, retryable: bool = True, delay_seconds: int = 30):
        super().__init__(code)
        self.code, self.retryable, self.delay_seconds = code, retryable, delay_seconds


def run_once(session_factory, *, worker_id: str, queues: Iterable[str],
             handlers: dict[str, JobHandler], lease_seconds: int = 60) -> JobView | None:
    """Optional worker driver: no open DB session while prepare() performs I/O."""
    with session_factory() as db:
        lease = claim(db, worker_id=worker_id, queues=queues, kinds=handlers,
                      lease_seconds=lease_seconds)
        db.commit()
    if lease is None:
        return None
    handler = handlers[lease.kind]
    try:
        # Reject revoked actors or stale resources before any provider side effect.
        # The authoritative resource locks are held only in this short transaction.
        with session_factory() as db:
            current = handler.verify_resource(db, lease)
            if current != (lease.resource_generation, lease.input_revision):
                result = complete(db, lease, verify_resource=lambda *_: current,
                                  publish=lambda *_: None)
                db.commit()
                return result
            lease = heartbeat(db, lease, lease_seconds=lease_seconds)
            db.commit()
        prepared = handler.prepare(lease)
        with session_factory() as db:
            result = complete(db, lease, verify_resource=handler.verify_resource,
                              publish=lambda session, current: handler.publish(session, current, prepared))
            db.commit()
            return result
    except Exception as exc:
        if isinstance(exc, CatalogError) and exc.code == "job.lease_lost":
            with session_factory() as db:
                return get_job(db, lease.job_id)
        failure = exc if isinstance(exc, JobExecutionFailure) else JobExecutionFailure(
            exc.code if isinstance(exc, CatalogError) else "job.execution_failed",
            retryable=exc.retryable if isinstance(exc, CatalogError) else True)
        with session_factory() as db:
            try:
                result = fail(db, lease, code=failure.code, retryable=failure.retryable,
                              delay_seconds=failure.delay_seconds)
                db.commit()
                return result
            except CatalogError as fence_error:
                if fence_error.code != "job.lease_lost":
                    raise
                db.rollback()
                return get_job(db, lease.job_id)


JobAuthorize = Callable[[Session, CommandContext, str, JobView], AuthorizationDecision]


def _authorize_public(db, context, action, job, authorize_job):
    decision = authorize_job(db, context, action, job)
    if not isinstance(decision, AuthorizationDecision):
        raise CatalogError("auth.unavailable", "权限服务不可用", status_code=503, retryable=True)
    return decision


def get_job_authorized(db: Session, job_id: UUID, *, context: CommandContext,
                       authorize_job: JobAuthorize) -> dict:
    job = get_job(db, job_id)
    _authorize_public(db, context, "job.read", job, authorize_job)
    return {"data": job.model_dump(mode="json"), "meta": {"data_revision": job.revision}}


def project_receipt(db, receipt, *, context, authorize_job, replayed=True):
    from . import api
    job = get_job(db, UUID(receipt.result["job_id"]))
    _authorize_public(db, context, receipt.operation, job, authorize_job)
    _authorize_public(db, context, "job.read", job, authorize_job)
    return {"data": job.model_dump(mode="json"),
        "meta": api.receipt_meta(receipt, data_revision=job.revision,
                                 identity_revision=None, replayed=replayed)}


def _public_command(db, job_id, command, *, context, authorize_job, action):
    from . import api
    current = get_job(db, job_id)
    handle = api.begin_command(db, context=context,
        authorize=lambda session, ctx, operation, _pid: _authorize_public(
            session, ctx, operation, current, authorize_job),
        operation=action, part_id=current.part_id, resource_type="job", resource_id=job_id,
        payload=command)
    if handle.receipt is not None:
        return project_receipt(db, handle.receipt, context=context, authorize_job=authorize_job)
    locked = _locked_job(db, job_id)
    if (locked.revision != command.expected_revision or locked.attempt != command.expected_attempt
            or locked.retry_count != command.expected_retry_count):
        raise CatalogError("job.version_conflict", "任务已变化，请重新核对")
    previous = _view(locked)
    if action == "job.cancel":
        result = cancel(db, job_id, context=context)
    else:
        result = retry(db, job_id, expected_attempt=command.expected_attempt,
                       expected_retry_count=command.expected_retry_count, context=context)
    audit = api.record_audit(db, current.part_id, context=context, action=action,
        changes={"job_id": str(job_id), "before_status": previous.status, "after_status": result.status,
                 "attempt": result.attempt, "retry_count": result.retry_count},
        before_revision=previous.revision, after_revision=result.revision,
        authorization=handle.decision.metadata, payload_hash=handle.payload_hash,
        revisions={"axis": "job", "before": {"job": previous.revision}, "after": {"job": result.revision}})
    receipt = api.finish_command(db, handle, part_id=current.part_id, applied_revision=result.revision,
        audit_id=audit.id, result={"resource": "job", "job_id": str(job_id)})
    return project_receipt(db, receipt, context=context, authorize_job=authorize_job, replayed=False)


def cancel_job(db: Session, job_id: UUID, command: CancelJob, *, context: CommandContext,
               authorize_job: JobAuthorize) -> dict:
    return _public_command(db, job_id, command, context=context, authorize_job=authorize_job, action="job.cancel")


def retry_job(db: Session, job_id: UUID, command: RetryJob, *, context: CommandContext,
              authorize_job: JobAuthorize) -> dict:
    return _public_command(db, job_id, command, context=context, authorize_job=authorize_job, action="job.retry")


def reserve_execution(db: Session, lease: JobLease, *, capability: str | None = None,
                      tokens: int = 0, searches: int = 0, external_calls: int = 0) -> UUID:
    """Persist an outbound reservation before network I/O. Unknown usage stays reserved."""
    if any(type(value) is not int or value < 0 for value in (tokens, searches, external_calls)):
        raise CatalogError("job.invalid_budget", "任务预算参数无效", status_code=422)
    job = _locked_job(db, lease.job_id)
    _fence(db, job, lease)
    limits = job.payload.get("limits", {})
    if capability and capability not in job.payload.get("capabilities", []):
        raise CatalogError("auth.forbidden", "此次人工请求未授权该能力", status_code=403)
    usage = dict(job.execution_usage or {})
    reserved, settled = usage.get("reserved_tokens", 0), usage.get("settled_tokens", 0)
    if (reserved + settled + tokens > limits.get("tokens", 0)
        or usage.get("searches", 0) + searches > limits.get("searches", 0)
        or usage.get("external_calls", 0) + external_calls > limits.get("external_calls", 0)):
        raise CatalogError("enrichment.budget_exceeded", "此次人工请求额度已用完")
    reservation = uuid4()
    usage.update(reserved_tokens=reserved + tokens,
        searches=usage.get("searches", 0) + searches,
        external_calls=usage.get("external_calls", 0) + external_calls)
    reservations = dict(usage.get("reservations", {}))
    if len(reservations) >= 200:
        raise CatalogError("enrichment.budget_exceeded", "此次人工请求调用次数已用完")
    reservations[str(reservation)] = {"reserved_tokens": tokens, "settled_tokens": None}
    usage["reservations"] = reservations
    job.execution_usage = usage
    _event(db, job, "budget_reserved", worker_id=lease.worker_id, state_change=False,
           context={"reservation_id": str(reservation), "tokens": tokens, "searches": searches,
                    "external_calls": external_calls, "capability": capability})
    return reservation


def settle_execution(db: Session, lease: JobLease, reservation_id: UUID, *, tokens: int) -> None:
    if type(tokens) is not int or tokens < 0 or tokens > 1_000_000_000:
        raise CatalogError("job.invalid_budget", "任务计量无效", status_code=422)
    job = _locked_job(db, lease.job_id)
    _fence(db, job, lease)
    usage = dict(job.execution_usage or {})
    reservations = dict(usage.get("reservations", {}))
    item = reservations.get(str(reservation_id))
    if item is None:
        raise CatalogError("job.reservation_not_found", "任务预算预留不存在")
    if item["settled_tokens"] is not None:
        if item["settled_tokens"] != tokens:
            raise CatalogError("job.reservation_conflict", "任务计量已确认")
        return
    usage["reserved_tokens"] -= item["reserved_tokens"]
    usage["settled_tokens"] = usage.get("settled_tokens", 0) + tokens
    reservations[str(reservation_id)] = {**item, "settled_tokens": tokens}
    usage["reservations"] = reservations
    job.execution_usage = usage
    _event(db, job, "budget_settled", worker_id=lease.worker_id, state_change=False,
           context={"reservation_id": str(reservation_id), "reserved_tokens": item["reserved_tokens"], "tokens": tokens})
