#!/usr/bin/env python3
"""Exact-image, same-schema hotfix; private receipts, backup and reversible routing."""
import datetime as dt
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import traceback

HELPER_SHA = "054e67d143a808e5672eea716573ac23a4ecf47ae6cd9a762a89901acfff5f16"
HELPER = Path(__file__).with_name("presales3_ops.py")
raw = HELPER.read_bytes()
if HELPER.is_symlink() or hashlib.sha256(raw).hexdigest() != HELPER_SHA:
    raise RuntimeError("reviewed helper checksum differs")
spec = importlib.util.spec_from_loader("hotfix_reviewed_helper", loader=None)
h = importlib.util.module_from_spec(spec)
h.__file__ = str(HELPER)
exec(compile(raw, str(HELPER), "exec"), h.__dict__)


class Hotfix(h.Release):
    def __init__(self, manifest):
        self.path = Path(manifest)
        self.root = self.path.parent
        h.need(self.path.name == "manifest.json" and not self.path.is_symlink(), "manifest path")
        h.need(self.root.resolve() == self.root and self.root.stat().st_uid == 0
               and stat.S_IMODE(self.root.stat().st_mode) == 0o700, "private root-owned release directory required")
        self.manifest_bytes = self.path.read_bytes()
        self.m = json.loads(self.manifest_bytes)
        h.need(not ({"source_handoff", "catalog_sync"} & self.m.keys()), "cross-system/environment changes forbidden")
        self.sha = self.m["release"]
        h.need(re.fullmatch("[0-9a-f]{40}", self.sha), "release SHA")
        h.need(self.root == h.BASE / ("presales-load-" + self.sha), "release directory identity")
        h.need(self.m["old_revision"] == "e6cfd25c51bc19c1f80d0bad23037d71e6d2aa0e", "unexpected parent")
        h.need(self.m["old_schema"] == self.m["target_head"] == "a3c5e7f9b2d4", "schema changes forbidden")
        self.old_app, self.old_front = self.m["old_app"], self.m["old_front"]
        h.need(self.old_app == "itsp-presales3-e6cfd25c51bc-app"
               and self.old_front == "itsp-presales3-e6cfd25c51bc-frontend", "old resource names")
        for key in ("old_app_id", "old_front_id"):
            h.need(re.fullmatch("[0-9a-f]{64}", self.m[key]), "old container ID")
        for key in ("old_app_image", "old_front_image", "app_image", "frontend_image"):
            h.need(re.fullmatch("sha256:[0-9a-f]{64}", self.m[key]), "image identity")
        h.need(self.m.get("additional_writers") == [], "only reviewed current writer allowed")
        h.need(self.m["caddyfile"] == "/opt/it-spareparts/Caddyfile", "Caddy path")
        h.need(type(self.m["port"]) is int and 1024 < self.m["port"] < 65536, "port")
        h.need(h.artifact(Path(__file__))["sha256"] == self.m["controller_sha256"], "controller changed")
        h.need(h.artifact(HELPER)["sha256"] == self.m["helper_sha256"] == HELPER_SHA, "helper changed")
        h.need(h.artifact(self.root / "readonly-smoke.py")["sha256"] == self.m["smoke_sha256"], "smoke changed")
        self.prefix = "itsp-presales-hf-" + self.sha[:12]
        self.app, self.front, self.net = self.prefix + "-app", self.prefix + "-frontend", self.prefix + "-net"
        self.caddyfile = Path(self.m["caddyfile"])

    def preflight(self):
        names = self.run(["docker", "ps", "-a", "--format", "{{.Names}}"]).decode().splitlines()
        h.need(not any(name.startswith(self.prefix) for name in names), "candidate resource collision")
        rows = json.loads(self.run(["docker", "inspect", *names]))
        for name, key in ((self.old_app, "app"), (self.old_front, "front")):
            row = self.inspect(name)
            h.need(row["Id"] == self.m["old_" + key + "_id"] and row["Image"] == self.m["old_" + key + "_image"]
                   and row["State"]["Running"], "old release identity/running drift")
            h.need(self.inspect(row["Image"], True)["Config"]["Labels"].get("org.opencontainers.image.revision")
                   == self.m["old_revision"], "old image revision")
            h.save(self.root / (name + ".inspect.private.json"), row)
        target = self.database_target(self.inspect(self.old_app))
        h.need(target in {("db", 5432, "/spareparts"), (h.DB, 5432, "/spareparts")}, "database target")
        h.need({row["Name"].lstrip("/") for row in rows if row["State"]["Running"]
                and self.database_target(row) == target} == {self.old_app}, "unreviewed database writer")
        h.need(self.schema() == self.m["old_schema"], "production schema")
        self.nginx_baseline(self.inspect(self.old_front))
        for key in ("app", "frontend"):
            image = self.inspect(self.m[key + "_image"], True)
            h.need(image["Id"] == self.m[key + "_image"] and image["Config"]["Labels"].get("org.opencontainers.image.revision") == self.sha,
                   "candidate image/source mismatch")
        h.need(h.artifact(self.root / "source.tar.gz")["sha256"] == self.m["source_sha256"], "source checksum")
        h.need(os.statvfs(self.root).f_bavail * os.statvfs(self.root).f_frsize >= 20 * 1024**3, "backup space below 20 GiB")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.m["port"]))
        subnet = ipaddress.ip_network(self.m["subnet"], strict=True)
        h.need(subnet.version == 4 and subnet.is_private and subnet.prefixlen >= 24, "private subnet required")
        ids = self.run(["docker", "network", "ls", "-q"]).decode().split()
        for network in json.loads(self.run(["docker", "network", "inspect", *ids])):
            for entry in network.get("IPAM", {}).get("Config", []) or []:
                if entry.get("Subnet"):
                    existing = ipaddress.ip_network(entry["Subnet"])
                    h.need(existing.version != 4 or not subnet.overlaps(existing), "network collision")
        data, active, st = self.caddyfile.read_bytes(), self.active(), self.caddyfile.stat()
        h.need(data.count((self.old_front + ":80").encode()) == 1, "Caddy source upstream count")
        h.need(all(h.site_dials(active).get(host) == [self.old_front + ":80"] for host in h.HOSTS), "Caddy active upstream")
        inode = [st.st_dev, st.st_ino]
        self.cas(data, active, inode)
        h.write_new(self.root / "Caddyfile.baseline", data)
        h.save(self.root / "all-inspect.private.json", rows)
        return {"containers": {row["Name"].lstrip("/"): h.fingerprint(row) for row in rows}, "active": active,
                "inode": inode, "schema": self.schema(), "counts": self.counts(), "db_image": self.inspect(h.DB)["Image"],
                "import_gate": self.import_gate()}

    def images(self):
        self.marker("preflight")
        self.protected()
        return {key: self.m[key + "_image"] for key in ("app", "frontend")}

    def freeze(self):
        self.marker("images")
        self.protected()
        pre = self.marker("preflight")
        self.cas((self.root / "Caddyfile.baseline").read_bytes(), pre["active"], pre["inode"])
        h.need(self.schema() == pre["schema"] and self.import_gate() == pre["import_gate"], "freeze precondition drift")
        self.run(["docker", "stop", "--time", "60", self.m["old_app_id"]], timeout=90)
        self.protected(old_running=False)
        self.source_tcp_zero()
        h.need(self.import_gate() == pre["import_gate"], "imports changed while stopping")
        return {"schema": self.schema(), "counts": self.counts(), "stopped_ids": [self.m["old_app_id"]]}

    def migrate(self):
        self.marker("backup-old")
        self.frozen()
        h.need(self.alembic(["heads"]) == self.m["target_head"] + " (head)", "candidate migration head")
        self.alembic(["upgrade", "head"])
        self.alembic(["check"])
        h.need(self.schema() == self.m["old_schema"] and self.counts() == self.marker("freeze")["counts"], "schema/data changed")
        return {"schema": self.schema(), "schema_change": False}

    def bluegreen(self):
        self.marker("migrate")
        self.frozen()
        self.run(["docker", "network", "create", "--label", "itsp.release=" + self.sha, "--subnet", self.m["subnet"], self.net])
        for name, old, image, front in ((self.app, self.old_app, self.m["app_image"], False), (self.front, self.old_front, self.m["frontend_image"], True)):
            snapshot = json.loads((self.root / (old + ".inspect.private.json")).read_text())
            self.clone(name, snapshot, image, front)
            self.run(["docker", "start", name])
        base = "http://127.0.0.1:" + str(self.m["port"])
        self.wait_http(base + "/health/db")
        routes = self.smoke(base)
        self.frozen()
        return {"app": h.fingerprint(self.inspect(self.app)), "frontend": h.fingerprint(self.inspect(self.front)), "routes": routes}

    def load_active(self, active):
        raw = json.dumps(active, separators=(",", ":")).encode()
        self.run(["docker", "exec", "-i", h.CADDY, "curl", "-fsS", "--max-time", "15", "-X", "POST",
                  "-H", "Content-Type: application/json", "--data-binary", "@-", "http://127.0.0.1:2019/load"], input=raw)

    def cutover(self):
        blue = self.marker("bluegreen")
        self.frozen()
        h.need(h.fingerprint(self.inspect(self.app)) == blue["app"] and h.fingerprint(self.inspect(self.front)) == blue["frontend"], "candidate changed")
        pre = self.marker("preflight")
        before = (self.root / "Caddyfile.baseline").read_bytes()
        after = before.replace((self.old_front + ":80").encode(), (self.front + ":80").encode())
        desired = h.replace_active(pre["active"], self.old_front + ":80", self.front + ":80")
        h.write_new(self.root / "Caddyfile.candidate", after)
        adapted = []
        for suffix in ("baseline", "candidate"):
            temporary = "/tmp/" + self.prefix + "-" + suffix
            self.run(["docker", "cp", str(self.root / ("Caddyfile." + suffix)), h.CADDY + ":" + temporary])
            self.run(["docker", "exec", h.CADDY, "caddy", "validate", "--config", temporary, "--adapter", "caddyfile"])
            adapted.append(json.loads(self.run(["docker", "exec", h.CADDY, "caddy", "adapt", "--config", temporary, "--adapter", "caddyfile"])))
        h.need(h.replace_active(adapted[0], self.old_front + ":80", self.front + ":80") == adapted[1], "source adaptation changed other settings")
        self.cas(before, pre["active"], pre["inode"])
        h.save(self.root / "exposure-started.json", {"release": self.sha, "active_after": desired, "after_sha256": h.digest(after)})
        self.write_caddy(after, pre["inode"])
        self.load_active(desired)
        self.cas(after, desired, pre["inode"])
        return {"active": desired, "data_preserving_rollback": True}

    def verify(self):
        cut, pre = self.marker("cutover"), self.marker("preflight")
        self.cas((self.root / "Caddyfile.candidate").read_bytes(), cut["active"], pre["inode"])
        h.need(self.schema() == self.m["target_head"], "live schema changed")
        result = {host: self.smoke("https://" + host) for host in sorted(h.HOSTS)}
        index = self.run(["docker", "exec", self.front, "cat", "/usr/share/nginx/html/index.html"])
        paths = ["/"] + re.findall(r'(?:src|href)="(/assets/[^\"]+)"', index.decode())
        for path in paths:
            expected = self.http("http://127.0.0.1:" + str(self.m["port"]) + path)
            for host in h.HOSTS:
                h.need(self.http("https://" + host + path) == expected, "public asset mismatch")
        blue = self.marker("bluegreen")
        for name, key in ((self.app, "app"), (self.front, "frontend")):
            h.need(h.fingerprint(self.inspect(name)) == blue[key], "candidate restart/identity changed")
            raw = self.run(["docker", "logs", "--since", cut["time"], name], merge_stderr=True)
            h.write_new(self.root / (name + "-" + h.stamp() + ".private.log"), raw)
            h.need(not h.has_log_errors(raw), "candidate error log")
        self.protected(old_running=False)
        return {"public": result, "assets_verified": len(paths), "schema": self.schema()}

    def rollback(self):
        pre = self.marker("preflight")
        h.need(self.schema() == self.m["old_schema"], "rollback requires unchanged schema")
        before = (self.root / "Caddyfile.baseline").read_bytes()
        candidate = self.root / "Caddyfile.candidate"
        after = candidate.read_bytes() if candidate.exists() else before
        new_active = h.replace_active(pre["active"], self.old_front + ":80", self.front + ":80")
        current_data, current_active = self.caddyfile.read_bytes(), self.active()
        h.need(current_data in (before, after) and current_active in (pre["active"], new_active), "unknown Caddy state")
        h.need([self.caddyfile.stat().st_dev, self.caddyfile.stat().st_ino] == pre["inode"], "Caddy inode changed")
        names = self.run(["docker", "ps", "-a", "--format", "{{.Names}}"]).decode().splitlines()
        if self.app in names:
            row = self.inspect(self.app)
            h.need(row["Image"] == self.m["app_image"] and row["Config"]["Labels"].get("bg.release") == self.sha, "rollback candidate identity")
            if row["State"]["Running"]:
                self.run(["docker", "stop", "--time", "60", row["Id"]], timeout=90)
        old = self.inspect(self.old_app)
        h.need(old["Id"] == self.m["old_app_id"] and old["Image"] == self.m["old_app_image"], "rollback old identity")
        if not old["State"]["Running"]:
            self.run(["docker", "start", old["Id"]])
        self.wait_http("http://127.0.0.1:18094/health/db")
        self.cas(current_data, current_active, pre["inode"])
        self.write_caddy(before, pre["inode"])
        self.load_active(pre["active"])
        self.cas(before, pre["active"], pre["inode"])
        self.wait_http("https://ybwznt.com/health/db")
        return {"old_app_restored": self.m["old_app_id"], "database_restored": False, "schema": self.schema()}

    recover = rollback

    def execute(self, phase):
        h.need(phase in {"preflight", "images", "freeze", "backup-old", "migrate", "bluegreen", "cutover", "verify", "rollback", "recover"}, "unknown phase")
        owner = h.BASE / ".presales-load-hotfix-owner.json"
        identity = {"release": self.sha, "manifest_sha256": h.digest(self.manifest_bytes)}
        if not owner.exists():
            h.need(phase == "preflight", "missing hotfix owner")
            h.save(owner, identity)
        h.need(not owner.is_symlink() and json.loads(owner.read_text()) == identity, "another hotfix owns release")
        name = phase + ("-" + h.stamp() if phase == "verify" else "")
        h.save(self.root / (name + ".intent.json"), {**identity, "phase": phase, "time": h.stamp()})
        result = self.backup("old") if phase == "backup-old" else getattr(self, phase)()
        target = self.root / (name + ".json")
        h.save(target, {**identity, "phase": phase, "status": "PASS", "time": dt.datetime.now(dt.timezone.utc).isoformat(), **result})
        print(json.dumps({"status": "PASS", "phase": phase, "receipt": str(target), "sha256": h.artifact(target)["sha256"]}))


def main():
    os.umask(0o077)
    h.need(len(sys.argv) == 3 and os.geteuid() == 0, "usage: controller MANIFEST PHASE (root)")
    release = Hotfix(sys.argv[1])
    locks = [os.open(h.BASE / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
             for name in (".catalog-source-handoff.lock", ".itsp-production-deploy.lock")]
    for lock in locks:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        release.execute(sys.argv[2])
    except Exception:
        path = release.root / ("failure-" + h.stamp() + ".private.log")
        h.write_new(path, traceback.format_exc())
        print(json.dumps({"status": "FAILED", "private_diagnostic": str(path)}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
