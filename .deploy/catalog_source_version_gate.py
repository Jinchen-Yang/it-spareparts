#!/usr/bin/env python3
"""D-0049 SELECT-only metadata gate; callers own identity, freeze and SQL settings.

No database driver, credentials, shell execution, file writes or business payloads
belong here. SQL callbacks must return all decoded JSON rows, not a truncated page.
The two guard functions intentionally match the reviewed shop guard semantics;
the release gate additionally rejects malformed metadata instead of ignoring it.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import time
from types import SimpleNamespace
from uuid import UUID

UTC = timezone.utc
NAMESPACE = "itsp"
MAX_ROWS = 250000

# Fixed six-digit UTC precision matches the source API's datetime.isoformat().
SOURCE_SQL = '''SELECT json_build_object('id', id, 'version',
    to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00')
  FROM dim_part ORDER BY id LIMIT 250001'''

# A single statement gives link and snapshot metadata the same PG snapshot.
# Do not select pn text, price, descriptions, or the complete snapshot JSON.
MIRROR_SQL = '''SELECT json_build_object('kind', 'link', 'part_id', part_id,
    'pn_id', pn_id, 'version',
    to_char(upstream_updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00')
  FROM catalog_part_link WHERE source_namespace = 'itsp'
UNION ALL
SELECT json_build_object('kind', 'snapshot', 'pn_id', id,
    'part_id', itsp_snapshot->'part_id',
    'upstream_updated_at', itsp_snapshot->'upstream_updated_at',
    'pn_master_version', itsp_snapshot->'pn_master_version',
    'exact_identity', coalesce((itsp_snapshot->>'status') = 'exact', false),
    'snapshot_type', jsonb_typeof(itsp_snapshot),
    'status_type', jsonb_typeof(itsp_snapshot->'status'))
  FROM pn LIMIT 250001'''


def need(condition, message):
    if not condition:
        raise RuntimeError("source-version gate: " + message)


def parse_source_version(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(UTC) if stamp.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def older_than_mirror(part_id, incoming_at, *, link=None, snapshots=()):
    """Compare only the same stable itsp part_id, as the actual shop guard does."""
    versions = []
    if link is not None and link.source_namespace == NAMESPACE and link.part_id == part_id:
        versions.append(link.upstream_updated_at)
    for snapshot in snapshots:
        if snapshot and snapshot.get("part_id") == part_id:
            versions.extend(
                parse_source_version(snapshot.get(key))
                for key in ("upstream_updated_at", "pn_master_version")
            )
    known = [version for version in versions if version is not None]
    return bool(known) and (incoming_at is None or any(version > incoming_at for version in known))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _rows(value, label):
    need(isinstance(value, list), label + " callback must return a complete list")
    need(0 < len(value) <= MAX_ROWS, label + " row bound exceeded or empty result")
    need(all(isinstance(row, dict) for row in value), label + " row shape invalid")


def _part(value):
    return type(value) is int and value > 0


def _pn(value, *, nullable=False):
    if value is None and nullable:
        return None
    need(isinstance(value, str), "PN identity must be a UUID string")
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise RuntimeError("source-version gate: PN identity invalid") from None
    need(canonical == value, "PN identity not canonical")
    return value


def _version(value, label, *, nullable=True):
    if value is None:
        need(nullable, label + " version missing")
        return None
    parsed = parse_source_version(value)
    need(parsed is not None, label + " non-null version unparseable")
    return parsed


def _source(source_rows):
    _rows(source_rows, "source")
    result = {}
    for row in source_rows:
        need(set(row) == {"id", "version"}, "source projection changed")
        part_id = row["id"]
        need(_part(part_id), "source identity invalid")
        need(part_id not in result, "duplicate source identity")
        result[part_id] = _version(row["version"], "source", nullable=False)
    return result


def _source_digest(source_rows):
    _source(source_rows)
    return digest(sorted(source_rows, key=lambda row: row["id"]))


def validate_versions(source_rows, mirror_rows):
    """Validate a complete finite technical projection; never expose raw rows.

    A non-exact snapshot without part_id is not mapped by guessing from its PN
    or link. Parseable historical versions on that snapshot remain ignored by
    the guard. Its link is independently compared using the link's stable ID.
    """
    source = _source(source_rows)
    _rows(mirror_rows, "mirror")
    counts = Counter()
    links, linked_pns, snapshots = {}, {}, {}
    compared_parts = set()
    link_fields = {"kind", "part_id", "pn_id", "version"}
    snapshot_fields = {"kind", "pn_id", "part_id", "upstream_updated_at",
                       "pn_master_version", "exact_identity", "snapshot_type", "status_type"}
    for row in mirror_rows:
        kind = row.get("kind")
        need(kind in {"link", "snapshot"}, "mirror record kind invalid")
        counts[kind + "_records"] += 1
        if kind == "link":
            need(set(row) == link_fields, "link projection changed")
            part_id, pn_id = row["part_id"], _pn(row["pn_id"], nullable=True)
            need(_part(part_id), "link identity invalid")
            need(part_id in source, "link source identity absent")
            need(part_id not in links, "duplicate link source identity")
            need(pn_id is None or pn_id not in linked_pns, "conflicting link PN identity")
            _version(row["version"], "link", nullable=False)
            links[part_id] = row
            if pn_id is not None:
                linked_pns[pn_id] = part_id
            else:
                counts["link_unbound_pn"] += 1
        else:
            need(set(row) == snapshot_fields, "snapshot projection changed")
            pn_id = _pn(row["pn_id"])
            need(pn_id not in snapshots, "duplicate snapshot PN identity")
            need(type(row["exact_identity"]) is bool, "snapshot exact flag invalid")
            need(row["snapshot_type"] in {None, "null", "object"}, "snapshot JSON type invalid")
            need(row["status_type"] in {None, "null", "string"}, "snapshot status type invalid")
            if row["snapshot_type"] != "object":
                need(row["status_type"] is None and not row["exact_identity"]
                     and all(row[key] is None for key in
                             ("part_id", "upstream_updated_at", "pn_master_version")),
                     "missing snapshot has inconsistent metadata")
            if row["exact_identity"]:
                need(row["status_type"] == "string", "exact snapshot status invalid")
            for key in ("upstream_updated_at", "pn_master_version"):
                _version(row[key], "snapshot " + key)
            part_id = row["part_id"]
            if part_id is None:
                need(not row["exact_identity"], "exact snapshot lacks stable identity")
                counts["snapshot_unmapped_identity"] += 1
                counts["snapshot_unmapped_versions_ignored"] += sum(
                    row[key] is not None for key in ("upstream_updated_at", "pn_master_version"))
            else:
                need(_part(part_id), "snapshot identity invalid")
                need(part_id in source, "snapshot source identity absent")
            snapshots[pn_id] = row

    need(links and snapshots, "expected link or PN inventory missing")
    for pn_id, part_id in linked_pns.items():
        need(pn_id in snapshots, "linked PN identity absent")
        snapshot_id = snapshots[pn_id]["part_id"]
        need(snapshot_id is None or snapshot_id == part_id, "link and snapshot identities disagree")

    def compare(kind, part_id, fields, *, link=None, snapshot=None):
        source_at = source[part_id]
        need(not older_than_mirror(part_id, source_at, link=link,
                                   snapshots=() if snapshot is None else (snapshot,)),
             "mirror version ahead of source")
        counts[kind + "_compared_records"] += 1
        compared_parts.add(part_id)
        for field, value in fields:
            version = parse_source_version(value)
            if version is None:
                counts[field + ".missing"] += 1
            else:
                counts[field + ".compared"] += 1
                counts[field + (".equal" if version == source_at else ".behind")] += 1

    for part_id, row in links.items():
        link = SimpleNamespace(source_namespace=NAMESPACE, part_id=part_id,
                               upstream_updated_at=parse_source_version(row["version"]))
        compare("link", part_id, [("catalog_part_link.upstream_updated_at", row["version"])], link=link)
    for row in snapshots.values():
        if row["part_id"] is not None:
            compare("snapshot", row["part_id"],
                    [("pn.itsp_snapshot." + key, row[key])
                     for key in ("upstream_updated_at", "pn_master_version")], snapshot=row)

    canonical_mirror = sorted(mirror_rows, key=lambda row: (
        row["kind"], row["pn_id"] or "", row["part_id"] or 0))
    return {"source_rows": len(source), "mirror_rows": len(mirror_rows),
            "compared_distinct_part_ids": len(compared_parts),
            "counts": dict(sorted(counts.items())), "ahead_version_fields": 0,
            "source_sha256": _source_digest(source_rows),
            "mirror_sha256": digest(canonical_mirror)}


def collect(source_sql, mirror_sql, *, assert_frozen, pause_seconds=5):
    """Two bounded stable rounds under the caller's exact freeze assertion.

    The callback must prove both systems' frozen identities and zero TCP DB
    clients on every invocation. No earlier production sample is reused.
    """
    need(isinstance(pause_seconds, (int, float)) and not isinstance(pause_seconds, bool)
         and 0 <= pause_seconds <= 30, "invalid pause interval")
    assert_frozen()
    rounds = []
    for index in range(2):
        started = datetime.now(UTC).isoformat()
        before = source_sql(SOURCE_SQL)
        before_digest = _source_digest(before)
        mirror = mirror_sql(MIRROR_SQL)
        after = source_sql(SOURCE_SQL)
        result = validate_versions(after, mirror)
        need(result["source_sha256"] == before_digest, "source changed within collection round")
        rounds.append({"started_at_utc": started,
                       "finished_at_utc": datetime.now(UTC).isoformat(), **result})
        if index == 0:
            time.sleep(pause_seconds)
            assert_frozen()
    assert_frozen()
    need(all(rounds[0][key] == rounds[1][key] for key in ("source_sha256", "mirror_sha256")),
         "source or mirror changed between collection rounds")
    return {"status": "PASS", "read_only": True, "rounds": rounds,
            "source_sha256": rounds[1]["source_sha256"], "mirror_sha256": rounds[1]["mirror_sha256"],
            "pause_between_rounds_seconds": pause_seconds,
            "guard_semantics": "same stable part_id; known mirror strictly newer rejects incoming",
            "strict_parse_and_identity_checks": True, "business_body_exported": False}
