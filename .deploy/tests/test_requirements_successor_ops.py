"""Synthetic contracts for the no-migration successor. No production access."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
import urllib.request
from unittest.mock import Mock, patch

DEPLOY = Path(__file__).resolve().parents[1]
REPO = DEPLOY.parent
spec = importlib.util.spec_from_file_location("requirements_successor_ops", DEPLOY / "requirements_successor_ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)

# docker 分支容器内 Caddy admin 的唯一端口来源：setUp 与 docker_request 必须共用。
DOCKER_ADMIN_PORT = 2019


def manifest(**overrides):
    result = {
        "release": ops.APP_SHA, "old_revision": ops.OLD_SHA, "old_schema": ops.SCHEMA, "target_head": ops.SCHEMA,
        "old_app": ops.OLD_PREFIX + "-app", "old_front": ops.OLD_PREFIX + "-frontend",
        "caddyfile": "/opt/it-spareparts/Caddyfile", "port": 18107, "subnet": "10.44.0.0/28",
        "additional_writers": [], "nginx_upstream_count": 1,
        "visibility_sha256": ops.VISIBILITY_DIGEST, "pool_sha256": ops.vis.POOL_DIGEST, "smoke_sha256": ops.SMOKE_DIGEST,
        "helper_sha256": ops.HELPER_DIGEST,
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


def tar_bytes(comment, files, modes=None, kinds=None):
    modes, kinds = modes or {}, kinds or {}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": comment}) as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            kind = kinds.get(name, "file")
            info.mode = modes.get(name, 0o644)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload.decode()
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = payload.decode()
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
            else:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
                continue
            archive.addfile(info)
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
        self.assertIsNot(ops.Release.cutover, ops.vis.Release.cutover)
        self.assertIsNot(ops.Release.load_active, ops.hf.Hotfix.load_active)
        self.assertIs(ops.Release.protected, ops.vis.Release.protected)
        successor_cutover = inspect_source(ops.Release.cutover)
        self.assertNotIn('"/load"', successor_cutover)
        self.assertNotIn("load_active(", successor_cutover)
        self.assertLess(successor_cutover.index("exposure-started.json"), successor_cutover.index("conditional_dial_patch"))
        self.assertIn("load_active(desired)", inspect_source(ops.vis.Release.cutover))
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
        previous = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "manifest.json"
                path.write_text("{}\n")
                os.chmod(path, 0o644)
                with self.assertRaisesRegex(RuntimeError, "private manifest required"):
                    ops.Release(str(path))
            before = os.umask(0o022)
            os.umask(before)
            with patch.object(ops.os, "geteuid", return_value=1000), \
                    patch.object(ops, "production_locks", side_effect=AssertionError("locked")), \
                    self.assertRaisesRegex(RuntimeError, "usage"):
                ops.main(["controller", "/tmp/manifest.json", "preflight"])
            after = os.umask(0o022)
            os.umask(after)
            self.assertEqual(before, after)
        finally:
            os.umask(previous)

    def test_root_rejects_loose_manifest_and_directory(self):
        script = """
import importlib.util, os, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location("succ", %r)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert os.geteuid() == 0, os.geteuid()
directory = tempfile.mkdtemp()
manifest = Path(directory) / "manifest.json"
manifest.write_text("{}\\n")
os.chmod(manifest, 0o644)
try:
    mod.Release(str(manifest))
except RuntimeError as exc:
    assert "private manifest required" in str(exc), exc
else:
    raise SystemExit("manifest mode was accepted")
os.chmod(manifest, 0o600)
os.chmod(directory, 0o755)
try:
    mod.Release(str(manifest))
except RuntimeError as exc:
    assert "private release directory required" in str(exc), exc
else:
    raise SystemExit("directory mode was accepted")
