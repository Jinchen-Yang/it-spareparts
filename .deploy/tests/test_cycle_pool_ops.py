"""Contracts for the repair-pool migration controller. No production access."""
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

DEPLOY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cycle_pool_ops", DEPLOY / "cycle_pool_ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)
dry_spec = importlib.util.spec_from_file_location("cycle_pool_dry_run", DEPLOY / "cycle_pool_dry_run.py")
dry = importlib.util.module_from_spec(dry_spec)
dry_spec.loader.exec_module(dry)


def manifest(**overrides):
    controller = hashlib.sha256((DEPLOY / "cycle_pool_ops.py").read_bytes()).hexdigest()
    result = {
        "release": ops.APP_SHA, "old_revision": ops.OLD_SHA, "old_schema": ops.OLD_SCHEMA,
        "mid_schema": ops.MID_SCHEMA, "target_head": ops.TARGET,
        "upgrade_path": [ops.OLD_SCHEMA, ops.MID_SCHEMA, ops.TARGET],
        "old_app": ops.OLD_PREFIX + "-app", "old_front": ops.OLD_PREFIX + "-frontend",
        "caddyfile": "/opt/it-spareparts/Caddyfile", "port": 18107, "subnet": "10.44.0.0/28",
        "additional_writers": [], "nginx_upstream_count": 1,
        "downgrade_allowed": False, "compose_down_allowed": False,
        "visibility_sha256": ops.VISIBILITY_DIGEST, "successor_sha256": ops.SUCCESSOR_DIGEST,
        "pool_sha256": ops.vis.POOL_DIGEST, "helper_sha256": ops.HELPER_DIGEST,
        "smoke_sha256": ops.SMOKE_DIGEST, "dry_run_sha256": ops.DRY_RUN_DIGEST,
        "controller_sha256": controller, "cycle_sell_token_sha256": "a" * 64,
    }
    for key in ("old_app_id", "old_front_id", "source_sha256", "nginx_sha256",
                "predecessor_owner_sha256", "evidence_sha256", "db_id"):
        result[key] = "b" * 64
    for key in ("old_app_image", "old_front_image", "app_image", "frontend_image", "db_image"):
        result[key] = "sha256:" + "c" * 64
    result["proxy_identities"] = {name: {"id": "d" * 64, "image": "sha256:" + "e" * 64, "process": name}
                                  for name in ("caddy", "cloudflared", "nginx")}
    result.update(overrides)
    return result


def snapshot_for(controller):
    digest = hashlib.sha256(controller).hexdigest()
    body = manifest(controller_sha256=digest)
    dial = ops.OLD_PREFIX + "-frontend:80"
    route = {"match": [{"host": ["ybwznt.com", "www.ybwznt.com"]}],
             "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": dial}]}], "terminal": True}
    mounts_a = [
        {"Destination": "/data/files", "Type": "volume", "Source": "/vol", "RW": True},
        {"Destination": "/run/secrets/db", "Type": "bind", "Source": "/sec", "RW": False},
    ]
    file_sha = "b" * 64
    return {
        "release": ops.APP_SHA, "captured_at": "2026-11-12T00:00:00+00:00", "controller_sha256": digest,
        "manifest": body, "manifest_sha256": file_sha,
        "files": {"manifest.json": file_sha, "predecessor_owner": body["predecessor_owner_sha256"], "nginx": body["nginx_sha256"]},
        "alembic_heads": {"stdout": ops.TARGET + " (head)", "code": 0},
        "alembic_current": {"stdout": ops.OLD_SCHEMA + " (mergepoint)", "code": 0},
        "route": route, "route_dial": dial, "mounts_a": mounts_a, "mounts_b": list(reversed(mounts_a)),
        "nginx_sha256": body["nginx_sha256"],
        "predecessor": {"path": ops.PREDECESSOR_OWNER, "release": ops.OLD_SHA, "rewritten": False},
        "daily_backup_dir": "/data/backups/spareparts", "legacy_backup_dir_used": False,
        "planned_upgrade": ["upgrade", "head"], "upgrade_path": [ops.OLD_SCHEMA, ops.MID_SCHEMA, ops.TARGET],
        "environment_file_labels": [],
        "receipts": {
            "preflight": {"status": "PASS", "release": ops.APP_SHA, "manifest_sha256": file_sha, "schema": ops.OLD_SCHEMA},
            "images": {"status": "PASS", "release": ops.APP_SHA, "manifest_sha256": file_sha},
        },
    }


