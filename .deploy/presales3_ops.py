#!/usr/bin/env python3
"""D-0048 精确版本发布控制器。只在审阅 manifest 后由负责人逐阶段调用。

python3 presales3_ops.py /data/spareparts/releases/presales3-<40sha>/manifest.json PHASE
失败只记录私有诊断，不自动重试迁移/切流，也不自动覆盖线上业务数据。
"""
import copy
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.error import HTTPError
from urllib.request import urlopen
from urllib.parse import urlsplit

DB = "it-spareparts-db-1"
CADDY = "it-spareparts-caddy-1"
BASE = Path("/data/spareparts/releases")
HOSTS = {"ybwznt.com", "www.ybwznt.com"}
TABLES = ("maintenance_project", "maintenance_project_contract", "dim_part", "f_sales_line",
          "f_purchase_line", "f_maintenance_order", "presales_inquiry", "presales_inquiry_line",
          "presales_offer", "presales_offer_revision", "work_item")


def need(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def artifact(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return {"path": str(path), "bytes": Path(path).stat().st_size, "sha256": value.hexdigest()}


def write_new(path, data):
    if not isinstance(data, bytes):
        data = data.encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def save(path, value):
    write_new(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def fingerprint(container):
    return {"id": container["Id"], "image": container["Image"],
            "running": container["State"]["Running"], "started_at": container["State"]["StartedAt"],
            "restart_count": container["RestartCount"], "oom_killed": container["State"]["OOMKilled"]}


def site_dials(config):
    def collect(handles):
        result = []
        for handler in handles or []:
            if handler.get("handler") == "reverse_proxy":
                result.extend(row["dial"] for row in handler.get("upstreams", []) if row.get("dial"))
            for route in handler.get("routes", []):
                result.extend(collect(route.get("handle")))
        return result
    result = {}
    for server in config.get("apps", {}).get("http", {}).get("servers", {}).values():
        for route in server.get("routes", []):
            for match in route.get("match", []):
                for host in match.get("host", []):
                    result[host] = collect(route.get("handle"))
    return result


def replace_active(config, old_target, new_target):
    """只改变恰好一个完整主站路由的 upstream；其他 active 字段保留。"""
    result = copy.deepcopy(config)
    changed = 0
    def replace(handles):
        nonlocal changed
        for handler in handles or []:
            if handler.get("handler") == "reverse_proxy":
                for upstream in handler.get("upstreams", []):
                    if upstream.get("dial") == old_target:
                        upstream["dial"] = new_target
                        changed += 1
            for route in handler.get("routes", []):
                replace(route.get("handle"))
    for server in result.get("apps", {}).get("http", {}).get("servers", {}).values():
        for route in server.get("routes", []):
            hosts = {host for match in route.get("match", []) for host in match.get("host", [])}
            if hosts & HOSTS:
                need(hosts == HOSTS, "unexpected main-site host grouping")
                replace(route.get("handle"))
    need(changed == 1, "upstream replacement is not singular")
    return result


def validate_manifest(manifest, root):
    sha = manifest.get("release", "")
    need(re.fullmatch(r"[0-9a-f]{40}", sha), "invalid release SHA")
    need(root == BASE / ("presales3-" + sha), "release directory mismatch")
    need(re.fullmatch(r"[0-9a-z_]{1,32}", manifest.get("target_head", "")), "invalid target head")
    need(re.fullmatch(r"[0-9a-f]{40}", manifest.get("old_revision", "")), "explicit old revision required")
    need(re.fullmatch(r"[0-9a-z_]{1,32}", manifest.get("old_schema", "")), "explicit old schema required")
    for field in ("old_app", "old_front"):
        need(re.fullmatch(r"(?:it-spareparts|itsp-presales3)-[a-z0-9-]{1,100}", manifest.get(field, "")), "invalid old container name")
    need(manifest["old_app"] != manifest["old_front"], "old app/frontend names must differ")
    for field in ("old_app_id", "old_front_id"):
        need(re.fullmatch(r"[0-9a-f]{64}", manifest.get(field, "")), "invalid frozen container ID")
    for field in ("old_app_image", "old_front_image", "app_image", "frontend_image"):
        need(re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.get(field, "")), "invalid image ID")
    need(type(manifest.get("port")) is int and 1024 < manifest["port"] < 65536, "invalid loopback port")
    need(manifest.get("caddyfile") in {"/opt/it-spareparts/Caddyfile", "/etc/caddy/Caddyfile"}, "unknown Caddy path")
    for field in ("source_sha256", "smoke_sha256", "controller_sha256"):
        need(re.fullmatch(r"[0-9a-f]{64}", manifest.get(field, "")), "reviewed checksum required: " + field)
    need(re.fullmatch(r"[0-9a-f]{64}", manifest.get("nginx_sha256", "")), "frozen nginx checksum required")
    need(type(manifest.get("nginx_upstream_count")) is int and manifest["nginx_upstream_count"] > 0, "frozen nginx token count required")
    need(isinstance(manifest.get("additional_writers"), list), "explicit additional_writers list required")
    names = {manifest["old_app"]}
    for writer in manifest["additional_writers"]:
        need(set(writer) == {"name", "id", "image"}, "additional writer fields invalid")
        need(re.fullmatch(r"(?:it-spareparts-app|itsp-presales3)-[a-z0-9-]{1,100}", writer["name"]), "writer outside application namespace")
        need(writer["name"] not in names and re.fullmatch(r"[0-9a-f]{64}", writer["id"])
             and re.fullmatch(r"sha256:[0-9a-f]{64}", writer["image"]), "duplicate or invalid writer identity")
        names.add(writer["name"])
    network = ipaddress.ip_network(manifest.get("subnet", ""), strict=True)
    need(network.version == 4 and network.is_private and network.prefixlen >= 24, "unsafe subnet")
    return manifest


class Release:
    def __init__(self, manifest_path):
        self.path = Path(manifest_path)
        self.root = self.path.parent
        need(self.path.name == "manifest.json" and not self.path.is_symlink(), "manifest path mismatch")
        need(self.root.resolve() == self.root and self.root.is_dir() and self.root.stat().st_uid == 0
             and stat.S_IMODE(self.root.stat().st_mode) == 0o700, "release directory must be private and root-owned")
        self.manifest_bytes = self.path.read_bytes()
        self.m = validate_manifest(json.loads(self.manifest_bytes), self.root)
        self.sha = self.m["release"]
        self.old_app, self.old_front = self.m["old_app"], self.m["old_front"]
        self.prefix = "itsp-presales3-" + self.sha[:12]
        self.app, self.front = self.prefix + "-app", self.prefix + "-frontend"
        self.net = self.prefix + "-net"
        self.caddyfile = Path(self.m["caddyfile"])

    def run(self, argv, *, stdin=None, input=None, stdout=None, timeout=180, merge_stderr=False):
        result = subprocess.run(argv, stdin=stdin, input=input, stdout=stdout or subprocess.PIPE,
                                stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE, timeout=timeout)
        if result.returncode or result.stderr:
            save_path = self.root / ("command-" + stamp() + ".private.log")
            write_new(save_path, (result.stdout or b"") + b"\nSTDERR\n" + (result.stderr or b""))
        need(result.returncode == 0, "command failed; inspect private diagnostic")
        return result.stdout if stdout is None else b""

    def inspect(self, name, image=False):
        rows = json.loads(self.run(["docker", *( ["image"] if image else []), "inspect", name]))
        need(len(rows) == 1, "invalid inspect response")
        return rows[0]

    def marker(self, name):
        row = json.loads((self.root / (name + ".json")).read_text())
        need(row.get("release") == self.sha and row.get("manifest_sha256") == digest(self.manifest_bytes)
             and row.get("phase") == name and row.get("status") == "PASS", "phase receipt mismatch")
        return row

    def writers(self):
        return [{"name": self.old_app, "id": self.m["old_app_id"], "image": self.m["old_app_image"]},
                *self.m["additional_writers"]]

    @staticmethod
    def database_target(container):
        values = [v.split("=", 1)[1] for v in (container["Config"].get("Env") or []) if v.startswith("DATABASE_URL=")]
        if len(values) != 1:
            return None
        value = urlsplit(values[0])
        return value.hostname, value.port or 5432, value.path

    def nginx_baseline(self, frontend):
        mounts = [m for m in frontend["HostConfig"].get("Mounts", []) if m.get("Target") == "/etc/nginx/conf.d/default.conf"]
        need(len(mounts) == 1 and mounts[0]["ReadOnly"], "nginx mount mismatch")
        raw = Path(mounts[0]["Source"]).read_bytes()
        need(digest(raw) == self.m["nginx_sha256"], "nginx source bytes drift")
        need(raw.count((self.old_app + ":8000").encode()) == self.m["nginx_upstream_count"], "nginx source token count drift")
        return raw

    def sql(self, query, container=DB):
        return self.run(["docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=30000",
            container, "psql", "-U", "spareparts", "-d", "spareparts", "-X", "-At", "-v", "ON_ERROR_STOP=1",
            "-c", query]).decode().strip()

    def schema(self, container=DB):
        value = self.sql("SELECT version_num FROM alembic_version", container)
        need(re.fullmatch(r"[0-9a-z_]{1,32}", value), "schema has multiple/invalid heads")
        return value

    def counts(self, container=DB):
        return {table: int(self.sql('SELECT count(*) FROM public."' + table + '"', container)) for table in TABLES}

    def data_fingerprints(self, columns=None):
        """保存旧列投影的行内容摘要；新增默认列不影响迁移前后的比较。"""
        if columns is None:
            columns = json.loads(self.sql("SELECT coalesce(json_object_agg(table_name, cols), '{}') FROM "
                "(SELECT c.table_name, json_agg(c.column_name ORDER BY c.ordinal_position) cols "
                "FROM information_schema.columns c JOIN information_schema.tables t "
                "ON t.table_schema=c.table_schema AND t.table_name=c.table_name "
                "WHERE c.table_schema='public' AND t.table_type='BASE TABLE' AND c.table_name<>'alembic_version' "
                "GROUP BY c.table_name) q"))
        def ident(value):
            return '"' + value.replace('"', '""') + '"'
        result = {}
        for table, fields in sorted(columns.items()):
            query = "COPY (SELECT encode(sha256(convert_to(row_to_json(t)::text,'UTF8')),'hex') AS fingerprint FROM "
            query += "(SELECT " + ",".join(ident(f) for f in fields) + " FROM public." + ident(table) + ") t ORDER BY fingerprint) TO STDOUT"
            with tempfile.TemporaryFile(dir=self.root) as target:
                self.run(["docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=120000",
                    DB, "psql", "-U", "spareparts", "-d", "spareparts", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", query],
                    stdout=target, timeout=150)
                target.seek(0)
                value, size = hashlib.sha256(), 0
                for chunk in iter(lambda: target.read(1024 * 1024), b""):
                    value.update(chunk)
                    size += len(chunk)
            result[table] = {"columns": fields, "sha256": value.hexdigest(), "bytes": size}
        return result

    def unchanged_business(self):
        before = self.marker("freeze")["data_fingerprints"]
        need(self.data_fingerprints({k: v["columns"] for k, v in before.items()}) == before,
             "pre-existing row content changed; forward-only")
        return True

    def active(self):
        return json.loads(self.run(["docker", "exec", CADDY, "curl", "-fsS", "--max-time", "5", "http://127.0.0.1:2019/config/"]))

    def caddy_destination(self):
        matches = [m["Destination"] for m in self.inspect(CADDY)["Mounts"]
                   if m["Type"] == "bind" and m["Source"] == str(self.caddyfile)]
        need(len(matches) == 1, "Caddy mount mismatch")
        return matches[0]

    def cas(self, data, active, inode):
        st = self.caddyfile.stat()
        need([st.st_dev, st.st_ino] == inode and not self.caddyfile.is_symlink(), "Caddy inode drift")
        need(self.caddyfile.read_bytes() == data and self.active() == active, "Caddy state drift")
        inside = self.run(["docker", "exec", CADDY, "sha256sum", self.caddy_destination()]).decode().split()[0]
        need(inside == digest(data), "Caddy bind bytes drift")

    def protected(self, *, old_running=None, partial_freeze=False):
        before = self.marker("preflight")["containers"]
        writers = {row["name"] for row in self.writers()}
        for name, expected in before.items():
            current = fingerprint(self.inspect(name))
            if name in writers and old_running is not None:
                expected = {**expected, "running": old_running}
            if name in writers and partial_freeze:
                expected = {**expected, "running": current["running"]}
            need(current == expected, "protected container drift: " + name)
        return True

    def frozen(self):
        self.marker("freeze")
        self.protected(old_running=False)
        pre = self.marker("preflight")
        self.cas((self.root / "Caddyfile.baseline").read_bytes(), pre["active"], pre["inode"])
        need(not (self.root / "exposure-started.json").exists(), "release may have admitted writes; forward-only")

    def preflight(self):
        need(not self.root.is_symlink() and stat.S_IMODE(self.root.stat().st_mode) == 0o700, "release directory must be private")
        names = self.run(["docker", "ps", "-a", "--format", "{{.Names}}"]).decode().splitlines()
        need(names and not any(n.startswith(self.prefix) for n in names), "candidate resource collision")
        all_rows = json.loads(self.run(["docker", "inspect", *names]))
        need(artifact(Path(__file__))["sha256"] == self.m["controller_sha256"], "controller checksum mismatch")
        available = os.statvfs(self.root)
        need(available.f_bavail * available.f_frsize >= 20 * 1024 ** 3, "release filesystem has less than 20 GiB free")
        for name, key in ((self.old_app, "app"), (self.old_front, "front")):
            item = self.inspect(name)
            need(item["Id"] == self.m["old_" + key + "_id"] and item["Image"] == self.m["old_" + key + "_image"]
                 and item["State"]["Running"], "frozen old release drift")
            need(self.inspect(item["Image"], True)["Config"]["Labels"]["org.opencontainers.image.revision"] == self.m["old_revision"],
                 "old OCI revision drift")
            save(self.root / (name + ".inspect.private.json"), item)
        db_target = self.database_target(self.inspect(self.old_app))
        need(db_target in {("db", 5432, "/spareparts"), (DB, 5432, "/spareparts")}, "unexpected application database target")
        writer_names = {row["name"] for row in self.writers()}
        actual_writers = {row["Name"].lstrip("/") for row in all_rows if row["State"]["Running"]
            and self.database_target(row) == db_target}
        need(actual_writers == writer_names, "same-database writer inventory differs from reviewed manifest")
        for writer in self.writers():
            row = self.inspect(writer["name"])
            need(row["Id"] == writer["id"] and row["Image"] == writer["image"] and row["State"]["Running"], "writer identity drift")
            need("it-spareparts_default" in row["NetworkSettings"]["Networks"] and not row["HostConfig"].get("PortBindings"), "writer network or exposed host port drift")
        self.nginx_baseline(self.inspect(self.old_front))
        need(self.schema() == self.m["old_schema"], "old schema drift")
        need(self.sql("SELECT count(*) FROM sys_import_batch WHERE status='processing'") == "0", "active import")
        need(self.sql("SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND (wait_event_type='Lock' OR application_name ILIKE '%migration%')") == "0", "active migration/lock")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.m["port"]))
        networks = self.run(["docker", "network", "ls", "-q"]).decode().split()
        candidate = ipaddress.ip_network(self.m["subnet"])
        for row in json.loads(self.run(["docker", "network", "inspect", *networks])):
            for block in row.get("IPAM", {}).get("Config", []) or []:
                if block.get("Subnet"):
                    existing = ipaddress.ip_network(block["Subnet"])
                    need(existing.version != 4 or not candidate.overlaps(existing), "candidate subnet overlaps")
        data, active, st = self.caddyfile.read_bytes(), self.active(), self.caddyfile.stat()
        need(all(site_dials(active).get(host) == [self.old_front + ":80"] for host in HOSTS), "old live upstream drift")
        need(data.count((self.old_front + ":80").encode()) == 1, "old Caddy upstream not singular")
        inode = [st.st_dev, st.st_ino]
        self.cas(data, active, inode)
        write_new(self.root / "Caddyfile.baseline", data)
        save(self.root / "all-inspect.private.json", all_rows)
        return {"containers": {row["Name"].lstrip("/"): fingerprint(row) for row in all_rows},
            "active": active, "inode": inode, "schema": self.schema(), "counts": self.counts(),
            "db_image": self.inspect(DB)["Image"]}

    def images(self):
        self.marker("preflight")
        self.protected()
        source = artifact(self.root / "source.tar.gz")
        need(source["sha256"] == self.m["source_sha256"], "source archive checksum mismatch")
        result = {}
        for key in ("app", "frontend"):
            image = self.inspect(self.m[key + "_image"], True)
            need(image["Id"] == self.m[key + "_image"] and image["Config"]["Labels"]["org.opencontainers.image.revision"] == self.sha,
                 "candidate image revision mismatch")
            path = self.root / (key + ".image.tar")
            with path.open("xb") as target:
                self.run(["docker", "image", "save", image["Id"]], stdout=target, timeout=1800)
            result[key] = {"id": image["Id"], "archive": artifact(path)}
        return {"images": result, "source": source}

    def freeze(self):
        self.marker("images")
        self.protected()
        pre = self.marker("preflight")
        self.cas((self.root / "Caddyfile.baseline").read_bytes(), pre["active"], pre["inode"])
        need(self.schema() == pre["schema"] and self.sql("SELECT count(*) FROM sys_import_batch WHERE status='processing'") == "0", "write-freeze precondition changed")
        for writer in self.writers():
            self.run(["docker", "stop", "--time", "60", writer["id"]], timeout=90)
        self.protected(old_running=False)
        need(self.sql("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                      "AND backend_type='client backend' AND client_addr IS NOT NULL") == "0",
             "TCP database clients remain after reviewed writer freeze")
        return {"stopped_ids": [row["id"] for row in self.writers()], "schema": self.schema(), "counts": self.counts(),
                "data_fingerprints": self.data_fingerprints()}

    def recover_freeze(self):
        """stop 已发生而摘要失败时，只允许原 schema 下恢复确切旧实例。"""
        need((self.root / "freeze.intent.json").exists() and not (self.root / "freeze.json").exists(), "not an incomplete freeze")
        need(not any((self.root / name).exists() for name in
                     ("migrate.intent.json", "migrate.json", "exposure-started.json", "cutover.intent.json")),
             "migration or exposure may have begun; freeze recovery forbidden")
        pre = self.marker("preflight")
        need(self.schema() == pre["schema"] == self.m["old_schema"], "freeze recovery schema drift")
        self.cas((self.root / "Caddyfile.baseline").read_bytes(), pre["active"], pre["inode"])
        self.protected(partial_freeze=True)
        restored = []
        for writer in self.writers():
            row = self.inspect(writer["name"])
            need(row["Id"] == writer["id"] and row["Image"] == writer["image"], "freeze recovery writer identity drift")
            if not row["State"]["Running"]:
                self.run(["docker", "start", writer["id"]])
                restored.append(writer["id"])
            current = self.inspect(writer["name"])
            need(current["State"]["Running"] and current["Id"] == writer["id"] and current["Image"] == writer["image"], "writer failed to recover")
        need(self.schema() == pre["schema"], "recovery changed schema")
        self.wait_http("https://ybwznt.com/health/db")
        return {"restored_ids": restored, "schema": pre["schema"], "no_migration_or_database_restore": True}

    def restore(self, path, expected_head, expected_counts, label):
        """生产备份留在本机，以无网络/无端口的真实 PG15 隔离恢复。"""
        name = self.prefix + "-restore-" + label
        image = self.marker("preflight")["db_image"]
        version = self.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "postgres", image, "--version"])
        need(re.search(rb"PostgreSQL\) 15\.", version), "restore image is not PostgreSQL 15")
        envfile = self.root / ("restore-" + label + ".env.private")
        write_new(envfile, "POSTGRES_USER=spareparts\nPOSTGRES_DB=spareparts\nPOSTGRES_PASSWORD=" + secrets.token_hex(32) + "\n")
        cid = self.run(["docker", "run", "-d", "--name", name, "--network", "none", "--memory", "4g", "--cpus", "2",
            "--pids-limit", "256", "--security-opt", "no-new-privileges", "--label", "itsp.release=" + self.sha,
            "--env-file", str(envfile), image]).decode().strip()
        try:
            row = self.inspect(cid)
            need(row["HostConfig"]["NetworkMode"] == "none" and not row["HostConfig"]["PortBindings"], "restore network exposure")
            for attempt in range(60):
                # initdb 的临时服务器只监听 Unix socket；TCP ready 才表示最终服务器已启动。
                probe = subprocess.run(["docker", "exec", cid, "pg_isready", "-h", "127.0.0.1", "-U", "spareparts", "-d", "spareparts"], capture_output=True)
                if probe.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("restore Postgres did not become ready")
            with path.open("rb") as source:
                self.run(["docker", "exec", "-i", cid, "pg_restore", "--exit-on-error", "--no-owner", "--no-acl",
                    "-U", "spareparts", "-d", "spareparts"], stdin=source, timeout=3600)
            need(self.schema(cid) == expected_head and self.counts(cid) == expected_counts, "restored schema/count mismatch")
            return {"actual_restore_performed": True, "schema": expected_head, "counts": expected_counts,
                    "network": "none", "published_ports": [], "container_id": cid}
        finally:
            row = self.inspect(cid)
            need(row["Id"] == cid and row["Name"] == "/" + name and row["Config"]["Labels"].get("itsp.release") == self.sha,
                 "restore cleanup ownership mismatch")
            self.run(["docker", "rm", "-f", "-v", cid])

    def backup(self, label):
        self.frozen()
        expected = self.marker("freeze")["counts"]
        head = self.marker("preflight")["schema"] if label == "old" else self.m["target_head"]
        if label == "new":
            self.marker("migrate")
        need(self.schema() == head and self.counts() == expected, "backup freeze/schema drift")
        dump = self.root / (label + "-head.dump")
        with dump.open("xb") as target:
            self.run(["docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on", DB,
                "pg_dump", "-U", "spareparts", "-d", "spareparts", "-Fc"], stdout=target, timeout=3600)
            target.flush()
            os.fsync(target.fileno())
        need(dump.stat().st_size > 1024, "backup is unexpectedly small")
        with dump.open("rb") as source:
            toc = self.run(["docker", "exec", "-i", DB, "pg_restore", "--list"], stdin=source)
        write_new(self.root / (label + "-toc.private.txt"), toc)
        artifacts = {"database": artifact(dump)}
        if label == "old":
            with (self.root / "globals.private.sql").open("xb") as target:
                self.run(["docker", "exec", DB, "pg_dumpall", "-U", "spareparts", "--globals-only"], stdout=target)
            upload = next(m for m in self.inspect(self.old_app)["Mounts"] if m["Destination"] == "/app/data/raw")
            need(upload["Type"] == "bind" and upload["Source"] == "/data/spareparts/uploads", "uploads mount drift")
            with (self.root / "uploads.tar").open("xb") as target:
                self.run(["tar", "--numeric-owner", "--acls", "--xattrs", "-cpf", "-", "-C", "/data/spareparts", "uploads"], stdout=target, timeout=3600)
            with (self.root / "config.private.tar").open("xb") as target:
                self.run(["tar", "-cpf", "-", "-C", str(self.root), "manifest.json", "all-inspect.private.json",
                    "Caddyfile.baseline", "preflight.json", "globals.private.sql"], stdout=target)
            artifacts.update({name: artifact(self.root / name) for name in ("globals.private.sql", "uploads.tar", "config.private.tar")})
        restored = self.restore(dump, head, expected, label)
        need(self.counts() == expected and self.schema() == head, "production changed during backup")
        self.frozen()
        return {"artifacts": artifacts, "restore": restored}

    def alembic(self, args):
        envfile = self.root / ("alembic-" + stamp() + ".env.private")
        env = [v for v in self.inspect(self.old_app)["Config"]["Env"] if not v.startswith(("PGAPPNAME=", "PGOPTIONS="))]
        env += ["PGAPPNAME=itsp_presales3_migration", "PGOPTIONS=-c lock_timeout=5s -c statement_timeout=600s"]
        write_new(envfile, "\n".join(env) + "\n")
        result = self.run(["docker", "run", "--rm", "--name", self.prefix + "-alembic", "--network", "it-spareparts_default",
            "--env-file", str(envfile), self.m["app_image"], "alembic", *args], timeout=1800)
        write_new(self.root / ("alembic-" + stamp() + ".private.log"), result)
        return result.decode().strip()

    def migrate(self):
        self.marker("backup-old")
        self.frozen()
        need(self.schema() == self.marker("preflight")["schema"], "migration starting head drift")
        need(self.alembic(["heads"]) == self.m["target_head"] + " (head)", "candidate has unexpected/multiple heads")
        self.alembic(["upgrade", self.m["target_head"]])
        need(self.schema() == self.m["target_head"] and self.counts() == self.marker("freeze")["counts"], "migration result mismatch")
        self.alembic(["check"])
        return {"from": self.marker("preflight")["schema"], "to": self.schema(), "migration_performed": True}

    def clone(self, name, old, image, frontend=False):
        cfg, host = copy.deepcopy(old["Config"]), copy.deepcopy(old["HostConfig"])
        cfg.update(Hostname=name, Image=image)
        def provenance(key):
            return key in {"org.opencontainers.image.revision", "bg.release", "bg.parent"} or key.startswith("hotfix.")
        cfg["Labels"] = {k: v for k, v in (cfg.get("Labels") or {}).items() if not provenance(k)}
        cfg["Labels"].update({k: v for k, v in (self.inspect(image, True)["Config"].get("Labels") or {}).items() if provenance(k)})
        cfg["Labels"].update({"bg.release": self.sha, "bg.parent": old["Name"].lstrip("/")})
        host["NetworkMode"] = self.net
        if frontend:
            host["PortBindings"] = {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(self.m["port"])}]}
            mounts = [m for m in host.get("Mounts", []) if m.get("Target") == "/etc/nginx/conf.d/default.conf"]
            need(len(mounts) == 1 and mounts[0]["ReadOnly"], "nginx mount mismatch")
            raw = self.nginx_baseline(old)
            path = self.root / "nginx.conf"
            write_new(path, raw.replace((self.old_app + ":8000").encode(), (self.app + ":8000").encode()))
            mounts[0]["Source"] = str(path)
        request = {**cfg, "HostConfig": host, "NetworkingConfig": {"EndpointsConfig": {self.net: {}}}}
        path = self.root / (name + ".create.private.json")
        save(path, request)
        response = json.loads(self.run(["curl", "-fsS", "--unix-socket", "/var/run/docker.sock", "-H", "Content-Type: application/json",
            "--data-binary", "@" + str(path), "http://localhost/containers/create?name=" + name]))
        need(re.fullmatch(r"[0-9a-f]{64}", response.get("Id", "")), "clone creation response invalid")
        other_net = "it-spareparts-ingress" if frontend else "it-spareparts_default"
        self.run(["docker", "network", "connect", other_net, name])
        current = self.inspect(name)
        for group, expected in (("Config", cfg), ("HostConfig", host)):
            actual = current[group]
            need(set(actual) == set(expected), "clone field set drift")
            for field in expected:
                left, right = expected[field], actual[field]
                if group == "HostConfig" and field == "OomKillDisable":
                    left, right = bool(left), bool(right)
                need(left == right, "clone differs: " + group + "." + field)
        need(current["Image"] == image and set(current["NetworkSettings"]["Networks"]) == {self.net, other_net}, "clone identity/net drift")
        for network in current["NetworkSettings"]["Networks"].values():
            need(set(network.get("Aliases") or []) <= {name}, "clone aliases changed")
        return fingerprint(current)

    def http(self, url, statuses=(200,)):
        try:
            with urlopen(url, timeout=20) as response:
                code, body = response.status, response.read()
        except HTTPError as exc:
            code, body = exc.code, exc.read()
        need(code in statuses, "HTTP smoke failed: " + url.split("?")[0])
        if url.endswith(("/health", "/health/db")):
            value = json.loads(body)
            need(value.get("status") == "ok", "health payload failed")
            if url.endswith("/health/db"):
                need(value.get("db") == "reachable", "database health failed")
        return {"status": code, "sha256": digest(body)}

    def wait_http(self, url):
        for attempt in range(60):
            try:
                return self.http(url)
            except Exception:
                if attempt == 59:
                    raise
                time.sleep(1)

    def smoke(self, base):
        result = {path: self.http(base + path) for path in ("/health", "/health/db", "/presales/ask", "/presales/quote", "/parts")}
        for path in ("/api/presales/inquiries", "/api/presales/library?part_ids=1", "/api/parts/suggest?q=ST"):
            result[path] = self.http(base + path, (401, 403))
        hook = self.root / "readonly-smoke.py"
        need(artifact(hook)["sha256"] == self.m.get("smoke_sha256"), "reviewed readonly smoke hook required")
        raw = self.run(["docker", "exec", "-i", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e",
                       "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=30000",
                       "-e", "ITSP_SMOKE_BASE=" + (base if base.startswith("https://") else "http://127.0.0.1:8000"),
                       "-w", "/app", self.app, "python", "-B", "-"],
                       input=hook.read_bytes(), timeout=300)
        write_new(self.root / ("smoke-" + stamp() + ".private.json"), raw)
        need(json.loads(raw).get("status") == "passed", "authenticated readonly smoke failed")
        return result

    def bluegreen(self):
        self.marker("backup-new")
        self.frozen()
        self.run(["docker", "network", "create", "--label", "itsp.release=" + self.sha, "--subnet", self.m["subnet"], self.net])
        for name, old, image, front in ((self.app, self.old_app, self.m["app_image"], False), (self.front, self.old_front, self.m["frontend_image"], True)):
            frozen = json.loads((self.root / (old + ".inspect.private.json")).read_text())
            self.clone(name, frozen, image, front)
            self.run(["docker", "start", name])
        base = "http://127.0.0.1:" + str(self.m["port"])
        for attempt in range(60):
            try:
                self.http(base + "/health/db")
                break
            except Exception:
                if attempt == 59:
                    raise
                time.sleep(1)
        routes = self.smoke(base)
        self.frozen()
        return {"app": fingerprint(self.inspect(self.app)), "frontend": fingerprint(self.inspect(self.front)), "routes": routes}

    def write_caddy(self, data, inode):
        fd = os.open(self.caddyfile, os.O_WRONLY | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            need([st.st_dev, st.st_ino] == inode, "opened Caddy inode drift")
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                need(count > 0, "short Caddy write")
                view = view[count:]
            os.ftruncate(fd, len(data))
            os.fsync(fd)
        finally:
            os.close(fd)

    def cutover(self):
        candidate = self.marker("bluegreen")
        self.frozen()
        for name, key in ((self.app, "app"), (self.front, "frontend")):
            need(fingerprint(self.inspect(name)) == candidate[key], "candidate state drift")
        need(self.schema() == self.m["target_head"], "cutover schema drift")
        self.smoke("http://127.0.0.1:" + str(self.m["port"]))
        pre = self.marker("preflight")
        before = (self.root / "Caddyfile.baseline").read_bytes()
        after = before.replace((self.old_front + ":80").encode(), (self.front + ":80").encode())
        desired = replace_active(pre["active"], self.old_front + ":80", self.front + ":80")
        path = self.root / "Caddyfile.candidate"
        write_new(path, after)
        temporary = "/tmp/" + self.prefix + ".caddyfile"
        self.run(["docker", "cp", str(path), CADDY + ":" + temporary])
        self.run(["docker", "exec", CADDY, "caddy", "validate", "--config", temporary, "--adapter", "caddyfile"])
        adapted = json.loads(self.run(["docker", "exec", CADDY, "caddy", "adapt", "--config", temporary, "--adapter", "caddyfile"]))
        need(adapted == desired, "candidate Caddy adaptation changes more than the main upstream")
        self.cas(before, pre["active"], pre["inode"])
        # 此 marker 在任何外部暴露前持久化。即使 reload/SSH结果不明，也禁止旧代码自动回切。
        save(self.root / "exposure-started.json", {"release": self.sha, "at": stamp(), "forward_only": True,
            "active_before": pre["active"], "active_after": desired, "before_sha256": digest(before), "after_sha256": digest(after)})
        self.write_caddy(after, pre["inode"])
        self.run(["docker", "exec", CADDY, "caddy", "reload", "--config", self.caddy_destination(), "--adapter", "caddyfile"])
        self.cas(after, desired, pre["inode"])
        return {"active": desired, "at": stamp(), "forward_only": True}

    def verify(self):
        cut = self.marker("cutover")
        pre = self.marker("preflight")
        after = (self.root / "Caddyfile.candidate").read_bytes()
        self.cas(after, cut["active"], pre["inode"])
        need(self.schema() == self.m["target_head"], "live schema drift")
        result = {host: self.smoke("https://" + host) for host in sorted(HOSTS)}
        index = self.run(["docker", "exec", self.front, "cat", "/usr/share/nginx/html/index.html"])
        paths = ["/"] + re.findall(r'(?:src|href)="(/assets/[^\"]+)"', index.decode())
        base = "http://127.0.0.1:" + str(self.m["port"])
        for path in paths:
            expected = self.http(base + path)
            for host in HOSTS:
                need(self.http("https://" + host + path) == expected, "public static identity mismatch")
        baseline = self.marker("bluegreen")
        for name, key in ((self.app, "app"), (self.front, "frontend")):
            need(fingerprint(self.inspect(name)) == baseline[key] and baseline[key]["running"]
                 and not baseline[key]["oom_killed"] and baseline[key]["restart_count"] == 0, "candidate restart/OOM/identity drift")
            raw = self.run(["docker", "logs", "--since", self.marker("cutover")["time"], name], merge_stderr=True)
            write_new(self.root / (name + "-observe-" + stamp() + ".private.log"), raw)
            need(not re.search(rb'Traceback|\bERROR\b|" 5[0-9][0-9] ', raw), "live application error")
        self.protected(old_running=False)
        need(self.sql("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock'") == "0", "live lock waiter")
        for path in ("/api/health", "/api/ready"):
            self.http("https://shop.ybwznt.com" + path)
        return {"public": result, "static_assets_verified": len(paths), "schema": self.schema(), "forward_only": True}

    def rollback(self):
        self.frozen()
        names = self.run(["docker", "ps", "-a", "--format", "{{.Names}}"]).decode().splitlines()
        for name in (self.app, self.front):
            if name in names:
                row = self.inspect(name)
                need(row["Config"]["Labels"].get("bg.release") == self.sha, "candidate stop ownership mismatch")
                self.run(["docker", "stop", "--time", "60", row["Id"]])
        need(self.counts() == self.marker("freeze")["counts"], "business counts changed; forward-only")
        self.unchanged_business()
        # 保守要求任何候选写入尚未产生；降级不删独立报价，不恢复旧dump覆盖业务。
        need(self.sql("SELECT count(*) FROM presales_offer WHERE inquiry_id IS NULL") == "0", "standalone quotes exist; forward-only")
        old = self.marker("preflight")["schema"]
        if self.schema() != old:
            self.alembic(["downgrade", old])
        need(self.schema() == old and self.counts() == self.marker("freeze")["counts"], "rollback schema/count mismatch")
        for writer in self.writers():
            self.run(["docker", "start", writer["id"]])
            row = self.inspect(writer["name"])
            need(row["Id"] == writer["id"] and row["Image"] == writer["image"] and row["State"]["Running"], "rollback writer drift")
        self.wait_http("https://ybwznt.com/health/db")
        return {"schema": old, "restored_old_app": self.m["old_app_id"], "no_database_restore": True}

    def execute(self, phase):
        need(phase in {"preflight", "images", "freeze", "recover-freeze", "backup-old", "migrate", "backup-new", "bluegreen", "cutover", "verify", "rollback"}, "unknown phase")
        need(artifact(Path(__file__))["sha256"] == self.m["controller_sha256"], "controller changed after review")
        # 持久 owner 防止两个发布在不同阶段之间交错；不自动清除或接管别人的owner。
        owner = BASE / ".presales3-active-owner.json"
        if not owner.exists():
            need(phase == "preflight", "missing release ownership")
            save(owner, {"release": self.sha, "manifest_sha256": digest(self.manifest_bytes)})
        need(not owner.is_symlink(), "owner symlink refused")
        need(json.loads(owner.read_text()) == {"release": self.sha, "manifest_sha256": digest(self.manifest_bytes)}, "another release owns production")
        target = self.root / ((phase + "-" + stamp() if phase == "verify" else phase) + ".json")
        need(not target.exists(), "phase already completed; inspect receipt before retry")
        # intent 也是只创建一次：网络结果不明时必须先核实，不能盲目重跑。
        if phase != "verify":
            save(self.root / (phase + ".intent.json"), {"release": self.sha, "time": stamp()})
        result = self.backup(phase.split("-")[1]) if phase.startswith("backup-") else getattr(self, phase.replace("-", "_"))()
        save(target, {"release": self.sha, "manifest_sha256": digest(self.manifest_bytes), "phase": phase,
                      "status": "PASS", "time": dt.datetime.now(dt.timezone.utc).isoformat(), **result})
        print(json.dumps({"phase": phase, "status": "PASS", "receipt": str(target), "sha256": artifact(target)["sha256"]}))


def main():
    os.umask(0o077)
    need(len(sys.argv) == 3 and os.geteuid() == 0, "usage: presales3_ops.py MANIFEST PHASE (root)")
    release = Release(sys.argv[1])
    lock = os.open(BASE / ".itsp-production-deploy.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        release.execute(sys.argv[2])
    except Exception:
        path = release.root / ("failure-" + stamp() + ".private.log")
        write_new(path, traceback.format_exc())
        print(json.dumps({"status": "FAILED", "private_diagnostic": str(path)}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
