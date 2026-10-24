"""Release a fully materialized response only while its current lineage holds."""
from contextlib import suppress
import re
from tempfile import SpooledTemporaryFile
from urllib.parse import quote

from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from app.authz.provenance import validate_provenance


def stream_materialized_bytes(content, bind, identity, provenance, *, media_type, headers):
    """Bounded bytes already read under one authorized snapshot; recheck publication."""
    resource = SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b')
    try:
        resource.write(content)
        resource.seek(0)
        return stream_materialized_file(resource, bind, identity, provenance,
            media_type=media_type, headers=headers)
    except BaseException:
        resource.close()
        raise


def stream_materialized_file(resource, bind, identity, provenance, *, media_type, headers):
    """Take ownership of an already complete spool; never query facts while streaming."""

    def chunks():
        while chunk := resource.read(64 * 1024):
            yield chunk

    class ClosingResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                with suppress(Exception):
                    resource.close()

    response = ClosingResponse(chunks(), media_type=media_type, headers=headers)
    response._resource = resource
    return guard_stream(response, bind, identity, provenance)


def attachment_headers(filename):
    """Preserve the download protocol's ASCII fallback and UTF-8 filename."""
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]+', '_', filename).strip(' .') or 'download'
    fallback = re.sub(r'[^A-Za-z0-9._-]+', '_', name.encode('ascii', 'ignore').decode()).strip(' ._') or 'download'
    encoded = quote(name, safe="!#$&+-.^_`|~")
    return {'Content-Disposition': f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}


def guard_stream(response, bind, identity, provenance):
    """Check before headers and each chunk; hold no transaction across streaming.

    The caller already rendered the complete spool in the same authorized
    snapshot as its source facts. This function never reads new source facts.
    """
    def check():
        with Session(bind) as current:
            validate_provenance(current, identity, provenance)
    try:
        check()
    except BaseException:
        response._resource.close()
        raise
    original = response.body_iterator
    async def chunks():
        try:
            async for chunk in original:
                await run_in_threadpool(check)
                yield chunk
        finally:
            response._resource.close()
    response.body_iterator = chunks()
    return response
