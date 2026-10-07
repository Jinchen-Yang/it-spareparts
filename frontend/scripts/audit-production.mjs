import { readFileSync, readdirSync } from "node:fs";
import { extname, join } from "node:path";
import { spawnSync } from "node:child_process";

// 白名单 = 「已评审、暂不阻塞」的运行时漏洞。每条必须写明评审结论与退出条件；
// 修复版发布后应升级依赖并删除条目（处理方式参照下方 axios 条目注释）。
const ALLOWED_ADVISORIES = {
  // 2026-10-07：npm 漏洞库对 axios <1.20.0 一次性收录 12 个 high 级通告
  // （原型污染 gadget、ReDoS、头部注入、HTTP/2 代理/DNS 绕过等），
  // 官方尚无修复版（patched_versions: None），升级不可行。
  // 本项目 axios 仅用于浏览器端调用自家后端 /api（src/api.ts 的共享实例），
  // 通告受影响面集中在 Node 端适配器（fromDataURI 解析、proxy/DNS 归一、
  // HTTP/2 adapter）与未使用的 toFormData 选项；浏览器 XHR/fetch 路径的
  // 实际风险经评审可接受。退出条件：axios 发布 1.20.0 修复版后升级并移除本条目。
  axios: new Set([
    "https://github.com/advisories/GHSA-vh66-26gq-q6x8",
    "https://github.com/advisories/GHSA-9fr6-4gfg-395g",
    "https://github.com/advisories/GHSA-c29m-xwm3-cm6r",
    "https://github.com/advisories/GHSA-mghh-pgcx-3jjj",
    "https://github.com/advisories/GHSA-x97p-jq2g-jp4f",
    "https://github.com/advisories/GHSA-3pq3-5fj3-cg6v",
    "https://github.com/advisories/GHSA-542g-h47m-68v8",
    "https://github.com/advisories/GHSA-j8rh-479h-cp32",
    "https://github.com/advisories/GHSA-4hqw-qxg8-jxx2",
    "https://github.com/advisories/GHSA-m8m8-qj5v-23w3",
    "https://github.com/advisories/GHSA-44g4-m2mj-wpvx",
    "https://github.com/advisories/GHSA-r4gj-5m52-g5wh",
  ]),
  // 既有例外（2026 年评审）：React Router RSC 系列通告；
  // BrowserRouter SPA 契约已从结构上排除 RSC 模式（见下方 FORBIDDEN_RSC_SOURCE）。
  "react-router": new Set([
    "https://github.com/advisories/GHSA-qwww-vcr4-c8h2",
  ]),
  "react-router-dom": new Set([
    "https://github.com/advisories/GHSA-qwww-vcr4-c8h2",
  ]),
};
const FORBIDDEN_RSC_DEPENDENCIES = new Set([
  "@react-router/dev",
  "@react-router/node",
  "@react-router/serve",
]);
const FORBIDDEN_RSC_SOURCE = [
  /unstable_RSC/,
  /RSCHydratedRouter/,
  /RSCStaticRouter/,
  /createRequestHandler/,
  /react-server/,
];

function fail(message) {
  console.error(`PRODUCTION_AUDIT_FAILED: ${message}`);
  process.exit(1);
}

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      return sourceFiles(path);
    }
    return [".ts", ".tsx", ".js", ".jsx"].includes(extname(path))
      ? [path]
      : [];
  });
}

const packageJson = JSON.parse(readFileSync("package.json", "utf8"));
const runtimeDependencies = packageJson.dependencies ?? {};

for (const dependency of FORBIDDEN_RSC_DEPENDENCIES) {
  if (dependency in runtimeDependencies) {
    fail(`RSC dependency is installed: ${dependency}`);
  }
}

const appSource = sourceFiles("src")
  .map((path) => readFileSync(path, "utf8"))
  .join("\n");
if (!appSource.includes("BrowserRouter")) {
  fail("BrowserRouter SPA contract is missing");
}
for (const pattern of FORBIDDEN_RSC_SOURCE) {
  if (pattern.test(appSource)) {
    fail(`RSC/server API detected: ${pattern}`);
  }
}

const audit = spawnSync(
  process.platform === "win32" ? "npm.cmd" : "npm",
  [
    "audit",
    "--omit=dev",
    "--audit-level=high",
    "--json",
    "--registry=https://registry.npmjs.org",
  ],
  { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 },
);

if (audit.error) {
  fail(`npm audit could not start: ${audit.error.message}`);
}
if (![0, 1].includes(audit.status)) {
  fail(`npm audit exited unexpectedly (${audit.status}): ${audit.stderr}`);
}

let report;
try {
  report = JSON.parse(audit.stdout);
} catch {
  fail("npm audit did not return valid JSON");
}
if (report.error) {
  fail(`npm audit service error: ${JSON.stringify(report.error)}`);
}

const vulnerabilities = report.vulnerabilities ?? {};
const packageNames = Object.keys(vulnerabilities);
if (packageNames.length === 0) {
  console.log("PRODUCTION_AUDIT_OK: no known runtime vulnerabilities");
  process.exit(0);
}

for (const packageName of packageNames) {
  const allowed = ALLOWED_ADVISORIES[packageName];
  if (!allowed) {
    fail(`unexpected vulnerable runtime package: ${packageName}`);
  }
  for (const advisory of vulnerabilities[packageName].via ?? []) {
    if (typeof advisory === "object" && !allowed.has(advisory.url)) {
      fail(`unexpected runtime advisory: ${advisory.url ?? advisory.title}`);
    }
  }
}

console.log(
  "PRODUCTION_AUDIT_OK: only allowlisted advisories remain " +
    "(react-router RSC-only exception; axios <1.20.0 batch pending upstream fix, " +
    "see ALLOWED_ADVISORIES)",
);
