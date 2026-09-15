"""Opt-in real Chromium acceptance; uses only the per-run synthetic database."""

import os
import socket
import subprocess
import threading
import time
from pathlib import Path
import pytest
from tests.test_mcp import employee  # fixture
from tests.test_maintenance_acceptance_checklist import _project, _xlsx


@pytest.mark.skipif(
    not os.environ.get("MCP_BROWSER_PYTHON"),
    reason="set MCP_BROWSER_PYTHON to a Python with Playwright",
)
def test_employee_browser(db, employee, monkeypatch, tmp_path):
    import uvicorn
    from app.main import app
    from app.config import get_settings
    from app.mcp.worker import run_one

    project = _project(db, "浏览器验收项目")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(get_settings(), "mcp_public_base_url", base)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error", access_log=False
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    stop = threading.Event()

    def worker():
        while not stop.wait(0.2):
            run_one()

    work = threading.Thread(target=worker, daemon=True)
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        work.start()
        # Verify the real SDK client over an actual HTTP socket, not only JSON-RPC fixtures.
        import asyncio
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def sdk_check():
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + employee.pat["token"]},
                trust_env=False,
            ) as http:
                async with streamable_http_client(base + "/mcp", http_client=http) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        assert len(listed.tools) == 16
                        result = await session.call_tool("pf_get_capabilities", {})
                        assert not result.isError
                        assert (
                            result.structuredContent["data"]["actor_display"]
                            == "mcp-admin"
                        )

        asyncio.run(sdk_check())
        file = tmp_path / "browser.xlsx"
        file.write_bytes(_xlsx([("巡检报告归档", "是"), ("客户签字", "否")]))
        proc = subprocess.run(
            [
                os.environ["MCP_BROWSER_PYTHON"],
                str(Path(__file__).with_name("mcp_browser_driver.py")),
                base,
                str(file),
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
    finally:
        stop.set()
        if work.is_alive():
            work.join(10)
        server.should_exit = True
        thread.join(10)
