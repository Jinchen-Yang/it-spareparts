"""PN media host transports; wire-compatible subset of shop OSS/FC protocols.

No app settings or database imports. Object bytes stay in OSS/FC; only metadata and
signed transport capabilities pass through this process. Provider config is opt-in.
"""
import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import requests
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.pn_catalog.errors import CatalogError


class MediaCloudSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PN_MEDIA_", extra="ignore")
    enabled: bool = False
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_region: str = ""
    oss_access_key_id: SecretStr = SecretStr("")
    oss_access_key_secret: SecretStr = SecretStr("")
    cloud_images_url: str = ""
    cloud_images_secret: SecretStr = SecretStr("")
    cloud_images_timeout_seconds: float = Field(default=60, ge=1, le=90)


class StorageError(CatalogError):
    def __init__(self, message):
        super().__init__("media.invalid_transport", message, status_code=422)


class StorageNotConfigured(CatalogError):
    def __init__(self, message="图片服务尚未配置"):
        super().__init__("media.provider_not_configured", message, status_code=503)


class StorageUnavailable(CatalogError):
    def __init__(self, message="图片服务暂时不可用"):
        super().__init__("media.provider_unavailable", message, status_code=503, retryable=True)


class ObjectNotFound(CatalogError):
    def __init__(self, message="上传文件尚不存在"):
        super().__init__("media.object_not_found", message, status_code=404, retryable=True)


class ObjectTooLarge(CatalogError):
    def __init__(self, message="图片文件超过允许大小"):
        super().__init__("media.image_too_large", message, status_code=422)


class ObjectConflict(CatalogError):
    def __init__(self, message="图片对象已变化"):
        super().__init__("media.object_conflict", message)


@dataclass(frozen=True)
class MultipartPart:
    part_number: int
    etag: str
    size_bytes: int

@dataclass(frozen=True)
class PartSignature:
    url: str
    headers: dict[str, str]
    expires_at: datetime

@dataclass(frozen=True)
class UploadForm:
    method: str
    url: str
    fields: dict[str, str]
    headers: dict[str, str]
    expires_at: datetime

@dataclass(frozen=True)
class ObjectMetadata:
    etag: str
    size_bytes: int
    content_type: str
    # 这是对象元数据，不代表上传者声明的哈希可信；正式对象仅由云端校验后写入。
    sha256: str | None
    request_fingerprint: str | None = None

def _staging_key(key: str) -> None:
    if (not isinstance(key, str) or not key.startswith("uploads/") or len(key) > 1024
        or any(p in {"", ".", ".."} for p in key.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in key)):
        raise StorageError("上传凭证只能访问临时上传目录")

def _policy(
    key: str,
    expected_size: int,
    content_type: str,
    expires_at: datetime,
    signed_fields: dict[str, str],
) -> str:
    _staging_key(key)
    if type(expected_size) is not int or not 0 < expected_size <= 20 * 1024 * 1024 or content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise StorageError("上传大小或图片类型无效")
    if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
        raise StorageError("上传凭证已过期")
    policy = {
        "expiration": expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "conditions": [
            {"key": key},
            {"Content-Type": content_type},
            ["content-length-range", 1, expected_size],
            {"success_action_status": "204"},
            {"x-oss-forbid-overwrite": "true"},
            *({name: value} for name, value in signed_fields.items()),
        ],
    }
    return base64.b64encode(json.dumps(policy, separators=(",", ":")).encode()).decode()

def _signature(policy: str, secret: str, date: str, region: str) -> str:
    # OSS POST V4：密钥派生含日期、地域和服务；只返回签名，不返回 AK Secret。
    # https://help.aliyun.com/zh/oss/developer-reference/signature-version-4-recommend
    signing_key = f"aliyun_v4{secret}".encode()
    for value in (date, region, "oss", "aliyun_v4_request"):
        signing_key = hmac.new(signing_key, value.encode(), hashlib.sha256).digest()
    return hmac.new(signing_key, policy.encode(), hashlib.sha256).hexdigest()