class PinTests(unittest.TestCase):
    def test_historical_controllers_stay_pinned(self):
        self.assertEqual(ops.VISIBILITY_DIGEST, hashlib.sha256(ops.VISIBILITY_PATH.read_bytes()).hexdigest())
        self.assertEqual(ops.SUCCESSOR_DIGEST, hashlib.sha256(ops.SUCCESSOR_PATH.read_bytes()).hexdigest())
        self.assertEqual(ops.vis.TARGET, "98afc0d2157b")
        self.assertEqual(ops.vis.OLD_HEAD, "e5a7c9d1f3b6")
        successor = ops.SUCCESSOR_PATH.read_text(encoding="utf-8")
        self.assertIn('ALLOWED_ALEMBIC = {"heads", "current", "check"}', successor)
        self.assertNotIn(ops.APP_SHA, successor)
        self.assertNotIn("c3e8a1d94b27", successor)
        self.assertIsNot(ops.Release.migrate, ops.vis.Release.migrate)
        self.assertIsNot(ops.Release.cutover, ops.vis.Release.cutover)
        self.assertIn('["upgrade", "head"]', ops.inspect.getsource(ops.Release.migrate) if False else __import__("inspect").getsource(ops.Release.migrate))

    def test_release_identity_is_the_merge_commit(self):
        ops.validate(manifest())
        for value in (ops.PARENT, ops.OLD_SHA, "7e5f15e5" + "ab" * 16, "a" * 40):
            with self.assertRaises(RuntimeError):
                ops.validate(manifest(release=value))
        for key, value in (
            ("target_head", ops.OLD_SCHEMA), ("old_schema", ops.TARGET), ("mid_schema", ops.TARGET),
            ("upgrade_path", [ops.OLD_SCHEMA, ops.TARGET]), ("downgrade_allowed", True),
            ("compose_down_allowed", True), ("additional_writers", [{"name": "other"}]),
            ("source_handoff", {}), ("catalog_sync", {}), ("old_app", "itsp-requirements-782c858a75d1-app"),
            ("successor_sha256", "0" * 64), ("daily_backup_dir", "/var/backups/spareparts"),
        ):
            with self.subTest(key=key):
                if key == "daily_backup_dir":
                    self.assertEqual(str(ops.DAILY_BACKUP_DIR), "/data/backups/spareparts")
                    self.assertNotEqual(ops.DAILY_BACKUP_DIR, ops.LEGACY_BACKUP_DIR)
                    continue
                with self.assertRaises(RuntimeError):
                    ops.validate(manifest(**{key: value}))

    def test_controller_does_not_rewrite_the_successor_owner_or_load_caddy(self):
        text = (DEPLOY / "cycle_pool_ops.py").read_text(encoding="utf-8")
        self.assertIn(ops.PREDECESSOR_OWNER, text)
        self.assertIn(ops.OWNER_NAME, text)
        self.assertNotIn("unlink", text)
        cutover = __import__("inspect").getsource(ops.Release.cutover)
        self.assertLess(cutover.index("exposure-started.json"), cutover.index("commit_caddyfile_dial"))
        self.assertNotIn('"/load"', cutover)
        self.assertNotIn("load_active(", cutover)
        self.assertIn("replace_active(baseline_adapted, old_dial, new_dial)", cutover)
        with self.assertRaisesRegex(RuntimeError, "unconditional config load refused"):
            ops.Release.load_active(object.__new__(ops.Release), {})


