"""Fenced manual workflows: close every database session before provider calls."""
import asyncio
import json
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select

from app.pn_catalog import api, jobs, media
from app.pn_catalog.content_dto import CandidateProvenance, CreateContentCandidate, CreateProfileCandidate
from app.pn_catalog.dto import CommandContext, ManualSpec, Provenance
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_ports import VerifiedImage
from app.services.spec_extract import SPEC_KEYS
from . import pn_catalog_enrichment as commands
from .pn_media_cloud import CloudBundleFile
from .pn_research_provider import HTTPResearch
from .pn_research_relevance import product_image_url


class ManualExecution:
    """A durable budget reservation is committed before every external side effect."""
    def __init__(self, session_factory, lease, authorize):
        self.sessions, self.lease, self.authorize = session_factory, lease, authorize
        self.reservations = []

    def check(self, capability=None, *, reserve_tokens=0, searches=0, external=False):
        with self.sessions() as db:
            current = commands.verify_resource(db, self.lease, authorize=self.authorize)
            if current != (self.lease.resource_generation, self.lease.input_revision):
                raise CatalogError("job.resource_changed", "任务输入已变化")
            now = db.scalar(select(func.clock_timestamp()))
            if now >= datetime.fromisoformat(self.lease.payload["deadline"]):
                raise CatalogError("enrichment.deadline_exceeded", "人工任务执行期限已过")
            self.lease = jobs.heartbeat(db, self.lease, lease_seconds=180)
            reservation = jobs.reserve_execution(db, self.lease, capability=capability,
                tokens=reserve_tokens, searches=searches, external_calls=int(external))
            db.commit()
        if reserve_tokens:
            self.reservations.append((reserve_tokens, reservation))
        return reservation

    def settle(self, reserved, measured):
        if not self.reservations or self.reservations[-1][0] != reserved:
            raise CatalogError("job.reservation_conflict", "模型计量与预算预留不匹配")
        _, reservation = self.reservations.pop()
        with self.sessions() as db:
            jobs.settle_execution(db, self.lease, reservation, tokens=measured)
            db.commit()

    def call(self, capability, fn, *args, **kwargs):
        self.check(capability, external=True)
        return fn(*args, **kwargs)


def _catalog(snapshot):
    profile = snapshot["profile"]
    fields = dict(profile["profile"])
    fields["summary"] = fields.pop("description", None)
    locked = set(profile.get("locked_fields", []))
    missing = [key for key, value in fields.items() if not value and key not in locked]
    sku = {}
    for grade, row in snapshot["content"].items():
        sku[grade] = {key: row.get(key) for key in ("title", "description")}
        missing += [f"{grade}_{key}" for key in ("title", "description")
                    if key in row and not row[key] and key not in row.get("protected_fields", [])]
    return {"fields": fields, "missing_fields": missing, "sku_fields": sku,
            "specs": profile.get("specs", {}), "conflicted_fields": [],
            "deleted_specs": profile.get("deleted_specs", [])}


def _parts(entries):
    batch, size = [], 0
    for entry in entries:
        if batch and (len(batch) >= 100 or size + entry["size_bytes"] > 128 * 1024 * 1024):
            yield batch
            batch, size = [], 0
        batch.append(entry)
        size += entry["size_bytes"]
    if batch:
        yield batch


def _context(lease, *, provider, model, suffix):
    original = CommandContext.model_validate_json(json.dumps(lease.context))
    provenance = Provenance(kind="ai", provider=provider, model=model[:128], run_id=str(lease.job_id),
                            template_version="pn-enrichment-v1")
    actor = original.actor.model_copy(update={"executor_id": "pn-enrichment-worker", "provenance": provenance})
    return original.model_copy(update={"actor": actor, "request_id": uuid5(NAMESPACE_URL,
        f"pn-enrichment/{lease.job_id}/{suffix}"), "causation_id": str(original.request_id)})


