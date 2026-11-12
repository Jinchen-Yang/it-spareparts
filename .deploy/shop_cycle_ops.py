#!/usr/bin/env python3
"""商城坏件循环窄发布控制器。0027 -> 0028 -> 0029。

不改写 shop_pn_ops.py（那条仍是 0026 -> 0027）。本文件不读取
/root/IT-shop/private/next.env，不改 CATALOG_* / PN_MASTER_*，不写入
CYCLE_SELL_TOKEN_SHA256，不 compose down，不改 Caddy 的 shop 站点。
迁移命令只允许官方形态：

docker compose --env-file /root/IT-shop/private/next.env -p it-shop-next
    -f <release>/prod.compose.yml --profile migration run --rm --no-deps
    <migration_service> alembic upgrade head
"""
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time
import traceback
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

BASE = Path("/data/shop/releases")
ENV_FILE = "/root/IT-shop/private/next.env"
PROJECT = "it-shop-next"
OLD_REVISION = "4b40bb24c6de3cc95e1361e97ca55e3b53475792"
OLD_HEAD = "0027_catalog_delivery_receipt"
TARGET_HEAD = "0029_cycle_sell_qty"
REFUSED_PREFIX = "f5481cf1"
SHOP_PN_DIGEST = "beca271f1420981e5dcf21b78c7469710ce084efc75d0247b86712b567361c48"
APPS = ("it-shop-next-backend", "it-shop-next-worker", "it-shop-next-agent", "it-shop-next-frontend")
BRIDGE = "it-shop-nocodb-nocodb-bridge-1"
MANAGED = (*APPS, BRIDGE)
DB = "it-shop-next-db"
CADDY = "it-spareparts-caddy-1"
CADDYFILE = Path("/opt/it-spareparts/Caddyfile")
PHASES = ("preflight", "freeze", "backup-old", "migrate", "backup-new", "create", "activate", "verify", "recover-freeze")
PRESERVED_PREFIXES = ("CATALOG_", "PN_MASTER_", "ITSP_CATALOG", "ITSP_PN_MASTER", "SHOP_CATALOG_")
_shop_pn = Path(__file__).with_name("shop_pn_ops.py")
if _shop_pn.is_symlink() or hashlib.sha256(_shop_pn.read_bytes()).hexdigest() != SHOP_PN_DIGEST:
    raise RuntimeError("shop PN controller bytes changed; refusing to retarget it")


def need(test, message):
    if not test:
        raise RuntimeError(message)


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def artifact(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "bytes": Path(path).stat().st_size, "sha256": digest.hexdigest()}


def save(path, value):
    data = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def env_map(row):
    values = row["Config"].get("Env") or []
    result = {}
    for entry in values:
        key, separator, value = entry.partition("=")
        need(separator == "=" and key not in result, "duplicate runtime environment keys")
        result[key] = value
    return result


def database_target(row):
    value = urlsplit(env_map(row).get("DATABASE_URL", ""))
    return (value.hostname, value.port or 5432, value.path) if value.hostname else None


def fingerprint(row):
    return {"id": row["Id"], "image": row["Image"], "running": row["State"]["Running"],
            "started": row["State"]["StartedAt"], "restarts": row["RestartCount"], "oom": row["State"]["OOMKilled"]}


def refuse_compose_down(argv):
    tokens = [str(item) for item in argv]
    if "down" in tokens and ({"compose", "docker-compose"} & set(tokens)):
        raise RuntimeError("compose down refused")
    if "down" in tokens and any("it-spareparts" in token for token in tokens):
        raise RuntimeError("it-spareparts compose down refused")


def script_refuses_down(text):
    return re.search(r"(?:docker[^\n;|&]*compose|docker-compose)(?:\s+[^\n;|&]*)?\s+down\b", text) is None


def candidate_release_allowed(value):
    return bool(re.fullmatch(r"[0-9a-f]{40}", value or "")) and not str(value).startswith(REFUSED_PREFIX) and value != OLD_REVISION


