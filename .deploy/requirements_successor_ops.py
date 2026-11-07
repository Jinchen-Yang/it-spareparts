#!/usr/bin/env python3
"""No-migration successor of the requirements-visibility production owner.

The application archive, tag and image revision are the verified merge
e62cd60cdbcc3fef896c5289388250057c4ce513. This file is a separate audited
controller: it must not be baked into that archive. Historical visibility
constants stay in requirements_visibility_ops.py and are pinned below.
"""
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


def read_archive(data):
    """Return the git pax comment, blob hashes and version.ts bytes. Do not extract to disk."""
    h.need(isinstance(data, bytes) and data.startswith(b"\x1f\x8b"), "source archive is not gzip")
    blobs, version = {}, None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                name = member.name
                if member.isdir() or name in {"pax_global_header", "././@PaxHeader"} or name.endswith("/PaxHeader"):
                    continue
                h.need(bool(name) and not name.startswith("/") and ".." not in Path(name).parts, "archive path refused")
                if member.issym() or member.islnk():
                    payload = member.linkname.encode()
                elif member.isfile():
                    stream = archive.extractfile(member)
                    h.need(stream is not None, "missing archive member")
                    payload = stream.read()
                else:
                    raise RuntimeError("unsupported archive member")
                h.need(name not in blobs, "duplicate archive member")
                if name == "frontend/src/version.ts":
                    version = payload
                blobs[name] = git_blob_sha1(payload)
            comment = archive.pax_headers.get("comment", "")
    except (tarfile.TarError, OSError):
        raise RuntimeError("source archive is not a git tar.gz") from None
    h.need(re.fullmatch(r"[0-9a-f]{40}", comment or ""), "git archive comment missing")
    return {"comment": comment, "blobs": blobs, "version_ts": version}


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
    blobs = {}
    for row in git("ls-tree", "-r", "-z", sha).split(b"\0"):
        if not row:
            continue
        meta, path = row.split(b"\t", 1)
        _mode, kind, oid = meta.decode().split(" ")
        h.need(kind == "blob", "tree contains a non-blob")
        name = path.decode()
        h.need(name not in blobs, "duplicate tree path")
        blobs[name] = oid
    return {"commit": commit, "parents": parents, "tree": tree, "blobs": blobs}


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
        h.need(predecessor.read_bytes() == before, "predecessor owner changed")
        owner = h.BASE / OWNER_NAME
        identity = {"release": self.sha, "manifest_sha256": h.digest(self.manifest_bytes),
                    "controller_sha256": self.m["controller_sha256"]}
        if not owner.exists():
            h.need(phase == "preflight", "missing preparation")
            h.save(owner, identity)
        h.need(file_mode(owner, 0o600) and json.loads(owner.read_bytes()) == identity, "release ownership mismatch")
        h.need(predecessor.read_bytes() == before, "predecessor owner changed")
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
        h.need(predecessor.read_bytes() == before, "predecessor owner changed")
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
    os.umask(0o077)
    h.need(os.geteuid() == 0 and len(argv) == 3, "usage: requirements_successor_ops.py MANIFEST PHASE (root)")
    run_locked(Release(argv[1]), argv[2])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(1) from None
