"""Static runtime contract for collection-plan production kill switches."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_collection_apply_and_canary_envs_are_explicitly_safe_and_wired_to_app():
    compose = yaml.safe_load(_read("docker-compose.yml"))
    root_env = _read(".env.example")
    backend_env = _read("backend/.env.example")

    app_environment = compose["services"]["app"]["environment"]
    assert app_environment["MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED"] == (
        "${MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED:-false}"
    )
    assert app_environment["MAINTENANCE_COLLECTION_CANARY_PROJECT_ID"] == (
        "${MAINTENANCE_COLLECTION_CANARY_PROJECT_ID:-}"
    )
    assert "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false" in root_env
    assert "MAINTENANCE_COLLECTION_CANARY_PROJECT_ID=" in root_env
    assert "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false" in backend_env
    assert "MAINTENANCE_COLLECTION_CANARY_PROJECT_ID=" in backend_env
