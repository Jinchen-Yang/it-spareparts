"""生产 HTTPS 入口的部署契约。"""

from __future__ import annotations

import json
import os
import textwrap
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DOC = REPO_ROOT / "docs" / "DEPLOY.md"
HTTPS_RUNBOOK = REPO_ROOT / "docs" / "releases" / "https-ingress-runbook.md"
CADDY_TEMPLATE = REPO_ROOT / ".deploy" / "Caddyfile.it-data.example"
ROLLBACK_SCRIPT = REPO_ROOT / ".deploy" / "rollback_https_ingress.sh"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _render_compose(*files: Path, cwd: Path = REPO_ROOT) -> dict[str, object]:
    command = ["docker", "compose"]
    for file in files:
        command.extend(["-f", str(file)])
    command.extend(["config", "--format", "json"])
    result = subprocess.run(
        command,
        cwd=cwd,
        env={
            **os.environ,
            "POSTGRES_PASSWORD": "test-postgres-password",
            "ADMIN_PASSWORD": "test-admin-password",
            "SECRET_KEY": "test-secret-key-with-at-least-32-bytes",
            "FRONTEND_PORT": "8080",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _compose_config() -> dict[str, object]:
    return _render_compose(REPO_ROOT / "docker-compose.yml")


def test_frontend_port_is_loopback_only() -> None:
    """HTTPS 之外不得保留可绕过 TLS 的公网前端端口。"""
    config = _compose_config()
    services = config["services"]
    assert isinstance(services, dict)
    frontend = services["frontend"]
    assert isinstance(frontend, dict)

    assert frontend["ports"] == [
        {
            "mode": "ingress",
            "target": 80,
            "published": "8080",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]


def test_public_proxy_network_reaches_frontend_only() -> None:
    """边缘代理不得因 HTTPS 接入而横向直达 app 或数据库。"""
    config = _compose_config()
    services = config["services"]
    networks = config["networks"]
    assert isinstance(services, dict)
    assert isinstance(networks, dict)

    assert services["frontend"]["networks"]["ingress"]["aliases"] == [
        "it-spareparts-frontend"
    ]
    assert "ingress" not in services["app"]["networks"]
    assert "ingress" not in services["db"]["networks"]
    assert networks["ingress"]["name"] == "it-spareparts-ingress"
    assert networks["ingress"]["internal"] is True


def test_https_transition_override_closes_legacy_public_port(tmp_path: Path) -> None:
    """旧版本切 HTTPS 时，覆盖文件也必须只发布回环端口。"""
    legacy_compose = tmp_path / "docker-compose.yml"
    legacy_compose.write_text(
        textwrap.dedent(
            """\
            services:
              frontend:
                image: nginx:alpine
                ports:
                  - "${FRONTEND_PORT:-8080}:80"
            """
        ),
        encoding="utf-8",
    )
    config = _render_compose(
        legacy_compose,
        REPO_ROOT / ".deploy" / "docker-compose.https.yml",
        cwd=tmp_path,
    )
    services = config["services"]
    assert isinstance(services, dict)
    frontend = services["frontend"]
    assert isinstance(frontend, dict)

    assert frontend["ports"] == [
        {
            "mode": "ingress",
            "target": 80,
            "published": "8080",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]


def test_deployment_guidance_cannot_reopen_plaintext_login() -> None:
    """运维文档不得把公网 HTTP 重新变成受支持的登录入口。"""
    deploy_doc = DEPLOY_DOC.read_text(encoding="utf-8")
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "http://<服务器公网IP>:8080" not in deploy_doc
    assert "sudo ufw allow 8080" not in deploy_doc
    assert "来源 `0.0.0.0/0`" not in deploy_doc
    assert "不得放行 8080" in deploy_doc
    assert "禁止开放公网 8080" in env_example


def test_caddy_template_and_runbook_preserve_https_safety_boundary() -> None:
    """代理模板必须独立成站、保留关键响应头，并支持安全回滚。"""
    template = CADDY_TEMPLATE.read_text(encoding="utf-8")
    runbook = HTTPS_RUNBOOK.read_text(encoding="utf-8")

    assert "https://{$IT_DATA_HOST}" in template
    assert "reverse_proxy {$IT_DATA_UPSTREAM}" in template
    assert 'Strict-Transport-Security "max-age={$IT_DATA_HSTS_MAX_AGE:300}"' in template
    assert 'X-Content-Type-Options "nosniff"' in template
    assert "includeSubDomains" not in template
    assert "preload" not in template

    assert "域名所有者已确认" in runbook
    assert "不能挂在" in runbook
    assert "不得使用\n`it-spareparts_default`" in runbook
    assert "IT_DATA_UPSTREAM: it-spareparts-frontend:80" in runbook
    assert "name: it-spareparts-ingress" in runbook
    assert ".https_monitor_url" in runbook
    assert "HTTPS_BUNDLE_SHA256" in runbook
    assert "rollback_https_ingress.sh" in runbook
    assert "ROLLBACK_LOCK_DIR=/run/lock/it-spareparts-https-rollback" in runbook
    assert 'sudo mkdir --mode=700 -- "$ROLLBACK_LOCK_DIR"' in runbook
    assert 'sudo test ! -L "$ROLLBACK_LOCK_DIR"' in runbook
    assert "for artifact_path in" in runbook
    assert "for path in" not in runbook
    assert "gw_priority: 1" in runbook
    assert "EXPECTED_HSTS_MAX_AGE=31536000" in runbook
    assert "assistant-compose.candidate.normalized.sha256" in runbook
    assert 'environment["IT_DATA_HSTS_MAX_AGE"] = "<normalized>"' in runbook
    assert 'test "$(id -un)" = ubuntu' in runbook
    assert "rollback-now.sh" in runbook
    assert (
        "ROLLBACK_CONTROL_DIR=/var/lib/it-spareparts-release-control"
        in runbook
    )
    assert (
        "sudo /var/lib/it-spareparts-release-control/rollback-now.sh"
        in runbook
    )
    assert 'ROLLBACK_NOW="$EVIDENCE_DIR/' not in runbook
    assert 'sudo "$ROLLBACK_NOW"' not in runbook
    assert "cache-control: no-store" in runbook
    assert "filename\\\\*=UTF-8''" in runbook
    assert "tr -d '\\r'" in runbook
    assert "\\r?$" not in runbook
    assert (
        "sudo /usr/local/sbin/it-spareparts-https-rollback \\\n"
        '  "$EVIDENCE_DIR" "$ASSISTANT_SMOKE_URL"'
    ) in runbook
    assert (
        "/home/ubuntu/apps/it-spareparts/.deploy/rollback_https_ingress.sh"
        not in runbook
    )
    assert "不得把重新开放明文 8080 当成常规回滚" in runbook
    assert "0、5、15、30 分钟" in runbook
    assert runbook.count(
        "caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile"
    ) >= 4
    assert runbook.count(
        'curl --proto \'=https\' --tlsv1.2 -fsS "$ASSISTANT_SMOKE_URL"'
    ) >= 3

    mode = ROLLBACK_SCRIPT.stat().st_mode
    assert mode & 0o100
    subprocess.run(["bash", "-n", str(ROLLBACK_SCRIPT)], check=True)


def test_runbook_header_normalization_accepts_curl_crlf(tmp_path: Path) -> None:
    """curl -D 保留 CRLF；验收必须先去掉 CR 再做精确匹配。"""
    headers = tmp_path / "headers"
    headers.write_bytes(
        b"HTTP/2 200\r\n"
        b"cache-control: no-store\r\n"
        b"x-content-type-options: nosniff\r\n"
        b"strict-transport-security: max-age=300\r\n"
    )
    command = r"""
    set -Eeuo pipefail
    normalized=$(tr -d '\r' < "$HEADERS")
    grep -Eqi '^cache-control: no-store$' <<<"$normalized"
    grep -Eqi '^x-content-type-options: nosniff$' <<<"$normalized"
    grep -Eqi '^strict-transport-security: max-age=300$' <<<"$normalized"
    """

    subprocess.run(
        ["bash", "-c", command],
        check=True,
        env={**os.environ, "HEADERS": str(headers)},
    )
