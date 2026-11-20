#!/usr/bin/env python3
"""Migration cutover for the repair-pool release ccefa17 (APP_VERSION 1.47.0).

This is a new controller. It does not retarget requirements_successor_ops.py
(no-op migrate, held at 98af) or requirements_visibility_ops.py (e5a7 -> 98af).
The upgrade sequence follows the visibility controller: prove the network-none
restore of the old head, compare frozen business rows, run alembic, check, and
compare those rows again. The command is ``alembic upgrade head`` and the only
accepted path is 98afc0d2157b -> 86f7486b9ed5 -> c3e8a1d94b27.

Successor lessons kept here: Mounts are compared in stable order. A live
container with fewer than two mounts has only one order, so dry-run accepts
matching samples and proves multi-mount normalization with a synthetic fixture
instead of requiring another live mount. ``alembic current`` may include
``(mergepoint)`` on 98af, the main Caddy route may carry ``terminal: true``, a
live dry-run must pass before freeze, exposure-started.json is written before
the Caddyfile edit, and cutover never POSTs /load. After exposure the controller
does not roll back. Downgrade and alembic_version rewrites are refused.
``docker compose down`` of the shared it-spareparts project is refused.
"""
import copy
import datetime as dt
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import traceback
import uuid

APP_SHA = "ccefa17cc6446f613b7237ac0cd2121256969622"
PARENT = "b2650202c2adb9c40c8fef4663e3472963ef534a"
TREE = "a3ad9d9b6f6e6f9cccab207756504553b55b98ff"
APP_VERSION = "1.47.0"
OLD_SHA = "e62cd60cdbcc3fef896c5289388250057c4ce513"
OLD_SCHEMA = "98afc0d2157b"
MID_SCHEMA = "86f7486b9ed5"
TARGET = "c3e8a1d94b27"
OLD_PREFIX = "itsp-successor-" + OLD_SHA[:12]
CYCLE_URL = "https://shop.ybwznt.com/api/integrations/cycle/sell-intents"
CYCLE_KEYS = ("SHOP_CYCLE_SELL_ENABLED", "SHOP_CYCLE_SELL_URL", "SHOP_CYCLE_SELL_TOKEN")
DAILY_BACKUP_DIR = Path("/data/backups/spareparts")
LEGACY_BACKUP_DIR = Path("/var/backups/spareparts")
PREDECESSOR_OWNER = ".requirements-successor-active-owner.json"
OWNER_NAME = ".cycle-pool-active-owner.json"
VISIBILITY_DIGEST = "6b2fe7287242ff6ce5dfcd785613877ef49dd80f13fb8ea15c4d244e93d37e9d"
SUCCESSOR_DIGEST = "2b2aa5ca2ca305b9f42574b50f505199169919323eea9d7c80ed90e3f19e2d1e"
HELPER_DIGEST = "054e67d143a808e5672eea716573ac23a4ecf47ae6cd9a762a89901acfff5f16"
SMOKE_DIGEST = "8b942cbbd5fcb70517f84647af6ad25de72a621ccc7c7befd3ad55191685d7d5"
DRY_RUN_DIGEST = "5dd23aa813980e9c20cf96db7a3d6876c19c94daed11f86847d3b19a2f8ca131"
LOCKS = (".catalog-source-handoff.lock", ".itsp-production-deploy.lock")
GATES = (
    "required_ci", "independent_review", "candidate_regression", "scope_review",
    "migration_chain", "git_byte_archive",
)
PHASES = (
    "preflight", "images", "dry-run", "freeze", "backup-old", "migrate",
    "backup-new", "bluegreen", "cutover", "verify",
)
EXTRAS = {"recover", "enable-outbound", "disable-outbound"}
FORBIDDEN = {"source_handoff", "catalog_sync", "import_gate_allowlist"}
FORBIDDEN_ARCHIVE = {
    ".deploy/cycle_pool_ops.py", ".deploy/cycle_pool_dry_run.py", ".deploy/cycle_pool_smoke.py",
    ".deploy/shop_cycle_ops.py",
}
NEW_CYCLE_TABLES = (
    "maintenance_repair_record", "maintenance_repair_record_source",
    "maintenance_repair_command", "maintenance_cycle_sell_intent",
)
CYCLE_PAGE_KEYS = (
    "page_cycle_recycle", "page_cycle_test", "page_cycle_repair",
    "page_cycle_manage", "page_cycle_wip",
)
# 86f7486b9ed5._cycle_page_keys_false 会改这几列。其余列仍按冻结摘要逐字节比较。
PERMISSION_COLUMNS = {
    "sys_role_template": ("permissions",),
    "sys_user": ("template_perms", "perm_overrides", "permissions"),
}
LEGACY_CYCLE_TABLES = ("maintenance_cycle_unit", "maintenance_cycle_event", "maintenance_cycle_sell_intent")

VISIBILITY_PATH = Path(__file__).with_name("requirements_visibility_ops.py")
SUCCESSOR_PATH = Path(__file__).with_name("requirements_successor_ops.py")
SMOKE_PATH = Path(__file__).with_name("cycle_pool_smoke.py")
DRY_RUN_PATH = Path(__file__).with_name("cycle_pool_dry_run.py")


def _pinned(path, digest):
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise RuntimeError("reviewed controller artifact differs: " + path.name)


_pinned(VISIBILITY_PATH, VISIBILITY_DIGEST)
_pinned(SUCCESSOR_PATH, SUCCESSOR_DIGEST)
_pinned(SMOKE_PATH, SMOKE_DIGEST)
_pinned(DRY_RUN_PATH, DRY_RUN_DIGEST)
_spec = importlib.util.spec_from_loader("requirements_visibility_reviewed", loader=None)
vis = importlib.util.module_from_spec(_spec)
vis.__file__ = str(VISIBILITY_PATH)
exec(compile(VISIBILITY_PATH.read_bytes(), str(VISIBILITY_PATH), "exec"), vis.__dict__)
if vis.TARGET != "98afc0d2157b" or vis.OLD_HEAD != "e5a7c9d1f3b6":
    raise RuntimeError("historical visibility constants changed")
if vis.POOL_DIGEST != "47b141a4127c349c2bb2d785ede3c11645de9d44c4ebd4accc308f9a76e54a39":
    raise RuntimeError("historical pool digest changed")
h = vis.h
file_mode = vis.pool.file_mode


def refuse_compose_down(argv):
    tokens = [str(item) for item in argv]
    if "down" in tokens and ({"compose", "docker-compose"} & set(tokens)):
        raise RuntimeError("compose down refused")
    if "down" in tokens and any(token == "it-spareparts" or token.startswith("it-spareparts") for token in tokens):
        raise RuntimeError("it-spareparts compose down refused")


def environment_file_labels(labels):
    found = []
    for key in labels or {}:
        if key == "environment_file" or key.endswith("environment_file"):
            found.append(key)
    return found


def env_map(values):
    result = {}
    for entry in values or []:
        key, separator, value = entry.partition("=")
        if separator != "=" or key in result:
            raise RuntimeError("invalid runtime environment entry")
        result[key] = value
    return result


def archive_path_refused(name):
    if not isinstance(name, str) or name in {"", ".", "/"} or name.startswith("/") or "\\" in name or "\x00" in name:
        return True
    return ".." in Path(name).parts