class Release:
    def __init__(self, manifest):
        self.path = Path(manifest)
        self.root = self.path.parent
        need(self.path.name == "manifest.json" and not self.path.is_symlink(), "manifest path invalid")
        need(self.root.resolve() == self.root and self.root.is_dir() and self.root.stat().st_uid == 0
             and stat.S_IMODE(self.root.stat().st_mode) == 0o700, "private root-owned release directory required")
        self.raw = self.path.read_bytes()
        self.m = json.loads(self.raw)
        self.release = self.m.get("release", "")
        need(candidate_release_allowed(self.release), "refusing shop main, the production baseline, or a non-SHA candidate")
        need(self.root == BASE / ("cycle-" + self.release), "release directory mismatch")
        need(self.m.get("old_revision") == OLD_REVISION, "unexpected baseline revision")
        need(self.m.get("old_head") == OLD_HEAD and self.m.get("target_head") == TARGET_HEAD, "migration scope differs")
        need(re.fullmatch(r"0028_[a-z0-9_]+", self.m.get("intermediate_head", "")), "intermediate revision must be 0028")
        need(re.fullmatch(r"[a-z][a-z0-9-]{0,31}", self.m.get("migration_service", "")), "migration service name refused")
        need(set(self.m.get("old", {})) == set(MANAGED), "explicit five-container manifest required")
        for item in self.m["old"].values():
            need(re.fullmatch(r"[0-9a-f]{64}", item.get("id", "")) and re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("image", "")),
                 "baseline identity invalid")
        for key in ("app_image", "frontend_image"):
            need(re.fullmatch(r"sha256:[0-9a-f]{64}", self.m.get(key, "")), "candidate image invalid")
        for key in ("source_sha256", "controller_sha256", "compose_sha256", "network_check_sha256", "network_rollback_sha256"):
            need(re.fullmatch(r"[0-9a-f]{64}", self.m.get(key, "")), "reviewed hash required: " + key)
        need(self.m.get("cycle_sell_token") in {None, ""}, "cycle sell token must stay empty in the manifest")
        self.prefix = "shop-cycle-" + self.release[:12]
        self.compose_file = self.root / "prod.compose.yml"

    def run(self, argv, *, input=None, stdin=None, stdout=None, timeout=180):
        refuse_compose_down(argv)
        if ENV_FILE in argv:
            need(argv[argv.index("--env-file") + 1] == ENV_FILE, "private env path drifted")
            need("cat" not in argv and "head" not in argv and "tail" not in argv, "private env read refused")
        result = subprocess.run(argv, input=input, stdin=stdin, stdout=stdout or subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode or result.stderr:
            save(self.root / ("command-" + now() + ".private.log"), (result.stdout or b"") + b"\nSTDERR\n" + (result.stderr or b""))
        need(result.returncode == 0, "command failed; inspect private diagnostic")
        return result.stdout if stdout is None else b""

    def inspect(self, name, image=False):
        result = json.loads(self.run(["docker", *(["image"] if image else []), "inspect", name]))
        need(len(result) == 1, "inspect result mismatch")
        return result[0]

    def mark(self, phase):
        row = json.loads((self.root / (phase + ".json")).read_bytes())
        need(row.get("manifest_sha256") == sha(self.raw) and row.get("phase") == phase and row.get("status") == "PASS",
             "phase receipt mismatch")
        return row

    def sql(self, query, container=DB):
        body = query.strip()
        need(re.match(r"(?is)^(select|with)\b", body) and ";" not in body, "only one read-only statement is allowed")
        need(not re.search(r"(?i)\b(insert|update|delete|truncate|drop|alter|grant|revoke)\b", body), "mutation SQL refused")
        values = env_map(self.inspect(DB))
        return self.run(["docker", "exec", "-e",
                         "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=3000 -c timezone=UTC",
                         container, "psql", "-U", values["POSTGRES_USER"], "-d", values["POSTGRES_DB"],
                         "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", query]).decode().strip()

    def schema(self, container=DB):
        head = self.sql("SELECT version_num FROM alembic_version", container)
        allowed = head in {OLD_HEAD, TARGET_HEAD} or re.fullmatch(r"0028_[a-z0-9_]+", head or "")
        need(allowed and "\n" not in head, "unexpected or multiple schema heads")
        return head

    def counts(self, tables=None, container=DB):
        if tables is None:
            tables = self.sql("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename", container).splitlines()
        need(tables and all(re.fullmatch(r"[a-z_][a-z0-9_]*", table) for table in tables), "unexpected table identifier")
        query = " UNION ALL ".join("SELECT '" + table + "',count(*) FROM public.\"" + table + "\"" for table in tables)
        return {line.split("|")[0]: int(line.split("|")[1]) for line in self.sql(query, container).splitlines()}

    def cycle_tables(self, container=DB):
        names = [name for name in self.sql(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE '%cycle%' ORDER BY tablename",
            container).splitlines() if name]
        need(all(re.fullmatch(r"[a-z_][a-z0-9_]*", name) for name in names), "unexpected cycle table identifier")
        return {name: int(self.sql('SELECT count(*) FROM public."' + name + '"', container)) for name in names}

    def entry(self):
        data = CADDYFILE.read_bytes()
        need(b"shop.ybwznt.com" in data and b"it-shop-next-frontend:80" in data, "shop site dial missing")
        active = self.run(["docker", "exec", CADDY, "curl", "-fsS", "--max-time", "5", "http://127.0.0.1:2019/config/"])
        return {"file_sha256": sha(data), "active_sha256": sha(json.dumps(json.loads(active), sort_keys=True).encode())}

    def protected(self):
        before = self.mark("preflight")
        exempt = {row["id"] for row in self.m["old"].values()}
        for cid, expected in before["containers"].items():
            if cid not in exempt:
                need(fingerprint(self.inspect(cid)) == expected, "protected container drift")
        need(self.entry() == before["entry"], "Caddy drift; shop site must stay unchanged")

    def script(self, name, key):
        path = self.root / "deploy" / "scripts" / name
        need(path.is_file() and not path.is_symlink(), "network script missing")
        text = path.read_text(encoding="utf-8")
        need(artifact(path)["sha256"] == self.m[key], "network script drifted")
        need(script_refuses_down(text), "network script contains compose down")
        return path

    def token_is_empty(self, row):
        values = env_map(row)
        need(values.get("CYCLE_SELL_TOKEN_SHA256", "") == "", "shop cycle token must stay empty during this deploy")
        return True

    def preserved_env(self, old_row, new_values):
        old = env_map(old_row)
        for key, value in old.items():
            if key.startswith(PRESERVED_PREFIXES):
                need(new_values.get(key) == value, "catalog or PN master setting changed")
        for key, value in new_values.items():
            if key.startswith(PRESERVED_PREFIXES):
                need(old.get(key) == value, "catalog or PN master setting added")
        need(new_values.get("CYCLE_SELL_TOKEN_SHA256", "") == "", "shop cycle token must stay empty during this deploy")

    def candidate_config(self, old, image):
        self.token_is_empty(old)
        cfg, host = copy.deepcopy(old["Config"]), copy.deepcopy(old["HostConfig"])
        cfg["Image"] = image
        cfg["Labels"] = {**(cfg.get("Labels") or {}), "org.opencontainers.image.revision": self.release,
                         "shop.cycle.release": self.release}
        self.preserved_env(old, env_map({"Config": cfg}))
        return cfg, host

    def alembic_argv(self, args):
        need(isinstance(args, list) and args and args[0] in {"heads", "current", "show", "history", "upgrade", "check"},
             "migration command refused")
        need("downgrade" not in args and "stamp" not in args, "downgrade or stamp refused")
        if args[0] == "upgrade":
            need(args == ["upgrade", "head"], "only alembic upgrade head is allowed")
        return ["docker", "compose", "--env-file", ENV_FILE, "-p", PROJECT, "-f", str(self.compose_file),
                "--profile", "migration", "run", "--rm", "--no-deps", "--name", self.prefix + "-alembic",
                "-e", "PGAPPNAME=shop_cycle_migration",
                "-e", "PGOPTIONS=-c lock_timeout=3s -c statement_timeout=600s",
                self.m["migration_service"], "alembic", *args]

    def alembic(self, args, timeout=180):
        return self.run(self.alembic_argv(args), timeout=timeout).decode().strip()

    def parent_of(self, revision):
        found = re.findall(r"(?m)^Parent:\s*(\S+)\s*$", self.alembic(["show", revision]))
        need(len(found) == 1, "migration parent is not singular")
        return found[0]

    def preflight(self):
        need(artifact(Path(__file__))["sha256"] == self.m["controller_sha256"], "controller changed")
        need(artifact(self.root / "source.tar.gz")["sha256"] == self.m["source_sha256"], "source archive changed")
        need(artifact(self.compose_file)["sha256"] == self.m["compose_sha256"], "compose file drifted")
        self.run(["bash", str(self.script("check-external-networks.sh", "network_check_sha256")),
                  "--env-file", ENV_FILE, "--compose", str(self.compose_file)])
        for key in ("app_image", "frontend_image"):
            image = self.inspect(self.m[key], True)
            need(image["Config"].get("Labels", {}).get("org.opencontainers.image.revision") == self.release, "candidate OCI revision differs")
        names = self.run(["docker", "ps", "-aq"]).decode().split()
        rows = json.loads(self.run(["docker", "inspect", *names]))
        backend = self.inspect(APPS[0])
        need(database_target(backend) == ("db", 5432, "/shop_next"), "unexpected shop database target")
        self.token_is_empty(backend)
        self.candidate_config(backend, self.m["app_image"])
        for name, expected in self.m["old"].items():
            row = self.inspect(name)
            need(row["Id"] == expected["id"] and row["Image"] == expected["image"] and row["State"]["Running"], "baseline changed: " + name)
            if name != APPS[3]:
                need(database_target(row) == ("db", 5432, "/shop_next"), "writer database mismatch")
                self.token_is_empty(row)
        same_db = {row["Id"] for row in rows if row["State"]["Running"] and "it-shop-next_private" in row["NetworkSettings"]["Networks"]
                   and database_target(row) == ("db", 5432, "/shop_next")}
        need(same_db == {self.m["old"][name]["id"] for name in (*APPS[:3], BRIDGE)}, "unlisted shop writer")
        need(self.schema() == OLD_HEAD, "baseline schema mismatch")
        need(self.cycle_tables() == {}, "cycle tables already exist")
        db = self.inspect(DB)
        need(db["State"]["Running"] and re.search(rb"PostgreSQL\) 15\.", self.run(["docker", "exec", DB, "postgres", "--version"])),
             "Postgres 15 required")
        heads = self.alembic(["heads"])
        need(heads == TARGET_HEAD + " (head)", "candidate schema is not 0029")
        need(self.parent_of(TARGET_HEAD) == self.m["intermediate_head"], "0029 parent is not the declared 0028")
        need(self.parent_of(self.m["intermediate_head"]) == OLD_HEAD, "0028 parent is not 0027")
        need(os.statvfs(self.root).f_bavail * os.statvfs(self.root).f_frsize > 20 * 1024 ** 3, "insufficient backup space")
        save(self.root / "Caddyfile.baseline", CADDYFILE.read_bytes())
        return {"containers": {row["Id"]: fingerprint(row) for row in rows}, "entry": self.entry(),
                "schema": OLD_HEAD, "db_image": db["Image"], "counts": self.counts(), "cycle_token": "empty"}

    def freeze(self):
        self.protected()
        need(self.schema() == OLD_HEAD and self.cycle_tables() == {}, "freeze schema changed")
        for name in (APPS[3], APPS[0], APPS[2], APPS[1], BRIDGE):
            expected = self.m["old"][name]
            row = self.inspect(name)
            need(row["Id"] == expected["id"] and row["Image"] == expected["image"], "writer identity drift")
            self.run(["docker", "stop", "--time", "660" if name == APPS[2] else "90", expected["id"]], timeout=700)
        self.protected()
        need(self.sql("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND client_addr IS NOT NULL") == "0",
             "unidentified database clients remain")
        return {"schema": self.schema(), "counts": self.counts(), "cycle_tables": {}}

    def restore(self, dump, head, counts, suffix):
        name = self.prefix + "-restore-" + suffix
        image = self.mark("preflight")["db_image"]
        values = env_map(self.inspect(DB))
        envfile = self.root / ("restore-" + suffix + ".env.private")
        save(envfile, ("POSTGRES_USER=" + values["POSTGRES_USER"] + "\nPOSTGRES_DB=" + values["POSTGRES_DB"]
                       + "\nPOSTGRES_PASSWORD=" + secrets.token_hex(32) + "\n").encode())
        cid = self.run(["docker", "run", "-d", "--name", name, "--network", "none", "--memory", "4g", "--cpus", "2",
                        "--pids-limit", "256", "--security-opt", "no-new-privileges",
                        "--label", "shop.cycle.release=" + self.release, "--env-file", str(envfile), image]).decode().strip()
        try:
            row = self.inspect(cid)
            need(row["HostConfig"]["NetworkMode"] == "none" and not row["HostConfig"].get("PortBindings"), "restore isolation differs")
            for _attempt in range(60):
                status = subprocess.run(["docker", "exec", cid, "pg_isready", "-h", "127.0.0.1", "-U", values["POSTGRES_USER"],
                                         "-d", values["POSTGRES_DB"]], capture_output=True)
                if status.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("restore Postgres readiness timeout")
            with dump.open("rb") as source:
                self.run(["docker", "exec", "-i", cid, "pg_restore", "--exit-on-error", "--no-owner", "--no-acl",
                          "-U", values["POSTGRES_USER"], "-d", values["POSTGRES_DB"]], stdin=source, timeout=3600)
            need(self.schema(cid) == head and self.counts(list(counts), cid) == counts, "isolated restore mismatch")
            cycles = self.cycle_tables(cid)
            if head == OLD_HEAD:
                need(cycles == {}, "old restore contains cycle tables")
            if head == TARGET_HEAD:
                need(cycles and all(value == 0 for value in cycles.values()), "new restore cycle tables are not empty")
            return {"actual_restore_performed": True, "head": head, "counts": counts, "cycle_tables": cycles,
                    "network": "none", "published_ports": [], "container_id": cid}
        finally:
            row = self.inspect(cid)
            need(row["Id"] == cid and row["Config"]["Labels"].get("shop.cycle.release") == self.release, "restore cleanup ownership differs")
            self.run(["docker", "rm", "-f", "-v", cid])

    def backup(self, suffix):
        self.mark("freeze")
        self.protected()
        expected = self.mark("freeze")["counts"]
        head = OLD_HEAD if suffix == "old" else TARGET_HEAD
        if suffix == "new":
            self.mark("migrate")
        need(self.schema() == head and self.counts(list(expected)) == expected, "backup schema/count drift")
        values = env_map(self.inspect(DB))
        dump = self.root / (suffix + ".dump")
        with dump.open("xb") as target:
            self.run(["docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on", DB, "pg_dump",
                      "-U", values["POSTGRES_USER"], "-d", values["POSTGRES_DB"], "-Fc"], stdout=target, timeout=3600)
            target.flush()
            os.fsync(target.fileno())
        need(dump.stat().st_size > 1024, "dump unexpectedly small")
        restored = self.restore(dump, head, self.counts(), suffix)
        need(self.schema() == head, "database changed during backup")
        return {"dump": artifact(dump), "restore": restored, "downgrade_invoked": False}

    def migrate(self):
        self.mark("backup-old")
        self.protected()
        before = self.schema()
        need(before == OLD_HEAD or before == self.m["intermediate_head"] or before == TARGET_HEAD, "migration starting schema differs")
        need(self.alembic(["heads"]) == TARGET_HEAD + " (head)", "candidate schema not singular")
        invoked = False
        if before != TARGET_HEAD:
            self.alembic(["upgrade", "head"], timeout=1800)
            invoked = True
        self.alembic(["check"], timeout=1800)
        need(self.schema() == TARGET_HEAD, "upgrade head did not reach 0029; not downgrading")
        need(self.counts(list(self.mark("freeze")["counts"])) == self.mark("freeze")["counts"], "migration changed baseline rows")
        cycles = self.cycle_tables()
        need(cycles and all(value == 0 for value in cycles.values()), "cycle tables are not empty after upgrade")
        return {"schema": TARGET_HEAD, "from": before, "upgrade_invoked": invoked, "downgrade_invoked": False,
                "alembic_version_rewritten": False, "upgrade_argv": ["upgrade", "head"],
                "intermediate_head": self.m["intermediate_head"], "cycle_tables_zero": True, "cycle_token": "empty"}

    def create(self):
        self.mark("backup-new")
        self.protected()
        need(self.schema() == TARGET_HEAD, "create schema differs")
        created = {}
        for name in APPS:
            expected = self.m["old"][name]
            old = self.inspect(expected["id"])
            need(not old["State"]["Running"] and old["Image"] == expected["image"], "old container is not the frozen writer")
            image = self.m["frontend_image"] if name == APPS[3] else self.m["app_image"]
            cfg, host = self.candidate_config(old, image) if name != APPS[3] else (
                {**copy.deepcopy(old["Config"]), "Image": image, "Labels": {**(old["Config"].get("Labels") or {}),
                 "org.opencontainers.image.revision": self.release, "shop.cycle.release": self.release}},
                copy.deepcopy(old["HostConfig"]))
            if name == APPS[3]:
                need(cfg.get("Env") == old["Config"].get("Env"), "frontend environment changed")
            self.token_is_empty({"Config": cfg})
            self.run(["docker", "rename", old["Id"], name + "-before-" + self.release[:12]])
            networks = {key: {"Aliases": value.get("Aliases") or []} for key, value in old["NetworkSettings"]["Networks"].items()}
            body = {**cfg, "HostConfig": host, "NetworkingConfig": {"EndpointsConfig": networks}}
            save(self.root / (name + ".create.private.json"), body)
            cid = json.loads(self.run(["curl", "-fsS", "--unix-socket", "/var/run/docker.sock", "-H", "Content-Type: application/json",
                                       "--data-binary", "@-", "http://localhost/containers/create?name=" + name],
                                      input=json.dumps(body).encode()))["Id"]
            need(re.fullmatch(r"[0-9a-f]{64}", cid), "invalid clone ID")
            row = self.inspect(cid)
            need(row["Name"] == "/" + name and not row["State"]["Running"] and row["Image"] == image, "clone identity mismatch")
            self.token_is_empty(row)
            self.preserved_env(old, env_map(row))
            save(self.root / (name + ".clone.json"), {"id": cid})
            created[name] = cid
        return {"created": created, "cycle_token": "empty"}

    def http(self, path, statuses=(200,)):
        request = Request("https://shop.ybwznt.com" + path, method="GET")
        try:
            with urlopen(request, timeout=20) as response:
                code, body = response.status, response.read()
        except HTTPError as error:
            code, body = error.code, error.read()
        need(code in statuses, "public HTTP check failed: " + path)
        if path in ("/api/health", "/api/ready"):
            need(json.loads(body).get("status") == "ok", "readiness body differs")
        return {"status": code, "sha256": sha(body)}

    def posted_cycle_rows(self):
        return sum(self.cycle_tables().values())

    def activate(self):
        created = self.mark("create")["created"]
        self.protected()
        need(self.schema() == TARGET_HEAD, "activation schema differs")
        need(self.posted_cycle_rows() == 0, "refusing to expose a database that already has cycle rows from another program")
        save(self.root / "exposure-started.json", {"release": self.release, "at": now(), "forward_only": True, "caddy_edited": False})
        self.run(["docker", "start", created[APPS[0]]])
        self.token_is_empty(self.inspect(created[APPS[0]]))
        for _attempt in range(60):
            row = self.inspect(created[APPS[0]])
            if row["State"].get("Health", {}).get("Status") == "healthy":
                break
            need(row["State"]["Running"], "candidate backend stopped")
            time.sleep(1)
        else:
            raise RuntimeError("backend health timeout; forward-only repair required")
        self.run(["docker", "start", created[APPS[3]]])
        self.run(["bash", str(self.script("rollback-networks.sh", "network_rollback_sha256")),
                  "reconnect", "--env-file", ENV_FILE, "--compose", str(self.compose_file)])
        self.http("/api/ready")
        self.http("/healthz")
        for name in (APPS[1], APPS[2]):
            self.run(["docker", "start", created[name]])
            self.token_is_empty(self.inspect(created[name]))
        self.run(["docker", "start", self.m["old"][BRIDGE]["id"]])
        self.protected()
        return {"started": {name: fingerprint(self.inspect(cid)) for name, cid in created.items()},
                "bridge": fingerprint(self.inspect(self.m["old"][BRIDGE]["id"])), "caddy_edited": False,
                "cycle_token": "empty"}

    def verify(self):
        active = self.mark("activate")
        self.protected()
        need(self.schema() == TARGET_HEAD, "live schema differs")
        cycles = self.cycle_tables()
        need(cycles and all(value == 0 for value in cycles.values()), "cycle tables changed before verification")
        for name, expected in active["started"].items():
            row = self.inspect(expected["id"])
            need(fingerprint(row) == expected and expected["running"], "live candidate changed")
            if name != APPS[3]:
                self.token_is_empty(row)
        checks = {path: self.http(path) for path in ("/api/health", "/api/ready", "/healthz")}
        return {"schema": TARGET_HEAD, "checks": checks, "forward_only": True, "caddy_edited": False,
                "downgrade_invoked": False, "cycle_token": "empty"}

    def recover_freeze(self):
        self.mark("preflight")
        need((self.root / "freeze.intent.json").exists() and not (self.root / "freeze.json").exists(), "not an incomplete freeze")
        need(not any((self.root / (name + ".intent.json")).exists() for name in ("migrate", "create", "activate"))
             and not (self.root / "exposure-started.json").exists(), "migration may have begun; forward-only")
        need(self.schema() == OLD_HEAD and self.posted_cycle_rows() == 0, "recovery schema differs or v2 rows exist")
        need(self.entry() == self.mark("preflight")["entry"], "entry changed during partial freeze")
        for name in (APPS[0], APPS[3], APPS[1], APPS[2], BRIDGE):
            row = self.inspect(self.m["old"][name]["id"])
            need(row["Id"] == self.m["old"][name]["id"] and row["Name"] == "/" + name and row["Image"] == self.m["old"][name]["image"],
                 "recovery identity differs")
            if not row["State"]["Running"]:
                self.run(["docker", "start", row["Id"]])
        deadline = time.monotonic() + 90
        checks = None
        for attempt in range(60):
            current = {}
            for name, expected in self.m["old"].items():
                row = self.inspect(expected["id"])
                need(row["Id"] == expected["id"] and row["Name"] == "/" + name and row["Image"] == expected["image"]
                     and row["State"]["Running"], "original process failed after restart: " + name)
                current[name] = row
            healthy = all(current[name]["State"].get("Health", {}).get("Status") == "healthy" for name in (APPS[0], APPS[3]))
            if healthy:
                try:
                    checks = {path: self.http(path) for path in ("/api/ready", "/healthz")}
                except Exception:
                    if attempt == 59 or time.monotonic() >= deadline:
                        raise RuntimeError("original public readiness failed after restart") from None
                else:
                    final = {name: self.inspect(self.m["old"][name]["id"]) for name in (APPS[0], APPS[3])}
                    need(all(row["State"].get("Health", {}).get("Status") == "healthy" for row in final.values()),
                         "original health changed during recovery verification")
                    break
            if attempt == 59 or time.monotonic() >= deadline:
                raise RuntimeError("original container health timeout after restart")
            time.sleep(1)
        return {"schema": OLD_HEAD, "restored_original_processes": True, "database_restored": False,
                "downgrade_invoked": False, "checks": checks}

    def reuse_intent(self, phase):
        intent = self.root / (phase + ".intent.json")
        if phase == "recover-freeze" and intent.exists():
            need(not (self.root / "recover-freeze.json").exists(), "recover-freeze already completed")
            return True
        if phase == "migrate" and intent.exists():
            need(self.schema() in {self.m["intermediate_head"], TARGET_HEAD},
                 "migrate intent exists; only continuation from 0028 or 0029 is allowed")
            return True
        if phase == "verify":
            return True
        need(not intent.exists(), "phase intent already exists")
        save(intent, {"at": now(), "phase": phase, "manifest_sha256": sha(self.raw)})
        return False

    def execute(self, phase):
        need(phase in PHASES, "unknown phase")
        need(artifact(Path(__file__))["sha256"] == self.m["controller_sha256"], "controller changed after review")
        if phase == "migrate" and (self.root / "migrate.json").exists():
            raise RuntimeError("migrate already completed")
        if phase == "recover-freeze":
            pass
        elif phase != "preflight":
            self.mark(PHASES[PHASES.index(phase) - 1])
        owner = BASE / ".shop-cycle-active-owner.json"
        if not owner.exists():
            need(phase == "preflight", "missing release owner")
            save(owner, {"release": self.release, "manifest_sha256": sha(self.raw)})
        need(not owner.is_symlink() and json.loads(owner.read_bytes()) == {"release": self.release, "manifest_sha256": sha(self.raw)},
             "other release owns shop")
        need(not (BASE / ".shop-pn-active-owner.json").is_symlink(), "shop PN owner must not be replaced by a symlink")
        self.reuse_intent(phase)
        if phase == "recover-freeze":
            result = self.recover_freeze()
        elif phase.startswith("backup-"):
            result = self.backup(phase.split("-", 1)[1])
        else:
            result = getattr(self, phase.replace("-", "_"))()
        need(result.get("downgrade_invoked") is not True, "downgrade result")
        receipt = self.root / (phase + ".json" if phase != "verify" else "verify-" + now() + ".json")
        save(receipt, {"phase": phase, "status": "PASS", "manifest_sha256": sha(self.raw), "at": now(), **result})
        print(json.dumps({"phase": phase, "status": "PASS", "receipt": str(receipt), "sha256": artifact(receipt)["sha256"]}))


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    os.umask(0o077)
    need(len(argv) == 3 and os.geteuid() == 0, "usage: shop_cycle_ops.py MANIFEST PHASE (root)")
    release = Release(argv[1])
    lock = os.open(BASE / ".shop-cycle-deploy.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        release.execute(argv[2])
    except Exception:
        log = release.root / ("failure-" + now() + ".private.log")
        try:
            save(log, traceback.format_exc().encode())
        except Exception:
            log = None
        print(json.dumps({"status": "FAILED", "private_diagnostic": None if log is None else str(log), "rollback_invoked": False}))
        return 1
    finally:
        os.close(lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
