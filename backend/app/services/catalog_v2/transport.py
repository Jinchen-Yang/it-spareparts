"""Persist canonical logical payloads and raw-byte fragment manifests.

Batch manifests are written by the publisher inside its publication transaction.
Snapshot page manifests are written once when the requested page size is known.
"""
from datetime import datetime, timezone
from uuid import uuid4

from app.config import get_settings
from app.models.catalog_v2 import CatalogV2PayloadFragment
from app.services.catalog_v2.canonical import canonical_bytes, bytes_hash


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def envelope(request_id=None) -> dict:
    return {"schema_version": 2, "source_namespace": "itsp",
            "request_id": request_id or str(uuid4()), "correlation_id": str(uuid4()),
            "generated_at": utc_text(datetime.now(timezone.utc))}


def response_limit() -> int:
    return min(get_settings().catalog_v2_response_max_bytes, 8 * 1024 * 1024)


def persist_fragments(db, *, epoch_id, payload, context, payload_id=None):
    """Persist every fragment; caller owns the enclosing transaction."""
    payload_id = payload_id or str(uuid4())
    # Leave room for Base64 and the common envelope plus opaque continuation.
    chunk_size = min(get_settings().catalog_v2_fragment_max_bytes, 4 * 1024 * 1024,
                     max(1, (response_limit() - 1536) * 3 // 4))
    count = (len(payload) + chunk_size - 1) // chunk_size
    payload_hash = bytes_hash(payload)
    for number in range(1, count + 1):
        raw = payload[(number - 1) * chunk_size:number * chunk_size]
        db.add(CatalogV2PayloadFragment(
            epoch_id=epoch_id, payload_id=payload_id, part_no=number,
            part_count=count, data=raw, part_hash=bytes_hash(raw),
            payload_hash=payload_hash, context=context,
        ))
    db.flush()
    return payload_id


def persist_batch_payload(db, batch):
    """Called before publishing projection/watermark: payload and manifest are atomic."""
    payload = canonical_bytes(dict(
        envelope(), publication_epoch=batch.epoch_id, batch_id=batch.batch_id,
        published_sequence=batch.published_sequence, previous_sequence=batch.previous_sequence,
        transaction_groups=batch.transaction_groups, items=batch.items,
        alias_moves=batch.alias_moves, batch_hash=batch.batch_hash,
        complete=True, next_cursor=None,
    ))
    batch.payload = payload
    if len(payload) > response_limit():
        persist_fragments(db, epoch_id=batch.epoch_id, payload=payload,
                          payload_id=batch.batch_id,
                          context={"kind": "changes", "batch_id": batch.batch_id,
                                   "published_sequence": batch.published_sequence,
                                   "previous_sequence": batch.previous_sequence})
