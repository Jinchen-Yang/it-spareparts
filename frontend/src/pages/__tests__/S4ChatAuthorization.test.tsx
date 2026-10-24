import { beforeEach, afterEach, it, expect, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import { api } from "../../api";
import { AuthzProvider } from "../../authz/AuthzProvider";
import { authzSnapshot } from "../../test/authzFixtures";
import { vocabulary } from "../../authz/rules";
import type { AuthorizationContext } from "../../authz/contextTypes";
import ChatPage from "../ChatPage";
const { snapshot } = vi.hoisted(() => ({ snapshot: vi.fn() }));
vi.mock("../../api/authz", async original => ({ ...await original<typeof import("../../api/authz")>(), getAuthzSnapshot: snapshot }));
const request = vi.spyOn(api, "request");
const allow = { effect: "allow" as const, reason: null };
function actor(grants: Record<string, string[]>) {
  const value = authzSnapshot({ enabled: true, read: false, export: false, scope: "none", customer_count: 0, reason: "", field_permissions: [] }, { mode: "unified_modules" });
  for (const id of ["agent_chat", "private_chat_session", "agent_file"]) value.resources[id] = { ...value.resources[id], runtime_state: "enabled", configurable: true, grant_version: 1, scope_kind: "self", field_permissions: [],
    decisions: Object.fromEntries(Object.entries(vocabulary[id].actions).map(([action, descriptor]) => [action, { effect: grants[id]?.includes(action) ? descriptor.kind === "display" ? "allow" : "requires_context" : "deny", reason: null }])) };
  return value;
}
function context(id: string, requirements: Record<string, string[]>, type = "chat_session"): AuthorizationContext {
  return { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0,
    context: { kind: "object", object_type: type, id, authorization_revision: `revision-${id}` },
    resources: Object.fromEntries(Object.entries(requirements).map(([id, actions]) => [id, { grant_version: 1, field_permissions: [], decisions: Object.fromEntries(actions.map(action => [action, allow])) }])) };
}
const resolve = (ctx: AuthorizationContext) => ({ data: { locator: { object_type: ctx.context.kind === "object" ? ctx.context.object_type : "", object_id: ctx.context.id }, authorization_context: ctx } });
function pending<T>() { let resolve!: (value: T) => void; return { promise: new Promise<T>(done => resolve = done), resolve }; }
function mount(grants: Record<string, string[]>, path = "/chat?session_id=7") {
  snapshot.mockResolvedValue({ data: actor(grants) });
  return render(<MemoryRouter initialEntries={[path]}><AuthzProvider token="chat-owner"><ChatPage /></AuthzProvider></MemoryRouter>);
}
function sse(ctx: AuthorizationContext, frames: object[]) {
  const run = "a".repeat(32);
  return new Response(new TextEncoder().encode([{ type: "ready", schema_version: 1, session_id: 7, run_id: run, authorization_context: ctx, replay_complete: true }, ...frames.map((frame, index) => ({ ...frame, run_id: run, seq: index + 1 }))].map(frame => `data: ${JSON.stringify(frame)}\n\n`).join("")), { headers: { "Content-Type": "text/event-stream" } });
}
beforeEach(() => { localStorage.clear(); localStorage.setItem("token", "chat-owner"); request.mockReset(); Element.prototype.scrollIntoView = vi.fn(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("history-only deep link resolves its exact object before facts and never lists/creates/runs", async () => {
  const gate = pending<any>(), ctx = context("7", { private_chat_session: ["read"] });
  request.mockReturnValueOnce(gate.promise).mockResolvedValueOnce({ data: { items: [{ role: "assistant", content: "私有历史" }], authorization_context: ctx } });
  mount({ private_chat_session: ["show_history", "read"] });
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  expect(request.mock.calls[0][0]).toMatchObject({ url: "/agent/sessions/7/messages", params: { view: "authorization" } });
  expect(screen.queryByText("私有历史")).toBeNull(); expect(screen.queryByRole("textbox", { name: "聊天消息" })).toBeNull();
  await act(async () => gate.resolve(resolve(ctx)));
  expect(await screen.findByText("私有历史")).toBeVisible(); expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls[1][0]).toMatchObject({ headers: { Authorization: "Bearer chat-owner" }, params: { authorization_precondition: expect.any(String) } });
});
it("read without show never issues a history request", async () => {
  mount({ private_chat_session: ["read", "show_delete", "delete"] });
  expect(await screen.findByRole("button", { name: "删除当前会话" })).toBeVisible(); expect(request).not.toHaveBeenCalled(); expect(screen.queryByText("新对话")).toBeNull();
});
it("attach-only accepts a canonical stream without producer run/use/history", async () => {
  const ctx = context("7", { private_chat_session: ["attach"] }); request.mockResolvedValue(resolve(ctx));
  const fetch = vi.fn().mockResolvedValue(sse(ctx, [{ type: "delta", text: "接续结果" }, { type: "done", configured: true, stopped: false, tool_calls: [] }])); vi.stubGlobal("fetch", fetch);
  mount({ private_chat_session: ["show_live", "attach"] });
  fireEvent.click(await screen.findByRole("button", { name: "接续实时结果" }));
  expect(await screen.findByText("接续结果")).toBeVisible(); expect(request).toHaveBeenCalledTimes(1); expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][0]).toBe("/api/agent/sessions/7/chat/attach");
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ authorization_precondition: { catalog_version: ctx.catalog_version, authorization_epoch: 0, context: ctx.context } });
});
it("409 never auto-resubmits and never commits partial assistant history", async () => {
  const ctx = context("7", { agent_chat: ["use"], private_chat_session: ["run"] }); request.mockResolvedValue(resolve(ctx));
  const fetch = vi.fn().mockResolvedValue(new Response("busy", { status: 409 })); vi.stubGlobal("fetch", fetch);
  mount({ agent_chat: ["show_conversation", "use"], private_chat_session: ["run"] });
  fireEvent.change(await screen.findByRole("textbox", { name: "聊天消息" }), { target: { value: "当前请求" } }); fireEvent.click(screen.getByRole("button", { name: "发送" }));
  expect(await screen.findByText(/聊天请求失败（409）/)).toBeVisible(); expect(fetch).toHaveBeenCalledTimes(1); expect(request).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("当前请求")).toBeNull(); expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
});
it("switching target aborts a late authorization before stream execution", async () => {
  const gate = pending<any>(), fetch = vi.fn(); request.mockReturnValue(gate.promise); vi.stubGlobal("fetch", fetch);
  mount({ private_chat_session: ["show_live", "attach"] }); fireEvent.click(await screen.findByRole("button", { name: "接续实时结果" }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1)); const signal = request.mock.calls[0][0].signal as AbortSignal;
  fireEvent.change(screen.getByRole("textbox", { name: "已知会话 ID" }), { target: { value: "8" } }); fireEvent.click(screen.getByRole("button", { name: "打开会话" }));
  expect(signal.aborted).toBe(true); await act(async () => gate.resolve(resolve(context("7", { private_chat_session: ["attach"] }))));
  expect(fetch).not.toHaveBeenCalled(); expect(screen.getByText("会话 #8")).toBeVisible();
});
it("preview-only image card never downloads the original", async () => {
  const history = context("7", { private_chat_session: ["read"] }), file = context("a".repeat(32), { agent_file: ["preview"] }, "agent_file");
  request.mockImplementation(async config => {
    const ctx = String(config.url).includes("/files/") ? file : history;
    if ((config.params as { view?: string } | undefined)?.view === "authorization") return resolve(ctx);
    return { data: String(config.url).includes("/files/") ? { file_id: file.context.id, filename: "图片.png", kind: "image", authorization_context: file } : { items: [{ role: "assistant", content: `[图片](/api/agent/files/${file.context.id})` }], authorization_context: history } } as any;
  });
  mount({ private_chat_session: ["show_history", "read"], agent_file: ["show_preview", "preview"] }); fireEvent.click(await screen.findByRole("button", { name: /预览/ }));
  await waitFor(() => expect(screen.getByText("这是图片文件。查看原件需要单独的下载权限。")).toBeVisible()); expect(request).toHaveBeenCalledTimes(4);
  expect(request.mock.calls.every(([config]) => !String(config.url).endsWith(file.context.id))).toBe(true); expect(screen.queryByRole("button", { name: /下载/ })).toBeNull();
});
