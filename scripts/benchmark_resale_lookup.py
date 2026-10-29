#!/usr/bin/env python3
"""Compare unchanged PN resolution on a fresh, synthetic local PostgreSQL database.

Run on jinchen-dev (or CI), from backend with its installed dependencies:
  BENCHMARK_DATABASE_BASE_URL=<dedicated local spareparts_test URL> \
    uv run python ../scripts/benchmark_resale_lookup.py --output <report.json>

The database is newly created and ownership-checked by tests.run_isolation;
existing databases are never seeded or migrated. No HTTP or shop requests run.
The measured scope is the resolve handler plus SQL, excluding network/auth.
The legacy handler below is frozen from baseline 592b12f; its single-item
lookup/merge helpers remain unchanged in the application.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
from datetime import datetime, timezone

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
BASELINE_REVISION = "d6e8f0a2b4c6"
INDEX_NAME = "ix_alias_pn_raw_upper"


def summary(values: list[float | int]) -> dict:
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 4),
        "p95": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 4),
        "max": round(ordered[-1], 4),
    }


def seed(engine, parts: int, aliases: int) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO dim_part (pn_std)
            SELECT 'BENCH-PN-' || lpad(i::text, 6, '0')
            FROM generate_series(1, :parts) AS i
        """), {"parts": parts})
        conn.execute(text("""
            INSERT INTO part_alias (pn_raw, pn_std, part_id, status)
            SELECT CASE WHEN i <= least(:parts, :aliases / 2) THEN lower(p.pn_std)
                        ELSE 'bench-alias-' || lpad(i::text, 6, '0') END,
                   p.pn_std, p.id,
                   CASE WHEN i % 10 = 8 THEN 'pending'
                        WHEN i % 10 = 9 THEN 'rejected' ELSE 'active' END
            FROM generate_series(1, :aliases) AS i
            JOIN dim_part p ON p.id = ((i - 1) % :parts) + 1
        """), {"parts": parts, "aliases": aliases})
        conn.execute(text("ANALYZE dim_part"))
        conn.execute(text("ANALYZE part_alias"))


def explain(engine, raws: list[str] | None = None) -> dict:
    from sqlalchemy import func, select

    from app.models.dimensions import PartAlias

    # Exactly the alias SELECT from find_pn_candidates, including projected
    # columns, upper(raw), and both allowed states. Do not force planner flags.
    upper = func.upper(PartAlias.pn_raw)
    query = (
        select(PartAlias).where(upper == "bench-pn-000001".upper())
        if raws is None else
        select(PartAlias, upper).where(upper.in_({raw.upper() for raw in raws}))
    ).where(PartAlias.status.in_(("active", "pending")))
    compiled = query.compile(engine, compile_kwargs={"render_postcompile": True})
    with engine.connect() as conn:
        plan = conn.exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + str(compiled),
            compiled.params,
        ).scalar_one()[0]
    return {"sql_template": str(compiled), "plan": plan}


def workloads(parts: int, aliases: int) -> dict[str, list[str]]:
    # Mix exact + alias, alias-only, and missing inputs deterministically.
    distinct = []
    for i in range(1, 101):
        if i % 3 == 1:
            distinct.append(f"bench-pn-{i:06d}")
        elif i % 3 == 2:
            distinct.append(f"bench-alias-{min(parts, aliases // 2) + i:06d}")
        else:
            distinct.append(f"bench-missing-{i:06d}")
    return {
        "1_distinct": distinct[:1],
        "10_distinct": distinct[:10],
        "100_distinct": distinct,
        "100_repeated": distinct[:1] * 100,
    }


def legacy_resolve_parts(body, response, _key, db) -> dict:
    """Frozen resolve algorithm from 592b12f, for a same-database comparison."""
    from app.services.resale_pn_lookup import canonical_part, find_pn_candidates

    response.headers["Cache-Control"] = "no-store"
    items = []
    cache = {}
    for candidate in body.items:
        raw = candidate.pnRaw.strip()
        if raw not in cache:
            matches, pending = find_pn_candidates(db, raw)
            bound = list(matches.values())
            if pending or len(bound) > 1:
                result = {
                    "status": "ambiguous",
                    "candidates": [{"partId": row.id, "pnStd": row.pn_std} for row, _ in bound],
                }
            elif not bound:
                result = {"status": "not_found", "candidates": []}
            else:
                original, kind = bound[0]
                part, merged = canonical_part(db, original)
                if part is None:
                    result = {"status": "ambiguous", "candidates": []}
                else:
                    result = {
                        "status": "merged" if merged else kind,
                        "partId": part.id, "pnStd": part.pn_std,
                        "redirectedFrom": original.id if merged else None,
                        "candidates": [],
                    }
            cache[raw] = result
        items.append({"sourceKey": candidate.sourceKey, **cache[raw]})
    return {"schemaVersion": 1, "items": items}


