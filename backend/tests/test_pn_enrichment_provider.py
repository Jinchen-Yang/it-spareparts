"""Deployable provider protocol tested with local HTTP transports only."""
import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest
from app.services.pn_research_provider import HTTPResearch, Settings, ResearchError
from app.services.pn_media_cloud import HTTPCloudImages, CloudBundleFile, MediaCloudSettings, StorageError, request_signature


def response(status, *, json=None, content=None, headers=None):
    import json as json_module
    raw = json_module.dumps(json).encode() if json is not None else (content or b"")
    return httpx.Response(status, stream=httpx.ByteStream(raw), headers=headers)


def completion(content="{}", **kw):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}], **kw}


def settings(**kw):
    return Settings(enabled=True, search_model_name="test-search", search_model_api_key="local-secret",
        text_model_name="test-text", text_model_api_key="local-secret", vision_model_name="test-vision",
        vision_model_api_key="local-secret", **kw)


@pytest.mark.parametrize("provider", ["glm", "qwen", "deepseek"])
def test_native_search_keeps_provider_evidence_and_reserves_before_io(provider):
    execution = Mock()
    def handler(request):
        execution.check.assert_called_once()
        body = json.loads(request.content)
        url = "https://example.org/products/PN-12345"
        rows = [{"title": "PN-12345 产品", "url": url}]
        if provider == "qwen":
            assert request.url.path.endswith("/services/aigc/text-generation/generation")
            data = {"output": {**completion(), "search_info": {"search_results": rows}}, "usage": {"total_tokens": 17}}
        elif provider == "deepseek":
            assert request.url.path.endswith("/v1/messages")
            assert body["tools"][0]["max_uses"] == 1
            data = {"type": "message", "stop_reason": "end_turn", "content": [
                {"type": "web_search_tool_result", "content": [{"type": "web_search_result", **rows[0]}]}],
                "usage": {"input_tokens": 10, "output_tokens": 7}}
        else:
            data = completion(web_search=[{"title": rows[0]["title"], "link": url}], usage={"total_tokens": 17})
        return response(200, json=data)
    adapter = HTTPResearch(settings(search_model_provider=provider), transport=httpx.MockTransport(handler), execution=execution)
    result = asyncio.run(adapter.search("PN-12345"))
    assert len(result.evidence) == 1 and result.provider == provider
    assert execution.check.call_args.kwargs["searches"] == 1
    assert execution.settle.call_args.args[1] == 17


def test_unknown_usage_keeps_reservation_and_fake_body_urls_are_not_evidence():
    execution = Mock()
    adapter = HTTPResearch(settings(), transport=httpx.MockTransport(lambda _: response(
        200, json=completion('来源 https://example.org/PN-12345'))), execution=execution)
    result = asyncio.run(adapter.search("PN-12345"))
    assert result.evidence == []
    execution.settle.assert_not_called()


@pytest.mark.parametrize("mutate", ["disabled", "missing_context", "http_endpoint", "redirect", "compressed", "oversize"])
def test_invalid_provider_configuration_or_transport_fails_closed(mutate):
    config = settings()
    execution = Mock()
    if mutate == "disabled":
        config = config.model_copy(update={"enabled": False})
    if mutate == "missing_context":
        execution = None
    if mutate == "http_endpoint":
        config = config.model_copy(update={"search_model_base_url": "http://insecure.test"})
    reply = response(200, json=completion())
    if mutate == "redirect":
        reply = response(302, headers={"Location": "https://evil.invalid"})
    if mutate == "compressed":
        reply = response(200, content=b"{}", headers={"Content-Encoding": "gzip"})
    if mutate == "oversize":
        reply = response(200, content=b" " * (512 * 1024 + 1))
    adapter = HTTPResearch(config, transport=httpx.MockTransport(lambda _: reply), execution=execution)
    with pytest.raises(ResearchError):
        asyncio.run(adapter.search("PN-12345"))


def test_vision_returns_typed_hints_not_commands():
    execution = Mock()
    def handler(request):
        body = json.loads(request.content)
        assert body["max_tokens"] == 2048
        return response(200, json=completion(json.dumps({"candidates": [{"pn": "PN-12345", "confidence": 0.8}]})))
    adapter = HTTPResearch(settings(), transport=httpx.MockTransport(handler), execution=execution)
    result = asyncio.run(adapter.recognize("https://storage.invalid/signed"))
    assert result.candidates[0].pn == "PN-12345"
    assert execution.check.call_args.args[0] == "recognition"


def cloud(transport):
    return HTTPCloudImages(MediaCloudSettings(enabled=True, cloud_images_url="https://fc.invalid",
        cloud_images_secret="x" * 32), transport=httpx.MockTransport(transport))


def test_bundle_uses_dedicated_fc_path_and_signature():
    from uuid import uuid4
    identifier = str(uuid4())
    def handler(request):
        assert request.url.path == "/v1/bundle"
        assert request.headers["X-Cloud-Signature"] == request_signature("x" * 32,
            request.headers["X-Cloud-Timestamp"], request.headers["X-Cloud-Request-Id"], request.content, path="/v1/bundle")
        return response(200, json={"key": f"bundle/{identifier}/part-1.zip", "size_bytes": 10,
                                        "sha256": "a" * 64, "filename": "images.zip"})
    result = cloud(handler).bundle(identifier, 1, [CloudBundleFile("img/a", "a" * 64, 10, "a.jpg")])
    assert result.size_bytes == 10


@pytest.mark.parametrize("url", ["http://example.org/a.jpg", "https://127.0.0.1/a.jpg", "https://169.254.169.254/a", "https://a:b@example.org/a", "https://example.org:444/a"])
def test_remote_import_rejects_unsafe_sources_before_fc(url):
    with pytest.raises(StorageError):
        cloud(lambda _: pytest.fail("unsafe URL reached FC")).import_remote_image(url, "uploads/ai/test")
