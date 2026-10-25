"""在独占本机测试库实测检索迁移、批量写入和写入期间的联想请求。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from statistics import median
import sys
from threading import Event
from time import perf_counter, sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tests.run_isolation import cleanup_database_run, create_database_run


def _vacuum(engine) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for table in ("dim_part", "part_search_text", "part_alias", "product_specs"):
            conn.execute(text("VACUUM (ANALYZE) " + table))


def _projection_stamp(conn):
    return conn.execute(text("""
        SELECT count(*), md5(string_agg(part_id::text || ':' || updated_at::text,
                                       ',' ORDER BY part_id))
        FROM part_search_text
    """)).one()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                        required=not os.environ.get("DATABASE_URL"))
    parser.add_argument("--rows", type=int, default=30000)
    parser.add_argument("--runs", type=int, default=7)
    args = parser.parse_args()
    if args.rows < 10000 or args.runs < 3:
        parser.error("rows 至少 10000，runs 至少 3")
    run = create_database_run(args.database_url)
    engine = None
    listener_installed = False
    report = {"rows": args.rows, "runs_per_mode": args.runs}
    starts = {}
    source_lock_starts = {}

    def before_sql(conn, _cursor, statement, _parameters, context, _many):
        if statement.startswith("SELECT refresh_part_search_text(id) FROM dim_part"):
            starts[id(context)] = perf_counter()

    def after_sql(conn, _cursor, statement, _parameters, context, _many):
        if "CREATE TABLE part_search_text" in statement:
            source_lock_starts["dim_part"] = perf_counter()
        for table in ("product_specs", "part_alias"):
            if "CREATE TRIGGER trg_" + table + "_search_text" in statement:
                source_lock_starts[table] = perf_counter()
        started = starts.pop(id(context), None)
        if started is None:
            return
        report["backfill_ms"] = round((perf_counter() - started) * 1000, 3)
        # 在迁移尚未提交时读取真实锁，避免仅按 DDL 名称推断锁模式。
        report["migration_locks_after_backfill"] = [dict(row) for row in conn.execute(text("""
            SELECT c.relname AS table_name, l.mode
            FROM pg_locks l JOIN pg_class c ON c.oid=l.relation
            WHERE l.pid=pg_backend_pid() AND l.granted AND c.relname IN
                ('dim_part','product_specs','part_alias','part_search_text','spec_key_label')
            ORDER BY c.relname,l.mode
        """)).mappings()]

    try:
        os.environ["DATABASE_URL"] = run.database_url
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
        command.upgrade(cfg, "b2f4a6c8d1e3")
        engine = create_engine(run.database_url)
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO dim_part(pn_std,pn_raw_sample,brand,manufacturer,summary,
                                     description,category_major,category_minor,data_quality_score)
                SELECT 'WRITE-'||lpad(i::text,5,'0')||'-'||upper(substr(md5(i::text),1,6)),
                       'RAW-'||i, CASE WHEN i%10<3 THEN 'HPE' ELSE 'VENDOR'||(i%30) END,
                       'MANUFACTURER'||(i%23), '企业级备件 '||i, '服务器部件 第'||i||'号',
                       CASE WHEN i%10<3 THEN '硬盘' ELSE '服务器内存' END, '存储模块', 0
                FROM generate_series(0,:last) i
            """), {"last": args.rows - 1})
            conn.execute(text("""
                INSERT INTO product_specs(part_id,spec_key,spec_value)
                SELECT id,'capacity','1200GB' FROM dim_part
                UNION ALL SELECT id,'interface','SAS' FROM dim_part
            """))
            conn.execute(text("""
                INSERT INTO part_alias(pn_raw,pn_std,part_id,status)
                SELECT 'LEGACY-'||pn_std,pn_std,id,'active' FROM dim_part
            """))
        event.listen(Engine, "before_cursor_execute", before_sql)
        event.listen(Engine, "after_cursor_execute", after_sql)
        listener_installed = True
        started = perf_counter()
        command.upgrade(cfg, "c4e6a8b1d3f5")
        migration_finished = perf_counter()
        report["migration_total_ms"] = round((migration_finished - started) * 1000, 3)
        report["source_table_lock_hold_ms"] = {
            table: round((migration_finished - acquired) * 1000, 3)
            for table, acquired in source_lock_starts.items()
        }
        event.remove(Engine, "before_cursor_execute", before_sql)
        event.remove(Engine, "after_cursor_execute", after_sql)
        listener_installed = False
        print(json.dumps({"migration": report}, ensure_ascii=False), flush=True)
        _vacuum(engine)
        with engine.connect() as conn:
            original_stamp = _projection_stamp(conn)
            assert original_stamp[0] == args.rows
        samples = {"disabled": [], "enabled": []}

        def quality_update(mode):
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE dim_part "
                                  + ("ENABLE" if mode == "enabled" else "DISABLE")
                                  + " TRIGGER trg_dim_part_search_text"))
            _vacuum(engine)
            started = perf_counter()
            with engine.begin() as conn:
                result = conn.execute(text(
                    "UPDATE dim_part SET data_quality_score=data_quality_score+1"
                ))
                assert result.rowcount == args.rows
            return round((perf_counter() - started) * 1000, 3)

        # 两种模式都预热；每轮交换顺序，减少缓存和运行顺序偏差。
        for mode in samples:
            quality_update(mode)
        for iteration in range(args.runs):
            modes = ("disabled", "enabled") if iteration % 2 == 0 else ("enabled", "disabled")
            for mode in modes:
                samples[mode].append(quality_update(mode))
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE dim_part ENABLE TRIGGER trg_dim_part_search_text"))
            assert _projection_stamp(conn) == original_stamp, "无关列更新重建了投影"
        baseline = median(samples["disabled"])
        enabled = median(samples["enabled"])
        report["unrelated_update"] = {
            "samples_ms": samples, "disabled_median_ms": baseline, "enabled_median_ms": enabled,
            "overhead_percent": round((enabled / baseline - 1) * 100, 3),
            "projection_unchanged": True,
        }
        print(json.dumps({"unrelated_update": report["unrelated_update"]}, ensure_ascii=False), flush=True)
        _vacuum(engine)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.parts import router
        from app.auth import _make_token
        from app.db import get_db
        from app.permissions import effective

        api = FastAPI()
        api.include_router(router, prefix="/api")

        def request_db():
            with Session(engine) as db:
                # 读如果等待锁，尽快失败，不能把无限等待当作并发成功。
                db.execute(text("SET LOCAL statement_timeout='2s'"))
                yield db

        api.dependency_overrides[get_db] = request_db
        token, _ = _make_token("sales", "写入基准", "写入基准",
                               perms=effective("sales", None), authn="sys_user")
        ready = Event()
        writer_pid = []

        def update_brand():
            with engine.begin() as conn:
                writer_pid.append(conn.scalar(text("SELECT pg_backend_pid()")))
                ready.set()
                started = perf_counter()
                result = conn.execute(text("""
                    /* part-search-brand-bench */
                    UPDATE dim_part SET brand='UPDATED-BRAND' WHERE id<=10000
                """))
                elapsed = (perf_counter() - started) * 1000
                assert result.rowcount == 10000
            return {"rows": result.rowcount, "statement_ms": round(elapsed, 3),
                    "with_commit_ms": round((perf_counter() - started) * 1000, 3)}

        def writer_active(conn):
            conn.execute(text("SELECT pg_stat_clear_snapshot()"))
            return bool(conn.scalar(text("""
                SELECT state='active' AND query LIKE '%part-search-brand-bench%'
                FROM pg_stat_activity WHERE pid=:pid
            """), {"pid": writer_pid[0]}))

        reads = []
        with TestClient(api) as client, ThreadPoolExecutor(max_workers=1) as executor:
            # 预热实际 HTTP 路由及请求会话；此处不计入并发读样本。
            response = client.get("/api/parts/suggest", params={"q": "硬盘"},
                                  headers={"Authorization": "Bearer " + token})
            assert response.status_code == 200 and response.json()
            future = executor.submit(update_brand)
            assert ready.wait(5), "批量写入未启动"
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline = perf_counter() + 5
                while not writer_active(observer):
                    assert not future.done(), "未捕获正在执行的批量更新"
                    assert perf_counter() < deadline, "批量更新没有开始执行"
                    sleep(.005)
                while len(reads) < 5 and not future.done():
                    active_before = writer_active(observer)
                    started = perf_counter()
                    response = client.get("/api/parts/suggest", params={"q": "硬盘"},
                                          headers={"Authorization": "Bearer " + token})
                    elapsed = (perf_counter() - started) * 1000
                    active_after = writer_active(observer)
                    assert response.status_code == 200 and response.json(), response.text
                    reads.append({"http_status": response.status_code, "count": len(response.json()),
                                  "elapsed_ms": round(elapsed, 3),
                                  "writer_active_before": active_before,
                                  "writer_active_after": active_after})
            report["related_brand_update"] = future.result(timeout=120)
        assert any(r["writer_active_before"] and r["writer_active_after"] for r in reads), \
            "没有联想请求在批量写语句执行期间完成"
        with engine.connect() as conn:
            rebuilt = conn.scalar(text("""
                SELECT count(*) FROM part_search_text
                WHERE part_id<=10000 AND doc LIKE '%UPDATED-BRAND%'
            """))
            assert rebuilt == 10000
        report["concurrent_suggest"] = {"path": "/api/parts/suggest?q=硬盘", "samples": reads,
                                        "completed_during_write": True}
        report["within_overhead_target"] = report["unrelated_update"]["overhead_percent"] <= 10
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if not report["within_overhead_target"]:
            raise SystemExit("无关列更新开销超过 10%，需定位后重新实测")
    finally:
        if listener_installed:
            event.remove(Engine, "before_cursor_execute", before_sql)
            event.remove(Engine, "after_cursor_execute", after_sql)
        if engine is not None:
            engine.dispose()
        if "app.db" in sys.modules:
            sys.modules["app.db"].engine.dispose()
        cleanup_database_run(run)


if __name__ == "__main__":
    main()
