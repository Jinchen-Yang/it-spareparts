"""Launched by test_mcp_browser; no production credentials or data."""

import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

base, file = sys.argv[1:]
with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        executable_path=os.environ.get("MCP_BROWSER_EXECUTABLE") or None,
        args=["--no-sandbox"],
    )
    page = browser.new_page(viewport={"width": 1440, "height": 1050})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/mcp-office")
    page.locator("#username").fill("mcp-admin")
    page.locator("#password").fill("synthetic-checklist-1")
    page.get_by_role("button", name="登录", exact=True).click()
    expect(page.locator("#identity")).to_contain_text("mcp-admin")
    page.locator("#files").set_input_files(file)
    page.locator("#upload").click()
    expect(page.locator("#uploadStatus")).to_contain_text("暂存成功 1 个")
    page.locator("#family").select_option("acceptance_checklist")
    page.locator("#projectQuery").fill("浏览器验收项目")
    page.locator("#findProjects").click()
    expect(page.locator("#projects option")).to_have_count(2)
    page.locator("#projects").select_option(index=1)
    page.locator("#preview").click()
    button = page.get_by_role("button", name="确认以上差异并正式提交", exact=True)
    expect(button).to_be_enabled(timeout=20000)
    expect(page.locator("#reviews")).to_contain_text("巡检报告归档")
    expect(page.locator("#reviews")).to_contain_text("原表行号")
    button.click()
    expect(page.get_by_role("button", name="已确认提交", exact=True)).to_be_visible()
    page.locator("#export").click()
    download = page.get_by_role(
        "button", name="下载 acceptance-checklist.xlsx", exact=True
    )
    expect(download).to_be_visible(timeout=20000)
    with page.expect_download() as info:
        download.click()
    assert Path(info.value.path()).stat().st_size > 100
    page.locator("#audit").click()
    expect(page.locator("#auditResult")).to_contain_text("download_completed")
    output = Path(os.environ.get("MCP_BROWSER_OUTPUT", "/tmp/partflow-mcp-acceptance"))
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / "desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(output / "mobile.png"), full_page=True)
    assert not errors, errors
    assert page.locator("#error").inner_text() == ""
    # Expired/missing review links leave the employee logged in and able to recover.
    page.goto(base + "/mcp-office?review=missing")
    page.locator("#username").fill("mcp-admin")
    page.locator("#password").fill("synthetic-checklist-1")
    page.get_by_role("button", name="登录", exact=True).click()
    expect(page.locator("#identity")).to_contain_text("mcp-admin")
    expect(page.locator("#error")).to_contain_text("不存在")
    page.locator("#audit").click()
    expect(page.locator("#auditResult")).to_contain_text("pf_search_audit")
    assert page.locator("#error").inner_text() == ""
    assert not errors, errors
    browser.close()
