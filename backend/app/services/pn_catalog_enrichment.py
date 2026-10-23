"""Human-triggered enrichment commands. Provider work never runs in these transactions."""
import json
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, or_, select, text

from app.pn_catalog import api, content, jobs, media
from app.pn_catalog.dto import CommandContext
from app.pn_catalog.enrichment_dto import CreateBundle, RecognizeInbox, ResearchPN
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.job_dto import JobSpec
from app.pn_catalog.job_models import CatalogJob
from app.pn_catalog.media_common import access, grant, load
from app.pn_catalog.media_inbox import can_read
from app.pn_catalog.media_models import Asset, GalleryGroup, GalleryMember, InboxItem
from app.pn_catalog.persistence import lock_part


def _human(context):
    if context.actor.actor_kind != "human" or context.actor.provenance.kind != "manual":
        raise CatalogError("enrichment.manual_required", "此功能需要实际操作人主动发起", status_code=403)


def _authorize(db, context, authorize, action, part_id, *, field="candidates", write=True):
    decision = api._authorize(db, context, authorize, action, part_id)
    grant(decision, field, write=write)
    return decision


def _inbox(db, inbox_id, *, context, authorize):
    # Authorization precedes the resource lock, including shared-inbox ownership scope.
    item = load(db, InboxItem, inbox_id)
    can_read(db, item, context, authorize)
    _authorize(db, context, authorize, "enrichment.recognize", None, field="inbox")
    item = load(db, InboxItem, inbox_id, lock=True, shared=True)
    can_read(db, item, context, authorize)
    if item.status != "ready" or item.asset_id is None:
        raise CatalogError("enrichment.inbox_unavailable", "仅未归档的已接收图片可识别")
    asset = load(db, Asset, item.asset_id)
    return item, asset


def _research_snapshot(db, part_id, capabilities, *, context, authorize):
    _authorize(db, context, authorize, "enrichment.research", part_id)
    for capability in capabilities:
        _authorize(db, context, authorize, "enrichment.research." + capability, part_id,
                   field="gallery" if capability == "images" else "candidates")
    api._authorize(db, context, authorize, "read", part_id)
    if "text" in capabilities:
        for action in ("create_candidate", "read_candidates", "read_content"):
            api._authorize(db, context, authorize, action, part_id)
    if "images" in capabilities:
        access(db, context, authorize, "gallery.research", part_id, write=True)
        access(db, context, authorize, "gallery.read", part_id)
    lock_part(db, part_id)
    profile = api.get_part(db, part_id, context=context, authorize=authorize)["data"]
    if profile["identity"]["status"] != "active":
        raise CatalogError("catalog.identity_changed", "型号身份已变化")
    profile = {key: value for key, value in profile.items() if key in {
        "part_id", "pn_std", "identity", "identity_revision", "profile_revision", "profile",
        "specs", "deleted_specs", "locked_fields"}}
    result = {"profile": profile, "content": {}}
    if "text" in capabilities:
        api._authorize(db, context, authorize, "create_candidate", part_id)
        api._authorize(db, context, authorize, "read_candidates", part_id)
        for grade in ("good", "bad"):
            value = content.get_content(db, part_id, grade, context=context, authorize=authorize)["data"]
            # SHARE row locks also serialize missing-row creation through parent lock in publication.
            result["content"][grade] = {key: item for key, item in value.items()
                if key in {"revision", "title", "description", "protected_fields"}}
    if "images" in capabilities:
        access(db, context, authorize, "gallery.research", part_id, write=True)
        access(db, context, authorize, "gallery.read", part_id)
        member = db.get(GalleryMember, part_id)
        group = load(db, GalleryGroup, member.group_id, lock=True, shared=True) if member else None
        result["gallery_revision"] = group.revision if group else 0
    return result


def _quota(db, kind, *, context, part_id=None, capabilities=()):
    # This short admission lock serializes only job creation, never worker I/O.
    db.execute(text("SELECT pg_advisory_xact_lock(742031, 4)"))
    active = (CatalogJob.kind == kind, CatalogJob.status.in_(["pending", "running"]))
    owner = CatalogJob.context["actor"]["principal_id"].astext == context.actor.principal_id
    if kind == "enrichment.research":
        overlap = or_(*(CatalogJob.payload["capabilities"].contains([capability]) for capability in capabilities))
        if db.scalar(select(CatalogJob.id).where(*active, or_(owner, CatalogJob.part_id == part_id), overlap).limit(1)):
            raise CatalogError("enrichment.busy", "同一操作人或型号已有该能力的未完成请求")
    elif (db.scalar(select(func.count()).select_from(CatalogJob).where(*active, owner)) >= 2
          or db.scalar(select(func.count()).select_from(CatalogJob).where(*active)) >= 100):
        raise CatalogError("enrichment.busy", "人工任务并发已满，请等待现有任务完成")


