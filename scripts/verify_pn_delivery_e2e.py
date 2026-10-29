"""Authorized jinchen-dev synthetic HTTP verification, never a production task.

Run with the itsp venv on Linux. PN_E2E_DATABASE_BASE_URL must explicitly name the
owned perf-verify-pg loopback port. Creates two uniquely owned databases and two
attached uvicorn children; always stops children and drops only these databases.
Only aggregate measurements leave this harness. No credentials or app logs print.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


class VerificationFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise VerificationFailure(code)


def run_command(command, *, timeout, **options):
    """Keep short-lived migration/TLS children attached and stop them on cancellation."""
    process = subprocess.Popen(command, **options)
    try:
        return process.wait(timeout=timeout)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def distribution(values):
    ordered = sorted(values)
    return {"n": len(values), "p50_ms": round(statistics.median(values), 2),
            "p95_ms": round(ordered[math.ceil(len(ordered) * .95) - 1], 2),
            "max_ms": round(max(values), 2)}


def request_json(url, body=None, headers=None, *, context=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, context=context, timeout=20) as response:
            return response.status, json.loads(response.read(16384))
    except urllib.error.HTTPError as exc:
        return exc.code, None


def wait_ready(process, url, context=None):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        require(process.poll() is None, "service_exited_before_ready")
        try:
            if request_json(url, context=context)[0] == 200:
                return
        except (OSError, ValueError):
            pass
        time.sleep(.05)
    raise VerificationFailure("service_startup_timeout")


class GateProxy(BaseHTTPRequestHandler):
    """Real HTTP forwarding; first notification waits for the create-response proof."""
    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.path != "/api/integrations/catalog/parts/sync":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 4096:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        with self.server.gate_lock:
            first = not self.server.seen_first
            self.server.seen_first = True
        if first:
            self.server.entered.set()
            if not self.server.release.wait(10):
                self.send_error(503)
                return
        connection = http.client.HTTPConnection("127.0.0.1", self.server.shop_port, timeout=15)
        try:
            connection.request("POST", self.path, body=body, headers={
                "Authorization": self.headers.get("Authorization", ""),
                "Content-Type": "application/json", "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            payload = response.read(4097)
            if len(payload) > 4096:
                self.send_error(502)
                return
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            self.send_error(502)
        finally:
            connection.close()


def verify(args, temporary, owned, processes, logs):
    base = make_url(os.environ["PN_E2E_DATABASE_BASE_URL"])
    require(sys.platform == "linux", "run_only_on_authorized_linux_dev_host")
    require(base.host == "127.0.0.1" and base.port == 32774 and base.database == "spareparts_test",
            "explicit_owned_dev_database_required")
    roots = {"itsp": Path(args.itsp_root).resolve(), "shop": Path(args.shop_root).resolve()}
    for root in roots.values():
        require(not (root / "backend/.env").exists(), "checkout_env_file_not_allowed")
        require((root / "backend/.venv/bin/python").is_file(), "prepared_venv_required")
    owner = "codex-perf-pn-http-e2e-" + uuid4().hex
    admin_dsn = base.set(drivername="postgresql").render_as_string(hide_password=False)
    databases = {}
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        for kind in ("itsp", "shop"):
            name = f"perf_pn_e2e_{kind}_{uuid4().hex[:16]}"
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            owned.append((admin_dsn, name, owner))
            admin.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(name), sql.Literal(owner)))
            databases[kind] = base.set(database=name)
    print(json.dumps({"stage": "owned_databases_created", "count": 2}), flush=True)

    cert, key = temporary / "task-ca.pem", temporary / "task-key.pem"
    result = run_command([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        "-addext", "basicConstraints=critical,CA:TRUE",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    require(result == 0, "task_tls_generation_failed")
    key.chmod(0o600)
    context = ssl.create_default_context(cafile=str(cert))
    itsp_port, shop_port = free_port(), free_port()
    require(itsp_port != shop_port, "ephemeral_port_collision")
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), GateProxy)
    proxy.daemon_threads = True
    proxy.shop_port = shop_port
    proxy.gate_lock, proxy.seen_first = threading.Lock(), False
    proxy.entered, proxy.release = threading.Event(), threading.Event()
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    source_url = f"https://localhost:{itsp_port}"
    shop_url = f"http://127.0.0.1:{shop_port}"
    notification_url = f"http://127.0.0.1:{proxy.server_port}/api/integrations/catalog/parts/sync"
    tokens = {name: secrets.token_urlsafe(36) for name in ("source", "write", "notification")}
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    # Explicit child environment: never inherit a real service's credentials/config.
    common = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1",
              "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "localhost,127.0.0.1,::1"}
    environments = {
        "itsp": {**common, "ENVIRONMENT": "dev", "DATABASE_URL": databases["itsp"].render_as_string(hide_password=False),
                 "RAW_FILE_DIR": str(temporary / "raw"),
                 "RESALE_CATALOG_TOKEN_SHA256": digest(tokens["source"]),
                 "PN_MASTER_INTEGRATION_TOKEN_SHA256": digest(tokens["write"]),
                 "SHOP_CATALOG_SYNC_ENABLED": "true", "SHOP_CATALOG_SYNC_URL": notification_url,
                 "SHOP_CATALOG_SYNC_TOKEN": tokens["notification"]},
        "shop": {**common, "APP_ENV": "test", "LOG_LEVEL": "WARNING",
                 "DATABASE_URL": databases["shop"].render_as_string(hide_password=False),
                 "CATALOG_SYNC_ENABLED": "true", "CATALOG_SYNC_TOKEN_SHA256": digest(tokens["notification"]),
                 "ITSP_CATALOG_URL": source_url + "/api/integrations/resale/parts",
                 "ITSP_CATALOG_TOKEN": tokens["source"], "SSL_CERT_FILE": str(cert)},
    }
    try:
        for kind in ("itsp", "shop"):
            backend = roots[kind] / "backend"
            log = open(temporary / f"{kind}.log", "wb")
            logs.append(log)
            migration = run_command([str(backend / ".venv/bin/python"), "-m", "alembic", "upgrade", "head"],
                cwd=backend, env=environments[kind], stdout=log, stderr=subprocess.STDOUT, timeout=180)
            require(migration == 0, f"{kind}_migration_failed")
            command = [str(backend / ".venv/bin/python"), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                       "--port", str(itsp_port if kind == "itsp" else shop_port), "--no-access-log"]
            if kind == "itsp":
                command += ["--ssl-keyfile", str(key), "--ssl-certfile", str(cert)]
            process = subprocess.Popen(command, cwd=backend, env=environments[kind], stdout=log, stderr=subprocess.STDOUT)
            processes.append(process)
            wait_ready(process, source_url + "/health" if kind == "itsp" else shop_url + "/api/ready",
                       context if kind == "itsp" else None)
        print(json.dumps({"stage": "real_http_services_ready", "source_tls_verified": True}), flush=True)
        source_dsn = databases["itsp"].set(drivername="postgresql").render_as_string(hide_password=False)
        shop_dsn = databases["shop"].set(drivername="postgresql").render_as_string(hide_password=False)
        with psycopg.connect(source_dsn, autocommit=True) as source, psycopg.connect(shop_dsn, autocommit=True) as shop:
            shop.execute("INSERT INTO catalog_import_state(source_namespace,cursor,updated_at) VALUES ('itsp','e2e-fixed',now())")
            cursor_before = shop.execute("SELECT * FROM catalog_import_state WHERE source_namespace='itsp'").fetchone()
            create_times, after_response_times, end_to_end_times = [], [], []
            barrier_proof = None
            last_delivery = None
            for ordinal in range(args.samples + 1):
                started = time.perf_counter()
                status, payload = request_json(source_url + "/api/integrations/pn-master/parts", {
                    "pn_std": f"SYNTHETIC-PN-E2E-{ordinal:04d}", "force": True, "description": "synthetic delivery verification",
                }, {"Authorization": "Bearer " + tokens["write"], "X-Actor-System": "shop",
                    "X-Actor-User": "synthetic-e2e", "X-Request-Id": str(uuid4())}, context=context)
                returned = time.perf_counter()
                require(status == 200 and payload.get("created") is True, "source_create_failed")
                part_id = payload["part_id"]
                if ordinal == 0:
                    require(proxy.entered.wait(5), "notification_did_not_start_after_create")
                    pending = source.execute("SELECT state FROM catalog_delivery WHERE part_id=%s", (part_id,)).fetchone()
                    require(pending == ("delivering",), "barrier_task_not_delivering")
                    require(shop.execute("SELECT count(*) FROM catalog_delivery_receipt").fetchone()[0] == 0,
                            "receipt_existed_before_barrier_release")
                    time.sleep(1)
                    barrier_proof = {"create_response_ms": round((returned - started) * 1000, 2),
                                     "notification_held_after_response_ms": 1000,
                                     "create_completed_before_remote_release": True}
                    proxy.release.set()
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    row = source.execute("SELECT state, attempts, receipt_status, delivery_id FROM catalog_delivery WHERE part_id=%s",
                                         (part_id,)).fetchone()
                    if row and row[0] == "succeeded":
                        break
                    require(row is None or row[0] != "conflict", "unexpected_catalog_conflict")
                    time.sleep(.02)
                else:
                    raise VerificationFailure("delivery_not_completed_within_budget")
                completed = time.perf_counter()
                require(row[1] == 1 and row[2] == "applied", "unexpected_retry_or_receipt")
                last_delivery = {"schemaVersion": 1, "deliveryId": str(row[3]), "partId": part_id}
                if ordinal:
                    create_times.append((returned - started) * 1000)
                    after_response_times.append((completed - returned) * 1000)
                    end_to_end_times.append((completed - started) * 1000)
            # Real HTTP duplicate replay uses the same durable receipt, without a new PN/SKU.
            replay_status, replay = request_json(shop_url + "/api/integrations/catalog/parts/sync", last_delivery,
                                                {"Authorization": "Bearer " + tokens["notification"]})
            require(replay_status == 200 and replay == {**last_delivery, "status": "applied"}, "replay_contract_failed")
            collision_status, _ = request_json(shop_url + "/api/integrations/catalog/parts/sync",
                {**last_delivery, "partId": last_delivery["partId"] + 1},
                {"Authorization": "Bearer " + tokens["notification"]})
            require(collision_status == 409, "delivery_collision_not_rejected")
            counts = {}
            for table in ("pn", "sku", "stock_txn", "agent_job", "catalog_delivery_receipt", "catalog_import_run"):
                counts[table] = shop.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
            require(counts["pn"] == args.samples + 1 and counts["sku"] == 2 * (args.samples + 1), "unexpected_identity_counts")
            require(counts["catalog_delivery_receipt"] == args.samples + 1, "unexpected_receipt_count")
            require(counts["stock_txn"] == counts["agent_job"] == counts["catalog_import_run"] == 0, "unexpected_side_effects")
            require(shop.execute("SELECT coalesce(sum(qty_on_hand),0) FROM sku").fetchone()[0] == 0, "unexpected_stock_quantity")
            require(shop.execute("SELECT * FROM catalog_import_state WHERE source_namespace='itsp'").fetchone() == cursor_before,
                    "batch_cursor_changed")
            return {"measurement": "synthetic_jinchen_dev_real_http_tls", "normal_samples": args.samples,
                    "barrier_warmup": barrier_proof, "create_http": distribution(create_times),
                    "create_response_to_observed_succeeded": distribution(after_response_times),
                    "request_start_to_observed_succeeded": distribution(end_to_end_times),
                    "poll_resolution_ms": 20, "exact_commit_timestamp_measured": False,
                    "counts": counts, "batch_cursor_unchanged": True, "stock_quantity_total": 0,
                    "replay_receipt_preserved": True, "delivery_id_collision_rejected": True}
    finally:
        proxy.release.set()
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--itsp-root", required=True)
    parser.add_argument("--shop-root", required=True)
    parser.add_argument("--samples", type=int, default=20, choices=range(1, 51))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    owned, processes, logs = [], [], []
    result, failure = None, None
    cleanup = {"processes_stopped": 0, "databases_removed": 0, "temporary_files_removed": False}
    with tempfile.TemporaryDirectory(prefix="codex-pn-http-e2e-") as temporary:
        try:
            result = verify(args, Path(temporary), owned, processes, logs)
        except BaseException as exc:
            # Only our fixed stage codes are printable; never exception/SQL/response text.
            failure = str(exc) if isinstance(exc, VerificationFailure) else type(exc).__name__
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                cleanup["processes_stopped"] += 1
            for log in logs:
                log.close()
            for dsn, name, owner in reversed(owned):
                try:
                    with psycopg.connect(dsn, autocommit=True) as admin:
                        marker = admin.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s", (name,)).fetchone()
                        require(marker == (owner,), "database_ownership_marker_mismatch")
                        admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
                        require(admin.execute("SELECT count(*) FROM pg_database WHERE datname=%s", (name,)).fetchone()[0] == 0,
                                "owned_database_still_present")
                        cleanup["databases_removed"] += 1
                except Exception:
                    failure = failure or "owned_database_cleanup_failed"
    cleanup["temporary_files_removed"] = True
    output = {"status": "failed" if failure else "completed", "failure_code": failure, "result": result, "cleanup": cleanup}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False), flush=True)
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
