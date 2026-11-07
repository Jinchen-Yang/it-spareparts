#!/usr/bin/env python3
"""No-migration successor of the requirements-visibility production owner.

The application archive, tag and image revision are the verified merge
e62cd60cdbcc3fef896c5289388250057c4ce513. This file is a separate audited
controller: it must not be baked into that archive. Historical visibility
constants stay in requirements_visibility_ops.py and are pinned below.
"""
import copy
import datetime as dt
import fcntl
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import traceback
import uuid

APP_SHA = "e62cd60cdbcc3fef896c5289388250057c4ce513"
PARENTS = (
    "ffbf783d346f051d91c6474652be8ce1fa323e09",
    "d37384cdd276adbef6e5ce0a56263108922e3730",
)
TREE = "5b22aee2545bc5ebb31abe0ce19a626516560048"
APP_VERSION = "1.46.0"
OLD_SHA = "782c858a75d1f38991aa7151e9eb54f0474fb031"
OLD_PREFIX = "itsp-requirements-" + OLD_SHA[:12]
VISIBILITY_DIGEST = "6b2fe7287242ff6ce5dfcd785613877ef49dd80f13fb8ea15c4d244e93d37e9d"
SMOKE_DIGEST = "f44594c3390cc67f4cb56cf9e0d58a54d6d25f399136bcf43c0124a394c6196f"
PREDECESSOR_OWNER = ".requirements-visibility-active-owner.json"
OWNER_NAME = ".requirements-successor-active-owner.json"
LOCKS = (".catalog-source-handoff.lock", ".itsp-production-deploy.lock")
GATES = (
    "required_ci", "independent_review", "candidate_regression", "scope_review",
    "no_new_migration", "git_byte_archive",
)
PHASES = ("preflight", "images", "freeze", "backup-old", "migrate", "backup-new", "bluegreen", "cutover", "verify")
FORBIDDEN = {"source_handoff", "catalog_sync", "import_gate_allowlist"}
ALLOWED_ALEMBIC = {"heads", "current", "check"}

VISIBILITY_PATH = Path(__file__).with_name("requirements_visibility_ops.py")
SMOKE_PATH = Path(__file__).with_name("requirements_successor_smoke.py")
_visibility_bytes = VISIBILITY_PATH.read_bytes()
_smoke_bytes = SMOKE_PATH.read_bytes()
if (VISIBILITY_PATH.is_symlink() or SMOKE_PATH.is_symlink()
        or hashlib.sha256(_visibility_bytes).hexdigest() != VISIBILITY_DIGEST
        or hashlib.sha256(_smoke_bytes).hexdigest() != SMOKE_DIGEST):
    raise RuntimeError("reviewed visibility controller or smoke differs")
_spec = importlib.util.spec_from_loader("requirements_visibility_reviewed", loader=None)
vis = importlib.util.module_from_spec(_spec)
vis.__file__ = str(VISIBILITY_PATH)
exec(compile(_visibility_bytes, str(VISIBILITY_PATH), "exec"), vis.__dict__)
if (vis.OLD_SHA != "559489e6ef548ef84be8112ada1c74117e6839ca" or vis.OLD_HEAD != "e5a7c9d1f3b6"
        or vis.TARGET != "98afc0d2157b"
        or vis.POOL_DIGEST != "47b141a4127c349c2bb2d785ede3c11645de9d44c4ebd4accc308f9a76e54a39"):
    raise RuntimeError("historical visibility constants changed")
SCHEMA = vis.TARGET
h = vis.h
hf = vis.pool.hf
file_mode = vis.pool.file_mode


def git_blob_sha1(payload):
    return hashlib.sha1(b"blob %d\0" % len(payload) + payload, usedforsecurity=False).hexdigest()


def archive_path_refused(name):
    if not isinstance(name, str) or name in {"", ".", "/"}:
        return True
    if name.startswith("/") or name.startswith("./") or "\\" in name or "\x00" in name:
        return True
    parts = Path(name.rstrip("/")).parts
    if ".." in parts:
        return True
    for part in parts:
        if part in {"PaxHeader", "@PaxHeader", "pax_global_header"} or "PaxHeader" in part:
            return True
    return False


