"""在独占临时测试库中生成 3 万型号，测量 M1 的完整服务耗时及真实执行计划。"""
import argparse
import json
import math
import os
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from tests.run_isolation import cleanup_database_run, create_database_run


def _percentiles(samples: list[float]) -> dict:
    values = sorted(samples)
    return {"p50_ms": round(values[math.ceil(len(values) * .50) - 1], 3),
            "p95_ms": round(values[math.ceil(len(values) * .95) - 1], 3)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"), required=not os.environ.get("DATABASE_URL"))
    parser.add_argument("--rows", type=int, default=30000)
    parser.add_argument("--runs", type=int, default=30)
    args = parser.parse_args()
    if args.rows < 12346 or args.runs < 1:
        parser.error("rows 至少 12346，runs 至少 1")
    # 复用测试隔离协议：仅本机 spareparts_test*，建完销毁，不触碰指定基础库的数据。
    run = create_database_run(args.database_url)
    engine = None
    try:
        os.environ["DATABASE_URL"] = run.database_url
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
        command.upgrade(cfg, "head")
        from app.services.part_search import search

        engine = create_engine(run.database_url)
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO dim_part(pn_std,pn_raw_sample,brand,manufacturer,summary,
                                     description,category_major,category_minor)
                SELECT 'M1-'||lpad(i::text,5,'0')||'-AC4L-'||upper(substr(md5(i::text),1,6)),
                       'RAW-'||i, 'VENDOR'||lpad((i%30)::text,2,'0'),
                       'MANUFACTURER'||(i%23), '企业级存储备件 '||i,
                       '耐用服务器部件 第'||i||'号',
                       (ARRAY['机械硬盘','固态硬盘','服务器内存'])[i%3+1],
                       (ARRAY['企业盘','高速盘','存储模块'])[i%3+1]
                FROM generate_series(0,:last) i
            """), {"last": args.rows - 1})
            conn.execute(text("""
                INSERT INTO product_specs(part_id,spec_key,spec_value,spec_unit)
                SELECT id, 'capacity', (ARRAY['1200GB','2400GB','4800GB'])[id%3+1], 'GB'
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
        # 大批量造数后执行常规维护，合并 GIN pending list；不强制查询计划。
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for table in ("dim_part", "part_search_text", "part_alias", "product_specs"):
                conn.execute(text("VACUUM (ANALYZE) " + table))
            pn = conn.execute(text("SELECT pn_std FROM dim_part ORDER BY id OFFSET 12345 LIMIT 1")).scalar_one()
        cases = [
            ("精确PN", pn), ("前缀", "M1-1234"), ("中段子串", pn[9:]),
            ("紧凑写法", pn.replace("-", "")), ("通配", "M1-1234?-AC4L*"),
            ("中文品类", "机械硬盘"), ("品牌", "VENDOR07"), ("规格值", "1200GB"),
            ("多词", "VENDOR07 NVME 容量"), ("错拼", pn[:12] + "X" + pn[13:]),
            ("单字符", "M"), ("纯数字", "12345"), ("无结果", "ZXQJNONEFOUND998877"),
        ]
        plans = {}
        captured = []

        def capture(_conn, _cursor, statement, parameters, _context, _many):
            if statement.startswith(("SELECT", "WITH")) and "FROM part_search_text s" in statement:
                captured.append((statement, parameters))

        timings = []
        report = {"rows": args.rows, "runs_per_case": args.runs, "cases": {}}
        with Session(engine) as db:
            for name, query in cases:
                warm = search(db, query)
                assert bool(warm) == (name != "无结果"), f"{name} 的召回语义错误"
                if name in ("精确PN", "紧凑写法"):
                    assert warm[0]["pn_std"] == pn and warm[0]["match_type"] == "exact_pn"
                samples = []
                count = 0
                for _ in range(args.runs):
                    start = perf_counter()
                    count = len(search(db, query))
                    samples.append((perf_counter() - start) * 1000)
                report["cases"][name] = {"query": query, "count": count, **_percentiles(samples)}
                timings.extend(samples)
                print(json.dumps({name: report["cases"][name]}, ensure_ascii=False), flush=True)
            event.listen(engine, "before_cursor_execute", capture)
            for name in ("精确PN", "紧凑写法", "纯数字"):
                query = dict(cases)[name]
                captured.clear()
                search(db, query)
                for i, (statement, parameters) in enumerate(captured[:]):
                    plan = db.connection().exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS) " + statement,
                                                           parameters).scalars().all()
                    plans[name if i == 0 else name + "-容错"] = plan
            event.remove(engine, "before_cursor_execute", capture)
        report["overall"] = _percentiles(timings)
        report["target_p95_ms"] = 150
        report["within_target"] = report["overall"]["p95_ms"] <= 150
        report["plans"] = plans
        report["gin_used"] = any("Bitmap Index Scan" in line and "part_search" in line
                                 for plan in plans.values() for line in plan)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    finally:
        if engine is not None:
            engine.dispose()
        # Alembic env 使用的应用引擎也须归还连接。
        if "app.db" in sys.modules:
            sys.modules["app.db"].engine.dispose()
        cleanup_database_run(run)


if __name__ == "__main__":
    main()
