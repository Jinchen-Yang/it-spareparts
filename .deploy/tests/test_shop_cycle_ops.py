"""Contracts for the shop cycle cutover. The 0026 -> 0027 controller stays put."""
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

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
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        dump = Path(folder.name) / "old.dump"
        dump.write_bytes(b"consistent snapshot")
        release._head = ops.OLD_HEAD
        release.schema = lambda container=ops.DB: release._head
        release.mark = Mock(side_effect=lambda name: {
            "backup-old": {"status": "PASS", "dump": ops.artifact(dump), "restore": {
                "actual_restore_performed": True, "head": ops.OLD_HEAD, "network": "none", "published_ports": [],
            }},
        }[name])
        release.protected = Mock()
        release.originals_running = Mock()
        release.online_review = Mock()
        release.migration_idle = Mock()
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
        release._head = ops.TARGET_HEAD
        seen.clear()
        ops.Release.migrate(release)
        self.assertEqual(seen, [["heads"], ["check"]])
        release.counts.assert_not_called()

    def test_private_environment_is_never_read_by_the_controller(self):
        text = (DEPLOY / "shop_cycle_ops.py").read_text(encoding="utf-8")
        self.assertIn('"/root/IT-shop/private/next.env"', text)
        self.assertNotIn("next.env.read", text)


class ShopCommandGuardTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.release = object.__new__(ops.Release)
        self.release.root = Path(self.folder.name)
        self.release.raw = b"{}"
        self.release.m = {"migration_service": "migration"}
        self.release.prefix = "shop-cycle-" + "a" * 12
        self.release.compose_file = self.release.root / "prod.compose.yml"

    def test_upgrade_head_reaches_subprocess_without_reading_private_env(self):
        argv = self.release.alembic_argv(["upgrade", "head"])
        result = Mock(returncode=0, stdout=b"upgraded", stderr=b"")
        with patch.object(ops.subprocess, "run", return_value=result) as run:
            self.assertEqual(self.release.run(argv), b"upgraded")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], argv)
        self.assertEqual(argv[-3:], ["alembic", "upgrade", "head"])

    def test_migration_dispatch_is_journaled_before_the_subprocess_starts(self):
        argv = self.release.alembic_argv(["upgrade", "head"])

        def dispatch(command, **kwargs):
            journals = list(self.release.root.glob("migration-dispatched-*.json"))
            self.assertEqual(len(journals), 1)
            journal = json.loads(journals[0].read_bytes())
            self.assertEqual(journal["manifest_sha256"], ops.sha(self.release.raw))
            raise ops.subprocess.TimeoutExpired(command, 1)

        with patch.object(ops.subprocess, "run", side_effect=dispatch):
            with self.assertRaises(ops.subprocess.TimeoutExpired):
                self.release.run(argv)
        self.assertEqual(len(list(self.release.root.glob("migration-dispatched-*.json"))), 1)

    def test_private_env_readers_are_rejected_before_subprocess(self):
        readers = [
            ["cat", ops.ENV_FILE],
            ["head", "-n", "1", ops.ENV_FILE],
            ["tail", ops.ENV_FILE],
            ["sed", "-n", "1p", ops.ENV_FILE],
            ["bash", "-c", "cat " + ops.ENV_FILE],
            ["python3", "-c", "print(open(" + repr(ops.ENV_FILE) + ").read())"],
        ]
        with patch.object(ops.subprocess, "run") as run:
            for argv in readers:
                with self.subTest(argv=argv), self.assertRaises(RuntimeError):
                    self.release.run(argv)
            run.assert_not_called()
        self.assertEqual(list(self.release.root.glob("migration-dispatched-*.json")), [])

    def test_compose_env_argument_cannot_be_turned_into_a_reader(self):
        argv = self.release.alembic_argv(["upgrade", "head"])
        prefix = argv[: argv.index("alembic")]
        malformed = [
            [*prefix, "cat", ops.ENV_FILE],
            [*prefix, "head", ops.ENV_FILE],
            [*prefix, "sh", "-c", "cat " + ops.ENV_FILE],
            [*argv, "--env-file", ops.ENV_FILE],
        ]
        with patch.object(ops.subprocess, "run") as run:
            for command in malformed:
                with self.subTest(command=command), self.assertRaises(RuntimeError):
                    self.release.run(command)
            run.assert_not_called()


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
        self.release.online_review = Mock()
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



class ShopOnlineReviewTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.release = object.__new__(ops.Release)
        self.release.root = Path(folder.name)
        self.release.m = {
            "strategy": "online-expand-v1", "release": "a" * 40,
            "source_sha256": "b" * 64, "app_image": "sha256:" + "c" * 64,
            "frontend_image": "sha256:" + "d" * 64,
            "old_head": ops.OLD_HEAD, "intermediate_head": "0028_cycle_sell_held", "target_head": ops.TARGET_HEAD,
        }
        self.review = {key: value for key, value in self.release.m.items() if key != "strategy"}
        self.review.update({"old_app_compatible": True, "cycle_writes_disabled": True})
        self.write_review()

    def write_review(self):
        path = self.release.root / "online-migration-review.json"
        path.write_text(json.dumps(self.review))
        self.release.m["online_migration_review_sha256"] = ops.artifact(path)["sha256"]

    def test_exact_candidate_and_both_explicit_compatibility_claims_are_required(self):
        self.assertEqual(self.release.online_review(), self.review)
        for key in ("release", "source_sha256", "app_image", "frontend_image", "old_head", "intermediate_head", "target_head"):
            with self.subTest(key=key):
                old = self.review[key]
                self.review[key] = "another-candidate"
                self.write_review()
                with self.assertRaisesRegex(RuntimeError, "bind the exact candidate"):
                    self.release.online_review()
                self.review[key] = old
        self.write_review()
        for key in ("old_app_compatible", "cycle_writes_disabled"):
            for value in (False, 1, "true", None):
                with self.subTest(key=key, value=value):
                    self.review[key] = value
                    self.write_review()
                    with self.assertRaisesRegex(RuntimeError, "bind the exact candidate"):
                        self.release.online_review()
            self.review[key] = True

    def test_changed_review_bytes_and_unreviewed_strategy_are_refused(self):
        path = self.release.root / "online-migration-review.json"
        path.write_text(path.read_text() + " ")
        with self.assertRaisesRegex(RuntimeError, "review changed"):
            self.release.online_review()
        self.write_review()
        self.release.m["strategy"] = "offline-freeze"
        with self.assertRaisesRegex(RuntimeError, "online expand strategy required"):
            self.release.online_review()

    def test_review_symlink_is_refused_even_when_content_hash_matches(self):
        path = self.release.root / "online-migration-review.json"
        target = self.release.root / "other-review.json"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "review changed"):
            self.release.online_review()


class ShopOnlineBackupTests(unittest.TestCase):
    def test_live_backup_uses_the_restored_snapshot_counts_without_stopping_old_services(self):
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        release = object.__new__(ops.Release)
        release.root = Path(folder.name)
        release.mark = Mock(return_value={})
        release.online_review = Mock()
        release.originals_running = Mock()
        release.protected = Mock()
        release.schema = Mock(return_value=ops.OLD_HEAD)
        release.inspect = Mock(return_value={"Config": {"Env": ["POSTGRES_USER=shop", "POSTGRES_DB=shop_next"]}})
        release.counts = Mock(side_effect=AssertionError("a live count is not the pg_dump snapshot"))
        release.restore = Mock(return_value={"actual_restore_performed": True, "head": ops.OLD_HEAD,
                                             "counts": {"catalog_part": 123}, "network": "none", "published_ports": []})

        def dump(argv, **kwargs):
            self.assertEqual(argv[:2], ["docker", "exec"])
            self.assertIn("pg_dump", argv)
            kwargs["stdout"].write(b"snapshot archive" * 200)
            return b""

        release.run = Mock(side_effect=dump)
        result = release.backup("old")
        self.assertIs(result["online"], True)
        self.assertEqual(result["snapshot_counts"], {"catalog_part": 123})
        release.restore.assert_called_once_with(release.root / "old.dump", ops.OLD_HEAD, None, "old")
        release.counts.assert_not_called()
        self.assertNotIn("freeze", [call.args[0] for call in release.mark.call_args_list])

    def test_legacy_freeze_entry_is_disabled_before_stopping_any_container(self):
        release = object.__new__(ops.Release)
        release.run = Mock()
        with self.assertRaisesRegex(RuntimeError, "freeze disabled"):
            release.freeze()
        release.run.assert_not_called()


