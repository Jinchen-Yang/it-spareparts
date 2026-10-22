"""D-36 发布投影：源捕获、完整事务闭包与水位在一个 RR 事务提交。

源表只读；发布写入仅限 catalog_v2_*。会话 advisory lock 在 RR 快照建立前
取得，finally 释放；绝不把 outbox 自增 ID 或时间当成提交顺序。
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.catalog_v2 import (
    CatalogV2Alarm, CatalogV2Batch, CatalogV2Change, CatalogV2Epoch,
    CatalogV2Projection,
)
from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductSpec
from app.services.catalog_v2.canonical import (
    batch_hash, canonical_bytes, canonical_hash, node_hash, normalize,
    normalize_decimal,
)
from app.services.catalog_v2.transport import persist_batch_payload

_LOG = logging.getLogger(__name__)
_LOCK = 0x4341545632  # database scoped, distinct from business/ETL locks
_NODE_FIELDS = (
    "pn_std", "status", "merged_into_id", "needs_review", "is_excluded",
    "machine_or_part", "description", "brand", "category_major", "category_minor",
    "unit", "locked_fields", "aggregate_revision", "identity_revision",
)


class PublicationError(ValueError):
    """Malformed source: stop the publication epoch, never guess a replacement."""


def _graph(db: Session) -> dict[int, tuple[str, int | None]]:
    return {r.id: (r.status, r.merged_into_id) for r in db.execute(
        select(DimPart.id, DimPart.status, DimPart.merged_into_id))}


def _root(graph, part_id):
    seen = set()
    current = part_id
    while current not in seen:
        seen.add(current)
        if current not in graph:
            raise PublicationError("identity_target_missing")
        state, target = graph[current]
        if state == "active" and target is None:
            return current
        if state != "merged" or target is None:
            raise PublicationError("invalid_identity_state")
        current = target
    raise PublicationError("identity_cycle")


def materialize_aliases(db: Session, part_ids=None) -> list[dict]:
    # Historical NULL part_id is resolved only by its exact unique pn_std.
    orphan = db.scalar(select(PartAlias.id).outerjoin(
        DimPart, DimPart.pn_std == PartAlias.pn_std).where(DimPart.id.is_(None)).limit(1))
    if orphan is not None:
        # A legacy alias can lose its textual owner on rename without being
        # deleted. Never turn that missing reference into a deletion tombstone.
        raise PublicationError("alias_owner_missing")
    stmt = select(PartAlias, DimPart.id).join(DimPart, DimPart.pn_std == PartAlias.pn_std)
    if part_ids is not None:
        stmt = stmt.where(DimPart.id.in_(part_ids))
    aliases = []
    for alias, owner in db.execute(stmt):
        if alias.part_id is not None and alias.part_id != owner:
            raise PublicationError("alias_owner_mismatch")
        if alias.status not in (None, "active", "pending", "rejected"):
            raise PublicationError("invalid_alias_state")
        aliases.append({"alias_id": alias.id, "pn_raw": alias.pn_raw,
                        "part_id": owner, "pn_std": alias.pn_std,
                        "status": alias.status, "deleted": False})
    if part_ids is None:
        if len(aliases) != db.scalar(select(func.count()).select_from(PartAlias)):
            raise PublicationError("alias_owner_missing")
    return sorted(aliases, key=lambda row: row["alias_id"])


def materialize_source(db: Session, part_ids=None) -> tuple[list[dict], list[dict]]:
    graph = _graph(db)
    stmt = select(DimPart).order_by(DimPart.id)
    if part_ids is not None:
        stmt = stmt.where(DimPart.id.in_(part_ids))
    parts = db.scalars(stmt).all()
    ids = [p.id for p in parts]
    specs = defaultdict(list)
    for spec in db.scalars(select(ProductSpec).where(ProductSpec.part_id.in_(ids))):
        if spec.source not in ("auto", "manual"):
            raise PublicationError("invalid_spec_source")
        specs[spec.part_id].append({
            "key": spec.spec_key, "value": spec.spec_value, "unit": spec.spec_unit,
            "numeric_value": (normalize_decimal(spec.numeric_value)
                              if spec.numeric_value is not None else None),
            "source": spec.source,
        })
    nodes = []
    for p in parts:
        node = {field: getattr(p, field) for field in _NODE_FIELDS}
        node.update(part_id=p.id, canonical_part_id=_root(graph, p.id), specs=specs[p.id])
        node = normalize(node)
        node["content_hash"] = node_hash(node)
        nodes.append(node)
    return nodes, materialize_aliases(db, part_ids)


def materialize_node(db: Session, part_id: int) -> dict | None:
    nodes, _ = materialize_source(db, [part_id])
    return nodes[0] if nodes else None


@contextmanager
def publication_transaction(engine: Engine):
    """Session lock first; acquiring it in RR would freeze a stale waiting snapshot."""
    with engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _LOCK})
        conn.commit()
        try:
            conn.execution_options(isolation_level="REPEATABLE READ")
            with conn.begin():
                with Session(bind=conn, expire_on_commit=False) as db:
                    yield db
                    db.flush()
        finally:
            conn.rollback()
            try:
                conn.execution_options(isolation_level="AUTOCOMMIT")
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK})
                conn.commit()
            except BaseException:
                conn.invalidate()  # never return a session holding the lock to the pool
                raise


def _active(db):
    return db.scalar(select(CatalogV2Epoch).where(CatalogV2Epoch.active.is_(True)))


def _project(db, epoch, items, sequence):
    for item in items:
        kind = item["kind"]
        data = item[kind]
        entity_id = data["part_id" if kind == "node" else "alias_id"]
        row = db.get(CatalogV2Projection, (epoch.id, kind, entity_id))
        if row is None:
            row = CatalogV2Projection(epoch_id=epoch.id, kind=kind, entity_id=entity_id)
            db.add(row)
        row.part_id = data["part_id"]
        row.data = data
        row.content_hash = data["content_hash"] if kind == "node" else canonical_hash(data)
        row.published_sequence = sequence


def seed(engine: Engine) -> str:
    """Explicit full rebuild; the migration must have enabled capture beforehand.

    All *visible* log rows belong to this baseline, even if an older transaction is
    still in flight. Its later commit remains unmarked and gets a subsequent batch.
    """
    with publication_transaction(engine) as db:
        nodes, aliases = materialize_source(db)
        db.execute(update(CatalogV2Epoch).where(CatalogV2Epoch.active.is_(True))
                   .values(active=False))
        epoch = CatalogV2Epoch(id=str(uuid4()), status="seeding", watermark=0,
                              active=True, retained_from_sequence=0)
        db.add(epoch)
        db.flush()
        items = ([{"kind": "node", "node": n} for n in nodes]
                 + [{"kind": "alias", "alias": a} for a in aliases])
        _project(db, epoch, items, 0)
        # Rebuild is the explicit operator recovery for earlier DELETE alarms.
        db.execute(update(CatalogV2Change).where(CatalogV2Change.published_epoch.is_(None))
                   .values(published_epoch=epoch.id, published_sequence=0))
        epoch.status = "ready"
        epoch.ready_at = datetime.now(timezone.utc)
        return epoch.id


def _parents(change) -> set[int]:
    return {x for x in (change.part_id, change.old_part_id, change.new_part_id) if x is not None}


def _closure(changes, eligible, graph, previous_nodes):
    groups = set(eligible)
    ids: set[int] = set()
    while True:
        old = (len(groups), len(ids))
        for c in changes:
            if c.txid in groups or ids.intersection(_parents(c)):
                groups.add(c.txid)
                ids.update(_parents(c))
        # Both directions include descendants whose canonical root may change.
        for pid, (_, target) in graph.items():
            if pid in ids and target is not None:
                ids.add(target)
            if target in ids:
                ids.add(pid)
        # Preserve the other end of an identity edge removed since last publication.
        for pid, node in previous_nodes.items():
            target = node["merged_into_id"]
            if pid in ids and target is not None:
                ids.add(target)
            if target in ids:
                ids.add(pid)
        if old == (len(groups), len(ids)):
            return groups, ids


def _stop(db, epoch, reason, details=None):
    epoch.status = "stopped"
    db.add(CatalogV2Alarm(epoch_id=epoch.id, code=reason, details=details or {}))
    _LOG.error("catalog-v2 publication stopped: %s epoch=%s", reason, epoch.id)


def publish_once(engine: Engine) -> dict | None:
    """Publish one complete fixed point, or retain the watermark on idle/unsafe work."""
    with publication_transaction(engine) as db:
        epoch = _active(db)
        if epoch is None or epoch.status != "ready":
            return None
        xmin = db.scalar(text("SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint"))
        changes = db.scalars(select(CatalogV2Change).where(
            CatalogV2Change.published_epoch.is_(None)).order_by(CatalogV2Change.id)).all()
        eligible = {c.txid for c in changes if c.txid < xmin}
        if not eligible:
            return None
        projections = db.scalars(select(CatalogV2Projection).where(
            CatalogV2Projection.epoch_id == epoch.id)).all()
        previous_nodes = {p.entity_id: p.data for p in projections if p.kind == "node"}
        previous_aliases = {p.entity_id: p.data for p in projections if p.kind == "alias"}
        groups, ids = _closure(changes, eligible, _graph(db), previous_nodes)
        selected = [c for c in changes if c.txid in groups]
        deletes = [c for c in selected if c.table_name == "dim_part" and c.op == "D"]
        if deletes:
            _stop(db, epoch, "node_deleted", {"part_ids": sorted({c.part_id for c in deletes})})
            return None
        # Visible newer groups can have changed an eligible group's aggregate. Do
        # not materialize that newer state unless its entire group is safe too.
        if not groups.issubset(eligible):
            return None
        try:
            if any(c.table_name == "part_alias" and (
                (c.op != "I" and c.old_part_id is None)
                or (c.op != "D" and c.new_part_id is None)
            ) for c in selected):
                raise PublicationError("alias_owner_missing")
            nodes, aliases = materialize_source(db, ids)
            if {n["part_id"] for n in nodes} != ids:
                raise PublicationError("identity_target_missing")
            current_aliases = {a["alias_id"]: a for a in aliases}
            tombstones = {}
            for c in selected:
                if c.table_name == "part_alias" and c.op == "D":
                    old = c.old_row
                    tombstones[c.alias_id] = {
                        "alias_id": c.alias_id, "pn_raw": old["pn_raw"],
                        "part_id": c.old_part_id, "pn_std": old["pn_std"],
                        "status": old["status"], "deleted": True,
                    }
            for aid, old in previous_aliases.items():
                if old["part_id"] in ids and not old["deleted"] and aid not in current_aliases:
                    tombstones.setdefault(aid, {**old, "deleted": True})
            for aid, old in tombstones.items():
                if aid not in current_aliases:
                    aliases.append(old)
            aliases.sort(key=lambda a: a["alias_id"])
            changed = (any(previous_nodes.get(n["part_id"]) != n for n in nodes)
                       or any(previous_aliases.get(a["alias_id"]) != a for a in aliases))
            if not changed:
                for c in selected:
                    c.published_epoch, c.published_sequence = epoch.id, epoch.watermark
                return None
            moves = []
            alias_before = dict(previous_aliases)
            for c in selected:
                if c.table_name == "part_alias" and c.old_row is not None:
                    alias_before.setdefault(c.alias_id, {"part_id": c.old_part_id})
            for a in aliases:
                old = alias_before.get(a["alias_id"])
                if old is not None and old["part_id"] != a["part_id"]:
                    moves.append({"alias_id": a["alias_id"], "old_part_id": old["part_id"],
                                  "new_part_id": a["part_id"]})
            batch = normalize({
                "schema_version": 2, "source_namespace": "itsp", "publication_epoch": epoch.id,
                "batch_id": str(uuid4()), "published_sequence": epoch.watermark + 1,
                "previous_sequence": epoch.watermark, "transaction_groups": [str(g) for g in groups],
                "items": ([{"kind": "node", "node": n} for n in nodes]
                          + [{"kind": "alias", "alias": a} for a in aliases]),
                "alias_moves": moves,
            })
            batch["batch_hash"] = batch_hash(batch)
            payload = canonical_bytes(batch)
        except (PublicationError, ValueError) as exc:
            _stop(db, epoch, "invalid_source", {"reason": str(exc) if isinstance(exc, PublicationError)
                                              else "invalid_canonical_value"})
            return None
        stored_batch = CatalogV2Batch(
            epoch_id=epoch.id, published_sequence=batch["published_sequence"],
            previous_sequence=batch["previous_sequence"], batch_id=batch["batch_id"],
            transaction_groups=batch["transaction_groups"], items=batch["items"],
            alias_moves=moves, batch_hash=batch["batch_hash"], payload=payload,
        )
        db.add(stored_batch)
        db.flush()
        persist_batch_payload(db, stored_batch)
        _project(db, epoch, batch["items"], batch["published_sequence"])
        epoch.watermark = batch["published_sequence"]
        for c in selected:
            c.published_epoch, c.published_sequence = epoch.id, epoch.watermark
        return batch


def status(engine: Engine) -> dict:
    with Session(engine) as db:
        epoch = _active(db)
        return {
            "publication_epoch": epoch.id if epoch else None,
            "status": epoch.status if epoch else "unseeded",
            "watermark": epoch.watermark if epoch else 0,
            "pending_changes": db.scalar(select(func.count()).select_from(CatalogV2Change)
                                          .where(CatalogV2Change.published_epoch.is_(None))),
        }


def cleanup(engine: Engine, *, retain_days: int = 30) -> int:
    """Explicit operator cleanup only. Never remove source captures or projections."""
    if retain_days < 30:
        raise ValueError("Published history must be retained for at least 30 days")
    cutoff = datetime.now(timezone.utc) - timedelta(days=retain_days)
    with publication_transaction(engine) as db:
        removed = 0
        for epoch in db.scalars(select(CatalogV2Epoch)):
            rows = db.scalars(select(CatalogV2Batch).where(
                CatalogV2Batch.epoch_id == epoch.id).order_by(CatalogV2Batch.published_sequence)).all()
            # Only prune a prefix, retaining gap detection even under clock skew.
            for batch in rows:
                if batch.created_at >= cutoff:
                    break
                epoch.retained_from_sequence = batch.published_sequence
                db.delete(batch)
                removed += 1
        return removed


async def publisher_loop(stop: asyncio.Event, engine: Engine, interval: float = 30):
    while not stop.is_set():
        try:
            await asyncio.to_thread(publish_once, engine)
        except Exception:
            _LOG.exception("catalog-v2 publisher iteration failed; watermark transaction rolled back")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
