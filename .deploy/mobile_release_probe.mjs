import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const [
  chromeBin,
  loginPath,
  evidencePath,
  screenshotPath,
  workDir,
] = process.argv.slice(2);
const TEST_MODE = process.env.MOBILE_PROBE_TEST_MODE === "1";
const CDP_TIMEOUT_MS = 10_000;
const NAVIGATION_TIMEOUT_MS = 30_000;
const TEST_TIMEOUT_MIN_MS = 20;
const TEST_TIMEOUT_MAX_MS = 1_000;
const PROFILE_REMOVE_MAX_RETRIES = 5;
const PROFILE_REMOVE_RETRY_DELAY_MS = 100;
const TRANSIENT_PROFILE_REMOVE_ERRORS = new Set([
  "ENOTEMPTY",
  "EBUSY",
  "EPERM",
]);
const PRODUCTION_ORIGIN = "https://hbzgc.icu";

function boundedTestTimeout(variableName, productionTimeoutMs) {
  if (!TEST_MODE) return productionTimeoutMs;
  const requested = Number(process.env[variableName] ?? 0);
  return Number.isSafeInteger(requested) &&
    requested >= TEST_TIMEOUT_MIN_MS &&
    requested <= TEST_TIMEOUT_MAX_MS
    ? requested
    : productionTimeoutMs;
}

const COMMAND_TIMEOUT_MS = boundedTestTimeout(
  "MOBILE_PROBE_TEST_COMMAND_TIMEOUT_MS",
  CDP_TIMEOUT_MS,
);
const NAVIGATION_COMMAND_TIMEOUT_MS = boundedTestTimeout(
  "MOBILE_PROBE_TEST_NAVIGATION_TIMEOUT_MS",
  NAVIGATION_TIMEOUT_MS,
);
const requestedTestOrigin = process.env.MOBILE_PROBE_TEST_ORIGIN;
const testCleanupLog = TEST_MODE
  ? process.env.MOBILE_PROBE_TEST_CLEANUP_LOG
  : undefined;
const testProfileRemoveFailures = TEST_MODE
  ? (process.env.MOBILE_PROBE_TEST_PROFILE_RM_FAILURES ?? "")
      .split(",")
      .filter(Boolean)
  : [];
const requestedTestDeadline = Number(
  process.env.MOBILE_PROBE_TEST_OVERALL_TIMEOUT_MS ?? 0,
);
const OVERALL_TIMEOUT_MS =
  TEST_MODE &&
  Number.isSafeInteger(requestedTestDeadline) &&
  requestedTestDeadline >= 20 &&
  requestedTestDeadline <= 5_000
    ? requestedTestDeadline
    : 170_000;
const ROUTE_DELAY_MS = TEST_MODE ? 10 : 1_500;

let chrome;
let chromeExit;
let cdpInput;
let cdpOutput;
let profilePath;
let validatedWorkDir;
let validatedLoginPath;
let validatedEvidencePath;
let validatedScreenshotPath;
let loginOwned = false;
let evidenceOwned = false;
let screenshotOwned = false;
let probeAborted = false;
let stderrTail = "";
const pending = new Map();
let nextId = 1;

function withDeadline(promise, label, timeoutMs, onTimeout) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      const error = new Error(`${label} timed out`);
      onTimeout?.(error);
      reject(error);
    }, timeoutMs);
    Promise.resolve(promise).then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function exactOrigin() {
  if (!requestedTestOrigin) return PRODUCTION_ORIGIN;
  if (!TEST_MODE) {
    throw new Error("test origin requires explicit mobile probe test mode");
  }
  const parsed = new URL(requestedTestOrigin);
  if (
    parsed.protocol !== "http:" ||
    parsed.hostname !== "127.0.0.1" ||
    !parsed.port ||
    parsed.pathname !== "/"
  ) {
    throw new Error("mobile probe test origin must be loopback HTTP");
  }
  return parsed.origin;
}

