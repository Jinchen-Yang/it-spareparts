"""已安装 OSS SDK 的分片接口与签名离线契约，不调用真实云资源。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import oss2
import pytest
import requests

from app.services.pn_media_cloud import (
    MediaCloudSettings,
    MultipartPart,
    ObjectConflict,
    ObjectNotFound,
    OSSStorage,
    StorageUnavailable,
)

KEY = "uploads/offline-user/offline-session"


@pytest.fixture
def storage():
    return OSSStorage(
        MediaCloudSettings(
            enabled=True,
            oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
            oss_bucket="offline-images",
            oss_region="cn-hangzhou",
            oss_access_key_id="offline-key",
            oss_access_key_secret="offline-secret",
        )
    )


def listing(entries=()):
    return SimpleNamespace(
        upload_list=list(entries),
        prefix_list=[],
        is_truncated=False,
        next_upload_id_marker="",
        next_key_marker="",
    )


def test_real_sdk_v4_signs_key_upload_id_part_number_and_exact_length(storage):
    expires = datetime.now(UTC) + timedelta(minutes=10)
    first = storage.sign_part(KEY, "remote-upload", 2, 7, expires)
    second = storage.sign_part(KEY, "remote-upload", 2, 8, expires)
    url, query = urlsplit(first.url), parse_qs(urlsplit(first.url).query)
    assert url.scheme == "https" and url.hostname == "offline-images.oss-cn-hangzhou.aliyuncs.com"
    assert query["uploadId"] == ["remote-upload"] and query["partNumber"] == ["2"]
    assert query["x-oss-signature-version"] == ["OSS4-HMAC-SHA256"]
    assert query["x-oss-additional-headers"] == ["content-length"]
    assert query["x-oss-signature"] != parse_qs(urlsplit(second.url).query)["x-oss-signature"]
    assert first.headers == {"Content-Type": "application/octet-stream"}
    assert "offline-secret" not in first.url


def test_init_recovers_lost_response_and_preserves_metadata(storage):
    client = storage._bucket = Mock()
    client.list_multipart_uploads.return_value = listing(
        [SimpleNamespace(key=KEY, upload_id="existing")]
    )
    assert storage.start_multipart(KEY, "image/png", "a" * 64) == "existing"
    client.init_multipart_upload.assert_not_called()
    client.list_multipart_uploads.return_value = listing()
    client.init_multipart_upload.return_value = SimpleNamespace(upload_id="new")
    assert storage.start_multipart(KEY, "image/png", "a" * 64) == "new"
    client.init_multipart_upload.assert_called_once_with(
        KEY, headers={"Content-Type": "image/png", "x-oss-meta-request-fingerprint": "a" * 64}
    )


def test_list_parts_and_complete_use_sdk_fields_and_forbid_overwrite(storage):
    client = storage._bucket = Mock()
    client.list_parts.return_value = SimpleNamespace(
        is_truncated=False, parts=[oss2.models.PartInfo(1, '"' + "A" * 32 + '"', size=1024)]
    )
    parts = storage.list_parts(KEY, "remote")
    assert parts == [MultipartPart(1, "A" * 32, 1024)]
    client.list_parts.assert_called_once_with(KEY, "remote", max_parts=21)
    storage.complete_multipart(KEY, "remote", parts)
    call = client.complete_multipart_upload.call_args
    assert call.args[:2] == (KEY, "remote")
    assert [(part.part_number, part.etag, part.size) for part in call.args[2]] == [
        (1, "A" * 32, 1024)
    ]
    assert call.kwargs["headers"] == {"x-oss-forbid-overwrite": "true"}


def test_unexpected_more_than_twenty_parts_is_not_silently_truncated(storage):
    storage._bucket = Mock()
    storage._bucket.list_parts.return_value = SimpleNamespace(is_truncated=True, parts=[])
    with pytest.raises(ObjectConflict):
        storage.list_parts(KEY, "remote")


def test_missing_upload_is_explicit_but_abort_is_idempotent(storage):
    client = storage._bucket = Mock()
    missing = oss2.exceptions.NoSuchUpload(404, {}, "sensitive", {"Code": "NoSuchUpload"})
    client.list_parts.side_effect = missing
    with pytest.raises(ObjectNotFound):
        storage.list_parts(KEY, "remote")
    client.complete_multipart_upload.side_effect = missing
    with pytest.raises(ObjectNotFound):
        storage.complete_multipart(KEY, "remote", [MultipartPart(1, "a" * 32, 1)])
    client.list_multipart_uploads.return_value = listing()
    client.abort_multipart_upload.side_effect = missing
    storage.abort_multipart(KEY, "remote")
    client.abort_multipart_upload.assert_called_once_with(KEY, "remote")


@pytest.mark.parametrize("operation", ["start", "list", "complete", "abort"])
def test_sdk_transport_errors_never_include_signed_urls_or_credentials(storage, operation):
    client = storage._bucket = Mock()
    error = requests.ConnectionError("private-key https://private/?Signature=secret")
    for method in ["list_multipart_uploads", "list_parts", "complete_multipart_upload"]:
        getattr(client, method).side_effect = error
    with pytest.raises(StorageUnavailable) as result:
        if operation == "start":
            storage.start_multipart(KEY, "image/png", "a" * 64)
        elif operation == "list":
            storage.list_parts(KEY, "remote")
        elif operation == "complete":
            storage.complete_multipart(KEY, "remote", [])
        else:
            storage.abort_multipart(KEY, "remote")
    assert "private" not in str(result.value) and "Signature" not in str(result.value)
    assert result.value.__suppress_context__


def test_orphan_cleanup_never_aborts_winning_upload_id(storage):
    client = storage._bucket = Mock()
    client.list_multipart_uploads.side_effect = AssertionError("must not list/abort winner")
    storage.abort_multipart(KEY, "loser", include_orphans=False)
    client.abort_multipart_upload.assert_called_once_with(KEY, "loser")


def test_post_policy_is_exact_key_type_bounded_size_and_never_overwrites(storage):
    import base64
    import json
    form = storage.sign_upload(KEY, 123, "image/png", datetime.now(UTC) + timedelta(minutes=5))
    conditions = json.loads(base64.b64decode(form.fields["policy"]))["conditions"]
    assert {"key": KEY} in conditions and {"Content-Type": "image/png"} in conditions
    assert ["content-length-range", 1, 123] in conditions
    assert {"x-oss-forbid-overwrite": "true"} in conditions
    assert "offline-secret" not in repr(form)


@pytest.mark.parametrize("url", ["http://cloud.invalid", "https://user:pass@cloud.invalid", "https://cloud.invalid/path", "https://cloud.invalid?x=y", "https://cloud.invalid#f", "https://cloud.invalid:bad", "https://cloud.invalid\\evil"])
def test_invalid_provider_origin_is_rejected_before_network(url):
    from app.services.pn_media_cloud import HTTPCloudImages, StorageNotConfigured
    config = MediaCloudSettings(enabled=True, cloud_images_url=url, cloud_images_secret="x" * 40)
    with pytest.raises(StorageNotConfigured):
        HTTPCloudImages(config).inspect(KEY, "image/png")


def test_disabled_or_missing_provider_configuration_fails_closed():
    from app.services.pn_media_cloud import HTTPCloudImages, StorageNotConfigured
    settings = MediaCloudSettings()
    with pytest.raises(StorageNotConfigured):
        OSSStorage(settings).signed_get("img/test")
    with pytest.raises(StorageNotConfigured):
        HTTPCloudImages(settings).inspect(KEY, "image/png")


def cloud_settings():
    return MediaCloudSettings(enabled=True, cloud_images_url="https://cloud.invalid", cloud_images_secret="test-secret" * 4)


def test_fc_wire_protocol_hmac_and_existing_prefixes():
    import json
    import httpx
    from app.services.pn_media_cloud import HTTPCloudImages, request_signature
    settings = cloud_settings()
    calls = []
    def endpoint(request):
        calls.append(json.loads(request.content))
        assert str(request.url) == "https://cloud.invalid/v1/process"
        assert request.headers["x-cloud-signature"] == request_signature(settings.cloud_images_secret.get_secret_value(),
            request.headers["x-cloud-timestamp"], request.headers["x-cloud-request-id"], request.content)
        body = ({"sha256": "a" * 64, "width": 600, "height": 400, "bytes": 100,
                 "content_type": "image/png", "extension": "png", "source_etag": "etag"}
                if calls[-1]["operation"] == "inspect" else {"ok": True})
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))
    cloud = HTTPCloudImages(settings, transport=httpx.MockTransport(endpoint))
    result = cloud.inspect(KEY, "image/png")
    cloud.copy(KEY, "img/pn-catalog/test/original", result.source_etag,
        sha256=result.sha256, content_type=result.content_type, expected_bytes=result.bytes)
    cloud.thumbnail("img/pn-catalog/test/original", "thumb/pn/test/recipe/thumbnail.webp",
        expected_sha256=result.sha256, expected_bytes=result.bytes)
    assert [c["operation"] for c in calls] == ["inspect", "copy", "thumbnail"]
    assert calls[1]["arguments"]["source_etag"] == "etag"


@pytest.mark.parametrize("status,error,code", [(404, "not_found", "media.object_not_found"),
    (413, "too_large", "media.image_too_large"), (409, "conflict", "media.object_conflict"),
    (422, "invalid_image", "media.invalid_image"), (401, "bad secret", "media.provider_auth_failed"),
    (503, "private signed url", "media.provider_unavailable")])
def test_fc_errors_are_stable_and_redacted(status, error, code):
    import json
    import httpx
    from app.pn_catalog.errors import CatalogError
    from app.services.pn_media_cloud import HTTPCloudImages
    transport = httpx.MockTransport(lambda _: httpx.Response(status,
        stream=httpx.ByteStream(json.dumps({"error": error, "private": "credential"}).encode())))
    with pytest.raises(CatalogError) as caught:
        HTTPCloudImages(cloud_settings(), transport=transport).inspect(KEY, "image/png")
    assert caught.value.code == code
    assert "credential" not in str(caught.value) and "private" not in str(caught.value)


@pytest.mark.parametrize("status,data,headers", [(302, b"", {"location": "https://evil.invalid"}),
    (200, b"x" * 65537, {}), (200, b"[]", {}), (200, b'{"sha256":"bad"}', {}),
    (200, b"{}", {"content-encoding": "gzip"})])
def test_fc_rejects_redirects_oversize_and_invalid_results(status, data, headers):
    import httpx
    from app.services.pn_media_cloud import HTTPCloudImages
    requests_seen = []
    def responder(request):
        requests_seen.append(request)
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(data))
    with pytest.raises(StorageUnavailable):
        HTTPCloudImages(cloud_settings(), transport=httpx.MockTransport(responder)).inspect(KEY, "image/png")
    assert len(requests_seen) == 1


def test_fc_has_total_deadline():
    import asyncio
    import httpx
    from app.services.pn_media_cloud import HTTPCloudImages
    settings = cloud_settings().model_copy(update={"cloud_images_timeout_seconds": 0.01})
    async def stall(request):
        await asyncio.sleep(1)
        pytest.fail("timeout did not cancel")
    with pytest.raises(StorageUnavailable):
        HTTPCloudImages(settings, transport=httpx.MockTransport(stall)).inspect(KEY, "image/png")


def test_network_guard_covers_each_orphan_listing_and_abort_without_affecting_local_signing(storage):
    client = storage._bucket = Mock()
    client.list_multipart_uploads.return_value = listing([SimpleNamespace(key=KEY, upload_id="orphan")])
    guarded_calls = []
    bound = storage.with_network_guard(lambda: guarded_calls.append(True))
    bound.abort_multipart(KEY, "known")
    assert len(guarded_calls) == 3  # listing plus both aborts
    assert storage._network_guard is None
    guarded_calls.clear()
    bound.signed_get("img/local-only")
    assert guarded_calls == []