def read_archive(data):
    """Header-check every member before reading a regular file. Never extract to disk."""
    h.need(isinstance(data, bytes) and data.startswith(b"\x1f\x8b"), "source archive is not gzip")
    members, blobs, version = {}, {}, None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                h.need(not archive_path_refused(name), "archive path refused")
                if member.islnk() or member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                    raise RuntimeError("special archive member refused")
                mode = stat.S_IMODE(member.mode)
                h.need(mode & ~0o777 == 0, "archive mode refused")
                if member.issym():
                    kind, payload = "symlink", member.linkname.encode()
                elif member.isdir():
                    kind, payload = "dir", None
                elif member.isfile():
                    kind = "file"
                    stream = archive.extractfile(member)
                    h.need(stream is not None, "missing archive member")
                    payload = stream.read()
                else:
                    raise RuntimeError("special archive member refused")
                h.need(name not in members, "duplicate archive member")
                members[name] = {"kind": kind, "mode": mode, "payload": payload}
                if kind != "dir":
                    blobs[name] = git_blob_sha1(payload)
                    if name == "frontend/src/version.ts":
                        version = payload
            comment = archive.pax_headers.get("comment", "")
    except (tarfile.TarError, OSError):
        raise RuntimeError("source archive is not a git tar.gz") from None
    h.need(re.fullmatch(r"[0-9a-f]{40}", comment or ""), "git archive comment missing")
    return {"comment": comment, "members": members, "blobs": blobs, "version_ts": version}


def legal_directories(paths):
    found = set()
    for path in paths:
        parent = path
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            found.add(parent)
    return found


def require_single_tar_umask(pairs):
    """Git archive stores 0666/0777 masked by one tar.umask. Symlinks stay 0777."""
    umask_bits = None
    for git_mode, tar_mode in pairs:
        tar_mode = stat.S_IMODE(tar_mode)
        if git_mode == "120000":
            h.need(tar_mode == 0o777, "archive mode refused")
            continue
        base = 0o666 if git_mode == "100644" else 0o777
        h.need(git_mode in {"100644", "100755", "dir"}, "archive mode refused")
        h.need(tar_mode & ~base == 0, "archive mode refused")
        implied = base & ~tar_mode
        if umask_bits is None:
            umask_bits = implied
        else:
            h.need((umask_bits & base) == implied, "archive mode is not one tar.umask")
    if umask_bits is None:
        umask_bits = 0
    for git_mode, tar_mode in pairs:
        if git_mode == "120000":
            continue
        base = 0o666 if git_mode == "100644" else 0o777
        h.need(stat.S_IMODE(tar_mode) == (base & ~umask_bits), "archive mode refused")
    return umask_bits


def app_version_from_source(text):
    h.need(isinstance(text, bytes), "version source missing")
    found = re.findall(br'(?m)^export const APP_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)";$', text)
    h.need(len(found) == 1, "APP_VERSION is not singular")
    return found[0].decode()


def git_commit_identity(repo, sha):
    h.need(re.fullmatch(r"[0-9a-f]{40}", sha), "commit sha")

    def git(*args):
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
        h.need(result.returncode == 0, "git identity failed")
        return result.stdout

    commit = git("rev-parse", "--verify", sha + "^{commit}").decode().strip()
    h.need(commit == sha, "resolved commit differs")
    parents = tuple(git("rev-parse", sha + "^@").decode().split())
    tree = git("rev-parse", sha + "^{tree}").decode().strip()
    blobs, modes = {}, {}
    for row in git("ls-tree", "-r", "-z", sha).split(b"\0"):
        if not row:
            continue
        meta, path = row.split(b"\t", 1)
        mode, kind, oid = meta.decode().split(" ")
        h.need(kind == "blob" and mode in {"100644", "100755", "120000"}, "tree contains a non-blob")
        name = path.decode()
        h.need(name not in blobs and not archive_path_refused(name), "duplicate tree path")
        blobs[name] = oid
        modes[name] = mode
    return {"commit": commit, "parents": parents, "tree": tree, "blobs": blobs, "modes": modes}


