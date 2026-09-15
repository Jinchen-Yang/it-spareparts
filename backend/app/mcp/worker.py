"""Persistent read/preview/export worker: uv run python -m app.mcp.worker.

No business apply job is executed here. Expired read/preview leases are safe to
retry, with old incomplete staging records retained for investigation.
"""

import logging
import time
from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.mcp.core import (
    Actor,
    McpError,
    actor_for,
    digest,
    enabled,
    event,
    get_settings,
    jsonable_encoder,
    now,
    refresh_actor,
    uid,
)
from app.mcp.workflows import export_bytes, make_preview
from app.models.mcp import McpRecord

_log = logging.getLogger(__name__)


def run_one():
    enabled()
    with SessionLocal() as db:
        rows = db.scalars(
            select(McpRecord)
            .where(McpRecord.kind == "job", McpRecord.state.in_(["queued", "running"]))
            .order_by(McpRecord.created_at)
            .with_for_update(skip_locked=True)
            .limit(20)
        )
        job = None
        for r in rows:
            if r.state == "queued" or r.updated_at < now() - timedelta(
                seconds=get_settings().mcp_worker_lease_seconds
            ):
                job = r
                break
        if job is None:
            return False
        payload = dict(job.payload)
        if payload.get("attempt", 0) >= 3:
            job.state = "failed"
            job.payload = {
                **payload,
                "error": {
                    "code": "retry_exhausted",
                    "message": "任务重试已达上限，请重新发起",
                },
            }
            from app.security import UserContext

            snapshot = payload["actor"]
            actor = Actor(
                snapshot["name"],
                UserContext(snapshot["name"], "unknown"),
                set(),
                snapshot["version"],
                snapshot.get("credential_id"),
            )
            event(
                db,
                actor,
                "worker_" + payload["task"],
                "failed",
                {"job_id": job.id, "code": "retry_exhausted"},
            )
            db.commit()
            return True
        marker = uid()
        job.state = "running"
        job.updated_at = now()
        job.payload = {
            **payload,
            "lease": marker,
            "attempt": payload.get("attempt", 0) + 1,
        }
        job_id = job.id
        db.commit()
    try:
        with SessionLocal() as db:
            # Hold the job lock while working. Another worker cannot steal an active job.
            job = db.scalar(
                select(McpRecord).where(McpRecord.id == job_id).with_for_update()
            )
            if job.payload.get("lease") != marker:
                return True
            if job.expires_at <= now():
                raise McpError("expired_request", "任务已过期，请重新创建", 409)
            snapshot = job.payload["actor"]
            actor = actor_for(
                db,
                snapshot["name"],
                version=snapshot["version"],
                scopes=snapshot["scopes"],
                credential_id=snapshot.get("credential_id"),
            )
            actor = refresh_actor(db, actor)
            event(
                db,
                actor,
                "worker_" + job.payload["task"],
                "requested",
                {"job_id": job.id},
            )
            result = (
                make_preview(db, actor, job.payload["args"])
                if job.payload["task"] == "preview"
                else export_bytes(db, actor, job.payload["args"])
            )
            job.payload = {**job.payload, "result": jsonable_encoder(result)}
            job.state = result.get("status", "succeeded")
            event(
                db,
                actor,
                "worker_" + job.payload["task"],
                "completed",
                {"job_id": job.id, "result_hash": digest(result)},
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001 - persist sanitized terminal failure
        _log.warning("MCP job failed id=%s type=%s", job_id, type(exc).__name__)
        with SessionLocal() as db:
            job = db.get(McpRecord, job_id)
            if job and job.payload.get("lease") == marker:
                code = (
                    exc.code
                    if isinstance(exc, McpError)
                    else "invalid_document"
                    if isinstance(exc, ValueError)
                    else "operation_failed"
                )
                job.state = "failed"
                job.payload = {
                    **job.payload,
                    "error": {
                        "code": code,
                        "message": exc.message
                        if isinstance(exc, McpError)
                        else "处理失败，请检查单据或联系管理员",
                    },
                }
                # Audit remains durable even when the original user's credentials were revoked.
                snapshot = job.payload["actor"]
                from app.security import UserContext

                failed_actor = Actor(
                    snapshot["name"],
                    UserContext(snapshot["name"], "unknown"),
                    set(),
                    snapshot["version"],
                    snapshot.get("credential_id"),
                )
                event(
                    db,
                    failed_actor,
                    "worker_" + job.payload["task"],
                    "failed",
                    {"job_id": job.id, "code": code},
                )
                db.commit()
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    last_cleanup = 0
    while True:
        try:
            if not get_settings().mcp_enabled:
                time.sleep(5)
                continue
            if time.monotonic() - last_cleanup > 3600:
                from app.mcp.cleanup import cleanup_files

                cleanup_files()
                last_cleanup = time.monotonic()
            if not run_one():
                time.sleep(2)
        except Exception:  # noqa: BLE001 - protocol/worker boundary must sanitize failures
            _log.exception("MCP worker paused")
            time.sleep(5)
