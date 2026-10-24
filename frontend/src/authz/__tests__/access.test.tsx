import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AuthzProvider, useAuthz } from "../AuthzProvider";
import { useAuthorizedQuery, useAuthorizedRunner, AuthorizationCancelled, type AuthorizedRequest } from "../access";
import { actionRule, ruleAllowed, surfaceRule, routeRule } from "../rules";
import { authzSnapshot } from "../../test/authzFixtures";
import type { AuthzSnapshot } from "../../api/authz";
const { getAuthzSnapshot } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn() }));
vi.mock("../../api/authz", async original => ({ ...await original<typeof import("../../api/authz")>(), getAuthzSnapshot }));
const snapshot = (version = "v1") => authzSnapshot({ enabled: true, read: true, export: true,
  scope: "all", customer_count: 0, reason: "", field_permissions: [] }, { version, mode: "unified_modules" });
function deferred<T>() { let resolve!: (v: T) => void; let reject!: (v: unknown) => void; const promise = new Promise<T>((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
beforeEach(() => { localStorage.setItem("token", "A"); getAuthzSnapshot.mockReset(); getAuthzSnapshot.mockResolvedValue({ data: snapshot() }); });
afterEach(cleanup);
function Probe({ query, id = 1 }: { query: (c: AuthorizedRequest) => Promise<string>; id?: number }) {
  const authz = useAuthz();
  const result = useAuthorizedQuery({ rule: actionRule("sales_analysis", "read_groups"), queryKey: [id], query });
  return <><div>{result.data || "empty"}</div><div>{authz.status}</div><button onClick={() => void authz.refresh()}>refresh</button><button onClick={result.retry}>retry</button></>;
}
it("OR keeps independent overview consumers, while display alone and old read never grant transport", () => {
  const value = snapshot();
  for (const d of Object.values(value.resources.sales_analysis.decisions)) d.effect = "deny";
  value.resources.sales_analysis.decisions.show_overview.effect = "allow";
  expect(ruleAllowed(value, surfaceRule("sales_analysis.overview"))).toBe(false);
  value.resources.sales_analysis.decisions.read_trend.effect = "allow";
  expect(ruleAllowed(value, surfaceRule("sales_analysis.overview"))).toBe(true);
  expect(ruleAllowed(value, actionRule("sales_analysis", "read_groups"))).toBe(false);
  expect(ruleAllowed(value, actionRule("sales_analysis", "read"))).toBe(false);
  expect(ruleAllowed(value, { anyOf: [[]] })).toBe(false);
});
it("embedded part reference cannot open /parts, and requires_context is never allow", () => {
  const value = snapshot();
  value.resources.pool_price_analysis = { ...value.resources.sales_analysis, decisions: {
    read: { effect: "allow", reason: null }, show_part_reference: { effect: "allow", reason: null },
  } };
  expect(ruleAllowed(value, surfaceRule("pool_price_analysis.part_reference"))).toBe(true);
  expect(ruleAllowed(value, routeRule("/parts"))).toBe(false);
  value.resources.sales_analysis.decisions.read_groups.effect = "requires_context";
  expect(ruleAllowed(value, actionRule("sales_analysis", "read_groups"))).toBe(false);
});
it("captures owner/version/queryKey; first render after query change clears old data and aborts", async () => {
  const pending = deferred<string>(), calls: AuthorizedRequest[] = [];
  const query = vi.fn((c: AuthorizedRequest) => { calls.push(c); return calls.length === 1 ? Promise.resolve("old") : pending.promise; });
  const view = render(<AuthzProvider token="A"><Probe query={query} /></AuthzProvider>);
  expect(await screen.findByText("old")).toBeVisible();
  view.rerender(<AuthzProvider token="A"><Probe query={query} id={2} /></AuthzProvider>);
  expect(screen.queryByText("old")).not.toBeInTheDocument();
  await waitFor(() => expect(query).toHaveBeenCalledTimes(2));
  expect(calls[0]).toMatchObject({ token: "A", snapshotVersion: "v1", queryKey: "[1]" });
  expect(calls[0].signal.aborted).toBe(true);
  expect(calls[1].queryKey).toBe("[2]");
  await act(async () => pending.resolve("current"));
  expect(screen.getByText("current")).toBeVisible();
});
it("refresh immediately removes old data; revoked requests cannot retry or accept late success", async () => {
  const pending = deferred<string>(); const calls: AuthorizedRequest[] = [];
  const query = vi.fn((c: AuthorizedRequest) => { calls.push(c); return pending.promise; });
  render(<AuthzProvider token="A"><Probe query={query} /></AuthzProvider>);
  await waitFor(() => expect(query).toHaveBeenCalledTimes(1));
  const denied = snapshot("v2"); denied.resources.sales_analysis.decisions.read_groups.effect = "deny";
  getAuthzSnapshot.mockResolvedValue({ data: denied });
  fireEvent.click(screen.getByText("refresh"));
  expect(screen.getByText("empty")).toBeVisible();
  expect(calls[0].signal.aborted).toBe(true);
  await waitFor(() => expect(screen.getByText("ready")).toBeVisible());
  await act(async () => pending.resolve("secret late"));
  fireEvent.click(screen.getByText("retry"));
  expect(screen.queryByText("secret late")).not.toBeInTheDocument();
  expect(query).toHaveBeenCalledTimes(1);
});
it("a 503 invalidates the shared snapshot without removing JWT or replaying", async () => {
  const query = vi.fn().mockRejectedValue({ response: { status: 503 } });
  render(<AuthzProvider token="A"><Probe query={query} /></AuthzProvider>);
  expect(await screen.findByText("error")).toBeVisible();
  expect(localStorage.getItem("token")).toBe("A");
  fireEvent.click(screen.getByText("retry"));
  expect(query).toHaveBeenCalledTimes(1);
});
it("late download after owner switch cannot commit a browser side effect", async () => {
  const pending = deferred<string>(), download = vi.fn(), errors: unknown[] = [];
  function Download() {
    const run = useAuthorizedRunner();
    return <button onClick={() => { void run({ rule: actionRule("sales_analysis", "export"), request: () => pending.promise, commit: download }).catch(e => errors.push(e)); }}>download</button>;
  }
  const view = render(<AuthzProvider token="A"><Download /></AuthzProvider>);
  await waitFor(() => expect(getAuthzSnapshot).toHaveBeenCalledTimes(1));
  await act(async () => {});
  fireEvent.click(screen.getByText("download"));
  localStorage.setItem("token", "B");
  view.rerender(<AuthzProvider token="B"><Download /></AuthzProvider>);
  await act(async () => pending.resolve("blob"));
  expect(download).not.toHaveBeenCalled();
  expect(errors[0]).toBeInstanceOf(AuthorizationCancelled);
});

it("StrictMode query remount after an already-ready Provider dispatches once and still cancels stale work", async () => {
  const pending = deferred<string>(), calls: AuthorizedRequest[] = [];
  const query = vi.fn((capture: AuthorizedRequest) => { calls.push(capture); return pending.promise; });
  function ReadyGate({ shown }: { shown: boolean }) {
    const authz = useAuthz();
    return <><div>owner:{authz.status}</div>{shown && authz.status === "ready" && <StrictMode><Probe query={query} /></StrictMode>}</>;
  }
  const view = render(<AuthzProvider token="A"><ReadyGate shown={false} /></AuthzProvider>);
  expect(await screen.findByText("owner:ready")).toBeVisible();
  view.rerender(<AuthzProvider token="A"><ReadyGate shown /></AuthzProvider>);
  await waitFor(() => expect(query).toHaveBeenCalledTimes(1));
  expect(calls[0].signal.aborted).toBe(false);
  await act(async () => pending.resolve("strict-current"));
  expect(screen.getByText("strict-current")).toBeVisible();
  view.rerender(<AuthzProvider token="A"><ReadyGate shown={false} /></AuthzProvider>);
  expect(calls[0].signal.aborted).toBe(true);
  expect(screen.queryByText("strict-current")).toBeNull();
});
