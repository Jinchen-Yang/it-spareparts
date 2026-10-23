"""Real pinned PDP fixture shared by authorization and sales integration tests."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest


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
    config = (root / "deploy/cerbos/config.yaml").read_text()
    config = config.replace("0.0.0.0:3592", f"127.0.0.1:{http_port}")
    config = config.replace("127.0.0.1:3593", f"127.0.0.1:{grpc_port}")
    config = config.replace("directory: /policies", f"directory: {policies}")
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