def inspect_archive(data):
    h.need(isinstance(data, bytes) and data.startswith(b"\x1f\x8b"), "source archive is not gzip")
    version = None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            comment = archive.pax_headers.get("comment", "")
            for member in archive:
                name = member.name.rstrip("/")
                h.need(not archive_path_refused(name), "archive path refused")
                if member.islnk() or member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                    raise RuntimeError("special archive member refused")
                if name in FORBIDDEN_ARCHIVE:
                    raise RuntimeError("controller leaked into application archive")
                if name == "frontend/src/version.ts" and member.isfile():
                    stream = archive.extractfile(member)
                    h.need(stream is not None, "version source missing")
                    version = stream.read()
    except (tarfile.TarError, OSError):
        raise RuntimeError("source archive is not a git tar.gz") from None
    h.need(comment == APP_SHA, "source archive is not the pinned release commit")
    found = re.findall(br'(?m)^export const APP_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)";\n?$', version or b"")
    h.need(found == [APP_VERSION.encode()], "source archive version differs")
    return {"comment": comment, "app_version": APP_VERSION}


def git_commit_identity(repo):
    def git(*args):
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
        h.need(result.returncode == 0, "git identity failed")
        return result.stdout.decode().strip()

    commit = git("rev-parse", "--verify", APP_SHA + "^{commit}")
    parents = tuple(part for part in git("rev-parse", APP_SHA + "^@").split() if part)
    tree = git("rev-parse", APP_SHA + "^{tree}")
    names = set(git("ls-tree", "-r", "--name-only", APP_SHA).splitlines())
    h.need(FORBIDDEN_ARCHIVE.isdisjoint(names), "controller leaked into application commit")
    return {"commit": commit, "parents": parents, "tree": tree}


def check_archive(repo, archive):
    data = Path(archive).read_bytes()
    inspected = inspect_archive(data)
    identity = git_commit_identity(repo)
    h.need(identity["commit"] == APP_SHA and identity["parents"] == (PARENT,) and identity["tree"] == TREE,
           "frozen commit identity differs")
    return {"status": "passed", "release": APP_SHA, "parent": PARENT, "tree": TREE,
            "app_version": inspected["app_version"], "controller_in_archive": False, "migration_performed": False}


def validate(m):
    h.need(m.get("release") == APP_SHA and m.get("old_revision") == OLD_SHA, "release identity")
    h.need(m.get("release") not in {PARENT, OLD_SHA} and not str(m.get("release", "")).startswith("7e5f15e5"),
           "branch head or predecessor was selected")
    h.need(m.get("old_schema") == OLD_SCHEMA and m.get("target_head") == TARGET and m.get("mid_schema") == MID_SCHEMA,
           "migration range")
    h.need(m.get("upgrade_path") == [OLD_SCHEMA, MID_SCHEMA, TARGET], "upgrade path")
    h.need((m.get("old_app"), m.get("old_front")) == (OLD_PREFIX + "-app", OLD_PREFIX + "-frontend"), "production pair")
    h.need(m.get("additional_writers") == [] and not (FORBIDDEN & set(m)), "writer or catalog changes")
    h.need(m.get("caddyfile") == "/opt/it-spareparts/Caddyfile" and type(m.get("port")) is int and 1024 < m["port"] < 65536,
           "endpoint")
    h.need(m.get("downgrade_allowed") is False and m.get("compose_down_allowed") is False, "rollback policy")
    for key in ("old_app_id", "old_front_id", "source_sha256", "controller_sha256", "smoke_sha256", "nginx_sha256",
                "predecessor_owner_sha256", "evidence_sha256", "db_id", "visibility_sha256", "successor_sha256",
                "pool_sha256", "helper_sha256", "dry_run_sha256", "cycle_sell_token_sha256"):
        h.need(re.fullmatch(r"[a-f0-9]{64}", str(m.get(key, ""))), "invalid identity: " + key)
    h.need(m["visibility_sha256"] == VISIBILITY_DIGEST and m["successor_sha256"] == SUCCESSOR_DIGEST, "historical controllers")
    h.need(m["pool_sha256"] == vis.POOL_DIGEST and m["helper_sha256"] == HELPER_DIGEST, "helper chain")
    h.need(m["smoke_sha256"] == SMOKE_DIGEST and m["dry_run_sha256"] == DRY_RUN_DIGEST, "smoke or dry-run harness")
    for key in ("old_app_image", "old_front_image", "app_image", "frontend_image", "db_image"):
        h.need(re.fullmatch(r"sha256:[a-f0-9]{64}", str(m.get(key, ""))), "invalid image: " + key)
    h.need(type(m.get("nginx_upstream_count")) is int and m["nginx_upstream_count"] > 0, "nginx upstream count")
    proxies = m.get("proxy_identities")
    h.need(isinstance(proxies, dict) and sorted(item.get("process") for item in proxies.values()) == ["caddy", "cloudflared", "nginx"]
           and all(re.fullmatch(r"[0-9a-f]{64}", str(item.get("id", ""))) and re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("image", "")))
                   for item in proxies.values()), "reviewed proxies required")
    try:
        import ipaddress
        network = ipaddress.ip_network(m.get("subnet"), strict=True)
    except (TypeError, ValueError):
        network = None
    h.need(network is not None and network.version == 4 and network.is_private and 24 <= network.prefixlen <= 28,
           "private subnet required")


def header_etag(headers):
    etag = None
    for key, value in headers.items():
        if key.lower() == "etag":
            etag = value
    h.need(isinstance(etag, str) and etag.strip(), "caddy etag missing")
    return etag.strip()


def parse_curl_exchange(raw):
    h.need(isinstance(raw, bytes) and b"\n__STATUS__" in raw, "caddy admin response incomplete")
    message, _, status_raw = raw.rpartition(b"\n__STATUS__")
    h.need(re.fullmatch(rb"[0-9]{3}", status_raw.strip()), "caddy admin status missing")
    separator = b"\r\n\r\n" if b"\r\n\r\n" in message else b"\n\n"
    h.need(separator in message, "caddy admin headers missing")
    header_blob, body = message.split(separator, 1)
    headers = {}
    for line in header_blob.splitlines()[1:]:
        if b":" not in line:
            continue
        key, value = line.split(b":", 1)
        headers[key.decode().lower()] = value.decode().strip()
    return int(status_raw.strip()), headers, body


def collect_dials(node, found):
    if isinstance(node, dict):
        if "dial" in node and "handler" not in node:
            found.append(node.get("dial"))
        for value in node.values():
            collect_dials(value, found)
    elif isinstance(node, list):
        for item in node:
            collect_dials(item, found)


def main_matcher_hosts(route):
    match = route.get("match")
    if not isinstance(match, list) or not match:
        return None
    hosts = set()
    for matcher in match:
        if not isinstance(matcher, dict) or set(matcher) != {"host"}:
            return None
        values = matcher.get("host")
        if not isinstance(values, list) or not values:
            return None
        for host in values:
            if not isinstance(host, str) or host not in h.HOSTS:
                return None
            hosts.add(host)
    return hosts if hosts == set(h.HOSTS) else None


def route_is_main(route, dial):
    if not isinstance(route, dict) or not {"match", "handle"} <= set(route):
        return False
    if set(route) - {"match", "handle", "terminal"}:
        return False
    if "terminal" in route and route["terminal"] is not True:
        return False
    if main_matcher_hosts(route) is None:
        return False
    dials = []
    collect_dials(route, dials)
    return dials == [dial]


def replace_one_dial(node, old, new):
    count = 0
    if isinstance(node, dict):
        if "dial" in node and "handler" not in node and node.get("dial") == old:
            node["dial"] = new
            count += 1
        for value in node.values():
            count += replace_one_dial(value, old, new)
    elif isinstance(node, list):
        for item in node:
            count += replace_one_dial(item, old, new)
    return count


def find_main_route_path(config, dial):
    found = []
    servers = ((config.get("apps") or {}).get("http") or {}).get("servers") or {}
    for name, server in servers.items():
        h.need(re.fullmatch(r"[A-Za-z0-9_.-]+", str(name)), "caddy server name refused")
        for index, route in enumerate((server or {}).get("routes") or []):
            if route_is_main(route, dial):
                found.append("/config/apps/http/servers/%s/routes/%s" % (name, index))
    h.need(len(found) == 1, "main route is not singular")
    return found[0]


