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
        caddyfile="/opt/it-spareparts/Caddyfile", port=18098, nginx_upstream_count=3, additional_writers=[],
        db_id="a"*64, db_image="sha256:"+"5"*64,
        proxy_identities={n: {"id":"b"*64,"image":"sha256:"+"6"*64,"process":n} for n in ("caddy","nginx","cloudflared")})


class PoolReleaseTests(TestCase):
    def test_freeze_scope_uses_only_the_literal_old_schema_query(self):
        query = ops.review_query((DEPLOY.parent / ops.REVIEW_MIGRATION).read_bytes())
        self.assertNotIn("answer_requires_review", query)
        for source in ('REVIEW_LINE_IDS = "SELECT 1; DELETE FROM work_item"', 'OTHER = "SELECT 1"'):
            with self.assertRaises(RuntimeError):
                ops.review_query(source)

    def test_migration_rejects_wrong_row_set_or_current_answer_on_quarantined_source(self):
        r = MagicMock()
        expected = {"count": 2, "ids_md5": "expected-set"}
        r.review_scope.return_value = {"count": 2, "ids_md5": "different-set"}
        with self.assertRaisesRegex(RuntimeError, "scope differs"):
            ops.Release.verify_review_projection(r, expected)
        r.require_valid_review_answers.assert_not_called()
        r.review_scope.return_value = expected
        self.assertEqual(ops.Release.verify_review_projection(r, expected), expected)
        r.require_valid_review_answers.assert_called_once()
        r.sql.return_value = "1"
        with self.assertRaisesRegex(RuntimeError, "still current"):
            ops.Release.require_valid_review_answers(r)
        r.sql.return_value = "0"
        ops.Release.require_valid_review_answers(r)

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
        r.m=base()
        r.inspect.return_value={"Id":r.m["db_id"],"Image":r.m["db_image"]}
        r.run.side_effect=[b"id\n", json.dumps([{"Id":"third","Name":"/unexpected","Config":{
            "Env":["DATABASE_URL=postgresql://it-spareparts-db-1/spareparts"]}}]).encode()]
        with self.assertRaisesRegex(RuntimeError, "writer"):
            ops.Release.writers_exact(r, {"original"})

    def test_idle_writer_declared_by_any_alias_ip_or_forwarded_port_is_never_ignored(self):
        for url in ("postgresql://db/spareparts", "postgresql+psycopg://it-spareparts-db-1/spareparts",
                    "postgresql://shareddb/spareparts", "postgresql://10.0.0.3/spareparts",
                    "postgresql://127.0.0.1:15432/%73pareparts", "postgresql://host/other?dbname=spareparts",
                    "postgresql://?service=unknown", "service=unknown"):
            with self.subTest(url=url):
                self.assertIs(ops.db_declaration({"Config":{"Env":["DATABASE_URL="+url]}}), True)

    def test_absent_dsn_on_database_bridge_or_reachable_published_port_requires_pinned_proxy(self):
        db={"NetworkSettings":{"Networks":{"production":{}},"Ports":{"5432/tcp":[{"HostIp":"127.0.0.1"}]}}}
        other={"Id":"container","Name":"/other","HostConfig":{"NetworkMode":"private"},
               "NetworkSettings":{"Networks":{"private":{}}}}
        self.assertFalse(ops.can_reach_without_dsn(other, db, [other]))
        other["HostConfig"]["NetworkMode"]="host"
        self.assertTrue(ops.can_reach_without_dsn(other, db, [other]))
        shared={"Id":"shared","Name":"/shared","HostConfig":{"NetworkMode":"container:container"}}
        self.assertTrue(ops.can_reach_without_dsn(shared, db, [other,shared]))
        other["HostConfig"]["NetworkMode"]="private"
        db["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostIp"]="0.0.0.0"
        self.assertTrue(ops.can_reach_without_dsn(other, db, [other]))
        db["NetworkSettings"]["Ports"]={}
        other["NetworkSettings"]["Networks"]["production"]={}
        self.assertTrue(ops.can_reach_without_dsn(other, db, [other]))

    def test_pinned_proxy_cannot_hide_a_database_dsn_or_changed_process(self):
        row={"Name":"/nginx","Id":"b"*64,"Image":"sha256:"+"6"*64,"Path":"/docker-entrypoint.sh","Args":["nginx"],
             "Config":{"Env":["PGDATABASE=spareparts"]}}
        self.assertTrue(ops.nonwriter_proxy(row,base()["proxy_identities"]))
        self.assertIs(ops.db_declaration(row),True)
        row["Args"]=["python"]
        self.assertFalse(ops.nonwriter_proxy(row,base()["proxy_identities"]))

    def test_repeated_recover_after_two_transient_errors_creates_independent_receipts(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(ops.h,"BASE",Path(folder)), patch.object(ops,"file_mode",return_value=True):
            r = MagicMock()
            r.root=Path(folder)/"deploy"
            r.root.mkdir()
            r.sha="a"*40
            r.manifest_bytes=b"identity"
            r.preflight.return_value={}
            ops.Release.execute(r,"preflight")
            r.rollback.side_effect=[RuntimeError("connection"),RuntimeError("second connection"),{"schema":ops.OLD_HEAD}]
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    ops.Release.execute(r,"recover")
            ops.Release.execute(r,"recover")
            self.assertEqual(len(list(r.root.glob("recover-*.intent.json"))),3)
            completed=[p for p in r.root.glob("recover-*.json") if not p.name.endswith(".intent.json")]
            self.assertEqual(len(completed),1)
            self.assertEqual(json.loads(completed[0].read_bytes())["schema"],ops.OLD_HEAD)

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

    def test_saved_guard_allows_retry_after_downgrade_without_rewriting_user_data(self):
        with tempfile.TemporaryDirectory() as folder:
            r = object.__new__(ops.Release)
            r.root, r.sha, r.manifest_bytes = Path(folder), "a"*40, b"frozen"
            expected={"release":r.sha,"manifest_sha256":ops.h.digest(r.manifest_bytes),"counts":{"offer":3},
                      "columns":{"offers":["price"]},"fingerprints":{"offers":"frozen"}}
            ops.h.save(r.root/"rollback-guard.json",expected)
            r.counts=MagicMock(return_value={"offer":3})
            r.data_fingerprints=MagicMock(side_effect=[{"offers":"changed"},{"offers":"frozen"}])
            r.impact=MagicMock(return_value={"cancelled_marked":0})
            with self.assertRaisesRegex(RuntimeError,"business records"):
                r.verify_downgrade()
            self.assertFalse((r.root/"rollback-schema-verified.json").exists())
            r.verify_downgrade()
            self.assertTrue((r.root/"rollback-schema-verified.json").is_file())
            # Routing may still require retry after the old process restarted and users wrote new data.
            r.counts.side_effect=AssertionError("do not compare new traffic against the old snapshot")
            r.verify_downgrade()

    def test_declared_database_server_and_client_in_same_process_are_not_exempt(self):
        row={"Path":"docker-entrypoint.sh","Args":["postgres"],"Config":{"Env":["POSTGRES_DB=spareparts"]}}
        self.assertIsNone(ops.db_declaration(row))
        row["Config"]["Env"].append("DATABASE_URL=postgresql://localhost:19999/spareparts")
        self.assertIs(ops.db_declaration(row),True)

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
