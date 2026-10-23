"""Public enrichment downloads; job execution input never crosses this boundary."""
from pydantic import Field
from .wire import WireModel
from .wire_media import MediaMeta


class BundleArchive(WireModel):
    number: int = Field(ge=1)
    size_bytes: int = Field(gt=0)
    sha256: str
    filename: str
    image_count: int = Field(gt=0)
    url: str


class BundleDownload(WireModel):
    job_id: str
    archives: list[BundleArchive]
    expires_in: int = Field(gt=0, le=900)


class BundleDownloadResponse(WireModel):
    data: BundleDownload
    meta: MediaMeta
