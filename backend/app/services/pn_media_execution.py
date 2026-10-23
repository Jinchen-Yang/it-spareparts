"""Host orchestration: every provider call happens after its DB session is closed."""
from datetime import datetime, timezone
from uuid import UUID
from types import SimpleNamespace

from app.pn_catalog import jobs, media
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_ports import verify_and_archive
from app.pn_catalog.media_models import MultipartUpload, UploadTicket
from app.services.pn_media_cloud import MultipartPart, ObjectConflict, ObjectNotFound


def media_handlers(*, storage, cloud, authorize, session_factory=None):
    """Only implemented job kinds are registered; unknown work is never claimed."""
    if session_factory is None:
        from app.db import SessionLocal
        session_factory = SessionLocal

    def verifier(db, lease):
        if lease.kind == "media.thumbnail":
            return media.verify_derivative_resource(db, lease, authorize=authorize)
        if lease.kind == "media.abort_multipart":
            return media.verify_multipart_abort_resource(db, lease, authorize=authorize)
        return media.verify_upload_resource(db, lease, authorize=authorize)

    def guard(lease):
        with session_factory() as db:
            current = verifier(db, lease)
            if current != (lease.resource_generation, lease.input_revision):
                raise CatalogError("job.resource_changed", "任务来源已变化")
            jobs.heartbeat(db, lease, lease_seconds=180)
            db.commit()

    def call(lease, operation, *args, **kwargs):
        guard(lease)
        return operation(*args, **kwargs)

    def task_storage(lease):
        # The OSS adapter also guards each internal page/abort request. Other ports
        # still receive a guard at each public operation boundary.
        factory = getattr(storage, "with_network_guard", None)
        return factory(lambda: guard(lease)) if factory is not None else storage

    def prepare_verify(lease):
        guarded = SimpleNamespace(inspect=lambda *args, **kw: call(lease, cloud.inspect, *args, **kw),
                                  copy=lambda *args, **kw: call(lease, cloud.copy, *args, **kw))
        return verify_and_archive(guarded, **{name: lease.payload[name] for name in
            ("staging_key", "target_key", "content_type", "declared_bytes")})

    def prepare_thumbnail(lease):
        call(lease, cloud.thumbnail, **{name: lease.payload[name] for name in
            ("source_key", "target_key", "expected_sha256", "expected_bytes")})

    def prepare_complete(lease):
        provider, payload = task_storage(lease), lease.payload
        parts = [MultipartPart(**value) for value in payload["parts"]]
        expected_size = sum(part.size_bytes for part in parts)
        fingerprint = payload.get("expected_fingerprint") or (
            f"pn-upload:{lease.resource_id}:{lease.resource_generation}"
        )
        def completed():
            try:
                info = call(lease, provider.head, payload["key"])
            except ObjectNotFound:
                return False
            if info.size_bytes != expected_size or info.request_fingerprint != fingerprint:
                raise ObjectConflict("分片合并对象与当前票据不符")
            return True
        if completed():
            return None
        try:
            call(lease, provider.complete_multipart, payload["key"], payload["storage_upload_id"], parts)
        except ObjectNotFound:
            if not completed():
                raise
        if not completed():
            raise ObjectNotFound("合并后的对象尚不存在")

    def prepare_abort(lease):
        provider = task_storage(lease)
        call(lease, provider.abort_multipart, lease.payload["key"], lease.payload.get("storage_upload_id"))

    return {
        "media.verify_upload": jobs.JobHandler(prepare_verify, verifier, media.publish_verified_upload),
        "media.thumbnail": jobs.JobHandler(prepare_thumbnail, verifier, media.publish_derivative),
        "media.complete_multipart": jobs.JobHandler(prepare_complete, verifier,
            lambda db, lease, _result: media.publish_multipart_completed(db, lease)),
        "media.abort_multipart": jobs.JobHandler(prepare_abort, verifier, media.publish_multipart_aborted),
    }


class MediaTransport:
    """HTTP adapter helper; callers first commit the domain's upload reservation."""
    def __init__(self, session_factory, *, storage, authorize):
        self.sessions, self.storage, self.authorize = session_factory, storage, authorize

    def initialize_multipart(self, upload_id: UUID, *, context):
        with self.sessions() as db:
            plan = media.multipart_start_plan(db, upload_id, context=context, authorize=self.authorize)
            db.commit()
        if plan["state"] == "uploading" and plan["storage_upload_id"]:
            return plan["storage_upload_id"]
        if plan["state"] != "initializing":
            raise CatalogError("request.precondition_failed", "分片初始化已失效", status_code=412)
        storage_id = self.storage.start_multipart(plan["key"], plan["content_type"], plan["fingerprint"])
        try:
            with self.sessions() as db:
                current = media.multipart_start_plan(db, upload_id, context=context, authorize=self.authorize)
                already = current["state"] == "uploading" and current["storage_upload_id"] == storage_id
                adopted = already or media.publish_multipart_started(db, upload_id,
                    operation_token=plan["operation_token"], storage_upload_id=storage_id)
                db.commit()
        except Exception:
            self._discard_if_orphan(upload_id, storage_id, plan)
            raise
        if not adopted:
            # Only the losing returned upload ID; never scan-and-abort the winner.
            self.storage.abort_multipart(plan["key"], storage_id, include_orphans=False)
            raise CatalogError("media.upload_conflict", "分片初始化期间票据已改变")
        return storage_id

    def _discard_if_orphan(self, upload_id, storage_id, plan):
        # Cleanup uses durable state, never an exception message or an assumed failed commit.
        with self.sessions() as db:
            ticket = db.get(UploadTicket, upload_id)
            multipart = db.get(MultipartUpload, upload_id)
            orphan = (ticket is None or multipart is None or ticket.status == "cancelled"
                or multipart.operation_token != plan["operation_token"]
                or (multipart.state == "uploading" and multipart.storage_upload_id != storage_id)
                or (ticket.status == "issued" and ticket.expires_at <= datetime.now(timezone.utc)))
            db.commit()
        if orphan:
            self.storage.abort_multipart(plan["key"], storage_id, include_orphans=False)

    def list_parts(self, upload_id: UUID, *, context):
        with self.sessions() as db:
            plan = media.multipart_storage_plan(db, upload_id, context=context, authorize=self.authorize)
            db.commit()
        if not plan["upload_id"]:
            raise CatalogError("request.precondition_failed", "分片会话尚未就绪", status_code=412)
        parts = self.storage.list_parts(plan["key"], plan["upload_id"])
        count = (plan["size_bytes"] + plan["part_size"] - 1) // plan["part_size"]
        if len(parts) > count or len(parts) > 10000:
            raise CatalogError("media.invalid_part", "存储分片数量无效", status_code=502)
        seen = set()
        for part in parts:
            expected = min(plan["part_size"], plan["size_bytes"] - (part.part_number - 1) * plan["part_size"])
            if (part.part_number in seen or part.part_number < 1 or part.part_number > count
                    or part.size_bytes != expected or not part.etag or len(part.etag) > 256):
                raise CatalogError("media.invalid_part", "存储分片数据无效", status_code=502)
            seen.add(part.part_number)
        with self.sessions() as db:
            current = media.multipart_storage_plan(db, upload_id, context=context, authorize=self.authorize)
            db.commit()
        if current != plan:
            raise CatalogError("media.upload_conflict", "核对期间上传会话已改变")
        return sorted(parts, key=lambda part: part.part_number)
