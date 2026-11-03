"""Pool release gates; all container and database operations below are isolated fakes."""
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch
import importlib.util
import json
import tempfile

DEPLOY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("presales_pool_ops", DEPLOY / "presales_pool_ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def base():
    return dict(release="a" * 40, old_revision=ops.OLD_SHA, old_schema=ops.OLD_HEAD, target_head=ops.TARGET,
        old_app=ops.OLD_PREFIX + "-app", old_front=ops.OLD_PREFIX + "-frontend",
        old_app_id="a"*64, old_front_id="b"*64, old_app_image="sha256:"+"1"*64,
        old_front_image="sha256:"+"2"*64, app_image="sha256:"+"3"*64, frontend_image="sha256:"+"4"*64,
        source_sha256="a"*64, controller_sha256="a"*64, smoke_sha256="a"*64, nginx_sha256="a"*64,
        caddyfile="/opt/it-spareparts/Caddyfile", port=18098, nginx_upstream_count=3, additional_writers=[])


class PoolReleaseTests(TestCase):
    def test_release_scope(self):
        m = base()
        ops.validate(m)
        for key, bad in (("old_revision","9"*40), ("old_schema",ops.TARGET), ("target_head","c7e1a3b5d9f2"),
                         ("old_front","other-site"), ("additional_writers",["shop"]), ("catalog_sync",{}),
                         ("source_handoff",{}), ("app_image","latest"), ("release",ops.OLD_SHA)):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                ops.validate({**m, key: bad})

    def test_migration_requires_real_restored_backup(self):
        r = MagicMock()
        r.marker.return_value = {"restore":{"actual_restore_performed":False}}
        with self.assertRaises(RuntimeError):
            ops.Release.migrate(r)
        r.alembic.assert_not_called()

    def test_migration_requires_single_known_head(self):
        r = MagicMock()
        r.marker.return_value = {"restore":{"actual_restore_performed":True, "schema":ops.OLD_HEAD}}
        r.schema.return_value = ops.OLD_HEAD
        r.alembic.return_value = "different (head)"
        with self.assertRaises(RuntimeError):
            ops.Release.migrate(r)
        r.alembic.assert_called_once_with(["heads"])

    def test_migration_checks_cancelled_tasks(self):
        r = MagicMock()
        old = {"open_tasks":3,"cancelled_marked":0,"draft_offers":7}
        r.marker.side_effect = lambda phase: {"backup-old":{"restore":{"actual_restore_performed":True,
            "schema":ops.OLD_HEAD}}, "freeze":{"counts":{},"impact":old}}[phase]
        r.schema.side_effect = [ops.OLD_HEAD, ops.TARGET]
        r.alembic.return_value = ops.TARGET + " (head)"
        r.counts.return_value = {}
        r.impact.return_value = {**old,"open_tasks":0,"cancelled_marked":2}
        with self.assertRaisesRegex(RuntimeError, "impact"):
            ops.Release.migrate(r)
        self.assertEqual([c.args[0] for c in r.alembic.call_args_list], [["heads"], ["upgrade","head"], ["check"]])

    def test_cutover_rejects_schema_or_unreviewed_writer(self):
        for writer_error in (False,True):
            with self.subTest(writer_error=writer_error):
                r = MagicMock()
                r.schema.return_value = ops.OLD_HEAD
                if writer_error:
                    r.writers_exact.side_effect=RuntimeError("unexpected writer")
                with self.assertRaises(RuntimeError), patch.object(ops.hf.Hotfix, "cutover") as cut:
                    ops.Release.cutover(r)
                cut.assert_not_called()

    def test_rollback_does_not_start_old_app_if_downgrade_fails(self):
        r = MagicMock()
        r.freeze_for_rollback.return_value = ({}, {"Id":"orig","State":{"Running":False}}, b"", {}, "18095")
        r.schema.return_value = ops.TARGET
        r.downgrade.side_effect = RuntimeError("not safe")
        with self.assertRaises(RuntimeError):
            ops.Release.rollback(r)
        r.run.assert_not_called()
        r.load_active.assert_not_called()

    def test_rollback_reopens_on_actual_old_port_without_loading_older_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            r=MagicMock()
            r.root=Path(folder)
            (r.root/"Caddyfile.baseline").write_bytes(b"old")
            r.freeze_for_rollback.return_value=({"active":{"old":1},"inode":[1,2]},
                {"Id":"orig","State":{"Running":False}}, b"new", {"new":1}, "19877")
            r.schema.side_effect=[ops.TARGET,ops.OLD_HEAD]
            result=ops.Release.rollback(r)
            r.downgrade.assert_called_once()
            r.run.assert_called_once_with(["docker","start","orig"])
            self.assertIn("http://127.0.0.1:19877/health/db", [c.args[0] for c in r.wait_http.call_args_list])
            self.assertFalse(result["restored_dump_into_production"])
            r.restore.assert_not_called()

    def test_old_schema_recovery_does_not_downgrade_twice(self):
        with tempfile.TemporaryDirectory() as folder:
            r=MagicMock()
            r.root=Path(folder)
            (r.root/"Caddyfile.baseline").write_bytes(b"before")
            r.freeze_for_rollback.return_value=({"active":{},"inode":[1,2]},
                {"Id":"orig","State":{"Running":True}}, b"before", {}, "18095")
            r.schema.return_value=ops.OLD_HEAD
            ops.Release.rollback(r)
            r.downgrade.assert_not_called()
            r.run.assert_not_called()
            r.write_caddy.assert_not_called()
            r.load_active.assert_not_called()

    def test_writers_exact_requires_matching_names(self):
        r=MagicMock()
        r.old_app="original"
        r.database_target.return_value=("db",5432,"/spareparts")
        r.run.side_effect=[b"id\n", json.dumps([{"Name":"/unexpected"}]).encode()]
        with self.assertRaisesRegex(RuntimeError, "writer"):
            ops.Release.writers_exact(r, {"original"})

    def test_cannot_restart_after_manual_or_unverified_downgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            r = MagicMock()
            r.root = Path(folder)
            (r.root / "migrate.json").touch()
            with self.assertRaisesRegex(RuntimeError, "downgrade verification"):
                ops.Release.verify_downgrade(r)

    def test_downgrade_guard_refuses_dropped_or_modified_quotes(self):
        r = MagicMock()
        guard = {"counts": {"presales_offer": 2}, "columns": {}, "fingerprints": {"offers": "original"}}
        r.counts.return_value = guard["counts"]
        r.data_fingerprints.return_value = {"offers": "changed"}
        with self.assertRaisesRegex(RuntimeError, "business records"):
            ops.Release.check_business(r, guard)

    def test_phase_refuses_replay_and_owner_change(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(ops.h,"BASE",Path(folder)), patch.object(ops,"file_mode",return_value=True):
            r=MagicMock()
            r.root=Path(folder)/"deploy"
            r.root.mkdir()
            r.sha="a"*40
            r.manifest_bytes=b"A"
            r.preflight.return_value={}
            ops.Release.execute(r,"preflight")
            with self.assertRaises(FileExistsError):
                ops.Release.execute(r,"preflight")
            r.manifest_bytes=b"B"
            with self.assertRaisesRegex(RuntimeError,"owner"):
                ops.Release.execute(r,"images")
            r.images.assert_not_called()


if __name__ == "__main__":
    import unittest
    unittest.main()