def _upload_form(
    url: str,
    key: str,
    expected_size: int,
    content_type: str,
    expires_at: datetime,
    access_key_id: str,
    access_key_secret: str,
    bucket: str,
    region: str,
) -> UploadForm:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    date = timestamp[:8]
    signed_fields = {
        "bucket": bucket,
        "x-oss-signature-version": "OSS4-HMAC-SHA256",
        "x-oss-credential": f"{access_key_id}/{date}/{region}/oss/aliyun_v4_request",
        "x-oss-date": timestamp,
    }
    policy = _policy(key, expected_size, content_type, expires_at, signed_fields)
    return UploadForm(
        method="POST",
        url=url,
        fields={
            "key": key,
            "Content-Type": content_type,
            "policy": policy,
            **{name: value for name, value in signed_fields.items() if name != "bucket"},
            "x-oss-signature": _signature(policy, access_key_secret, date, region),
            "success_action_status": "204",
            "x-oss-forbid-overwrite": "true",
        },
        headers={},
        expires_at=expires_at,
    )

def _download_disposition(filename: str) -> str:
    # 两种文件名同时提供；去掉路径与控制字符，避免响应头注入。
    name = "".join(char for char in filename if ord(char) >= 32 and ord(char) != 127)
    name = name.replace("\\", "_").replace("/", "_").strip()[:240] or "download"
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip() or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"

