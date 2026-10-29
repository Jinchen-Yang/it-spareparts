"""纯合成发布控制器测试；只在 jinchen-dev 执行，无生产连接。"""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
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

    def catalog(self, token=b"synthetic-independent-catalog-token-123"):
        self.release.m["catalog_sync"] = {"enabled": True, "url": ops.CATALOG_SYNC_URL,
                                          "token_sha256": ops.digest(token)}
        ops.write_new(self.root / "catalog.token.private", token)
        return token.decode("ascii")

    def root_token_metadata(self, *, uid=0, mode=stat.S_IFREG | 0o600):
        return patch.object(ops.os, "fstat", return_value=SimpleNamespace(st_uid=uid, st_mode=mode))

    def test_catalog_manifest_has_only_enabled_fixed_url_and_hash(self):
        valid = manifest()
        valid["catalog_sync"] = {"enabled": True, "url": ops.CATALOG_SYNC_URL, "token_sha256": "f" * 64}
        root = ops.BASE / ("presales3-" + valid["release"])
        self.assertEqual(ops.validate_manifest(valid, root), valid)
        for catalog in (None, {}, {**valid["catalog_sync"], "token": "must-not-be-in-manifest"},
                        {**valid["catalog_sync"], "enabled": False}, {**valid["catalog_sync"], "enabled": 1},
                        {**valid["catalog_sync"], "url": "https://other.example/parts/sync"},
                        {**valid["catalog_sync"], "token_sha256": "bad"}):
            with self.subTest(catalog=catalog), self.assertRaises(RuntimeError):
                ops.validate_manifest({**valid, "catalog_sync": catalog}, root)

    def test_absent_catalog_config_preserves_environment_and_does_not_probe(self):
        original = ["OTHER=exact value", "SHOP_CATALOG_SYNC_ENABLED=false", "SHOP_CATALOG_SYNC_TOKEN=original"]
        self.assertEqual(self.release.catalog_environment(original), original)
        self.assertIsNot(self.release.catalog_environment(original), original)
        self.assertIsNone(self.release.catalog_environment(None))
        self.release.run = Mock()
        self.assertIsNone(self.release.check_catalog_settings({"Config": {"Env": original}}))
        self.release.run.assert_not_called()

    def test_catalog_environment_only_changes_three_keys(self):
        token = self.catalog()
        old = ["OTHER=exact value", "SHOP_CATALOG_SYNC_URL=https://old.invalid", "SECRET_KEY=unchanged-other-key",
               "SHOP_CATALOG_SYNC_ENABLED=false", "SHOP_CATALOG_SYNC_TOKEN=old-token", "DATABASE_URL=unchanged"]
        with self.root_token_metadata():
            result = self.release.catalog_environment(old)
        self.assertEqual(result, ["OTHER=exact value", "SHOP_CATALOG_SYNC_URL=" + ops.CATALOG_SYNC_URL,
            "SECRET_KEY=unchanged-other-key", "SHOP_CATALOG_SYNC_ENABLED=true", "SHOP_CATALOG_SYNC_TOKEN=" + token,
            "DATABASE_URL=unchanged"])
        self.assertIn("SHOP_CATALOG_SYNC_TOKEN=old-token", old)

    def test_catalog_token_owner_mode_hash_and_format_fail_closed(self):
        self.catalog()
        for uid, mode in ((1000, stat.S_IFREG | 0o600), (0, stat.S_IFREG | 0o644), (0, stat.S_IFIFO | 0o600)):
            with self.subTest(uid=uid, mode=mode), self.root_token_metadata(uid=uid, mode=mode), self.assertRaises(RuntimeError):
                self.release.catalog_environment([])
        for value in (b"short", b"x" * 257, b"x" * 32 + b"\n", b"x" * 31 + b" ", b"x" * 31 + b"\xff"):
            (self.root / "catalog.token.private").write_bytes(value)
            self.release.m["catalog_sync"]["token_sha256"] = ops.digest(value)
            with self.subTest(value_length=len(value)), self.root_token_metadata(), self.assertRaises(RuntimeError):
                self.release.catalog_environment([])
        (self.root / "catalog.token.private").write_bytes(b"x" * 32)
        self.release.m["catalog_sync"]["token_sha256"] = "0" * 64
        with self.root_token_metadata(), self.assertRaises(RuntimeError):
            self.release.catalog_environment([])

    def test_catalog_token_symlink_is_rejected(self):
        self.catalog()
        path = self.root / "catalog.token.private"
        target = self.root / "elsewhere.private"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaises(OSError):
            self.release.catalog_environment([])

    def test_catalog_credential_cannot_reuse_gallery_directory_or_master_credentials(self):
        token = self.catalog()
        for original in (["SHOP_GALLERY_TOKEN=" + token], ["RESALE_CATALOG_TOKEN_SHA256=" + ops.digest(token.encode())],
                         ["PN_MASTER_INTEGRATION_TOKEN_SHA256=" + ops.digest(token.encode())],
                         ["RESALE_EXPORT_TOKEN_SHA256=" + ops.digest(token.encode())], ["SECRET_KEY=" + token]):
            with self.subTest(key=original[0].partition("=")[0]), self.root_token_metadata(), self.assertRaises(RuntimeError):
                self.release.catalog_environment(original)

    def test_candidate_settings_probe_is_network_isolated_and_has_no_secret_in_argv(self):
        token = self.catalog()
        self.release.run = Mock(return_value=b"true\n")
        with self.root_token_metadata():
            receipt = self.release.check_catalog_settings({"Config": {"Env": ["OTHER=keep"]}})
        self.assertEqual(receipt, {**self.release.m["catalog_sync"], "settings_verified": True})
        command = self.release.run.call_args.args[0]
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("--read-only", command)
        self.assertNotIn("-p", command)
        self.assertNotIn(token, " ".join(command))
        self.assertNotIn(token.encode(), self.release.run.call_args.kwargs["input"])
        self.assertIn("SHOP_CATALOG_SYNC_TOKEN=" + token, (self.root / "catalog-settings.env.private").read_text())

    def test_candidate_settings_probe_rejects_old_or_ignoring_image(self):
        self.catalog()
        self.release.run = Mock(return_value=b"false\n")
        with self.root_token_metadata(), self.assertRaisesRegex(RuntimeError, "does not support/accept"):
            self.release.check_catalog_settings({"Config": {"Env": []}})

    def test_probe_prints_false_for_settings_without_catalog_fields(self):
        fake_app, fake_config = ModuleType("app"), ModuleType("app.config")
        fake_config.Settings = type("OldSettings", (), {"model_fields": {"database_url": object()}})
        captured = io.StringIO()
        with patch.dict(sys.modules, {"app": fake_app, "app.config": fake_config}), contextlib.redirect_stdout(captured):
            exec(ops.CATALOG_SETTINGS_PROBE, {})
        self.assertEqual(captured.getvalue(), "false\n")

    def test_clone_requires_completed_settings_probe_before_creating_container(self):
        self.catalog()
        self.release.marker = Mock(return_value={})
        self.release.run = Mock()
        old = {"Config": {"Env": ["OTHER=unchanged"]}, "HostConfig": {}}
        with self.assertRaisesRegex(RuntimeError, "lack completed candidate preflight"):
            self.release.clone("newapp", old, "sha256:" + "1" * 64)
        self.release.run.assert_not_called()

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

    def test_safe_path_and_legacy_logs_detect_server_errors_only(self):
        for raw in (b"safe_path=/api/presales status=500 request_time=0.01",
                    b"safe_path=/health/db status=504\n", b"status=503",
                    b'GET /health/db HTTP/1.1" 502 Bad Gateway',
                    b"Traceback (most recent call last):", b"ERROR request failed"):
            with self.subTest(raw=raw):
                self.assertTrue(ops.has_log_errors(raw))
        for raw in (b"safe_path=/api/presales status=200 request_time=0.01",
                    b"safe_path=/api/presales status=401", b"safe_path=/api/presales status=409",
                    b'GET /api/presales HTTP/1.1" 404 Not Found',
                    b"safe_path=/status=500/ status=200", b"status=5000", b"status=200 upstream_status=502"):
            with self.subTest(raw=raw):
                self.assertFalse(ops.has_log_errors(raw))

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
