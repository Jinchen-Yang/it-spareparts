#!/usr/bin/env python3
"""Static, non-production self-test for the v1.21 Beta release controls."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / ".deploy" / "v121_beta_manifest.py"
RELEASE_PATH = ROOT / ".deploy" / "v121_beta_release.sh"
BUILD_PATH = ROOT / ".deploy" / "v121_beta_build.sh"
REHEARSE_PATH = ROOT / ".deploy" / "v121_beta_rehearse.sh"


def run(*args: str) -> None:
    subprocess.run(args, cwd=ROOT, check=True)


def load_manifest_module():
    spec = importlib.util.spec_from_file_location("v121_beta_manifest", MANIFEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def permissions(*, maintenance: bool, replenishment: bool) -> dict:
    module = load_manifest_module()
    maintenance_graph = {
        "page_maintenance": maintenance,
        "page_maintenance_beta": maintenance,
        **{key: False for key in module.MAINTENANCE_ACTIONS},
    }
    replenishment_graph = {
        "page_replenishment_beta": replenishment,
        "data_pool_price_governance": replenishment,
        "action_replenishment_create": False,
        "action_replenishment_review": False,
    }
    return {"maintenance": maintenance_graph, "replenishment": replenishment_graph}


def main() -> None:
    run("bash", "-n", str(RELEASE_PATH))
    run("bash", "-n", str(BUILD_PATH))
    run("bash", "-n", str(REHEARSE_PATH))
    if shutil.which("shellcheck"):
        run("shellcheck", "-x", str(RELEASE_PATH))
        run("shellcheck", "-x", str(BUILD_PATH))
        run("shellcheck", "-x", str(REHEARSE_PATH))
    release = RELEASE_PATH.read_text(encoding="utf-8")
    forbidden = (
        r"\balembic\s+downgrade\b",
        r"\bdocker\s+compose\s+down\b",
        r"\bcompose\s+down\b",
        r"\bcompose\s+up\b[^\n]*\bdb\b",
        r"\bdocker\s+volume\s+(rm|prune)\b",
    )
    for pattern in forbidden:
        assert re.search(pattern, release) is None, pattern
    up_lines = [line.strip() for line in release.splitlines() if re.search(r"\bcompose up\b", line)]
    assert up_lines
    assert all("--no-deps" in line and "--no-build" in line for line in up_lines)
    assert '"MAINTENANCE_CUTOVER_ENABLED": "false"' in release
    assert "statement_timeout=120000" in release and "lock_timeout=5000" in release
    assert "migration pressure sampling has a gap" in release
    assert "migration pressure sampler exited early" in release
    assert "--network none" in release and "isolated restore" in release
    assert all(point in release for point in ("0|5|15|30", "open-empty-beta", "pilot-smoke"))
    assert "OBSERVATION_GRACE_SECONDS=120" in release
    assert "pilot_app_restart_count" in release and "pilot_frontend_restart_count" in release
    assert 'if fields[1] == "admin"' in release and "permission snapshot" in release
    assert "emergency_stop_public_surface" in release and "fail_closed" in release
    assert "app/frontend stop could not be proven" in release
    assert "release package artifact has unsafe owner, mode or link count" in release
    preclose_call = release.index("    preclose_beta_surface\n")
    package_verify = release.index('python3 "$MANIFEST_TOOL" verify')
    assert preclose_call < package_verify
    assert "stopped before package verification" in release
    assert "pilot app/frontend restarted before the observation baseline" in release
    assert "completed outside its two-minute window" in release

    module = load_manifest_module()
    assert set(module.ACTION_CANARY_ROUTES) == {
        *module.MAINTENANCE_ACTIONS,
        "action_replenishment_create",
        "action_replenishment_review",
    }
    assert module.ACTION_CANARY_ROUTES["action_maintenance_roundtrip_apply"] == (
        "POST",
        "/api/maintenance/roundtrip-import",
    )
    assert callable(module._fetch_ci_evidence)
    assert module._ci_live_identity({"captured_at": "ignored", "checks": []}) == {
        "checks": []
    }
    head = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=ROOT, text=True).strip()
    inventory = module._migration_inventory(ROOT, head)
    assert inventory
    assert any(row["revision"] == module.DB_TO for row in inventory)
    assert all(row["revision"] != module.DB_FROM for row in inventory)

    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        safe = {
            "format": "v121-beta-allowlist-v1",
            "accounts": [
                {
                    "username": "named.pilot",
                    "role": "purchaser",
                    **permissions(maintenance=True, replenishment=True),
                }
            ],
            "canary_evidence": [],
        }
        path = folder / "allowlist.json"
        path.write_text(json.dumps(safe), encoding="utf-8")
        summary, evidence = module._parse_allowlist(
            path, repository="Example/it-spareparts", target=head
        )
        assert summary["account_count"] == 1 and not evidence

        admin = json.loads(json.dumps(safe))
        admin["accounts"][0]["role"] = "admin"
        path.write_text(json.dumps(admin), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError:
            pass
        else:
            raise AssertionError("Maintenance Beta admin pilot was accepted")

        canary = {
            "format": "v121-action-canary-v1",
            "source": "github-commit-comment-api",
            "repository": "Example/it-spareparts",
            "username": "named.pilot",
            "action": "action_maintenance_site_issue_manage",
            "target_sha": head,
            "environment": "isolated",
            "executor_id": "reviewer.one",
            "author_association": "COLLABORATOR",
            "comment_id": 122,
            "comment_url": f"https://github.com/Example/it-spareparts/commit/{head}#commitcomment-122",
            "body_sha256": "b" * 64,
            "completed_at": "2026-08-10T12:00:00+00:00",
            "request": {
                "method": "POST",
                "route_template": "/api/maintenance/site-issues/projects/{project_id}",
                "path": "/api/maintenance/site-issues/projects/project-1",
                "payload_sha256": "1" * 64,
            },
            "result": {
                "expected_status": 201,
                "observed_status": 201,
                "response_sha256": "2" * 64,
            },
            "conclusion": "passed",
        }
        canary_path = folder / "site-issue-canary.json"
        canary_path.write_text(json.dumps(canary), encoding="utf-8")
        canary_body = {
            key: canary[key]
            for key in (
                "format",
                "username",
                "action",
                "target_sha",
                "environment",
                "request",
                "result",
                "conclusion",
            )
        }
        captured_canary = module._canary_comment_evidence(
            {
                "body": json.dumps(canary_body),
                "user": {"login": "reviewer.one", "type": "User"},
                "author_association": "COLLABORATOR",
                "commit_id": head,
                "created_at": "2026-08-10T12:00:00Z",
                "updated_at": "2026-08-10T12:00:00Z",
                "id": 122,
                "html_url": f"https://github.com/Example/it-spareparts/commit/{head}#commitcomment-122",
            },
            repository="Example/it-spareparts",
            target=head,
        )
        assert captured_canary["executor_id"] == "reviewer.one"
        allowed_write = json.loads(json.dumps(safe))
        allowed_write["accounts"][0]["maintenance"][canary["action"]] = True
        allowed_write["canary_evidence"] = [
            {
                "username": canary["username"],
                "action": canary["action"],
                "target_sha": head,
                "conclusion": "passed",
                "path": canary_path.name,
                "sha256": hashlib.sha256(canary_path.read_bytes()).hexdigest(),
            }
        ]
        path.write_text(json.dumps(allowed_write), encoding="utf-8")
        summary, evidence = module._parse_allowlist(
            path, repository="Example/it-spareparts", target=head
        )
        assert summary["canary_evidence_count"] == 1 and evidence == [canary_path]

        wrong_route_canary = json.loads(json.dumps(canary))
        wrong_route_canary["request"]["path"] = "/api/maintenance/bad-returns"
        canary_path.write_text(json.dumps(wrong_route_canary), encoding="utf-8")
        allowed_write["canary_evidence"][0]["sha256"] = hashlib.sha256(
            canary_path.read_bytes()
        ).hexdigest()
        path.write_text(json.dumps(allowed_write), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError:
            pass
        else:
            raise AssertionError("canary for a different API route was accepted")

        malformed_canary = json.loads(json.dumps(canary))
        malformed_canary["result"]["observed_status"] = 500
        canary_path.write_text(json.dumps(malformed_canary), encoding="utf-8")
        allowed_write["canary_evidence"][0]["sha256"] = hashlib.sha256(
            canary_path.read_bytes()
        ).hexdigest()
        path.write_text(json.dumps(allowed_write), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError:
            pass
        else:
            raise AssertionError("failed write canary was accepted")

        review = {
            "format": "github-exact-sha-independent-review-v1",
            "source": "github-commit-comment-api",
            "repository": "Example/it-spareparts",
            "target_sha": head,
            "scope": "full-release-candidate",
            "reviewer_id": "reviewer.two",
            "completed_at": "2026-08-10T12:00:00Z",
            "p0_count": 0,
            "p1_count": 0,
            "conclusion": "approved",
            "author_association": "COLLABORATOR",
            "comment_id": 123,
            "comment_url": f"https://github.com/Example/it-spareparts/commit/{head}#commitcomment-123",
            "report_url": "https://github.com/Example/it-spareparts/issues/1",
            "body_sha256": "a" * 64,
        }
        review_body = {
            "format": "v121-independent-review-attestation-v1",
            "target_sha": head,
            "scope": "full-release-candidate",
            "p0_count": 0,
            "p1_count": 0,
            "conclusion": "approved",
            "report_url": "https://github.com/Example/it-spareparts/issues/1",
        }
        captured_review = module._review_comment_evidence(
            {
                "body": json.dumps(review_body),
                "user": {"login": "reviewer.two", "type": "User"},
                "author_association": "COLLABORATOR",
                "commit_id": head,
                "created_at": "2026-08-10T12:00:00Z",
                "updated_at": "2026-08-10T12:00:00Z",
                "id": 123,
                "html_url": f"https://github.com/Example/it-spareparts/commit/{head}#commitcomment-123",
            },
            repository="Example/it-spareparts",
            target=head,
        )
        assert captured_review["format"] == "github-exact-sha-independent-review-v1"
        assert captured_review["reviewer_id"] == "reviewer.two"
        assert module._validate_review_evidence(
            review, repository="Example/it-spareparts", target=head
        ) == ("reviewer.two", 123)
        review["p1_count"] = 1
        try:
            module._validate_review_evidence(
                review, repository="Example/it-spareparts", target=head
            )
        except module.ManifestError:
            pass
        else:
            raise AssertionError("review with unresolved P1 was accepted")

    print(f"v1.21 Beta release-control static self-test passed ({len(inventory)} migrations)")


if __name__ == "__main__":
    main()
