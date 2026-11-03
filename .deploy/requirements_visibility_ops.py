#!/usr/bin/env python3
"""Publish D0050/51/53 from the exact shared-pool production release."""
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import traceback
import uuid

OLD_SHA = "559489e6ef548ef84be8112ada1c74117e6839ca"
OLD_HEAD = "e5a7c9d1f3b6"
TARGET = "98afc0d2157b"
OLD_PREFIX = "itsp-pool-" + OLD_SHA[:12]
POOL_DIGEST = "47b141a4127c349c2bb2d785ede3c11645de9d44c4ebd4accc308f9a76e54a39"
helper = Path(__file__).with_name("presales_pool_ops.py")
raw = helper.read_bytes()
if helper.is_symlink() or hashlib.sha256(raw).hexdigest() != POOL_DIGEST:
    raise RuntimeError("reviewed pool controller differs")
spec = importlib.util.spec_from_loader("requirements_pool", loader=None)
pool = importlib.util.module_from_spec(spec)
pool.__file__ = str(helper)
exec(compile(raw, str(helper), "exec"), pool.__dict__)
h = pool.h
PHASES = ("preflight", "images", "freeze", "backup-old", "migrate", "backup-new", "bluegreen", "cutover", "verify")


def validate(m):
    h.need(re.fullmatch("[a-f0-9]{40}", m["release"]) and m["release"] != OLD_SHA, "candidate revision")
    h.need((m["old_revision"], m["old_schema"], m["target_head"]) == (OLD_SHA, OLD_HEAD, TARGET), "release range")
    h.need((m["old_app"], m["old_front"]) == (OLD_PREFIX + "-app", OLD_PREFIX + "-frontend"), "production pair")
    h.need(m["additional_writers"] == [] and not ({"source_handoff", "catalog_sync"} & m.keys()), "writer/environment changes")
    h.need(m["caddyfile"] == "/opt/it-spareparts/Caddyfile" and type(m["port"]) is int and 1024 < m["port"] < 65536, "endpoint")
    for key in ("old_app_id", "old_front_id", "source_sha256", "controller_sha256", "smoke_sha256", "nginx_sha256",
                "predecessor_owner_sha256", "evidence_sha256", "db_id"):
        h.need(re.fullmatch("[a-f0-9]{64}", m[key]), "invalid identity: " + key)
    for key in ("old_app_image", "old_front_image", "app_image", "frontend_image", "db_image"):
        h.need(re.fullmatch("sha256:[a-f0-9]{64}", m[key]), "invalid image: " + key)
    h.need(type(m["nginx_upstream_count"]) is int and m["nginx_upstream_count"] > 0, "nginx upstream count")
    proxies = m["proxy_identities"].values()
    h.need(sorted(p["process"] for p in proxies) == ["caddy", "cloudflared", "nginx"] and all(
        re.fullmatch("[0-9a-f]{64}", p["id"]) and re.fullmatch("sha256:[0-9a-f]{64}", p["image"])
        for p in proxies), "reviewed proxies required")


