from types import SimpleNamespace

import pytest

from app.pn_catalog.errors import CatalogError
from app.services.pn_catalog_runtime import (
    WorkerSettings,
    build_worker_handlers,
    install_media_runtime,
    run_worker_once,
)
from app.services.pn_media_cloud import MediaCloudSettings
from app.services.pn_research_provider import Settings as EnrichmentSettings


def test_api_and_worker_configuration_never_start_io_or_claim_work():
    app = SimpleNamespace(state=SimpleNamespace())
    disabled_media = MediaCloudSettings(enabled=False)
    disabled_ai = EnrichmentSettings(enabled=False)
    install_media_runtime(app, media_settings=disabled_media, enrichment_settings=disabled_ai)
    assert not any(app.state.pn_catalog_capabilities.values())
    assert not hasattr(app.state, "pn_media_storage")
    def no_sessions():
        raise AssertionError("configuration must not open a database session")
    def no_authorization(*args):
        raise AssertionError("configuration must not invoke the PDP")
    handlers = build_worker_handlers(no_sessions, authorize=no_authorization,
        media_settings=disabled_media, enrichment_settings=disabled_ai)
    assert set(handlers) == {"identity.recalculate"}
    with pytest.raises(CatalogError) as disabled:
        run_worker_once(no_sessions, worker_id="test", settings=WorkerSettings(enabled=False), handlers=handlers)
    assert disabled.value.code == "job.worker_disabled"


def test_bundle_is_available_without_enabling_paid_research():
    app = SimpleNamespace(state=SimpleNamespace())
    media_settings = MediaCloudSettings(enabled=True)
    ai_settings = EnrichmentSettings(enabled=False)
    install_media_runtime(app, media_settings=media_settings, enrichment_settings=ai_settings)
    assert app.state.pn_catalog_capabilities == {
        "media": True, "bundle": True, "research": False, "recognition": False}
    handlers = build_worker_handlers(None, authorize=lambda *args: None,
        media_settings=media_settings, enrichment_settings=ai_settings)
    assert "enrichment.bundle" in handlers
    assert "enrichment.research" not in handlers and "enrichment.recognize" not in handlers
