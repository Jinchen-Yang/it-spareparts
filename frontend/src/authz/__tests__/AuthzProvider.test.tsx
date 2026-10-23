import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import { AuthzProvider, useAuthz } from "../AuthzProvider";
import { invalidateAuthzSession } from "../events";
import type { AuthzSnapshot } from "../../api/authz";
const { getAuthzSnapshot } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn() }));
vi.mock("../../api/authz", async (importOriginal) => ({ ...await importOriginal<typeof import("../../api/authz")>(), getAuthzSnapshot }));
const snapshot = (version: string, enabled = true): AuthzSnapshot => ({ version, can_manage: false,
  sales_analysis: { enabled, read: true, export: false, scope: "assigned_customers", customer_count: 1, reason: "allowed", field_permissions: ["data_customer"] } });
function Probe() {
  const authz = useAuthz();
  return <><div>{authz.status}:{authz.snapshot?.version || "empty"}</div><button onClick={() => void authz.refresh()}>刷新</button></>;
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; }
beforeEach(() => { localStorage.clear(); getAuthzSnapshot.mockReset(); });
afterEach(cleanup);
it("A→B 切换先清旧快照，A 的迟到响应不覆盖 B", async () => {
  const a = deferred<{ data: AuthzSnapshot }>(); const b = deferred<{ data: AuthzSnapshot }>();
  getAuthzSnapshot.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);
  localStorage.setItem("token", "A");
  const view = render(<AuthzProvider token="A"><Probe /></AuthzProvider>);
  localStorage.setItem("token", "B");
  view.rerender(<AuthzProvider token="B"><Probe /></AuthzProvider>);
  expect(screen.getByText("loading:empty")).toBeInTheDocument();
  await act(async () => b.resolve({ data: snapshot("B") }));
  expect(screen.getByText("ready:B")).toBeInTheDocument();
  await act(async () => a.resolve({ data: snapshot("A") }));
  expect(screen.getByText("ready:B")).toBeInTheDocument();
});
it("同 token 的后发刷新优先，失败不能继续显示旧授权", async () => {
  localStorage.setItem("token", "A");
  getAuthzSnapshot.mockResolvedValueOnce({ data: snapshot("ready") });
  render(<AuthzProvider token="A"><Probe /></AuthzProvider>);
  expect(await screen.findByText("ready:ready")).toBeInTheDocument();
  getAuthzSnapshot.mockRejectedValueOnce(new Error("PDP unavailable"));
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  expect(await screen.findByText("error:empty")).toBeInTheDocument();
});
it("401 invalidation 清快照并拒绝尚未完成响应", async () => {
  localStorage.setItem("token", "A"); const pending = deferred<{ data: AuthzSnapshot }>();
  getAuthzSnapshot.mockReturnValueOnce(pending.promise);
  render(<AuthzProvider token="A"><Probe /></AuthzProvider>);
  act(() => invalidateAuthzSession("A"));
  expect(screen.getByText("error:empty")).toBeInTheDocument();
  await act(async () => pending.resolve({ data: snapshot("late") }));
  expect(screen.getByText("error:empty")).toBeInTheDocument();
});
it("显式关闭新模块有效；非法字段响应拒绝而不是默认授权", async () => {
  localStorage.setItem("token", "A");
  getAuthzSnapshot.mockResolvedValueOnce({ data: snapshot("legacy", false) });
  render(<AuthzProvider token="A"><Probe /></AuthzProvider>);
  expect(await screen.findByText("ready:legacy")).toBeInTheDocument();
  const invalid = snapshot("bad"); invalid.sales_analysis.field_permissions = ["data_unknown"];
  getAuthzSnapshot.mockResolvedValueOnce({ data: invalid });
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await waitFor(() => expect(screen.getByText("error:empty")).toBeInTheDocument());
});
it("同一会话刷新乱序时，旧允许结果不能覆盖较新的失败", async () => {
  localStorage.setItem("token", "A");
  const older = deferred<{ data: AuthzSnapshot }>();
  getAuthzSnapshot.mockReturnValueOnce(older.promise).mockRejectedValueOnce(new Error("unavailable"));
  render(<AuthzProvider token="A"><Probe /></AuthzProvider>);
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  expect(await screen.findByText("error:empty")).toBeInTheDocument();
  await act(async () => older.resolve({ data: snapshot("old-allow") }));
  expect(screen.getByText("error:empty")).toBeInTheDocument();
});
it("消费者重新渲染遇到静默换 token 时也不继续使用旧快照", async () => {
  localStorage.setItem("token", "A"); getAuthzSnapshot.mockResolvedValue({ data: snapshot("A") });
  const view = render(<AuthzProvider token="A"><Probe /></AuthzProvider>);
  expect(await screen.findByText("ready:A")).toBeInTheDocument();
  localStorage.setItem("token", "B");
  view.rerender(<AuthzProvider token="A"><Probe /></AuthzProvider>);
  expect(screen.getByText("loading:empty")).toBeInTheDocument();
});
