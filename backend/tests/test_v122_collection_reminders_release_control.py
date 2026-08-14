from __future__ import annotations

import copy
import importlib.util
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
DEPLOY = REPO_ROOT / ".deploy"
MANIFEST = DEPLOY / "v122_collection_reminders_manifest.py"
BUILD = DEPLOY / "v122_collection_reminders_build.sh"
REHEARSE = DEPLOY / "v122_collection_reminders_rehearse.sh"
RELEASE = DEPLOY / "v122_collection_reminders_release.sh"
STATIC_TEST = DEPLOY / "v122_collection_reminders_static_test.py"
CONTRACT = (
    REPO_ROOT
    / ".ai"
    / "contracts"
    / "maintenance-collections"
    / "project-manager-xls-v1.yaml"
)
COMPOSE = REPO_ROOT / "docker-compose.yml"


TARGET_SHA = "a" * 40
FINAL_TARGET_SHA = "6" * 40
PARENT_SHA = "b" * 40
IMAGE_ID = "sha256:" + "c" * 64
FRONTEND_IMAGE_ID = "sha256:" + "d" * 64
DB_IMAGE_ID = "sha256:" + "e" * 64
SBOM_SHA = "f" * 64
CANARY_PROJECT_ID = "123e4567-e89b-12d3-a456-426614174000"
CANARY_MILESTONE_ID = "milestone-canary-0001"
NON_CANARY_MILESTONE_ID = "milestone-other-0001"
DYNAMIC_CANARY_MILESTONE_ID = "{canary_milestone_id}"
CROSS_PROJECT_ID = "123e4567-e89b-12d3-a456-426614174099"
CROSS_PROJECT_CONTRACT_ID = "contract-other"
REAL_SAMPLE_SHA256 = "a783af09fa108d366a26e10fe188be52d20a9ce1fe02121bfd683d96356c8c18"


def _follow_up_case(
    *, account: str, milestone_id: str, expected_status: int, key: str,
) -> dict:
    return {
        "method": "POST",
        "account": account,
        "path": f"/api/maintenance/collection-milestones/{milestone_id}/follow-ups",
        "expected_status": expected_status,
        "body": {
            "expected_version": 1,
            "idempotency_key": key,
            "action": "handle",
            "note": "release canary",
        },
    }


def _setup_contract_case() -> dict:
    return {
        "method": "POST",
        "account": "importer",
        "path": f"/api/maintenance/projects/stable/{CANARY_PROJECT_ID}/contracts",
        "expected_status": 201,
        "body": {
            "contract_id": "canary-contract-source",
            "contract_no": "CANARY-CONTRACT-001",
            "contract_amount": "1000.00",
            "contract_status": "canary",
            "status_mapping_state": "mapped",
            "status_mapping_version": "release-canary-v1",
            "included_in_total": False,
            "effective_from": "2026-01-01",
            "source": "release_canary",
            "reason": "v122 release canary setup",
        },
    }


def _action_control_cases(token: str = "control-token") -> dict:
    def update(username: str, overrides: dict[str, bool]) -> dict:
        return {
            "method": "PUT",
            "token": token,
            "path": f"/api/accounts/{username}",
            "expected_status": 200,
            "body": {"overrides": overrides},
        }

    verify = {
        "method": "GET",
        "token": token,
        "path": "/api/accounts",
        "expected_status": 200,
    }
    original = {
        "importer": {
            "data_customer": False,
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "action_maintenance_collection_plan_import": False,
        },
        "follower": {
            "own_customers_only": True,
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "action_maintenance_collection_follow_up": False,
        },
        "denied": {
            "data_supplier": False,
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "action_maintenance_collection_plan_import": False,
            "action_maintenance_collection_follow_up": False,
        },
    }
    return {
        "action_grant": [
            update("importer", {**original["importer"], "action_maintenance_collection_plan_import": True}),
            update("follower", {**original["follower"], "action_maintenance_collection_follow_up": True}),
            update("denied", dict(original["denied"])),
        ],
        "action_verify_granted": dict(verify),
        "action_restore": [
            update("denied", dict(original["denied"])),
            update("follower", dict(original["follower"])),
            update("importer", dict(original["importer"])),
        ],
        "action_verify_restored": dict(verify),
    }


def _action_account_rows(controls: dict, list_name: str) -> list[dict]:
    return [
        {
            "username": case["path"].rsplit("/", 1)[-1],
            "overrides": case["body"]["overrides"],
        }
        for case in controls[list_name]
    ]


def _full_canary_docker_body(event_log: Path) -> str:
    return f"""
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
    if [[ "$*" == *"SELECT milestone_id"* ]]; then echo milestone-canary-0001; exit 0; fi
    if [[ "$*" == *"maintenance_collection_milestone"* ]]; then echo '0:0:0:0'; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"exec -T app"* ]]; then
      apply=$(sed -n 's/^MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=//p' "$V122_APP_DIR/.env")
      project=$(sed -n 's/^MAINTENANCE_COLLECTION_CANARY_PROJECT_ID=//p' "$V122_APP_DIR/.env")
      printf 'readback:%s\n' "$apply" >> {event_log}
      printf '%s\n%s\n' "$apply" "$project"; exit 0
    fi
    if [[ "$*" == *"compose"* && "$*" == *"up --no-deps --no-build"* ]]; then
      apply=$(sed -n 's/^MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=//p' "$V122_APP_DIR/.env")
      printf 'restart:%s\n' "$apply" >> {event_log}
      exit 0
    fi
    exit 97
    """


def _write_full_canary_curl_stub(
    path: Path,
    *,
    controls: dict,
    curl_calls: Path,
    event_log: Path,
    restore_verify_failure: str | None = None,
    permission_code: str = "permission_denied",
    replace_spec: tuple[Path, Path] | None = None,
    snapshot_audit: Path | None = None,
) -> Path:
    restored_rows = json.dumps(
        _action_account_rows(controls, "action_restore"), separators=(",", ":"),
    )
    granted_rows = json.dumps(
        _action_account_rows(controls, "action_grant"), separators=(",", ":"),
    )
    verify_count = event_log.with_name("restore-verify-count")
    if restore_verify_failure == "transport":
        second_verify = "exit 7"
    elif restore_verify_failure == "status":
        second_verify = f"printf '%s' '{restored_rows}' >\"$out\"; status=503"
    elif restore_verify_failure is None:
        second_verify = f"printf '%s' '{restored_rows}' >\"$out\"; status=200"
    else:
        raise AssertionError(f"unsupported restore verifier failure: {restore_verify_failure}")
    replace_once = ""
    if replace_spec is not None:
        original, replacement = replace_spec
        marker = original.with_name(f".{original.name}.replaced")
        replace_once = f"""
        if [ ! -e "{marker}" ]; then
          cp -- "{replacement}" "{original}"
          chmod 600 "{original}"
          : > "{marker}"
        fi
        """
    audit_snapshot = ""
    if snapshot_audit is not None:
        audit_snapshot = f"""
        work=${{out%/*}}
        snapshot="$work/sealed-canary-spec.json"
        kind=unsafe
        [ -f "$snapshot" ] && [ ! -L "$snapshot" ] && kind=regular
        printf '%s %s %s\n' "$(stat -c %a "$work" 2>/dev/null)" \
          "$(stat -c %a "$snapshot" 2>/dev/null)" "$kind" > "{snapshot_audit}"
        """
    return _write(
        path,
        f"""
        #!/usr/bin/env bash
        printf '%s\n' "$*" >> {curl_calls}
        out=''
        data=''
        previous=''
        for arg in "$@"; do
          if [ "$previous" = output ]; then out=$arg; fi
          if [ "$previous" = data ]; then data="${{arg#@}}"; fi
          previous=''
          [ "$arg" = --output ] && previous=output
          [ "$arg" = --data-binary ] && previous=data
        done
        url=${{!#}}
        {audit_snapshot}
        {replace_once}
        case "$out" in
          *action_verify_restored.response)
            count=0
            [ ! -f {verify_count} ] || count=$(cat {verify_count})
            count=$((count + 1))
            printf '%s\n' "$count" > {verify_count}
            printf 'verify-restored:%s\n' "$count" >> {event_log}
            if [ "$count" -gt 1 ]; then {second_verify};
            else printf '%s' '{restored_rows}' >"$out"; status=200; fi ;;
          *action_verify_granted.response)
            printf '%s' '{granted_rows}' >"$out"; status=200 ;;
          *action_grant-*.response|*action_restore-*.response)
            case "$out" in *action_restore-*.response) printf 'restore-put\n' >> {event_log} ;; esac
            python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); json.dump({{"username":sys.argv[2],"overrides":body["overrides"]}},open(sys.argv[3],"w"))' "$data" "${{url##*/}}" "$out"; status=200 ;;
          *login-follower.response.json)
            printf '%s' '{{"token":"follower-token","role":"user","permissions":{{"action_maintenance_collection_follow_up":true}}}}' >"$out"; status=200 ;;
          *login-importer.response.json)
            printf '%s' '{{"token":"importer-token","role":"admin","permissions":{{"action_maintenance_collection_plan_import":true}}}}' >"$out"; status=200 ;;
          *login-denied.response.json)
            printf '%s' '{{"token":"denied-token","role":"admin","permissions":{{"action_maintenance_collection_plan_import":false,"action_maintenance_collection_follow_up":false}}}}' >"$out"; status=200 ;;
          *setup_contract.response)
            printf '%s' '{{"project_id":"{CANARY_PROJECT_ID}","project_contract_id":"contract-live","version":3}}' >"$out"; status=201 ;;
          *import_preview_positive.response)
            printf '%s' '{{"batch_id":"batch-canary","batch_version":7,"data_version":"data-v7","status":"valid","rows":[{{"external_order_no":"ORDER-1","row_key":"row-live"}}]}}' >"$out"; status=200 ;;
          *cross_project_negative.response)
            printf '%s' '{{"detail":{{"code":"canary_scope_denied"}}}}' >"$out"; status=403 ;;
          *apply_last.response|*follow_up_positive.response)
            printf '{{}}' >"$out"; status=200 ;;
          *permission_negative.response)
            printf '%s' '{{"detail":{{"code":"{permission_code}"}}}}' >"$out"; status=403 ;;
          *) printf '{{}}' >"$out"; status=200 ;;
        esac
        printf '%s' "$status"
        """,
        mode=0o700,
    )


def _write_rollback_curl_stub(
    path: Path,
    *,
    spec: dict,
    curl_calls: Path,
    granted_rows: list[dict] | None = None,
    restored_rows: list[dict] | None = None,
    restore_verify_failure: str | None = None,
    replace_spec: tuple[Path, Path] | None = None,
    snapshot_audit: Path | None = None,
) -> Path:
    granted = json.dumps(
        granted_rows or _action_account_rows(spec, "action_grant"),
        separators=(",", ":"),
    )
    restored = json.dumps(
        restored_rows or _action_account_rows(spec, "action_restore"),
        separators=(",", ":"),
    )
    if restore_verify_failure == "transport":
        restore_verify = "exit 7"
    elif restore_verify_failure == "status":
        restore_verify = f"printf '%s' '{restored}' >\"$out\"; status=503"
    elif restore_verify_failure == "outcome":
        restore_verify = (
            f"printf '%s' '{restored}' >\"$out\"; "
            'mkdir "${out%.response}.outcome.json"; status=200'
        )
    elif restore_verify_failure in {None, "mismatch"}:
        restore_verify = f"printf '%s' '{restored}' >\"$out\"; status=200"
    else:
        raise AssertionError(f"unsupported rollback verifier failure: {restore_verify_failure}")
    replace_once = ""
    if replace_spec is not None:
        original, replacement = replace_spec
        marker = original.with_name(f".{original.name}.replaced")
        replace_once = f"""
        if [ ! -e "{marker}" ]; then
          cp -- "{replacement}" "{original}"
          chmod 600 "{original}"
          : > "{marker}"
        fi
        """
    audit_snapshot = ""
    if snapshot_audit is not None:
        audit_snapshot = f"""
        work=${{out%/*}}
        snapshot="$work/sealed-canary-spec.json"
        kind=unsafe
        [ -f "$snapshot" ] && [ ! -L "$snapshot" ] && kind=regular
        printf '%s %s %s\n' "$(stat -c %a "$work" 2>/dev/null)" \
          "$(stat -c %a "$snapshot" 2>/dev/null)" "$kind" > "{snapshot_audit}"
        """
    return _write(
        path,
        f"""
        #!/usr/bin/env bash
        printf '%s\n' "$*" >> {curl_calls}
        out=''
        data=''
        previous=''
        for arg in "$@"; do
          if [ "$previous" = output ]; then out=$arg; fi
          if [ "$previous" = data ]; then data="${{arg#@}}"; fi
          previous=''
          [ "$arg" = --output ] && previous=output
          [ "$arg" = --data-binary ] && previous=data
        done
        url=${{!#}}
        {audit_snapshot}
        {replace_once}
        case "$out" in
          *action_verify_granted.response)
            printf '%s' '{granted}' >"$out"; status=200 ;;
          *action_verify_restored.response)
            {restore_verify} ;;
          *action_restore-*.response)
            python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); json.dump({{"username":sys.argv[2],"overrides":body["overrides"]}},open(sys.argv[3],"w"))' "$data" "${{url##*/}}" "$out"; status=200 ;;
          *) printf '{{}}' >"$out"; status=200 ;;
        esac
        printf '%s' "$status"
        """,
        mode=0o700,
    )


def _cross_project_apply_case() -> dict:
    return {
        "method": "POST",
        "account": "importer",
        "path": "/api/maintenance/collection-plan-imports/{batch_id}/apply",
        "expected_status": 403,
        "body": {
            "project_id": CROSS_PROJECT_ID,
            "project_version": 1,
            "project_contract_id": CROSS_PROJECT_CONTRACT_ID,
            "project_contract_version": 1,
        },
    }


