"""Real pinned PDP fixture shared by authorization and sales integration tests."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest


def local_cerbos_config(source: str, *, http_port: int, grpc_port: int, policies: Path) -> str:
    """Render fixture fields once; generated addresses must never be substituted again."""
    replacements = {
        "httpListenAddr": f"127.0.0.1:{http_port}",
        "grpcListenAddr": f"127.0.0.1:{grpc_port}",
        # A JSON string is also a YAML quoted scalar, including spaces and colons.
        "directory": json.dumps(str(policies)),
    }
    counts = dict.fromkeys(replacements, 0)

    def replace_field(match):
        field = match["field"]
        counts[field] += 1
        return f'{match["indent"]}{field}: {replacements[field]}'

    config = re.sub(
        r"^(?P<indent>[ \t]*)(?P<field>httpListenAddr|grpcListenAddr|directory):[^\n]*$",
        replace_field,
        source,
        flags=re.MULTILINE,
    )
    if any(count != 1 for count in counts.values()):
        raise ValueError("Cerbos fixture requires exactly one HTTP, gRPC and policy-directory field")
    return config


@contextmanager
def running_cerbos(directory: Path):
    binary = os.environ.get("CERBOS_BINARY") or shutil.which("cerbos")
    if not binary:
        pytest.skip("Set CERBOS_BINARY to the pinned Cerbos 0.57.0 binary")
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True)
    if "0.57.0" not in version.stdout:
        pytest.fail("Authorization integration requires Cerbos 0.57.0")
    root = Path(__file__).resolve().parents[2]
    policies = root / "deploy/cerbos/policies"
    # Separate HTTP, gRPC and debug sockets, all restricted to loopback.
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        http_port, grpc_port, debug_port = [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
    directory.mkdir(parents=True, exist_ok=True)
    config = local_cerbos_config(
        (root / "deploy/cerbos/config.yaml").read_text(),
        http_port=http_port, grpc_port=grpc_port, policies=policies,
    )
    config_path = directory / "config.yaml"
    config_path.write_text(config)
    url = f"http://127.0.0.1:{http_port}"
    log_path = directory / "cerbos.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([
            binary, "server", "--config", str(config_path),
            "--debug-listen-addr", f"127.0.0.1:{debug_port}",
        ], stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "CERBOS_NO_TELEMETRY": "1"})
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail(f"Cerbos failed to start: {log_path.read_text()}")
                try:
                    with urlopen(f"{url}/_cerbos/health", timeout=.2) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    time.sleep(.05)
            else:
                pytest.fail("Cerbos did not become healthy")
            yield url
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.fixture(scope="session")
def cerbos_url(tmp_path_factory):
    with running_cerbos(tmp_path_factory.mktemp("authz-pdp")) as url:
        yield url
