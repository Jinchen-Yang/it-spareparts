"""Current resource checks for opaque media job access, without URLs or writes."""

from uuid import UUID

from .errors import CatalogError
from .media_common import access, load, read_identity
from .media_derivatives import assert_visible
from .media_models import GalleryEntry, MultipartUpload
from .media_uploads import owned_upload


def authorize_job_resource(db, job, *, context, authorize):
    expected_types = {
        "media.thumbnail": "media.entry",
        "media.verify_upload": "media.upload",
        "media.complete_multipart": "media.upload",
        "media.abort_multipart": "media.multipart",
    }
    if job.kind not in expected_types or expected_types[job.kind] != job.resource_type:
        raise CatalogError("auth.forbidden", "任务媒体资源类型不匹配", status_code=403)
    try:
        identifier = UUID(job.resource_id)
    except (ValueError, TypeError, AttributeError):
        raise CatalogError(
            "auth.forbidden", "任务媒体资源编号无效", status_code=403
        ) from None
    if job.kind == "media.thumbnail":
        entry = load(db, GalleryEntry, identifier)
        if entry.origin_part_id != job.part_id:
            raise CatalogError("auth.forbidden", "任务图片归属不匹配", status_code=403)
        access(db, context, authorize, "gallery.read", entry.origin_part_id)
        read_identity(db, [entry.origin_part_id])
        entry = load(db, GalleryEntry, identifier, lock=True, shared=True)
        assert_visible(db, entry)
        return
    ticket = owned_upload(db, identifier, context, authorize)
    if ticket.part_id != job.part_id:
        raise CatalogError("auth.forbidden", "任务上传归属不匹配", status_code=403)
    access(db, context, authorize, "uploads.read", ticket.part_id, field="uploads")
    read_identity(db, [ticket.part_id])
    if job.kind in {"media.complete_multipart", "media.abort_multipart"}:
        load(db, MultipartUpload, identifier)
    # Cancelled/failed tickets stay readable to their current owner so cleanup
    # progress and terminal errors remain visible; this never grants retry rights.
