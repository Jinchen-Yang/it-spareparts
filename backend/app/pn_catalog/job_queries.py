"""Bounded owner-first job history; continuation never drops denied scan rows."""

import base64
import hashlib
import hmac
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select

from .errors import CatalogError
from .job_models import CatalogJob
from .jobs import _authorize_public, _view

SCAN_LIMIT = 200
PAGE_BYTES = 512 * 1024
STATUSES = frozenset(
    {"pending", "running", "succeeded", "failed", "cancelled", "stale"}
)


def _cursor(value, secret):
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret, b"pn-job-page:v1:" + body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")


def _decode(cursor, fingerprint, secret):
    try:
        if len(cursor) > 512:
            raise ValueError()
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        )
        body, signature = raw[:-32], raw[-32:]
        expected = hmac.new(secret, b"pn-job-page:v1:" + body, hashlib.sha256).digest()
        value = json.loads(body)
        if not hmac.compare_digest(signature, expected) or value["q"] != fingerprint:
            raise ValueError()
        timestamp = datetime.fromisoformat(value["at"])
        if timestamp.tzinfo is None:
            raise ValueError()
        return timestamp, UUID(value["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise CatalogError(
            "pagination.invalid_cursor",
            "任务分页游标无效或筛选条件已变化",
            status_code=422,
        ) from exc


def list_jobs(
    db,
    *,
    context,
    authorize_job,
    cursor_secret: bytes,
    part_id=None,
    inbox_id=None,
    statuses=(),
    cursor=None,
    limit=20,
):
    if (
        type(limit) is not int
        or not 1 <= limit <= 50
        or len(statuses) > 6
        or not set(statuses) <= STATUSES
        or (part_id is not None and inbox_id is not None)
    ):
        raise CatalogError(
            "request.validation_failed", "任务筛选参数无效", status_code=422
        )
    if part_id is not None and (type(part_id) is not int or part_id <= 0):
        raise CatalogError("request.validation_failed", "型号编号无效", status_code=422)
    owner = context.actor.principal_id
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                owner,
                context.actor.client_id,
                part_id,
                str(inbox_id) if inbox_id else None,
                sorted(set(statuses)),
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    boundary = _decode(cursor, fingerprint, cursor_secret) if cursor else None
    filters = [CatalogJob.context["actor"]["principal_id"].astext == owner]
    if part_id is not None:
        filters.append(
            or_(
                CatalogJob.part_id == part_id,
                and_(
                    CatalogJob.kind == "identity.recalculate",
                    CatalogJob.payload["identity_revisions"].has_key(str(part_id)),  # noqa: E711
                ),
            )
        )
    if inbox_id is not None:
        filters += [
            CatalogJob.resource_type == "enrichment.recognize",
            CatalogJob.resource_id == str(inbox_id),
        ]
    if statuses:
        filters.append(CatalogJob.status.in_(set(statuses)))
    items, consumed, remaining = [], 0, PAGE_BYTES - 2048
    last, has_more, budget_full = None, False, False
    while consumed < SCAN_LIMIT:
        query = select(CatalogJob).where(*filters)
        if boundary is not None:
            at, identifier = boundary
            query = query.where(
                or_(
                    CatalogJob.created_at < at,
                    and_(CatalogJob.created_at == at, CatalogJob.id < identifier),
                )
            )
        batch_size = min(32, SCAN_LIMIT - consumed)
        rows = list(
            db.scalars(
                query.order_by(CatalogJob.created_at.desc(), CatalogJob.id.desc())
                .limit(batch_size + 1)
                .execution_options(populate_existing=True)
            )
        )
        if not rows:
            has_more = False
            break
        for index, row in enumerate(rows[:batch_size]):
            view = _view(row)
            try:
                _authorize_public(db, context, "job.read", view, authorize_job)
            except CatalogError as exc:
                if exc.status_code not in (403, 404):
                    raise
                value = None
            else:
                value = view.model_dump(mode="json")
                size = len(
                    json.dumps(
                        value, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                )
                if size > remaining:
                    if not items:
                        raise CatalogError(
                            "job.result_too_large",
                            "任务结果超过分页预算",
                            status_code=503,
                        )
                    has_more = budget_full = True
                    break  # This authorized row has not been consumed; next page retains it.
                remaining -= size
                items.append(value)
            consumed += 1
            last = boundary = (row.created_at, row.id)
            has_more = index + 1 < len(rows)
            if len(items) >= limit or consumed >= SCAN_LIMIT:
                break
        if len(items) >= limit or consumed >= SCAN_LIMIT or budget_full:
            break
        if not has_more:
            break
    next_cursor = (
        _cursor(
            {"q": fingerprint, "at": last[0].isoformat(), "id": str(last[1])},
            cursor_secret,
        )
        if has_more and last
        else None
    )
    return {
        "data": items,
        "meta": {
            "request_id": str(context.request_id),
            "replayed": False,
            "next_cursor": next_cursor,
        },
    }
