"""Stable identity primitives shared by existing site-issue commands.

Callers own payload normalization, authorization, replay, and transaction
completion. Keep the historical JSON bytes and advisory-key namespace intact.
"""

import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session


def site_issue_request_fingerprint(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def lock_site_issue_idempotency_key(db: Session, idempotency_key: str) -> None:
    """Serialize one client command without persisting sensitive request content."""

    db.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(idempotency_key, 0)
            )
        )
    )