function validatePrivateInputs() {
  if (
    !chromeBin ||
    !loginPath ||
    !evidencePath ||
    !screenshotPath ||
    !workDir
  ) {
    throw new Error("mobile probe arguments are incomplete");
  }
  const work = fs.lstatSync(workDir);
  if (
    !work.isDirectory() ||
    work.isSymbolicLink() ||
    (work.mode & 0o777) !== 0o700 ||
    work.uid !== process.geteuid()
  ) {
    throw new Error("mobile probe work directory is unsafe");
  }
  validatedWorkDir = fs.realpathSync(workDir);
  const directChild = (candidate, label) => {
    const resolved = path.resolve(candidate);
    if (path.dirname(resolved) !== validatedWorkDir) {
      throw new Error(`${label} must be a direct child of the private work dir`);
    }
    return resolved;
  };
  validatedLoginPath = directChild(loginPath, "approved login response");
  validatedEvidencePath = directChild(evidencePath, "mobile evidence");
  validatedScreenshotPath = directChild(screenshotPath, "mobile screenshot");
  const login = fs.lstatSync(validatedLoginPath);
  if (
    !login.isFile() ||
    login.isSymbolicLink() ||
    (login.mode & 0o777) !== 0o600 ||
    login.uid !== process.geteuid() ||
    login.nlink !== 1
  ) {
    throw new Error("approved login response is unsafe");
  }
  for (const [candidate, label] of [
    [validatedEvidencePath, "mobile evidence"],
    [validatedScreenshotPath, "mobile screenshot"],
  ]) {
    try {
      fs.lstatSync(candidate);
    } catch (error) {
      if (error?.code === "ENOENT") continue;
      throw error;
    }
    throw new Error(`${label} already exists`);
  }
  const browser = fs.lstatSync(chromeBin);
  if (!browser.isFile() || browser.isSymbolicLink()) {
    throw new Error("Chrome binary is unsafe");
  }
  loginOwned = true;
}

function rejectPending(reason) {
  for (const { reject, timer } of pending.values()) {
    clearTimeout(timer);
    reject(reason);
  }
  pending.clear();
}

function abortProbe(reason) {
  probeAborted = true;
  rejectPending(reason);
}

function ensureActive() {
  if (probeAborted) {
    throw new Error("mobile release probe is no longer active");
  }
}

function logTestCleanup(event) {
  if (testCleanupLog) {
    fs.appendFileSync(testCleanupLog, `${event}\n`);
  }
}

function removeProfileAttempt() {
  const injectedCode = testProfileRemoveFailures.shift();
  if (injectedCode) {
    const error = new Error(
      `injected profile removal failure: ${injectedCode}`,
    );
    error.code = injectedCode;
    throw error;
  }
  fs.rmSync(profilePath, { recursive: true, force: true });
}

async function removeProfileWithRetry() {
  for (let retry = 0; ; retry += 1) {
    try {
      removeProfileAttempt();
      logTestCleanup("profile-removed");
      return;
    } catch (error) {
      const code =
        error && typeof error === "object" ? error.code : undefined;
      logTestCleanup(`profile-remove-error:${code ?? "UNKNOWN"}`);
      if (
        !TRANSIENT_PROFILE_REMOVE_ERRORS.has(code) ||
        retry >= PROFILE_REMOVE_MAX_RETRIES
      ) {
        throw error;
      }
      await new Promise((resolve) =>
        setTimeout(resolve, PROFILE_REMOVE_RETRY_DELAY_MS),
      );
    }
  }
}

async function delay(milliseconds) {
  await new Promise((resolve) => setTimeout(resolve, milliseconds));
  ensureActive();
}

function bindPipeFrames(stream) {
  let buffered = Buffer.alloc(0);
  stream.on("data", (chunk) => {
    buffered = Buffer.concat([buffered, chunk]);
    while (true) {
      const boundary = buffered.indexOf(0);
      if (boundary < 0) break;
      const frame = buffered.subarray(0, boundary);
      buffered = buffered.subarray(boundary + 1);
      if (!frame.length) continue;
      let message;
      try {
        message = JSON.parse(frame.toString("utf8"));
      } catch {
        rejectPending(new Error("CDP pipe returned malformed JSON"));
        continue;
      }
      const entry = pending.get(message.id);
      if (!entry) continue;
      pending.delete(message.id);
      clearTimeout(entry.timer);
      if (message.error) {
        entry.reject(new Error(JSON.stringify(message.error)));
      } else {
        entry.resolve(message.result);
      }
    }
  });
  stream.on("error", (error) => rejectPending(error));
  stream.on("end", () => {
    if (buffered.length) {
      rejectPending(new Error("CDP pipe ended with an incomplete frame"));
    } else {
      rejectPending(new Error("CDP pipe closed"));
    }
  });
}

