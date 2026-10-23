"""Host ports match the existing shop OSS/FC adapter semantics.

The host performs cloud calls outside database transactions, then submits verified
results through the fenced publication functions. No provider configuration lives
in the domain and client commands cannot manufacture these trusted results.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class VerifiedImage:
    sha256: str
    width: int
    height: int
    size_bytes: int
    content_type: str
    source_etag: str
    object_key: str


class Storage(Protocol):
    def signed_get(
        self, key: str, expires_in: int = 300, *, download_filename: str | None = None
    ) -> str: ...
    def sign_upload(
        self, key: str, expected_size: int, content_type: str, expires_at: datetime
    ): ...
    def start_multipart(self, key: str, content_type: str, fingerprint: str) -> str: ...
    def sign_part(
        self, key: str, upload_id: str, number: int, size: int, expires_at: datetime
    ): ...
    def list_parts(self, key: str, upload_id: str): ...
    def complete_multipart(self, key: str, upload_id: str, parts: list): ...
    def abort_multipart(self, key: str, upload_id: str | None) -> None: ...
    def head(self, key: str): ...


class CloudImages(Protocol):
    def inspect(
        self, key: str, content_type: str, *, max_bytes: int, max_pixels: int
    ): ...
    def copy(
        self,
        source_key: str,
        target_key: str,
        source_etag: str,
        *,
        sha256: str,
        content_type: str,
        expected_bytes: int,
    ): ...
    def thumbnail(
        self,
        source_key: str,
        target_key: str,
        *,
        expected_sha256: str,
        expected_bytes: int,
    ): ...


def verify_and_archive(
    cloud: CloudImages,
    *,
    staging_key: str,
    target_key: str,
    content_type: str,
    declared_bytes: int,
) -> VerifiedImage:
    """Reuse FC full decode/SHA/EXIF inspection and immutable copy; no DB session."""
    from .errors import CatalogError

    info = cloud.inspect(
        staging_key, content_type, max_bytes=20 * 1024 * 1024, max_pixels=40_000_000
    )
    if info.bytes != declared_bytes or info.content_type != content_type:
        raise CatalogError("media.invalid_image", "文件与上传声明不符", status_code=422)
    cloud.copy(
        staging_key,
        target_key,
        info.source_etag,
        sha256=info.sha256,
        content_type=info.content_type,
        expected_bytes=info.bytes,
    )
    return VerifiedImage(
        info.sha256,
        info.width,
        info.height,
        info.bytes,
        info.content_type,
        info.source_etag,
        target_key,
    )
