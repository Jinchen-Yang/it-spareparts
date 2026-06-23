#!/usr/bin/env node
/**
 * Authenticated screenshot driver for the React SPA — no npm deps (Node 22+ global
 * WebSocket + Chrome/Chromium over CDP). Logs in via the API, injects the token into
 * localStorage, navigates to a data page, and writes a PNG.
 *
 * Usage:
 *   node shot.mjs                         # -> ./shot.png, 采购记录 page
 *   node shot.mjs out.png 利润分析         # custom file + menu label
 *   FRONTEND=http://localhost:5176 BASE=http://localhost:8000 node shot.mjs
 *   CHROME=/path/to/chromium node shot.mjs        # override browser binary
 */
import { spawn } from "node:child_process";
import { writeFileSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const FRONTEND = process.env.FRONTEND || "http://localhost:5176";
const BASE = process.env.BASE || "http://localhost:8000";
const USER = process.env.ADMIN_USER || "admin";
const PASS = process.env.ADMIN_PASS || "admin888";
const OUT = resolve(process.argv[2] || "shot.png");
const PAGE_LABEL = process.argv[3] || "采购记录";
const PORT = Number(process.env.CDP_PORT || 9333);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CHROME = [
  process.env.CHROME,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium", "/usr/bin/chromium-browser",
].find((p) => p && existsSync(p));
if (!CHROME) {
  console.error("No Chrome/Chromium found. Install it or set CHROME=<path>.");
  process.exit(2);
}

// 1) login for a token (before launching the browser)
let login;
try {
  const resp = await fetch(`${BASE}/api/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: USER, password: PASS }),
  });
  login = await resp.json();
} catch (e) {
  console.error(`login request to ${BASE} failed: ${e.message} — is the backend up?`);
  process.exit(1);
}
if (!login?.token) {
  console.error("login failed (no token) — is the DB seeded? run dev-up.sh");
  process.exit(1);
}

// 2) launch headless Chrome
const udd = mkdtempSync(join(tmpdir(), "sp-shot-"));
const chrome = spawn(CHROME, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${udd}`,
  "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--window-size=1440,900",
  "about:blank",
], { stdio: "ignore" });
const cleanup = (code) => {
  try { chrome.kill("SIGKILL"); } catch {}
  try { rmSync(udd, { recursive: true, force: true }); } catch {}
  process.exit(code);
};

// 3) wait for devtools endpoint
let ver;
for (let i = 0; i < 60; i++) {
  try { ver = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); break; }
  catch { await sleep(200); }
}
if (!ver?.webSocketDebuggerUrl) { console.error("Chrome devtools didn't come up"); cleanup(2); }

// 4) minimal CDP client over the browser websocket (flattened sessions)
const ws = new WebSocket(ver.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = (e) => rej(new Error("ws error")); });
let nextId = 1;
const pending = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    const { resolve: rs, reject: rj } = pending.get(m.id); pending.delete(m.id);
    m.error ? rj(new Error(m.error.message)) : rs(m.result);
  }
};
const send = (method, params = {}, sessionId) =>
  new Promise((rs, rj) => {
    const id = nextId++; pending.set(id, { resolve: rs, reject: rj });
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });

try {
  const { targetId } = await send("Target.createTarget", { url: FRONTEND });
  const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
  const S = (m, p) => send(m, p, sessionId);
  await S("Page.enable");
  await S("Runtime.enable");
  await sleep(1300); // initial load (login screen)

  // inject auth into localStorage and reload
  const tok = JSON.stringify(login.token);
  const role = JSON.stringify(login.role || "admin");
  const name = JSON.stringify(login.name || "管理员");
  const perms = JSON.stringify(JSON.stringify(login.permissions || {}));
  await S("Runtime.evaluate", {
    expression: `(() => { localStorage.setItem('token',${tok}); localStorage.setItem('role',${role});` +
      ` localStorage.setItem('name',${name}); localStorage.setItem('permissions',${perms}); location.reload(); })()`,
  });
  await sleep(1900); // authenticated boot + default page

  // navigate to the requested data page
  await S("Runtime.evaluate", {
    expression: `(() => { const m=[...document.querySelectorAll('.ant-menu-item')]` +
      `.find(e=>e.textContent.includes(${JSON.stringify(PAGE_LABEL)})); if(m) m.click(); })()`,
  });
  await sleep(1700); // page data loads

  const { data } = await S("Page.captureScreenshot", { format: "png" });
  writeFileSync(OUT, Buffer.from(data, "base64"));
  console.log(`✓ screenshot → ${OUT}  (${login.role} · ${PAGE_LABEL})`);
  ws.close();
  cleanup(0);
} catch (e) {
  console.error("CDP flow failed:", e.message);
  cleanup(2);
}