class MigrationTests(unittest.TestCase):
    def release(self, head):
        release = object.__new__(ops.Release)
        release._head = head
        release.schema = lambda container=ops.h.DB: release._head
        release.marker = Mock(side_effect=lambda name: {
            "backup-old": {"restore": {"actual_restore_performed": True, "schema": ops.OLD_SCHEMA,
                                       "network": "none", "published_ports": []},
                           "uploads_content_compared": True},
            "preflight": {"schema": ops.OLD_SCHEMA},
            "freeze": {"counts": {"maintenance_project": 3, "maintenance_project_contract": 4}},
        }[name])
        release.frozen = Mock()
        release.writers_exact = Mock()
        release.source_tcp_zero = Mock()
        release.unchanged_business = Mock()
        release.assert_cycle_shape = Mock()
        release.counts = Mock(return_value={"maintenance_project": 3, "maintenance_project_contract": 4})
        seen = []

        def alembic(args):
            seen.append(list(args))
            if args == ["show", ops.TARGET]:
                return "Parent: " + ops.MID_SCHEMA + "\n"
            if args == ["show", ops.MID_SCHEMA]:
                return "Parent: " + ops.OLD_SCHEMA + "\n"
            if args == ["heads"]:
                return ops.TARGET + " (head)"
            if args == ["current"]:
                if release._head == ops.OLD_SCHEMA:
                    return ops.OLD_SCHEMA + " (mergepoint)"
                if release._head == ops.MID_SCHEMA:
                    return ops.MID_SCHEMA
                return ops.TARGET + " (head)"
            if args == ["upgrade", "head"]:
                release._head = ops.TARGET
                return ""
            if args == ["check"]:
                return ""
            raise AssertionError(args)

        release.alembic = alembic
        release.seen = seen
        return release

    def test_upgrade_head_from_98af_and_refuses_downgrade(self):
        release = self.release(ops.OLD_SCHEMA)
        with unittest.mock.patch.object(ops.vis.pool.hf.Hotfix, "alembic", side_effect=AssertionError("raw alembic")):
            for args in (["downgrade", ops.OLD_SCHEMA], ["stamp", ops.TARGET], ["upgrade", ops.TARGET], ["upgrade"]):
                with self.assertRaises(RuntimeError):
                    ops.Release.alembic(object.__new__(ops.Release), args)
        result = ops.Release.migrate(release)
        self.assertEqual(result["upgrade_argv"], ["upgrade", "head"])
        self.assertEqual(result["path"], [ops.OLD_SCHEMA, ops.MID_SCHEMA, ops.TARGET])
        self.assertTrue(result["upgrade_invoked"])
        self.assertFalse(result["downgrade_invoked"])
        self.assertFalse(result["alembic_version_rewritten"])
        self.assertEqual(result["maintenance_project"], 3)
        self.assertEqual(result["cycle_tables_zero"], True)
        self.assertEqual(release.seen, [["show", ops.TARGET], ["show", ops.MID_SCHEMA], ["heads"], ["current"],
                                         ["upgrade", "head"], ["check"]])

    def test_partial_86f7_reruns_upgrade_head_without_downgrade(self):
        release = self.release(ops.MID_SCHEMA)
        result = ops.Release.migrate(release)
        self.assertEqual(result["from"], ops.MID_SCHEMA)
        self.assertEqual(result["to"], ops.TARGET)
        self.assertIn(["upgrade", "head"], release.seen)
        self.assertNotIn(["downgrade", ops.OLD_SCHEMA], release.seen)

    def test_current_accepts_98af_mergepoint_and_heads_stay_exact(self):
        release = object.__new__(ops.Release)
        release.schema = Mock(return_value=ops.OLD_SCHEMA)
        release.alembic = Mock(side_effect=[
            ops.OLD_SCHEMA + " (mergepoint)",
            ops.MID_SCHEMA,
            ops.TARGET + " (head)",
        ])
        self.assertEqual(ops.Release.current_revision(release), ops.OLD_SCHEMA + " (mergepoint)")
        release.schema = Mock(return_value=ops.MID_SCHEMA)
        self.assertEqual(ops.Release.current_revision(release), ops.MID_SCHEMA)
        release.schema = Mock(return_value=ops.TARGET)
        self.assertEqual(ops.Release.current_revision(release), ops.TARGET + " (head)")
        release.schema = Mock(return_value=ops.OLD_SCHEMA)
        release.alembic = Mock(return_value=ops.OLD_SCHEMA + " (head) (mergepoint)")
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            ops.Release.current_revision(release)
        release.schema = Mock(return_value=ops.MID_SCHEMA)
        release.alembic = Mock(return_value=ops.MID_SCHEMA + " (head)")
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            ops.Release.current_revision(release)
        release.alembic = Mock(return_value=ops.TARGET + " (head)")
        self.assertEqual(ops.Release.head_revision(release), ops.TARGET + " (head)")
        release.alembic = Mock(return_value=ops.TARGET + " (head) (mergepoint)")
        with self.assertRaisesRegex(RuntimeError, "candidate migration head"):
            ops.Release.head_revision(release)

    def test_recover_does_not_downgrade_a_moved_schema(self):
        release = object.__new__(ops.Release)
        release.root = Path("/tmp")
        release.schema = Mock(return_value=ops.MID_SCHEMA)
        with self.assertRaisesRegex(RuntimeError, "do not downgrade"):
            ops.Release.recover_gate(release)
        self.assertNotIn('["downgrade"', __import__("inspect").getsource(ops.Release.rollback))
        self.assertNotIn("UPDATE alembic_version", __import__("inspect").getsource(ops.Release.rollback))

    def test_page_key_projection_is_the_only_allowed_row_change(self):
        release = object.__new__(ops.Release)
        role = ops.Release._permission_expr(release, "permissions", "merge_always")
        user = ops.Release._permission_expr(release, "template_perms", "merge_object")
        removed = ops.Release._permission_expr(release, "perm_overrides", "remove_object")
        self.assertIn("ELSE '{}'::jsonb", role)
        self.assertIn("'page_cycle_repair', false", role)
        self.assertIn("ELSE template_perms", user)
        self.assertNotIn("'{}'", user)
        self.assertIn("- 'page_cycle_wip'", removed)
        self.assertIn("ELSE perm_overrides", removed)
        business = {"sys_user": {"columns": ["id", "username"], "sha256": "1" * 64, "bytes": 1}}
        before = {"sys_user.permissions": {"sha256": "a" * 64, "bytes": 2}}
        after = {"sys_user.permissions": {"sha256": "b" * 64, "bytes": 3}}
        release.marker = Mock(return_value={
            "business_fingerprints": business, "permission_before": before, "permission_after": after,
        })
        release.data_fingerprints = Mock(return_value=business)
        release.permission_hashes = Mock(return_value=before)
        release.schema = Mock(return_value=ops.OLD_SCHEMA)
        self.assertTrue(ops.Release.unchanged_business(release))
        release.schema = Mock(return_value=ops.MID_SCHEMA)
        release.permission_hashes = Mock(return_value=after)
        self.assertTrue(ops.Release.unchanged_business(release))
        release.permission_hashes = Mock(return_value=before)
        with self.assertRaisesRegex(RuntimeError, "cycle page keys"):
            ops.Release.unchanged_business(release)
        release.schema = Mock(return_value=ops.OLD_SCHEMA)
        release.data_fingerprints = Mock(return_value={
            "sys_user": {"columns": ["id", "username"], "sha256": "f" * 64, "bytes": 9},
        })
        release.permission_hashes = Mock(return_value=before)
        with self.assertRaisesRegex(RuntimeError, "business rows changed"):
            ops.Release.unchanged_business(release)
        source = __import__("inspect").getsource(ops.Release.recreate_app)
        self.assertLess(source.index("outbound_names"), source.index('["docker", "stop"'))
        self.assertLess(source.index("create.redacted.json"), source.index('["docker", "stop"'))
        self.assertLess(source.index("cycle_environment"), source.index('["docker", "stop"'))