def enrichment_handlers(session_factory, *, authorize, storage, cloud, settings=None, provider_factory=None):
    """Provider factory receives a ManualExecution and returns the async search/analyze/recognize port."""
    if provider_factory is None:
        if settings is None:
            raise ValueError("explicit enrichment settings required")
        def provider_factory(execution):
            return HTTPResearch(settings, execution=execution)

    def prepare_recognition(lease):
        execution = ManualExecution(session_factory, lease, authorize)
        image = execution.call("recognition", cloud.recognition, lease.payload["object_key"], lease.payload["content_type"])
        # Pure local OSS signing; only the provider subsequently reads the image.
        url = storage.signed_get(image.key, expires_in=120)
        return asyncio.run(provider_factory(execution).recognize(url))

    def prepare_research(lease):
        execution = ManualExecution(session_factory, lease, authorize)
        snapshot, capabilities = lease.payload["snapshot"], lease.payload["capabilities"]
        adapter = provider_factory(execution).with_context(_catalog(snapshot))
        pn = snapshot["profile"]["pn_std"]
        result = asyncio.run(adapter.search(pn))
        # A second explicit bounded search compensates for an empty evidence response only.
        if not result.evidence and lease.payload["limits"]["searches"] > 1:
            result = asyncio.run(adapter.search(pn))
        evidence = [item for item in result.evidence if len(str(item.url)) <= 2048][:20]
        if "images" in capabilities:
            expanded = []
            for item in evidence[:10]:
                discovered = execution.call("images", cloud.discover_remote_images, str(item.url))
                urls = [candidate.url for candidate in discovered
                        if candidate.source_url == str(item.url) and product_image_url(candidate.url)]
                expanded.append(item.model_copy(update={"image_urls": urls[:20]}))
            evidence = expanded
        if not evidence:
            return {"analysis": None, "images": [], "evidence": []}
        analysis = asyncio.run(adapter.analyze(pn, evidence))
        allowed = {str(item.url): {str(url) for url in item.image_urls} for item in evidence}
        images = []
        if "images" in capabilities:
            for index, image in enumerate(analysis.images):
                if (image.has_foreign_watermark or image.confidence <= 0
                    or str(image.url) not in allowed.get(str(image.source_url), set())
                    or not product_image_url(str(image.url))):
                    continue
                staging = f"uploads/ai/pn-catalog/{lease.job_id}/{index}"
                imported = execution.call("images", cloud.import_remote_image, str(image.url), staging)
                inspected = execution.call("images", cloud.inspect, imported.key, imported.content_type)
                target = f"img/pn-research/{lease.job_id}/{index}/{inspected.sha256}/original"
                execution.call("images", cloud.copy, imported.key, target, inspected.source_etag,
                    sha256=inspected.sha256, content_type=inspected.content_type, expected_bytes=inspected.bytes)
                images.append((index, VerifiedImage(sha256=inspected.sha256, width=inspected.width,
                    height=inspected.height, size_bytes=inspected.bytes, content_type=inspected.content_type,
                    source_etag=inspected.source_etag, object_key=target), str(image.source_url)))
        return {"analysis": analysis, "images": images, "evidence": list(allowed),
                "evidence_records": [{"url": str(item.url), "title": item.title[:300]} for item in evidence]}

    def prepare_bundle(lease):
        execution = ManualExecution(session_factory, lease, authorize)
        archives = []
        for number, entries in enumerate(_parts(lease.payload["snapshot"]["entries"]), 1):
            files = [CloudBundleFile(key=e["object_key"], sha256=e["sha256"], size_bytes=e["size_bytes"],
                                    filename=f"{index + 1}-{e['filename']}"[:255]) for index, e in enumerate(entries)]
            # Stable per-job paths make provider retries idempotent; result has no signed URLs/keys.
            archive = execution.call("bundle", cloud.bundle, str(lease.job_id), number, files,
                                     watermark_text="PN 图库" if lease.payload["watermark"] else None)
            archives.append({"number": number, "size_bytes": archive.size_bytes, "sha256": archive.sha256,
                             "filename": archive.filename, "image_count": len(files)})
        return {"archives": archives}

    def publish_research(db, lease, prepared):
        analysis = prepared["analysis"]
        if analysis is None:
            return {"candidate_ids": [], "entry_ids": [], "outcome": "no_evidence"}
        snapshot = lease.payload["snapshot"]
        context = _context(lease, provider=analysis.provider, model=analysis.model, suffix="publication")
        provenance = CandidateProvenance(**context.actor.provenance.model_dump())
        profile, specs, content = {}, {}, {"good": {}, "bad": {}}
        allowed = set(prepared["evidence"])
        catalog = _catalog(snapshot)
        missing = set(catalog["missing_fields"])
        ignored = []
        for proposal in analysis.fields:
            if (not proposal.source_urls or not set(map(str, proposal.source_urls)) <= allowed or proposal.confidence <= 0):
                ignored.append(proposal.field)
                continue
            if proposal.field == "specs":
                for key, value in proposal.value.items():
                    if (key in SPEC_KEYS and key not in snapshot["profile"].get("specs", {})
                        and key not in snapshot["profile"].get("deleted_specs", []) and len(str(value)) <= 128):
                        specs[key] = ManualSpec(value=str(value), unit=SPEC_KEYS[key][1], numeric_value=None)
            elif proposal.field in missing:
                if proposal.field.startswith(("good_", "bad_")):
                    grade, key = proposal.field.split("_", 1)
                    content[grade][key] = proposal.value
                else:
                    profile["description" if proposal.field == "summary" else proposal.field] = proposal.value
        candidate_ids = []
        if "text" in lease.payload["capabilities"]:
            common = {"part_id": lease.part_id, "expected_identity_revision": lease.resource_generation,
                      "provenance": provenance}
            commands_to_publish = []
            if profile or specs:
                commands_to_publish.append(CreateProfileCandidate(**common, resource="profile",
                    base_revision=lease.input_revision, changes=profile, spec_updates=specs))
            for grade, changes in content.items():
                if changes:
                    commands_to_publish.append(CreateContentCandidate(**common, resource="content", grade=grade,
                        base_revision=snapshot["content"][grade]["revision"], changes=changes))
            for command in commands_to_publish:
                command_context = _context(lease, provider=analysis.provider, model=analysis.model,
                                           suffix=f"candidate/{command.resource}/{getattr(command, 'grade', '')}")
                result = api.create_candidate(db, command, context=command_context, authorize=authorize)
                candidate_ids.append(result["data"]["id"])
        entries = []
        gallery_revision = snapshot.get("gallery_revision", 0)
        for index, image, source in prepared["images"]:
            result = media.publish_research_candidate(db, lease.part_id, image, source_job_id=lease.job_id,
                image_index=index, expected_identity_revision=lease.resource_generation,
                expected_gallery_revision=gallery_revision, context=context, authorize=authorize,
                provenance={"source_url": source, "provider": analysis.provider, "model": analysis.model,
                            "run_id": str(lease.job_id), "template_version": "pn-enrichment-v1"})
            gallery_revision = result["gallery_revision"]
            entries.append(result["entry_id"])
        return {"candidate_ids": candidate_ids, "entry_ids": entries, "ignored_fields": ignored,
                "provider": analysis.provider, "model": analysis.model, "run_id": str(lease.job_id),
                "evidence": prepared["evidence_records"],
                "field_sources": [{"field": field.field, "confidence": field.confidence,
                    "evidence_indices": [prepared["evidence"].index(str(url)) for url in field.source_urls
                                         if str(url) in allowed]} for field in analysis.fields]}

    def verify(db, lease):
        if db.scalar(select(func.clock_timestamp())) >= datetime.fromisoformat(lease.payload["deadline"]):
            return None
        return commands.verify_resource(db, lease, authorize=authorize)

    return {"enrichment.recognize": jobs.JobHandler(prepare_recognition, verify,
                lambda db, lease, result: result.model_dump(mode="json")),
            "enrichment.research": jobs.JobHandler(prepare_research, verify, publish_research),
            "enrichment.bundle": jobs.JobHandler(prepare_bundle, verify, lambda db, lease, result: result)}