def _standard_canary_spec(workbook: Path, token: str = "control-token") -> dict:
    return {
        "base_url": "https://canary.invalid",
        "named_accounts": {
            "follower": {
                "username": "follower",
                "password": "secret-zero",
                "expected_role": "user",
                "required_permissions": ["action_maintenance_collection_follow_up"],
            },
            "importer": {
                "username": "importer",
                "password": "secret-one",
                "expected_role": "admin",
                "required_permissions": ["action_maintenance_collection_plan_import"],
            },
            "denied": {
                "username": "denied",
                "password": "secret-two",
                "expected_role": "admin",
                "forbidden_permissions": [
                    "action_maintenance_collection_plan_import",
                    "action_maintenance_collection_follow_up",
                ],
            },
        },
        **_action_control_cases(token),
        "setup_contract": _setup_contract_case(),
        "follow_up_positive": _follow_up_case(
            account="follower",
            milestone_id=DYNAMIC_CANARY_MILESTONE_ID,
            expected_status=200,
            key="follow-positive-0001",
        ),
        "cross_project_negative": _cross_project_apply_case(),
        "permission_negative": _follow_up_case(
            account="denied",
            milestone_id=DYNAMIC_CANARY_MILESTONE_ID,
            expected_status=403,
            key="follow-denied-0001",
        ),
        "import_preview_positive": {
            "method": "POST",
            "account": "importer",
            "path": "/api/maintenance/collection-plan-imports/preview",
            "expected_status": 200,
            "project_version": 4,
            "workbook_path": str(workbook),
            "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            "idempotency_key": "canary-preview-0001",
            "bindings": [{
                "external_order_no": "ORDER-1",
                "project_id": CANARY_PROJECT_ID,
                "project_version": 4,
                "project_contract_id": "{setup_contract.project_contract_id}",
                "project_contract_version": "{setup_contract.version}",
                "existing_binding_version": None,
                "reason": None,
            }],
        },
        "apply_last": {
            "method": "POST",
            "account": "importer",
            "path": "/api/maintenance/collection-plan-imports/{batch_id}/apply",
            "expected_status": 200,
        },
    }


def _load_manifest_module():
    spec = importlib.util.spec_from_file_location("v122_manifest", MANIFEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script(path: Path) -> str:
    assert path.is_file()
    assert path.stat().st_mode & stat.S_IXUSR
    subprocess.run(["bash", "-n", str(path)], cwd=REPO_ROOT, check=True)
    return path.read_text(encoding="utf-8")


def test_v122_release_artifacts_are_versioned_executable_and_syntax_checked():
    assert MANIFEST.is_file()
    assert STATIC_TEST.is_file()
    for path in (BUILD, REHEARSE, RELEASE):
        _script(path)

    subprocess.run(["python3", "-m", "py_compile", str(MANIFEST), str(STATIC_TEST)], check=True)


def test_manifest_contract_is_d9_to_c8_collection_reminders_not_old_v121_or_f9():
    module = _load_manifest_module()

    assert module.FORMAT == "v122-collection-reminders-2"
    assert module.DB_FROM == "d9f1a3c7e5b2"
    assert module.DB_TO == "c8e2a4f6b1d3"
    assert module.REQUIRED_RUNTIME_FLAGS == (
        "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED",
        "MAINTENANCE_COLLECTION_CANARY_PROJECT_ID",
    )
    assert module.COLLECTION_ACTIONS == (
        "action_maintenance_collection_follow_up",
        "action_maintenance_collection_plan_import",
    )

    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (MANIFEST, BUILD, REHEARSE, RELEASE, STATIC_TEST)
    )
    assert "f9b2d4e7c1a6" not in combined
    assert "v121_beta" not in combined
    assert "v122_beta" not in combined


def test_static_release_self_test_is_runnable_without_production_access():
    assert STATIC_TEST.is_file()
    if shutil.which("python3"):
        subprocess.run(["python3", str(STATIC_TEST)], cwd=REPO_ROOT, check=True)


