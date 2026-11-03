#!/usr/bin/env python3
"""Release the shared pool from the exact production hotfix, with a single migration."""
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import traceback
from types import ModuleType
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

OLD_SHA = "e2e335bc72bf8cb95ce72ffae5e0e8e151b6da5c"
OLD_HEAD = "a3c5e7f9b2d4"
TARGET = "e5a7c9d1f3b6"
OLD_PREFIX = "itsp-presales-hf-" + OLD_SHA[:12]
HOTFIX = Path(__file__).with_name("presales_load_hotfix_ops.py")
HOTFIX_DIGEST = "9227dc2f973185c2f4fe173250436cdada150bd8bb6bf6a4b1828c07d9e101d9"
raw = HOTFIX.read_bytes()
if HOTFIX.is_symlink() or hashlib.sha256(raw).hexdigest() != HOTFIX_DIGEST:
    raise RuntimeError("previous release controller checksum differs")
hf = ModuleType("verified_hotfix")
hf.__file__ = str(HOTFIX)
exec(compile(raw, str(HOTFIX), "exec"), hf.__dict__)
h = hf.h


def file_mode(path, mode):
    meta = path.lstat()
    return (stat.S_ISREG(meta.st_mode) and meta.st_uid == 0
            and stat.S_IMODE(meta.st_mode) == mode)


def validate(m):
    h.need(re.fullmatch("[a-f0-9]{40}", m["release"]) and m["release"] != OLD_SHA, "candidate revision")
    h.need((m["old_revision"], m["old_schema"], m["target_head"]) == (OLD_SHA, OLD_HEAD, TARGET),
           "unexpected release or migration range")
    h.need((m["old_app"], m["old_front"]) == (OLD_PREFIX + "-app", OLD_PREFIX + "-frontend"),
           "wrong production pair")
    h.need(m["additional_writers"] == [] and not ({"source_handoff", "catalog_sync"} & m.keys()),
           "unexpected writers or environment changes")
    h.need(m["caddyfile"] == "/opt/it-spareparts/Caddyfile" and type(m["port"]) is int
           and 1024 < m["port"] < 65536, "Caddy path or candidate port")
    for key in ("old_app_id", "old_front_id", "source_sha256", "controller_sha256", "smoke_sha256", "nginx_sha256"):
        h.need(re.fullmatch("[a-f0-9]{64}", m[key]), "invalid identity: " + key)
    for key in ("old_app_image", "old_front_image", "app_image", "frontend_image"):
        h.need(re.fullmatch("sha256:[a-f0-9]{64}", m[key]), "invalid image: " + key)
    h.need(type(m["nginx_upstream_count"]) is int and m["nginx_upstream_count"] > 0, "nginx upstream count")
    h.need(re.fullmatch("[a-f0-9]{64}", m["db_id"]) and re.fullmatch("sha256:[a-f0-9]{64}", m["db_image"]),
           "database container identity")
    proxies = m["proxy_identities"].values()
    h.need(sorted(p["process"] for p in proxies) == ["caddy", "cloudflared", "nginx"] and all(
        re.fullmatch("[0-9a-f]{64}", p["id"]) and re.fullmatch("sha256:[a-f0-9]{64}", p["image"])
        for p in proxies), "reviewed proxy identities required")


