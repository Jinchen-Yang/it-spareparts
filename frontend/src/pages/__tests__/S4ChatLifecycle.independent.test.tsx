import { beforeEach, afterEach, it, expect, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { Grid } from "antd";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import { api } from "../../api";
import { AuthzProvider } from "../../authz/AuthzProvider";
import { authzSnapshot } from "../../test/authzFixtures";
import { vocabulary } from "../../authz/rules";
import type { AuthorizationContext } from "../../authz/contextTypes";
import ChatPage from "../ChatPage";
import generatedFile from "../../../../docs/reviews/authz-rewrite-20261023/s4-agent-generated-file-wire.json";

const { snapshot } = vi.hoisted(() => ({ snapshot: vi.fn() }));
vi.mock("../../api/authz", async original => ({ ...await original<typeof import("../../api/authz")>(), getAuthzSnapshot: snapshot }));
const request = vi.spyOn(api, "request");
const allow = { effect: "allow" as const, reason: null };
function context(id: string, resource: string, action: string, type: string): AuthorizationContext {
  return { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0,
    context: { kind: "object", object_type: type, id, authorization_revision: `revision-${id}` },
    resources: { [resource]: { grant_version: 1, field_permissions: [], decisions: { [action]: allow } } } };
}
function resolve(ctx: AuthorizationContext) {
  return { data: { locator: { object_type: ctx.context.kind === "object" ? ctx.context.object_type : "", object_id: ctx.context.id }, authorization_context: ctx } };
}
function pending<T>() { let resolve!: (value: T) => void; return { promise: new Promise<T>(done => resolve = done), resolve }; }
function mount(grants: Record<string, string[]> = { private_chat_session: ["show_create", "create", "show_delete", "delete"], agent_file: ["show_upload", "upload"] }) {
  const value = authzSnapshot({ enabled: true, read: false, export: false, scope: "none", customer_count: 0, reason: "", field_permissions: [] }, { mode: "unified_modules" });
  for (const id of ["agent_chat", "private_chat_session", "agent_file"]) value.resources[id] = { ...value.resources[id], runtime_state: "enabled", configurable: true, grant_version: 1, scope_kind: "self", field_permissions: [],
    decisions: Object.fromEntries(Object.entries(vocabulary[id].actions).map(([action, descriptor]) => [action, { effect: grants[id]?.includes(action) ? descriptor.kind === "display" ? "allow" : "requires_context" : "deny", reason: null }])) };
  snapshot.mockResolvedValue({ data: value });
  return render(<MemoryRouter initialEntries={["/chat?session_id=7"]}><AuthzProvider token="chat-owner"><ChatPage /></AuthzProvider></MemoryRouter>);
}
beforeEach(() => { localStorage.clear(); localStorage.setItem("token", "chat-owner"); request.mockReset(); Element.prototype.scrollIntoView = vi.fn(); vi.spyOn(Grid, "useBreakpoint").mockReturnValue({ md: true }); });
afterEach(() => { cleanup(); vi.mocked(Grid.useBreakpoint).mockRestore(); });

it.each(["create", "delete"] as const)("late upload from prior session is cancelled on %s target transition", async transition => {
  const upload = pending<any>();
  request.mockImplementation(async config => {
    if (config.url === "/agent/upload") {
      if ((config.params as any)?.view) return resolve(context("a".repeat(64), "agent_file", "upload", "operation_input"));
      return upload.promise;
    }
    if (config.url === "/agent/sessions") return (config.params as any)?.view
      ? resolve(context("b".repeat(64), "private_chat_session", "create", "operation_input")) : { data: { id: 8, title: "new target" } };
    if (config.url === "/agent/sessions/7") return (config.params as any)?.view
      ? resolve(context("7", "private_chat_session", "delete", "chat_session")) : { data: { ok: true } };
    throw new Error(`unexpected request ${config.url}`);
  });
  mount(); await screen.findByText("会话 #7");
  fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [new File(["old target input"], "old-target.txt", { type: "text/plain" })] } });
  await waitFor(() => expect(request.mock.calls.filter(([c]) => c.url === "/agent/upload")).toHaveLength(2));
  const signal = request.mock.calls.find(([c]) => c.url === "/agent/upload" && !(c.params as any)?.view)![0].signal as AbortSignal;
  if (transition === "create") {
    fireEvent.click(await screen.findByRole("button", { name: /新对话/ }));
    await screen.findByText("会话 #8");
  } else {
    fireEvent.click(screen.getByRole("button", { name: "删除当前会话" }));
    fireEvent.click(await screen.findByRole("button", { name: /^(OK|确\s*定)$/ }));
    await waitFor(() => expect(screen.queryByText("会话 #7")).toBeNull());
  }
  await act(async () => upload.resolve({ data: { file_id: "abcdef123456", ext: "txt" } }));
  expect(screen.queryByText(/📎 old-target\.txt/)).toBeNull();
  expect(screen.queryByText("已附加 old-target.txt，输入要求后发送")).toBeNull();
  expect(signal.aborted).toBe(true);
  expect(request.mock.calls.filter(([c]) => c.url === "/agent/upload")).toHaveLength(2);
});

it("actual worker generated file locator renders a separately authorized card", async () => {
  // The content/id are unchanged output from the real PG/PDP/write_report worker.
  // Only this UI test's authorization observation is synthetic; actual wire validation
  // remains separately covered by s4AgentWire.test.ts against the HTTP golden.
  const history = context("7", "private_chat_session", "read", "chat_session");
  const file = context(generatedFile.file_id, "agent_file", "preview", "agent_file");
  request.mockImplementation(async config => {
    const isFile = String(config.url).includes("/files/");
    const ctx = isFile ? file : history;
    if ((config.params as any)?.view) return resolve(ctx);
    return { data: isFile ? { file_id: generatedFile.file_id, filename: "合成报表.xlsx", kind: "table", sheets: [{ name: "Sheet1", rows: [["标识", "数量"], ["S4-QA-ONLY", 2]], total_rows: 2, truncated: false }], authorization_context: file }
      : { items: [{ role: "assistant", content: generatedFile.content }], authorization_context: history } };
  });
  mount({ private_chat_session: ["show_history", "read"], agent_file: ["show_preview", "preview"] });
  const button = await screen.findByRole("button", { name: /预览/ });
  expect(request).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("button", { name: /下载/ })).toBeNull();
  fireEvent.click(button);
  await waitFor(() => expect(screen.getByText("S4-QA-ONLY")).toBeVisible());
  expect(request).toHaveBeenCalledTimes(4);
  expect(request.mock.calls.filter(([config]) => String(config.url).includes("/files/")).map(([config]) => config.url))
    .toEqual([`/agent/files/${generatedFile.file_id}/preview`, `/agent/files/${generatedFile.file_id}/preview`]);
});