def _write(path: Path, content: str | bytes, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(mode)
    return path


def _contract(path: Path, *, final: bool = False, duplicate: bool = False) -> Path:
    state = "approved_for_production_candidate" if final else "approved_for_implementation"
    allowed = "true" if final else "false"
    duplicate_line = f"production_apply_allowed: {allowed}\n" if duplicate else ""
    return _write(
        path,
        f"""
        contract_version: project-manager-xls-v1
        contract_state: {state}
        production_apply_allowed: {allowed}
        {duplicate_line}
        header_signature:
          value: {'1' * 64}
        """,
    )


def _json_artifact(path: Path, payload: dict) -> Path:
    return _write(path, json.dumps(payload, sort_keys=True) + "\n")


def _package_inputs(
    tmp_path: Path,
    *,
    final_contract: bool = False,
    target_sha: str = TARGET_SHA,
) -> dict[str, Path]:
    compose = _write(
        tmp_path / "candidate-compose.yml",
        """
        services:
          app:
            image: v122-app
            environment:
              MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED: ${MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED:-false}
              MAINTENANCE_COLLECTION_CANARY_PROJECT_ID: ${MAINTENANCE_COLLECTION_CANARY_PROJECT_ID:-}
          frontend:
            image: v122-frontend
          db:
            image: postgres:15
        """,
    )
    source = tmp_path / "source.tar"
    with tarfile.open(
        source,
        "w",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": target_sha},
    ) as archive:
        for tool in (MANIFEST, BUILD, REHEARSE, RELEASE, STATIC_TEST):
            content = tool.read_bytes()
            info = tarfile.TarInfo(f"source/.deploy/{tool.name}")
            info.mode = 0o700
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    source.chmod(0o600)
    images = _write(tmp_path / "images.tar", b"image-bundle")
    sbom = _json_artifact(
        tmp_path / "dependency-sbom.cdx.json",
        {"bomFormat": "CycloneDX", "specVersion": "1.5", "components": [{"name": "app"}]},
    )
    build = _json_artifact(
        tmp_path / "build-evidence.json",
        {
            "format": "v122-collection-reminders-build-v2",
            "target_sha": target_sha,
            "app_image_id": IMAGE_ID,
            "frontend_image_id": FRONTEND_IMAGE_ID,
            "source_tar_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "source_archive_commit": target_sha,
            "image_bundle_sha256": hashlib.sha256(images.read_bytes()).hexdigest(),
            "candidate_compose_sha256": hashlib.sha256(compose.read_bytes()).hexdigest(),
        },
    )
    ci = _json_artifact(
        tmp_path / "ci-evidence.json",
        {
            "format": "v122-collection-reminders-ci-v1",
            "target_sha": target_sha,
            "required_checks": {
                "后端测试（pytest + 迁移链验证）": "success",
                "前端类型检查 + 构建": "success",
            },
        },
    )
    return {
        "compose": compose,
        "contract": _contract(tmp_path / "contract.yaml", final=final_contract),
        "sbom": sbom,
        "build": build,
        "source": source,
        "images": images,
        "ci": ci,
    }


def _build_package(
    tmp_path: Path,
    *,
    final_contract: bool = False,
    target_sha: str = TARGET_SHA,
) -> Path:
    artifacts = _package_inputs(
        tmp_path / "inputs",
        final_contract=final_contract,
        target_sha=target_sha,
    )
    package = tmp_path / "package"
    command = [
        "python3", str(MANIFEST), "build",
        "--target-sha", target_sha,
        "--parent-production-sha", PARENT_SHA,
        "--app-image-id", IMAGE_ID,
        "--frontend-image-id", FRONTEND_IMAGE_ID,
        "--database-image-id", DB_IMAGE_ID,
        "--previous-app-image-id", "sha256:" + "8" * 64,
        "--previous-frontend-image-id", "sha256:" + "9" * 64,
        "--candidate-compose", str(artifacts["compose"]),
        "--contract", str(artifacts["contract"]),
        "--sbom", str(artifacts["sbom"]),
        "--build-evidence", str(artifacts["build"]),
        "--source-bundle", str(artifacts["source"]),
        "--image-bundle", str(artifacts["images"]),
        "--ci-evidence", str(artifacts["ci"]),
        "--canary-project-id", CANARY_PROJECT_ID,
        "--output", str(package),
    ]
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    return package


def _verify_package(package: Path, *, expected_ok: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["python3", str(MANIFEST), "verify", str(package)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    assert (completed.returncode == 0) is expected_ok, completed.stderr
    return completed


def test_manifest_builds_flat_portable_self_verifying_preliminary_package(tmp_path: Path):
    package = _build_package(tmp_path)
    names = {path.name for path in package.iterdir()}
    assert names == {
        "build-evidence.json",
        "candidate-compose.yml",
        "ci-evidence.json",
        "contract.yaml",
        "dependency-sbom.cdx.json",
        "images.tar",
        "manifest.json",
        "manifest.sha256",
        "source.tar",
        "v122_collection_reminders_build.sh",
        "v122_collection_reminders_manifest.py",
        "v122_collection_reminders_rehearse.sh",
        "v122_collection_reminders_release.sh",
        "v122_collection_reminders_static_test.py",
    }
    assert all(path.is_file() and not path.is_symlink() for path in package.iterdir())
    payload = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert payload["production_ready"] is False
    assert payload["contract"]["state"] == "approved_for_implementation"
    assert all("/" not in entry["path"] for entry in payload["artifacts"].values())
    _verify_package(package)


@pytest.mark.parametrize("artifact", [
    "candidate-compose.yml",
    "contract.yaml",
    "dependency-sbom.cdx.json",
    "build-evidence.json",
    "source.tar",
    "images.tar",
    "ci-evidence.json",
    "v122_collection_reminders_manifest.py",
    "v122_collection_reminders_release.sh",
])
def test_manifest_verify_rejects_missing_or_tampered_artifacts(tmp_path: Path, artifact: str):
    package = _build_package(tmp_path / "missing")
    (package / artifact).unlink()
    _verify_package(package, expected_ok=False)

    package = _build_package(tmp_path / "tampered")
    with (package / artifact).open("ab") as stream:
        stream.write(b"tamper")
    _verify_package(package, expected_ok=False)


def test_manifest_rejects_duplicate_contract_keys(tmp_path: Path):
    artifacts = _package_inputs(tmp_path / "inputs")
    _contract(artifacts["contract"], duplicate=True)
    command = [
        "python3", str(MANIFEST), "build",
        "--target-sha", TARGET_SHA,
        "--parent-production-sha", PARENT_SHA,
        "--app-image-id", IMAGE_ID,
        "--frontend-image-id", FRONTEND_IMAGE_ID,
        "--database-image-id", DB_IMAGE_ID,
        "--previous-app-image-id", "sha256:" + "8" * 64,
        "--previous-frontend-image-id", "sha256:" + "9" * 64,
        "--candidate-compose", str(artifacts["compose"]),
        "--contract", str(artifacts["contract"]),
        "--sbom", str(artifacts["sbom"]),
        "--build-evidence", str(artifacts["build"]),
        "--source-bundle", str(artifacts["source"]),
        "--image-bundle", str(artifacts["images"]),
        "--ci-evidence", str(artifacts["ci"]),
        "--canary-project-id", CANARY_PROJECT_ID,
        "--output", str(tmp_path / "package"),
    ]
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True)
    assert completed.returncode != 0
    assert "duplicate contract key" in completed.stderr


def _fake_sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _rehearsal(
    path: Path,
    *,
    package: Path,
    target_sha: str = TARGET_SHA,
    stage: str,
) -> Path:
    payload = json.loads((package / "manifest.json").read_text())
    return _json_artifact(
        path,
        {
            "format": "v122-collection-reminders-rehearsal-v2",
            "stage": stage,
            "success": True,
            "target_sha": target_sha,
            "parent_production_sha": PARENT_SHA,
            "from_revision": "d9f1a3c7e5b2",
            "to_revision": "c8e2a4f6b1d3",
            "database_image_id": payload["database"]["image_id"],
            "app_image_id": payload["images"]["app_image_id"],
            "frontend_image_id": payload["images"]["frontend_image_id"],
            "package_manifest_sha256": hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest(),
            "contract_sha256": payload["contract"]["sha256"],
            "candidate_compose_sha256": payload["artifacts"]["compose"]["sha256"],
            "db_dump_sha256": _fake_sha(stage + "-db-dump"),
            "globals_sha256": _fake_sha(stage + "-globals"),
            "uploads_archive_sha256": _fake_sha(stage + "-uploads"),
            "backup_manifest_sha256": _fake_sha(stage + "-backup-manifest"),
            "backup_checksums_sha256": _fake_sha(stage + "-backup-checksums"),
            "uploads_restore_sha256": _fake_sha(stage + "-uploads-restore"),
            "db_uploads_consistency_sha256": _fake_sha(stage + "-db-uploads"),
            "invariants_sha256": _fake_sha(stage + "-invariants"),
            "parser_result_sha256": _fake_sha(stage + "-parser"),
            "http_preview_summary_sha256": _fake_sha(stage + "-preview"),
            "http_apply_summary_sha256": _fake_sha(stage + "-apply"),
            "sample_xls_sha256": REAL_SAMPLE_SHA256,
            "parser_project_count": 3,
            "parser_milestone_count": 19,
            "db_restore": True,
            "globals_restore": True,
            "uploads_restore_verified": True,
            "db_uploads_references_complete": True,
            "preview_zero_domain_write": True,
            "synthetic_apply_verified": True,
        },
    )


def _minimal_unbound_rehearsal(path: Path, *, target_sha: str = TARGET_SHA, stage: str) -> Path:
    return _json_artifact(
        path,
        {
            "format": "v122-collection-reminders-rehearsal-v2",
            "stage": stage,
            "success": True,
            "target_sha": target_sha,
            "parent_production_sha": PARENT_SHA,
            "from_revision": "d9f1a3c7e5b2",
            "to_revision": "c8e2a4f6b1d3",
            "db_restore": True,
            "globals_restore": True,
            "uploads_restore_verified": True,
            "db_uploads_references_complete": True,
            "preview_zero_domain_write": True,
            "synthetic_apply_verified": True,
        },
    )


def test_finalize_requires_promoted_contract_and_two_bound_rehearsals(tmp_path: Path):
    preliminary = _build_package(tmp_path / "preliminary")
    prelim_rehearsal = _rehearsal(
        tmp_path / "preliminary-rehearsal.json",
        package=preliminary,
        stage="preliminary",
    )
    final_candidate = _build_package(
        tmp_path / "candidate",
        final_contract=True,
        target_sha=FINAL_TARGET_SHA,
    )
    final_rehearsal = _rehearsal(
        tmp_path / "final-rehearsal.json",
        package=final_candidate,
        target_sha=FINAL_TARGET_SHA,
        stage="final",
    )
    final_package = tmp_path / "final-package"

    completed = subprocess.run(
        [
            "python3", str(MANIFEST), "finalize", str(preliminary), str(final_candidate),
            "--preliminary-rehearsal", str(prelim_rehearsal),
            "--final-rehearsal", str(final_rehearsal),
            "--output", str(final_package),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
    _verify_package(final_package)
    subprocess.run(
        ["python3", str(MANIFEST), "preflight", str(final_package)],
        cwd=REPO_ROOT,
        check=True,
    )
    payload = json.loads((final_package / "manifest.json").read_text(encoding="utf-8"))
    assert payload["production_ready"] is True
    assert payload["contract"]["state"] == "approved_for_production_candidate"
    assert {"preliminary-rehearsal.json", "final-rehearsal.json"} <= {
        entry["path"] for entry in payload["artifacts"].values()
    }
    final_rehearsal = json.loads((final_package / "final-rehearsal.json").read_text())
    assert final_rehearsal["sample_xls_sha256"] == REAL_SAMPLE_SHA256
    assert final_rehearsal["parser_project_count"] == 3
    assert final_rehearsal["parser_milestone_count"] == 19
    for key in (
        "package_manifest_sha256",
        "contract_sha256",
        "candidate_compose_sha256",
        "db_dump_sha256",
        "globals_sha256",
        "uploads_archive_sha256",
        "backup_manifest_sha256",
        "backup_checksums_sha256",
        "uploads_restore_sha256",
        "db_uploads_consistency_sha256",
        "invariants_sha256",
        "parser_result_sha256",
        "http_preview_summary_sha256",
        "http_apply_summary_sha256",
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", final_rehearsal[key])


def test_finalize_rejects_false_contract_and_wrong_sha_rehearsal(tmp_path: Path):
    preliminary = _build_package(tmp_path / "preliminary")
    false_candidate = _build_package(
        tmp_path / "false-candidate",
        final_contract=False,
        target_sha=FINAL_TARGET_SHA,
    )
    final_candidate = _build_package(
        tmp_path / "final-candidate",
        final_contract=True,
        target_sha=FINAL_TARGET_SHA,
    )
    prelim_rehearsal = _rehearsal(
        tmp_path / "preliminary-rehearsal.json",
        package=preliminary,
        stage="preliminary",
    )
    wrong_final = _rehearsal(
        tmp_path / "wrong-final-rehearsal.json",
        package=final_candidate,
        target_sha="7" * 40,
        stage="final",
    )
    for candidate, final in (
        (false_candidate, prelim_rehearsal),
        (final_candidate, wrong_final),
    ):
        completed = subprocess.run(
            [
                "python3", str(MANIFEST), "finalize", str(preliminary), str(candidate),
                "--preliminary-rehearsal", str(prelim_rehearsal),
                "--final-rehearsal", str(final),
                "--output", str(tmp_path / f"out-{candidate.parent.name}-{final.stem}"),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
        )
        assert completed.returncode != 0


def test_finalize_rejects_unbound_handwritten_rehearsal_evidence(tmp_path: Path):
    preliminary = _build_package(tmp_path / "preliminary")
    candidate = _build_package(
        tmp_path / "candidate",
        final_contract=True,
        target_sha=FINAL_TARGET_SHA,
    )
    prelim_rehearsal = _minimal_unbound_rehearsal(
        tmp_path / "preliminary-rehearsal.json",
        stage="preliminary",
    )
    final_rehearsal = _minimal_unbound_rehearsal(
        tmp_path / "final-rehearsal.json",
        target_sha=FINAL_TARGET_SHA,
        stage="final",
    )

    completed = subprocess.run(
        [
            "python3", str(MANIFEST), "finalize", str(preliminary), str(candidate),
            "--preliminary-rehearsal", str(prelim_rehearsal),
            "--final-rehearsal", str(final_rehearsal),
            "--output", str(tmp_path / "production-package"),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0
    assert "rehearsal evidence" in completed.stderr


def test_build_gate_rejects_untracked_worktree_before_any_docker_build(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "release@test.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Release Test"], cwd=repo, check=True)
    _write(repo / "tracked", "ok\n")
    subprocess.run(["git", "add", "tracked"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", sha], cwd=repo, check=True)
    _write(repo / "untracked", "must block\n")

    completed = subprocess.run(
        [str(BUILD), str(repo), sha, str(tmp_path / "output")],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    assert completed.returncode != 0
    assert "worktree is not completely clean" in completed.stderr


@pytest.mark.parametrize(
    ("name", "member_type", "linkname"),
    [
        ("../escape", tarfile.REGTYPE, ""),
        ("/absolute", tarfile.REGTYPE, ""),
        ("escape-symlink", tarfile.SYMTYPE, "../../outside"),
        ("escape-hardlink", tarfile.LNKTYPE, "../../outside"),
        ("character-device", tarfile.CHRTYPE, ""),
        ("named-pipe", tarfile.FIFOTYPE, ""),
    ],
)
def test_rehearsal_rejects_unsafe_tar_before_docker(
    tmp_path: Path,
    name: str,
    member_type: bytes,
    linkname: str,
):
    db_dump = _write(tmp_path / "postgres_custom.dump", b"dump")
    _write(tmp_path / "postgres_globals.sql", "-- globals\n")
    uploads = tmp_path / "uploads.tar"
    with tarfile.open(uploads, "w") as archive:
        info = tarfile.TarInfo(name)
        info.type = member_type
        info.linkname = linkname
        if member_type == tarfile.REGTYPE:
            content = b"attack"
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        else:
            archive.addfile(info)
    uploads.chmod(0o600)
    _write(
        tmp_path / "sha256sums",
        f"{hashlib.sha256(db_dump.read_bytes()).hexdigest()}  postgres_custom.dump\n"
        f"{hashlib.sha256((tmp_path / 'postgres_globals.sql').read_bytes()).hexdigest()}  postgres_globals.sql\n"
        f"{hashlib.sha256(uploads.read_bytes()).hexdigest()}  uploads.tar\n",
    )
    compose = _write(tmp_path / "candidate-compose.yml", "services: {}\n")
    calls = tmp_path / "docker-calls"
    stub = tmp_path / "bin"
    _write(
        stub / "docker",
        f"""
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> {calls}
        exit 97
        """,
        mode=0o700,
    )
    completed = subprocess.run(
        [
            str(REHEARSE), str(db_dump), str(uploads), TARGET_SHA, PARENT_SHA,
            DB_IMAGE_ID, IMAGE_ID, FRONTEND_IMAGE_ID, str(compose),
            str(tmp_path / "out"),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": f"{stub}:/usr/bin:/bin"},
    )
    assert completed.returncode != 0
    assert "unsafe uploads archive member" in completed.stderr
    assert not calls.exists()


def test_rehearsal_preview_zero_write_is_derived_from_actual_http_preview():
    script = _script(REHEARSE)

    assert "HTTP_PREVIEW_DOMAIN_BEFORE" in script
    assert "HTTP_PREVIEW_DOMAIN_AFTER" in script
    assert "preview_zero_domain_write\": http_preview_before == http_preview_after" in script
    assert '"domain_write_count": 0' not in script


def test_rehearsal_upload_tree_digest_includes_mtime_metadata():
    script = _script(REHEARSE)

    assert "st_mtime_ns" in script


def test_rehearsal_apply_request_never_persists_login_token():
    script = _script(REHEARSE)

    assert '"token": token' not in script
    assert '"token": login' not in script
    assert '"body": apply_body' in script
    assert "/spec.json" in script


def _production_package(tmp_path: Path) -> Path:
    preliminary = _build_package(tmp_path / "preliminary")
    candidate = _build_package(
        tmp_path / "candidate",
        final_contract=True,
        target_sha=FINAL_TARGET_SHA,
    )
    prelim_rehearsal = _rehearsal(
        tmp_path / "preliminary-rehearsal.json",
        package=preliminary,
        target_sha=TARGET_SHA,
        stage="preliminary",
    )
    final_rehearsal = _rehearsal(
        tmp_path / "final-rehearsal.json",
        package=candidate,
        target_sha=FINAL_TARGET_SHA,
        stage="final",
    )
    package = tmp_path / "production-package"
    completed = subprocess.run(
        [
            "python3", str(MANIFEST), "finalize", str(preliminary), str(candidate),
            "--preliminary-rehearsal", str(prelim_rehearsal),
            "--final-rehearsal", str(final_rehearsal),
            "--output", str(package),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
    return package


def _release_test_env(
    tmp_path: Path,
    package: Path,
    *,
    docker_body: str = "exit 97",
) -> tuple[dict[str, str], Path, Path, Path]:
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True)
    shutil.copy2(package / "candidate-compose.yml", app_dir / "docker-compose.yml")
    (app_dir / "docker-compose.yml").chmod(0o600)
    _write(
        app_dir / ".env",
        f"""
        MAINTENANCE_BETA_ENABLED=true
        MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false
        MAINTENANCE_COLLECTION_CANARY_PROJECT_ID={CANARY_PROJECT_ID}
        """,
    )
    root_state = _json_artifact(
        tmp_path / "root-release-state.json",
        {
            "format": "it-spareparts-production-state-v1",
            "production_sha": PARENT_SHA,
            "compose_sha256": hashlib.sha256((app_dir / "docker-compose.yml").read_bytes()).hexdigest(),
            "app_image_id": "sha256:" + "8" * 64,
            "frontend_image_id": "sha256:" + "9" * 64,
            "database_image_id": DB_IMAGE_ID,
        },
    )
    evidence = tmp_path / "evidence"
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    calls = tmp_path / "docker-calls"
    stub = tmp_path / "bin"
    _write(
        stub / "docker",
        f"""
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> {calls}
        {docker_body}
        """,
        mode=0o700,
    )
    manifest_sha = hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest()
    env = {
        **os.environ,
        "PATH": f"{stub}:/usr/bin:/bin",
        "V122_TEST_MODE": "1",
        "V122_APP_DIR": str(app_dir),
        "V122_ROOT_RELEASE_STATE": str(root_state),
        "V122_BACKUP_ROOT": str(backup_root),
        "V122_BACKUP_GENERATION": "20260815T010203Z-test",
        "V122_EXPECTED_MANIFEST_SHA256": manifest_sha,
        "V122_GLOBAL_LOCK_FILE": str(tmp_path / "v122-global.lock"),
    }
    return env, evidence, calls, backup_root


def _write_release_state(evidence: Path, package: Path, *, phase: str, generation: int = 1, **extra):
    evidence.mkdir(parents=True, exist_ok=True)
    manifest_sha = hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest()
    payload = {
        "format": "v122-collection-reminders-release-state-v2",
        "manifest_sha256": manifest_sha,
        "target_sha": json.loads((package / "manifest.json").read_text())["target_sha"],
        "parent_production_sha": PARENT_SHA,
        "package_dir": str(package.resolve()),
        "phase": phase,
        "generation": generation,
        **extra,
    }
    _json_artifact(evidence / "release-state.json", payload)


def test_release_phase_order_rejects_skip_repeat_and_regression_without_docker(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package)
    release = package / "v122_collection_reminders_release.sh"

    skipped = subprocess.run(
        [str(release), str(package), str(evidence), "backup"],
        text=True,
        capture_output=True,
        env=env,
    )
    assert skipped.returncode != 0
    assert "phase" in skipped.stderr.lower()
    assert not calls.exists()


def test_release_preflight_uses_defined_safe_file_and_records_state(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ \"$*\" == *\"compose\"* && \"$*\" == *\"config -q\"* ]]; then exit 0; fi
    exit 97
    """
    env, evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "preflight"],
        text=True,
        capture_output=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert "config -q" in calls.read_text()
    assert json.loads((evidence / "release-state.json").read_text())["phase"] == "preflight"


def test_preliminary_package_can_reach_restore_gate_but_not_production_actions(tmp_path: Path):
    package = _build_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"config -q"* ]]; then exit 0; fi
    exit 97
    """
    env, evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    release = package / "v122_collection_reminders_release.sh"
    preflight = subprocess.run(
        [str(release), str(package), str(evidence), "preflight"],
        text=True, capture_output=True, env=env,
    )
    assert preflight.returncode == 0, preflight.stderr
    for phase, command, args in (
        ("restore_checked", "migrate", []),
        ("migrated", "deploy", []),
        ("deployed", "canary", [CANARY_PROJECT_ID, str(_json_artifact(tmp_path / "canary.json", {}))]),
        ("canary", "observe", ["0"]),
    ):
        _write_release_state(evidence, package, phase=phase)
        before = calls.read_bytes()
        completed = subprocess.run(
            [str(release), str(package), str(evidence), command, *args],
            text=True, capture_output=True, env=env,
        )
        assert completed.returncode != 0
        assert "production-ready" in completed.stderr
        assert calls.read_bytes() == before


def test_release_lock_is_global_not_split_by_evidence_directory(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    env, _evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package)
    lock = env["V122_GLOBAL_LOCK_FILE"]
    holder = subprocess.Popen(
        ["bash", "-c", 'exec 9>"$1"; flock -x 9; echo locked; read -r _', "holder", lock],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        completed = subprocess.run(
            [str(package / "v122_collection_reminders_release.sh"), str(package), str(tmp_path / "other-evidence"), "preflight"],
            text=True, capture_output=True, env=env,
        )
        assert completed.returncode != 0
        assert "global" in completed.stderr.lower() or "lock" in completed.stderr.lower()
        assert not calls.exists()
    finally:
        if holder.stdin:
            holder.stdin.write("done\n")
            holder.stdin.flush()
        holder.wait(timeout=5)


@pytest.mark.parametrize("field", ["app_image_id", "frontend_image_id", "database_image_id"])
def test_preflight_binds_all_previous_production_images(tmp_path: Path, field: str):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body="exit 0")
    root_state = Path(env["V122_ROOT_RELEASE_STATE"])
    payload = json.loads(root_state.read_text())
    payload[field] = "sha256:" + "0" * 64
    root_state.write_text(json.dumps(payload) + "\n")
    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "preflight"],
        text=True, capture_output=True, env=env,
    )
    assert completed.returncode != 0
    assert field in completed.stderr
    assert not calls.exists()


def test_release_freeze_closes_apply_stops_app_and_persists_frozen_state(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ \"$*\" == *\"compose\"* && \"$*\" == *\"ps -q db\"* ]]; then echo db-cid; exit 0; fi
    if [[ \"$*\" == *\"to_regclass\"* && \"$*\" == *\"maintenance_collection_plan_import_batch\"* ]]; then echo t; exit 0; fi
    if [[ \"$*\" == *\"maintenance_collection_plan_import_batch\"* && \"$*\" == *\"status = 'processing'\"* ]]; then echo 0; exit 0; fi
    if [[ \"$*\" == *\"compose\"* && \"$*\" == *\"stop app\"* ]]; then exit 0; fi
    if [[ \"$*\" == *\"compose\"* && \"$*\" == *\"ps -q app\"* ]]; then exit 0; fi
    exit 0
    """
    env, evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    _write_release_state(evidence, package, phase="preflight")
    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "freeze-writes"],
        text=True, capture_output=True, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false" in (Path(env["V122_APP_DIR"]) / ".env").read_text()
    assert "stop app" in calls.read_text()
    assert "maintenance_collection_plan_import_batch" in calls.read_text()
    assert json.loads((evidence / "release-state.json").read_text())["phase"] == "frozen"


def test_release_freeze_treats_missing_import_table_as_zero_processing(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"stop app"* ]]; then exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q app"* ]]; then exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
    if [[ "$*" == *"to_regclass"* && "$*" == *"maintenance_collection_plan_import_batch"* ]]; then echo f; exit 0; fi
    if [[ "$*" == *"status = 'processing'"* ]]; then exit 97; fi
    exit 97
    """
    env, evidence, calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body=docker,
    )
    _write_release_state(evidence, package, phase="preflight")

    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "freeze-writes"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    call_text = calls.read_text()
    assert "to_regclass" in call_text
    assert "status = 'processing'" not in call_text
    state = json.loads((evidence / "release-state.json").read_text())
    assert state["phase"] == "frozen"
    assert state["processing_batches"] == "0"


def _freeze_query_failure_docker(*, probe: str, processing: str) -> str:
    return f"""
    if [[ "$*" == *"compose"* && "$*" == *"stop app"* ]]; then exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q app"* ]]; then
      if [ -f "$V122_TEST_RESTORED_FLAG" ]; then echo app-cid; fi
      exit 0
    fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q frontend"* ]]; then
      if [ -f "$V122_TEST_RESTORED_FLAG" ]; then echo frontend-cid; fi
      exit 0
    fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
    if [[ "$*" == *"to_regclass"* && "$*" == *"maintenance_collection_plan_import_batch"* ]]; then {probe}; fi
    if [[ "$*" == *"status = 'processing'"* ]]; then {processing}; fi
    if [[ "$*" == *"image inspect"* ]]; then echo "${{!#}}"; exit 0; fi
    if [[ "$*" == *"tag "* ]]; then exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"up --no-deps --no-build --force-recreate -d app frontend"* ]]; then
      touch "$V122_TEST_RESTORED_FLAG"
      exit 0
    fi
    if [[ "$*" == *"inspect"* && "$*" == *"app-cid"* ]]; then echo "$V122_TEST_APP_CONTAINER_IMAGE"; exit 0; fi
    if [[ "$*" == *"inspect"* && "$*" == *"frontend-cid"* ]]; then echo "$V122_TEST_FRONTEND_CONTAINER_IMAGE"; exit 0; fi
    exit 97
    """


def _run_freeze_query_failure(
    tmp_path: Path,
    *,
    probe: str,
    processing: str,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, dict[str, str]]:
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, _backup_root = _release_test_env(
        tmp_path / "runtime",
        package,
        docker_body=_freeze_query_failure_docker(probe=probe, processing=processing),
    )
    env["V122_TEST_RESTORED_FLAG"] = str(tmp_path / "restored")
    env["V122_TEST_APP_CONTAINER_IMAGE"] = "sha256:" + "8" * 64
    env["V122_TEST_FRONTEND_CONTAINER_IMAGE"] = "sha256:" + "9" * 64
    _write_release_state(evidence, package, phase="preflight")
    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "freeze-writes"],
        text=True,
        capture_output=True,
        env=env,
    )
    return completed, evidence, calls, env


def test_release_freeze_refuses_when_import_batch_is_processing_and_restores(tmp_path: Path):
    completed, evidence, calls, env = _run_freeze_query_failure(
        tmp_path,
        probe="echo t; exit 0",
        processing="echo 2; exit 0",
    )

    assert completed.returncode != 0
    assert "still processing" in completed.stderr
    assert json.loads((evidence / "release-state.json").read_text())["phase"] == "preflight"
    call_text = calls.read_text()
    assert f"tag {env['V122_TEST_APP_CONTAINER_IMAGE']} it-spareparts-app:latest" in call_text
    assert f"tag {env['V122_TEST_FRONTEND_CONTAINER_IMAGE']} it-spareparts-frontend:latest" in call_text
    assert "up --no-deps --no-build --force-recreate -d app frontend" in call_text


@pytest.mark.parametrize(
    ("probe", "processing"),
    [
        ("exit 97", "exit 98"),
        ("echo t; exit 0", "exit 98"),
        ("echo true; exit 0", "exit 98"),
        ("echo t; exit 0", "echo not-a-number; exit 0"),
    ],
)
def test_release_freeze_fails_closed_and_restores_on_query_error_or_invalid_output(
    tmp_path: Path,
    probe: str,
    processing: str,
):
    completed, evidence, calls, env = _run_freeze_query_failure(
        tmp_path,
        probe=probe,
        processing=processing,
    )

    assert completed.returncode != 0
    assert "could not check collection import processing" in completed.stderr
    assert json.loads((evidence / "release-state.json").read_text())["phase"] == "preflight"
    assert "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false" in Path(env["V122_APP_DIR"]).joinpath(".env").read_text()
    call_text = calls.read_text()
    assert f"tag {env['V122_TEST_APP_CONTAINER_IMAGE']} it-spareparts-app:latest" in call_text
    assert f"tag {env['V122_TEST_FRONTEND_CONTAINER_IMAGE']} it-spareparts-frontend:latest" in call_text
    assert "up --no-deps --no-build --force-recreate -d app frontend" in call_text


def test_release_freeze_queries_are_error_stopping_and_validate_scalar_output():
    script = _script(RELEASE)
    freeze = script.rsplit("  freeze-writes)", 1)[1].split("  backup)", 1)[0]

    assert "to_regclass('public.maintenance_collection_plan_import_batch') IS NOT NULL" in freeze
    assert freeze.count("-v ON_ERROR_STOP=1") >= 2
    assert 'case "$IMPORT_TABLE_EXISTS" in' in freeze
    assert '[[ "$PROCESSING" =~ ^[0-9]+$ ]]' in freeze


def test_release_freeze_failure_restores_previous_images_and_keeps_apply_closed(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    old_app = "sha256:" + "8" * 64
    old_frontend = "sha256:" + "9" * 64
    docker = """
    if [[ "$*" == *"image inspect"* ]]; then echo "${!#}"; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"stop app"* ]]; then exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q app"* ]]; then echo app-cid; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q frontend"* ]]; then echo frontend-cid; exit 0; fi
    if [[ "$*" == *"inspect"* && "$*" == *"app-cid"* ]]; then echo "$V122_TEST_APP_CONTAINER_IMAGE"; exit 0; fi
    if [[ "$*" == *"inspect"* && "$*" == *"frontend-cid"* ]]; then echo "$V122_TEST_FRONTEND_CONTAINER_IMAGE"; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then exit 0; fi
    exit 97
    """
    env, evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    env["V122_TEST_APP_CONTAINER_IMAGE"] = old_app
    env["V122_TEST_FRONTEND_CONTAINER_IMAGE"] = old_frontend
    _write_release_state(evidence, package, phase="preflight")

    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "freeze-writes"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false" in (Path(env["V122_APP_DIR"]) / ".env").read_text()
    call_text = calls.read_text()
    assert f"tag {old_app} it-spareparts-app:latest" in call_text
    assert f"tag {old_frontend} it-spareparts-frontend:latest" in call_text
    assert "up --no-deps --no-build --force-recreate -d app frontend" in call_text


def test_release_migrate_uses_exact_target_app_image_without_build_and_verifies_head(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    migrated_flag = tmp_path / "migrated-flag"
    docker = f"""
    if [[ \"$*\" == *\"compose\"* && \"$*\" == *\"ps -q db\"* ]]; then echo db-cid; exit 0; fi
    if [[ \"$*\" == *\"compose\"* && \"$*\" == *\"stop app\"* ]]; then exit 0; fi
    if [[ \"$*\" == *\"compose\"* && \"$*\" == *\"ps -q app\"* ]]; then exit 0; fi
    if [[ \"$*\" == *\"image inspect\"* ]]; then echo \"${{!#}}\"; exit 0; fi
    if [[ \"$*\" == *\"exec db-cid psql\"* && \"$*\" == *\"SELECT version_num FROM alembic_version\"* ]]; then
      if [ -f {migrated_flag} ]; then echo c8e2a4f6b1d3; else echo d9f1a3c7e5b2; fi
      exit 0
    fi
    if [[ \"$*\" == *\"tag {IMAGE_ID} it-spareparts-app:latest\"* ]]; then exit 0; fi
    if [[ \"$*\" == *\"compose\"* && \"$*\" == *\"run --rm --no-deps --no-build\"* && \"$*\" == *\"alembic upgrade c8e2a4f6b1d3\"* ]]; then touch {migrated_flag}; exit 0; fi
    exit 97
    """
    env, evidence, calls, _backup_root = _release_test_env(
        tmp_path / "runtime",
        package,
        docker_body=docker,
    )
    _write_release_state(evidence, package, phase="restore_checked")
    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "migrate"],
        text=True,
        capture_output=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    call_text = calls.read_text()
    assert "stop app" in call_text
    assert f"tag {IMAGE_ID} it-spareparts-app:latest" in call_text
    assert "run --rm --no-deps --no-build" in call_text
    assert json.loads((evidence / "release-state.json").read_text())["phase"] == "migrated"


def test_release_deploy_and_rollback_retag_exact_image_ids_without_build(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ \"$*\" == *\"compose\"* && \"$*\" == *\"ps -q app\"* ]]; then echo app-cid; exit 0; fi
    if [[ \"$*\" == *\"compose\"* && \"$*\" == *\"ps -q frontend\"* ]]; then echo frontend-cid; exit 0; fi
    if [[ \"$*\" == *\"image inspect\"* ]]; then
      echo \"${!#}\"
      exit 0
    fi
    if [[ \"$*\" == *\"inspect\"* && \"$*\" == *\"app-cid\"* ]]; then echo \"$V122_TEST_APP_CONTAINER_IMAGE\"; exit 0; fi
    if [[ \"$*\" == *\"inspect\"* && \"$*\" == *\"frontend-cid\"* ]]; then echo \"$V122_TEST_FRONTEND_CONTAINER_IMAGE\"; exit 0; fi
    exit 0
    """
    env, evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    env["V122_TEST_APP_CONTAINER_IMAGE"] = IMAGE_ID
    env["V122_TEST_FRONTEND_CONTAINER_IMAGE"] = FRONTEND_IMAGE_ID
    _write_release_state(evidence, package, phase="migrated")
    release = package / "v122_collection_reminders_release.sh"
    deployed = subprocess.run([str(release), str(package), str(evidence), "deploy"], text=True, capture_output=True, env=env)
    assert deployed.returncode == 0, deployed.stderr
    call_text = calls.read_text()
    assert f"tag {IMAGE_ID} it-spareparts-app:latest" in call_text
    assert f"tag {FRONTEND_IMAGE_ID} it-spareparts-frontend:latest" in call_text
    assert "up --no-deps --no-build --force-recreate -d app frontend" in call_text
    assert json.loads((evidence / "release-state.json").read_text())["phase"] == "deployed"

    env["V122_TEST_APP_CONTAINER_IMAGE"] = "sha256:" + "8" * 64
    env["V122_TEST_FRONTEND_CONTAINER_IMAGE"] = "sha256:" + "9" * 64
    rolled = subprocess.run([str(release), str(package), str(evidence), "rollback-images"], text=True, capture_output=True, env=env)
    assert rolled.returncode == 0, rolled.stderr
    call_text = calls.read_text()
    assert "tag sha256:" + "8" * 64 + " it-spareparts-app:latest" in call_text
    assert "tag sha256:" + "9" * 64 + " it-spareparts-frontend:latest" in call_text
    assert "--no-build" in call_text
    assert "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false" in (Path(env["V122_APP_DIR"]) / ".env").read_text()
    rolled_state = json.loads((evidence / "release-state.json").read_text())
    assert "action_restore_evidence_sha256" not in rolled_state

def test_release_wrong_canary_fails_before_env_or_docker(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package)
    _write_release_state(evidence, package, phase="deployed")
    env_file = Path(env["V122_APP_DIR"]) / ".env"
    before = env_file.read_bytes()
    spec = _json_artifact(tmp_path / "canary.json", {"base_url": "https://invalid.test"})
    completed = subprocess.run(
        [
            str(package / "v122_collection_reminders_release.sh"),
            str(package), str(evidence), "canary",
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", str(spec),
        ],
        text=True,
        capture_output=True,
        env=env,
    )
    assert completed.returncode != 0
    assert "does not match manifest" in completed.stderr
    assert env_file.read_bytes() == before
    assert not calls.exists()


def test_canary_prestate_mismatch_fails_before_any_account_put(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"exec -T app"* ]]; then
      apply=$(sed -n 's/^MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=//p' "$V122_APP_DIR/.env")
      project=$(sed -n 's/^MAINTENANCE_COLLECTION_CANARY_PROJECT_ID=//p' "$V122_APP_DIR/.env")
      printf '%s\n%s\n' "$apply" "$project"; exit 0
    fi
    if [[ "$*" == *"compose"* && "$*" == *"up --no-deps --no-build"* ]]; then exit 0; fi
    exit 97
    """
    env, evidence, _calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body=docker,
    )
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    spec = _standard_canary_spec(workbook)
    mismatched = _action_account_rows(spec, "action_restore")
    mismatched[0] = {
        **mismatched[0],
        "overrides": {**mismatched[0]["overrides"], "data_customer": True},
    }
    curl_calls = tmp_path / "curl-calls"
    curl_stub = Path(env["PATH"].split(":", 1)[0]) / "curl"
    _write(
        curl_stub,
        f"""
        #!/usr/bin/env bash
        printf '%s\n' "$*" >> {curl_calls}
        out=''
        previous=''
        for arg in "$@"; do
          if [ "$previous" = output ]; then out=$arg; fi
          previous=''
          [ "$arg" = --output ] && previous=output
        done
        case "$out" in
          *action_verify_restored.response)
            printf '%s' '{json.dumps(mismatched, separators=(",", ":"))}' >"$out"; status=200 ;;
          *) printf '{{}}' >"$out"; status=200 ;;
        esac
        printf '%s' "$status"
        """,
        mode=0o700,
    )
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    _write_release_state(evidence, package, phase="deployed")

    completed = subprocess.run(
        [
            str(package / "v122_collection_reminders_release.sh"),
            str(package),
            str(evidence),
            "canary",
            CANARY_PROJECT_ID,
            str(spec_file),
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert "account overrides state mismatch" in completed.stderr
    assert "--request PUT" not in curl_calls.read_text()


def test_successful_canary_persists_exact_sealed_spec_sha256(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    event_log = tmp_path / "canary-events"
    env, evidence, _calls, _backup_root = _release_test_env(
        tmp_path / "runtime",
        package,
        docker_body=_full_canary_docker_body(event_log),
    )
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    spec = _standard_canary_spec(workbook)
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    expected_snapshot_sha = hashlib.sha256(spec_file.read_bytes()).hexdigest()
    replacement_spec = copy.deepcopy(spec)
    for list_name in ("action_grant", "action_restore"):
        for case in replacement_spec[list_name]:
            if case["path"] == "/api/accounts/importer":
                case["body"]["overrides"]["data_customer"] = True
    replacement_file = _json_artifact(tmp_path / "replacement-canary.json", replacement_spec)
    curl_calls = tmp_path / "curl-calls"
    snapshot_audit = tmp_path / "canary-snapshot-audit"
    _write_full_canary_curl_stub(
        Path(env["PATH"].split(":", 1)[0]) / "curl",
        controls=spec,
        curl_calls=curl_calls,
        event_log=event_log,
        replace_spec=(spec_file, replacement_file),
        snapshot_audit=snapshot_audit,
    )
    _write_release_state(evidence, package, phase="deployed")

    completed = subprocess.run(
        [
            str(package / "v122_collection_reminders_release.sh"),
            str(package),
            str(evidence),
            "canary",
            CANARY_PROJECT_ID,
            str(spec_file),
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    state = json.loads((evidence / "release-state.json").read_text())
    assert state["phase"] == "canary"
    assert state["canary_spec_sha256"] == expected_snapshot_sha
    canary_evidence = json.loads((evidence / "canary-evidence.json").read_text())
    assert canary_evidence["canary_spec_sha256"] == expected_snapshot_sha
    assert hashlib.sha256(spec_file.read_bytes()).hexdigest() != expected_snapshot_sha
    assert snapshot_audit.read_text().strip() == "700 600 regular"


@pytest.mark.parametrize("restore_verify_failure", ["transport", "status"])
def test_canary_cleanup_restarts_closed_app_before_failed_restore_verification(
    tmp_path: Path,
    restore_verify_failure: str,
):
    package = _production_package(tmp_path / "pkg")
    event_log = tmp_path / "canary-events"
    env, evidence, calls, _backup_root = _release_test_env(
        tmp_path / "runtime",
        package,
        docker_body=_full_canary_docker_body(event_log),
    )
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    spec = _standard_canary_spec(workbook)
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    curl_calls = tmp_path / "curl-calls"
    _write_full_canary_curl_stub(
        Path(env["PATH"].split(":", 1)[0]) / "curl",
        controls=spec,
        curl_calls=curl_calls,
        event_log=event_log,
        restore_verify_failure=restore_verify_failure,
        # Deliberately fail after /apply has succeeded and the flag is open.
        permission_code="wrong_permission_code",
    )
    _write_release_state(evidence, package, phase="deployed")

    completed = subprocess.run(
        [
            str(package / "v122_collection_reminders_release.sh"),
            str(package),
            str(evidence),
            "canary",
            CANARY_PROJECT_ID,
            str(spec_file),
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false" in (
        Path(env["V122_APP_DIR"]) / ".env"
    ).read_text()
    assert json.loads((evidence / "release-state.json").read_text())["phase"] == "deployed"
    events = event_log.read_text().splitlines()
    cleanup_restart = len(events) - 1 - events[::-1].index("restart:false")
    cleanup_readback = len(events) - 1 - events[::-1].index("readback:false")
    first_restore_put = events.index("restore-put")
    failed_restore_verify = events.index("verify-restored:2")
    assert cleanup_restart < cleanup_readback < first_restore_put < failed_restore_verify
    assert calls.read_text().count("up --no-deps --no-build --force-recreate -d app") == 3
    assert events.count("restore-put") == 3


def test_canary_grant_get_mismatch_triggers_full_restore(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"exec -T app"* ]]; then
      apply=$(sed -n 's/^MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=//p' "$V122_APP_DIR/.env")
      project=$(sed -n 's/^MAINTENANCE_COLLECTION_CANARY_PROJECT_ID=//p' "$V122_APP_DIR/.env")
      printf '%s\n%s\n' "$apply" "$project"; exit 0
    fi
    if [[ "$*" == *"compose"* && "$*" == *"up --no-deps --no-build"* ]]; then exit 0; fi
    exit 97
    """
    env, evidence, _calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body=docker,
    )
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    spec = _standard_canary_spec(workbook)
    restored = _action_account_rows(spec, "action_restore")
    granted = _action_account_rows(spec, "action_grant")
    granted[1] = {**granted[1], "overrides": {**granted[1]["overrides"], "data_supplier": False}}
    curl_calls = tmp_path / "curl-calls"
    curl_stub = Path(env["PATH"].split(":", 1)[0]) / "curl"
    _write(
        curl_stub,
        f"""
        #!/usr/bin/env bash
        printf '%s\n' "$*" >> {curl_calls}
        out=''
        data=''
        previous=''
        for arg in "$@"; do
          if [ "$previous" = output ]; then out=$arg; fi
          if [ "$previous" = data ]; then data="${{arg#@}}"; fi
          previous=''
          [ "$arg" = --output ] && previous=output
          [ "$arg" = --data-binary ] && previous=data
        done
        url=${{!#}}
        case "$out" in
          *action_verify_restored.response)
            printf '%s' '{json.dumps(restored, separators=(",", ":"))}' >"$out"; status=200 ;;
          *action_verify_granted.response)
            printf '%s' '{json.dumps(granted, separators=(",", ":"))}' >"$out"; status=200 ;;
          *action_grant-*.response|*action_restore-*.response)
            python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); json.dump({{"username":sys.argv[2],"overrides":body["overrides"]}},open(sys.argv[3],"w"))' "$data" "${{url##*/}}" "$out"; status=200 ;;
          *) printf '{{}}' >"$out"; status=200 ;;
        esac
        printf '%s' "$status"
        """,
        mode=0o700,
    )
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    _write_release_state(evidence, package, phase="deployed")

    completed = subprocess.run(
        [
            str(package / "v122_collection_reminders_release.sh"),
            str(package),
            str(evidence),
            "canary",
            CANARY_PROJECT_ID,
            str(spec_file),
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert "account overrides state mismatch" in completed.stderr
    call_lines = curl_calls.read_text().splitlines()
    assert len([line for line in call_lines if "action_grant-" in line]) == 3
    assert len([line for line in call_lines if "action_restore-" in line]) == 3


def test_rollback_rejects_different_valid_spec_before_account_put_or_image_change(
    tmp_path: Path,
):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body="exit 97",
    )
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    original_spec = _standard_canary_spec(workbook)
    original_file = _json_artifact(tmp_path / "original-canary.json", original_spec)
    replacement_spec = copy.deepcopy(original_spec)
    for list_name in ("action_grant", "action_restore"):
        for case in replacement_spec[list_name]:
            if case["path"] == "/api/accounts/importer":
                case["body"]["overrides"]["data_customer"] = True
    replacement_file = _json_artifact(tmp_path / "replacement-canary.json", replacement_spec)
    curl_calls = tmp_path / "curl-calls"
    _write_rollback_curl_stub(
        Path(env["PATH"].split(":", 1)[0]) / "curl",
        spec=replacement_spec,
        curl_calls=curl_calls,
    )
    _write_release_state(
        evidence,
        package,
        phase="canary",
        actions_granted=True,
        canary_spec_sha256=hashlib.sha256(original_file.read_bytes()).hexdigest(),
    )
    before_state = (evidence / "release-state.json").read_bytes()

    completed = subprocess.run(
        [
            str(package / "v122_collection_reminders_release.sh"),
            str(package),
            str(evidence),
            "rollback-images",
            str(replacement_file),
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert not curl_calls.exists()
    assert not calls.exists()
    assert (evidence / "release-state.json").read_bytes() == before_state


def test_rollback_uses_private_snapshot_when_original_spec_is_replaced_during_get(
    tmp_path: Path,
):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"image inspect --format"* ]]; then echo "${!#}"; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q app"* ]]; then echo app-cid; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q frontend"* ]]; then echo frontend-cid; exit 0; fi
    if [[ "$*" == *"inspect"* && "$*" == *"app-cid"* ]]; then echo "$V122_TEST_APP_CONTAINER_IMAGE"; exit 0; fi
    if [[ "$*" == *"inspect"* && "$*" == *"frontend-cid"* ]]; then echo "$V122_TEST_FRONTEND_CONTAINER_IMAGE"; exit 0; fi
    exit 0
    """
    env, evidence, calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body=docker,
    )
    env["V122_TEST_APP_CONTAINER_IMAGE"] = "sha256:" + "8" * 64
    env["V122_TEST_FRONTEND_CONTAINER_IMAGE"] = "sha256:" + "9" * 64
    rollback_tmp = tmp_path / "rollback-tmp"
    rollback_tmp.mkdir()
    env["TMPDIR"] = str(rollback_tmp)
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    spec = _standard_canary_spec(workbook)
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    expected_snapshot_sha = hashlib.sha256(spec_file.read_bytes()).hexdigest()
    replacement_spec = copy.deepcopy(spec)
    for list_name in ("action_grant", "action_restore"):
        for case in replacement_spec[list_name]:
            if case["path"] == "/api/accounts/importer":
                case["body"]["overrides"]["data_customer"] = True
    replacement_file = _json_artifact(tmp_path / "replacement-canary.json", replacement_spec)
    curl_calls = tmp_path / "curl-calls"
    snapshot_audit = tmp_path / "rollback-snapshot-audit"
    _write_rollback_curl_stub(
        Path(env["PATH"].split(":", 1)[0]) / "curl",
        spec=spec,
        curl_calls=curl_calls,
        replace_spec=(spec_file, replacement_file),
        snapshot_audit=snapshot_audit,
    )
    _write_release_state(
        evidence,
        package,
        phase="canary",
        actions_granted=True,
        canary_spec_sha256=expected_snapshot_sha,
    )

    completed = subprocess.run(
        [
            str(package / "v122_collection_reminders_release.sh"),
            str(package),
            str(evidence),
            "rollback-images",
            str(spec_file),
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    curl_text = curl_calls.read_text()
    assert "action_verify_granted" in curl_text
    assert curl_text.count("--request PUT") == 3
    assert "action_verify_restored" in curl_text
    assert "tag sha256:" + "8" * 64 + " it-spareparts-app:latest" in calls.read_text()
    state = json.loads((evidence / "release-state.json").read_text())
    assert state["phase"] == "rolled_back"
    assert state["canary_spec_sha256"] == expected_snapshot_sha
    restore_evidence = evidence / "action-restore-evidence.json"
    bound_restore_sha = hashlib.sha256(restore_evidence.read_bytes()).hexdigest()
    assert state["action_restore_evidence_sha256"] == bound_restore_sha
    restore_evidence.write_bytes(restore_evidence.read_bytes() + b"\n")
    assert hashlib.sha256(restore_evidence.read_bytes()).hexdigest() != bound_restore_sha
    assert hashlib.sha256(spec_file.read_bytes()).hexdigest() != expected_snapshot_sha
    assert snapshot_audit.read_text().strip() == "700 600 regular"
    assert list(rollback_tmp.iterdir()) == []


def test_rollback_snapshot_hash_inconsistency_fails_before_any_runtime_action(
    tmp_path: Path,
):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body="exit 97",
    )
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    spec = _standard_canary_spec(workbook)
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    original_sha = hashlib.sha256(spec_file.read_bytes()).hexdigest()
    replacement_spec = copy.deepcopy(spec)
    for list_name in ("action_grant", "action_restore"):
        for case in replacement_spec[list_name]:
            if case["path"] == "/api/accounts/importer":
                case["body"]["overrides"]["data_customer"] = True
    replacement_file = _json_artifact(tmp_path / "replacement-canary.json", replacement_spec)
    curl_calls = tmp_path / "curl-calls"
    _write_rollback_curl_stub(
        Path(env["PATH"].split(":", 1)[0]) / "curl",
        spec=spec,
        curl_calls=curl_calls,
    )
    _write(
        Path(env["PATH"].split(":", 1)[0]) / "sha256sum",
        """
        #!/usr/bin/env bash
        if [ "$#" -eq 1 ] && [ "$1" = "$V122_TEST_RACY_SPEC" ]; then
          /usr/bin/sha256sum "$@"
          cp -- "$V122_TEST_REPLACEMENT_SPEC" "$1"
          chmod 600 "$1"
          exit 0
        fi
        exec /usr/bin/sha256sum "$@"
        """,
        mode=0o700,
    )
    env["V122_TEST_RACY_SPEC"] = str(spec_file)
    env["V122_TEST_REPLACEMENT_SPEC"] = str(replacement_file)
    _write_release_state(
        evidence,
        package,
        phase="canary",
        actions_granted=True,
        canary_spec_sha256=original_sha,
    )
    env_file = Path(env["V122_APP_DIR"]) / ".env"
    env_file.write_text(
        env_file.read_text().replace(
            "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false",
            "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=true",
        )
    )
    before_env = env_file.read_bytes()
    before_state = (evidence / "release-state.json").read_bytes()

    completed = subprocess.run(
        [
            str(package / "v122_collection_reminders_release.sh"),
            str(package),
            str(evidence),
            "rollback-images",
            str(spec_file),
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert "snapshot" in completed.stderr.lower()
    assert not curl_calls.exists()
    assert not calls.exists()
    assert env_file.read_bytes() == before_env
    assert (evidence / "release-state.json").read_bytes() == before_state


def test_rollback_legacy_granted_state_without_spec_sha_fails_closed(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body="exit 97",
    )
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    spec = _standard_canary_spec(workbook)
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    curl_calls = tmp_path / "curl-calls"
    _write_rollback_curl_stub(
        Path(env["PATH"].split(":", 1)[0]) / "curl",
        spec=spec,
        curl_calls=curl_calls,
    )
    _write_release_state(evidence, package, phase="canary", actions_granted=True)
    before_state = (evidence / "release-state.json").read_bytes()

    completed = subprocess.run(
        [
            str(package / "v122_collection_reminders_release.sh"),
            str(package),
            str(evidence),
            "rollback-images",
            str(spec_file),
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert "canary spec SHA-256" in completed.stderr
    assert not curl_calls.exists()
    assert not calls.exists()
    assert (evidence / "release-state.json").read_bytes() == before_state


def test_rollback_live_grant_mismatch_fails_before_restore_put_or_image_change(
    tmp_path: Path,
):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body="exit 97",
    )
    rollback_tmp = tmp_path / "rollback-tmp"
    rollback_tmp.mkdir()
    env["TMPDIR"] = str(rollback_tmp)
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    spec = _standard_canary_spec(workbook)
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    mismatched_grant = _action_account_rows(spec, "action_grant")
    mismatched_grant[0] = {
        **mismatched_grant[0],
        "overrides": {**mismatched_grant[0]["overrides"], "data_customer": True},
    }
    curl_calls = tmp_path / "curl-calls"
    _write_rollback_curl_stub(
        Path(env["PATH"].split(":", 1)[0]) / "curl",
        spec=spec,
        curl_calls=curl_calls,
        granted_rows=mismatched_grant,
    )
    _write_release_state(
        evidence,
        package,
        phase="canary",
        actions_granted=True,
        canary_spec_sha256=hashlib.sha256(spec_file.read_bytes()).hexdigest(),
    )
    before_state = (evidence / "release-state.json").read_bytes()

    completed = subprocess.run(
        [
            str(package / "v122_collection_reminders_release.sh"),
            str(package),
            str(evidence),
            "rollback-images",
            str(spec_file),
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert "account overrides state mismatch" in completed.stderr
    assert "action_verify_granted" in curl_calls.read_text()
    assert "--request PUT" not in curl_calls.read_text()
    assert not calls.exists()
    assert (evidence / "release-state.json").read_bytes() == before_state
    assert list(rollback_tmp.iterdir()) == []


@pytest.mark.parametrize(
    "restore_verify_failure",
    ["transport", "status", "mismatch", "outcome"],
)
def test_rollback_restore_verification_failure_never_switches_images(
    tmp_path: Path,
    restore_verify_failure: str,
):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body="exit 97",
    )
    rollback_tmp = tmp_path / "rollback-tmp"
    rollback_tmp.mkdir()
    env["TMPDIR"] = str(rollback_tmp)
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    spec = _standard_canary_spec(workbook)
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    restored = _action_account_rows(spec, "action_restore")
    if restore_verify_failure == "mismatch":
        restored[-1] = {
            **restored[-1],
            "overrides": {**restored[-1]["overrides"], "data_customer": True},
        }
    curl_calls = tmp_path / "curl-calls"
    _write_rollback_curl_stub(
        Path(env["PATH"].split(":", 1)[0]) / "curl",
        spec=spec,
        curl_calls=curl_calls,
        restored_rows=restored,
        restore_verify_failure=restore_verify_failure,
    )
    _write_release_state(
        evidence,
        package,
        phase="canary",
        actions_granted=True,
        canary_spec_sha256=hashlib.sha256(spec_file.read_bytes()).hexdigest(),
    )
    before_state = (evidence / "release-state.json").read_bytes()

    completed = subprocess.run(
        [
            str(package / "v122_collection_reminders_release.sh"),
            str(package),
            str(evidence),
            "rollback-images",
            str(spec_file),
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    curl_text = curl_calls.read_text()
    assert "action_verify_granted" in curl_text
    assert curl_text.count("--request PUT") == 3
    assert not calls.exists()
    assert (evidence / "release-state.json").read_bytes() == before_state
    assert list(rollback_tmp.iterdir()) == []


@pytest.mark.parametrize("response_mismatch", ["username", "overrides"])
def test_canary_partial_grant_failure_attempts_all_puts_then_restores_in_reverse(
    tmp_path: Path,
    response_mismatch: str,
):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"exec -T app"* ]]; then
      apply=$(sed -n 's/^MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=//p' "$V122_APP_DIR/.env")
      project=$(sed -n 's/^MAINTENANCE_COLLECTION_CANARY_PROJECT_ID=//p' "$V122_APP_DIR/.env")
      printf '%s\n%s\n' "$apply" "$project"; exit 0
    fi
    if [[ "$*" == *"compose"* && "$*" == *"up --no-deps --no-build"* ]]; then exit 0; fi
    exit 97
    """
    env, evidence, _calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body=docker,
    )
    curl_calls = tmp_path / "curl-calls"
    curl_stub = Path(env["PATH"].split(":", 1)[0]) / "curl"
    controls = _action_control_cases(token="sealed-partial-grant-token")
    restored_rows = json.dumps(
        _action_account_rows(controls, "action_restore"),
        separators=(",", ":"),
    )
    _write(
        curl_stub,
        f"""
        #!/usr/bin/env bash
        printf '%s\n' "$*" >> {curl_calls}
        out=''
        data=''
        previous=''
        for arg in "$@"; do
          if [ "$previous" = output ]; then out=$arg; fi
          if [ "$previous" = data ]; then data="${{arg#@}}"; fi
          previous=''
          [ "$arg" = --output ] && previous=output
          [ "$arg" = --data-binary ] && previous=data
        done
        url=${{!#}}
        case "$out" in
          *action_verify_restored.response)
            printf '%s' '{restored_rows}' >"$out"; status=200 ;;
          *action_grant-*.response|*action_restore-*.response)
            python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); target=sys.argv[2]; overrides=body["overrides"]; mismatch="action_grant-001.response" in sys.argv[3]; target="wrong-account" if mismatch and sys.argv[4]=="username" else target; overrides={{}} if mismatch and sys.argv[4]=="overrides" else overrides; json.dump({{"username":target,"overrides":overrides}},open(sys.argv[3],"w"))' "$data" "${{url##*/}}" "$out" "{response_mismatch}"; status=200 ;;
          *) printf '{{}}' >"$out"; status=200 ;;
        esac
        printf '%s' "$status"
        """,
        mode=0o700,
    )
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    spec = _standard_canary_spec(workbook, token="sealed-partial-grant-token")
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    _write_release_state(evidence, package, phase="deployed")

    completed = subprocess.run(
        [
            str(package / "v122_collection_reminders_release.sh"),
            str(package),
            str(evidence),
            "canary",
            CANARY_PROJECT_ID,
            str(spec_file),
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert f"response {response_mismatch} mismatch" in completed.stderr
    assert "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false" in (
        Path(env["V122_APP_DIR"]) / ".env"
    ).read_text()
    call_lines = curl_calls.read_text().splitlines()
    grant_urls = [line.rsplit(" ", 1)[-1] for line in call_lines if "action_grant-" in line]
    restore_urls = [line.rsplit(" ", 1)[-1] for line in call_lines if "action_restore-" in line]
    assert grant_urls == [
        "https://canary.invalid/api/accounts/importer",
        "https://canary.invalid/api/accounts/follower",
        "https://canary.invalid/api/accounts/denied",
    ]
    assert restore_urls == list(reversed(grant_urls))
    assert all("--request PUT" in line for line in call_lines if "action_" in line and "-00" in line)
    assert "sealed-partial-grant-token" not in curl_calls.read_text()


@pytest.mark.parametrize(
    ("returned_included_in_total", "reaches_import"),
    [(None, True), (True, False)],
)
def test_canary_failure_closes_apply_restores_actions_and_keeps_secrets_out_of_evidence(
    tmp_path: Path,
    returned_included_in_total: bool | None,
    reaches_import: bool,
):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
    if [[ "$*" == *"SELECT milestone_id"* ]]; then echo milestone-canary-0001; exit 0; fi
    if [[ "$*" == *"maintenance_collection_milestone"* ]]; then echo '0:0:0:0'; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"exec -T app"* ]]; then
      apply=$(sed -n 's/^MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=//p' "$V122_APP_DIR/.env")
      project=$(sed -n 's/^MAINTENANCE_COLLECTION_CANARY_PROJECT_ID=//p' "$V122_APP_DIR/.env")
      printf '%s\n%s\n' "$apply" "$project"; exit 0
    fi
    if [[ "$*" == *"compose"* && "$*" == *"up --no-deps --no-build"* ]]; then exit 0; fi
    exit 97
    """
    env, evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    curl_calls = tmp_path / "curl-calls"
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    controls = _action_control_cases("super-secret-control-token")
    restored_rows = json.dumps(
        _action_account_rows(controls, "action_restore"), separators=(",", ":"),
    )
    granted_rows = json.dumps(
        _action_account_rows(controls, "action_grant"), separators=(",", ":"),
    )
    curl_stub = Path(env["PATH"].split(":", 1)[0]) / "curl"
    _write(
        curl_stub,
        f"""
        #!/usr/bin/env bash
        printf '%s\n' "$*" >> {curl_calls}
        out=''
        data=''
        previous=''
        for arg in "$@"; do
          if [ "$previous" = output ]; then out=$arg; fi
          if [ "$previous" = data ]; then data="${{arg#@}}"; fi
          previous=''
          [ "$arg" = --output ] && previous=output
          [ "$arg" = --data-binary ] && previous=data
        done
        url=${{!#}}
        case "$out" in
          *action_verify_restored.response)
            printf '%s' '{restored_rows}' >"$out"; status=200 ;;
          *action_verify_granted.response)
            printf '%s' '{granted_rows}' >"$out"; status=200 ;;
          *action_grant-*.response|*action_restore-*.response)
            python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); json.dump({{"username":sys.argv[2],"overrides":body["overrides"]}},open(sys.argv[3],"w"))' "$data" "${{url##*/}}" "$out"; status=200 ;;
          *login-follower.response.json)
            printf '%s' '{{"token":"follower-token","role":"user","permissions":{{"action_maintenance_collection_follow_up":true}}}}' >"$out"; status=200 ;;
          *login-importer.response.json)
            printf '%s' '{{"token":"importer-token","role":"admin","permissions":{{"action_maintenance_collection_plan_import":true}}}}' >"$out"; status=200 ;;
          *login-denied.response.json)
            printf '%s' '{{"token":"denied-token","role":"admin","permissions":{{"action_maintenance_collection_plan_import":false}}}}' >"$out"; status=200 ;;
          *setup_contract.response)
            printf '%s' '{{"project_id":"{CANARY_PROJECT_ID}","project_contract_id":"contract-live","version":3{',"included_in_total":true' if returned_included_in_total is True else ''}}}' >"$out"; status=201 ;;
          *cross_project_negative.response) printf '%s' '{{"detail":{{"code":"canary_scope_denied"}}}}' >"$out"; status=403 ;;
          *permission_negative.response) printf '%s' '{{"detail":{{"code":"permission_denied"}}}}' >"$out"; status=403 ;;
          *import_preview_positive.response)
            printf '%s' '{{"batch_id":"batch-canary","batch_version":7,"data_version":"data-v7","status":"valid","rows":[{{"external_order_no":"ORDER-1","row_key":"row-live"}}]}}' >"$out"; status=200 ;;
          *apply_last.response) printf '{{}}' >"$out"; status=500 ;;
          *) printf '{{}}' >"$out"; status=200 ;;
        esac
        printf '%s' "$status"
        """,
        mode=0o700,
    )
    spec = {
        "base_url": "https://canary.invalid",
        "named_accounts": {
            "follower": {"username": "follower", "password": "secret-zero", "expected_role": "user", "required_permissions": ["action_maintenance_collection_follow_up"]},
            "importer": {"username": "importer", "password": "secret-one", "expected_role": "admin", "required_permissions": ["action_maintenance_collection_plan_import"]},
            "denied": {"username": "denied", "password": "secret-two", "expected_role": "admin", "forbidden_permissions": ["action_maintenance_collection_plan_import", "action_maintenance_collection_follow_up"]},
        },
        **_action_control_cases("super-secret-control-token"),
        "setup_contract": _setup_contract_case(),
        "follow_up_positive": _follow_up_case(account="follower", milestone_id=DYNAMIC_CANARY_MILESTONE_ID, expected_status=200, key="follow-positive-0001"),
        "cross_project_negative": _cross_project_apply_case(),
        "permission_negative": _follow_up_case(account="denied", milestone_id=DYNAMIC_CANARY_MILESTONE_ID, expected_status=403, key="follow-denied-0001"),
        "import_preview_positive": {
            "method": "POST",
            "account": "importer",
            "path": "/api/maintenance/collection-plan-imports/preview",
            "expected_status": 200,
            "project_version": 4,
            "workbook_path": str(workbook),
            "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            "idempotency_key": "canary-preview-0001",
            "bindings": [{
                "external_order_no": "ORDER-1",
                "project_id": CANARY_PROJECT_ID,
                "project_version": 4,
                "project_contract_id": "{setup_contract.project_contract_id}",
                "project_contract_version": "{setup_contract.version}",
                "existing_binding_version": None,
                "reason": None,
            }],
        },
        "apply_last": {
            "method": "POST",
            "account": "importer",
            "path": "/api/maintenance/collection-plan-imports/{batch_id}/apply",
            "expected_status": 200,
        },
    }
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    _write_release_state(evidence, package, phase="deployed")
    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "canary", CANARY_PROJECT_ID, str(spec_file)],
        text=True, capture_output=True, env=env,
    )
    assert completed.returncode != 0
    env_text = (Path(env["V122_APP_DIR"]) / ".env").read_text()
    assert "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false" in env_text
    curl_text = curl_calls.read_text()
    assert "action_restore" in curl_text and "action_verify_restored" in curl_text
    assert "super-secret-control-token" not in curl_text
    if reaches_import:
        assert calls.read_text().count("maintenance_collection_milestone") >= 4
    else:
        assert "import_preview_positive" not in curl_text
    assert not (evidence / "canary-evidence.json").exists()


def test_canary_rejects_scope_code_mismatch_from_real_follow_up_endpoint(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
    if [[ "$*" == *"SELECT milestone_id"* ]]; then echo milestone-canary-0001; exit 0; fi
    if [[ "$*" == *"maintenance_collection_milestone"* ]]; then echo '0:0:0:0'; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"exec -T app"* ]]; then
      apply=$(sed -n 's/^MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=//p' "$V122_APP_DIR/.env")
      project=$(sed -n 's/^MAINTENANCE_COLLECTION_CANARY_PROJECT_ID=//p' "$V122_APP_DIR/.env")
      printf '%s\n%s\n' "$apply" "$project"; exit 0
    fi
    if [[ "$*" == *"compose"* && "$*" == *"up --no-deps --no-build"* ]]; then exit 0; fi
    exit 97
    """
    env, evidence, _calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    controls = _action_control_cases()
    env["V122_TEST_RESTORED_ACCOUNTS"] = str(
        _json_artifact(
            tmp_path / "restored-accounts.json",
            _action_account_rows(controls, "action_restore"),
        )
    )
    env["V122_TEST_GRANTED_ACCOUNTS"] = str(
        _json_artifact(
            tmp_path / "granted-accounts.json",
            _action_account_rows(controls, "action_grant"),
        )
    )
    curl_stub = Path(env["PATH"].split(":", 1)[0]) / "curl"
    _write(
        curl_stub,
        """
        #!/usr/bin/env bash
        out=''
        data=''
        previous=''
        for arg in "$@"; do
          if [ "$previous" = output ]; then out=$arg; fi
          if [ "$previous" = data ]; then data="${arg#@}"; fi
          previous=''
          [ "$arg" = --output ] && previous=output
          [ "$arg" = --data-binary ] && previous=data
        done
        url=${!#}
        case "$out" in
          *action_verify_restored.response)
            cp "$V122_TEST_RESTORED_ACCOUNTS" "$out"; status=200 ;;
          *action_verify_granted.response)
            cp "$V122_TEST_GRANTED_ACCOUNTS" "$out"; status=200 ;;
          *action_grant-*.response|*action_restore-*.response)
            python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); json.dump({"username":sys.argv[2],"overrides":body["overrides"]},open(sys.argv[3],"w"))' "$data" "${url##*/}" "$out"; status=200 ;;
          *login-follower.response.json)
            printf '%s' '{"token":"follower-token","role":"user","permissions":{"action_maintenance_collection_follow_up":true}}' >"$out"; status=200 ;;
          *login-importer.response.json)
            printf '%s' '{"token":"importer-token","role":"admin","permissions":{"action_maintenance_collection_plan_import":true}}' >"$out"; status=200 ;;
          *login-denied.response.json)
            printf '%s' '{"token":"denied-token","role":"admin","permissions":{"action_maintenance_collection_plan_import":false,"action_maintenance_collection_follow_up":false}}' >"$out"; status=200 ;;
          *setup_contract.response)
            printf '%s' '{"project_id":"123e4567-e89b-12d3-a456-426614174000","project_contract_id":"contract-live","version":3}' >"$out"; status=201 ;;
          *import_preview_positive.response)
            printf '%s' '{"batch_id":"batch-canary","batch_version":7,"data_version":"data-v7","status":"valid","rows":[{"external_order_no":"ORDER-1","row_key":"row-live"}]}' >"$out"; status=200 ;;
          *cross_project_negative.response)
            printf '%s' '{"detail":{"code":"permission_denied"}}' >"$out"; status=403 ;;
          *permission_negative.response)
            printf '%s' '{"detail":{"code":"permission_denied"}}' >"$out"; status=403 ;;
          *) printf '{}' >"$out"; status=200 ;;
        esac
        printf '%s' "$status"
        """,
        mode=0o700,
    )
    spec = {
        "base_url": "https://canary.invalid",
        "named_accounts": {
            "follower": {
                "username": "follower",
                "password": "secret-zero",
                "expected_role": "user",
                "required_permissions": ["action_maintenance_collection_follow_up"],
            },
            "importer": {
                "username": "importer",
                "password": "secret-one",
                "expected_role": "admin",
                "required_permissions": [
                    "action_maintenance_collection_plan_import",
                ],
            },
            "denied": {
                "username": "denied",
                "password": "secret-two",
                "expected_role": "admin",
                "forbidden_permissions": [
                    "action_maintenance_collection_plan_import",
                    "action_maintenance_collection_follow_up",
                ],
            },
        },
        **_action_control_cases(),
        "setup_contract": _setup_contract_case(),
        "follow_up_positive": _follow_up_case(account="follower", milestone_id=DYNAMIC_CANARY_MILESTONE_ID, expected_status=200, key="follow-positive-0001"),
        "cross_project_negative": _cross_project_apply_case(),
        "permission_negative": _follow_up_case(account="denied", milestone_id=DYNAMIC_CANARY_MILESTONE_ID, expected_status=403, key="follow-denied-0001"),
        "import_preview_positive": {
            "method": "POST",
            "account": "importer",
            "path": "/api/maintenance/collection-plan-imports/preview",
            "expected_status": 200,
            "project_version": 4,
            "workbook_path": str(workbook),
            "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            "idempotency_key": "canary-preview-0001",
            "bindings": [{
                "external_order_no": "ORDER-1",
                "project_id": CANARY_PROJECT_ID,
                "project_version": 4,
                "project_contract_id": "{setup_contract.project_contract_id}",
                "project_contract_version": "{setup_contract.version}",
                "existing_binding_version": None,
                "reason": None,
            }],
        },
        "apply_last": {
            "method": "POST",
            "account": "importer",
            "path": "/api/maintenance/collection-plan-imports/{batch_id}/apply",
            "expected_status": 200,
        },
    }
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    _write_release_state(evidence, package, phase="deployed")

    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "canary", CANARY_PROJECT_ID, str(spec_file)],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert "canary_scope_denied" in completed.stderr
    assert not (evidence / "canary-evidence.json").exists()


def test_canary_state_write_failure_closes_apply_and_restores_actions(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
    if [[ "$*" == *"SELECT milestone_id"* ]]; then echo milestone-canary-0001; exit 0; fi
    if [[ "$*" == *"maintenance_collection_milestone"* ]]; then echo '0:0:0:0'; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"exec -T app"* ]]; then
      apply=$(sed -n 's/^MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=//p' "$V122_APP_DIR/.env")
      project=$(sed -n 's/^MAINTENANCE_COLLECTION_CANARY_PROJECT_ID=//p' "$V122_APP_DIR/.env")
      printf '%s\n%s\n' "$apply" "$project"; exit 0
    fi
    if [[ "$*" == *"compose"* && "$*" == *"up --no-deps --no-build"* ]]; then exit 0; fi
    exit 97
    """
    env, evidence, _calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    curl_calls = tmp_path / "curl-calls"
    applied_payload = tmp_path / "applied-payload.json"
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    controls = _action_control_cases()
    restored_rows = json.dumps(
        _action_account_rows(controls, "action_restore"), separators=(",", ":"),
    )
    granted_rows = json.dumps(
        _action_account_rows(controls, "action_grant"), separators=(",", ":"),
    )
    state_path = evidence / "release-state.json"
    curl_stub = Path(env["PATH"].split(":", 1)[0]) / "curl"
    _write(
        curl_stub,
        f"""
        #!/usr/bin/env bash
        printf '%s\n' "$*" >> {curl_calls}
        out=''
        data=''
        previous=''
        for arg in "$@"; do
          if [ "$previous" = output ]; then out=$arg; fi
          if [ "$previous" = data ]; then data="${{arg#@}}"; fi
          previous=''
          [ "$arg" = --output ] && previous=output
          [ "$arg" = --data-binary ] && previous=data
        done
        url=${{!#}}
        case "$out" in
          *action_verify_restored.response)
            printf '%s' '{restored_rows}' >"$out"; status=200 ;;
          *action_verify_granted.response)
            printf '%s' '{granted_rows}' >"$out"; status=200 ;;
          *action_grant-*.response|*action_restore-*.response)
            python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); json.dump({{"username":sys.argv[2],"overrides":body["overrides"]}},open(sys.argv[3],"w"))' "$data" "${{url##*/}}" "$out"; status=200 ;;
          *login-follower.response.json)
            printf '%s' '{{"token":"follower-token","role":"user","permissions":{{"action_maintenance_collection_follow_up":true}}}}' >"$out"; status=200 ;;
          *login-importer.response.json)
            printf '%s' '{{"token":"importer-token","role":"admin","permissions":{{"action_maintenance_collection_plan_import":true}}}}' >"$out"; status=200 ;;
          *login-denied.response.json)
            printf '%s' '{{"token":"denied-token","role":"admin","permissions":{{"action_maintenance_collection_plan_import":false,"action_maintenance_collection_follow_up":false}}}}' >"$out"; status=200 ;;
          *setup_contract.response)
            printf '%s' '{{"project_id":"{CANARY_PROJECT_ID}","project_contract_id":"contract-live","version":3}}' >"$out"; status=201 ;;
          *cross_project_negative.response)
            printf '%s' '{{"detail":{{"code":"canary_scope_denied"}}}}' >"$out"; status=403 ;;
          *permission_negative.response)
            printf '%s' '{{"detail":{{"code":"permission_denied"}}}}' >"$out"; status=403 ;;
          *import_preview_positive.response)
            printf '%s' '{{"batch_id":"batch-canary","batch_version":7,"data_version":"data-v7","status":"valid","rows":[{{"external_order_no":"ORDER-1","row_key":"row-live"}}]}}' >"$out"; status=200 ;;
          *apply_last.response)
            cp -- "$data" {applied_payload}
            rm -f {state_path}
            mkdir {state_path}
            printf '{{}}' >"$out"; status=200 ;;
          *) printf '{{}}' >"$out"; status=200 ;;
        esac
        printf '%s' "$status"
        """,
        mode=0o700,
    )
    spec = {
        "base_url": "https://canary.invalid",
        "named_accounts": {
            "follower": {"username": "follower", "password": "secret-zero", "expected_role": "user", "required_permissions": ["action_maintenance_collection_follow_up"]},
            "importer": {"username": "importer", "password": "secret-one", "expected_role": "admin", "required_permissions": ["action_maintenance_collection_plan_import"]},
            "denied": {"username": "denied", "password": "secret-two", "expected_role": "admin", "forbidden_permissions": ["action_maintenance_collection_plan_import", "action_maintenance_collection_follow_up"]},
        },
        **_action_control_cases(),
        "setup_contract": _setup_contract_case(),
        "follow_up_positive": _follow_up_case(account="follower", milestone_id=DYNAMIC_CANARY_MILESTONE_ID, expected_status=200, key="follow-positive-0001"),
        "cross_project_negative": _cross_project_apply_case(),
        "permission_negative": _follow_up_case(account="denied", milestone_id=DYNAMIC_CANARY_MILESTONE_ID, expected_status=403, key="follow-denied-0001"),
        "import_preview_positive": {
            "method": "POST",
            "account": "importer",
            "path": "/api/maintenance/collection-plan-imports/preview",
            "expected_status": 200,
            "project_version": 4,
            "workbook_path": str(workbook),
            "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            "idempotency_key": "canary-preview-0001",
            "bindings": [{
                "external_order_no": "ORDER-1",
                "project_id": CANARY_PROJECT_ID,
                "project_version": 4,
                "project_contract_id": "{setup_contract.project_contract_id}",
                "project_contract_version": "{setup_contract.version}",
                "existing_binding_version": None,
                "reason": None,
            }],
        },
        "apply_last": {
            "method": "POST",
            "account": "importer",
            "path": "/api/maintenance/collection-plan-imports/{batch_id}/apply",
            "expected_status": 200,
        },
    }
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    _write_release_state(evidence, package, phase="deployed")

    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "canary", CANARY_PROJECT_ID, str(spec_file)],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false" in (Path(env["V122_APP_DIR"]) / ".env").read_text()
    curl_text = curl_calls.read_text()
    assert "action_restore" in curl_text and "action_verify_restored" in curl_text
    assert "--form" in curl_text and "canary.xls" in curl_text
    assert "/api/maintenance/collection-plan-imports/batch-canary/apply" in curl_text
    assert json.loads(applied_payload.read_text()) == {
        "expected_batch_version": 7,
        "expected_data_version": "data-v7",
        "bindings": [{
            "row_key": "row-live",
            "external_order_no": "ORDER-1",
            "project_id": CANARY_PROJECT_ID,
            "project_version": 4,
            "project_contract_id": "contract-live",
            "project_contract_version": 3,
            "existing_binding_version": None,
            "reason": None,
        }],
    }


@pytest.mark.parametrize(
    "invalid_input",
    [
        "workbook_sha",
        "binding_project",
        "fake_follow_path",
        "different_apply_account",
        "admin_follow_up",
        "setup_contract_included_in_total",
        "setup_contract_source",
        "setup_contract_id",
        "setup_contract_no",
        "legacy_action_dict",
        "legacy_action_path",
        "action_post",
        "wrong_action_target",
        "duplicate_action_target",
        "restore_not_reversed",
        "verify_virtual_path",
        "verify_post",
        "action_body_extra",
        "action_overrides_not_dict",
        "grant_deletes_unrelated_override",
        "grant_changes_unrelated_override",
        "grant_adds_unrelated_override",
    ],
)
def test_canary_rejects_invalid_spec_before_runtime_changes(
    tmp_path: Path, invalid_input: str,
):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body="exit 97",
    )
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    binding = {
        "external_order_no": "ORDER-1",
        "project_id": CANARY_PROJECT_ID,
        "project_version": 4,
        "project_contract_id": "{setup_contract.project_contract_id}",
        "project_contract_version": "{setup_contract.version}",
        "existing_binding_version": None,
        "reason": None,
    }
    preview = {
        "method": "POST",
        "account": "importer",
        "path": "/api/maintenance/collection-plan-imports/preview",
        "expected_status": 200,
        "project_version": 4,
        "workbook_path": str(workbook),
        "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
        "idempotency_key": "canary-preview-0001",
        "bindings": [binding],
    }
    if invalid_input == "workbook_sha":
        preview["workbook_sha256"] = "0" * 64
    elif invalid_input == "binding_project":
        binding["project_id"] = "123e4567-e89b-12d3-a456-426614174099"
    spec = {
        "base_url": "https://canary.invalid",
        "named_accounts": {
            "follower": {"username": "follower", "password": "one", "expected_role": "user", "required_permissions": ["action_maintenance_collection_follow_up"]},
            "importer": {"username": "importer", "password": "two", "expected_role": "admin", "required_permissions": ["action_maintenance_collection_plan_import"]},
            "importer2": {"username": "importer2", "password": "four", "expected_role": "admin", "required_permissions": ["action_maintenance_collection_plan_import"]},
            "denied": {"username": "denied", "password": "three", "expected_role": "admin", "forbidden_permissions": ["action_maintenance_collection_plan_import", "action_maintenance_collection_follow_up"]},
        },
        **_action_control_cases(),
        "setup_contract": _setup_contract_case(),
        "follow_up_positive": _follow_up_case(account="follower", milestone_id=DYNAMIC_CANARY_MILESTONE_ID, expected_status=200, key="follow-positive-0001"),
        "cross_project_negative": _cross_project_apply_case(),
        "permission_negative": _follow_up_case(account="denied", milestone_id=DYNAMIC_CANARY_MILESTONE_ID, expected_status=403, key="follow-denied-0001"),
        "import_preview_positive": preview,
        "apply_last": {"method": "POST", "account": "importer", "path": "/api/maintenance/collection-plan-imports/{batch_id}/apply", "expected_status": 200},
    }
    if invalid_input == "fake_follow_path":
        spec["follow_up_positive"]["path"] = "/api/follow-up"
    elif invalid_input == "different_apply_account":
        spec["apply_last"]["account"] = "importer2"
    elif invalid_input == "admin_follow_up":
        spec["named_accounts"]["follower"]["expected_role"] = "admin"
    elif invalid_input == "setup_contract_included_in_total":
        spec["setup_contract"]["body"]["included_in_total"] = True
    elif invalid_input == "setup_contract_source":
        spec["setup_contract"]["body"]["source"] = "manual"
    elif invalid_input == "setup_contract_id":
        spec["setup_contract"]["body"]["contract_id"] = "business-contract-source"
    elif invalid_input == "setup_contract_no":
        spec["setup_contract"]["body"]["contract_no"] = "BUSINESS-CONTRACT-001"
    elif invalid_input == "legacy_action_dict":
        spec["action_grant"] = spec["action_grant"][0]
    elif invalid_input == "legacy_action_path":
        spec["action_grant"][0]["path"] = "/api/accounts/grant"
    elif invalid_input == "action_post":
        spec["action_grant"][0]["method"] = "POST"
    elif invalid_input == "wrong_action_target":
        spec["action_grant"][0]["path"] = "/api/accounts/not-a-canary-account"
    elif invalid_input == "duplicate_action_target":
        spec["action_grant"][1]["path"] = spec["action_grant"][0]["path"]
    elif invalid_input == "restore_not_reversed":
        spec["action_restore"] = list(reversed(spec["action_restore"]))
    elif invalid_input == "verify_virtual_path":
        spec["action_verify_granted"]["path"] = "/api/accounts/verify-granted"
    elif invalid_input == "verify_post":
        spec["action_verify_restored"]["method"] = "POST"
    elif invalid_input == "action_body_extra":
        spec["action_grant"][0]["body"]["role"] = "admin"
    elif invalid_input == "action_overrides_not_dict":
        spec["action_restore"][0]["body"]["overrides"] = []
    elif invalid_input == "grant_deletes_unrelated_override":
        del spec["action_grant"][0]["body"]["overrides"]["data_customer"]
    elif invalid_input == "grant_changes_unrelated_override":
        spec["action_grant"][0]["body"]["overrides"]["data_customer"] = True
    elif invalid_input == "grant_adds_unrelated_override":
        spec["action_grant"][0]["body"]["overrides"]["data_profit"] = True
    spec_file = _json_artifact(tmp_path / "canary.json", spec)
    _write_release_state(evidence, package, phase="deployed")
    env_file = Path(env["V122_APP_DIR"]) / ".env"
    before = env_file.read_bytes()

    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "canary", CANARY_PROJECT_ID, str(spec_file)],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert env_file.read_bytes() == before
    assert not calls.exists() or calls.read_text() == ""


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_release_backup_never_overwrites_existing_generation_before_docker(
    tmp_path: Path,
    kind: str,
):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, backup_root = _release_test_env(tmp_path / "runtime", package)
    _write_release_state(evidence, package, phase="frozen")
    target = backup_root / env["V122_BACKUP_GENERATION"]
    if kind == "directory":
        target.mkdir()
    elif kind == "file":
        _write(target, "existing\n")
    else:
        target.symlink_to(tmp_path)
    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "backup"],
        text=True,
        capture_output=True,
        env=env,
    )
    assert completed.returncode != 0
    assert "already exists" in completed.stderr
    assert not calls.exists()


def test_full_backup_binds_active_assets_previous_images_and_upload_metadata(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    dump = _write(tmp_path / "fixture.dump", b"custom-dump")
    uploads = tmp_path / "fixture-uploads.tar"
    with tarfile.open(uploads, "w") as archive:
        content = b"abc"
        info = tarfile.TarInfo("raw/file.bin")
        info.mode = 0o640
        info.uid = 1000
        info.gid = 1001
        info.mtime = 123456789
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    uploads.chmod(0o600)
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
    if [[ "$*" == *"pg_current_wal_lsn"* ]]; then echo 0/ABC; exit 0; fi
    if [[ "$*" == *"pg_dump "* ]]; then exit 0; fi
    if [[ "$1" == cp ]]; then cp "$V122_TEST_DUMP" "${!#}"; exit 0; fi
    if [[ "$*" == *"pg_dumpall --globals-only"* ]]; then echo '-- globals'; exit 0; fi
    if [[ "$*" == *"/backup/uploads.tar"* ]]; then
      for arg in "$@"; do case "$arg" in *:/backup) dest="${arg%:/backup}";; esac; done
      cp "$V122_TEST_UPLOADS" "$dest/uploads.tar"; exit 0
    fi
    if [[ "$*" == *"find /uploads -type f | wc -l"* ]]; then echo 1; exit 0; fi
    if [[ "$*" == *"find /uploads -type f -exec wc"* ]]; then echo 3; exit 0; fi
    if [[ "$*" == *"image inspect --format"* ]]; then echo "${!#}"; exit 0; fi
    if [[ "$*" == *"image inspect"* ]]; then echo '[]'; exit 0; fi
    if [[ "$1" == tag ]]; then exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q app"* ]]; then echo app-cid; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q frontend"* ]]; then echo frontend-cid; exit 0; fi
    if [[ "$*" == *"inspect"* && "$*" == *"app-cid"* ]]; then echo "$V122_TEST_APP_CONTAINER_IMAGE"; exit 0; fi
    if [[ "$*" == *"inspect"* && "$*" == *"frontend-cid"* ]]; then echo "$V122_TEST_FRONTEND_CONTAINER_IMAGE"; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"up --no-deps --no-build"* ]]; then exit 0; fi
    if [[ "$1" == ps ]]; then echo '{}'; exit 0; fi
    exit 97
    """
    env, evidence, calls, backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    env["V122_TEST_DUMP"] = str(dump)
    env["V122_TEST_UPLOADS"] = str(uploads)
    env["V122_TEST_APP_CONTAINER_IMAGE"] = "sha256:" + "8" * 64
    env["V122_TEST_FRONTEND_CONTAINER_IMAGE"] = "sha256:" + "9" * 64
    _write_release_state(evidence, package, phase="frozen")
    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "backup"],
        text=True, capture_output=True, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    backup = backup_root / env["V122_BACKUP_GENERATION"]
    manifest = json.loads((backup / "backup-manifest.json").read_text())
    root_state = json.loads(Path(env["V122_ROOT_RELEASE_STATE"]).read_text())
    assert manifest["previous_app_image_id"] == root_state["app_image_id"]
    assert manifest["previous_frontend_image_id"] == root_state["frontend_image_id"]
    assert manifest["db_image_id"] == root_state["database_image_id"]
    for key in ("active_compose_sha256", "active_env_sha256", "root_release_state_sha256", "uploads_metadata_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", manifest[key])
    assert manifest["uploads_file_count"] == 1
    assert manifest["uploads_total_bytes"] == 3
    state = json.loads((evidence / "release-state.json").read_text())
    assert state["backup_dir"] == str(backup)
    assert state["backup_manifest_sha256"] == hashlib.sha256((backup / "backup-manifest.json").read_bytes()).hexdigest()
    assert state["backup_checksums_sha256"] == hashlib.sha256((backup / "sha256sums").read_bytes()).hexdigest()
    assert state["service_restored"] is True
    call_text = calls.read_text()
    assert "tag sha256:" + "8" * 64 + " it-spareparts-app:latest" in call_text
    assert "tag sha256:" + "9" * 64 + " it-spareparts-frontend:latest" in call_text
    assert "up --no-deps --no-build --force-recreate -d app frontend" in call_text


def test_restore_check_rejects_assets_outside_state_bound_backup(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, backup_root = _release_test_env(tmp_path / "runtime", package)
    bound = backup_root / env["V122_BACKUP_GENERATION"]
    bound.mkdir()
    manifest = _json_artifact(bound / "backup-manifest.json", {"format": "fixture"})
    checksums = _write(bound / "sha256sums", "0" * 64 + "  fixture\n")
    _write_release_state(
        evidence, package, phase="backup", backup_dir=str(bound),
        backup_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        backup_checksums_sha256=hashlib.sha256(checksums.read_bytes()).hexdigest(),
    )
    outside_dump = _write(tmp_path / "outside.dump", b"dump")
    outside_uploads = _write(tmp_path / "outside.tar", b"tar")
    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "restore-check", str(outside_dump), str(outside_uploads)],
        text=True, capture_output=True, env=env,
    )
    assert completed.returncode != 0
    assert "state-bound backup" in completed.stderr
    assert not calls.exists()


@pytest.mark.parametrize("phase", ["backup", "restore_checked", "canary", "observe_0", "observe_15", "observed"])
def test_resume_previous_images_is_available_from_predeploy_and_canary_phases(tmp_path: Path, phase: str):
    package = _production_package(tmp_path / "pkg")
    old_app = "sha256:" + "8" * 64
    old_frontend = "sha256:" + "9" * 64
    docker = """
    if [[ "$*" == *"image inspect"* ]]; then echo "${!#}"; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q app"* ]]; then echo app-cid; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q frontend"* ]]; then echo frontend-cid; exit 0; fi
    if [[ "$*" == *"inspect"* && "$*" == *"app-cid"* ]]; then echo "$V122_TEST_APP_CONTAINER_IMAGE"; exit 0; fi
    if [[ "$*" == *"inspect"* && "$*" == *"frontend-cid"* ]]; then echo "$V122_TEST_FRONTEND_CONTAINER_IMAGE"; exit 0; fi
    exit 0
    """
    env, evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    env["V122_TEST_APP_CONTAINER_IMAGE"] = old_app
    env["V122_TEST_FRONTEND_CONTAINER_IMAGE"] = old_frontend
    _write_release_state(evidence, package, phase=phase)
    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "rollback-images"],
        text=True, capture_output=True, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert f"tag {old_app} it-spareparts-app:latest" in calls.read_text()
    assert f"tag {old_frontend} it-spareparts-frontend:latest" in calls.read_text()


def test_preliminary_backup_can_resume_previous_images(tmp_path: Path):
    package = _build_package(tmp_path / "pkg")
    old_app = "sha256:" + "8" * 64
    old_frontend = "sha256:" + "9" * 64
    docker = """
    if [[ "$*" == *"image inspect"* ]]; then echo "${!#}"; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q app"* ]]; then echo app-cid; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q frontend"* ]]; then echo frontend-cid; exit 0; fi
    if [[ "$*" == *"inspect"* && "$*" == *"app-cid"* ]]; then echo "$V122_TEST_APP_CONTAINER_IMAGE"; exit 0; fi
    if [[ "$*" == *"inspect"* && "$*" == *"frontend-cid"* ]]; then echo "$V122_TEST_FRONTEND_CONTAINER_IMAGE"; exit 0; fi
    exit 0
    """
    env, evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    env["V122_TEST_APP_CONTAINER_IMAGE"] = old_app
    env["V122_TEST_FRONTEND_CONTAINER_IMAGE"] = old_frontend
    _write_release_state(evidence, package, phase="backup")
    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "rollback-images"],
        text=True, capture_output=True, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert f"tag {old_app} it-spareparts-app:latest" in calls.read_text()


def test_post_canary_rollback_requires_sealed_action_restore_spec_before_docker(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, _backup_root = _release_test_env(tmp_path / "runtime", package)
    _write_release_state(evidence, package, phase="canary", actions_granted=True)
    completed = subprocess.run(
        [str(package / "v122_collection_reminders_release.sh"), str(package), str(evidence), "rollback-images"],
        text=True, capture_output=True, env=env,
    )
    assert completed.returncode != 0
    assert "action permissions" in completed.stderr
    assert not calls.exists()


def test_release_observation_is_a_persisted_0_5_15_30_sequence(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"ps -q app"* ]]; then echo app-cid; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps"* ]]; then echo healthy; exit 0; fi
    if [[ "$*" == *"health/db"* || "$*" == *"127.0.0.1:8000/health"* ]]; then echo 200; exit 0; fi
    if [[ "$*" == *"pg_locks"* || "$*" == *"state = 'active'"* || "$*" == *"maintenance_collection_milestone_operation"* ]]; then echo 0; exit 0; fi
    if [[ "$*" == *"RestartCount"* ]]; then echo 0; exit 0; fi
    if [[ "$*" == *"find /uploads -type f | wc -l"* ]]; then echo 1; exit 0; fi
    if [[ "$*" == *"find /uploads -type f -exec wc"* ]]; then echo 3; exit 0; fi
    if [[ "$1" == logs ]]; then exit 0; fi
    if [[ "$1" == stats ]]; then echo stats-ok; exit 0; fi
    exit 0
    """
    env, evidence, _calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body=docker
    )
    release = package / "v122_collection_reminders_release.sh"
    _write_release_state(evidence, package, phase="canary")
    skipped = subprocess.run(
        [str(release), str(package), str(evidence), "observe", "5"],
        text=True, capture_output=True, env=env,
    )
    assert skipped.returncode != 0
    for minute, phase in (("0", "observe_0"), ("5", "observe_5"), ("15", "observe_15"), ("30", "observed")):
        completed = subprocess.run(
            [str(release), str(package), str(evidence), "observe", minute],
            text=True, capture_output=True, env=env,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads((evidence / "release-state.json").read_text())["phase"] == phase
        metrics = json.loads((evidence / f"observe-{minute}.json").read_text())
        assert metrics["health_status"] == 200
        assert metrics["readiness_status"] == 200
        assert metrics["http_5xx_count"] == 0
        assert metrics["blocking_lock_count"] == 0
        assert metrics["slow_query_count"] == 0
        assert metrics["restart_count"] == 0
        assert metrics["uploads_file_count"] == 1
        assert metrics["uploads_total_bytes"] == 3
        assert metrics["audit_count"] == 0


def test_release_observation_fails_closed_on_bad_operational_metrics_without_advancing(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"ps -q app"* ]]; then echo app-cid; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q frontend"* ]]; then echo frontend-cid; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps"* ]]; then echo healthy; exit 0; fi
    if [[ "$*" == *"health/db"* || "$*" == *"127.0.0.1:8000/health"* ]]; then echo 200; exit 0; fi
    if [[ "$*" == *"pg_locks"* ]]; then echo 2; exit 0; fi
    if [[ "$*" == *"state = 'active'"* ]]; then echo 3; exit 0; fi
    if [[ "$*" == *"maintenance_collection_milestone_operation"* ]]; then echo 9; exit 0; fi
    if [[ "$*" == *"RestartCount"* ]]; then echo 1; exit 0; fi
    if [[ "$*" == *"find /uploads -type f | wc -l"* ]]; then echo 1; exit 0; fi
    if [[ "$*" == *"find /uploads -type f -exec wc"* ]]; then echo 3; exit 0; fi
    if [[ "$1" == logs ]]; then echo 'GET /api/x 500'; exit 0; fi
    if [[ "$1" == stats ]]; then echo stats-ok; exit 0; fi
    exit 0
    """
    env, evidence, _calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    release = package / "v122_collection_reminders_release.sh"
    _write_release_state(evidence, package, phase="canary")

    completed = subprocess.run(
        [str(release), str(package), str(evidence), "observe", "0"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert completed.returncode != 0
    assert "observation" in completed.stderr.lower()
    assert json.loads((evidence / "release-state.json").read_text())["phase"] == "canary"


def test_release_observation_rejects_upload_drift_against_previous_point(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"ps -q app"* ]]; then echo app-cid; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
    if [[ "$*" == *"compose"* && "$*" == *"ps"* ]]; then echo healthy; exit 0; fi
    if [[ "$*" == *"health/db"* || "$*" == *"127.0.0.1:8000/health"* ]]; then echo 200; exit 0; fi
    if [[ "$*" == *"pg_locks"* || "$*" == *"state = 'active'"* || "$*" == *"maintenance_collection_milestone_operation"* ]]; then echo 0; exit 0; fi
    if [[ "$*" == *"RestartCount"* ]]; then echo 0; exit 0; fi
    if [[ "$*" == *"find /uploads -type f | wc -l"* ]]; then
      if [ -f "$V122_TEST_UPLOAD_DRIFT" ]; then echo 2; else echo 1; fi
      exit 0
    fi
    if [[ "$*" == *"find /uploads -type f -exec wc"* ]]; then echo 3; exit 0; fi
    if [[ "$1" == logs ]]; then exit 0; fi
    if [[ "$1" == stats ]]; then echo stats-ok; exit 0; fi
    exit 0
    """
    env, evidence, _calls, _backup_root = _release_test_env(tmp_path / "runtime", package, docker_body=docker)
    drift_flag = tmp_path / "drift"
    env["V122_TEST_UPLOAD_DRIFT"] = str(drift_flag)
    release = package / "v122_collection_reminders_release.sh"
    _write_release_state(evidence, package, phase="canary")

    first = subprocess.run([str(release), str(package), str(evidence), "observe", "0"], text=True, capture_output=True, env=env)
    assert first.returncode == 0, first.stderr
    drift_flag.write_text("drift\n")
    second = subprocess.run([str(release), str(package), str(evidence), "observe", "5"], text=True, capture_output=True, env=env)

    assert second.returncode != 0
    assert "uploads" in second.stderr.lower()
    assert json.loads((evidence / "release-state.json").read_text())["phase"] == "observe_0"