def db_declaration(row):
    """True = possible writer, False = explicit other database, None = no client declaration.

    Deliberately ignore the spelling of a DSN's host or port: all aliases, IPs,
    published ports, multi-host DSNs and remote forwarders for this database are in scope.
    Unknown libpq service files/keyword DSNs are not proof of a different target.
    """
    declared_other = False
    ambiguous = False
    for item in row["Config"].get("Env") or []:
        key, _, raw_value = item.partition("=")
        value = raw_value.strip()
        if key in {"PGSERVICE", "PGSERVICEFILE", "DATABASE_URL_FILE", "DATABASE_DSN_FILE"} and value:
            ambiguous = True
        if key in {"POSTGRES_DB", "PGDATABASE", "DB_NAME", "DATABASE_NAME"} and value:
            if key == "POSTGRES_DB" and row.get("Path", "").rsplit("/", 1)[-1] == "docker-entrypoint.sh" and row.get("Args") == ["postgres"]:
                continue  # In a database-server container, this variable initializes its own cluster.
            if value == "spareparts":
                return True
            if not re.fullmatch(r"[\w.-]+", value):
                ambiguous = True
            declared_other = True
        if key in {"PGHOST", "DB_HOST", "DATABASE_HOST"} and value:
            ambiguous = True
        if not (value.lower().startswith(("postgres:", "postgresql:", "postgresql+")) or
                key.endswith(("DATABASE_URL", "DATABASE_DSN", "DATABASE_URI"))):
            continue
        try:
            url = urlsplit(value)
            if url.scheme in {"sqlite", "mysql", "redis"}:
                continue
            if url.scheme.split("+")[0] not in {"postgres", "postgresql"}:
                return True
            args = parse_qs(url.query, keep_blank_values=True)
            if set(args) & {"service", "servicefile", "hostaddr", "options"}:
                return True
            names = [unquote(url.path.lstrip("/")), *args.get("dbname", []), *args.get("database", [])]
            if "spareparts" in names or not all(re.fullmatch(r"[\w.-]+", n) for n in names):
                return True
            declared_other = True
        except ValueError:
            return True
    if ambiguous:
        return True
    return False if declared_other else None


def can_reach_without_dsn(row, db, rows):
    mode = row["HostConfig"]["NetworkMode"]
    if mode.startswith("container:"):
        peer = mode.partition(":")[2]
        peers = [r for r in rows if r["Id"] == peer or r["Name"].lstrip("/") == peer]
        h.need(len(peers) == 1 and not peers[0]["HostConfig"]["NetworkMode"].startswith("container:"),
               "unknown shared network namespace")
        return can_reach_without_dsn(peers[0], db, rows)
    if mode == "none":
        return False
    if set(row["NetworkSettings"]["Networks"]) & set(db["NetworkSettings"]["Networks"]):
        return True
    bindings = db["NetworkSettings"]["Ports"].get("5432/tcp") or []
    return bool(bindings) if mode == "host" else any(b["HostIp"] not in {"127.0.0.1", "::1"} for b in bindings)


def nonwriter_proxy(row, pinned):
    name = row["Name"].lstrip("/")
    spec = pinned.get(name)
    if spec is None or spec["id"] != row["Id"] or spec["image"] != row["Image"]:
        return False
    command = [row["Path"].rsplit("/", 1)[-1], *(row.get("Args") or [])]
    executable = spec["process"]
    if executable == "nginx":
        return command[:2] == ["docker-entrypoint.sh", "nginx"]
    if executable == "cloudflared":
        return command[0] == "cloudflared" and "tunnel" in command[1:]
    return executable == "caddy" and command[:2] == ["caddy", "run"]


IMPACT = """SELECT json_build_object(
  'restricted_submitted_inquiries', (SELECT count(*) FROM presales_inquiry i JOIN audience_rule r
      ON r.id=i.offer_audience_rule_id WHERE i.submitted_at IS NOT NULL AND i.status <> 'draft' AND r.mode<>'public'),
  'restricted_offer_or_field_rules', (SELECT count(DISTINCT r.id) FROM audience_rule r
      JOIN (SELECT supplier_audience_rule_id AS id FROM presales_offer UNION SELECT reporter_audience_rule_id
      FROM presales_offer UNION SELECT notes_audience_rule_id FROM presales_offer UNION SELECT offer_audience_rule_id
      FROM presales_offer UNION SELECT notes_audience_rule_id FROM presales_inquiry UNION SELECT notes_audience_rule_id
      FROM presales_inquiry_line UNION SELECT rule_id FROM field_value WHERE subject_type='presales_inquiry') refs
      ON refs.id=r.id WHERE r.mode<>'public'),
  'open_tasks', (SELECT count(*) FROM work_item WHERE subject_type IN ('presales_inquiry','presales_offer_notice')
      AND status IN ('pending','in_progress')),
  'cancelled_marked', (SELECT count(*) FROM work_item WHERE status='cancelled' AND last_reason LIKE 'D-0052|%'),
  'draft_offers', (SELECT count(*) FROM presales_offer WHERE status='draft'),
  'draft_responses', (SELECT count(*) FROM presales_line_response WHERE is_draft))"""