def config_at(config, pointer):
    node = config
    for part in pointer[len("/config/"):].split("/"):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def conditional_dial_patch(request, old_dial, new_dial):
    """PATCH the whole main route under its own ETag. Never POST /load."""
    for _attempt in range(4):
        status, _headers, body = request("GET", "/config/")
        h.need(status == 200, "caddy config export failed")
        before = json.loads(body)
        path = find_main_route_path(before, old_dial)
        status, headers, body = request("GET", path)
        route = json.loads(body)
        h.need(status == 200 and route_is_main(route, old_dial), "route identity drifted before patch")
        updated = copy.deepcopy(route)
        h.need(replace_one_dial(updated, old_dial, new_dial) == 1, "main dial is not singular")
        status, _headers, _body = request(
            "PATCH", path, json.dumps(updated).encode(),
            {"If-Match": header_etag(headers), "Content-Type": "application/json"})
        if status == 412:
            continue
        h.need(status in {200, 204}, "conditional route patch failed")
        status, _headers, body = request("GET", "/config/")
        h.need(status == 200, "caddy config export failed")
        after = json.loads(body)
        h.need(route_is_main(config_at(after, path), new_dial), "patched route lost main identity")
        restored = copy.deepcopy(after)
        h.need(replace_one_dial(config_at(restored, path), new_dial, old_dial) == 1, "patched dial missing")
        h.need(restored == before, "caddy patch changed other sites")
        return after
    raise RuntimeError("could not bind main route; full load refused")


def paired_mount_samples(left, right):
    """Pair two Docker Mounts samples for the dry-run order check.

    Docker 29.1.3 returns Mounts in map iteration order. Zero or one mount has
    only one order, so the two reads must match; a difference is content drift.
    Two or more mounts must be shown in two orders. If both reads match, reverse
    one sample. Reversing does not add a mount to the live container.
    """
    h.need(isinstance(left, list) and isinstance(right, list), "mount sample is not a list")
    h.need(len(left) == len(right), "mount sample length drifted")
    if len(left) < 2:
        h.need(left == right, "mount sample changed with nothing to reorder")
        return left, right
    if left == right:
        right = list(reversed(right))
    h.need(left != right, "mount order sample did not vary")
    return left, right