def _owned_job(db, job_id, context):
    job = db.scalar(select(CatalogJob).where(CatalogJob.id == job_id)
                    .execution_options(populate_existing=True))
    if job is None:
        raise CatalogError("job.not_found", "任务不存在", status_code=404)
    if job.context.get("actor", {}).get("principal_id") != context.actor.principal_id:
        raise CatalogError("auth.forbidden", "只能查看或管理本人提交的任务", status_code=403)
    return job


def authorize_job(db, context, action, job, *, authorize):
    """Host job-policy adapter: action grant plus current resource visibility, never payload trust."""
    current = _owned_job(db, job.job_id, context)
    if (job.kind, job.resource_id, job.part_id) != (current.kind, current.resource_id, current.part_id):
        raise CatalogError("auth.forbidden", "任务与当前资源不一致", status_code=403)
    field = "inbox" if job.kind == "enrichment.recognize" else "gallery" if job.kind == "enrichment.bundle" else "candidates"
    domain_action = "research." + action if job.kind == "enrichment.research" and action.startswith("job.") else action
    decision = _authorize(db, context, authorize, domain_action, job.part_id, field=field,
                          write=action not in {"job.read", "enrichment.bundle", "enrichment.bundle.read"})
    if job.kind == "enrichment.recognize":
        item = load(db, InboxItem, UUID(job.resource_id))
        can_read(db, item, context, authorize)
    elif job.kind in {"enrichment.research", "enrichment.bundle"}:
        api.get_part(db, job.part_id, context=context, authorize=authorize)
        if job.kind == "enrichment.bundle":
            access(db, context, authorize, "gallery.read", job.part_id)
        else:
            api._authorize(db, context, authorize, "read_candidates", job.part_id)
    else:
        raise CatalogError("auth.forbidden", "此授权入口不支持该任务", status_code=403)
    return decision


def _project(db, receipt, *, context, authorize, replayed):
    return jobs.project_receipt(db, receipt, context=context, replayed=replayed,
        authorize_job=lambda session, ctx, action, job: authorize_job(session, ctx, action, job, authorize=authorize))


def _finish(db, handle, spec):
    job = jobs.enqueue(db, spec, context=handle.context)
    audit = api.record_audit(db, spec.part_id, context=handle.context, action=spec.kind,
        changes={"job_id": str(job.job_id), "resource_type": spec.resource_type,
                 "resource_id": spec.resource_id, "capabilities": spec.payload.get("capabilities", [])},
        before_revision=0, after_revision=job.revision, authorization=handle.decision.metadata,
        payload_hash=handle.payload_hash)
    return api.finish_command(db, handle, part_id=spec.part_id, applied_revision=job.revision,
        audit_id=audit.id, result={"resource": "job", "job_id": str(job.job_id)})


def _begin(db, command, resource_id, part_id, action, *, context, authorize):
    _human(context)
    return api.begin_command(db, context=context, authorize=authorize, operation=action,
        part_id=part_id, resource_type="job", resource_id=resource_id, payload=command)


def _spec(context, kind, identifier, part_id, generation, revision, payload):
    return JobSpec(kind=kind, queue="enrichment", resource_type=kind, resource_id=str(identifier),
        part_id=part_id, resource_generation=generation, input_revision=revision,
        deduplication_key=f"{context.actor.principal_id}:{context.actor.client_id}:{context.request_id}",
        payload=payload, max_attempts=2)


def recognize(db, inbox_id: UUID, command: RecognizeInbox, *, context, authorize):
    handle = _begin(db, command, inbox_id, None, "enrichment.recognize", context=context, authorize=authorize)
    if handle.receipt:
        return _project(db, handle.receipt, context=context, authorize=authorize, replayed=True)
    _quota(db, "enrichment.recognize", context=context)
    item, asset = _inbox(db, inbox_id, context=context, authorize=authorize)
    if item.revision != command.expected_revision:
        raise CatalogError("media.version_conflict", "图片已变化，请重新确认")
    payload = {"asset_id": str(asset.id), "object_key": asset.object_key,
               "content_type": asset.content_type, "capabilities": ["recognition"],
               "limits": {"tokens": 24000, "searches": 0, "external_calls": 4},
               "deadline": (db.scalar(select(func.clock_timestamp())) + timedelta(seconds=600)).isoformat()}
    receipt = _finish(db, handle, _spec(context, "enrichment.recognize", inbox_id, None,
        item.generation, item.revision, payload))
    return _project(db, receipt, context=context, authorize=authorize, replayed=False)