function startChrome() {
  ensureActive();
  profilePath = fs.mkdtempSync(
    path.join(validatedWorkDir, "chrome-profile-"),
  );
  fs.chmodSync(profilePath, 0o700);
  const args = [
    "--headless=new",
    "--no-first-run",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--remote-debugging-pipe",
    "--window-size=375,812",
    `--user-data-dir=${profilePath}`,
    "about:blank",
  ];
  chrome = spawn(chromeBin, args, {
    stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"],
  });
  cdpInput = chrome.stdio[3];
  cdpOutput = chrome.stdio[4];
  chrome.stderr.on("data", (chunk) => {
    stderrTail = (stderrTail + chunk.toString("utf8")).slice(-16_384);
  });
  chromeExit = new Promise((resolve, reject) => {
    chrome.once("error", reject);
    chrome.once("exit", (code, signal) => resolve({ code, signal }));
  });
  chromeExit
    .then(({ code, signal }) => {
      rejectPending(
        new Error(`Chrome exited before probe completion: ${code}/${signal}`),
      );
    })
    .catch((error) => rejectPending(error));
  bindPipeFrames(cdpOutput);
}

function command(
  method,
  params = {},
  sessionId,
  timeoutMs = COMMAND_TIMEOUT_MS,
) {
  ensureActive();
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`${method} timed out`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    const message = { id, method, params };
    if (sessionId) message.sessionId = sessionId;
    cdpInput.write(`${JSON.stringify(message)}\0`, (error) => {
      if (!error) return;
      const entry = pending.get(id);
      if (!entry) return;
      pending.delete(id);
      clearTimeout(entry.timer);
      entry.reject(error);
    });
  });
}

function navigate(url, sessionId) {
  return command(
    "Page.navigate",
    { url },
    sessionId,
    NAVIGATION_COMMAND_TIMEOUT_MS,
  );
}

async function stopChrome() {
  rejectPending(new Error("mobile probe cleanup"));
  if (!chrome) return;
  cdpInput?.end();
  cdpOutput?.destroy();
  if (chrome.exitCode === null && chrome.signalCode === null) {
    chrome.kill("SIGTERM");
  }
  try {
    await withDeadline(chromeExit, "Chrome graceful exit", 1_000);
  } catch {
    if (chrome.exitCode === null && chrome.signalCode === null) {
      chrome.kill("SIGKILL");
    }
    await withDeadline(chromeExit, "Chrome forced exit", 5_000);
  }
}