class CandidateEnvTests(unittest.TestCase):
    def test_candidate_env_is_explicit_and_outbound_stays_off(self):
        release = object.__new__(ops.Release)
        token = "T" * 32
        release.m = {"cycle_sell_token_sha256": hashlib.sha256(token.encode()).hexdigest()}
        release.read_token = lambda: token
        original = ["DATABASE_URL=postgresql://db/spareparts", "SHOP_CATALOG_SYNC_ENABLED=false", "PN_MASTER_MODE=off"]
        env = ops.Release.cycle_environment(release, original, "false")
        self.assertEqual(env[:3], original)
        self.assertIn("SHOP_CYCLE_SELL_ENABLED=false", env)
        self.assertIn("SHOP_CYCLE_SELL_URL=" + ops.CYCLE_URL, env)
        self.assertIn("SHOP_CYCLE_SELL_TOKEN=" + token, env)
        self.assertNotIn("environment_file", " ".join(env))
        with self.assertRaisesRegex(RuntimeError, "callback token"):
            ops.Release.cycle_environment(release, original + ["CYCLE_SELL_CALLBACK_TOKEN_SHA256=" + "ab" * 32], "false")

    def test_mounts_normalize_and_route_accepts_terminal_true(self):
        base = {"Config": {"Image": "sha256:abc"}, "HostConfig": {"NetworkMode": "it-spareparts_default"},
                "Mounts": [{"Destination": "/b", "Type": "bind", "Source": "/b"},
                           {"Destination": "/a", "Type": "bind", "Source": "/a"}]}
        flipped = dict(base, Mounts=list(reversed(base["Mounts"])))
        self.assertEqual(ops.Release.config(base), ops.Release.config(flipped))
        drifted = dict(base, Mounts=[dict(base["Mounts"][0], Source="/evil"), base["Mounts"][1]])
        self.assertNotEqual(ops.Release.config(base), ops.Release.config(drifted))
        route = {"match": [{"host": ["www.ybwznt.com", "ybwznt.com"]}],
                 "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "old:80"}]}], "terminal": True}
        self.assertTrue(ops.route_is_main(route, "old:80"))
        self.assertFalse(ops.route_is_main(dict(route, terminal=False), "old:80"))
        self.assertFalse(ops.route_is_main(dict(route, group="g1"), "old:80"))

    def test_compose_down_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "compose down refused"):
            ops.refuse_compose_down(["docker", "compose", "-p", "it-spareparts", "down"])
        with self.assertRaisesRegex(RuntimeError, "it-spareparts compose down refused"):
            ops.refuse_compose_down(["docker", "down", "it-spareparts"])

    def test_freeze_refuses_a_partial_dry_run(self):
        release = object.__new__(ops.Release)
        release.marker = Mock(return_value={"checks_passed": 18, "checks_failed": 1, "negatives": "ALL_OK",
                                             "controller_sha256": "b" * 64})
        with self.assertRaisesRegex(RuntimeError, "19/19"):
            ops.Release.freeze(release)


