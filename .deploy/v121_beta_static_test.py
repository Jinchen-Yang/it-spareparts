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


def run_live_projection(release: str, raw: str, mode: str) -> subprocess.CompletedProcess[str]:
    marker = '  python3 - "$raw" "$mode" <<\'PY\'\n'
    start = release.index(marker) + len(marker)
    end = release.index("\nPY\n}", start)
    return subprocess.run(
        ("python3", "-", raw, mode),
        cwd=ROOT,
        input=release[start:end],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


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
    assert '"MAINTENANCE_CUTOVER_ENABLED": "true"' not in release
    assert "MAINTENANCE_CUTOVER_ENABLED=true" not in release
    assert release.count('"MAINTENANCE_CUTOVER_ENABLED": "false"') == 1
    assert not re.search(r"\bset_flags\s+(?:true|false)\s+(?:true|false)\s+(?:true|false)", release)
    flag_assertions = re.findall(
        r"\bassert_flags\s+(true|false)\s+(true|false)\s+(true|false)", release
    )
    assert flag_assertions and all(cutover == "false" for _, _, cutover in flag_assertions)
    assert "cutover)" not in release
    assert "EXPECTED_FROM=f1c8e4a7b2d9" in release
    assert "EXPECTED_TO=d9f1a3c7e5b2" in release
    assert all(
        command in release
        for command in ("backup-restore", "migrate", "pilot-smoke", "observe")
    )
    assert 'f"{mode} pilot exposes Maintenance write actions:' in release
    assert "review callback is action-gated" not in release
    assert "not page-gated" not in release

    module = load_manifest_module()
    assert module.PILOT_REVIEW_SCOPE == "stable-plus-beta-pilot-cutover-disabled"
    assert module.INITIAL_PILOT_POLICY["maintenance_cutover_enabled"] is False
    assert module.INITIAL_PILOT_POLICY["maintenance_write_actions"] == "excluded"
    assert module.INITIAL_PILOT_POLICY["maintenance_business_data_migration"] == "deferred"
    assert module.INITIAL_PILOT_POLICY["maintenance_cutover"] == "deferred"
    assert module.INITIAL_PILOT_POLICY["admin_pilots"] == "excluded"
    assert module.INITIAL_PILOT_POLICY["permission_projection"] == "raw-runtime-effective"
    assert module.INITIAL_PILOT_POLICY["replenishment_create"] == (
        "exact-sha-live-canary-required"
    )
    assert module.INITIAL_PILOT_POLICY["replenishment_review"] == "deferred"
    assert module.INITIAL_PILOT_POLICY["database_schema_migration"] == "required-prerequisite"
    permission_source = (ROOT / "backend/app/permissions.py").read_text(encoding="utf-8")
    maintenance_permissions = set(
        re.findall(r'"(action_maintenance_[a-z_]+)"', permission_source)
    )
    assert set(module.MAINTENANCE_ACTIONS) == maintenance_permissions
    assert all(action in release for action in module.MAINTENANCE_ACTIONS)
    smoke_guard_start = release.index('if mode in {"reader", "creator"}:\n    maintenance_actions = (')
    smoke_guard_end = release.index("\nstatus, features=", smoke_guard_start)
    smoke_guard = release[smoke_guard_start:smoke_guard_end]
    assert set(re.findall(r'"(action_maintenance_[a-z_]+)"', smoke_guard)) == set(
        module.MAINTENANCE_ACTIONS
    )
    for key, value in (
        ("page_replenishment_beta", "True"),
        ("data_pool_price_governance", "True"),
        ("action_replenishment_create", "True"),
        ("action_replenishment_review", "False"),
    ):
        assert f'"{key}": {value}' in smoke_guard
    assert "credential is not the scoped replenishment creator" in smoke_guard
    for key, value in (
        ("page_maintenance", "True"),
        ("page_maintenance_beta", "True"),
        ("page_replenishment_beta", "False"),
        ("action_replenishment_create", "False"),
        ("action_replenishment_review", "False"),
    ):
        assert f'"{key}": {value}' in smoke_guard
    assert "credential is not the scoped Maintenance reader" in smoke_guard
    assert 'smoke reader "$2"' in release
    assert 'smoke creator "$3"' in release
    assert '! smoke reader "$4"' in release
    assert '! smoke creator "$5"' in release
    assert 'features.get("replenishment") is not True' in release
    assert 'features.get("maintenance") is not True' in release
    assert "Maintenance Beta reader smoke failed" in release
    assert "Maintenance reader unexpectedly accesses replenishment" in release
    assert "replenishment creator unexpectedly accesses Maintenance Beta" in release
    assert '"can_view_price": True' in release
    assert '"can_create": True' in release
    assert '"can_review": False' in release
    assert "replenishment creator catalog smoke failed" in release
    replenishment_source = (ROOT / "backend/app/api/replenishment.py").read_text(
        encoding="utf-8"
    )
    review_start = replenishment_source.index(
        '@router.post("/applications/{application_id}/review-results")'
    )
    review_end = replenishment_source.index("\n@router.", review_start + 1)
    review_route = replenishment_source[review_start:review_end]
    assert "_page: None = Depends(_beta_page_whitelist)" in review_route
    reader_projection_row = "\t".join(
        ["named.reader", "project_manager", "t", "t"]
        + ["f"] * len(module.MAINTENANCE_ACTIONS)
        + ["f", "t", "f", "f"]
    )
    creator_projection_row = "\t".join(
        ["named.pilot", "purchaser", "f", "f"]
        + ["f"] * len(module.MAINTENANCE_ACTIONS)
        + ["t", "t", "t", "f"]
    )
    safe_projection = run_live_projection(
        release,
        "\n".join((reader_projection_row, creator_projection_row)),
        "full",
    )
    assert safe_projection.returncode == 0, safe_projection.stderr
    safe_projection_data = json.loads(safe_projection.stdout)
    assert safe_projection_data["maintenance_write_enabled_count"] == 0
    assert safe_projection_data["admin_pilot_count"] == 0
    assert safe_projection_data["maintenance_read_account_count"] == 1
    assert safe_projection_data["replenishment_creator_account_count"] == 1
    assert safe_projection_data["replenishment_review_enabled_count"] == 0
    assert safe_projection_data["cross_domain_account_count"] == 0
    assert safe_projection_data["replenishment_noncreator_account_count"] == 0
    assert safe_projection_data["reader_replenishment_action_enabled_count"] == 0
    assert safe_projection_data["replenishment_creator_missing_price_count"] == 0
    cross_domain_projection = run_live_projection(
        release,
        "\n".join(
            (
                reader_projection_row,
                creator_projection_row.replace("\tf\tf\t", "\tt\tt\t", 1),
            )
        ),
        "full",
    )
    assert cross_domain_projection.returncode != 0
    assert "cross-domain Beta account" in cross_domain_projection.stderr
    noncreator_projection = run_live_projection(
        release,
        "\n".join(
            (
                reader_projection_row,
                creator_projection_row.rsplit("\t", 2)[0] + "\tf\tf",
            )
        ),
        "full",
    )
    assert noncreator_projection.returncode != 0
    assert "un-smoked Replenishment profile" in noncreator_projection.stderr
    reader_action_fields = reader_projection_row.split("\t")
    reader_action_fields[-2] = "t"
    reader_action_projection = run_live_projection(
        release,
        "\n".join(("\t".join(reader_action_fields), creator_projection_row)),
        "full",
    )
    assert reader_action_projection.returncode != 0
    assert "reader contains a Replenishment action" in reader_action_projection.stderr
    creator_without_price_fields = creator_projection_row.split("\t")
    creator_without_price_fields[-3] = "f"
    creator_without_price_projection = run_live_projection(
        release,
        "\n".join((reader_projection_row, "\t".join(creator_without_price_fields))),
        "full",
    )
    assert creator_without_price_projection.returncode != 0
    assert "creator lacks the required price permission" in (
        creator_without_price_projection.stderr
    )
    hidden_maintenance_write = run_live_projection(
        release,
        "\t".join(
            ["replenishment.pilot", "sales", "t", "f", "t"]
            + ["f"] * (len(module.MAINTENANCE_ACTIONS) - 1)
            + ["t", "t", "f", "f"]
        ),
        "full",
    )
    assert hidden_maintenance_write.returncode != 0
    assert "raw runtime-effective Maintenance write" in hidden_maintenance_write.stderr
    admin_callback = run_live_projection(
        release,
        "\t".join(
            ["named.admin", "admin"]
            + ["f"] * (2 + len(module.MAINTENANCE_ACTIONS))
            + ["t", "f", "f", "f"]
        ),
        "full",
    )
    assert admin_callback.returncode != 0
    assert "raw runtime-effective Maintenance write" in admin_callback.stderr
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
                    "username": "named.reader",
                    "role": "project_manager",
                    **permissions(maintenance=True, replenishment=False),
                },
                {
                    "username": "named.pilot",
                    "role": "purchaser",
                    **permissions(maintenance=False, replenishment=True),
                },
            ],
            "canary_evidence": [],
        }
        safe["accounts"][0]["replenishment"]["data_pool_price_governance"] = True
        path = folder / "allowlist.json"
        path.write_text(json.dumps(safe), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError as exc:
            assert "opens Replenishment Beta without the scoped creator action" in str(exc)
        else:
            raise AssertionError("pilot without a replenishment creator was accepted")

        for admin_role in ("admin", "Admin"):
            admin = json.loads(json.dumps(safe))
            admin["accounts"][0]["role"] = admin_role
            path.write_text(json.dumps(admin), encoding="utf-8")
            try:
                module._parse_allowlist(
                    path, repository="Example/it-spareparts", target=head
                )
            except module.ManifestError as exc:
                assert "admin" in str(exc)
            else:
                raise AssertionError("Maintenance Beta admin pilot was accepted")

        replenishment_only_admin = {
            "format": "v121-beta-allowlist-v1",
            "accounts": [
                {
                    "username": "named.admin",
                    "role": "admin",
                    **permissions(maintenance=False, replenishment=True),
                }
            ],
            "canary_evidence": [],
        }
        path.write_text(json.dumps(replenishment_only_admin), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError as exc:
            assert "admin" in str(exc)
        else:
            raise AssertionError("Replenishment-only admin bypassed the scoped pilot boundary")

        for maintenance_action in module.MAINTENANCE_ACTIONS:
            maintenance_write = json.loads(json.dumps(safe))
            maintenance_write["accounts"][0]["maintenance"][maintenance_action] = True
            path.write_text(json.dumps(maintenance_write), encoding="utf-8")
            try:
                module._parse_allowlist(
                    path, repository="Example/it-spareparts", target=head
                )
            except module.ManifestError as exc:
                assert "excludes every Maintenance write action" in str(exc)
            else:
                raise AssertionError(
                    f"initial pilot accepted Maintenance write: {maintenance_action}"
                )

        canary = {
            "format": "v121-action-canary-v1",
            "source": "github-commit-comment-api",
            "repository": "Example/it-spareparts",
            "username": "named.reader",
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
        canary_path = folder / "site-issue-canary.json"
        canary_path.write_text(json.dumps(captured_canary), encoding="utf-8")
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
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError as exc:
            assert "excludes every Maintenance write action" in str(exc)
        else:
            raise AssertionError("Maintenance canary bypassed the scoped pilot write exclusion")

        replenishment_body = {
            "format": "v121-action-canary-v1",
            "username": "named.pilot",
            "action": "action_replenishment_create",
            "target_sha": head,
            "environment": "isolated",
            "request": {
                "method": "POST",
                "route_template": "/api/replenishment-beta/applications",
                "path": "/api/replenishment-beta/applications",
                "payload_sha256": "3" * 64,
            },
            "result": {
                "expected_status": 201,
                "observed_status": 201,
                "response_sha256": "4" * 64,
            },
            "conclusion": "passed",
        }
        captured_replenishment = module._canary_comment_evidence(
            {
                "body": json.dumps(replenishment_body),
                "user": {"login": "reviewer.one", "type": "User"},
                "author_association": "COLLABORATOR",
                "commit_id": head,
                "created_at": "2026-08-10T12:00:00Z",
                "updated_at": "2026-08-10T12:00:00Z",
                "id": 124,
                "html_url": (
                    f"https://github.com/Example/it-spareparts/commit/{head}"
                    "#commitcomment-124"
                ),
            },
            repository="Example/it-spareparts",
            target=head,
        )
        canary_failures = []
        wrong_route_body = json.loads(json.dumps(replenishment_body))
        wrong_route_body["request"]["path"] = "/api/replenishment-beta/catalog"
        canary_failures.append(("wrong route", wrong_route_body, {}))
        wrong_method_body = json.loads(json.dumps(replenishment_body))
        wrong_method_body["request"]["method"] = "PUT"
        canary_failures.append(("wrong method", wrong_method_body, {}))
        wrong_sha_body = json.loads(json.dumps(replenishment_body))
        wrong_sha_body["target_sha"] = "0" * 40
        canary_failures.append(("wrong SHA", wrong_sha_body, {}))
        canary_failures.append(
            (
                "edited comment",
                replenishment_body,
                {"updated_at": "2026-08-10T12:01:00Z"},
            )
        )
        canary_failures.append(
            (
                "non-collaborator",
                replenishment_body,
                {"author_association": "NONE"},
            )
        )
        for label, body, overrides in canary_failures:
            payload = {
                "body": json.dumps(body),
                "user": {"login": "reviewer.one", "type": "User"},
                "author_association": "COLLABORATOR",
                "commit_id": head,
                "created_at": "2026-08-10T12:00:00Z",
                "updated_at": "2026-08-10T12:00:00Z",
                "id": 224,
                "html_url": (
                    f"https://github.com/Example/it-spareparts/commit/{head}"
                    "#commitcomment-224"
                ),
                **overrides,
            }
            try:
                module._canary_comment_evidence(
                    payload,
                    repository="Example/it-spareparts",
                    target=head,
                )
            except module.ManifestError:
                pass
            else:
                raise AssertionError(f"canary accepted {label}")
        replenishment_path = folder / "replenishment-create-canary.json"
        replenishment_path.write_text(
            json.dumps(captured_replenishment), encoding="utf-8"
        )
        replenishment_write = json.loads(json.dumps(safe))
        replenishment_write["accounts"][1]["replenishment"][
            "action_replenishment_create"
        ] = True
        replenishment_write["canary_evidence"] = [
            {
                "username": "named.pilot",
                "action": "action_replenishment_create",
                "target_sha": head,
                "conclusion": "passed",
                "path": replenishment_path.name,
                "sha256": hashlib.sha256(replenishment_path.read_bytes()).hexdigest(),
            }
        ]
        path.write_text(json.dumps(replenishment_write), encoding="utf-8")
        summary, evidence = module._parse_allowlist(
            path, repository="Example/it-spareparts", target=head
        )
        assert summary["canary_evidence_count"] == 1
        assert summary["maintenance_write_enabled_count"] == 0
        assert summary["admin_pilot_count"] == 0
        assert summary["maintenance_read_account_count"] == 1
        assert summary["replenishment_creator_account_count"] == 1
        assert summary["replenishment_review_enabled_count"] == 0
        assert summary["cross_domain_account_count"] == 0
        assert summary["replenishment_noncreator_account_count"] == 0
        assert summary["reader_replenishment_action_enabled_count"] == 0
        assert summary["replenishment_creator_missing_price_count"] == 0
        assert replenishment_write["accounts"][0]["replenishment"][
            "data_pool_price_governance"
        ] is True
        assert evidence == [replenishment_path]

        cross_domain = json.loads(json.dumps(replenishment_write))
        cross_domain["accounts"][1]["maintenance"]["page_maintenance"] = True
        cross_domain["accounts"][1]["maintenance"]["page_maintenance_beta"] = True
        path.write_text(json.dumps(cross_domain), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError as exc:
            assert "crosses the Maintenance reader and replenishment creator" in str(exc)
        else:
            raise AssertionError("cross-domain Beta account was accepted")

        replenishment_without_create = json.loads(json.dumps(replenishment_write))
        replenishment_without_create["accounts"][1]["replenishment"][
            "action_replenishment_create"
        ] = False
        path.write_text(json.dumps(replenishment_without_create), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError as exc:
            assert "opens Replenishment Beta without the scoped creator action" in str(exc)
        else:
            raise AssertionError("un-smoked Replenishment profile was accepted")

        for action in ("action_replenishment_create", "action_replenishment_review"):
            reader_with_replenishment_action = json.loads(json.dumps(replenishment_write))
            reader_with_replenishment_action["accounts"][0]["replenishment"][action] = True
            path.write_text(json.dumps(reader_with_replenishment_action), encoding="utf-8")
            try:
                module._parse_allowlist(
                    path, repository="Example/it-spareparts", target=head
                )
            except module.ManifestError as exc:
                if action == "action_replenishment_review":
                    assert "defers replenishment review" in str(exc)
                else:
                    assert "enabled without its Beta page" in str(exc)
            else:
                raise AssertionError(f"Maintenance reader accepted {action}")

        account_without_beta = json.loads(json.dumps(replenishment_write))
        account_without_beta["accounts"].append(
            {
                "username": "named.stable",
                "role": "readonly",
                **permissions(maintenance=False, replenishment=False),
            }
        )
        path.write_text(json.dumps(account_without_beta), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError as exc:
            assert "has no effective Beta access" in str(exc)
        else:
            raise AssertionError("account with both Beta pages false was accepted")

        creator_without_maintenance = json.loads(json.dumps(replenishment_write))
        creator_without_maintenance["accounts"] = [
            creator_without_maintenance["accounts"][1]
        ]
        path.write_text(json.dumps(creator_without_maintenance), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError as exc:
            assert "requires at least one named Maintenance read account" in str(exc)
        else:
            raise AssertionError("pilot without a Maintenance reader was accepted")

        create_without_price = json.loads(json.dumps(replenishment_write))
        create_without_price["accounts"][1]["replenishment"][
            "data_pool_price_governance"
        ] = False
        path.write_text(json.dumps(create_without_price), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError as exc:
            assert "without price permission" in str(exc)
        else:
            raise AssertionError("replenishment create without price permission was accepted")

        path.write_text(json.dumps(replenishment_write), encoding="utf-8")

        original_fetch_canary = module._fetch_canary_comment
        module._fetch_canary_comment = lambda **_kwargs: {
            **captured_replenishment,
            "comment_id": 999,
        }
        try:
            try:
                module._parse_allowlist(
                    path,
                    repository="Example/it-spareparts",
                    target=head,
                    verify_live_canaries=True,
                )
            except module.ManifestError:
                pass
            else:
                raise AssertionError("GitHub live canary drift was accepted")
        finally:
            module._fetch_canary_comment = original_fetch_canary

        unused_maintenance_canary = json.loads(json.dumps(safe))
        unused_maintenance_canary["canary_evidence"] = [
            {
                "username": canary["username"],
                "action": canary["action"],
                "target_sha": head,
                "conclusion": "passed",
                "path": canary_path.name,
                "sha256": hashlib.sha256(canary_path.read_bytes()).hexdigest(),
            }
        ]
        path.write_text(json.dumps(unused_maintenance_canary), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError:
            pass
        else:
            raise AssertionError("unused Maintenance canary was accepted")

        missing_canary = json.loads(json.dumps(replenishment_write))
        missing_canary["canary_evidence"] = []
        path.write_text(json.dumps(missing_canary), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError:
            pass
        else:
            raise AssertionError("replenishment write without real canary was accepted")

        replenishment_review_body = json.loads(json.dumps(replenishment_body))
        replenishment_review_body["action"] = "action_replenishment_review"
        replenishment_review_body["request"].update(
            {
                "route_template": (
                    "/api/replenishment-beta/applications/{application_id}/review-results"
                ),
                "path": (
                    "/api/replenishment-beta/applications/application-1/review-results"
                ),
                "payload_sha256": "5" * 64,
            }
        )
        replenishment_review_body["result"]["response_sha256"] = "6" * 64
        captured_replenishment_review = module._canary_comment_evidence(
            {
                "body": json.dumps(replenishment_review_body),
                "user": {"login": "reviewer.one", "type": "User"},
                "author_association": "COLLABORATOR",
                "commit_id": head,
                "created_at": "2026-08-10T12:00:00Z",
                "updated_at": "2026-08-10T12:00:00Z",
                "id": 125,
                "html_url": (
                    f"https://github.com/Example/it-spareparts/commit/{head}"
                    "#commitcomment-125"
                ),
            },
            repository="Example/it-spareparts",
            target=head,
        )
        review_canary_failures = []
        review_wrong_route = json.loads(json.dumps(replenishment_review_body))
        review_wrong_route["request"]["path"] = "/api/replenishment-beta/catalog"
        review_canary_failures.append(("wrong route", review_wrong_route, {}))
        review_wrong_method = json.loads(json.dumps(replenishment_review_body))
        review_wrong_method["request"]["method"] = "PATCH"
        review_canary_failures.append(("wrong method", review_wrong_method, {}))
        review_wrong_sha = json.loads(json.dumps(replenishment_review_body))
        review_wrong_sha["target_sha"] = "0" * 40
        review_canary_failures.append(("wrong SHA", review_wrong_sha, {}))
        review_canary_failures.append(
            (
                "edited comment",
                replenishment_review_body,
                {"updated_at": "2026-08-10T12:01:00Z"},
            )
        )
        review_canary_failures.append(
            (
                "non-collaborator",
                replenishment_review_body,
                {"author_association": "NONE"},
            )
        )
        for label, body, overrides in review_canary_failures:
            payload = {
                "body": json.dumps(body),
                "user": {"login": "reviewer.one", "type": "User"},
                "author_association": "COLLABORATOR",
                "commit_id": head,
                "created_at": "2026-08-10T12:00:00Z",
                "updated_at": "2026-08-10T12:00:00Z",
                "id": 225,
                "html_url": (
                    f"https://github.com/Example/it-spareparts/commit/{head}"
                    "#commitcomment-225"
                ),
                **overrides,
            }
            try:
                module._canary_comment_evidence(
                    payload,
                    repository="Example/it-spareparts",
                    target=head,
                )
            except module.ManifestError:
                pass
            else:
                raise AssertionError(f"review canary accepted {label}")
        replenishment_review_path = folder / "replenishment-review-canary.json"
        replenishment_review_path.write_text(
            json.dumps(captured_replenishment_review), encoding="utf-8"
        )
        replenishment_review = json.loads(json.dumps(safe))
        replenishment_review["accounts"][1]["replenishment"][
            "action_replenishment_review"
        ] = True
        replenishment_review["accounts"][1]["replenishment"][
            "data_pool_price_governance"
        ] = False
        replenishment_review["canary_evidence"] = [
            {
                "username": "named.pilot",
                "action": "action_replenishment_review",
                "target_sha": head,
                "conclusion": "passed",
                "path": replenishment_review_path.name,
                "sha256": hashlib.sha256(
                    replenishment_review_path.read_bytes()
                ).hexdigest(),
            }
        ]
        path.write_text(json.dumps(replenishment_review), encoding="utf-8")
        try:
            module._parse_allowlist(path, repository="Example/it-spareparts", target=head)
        except module.ManifestError as exc:
            assert "defers replenishment review" in str(exc)
        else:
            raise AssertionError("deferred replenishment review was accepted into the pilot")

        review = {
            "format": "github-exact-sha-independent-review-v1",
            "source": "github-commit-comment-api",
            "repository": "Example/it-spareparts",
            "target_sha": head,
            "scope": module.PILOT_REVIEW_SCOPE,
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
            "scope": module.PILOT_REVIEW_SCOPE,
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
        misleading_review = json.loads(json.dumps(review))
        misleading_review["scope"] = "full-release-candidate"
        try:
            module._validate_review_evidence(
                misleading_review, repository="Example/it-spareparts", target=head
            )
        except module.ManifestError:
            pass
        else:
            raise AssertionError("misleading full-release review scope was accepted")
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
