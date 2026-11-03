"""Synthetic release contracts, with no production access."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

DEPLOY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("requirements_ops", DEPLOY / "requirements_visibility_ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def manifest():
    result = dict(release="a" * 40, old_revision=ops.OLD_SHA, old_schema=ops.OLD_HEAD, target_head=ops.TARGET,
        old_app=ops.OLD_PREFIX + "-app", old_front=ops.OLD_PREFIX + "-frontend",
        caddyfile="/opt/it-spareparts/Caddyfile", port=18103, additional_writers=[], nginx_upstream_count=1)
    for key in ("old_app_id", "old_front_id", "source_sha256", "controller_sha256", "smoke_sha256", "nginx_sha256",
                "predecessor_owner_sha256", "evidence_sha256", "db_id"):
        result[key] = "b" * 64
    for key in ("old_app_image", "old_front_image", "app_image", "frontend_image", "db_image"):
        result[key] = "sha256:" + "c" * 64
    result["proxy_identities"] = {name: {"id": "d" * 64, "image": "sha256:" + "e" * 64, "process": name}
                                  for name in ("caddy", "cloudflared", "nginx")}
    return result


class ReleaseTests(unittest.TestCase):
    def test_current_production_and_merged_migration_only(self):
        ops.validate(manifest())
        for key, value in (("old_revision", "e" * 40), ("target_head", ops.OLD_HEAD),
                           ("old_schema", "a3c5e7f9b2d4"), ("old_app", "unrelated"),
                           ("additional_writers", [{"name": "other"}]), ("source_handoff", {})):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                ops.validate({**manifest(), key: value})

    def test_migration_requires_real_restore_and_checks_content_both_sides(self):
        release = object.__new__(ops.Release)
        release.marker = Mock(return_value={"restore": {"actual_restore_performed": False, "schema": ops.OLD_HEAD}})
        release.unchanged_business = Mock()
        with patch.object(ops.h.Release, "migrate") as migrate:
            with self.assertRaises(RuntimeError):
                release.migrate()
            migrate.assert_not_called()
            release.marker.return_value["restore"]["actual_restore_performed"] = True
            release.migrate()
            self.assertEqual(release.unchanged_business.call_count, 2)
            migrate.assert_called_once_with(release)

    def test_recovery_never_downgrades_or_restores_exposed_release(self):
        release = object.__new__(ops.Release)
        with tempfile.TemporaryDirectory() as directory:
            release.root = Path(directory)
            (release.root / "cutover.intent.json").write_text("{}")
            release.run = Mock()
            with self.assertRaisesRegex(RuntimeError, "forward repair"):
                release.rollback()
            release.run.assert_not_called()

    def test_missing_exact_gate_or_reference_fails_before_production_read(self):
        release = object.__new__(ops.Release)
        release.sha = "a" * 40
        evidence = {"release": release.sha, "release_tag_sha": release.sha, "old_revision": ops.OLD_SHA,
                    "target_head": ops.TARGET, "release_tag": "release-example", "references": {}}
        gates = ("required_ci", "independent_review", "candidate_regression", "scope_review", "old_app_on_expanded_schema")
        evidence.update({gate: "passed" for gate in gates})
        with tempfile.TemporaryDirectory() as directory:
            release.root = Path(directory)
            path = release.root / "release-evidence.json"
            path.write_text(json.dumps(evidence))
            release.m = {"evidence_sha256": ops.h.artifact(path)["sha256"]}
            with self.assertRaisesRegex(RuntimeError, "missing evidence: required_ci"):
                release.evidence()

    def test_verified_pool_caddy_algorithm_is_reused(self):
        self.assertIs(ops.Release.plan_caddy_change, ops.pool.Release.plan_caddy_change)
        self.assertIs(ops.Release.writers_exact, ops.pool.Release.writers_exact)
        self.assertIs(ops.Release.frozen, ops.pool.Release.frozen)


if __name__ == "__main__":
    unittest.main()