class DryRunTests(unittest.TestCase):
    def test_live_snapshot_shape_passes_19_and_negatives(self):
        controller = (DEPLOY / "cycle_pool_ops.py").read_bytes()
        report = dry.evaluate(snapshot_for(controller), controller)
        self.assertEqual(report["passed"], 19)
        self.assertEqual(report["failed"], 0, report["errors"])
        negatives = dry.negative_report(snapshot_for(controller), controller)
        self.assertEqual(negatives["status"], "ALL_OK")
        self.assertGreaterEqual(negatives["count"], 21)
        self.assertEqual(negatives["refused"], negatives["count"])
        self.assertEqual(len(dry.CHECKS), 19)


class OutboundTests(unittest.TestCase):
    def test_enable_recreates_only_the_app(self):
        import tempfile
        release = object.__new__(ops.Release)
        release.sha = ops.APP_SHA
        release.app = "itsp-cycle-ccefa17cc644-app"
        release.front = "itsp-cycle-ccefa17cc644-frontend"
        token = "t" * 40
        release.m = {"app_image": "sha256:" + "c" * 64, "port": 18080,
                     "cycle_sell_token_sha256": hashlib.sha256(token.encode()).hexdigest()}
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        release.root = Path(folder.name)
        (release.root / "exposure-started.json").write_text("{}\n")
        (release.root / (release.app + ".create.redacted.json")).write_text("{}\n")
        release.caddyfile = release.root / "Caddyfile"
        release.caddyfile.write_bytes(b"shop.ybwznt.com { reverse_proxy it-shop-next-frontend:80 }\n")
        base_env = [
            "SHOP_CYCLE_SELL_ENABLED=false",
            "SHOP_CYCLE_SELL_URL=" + ops.CYCLE_URL,
            "SHOP_CYCLE_SELL_TOKEN=" + token,
            "SHOP_CATALOG_SYNC_ENABLED=false",
        ]
        state = {"enabled": "false"}

        def app_row():
            env = [("SHOP_CYCLE_SELL_ENABLED=" + state["enabled"] if item.startswith("SHOP_CYCLE_SELL_ENABLED=") else item)
                   for item in base_env]
            return {"Id": "a" * 64, "Image": release.m["app_image"], "RestartCount": 0,
                    "State": {"Running": True, "StartedAt": "t", "OOMKilled": False},
                    "Config": {"Env": env, "Labels": {"bg.release": ops.APP_SHA}, "Hostname": "app"},
                    "HostConfig": {"NetworkMode": release.app + "-net"}, "Name": "/" + release.app,
                    "NetworkSettings": {"Networks": {}}}

        protected = (release.front, ops.h.DB, ops.h.CADDY, "it-shop-next-frontend", "it-shop-next-backend", "it-shop-next-db")

        def inspect(name, image=False):
            if name in {release.app, "a" * 64}:
                return app_row()
            if name in protected:
                return {"Id": name, "Image": "sha256:" + "1" * 64, "RestartCount": 0,
                        "State": {"Running": True, "StartedAt": "t", "OOMKilled": False}}
            raise AssertionError(name)

        commands = []

        def run(argv, **kwargs):
            commands.append(list(argv))
            if argv[:2] == ["docker", "start"]:
                state["enabled"] = "true"
            return b""

        release.inspect = inspect
        release.schema = Mock(return_value=ops.TARGET)
        release.run = run
        release.clone = Mock(return_value={"id": "b" * 64})
        release.wait_http = Mock()
        release.read_token = lambda: token
        result = ops.Release.enable_outbound(release)
        self.assertTrue(result["enabled"] and result["db_untouched"] and result["caddy_untouched"] and result["shop_untouched"])
        self.assertNotIn(token, json.dumps(result))
        stopped = [argv for argv in commands if argv[:2] == ["docker", "stop"]]
        self.assertEqual(stopped, [["docker", "stop", "--time", "60", "a" * 64]])
        self.assertFalse(any(ops.h.DB in argv or ops.h.CADDY in argv or "it-shop-next-frontend" in argv for argv in commands))
        prepared = release.clone.call_args.args[1]["Config"]["Env"]
        self.assertIn("SHOP_CYCLE_SELL_ENABLED=true", prepared)
        self.assertIn("SHOP_CATALOG_SYNC_ENABLED=false", prepared)
        audit_name = release.clone.call_args.kwargs["audit_name"]
        self.assertNotEqual(audit_name, release.app)
        self.assertTrue(audit_name.startswith(release.app + "-outbound-on-"))
        self.assertTrue((release.root / (release.app + ".create.redacted.json")).is_file())


class RecoverLockTests(unittest.TestCase):
    def test_plain_recover_intent_blocks_later_phases(self):
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        release = object.__new__(ops.Release)
        release.root = Path(folder.name)
        self.assertFalse(ops.Release.recover_started(release))
        (release.root / "recover.intent.json").write_text("{}\n")
        self.assertTrue(ops.Release.recover_started(release))
        self.assertFalse(ops.Release.recover_passed(release))
        (release.root / "recover-stamp.intent.json").write_text("{}\n")
        self.assertFalse(ops.Release.recover_passed(release))
        (release.root / "recover-stamp-abc.json").write_text("{}\n")
        self.assertTrue(ops.Release.recover_passed(release))


if __name__ == "__main__":
    unittest.main()
