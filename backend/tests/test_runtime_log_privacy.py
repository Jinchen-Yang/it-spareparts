"""Deployment access logs must not persist sensitive URL query strings."""

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import StatementError

from app.db import engine


ROOT = Path(__file__).resolve().parents[2]
NGINX_IMAGE = (
    "nginx:1.27-alpine@"
    "sha256:62223d644fa234c3a1cc785ee14242ec47a77364226f1c811d2f669f96dc2ac8"
)


def test_frontend_access_log_keeps_path_status_and_timing_without_query_sources():
    format_config = (ROOT / "frontend" / "nginx-log-format.conf").read_text(
        encoding="utf-8"
    )
    server_config = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY nginx-log-format.conf /etc/nginx/conf.d/00-log-format.conf" in dockerfile
    assert "access_log /var/log/nginx/access.log safe_path;" in server_config
    assert re.search(
        r"location /api\s*\{[^}]*error_log /dev/null;",
        server_config,
        flags=re.DOTALL,
    )
    assert "log_format safe_path" in format_config
    assert "$request_method" in format_config
    assert "$uri" in format_config
    assert "$status" in format_config
    assert "$request_time" in format_config
    assert "$upstream_response_time" in format_config
    for unsafe_source in (
        "$request ",
        "$request_uri",
        "$args",
        "$query_string",
        "$http_referer",
    ):
        assert unsafe_source not in format_config


def test_frontend_api_fault_log_does_not_persist_query_values():
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable for the Nginx fault-log gate")

    docker_ready = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if docker_ready.returncode != 0:
        pytest.skip("Docker daemon is unavailable for the Nginx fault-log gate")

    image_ready = subprocess.run(
        ["docker", "image", "inspect", NGINX_IMAGE],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if image_ready.returncode != 0:
        if not os.getenv("CI"):
            pytest.skip("Pinned Nginx image is unavailable outside CI")
        pull_result = subprocess.run(
            ["docker", "pull", NGINX_IMAGE],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert pull_result.returncode == 0, pull_result.stderr

    container_name = f"itspareparts-nginx-log-privacy-{uuid.uuid4().hex}"
    sentinel = f"SENSITIVE_QUERY_FAULT_{uuid.uuid4().hex}"
    api_path = "/api/maintenance/projects/stable/operations"
    run_result = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container_name,
            "--add-host",
            "app:127.0.0.1",
            "--volume",
            f"{ROOT / 'frontend' / 'nginx-log-format.conf'}:"
            "/etc/nginx/conf.d/00-log-format.conf:ro",
            "--volume",
            f"{ROOT / 'frontend' / 'nginx.conf'}:"
            "/etc/nginx/conf.d/default.conf:ro",
            NGINX_IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert run_result.returncode == 0, run_result.stderr

    try:
        time.sleep(0.5)
        request_result = subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "wget",
                "-S",
                "-O",
                "/dev/null",
                f"http://127.0.0.1{api_path}?q={sentinel}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert request_result.returncode != 0
        time.sleep(0.2)
        logs_result = subprocess.run(
            ["docker", "logs", container_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert logs_result.returncode == 0, logs_result.stderr
        logs = f"{logs_result.stdout}\n{logs_result.stderr}"
        assert sentinel not in logs
        assert api_path in logs
        assert "status=502" in logs
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )


def test_backend_uvicorn_default_access_log_is_disabled():
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    command_match = re.search(r"^CMD (\[.*\])$", dockerfile, flags=re.MULTILINE)

    assert command_match is not None
    command = json.loads(command_match.group(1))
    assert command[0] == "uvicorn"
    assert "--no-access-log" in command


def test_database_and_sqlalchemy_logs_hide_bind_parameters():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "log_min_duration_statement=1000" in compose
    assert "log_parameter_max_length=0" in compose
    assert "log_parameter_max_length_on_error=0" in compose
    assert "log_statement=none" in compose
    assert engine.hide_parameters is True

    sensitive = "客户合同搜索词-SQL-LOG-SENTINEL"
    error = StatementError(
        "synthetic database failure",
        "SELECT :q",
        {"q": sensitive},
        RuntimeError("synthetic"),
        hide_parameters=engine.hide_parameters,
    )
    rendered = str(error)
    assert sensitive not in rendered
    assert "SQL parameters hidden" in rendered