def object_exists(repo, spec):
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", spec],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def frozen_objects_ready(repo):
    if not object_exists(repo, APP_SHA + "^{commit}") or not object_exists(repo, APP_SHA + "^{tree}"):
        return False
    if any(not object_exists(repo, parent + "^{commit}") for parent in PARENTS):
        return False
    identity = git_commit_identity(repo, APP_SHA)
    if identity["parents"] != PARENTS or identity["tree"] != TREE:
        return False
    payload = b"".join(oid.encode() + b"\n" for oid in identity["blobs"].values())
    checked = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch-check"], input=payload, capture_output=True)
    return checked.returncode == 0 and b" missing" not in checked.stdout


def fetch_exact_frozen_commit(repo):
    """Fetch APP_SHA itself. A green fetch of origin/main is not this commit."""
    result = subprocess.run(
        ["git", "-C", str(repo), "fetch", "--no-tags", "--depth=1", "origin", APP_SHA],
        capture_output=True)
    h.need(result.returncode == 0, "frozen commit fetch failed")


def ensure_frozen_commit(repo, fetch=None):
    if not frozen_objects_ready(repo):
        (fetch or fetch_exact_frozen_commit)(repo)
        h.need(frozen_objects_ready(repo), "frozen commit objects missing after fetch")
    return git_commit_identity(repo, APP_SHA)


def canonical_archive(repo, sha):
    """Bytes of `git -c core.autocrlf=false -c core.eol=lf archive` for one commit."""
    h.need(re.fullmatch(r"[0-9a-f]{40}", sha), "archive commit")
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.autocrlf=false", "-c", "core.eol=lf", "archive", "--format=tar.gz", sha],
        capture_output=True)
    h.need(result.returncode == 0, "canonical git archive failed")
    return result.stdout


def compare_archive_to_commit(repo, sha, data):
    identity = git_commit_identity(repo, sha)
    archive = read_archive(data)
    h.need(archive["comment"] == sha, "archive comment is not the requested commit")
    directories = legal_directories(identity["modes"])
    pairs = []
    seen = set()
    for name, member in archive["members"].items():
        if member["kind"] == "dir":
            h.need(name in directories, "archive path refused")
            pairs.append(("dir", member["mode"]))
            continue
        git_mode = identity["modes"].get(name)
        h.need(git_mode is not None, "archive member not in commit")
        if git_mode == "120000":
            h.need(member["kind"] == "symlink", "archive member type refused")
        else:
            h.need(member["kind"] == "file", "archive member type refused")
        pairs.append((git_mode, member["mode"]))
        seen.add(name)
    h.need(seen == set(identity["modes"]), "archive member set differs from commit")
    require_single_tar_umask(pairs)
    if archive["blobs"] != identity["blobs"]:
        missing = len(set(identity["blobs"]) - set(archive["blobs"]))
        extra = len(set(archive["blobs"]) - set(identity["blobs"]))
        changed = len([p for p in set(archive["blobs"]) & set(identity["blobs"]) if archive["blobs"][p] != identity["blobs"][p]])
        h.need(False, "archive bytes differ from git blobs missing=%s extra=%s changed=%s" % (missing, extra, changed))
    identity["app_version"] = app_version_from_source(archive["version_ts"])
    return identity


def assert_release_archive(repo, data):
    identity = compare_archive_to_commit(repo, APP_SHA, data)
    h.need(identity["parents"] == PARENTS, "merge parents differ")
    h.need(identity["tree"] == TREE, "merge tree differs")
    h.need(identity["app_version"] == APP_VERSION, "pinned application version differs")
    h.need(".deploy/requirements_successor_ops.py" not in identity["blobs"], "controller leaked into application archive")
    h.need(".deploy/requirements_successor_smoke.py" not in identity["blobs"], "controller smoke leaked into application archive")
    return identity