async function main() {
  validatePrivateInputs();
  const origin = exactOrigin();
  const login = JSON.parse(fs.readFileSync(validatedLoginPath, "utf8"));
  if (typeof login.token !== "string" || !login.token) {
    throw new Error("approved login did not return a token");
  }
  startChrome();
  const targets = await command("Target.getTargets");
  const page = targets.targetInfos?.find((item) => item.type === "page");
  if (!page?.targetId) throw new Error("CDP page target is unavailable");
  const attached = await command("Target.attachToTarget", {
    targetId: page.targetId,
    flatten: true,
  });
  if (!attached.sessionId) throw new Error("CDP page session is unavailable");
  const sessionId = attached.sessionId;
  await command("Page.enable", {}, sessionId);
  await command("Runtime.enable", {}, sessionId);
  await command(
    "Emulation.setDeviceMetricsOverride",
    {
      width: 375,
      height: 812,
      deviceScaleFactor: 1,
      mobile: true,
    },
    sessionId,
  );
  await navigate(`${origin}/`, sessionId);
  await delay(ROUTE_DELAY_MS);
  const localValues = {
    token: login.token,
    role: login.role,
    name: login.name ?? "",
    permissions: JSON.stringify(login.permissions ?? {}),
  };
  await command(
    "Runtime.evaluate",
    {
      expression: `for (const [key,value] of Object.entries(${JSON.stringify(
        localValues,
      )})) localStorage.setItem(key,value)`,
    },
    sessionId,
  );

  const routes = [
    { expectedRoute: "/maintenance", anchor: "详细盈亏" },
    { expectedRoute: "/maintenance/downloads", anchor: "下载中心" },
    { expectedRoute: "/maintenance/reminders", anchor: "项目提醒" },
  ];
  const checks = [];
  for (const { expectedRoute, anchor } of routes) {
    await navigate(`${origin}${expectedRoute}`, sessionId);
    await delay(ROUTE_DELAY_MS);
    const evaluated = await command(
      "Runtime.evaluate",
      {
        returnByValue: true,
        expression: `(() => ({
          route: location.pathname,
          width: innerWidth,
          globalOverflow: document.documentElement.scrollWidth > innerWidth,
          failed: document.body.innerText.includes("加载失败"),
          hasContent: document.body.innerText.trim().length > 0,
          hasAnchor: document.body.innerText.includes(${JSON.stringify(anchor)})
        }))()`,
      },
      sessionId,
    );
    const check = evaluated.result.value;
    if (
      check.route !== expectedRoute ||
      check.width !== 375 ||
      check.globalOverflow ||
      check.failed ||
      !check.hasContent ||
      !check.hasAnchor
    ) {
      throw new Error(
        `mobile route assertion failed: ${expectedRoute} ${JSON.stringify(
          check,
        )}`,
      );
    }
    checks.push(check);
  }

  const downloadProbe = await command(
    "Runtime.evaluate",
    {
      awaitPromise: true,
      returnByValue: true,
      expression: `fetch("/api/maintenance/board/export?lifecycle=all", {
        headers: {Authorization: "Bearer " + localStorage.getItem("token")},
        signal: AbortSignal.timeout(15000)
      }).then(async response => {
        const result = {
          status: response.status,
          disposition: response.headers.get("content-disposition") || "",
          type: response.headers.get("content-type") || "",
          cache: response.headers.get("cache-control") || ""
        };
        if (response.body) await response.body.cancel();
        return result;
      })`,
    },
    sessionId,
  );
  const download = downloadProbe.result.value;
  if (
    download.status !== 200 ||
    !download.disposition.toLowerCase().startsWith("attachment;") ||
    !download.type.toLowerCase().startsWith("text/csv") ||
    download.cache.toLowerCase() !== "no-store"
  ) {
    throw new Error("mobile download contract failed");
  }

  await navigate(`${origin}/maintenance`, sessionId);
  await delay(ROUTE_DELAY_MS);
  await command(
    "Runtime.evaluate",
    {
      expression: `(() => {
        const style = document.createElement("style");
        style.textContent =
          ".ant-card-body,.ant-table,.ant-list-items{filter:blur(16px)!important}";
        document.head.appendChild(style);
      })()`,
    },
    sessionId,
  );
  const capture = await command(
    "Page.captureScreenshot",
    { format: "png", fromSurface: true },
    sessionId,
  );
  ensureActive();
  fs.writeFileSync(
    validatedScreenshotPath,
    Buffer.from(capture.data, "base64"),
    {
      mode: 0o600,
      flag: "wx",
    },
  );
  screenshotOwned = true;
  const lines = checks.map(
    (check) =>
      `${check.route} viewport=${check.width} overflow=0 load_failed=0`,
  );
  ensureActive();
  fs.writeFileSync(
    validatedEvidencePath,
    `${new Date().toISOString()} origin=${origin} ${lines.join(
      " ",
    )} download_probe=200\n`,
    { mode: 0o600, flag: "wx" },
  );
  evidenceOwned = true;
}

let primaryError;
try {
  await withDeadline(
    main(),
    "mobile release probe",
    OVERALL_TIMEOUT_MS,
    abortProbe,
  );
} catch (error) {
  primaryError = error;
}
abortProbe(new Error("mobile probe cleanup"));

const cleanupErrors = [];
try {
  await stopChrome();
} catch (error) {
  cleanupErrors.push(error);
}
if (profilePath) {
  try {
    await removeProfileWithRetry();
  } catch (error) {
    cleanupErrors.push(error);
  }
}
if (loginOwned) {
  try {
    fs.rmSync(validatedLoginPath, { force: true });
  } catch (error) {
    cleanupErrors.push(error);
  }
}
if (primaryError || cleanupErrors.length) {
  for (const [output, owned] of [
    [validatedEvidencePath, evidenceOwned],
    [validatedScreenshotPath, screenshotOwned],
  ]) {
    if (!owned) continue;
    try {
      fs.rmSync(output, { force: true });
    } catch (error) {
      cleanupErrors.push(error);
    }
  }
  const messages = [
    primaryError,
    ...cleanupErrors,
    stderrTail ? new Error(stderrTail.trim()) : undefined,
  ]
    .filter(Boolean)
    .map((error) => (error instanceof Error ? error.message : String(error)));
  console.error(messages.join("\n"));
  process.exitCode = 1;
}
