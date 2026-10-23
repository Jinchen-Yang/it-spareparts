"""Opt-in host wiring. API construction never starts workers or performs provider I/O."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.pn_catalog import jobs
from app.pn_catalog.errors import CatalogError

from .pn_media_cloud import HTTPCloudImages, MediaCloudSettings, OSSStorage
from .pn_research_provider import Settings as EnrichmentSettings


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PN_CATALOG_WORKER_", extra="ignore")
    enabled: bool = False
    poll_seconds: float = Field(default=2, ge=0.1, le=60)
    lease_seconds: int = Field(default=180, ge=120, le=3600)


def install_media_runtime(app, *, media_settings=None, enrichment_settings=None):
    media_settings = media_settings or MediaCloudSettings()
    enrichment_settings = enrichment_settings or EnrichmentSettings()
    app.state.pn_catalog_capabilities = {
        "media": media_settings.enabled,
        "bundle": media_settings.enabled,
        "research": media_settings.enabled and enrichment_settings.enabled,
        "recognition": media_settings.enabled and enrichment_settings.enabled,
    }
    if media_settings.enabled:
        app.state.pn_media_storage = OSSStorage(media_settings)


def original_actor_authorize(db, context, action, part_id):
    # The B3 host owns identity verification. B2 fails closed until it is installed.
    try:
        from .pn_catalog_access import resolve_job_access
    except ImportError as exc:
        raise CatalogError("auth.unavailable", "任务授权框架尚未接入", status_code=503) from exc
    access = resolve_job_access(db, context.actor)
    return access.authorize(db, context, action, part_id)


def build_worker_handlers(session_factory, *, authorize, media_settings=None, enrichment_settings=None):
    from .pn_catalog_identity import recalculation_handler
    from .pn_enrichment_execution import enrichment_handlers
    from .pn_media_execution import media_handlers
    media_settings = media_settings or MediaCloudSettings()
    enrichment_settings = enrichment_settings or EnrichmentSettings()
    handlers = {"identity.recalculate": recalculation_handler(authorize)}
    if media_settings.enabled:
        storage, cloud = OSSStorage(media_settings), HTTPCloudImages(media_settings)
        handlers.update(media_handlers(storage=storage, cloud=cloud, authorize=authorize,
                                       session_factory=session_factory))
        auxiliary = enrichment_handlers(session_factory, authorize=authorize, storage=storage,
                                        cloud=cloud, settings=enrichment_settings)
        handlers["enrichment.bundle"] = auxiliary["enrichment.bundle"]
        if enrichment_settings.enabled:
            handlers.update(auxiliary)
    return handlers


def run_worker_once(session_factory, *, worker_id, settings, handlers):
    if not settings.enabled:
        raise CatalogError("job.worker_disabled", "共享资料执行器未启用", status_code=503)
    return jobs.run_once(session_factory, worker_id=worker_id,
        queues=("maintenance", "media", "enrichment"), handlers=handlers,
        lease_seconds=settings.lease_seconds)
