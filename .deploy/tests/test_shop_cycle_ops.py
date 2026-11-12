"""Contracts for the shop cycle cutover. The 0026 -> 0027 controller stays put."""
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

DEPLOY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("shop_cycle_ops", DEPLOY / "shop_cycle_ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)
pn_spec = importlib.util.spec_from_file_location("shop_pn_ops", DEPLOY / "shop_pn_ops.py")
pn = importlib.util.module_from_spec(pn_spec)
pn_spec.loader.exec_module(pn)


class ShopBoundaryTests(unittest.TestCase):
    def test_pn_controller_is_unchanged_and_still_0026_to_0027(self):
        self.assertEqual(hashlib.sha256((DEPLOY / "shop_pn_ops.py").read_bytes()).hexdigest(), ops.SHOP_PN_DIGEST)
        self.assertEqual(pn.OLD_HEAD, "0026_pn_master_follow_merges")
        self.assertEqual(pn.NEW_HEAD, "0027_catalog_delivery_receipt")
        self.assertNotEqual(pn.NEW_HEAD, ops.TARGET_HEAD)

    def test_release_identity_and_heads(self):
        self.assertEqual((ops.OLD_HEAD, ops.TARGET_HEAD), ("0027_catalog_delivery_receipt", "0029_cycle_sell_qty"))
        self.assertTrue(ops.candidate_release_allowed("c" * 40))
        self.assertFalse(ops.candidate_release_allowed(ops.REFUSED_PREFIX + "ab" * 16))
        self.assertFalse(ops.candidate_release_allowed(ops.OLD_REVISION))
        self.assertFalse(ops.candidate_release_allowed("f5481cf1"))
        self.assertTrue(ops.script_refuses_down("echo check networks\n"))
        self.assertFalse(ops.script_refuses_down("docker compose -p it-spareparts down\n"))
        self.assertFalse(ops.script_refuses_down("docker compose --env-file /root/IT-shop/private/next.env down\n"))
        with self.assertRaisesRegex(RuntimeError, "compose down refused"):
            ops.refuse_compose_down(["docker", "compose", "-p", "it-spareparts", "down"])

    def test_migration_argv_is_the_official_compose_shape(self):
        release = object.__new__(ops.Release)
        release.m = {"migration_service": "migration"}
        release.prefix = "shop-cycle-" + "a" * 12
        release.compose_file = Path("/data/shop/releases/cycle-" + "a" * 40 + "/prod.compose.yml")
        argv = ops.Release.alembic_argv(release, ["upgrade", "head"])
        self.assertEqual(argv[:8], [
            "docker", "compose", "--env-file", "/root/IT-shop/private/next.env", "-p", "it-shop-next",
            "-f", str(release.compose_file),
        ])
        self.assertEqual(argv[8:10], ["--profile", "migration"])
        self.assertEqual(argv[-3:], ["alembic", "upgrade", "head"])
        self.assertNotIn("down", argv)
        self.assertNotIn("cat", argv)
        for args in (["downgrade", ops.OLD_HEAD], ["stamp", ops.TARGET_HEAD], ["upgrade", ops.TARGET_HEAD]):
            with self.assertRaises(RuntimeError):
                ops.Release.alembic_argv(release, args)

    def test_env_preserves_catalog_and_keeps_the_cycle_token_empty(self):
        old = {"Config": {"Env": ["CATALOG_SYNC_ENABLED=true", "PN_MASTER_MODE=off", "OTHER=keep"],
                          "Labels": {}, "Image": "original"}, "HostConfig": {"NetworkMode": "it-shop-next_private"}}
        release = object.__new__(ops.Release)
        release.release = "c" * 40
        cfg, host = ops.Release.candidate_config(release, old, "sha256:" + "d" * 64)
        self.assertEqual(host, {"NetworkMode": "it-shop-next_private"})
        values = dict(item.split("=", 1) for item in cfg["Env"])
        self.assertEqual(values["CATALOG_SYNC_ENABLED"], "true")
        self.assertEqual(values["PN_MASTER_MODE"], "off")
        self.assertEqual(values.get("CYCLE_SELL_TOKEN_SHA256", ""), "")
        self.assertEqual(old["Config"]["Env"], ["CATALOG_SYNC_ENABLED=true", "PN_MASTER_MODE=off", "OTHER=keep"])
        with self.assertRaisesRegex(RuntimeError, "catalog or PN master"):
            ops.Release.preserved_env(None, old, {"CATALOG_SYNC_ENABLED": "false", "PN_MASTER_MODE": "off"})
        with self.assertRaisesRegex(RuntimeError, "token must stay empty"):
            ops.Release.token_is_empty(None, {"Config": {"Env": ["CYCLE_SELL_TOKEN_SHA256=" + "ab" * 32]}})

    def test_migrate_upgrades_head_and_can_continue_from_0028(self):
        release = object.__new__(ops.Release)
        release.m = {"intermediate_head": "0028_cycle_sell_held", "migration_service": "migration"}
        release._head = ops.OLD_HEAD
        release.schema = lambda container=ops.DB: release._head
        release.mark = Mock(side_effect=lambda name: {
            "backup-old": {"status": "PASS"},
            "freeze": {"counts": {"catalog_part": 2}},
        }[name])
        release.protected = Mock()
        release.counts = Mock(return_value={"catalog_part": 2})
        release.cycle_tables = Mock(return_value={"cycle_sell_intent": 0})
        seen = []

        def alembic(args, timeout=180):
            seen.append(list(args))
            if args == ["heads"]:
                return ops.TARGET_HEAD + " (head)"
            if args == ["upgrade", "head"]:
                release._head = ops.TARGET_HEAD
                return ""
            if args == ["check"]:
                return ""
            raise AssertionError(args)

        release.alembic = alembic
        result = ops.Release.migrate(release)
        self.assertEqual(result["upgrade_argv"], ["upgrade", "head"])
        self.assertFalse(result["downgrade_invoked"])
        self.assertTrue(result["cycle_tables_zero"])
        self.assertEqual(seen, [["heads"], ["upgrade", "head"], ["check"]])
        release._head = "0028_cycle_sell_held"
        seen.clear()
        ops.Release.migrate(release)
        self.assertEqual(seen, [["heads"], ["upgrade", "head"], ["check"]])
        self.assertNotIn("downgrade", json.dumps(seen))

    def test_controller_never_edits_the_shop_caddy_site(self):
        text = (DEPLOY / "shop_cycle_ops.py").read_text(encoding="utf-8")
        self.assertIn("shop.ybwznt.com", text)
        self.assertIn("it-shop-next-frontend:80", text)
        self.assertIn('"caddy_edited": False', text)
        self.assertNotIn("Caddyfile.write", text)
        self.assertIn('"/root/IT-shop/private/next.env"', text)
        self.assertNotIn("next.env.read", text)


class ShopNetworkArgvTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        scripts = root / "deploy" / "scripts"
        scripts.mkdir(parents=True)
        self.check = scripts / "check-external-networks.sh"
        self.rollback = scripts / "rollback-networks.sh"
        self.check.write_text("echo check-external-networks\n", encoding="utf-8")
        self.rollback.write_text("echo rollback-networks\n", encoding="utf-8")
        compose = root / "prod.compose.yml"
        compose.write_text("name: it-shop-next\n", encoding="utf-8")
        source = root / "source.tar.gz"
        source.write_bytes(b"source")
        self.release = object.__new__(ops.Release)
        self.release.root = root
        self.release.release = "c" * 40
        self.release.compose_file = compose
        self.release.m = {
            "controller_sha256": hashlib.sha256((DEPLOY / "shop_cycle_ops.py").read_bytes()).hexdigest(),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "compose_sha256": hashlib.sha256(compose.read_bytes()).hexdigest(),
            "network_check_sha256": hashlib.sha256(self.check.read_bytes()).hexdigest(),
            "network_rollback_sha256": hashlib.sha256(self.rollback.read_bytes()).hexdigest(),
        }
        self.seen = []

        def run(argv, **kwargs):
            self.seen.append(list(argv))
            if argv[:1] == ["bash"]:
                raise RuntimeError("captured network argv")
            return b""

        self.release.run = run

    def test_preflight_check_passes_env_file_and_compose(self):
        with self.assertRaisesRegex(RuntimeError, "captured network argv"):
            ops.Release.preflight(self.release)
        argv = self.seen[0]
        self.assertEqual(argv, [
            "bash", str(self.check),
            "--env-file", "/root/IT-shop/private/next.env",
            "--compose", str(self.release.compose_file),
        ])
        ops.refuse_compose_down(argv)
        self.assertEqual(argv[argv.index("--env-file") + 1], ops.ENV_FILE)
        self.assertNotIn("down", argv)

    def test_activate_rollback_reconnects_with_env_file_and_compose(self):
        self.release.mark = Mock(return_value={"created": {name: name + "-id" for name in ops.APPS}})
        self.release.protected = Mock()
        self.release.schema = Mock(return_value=ops.TARGET_HEAD)
        self.release.posted_cycle_rows = Mock(return_value=0)
        self.release.token_is_empty = Mock(return_value=True)
        self.release.inspect = Mock(return_value={"State": {"Running": True, "Health": {"Status": "healthy"}}})
        with self.assertRaisesRegex(RuntimeError, "captured network argv"):
            ops.Release.activate(self.release)
        self.assertEqual(self.seen[0], ["docker", "start", ops.APPS[0] + "-id"])
        self.assertEqual(self.seen[1], ["docker", "start", ops.APPS[3] + "-id"])
        argv = self.seen[2]
        self.assertEqual(argv, [
            "bash", str(self.rollback),
            "reconnect",
            "--env-file", "/root/IT-shop/private/next.env",
            "--compose", str(self.release.compose_file),
        ])
        ops.refuse_compose_down(argv)
        self.assertEqual(argv[argv.index("--env-file") + 1], ops.ENV_FILE)
        self.assertNotIn("down", argv)


class ShopRecoverTests(unittest.TestCase):
    def test_failed_recover_can_be_retried_until_pass(self):
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        release = object.__new__(ops.Release)
        release.root = Path(folder.name)
        release.raw = b"{}"
        release.m = {"intermediate_head": "0028_cycle_sell_held"}
        release.schema = Mock(return_value="0028_cycle_sell_held")
        self.assertTrue(ops.Release.reuse_intent(release, "recover-freeze") is False)
        self.assertTrue((release.root / "recover-freeze.intent.json").is_file())
        self.assertTrue(ops.Release.reuse_intent(release, "recover-freeze"))
        (release.root / "recover-freeze.json").write_text("{}\n")
        with self.assertRaisesRegex(RuntimeError, "already completed"):
            ops.Release.reuse_intent(release, "recover-freeze")

    def test_recover_waits_for_backend_and_frontend_health(self):
        import tempfile
        from unittest.mock import patch
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        release = object.__new__(ops.Release)
        release.root = Path(folder.name)
        (release.root / "freeze.intent.json").write_text("{}\n")
        release.m = {"old": {name: {"id": name + "-id", "image": "sha256:" + "a" * 64} for name in ops.MANAGED}}
        release.mark = Mock(return_value={"entry": {"ok": True}})
        release.schema = Mock(return_value=ops.OLD_HEAD)
        release.posted_cycle_rows = Mock(return_value=0)
        release.entry = Mock(return_value={"ok": True})
        state = {"probes": 0}

        def inspect(cid):
            name = cid[: -len("-id")] if cid.endswith("-id") else cid
            status = "healthy" if state["probes"] else "starting"
            return {"Id": cid if cid.endswith("-id") else name + "-id", "Name": "/" + name,
                    "Image": "sha256:" + "a" * 64,
                    "State": {"Running": True, "Health": {"Status": status}, "StartedAt": "t", "OOMKilled": False},
                    "RestartCount": 0}

        release.inspect = inspect
        release.run = Mock(return_value=b"")
        release.http = Mock(return_value={"status": 200, "sha256": "ab"})

        def sleep(_seconds):
            state["probes"] += 1

        with patch.object(ops.time, "sleep", sleep):
            result = ops.Release.recover_freeze(release)
        self.assertEqual(state["probes"], 1)
        self.assertEqual(set(result["checks"]), {"/api/ready", "/healthz"})
        release.http.assert_any_call("/api/ready")
        release.http.assert_any_call("/healthz")


if __name__ == "__main__":
    unittest.main()