def check_archive(repo, archive):
    path = Path(archive)
    h.need(path.is_file() and not path.is_symlink(), "archive path")
    data = path.read_bytes()
    identity = assert_release_archive(Path(repo), data)
    return {
        "status": "passed", "release": APP_SHA, "parents": list(identity["parents"]), "tree": identity["tree"],
        "app_version": identity["app_version"], "files": len(identity["blobs"]), "bytes": len(data),
        "sha256": h.digest(data), "migration_performed": False, "controller_in_archive": False,
    }


def require_same_bytes(before, current, message):
    h.need(current == before, message)


def header_etag(headers):
    etag = None
    for key, value in headers.items():
        if key.lower() == "etag":
            etag = value
    h.need(isinstance(etag, str) and etag.strip(), "caddy etag missing")
    return etag.strip()


def parse_curl_exchange(raw):
    """Split `curl -D - -w '\\n__STATUS__%{http_code}'` output. Do not treat 412 as transport failure."""
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


def find_main_dial(config, old_dial):
    found = []

    def walk(handles, prefix):
        for index, handler in enumerate(handles or []):
            if not isinstance(handler, dict):
                continue
            if handler.get("handler") == "reverse_proxy":
                for upstream_index, upstream in enumerate(handler.get("upstreams") or []):
                    if isinstance(upstream, dict) and upstream.get("dial") == old_dial:
                        found.append("%s/handle/%s/upstreams/%s/dial" % (prefix, index, upstream_index))
            for route_index, route in enumerate(handler.get("routes") or []):
                if isinstance(route, dict):
                    walk(route.get("handle"), "%s/handle/%s/routes/%s" % (prefix, index, route_index))

    for server_name, server in (config.get("apps", {}).get("http", {}).get("servers", {}) or {}).items():
        h.need(re.fullmatch(r"[A-Za-z0-9_.-]+", str(server_name)), "caddy server name refused")
        for route_index, route in enumerate((server or {}).get("routes") or []):
            hosts = {host for match in (route.get("match") or []) for host in (match.get("host") or [])}
            if not (hosts & h.HOSTS):
                continue
            h.need(hosts == h.HOSTS, "unexpected main-site host grouping")
            walk(route.get("handle"), "/config/apps/http/servers/%s/routes/%s" % (server_name, route_index))
    h.need(len(found) == 1, "main dial is not singular")
    return found[0]


