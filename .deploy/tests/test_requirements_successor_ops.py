"""Synthetic contracts for the no-migration successor. No production access."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

DEPLOY = Path(__file__).resolve().parents[1]
REPO = DEPLOY.parent
spec = importlib.util.spec_from_file_location("requirements_successor_ops", DEPLOY / "requirements_successor_ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def manifest(**overrides):
    result = {
        "release": ops.APP_SHA, "old_revision": ops.OLD_SHA, "old_schema": ops.SCHEMA, "target_head": ops.SCHEMA,
        "old_app": ops.OLD_PREFIX + "-app", "old_front": ops.OLD_PREFIX + "-frontend",
        "caddyfile": "/opt/it-spareparts/Caddyfile", "port": 18107, "subnet": "10.44.0.0/28",
        "additional_writers": [], "nginx_upstream_count": 1,
        "visibility_sha256": ops.VISIBILITY_DIGEST, "pool_sha256": ops.vis.POOL_DIGEST, "smoke_sha256": ops.SMOKE_DIGEST,
    }
    for key in ("old_app_id", "old_front_id", "source_sha256", "controller_sha256", "nginx_sha256",
                "predecessor_owner_sha256", "evidence_sha256", "db_id"):
        result[key] = "b" * 64
    for key in ("old_app_image", "old_front_image", "app_image", "frontend_image", "db_image"):
        result[key] = "sha256:" + "c" * 64
    result["proxy_identities"] = {name: {"id": "d" * 64, "image": "sha256:" + "e" * 64, "process": name}
                                  for name in ("caddy", "cloudflared", "nginx")}
    result.update(overrides)
    return result


def evidence_body():
    return {
        "release": ops.APP_SHA, "release_tag_sha": ops.APP_SHA,
        "release_tag": "release-requirements-successor-" + ops.APP_SHA,
        "parents": list(ops.PARENTS), "tree": ops.TREE, "app_version": ops.APP_VERSION,
        "old_revision": ops.OLD_SHA, "old_schema": ops.SCHEMA, "target_head": ops.SCHEMA,
        "migration_performed": False,
        "references": {gate: "recorded " + ops.APP_SHA + " " + gate for gate in ops.GATES},
        **{gate: "passed" for gate in ops.GATES},
    }


def version_source(version="1.46.0", newline="\n"):
    return ('export const APP_VERSION = "%s";' % version).encode() + newline.encode()


def tar_bytes(comment, files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": comment}) as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class PinTests(unittest.TestCase):
    def test_historical_controller_constants_and_functions_stay_put(self):
        text = ops.VISIBILITY_PATH.read_text()
        self.assertEqual(ops.VISIBILITY_DIGEST, ops.h.digest(ops.VISIBILITY_PATH.read_bytes()))
        self.assertNotIn(ops.APP_SHA, text)
        self.assertIn("559489e6ef548ef84be8112ada1c74117e6839ca", text)
        self.assertEqual(ops.vis.OLD_HEAD, "e5a7c9d1f3b6")
        self.assertEqual(ops.SCHEMA, "98afc0d2157b")
        self.assertIs(ops.file_mode, ops.vis.pool.file_mode)
        self.assertIs(ops.Release.plan_caddy_change, ops.vis.pool.Release.plan_caddy_change)
        self.assertIs(ops.Release.writers_exact, ops.vis.pool.Release.writers_exact)
        self.assertIs(ops.Release.freeze, ops.vis.Release.freeze)
        self.assertIs(ops.Release.backup, ops.vis.Release.backup)
        self.assertIs(ops.Release.cutover, ops.vis.Release.cutover)
        self.assertIs(ops.Release.protected, ops.vis.Release.protected)
        self.assertIsNot(ops.Release.migrate, ops.vis.Release.migrate)
        self.assertIsNot(ops.Release.verify, ops.vis.Release.verify)
        self.assertIsNot(ops.Release.rollback, ops.vis.Release.rollback)
        self.assertIn("super().preflight()", inspect_source(ops.Release.preflight))
        self.assertIn('TARGET + " (head)"', inspect_source(ops.vis.Release.preflight))
        cutover = inspect_source(ops.vis.Release.cutover)
        self.assertLess(cutover.index("exposure-started.json"), cutover.index("self.write_caddy"))
        restore = inspect_source(ops.h.Release.restore)
        self.assertIn('"--network", "none"', restore)
        self.assertIn("restore image is not PostgreSQL 15", restore)
        self.assertIn('"published_ports": []', restore)
        self.assertIn("core.autocrlf=false", inspect_source(ops.canonical_archive))
        self.assertIn("core.eol=lf", inspect_source(ops.canonical_archive))

    def test_only_the_verified_merge_and_held_schema_validate(self):
        ops.validate(manifest())
        for key, value in (
            ("release", ops.PARENTS[0]), ("release", ops.PARENTS[1]), ("release", ops.OLD_SHA), ("release", "a" * 40),
            ("target_head", "e5a7c9d1f3b6"), ("old_schema", "e5a7c9d1f3b6"), ("old_app", "itsp-pool-559489e6ef54-app"),
            ("additional_writers", [{"name": "other"}]), ("source_handoff", {}), ("catalog_sync", {}),
            ("import_gate_allowlist", {"batches": []}), ("pool_sha256", "a" * 64), ("smoke_sha256", "d" * 64),
            ("caddyfile", "/etc/caddy/Caddyfile"), ("port", True), ("port", 80), ("port", 65536),
            ("subnet", "10.0.0.0/16"), ("subnet", "8.8.8.0/24"), ("subnet", "10.1.0.0/32"), ("subnet", "fd00::/64"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                ops.validate(manifest(**{key: value}))

    def test_private_manifest_check_is_live(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text("{}\n")
            with self.assertRaisesRegex(RuntimeError, "private manifest required"):
                ops.Release(str(path))

    def test_upgrade_and_downgrade_never_reach_the_historical_runner(self):
        release = object.__new__(ops.Release)
        seen = []

        def fake(self, args):
            seen.append(list(args))
            return "ok"

        with patch.object(ops.h.Release, "alembic", fake):
            for args in (["upgrade", "head"], ["downgrade", ops.SCHEMA], ["stamp"], ["upgrade"]):
                with self.subTest(args=args), self.assertRaisesRegex(RuntimeError, "migration command refused"):
                    ops.Release.alembic(release, args)
            self.assertEqual(seen, [])
            self.assertEqual(ops.Release.alembic(release, ["heads"]), "ok")
        self.assertEqual(seen, [["heads"]])

    def test_reviewed_smoke_is_read_only_and_pinned(self):
        text = ops.SMOKE_PATH.read_text()
        self.assertEqual(ops.h.digest(ops.SMOKE_PATH.read_bytes()), ops.SMOKE_DIGEST)
        self.assertIn('HELD_HEAD = "98afc0d2157b"', text)
        self.assertIn('"mode": "readonly_database_projection"', text)
        for token in ("upgrade", "downgrade", "INSERT", "UPDATE", "DELETE", "import_gate_allowlist", "commit("):
            self.assertNotIn(token, text)


class MigrationGateTests(unittest.TestCase):
    def release(self, backup):
        release = object.__new__(ops.Release)
        release.marker = Mock(side_effect=lambda name: {
            "backup-old": backup,
            "preflight": {"schema": ops.SCHEMA},
            "freeze": {"schema": ops.SCHEMA, "counts": {"kept": 1}},
        }[name])
        release.frozen = Mock()
        release.writers_exact = Mock()
        release.source_tcp_zero = Mock()
        release.unchanged_business = Mock()
        release.schema = Mock(return_value=ops.SCHEMA)
        release.counts = Mock(return_value={"kept": 1})
        return release

    def test_migrate_requires_network_none_restore_and_refuses_schema_commands(self):
        backup = {"restore": {"actual_restore_performed": True, "schema": ops.SCHEMA, "network": "none",
                              "published_ports": []}, "uploads_content_compared": True}
        release = self.release(backup)
        seen = []

        def fake(self, args):
            seen.append(list(args))
            return ops.SCHEMA + " (head)" if args[0] in {"heads", "current"} else ""

        with patch.object(ops.vis.Release, "migrate", side_effect=AssertionError("historical migrate")), \
                patch.object(ops.h.Release, "alembic", fake):
            result = ops.Release.migrate(release)
        self.assertEqual(seen, [["heads"], ["current"], ["check"]])
        self.assertEqual(result["migration_performed"], False)
        self.assertEqual(result["upgrade_invoked"], False)
        self.assertEqual(release.unchanged_business.call_count, 2)
        for broken in (
            {"restore": {"actual_restore_performed": False, "schema": ops.SCHEMA, "network": "none", "published_ports": []},
             "uploads_content_compared": True},
            {"restore": {"actual_restore_performed": True, "schema": "e5a7c9d1f3b6", "network": "none", "published_ports": []},
             "uploads_content_compared": True},
            {"restore": {"actual_restore_performed": True, "schema": ops.SCHEMA, "network": "bridge", "published_ports": []},
             "uploads_content_compared": True},
            {"restore": {"actual_restore_performed": True, "schema": ops.SCHEMA, "network": "none", "published_ports": [80]},
             "uploads_content_compared": True},
            {"restore": {"actual_restore_performed": True, "schema": ops.SCHEMA, "network": "none", "published_ports": []},
             "uploads_content_compared": False},
        ):
            probe = self.release(broken)
            with patch.object(ops.h.Release, "alembic", side_effect=AssertionError("alembic")), self.assertRaises(RuntimeError):
                ops.Release.migrate(probe)
            probe.frozen.assert_not_called()

    def test_new_head_stops_before_check(self):
        release = self.release({"restore": {"actual_restore_performed": True, "schema": ops.SCHEMA, "network": "none",
                                            "published_ports": []}, "uploads_content_compared": True})
        seen = []

        def fake(self, args):
            seen.append(args[0])
            return "c7e1a3b5d9f2 (head)" if args[0] == "heads" else ""

        with patch.object(ops.h.Release, "alembic", fake), self.assertRaisesRegex(RuntimeError, "candidate migration head"):
            ops.Release.migrate(release)
        self.assertEqual(seen, ["heads"])


class RecoveryTests(unittest.TestCase):
    def release(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        release = object.__new__(ops.Release)
        release.root = Path(folder.name)
        release.sha = ops.APP_SHA
        release.app, release.front, release.old_app = "new-app", "new-front", "old-app"
        release.m = {"app_image": "sha256:" + "1" * 64, "frontend_image": "sha256:" + "2" * 64,
                     "old_app_id": "ab" * 32, "old_app_image": "sha256:" + "3" * 64}
        release.marker = Mock(return_value={"active": {}, "inode": [1, 2]})
        release.cas = Mock()
        release.schema = Mock(return_value=ops.SCHEMA)
        release.run = Mock(return_value=b"")
        release.inspect = Mock()
        release.writers_exact = Mock()
        release.source_tcp_zero = Mock()
        release.unchanged_business = Mock()
        release.wait_http = Mock()
        (release.root / "Caddyfile.baseline").write_bytes(b"baseline")
        return release

    def test_exposure_and_schema_change_stop_before_docker(self):
        for name in ("cutover.intent.json", "exposure-started.json"):
            release = self.release()
            (release.root / name).write_text("{}\n")
            with self.assertRaisesRegex(RuntimeError, "forward repair"):
                ops.Release.rollback(release)
            release.run.assert_not_called()
            release.cas.assert_not_called()
        release = self.release()
        release.schema.return_value = "e5a7c9d1f3b6"
        with patch.object(ops.h.Release, "alembic", side_effect=AssertionError("downgrade")), \
                self.assertRaisesRegex(RuntimeError, "schema changed"):
            ops.Release.rollback(release)
        release.run.assert_not_called()

    def test_pre_exposure_restart_stops_only_the_candidate_and_starts_the_old_writer(self):
        release = self.release()
        release.run.side_effect = [b"new-app\nold-app\n", b"", b""]
        release.inspect.side_effect = [
            {"Image": release.m["app_image"], "Id": "cd" * 32, "Config": {"Labels": {"bg.release": ops.APP_SHA}}},
            {"Id": release.m["old_app_id"], "Image": release.m["old_app_image"], "State": {"Running": False}},
        ]
        with patch.object(ops.h.Release, "alembic", side_effect=AssertionError("downgrade")), \
                patch.object(ops.vis.Release, "rollback", side_effect=AssertionError("historical rollback")):
            result = ops.Release.rollback(release)
        commands = [call.args[0] for call in release.run.call_args_list]
        self.assertEqual(commands[1][:3], ["docker", "stop", "--time"])
        self.assertEqual(commands[1][-1], "cd" * 32)
        self.assertEqual(commands[2], ["docker", "start", release.m["old_app_id"]])
        self.assertTrue(all(command[1] != "rm" for command in commands))
        self.assertEqual(result["restored_dump_into_production"], False)
        self.assertEqual(result["schema"], ops.SCHEMA)


class EvidenceTests(unittest.TestCase):
    def test_missing_gate_fails_before_predecessor_or_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = evidence_body()
            del body["references"]["required_ci"]
            path = root / "release-evidence.json"
            path.write_text(json.dumps(body))
            release = object.__new__(ops.Release)
            release.root = root
            release.sha = ops.APP_SHA
            release.m = {"evidence_sha256": ops.h.artifact(path)["sha256"]}
            release.require_source_archive = Mock(side_effect=AssertionError("archive"))
            release.predecessor = Mock(side_effect=AssertionError("predecessor"))
            with patch.object(ops, "file_mode", return_value=True), self.assertRaisesRegex(RuntimeError, "missing evidence: required_ci"):
                ops.Release.evidence(release)
            release.require_source_archive.assert_not_called()
            release.predecessor.assert_not_called()

    def test_old_expansion_gate_does_not_replace_no_migration_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = evidence_body()
            body["no_new_migration"] = "waived"
            body["old_app_on_expanded_schema"] = "passed"
            path = root / "release-evidence.json"
            path.write_text(json.dumps(body))
            release = object.__new__(ops.Release)
            release.root, release.sha = root, ops.APP_SHA
            release.m = {"evidence_sha256": ops.h.artifact(path)["sha256"]}
            with patch.object(ops, "file_mode", return_value=True), self.assertRaisesRegex(RuntimeError, "missing evidence: no_new_migration"):
                ops.Release.evidence(release)

    def test_predecessor_owner_must_match_the_visibility_manifest_without_being_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "release"
            root.mkdir()
            archive = tar_bytes(ops.APP_SHA, {"frontend/src/version.ts": version_source()})
            (root / "source.tar.gz").write_bytes(archive)
            body = evidence_body()
            evidence = root / "release-evidence.json"
            evidence.write_text(json.dumps(body))
            owner = {"release": ops.OLD_SHA, "manifest_sha256": "a" * 64}
            predecessor = base / ops.PREDECESSOR_OWNER
            ops.h.save(predecessor, owner)
            manifest_path = base / ("requirements-visibility-" + ops.OLD_SHA) / "manifest.json"
            manifest_path.parent.mkdir()
            payload = {"release": ops.OLD_SHA, "target_head": ops.SCHEMA, "old_revision": "559489e6ef548ef84be8112ada1c74117e6839ca"}
            ops.h.save(manifest_path, payload)
            owner["manifest_sha256"] = ops.h.artifact(manifest_path)["sha256"]
            predecessor.unlink()
            ops.h.save(predecessor, owner)
            before = predecessor.read_bytes()
            release = object.__new__(ops.Release)
            release.root, release.sha = root, ops.APP_SHA
            release.m = {"evidence_sha256": ops.h.artifact(evidence)["sha256"], "source_sha256": ops.h.digest(archive),
                         "predecessor_owner_sha256": ops.h.artifact(predecessor)["sha256"]}
            with patch.object(ops, "file_mode", return_value=True), patch.object(ops.h, "BASE", base):
                ops.Release.evidence(release)
            self.assertEqual(predecessor.read_bytes(), before)
            owner["release"] = ops.APP_SHA
            predecessor.unlink()
            ops.h.save(predecessor, owner)
            release.m["predecessor_owner_sha256"] = ops.h.artifact(predecessor)["sha256"]
            with patch.object(ops, "file_mode", return_value=True), patch.object(ops.h, "BASE", base), \
                    self.assertRaisesRegex(RuntimeError, "predecessor owner identity"):
                ops.Release.evidence(release)


class PhaseTests(unittest.TestCase):
    def prepared(self, phase="preflight"):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        root = base / ("requirements-successor-" + ops.APP_SHA)
        root.mkdir()
        manifest_path = root / "manifest.json"
        manifest_path.write_text("{}\n")
        predecessor = base / ops.PREDECESSOR_OWNER
        predecessor.write_bytes(b'{"release":"held"}\n')
        release = object.__new__(ops.Release)
        release.path = manifest_path
        release.manifest_bytes = manifest_path.read_bytes()
        release.root = root
        release.sha = ops.APP_SHA
        release.m = {"controller_sha256": "ab" * 32}
        release.evidence = Mock()
        release.preflight = Mock(return_value={"ok": True})
        release.rollback = Mock(side_effect=AssertionError("rollback"))
        return base, predecessor, release

    def test_failure_preserves_predecessor_and_does_not_roll_back(self):
        base, predecessor, release = self.prepared()
        before = predecessor.read_bytes()
        release.preflight.side_effect = RuntimeError("boom")
        with patch.object(ops, "file_mode", return_value=True), patch.object(ops.h, "BASE", base), \
                patch("sys.stdout", io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                ops.run_locked(release, "preflight")
        self.assertEqual(raised.exception.code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["rollback_invoked"], False)
        self.assertTrue(Path(payload["diagnostic"]).is_file())
        self.assertIn("boom", Path(payload["diagnostic"]).read_text())
        self.assertEqual(predecessor.read_bytes(), before)
        self.assertFalse((release.root / "exposure-started.json").exists())
        self.assertFalse((release.root / "preflight.json").exists())
        self.assertTrue((release.root / "preflight.intent.json").exists())
        release.rollback.assert_not_called()
        owner = json.loads((base / ops.OWNER_NAME).read_text())
        self.assertEqual(owner["release"], ops.APP_SHA)
        self.assertNotEqual(owner["release"], ops.OLD_SHA)

    def test_recovered_release_and_skipped_phase_do_not_run(self):
        base, _predecessor, release = self.prepared("images")
        (release.root / "recover-1.intent.json").write_text("{}\n")
        with patch.object(ops, "file_mode", return_value=True), patch.object(ops.h, "BASE", base), \
                self.assertRaisesRegex(RuntimeError, "terminal"):
            ops.Release.execute(release, "preflight")
        release.preflight.assert_not_called()
        release.evidence.assert_not_called()

    def test_claimed_migration_result_is_rejected(self):
        base, _predecessor, release = self.prepared()
        identity = {"release": ops.APP_SHA, "manifest_sha256": ops.h.digest(release.manifest_bytes),
                    "controller_sha256": release.m["controller_sha256"]}
        ops.h.save(base / ops.OWNER_NAME, identity)
        ops.h.save(release.root / "backup-old.json", {**identity, "phase": "backup-old", "status": "PASS"})
        release.migrate = Mock(return_value={"migration_performed": True, "upgrade_invoked": False, "downgrade_invoked": False})
        with patch.object(ops, "file_mode", return_value=True), patch.object(ops.h, "BASE", base), \
                self.assertRaisesRegex(RuntimeError, "migration result"):
            ops.Release.execute(release, "migrate")
        release.rollback.assert_not_called()

    def test_preflight_keeps_parent_gate_and_rejects_processing_waiver(self):
        release = object.__new__(ops.Release)
        with patch.object(ops.vis.Release, "preflight", return_value={"schema": ops.SCHEMA, "import_gate": {
                "mode": "all_zero", "batches": [], "jobs": [], "active_return_receipts": 0}}) as parent:
            self.assertEqual(ops.Release.preflight(release)["schema"], ops.SCHEMA)
            parent.assert_called_once()
        with patch.object(ops.vis.Release, "preflight", return_value={"schema": ops.SCHEMA, "import_gate": {
                "mode": "exact_readonly_exceptions", "batches": [1], "jobs": [], "active_return_receipts": 0}}), \
                self.assertRaisesRegex(RuntimeError, "old waiver refused"):
            ops.Release.preflight(release)

    def test_verify_uses_hotfix_assets_and_the_pinned_version(self):
        release = object.__new__(ops.Release)
        release.app, release.front, release.sha = "app", "front", ops.APP_SHA
        release.writers_exact = Mock()
        release.require_public_version = Mock(return_value={"version": ops.APP_VERSION})
        release.schema = Mock(return_value=ops.SCHEMA)

        def inspect(name, image=False):
            if image:
                return {"Config": {"Labels": {"org.opencontainers.image.revision": ops.APP_SHA}}}
            return {"Image": "sha256:" + "a" * 64}

        release.inspect = Mock(side_effect=inspect)
        with patch.object(ops.vis.Release, "verify", side_effect=AssertionError("version 1.45 gate")), \
                patch.object(ops.hf.Hotfix, "verify", return_value={"public": {}}) as parent:
            result = ops.Release.verify(release)
        parent.assert_called_once()
        release.writers_exact.assert_called_once_with({release.app})
        self.assertEqual(result["published_version"]["version"], ops.APP_VERSION)
        self.assertEqual(result["migration_performed"], False)

    def test_public_version_must_match_loopback_bytes(self):
        release = object.__new__(ops.Release)
        release.front = "front"
        bundle = b"build " + ops.APP_VERSION.encode()
        release.run = Mock(side_effect=[b"/usr/share/nginx/html/assets/app.js\n", bundle])
        release.http = Mock(return_value={"sha256": ops.h.digest(bundle)})
        found = ops.Release.require_public_version(release)
        self.assertEqual(found["version"], ops.APP_VERSION)
        self.assertEqual(release.http.call_count, len(ops.h.HOSTS))
        release.run = Mock(side_effect=[b"/tmp/app.js\n", bundle])
        with self.assertRaisesRegex(RuntimeError, "unexpected asset path"):
            ops.Release.require_public_version(release)
        release.run = Mock(side_effect=[b"/usr/share/nginx/html/assets/app.js\n", b"1.45.0 only"])
        with self.assertRaisesRegex(RuntimeError, "pinned version asset missing"):
            ops.Release.require_public_version(release)

    def test_caddy_drift_fails_before_runtime_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            release = object.__new__(ops.Release)
            release.root = Path(directory)
            (release.root / "Caddyfile.baseline").write_bytes(b"baseline")
            release.cas = Mock()
            release.mounted_caddy = Mock(return_value={"drift": True})
            release.run = Mock()
            with self.assertRaisesRegex(RuntimeError, "differs from baseline runtime"):
                ops.Release.plan_caddy_change(release, {"active": {"ok": True}, "inode": [1, 2]})
            release.run.assert_not_called()
            self.assertFalse((release.root / "exposure-started.json").exists())

    def test_deploy_entrypoint_refuses_non_root_before_locks(self):
        with patch.object(ops.os, "geteuid", return_value=1000), \
                patch.object(ops, "production_locks", side_effect=AssertionError("locked")), \
                self.assertRaisesRegex(RuntimeError, "usage"):
            ops.main(["controller", "/tmp/manifest.json", "preflight"])

    def test_locks_are_ordered_nonblocking_and_exclusive_across_processes(self):
        opened, flocked = [], []

        def fake_open(path, flags, mode=0):
            opened.append((Path(path).name, flags))
            return 100 + len(opened)

        with patch.object(ops.os, "open", fake_open), patch.object(ops.fcntl, "flock", lambda fd, op: flocked.append(op)), \
                patch.object(ops.h, "BASE", Path("/tmp")):
            ops.production_locks()
        self.assertEqual([name for name, _flags in opened], list(ops.LOCKS))
        self.assertTrue(all(flags & os.O_NOFOLLOW and flags & os.O_CREAT and flags & os.O_WRONLY for _name, flags in opened))
        self.assertEqual(flocked, [ops.fcntl.LOCK_EX | ops.fcntl.LOCK_NB, ops.fcntl.LOCK_EX | ops.fcntl.LOCK_NB])
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["SUCCESSOR"] = str(DEPLOY / "requirements_successor_ops.py")
            env["BASE"] = directory
            script = textwrap.dedent("""
                import importlib.util, os
                from pathlib import Path
                spec = importlib.util.spec_from_file_location("successor_lock", os.environ["SUCCESSOR"])
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.h.BASE = Path(os.environ["BASE"])
                mod.production_locks()
                print("READY", flush=True)
                import time
                time.sleep(30)
            """)
            proc = subprocess.Popen([sys.executable, "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                line = proc.stdout.readline()
                self.assertEqual(line.strip(), "READY")
                with patch.object(ops.h, "BASE", Path(directory)), self.assertRaises(BlockingIOError):
                    ops.production_locks()
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdout.close()
                proc.stderr.close()


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        probe = subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", ops.APP_SHA + "^{commit}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode != 0:
            subprocess.run(["git", "-C", str(REPO), "fetch", "--no-tags", "--depth=50", "origin", "main"], check=True)
        cls.archive = ops.canonical_archive(REPO, ops.APP_SHA)

    def test_pinned_merge_matches_parents_tree_and_git_blobs(self):
        identity = ops.git_commit_identity(REPO, ops.APP_SHA)
        self.assertEqual(identity["parents"], ops.PARENTS)
        self.assertEqual(identity["tree"], ops.TREE)
        checked = ops.assert_release_archive(REPO, self.archive)
        self.assertEqual(checked["app_version"], "1.46.0")
        self.assertGreater(len(checked["blobs"]), 100)
        self.assertNotIn(".deploy/requirements_successor_ops.py", checked["blobs"])
        repeat = ops.canonical_archive(REPO, ops.APP_SHA)
        self.assertEqual(ops.read_archive(self.archive)["blobs"], ops.read_archive(repeat)["blobs"])
        self.assertEqual(ops.read_archive(self.archive)["comment"], ops.APP_SHA)

    def test_pr_head_and_first_parent_archives_are_rejected(self):
        for sha in ops.PARENTS:
            archive = ops.canonical_archive(REPO, sha)
            with self.subTest(sha=sha), self.assertRaises(RuntimeError):
                ops.assert_release_archive(REPO, archive)

    def test_crlf_and_wrong_comment_do_not_match_git_blobs(self):
        self.assertEqual(ops.app_version_from_source(version_source()), "1.46.0")
        with self.assertRaises(RuntimeError):
            ops.app_version_from_source(version_source(newline="\r\n"))
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "false"], check=True)
            source = repo / "frontend" / "src"
            source.mkdir(parents=True)
            (source / "version.ts").write_bytes(version_source("0.0.1"))
            (repo / "run.sh").write_bytes(b"echo ok\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "hold"], check=True, capture_output=True)
            sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            good = ops.canonical_archive(repo, sha)
            self.assertEqual(ops.compare_archive_to_commit(repo, sha, good)["app_version"], "0.0.1")
            crlf = tar_bytes(sha, {"frontend/src/version.ts": version_source("0.0.1"), "run.sh": b"echo ok\r\n"})
            with self.assertRaisesRegex(RuntimeError, "archive bytes differ from git blobs"):
                ops.compare_archive_to_commit(repo, sha, crlf)
            wrong = tar_bytes(ops.APP_SHA, {"frontend/src/version.ts": version_source("0.0.1"), "run.sh": b"echo ok\n"})
            with self.assertRaisesRegex(RuntimeError, "archive comment is not the requested commit"):
                ops.compare_archive_to_commit(repo, sha, wrong)

    def test_check_archive_command_does_not_take_production_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "source.tar.gz"
            archive.write_bytes(self.archive)
            with patch.object(ops, "production_locks", side_effect=AssertionError("locked")), \
                    patch("sys.stdout", io.StringIO()) as output:
                self.assertEqual(ops.main(["controller", "--check-archive", str(REPO), str(archive)]), 0)
            report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["release"], ops.APP_SHA)
        self.assertEqual(report["parents"], list(ops.PARENTS))
        self.assertEqual(report["tree"], ops.TREE)
        self.assertEqual(report["app_version"], "1.46.0")
        self.assertEqual(report["migration_performed"], False)
        self.assertEqual(report["controller_in_archive"], False)
        self.assertNotIn("rollback_invoked", report)


def inspect_source(function):
    import inspect
    return inspect.getsource(function)


if __name__ == "__main__":
    unittest.main()
