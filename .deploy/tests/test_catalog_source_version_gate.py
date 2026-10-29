"""D-0049 pure metadata tests plus opt-in owned PostgreSQL15 SQL drill.

Execute only on jinchen-dev. The SQL drill creates synthetic network-none data,
never reads another container and always cleans up by exact ID plus owner label.
"""
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

SOURCE = Path(__file__).resolve().parents[1] / "catalog_source_version_gate.py"
SPEC = importlib.util.spec_from_file_location("catalog_source_version_gate", SOURCE)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

T0 = "2026-10-29T20:00:00.000001+00:00"
T1 = "2026-10-29T20:00:00.000002+00:00"
T2 = "2026-10-29T20:00:00.000003+00:00"
PN1 = "00000000-0000-4000-8000-000000000001"
PN2 = "00000000-0000-4000-8000-000000000002"
PN3 = "00000000-0000-4000-8000-000000000003"


def snapshot(pn_id=PN1, part_id=1, version=T1, master=T0, *, exact=True):
    return {"kind": "snapshot", "pn_id": pn_id, "part_id": part_id,
            "upstream_updated_at": version, "pn_master_version": master,
            "exact_identity": exact, "snapshot_type": "object", "status_type": "string"}


def fixture():
    source = [{"id": 1, "version": T1}, {"id": 2, "version": T1}]
    mirror = [{"kind": "link", "part_id": 1, "pn_id": PN1, "version": T1},
              {"kind": "link", "part_id": 2, "pn_id": None, "version": T1},
              snapshot(), snapshot(PN2, None, None, None, exact=False)]
    return source, mirror