class OSSStorage:
    """Narrow shop-compatible OSS transport. Construction and signing never perform I/O."""

    def __init__(self, settings: MediaCloudSettings):
        self._settings = settings
        self._bucket = None
        self._network_guard = None

    def _configured(self) -> None:
        settings = self._settings
        if not settings.enabled or not all(
            (
                settings.oss_endpoint,
                settings.oss_bucket,
                settings.oss_region,
                settings.oss_access_key_id.get_secret_value(),
                settings.oss_access_key_secret.get_secret_value(),
            )
        ):
            raise StorageNotConfigured("图片存储尚未配置，请联系管理员配置 OSS")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", settings.oss_region):
            raise StorageNotConfigured("OSS Region 必须是有效地域标识")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", settings.oss_bucket):
            raise StorageNotConfigured("OSS Bucket 名称无效")
        endpoint = _https_origin(settings.oss_endpoint)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise StorageNotConfigured("OSS Endpoint 必须是完整的 HTTPS 服务地址")

    def _client(self):
        self._configured()
        if self._bucket is None:
            import oss2

            auth = oss2.AuthV4(
                self._settings.oss_access_key_id.get_secret_value(),
                self._settings.oss_access_key_secret.get_secret_value(),
            )
            self._bucket = oss2.Bucket(
                auth,
                self._settings.oss_endpoint,
                self._settings.oss_bucket,
                connect_timeout=10,
                region=self._settings.oss_region,
            )
        if self._network_guard is not None:
            return _GuardedBucket(self._bucket, self._network_guard)
        return self._bucket

    def with_network_guard(self, callback):
        from copy import copy
        bound = copy(self)
        bound._network_guard = callback
        return bound

    def sign_upload(
        self, key: str, expected_size: int, content_type: str, expires_at: datetime
    ) -> UploadForm:
        self._configured()
        endpoint = urlsplit(self._settings.oss_endpoint)
        return _upload_form(
            f"https://{self._settings.oss_bucket}.{endpoint.netloc}",
            key,
            expected_size,
            content_type,
            expires_at,
            self._settings.oss_access_key_id.get_secret_value(),
            self._settings.oss_access_key_secret.get_secret_value(),
            self._settings.oss_bucket,
            self._settings.oss_region,
        )

    def start_multipart(self, key: str, content_type: str, fingerprint: str) -> str:
        _staging_key(key)
        import oss2

        client = self._client()
        try:
            # 随机 key 只属于当前会话；初始化响应丢失时恢复同一个 OSS uploadId。
            for pending in oss2.MultipartUploadIterator(client, prefix=key):
                if pending.key == key:
                    return pending.upload_id
            return client.init_multipart_upload(
                key,
                headers={
                    "Content-Type": content_type,
                    "x-oss-meta-request-fingerprint": fingerprint,
                },
            ).upload_id
        except (oss2.exceptions.OssError, requests.RequestException):
            raise StorageUnavailable("暂时无法初始化分片上传，请使用同一请求重试") from None

    def sign_part(self, key, upload_id, number, size, expires_at):
        _staging_key(key)
        seconds = int((expires_at - datetime.now(UTC)).total_seconds())
        if seconds <= 0 or not 1 <= number <= 20 or not 0 < size <= 1024 * 1024:
            raise StorageError("分片参数或有效期无效")
        import oss2

        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
        try:
            url = self._client().sign_url(
                "PUT",
                key,
                seconds,
                headers=headers,
                params={"uploadId": upload_id, "partNumber": str(number)},
                additional_headers={"content-length"},
            )
            # 浏览器禁止脚本设置 Content-Length，但发送 Blob 时自动带精确长度。
            return PartSignature(url, {"Content-Type": headers["Content-Type"]}, expires_at)
        except (oss2.exceptions.OssError, requests.RequestException):
            raise StorageUnavailable("暂时无法签发分片上传链接") from None

    def list_parts(self, key, upload_id):
        _staging_key(key)
        import oss2

        try:
            result = self._client().list_parts(key, upload_id, max_parts=21)
            if result.is_truncated or len(result.parts) > 20:
                raise ObjectConflict("分片数量超出允许范围")
            return [
                MultipartPart(item.part_number, item.etag.strip('"'), item.size)
                for item in result.parts
            ]
        except oss2.exceptions.NoSuchUpload:
            raise ObjectNotFound("分片上传会话已不存在") from None
        except (oss2.exceptions.OssError, requests.RequestException):
            raise StorageUnavailable("暂时无法读取已上传分片") from None

    def complete_multipart(self, key, upload_id, parts):
        _staging_key(key)
        import oss2

        try:
            self._client().complete_multipart_upload(
                key,
                upload_id,
                [
                    oss2.models.PartInfo(part.part_number, part.etag, size=part.size_bytes)
                    for part in parts
                ],
                headers={"x-oss-forbid-overwrite": "true"},
            )
        except oss2.exceptions.NoSuchUpload:
            raise ObjectNotFound("分片上传会话已完成或不存在") from None
        except (oss2.exceptions.OssError, requests.RequestException):
            raise StorageUnavailable("合并结果暂未确认，请查询上传状态后重试") from None

    def abort_multipart(self, key, upload_id, *, include_orphans=True):
        _staging_key(key)
        import oss2

        client = self._client()
        try:
            # 也覆盖初始化响应丢失、数据库尚未记录 uploadId 的孤立分片。
            identifiers = {upload_id} if upload_id else set()
            if include_orphans:
                identifiers.update(
                    item.upload_id
                    for item in oss2.MultipartUploadIterator(client, prefix=key)
                    if item.key == key
                )
            for identifier in identifiers:
                try:
                    client.abort_multipart_upload(key, identifier)
                except oss2.exceptions.NoSuchUpload:
                    pass
        except (oss2.exceptions.OssError, requests.RequestException):
            raise StorageUnavailable("暂时无法取消分片上传，请重试") from None

    def head(self, key: str) -> ObjectMetadata:
        """只读取 OSS 响应头，应用服务器不接收图片字节。"""
        client = self._client()
        import oss2

        try:
            result = client.head_object(key)
            if not result.etag or result.content_length is None or result.content_length < 0:
                raise StorageUnavailable("图片存储返回了无效的文件信息")
            digest = result.headers.get("x-oss-meta-sha256")
            return ObjectMetadata(
                etag=result.etag,
                size_bytes=result.content_length,
                content_type=result.content_type or "application/octet-stream",
                sha256=digest if digest and re.fullmatch(r"[0-9a-f]{64}", digest) else None,
                request_fingerprint=result.headers.get("x-oss-meta-request-fingerprint"),
            )
        except oss2.exceptions.NotFound:
            raise ObjectNotFound("上传文件尚不存在，请完成上传后再试") from None
        except (oss2.exceptions.OssError, requests.RequestException):
            raise StorageUnavailable("暂时无法读取图片信息，请稍后重试") from None

    def signed_get(
        self, key: str, expires_in: int = 3600, *, download_filename: str | None = None
    ) -> str:
        client = self._client()
        import oss2

        try:
            if download_filename is not None:
                return client.sign_url(
                    "GET",
                    key,
                    expires_in,
                    params={
                        "response-content-disposition": _download_disposition(download_filename)
                    },
                )
            return client.sign_url("GET", key, expires_in)
        except (oss2.exceptions.OssError, requests.RequestException):
            raise StorageUnavailable("暂时无法生成图片访问链接") from None