def measure(engine, batches: dict, warmups: int, repetitions: int, *, legacy: bool) -> tuple[dict, dict]:
    from fastapi import Response
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from app.api.resale_catalog_export import ResolveRequest, resolve_parts

    resolver = legacy_resolve_parts if legacy else resolve_parts
    results, outputs = {}, {}
    for name, raws in batches.items():
        request = ResolveRequest(items=[
            {"sourceKey": str(i), "pnRaw": raw} for i, raw in enumerate(raws)
        ])
        elapsed, counts = [], []
        fingerprint = None
        for iteration in range(warmups + repetitions):
            count = 0

            def count_sql(*_args):
                nonlocal count
                count += 1

            event.listen(engine, "before_cursor_execute", count_sql)
            try:
                with Session(engine) as db:
                    started = time.perf_counter()
                    result = resolver(request, Response(), None, db)
                    duration_ms = (time.perf_counter() - started) * 1000
            finally:
                event.remove(engine, "before_cursor_execute", count_sql)
            digest = hashlib.sha256(
                json.dumps(result, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            if fingerprint is not None and digest != fingerprint:
                raise RuntimeError("resolution changed between repetitions")
            fingerprint = digest
            if iteration >= warmups:
                elapsed.append(duration_ms)
                counts.append(count)
        results[name] = {
            "items": len(raws), "distinct_inputs": len(set(raws)),
            "latency_ms": summary(elapsed), "sql_count": summary(counts),
            "samples_ms": [round(value, 4) for value in elapsed],
            "result_sha256": fingerprint,
        }
        outputs[name] = result
        print(f"Completed {name}: p95={results[name]['latency_ms']['p95']} ms", flush=True)
    return results, outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--candidate", choices=["batch", "index"], default="batch")
    parser.add_argument("--parts", type=int, default=30000)
    parser.add_argument("--aliases", type=int, default=100000)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--repetitions", type=int, default=20)
    args = parser.parse_args()
    if not 100 <= args.parts <= 100000 or not 200 <= args.aliases <= 300000:
        parser.error("require 100..100000 parts and 200..300000 aliases")
    if not 0 <= args.warmups <= 10 or not 1 <= args.repetitions <= 50:
        parser.error("require 0..10 warmups and 1..50 repetitions")
    base_url = os.environ.get("BENCHMARK_DATABASE_BASE_URL")
    if not base_url:
        parser.error("BENCHMARK_DATABASE_BASE_URL must name a dedicated local test server")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from tests.run_isolation import cleanup_database_run, create_database_run

    owned = create_database_run(base_url)
    engine = None
    try:
        os.environ["DATABASE_URL"] = owned.database_url
        engine = create_engine(owned.database_url)
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        command.upgrade(cfg, BASELINE_REVISION)
        seed(engine, args.parts, args.aliases)
        report = {
            "measured_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": "synthetic_only", "scope": "resolve_handler_and_sql_no_http_auth",
            "baseline_revision": BASELINE_REVISION,
            "candidate": args.candidate,
            "baseline_code_sha": "592b12f17b2bfea3b8ba7cd5a386ab9f1650295b",
            "parts": args.parts, "aliases": args.aliases,
            "warmups_per_workload": args.warmups, "serial_repetitions": args.repetitions,
            "percentile_method": "p50 median; p95 nearest rank",
            "before_explain": explain(engine),
        }
        plan = report["before_explain"]["plan"]["Plan"]
        # The benchmark is intentionally fail-closed if the baseline hypothesis
        # no longer holds (for example an upstream migration added an index).
        if plan["Node Type"] != "Seq Scan" or plan.get("Rows Removed by Filter", 0) < 100:
            raise RuntimeError("baseline does not confirm an alias-table sequential scan")
        print("Baseline EXPLAIN confirms alias-table sequential scan", flush=True)
        batches = workloads(args.parts, args.aliases)
        report["before"], expected = measure(
            engine, batches, args.warmups, args.repetitions, legacy=True,
        )
        if not args.baseline_only:
            if args.candidate == "index":
                with engine.begin() as conn:
                    conn.execute(text(
                        f"CREATE INDEX {INDEX_NAME} ON part_alias USING btree (upper(pn_raw))"
                    ))
                    conn.execute(text("ANALYZE part_alias"))
                    report["index_bytes"] = conn.scalar(
                        text("SELECT pg_relation_size(:name)"), {"name": INDEX_NAME}
                    )
            report["after_explain"] = explain(
                engine, batches["100_distinct"] if args.candidate == "batch" else None,
            )
            report["after"], actual = measure(
                engine, batches, args.warmups, args.repetitions,
                legacy=args.candidate == "index",
            )
            report["output_equivalent"] = actual == expected
            if not report["output_equivalent"]:
                raise RuntimeError("before/after resolution outputs differ")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"Synthetic report: {args.output}", flush=True)
    finally:
        if engine is not None:
            engine.dispose()
        app_db = sys.modules.get("app.db")
        if app_db is not None:
            app_db.engine.dispose()
        cleanup_database_run(owned)


if __name__ == "__main__":
    main()
