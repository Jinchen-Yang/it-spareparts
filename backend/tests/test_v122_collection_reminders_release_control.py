from __future__ import annotations

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
REAL_SAMPLE_SHA256 = "a783af09fa108d366a26e10fe188be52d20a9ce1fe02121bfd683d96356c8c18"


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
    if [[ \"$*\" == *\"maintenance_collection_plan_import_batch\"* && \"$*\" == *\"processing\"* ]]; then echo 0; exit 0; fi
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


def test_canary_failure_closes_apply_restores_actions_and_keeps_secrets_out_of_evidence(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
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
          *login-follower.response.json)
            printf '%s' '{{"token":"follower-token","role":"user","permissions":{{"action_maintenance_collection_follow_up":true}}}}' >"$out"; status=200 ;;
          *login-importer.response.json)
            printf '%s' '{{"token":"importer-token","role":"admin","permissions":{{"action_maintenance_collection_plan_import":true}}}}' >"$out"; status=200 ;;
          *login-denied.response.json)
            printf '%s' '{{"token":"denied-token","role":"admin","permissions":{{"action_maintenance_collection_plan_import":false}}}}' >"$out"; status=200 ;;
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
    base_case = {"method": "POST", "token": "super-secret-control-token", "expected_status": 200, "body": {}}
    spec = {
        "base_url": "https://canary.invalid",
        "named_accounts": {
            "follower": {"username": "follower", "password": "secret-zero", "expected_role": "user", "required_permissions": ["action_maintenance_collection_follow_up"]},
            "importer": {"username": "importer", "password": "secret-one", "expected_role": "admin", "required_permissions": ["action_maintenance_collection_plan_import"]},
            "denied": {"username": "denied", "password": "secret-two", "expected_role": "admin", "forbidden_permissions": ["action_maintenance_collection_plan_import"]},
        },
        "action_grant": {**base_case, "path": "/api/accounts/grant"},
        "action_verify_granted": {**base_case, "path": "/api/accounts/verify-granted"},
        "action_restore": {**base_case, "path": "/api/accounts/restore"},
        "action_verify_restored": {**base_case, "path": "/api/accounts/verify-restored"},
        "follow_up_positive": {"method": "POST", "account": "follower", "path": "/api/follow-up", "expected_status": 200, "body": {}},
        "cross_project_negative": {"method": "POST", "account": "follower", "path": "/api/cross-project", "expected_status": 403, "body": {}},
        "permission_negative": {"method": "POST", "account": "denied", "path": "/api/permission", "expected_status": 403, "body": {}},
        "import_preview_positive": {
            "method": "POST",
            "account": "importer",
            "path": "/api/maintenance/collection-plan-imports/preview",
            "expected_status": 200,
            "workbook_path": str(workbook),
            "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            "idempotency_key": "canary-preview-0001",
            "bindings": [{
                "external_order_no": "ORDER-1",
                "project_id": CANARY_PROJECT_ID,
                "project_version": 4,
                "project_contract_id": "contract-live",
                "project_contract_version": 3,
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
    assert calls.read_text().count("maintenance_collection_milestone") >= 4
    assert not (evidence / "canary-evidence.json").exists()


def test_canary_rejects_scope_code_mismatch_and_admin_positive_account(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
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
    curl_stub = Path(env["PATH"].split(":", 1)[0]) / "curl"
    _write(
        curl_stub,
        """
        #!/usr/bin/env bash
        out=''
        previous=''
        for arg in "$@"; do
          if [ "$previous" = output ]; then out=$arg; fi
          previous=''
          [ "$arg" = --output ] && previous=output
        done
        case "$out" in
          *login-importer.response.json)
            printf '%s' '{"token":"importer-token","role":"admin","permissions":{"action_maintenance_collection_plan_import":true,"action_maintenance_collection_follow_up":true}}' >"$out"; status=200 ;;
          *login-denied.response.json)
            printf '%s' '{"token":"denied-token","role":"admin","permissions":{"action_maintenance_collection_plan_import":false,"action_maintenance_collection_follow_up":false}}' >"$out"; status=200 ;;
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
    base_case = {"method": "POST", "token": "control-token", "expected_status": 200, "body": {}}
    spec = {
        "base_url": "https://canary.invalid",
        "named_accounts": {
            "importer": {
                "username": "importer",
                "password": "secret-one",
                "expected_role": "admin",
                "required_permissions": [
                    "action_maintenance_collection_plan_import",
                    "action_maintenance_collection_follow_up",
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
        "action_grant": {**base_case, "path": "/api/accounts/grant"},
        "action_verify_granted": {**base_case, "path": "/api/accounts/verify-granted"},
        "action_restore": {**base_case, "path": "/api/accounts/restore"},
        "action_verify_restored": {**base_case, "path": "/api/accounts/verify-restored"},
        "follow_up_positive": {"method": "POST", "account": "importer", "path": "/api/follow-up", "expected_status": 200, "body": {}},
        "cross_project_negative": {"method": "POST", "account": "importer", "path": "/api/cross-project", "expected_status": 403, "body": {}},
        "permission_negative": {"method": "POST", "account": "denied", "path": "/api/permission", "expected_status": 403, "body": {}},
        "import_preview_positive": {
            "method": "POST",
            "account": "importer",
            "path": "/api/maintenance/collection-plan-imports/preview",
            "expected_status": 200,
            "workbook_path": str(workbook),
            "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            "idempotency_key": "canary-preview-0001",
            "bindings": [{
                "external_order_no": "ORDER-1",
                "project_id": CANARY_PROJECT_ID,
                "project_version": 4,
                "project_contract_id": "contract-live",
                "project_contract_version": 3,
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
    assert "admin" in completed.stderr or "canary_scope_denied" in completed.stderr
    assert not (evidence / "canary-evidence.json").exists()


def test_canary_state_write_failure_closes_apply_and_restores_actions(tmp_path: Path):
    package = _production_package(tmp_path / "pkg")
    docker = """
    if [[ "$*" == *"compose"* && "$*" == *"ps -q db"* ]]; then echo db-cid; exit 0; fi
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
        case "$out" in
          *login-follower.response.json)
            printf '%s' '{{"token":"follower-token","role":"user","permissions":{{"action_maintenance_collection_follow_up":true}}}}' >"$out"; status=200 ;;
          *login-importer.response.json)
            printf '%s' '{{"token":"importer-token","role":"admin","permissions":{{"action_maintenance_collection_plan_import":true}}}}' >"$out"; status=200 ;;
          *login-denied.response.json)
            printf '%s' '{{"token":"denied-token","role":"admin","permissions":{{"action_maintenance_collection_plan_import":false,"action_maintenance_collection_follow_up":false}}}}' >"$out"; status=200 ;;
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
    base_case = {"method": "POST", "token": "control-token", "expected_status": 200, "body": {}}
    spec = {
        "base_url": "https://canary.invalid",
        "named_accounts": {
            "follower": {"username": "follower", "password": "secret-zero", "expected_role": "user", "required_permissions": ["action_maintenance_collection_follow_up"]},
            "importer": {"username": "importer", "password": "secret-one", "expected_role": "admin", "required_permissions": ["action_maintenance_collection_plan_import"]},
            "denied": {"username": "denied", "password": "secret-two", "expected_role": "admin", "forbidden_permissions": ["action_maintenance_collection_plan_import", "action_maintenance_collection_follow_up"]},
        },
        "action_grant": {**base_case, "path": "/api/accounts/grant"},
        "action_verify_granted": {**base_case, "path": "/api/accounts/verify-granted"},
        "action_restore": {**base_case, "path": "/api/accounts/restore"},
        "action_verify_restored": {**base_case, "path": "/api/accounts/verify-restored"},
        "follow_up_positive": {"method": "POST", "account": "follower", "path": "/api/follow-up", "expected_status": 200, "body": {}},
        "cross_project_negative": {"method": "POST", "account": "follower", "path": "/api/cross-project", "expected_status": 403, "body": {}},
        "permission_negative": {"method": "POST", "account": "denied", "path": "/api/permission", "expected_status": 403, "body": {}},
        "import_preview_positive": {
            "method": "POST",
            "account": "importer",
            "path": "/api/maintenance/collection-plan-imports/preview",
            "expected_status": 200,
            "workbook_path": str(workbook),
            "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            "idempotency_key": "canary-preview-0001",
            "bindings": [{
                "external_order_no": "ORDER-1",
                "project_id": CANARY_PROJECT_ID,
                "project_version": 4,
                "project_contract_id": "contract-live",
                "project_contract_version": 3,
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


@pytest.mark.parametrize("invalid_input", ["workbook_sha", "binding_project"])
def test_canary_rejects_invalid_workbook_or_binding_before_runtime_changes(
    tmp_path: Path, invalid_input: str,
):
    package = _production_package(tmp_path / "pkg")
    env, evidence, calls, _backup_root = _release_test_env(
        tmp_path / "runtime", package, docker_body="exit 97",
    )
    workbook = _write(tmp_path / "canary.xls", b"one-row-canary-workbook")
    base_case = {
        "method": "POST",
        "token": "control-token",
        "expected_status": 200,
        "body": {},
    }
    binding = {
        "external_order_no": "ORDER-1",
        "project_id": CANARY_PROJECT_ID,
        "project_version": 4,
        "project_contract_id": "contract-live",
        "project_contract_version": 3,
        "existing_binding_version": None,
        "reason": None,
    }
    preview = {
        "method": "POST",
        "account": "importer",
        "path": "/api/maintenance/collection-plan-imports/preview",
        "expected_status": 200,
        "workbook_path": str(workbook),
        "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
        "idempotency_key": "canary-preview-0001",
        "bindings": [binding],
    }
    if invalid_input == "workbook_sha":
        preview["workbook_sha256"] = "0" * 64
    else:
        binding["project_id"] = "123e4567-e89b-12d3-a456-426614174099"
    spec = {
        "base_url": "https://canary.invalid",
        "named_accounts": {
            "follower": {"username": "follower", "password": "one", "expected_role": "user", "required_permissions": ["action_maintenance_collection_follow_up"]},
            "importer": {"username": "importer", "password": "two", "expected_role": "admin", "required_permissions": ["action_maintenance_collection_plan_import"]},
            "denied": {"username": "denied", "password": "three", "expected_role": "admin", "forbidden_permissions": ["action_maintenance_collection_plan_import"]},
        },
        "action_grant": {**base_case, "path": "/api/accounts/grant"},
        "action_verify_granted": {**base_case, "path": "/api/accounts/verify-granted"},
        "action_restore": {**base_case, "path": "/api/accounts/restore"},
        "action_verify_restored": {**base_case, "path": "/api/accounts/verify-restored"},
        "follow_up_positive": {"method": "POST", "account": "follower", "path": "/api/follow-up", "expected_status": 200},
        "cross_project_negative": {"method": "POST", "account": "follower", "path": "/api/cross-project", "expected_status": 403},
        "permission_negative": {"method": "POST", "account": "denied", "path": "/api/permission", "expected_status": 403},
        "import_preview_positive": preview,
        "apply_last": {"method": "POST", "account": "importer", "path": "/api/maintenance/collection-plan-imports/{batch_id}/apply", "expected_status": 200},
    }
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