class Release(pool.Release):
    def __init__(self, manifest):
        self.path = Path(manifest)
        self.root = self.path.parent
        h.need(self.path.name == "manifest.json" and pool.file_mode(self.path, 0o600), "private manifest required")
        h.need(self.root.resolve() == self.root and self.root.is_dir() and self.root.stat().st_uid == 0
               and stat.S_IMODE(self.root.stat().st_mode) == 0o700, "private release directory required")
        self.manifest_bytes = self.path.read_bytes()
        self.m = json.loads(self.manifest_bytes)
        validate(self.m)
        self.sha = self.m["release"]
        h.need(self.root == h.BASE / ("requirements-visibility-" + self.sha), "release directory mismatch")
        for path, key in ((Path(__file__), "controller_sha256"), (self.root / "readonly-smoke.py", "smoke_sha256"),
                          (self.root / "release-evidence.json", "evidence_sha256")):
            h.need(pool.file_mode(path, 0o600) and h.artifact(path)["sha256"] == self.m[key], "artifact drift")
        self.old_app, self.old_front = self.m["old_app"], self.m["old_front"]
        self.prefix = "itsp-requirements-" + self.sha[:12]
        self.app, self.front, self.net = self.prefix + "-app", self.prefix + "-frontend", self.prefix + "-net"
        self.caddyfile = Path(self.m["caddyfile"])

    def evidence(self):
        path = self.root / "release-evidence.json"
        h.need(h.artifact(path)["sha256"] == self.m["evidence_sha256"], "release evidence drift")
        evidence = json.loads(path.read_bytes())
        h.need(evidence["release"] == evidence["release_tag_sha"] == self.sha and evidence["old_revision"] == OLD_SHA
               and evidence["target_head"] == TARGET and evidence["release_tag"].startswith("release-"), "evidence identity")
        for gate in ("required_ci", "independent_review", "candidate_regression", "scope_review", "old_app_on_expanded_schema"):
            h.need(evidence[gate] == "passed" and evidence["references"].get(gate), "missing evidence: " + gate)
        owner = h.BASE / ".presales-pool-active-owner.json"
        h.need(pool.file_mode(owner, 0o600) and h.artifact(owner)["sha256"] == self.m["predecessor_owner_sha256"]
               and json.loads(owner.read_bytes())["release"] == OLD_SHA, "production predecessor changed")

    def preflight(self):
        self.evidence()
        self.writers_exact({self.old_app})
        result = pool.hf.Hotfix.preflight(self)
        self.plan_caddy_change(result)
        h.need(self.alembic(["heads"]) == TARGET + " (head)", "candidate migration head")
        result["protected_configs"] = {name: self.config(self.inspect(name)) for name in result["containers"]}
        return result

    @staticmethod
    def config(row):
        return {key: row[key] for key in ("Config", "HostConfig", "Mounts")}

    def protected(self, *, old_running=None, partial_freeze=False):
        pool.hf.Hotfix.protected(self, old_running=old_running, partial_freeze=partial_freeze)
        pre = self.marker("preflight")
        names = set(self.run(["docker", "ps", "-a", "--format", "{{.Names}}"] ).decode().splitlines())
        h.need(names - set(pre["containers"]) <= {self.app, self.front}, "unreviewed container appeared")
        for name, expected in pre["protected_configs"].items():
            h.need(self.config(self.inspect(name)) == expected, "protected configuration drift: " + name)

    def freeze(self):
        self.writers_exact({self.old_app})
        self.plan_caddy_change(self.marker("preflight"))
        result = pool.hf.Hotfix.freeze(self)
        self.writers_exact(set())
        result["data_fingerprints"] = self.data_fingerprints()
        return result

    def migrate(self):
        backup = self.marker("backup-old")
        h.need(backup["restore"]["actual_restore_performed"] and backup["restore"]["schema"] == OLD_HEAD, "old-head restore required")
        self.unchanged_business()
        result = h.Release.migrate(self)
        self.unchanged_business()
        return result

    def backup(self, label):
        result = super().backup(label)
        if label == "old":
            self.run(["tar", "--compare", "--file", str(self.root / "uploads.tar"), "--directory", "/data/spareparts"], timeout=3600)
            result["uploads_content_compared"] = True
        return result

    def cutover(self):
        self.writers_exact({self.app})
        h.need(self.schema() == TARGET, "schema drift before cutover")
        blue = self.marker("bluegreen")
        self.frozen()
        h.need(h.fingerprint(self.inspect(self.app)) == blue["app"] and
               h.fingerprint(self.inspect(self.front)) == blue["frontend"], "candidate changed")
        pre = self.marker("preflight")
        after, desired = self.plan_caddy_change(pre)
        h.write_new(self.root / "Caddyfile.candidate", after)
        h.save(self.root / "exposure-started.json", {"release": self.sha, "active_after": desired, "after_sha256": h.digest(after)})
        self.cas((self.root / "Caddyfile.baseline").read_bytes(), pre["active"], pre["inode"])
        self.write_caddy(after, pre["inode"])
        self.cas(after, pre["active"], pre["inode"])
        h.need(self.mounted_caddy() == desired, "mounted candidate differs from runtime target")
        self.cas(after, pre["active"], pre["inode"])
        self.load_active(desired)
        self.cas(after, desired, pre["inode"])
        return {"active": desired, "forward_only_after_exposure": True}

    def verify(self):
        self.writers_exact({self.app})
        result = pool.hf.Hotfix.verify(self)
        root = "/usr/share/nginx/html"
        paths = self.run(["docker", "exec", self.front, "grep", "-rlF", "1.45.0", root + "/assets"]).decode().splitlines()
        for path in paths:
            h.need(re.fullmatch(re.escape(root) + r"/assets/[A-Za-z0-9_.-]+", path), "unexpected asset path")
            bundle = self.run(["docker", "exec", self.front, "cat", path])
            phrase = "可见人员"
            if phrase.encode() not in bundle and json.dumps(phrase, ensure_ascii=True)[1:-1].encode() not in bundle.lower():
                continue
            for host in sorted(h.HOSTS):
                h.need(self.http("https://" + host + path[len(root):])["sha256"] == h.digest(bundle), "public changelog differs")
            result["published_changelog"] = {"version": "1.45.0", "asset_sha256": h.digest(bundle)}
            return result
        raise RuntimeError("version 1.45 changelog missing")

    def rollback(self):
        """Before exposure only. Preserve the expanded schema and every business row."""
        h.need(not (self.root / "cutover.intent.json").exists(), "cutover may have begun; forward repair required")
        pre = self.marker("preflight")
        self.cas((self.root / "Caddyfile.baseline").read_bytes(), pre["active"], pre["inode"])
        head = self.schema()
        h.need(head in {OLD_HEAD, TARGET}, "unknown migration state")
        names = self.run(["docker", "ps", "-a", "--format", "{{.Names}}"] ).decode().splitlines()
        for name, key in ((self.app, "app"), (self.front, "frontend")):
            if name in names:
                row = self.inspect(name)
                h.need(row["Image"] == self.m[key + "_image"] and row["Config"]["Labels"].get("bg.release") == self.sha, "candidate identity drift")
                self.run(["docker", "stop", "--time", "60", row["Id"]])
        old = self.inspect(self.old_app)
        h.need(old["Id"] == self.m["old_app_id"] and old["Image"] == self.m["old_app_image"], "old writer identity drift")
        if not old["State"]["Running"]:
            self.writers_exact(set())
            self.source_tcp_zero()
            if (self.root / "freeze.json").exists():
                self.unchanged_business()
            else:
                h.need(head == OLD_HEAD and not (self.root / "migrate.intent.json").exists(), "partial freeze schema drift")
            self.run(["docker", "start", old["Id"]])
        h.need(self.schema() == head, "recovery changed schema")
        self.wait_http("https://ybwznt.com/health/db")
        return {"schema": head, "schema_preserved": True, "restored_dump_into_production": False}

    def execute(self, phase):
        h.need(phase in {*PHASES, "recover"}, "unknown phase")
        h.need(self.path.read_bytes() == self.manifest_bytes, "manifest changed")
        self.evidence()
        owner = h.BASE / ".requirements-visibility-active-owner.json"
        identity = {"release": self.sha, "manifest_sha256": h.digest(self.manifest_bytes)}
        if not owner.exists():
            h.need(phase == "preflight", "missing preparation")
            h.save(owner, identity)
        h.need(pool.file_mode(owner, 0o600) and json.loads(owner.read_bytes()) == identity, "release ownership mismatch")
        if phase != "recover":
            h.need(not list(self.root.glob("recover-*.intent.json")), "recovered release is terminal")
            if phase != "preflight":
                self.marker(PHASES[PHASES.index(phase) - 1])
        suffix = phase + ("-" + h.stamp() + "-" + uuid.uuid4().hex if phase in {"verify", "recover"} else "")
        h.save(self.root / (suffix + ".intent.json"), {**identity, "phase": phase, "time": h.stamp()})
        if phase.startswith("backup-"):
            result = self.backup(phase.split("-")[1])
        else:
            result = self.rollback() if phase == "recover" else getattr(self, phase)()
        receipt = self.root / (suffix + ".json")
        h.save(receipt, {**identity, "phase": phase, "status": "PASS", "time": dt.datetime.now(dt.timezone.utc).isoformat(), **result})
        print(json.dumps({"phase": phase, "status": "PASS", "receipt": str(receipt), "sha256": h.artifact(receipt)["sha256"]}))


if __name__ == "__main__":
    os.umask(0o077)
    h.need(os.geteuid() == 0 and len(sys.argv) == 3, "usage: requirements_visibility_ops.py MANIFEST PHASE (root)")
    release = Release(sys.argv[1])
    locks = []
    try:
        for name in (".catalog-source-handoff.lock", ".itsp-production-deploy.lock"):
            fd = os.open(h.BASE / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            locks.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release.execute(sys.argv[2])
    except Exception:
        log = release.root / ("failure-" + h.stamp() + ".private.log")
        h.write_new(log, traceback.format_exc())
        print(json.dumps({"status": "FAILED", "diagnostic": str(log)}))
        sys.exit(1)
    finally:
        for fd in reversed(locks):
            os.close(fd)
