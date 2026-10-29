"""纯合成发布控制器测试；只在 jinchen-dev 执行，无生产连接。"""
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "presales3_ops.py"
SPEC = importlib.util.spec_from_file_location("presales3_ops", SOURCE)
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


def manifest():
    return {"release": "a" * 40, "old_revision": "b" * 40, "old_schema": "oldhead",
            "target_head": "newhead", "old_app": "it-spareparts-app-r142",
            "old_front": "it-spareparts-frontend-r142", "old_app_id": "c" * 64,
            "old_front_id": "d" * 64, "old_app_image": "sha256:" + "e" * 64,
            "old_front_image": "sha256:" + "f" * 64, "app_image": "sha256:" + "1" * 64,
            "frontend_image": "sha256:" + "2" * 64, "port": 18299,
            "caddyfile": "/opt/it-spareparts/Caddyfile", "subnet": "172.30.249.0/24",
            "source_sha256": "3" * 64, "smoke_sha256": "4" * 64, "controller_sha256": "5" * 64,
            "additional_writers": [], "nginx_sha256": "6" * 64, "nginx_upstream_count": 3}


def config():
    return {"apps": {"http": {"servers": {"main": {"routes": [
        {"match": [{"host": sorted(ops.HOSTS)}], "handle": [{"handler": "subroute", "routes": [
            {"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "old:80"}]}]}]}]},
        {"match": [{"host": ["shop.ybwznt.com"]}], "handle": [{"handler": "reverse_proxy",
            "upstreams": [{"dial": "shop:80"}], "transport": {"protocol": "http"}}]},
    ]}}}}, "admin": {"listen": "localhost:2019"}}


def container(name="oldapp", running=True):
    return {"Id": name + "-id", "Name": "/" + name, "Image": name + "-image", "RestartCount": 0,
            "State": {"Running": running, "StartedAt": "2026-10-28T00:00:00Z", "OOMKilled": False}}


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.release = ops.Release.__new__(ops.Release)
        self.release.root = self.root
        self.release.m = manifest()
        self.release.sha = "a" * 40
        self.release.manifest_bytes = json.dumps(manifest()).encode()
        self.release.prefix = "itsp-presales3-test"
        self.release.app, self.release.front = "newapp", "newfront"
        self.release.old_app, self.release.old_front = "oldapp", "oldfront"
        self.release.net = "newnet"

    def test_python310_syntax(self):
        ast.parse(SOURCE.read_text(), feature_version=(3, 10))

    def test_manifest_binds_explicit_baseline_and_future_head(self):
        valid = manifest()
        root = ops.BASE / ("presales3-" + valid["release"])
        self.assertEqual(ops.validate_manifest(valid, root), valid)
        for field, value in (("old_revision", ""), ("old_schema", ""), ("old_app", "other-app"),
                             ("target_head", "head;rm"), ("smoke_sha256", ""), ("port", True),
                             ("subnet", "8.8.8.0/24"), ("old_front_id", "short")):
            with self.subTest(field=field), self.assertRaises((RuntimeError, ValueError)):
                ops.validate_manifest({**valid, field: value}, root)

    def test_artifact_stream_hash_and_exclusive_private_write(self):
        path = self.root / "data"
        content = b"synthetic" * 500000
        ops.write_new(path, content)
        self.assertEqual(ops.artifact(path)["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            ops.write_new(path, "must not overwrite")
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(FileExistsError):
            ops.write_new(link, "must not follow")
        self.assertEqual(path.read_bytes(), content)

    def test_active_config_changes_only_main_upstream(self):
        before = config()
        after = ops.replace_active(before, "old:80", "new:80")
        self.assertEqual(ops.site_dials(after)["ybwznt.com"], ["new:80"])
        self.assertEqual(ops.site_dials(after)["shop.ybwznt.com"], ["shop:80"])
        self.assertEqual(before, config())
        wanted = copy.deepcopy(before)
        wanted["apps"]["http"]["servers"]["main"]["routes"][0]["handle"][0]["routes"][0]["handle"][0]["upstreams"][0]["dial"] = "new:80"
        self.assertEqual(after, wanted)

    def test_active_config_rejects_multiple_or_extra_hosts(self):
        multiple = config()
        handler = multiple["apps"]["http"]["servers"]["main"]["routes"][0]["handle"][0]["routes"][0]["handle"][0]
        handler["upstreams"].append({"dial": "old:80"})
        with self.assertRaises(RuntimeError):
            ops.replace_active(multiple, "old:80", "new:80")
        extra = config()
        extra["apps"]["http"]["servers"]["main"]["routes"][0]["match"][0]["host"].append("shop.ybwznt.com")
        with self.assertRaises(RuntimeError):
            ops.replace_active(extra, "old:80", "new:80")

    def test_protected_allows_only_expected_old_app_stop(self):
        rows = {"oldapp": container(), "db": container("db")}
        self.release.marker = Mock(return_value={"containers": {k: ops.fingerprint(v) for k, v in rows.items()}})
        stopped = {**rows, "oldapp": container(running=False)}
        self.release.inspect = Mock(side_effect=lambda n: stopped[n])
        self.assertTrue(self.release.protected(old_running=False))
        stopped["db"] = container("db", running=False)
        with self.assertRaisesRegex(RuntimeError, "db"):
            self.release.protected(old_running=False)

    def test_exposure_marker_forbids_rollback_before_mutation(self):
        ops.write_new(self.root / "exposure-started.json", "{}")
        ops.write_new(self.root / "Caddyfile.baseline", "baseline")
        self.release.marker = Mock(return_value={"active": {}, "inode": [1, 2]})
        self.release.protected = Mock()
        self.release.cas = Mock()
        self.release.run = Mock()
        with self.assertRaisesRegex(RuntimeError, "forward-only"):
            self.release.rollback()
        self.release.run.assert_not_called()

    def test_rollback_stops_candidate_before_row_hash_and_never_overwrites(self):
        self.release.frozen = Mock()
        self.release.marker = Mock(return_value={"counts": {"x": 1}})
        self.release.counts = Mock(return_value={"x": 1})
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return b"newapp\n" if argv[1] == "ps" else b""
        self.release.run = run
        self.release.inspect = Mock(return_value={"Id": "owned", "Config": {"Labels": {"bg.release": self.release.sha}}})
        def changed():
            self.assertEqual(calls[-1], ["docker", "stop", "--time", "60", "owned"])
            raise RuntimeError("content changed; forward-only")
        self.release.unchanged_business = changed
        self.release.alembic = Mock()
        with self.assertRaisesRegex(RuntimeError, "content changed"):
            self.release.rollback()
        self.release.alembic.assert_not_called()
        self.assertFalse(any("start" in call or "pg_restore" in call for call in calls))

    def test_cutover_marker_is_durable_before_any_exposure(self):
        release = self.release
        release.old_front = "old"
        pre = {"active": config(), "inode": [1, 2]}
        new = container("newapp")
        front = container("newfront")
        release.marker = Mock(side_effect=lambda n: pre if n == "preflight" else {"app": ops.fingerprint(new), "frontend": ops.fingerprint(front)})
        release.frozen = Mock()
        release.inspect = Mock(side_effect=lambda n: new if n == "newapp" else front)
        release.schema = Mock(return_value="newhead")
        release.smoke = Mock()
        release.cas = Mock()
        release.caddy_destination = Mock(return_value="/etc/caddy/Caddyfile")
        ops.write_new(self.root / "Caddyfile.baseline", "reverse_proxy old:80")
        desired = ops.replace_active(config(), "old:80", "newfront:80")
        exposure = self.root / "exposure-started.json"
        def run(argv, **kwargs):
            if "adapt" in argv:
                return json.dumps(desired).encode()
            if "reload" in argv:
                self.assertTrue(exposure.exists())
                raise RuntimeError("simulated reload outcome unknown")
            return b""
        release.run = run
        release.write_caddy = Mock(side_effect=lambda *args: self.assertTrue(exposure.exists()))
        with self.assertRaisesRegex(RuntimeError, "outcome unknown"):
            release.cutover()
        self.assertTrue(json.loads(exposure.read_text())["forward_only"])
        release.write_caddy.assert_called_once()

    def test_restore_isolated_and_owned_cleanup_after_failure(self):
        release = self.release
        release.marker = Mock(return_value={"db_image": "sha256:" + "d" * 64})
        restore_name = release.prefix + "-restore-test"
        row = {"Id": "owned-id", "Name": "/" + restore_name,
               "Config": {"Labels": {"itsp.release": release.sha}},
               "HostConfig": {"NetworkMode": "none", "PortBindings": {}}}
        release.inspect = Mock(return_value=row)
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if "--version" in argv:
                return b"postgres (PostgreSQL) 15.10\n"
            if "-d" in argv and "run" in argv:
                return b"owned-id\n"
            if "pg_restore" in argv:
                raise RuntimeError("synthetic failed restore")
            return b""
        release.run = run
        dump = self.root / "dump"
        ops.write_new(dump, b"synthetic")
        with patch.object(ops.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), self.assertRaisesRegex(RuntimeError, "failed restore"):
            release.restore(dump, "head", {}, "test")
        create = next(call for call in calls if "--pids-limit" in call)
        self.assertIn("--memory", create)
        self.assertIn("none", create)
        self.assertNotIn("-p", create)
        self.assertEqual(calls[-1], ["docker", "rm", "-f", "-v", "owned-id"])

    def test_run_can_observe_errors_on_docker_stderr(self):
        completed = subprocess.CompletedProcess([], 0, b"ERROR: synthetic log\n", None)
        with patch.object(ops.subprocess, "run", return_value=completed) as run:
            result = self.release.run(["docker", "logs", "candidate"], merge_stderr=True)
        self.assertIn(b"ERROR", result)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.STDOUT)

    def test_incomplete_freeze_can_only_recover_original_writers_without_migration(self):
        release = self.release
        release.m["additional_writers"] = [{"name": "legacy", "id": "legacy-id", "image": "legacy-image"}]
        rows = {"oldapp": {**container(), "Id": release.m["old_app_id"], "Image": release.m["old_app_image"]},
                "legacy": container("legacy")}
        pre = {"schema": "oldhead", "active": {}, "inode": [1, 2]}
        release.marker = Mock(return_value=pre)
        release.schema = Mock(return_value="oldhead")
        release.sql = Mock(return_value="0")
        release.inspect = Mock(side_effect=lambda name: rows[name])
        release.protected, release.cas, release.wait_http = Mock(), Mock(), Mock()
        release.data_fingerprints = Mock(side_effect=RuntimeError("synthetic hash failure after stop"))
        release.counts = Mock(return_value={})
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            for row in rows.values():
                if argv[-1] == row["Id"]:
                    row["State"]["Running"] = "start" in argv
            return b""
        release.run = run
        ops.write_new(self.root / "freeze.intent.json", "{}")
        ops.write_new(self.root / "Caddyfile.baseline", "baseline")
        with self.assertRaisesRegex(RuntimeError, "hash failure"):
            release.freeze()
        self.assertTrue(all(not row["State"]["Running"] for row in rows.values()))
        self.assertFalse((self.root / "freeze.json").exists())
        result = release.recover_freeze()
        self.assertEqual(set(result["restored_ids"]), {release.m["old_app_id"], "legacy-id"})
        self.assertTrue(all(row["State"]["Running"] for row in rows.values()))
        self.assertFalse(any("pg_restore" in call or "alembic" in call for call in calls))

    def test_incomplete_freeze_recovery_refuses_migration_intent(self):
        ops.write_new(self.root / "freeze.intent.json", "{}")
        ops.write_new(self.root / "migrate.intent.json", "{}")
        self.release.run = Mock()
        with self.assertRaisesRegex(RuntimeError, "migration or exposure"):
            self.release.recover_freeze()
        self.release.run.assert_not_called()

    def test_nginx_preserves_three_frozen_upstream_tokens_and_rejects_drift(self):
        raw = b"health oldapp:8000\nhealth_db oldapp:8000\napi oldapp:8000\nunchanged other:8000\n"
        path = self.root / "nginx.source"
        ops.write_new(path, raw)
        self.release.m["nginx_sha256"] = ops.digest(raw)
        frontend = {"HostConfig": {"Mounts": [{"Target": "/etc/nginx/conf.d/default.conf", "Source": str(path), "ReadOnly": True}]}}
        old = self.release.nginx_baseline(frontend)
        new = old.replace(b"oldapp:8000", b"newapp:8000")
        self.assertEqual(new.replace(b"newapp:8000", b"oldapp:8000"), raw)
        self.assertEqual(new.count(b"newapp:8000"), 3)
        self.release.m["nginx_upstream_count"] = 1
        with self.assertRaisesRegex(RuntimeError, "token count drift"):
            self.release.nginx_baseline(frontend)


if __name__ == "__main__":
    unittest.main(verbosity=2)