class ShopMigrationIdleTests(unittest.TestCase):
    def setUp(self):
        self.release = object.__new__(ops.Release)
        self.release.prefix = "shop-cycle-" + "a" * 12
        self.release.run = Mock(return_value=b"")
        self.release.sql = Mock(return_value="0")

    def test_running_migration_container_blocks_retry(self):
        self.release.run.return_value = b"running-container-id\n"
        with self.assertRaisesRegex(RuntimeError, "container is still running"):
            self.release.migration_idle()
        self.release.sql.assert_not_called()

    def test_database_session_blocks_retry_even_when_container_is_missing(self):
        self.release.sql.return_value = "1"
        with self.assertRaisesRegex(RuntimeError, "session is still running"):
            self.release.migration_idle()

    def test_no_migration_container_or_session_allows_retry(self):
        self.release.migration_idle()
        self.assertIn("shop_cycle_migration", self.release.sql.call_args.args[0])
        self.assertIn("SELECT count(*)", self.release.sql.call_args.args[0])


class ShopMigrationRetryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.release = object.__new__(ops.Release)
        self.release.root = Path(folder.name)
        self.release.raw = b'{"release":"this-release"}'
        self.release.m = {"intermediate_head": "0028_cycle_sell_held"}
        self.release.schema = Mock(return_value=ops.OLD_HEAD)
        self.release.migration_idle = Mock()

    def test_unlaunched_migration_at_0027_can_be_retried(self):
        self.assertFalse(self.release.reuse_intent("migrate"))
        intent = json.loads((self.release.root / "migrate.intent.json").read_bytes())
        self.assertIs(intent["dispatch_tracking"], True)
        self.assertEqual(intent["manifest_sha256"], ops.sha(self.release.raw))
        self.assertTrue(self.release.reuse_intent("migrate"))

    def test_0027_retry_is_refused_after_dispatch_even_if_schema_is_unchanged(self):
        self.release.reuse_intent("migrate")
        ops.save(self.release.root / "migration-dispatched-test.json", {"manifest_sha256": ops.sha(self.release.raw)})
        with self.assertRaises(RuntimeError):
            self.release.reuse_intent("migrate")

    def test_legacy_untracked_intent_cannot_prove_nonexecution(self):
        ops.save(self.release.root / "migrate.intent.json", {"phase": "migrate", "manifest_sha256": ops.sha(self.release.raw)})
        with self.assertRaises(RuntimeError):
            self.release.reuse_intent("migrate")

    def test_another_manifest_intent_cannot_authorize_0027_retry(self):
        ops.save(self.release.root / "migrate.intent.json", {
            "phase": "migrate", "manifest_sha256": "f" * 64, "dispatch_tracking": True,
        })
        with self.assertRaises(RuntimeError):
            self.release.reuse_intent("migrate")

    def test_intermediate_or_target_schema_can_continue_after_dispatch(self):
        self.release.reuse_intent("migrate")
        ops.save(self.release.root / "migration-dispatched-test.json", {"manifest_sha256": ops.sha(self.release.raw)})
        for head in (self.release.m["intermediate_head"], ops.TARGET_HEAD):
            with self.subTest(head=head):
                self.release.schema.return_value = head
                self.assertTrue(self.release.reuse_intent("migrate"))


class ShopExecuteGateTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.release = object.__new__(ops.Release)
        self.release.root = self.base / "release"
        self.release.root.mkdir()
        self.release.release = "a" * 40
        self.release.raw = b'{"release":"test"}'
        self.release.compose_file = self.release.root / "prod.compose.yml"
        self.release.compose_file.write_bytes(b"reviewed compose")
        source = self.release.root / "source.tar.gz"
        source.write_bytes(b"reviewed source")
        self.release.m = {
            "controller_sha256": ops.artifact(DEPLOY / "shop_cycle_ops.py")["sha256"],
            "source_sha256": ops.artifact(source)["sha256"],
            "compose_sha256": ops.artifact(self.release.compose_file)["sha256"],
        }
        ops.save(self.base / ".shop-cycle-active-owner.json", {
            "release": self.release.release, "manifest_sha256": ops.sha(self.release.raw),
        })
        self.base_patch = patch.object(ops, "BASE", self.base)
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)
        self.release.run = Mock()

    def test_rejected_recovery_does_not_prevent_a_later_forward_phase(self):
        self.release.mark = Mock(return_value={})
        with self.assertRaisesRegex(RuntimeError, "no legacy freeze"):
            self.release.execute("recover-freeze")
        self.assertFalse((self.release.root / "recover-freeze.intent.json").exists())
        self.release.run.assert_not_called()
        self.release.migrate = Mock(return_value={"schema": ops.TARGET_HEAD})
        with patch("builtins.print"):
            self.release.execute("migrate")
        self.release.migrate.assert_called_once()
        self.assertTrue((self.release.root / "migrate.json").exists())

    def test_changed_source_or_compose_is_refused_before_migration_intent(self):
        for filename in ("source.tar.gz", "prod.compose.yml"):
            with self.subTest(filename=filename):
                path = self.release.root / filename
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                self.release.mark = Mock()
                self.release.migrate = Mock()
                with self.assertRaisesRegex(RuntimeError, "release inputs changed"):
                    self.release.execute("migrate")
                self.assertFalse((self.release.root / "migrate.intent.json").exists())
                self.release.mark.assert_not_called()
                self.release.migrate.assert_not_called()
                self.release.run.assert_not_called()
                path.write_bytes(original)


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

    def completed_freeze(self):
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        release = object.__new__(ops.Release)
        release.root = Path(folder.name)
        release.raw = b"{}"
        release.m = {"old": {
            name: {"id": name + "-id", "image": "sha256:" + "a" * 64} for name in ops.MANAGED
        }}
        ops.save(release.root / "freeze.intent.json", {"phase": "freeze", "manifest_sha256": ops.sha(release.raw)})
        ops.save(release.root / "freeze.json", {"phase": "freeze", "status": "PASS", "manifest_sha256": ops.sha(release.raw)})
        release.reuse_intent("migrate")
        release.mark = Mock(return_value={"entry": {"ok": True}})
        release.schema = Mock(return_value=ops.OLD_HEAD)
        release.migration_idle = Mock()
        release.posted_cycle_rows = Mock(return_value=0)
        release.entry = Mock(return_value={"ok": True})
        running = set()

        def inspect(cid):
            name = cid.removesuffix("-id")
            return {"Id": cid, "Name": "/" + name, "Image": release.m["old"][name]["image"],
                    "State": {"Running": cid in running, "Health": {"Status": "healthy"}}}

        def run(argv, **kwargs):
            self.assertEqual(argv[:2], ["docker", "start"])
            running.add(argv[2])
            return b""

        release.inspect = Mock(side_effect=inspect)
        release.run = Mock(side_effect=run)
        release.http = Mock(return_value={"status": 200, "sha256": "ab"})
        return release

    def test_completed_freeze_and_unlaunched_migrate_can_restore_all_five_originals(self):
        release = self.completed_freeze()
        result = release.recover_freeze()
        self.assertTrue(result["restored_original_processes"])
        self.assertFalse(result["database_restored"])
        self.assertFalse(result["downgrade_invoked"])
        self.assertEqual({call.args[0][2] for call in release.run.call_args_list}, {
            row["id"] for row in release.m["old"].values()
        })
        self.assertEqual(set(result["checks"]), {"/api/ready", "/healthz"})

    def test_recovery_refuses_a_dispatched_migration_or_exposure_before_starting_anything(self):
        for blocker in ("migration-dispatched-test.json", "create.intent.json", "exposure-started.json"):
            with self.subTest(blocker=blocker):
                release = self.completed_freeze()
                ops.save(release.root / blocker, {"manifest_sha256": ops.sha(release.raw)})
                with self.assertRaises(RuntimeError):
                    release.recover_freeze()
                release.run.assert_not_called()

    def test_recovery_refuses_legacy_untracked_migrate_intent(self):
        release = self.completed_freeze()
        (release.root / "migrate.intent.json").write_text(json.dumps({
            "phase": "migrate", "manifest_sha256": ops.sha(release.raw),
        }))
        with self.assertRaises(RuntimeError):
            release.recover_freeze()
        release.run.assert_not_called()

    def test_recover_waits_for_backend_and_frontend_health(self):
        import tempfile
        from unittest.mock import patch
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        release = object.__new__(ops.Release)
        release.root = Path(folder.name)
        release.raw = b"{}"
        (release.root / "freeze.intent.json").write_text("{}\n")
        release.m = {"old": {name: {"id": name + "-id", "image": "sha256:" + "a" * 64} for name in ops.MANAGED}}
        release.mark = Mock(return_value={"entry": {"ok": True}})
        release.schema = Mock(return_value=ops.OLD_HEAD)
        release.migration_idle = Mock()
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