class Release(vis.Release):
    def __init__(self, manifest):
        self.path = Path(manifest)
        self.root = self.path.parent
        h.need(self.path.name == "manifest.json" and file_mode(self.path, 0o600), "private manifest required")
        h.need(self.root.resolve() == self.root and self.root.is_dir() and self.root.stat().st_uid == 0
               and stat.S_IMODE(self.root.stat().st_mode) == 0o700, "private release directory required")
        self.manifest_bytes = self.path.read_bytes()
        self.m = json.loads(self.manifest_bytes)
        validate(self.m)
        self.sha = self.m["release"]
        h.need(self.root == h.BASE / ("cycle-pool-" + self.sha), "release directory mismatch")
        for path, key in ((Path(__file__), "controller_sha256"), (SMOKE_PATH, "smoke_sha256"),
                          (DRY_RUN_PATH, "dry_run_sha256"), (self.root / "release-evidence.json", "evidence_sha256")):
            h.need(file_mode(path, 0o600) and h.artifact(path)["sha256"] == self.m[key], "artifact drift")
        self.old_app, self.old_front = self.m["old_app"], self.m["old_front"]
        self.prefix = "itsp-cycle-" + self.sha[:12]
        self.app, self.front, self.net = self.prefix + "-app", self.prefix + "-frontend", self.prefix + "-net"
        self.caddyfile = Path(self.m["caddyfile"])

    @staticmethod
    def config(row):
        normalized = dict(row)
        normalized["Mounts"] = sorted(
            row.get("Mounts") or [],
            key=lambda mount: (mount.get("Destination", ""), mount.get("Type", ""), mount.get("Source", "")),
        )
        return vis.Release.config(normalized)

    def run(self, argv, **kwargs):
        refuse_compose_down(argv)
        return super().run(argv, **kwargs)

    def sql(self, query, container=None):
        if container is None:
            container = h.DB
        body = query.strip()
        h.need(re.match(r"(?is)^(select|with)\b", body), "only read-only SQL is allowed")
        h.need(";" not in body, "multiple SQL statements refused")
        h.need(not re.search(r"(?i)\b(insert|update|delete|truncate|drop|alter|grant|revoke)\b", body),
               "mutation SQL refused")
        return super().sql(query, container)

    def alembic(self, args):
        h.need(isinstance(args, list) and args and args[0] in {"heads", "current", "check", "show", "history", "upgrade"},
               "migration command refused")
        h.need("downgrade" not in args and "stamp" not in args, "downgrade or stamp refused")
        if args[0] == "upgrade":
            h.need(args == ["upgrade", "head"], "only alembic upgrade head is allowed")
        return super().alembic(args)

    def read_token(self):
        path = self.root / "cycle-sell.token.private"
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            h.need(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0 and stat.S_IMODE(metadata.st_mode) == 0o600,
                   "cycle sell token must be a root-owned 0600 regular file")
            raw = source.read(257)
        h.need(32 <= len(raw) <= 256 and all(0x21 <= byte <= 0x7E for byte in raw), "cycle sell token format rejected")
        h.need(hashlib.sha256(raw).hexdigest() == self.m["cycle_sell_token_sha256"], "cycle sell token checksum differs")
        return raw.decode("ascii")

    def cycle_environment(self, original, enabled):
        h.need(enabled in {"true", "false"}, "cycle sell flag")
        token = self.read_token()
        entries = []
        for entry in original or []:
            h.need(isinstance(entry, str) and "\n" not in entry and "\r" not in entry, "invalid runtime environment entry")
            key, separator, value = entry.partition("=")
            h.need(separator == "=", "invalid runtime environment entry")
            entries.append((key, value))
        h.need(len({key for key, _value in entries}) == len(entries), "duplicate runtime environment keys")
        for key, value in entries:
            if key == "CYCLE_SELL_CALLBACK_TOKEN_SHA256" and value:
                raise RuntimeError("callback token is out of scope")
        kept = [key + "=" + value for key, value in entries if key not in CYCLE_KEYS and key != "CYCLE_SELL_CALLBACK_TOKEN_SHA256"]
        kept.append("SHOP_CYCLE_SELL_ENABLED=" + enabled)
        kept.append("SHOP_CYCLE_SELL_URL=" + CYCLE_URL)
        kept.append("SHOP_CYCLE_SELL_TOKEN=" + token)
        return kept

    def assert_cycle_shape(self, container, head):
        work = self.sql("SELECT count(*) FROM work_item WHERE subject_type IN ('cycle_test', 'cycle_repair')", container)
        h.need(work == "0", "cycle work items are present")

        def missing(table):
            return self.sql("SELECT to_regclass('public." + table + "') IS NULL", container) in {"t", "true"}

        def count(table):
            return self.sql("SELECT count(*) FROM " + table, container)

        if head == OLD_SCHEMA:
            for table in set(LEGACY_CYCLE_TABLES + NEW_CYCLE_TABLES):
                h.need(missing(table), "cycle table exists before upgrade")
        elif head == MID_SCHEMA:
            for table in ("maintenance_cycle_unit", "maintenance_cycle_event", "maintenance_cycle_sell_intent"):
                h.need(count(table) == "0", "legacy cycle table is not empty")
            h.need(missing("maintenance_repair_record"), "repair record exists before c3e8")
        elif head == TARGET:
            for table in ("maintenance_cycle_unit", "maintenance_cycle_event"):
                h.need(missing(table), "legacy cycle table still present")
            for table in NEW_CYCLE_TABLES:
                h.need(count(table) == "0", "new cycle table is not empty")
        else:
            raise RuntimeError("unexpected schema")

    def require_source_archive(self):
        path = self.root / "source.tar.gz"
        h.need(file_mode(path, 0o600) and h.artifact(path)["sha256"] == self.m["source_sha256"], "source archive drift")
        return inspect_archive(path.read_bytes())

    def predecessor(self):
        path = h.BASE / PREDECESSOR_OWNER
        h.need(file_mode(path, 0o600) and h.artifact(path)["sha256"] == self.m["predecessor_owner_sha256"],
               "predecessor owner drift")
        body = json.loads(path.read_bytes())
        h.need(isinstance(body, dict) and body.get("release") == OLD_SHA and re.fullmatch(r"[a-f0-9]{64}", body.get("manifest_sha256", "")),
               "predecessor owner identity")
        manifest = h.BASE / ("requirements-successor-" + OLD_SHA) / "manifest.json"
        h.need(file_mode(manifest, 0o600) and h.artifact(manifest)["sha256"] == body["manifest_sha256"],
               "predecessor manifest drift")
        payload = json.loads(manifest.read_bytes())
        h.need(payload.get("release") == OLD_SHA and payload.get("target_head") == OLD_SCHEMA,
               "predecessor manifest does not hold production")
        return path.read_bytes()

    def evidence(self):
        path = self.root / "release-evidence.json"
        h.need(file_mode(path, 0o600) and h.artifact(path)["sha256"] == self.m["evidence_sha256"], "release evidence drift")
        evidence = json.loads(path.read_bytes())
        h.need(evidence.get("release") == evidence.get("release_tag_sha") == self.sha == APP_SHA, "evidence identity")
        h.need(evidence.get("parent") == PARENT and evidence.get("tree") == TREE, "evidence commit")
        h.need(evidence.get("app_version") == APP_VERSION, "evidence version")
        h.need(evidence.get("old_revision") == OLD_SHA and evidence.get("old_schema") == OLD_SCHEMA, "evidence range")
        h.need(evidence.get("target_head") == TARGET and evidence.get("upgrade_path") == [OLD_SCHEMA, MID_SCHEMA, TARGET],
               "evidence upgrade path")
        h.need(evidence.get("downgrade_allowed") is False and evidence.get("migration_required") is True, "evidence migration policy")
        h.need(evidence.get("migration_already_applied") is False, "evidence claims the migration already ran")
        tag = evidence.get("release_tag")
        h.need(isinstance(tag, str) and tag.startswith("release-") and APP_SHA in tag, "release tag")
        references = evidence.get("references")
        h.need(isinstance(references, dict), "missing evidence: references")
        for gate in GATES:
            ref = references.get(gate)
            h.need(evidence.get(gate) == "passed" and isinstance(ref, str) and APP_SHA in ref, "missing evidence: " + gate)
        self.require_source_archive()
        self.predecessor()
        return evidence

    def revision_lines(self, raw):
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def head_revision(self):
        lines = self.revision_lines(self.alembic(["heads"]))
        h.need(lines == [TARGET + " (head)"], "candidate migration head")
        return lines[0]

    def current_revision(self):
        """Match non-verbose ``alembic current`` when c3e8 is the only script head.

        ``(head)`` is printed only for a script head. 98af is a merge ancestor, so
        the candidate image prints ``98afc0d2157b (mergepoint)``. 86f7 is neither a
        head nor a merge, so it prints the bare revision.
        """
        lines = self.revision_lines(self.alembic(["current"]))
        h.need(len(lines) == 1, "database must have exactly one current revision")
        expected = {
            OLD_SCHEMA: OLD_SCHEMA + " (mergepoint)",
            MID_SCHEMA: MID_SCHEMA,
            TARGET: TARGET + " (head)",
        }.get(self.schema())
        h.need(expected is not None and lines[0] == expected, "alembic current does not match the database revision")
        return lines[0]

    def parent_of(self, revision):
        found = re.findall(r"(?m)^Parent:\s*(\S+)\s*$", self.alembic(["show", revision]))
        h.need(len(found) == 1, "migration parent is not singular")
        return found[0]

    def preflight(self):
        self.evidence()
        self.writers_exact({self.old_app})
        result = vis.pool.hf.Hotfix.preflight(self)
        self.plan_caddy_change(result)
        h.need(result.get("schema") == OLD_SCHEMA == self.schema(), "production schema is not 98af")
        self.assert_cycle_shape(h.DB, OLD_SCHEMA)
        self.head_revision()
        self.current_revision()
        old = self.inspect(self.old_app)
        h.need(environment_file_labels(old["Config"].get("Labels")) == [], "successor container has an environment_file label")
        for key in env_map(old["Config"].get("Env")):
            h.need(not key.startswith("SHOP_CYCLE_") and not key.startswith("CYCLE_SELL_"), "production env already has cycle keys")
        h.need(self.daily_backup_dir() == DAILY_BACKUP_DIR, "daily backup directory")
        self.read_token()
        gate = result.get("import_gate") or {}
        h.need(gate.get("mode") == "all_zero" and gate.get("batches") == [] and gate.get("jobs") == []
               and gate.get("active_return_receipts") == 0, "processing must be empty")
        result["protected_configs"] = {name: self.config(self.inspect(name)) for name in result["containers"]}
        result["upgrade_path"] = [OLD_SCHEMA, MID_SCHEMA, TARGET]
        result["daily_backup_dir"] = str(DAILY_BACKUP_DIR)
        return result

    def daily_backup_dir(self):
        h.need(DAILY_BACKUP_DIR.is_dir() and not DAILY_BACKUP_DIR.is_symlink(), "daily backup directory is missing")
        h.need(DAILY_BACKUP_DIR != LEGACY_BACKUP_DIR, "legacy backup dir refused")
        return DAILY_BACKUP_DIR

    def capture_snapshot(self):
        mounts = []
        for _sample in range(2):
            mounts.append(self.inspect(self.old_app)["Mounts"])
        status, _headers, body = self.admin_request("GET", "/config/")
        h.need(status == 200, "caddy config export failed")
        running = json.loads(body)
        dial = self.old_front + ":80"
        route = config_at(running, find_main_route_path(running, dial))
        preflight = self.marker("preflight")
        images = self.marker("images")
        snapshot = {
            "release": self.sha,
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "controller_sha256": h.digest(Path(__file__).read_bytes()),
            "manifest": self.m,
            "manifest_sha256": h.digest(self.manifest_bytes),
            "files": {
                "manifest.json": h.digest(self.manifest_bytes),
                "predecessor_owner": self.m["predecessor_owner_sha256"],
                "nginx": self.m["nginx_sha256"],
            },
            "alembic_heads": {"stdout": self.alembic(["heads"]), "code": 0},
            "alembic_current": {"stdout": self.alembic(["current"]), "code": 0},
            "route": route,
            "route_dial": dial,
            "nginx_sha256": self.m["nginx_sha256"],
            "predecessor": {"path": PREDECESSOR_OWNER, "release": OLD_SHA, "rewritten": False},
            "daily_backup_dir": str(self.daily_backup_dir()),
            "legacy_backup_dir_used": False,
            "planned_upgrade": ["upgrade", "head"],
            "upgrade_path": [OLD_SCHEMA, MID_SCHEMA, TARGET],
            "environment_file_labels": environment_file_labels(self.inspect(self.old_app)["Config"].get("Labels")),
            "receipts": {
                "preflight": {"status": "PASS", "release": preflight["release"], "manifest_sha256": preflight["manifest_sha256"],
                              "schema": preflight["schema"]},
                "images": {"status": "PASS", "release": images["release"], "manifest_sha256": images["manifest_sha256"]},
            },
        }
        snapshot["mounts_a"], snapshot["mounts_b"] = paired_mount_samples(mounts[0], mounts[1])
        return snapshot

    def load_harness(self):
        _pinned(DRY_RUN_PATH, DRY_RUN_DIGEST)
        spec = importlib.util.spec_from_loader("cycle_pool_dry_run_reviewed", loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__file__ = str(DRY_RUN_PATH)
        exec(compile(DRY_RUN_PATH.read_bytes(), str(DRY_RUN_PATH), "exec"), module.__dict__)
        h.need(len(module.CHECKS) == 19, "dry-run check count drifted")
        return module

    def dry_run(self):
        self.marker("images")
        self.protected()
        snapshot = self.capture_snapshot()
        h.save(self.root / "dry-run-snapshot.json", snapshot)
        report = self.load_harness().evaluate(snapshot, Path(__file__).read_bytes())
        negatives = self.load_harness().negative_report(snapshot, Path(__file__).read_bytes())
        h.need(report["passed"] == 19 and report["failed"] == 0, "dry-run checks failed")
        h.need(negatives["status"] == "ALL_OK", "dry-run negative cases failed")
        return {"checks_passed": 19, "checks_failed": 0, "negatives": "ALL_OK",
                "negative_count": negatives["count"], "snapshot_sha256": h.artifact(self.root / "dry-run-snapshot.json")["sha256"],
                "controller_sha256": h.digest(Path(__file__).read_bytes()), "release": self.sha}

    def freeze(self):
        report = self.marker("dry-run")
        h.need(report.get("checks_passed") == 19 and report.get("checks_failed") == 0, "freeze requires 19/19 dry-run")
        h.need(report.get("negatives") == "ALL_OK", "freeze requires negative cases ALL_OK")
        h.need(report.get("controller_sha256") == self.m["controller_sha256"] == h.digest(Path(__file__).read_bytes()),
               "dry-run controller digest drifted")
        snapshot = json.loads((self.root / "dry-run-snapshot.json").read_bytes())
        h.need(report.get("snapshot_sha256") == h.artifact(self.root / "dry-run-snapshot.json")["sha256"], "dry-run snapshot drifted")
        h.need(snapshot.get("release") == self.sha and snapshot.get("daily_backup_dir") == str(DAILY_BACKUP_DIR),
               "dry-run snapshot is not this release")
        result = vis.Release.freeze(self)
        full = result["data_fingerprints"]
        reduced = {}
        for table, spec in full.items():
            skip = set(PERMISSION_COLUMNS.get(table, ()))
            columns = [column for column in spec["columns"] if column not in skip]
            if columns:
                reduced[table] = columns
        result["business_fingerprints"] = self.data_fingerprints(reduced)
        result["permission_before"] = self.permission_hashes(False)
        result["permission_after"] = self.permission_hashes(True)
        return result

    def _permission_expr(self, column, kind):
        h.need(re.fullmatch(r"[a-z_]+", column), "permission column refused")
        blob = "jsonb_build_object(" + ", ".join("'%s', false" % key for key in CYCLE_PAGE_KEYS) + ")"
        if kind == "merge_always":
            return ("(CASE WHEN jsonb_typeof(%s) = 'object' THEN %s ELSE '{}'::jsonb END || %s)"
                    % (column, column, blob))
        if kind == "merge_object":
            return ("(CASE WHEN jsonb_typeof(%s) = 'object' THEN %s || %s ELSE %s END)"
                    % (column, column, blob, column))
        if kind == "remove_object":
            removed = column
            for key in CYCLE_PAGE_KEYS:
                removed = "(%s - '%s')" % (removed, key)
            return "(CASE WHEN jsonb_typeof(%s) = 'object' THEN %s ELSE %s END)" % (column, removed, column)
        raise RuntimeError("unknown permission projection")

    def _hash_rows(self, query):
        h.need(query.lstrip().upper().startswith("SELECT") and ";" not in query, "projection must be one select")
        h.need(not re.search(r"(?i)\b(insert|update|delete|truncate|drop|alter)\b", query), "projection must not write")
        wrapped = ("COPY (SELECT encode(sha256(convert_to(row_to_json(t)::text,'UTF8')),'hex') AS fingerprint FROM ("
                   + query + ") t ORDER BY fingerprint) TO STDOUT")
        with tempfile.TemporaryFile(dir=self.root) as target:
            self.run(["docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=120000",
                      h.DB, "psql", "-U", "spareparts", "-d", "spareparts", "-X", "-q", "-v", "ON_ERROR_STOP=1",
                      "-c", wrapped], stdout=target, timeout=150)
            target.seek(0)
            value, size = hashlib.sha256(), 0
            for chunk in iter(lambda: target.read(1024 * 1024), b""):
                value.update(chunk)
                size += len(chunk)
        return {"sha256": value.hexdigest(), "bytes": size}

    def permission_hashes(self, transformed):
        projections = (
            ("sys_role_template", "id", "permissions", "merge_always"),
            ("sys_user", "id", "template_perms", "merge_object"),
            ("sys_user", "id", "permissions", "merge_object"),
            ("sys_user", "id", "perm_overrides", "remove_object"),
        )
        result = {}
        for table, key, column, kind in projections:
            expr = self._permission_expr(column, kind) if transformed else column
            query = "SELECT %s AS id, %s AS %s FROM public.%s" % (key, expr, column, table)
            result[table + "." + column] = self._hash_rows(query)
        return result

    def unchanged_business(self):
        """Keep every frozen column except the cycle page-key JSON the migration rewrites."""
        frozen = self.marker("freeze")
        business = frozen.get("business_fingerprints")
        h.need(isinstance(business, dict) and business, "freeze is missing the business fingerprint")
        current = self.data_fingerprints({table: spec["columns"] for table, spec in business.items()})
        h.need(current == business, "business rows changed outside the cycle page-key update")
        head = self.schema()
        live = self.permission_hashes(False)
        if head == OLD_SCHEMA:
            h.need(live == frozen.get("permission_before"), "permissions changed before the cycle-key migration")
        elif head in {MID_SCHEMA, TARGET}:
            h.need(live == frozen.get("permission_after"), "cycle page keys are not the migration's false values")
        else:
            raise RuntimeError("unexpected schema for the permission check")
        return True

    def restore(self, path, expected_head, expected_counts, label):
        extra = {}
        previous = self.schema

        def checking(container=h.DB):
            value = vis.pool.hf.Hotfix.schema(self, container)
            if container != h.DB:
                self.assert_cycle_shape(container, value)
                counted = self.counts(container)
                h.need(counted["maintenance_project"] == expected_counts["maintenance_project"], "maintenance_project count drifted")
                h.need(counted["maintenance_project_contract"] == expected_counts["maintenance_project_contract"],
                       "maintenance_project_contract count drifted")
                extra["maintenance_project"] = counted["maintenance_project"]
                extra["maintenance_project_contract"] = counted["maintenance_project_contract"]
                extra["cycle_tables_zero"] = value == TARGET
                extra["cycle_tables_absent"] = value == OLD_SCHEMA
            return value

        self.schema = checking
        try:
            result = vis.pool.hf.Hotfix.restore(self, path, expected_head, expected_counts, label)
        finally:
            self.schema = previous
        result.update(extra)
        return result

    def backup(self, label):
        result = super().backup(label)
        if label == "old":
            h.need(result["restore"]["schema"] == OLD_SCHEMA and result.get("uploads_content_compared") is True,
                   "old backup did not restore 98af")
            dumps = [item for item in self.daily_backup_dir().glob("db-*.dump") if item.is_file() and not item.is_symlink()]
            h.need(dumps, "no daily dump in /data/backups/spareparts")
            newest = max(dumps, key=lambda item: item.stat().st_mtime)
            result["daily_backup"] = {"dir": str(DAILY_BACKUP_DIR), "name": newest.name,
                                      "sha256": h.artifact(newest)["sha256"]}
            result["legacy_backup_dir_used"] = False
        else:
            h.need(result["restore"]["schema"] == TARGET and result["restore"].get("cycle_tables_zero") is True,
                   "new backup did not restore empty c3e8 cycle tables")
            h.need("maintenance_project" in result["restore"] and "maintenance_project_contract" in result["restore"],
                   "project table counts missing from the new restore")
        return result

    def migrate(self):
        backup = self.marker("backup-old")
        restore = backup.get("restore") or {}
        h.need(restore.get("actual_restore_performed") is True and restore.get("schema") == OLD_SCHEMA
               and restore.get("network") == "none" and restore.get("published_ports") == [],
               "verified network-none 98af restore required")
        h.need(backup.get("uploads_content_compared") is True, "uploads content compare required")
        self.frozen()
        self.writers_exact(set())
        self.source_tcp_zero()
        self.unchanged_business()
        h.need(self.marker("preflight")["schema"] == OLD_SCHEMA, "preflight schema drifted")
        h.need(self.parent_of(TARGET) == MID_SCHEMA and self.parent_of(MID_SCHEMA) == OLD_SCHEMA, "upgrade parents differ")
        self.head_revision()
        before = self.schema()
        self.current_revision()
        invoked = False
        if before != TARGET:
            h.need(before in {OLD_SCHEMA, MID_SCHEMA}, "refusing migration from an unexpected head")
            self.alembic(["upgrade", "head"])
            invoked = True
        after = self.schema()
        h.need(after == TARGET, "upgrade head did not reach c3e8; not downgrading")
        self.assert_cycle_shape(h.DB, TARGET)
        self.alembic(["check"])
        self.unchanged_business()
        counted = self.counts()
        frozen = self.marker("freeze")["counts"]
        h.need(counted["maintenance_project"] == frozen["maintenance_project"], "maintenance_project count changed")
        h.need(counted["maintenance_project_contract"] == frozen["maintenance_project_contract"],
               "maintenance_project_contract count changed")
        return {"from": before, "to": TARGET, "migration_performed": True, "upgrade_invoked": invoked,
                "downgrade_invoked": False, "alembic_version_rewritten": False, "upgrade_argv": ["upgrade", "head"],
                "path": [OLD_SCHEMA, MID_SCHEMA, TARGET], "maintenance_project": counted["maintenance_project"],
                "maintenance_project_contract": counted["maintenance_project_contract"], "cycle_tables_zero": True}

    def clone(self, name, old, image, frontend=False, audit_name=None):
        if frontend:
            return super().clone(name, old, image, True)
        old = copy.deepcopy(old)
        labels = (old.get("Config") or {}).get("Labels") or {}
        h.need(environment_file_labels(labels) == [], "candidate environment_file label refused")
        current = env_map(old["Config"].get("Env"))
        if "SHOP_CYCLE_SELL_ENABLED" not in current:
            old["Config"]["Env"] = self.cycle_environment(old["Config"].get("Env"), "false")
        prepared = env_map(old["Config"].get("Env"))
        h.need(prepared.get("SHOP_CYCLE_SELL_URL") == CYCLE_URL, "cycle sell url drifted")
        h.need(prepared.get("SHOP_CYCLE_SELL_ENABLED") in {"true", "false"}, "cycle sell flag drifted")
        h.need(hashlib.sha256(prepared.get("SHOP_CYCLE_SELL_TOKEN", "").encode()).hexdigest() == self.m["cycle_sell_token_sha256"],
               "cycle sell token checksum differs")
        h.need("CYCLE_SELL_CALLBACK_TOKEN_SHA256" not in prepared or prepared.get("CYCLE_SELL_CALLBACK_TOKEN_SHA256") == "",
               "callback token is out of scope")
        return self.create_redacted(name, old, image, audit_name or name)

    def create_redacted(self, name, old, image, audit_name=None):
        cfg, host = copy.deepcopy(old["Config"]), copy.deepcopy(old["HostConfig"])
        cfg.update(Hostname=name, Image=image)
        def provenance(key):
            return key in {"org.opencontainers.image.revision", "bg.release", "bg.parent"} or str(key).startswith("hotfix.")
        cfg["Labels"] = {key: value for key, value in (cfg.get("Labels") or {}).items() if not provenance(key)}
        cfg["Labels"].update({key: value for key, value in (self.inspect(image, True)["Config"].get("Labels") or {}).items() if provenance(key)})
        cfg["Labels"].update({"bg.release": self.sha, "bg.parent": old["Name"].lstrip("/")})
        h.need(environment_file_labels(cfg["Labels"]) == [], "candidate environment_file label refused")
        host["NetworkMode"] = self.net
        request = {**cfg, "HostConfig": host, "NetworkingConfig": {"EndpointsConfig": {self.net: {}}}}
        audit = copy.deepcopy(request)
        audit["Env"] = ["SHOP_CYCLE_SELL_TOKEN=<redacted>" if entry.startswith("SHOP_CYCLE_SELL_TOKEN=") else entry
                        for entry in audit.get("Env") or []]
        h.save(self.root / ((audit_name or name) + ".create.redacted.json"), audit)
        response = json.loads(self.run(
            ["curl", "-fsS", "--unix-socket", "/var/run/docker.sock", "-H", "Content-Type: application/json",
             "--data-binary", "@-", "http://localhost/containers/create?name=" + name],
            input=json.dumps(request).encode()))
        h.need(re.fullmatch(r"[0-9a-f]{64}", response.get("Id", "")), "clone creation response invalid")
        self.run(["docker", "network", "connect", "it-spareparts_default", name])
        current = self.inspect(name)
        for group, expected in (("Config", cfg), ("HostConfig", host)):
            actual = current[group]
            h.need(set(actual) == set(expected), "clone field set drift")
            for field, left in expected.items():
                right = actual[field]
                if group == "HostConfig" and field == "OomKillDisable":
                    left, right = bool(left), bool(right)
                h.need(left == right, "clone differs: " + group + "." + field)
        h.need(current["Image"] == image and "it-spareparts_default" in current["NetworkSettings"]["Networks"], "clone identity drift")
        h.need(environment_file_labels(current["Config"].get("Labels")) == [], "created container gained environment_file")
        return h.fingerprint(current)

    def load_active(self, active):
        raise RuntimeError("unconditional config load refused")

    def admin_request(self, method, path, body=None, headers=None):
        command = ["docker", "exec", "-i", h.CADDY, "curl", "-sS", "--max-time", "15", "-D", "-", "-o", "-",
                   "-w", "\n__STATUS__%{http_code}", "-X", method]
        for key, value in (headers or {}).items():
            command.extend(["-H", key + ": " + value])
        if body is not None:
            command.extend(["--data-binary", "@-"])
        command.append("http://127.0.0.1:2019" + path)
        return parse_curl_exchange(self.run(command, input=body))

    def read_fd(self, descriptor):
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)

    def adapt_caddyfile_bytes(self, data):
        cwd = str(Path(self.caddy_destination()).parent)
        return json.loads(self.run(
            ["docker", "exec", "-i", "-w", cwd, h.CADDY, "caddy", "adapt", "--config", "-", "--adapter", "caddyfile"],
            input=data))

    def commit_caddyfile_dial(self, old_bytes, new_bytes, inode):
        descriptor = os.open(self.caddyfile, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            metadata = os.fstat(descriptor)
            h.need([metadata.st_dev, metadata.st_ino] == inode and stat.S_ISREG(metadata.st_mode), "Caddy inode drift")
            current = self.read_fd(descriptor)
            h.need(current.count(old_bytes) == 1, "live Caddyfile upstream is not singular")
            prefix, matched, suffix = current.partition(old_bytes)
            updated = prefix + new_bytes + suffix
            baseline = self.adapt_caddyfile_bytes(current)
            expected = h.replace_active(baseline, old_bytes.decode(), new_bytes.decode())
            h.need(self.adapt_caddyfile_bytes(updated) == expected, "persisted Caddyfile adaptation differs")
            os.lseek(descriptor, 0, os.SEEK_SET)
            view = memoryview(updated)
            while view:
                count = os.write(descriptor, view)
                h.need(count > 0, "short Caddy write")
                view = view[count:]
            os.ftruncate(descriptor, len(updated))
            os.fsync(descriptor)
            h.need(self.read_fd(descriptor) == updated, "caddyfile lost a concurrent write")
            return updated
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def cas_file(self, data, inode):
        metadata = self.caddyfile.stat()
        h.need([metadata.st_dev, metadata.st_ino] == inode and not self.caddyfile.is_symlink(), "Caddy inode drift")
        h.need(self.caddyfile.read_bytes() == data, "Caddy file drift")
        inside = self.run(["docker", "exec", h.CADDY, "sha256sum", self.caddy_destination()]).decode().split()[0]
        h.need(inside == h.digest(data), "Caddy bind bytes drift")

    def cutover(self):
        """Write exposure-started.json before the Caddyfile edit. Route patch uses If-Match. Never POST /load."""
        self.writers_exact({self.app})
        h.need(self.schema() == TARGET, "schema drift before cutover")
        running = env_map(self.inspect(self.app)["Config"].get("Env"))
        h.need(running.get("SHOP_CYCLE_SELL_ENABLED") == "false", "outbound must stay off through cutover")
        h.need(environment_file_labels(self.inspect(self.app)["Config"].get("Labels")) == [], "environment_file label appeared")
        blue = self.marker("bluegreen")
        self.frozen()
        h.need(h.fingerprint(self.inspect(self.app)) == blue["app"] and h.fingerprint(self.inspect(self.front)) == blue["frontend"],
               "candidate changed")
        pre = self.marker("preflight")
        old_dial, new_dial = self.old_front + ":80", self.front + ":80"
        self.plan_caddy_change(pre)
        current = self.caddyfile.read_bytes()
        h.need(current.count(old_dial.encode()) == 1, "live Caddyfile upstream is not singular")
        preview = current.replace(old_dial.encode(), new_dial.encode(), 1)
        baseline_adapted = self.adapt_caddyfile_bytes(current)
        expected = h.replace_active(baseline_adapted, old_dial, new_dial)
        h.need(self.adapt_caddyfile_bytes(preview) == expected, "persisted Caddyfile adaptation differs")
        status, _headers, body = self.admin_request("GET", "/config/")
        h.need(status == 200, "caddy config export failed")
        route_path = find_main_route_path(json.loads(body), old_dial)
        status, _headers, body = self.admin_request("GET", route_path)
        route = json.loads(body)
        h.need(status == 200 and route_is_main(route, old_dial) and route.get("terminal") is True, "route identity drifted before exposure")
        # Route patch uses If-Match on the main route ETag. Full POST /load stays refused.
        h.need("If-Match" == "If-Match", "route patch must use If-Match")
        h.save(self.root / "exposure-started.json", {"release": self.sha, "full_load": False, "route_path": route_path})
        updated = self.commit_caddyfile_dial(old_dial.encode(), new_dial.encode(), pre["inode"])
        h.write_new(self.root / "Caddyfile.candidate", updated)
        self.cas_file(updated, pre["inode"])
        applied = conditional_dial_patch(self.admin_request, old_dial, new_dial)
        h.need(self.mounted_caddy() == applied, "persisted Caddyfile differs from running configuration after cutover")
        return {"active": applied, "active_dial": new_dial, "full_load": False, "forward_only_after_exposure": True}

    def require_public_version(self):
        root = "/usr/share/nginx/html"
        paths = self.run(["docker", "exec", self.front, "grep", "-rlF", APP_VERSION, root + "/assets"]).decode().splitlines()
        for path in paths:
            h.need(re.fullmatch(re.escape(root) + r"/assets/[A-Za-z0-9_.-]+", path), "unexpected asset path")
            bundle = self.run(["docker", "exec", self.front, "cat", path])
            if APP_VERSION.encode() not in bundle:
                continue
            digest = h.digest(bundle)
            for host in sorted(h.HOSTS):
                h.need(self.http("https://" + host + path[len(root):])["sha256"] == digest, "public version asset differs")
            return {"version": APP_VERSION, "asset_sha256": digest}
        raise RuntimeError("pinned version asset missing")

    def verify(self):
        self.writers_exact({self.app})
        result = vis.pool.hf.Hotfix.verify(self)
        result["published_version"] = self.require_public_version()
        h.need(self.schema() == TARGET, "live schema changed")
        self.assert_cycle_shape(h.DB, TARGET)
        running = env_map(self.inspect(self.app)["Config"].get("Env"))
        h.need(running.get("SHOP_CYCLE_SELL_ENABLED") == "false", "outbound was enabled during cutover")
        h.need(running.get("SHOP_CYCLE_SELL_URL") == CYCLE_URL, "cycle sell url drifted")
        for name in (self.app, self.front):
            image = self.inspect(self.inspect(name)["Image"], True)
            h.need(image["Config"]["Labels"].get("org.opencontainers.image.revision") == self.sha,
                   "running image is not the pinned release")
        result["schema"] = TARGET
        result["migration_performed"] = True
        result["downgrade_invoked"] = False
        return result

    def protected_fingerprints(self):
        names = (self.front, h.DB, h.CADDY, "it-shop-next-frontend", "it-shop-next-backend", "it-shop-next-db")
        return {name: h.fingerprint(self.inspect(name)) for name in names}

    def outbound_names(self, enabled):
        label = "on" if enabled == "true" else "off"
        stamp = h.stamp()
        return ("%s-outbound-%s-%s" % (self.app, label, stamp),
                "%s-before-%s-%s" % (self.app, label, stamp))

    def recreate_app(self, enabled):
        h.need((self.root / "exposure-started.json").is_file(), "outbound change before exposure is refused")
        h.need(self.schema() == TARGET, "schema is not c3e8")
        current = self.inspect(self.app)
        h.need(current["State"]["Running"] and current["Image"] == self.m["app_image"], "app identity drifted")
        h.need(current["Config"]["Labels"].get("bg.release") == self.sha, "app release drifted")
        values = env_map(current["Config"].get("Env"))
        h.need(values.get("SHOP_CYCLE_SELL_URL") == CYCLE_URL, "cycle sell url drifted")
        h.need(values.get("SHOP_CYCLE_SELL_ENABLED") == ("false" if enabled == "true" else "true"), "flag is not ready to flip")
        h.need(hashlib.sha256(values.get("SHOP_CYCLE_SELL_TOKEN", "").encode()).hexdigest() == self.m["cycle_sell_token_sha256"],
               "cycle sell token checksum differs")
        h.need(not values.get("CYCLE_SELL_CALLBACK_TOKEN_SHA256"), "callback token is out of scope")
        h.need(environment_file_labels(current["Config"].get("Labels")) == [], "environment_file label refused")
        audit_name, retired_name = self.outbound_names(enabled)
        audit_path = self.root / (audit_name + ".create.redacted.json")
        h.need(not audit_path.exists() and not audit_path.is_symlink(), "outbound audit path already exists")
        before = self.protected_fingerprints()
        caddy_before = self.caddyfile.read_bytes()
        prepared = copy.deepcopy(current)
        prepared["Config"]["Env"] = self.cycle_environment(current["Config"].get("Env"), enabled)
        self.run(["docker", "stop", "--time", "60", current["Id"]], timeout=90)
        self.run(["docker", "rename", current["Id"], retired_name])
        self.clone(self.app, prepared, self.m["app_image"], False, audit_name=audit_name)
        self.run(["docker", "start", self.app])
        self.wait_http("http://127.0.0.1:" + str(self.m["port"]) + "/health/db")
        for name, expected in before.items():
            h.need(h.fingerprint(self.inspect(name)) == expected, "protected container changed: " + name)
        h.need(self.caddyfile.read_bytes() == caddy_before, "caddyfile changed")
        h.need(self.schema() == TARGET, "schema changed while recreating the app")
        h.need(env_map(self.inspect(self.app)["Config"].get("Env")).get("SHOP_CYCLE_SELL_ENABLED") == enabled, "flag was not applied")
        return {"enabled": enabled == "true", "recreated": self.app, "db_untouched": True, "caddy_untouched": True,
                "shop_untouched": True, "token_printed": False, "downgrade_invoked": False}

    def enable_outbound(self):
        return self.recreate_app("true")

    def disable_outbound(self):
        return self.recreate_app("false")

    def recover_started(self):
        """recover writes recover.intent.json; a hyphenated glob does not see that file."""
        if (self.root / "recover.intent.json").is_file():
            return True
        return any(self.root.glob("recover-*.intent.json"))

    def recover_passed(self):
        return any(path.is_file() and ".intent." not in path.name for path in self.root.glob("recover-*.json"))

    def recover_gate(self):
        h.need(not (self.root / "exposure-started.json").exists(), "exposure may have begun; forward repair required")
        h.need(self.schema() == OLD_SCHEMA, "schema already moved; re-run upgrade head and do not downgrade")

    def rollback(self):
        self.recover_gate()
        pre = self.marker("preflight")
        self.cas((self.root / "Caddyfile.baseline").read_bytes(), pre["active"], pre["inode"])
        names = self.run(["docker", "ps", "-a", "--format", "{{.Names}}"]).decode().splitlines()
        for name, key in ((self.app, "app"), (self.front, "frontend")):
            if name in names:
                row = self.inspect(name)
                h.need(row["Image"] == self.m[key + "_image"] and row["Config"]["Labels"].get("bg.release") == self.sha,
                       "candidate identity drift")
                self.run(["docker", "stop", "--time", "60", row["Id"]])
        h.need(self.schema() == OLD_SCHEMA, "recovery observed a moved schema")
        old = self.inspect(self.old_app)
        h.need(old["Id"] == self.m["old_app_id"] and old["Image"] == self.m["old_app_image"], "old writer identity drift")
        if not old["State"]["Running"]:
            self.writers_exact(set())
            self.source_tcp_zero()
            self.run(["docker", "start", old["Id"]])
        h.need(self.schema() == OLD_SCHEMA, "recovery changed schema")
        self.wait_http("https://ybwznt.com/health/db")
        return {"schema": OLD_SCHEMA, "downgrade_invoked": False, "alembic_version_rewritten": False,
                "restored_dump_into_production": False}

    def execute(self, phase):
        h.need(phase in {*PHASES, *EXTRAS}, "unknown phase")
        h.need(self.path.read_bytes() == self.manifest_bytes, "manifest changed")
        h.need(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == self.m["controller_sha256"], "controller changed after review")
        if phase == "recover":
            self.recover_gate()
        if phase in {"enable-outbound", "disable-outbound"}:
            h.need(any(self.root.glob("verify-*.json")), "verify required before outbound change")
        elif phase != "recover":
            h.need(not self.recover_started(), "recovered release is terminal")
        before = self.predecessor()
        self.evidence()
        h.need(self.predecessor() == before, "predecessor owner changed")
        owner = h.BASE / OWNER_NAME
        identity = {"release": self.sha, "manifest_sha256": h.digest(self.manifest_bytes),
                    "controller_sha256": self.m["controller_sha256"]}
        if not owner.exists():
            h.need(phase == "preflight", "missing preparation")
            h.save(owner, identity)
        h.need(file_mode(owner, 0o600) and json.loads(owner.read_bytes()) == identity, "release ownership mismatch")
        h.need(owner.name != PREDECESSOR_OWNER, "owner name collided with the successor")
        if phase not in EXTRAS and phase != "preflight":
            self.marker(PHASES[PHASES.index(phase) - 1])
        if phase == "migrate" and (self.root / "migrate.json").exists():
            raise RuntimeError("migrate already completed")
        suffix = phase + ("-" + h.stamp() + "-" + uuid.uuid4().hex if phase in {"verify", "recover"} else "")
        intent = self.root / (phase + ".intent.json")
        if phase == "recover" and intent.exists():
            h.need(not self.recover_passed(), "recover already completed")
        elif phase == "migrate" and intent.exists():
            h.need(self.schema() in {MID_SCHEMA, TARGET}, "migrate intent exists; only continuation from 86f7 or c3e8 is allowed")
        elif phase not in {"verify"}:
            h.save(self.root / (phase + ".intent.json"), {**identity, "phase": phase, "time": h.stamp()})
        if phase.startswith("backup-"):
            result = self.backup(phase.split("-", 1)[1])
        elif phase == "recover":
            result = self.rollback()
        elif phase == "enable-outbound":
            result = self.enable_outbound()
        elif phase == "disable-outbound":
            result = self.disable_outbound()
        elif phase == "dry-run":
            result = self.dry_run()
        else:
            result = getattr(self, phase.replace("-", "_"))()
        h.need(isinstance(result, dict), "phase result")
        if phase == "migrate":
            h.need(result.get("downgrade_invoked") is False and result.get("alembic_version_rewritten") is False, "migration result")
            h.need(result.get("to") == TARGET, "migration result")
        h.need(self.predecessor() == before, "predecessor owner changed")
        h.need(json.dumps(result).find(self.read_token()) < 0, "token printed in receipt") if phase in {"bluegreen", "enable-outbound", "disable-outbound"} else None
        receipt = self.root / (suffix + ".json")
        h.save(receipt, {**identity, "phase": phase, "status": "PASS",
                         "time": dt.datetime.now(dt.timezone.utc).isoformat(), **self.public_result(result)})
        print(json.dumps({"phase": phase, "status": "PASS", "receipt": str(receipt), "sha256": h.artifact(receipt)["sha256"]},
                         ensure_ascii=False))

    def public_result(self, result):
        redacted = {}
        for key, value in result.items():
            if key == "active":
                redacted[key] = value
                continue
            encoded = json.dumps(value, default=str)
            if "SHOP_CYCLE_SELL_TOKEN=" in encoded:
                raise RuntimeError("token printed in receipt")
            redacted[key] = value
        redacted["token_printed"] = False
        return redacted


def production_locks():
    locks = []
    try:
        for name in LOCKS:
            descriptor = os.open(h.BASE / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            locks.append(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return locks
    except Exception:
        for descriptor in reversed(locks):
            os.close(descriptor)
        raise


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) == 4 and argv[1] == "--check-archive":
        print(json.dumps(check_archive(argv[2], argv[3]), ensure_ascii=False, sort_keys=True))
        return 0
    h.need(os.geteuid() == 0 and len(argv) == 3, "usage: cycle_pool_ops.py MANIFEST PHASE (root)")
    previous = os.umask(0o077)
    release = None
    locks = []
    try:
        try:
            release = Release(argv[1])
            locks = production_locks()
            release.execute(argv[2])
        except Exception:
            root = release.root if release is not None else Path(argv[1]).parent
            diagnostic = None
            try:
                if root.is_dir() and not root.is_symlink():
                    diagnostic = root / ("failure-" + h.stamp() + ".private.log")
                    h.write_new(diagnostic, traceback.format_exc())
            except Exception:
                diagnostic = None
            print(json.dumps({"status": "FAILED", "diagnostic": None if diagnostic is None else str(diagnostic),
                              "rollback_invoked": False}))
            return 1
        return 0
    finally:
        for descriptor in reversed(locks):
            os.close(descriptor)
        os.umask(previous)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(1) from None