class Release(hf.Hotfix):
    def __init__(self, manifest):
        self.path = Path(manifest)
        self.root = self.path.parent
        h.need(self.path.name == "manifest.json" and file_mode(self.path, 0o600), "root-private manifest required")
        h.need(self.root.resolve() == self.root and self.root.is_dir() and self.root.stat().st_uid == 0
               and stat.S_IMODE(self.root.stat().st_mode) == 0o700, "root-private directory required")
        self.manifest_bytes = self.path.read_bytes()
        self.m = json.loads(self.manifest_bytes)
        validate(self.m)
        self.sha = self.m["release"]
        h.need(self.root == h.BASE / ("presales-pool-" + self.sha), "release directory mismatch")
        for filename, digest in ((Path(__file__), self.m["controller_sha256"]),
                                 (self.root / "readonly-smoke.py", self.m["smoke_sha256"])):
            h.need(file_mode(filename, 0o600) and h.artifact(filename)["sha256"] == digest, "artifact drift")
        self.old_app, self.old_front = self.m["old_app"], self.m["old_front"]
        self.prefix = "itsp-pool-" + self.sha[:12]
        self.app, self.front, self.net = self.prefix + "-app", self.prefix + "-frontend", self.prefix + "-net"
        self.caddyfile = Path(self.m["caddyfile"])

    def writers_exact(self, allowed):
        db = self.inspect(h.DB)
        h.need(db["Id"] == self.m["db_id"] and db["Image"] == self.m["db_image"], "database identity drift")
        names = self.run(["docker", "ps", "-q"]).decode().split()
        rows = json.loads(self.run(["docker", "inspect", *names]))
        actual = set()
        for row in rows:
            if row["Id"] == db["Id"]:
                continue
            declaration = db_declaration(row)
            if declaration is True or (declaration is None and can_reach_without_dsn(row, db, rows)
                                       and not nonwriter_proxy(row, self.m["proxy_identities"])):
                actual.add(row["Name"].lstrip("/"))
        h.need(actual == allowed, "unreviewed database writer or writer missing")

    def impact(self):
        return json.loads(self.sql(IMPACT))

    def preflight(self):
        self.writers_exact({self.old_app})
        result = super().preflight()
        h.need(self.alembic(["heads"]) == TARGET + " (head)", "release has unrelated/multiple migration heads")
        history = self.alembic(["show", TARGET])
        h.need(re.search(r"(?m)^Parent: " + OLD_HEAD + r"\s*$", history), "candidate includes an intermediate migration")
        h.need(self.sql("SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND "
                        "(wait_event_type='Lock' OR application_name ILIKE '%migration%')") == "0",
               "existing migration/lock")
        result["impact"] = self.impact()
        h.need(result["impact"]["cancelled_marked"] == 0, "preexisting pool migration marks")
        return result

    def freeze(self):
        self.writers_exact({self.old_app})
        result = super().freeze()
        self.writers_exact(set())
        result["impact"] = self.impact()
        h.need(result["impact"]["cancelled_marked"] == 0, "unexpected cancellation marker")
        return result

    def frozen(self):
        super().frozen()
        candidate_allowed = set()
        if (self.root / "bluegreen.intent.json").exists():
            names = self.run(["docker", "ps", "--format", "{{.Names}}"]).decode().splitlines()
            if self.app in names:
                self.marker("backup-new")
                row = self.inspect(self.app)
                h.need(row["Image"] == self.m["app_image"] and row["Config"]["Labels"].get("bg.release") == self.sha,
                       "candidate identity before cutover")
                candidate_allowed.add(self.app)
        self.writers_exact(candidate_allowed)
        if not candidate_allowed:
            self.source_tcp_zero()

    def migrate(self):
        backup = self.marker("backup-old")
        h.need(backup["restore"]["actual_restore_performed"] and backup["restore"]["schema"] == OLD_HEAD,
               "verified frozen old-head restore required")
        self.frozen()
        self.writers_exact(set())
        self.source_tcp_zero()
        h.need(self.schema() == OLD_HEAD, "old schema drift before migration")
        h.need(self.alembic(["heads"]) == TARGET + " (head)", "release-only head required")
        self.alembic(["upgrade", "head"])
        h.need(self.schema() == TARGET and self.counts() == self.marker("freeze")["counts"], "migration result")
        self.alembic(["check"])
        impact = self.impact()
        old = self.marker("freeze")["impact"]
        h.need(impact["cancelled_marked"] == old["open_tasks"] and impact["open_tasks"] == 0
               and all(impact[k] == old[k] for k in old if k not in {"cancelled_marked", "open_tasks"}),
               "unexpected pool migration impact")
        return {"old_schema": OLD_HEAD, "schema": TARGET, "impact": impact}

    def bluegreen(self):
        self.marker("backup-new")
        self.writers_exact(set())
        return super().bluegreen()

    def cutover(self):
        self.writers_exact({self.app})
        h.need(self.schema() == TARGET, "schema drift before cutover")
        return super().cutover()

    def smoke(self, base):
        result = {path: self.http(base + path) for path in ("/health", "/health/db", "/presales/pool")}
        for path in ("/api/presales/pool/counts", "/api/presales/pool/lines?tab=all&page_size=1"):
            result[path] = self.http(base + path, (401, 403))
        hook = (self.root / "readonly-smoke.py").read_bytes()
        h.need(h.digest(hook) == self.m["smoke_sha256"], "readonly smoke hook drift")
        raw = self.run(["docker", "exec", "-i", "-e", "PGOPTIONS=-c default_transaction_read_only=on",
                        "-w", "/app", self.app, "python", "-B", "-"], input=hook)
        check = json.loads(raw)
        h.need(check.get("status") == "passed" and check.get("mode") == "readonly_database_projection",
               "pool projection failed")
        result["projection"] = check
        return result

    def verify(self):
        self.writers_exact({self.app})
        result = super().verify()
        root = "/usr/share/nginx/html"
        paths = self.run(["docker", "exec", self.front, "grep", "-rlF", "1.44.0", root + "/assets"]).decode().splitlines()
        phrase = "询价报价"
        for path in paths:
            h.need(re.fullmatch(re.escape(root) + r"/assets/[A-Za-z0-9_.-]+", path), "unexpected asset path")
            bundle = self.run(["docker", "exec", self.front, "cat", path])
            escaped = json.dumps(phrase, ensure_ascii=True)[1:-1].encode()
            if phrase.encode() not in bundle and escaped not in bundle.lower():
                continue
            for host in sorted(h.HOSTS):
                h.need(self.http("https://" + host + path[len(root):])["sha256"] == h.digest(bundle),
                       "public changelog bundle differs")
            result["published_changelog"] = {"version": "1.44.0", "asset_sha256": h.digest(bundle)}
            return result
        raise RuntimeError("verified changelog bundle missing")

    def freeze_for_rollback(self):
        """Only the manifest's exact candidate may be stopped; check the live Caddy state before any mutation."""
        pre = self.marker("preflight")
        h.need((self.root / "freeze.intent.json").is_file(), "no freeze was attempted")
        before = (self.root / "Caddyfile.baseline").read_bytes()
        after = before.replace((self.old_front + ":80").encode(), (self.front + ":80").encode())
        active = self.active()
        data = self.caddyfile.read_bytes()
        h.need(data in (before, after) and active in (pre["active"], h.replace_active(
            pre["active"], self.old_front + ":80", self.front + ":80")), "unknown Caddy state")
        self.cas(data, active, pre["inode"])
        old = self.inspect(self.old_app)
        h.need(old["Id"] == self.m["old_app_id"] and old["Image"] == self.m["old_app_image"], "old app changed")
        old_front = self.inspect(self.old_front)
        h.need(old_front["Id"] == self.m["old_front_id"] and old_front["Image"] == self.m["old_front_image"]
               and old_front["State"]["Running"], "old front changed")
        ports = old_front["HostConfig"]["PortBindings"].get("80/tcp")
        h.need(len(ports or []) == 1 and ports[0]["HostIp"] == "127.0.0.1"
               and re.fullmatch("[0-9]{1,5}", ports[0]["HostPort"]), "unknown old frontend health endpoint")
        names = self.run(["docker", "ps", "-a", "--format", "{{.Names}}"]).decode().splitlines()
        allowed = {self.old_app} if old["State"]["Running"] else set()
        candidate = self.inspect(self.app) if self.app in names else None
        if candidate:
            h.need(candidate["Image"] == self.m["app_image"] and candidate["Config"]["Labels"].get("bg.release") == self.sha,
                   "rollback candidate identity drift")
            if candidate["State"]["Running"]:
                allowed.add(self.app)
        self.writers_exact(allowed)
        head = self.schema()
        h.need(head in (OLD_HEAD, TARGET) and not (old["State"]["Running"] and head != OLD_HEAD), "wrong rollback head")
        if candidate and candidate["State"]["Running"]:
            self.run(["docker", "stop", "--time", "60", candidate["Id"]], timeout=90)
        if not old["State"]["Running"]:
            self.writers_exact(set())
            self.source_tcp_zero()
        return pre, old, data, active, ports[0]["HostPort"]

    def downgrade(self):
        """A fresh snapshot keeps post-cutover data; downgrade never reloads an older dump."""
        if (self.root / "rollback-guard.json").exists():
            guard = self.rollback_guard()
            self.check_business(guard)
            self.writers_exact(set())
            self.source_tcp_zero()
            self.alembic(["downgrade", OLD_HEAD])
            return
        counts = self.counts()
        suffix = "rollback-" + h.stamp().lower()
        dump = self.root / (suffix + ".dump")
        with dump.open("xb") as stream:
            self.run(["docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on", h.DB,
                      "pg_dump", "-U", "spareparts", "-d", "spareparts", "-Fc"], stdout=stream, timeout=3600)
            stream.flush()
            os.fsync(stream.fileno())
        restore = self.restore(dump, TARGET, counts, suffix)
        h.need(restore["actual_restore_performed"], "current head backup restore failed")
        h.save(self.root / (suffix + "-backup.json"), {"artifact": h.artifact(dump), "restore": restore})
        tables = ("presales_offer", "presales_offer_revision", "presales_inquiry", "presales_inquiry_line",
                  "presales_line_response", "presales_offer_line_link", "subject_event")
        projections = {}
        for table in tables:
            projections[table] = json.loads(self.sql("SELECT json_agg(column_name ORDER BY ordinal_position) FROM "
                "information_schema.columns WHERE table_schema='public' AND table_name='" + table + "'"))
        projections["presales_offer"].remove("updated_by")
        for name in ("answer_state", "current_offer_id", "answer_reason", "answered_by", "answered_at"):
            projections["presales_inquiry_line"].remove(name)
        before = self.data_fingerprints(projections)
        h.save(self.root / "rollback-guard.json", {"release": self.sha, "manifest_sha256": h.digest(self.manifest_bytes),
               "columns": projections, "fingerprints": before, "counts": counts, "backup_receipt": suffix + "-backup.json"})
        self.writers_exact(set())
        self.source_tcp_zero()
        self.alembic(["downgrade", OLD_HEAD])

    def rollback_guard(self):
        guard = json.loads((self.root / "rollback-guard.json").read_bytes())
        h.need(guard["release"] == self.sha and guard["manifest_sha256"] == h.digest(self.manifest_bytes),
               "rollback guard identity mismatch")
        return guard

    def check_business(self, guard):
        h.need(self.counts() == guard["counts"] and self.data_fingerprints(guard["columns"]) == guard["fingerprints"],
               "downgrade or concurrent writer changed preserved business records")

    def verify_downgrade(self):
        verified = self.root / "rollback-schema-verified.json"
        if verified.exists():
            h.need(json.loads(verified.read_bytes()) == {"release": self.sha, "manifest_sha256": h.digest(self.manifest_bytes)},
                   "rollback verification identity mismatch")
            return
        if (self.root / "rollback-guard.json").exists():
            self.check_business(self.rollback_guard())
            h.need(self.impact()["cancelled_marked"] == 0, "migration work-item markers remain")
            h.save(verified, {"release": self.sha, "manifest_sha256": h.digest(self.manifest_bytes)})
        else:
            h.need(not (self.root / "migrate.json").exists(), "completed migration lacks downgrade verification")

    def rollback(self):
        pre, old, data, active, port = self.freeze_for_rollback()
        if self.schema() == TARGET:
            self.downgrade()
        h.need(self.schema() == OLD_HEAD, "cannot restore old application with wrong schema")
        self.verify_downgrade()
        if not old["State"]["Running"]:
            self.run(["docker", "start", old["Id"]])
        self.wait_http("http://127.0.0.1:" + port + "/health/db")
        self.cas(data, active, pre["inode"])
        original = (self.root / "Caddyfile.baseline").read_bytes()
        if data != original:
            self.write_caddy(original, pre["inode"])
        if active != pre["active"]:
            self.load_active(pre["active"])
        self.cas(original, pre["active"], pre["inode"])
        self.wait_http("https://ybwznt.com/health/db")
        return {"schema": OLD_HEAD, "old_app": old["Id"], "restored_dump_into_production": False}

    def execute(self, phase):
        h.need(phase in {"preflight", "images", "freeze", "backup-old", "migrate", "backup-new",
                         "bluegreen", "cutover", "verify", "rollback", "recover"}, "unknown phase")
        owner = h.BASE / ".presales-pool-active-owner.json"
        identity = {"release": self.sha, "manifest_sha256": h.digest(self.manifest_bytes)}
        if not owner.exists():
            h.need(phase == "preflight", "no matching preparation")
            h.save(owner, identity)
        h.need(file_mode(owner, 0o600) and json.loads(owner.read_bytes()) == identity, "release owner mismatch")
        # Recovery must remain available after arbitrarily many interrupted attempts.
        # Each mutation still has its own non-overwritable intent and immutable outcome.
        suffix = phase + ("-" + h.stamp() + "-" + uuid.uuid4().hex if phase in {"verify", "recover"} else "")
        h.save(self.root / (suffix + ".intent.json"), {**identity, "phase": phase, "time": h.stamp()})
        if phase in {"backup-old", "backup-new"}:
            result = self.backup(phase.split("-")[1])
        elif phase == "recover":
            result = self.rollback()
        else:
            result = getattr(self, phase)()
        receipt = self.root / (suffix + ".json")
        h.save(receipt, {**identity, "phase": phase, "status": "PASS", "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                         **result})
        print(json.dumps({"phase": phase, "status": "PASS", "receipt": str(receipt), "sha256": h.artifact(receipt)["sha256"]}))


if __name__ == "__main__":
    os.umask(0o077)
    h.need(os.geteuid() == 0 and len(sys.argv) == 3, "usage: presales_pool_ops.py /absolute/manifest.json PHASE")
    release = Release(sys.argv[1])
    for name in (".catalog-source-handoff.lock", ".itsp-production-deploy.lock"):
        fd = os.open(h.BASE / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        release.execute(sys.argv[2])
    except Exception:
        log = release.root / ("failure-" + h.stamp() + ".private.log")
        h.write_new(log, traceback.format_exc())
        print(json.dumps({"status": "FAILED", "diagnostic": str(log)}))
        sys.exit(1)
