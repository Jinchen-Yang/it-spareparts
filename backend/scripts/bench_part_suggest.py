"""在独占临时测试库中生成 3 万型号，测量 M1 的完整服务耗时及真实执行计划。"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
from statistics import median
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tests.run_isolation import cleanup_database_run, create_database_run


_BASELINE_REVISION = "c4e6a8b1d3f5"
_R5_REVISION = "d6f8a0b2c4e6"
_SHORT_CASES = ("短词S", "短词M", "短词硬盘", "短词内存", "短词HP", "短词1T")


def _percentiles(samples: list[float]) -> dict:
    values = sorted(samples)
    return {"p50_ms": round(values[math.ceil(len(values) * .50) - 1], 3),
            "p95_ms": round(values[math.ceil(len(values) * .95) - 1], 3)}


def _load_search(source: Path | None):
    if source:
        spec = importlib.util.spec_from_file_location("part_search_baseline", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.search
    from app.services.part_search import search
    return search


def _migrate(cfg, revision: str, *, downgrade: bool = False) -> dict:
    from alembic import command

    statements = []

    def migration_start(_conn, _cursor, _statement, _parameters, context, _many):
        context.bench_started = perf_counter()

    def migration_end(_conn, _cursor, statement, _parameters, context, _many):
        statements.append({"sql": statement.strip(),
                           "elapsed_ms": round((perf_counter() - context.bench_started) * 1000, 3)})

    event.listen(Engine, "before_cursor_execute", migration_start)
    event.listen(Engine, "after_cursor_execute", migration_end)
    started = perf_counter()
    try:
        (command.downgrade if downgrade else command.upgrade)(cfg, revision)
    finally:
        elapsed_ms = round((perf_counter() - started) * 1000, 3)
        event.remove(Engine, "before_cursor_execute", migration_start)
        event.remove(Engine, "after_cursor_execute", migration_end)
    return {"direction": "downgrade" if downgrade else "upgrade", "revision": revision,
            "elapsed_ms": elapsed_ms, "statements": statements,
            "backfill_ms": round(sum(s["elapsed_ms"] for s in statements
                                     if " ".join(s["sql"].split()).upper().startswith((
                                         "UPDATE PART_SEARCH_TEXT SET SHORT_KEYS",
                                         "INSERT INTO PART_SEARCH_SHORT_KEY"))), 3)}


def _vacuum(engine) -> None:
    # 两种结构使用同样的常规维护，不做 VACUUM FULL 或强制查询计划。
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        tables = ["dim_part", "part_search_text", "part_alias", "product_specs"]
        if conn.execute(text("SELECT to_regclass('part_search_short_key')")).scalar():
            tables.append("part_search_short_key")
        for table in tables:
            conn.execute(text("VACUUM (ANALYZE) " + table))


def _storage_snapshot(engine) -> dict:
    with engine.connect() as conn:
        storage = dict(conn.execute(text("""
            SELECT pg_relation_size('part_search_text') AS heap_bytes,
                   pg_relation_size('part_search_text') / 8192 AS heap_pages,
                   pg_relation_filenode('part_search_text') AS relfilenode,
                   current_setting('block_size')::integer AS block_size,
                   (SELECT version_num FROM alembic_version) AS revision
        """)).mappings().one())
        storage["columns"] = [dict(row) for row in conn.execute(text("""
            SELECT a.attnum AS position, a.attname AS name,
                   format_type(a.atttypid, a.atttypmod) AS type,
                   a.attnotnull AS not_null,
                   pg_get_expr(d.adbin, d.adrelid) AS default_expression
            FROM pg_attribute a LEFT JOIN pg_attrdef d
              ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = 'part_search_text'::regclass
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
        """)).mappings()]
        storage["documents"] = dict(conn.execute(text("""
            SELECT count(*) AS rows,
                   md5(string_agg(part_id::text || ':' || doc || ':' || doc_compact
                                  || ':' || updated_at::text, E'\\n' ORDER BY part_id)) AS checksum
            FROM part_search_text
        """)).mappings().one())
        storage["short_key_table"] = None
        if conn.execute(text("SELECT to_regclass('part_search_short_key')")).scalar():
            storage["short_key_table"] = dict(conn.execute(text("""
                SELECT count(*) AS rows,
                       pg_relation_size('part_search_short_key') / 8192 AS heap_pages
                FROM part_search_short_key
            """)).mappings().one())
        storage["indexes"] = [dict(row) for row in conn.execute(text("""
            SELECT tablename, indexname, indexdef FROM pg_indexes
            WHERE schemaname = 'public' AND tablename IN (
                'part_search_text', 'part_search_short_key', 'dim_part', 'part_alias')
            ORDER BY tablename, indexname
        """)).mappings()]
    return storage


def _write_report(report: dict, output: Path | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"), required=not os.environ.get("DATABASE_URL"))
    parser.add_argument("--rows", type=int, default=30000)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--revision", default="head", help="造数后升级到此 revision；用 c4e6a8b1d3f5 测原服务")
    parser.add_argument("--target-p95-ms", type=float, default=70)
    parser.add_argument("--output", type=Path, help="另存完整 JSON（即使超出目标也保留结果）")
    parser.add_argument("--baseline-service-source", type=Path, help="从已保存的原服务源码加载 search，供同数据前后对比")
    parser.add_argument("--compare-r5", action="store_true",
                        help="同一临时库交替测基线/r5 三对，各30次，按三个p95的中位数验收")
    args = parser.parse_args()
    if args.rows < 12346 or args.runs < 1:
        parser.error("rows 至少 12346，runs 至少 1")
    if args.target_p95_ms <= 0:
        parser.error("target-p95-ms 必须大于 0")
    if args.baseline_service_source and not args.baseline_service_source.is_file():
        parser.error("baseline-service-source 必须是已保存的基线服务源码")
    if args.compare_r5:
        if args.baseline_service_source is None:
            parser.error("compare-r5 必须显式提供 baseline-service-source，不能用当前服务冒充基线")
        if args.rows != 30000 or args.runs != 30:
            parser.error("compare-r5 固定使用30000型号、每类每轮30次")
        if args.revision not in ("head", _R5_REVISION) or args.target_p95_ms != 70:
            parser.error("compare-r5 固定比较 c4/r5，短词目标固定为70ms")
    # 复用测试隔离协议：仅本机 spareparts_test*，建完销毁，不触碰指定基础库的数据。
    run = create_database_run(args.database_url)
    engine = None
    try:
        os.environ["DATABASE_URL"] = run.database_url
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
        # 原迁移已上线；先在原结构造数，再实测新增迁移对完整投影的回填。
        command.upgrade(cfg, _BASELINE_REVISION)
        from app.config import get_settings

        engine = create_engine(run.database_url)
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO dim_part(pn_std,pn_raw_sample,brand,manufacturer,summary,
                                     description,category_major,category_minor)
                SELECT CASE i WHEN 0 THEN '硬盘' WHEN 1 THEN 'HPE'
                       ELSE (CASE WHEN i%100<50 THEN 'M1' WHEN i%100<80 THEN 'S'
                             WHEN i%100<90 THEN 'HP' ELSE 'D' END)
                            ||'-'||lpad(i::text,5,'0')||'-AC4L-'||upper(substr(md5(i::text),1,6)) END,
                       'RAW-'||i, CASE WHEN i%10<3 THEN 'HPE'
                       ELSE 'VENDOR'||lpad((i%30)::text,2,'0') END,
                       'MANUFACTURER'||(i%23), '企业级存储备件 '||i,
                       '耐用服务器部件 第'||i||'号',
                       CASE WHEN i%10<3 THEN (ARRAY['机械硬盘','固态硬盘'])[i%2+1]
                       WHEN i%10<5 THEN '服务器内存' ELSE '网络设备' END,
                       (ARRAY['企业盘','高速盘','存储模块'])[i%3+1]
                FROM generate_series(0,:last) i
            """), {"last": args.rows - 1})
            conn.execute(text("""
                INSERT INTO product_specs(part_id,spec_key,spec_value,spec_unit)
                SELECT id, 'capacity', CASE WHEN id%5=0 THEN '1TB'
                       ELSE (ARRAY['1200GB','2400GB','4800GB'])[id%3+1] END, 'GB'
                FROM dim_part
            """))
            conn.execute(text("""
                INSERT INTO product_specs(part_id,spec_key,spec_value)
                SELECT id, 'interface', (ARRAY['SAS','SATA','NVME'])[id%3+1] FROM dim_part
            """))
            conn.execute(text("""
                INSERT INTO part_alias(pn_raw,pn_std,part_id,status)
                SELECT 'LEGACY-'||pn_std,pn_std,id,'active' FROM dim_part
            """))
        if args.compare_r5:
            _compare_r5(args, cfg, engine)
            return
        search = _load_search(args.baseline_service_source)
        migration = _migrate(cfg, args.revision)
        _vacuum(engine)
        with engine.connect() as conn:
            pn = conn.execute(text("SELECT pn_std FROM dim_part ORDER BY id OFFSET 12345 LIMIT 1")).scalar_one()
        report = _measure(engine, search, pn, args.runs, args.target_p95_ms)
        report.update(rows=args.rows, runs_per_case=args.runs, revision=args.revision,
                      service_source=str(args.baseline_service_source) if args.baseline_service_source else "app.services.part_search",
                      candidate_limit=get_settings().part_search_candidate_limit,
                      migration=migration, storage=_storage_snapshot(engine))
        _write_report(report, args.output)
        if not report["within_target"]:
            raise SystemExit(f"存在查询类别 p95 超过 {args.target_p95_ms:g}ms，需定位后重新实测")
    finally:
        if engine is not None:
            engine.dispose()
        # Alembic env 使用的应用引擎也须归还连接。
        if "app.db" in sys.modules:
            sys.modules["app.db"].engine.dispose()
        cleanup_database_run(run)