class MetadataGateTests(unittest.TestCase):
    def setUp(self):
        self.source, self.mirror = fixture()

    def reject(self, reason):
        with self.assertRaisesRegex(RuntimeError, reason):
            gate.validate_versions(self.source, self.mirror)

    def test_python310_syntax_and_no_execution_or_business_projection(self):
        ast.parse(SOURCE.read_text(), feature_version=(3, 10))
        for query in (gate.SOURCE_SQL, gate.MIRROR_SQL):
            self.assertTrue(query.startswith("SELECT "))
            for token in ("pn_display", "pn_std", "description", "price", "SELECT *", "UPDATE "):
                self.assertNotIn(token, query)
        tree = ast.parse(SOURCE.read_text())
        self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                             and node.func.id in {"exec", "eval", "open", "compile"} for node in ast.walk(tree)))

    def test_valid_snapshot_link_null_pn_missing_master_and_digest_order(self):
        self.mirror[2]["pn_master_version"] = None
        first = gate.validate_versions(self.source, self.mirror)
        second = gate.validate_versions(list(reversed(self.source)), list(reversed(self.mirror)))
        self.assertEqual(first, second)
        self.assertEqual(first["counts"]["snapshot_unmapped_identity"], 1)
        self.assertEqual(first["counts"]["link_unbound_pn"], 1)
        self.assertEqual(first["counts"]["pn.itsp_snapshot.pn_master_version.missing"], 1)
        self.assertEqual(first["ahead_version_fields"], 0)
        self.assertNotIn(PN1, json.dumps(first))
        self.assertNotIn(T1, json.dumps(first))

    def test_nonexact_unmapped_snapshot_keeps_parseable_versions_ignored(self):
        self.mirror[3].update(upstream_updated_at=T2, pn_master_version=T2)
        result = gate.validate_versions(self.source, self.mirror)
        self.assertEqual(result["counts"]["snapshot_unmapped_versions_ignored"], 2)
        # Its link is still checked independently, even with no mapped snapshot.
        self.mirror[1].update(pn_id=PN2, version=T2)
        self.reject("mirror version ahead")

    def test_unmapped_snapshot_nonnull_unparseable_version_is_rejected(self):
        self.mirror[3]["pn_master_version"] = "not-a-version"
        self.reject("non-null version unparseable")

    def test_missing_or_json_null_snapshot_is_allowed_but_malformed_shape_is_not(self):
        for kind in (None, "null"):
            self.mirror[3].update(snapshot_type=kind, status_type=None)
            gate.validate_versions(self.source, self.mirror)
        for kind in ("array", "string", "number", "boolean"):
            self.mirror[3]["snapshot_type"] = kind
            self.reject("snapshot JSON type invalid")
        self.mirror[3].update(snapshot_type=None, status_type=None, upstream_updated_at=T1)
        self.reject("missing snapshot has inconsistent")

    def test_source_duplicates_invalid_ids_and_nonparseable_versions_fail_closed(self):
        original = copy.deepcopy(self.source)
        self.source.append(copy.deepcopy(self.source[0]))
        self.reject("duplicate source")
        for value in (None, True, 0, -1, "1", 1.0):
            self.source = copy.deepcopy(original)
            self.source[0]["id"] = value
            self.reject("source identity invalid")
        for value in (None, "", "bad", "2026-10-29T20:00:00", 1, False):
            self.source = copy.deepcopy(original)
            self.source[0]["version"] = value
            self.reject("source .*version")

    def test_empty_truncated_or_changed_projection_fail_closed(self):
        with patch.object(gate, "MAX_ROWS", 1):
            self.reject("row bound")
        for value in ([], {}, None):
            with self.assertRaises(RuntimeError):
                gate.validate_versions(value, self.mirror)
        self.source[0]["unreviewed_column"] = "no"
        self.reject("source projection changed")

    def test_link_missing_source_duplicate_part_conflicting_pn_or_invalid_uuid(self):
        original = copy.deepcopy(self.mirror)
        self.mirror[0]["part_id"] = 3
        self.reject("link source identity absent")
        self.mirror = copy.deepcopy(original)
        self.mirror.append(copy.deepcopy(self.mirror[0]))
        self.reject("duplicate link source identity")
        self.mirror = copy.deepcopy(original)
        self.mirror[1]["pn_id"] = PN1
        self.reject("conflicting link PN identity")
        for value in ("bad", 3, "00000000000040008000000000000001"):
            self.mirror = copy.deepcopy(original)
            self.mirror[0]["pn_id"] = value
            self.reject("PN identity")

    def test_link_and_snapshot_same_pn_different_stable_identity_rejected(self):
        self.mirror[2]["part_id"] = 2
        self.reject("link and snapshot identities disagree")

    def test_linked_pn_missing_from_snapshot_inventory_rejected(self):
        self.mirror[2]["pn_id"] = PN3
        self.reject("linked PN identity absent")

    def test_snapshot_duplicate_pn_unknown_source_and_invalid_nonnull_id_rejected(self):
        original = copy.deepcopy(self.mirror)
        self.mirror.append(copy.deepcopy(self.mirror[2]))
        self.reject("duplicate snapshot PN")
        self.mirror = copy.deepcopy(original)
        self.mirror[2]["part_id"] = 3
        self.reject("snapshot source identity absent")
        for value in ("1", True, 0, -1, 1.0):
            self.mirror = copy.deepcopy(original)
            self.mirror[2]["part_id"] = value
            self.reject("snapshot identity invalid")

    def test_exact_snapshot_requires_id_string_status_and_boolean_flag(self):
        self.mirror[2]["part_id"] = None
        self.reject("exact snapshot lacks stable identity")
        self.mirror[2]["part_id"] = 1
        self.mirror[2]["status_type"] = None
        self.reject("exact snapshot status invalid")
        self.mirror[2]["status_type"] = "string"
        self.mirror[2]["exact_identity"] = 1
        self.reject("snapshot exact flag invalid")

    def test_link_and_both_snapshot_versions_individually_reject_regression(self):
        original = copy.deepcopy(self.mirror)
        for index, key in ((0, "version"), (2, "upstream_updated_at"), (2, "pn_master_version")):
            self.mirror = copy.deepcopy(original)
            self.mirror[index][key] = T2
            self.reject("mirror version ahead")
        for index, key in ((0, "version"), (2, "upstream_updated_at"), (2, "pn_master_version")):
            for value in ("bad", "", 7, [], {}, "2026-10-29T20:00:00"):
                self.mirror = copy.deepcopy(original)
                self.mirror[index][key] = value
                self.reject("non-null version unparseable")

    def test_guard_semantics_same_id_and_known_version_only(self):
        incoming = gate.parse_source_version(T1)
        self.assertFalse(gate.older_than_mirror(1, incoming, snapshots=[snapshot(part_id=2, version=T2)]))
        self.assertFalse(gate.older_than_mirror(1, incoming, snapshots=[snapshot(version=T1, master=None)]))
        self.assertTrue(gate.older_than_mirror(1, incoming, snapshots=[snapshot(version=T2)]))
        self.assertTrue(gate.older_than_mirror(1, None, snapshots=[snapshot(version=T1)]))
        self.assertFalse(gate.older_than_mirror(1, None, snapshots=[{"part_id": 1}]))

    def test_collect_queries_and_freeze_order_and_no_raw_data_in_receipt(self):
        events = []
        def source_sql(query):
            self.assertEqual(query, gate.SOURCE_SQL)
            events.append("source")
            return copy.deepcopy(self.source)
        def mirror_sql(query):
            self.assertEqual(query, gate.MIRROR_SQL)
            events.append("mirror")
            return copy.deepcopy(self.mirror)
        result = gate.collect(source_sql, mirror_sql,
                              assert_frozen=lambda: events.append("frozen"), pause_seconds=0)
        self.assertEqual(events, ["frozen", "source", "mirror", "source", "frozen",
                                  "source", "mirror", "source", "frozen"])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["rounds"][0]["source_sha256"], result["rounds"][1]["source_sha256"])
        self.assertNotIn(PN1, json.dumps(result))
        self.assertNotIn(T1, json.dumps(result))

    def test_collect_rejects_within_and_between_round_source_drift(self):
        changed = copy.deepcopy(self.source)
        changed[0]["version"] = T2
        for series, reason in (([self.source, changed], "within collection"),
                               ([self.source, self.source, changed, changed], "between collection")):
            with self.subTest(reason=reason), self.assertRaisesRegex(RuntimeError, reason):
                gate.collect(Mock(side_effect=series), Mock(return_value=self.mirror),
                             assert_frozen=Mock(), pause_seconds=0)

    def test_collect_rejects_mirror_drift_and_freeze_callback_failure(self):
        changed = copy.deepcopy(self.mirror)
        changed[2]["pn_master_version"] = None
        with self.assertRaisesRegex(RuntimeError, "between collection"):
            gate.collect(Mock(return_value=self.source), Mock(side_effect=[self.mirror, changed]),
                         assert_frozen=Mock(), pause_seconds=0)
        for fail_at in (0, 1, 2):
            failures = [None] * fail_at + [RuntimeError("synthetic freeze changed")]
            with self.subTest(fail_at=fail_at), self.assertRaisesRegex(RuntimeError, "freeze changed"):
                gate.collect(Mock(return_value=self.source), Mock(return_value=self.mirror),
                             assert_frozen=Mock(side_effect=failures), pause_seconds=0)

    def test_callback_failure_never_yields_partial_pass(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic read failure"):
            gate.collect(Mock(side_effect=RuntimeError("synthetic read failure")), Mock(),
                         assert_frozen=Mock(), pause_seconds=0)


@unittest.skipUnless(os.environ.get("D49_VERSION_GATE_SQL_DRILL") == "1", "explicit isolated jinchen-dev SQL drill only")
class PostgreSQLMetadataGateTests(unittest.TestCase):
    @classmethod
    def command(cls, argv, input=None):
        return subprocess.run(argv, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=90, check=True).stdout

    @classmethod
    def setUpClass(cls):
        if socket.gethostname() != "jinchen-dev":
            raise RuntimeError("synthetic SQL drill only runs on jinchen-dev")
        cls.owner = "d49-gate-" + uuid4().hex[:16]
        cls.cid = cls.command(["docker", "run", "-d", "--name", cls.owner,
            "--network", "none", "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
            "--label", "codex.d49-gate=" + cls.owner, "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "postgres:15"]).decode().strip()
        cls.addClassCleanup(cls.cleanup)
        row = json.loads(cls.command(["docker", "inspect", cls.cid]))[0]
        if row["HostConfig"]["NetworkMode"] != "none" or row["HostConfig"].get("PortBindings"):
            raise RuntimeError("synthetic container isolation differs")
        for _ in range(60):
            result = subprocess.run(["docker", "exec", cls.cid, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], capture_output=True)
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("synthetic PostgreSQL not ready")
        cls.sql("CREATE DATABASE source_test; CREATE DATABASE mirror_test;", database="postgres")
        cls.sql("CREATE TABLE dim_part(id bigint primary key, updated_at timestamptz NOT NULL);"
                "INSERT INTO dim_part VALUES(1, '" + T1 + "'),(2, '" + T1 + "');", database="source_test")
        cls.sql("CREATE TABLE pn(id uuid primary key, itsp_snapshot jsonb);"
                "CREATE TABLE catalog_part_link(source_namespace text,part_id bigint,pn_id uuid,upstream_updated_at timestamptz);",
                database="mirror_test")

    @classmethod
    def cleanup(cls):
        row = json.loads(cls.command(["docker", "inspect", cls.cid]))[0]
        if row["Id"] != cls.cid or row["Name"] != "/" + cls.owner or row["Config"]["Labels"].get("codex.d49-gate") != cls.owner:
            raise RuntimeError("synthetic cleanup ownership differs")
        cls.command(["docker", "rm", "-f", "-v", cls.cid])

    @classmethod
    def sql(cls, query, *, database, readonly=False):
        options = "-c timezone=UTC -c statement_timeout=30000 -c lock_timeout=3000"
        if readonly:
            options += " -c default_transaction_read_only=on"
        return cls.command(["docker", "exec", "-i", "-e", "PGOPTIONS=" + options,
                            cls.cid, "psql", "-U", "postgres", "-d", database, "-X", "-At",
                            "-v", "ON_ERROR_STOP=1"], input=query.encode()).decode().strip()

    @classmethod
    def rows(cls, query, database):
        raw = cls.sql(query, database=database, readonly=True)
        return [json.loads(line) for line in raw.splitlines() if line]

    def setUp(self):
        self.sql("TRUNCATE pn,catalog_part_link;", database="mirror_test")
        exact = json.dumps({"status": "exact", "part_id": 1, "upstream_updated_at": T1,
                            "pn_master_version": None, "description": "synthetic body must not leave SQL projection"})
        unmapped = json.dumps({"status": "not_found", "upstream_updated_at": T2})
        self.sql("INSERT INTO pn VALUES('" + PN1 + "','" + exact + "'),('" + PN2 + "','" + unmapped + "'),('" + PN3 + "',NULL);"
                 "INSERT INTO catalog_part_link VALUES('itsp',1,'" + PN1 + "','" + T1 + "'),('itsp',2,NULL,'" + T1 + "'),"
                 "('other',3,NULL,'" + T2 + "');", database="mirror_test")

    def frozen(self):
        for database in ("source_test", "mirror_test"):
            self.assertEqual(self.sql("SELECT current_setting('transaction_read_only');", database=database, readonly=True), "on")
            self.assertEqual(self.sql("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND client_addr IS NOT NULL;",
                                      database=database, readonly=True), "0")

    def test_actual_pg15_queries_types_missing_identity_and_readonly(self):
        result = gate.collect(lambda query: self.rows(query, "source_test"),
                              lambda query: self.rows(query, "mirror_test"),
                              assert_frozen=self.frozen, pause_seconds=0)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["rounds"][0]["counts"]["snapshot_unmapped_identity"], 2)
        self.assertEqual(result["rounds"][0]["counts"]["snapshot_unmapped_versions_ignored"], 1)
        raw = json.dumps(self.rows(gate.MIRROR_SQL, "mirror_test"))
        self.assertNotIn("synthetic body", raw)
        self.assertNotIn("description", raw)
        self.assertEqual(len(self.rows(gate.MIRROR_SQL, "mirror_test")), 5)

    def test_actual_pg15_ahead_and_nonnull_malformed_snapshot_fail_closed(self):
        for value in (T2, "invalid"):
            self.sql("UPDATE pn SET itsp_snapshot=jsonb_set(itsp_snapshot,'{pn_master_version}',to_jsonb('" + value + "'::text))"
                     " WHERE id='" + PN1 + "';", database="mirror_test")
            with self.assertRaisesRegex(RuntimeError, "ahead|unparseable"):
                gate.collect(lambda query: self.rows(query, "source_test"),
                             lambda query: self.rows(query, "mirror_test"),
                             assert_frozen=self.frozen, pause_seconds=0)


if __name__ == "__main__":
    if socket.gethostname() != "jinchen-dev":
        raise RuntimeError("tests may run only on jinchen-dev")
    unittest.main()