def research(db, part_id: int, command: ResearchPN, *, context, authorize):
    handle = _begin(db, command, part_id, part_id, "enrichment.research", context=context, authorize=authorize)
    if handle.receipt:
        return _project(db, handle.receipt, context=context, authorize=authorize, replayed=True)
    _quota(db, "enrichment.research", context=context, part_id=part_id, capabilities=command.capabilities)
    # Exclusive parent prevents content insertion races while collecting both grades.
    snapshot = _research_snapshot(db, part_id, command.capabilities, context=context, authorize=authorize)
    profile = snapshot["profile"]
    if (profile["identity_revision"] != command.expected_identity_revision
        or profile["profile_revision"] != command.expected_profile_revision
        or any(snapshot["content"][grade]["revision"] != revision for grade, revision in command.expected_content_revisions.items() if grade in snapshot["content"])
        or ("images" in command.capabilities and snapshot["gallery_revision"] != command.expected_gallery_revision)):
        raise CatalogError("enrichment.version_conflict", "资料已变化，请重新核对研究范围")
    payload = {"snapshot": snapshot, "capabilities": command.capabilities,
        "limits": {"tokens": 24000, "searches": command.max_searches, "external_calls": 100},
        "deadline": (db.scalar(select(func.clock_timestamp())) + timedelta(seconds=600)).isoformat()}
    receipt = _finish(db, handle, _spec(context, "enrichment.research", part_id, part_id,
        profile["identity_revision"], profile["profile_revision"], payload))
    return _project(db, receipt, context=context, authorize=authorize, replayed=False)


def bundle(db, part_id: int, command: CreateBundle, *, context, authorize):
    handle = _begin(db, command, part_id, part_id, "enrichment.bundle", context=context, authorize=authorize)
    if handle.receipt:
        return _project(db, handle.receipt, context=context, authorize=authorize, replayed=True)
    _authorize(db, context, authorize, "enrichment.bundle", part_id, field="gallery", write=False)
    _quota(db, "enrichment.bundle", context=context)
    snapshot = media.bundle_snapshot(db, part_id, grade=command.grade, entry_ids=command.entry_ids,
                                     context=context, authorize=authorize)
    if (snapshot["identity_revision"] != command.expected_identity_revision
        or snapshot["gallery_revision"] != command.expected_gallery_revision):
        raise CatalogError("gallery.version_conflict", "图库已变化，请重新确认打包范围")
    payload = {"snapshot": snapshot, "watermark": command.watermark, "capabilities": ["bundle"],
        "limits": {"tokens": 0, "searches": 0, "external_calls": 100},
        "deadline": (db.scalar(select(func.clock_timestamp())) + timedelta(days=1)).isoformat()}
    receipt = _finish(db, handle, _spec(context, "enrichment.bundle", part_id, part_id,
        snapshot["identity_revision"], snapshot["gallery_revision"], payload))
    return _project(db, receipt, context=context, authorize=authorize, replayed=False)


def verify_resource(db, lease, *, authorize):
    context = CommandContext.model_validate_json(json.dumps(lease.context))
    if lease.kind == "enrichment.recognize":
        item, asset = _inbox(db, UUID(lease.resource_id), context=context, authorize=authorize)
        if str(asset.id) != lease.payload["asset_id"]:
            return None
        return item.generation, item.revision
    if lease.kind == "enrichment.research":
        current = _research_snapshot(db, lease.part_id, lease.payload["capabilities"], context=context, authorize=authorize)
        if current != lease.payload["snapshot"]:
            return None
        return current["profile"]["identity_revision"], current["profile"]["profile_revision"]
    if lease.kind == "enrichment.bundle":
        _authorize(db, context, authorize, "enrichment.bundle", lease.part_id, field="gallery", write=False)
        current = media.verify_bundle_snapshot(db, lease.payload["snapshot"], context=context, authorize=authorize)
        return current["identity_revision"], current["gallery_revision"]
    raise CatalogError("job.invalid_kind", "不支持的人工任务")


def bundle_downloads(db, job_id, *, context, authorize, storage):
    job = _owned_job(db, job_id, context)
    if job.kind != "enrichment.bundle":
        raise CatalogError("job.not_found", "打包任务不存在", status_code=404)
    _authorize(db, context, authorize, "enrichment.bundle.read", job.part_id, field="gallery", write=False)
    media.verify_bundle_snapshot(db, job.payload["snapshot"], context=context, authorize=authorize)
    job = db.scalar(select(CatalogJob).where(CatalogJob.id == job_id).with_for_update(read=True)
                    .execution_options(populate_existing=True))
    now = db.scalar(select(func.clock_timestamp()))
    if job.status != "succeeded" or job.finished_at + timedelta(days=1) <= now:
        raise CatalogError("enrichment.bundle_unavailable", "压缩包尚未就绪或已过期")
    # signed_get is a strictly local cryptographic operation; storage must not perform I/O here.
    ttl = min(900, int((job.finished_at + timedelta(days=1) - now).total_seconds()))
    if ttl < 1:
        raise CatalogError("enrichment.bundle_unavailable", "压缩包已过期")
    archives = [{**archive, "url": storage.signed_get(
        f"bundle/{job.id}/part-{archive['number']}.zip", expires_in=ttl,
        download_filename=f"PN-images-{archive['number']}.zip")} for archive in job.result["archives"]]
    return {"data": {"job_id": str(job.id), "archives": archives, "expires_in": ttl}, "meta": {"replayed": False}}