def _measure(engine, search, pn: str, runs: int, target_p95_ms: float) -> dict:
    cases = [
        ("精确PN", pn), ("前缀", "M1-1234"), ("中段子串", pn[9:]),
        ("紧凑写法", pn.replace("-", "")), ("通配", "M1-1234?-AC4L*"),
        ("中文品类", "机械硬盘"), ("高频中文品类", "硬盘"),
        ("品牌", "VENDOR07"), ("高频品牌", "HPE"), ("规格值", "1200GB"),
        ("多词", "VENDOR07 NVME 容量"), ("错拼", pn[:12] + "X" + pn[13:]),
        ("单字符", "M"), ("纯数字", "12345"), ("无结果", "ZXQJNONEFOUND998877"),
        ("短词S", "S"), ("短词M", "M"), ("短词硬盘", "硬盘"),
        ("短词内存", "内存"), ("短词HP", "HP"), ("短词1T", "1T"),
    ]
    plans = {}
    statements = {}
    captured = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if statement.startswith(("SELECT", "WITH")) and "part_search_text s" in statement:
            captured.append((statement, parameters))

    timings = []
    report = {"cases": {}}
    with Session(engine) as db:
        report["distribution"] = dict(db.execute(text("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE category_major LIKE '%硬盘%') AS disk_rows,
                   count(*) FILTER (WHERE category_major LIKE '%内存%') AS memory_rows,
                   count(*) FILTER (WHERE pn_std LIKE 'M1-%') AS m1_pn_rows,
                   count(*) FILTER (WHERE pn_std LIKE 'S-%') AS s_pn_rows,
                   count(*) FILTER (WHERE pn_std LIKE 'HP-%') AS hp_pn_rows,
                   count(*) FILTER (WHERE pn_std LIKE 'D-%') AS d_pn_rows
            FROM dim_part
        """)).mappings().one())
        for name, query in cases:
            warm = search(db, query)
            assert bool(warm) == (name != "无结果"), f"{name} 的召回语义错误"
            if name in ("精确PN", "紧凑写法"):
                assert warm[0]["pn_std"] == pn and warm[0]["match_type"] == "exact_pn"
            if name in ("高频中文品类", "高频品牌"):
                assert warm[0]["pn_std"] == query and warm[0]["match_type"] == "exact_pn"
            samples = []
            count = 0
            for _ in range(runs):
                start = perf_counter()
                count = len(search(db, query))
                samples.append((perf_counter() - start) * 1000)
            report["cases"][name] = {"query": query, "count": count,
                                     "samples_ms": samples, **_percentiles(samples)}
            report["cases"][name]["within_target"] = report["cases"][name]["p95_ms"] <= target_p95_ms
            timings.extend(samples)
            print(json.dumps({name: report["cases"][name]}, ensure_ascii=False), flush=True)
        report["planner_settings"] = dict(db.execute(text("""
            SELECT current_setting('jit') AS jit,
                   current_setting('plan_cache_mode') AS plan_cache_mode,
                   current_setting('max_parallel_workers_per_gather') AS max_parallel_workers_per_gather
        """)).mappings().one())
        report["prepared_plans"] = dict(db.execute(text("""
            SELECT coalesce(sum(custom_plans), 0)::bigint AS custom_plans,
                   coalesce(sum(generic_plans), 0)::bigint AS generic_plans
            FROM pg_prepared_statements WHERE statement LIKE '%part_search_text%'
        """)).mappings().one())
        event.listen(engine, "before_cursor_execute", capture)
        for name, query in cases:
            captured.clear()
            search(db, query)
            for i, (statement, parameters) in enumerate(captured[:]):
                plan = db.connection().exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS) " + statement,
                                                       parameters).scalars().all()
                key = name + "-SQL" + str(i + 1)
                plans[key] = plan
                statements[key] = {"sql": statement, "parameters": parameters}
        event.remove(engine, "before_cursor_execute", capture)
        # 同时保留原文 ILIKE 和当前大写投影 LIKE 的自然计划，不强制开关索引。
        for term in ("S", "硬盘"):
            for operator in ("ILIKE", "LIKE"):
                plans[f"原始-{operator}-{term}"] = db.execute(text(
                    "EXPLAIN (ANALYZE, BUFFERS) SELECT part_id FROM part_search_text "
                    f"WHERE doc {operator} :pattern"), {"pattern": "%" + term + "%"}).scalars().all()
    report["overall"] = _percentiles(timings)
    report["target_p95_ms"] = target_p95_ms
    report["within_target"] = all(case["within_target"] for case in report["cases"].values())
    report["plans"] = plans
    report["statements"] = statements
    report["gin_used"] = any("Bitmap Index Scan" in line and "part_search" in line
                             for plan in plans.values() for line in plan)
    return report


def _compare_r5(args, cfg, engine) -> None:
    from app.config import get_settings

    baseline_search = _load_search(args.baseline_service_source)
    current_search = _load_search(None)
    source = args.baseline_service_source.resolve()
    report = {"mode": "compare-r5", "rows": args.rows, "runs_per_case": 30, "pairs": 3,
              "order": ["baseline", "r5"] * 3,
              "baseline_service_source": str(source),
              "baseline_service_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "current_service_sha256": hashlib.sha256(
                  (Path(__file__).resolve().parents[1] / "app/services/part_search.py").read_bytes()).hexdigest(),
              "candidate_limit": get_settings().part_search_candidate_limit,
              "acceptance": {"regression_factor": 1.15, "regression_additive_ms": 8,
                             "short_p95_ms": 70, "short_cases": list(_SHORT_CASES),
                             "mandatory_cases": ["错拼", "品牌"]},
              "rounds": [], "cases": {}}
    baseline_storage = None
    distribution = None
    for pair in range(1, 4):
        for label, revision, search in (("baseline", _BASELINE_REVISION, baseline_search),
                                        ("r5", _R5_REVISION, current_search)):
            # Session 已退出；新物理连接防止跨轮复用旧 schema 的 prepared plan。
            engine.dispose()
            migration = _migrate(cfg, revision, downgrade=label == "baseline" and pair > 1)
            _vacuum(engine)
            storage = _storage_snapshot(engine)
            if baseline_storage is None:
                baseline_storage = storage
            columns_equal = storage["columns"] == baseline_storage["columns"]
            heap_equal = storage["heap_pages"] == baseline_storage["heap_pages"]
            documents_equal = storage["documents"] == baseline_storage["documents"]
            schema_valid = (storage["revision"] == revision
                            and [c["name"] for c in storage["columns"]] == [
                                "part_id", "doc", "doc_compact", "updated_at"]
                            and (storage["short_key_table"] is not None) == (label == "r5"))
            if label == "r5" and storage["short_key_table"] is not None:
                schema_valid = schema_valid and (storage["short_key_table"]["rows"]
                                                  == storage["documents"]["rows"])
            structure = {"columns_equal": columns_equal, "heap_pages_equal": heap_equal,
                         "documents_equal": documents_equal, "schema_valid": schema_valid,
                         "same_relfilenode": storage["relfilenode"] == baseline_storage["relfilenode"]}
            print(json.dumps({"pair": pair, "variant": label, "storage": storage,
                              "structure_comparison": structure,
                              "migration": migration}, ensure_ascii=False), flush=True)
            with engine.connect() as conn:
                pn = conn.execute(text("SELECT pn_std FROM dim_part ORDER BY id OFFSET 12345 LIMIT 1")).scalar_one()
            measured = _measure(engine, search, pn, 30, 70)
            if distribution is None:
                distribution = measured["distribution"]
            structure["distribution_equal"] = distribution == measured["distribution"]
            measured.update(pair=pair, variant=label, revision=revision,
                            storage=storage, structure_comparison=structure, migration=migration)
            report["rounds"].append(measured)
            # 每半轮落盘；即使后续被中断也保留已经采集的完整原始数据。
            if args.output:
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["distribution"] = distribution
    for name, first in report["rounds"][0]["cases"].items():
        variants = {label: [r["cases"][name]["p95_ms"] for r in report["rounds"]
                            if r["variant"] == label] for label in ("baseline", "r5")}
        base_p95 = median(variants["baseline"])
        r5_p95 = median(variants["r5"])
        ceiling = max(base_p95 * 1.15, base_p95 + 8)
        relative_pass = r5_p95 <= ceiling
        short_pass = name not in _SHORT_CASES or r5_p95 <= 70
        report["cases"][name] = {"query": first["query"], "round_p95_ms": variants,
                                 "baseline_median_p95_ms": base_p95,
                                 "r5_median_p95_ms": r5_p95,
                                 "ceiling_ms": round(ceiling, 3),
                                 "regression_pass": relative_pass, "short_target_pass": short_pass,
                                 "passed": relative_pass and short_pass}
    report["physical_invariants_passed"] = all(all(r["structure_comparison"].values())
                                                for r in report["rounds"])
    report["mandatory_cases_passed"] = all(report["cases"][name]["passed"] for name in ("错拼", "品牌"))
    report["all_passed"] = report["physical_invariants_passed"] and all(
        c["passed"] for c in report["cases"].values())
    _write_report(report, args.output)
    if not report["all_passed"]:
        raise SystemExit("r5 三对交替基准未通过，详见逐类中位p95、阈值和结构对照")


if __name__ == "__main__":
    main()
