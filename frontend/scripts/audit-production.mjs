import { readFileSync, readdirSync } from "node:fs";
import { extname, join } from "node:path";
import { spawnSync } from "node:child_process";

const ALLOWED_ADVISORY =
  "https://github.com/advisories/GHSA-qwww-vcr4-c8h2";
const ALLOWED_PACKAGES = new Set(["react-router", "react-router-dom"]);
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
  if (!ALLOWED_PACKAGES.has(packageName)) {
    fail(`unexpected vulnerable runtime package: ${packageName}`);
  }
  for (const advisory of vulnerabilities[packageName].via ?? []) {
    if (
      typeof advisory === "object" &&
      advisory.url !== ALLOWED_ADVISORY
    ) {
      fail(`unexpected runtime advisory: ${advisory.url ?? advisory.title}`);
    }
  }
}

const advisoryUrls = new Set(
  packageNames.flatMap((packageName) =>
    (vulnerabilities[packageName].via ?? [])
      .filter((item) => typeof item === "object")
      .map((item) => item.url),
  ),
);
if (advisoryUrls.size !== 1 || !advisoryUrls.has(ALLOWED_ADVISORY)) {
  fail("runtime advisories do not match the reviewed RSC-only exception");
}

console.log(
  "PRODUCTION_AUDIT_OK: only GHSA-qwww-vcr4-c8h2 remains; " +
    "the BrowserRouter SPA contract excludes React Router RSC mode",
);