class _GuardedBucket:
    """Task-local wrapper; each paginated SDK network request renews the same lease."""
    _network_methods = {"list_multipart_uploads", "init_multipart_upload", "list_parts",
                        "complete_multipart_upload", "abort_multipart_upload", "head_object"}

    def __init__(self, bucket, guard):
        self.bucket, self.guard = bucket, guard

    def __getattr__(self, name):
        value = getattr(self.bucket, name)
        if name not in self._network_methods:
            return value
        def guarded(*args, **kwargs):
            self.guard()
            return value(*args, **kwargs)
        return guarded


def _https_origin(value: str):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
            and not parsed.password and parsed.path in {"", "/"} and not parsed.query
            and not parsed.fragment and parsed.port != 0
            and not any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise StorageNotConfigured("图片服务地址必须为 HTTPS 服务源站")
    return parsed


def _object_key(key, prefixes):
    if (not isinstance(key, str) or not 1 <= len(key) <= 1024 or not key.startswith(prefixes)
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in key)):
        raise StorageError("图片对象位置无效")
    return key


def canonical_body(operation, arguments):
    return json.dumps({"operation": operation, "arguments": arguments}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def request_signature(secret, timestamp, request_id, body, *, path="/v1/process"):
    digest = hashlib.sha256(body).hexdigest()
    message = f"POST\n{path}\n{timestamp}\n{request_id}\n{digest}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class RemoteImage:
    sha256: str
    width: int
    height: int
    bytes: int
    content_type: str
    extension: str
    source_etag: str


class HTTPCloudImages:
    """FC metadata protocol with a total deadline and bounded, uncompressed responses."""
    def __init__(self, settings: MediaCloudSettings, *, transport=None):
        self._settings, self._transport = settings, transport

    async def _request(self, operation, arguments):
        import asyncio
        import time
        from uuid import uuid4
        import httpx

        settings = self._settings
        _https_origin(settings.cloud_images_url)
        secret = settings.cloud_images_secret.get_secret_value()
        if not settings.enabled or len(secret) < 32:
            raise StorageNotConfigured()
        body = canonical_body(operation, arguments)
        path = "/v1/bundle" if operation == "bundle" else "/v1/process"
        if len(body) > (256 * 1024 if operation == "bundle" else 16 * 1024):
            raise StorageError("图片处理请求过大")
        request_id = uuid4().hex
        timestamp = str(int(time.time()))
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity",
            "X-Cloud-Timestamp": timestamp, "X-Cloud-Request-Id": request_id,
            "X-Cloud-Signature": request_signature(secret, timestamp, request_id, body, path=path)}
        try:
            async with asyncio.timeout(settings.cloud_images_timeout_seconds):
                async with httpx.AsyncClient(transport=self._transport, trust_env=False) as client:
                    async with client.stream("POST", settings.cloud_images_url.rstrip("/") + path,
                        content=body, headers=headers, follow_redirects=False,
                        timeout=httpx.Timeout(settings.cloud_images_timeout_seconds)) as response:
                        if response.headers.get("content-encoding", "identity") != "identity":
                            raise StorageUnavailable("图片服务返回格式无效")
                        data = bytearray()
                        async for chunk in response.aiter_raw():
                            data.extend(chunk)
                            if len(data) > 64 * 1024:
                                raise StorageUnavailable("图片服务返回过大")
                        try:
                            result = json.loads(data)
                            if not isinstance(result, dict):
                                raise ValueError()
                        except (ValueError, RecursionError):
                            raise StorageUnavailable("图片服务返回格式无效") from None
                        if response.status_code == 200:
                            return result
                        code = result.get("error")
                        code = code if isinstance(code, str) else None
                        if response.status_code in {401, 403} or code in {"not_configured", "unauthorized"}:
                            raise CatalogError("media.provider_auth_failed", "图片服务鉴权失败", status_code=503)
                        if code == "not_found":
                            raise ObjectNotFound()
                        if code == "too_large":
                            raise ObjectTooLarge()
                        if code == "conflict":
                            raise ObjectConflict()
                        if code == "invalid_image":
                            raise CatalogError("media.invalid_image", "图片格式、尺寸或内容无效", status_code=422)
                        raise StorageUnavailable()
        except (httpx.HTTPError, TimeoutError, OSError):
            raise StorageUnavailable() from None

    def _call(self, operation, arguments):
        import asyncio
        return asyncio.run(self._request(operation, arguments))

    def inspect(self, key, content_type, *, max_bytes=20 * 1024 * 1024, max_pixels=40_000_000):
        _object_key(key, ("uploads/", "img/"))
        if (type(max_bytes) is not int or not 1 <= max_bytes <= 20 * 1024 * 1024
            or type(max_pixels) is not int or not 1 <= max_pixels <= 40_000_000
            or content_type not in {"image/jpeg", "image/png", "image/webp"}):
            raise StorageError("图片校验限额或类型无效")
        result = self._call("inspect", {"key": key, "content_type": content_type,
                                       "max_bytes": max_bytes, "max_pixels": max_pixels})
        try:
            image = RemoteImage(**result)
            if (not isinstance(image.sha256, str) or re.fullmatch(r"[a-f0-9]{64}", image.sha256) is None
                or type(image.width) is not int or type(image.height) is not int
                or min(image.width, image.height) < 1 or image.width * image.height > max_pixels
                or type(image.bytes) is not int or not 1 <= image.bytes <= max_bytes
                or (image.content_type, image.extension) not in {("image/jpeg", "jpg"), ("image/png", "png"), ("image/webp", "webp")}
                or image.content_type != content_type or not isinstance(image.source_etag, str)
                or not 1 <= len(image.source_etag) <= 256
                or any(ord(c) < 32 or ord(c) == 127 for c in image.source_etag)):
                raise ValueError()
            return image
        except (TypeError, ValueError):
            raise StorageUnavailable("图片校验结果无效") from None

    def copy(self, source_key, target_key, source_etag, *, sha256, content_type, expected_bytes):
        _object_key(source_key, ("uploads/", "img/"))
        _object_key(target_key, ("img/",))
        if (not isinstance(sha256, str) or re.fullmatch(r"[a-f0-9]{64}", sha256) is None or type(expected_bytes) is not int
            or not 1 <= expected_bytes <= 20 * 1024 * 1024 or not isinstance(source_etag, str)
            or not 1 <= len(source_etag) <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in source_etag)
            or content_type not in {"image/jpeg", "image/png", "image/webp"}):
            raise StorageError("图片复制参数无效")
        result = self._call("copy", {"source_key": source_key, "target_key": target_key,
            "source_etag": source_etag, "sha256": sha256, "content_type": content_type, "expected_bytes": expected_bytes})
        if result != {"ok": True} or type(result.get("ok")) is not bool:
            raise StorageUnavailable("图片复制结果无效")

    def thumbnail(self, source_key, target_key, *, expected_sha256, expected_bytes):
        _object_key(source_key, ("img/",))
        _object_key(target_key, ("thumb/",))
        if (not isinstance(expected_sha256, str) or re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None
            or type(expected_bytes) is not int or not 1 <= expected_bytes <= 20 * 1024 * 1024):
            raise StorageError("缩略图输入无效")
        result = self._call("thumbnail", {"source_key": source_key, "target_key": target_key,
            "expected_sha256": expected_sha256, "expected_bytes": expected_bytes})
        if result != {"ok": True} or type(result.get("ok")) is not bool:
            raise StorageUnavailable("缩略图处理结果无效")


    def recognition(self, source_key, content_type):
        _object_key(source_key, ("img/", "uploads/"))
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise StorageError("图片类型无效")
        result = self._call("recognition", {"source_key": source_key, "content_type": content_type,
            "max_bytes": 20 * 1024 * 1024, "max_pixels": 40_000_000})
        try:
            image = RemoteRecognitionImage(**result)
            _object_key(image.key, ("vision/",))
            if image.content_type != "image/jpeg" or type(image.size_bytes) is not int or not 0 < image.size_bytes <= 4 * 1024 * 1024:
                raise ValueError()
            return image
        except (TypeError, ValueError, CatalogError):
            raise StorageUnavailable("识别副本结果无效") from None

    def import_remote_image(self, url, staging_key):
        _public_url(url)
        _object_key(staging_key, ("uploads/ai/",))
        result = self._call("import_remote_image", {"url": url, "staging_key": staging_key})
        try:
            key = result.pop("key")
            if key != staging_key:
                raise ValueError()
            # Re-inspection independently checks SHA, decode and metadata; caller budgets both calls.
            return RemoteImportedImage(**result, key=key)
        except (TypeError, ValueError, KeyError):
            raise StorageUnavailable("研究图片导入结果无效") from None

    def discover_remote_images(self, source_url):
        _public_url(source_url)
        result = self._call("discover_remote_images", {"source_url": source_url})
        try:
            if set(result) != {"candidates"} or not isinstance(result["candidates"], list) or len(result["candidates"]) > 20:
                raise ValueError()
            candidates = [RemoteImageCandidate(**raw) for raw in result["candidates"]]
            for candidate in candidates:
                _public_url(candidate.url)
                _public_url(candidate.source_url)
                if not isinstance(candidate.caption, str) or len(candidate.caption) > 500:
                    raise ValueError()
            return candidates
        except (TypeError, ValueError, CatalogError):
            raise StorageUnavailable("网页图片候选无效") from None

    def bundle(self, bundle_id, part_number, files, *, watermark_text=None):
        from dataclasses import asdict
        from uuid import UUID
        identifier = str(UUID(str(bundle_id)))
        if (type(part_number) is not int or not 1 <= part_number <= 1000 or not 1 <= len(files) <= 100
            or sum(f.size_bytes for f in files) > 128 * 1024 * 1024
            or (watermark_text is not None and (not isinstance(watermark_text, str) or len(watermark_text) > 200))):
            raise StorageError("压缩分卷参数无效")
        for f in files:
            _object_key(f.key, ("img/",))
            if (not re.fullmatch(r"[a-f0-9]{64}", f.sha256) or type(f.size_bytes) is not int or f.size_bytes <= 0
                or not f.filename or len(f.filename) > 255 or any(ord(c) < 32 or c in "/\\" for c in f.filename)):
                raise StorageError("压缩文件清单无效")
        result = self._call("bundle", {"bundle_id": identifier, "part_number": part_number,
            "files": [asdict(f) for f in files], "watermark_text": watermark_text, "timeout_seconds": None})
        try:
            archive = CloudBundleArchive(**result)
            if (archive.key != f"bundle/{identifier}/part-{part_number}.zip"
                or type(archive.size_bytes) is not int or not 0 < archive.size_bytes <= 129 * 1024 * 1024
                or not re.fullmatch(r"[a-f0-9]{64}", archive.sha256)
                or not isinstance(archive.filename, str) or not archive.filename.endswith(".zip")
                or len(archive.filename) > 256 or any(ord(c) < 32 or c in "/\\" for c in archive.filename)):
                raise ValueError()
            return archive
        except (TypeError, ValueError):
            raise StorageUnavailable("压缩结果无效") from None


def _public_url(value):
    from urllib.parse import urlsplit
    import ipaddress
    try:
        parsed = urlsplit(value)
        if (len(value) > 4096 or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in {None, 443} or any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in value)):
            raise ValueError()
        if parsed.hostname.lower() in {"localhost", "metadata.google.internal"} or parsed.hostname.endswith((".local", ".internal")):
            raise ValueError()
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
    except (ValueError, TypeError):
        raise StorageError("研究来源必须是公开 HTTPS 地址") from None
    # FC revalidates DNS and every redirect against public IP ranges before fetching.


@dataclass(frozen=True)
class RemoteRecognitionImage:
    key: str
    content_type: str
    size_bytes: int


@dataclass(frozen=True)
class RemoteImportedImage(RemoteImage):
    key: str


@dataclass(frozen=True)
class RemoteImageCandidate:
    url: str
    source_url: str
    caption: str = ""


@dataclass(frozen=True)
class CloudBundleFile:
    key: str
    sha256: str
    size_bytes: int
    filename: str


@dataclass(frozen=True)
class CloudBundleArchive:
    key: str
    size_bytes: int
    sha256: str
    filename: str
