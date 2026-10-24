import { StrictMode, startTransition, useEffect, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AuthzProvider, useAuthz } from "../AuthzProvider";
import { AuthorizationCancelled, useAuthorizedQuery, useAuthorizedRunner, type AuthorizedRequest } from "../access";
import { actionRule } from "../rules";
import { authzSnapshot } from "../../test/authzFixtures";

const { getAuthzSnapshot } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn() }));
vi.mock("../../api/authz", async original => ({ ...await original<typeof import("../../api/authz")>(), getAuthzSnapshot }));
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; }
const value = (version: string) => authzSnapshot({ enabled: true, read: true, export: true, scope: "all", customer_count: 0, reason: "", field_permissions: [] }, { version, mode: "unified_modules" });
let root: Root, host: HTMLDivElement, previousActEnvironment: unknown;
const actEnvironment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: unknown };
beforeEach(() => {
  // Preserve browser concurrent-root/passive-effect ordering. RTL's act flushes
  // effect setState before the request microtask and masks this regression.
  previousActEnvironment = actEnvironment.IS_REACT_ACT_ENVIRONMENT;
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = false;
  localStorage.setItem("token", "A");
  getAuthzSnapshot.mockReset();
  getAuthzSnapshot.mockImplementation(async (token: string) => ({ data: value(token) }));
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { root.unmount(); host.remove(); actEnvironment.IS_REACT_ACT_ENVIRONMENT = previousActEnvironment; });
function Query({ query, id = 1 }: { query: (capture: AuthorizedRequest) => Promise<string>; id?: number }) {
  const result = useAuthorizedQuery({ rule: actionRule("sales_analysis", "read_groups"), queryKey: [id], query });
  return <output>{result.data ?? (result.loading ? "loading" : "empty")}</output>;
}
function Ready({ children }: { children: ReactNode }) {
  const authz = useAuthz();
  return <><span>{authz.status}</span>{authz.status === "ready" && children}</>;
}
const show = (token: string, child: ReactNode) => root.render(<StrictMode><AuthzProvider token={token}><Ready>{child}</Ready></AuthzProvider></StrictMode>);

it("a ready route first mounted in StrictMode sends one captured request without a later interaction", async () => {
  const query = vi.fn(async (capture: AuthorizedRequest) => `rows:${capture.token}:${capture.queryKey}`);
  function MountedPageWork() {
    useEffect(() => {
      const started = performance.now();
      while (performance.now() - started < 12) { /* synchronous page setup occupies one frame */ }
    }, []);
    return null;
  }
  show("A", <div>route pending</div>);
  await vi.waitFor(() => expect(host.textContent).toContain("ready"));
  startTransition(() => show("A", <><Query query={query} /><MountedPageWork /></>));
  await vi.waitFor(() => expect(host.textContent).toContain("rows:A:[1]"));
  expect(query).toHaveBeenCalledTimes(1);
  expect(query.mock.calls[0][0]).toMatchObject({ token: "A", snapshotVersion: "A", queryKey: "[1]" });
  expect(query.mock.calls[0][0].signal.aborted).toBe(false);
});

it("a query-only change keeps the later response when the previous query finishes last", async () => {
  const old = deferred<string>(), current = deferred<string>();
  const query = vi.fn((capture: AuthorizedRequest) => capture.queryKey === "[1]" ? old.promise : current.promise);
  show("A", <Query query={query} />);
  await vi.waitFor(() => expect(query).toHaveBeenCalledTimes(1));
  show("A", <Query query={query} id={2} />);
  await vi.waitFor(() => expect(query).toHaveBeenCalledTimes(2));
  const [first, second] = query.mock.calls.map(([capture]) => capture);
  expect(first.signal.aborted).toBe(true);
  expect(second).toMatchObject({ token: first.token, snapshotVersion: first.snapshotVersion, generation: first.generation, queryKey: "[2]" });
  current.resolve("query-2-current");
  await vi.waitFor(() => expect(host.textContent).toContain("query-2-current"));
  old.resolve("query-1-late"); await old.promise;
  await vi.waitFor(() => expect(host.textContent).not.toContain("query-1-late"));
  expect(host.textContent).toContain("query-2-current");
});

it("StrictMode setup does not revive query work after an actual route unmount", async () => {
  const pending = deferred<string>();
  const query = vi.fn((_capture: AuthorizedRequest) => pending.promise);
  show("A", <Query query={query} />);
  await vi.waitFor(() => expect(query).toHaveBeenCalledTimes(1));
  show("A", <div>route closed</div>);
  await vi.waitFor(() => expect(host.textContent).toContain("route closed"));
  expect(query.mock.calls[0][0].signal.aborted).toBe(true);
  pending.resolve("late private rows"); await pending.promise;
  await vi.waitFor(() => expect(host.textContent).not.toContain("late private rows"));
  expect(query).toHaveBeenCalledTimes(1);
});

it("StrictMode actor and query changes abort the old capture and only show the current response", async () => {
  const old = deferred<string>(), current = deferred<string>();
  const query = vi.fn((capture: AuthorizedRequest) => capture.token === "A" ? old.promise : current.promise);
  show("A", <Query query={query} />);
  await vi.waitFor(() => expect(query).toHaveBeenCalledTimes(1));
  localStorage.setItem("token", "B"); show("B", <Query query={query} id={2} />);
  await vi.waitFor(() => expect(query).toHaveBeenCalledTimes(2));
  expect(query.mock.calls[0][0].signal.aborted).toBe(true);
  expect(query.mock.calls[1][0]).toMatchObject({ token: "B", snapshotVersion: "B", queryKey: "[2]" });
  old.resolve("A-private"); current.resolve("B-current");
  await vi.waitFor(() => expect(host.textContent).toContain("B-current"));
  expect(host.textContent).not.toContain("A-private");
});

it("one user command is not replayed by StrictMode and an unmounted completion cannot commit", async () => {
  const pending = deferred<string>(), commit = vi.fn(), errors: unknown[] = [];
  const request = vi.fn((_capture: AuthorizedRequest) => pending.promise);
  function Command() {
    const run = useAuthorizedRunner();
    return <button onClick={() => { void run({ rule: actionRule("sales_analysis", "export"), request, commit }).catch(error => errors.push(error)); }}>export</button>;
  }
  show("A", <Command />);
  await vi.waitFor(() => expect(host.querySelector("button")).not.toBeNull());
  host.querySelector("button")!.click();
  await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  show("A", <div>route closed</div>);
  await vi.waitFor(() => expect(host.textContent).toContain("route closed"));
  expect(request.mock.calls[0][0].signal.aborted).toBe(true);
  pending.resolve("download bytes");
  await vi.waitFor(() => expect(errors[0]).toBeInstanceOf(AuthorizationCancelled));
  expect(request).toHaveBeenCalledTimes(1); expect(commit).not.toHaveBeenCalled();
});