def set_config_pointer(config, pointer, value):
    h.need(pointer.startswith("/config/"), "caddy dial path refused")
    node = config
    parts = pointer[len("/config/"):].split("/")
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    last = parts[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


def only_dial_changed(before, after, pointer, new_dial):
    expected = copy.deepcopy(before)
    set_config_pointer(expected, pointer, new_dial)
    return expected == after


def conditional_dial_patch(request, old_dial, new_dial, mutate=None):
    """PATCH one dial with Caddy's documented If-Match. Never POST /load."""
    def export():
        status, _headers, body = request("GET", "/config/")
        h.need(status == 200, "caddy config export failed")
        return json.loads(body)

    if mutate is not None:
        mutate()
    before = export()
    path = find_main_dial(before, old_dial)
    status, headers, body = request("GET", path)
    h.need(status == 200 and json.loads(body) == old_dial, "dial drifted before conditional patch")
    etag = header_etag(headers)
    status, _headers, body = request(
        "PATCH", path, json.dumps(new_dial).encode(),
        {"If-Match": etag, "Content-Type": "application/json"})
    if status == 412:
        status, headers, body = request("GET", path)
        h.need(status == 200 and json.loads(body) == old_dial, "dial etag conflict is not the old upstream; full load refused")
        status, _headers, body = request(
            "PATCH", path, json.dumps(new_dial).encode(),
            {"If-Match": header_etag(headers), "Content-Type": "application/json"})
    h.need(status in {200, 204}, "conditional dial patch failed")
    after = export()
    h.need(only_dial_changed(before, after, path, new_dial), "caddy patch changed other sites")
    h.need(find_main_dial(after, new_dial) == path, "main dial was not replaced")
    return after


def validate(m):
    h.need(m.get("release") == APP_SHA and m.get("old_revision") == OLD_SHA, "release identity")
    h.need(m.get("old_schema") == m.get("target_head") == SCHEMA, "schema must stay held")
    h.need((m.get("old_app"), m.get("old_front")) == (OLD_PREFIX + "-app", OLD_PREFIX + "-frontend"), "production pair")
    h.need(m.get("additional_writers") == [] and not (FORBIDDEN & set(m)), "writer/environment/waiver changes")
    h.need(m.get("caddyfile") == "/opt/it-spareparts/Caddyfile" and type(m.get("port")) is int and 1024 < m["port"] < 65536,
           "endpoint")
    try:
        network = ipaddress.ip_network(m.get("subnet"), strict=True)
    except (TypeError, ValueError):
        network = None
    h.need(network is not None and network.version == 4 and network.is_private and 24 <= network.prefixlen <= 28,
           "private subnet required")
    for key in ("old_app_id", "old_front_id", "source_sha256", "controller_sha256", "smoke_sha256", "nginx_sha256",
                "predecessor_owner_sha256", "evidence_sha256", "db_id", "visibility_sha256", "pool_sha256"):
        h.need(re.fullmatch(r"[a-f0-9]{64}", str(m.get(key, ""))), "invalid identity: " + key)
    h.need(m["visibility_sha256"] == VISIBILITY_DIGEST and m["pool_sha256"] == vis.POOL_DIGEST, "helper chain")
    h.need(m["smoke_sha256"] == SMOKE_DIGEST, "smoke is not the reviewed projection")
    for key in ("old_app_image", "old_front_image", "app_image", "frontend_image", "db_image"):
        h.need(re.fullmatch(r"sha256:[a-f0-9]{64}", str(m.get(key, ""))), "invalid image: " + key)
    h.need(type(m.get("nginx_upstream_count")) is int and m["nginx_upstream_count"] > 0, "nginx upstream count")
    proxies = m.get("proxy_identities")
    h.need(isinstance(proxies, dict) and sorted(p.get("process") for p in proxies.values()) == ["caddy", "cloudflared", "nginx"]
           and all(re.fullmatch(r"[0-9a-f]{64}", str(p.get("id", ""))) and re.fullmatch(r"sha256:[0-9a-f]{64}", str(p.get("image", "")))
                   for p in proxies.values()), "reviewed proxies required")


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
        h.need(self.root == h.BASE / ("requirements-successor-" + self.sha), "release directory mismatch")
        for path, key in ((Path(__file__), "controller_sha256"), (self.root / "readonly-smoke.py", "smoke_sha256"),
                          (self.root / "release-evidence.json", "evidence_sha256")):
            h.need(file_mode(path, 0o600) and h.artifact(path)["sha256"] == self.m[key], "artifact drift")
        h.need(self.m["smoke_sha256"] == SMOKE_DIGEST, "smoke is not the reviewed projection")
        self.old_app, self.old_front = self.m["old_app"], self.m["old_front"]
        self.prefix = "itsp-successor-" + self.sha[:12]
        self.app, self.front, self.net = self.prefix + "-app", self.prefix + "-frontend", self.prefix + "-net"
        self.caddyfile = Path(self.m["caddyfile"])

    def alembic(self, args):
        h.need(isinstance(args, list) and bool(args) and args[0] in ALLOWED_ALEMBIC
               and "upgrade" not in args and "downgrade" not in args, "migration command refused")
        return super().alembic(args)

    def require_source_archive(self):
        path = self.root / "source.tar.gz"
        h.need(file_mode(path, 0o600) and h.artifact(path)["sha256"] == self.m["source_sha256"], "source archive drift")
        archive = read_archive(path.read_bytes())
        h.need(archive["comment"] == self.sha == APP_SHA, "source archive is not the pinned release commit")
        h.need(app_version_from_source(archive["version_ts"]) == APP_VERSION, "source archive version differs")
        return archive

    def predecessor(self):
        path = h.BASE / PREDECESSOR_OWNER
        h.need(file_mode(path, 0o600) and h.artifact(path)["sha256"] == self.m["predecessor_owner_sha256"],
               "predecessor owner drift")
        body = json.loads(path.read_bytes())
        h.need(isinstance(body, dict) and set(body) == {"release", "manifest_sha256"} and body["release"] == OLD_SHA
               and re.fullmatch(r"[a-f0-9]{64}", body["manifest_sha256"]), "predecessor owner identity")
        manifest = h.BASE / ("requirements-visibility-" + OLD_SHA) / "manifest.json"
        h.need(file_mode(manifest, 0o600) and h.artifact(manifest)["sha256"] == body["manifest_sha256"],
               "predecessor manifest drift")
        payload = json.loads(manifest.read_bytes())
        h.need(payload.get("release") == OLD_SHA and payload.get("target_head") == SCHEMA,
               "predecessor manifest does not hold production")
        return body

    def evidence(self):
        path = self.root / "release-evidence.json"
        h.need(file_mode(path, 0o600) and h.artifact(path)["sha256"] == self.m["evidence_sha256"], "release evidence drift")
        evidence = json.loads(path.read_bytes())
        h.need(evidence.get("release") == evidence.get("release_tag_sha") == self.sha == APP_SHA, "evidence identity")
        h.need(evidence.get("parents") == list(PARENTS), "evidence parents")
        h.need(evidence.get("tree") == TREE, "evidence tree")
        h.need(evidence.get("app_version") == APP_VERSION, "evidence version")
        h.need(evidence.get("old_revision") == OLD_SHA and evidence.get("old_schema") == evidence.get("target_head") == SCHEMA,
               "evidence range")
        tag = evidence.get("release_tag")
        h.need(isinstance(tag, str) and tag.startswith("release-") and APP_SHA in tag, "release tag")
        h.need(evidence.get("migration_performed") is False, "evidence claims a migration")
        references = evidence.get("references")
        h.need(isinstance(references, dict), "missing evidence: references")
        for gate in GATES:
            ref = references.get(gate)
            h.need(evidence.get(gate) == "passed" and isinstance(ref, str) and APP_SHA in ref, "missing evidence: " + gate)
        self.require_source_archive()
        self.predecessor()
        return evidence

    def preflight(self):
        result = super().preflight()
        h.need(result.get("schema") == SCHEMA, "production schema is not the held head")
        gate = result.get("import_gate") or {}
        h.need(gate.get("mode") == "all_zero" and gate.get("batches") == [] and gate.get("jobs") == []
               and gate.get("active_return_receipts") == 0, "processing must be empty; old waiver refused")
        return result

    def migrate(self):
        backup = self.marker("backup-old")
        restore = backup.get("restore") or {}
        h.need(restore.get("actual_restore_performed") is True and restore.get("schema") == SCHEMA
               and restore.get("network") == "none" and restore.get("published_ports") == [],
               "verified network-none 98af restore required")
        h.need(backup.get("uploads_content_compared") is True, "uploads content compare required")
        self.frozen()
        self.writers_exact(set())
        self.source_tcp_zero()
        h.need(self.schema() == SCHEMA == self.marker("preflight")["schema"] == self.marker("freeze")["schema"],
               "schema drifted before the no-migration gate")
        self.unchanged_business()
        h.need(self.alembic(["heads"]) == SCHEMA + " (head)", "candidate migration head")
        h.need(self.alembic(["current"]) == SCHEMA + " (head)", "database is not already at the held head")
        self.alembic(["check"])
        self.unchanged_business()
        h.need(self.schema() == SCHEMA and self.counts() == self.marker("freeze")["counts"], "no-migration gate changed data")
        return {"schema": SCHEMA, "migration_performed": False, "upgrade_invoked": False, "downgrade_invoked": False}

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

    def load_active(self, active):
        raise RuntimeError("unconditional config load refused")

    def admin_request(self, method, path, body=None, headers=None):
        command = ["docker", "exec", "-i", h.CADDY, "curl", "-sS", "--max-time", "15", "-D", "-", "-o", "-",
                   "-w", "\n__STATUS__%{http_code}", "-X", method]
        for key, value in (headers or {}).items():
            command.extend(["-H", key + ": " + value])
        if body is not None:
            command.append("--data-binary")
            command.append("@-")
        command.append("http://127.0.0.1:2019" + path)
        return parse_curl_exchange(self.run(command, input=body))

    def cas_file(self, data, inode):
        descriptor = self.caddyfile.stat()
        h.need([descriptor.st_dev, descriptor.st_ino] == inode and not self.caddyfile.is_symlink(), "Caddy inode drift")
        h.need(self.caddyfile.read_bytes() == data, "Caddy file drift")
        inside = self.run(["docker", "exec", h.CADDY, "sha256sum", self.caddy_destination()]).decode().split()[0]
        h.need(inside == h.digest(data), "Caddy bind bytes drift")

    def cutover(self):
        """Patch the one live dial. The deploy locks do not prove exclusive access to Caddy."""
        self.writers_exact({self.app})
        h.need(self.schema() == SCHEMA, "schema drift before cutover")
        blue = self.marker("bluegreen")
        self.frozen()
        h.need(h.fingerprint(self.inspect(self.app)) == blue["app"] and
               h.fingerprint(self.inspect(self.front)) == blue["frontend"], "candidate changed")
        pre = self.marker("preflight")
        old_dial, new_dial = self.old_front + ":80", self.front + ":80"
        after, _desired = self.plan_caddy_change(pre)
        h.write_new(self.root / "Caddyfile.candidate", after)
        h.save(self.root / "exposure-started.json",
               {"release": self.sha, "full_load": False, "after_sha256": h.digest(after)})
        live = self.caddyfile.read_bytes()
        old_bytes, new_bytes = old_dial.encode(), new_dial.encode()
        h.need(live.count(old_bytes) == 1, "live Caddyfile upstream is not singular")
        updated = live.replace(old_bytes, new_bytes, 1)
        self.write_caddy(updated, pre["inode"])
        self.cas_file(updated, pre["inode"])
        applied = conditional_dial_patch(self.admin_request, old_dial, new_dial)
        return {"active_dial": new_dial, "full_load": False, "forward_only_after_exposure": True,
                "other_sites_preserved": True, "config_sha256": h.digest(json.dumps(applied, sort_keys=True).encode())}

    def verify(self):
        self.writers_exact({self.app})
        result = hf.Hotfix.verify(self)
        result["published_version"] = self.require_public_version()
        h.need(self.schema() == SCHEMA, "live schema changed")
        for name in (self.app, self.front):
            image = self.inspect(self.inspect(name)["Image"], True)
            h.need(image["Config"]["Labels"].get("org.opencontainers.image.revision") == self.sha == APP_SHA,
                   "running image is not the pinned release")
        result["schema"] = SCHEMA
        result["migration_performed"] = False
        return result

    def rollback(self):
        """Before exposure only. Keep schema 98af, restart the exact previous writer, restore no dump."""
        h.need(not (self.root / "cutover.intent.json").exists(), "cutover may have begun; forward repair required")
        h.need(not (self.root / "exposure-started.json").exists(), "exposure may have begun; forward repair required")
        pre = self.marker("preflight")
        self.cas((self.root / "Caddyfile.baseline").read_bytes(), pre["active"], pre["inode"])
        head = self.schema()
        h.need(head == SCHEMA, "schema changed; forward repair required")
        names = self.run(["docker", "ps", "-a", "--format", "{{.Names}}"]).decode().splitlines()
        for name, key in ((self.app, "app"), (self.front, "frontend")):
            if name in names:
                row = self.inspect(name)
                h.need(row["Image"] == self.m[key + "_image"] and row["Config"]["Labels"].get("bg.release") == self.sha,
                       "candidate identity drift")
                self.run(["docker", "stop", "--time", "60", row["Id"]])
        old = self.inspect(self.old_app)
        h.need(old["Id"] == self.m["old_app_id"] and old["Image"] == self.m["old_app_image"], "old writer identity drift")
        if not old["State"]["Running"]:
            self.writers_exact(set())
            self.source_tcp_zero()
            if (self.root / "freeze.json").exists():
                self.unchanged_business()
            else:
                h.need(not (self.root / "migrate.intent.json").exists(), "partial migration attempt")
            self.run(["docker", "start", old["Id"]])
        h.need(self.schema() == SCHEMA, "recovery changed schema")
        self.wait_http("https://ybwznt.com/health/db")
        return {"schema": SCHEMA, "schema_preserved": True, "migration_performed": False,
                "restored_dump_into_production": False, "downgrade_invoked": False}

    def execute(self, phase):
        h.need(phase in {*PHASES, "recover"}, "unknown phase")
        h.need(self.path.read_bytes() == self.manifest_bytes, "manifest changed")
        if phase != "recover":
            h.need(not list(self.root.glob("recover-*.intent.json")), "recovered release is terminal")
        predecessor = h.BASE / PREDECESSOR_OWNER
        before = predecessor.read_bytes()
        self.evidence()
        require_same_bytes(before, predecessor.read_bytes(), "predecessor owner changed")
        owner = h.BASE / OWNER_NAME
        identity = {"release": self.sha, "manifest_sha256": h.digest(self.manifest_bytes),
                    "controller_sha256": self.m["controller_sha256"]}
        if not owner.exists():
            h.need(phase == "preflight", "missing preparation")
            h.save(owner, identity)
        h.need(file_mode(owner, 0o600) and json.loads(owner.read_bytes()) == identity, "release ownership mismatch")
        require_same_bytes(before, predecessor.read_bytes(), "predecessor owner changed")
        if phase != "recover" and phase != "preflight":
            self.marker(PHASES[PHASES.index(phase) - 1])
        suffix = phase + ("-" + h.stamp() + "-" + uuid.uuid4().hex if phase in {"verify", "recover"} else "")
        h.save(self.root / (suffix + ".intent.json"), {**identity, "phase": phase, "time": h.stamp()})
        if phase.startswith("backup-"):
            result = self.backup(phase.split("-")[1])
        else:
            result = self.rollback() if phase == "recover" else getattr(self, phase)()
        h.need(isinstance(result, dict), "phase result")
        if phase == "migrate":
            h.need(result.get("migration_performed") is False and result.get("upgrade_invoked") is False
                   and result.get("downgrade_invoked") is False, "migration result")
        receipt = self.root / (suffix + ".json")
        h.save(receipt, {**identity, "phase": phase, "status": "PASS",
                         "time": dt.datetime.now(dt.timezone.utc).isoformat(), **result})
        require_same_bytes(before, predecessor.read_bytes(), "predecessor owner changed")
        print(json.dumps({"phase": phase, "status": "PASS", "receipt": str(receipt), "sha256": h.artifact(receipt)["sha256"]}))


def production_locks():
    locks = []
    try:
        for name in LOCKS:
            fd = os.open(h.BASE / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            locks.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return locks
    except Exception:
        for fd in reversed(locks):
            os.close(fd)
        raise


def run_locked(release, phase):
    locks = production_locks()
    try:
        try:
            release.execute(phase)
        except Exception:
            log = release.root / ("failure-" + h.stamp() + ".private.log")
            h.write_new(log, traceback.format_exc())
            print(json.dumps({"status": "FAILED", "diagnostic": str(log), "rollback_invoked": False}))
            raise SystemExit(1) from None
    finally:
        for fd in reversed(locks):
            os.close(fd)


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) == 4 and argv[1] == "--check-archive":
        print(json.dumps(check_archive(argv[2], argv[3]), ensure_ascii=False, sort_keys=True))
        return 0
    h.need(os.geteuid() == 0 and len(argv) == 3, "usage: requirements_successor_ops.py MANIFEST PHASE (root)")
    previous_umask = os.umask(0o077)
    try:
        run_locked(Release(argv[1]), argv[2])
        return 0
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(1) from None