print("root-denials-ok")
""" % str(DEPLOY / "requirements_successor_ops.py")
        result = subprocess.run(["sudo", "-n", sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("root-denials-ok", result.stdout)

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
        release = self.release()
        (release.root / "exposure-started.json").write_text("{}\n")
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

    def test_pre_exposure_cutover_intent_does_not_block_recovery(self):
        """P2-2：cutover.intent 只表示进入切流阶段；暴露标记写入前失败仍允许 recover。"""
        release = self.release()
        (release.root / "cutover.intent.json").write_text("{}\n")
        release.run.side_effect = [b"new-app\nold-app\n", b"", b""]
        release.inspect.side_effect = [
            {"Image": release.m["app_image"], "Id": "cd" * 32, "Config": {"Labels": {"bg.release": ops.APP_SHA}}},
            {"Id": release.m["old_app_id"], "Image": release.m["old_app_image"], "State": {"Running": False}},
        ]
        with patch.object(ops.h.Release, "alembic", side_effect=AssertionError("downgrade")), \
                patch.object(ops.vis.Release, "rollback", side_effect=AssertionError("historical rollback")):
            result = ops.Release.rollback(release)
        self.assertEqual(result["schema"], ops.SCHEMA)
        self.assertEqual(result["restored_dump_into_production"], False)
        self.assertEqual(result["downgrade_invoked"], False)

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
        ops.ensure_frozen_commit(REPO)
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


class MemberContractTests(unittest.TestCase):
    def test_type_mode_and_path_mutations_are_rejected_without_extracting(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
            source = repo / "frontend" / "src"
            source.mkdir(parents=True)
            (source / "version.ts").write_bytes(version_source("0.0.1"))
            script = repo / "run.sh"
            script.write_bytes(b"echo ok\n")
            os.chmod(script, 0o755)
            os.symlink("run.sh", repo / "link")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "hold"], check=True, capture_output=True)
            sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            good = ops.canonical_archive(repo, sha)
            self.assertEqual(ops.compare_archive_to_commit(repo, sha, good)["modes"]["link"], "120000")
            samples = {
                "symlink": (tar_bytes(sha, {
                    "frontend/src/version.ts": version_source("0.0.1"), "run.sh": b"echo ok\n", "link": b"run.sh",
                }, kinds={"run.sh": "symlink", "link": "symlink"}), "archive member type refused"),
                "hardlink": (tar_bytes(sha, {
                    "frontend/src/version.ts": version_source("0.0.1"), "run.sh": b"echo ok\n", "alias": b"run.sh",
                }, kinds={"alias": "hardlink"}), "special archive member refused"),
                "mode": (tar_bytes(sha, {
                    "frontend/src/version.ts": version_source("0.0.1"), "run.sh": b"echo ok\n", "link": b"run.sh",
                }, modes={"run.sh": 0o7777}, kinds={"link": "symlink"}), "archive mode refused"),
                "directory": (tar_bytes(sha, {"../out": b"", "frontend/src/version.ts": version_source("0.0.1")},
                                        kinds={"../out": "dir"}), "archive path refused"),
                "pax": (tar_bytes(sha, {"foo/PaxHeader": b"extra", "frontend/src/version.ts": version_source("0.0.1")}),
                        "archive path refused"),
                "fifo": (tar_bytes(sha, {"frontend/src/version.ts": version_source("0.0.1"), "run.sh": b""},
                                   kinds={"run.sh": "fifo"}), "special archive member refused"),
            }
            for name, (blob, message) in samples.items():
                with self.subTest(name=name):
                    with self.assertRaisesRegex(RuntimeError, message):
                        ops.compare_archive_to_commit(repo, sha, blob)


class FetchContractTests(unittest.TestCase):
    def test_fetch_exit_zero_without_frozen_objects_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "-C", directory, "init"], check=True, capture_output=True)

            def fetch(_repo):
                return None

            with self.assertRaisesRegex(RuntimeError, "frozen commit objects missing after fetch"):
                ops.ensure_frozen_commit(directory, fetch=fetch)
        source = inspect_source(ops.fetch_exact_frozen_commit)
        self.assertIn("APP_SHA", source)
        self.assertIn("--depth=2", source)
        self.assertNotIn('"main"', source)
        self.assertNotIn("--depth=1", source)
        self.assertNotIn("--depth=50", source)

    def test_fresh_bare_fetch_includes_parents_trees_and_blobs(self):
        origin = subprocess.check_output(["git", "-C", str(REPO), "remote", "get-url", "origin"], text=True).strip()
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "--bare", directory], check=True, capture_output=True)
            subprocess.run(["git", "-C", directory, "remote", "add", "origin", origin], check=True)
            shallow = subprocess.run(
                ["git", "-C", directory, "fetch", "--no-tags", "--depth=1", "origin", ops.APP_SHA],
                capture_output=True, text=True)
            self.assertEqual(shallow.returncode, 0, shallow.stderr)
            self.assertFalse(ops.object_exists(directory, ops.PARENTS[0] + "^{commit}"))
            self.assertFalse(ops.frozen_objects_ready(directory))
            ops.ensure_frozen_commit(directory)
            self.assertTrue(ops.frozen_objects_ready(directory))
            identity = ops.git_commit_identity(directory, ops.APP_SHA)
            self.assertEqual(identity["parents"], ops.PARENTS)
            self.assertEqual(identity["tree"], ops.TREE)
            archive = ops.canonical_archive(directory, ops.APP_SHA)
            checked = ops.assert_release_archive(directory, archive)
            self.assertEqual(len(checked["blobs"]), 1940)


def compile_without(method, drops):
    source = textwrap.dedent(inspect_source(method))
    for drop in drops:
        if drop not in source:
            raise AssertionError("guard not found: " + drop)
        source = source.replace(drop, "pass")
    namespace = {}
    exec(source, ops.__dict__, namespace)
    return namespace[method.__name__]


class GuardMutationTests(unittest.TestCase):
    def owner_release(self):
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
        release.evidence = Mock(side_effect=lambda: predecessor.write_bytes(b'{"release":"changed"}\n'))
        release.preflight = Mock(return_value={"ok": True})
        return base, release

    def test_owner_guard_deletion_accepts_then_real_method_rejects(self):
        guard = 'require_same_bytes(before, predecessor.read_bytes(), "predecessor owner changed")'
        mutated = compile_without(ops.Release.execute, [guard])
        base, release = self.owner_release()
        with patch.object(ops, "file_mode", return_value=True), patch.object(ops.h, "BASE", base), \
                self.assertRaisesRegex(RuntimeError, "predecessor owner changed"):
            ops.Release.execute(release, "preflight")
        release.preflight.assert_not_called()
        with patch.object(ops, "file_mode", return_value=True), patch.object(ops.h, "BASE", base), \
                patch("sys.stdout", io.StringIO()):
            mutated(release, "preflight")
        release.preflight.assert_called_once()
        base, release = self.owner_release()
        with patch.object(ops, "file_mode", return_value=True), patch.object(ops.h, "BASE", base), \
                self.assertRaisesRegex(RuntimeError, "predecessor owner changed"):
            ops.Release.execute(release, "preflight")
        self.assertIn(guard, inspect_source(ops.Release.execute))

    def test_file_cas_guard_deletion_accepts_drift(self):
        guard = 'h.need(self.caddyfile.read_bytes() == data, "Caddy file drift")'
        mutated = compile_without(ops.Release.cas_file, [guard])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Caddyfile"
            path.write_bytes(b"changed-by-someone-else")
            inode = [path.stat().st_dev, path.stat().st_ino]
            release = object.__new__(ops.Release)
            release.caddyfile = path
            release.run = Mock(return_value=(ops.h.digest(b"expected") + "  x\n").encode())
            release.caddy_destination = Mock(return_value="/etc/caddy/Caddyfile")
            with self.assertRaisesRegex(RuntimeError, "Caddy file drift"):
                ops.Release.cas_file(release, b"expected", inode)
            mutated(release, b"expected", inode)
            with self.assertRaisesRegex(RuntimeError, "Caddy file drift"):
                ops.Release.cas_file(release, b"expected", inode)
        self.assertIn(guard, inspect_source(ops.Release.cas_file))

    def test_rollback_cas_guard_deletion_accepts_drift(self):
        guard = 'self.cas((self.root / "Caddyfile.baseline").read_bytes(), pre["active"], pre["inode"])'
        mutated = compile_without(ops.Release.rollback, [guard])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live = root / "Caddyfile"
            live.write_bytes(b"concurrent-edit")
            (root / "Caddyfile.baseline").write_bytes(b"baseline")
            inode = [live.stat().st_dev, live.stat().st_ino]
            release = object.__new__(ops.Release)
            release.root = root
            release.caddyfile = live
            release.sha = ops.APP_SHA
            release.old_app, release.app, release.front = "old-app", "new-app", "new-front"
            release.m = {"old_app_id": "ab" * 32, "old_app_image": "sha256:" + "c" * 64,
                         "app_image": "sha256:" + "d" * 64, "frontend_image": "sha256:" + "e" * 64}
            release.marker = Mock(return_value={"active": {"same": True}, "inode": inode})
            release.active = lambda: {"same": True}
            release.schema = Mock(return_value=ops.SCHEMA)
            release.run = Mock(return_value=b"")
            release.inspect = Mock(return_value={"Id": release.m["old_app_id"], "Image": release.m["old_app_image"],
                                                 "State": {"Running": True}})
            release.wait_http = Mock()
            with self.assertRaisesRegex(RuntimeError, "Caddy state drift"):
                ops.Release.rollback(release)
            self.assertEqual(mutated(release)["schema_preserved"], True)
            with self.assertRaisesRegex(RuntimeError, "Caddy state drift"):
                ops.Release.rollback(release)
        self.assertIn(guard, inspect_source(ops.Release.rollback))


def caddy_config(port):
    return {
        "admin": {"listen": "127.0.0.1:%s" % port},
        "apps": {"http": {"servers": {
            "main": {
                "listen": ["127.0.0.1:%s" % (port + 1)],
                "automatic_https": {"disable": True},
                "routes": [{"match": [{"host": ["www.ybwznt.com", "ybwznt.com"]}],
                            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "old-front:80"}]}]}],
            },
            "other": {
                "listen": ["127.0.0.1:%s" % (port + 2)],
                "automatic_https": {"disable": True},
                "routes": [{"match": [{"host": ["files.example"]}],
                            "handle": [{"handler": "static_response", "body": "other-old"}]}],
            },
        }}},
    }


class CaddyConditionalTests(unittest.TestCase):
    def setUp(self):
        self.port = 20219
        self.process = None
        self.container = None
        binary = os.environ.get("ITSP_CADDY_BIN") or shutil.which("caddy")
        if binary:
            config = Path("/tmp/itsp-successor-caddy-m2.json")
            config.write_text(json.dumps(caddy_config(self.port)))
            self.process = subprocess.Popen([binary, "run", "--config", str(config)],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.request = self.local_request
        elif shutil.which("docker"):
            # Docker 分支：容器内 Caddy admin 固定监听 2019。self.port 必须与 admin 端口一致：
            # 测试方法会用 caddy_config(self.port) 做 POST /load；若 self.port 仍是 local 分支的
            # 20219，/load 会把 admin 迁到 20219，随后 docker_request 仍 curl 2019 必然失败
            # （CI 确定性红，run 42375262184）。容器名前缀可用环境变量覆盖（共享主机要求 itst-team-*）。
            self.port = DOCKER_ADMIN_PORT
            prefix = os.environ.get("ITSP_CADDY_CONTRACT_NAME_PREFIX", "itsp-successor-caddy-m2-")
            self.container = prefix + os.urandom(4).hex()
            self.addCleanup(self._remove_container)
            image = os.environ.get("ITSP_CADDY_CONTRACT_IMAGE", "caddy:2.11.4")
            config = Path("/tmp") / (self.container + ".json")
            config.write_text(json.dumps(caddy_config(self.port)))
            created = subprocess.run(["docker", "create", "--network", "none", "--name", self.container, image,
                                      "caddy", "run", "--config", "/etc/caddy/itsp.json"], capture_output=True, text=True)
            self.assertEqual(created.returncode, 0, created.stderr)
            copied = subprocess.run(["docker", "cp", str(config), self.container + ":/etc/caddy/itsp.json"],
                                    capture_output=True, text=True)
            self.assertEqual(copied.returncode, 0, copied.stderr)
            started = subprocess.run(["docker", "start", self.container], capture_output=True, text=True)
            self.assertEqual(started.returncode, 0, started.stderr)
            self.request = lambda method, path, body=None, headers=None: self.docker_request(method, path, body, headers)
        else:
            self.fail("real Caddy binary or docker is required")
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                status, _headers, _body = self.request("GET", "/config/")
                if status == 200:
                    return
            except Exception:
                time.sleep(0.1)
        self.fail("caddy admin did not become ready")

    def tearDown(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def _remove_container(self):
        """docker 分支的清理：tearDown 只处理 local caddy 进程，容器必须在 addCleanup 中删除。"""
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def local_request(self, method, path, body=None, headers=None):
        command = ["curl", "-sS", "--max-time", "5", "-D", "-", "-o", "-", "-w", "\n__STATUS__%{http_code}",
                   "-X", method]
        for key, value in (headers or {}).items():
            command.extend(["-H", key + ": " + value])
        if body is not None:
            command.extend(["--data-binary", "@-"])
        command.append("http://127.0.0.1:%s%s" % (self.port, path))
        result = subprocess.run(command, input=body, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError("caddy admin transport failed")
        return ops.parse_curl_exchange(result.stdout)

    def docker_request(self, method, path, body=None, headers=None):
        command = ["docker", "exec", "-i", self.container, "curl", "-sS", "--max-time", "5", "-D", "-", "-o", "-",
                   "-w", "\n__STATUS__%{http_code}", "-X", method]
        for key, value in (headers or {}).items():
            command.extend(["-H", key + ": " + value])
        if body is not None:
            command.extend(["--data-binary", "@-"])
        command.append("http://127.0.0.1:%d%s" % (DOCKER_ADMIN_PORT, path))
        raw = subprocess.check_output(command, input=body)
        return ops.parse_curl_exchange(raw)

    def other_body(self):
        _status, _headers, body = self.request("GET", "/config/apps/http/servers/other/routes/0/handle/0/body")
        return json.loads(body)

    def dial_at(self, route):
        status, _headers, body = self.request(
            "GET", "/config/apps/http/servers/main/routes/%s/handle/0/upstreams/0/dial" % route)
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_route_etag_rejects_swapped_match_before_writing(self):
        config = caddy_config(self.port)
        config["apps"]["http"]["servers"]["main"]["routes"] = [
            {"match": [{"host": ["ybwznt.com", "www.ybwznt.com"]}],
             "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "old-front:80"}]}]},
            {"match": [{"host": ["files.example"]}],
             "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "old-front:80"}]}]},
        ]
        loaded = self.request("POST", "/load", json.dumps(config).encode(), {"Content-Type": "application/json"})
        self.assertIn(loaded[0], (200, 204))
        status, headers, _body = self.request(
            "GET", "/config/apps/http/servers/main/routes/0/handle/0/upstreams/0/dial")
        self.assertEqual(status, 200)
        self.swap_matches()
        scalar = self.request(
            "PATCH", "/config/apps/http/servers/main/routes/0/handle/0/upstreams/0/dial",
            json.dumps("wrong-front:80").encode(),
            {"If-Match": ops.header_etag(headers), "Content-Type": "application/json"})
        self.assertEqual(scalar[0], 200)
        self.assertEqual(self.dial_at(0), "wrong-front:80")
        reloaded = self.request("POST", "/load", json.dumps(config).encode(), {"Content-Type": "application/json"})
        self.assertIn(reloaded[0], (200, 204))
        swapped = {"done": False}
        calls = []

        def request(method, path, body=None, headers=None):
            calls.append((method, path))
            result = self.request(method, path, body, headers)
            if method == "GET" and path.endswith("/routes/0") and not swapped["done"]:
                swapped["done"] = True
                self.swap_matches()
            return result

        ops.conditional_dial_patch(request, "old-front:80", "new-front:80")
        self.assertNotIn(("POST", "/load"), calls)
        self.assertEqual(self.dial_at(0), "old-front:80")
        self.assertEqual(self.dial_at(1), "new-front:80")

    def swap_matches(self):
        self.request("PATCH", "/config/apps/http/servers/main/routes/0/match/0/host",
                     json.dumps(["files.example"]).encode(), {"Content-Type": "application/json"})
        self.request("PATCH", "/config/apps/http/servers/main/routes/1/match/0/host",
                     json.dumps(["ybwznt.com", "www.ybwznt.com"]).encode(), {"Content-Type": "application/json"})


class FileRestartTests(unittest.TestCase):
    def test_reread_preserves_same_inode_edit_across_caddy_restart(self):
        binary = os.environ.get("ITSP_CADDY_BIN") or shutil.which("caddy") or "/tmp/caddy-2.11.4"
        self.assertTrue(Path(binary).is_file() or shutil.which("docker"), "real Caddy is required")
        port = 20319
        config = caddy_config(port)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "caddy.json"
        path.write_bytes(json.dumps(config).encode())
        inode = [path.stat().st_dev, path.stat().st_ino]
        release = object.__new__(ops.Release)
        release.caddyfile = path

        def hook():
            current = path.read_bytes()
            path.write_bytes(current.replace(b'"body": "other-old"', b'"body": "other-concurrent"'))

        release.caddyfile_before_commit = hook
        updated = ops.Release.commit_caddyfile_dial(release, b"old-front:80", b"new-front:80", inode)
        self.assertIn(b"other-concurrent", updated)
        self.assertIn(b"new-front:80", updated)
        self.assertEqual([path.stat().st_dev, path.stat().st_ino], inode)
        if Path(binary).is_file():
            process = subprocess.Popen([binary, "run", "--config", str(path)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: process.poll() is not None or process.terminate())
            self.wait_admin(port)
            body = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%s/config/apps/http/servers/other/routes/0/handle/0/body" % port).read())
            dial = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%s/config/apps/http/servers/main/routes/0/handle/0/upstreams/0/dial" % port).read())
            self.assertEqual(body, "other-concurrent")
            self.assertEqual(dial, "new-front:80")
            process.terminate()
            process.wait(timeout=5)
        mutated_source = textwrap.dedent(inspect_source(ops.Release.commit_caddyfile_dial)).replace(
            "current = self.read_fd(fd)", "current = stale", 1)
        namespace = {}
        exec(mutated_source, dict(ops.__dict__), namespace)
        path.write_bytes(json.dumps(config).encode())
        lost = namespace["commit_caddyfile_dial"](release, b"old-front:80", b"new-front:80", inode)
        self.assertNotIn(b"other-concurrent", lost)
        path.write_bytes(json.dumps(config).encode())
        restored = ops.Release.commit_caddyfile_dial(release, b"old-front:80", b"new-front:80", inode)
        self.assertIn(b"other-concurrent", restored)

    def wait_admin(self, port):
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen("http://127.0.0.1:%s/config/" % port, timeout=0.2).read()
                return
            except Exception:
                time.sleep(0.1)
        self.fail("caddy did not restart")


class VerifyTraversalTests(unittest.TestCase):
    def test_hotfix_verify_reads_cutover_active(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        live = root / "Caddyfile"
        live.write_bytes(b"candidate-bytes")
        (root / "Caddyfile.candidate").write_bytes(b"candidate-bytes")
        active = {"apps": {"http": {"servers": {"other": {"keep": "other-concurrent"}}}}}
        inode = [live.stat().st_dev, live.stat().st_ino]
        manifest = b"{}\n"
        release = object.__new__(ops.Release)
        release.root = root
        release.caddyfile = live
        release.sha = ops.APP_SHA
        release.manifest_bytes = manifest
        release.m = {"target_head": ops.SCHEMA, "port": 18080, "additional_writers": [],
                     "old_app_id": "ab" * 32, "old_app_image": "sha256:" + "c" * 64}
        release.old_app, release.app, release.front = "old-app", "new-app", "new-front"
        release.active = lambda: active
        release.schema = lambda: ops.SCHEMA
        release.caddy_destination = lambda: "/etc/caddy/Caddyfile"
        container = {"Id": "a" * 64, "Image": "sha256:" + "b" * 64, "RestartCount": 0,
                     "State": {"Running": True, "StartedAt": "2026-11-07T00:00:00Z", "OOMKilled": False}}
        release.inspect = Mock(side_effect=lambda name, image=False: (
            {"Config": {"Labels": {"org.opencontainers.image.revision": ops.APP_SHA}}} if image else container))
        bundle = b"asset " + ops.APP_VERSION.encode()
        digest = ops.h.digest(bundle)

        def run(argv, input=None, **kwargs):
            if "sha256sum" in argv:
                return (digest + "  file\n").encode() if False else (ops.h.digest(b"candidate-bytes") + "  file\n").encode()
            if "grep" in argv:
                return b"/usr/share/nginx/html/assets/app.js\n"
            if "cat" in argv and argv[-1].endswith("index.html"):
                return b'<script src="/assets/app.js"></script>'
            if argv[-1].endswith("app.js"):
                return bundle
            if argv[1] == "logs":
                return b"ready\n"
            if argv[:2] == ["docker", "ps"]:
                return b""
            raise AssertionError(argv)

        release.run = run
        release.http = Mock(return_value={"sha256": digest})
        release.smoke = lambda base: {"ok": True}
        release.writers_exact = Mock()
        identity = {"release": ops.APP_SHA, "manifest_sha256": ops.h.digest(manifest), "status": "PASS"}
        ops.h.save(root / "preflight.json", {**identity, "phase": "preflight", "inode": inode, "containers": {},
                                              "protected_configs": {}, "active": active})
        ops.h.save(root / "cutover.json", {**identity, "phase": "cutover", "time": "2026-11-07T00:00:00Z"})
        with self.assertRaises(KeyError):
            ops.hf.Hotfix.verify(release)
        ops.h.save(root / "bluegreen.json", {**identity, "phase": "bluegreen",
                                              "app": ops.h.fingerprint(container), "frontend": ops.h.fingerprint(container)})
        (root / "cutover.json").unlink()
        ops.h.save(root / "cutover.json", {**identity, "phase": "cutover", "time": "2026-11-07T00:00:00Z",
                                           "active": active})
        result = ops.Release.verify(release)
        self.assertEqual(result["schema"], ops.SCHEMA)
        self.assertEqual(result["migration_performed"], False)
        self.assertEqual(result["published_version"]["version"], ops.APP_VERSION)
        release.writers_exact.assert_called_once_with({"new-app"})


class SuccessorManifestAndTestContractTests(unittest.TestCase):
    """P2-1 / P2-4：manifest 继承链字段、docker 端口自洽与容器清理的 hermetic 契约（不依赖 docker）。"""

    def test_manifest_requires_reviewed_inherited_helper_digest(self):
        helper = DEPLOY / "presales3_ops.py"
        self.assertEqual(ops.HELPER_DIGEST, hashlib.sha256(helper.read_bytes()).hexdigest())
        cases = [
            (lambda: {key: value for key, value in manifest().items() if key != "helper_sha256"},
             "invalid identity: helper_sha256"),
            (lambda: manifest(helper_sha256="not-a-digest"), "invalid identity: helper_sha256"),
            (lambda: manifest(helper_sha256="0" * 64), "historical helper digest differs"),
        ]
        for build, message in cases:
            with self.assertRaisesRegex(RuntimeError, message):
                ops.validate(build())
        ops.validate(manifest())

    def test_manifest_required_identity_keys_are_validated(self):
        for key in ("old_app_id", "old_front_id", "source_sha256", "controller_sha256", "smoke_sha256",
                    "nginx_sha256", "predecessor_owner_sha256", "evidence_sha256", "db_id",
                    "visibility_sha256", "pool_sha256", "helper_sha256"):
            value = manifest()
            value.pop(key, None)
            with self.assertRaisesRegex(RuntimeError, "invalid identity: " + key):
                ops.validate(value)

    def test_docker_admin_port_is_single_source(self):
        self.assertEqual(DOCKER_ADMIN_PORT, 2019)
        self.assertEqual(caddy_config(DOCKER_ADMIN_PORT)["admin"]["listen"],
                         "127.0.0.1:%d" % DOCKER_ADMIN_PORT)
        instance = object.__new__(CaddyConditionalTests)
        instance.container = "itsp-test-container"
        with patch.object(subprocess, "check_output",
                          return_value=b"HTTP/1.1 200 OK\r\nETag: x\r\n\r\n{}\n__STATUS__200") as call:
            instance.docker_request("GET", "/config/")
        self.assertIn("http://127.0.0.1:%d/config/" % DOCKER_ADMIN_PORT, call.call_args[0][0])
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertIn("self.port = DOCKER_ADMIN_PORT", source)
        docker_branch = source.split('elif shutil.which("docker"):')[1].split("else:")[0]
        self.assertNotIn("self.port = 20219", docker_branch)

    def test_docker_branch_registers_container_cleanup(self):
        source = Path(__file__).read_text(encoding="utf-8")
        docker_branch = source.split('elif shutil.which("docker"):')[1].split("else:")[0]
        self.assertIn("self.addCleanup(self._remove_container)", docker_branch)
        self.assertIn("def _remove_container(self)", source)
        self.assertIn('"docker", "rm", "-f", self.container', source)

    def test_route_identity_rejects_or_branches_without_host(self):
        """独立复审 F1：matcher set 为 OR；无 host 的并行分支不得被判为主站。"""
        route = caddy_config(20219)["apps"]["http"]["servers"]["main"]["routes"][0]
        self.assertTrue(ops.route_is_main(route, "old-front:80"))
        handle = route["handle"]
        for bad in (
            {"match": [{"host": ["ybwznt.com", "www.ybwznt.com"]}, {"path": ["/"]}], "handle": handle},
            {"match": [{"path": ["/"]}], "handle": handle},
            {"match": [], "handle": handle},
            {"match": [{"host": ["ybwznt.com", "www.ybwznt.com"]}, {"host": ["files.example"]}], "handle": handle},
            {"match": [{"host": ["ybwznt.com", "www.ybwznt.com"]}], "handle": handle, "extra": 1},
        ):
            self.assertFalse(ops.route_is_main(bad, "old-front:80"))

    def test_adapt_uses_live_caddy_offline_stdin(self):
        """F2：持久字节必须用现场 Caddy 离线 adapt，且走 stdin（不 load、不依赖宿主机二进制）。"""
        release = object.__new__(ops.Release)
        release.caddyfile = Path("/tmp/unused")
        release.caddy_destination = Mock(return_value="/opt/it-spareparts/Caddyfile")
        release.run = Mock(return_value=b'{"apps": {}}')
        data = b"ybwznt.com { reverse_proxy old-front:80 }"
        self.assertEqual(ops.Release.adapt_caddyfile_bytes(release, data), {"apps": {}})
        argv = release.run.call_args.args[0]
        self.assertIn("adapt", argv)
        self.assertIn("--config", argv)
        self.assertIn("-", argv)
        self.assertEqual(release.run.call_args.kwargs.get("input"), data)

    def test_commit_rejects_persisted_config_that_adapts_to_other_sites(self):
        """F2：重读后的持久 Caddyfile adaptation 与计划运行配置不一致时，必须在写入前拒绝。"""
        release = object.__new__(ops.Release)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        release.root = Path(folder.name)
        caddyfile = release.root / "Caddyfile"
        caddyfile.write_bytes(b"ybwznt.com { reverse_proxy old-front:80 }\n")
        release.caddyfile = caddyfile
        release.read_fd = lambda _fd: caddyfile.read_bytes()
        release.adapt_caddyfile_bytes = Mock(return_value={"sites": {"files.example": {}}})
        inode = [os.stat(caddyfile).st_dev, os.stat(caddyfile).st_ino]
        with self.assertRaisesRegex(RuntimeError, "persisted Caddyfile adaptation differs"):
            ops.Release.commit_caddyfile_dial(
                release, b"old-front:80", b"new-front:80", inode,
                {"sites": {"ybwznt.com": {}}})
        self.assertEqual(caddyfile.read_bytes(),
                         b"ybwznt.com { reverse_proxy old-front:80 }\n")
        release.adapt_caddyfile_bytes.assert_called_once_with(
            b"ybwznt.com { reverse_proxy old-front:80 }\n")
        """独立复审 F5：recover 资格检查在写 intent 之前，拒绝不污染发布状态。"""
        release = object.__new__(ops.Release)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        release.root = Path(folder.name)
        release.schema = Mock(return_value=ops.SCHEMA)
        release.run = Mock()
        (release.root / "exposure-started.json").write_text("{}\n")
        with self.assertRaisesRegex(RuntimeError, "forward repair"):
            ops.Release.recover_gate(release)
        release.run.assert_not_called()
        clean = object.__new__(ops.Release)
        folder2 = tempfile.TemporaryDirectory()
        self.addCleanup(folder2.cleanup)
        clean.root = Path(folder2.name)
        clean.schema = Mock(return_value=ops.SCHEMA)
        clean.run = Mock()
        self.assertIsNone(ops.Release.recover_gate(clean))


def inspect_source(function):
    import inspect
    return inspect.getsource(function)


if __name__ == "__main__":
    unittest.main()
