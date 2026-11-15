"""itsp 循环池发布在 bluegreen 中止后的完成暴露脚本（D-0060）。

前次发布（ccefa17cc644，2026-11-13）在 backup-new PASS 后，bluegreen 于
克隆 frontend 时因 nginx.conf 的 O_EXCL 自碰撞中止；旧栈当晚被人工重启
恢复服务，preflight 容器指纹（started_at）已漂移，原控制器按设计不可
原地续跑。本脚本遵循 D-0059「另建步骤、保留现场证据」的先例，只做剩余
的机械步骤：克隆候选 frontend、健康探测、Caddy 上游条件替换、停旧
app、公网验证，并按 O_EXCL 落回执。

迁移、备份、镜像、dry-run 门禁沿用前次发布的 PASS 回执；本脚本不复核
业务数据，不改数据库，不生成或轮换 token，不打开 SHOP_CYCLE_SELL_ENABLED。
事后 enable-outbound / disable-outbound 仍由原 cycle_pool_ops.py 执行
（其 recreate_app 依赖本脚本产出的 exposure-started.json 与 verify-*.json）。

用法（root）：
    python3 cycle_pool_expose.py <发布目录> prepare|expose|final-verify
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

RELEASE = "ccefa17cc6446f613b7237ac0cd2121256969622"
OLD_SHA = "e62cd60cdbcc3fef896c5289388250057c4ce513"
OLD_SCHEMA = "98afc0d2157b"
TARGET = "c3e8a1d94b27"
OLD_APP = "itsp-successor-" + OLD_SHA[:12] + "-app"
OLD_FRONT = "itsp-successor-" + OLD_SHA[:12] + "-frontend"
APP = "itsp-cycle-ccefa17cc644-app"
FRONT = "itsp-cycle-ccefa17cc644-frontend"
NET = "itsp-cycle-ccefa17cc644-net"
INGRESS = "it-spareparts-ingress"
DEFAULT_NET = "it-spareparts_default"
DB = "it-spareparts-db-1"
CADDY = "it-spareparts-caddy-1"
CADDYFILE = Path("/opt/it-spareparts/Caddyfile")
CYCLE_URL = "https://shop.ybwznt.com/api/integrations/cycle/sell-intents"
HOSTS = ("www.ybwznt.com", "ybwznt.com")
SCRIPT_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

PASS_RECEIPTS = ("preflight", "images", "freeze", "backup-old", "migrate", "backup-new")
ABSENT_RECEIPTS = ("bluegreen.json", "cutover.json", "exposure-started.json")


def need(condition, message):
    if not condition:
        raise RuntimeError(message)


def stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def save_new(path: Path, value):
    data = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def run(argv, *, timeout=120, input_bytes=None):
    tokens = [str(item) for item in argv]
    need(not ("down" in tokens and ({"compose", "docker-compose"} & set(tokens))), "compose down refused")
    completed = subprocess.run(tokens, input=input_bytes, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=timeout)
    need(completed.returncode == 0, "command failed: " + " ".join(tokens[:3]) + " -> " + completed.stdout.decode(errors="replace")[-400:])
    return completed.stdout


def inspect(name):
    return json.loads(run(["docker", "inspect", name]))[0]


def fingerprint(row):
    return {"id": row["Id"], "image": row["Image"], "running": row["State"]["Running"],
            "started_at": row["State"]["StartedAt"], "restart_count": row["RestartCount"],
            "oom_killed": row["State"]["OOMKilled"]}


def env_map(values):
    result = {}
    for entry in values or []:
        key, separator, value = entry.partition("=")
        need(separator == "=" and key not in result, "invalid runtime environment entry")
        result[key] = value
    return result


def digest(data: bytes):
    return hashlib.sha256(data).hexdigest()


def http(url, statuses=(200,)):
    try:
        with urlopen(url, timeout=20) as response:
            code, body = response.status, response.read()
    except HTTPError as exc:
        code, body = exc.code, exc.read()
    need(code in statuses, "HTTP check failed: " + url.split("?")[0] + " -> " + str(code))
    return {"url": url.split("?")[0], "status": code, "sha256": digest(body)}


def schema():
    out = run(["docker", "exec", DB, "psql", "-U", "spareparts", "-d", "spareparts", "-tAc",
               "select version_num from alembic_version"]).decode().strip()
    need(re.fullmatch(r"[0-9a-f]{12}", out), "schema query failed")
    return out


class Expose:
    def __init__(self, root: Path):
        self.root = root
        need(root.is_dir() and not root.is_symlink() and (os.stat(root).st_mode & 0o777) == 0o700,
             "release directory must be private (0700)")
        self.m = json.loads((root / "manifest.json").read_bytes())
        need(self.m["release"] == RELEASE, "manifest release mismatch")
        for key, expected in (("app_image", None), ("frontend_image", None), ("port", 18105),
                              ("old_app", OLD_APP), ("old_front", OLD_FRONT)):
            need(key in self.m, "manifest key missing: " + key)
            if expected is not None:
                need(self.m[key] == expected, "manifest drift: " + key)
        self.identity = {"release": RELEASE, "script_sha256": SCRIPT_SHA}

    # ---------- 公共门禁 ----------

    def receipts(self):
        for name in PASS_RECEIPTS:
            row = json.loads((self.root / (name + ".json")).read_bytes())
            need(row.get("status") == "PASS" and row.get("release") == RELEASE,
                 "prior PASS receipt missing or foreign: " + name)
        for name in ABSENT_RECEIPTS:
            need(not (self.root / name).exists(), "release already advanced: " + name)
        need(not any(self.root.glob("verify-*.json")), "verify receipt already exists")

    def candidate_app_gate(self):
        current = inspect(APP)
        need(current["State"]["Running"] and current["Image"] == self.m["app_image"], "candidate app identity drift")
        need(current["Config"]["Labels"].get("bg.release") == RELEASE, "candidate app release drift")
        need(set(current["NetworkSettings"]["Networks"]) == {NET, DEFAULT_NET}, "candidate app networks drifted")
        values = env_map(current["Config"].get("Env"))
        need(values.get("SHOP_CYCLE_SELL_URL") == CYCLE_URL, "cycle sell url drifted")
        need(values.get("SHOP_CYCLE_SELL_ENABLED") == "false", "outbound flag must stay false here")
        need(digest(values.get("SHOP_CYCLE_SELL_TOKEN", "").encode()) == self.m["cycle_sell_token_sha256"],
             "cycle sell token checksum differs")
        need(not values.get("CYCLE_SELL_CALLBACK_TOKEN_SHA256"), "callback token is out of scope")
        return current

    def caddy_gate(self):
        current = CADDYFILE.read_bytes()
        baseline = (self.root / "Caddyfile.baseline").read_bytes()
        need(current == baseline, "Caddyfile drifted from baseline")
        need(current.count((OLD_FRONT + ":80").encode()) == 1, "upstream target count is not 1")
        return current

    # ---------- prepare：克隆候选 frontend ----------

    def prepare(self):
        self.receipts()
        need(schema() == TARGET, "schema is not c3e8")
        self.candidate_app_gate()
        current = self.caddy_gate()
        need(not (self.root / "expose-prepare.json").exists(), "prepare already completed")
        need(not (self.root / (FRONT + ".create.private.json")).exists(), "frontend request already exists")
        names = run(["docker", "ps", "-a", "--format", "{{.Names}}"]).decode().split()
        need(FRONT not in names, "frontend name already taken")

        nginx = (self.root / "nginx.conf").read_bytes()
        need(digest(nginx) == self.m["nginx_sha256"], "nginx baseline digest differs from manifest")
        replaced = nginx.replace((OLD_APP + ":8000").encode(), (APP + ":8000").encode())
        count = nginx.count((OLD_APP + ":8000").encode())
        need(count >= 1 and replaced != nginx, "nginx upstream replacement made no change")
        nginx_path = self.root / "nginx-cycle.conf"
        save_new(nginx_path, replaced)

        frozen = json.loads((self.root / (OLD_FRONT + ".inspect.private.json")).read_bytes())
        cfg, host = json.loads(json.dumps(frozen["Config"])), json.loads(json.dumps(frozen["HostConfig"]))
        cfg.update(Hostname=FRONT, Image=self.m["frontend_image"])

        def provenance(key):
            return key in {"org.opencontainers.image.revision", "bg.release", "bg.parent"} or str(key).startswith("hotfix.")
        cfg["Labels"] = {k: v for k, v in (cfg.get("Labels") or {}).items() if not provenance(k)}
        cfg["Labels"].update({k: v for k, v in (inspect(self.m["frontend_image"])["Config"].get("Labels") or {}).items() if provenance(k)})
        cfg["Labels"].update({"bg.release": RELEASE, "bg.parent": "/" + OLD_FRONT})
        host["NetworkMode"] = NET
        host["PortBindings"] = {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(self.m["port"])}]}
        mounts = [m for m in host.get("Mounts", []) if m.get("Target") == "/etc/nginx/conf.d/default.conf"]
        need(len(mounts) == 1 and mounts[0]["ReadOnly"], "nginx mount mismatch on frozen frontend")
        mounts[0]["Source"] = str(nginx_path)

        request = {**cfg, "HostConfig": host, "NetworkingConfig": {"EndpointsConfig": {NET: {}}}}
        save_new(self.root / (FRONT + ".create.private.json"), request)
        response = json.loads(run(["curl", "-fsS", "--unix-socket", "/var/run/docker.sock",
                                   "-H", "Content-Type: application/json",
                                   "--data-binary", "@" + str(self.root / (FRONT + ".create.private.json")),
                                   "http://localhost/containers/create?name=" + FRONT]))
        need(re.fullmatch(r"[0-9a-f]{64}", response.get("Id", "")), "frontend creation response invalid")
        run(["docker", "network", "connect", INGRESS, FRONT])
        run(["docker", "start", FRONT])
        base = "http://127.0.0.1:" + str(self.m["port"])
        routes = None
        for attempt in range(60):
            try:
                routes = {path: http(base + path) for path in ("/health", "/health/db")}
                break
            except Exception:
                if attempt == 59:
                    raise
                time.sleep(1)
        need(routes is not None, "frontend health did not come up")
        current_front = inspect(FRONT)
        need(set(current_front["NetworkSettings"]["Networks"]) == {NET, INGRESS}, "frontend networks drifted")
        save_new(self.root / "expose-prepare.json", {
            **self.identity, "phase": "prepare", "status": "PASS", "time": stamp(),
            "frontend": fingerprint(current_front), "routes": routes,
            "nginx_replacements": count, "nginx_cycle_sha256": digest(replaced),
            "caddy_sha256_before": digest(current),
        })

    # ---------- expose：Caddy 条件替换 + 停旧 app ----------

    def expose(self):
        row = json.loads((self.root / "expose-prepare.json").read_bytes())
        need(row.get("status") == "PASS" and row.get("script_sha256") == SCRIPT_SHA, "prepare receipt mismatch")
        self.candidate_app_gate()
        before = self.caddy_gate()
        front = inspect(FRONT)
        need(front["State"]["Running"] and front["Image"] == self.m["frontend_image"], "frontend identity drift")
        base = "http://127.0.0.1:" + str(self.m["port"])
        http(base + "/health/db")

        after = before.replace((OLD_FRONT + ":80").encode(), (FRONT + ":80").encode())
        need(after != before and after.count((FRONT + ":80").encode()) == 1, "upstream replacement failed")
        candidate = self.root / "Caddyfile.expose"
        save_new(candidate, after)
        temporary = "/tmp/cycle-pool-expose-" + uuid.uuid4().hex[:8]
        run(["docker", "cp", str(candidate), CADDY + ":" + temporary])
        run(["docker", "exec", CADDY, "caddy", "validate", "--config", temporary, "--adapter", "caddyfile"])
        matches = [m for m in inspect(CADDY)["Mounts"] if m.get("Type") == "bind" and m.get("Source") == str(CADDYFILE)]
        need(len(matches) == 1, "caddy bind mount mismatch")
        destination = matches[0]["Destination"]
        save_new(self.root / "exposure-started.json", {
            **self.identity, "at": stamp(), "forward_only": True,
            "replaced": OLD_FRONT + ":80 -> " + FRONT + ":80",
            "before_sha256": digest(before), "after_sha256": digest(after),
        })
        stat_before = CADDYFILE.stat()
        fd = os.open(CADDYFILE, os.O_WRONLY | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            need([st.st_dev, st.st_ino] == [stat_before.st_dev, stat_before.st_ino], "caddy inode drift")
            view = memoryview(after)
            while view:
                written = os.write(fd, view)
                need(written > 0, "short caddy write")
                view = view[written:]
            os.ftruncate(fd, len(after))
            os.fsync(fd)
        finally:
            os.close(fd)
        need(CADDYFILE.read_bytes() == after, "caddy content mismatch after write")
        run(["docker", "exec", CADDY, "caddy", "reload", "--config", destination, "--adapter", "caddyfile"])
        public = {}
        for host in HOSTS:
            for path in ("/health", "/health/db", "/"):
                public[host + path] = http("https://" + host + path)
        old_app = inspect(OLD_APP)
        need(old_app["State"]["Running"], "old app already stopped before cutover")
        run(["docker", "stop", "--time", "60", OLD_APP], timeout=120)
        for host in HOSTS:
            public["post-stop:" + host + "/health/db"] = http("https://" + host + "/health/db")
        save_new(self.root / "expose-cutover.json", {
            **self.identity, "phase": "expose", "status": "PASS", "time": stamp(),
            "caddy_before_sha256": digest(before), "caddy_after_sha256": digest(after),
            "old_app_stopped": OLD_APP, "old_app_fingerprint_before_stop": fingerprint(old_app),
            "public": public,
        })

    # ---------- final-verify：公网资产与日志 ----------

    def final_verify(self):
        row = json.loads((self.root / "expose-cutover.json").read_bytes())
        need(row.get("status") == "PASS" and row.get("script_sha256") == SCRIPT_SHA, "cutover receipt mismatch")
        need(schema() == TARGET, "live schema changed")
        base = "http://127.0.0.1:" + str(self.m["port"])
        result = {host: {path: http("https://" + host + path) for path in ("/health", "/health/db")}
                  for host in HOSTS}
        index = run(["docker", "exec", FRONT, "cat", "/usr/share/nginx/html/index.html"])
        paths = ["/"] + re.findall(r'(?:src|href)="(/assets/[^"]+)"', index.decode())
        for path in paths:
            expected = http(base + path)
            for host in HOSTS:
                need(http("https://" + host + path) == expected, "public asset mismatch: " + host + path)
        cut_time = row["time"]
        logs = {}
        for name in (APP, FRONT):
            raw = run(["docker", "logs", "--since", cut_time, name], timeout=120)
            save_new(self.root / (name + "-expose-" + stamp() + ".private.log"), raw)
            need(not re.search(rb'Traceback|\bERROR\b|" 5[0-9][0-9] |(?:^|\s)status=5[0-9][0-9](?:\s|$)', raw),
                 "candidate error log: " + name)
            logs[name] = len(raw)
        need(not inspect(OLD_APP)["State"]["Running"], "old app must stay stopped")
        need(inspect(OLD_FRONT)["State"]["Running"], "old frontend kept for rollback reference")
        counts = run(["docker", "exec", DB, "psql", "-U", "spareparts", "-d", "spareparts", "-tAc",
                      "select (select count(*) from maintenance_repair_record), "
                      "(select count(*) from maintenance_cycle_sell_intent), "
                      "(select count(*) from sys_user where permissions->>'page_cycle_repair' = 'true' "
                      " or perm_overrides->>'page_cycle_repair' = 'true')"]).decode().strip()
        save_new(self.root / ("verify-" + stamp() + "-expose.json"), {
            **self.identity, "phase": "final-verify", "status": "PASS", "time": stamp(),
            "public": result, "assets_verified": len(paths), "log_bytes": logs,
            "db_cycle_counts_and_pagekey_users": counts, "schema": TARGET,
        })


def main():
    need(len(sys.argv) == 3, "usage: cycle_pool_expose.py <release_dir> prepare|expose|final-verify")
    phase = sys.argv[2]
    need(phase in {"prepare", "expose", "final-verify"}, "unknown phase")
    task = Expose(Path(sys.argv[1]))
    getattr(task, phase.replace("-", "_"))()
    print(json.dumps({"phase": phase, "status": "PASS", "script_sha256": SCRIPT_SHA}))


if __name__ == "__main__":
    main()
