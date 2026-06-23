#!/usr/bin/env node
/**
 * API smoke driver for the IT 备件智能管理系统 (FastAPI backend).
 * Drives the RUNNING backend — logs in, exercises the core read endpoints,
 * chains the search→overview flow, prints PASS/FAIL, exits non-zero on failure.
 *
 * Most PRs in this repo touch the backend (ETL / services / agent / API), so this
 * API-layer smoke is the primary harness. Run it after dev-up.sh (or against prod
 * with BASE=http://<host>:8080).
 *
 * Usage:
 *   node driver.mjs                         # localhost:8000, admin/admin888
 *   BASE=http://localhost:8080 node driver.mjs
 *   BASE=http://host:8080 ADMIN_USER=admin ADMIN_PASS=secret node driver.mjs
 */
const BASE = process.env.BASE || "http://localhost:8000";
const USER = process.env.ADMIN_USER || "admin";
const PASS = process.env.ADMIN_PASS || "admin888";

let pass = 0, fail = 0;
const lines = [];
const check = (name, ok, detail = "") => {
  ok ? pass++ : fail++;
  lines.push(`  ${ok ? "✓" : "✗"} ${name}${detail ? `  (${detail})` : ""}`);
};

async function main() {
  console.log(`▶ driving ${BASE} as ${USER}`);

  // 1) health — no auth
  let r = await fetch(`${BASE}/health`).catch((e) => ({ ok: false, status: 0, _e: e }));
  check("GET /health", r.ok, `HTTP ${r.status || 0}`);

  // 2) login
  r = await fetch(`${BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: USER, password: PASS }),
  });
  const login = r.ok ? await r.json() : {};
  check("POST /api/auth/login", r.ok && !!login.token, `role=${login.role}`);
  if (!login.token) {
    finish();
    return;
  }
  const H = { Authorization: `Bearer ${login.token}` };
  const get = async (path) => {
    const x = await fetch(`${BASE}/api${path}`, { headers: H });
    return { status: x.status, ok: x.ok, body: x.ok ? await x.json() : null };
  };

  // 3) core read endpoints
  let g = await get(`/parts/search?q=${encodeURIComponent("三星")}`);
  check("GET /api/parts/search", g.ok && Array.isArray(g.body?.items), `items=${g.body?.items?.length}`);
  const firstPn = g.body?.items?.[0]?.pn_std;

  g = await get(`/purchases/recent?days=3650&page=1&page_size=5`);
  check("GET /api/purchases/recent", g.ok && Array.isArray(g.body?.items), `items=${g.body?.items?.length}`);

  g = await get(`/profit?dimension=part`);
  check("GET /api/profit", g.ok && Array.isArray(g.body?.rows), `rows=${g.body?.rows?.length}`);

  g = await get(`/inventory?page=1`);
  check("GET /api/inventory", g.ok && Array.isArray(g.body?.items), `items=${g.body?.items?.length}`);

  // 4) search → overview chain (the showcase flow)
  if (firstPn) {
    g = await get(`/parts/overview?pn_std=${encodeURIComponent(firstPn)}`);
    check("GET /api/parts/overview", g.ok && !!g.body?.part, `pn=${firstPn}`);
  } else {
    check("GET /api/parts/overview", false, "no search hit to drill into — is the DB seeded?");
  }

  finish();
}

function finish() {
  console.log(lines.join("\n"));
  console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error("driver crashed:", e.message);
  console.error("  is the backend up? try:  curl " + BASE + "/health");
  process.exit(2);
});
