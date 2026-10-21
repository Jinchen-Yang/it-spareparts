"""D-36 catalog v2: immutable publications, fixed snapshots and bounded transport.

Credentials and errors are local to this router. Reads write only catalog transport
metadata; source master data is never changed here.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models.catalog_v2 import (
    CatalogV2Batch, CatalogV2Cursor, CatalogV2Epoch, CatalogV2PayloadFragment,
    CatalogV2Projection, CatalogV2Snapshot,
)
from app.services.catalog_v2.canonical import (
    CanonicalError, canonical_bytes, canonical_hash, segment_hash, snapshot_hash,
    strict_loads,
)
from app.services.catalog_v2.transport import (
    envelope, persist_fragments, response_limit, utc_text,
)

_MAX_INT = 9007199254740991
_QUERY_FIELDS = {
    "create_snapshot": set(),
    "list_snapshot": {"snapshot_id", "cursor", "limit"},
    "changes": {"publication_epoch", "after", "cursor", "limit"},
    "part_current": set(),
    "digests": {"snapshot_id", "publication_epoch", "watermark", "cursor", "limit"},
}


class CatalogError(Exception):
    def __init__(self, status: int, code: str, message: str, *, details=None):
        self.status, self.code, self.message = status, code, message
        self.details = details or {}


def _failure(error: CatalogError, request: Request):
    return JSONResponse(status_code=error.status, content={"error": {
        "code": error.code, "message": error.message,
        "retryable": error.status in {429, 502, 503, 504},
        "request_id": getattr(request.state, "catalog_request_id", str(uuid4())),
        "details": error.details,
    }}, headers={"Cache-Control": "no-store"})


def _invalid(code="invalid_field"):
    raise CatalogError(422, code, "目录请求字段或游标无效")


def _expired(reason):
    raise CatalogError(410, "cursor_expired", "发布版本已失效，请重新获取全量快照",
                       details={"reason": reason})


def _authenticate(request: Request):
    clients = get_settings().catalog_v2_read_clients
    if not clients:
        raise CatalogError(404, "integration_disabled", "目录 v2 尚未启用")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 4096:
        raise CatalogError(401, "invalid_service_credential", "目录服务凭据无效")
    digest = hashlib.sha256(token.encode()).hexdigest()
    matched = None
    for client_id, secret in clients.items():
        if hmac.compare_digest(digest, secret.get_secret_value()):
            matched = client_id
    if matched is None:
        raise CatalogError(401, "invalid_service_credential", "目录服务凭据无效")
    request.state.catalog_client_id = matched


class CatalogRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        allowed = _QUERY_FIELDS[self.endpoint.__name__]

        async def wrapped(request):
            request.state.catalog_request_id = str(uuid4())
            try:
                _authenticate(request)
                seen = set()
                for field, _ in request.query_params.multi_items():
                    if field not in allowed or field in seen:
                        _invalid()
                    seen.add(field)
                # Read the ASGI stream with a hard bound, including chunked bodies.
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 64 * 1024:
                        raise CatalogError(413, "payload_too_large", "请求体超过 64 KiB 上限")
                    body.extend(chunk)
                request._body = bytes(body)
                if request.method == "POST":
                    try:
                        value = strict_loads(bytes(body))
                    except (ValueError, UnicodeError, RecursionError):
                        _invalid()
                    if not isinstance(value, dict) or set(value) != {"schema_version", "request_id"}:
                        _invalid()
                    if type(value["schema_version"]) is not int:
                        _invalid()
                    if value["schema_version"] != 2:
                        _invalid("unsupported_schema")
                    value["request_id"] = _uuid(value["request_id"])
                    request.state.catalog_request_id = value["request_id"]
                    request.state.catalog_body = value
                elif body:
                    _invalid()
                return await handler(request)
            except CatalogError as error:
                return _failure(error, request)
            except RequestValidationError:
                return _failure(CatalogError(422, "invalid_field", "目录请求字段无效"), request)
            except HTTPException as error:
                if error.status_code not in {503, 504}:
                    raise
                return _failure(CatalogError(error.status_code, "upstream_unavailable", "目录读取暂时不可用"), request)
            except SQLAlchemyError:
                return _failure(CatalogError(503, "upstream_unavailable", "目录读取暂时不可用"), request)
            except CanonicalError:
                return _failure(CatalogError(503, "publication_not_ready", "发布目录校验失败"), request)
        return wrapped


router = APIRouter(prefix="/integrations/catalog/v2", tags=["catalog-v2-readonly"],
                   route_class=CatalogRoute)


def _uuid(value):
    if not isinstance(value, str):
        _invalid()
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        _invalid()


def _integer(value, *, minimum=0, maximum=_MAX_INT):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
        _invalid()
    if len(value) > 16:
        _invalid()
    number = int(value)
    if not minimum <= number <= maximum:
        _invalid()
    return number


def _limit(request):
    return _integer(request.query_params.get("limit", "100"), minimum=1, maximum=500)


def _active(db):
    epoch = db.scalar(select(CatalogV2Epoch).where(CatalogV2Epoch.active.is_(True)))
    if epoch is None or epoch.status != "ready":
        raise CatalogError(503, "publication_not_ready", "目录发布尚未就绪")
    return epoch


def _reply(data):
    return Response(canonical_bytes(data), media_type="application/json",
                    headers={"Cache-Control": "no-store"})


def _lock_id(binding):
    return int(canonical_hash(binding)[:16], 16) - 2**63


@contextmanager
def _consistent_session(db, lock=None):
    """Freeze projection and watermark together; optional idempotency lock precedes RR."""
    with db.get_bind().connect() as conn:
        if lock is not None:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock})
            conn.commit()
        try:
            conn.execution_options(isolation_level="REPEATABLE READ")
            with conn.begin(), Session(bind=conn, expire_on_commit=False) as session:
                yield session
                session.flush()
        finally:
            conn.rollback()
            if lock is not None:
                try:
                    conn.execution_options(isolation_level="AUTOCOMMIT")
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock})
                    conn.commit()
                except BaseException:
                    conn.invalidate()
                    raise


def _snapshot_metadata(snapshot):
    return dict(snapshot_id=snapshot.snapshot_id, publication_epoch=snapshot.epoch_id,
                watermark=snapshot.watermark, node_count=snapshot.node_count,
                alias_count=snapshot.alias_count, content_hash=snapshot.content_hash)


def _snapshot_from_projection(db, epoch, client_id, request_id, request_hash):
    rows = db.scalars(select(CatalogV2Projection).where(
        CatalogV2Projection.epoch_id == epoch.id)).all()
    nodes = sorted((row.data for row in rows if row.kind == "node"), key=lambda n: n["part_id"])
    aliases = sorted((row.data for row in rows if row.kind == "alias" and not row.data["deleted"]),
                     key=lambda a: a["alias_id"])
    snapshot = CatalogV2Snapshot(
        client_id=client_id, request_id=request_id, request_hash=request_hash,
        epoch_id=epoch.id, watermark=epoch.watermark, node_count=len(nodes), alias_count=len(aliases),
        content_hash=snapshot_hash(nodes, aliases),
        items=[{"kind": "node", "node": n} for n in nodes] + [{"kind": "alias", "alias": a} for a in aliases],
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


@router.post("/snapshots")
def create_snapshot(request: Request, db: Session = Depends(get_db)):
    body, client = request.state.catalog_body, request.state.catalog_client_id
    digest = canonical_hash(body)
    lock = _lock_id({"kind": "snapshot-request", "client_id": client, "request_id": body["request_id"]})
    with _consistent_session(db, lock) as session:
        epoch = _active(session)
        snapshot = session.scalar(select(CatalogV2Snapshot).where(
            CatalogV2Snapshot.client_id == client, CatalogV2Snapshot.request_id == body["request_id"]))
        if snapshot is not None:
            if snapshot.request_hash != digest:
                raise CatalogError(409, "idempotency_conflict", "同一请求号的内容已变化")
            _check_snapshot(snapshot, epoch)
        else:
            snapshot = _snapshot_from_projection(session, epoch, client, body["request_id"], digest)
        result = dict(envelope(body["request_id"]), **_snapshot_metadata(snapshot),
                      expires_at=utc_text(snapshot.expires_at))
    return _reply(result)


def _check_snapshot(snapshot, epoch):
    if snapshot.expires_at <= datetime.now(timezone.utc):
        raise CatalogError(410, "snapshot_expired", "快照已过期，请使用新请求号获取全量")
    if snapshot.epoch_id != epoch.id:
        _expired("epoch_changed")


def _snapshot(db, snapshot_id, client, epoch):
    row = db.get(CatalogV2Snapshot, snapshot_id)
    if row is None:
        raise CatalogError(410, "snapshot_expired", "快照不存在或已过期")
    if row.client_id != client:
        _invalid("invalid_cursor")
    _check_snapshot(row, epoch)
    return row


def _cursor_key():
    settings = get_settings()
    secret = settings.catalog_v2_cursor_hmac_key.get_secret_value() or settings.secret_key
    return hmac.digest(secret.encode(), b"catalog-v2-opaque-cursor", "sha256")


def _cursor_token(identity):
    signature = hmac.digest(_cursor_key(), identity.encode(), "sha256")
    return identity + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _new_cursor(db, client, context, expires):
    existing = db.scalar(select(CatalogV2Cursor).where(
        CatalogV2Cursor.client_id == client, CatalogV2Cursor.context == context,
        CatalogV2Cursor.expires_at > datetime.now(timezone.utc)).limit(1))
    if existing is None:
        existing = CatalogV2Cursor(client_id=client, context=context, expires_at=expires)
        db.add(existing)
        db.flush()
    return _cursor_token(existing.id)


def _read_cursor(db, token, client, binding):
    if token is None:
        return None
    if not re.fullmatch(r"[0-9a-f-]{36}\.[A-Za-z0-9_-]{43}", token):
        _invalid("invalid_cursor")
    try:
        identity, signature = token.split(".")
        if str(UUID(identity)) != identity:
            _invalid("invalid_cursor")
        if not hmac.compare_digest(_cursor_token(identity), token):
            _invalid("invalid_cursor")
    except (ValueError, binascii.Error):
        _invalid("invalid_cursor")
    row = db.get(CatalogV2Cursor, identity)
    if row is None or row.expires_at <= datetime.now(timezone.utc):
        _expired("retention_expired")
    if row.client_id != client or row.context.get("binding") != binding:
        _invalid("invalid_cursor")
    return row.context


def _fragment_reply(db, request, *, epoch, payload_id, part_no, binding, expires, after_cursor=None):
    fragment = db.get(CatalogV2PayloadFragment, (epoch, payload_id, part_no))
    if fragment is None:
        _expired("retention_expired")
    context = fragment.context
    if "client_id" in context and context["client_id"] != request.state.catalog_client_id:
        _invalid("invalid_cursor")
    if "after_cursor" in context:
        # Replays must continue with the exact logical page persisted inside the
        # payload, including when two initial page requests raced before its lock.
        after_cursor = context["after_cursor"]
    last = fragment.part_no == fragment.part_count
    next_cursor = after_cursor if last else _new_cursor(
        db, request.state.catalog_client_id,
        {"binding": binding, "payload_id": payload_id, "part_no": part_no + 1,
         "after_cursor": after_cursor}, expires,
    )
    metadata = {key: context[key] for key in (
        "snapshot_id", "batch_id", "published_sequence", "previous_sequence") if key in context}
    response = dict(envelope(), publication_epoch=epoch, **metadata,
                    transfer_mode="base64_fragments", payload_id=payload_id,
                    part_no=part_no, part_count=fragment.part_count,
                    part_hash=fragment.part_hash, payload_hash=fragment.payload_hash,
                    payload_fragment=base64.b64encode(fragment.data).decode(),
                    complete=last, next_cursor=next_cursor)
    db.commit()
    return _reply(response)


@router.get("/list")
def list_snapshot(request: Request, db: Session = Depends(get_db)):
    epoch, client, limit = _active(db), request.state.catalog_client_id, _limit(request)
    snapshot_id = _uuid(request.query_params.get("snapshot_id"))
    snapshot = _snapshot(db, snapshot_id, client, epoch)
    binding = {"kind": "list", "epoch": epoch.id, "snapshot_id": snapshot_id, "limit": limit}
    context = _read_cursor(db, request.query_params.get("cursor"), client, binding) or {}
    if "payload_id" in context:
        return _fragment_reply(db, request, epoch=epoch.id, payload_id=context["payload_id"],
                               part_no=context["part_no"], binding=binding, expires=snapshot.expires_at,
                               after_cursor=context.get("after_cursor"))
    position = context.get("position", 0)
    end = min(position + limit, len(snapshot.items))
    complete = end == len(snapshot.items)
    next_cursor = None if complete else _new_cursor(
        db, client, {"binding": binding, "position": end}, snapshot.expires_at)
    logical = dict(envelope(), **_snapshot_metadata(snapshot), items=snapshot.items[position:end],
                   next_cursor=next_cursor, complete=complete)
    payload = canonical_bytes(logical)
    if len(payload) <= response_limit():
        db.commit()
        return _reply(logical)
    page_binding = dict(binding, position=position, client_id=client)
    # The same requested page reuses exact persisted bytes, including its envelope.
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_id(page_binding)})
    fragment = db.scalar(select(CatalogV2PayloadFragment).where(
        CatalogV2PayloadFragment.epoch_id == epoch.id,
        CatalogV2PayloadFragment.part_no == 1,
        CatalogV2PayloadFragment.context.contains({"page_binding": page_binding})))
    if fragment is None:
        payload_id = persist_fragments(db, epoch_id=epoch.id, payload=payload,
                                      context={"snapshot_id": snapshot_id, "client_id": client,
                                               "page_binding": page_binding,
                                               "after_cursor": next_cursor})
    else:
        payload_id = fragment.payload_id
    return _fragment_reply(db, request, epoch=epoch.id, payload_id=payload_id, part_no=1,
                           binding=binding, expires=snapshot.expires_at, after_cursor=next_cursor)


@router.get("/changes")
def changes(request: Request, db: Session = Depends(get_db)):
    epoch, client, limit = _active(db), request.state.catalog_client_id, _limit(request)
    requested_epoch = _uuid(request.query_params.get("publication_epoch"))
    after = _integer(request.query_params.get("after"))
    if requested_epoch != epoch.id:
        _expired("epoch_changed")
    if after < epoch.retained_from_sequence:
        _expired("retention_expired")
    if after > epoch.watermark:
        _expired("watermark_unavailable")
    binding = {"kind": "changes", "epoch": epoch.id, "after": after, "limit": limit}
    context = _read_cursor(db, request.query_params.get("cursor"), client, binding) or {}
    if after == epoch.watermark and not context:
        return _reply(dict(envelope(), publication_epoch=epoch.id, batch_id=None,
                           published_sequence=after, previous_sequence=after,
                           transaction_groups=[], items=[], alias_moves=[], batch_hash=None,
                           complete=True, next_cursor=None))
    batch = db.scalar(select(CatalogV2Batch).where(
        CatalogV2Batch.epoch_id == epoch.id, CatalogV2Batch.published_sequence > after
    ).order_by(CatalogV2Batch.published_sequence).limit(1))
    if batch is None or batch.previous_sequence != after:
        _expired("watermark_unavailable")
    if "payload_id" in context and context["payload_id"] != batch.batch_id:
        _invalid("invalid_cursor")
    fragment = db.get(CatalogV2PayloadFragment, (epoch.id, batch.batch_id, 1))
    if fragment is not None:
        return _fragment_reply(db, request, epoch=epoch.id, payload_id=batch.batch_id,
                               part_no=context.get("part_no", 1), binding=binding,
                               expires=batch.created_at + timedelta(days=30))
    # Small logical batches were persisted in the publication transaction as well.
    return Response(batch.payload, media_type="application/json", headers={"Cache-Control": "no-store"})


@router.get("/parts/{part_id}")
def part_current(part_id: str, request: Request, db: Session = Depends(get_db)):
    from app.services.catalog_v2.publisher import PublicationError, materialize_source
    identity = _integer(part_id, minimum=1)
    with _consistent_session(db) as session:
        epoch = _active(session)
        try:
            nodes, aliases = materialize_source(session, [identity])
        except (PublicationError, CanonicalError):
            raise CatalogError(503, "publication_not_ready", "当前目录身份需先修复")
        if not nodes:
            raise CatalogError(404, "part_not_found", "主数据库身份不存在")
        node = nodes[0]
        projection = session.get(CatalogV2Projection, (epoch.id, "node", identity))
        published = projection is not None and projection.content_hash == node["content_hash"]
        if published:
            old_aliases = session.scalars(select(CatalogV2Projection).where(
                CatalogV2Projection.epoch_id == epoch.id, CatalogV2Projection.kind == "alias",
                CatalogV2Projection.part_id == identity)).all()
            old = sorted((a.data for a in old_aliases if not a.data["deleted"]), key=lambda a: a["alias_id"])
            published = old == aliases
        result = dict(envelope(), publication_epoch=epoch.id, read_basis="source_current",
                      node=node, aliases=aliases,
                      publication_status="published" if published else "pending",
                      published_sequence=projection.published_sequence if published else None)
        if len(canonical_bytes(result)) > response_limit():
            raise CatalogError(413, "aggregate_too_large", "当前完整节点超过读取上限，请使用目录同步分片")
    return _reply(result)


@router.get("/digests")
def digests(request: Request, db: Session = Depends(get_db)):
    client, limit, query = request.state.catalog_client_id, _limit(request), request.query_params
    with _consistent_session(db) as session:
        epoch = _active(session)
        if "snapshot_id" in query:
            if "publication_epoch" in query or "watermark" in query:
                _invalid()
            snapshot = _snapshot(session, _uuid(query["snapshot_id"]), client, epoch)
            selector = {"snapshot_id": snapshot.snapshot_id}
        else:
            requested_epoch = _uuid(query.get("publication_epoch"))
            watermark = _integer(query.get("watermark"))
            if requested_epoch != epoch.id:
                _expired("epoch_changed")
            if watermark < epoch.retained_from_sequence:
                _expired("retention_expired")
            snapshot = session.scalar(select(CatalogV2Snapshot).where(
                CatalogV2Snapshot.client_id == client, CatalogV2Snapshot.epoch_id == epoch.id,
                CatalogV2Snapshot.watermark == watermark,
                CatalogV2Snapshot.expires_at > datetime.now(timezone.utc)).order_by(CatalogV2Snapshot.created_at).limit(1))
            if snapshot is None:
                if watermark != epoch.watermark:
                    _expired("watermark_unavailable")
                snapshot = _snapshot_from_projection(session, epoch, client, str(uuid4()), canonical_hash({}))
            selector = {"watermark": watermark}
        binding = dict(kind="digests", epoch=epoch.id, limit=limit, **selector)
        context = _read_cursor(session, query.get("cursor"), client, binding) or {}
        position = context.get("position", 0)
        nodes = [item["node"] for item in snapshot.items if item["kind"] == "node"]
        aliases = [item["alias"] for item in snapshot.items if item["kind"] == "alias"]
        count = (max((node["part_id"] for node in nodes), default=1) - 1) // 1000 + 1
        end = min(position + limit, count)
        segments = []
        for index in range(position, end):
            start, finish = index * 1000 + 1, (index + 1) * 1000 + 1
            segments.append(dict(start_part_id=start, end_part_id_exclusive=finish,
                                 node_count=sum(start <= n["part_id"] < finish for n in nodes),
                                 alias_count=sum(start <= a["part_id"] < finish for a in aliases),
                                 content_hash=segment_hash(nodes, aliases, start, finish)))
        next_cursor = None if end == count else _new_cursor(
            session, client, {"binding": binding, "position": end}, snapshot.expires_at)
        result = dict(envelope(), publication_epoch=epoch.id, watermark=snapshot.watermark,
                      **({"snapshot_id": snapshot.snapshot_id} if "snapshot_id" in query else {}),
                      segments=segments, complete=end == count, next_cursor=next_cursor)
    return _reply(result)
