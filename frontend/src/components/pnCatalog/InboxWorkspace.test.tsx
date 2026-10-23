import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import api from "../../api";
import InboxWorkspace from "./InboxWorkspace";
import type { Inbox } from "./workflows";
const actions = ["inbox.read", "inbox.claim", "inbox.release", "inbox.assign", "gallery.read", "gallery.archive", "enrichment.recognize", "job.read"];
const snapshot = { schema_version: 1, resources: { part_gallery: { migration_state: "implemented", runtime_state: "enabled", decisions: Object.fromEntries(actions.map(a => [a, { effect: "allow" }])), field_permissions: ["inbox", "gallery"] } } };
vi.mock("../../authz/AuthzProvider", async importOriginal => ({ ...await importOriginal<object>(), useAuthz: () => ({ status: "ready", snapshot }) }));
const original = api.defaults.adapter;
beforeEach(() => { localStorage.setItem("token", "inbox-editor"); });
afterEach(() => { cleanup(); api.defaults.adapter = original; });
const initial: Inbox = { inbox_id: "item", upload_id: "upload", asset_id: "asset", group_id: null, position: 0, client_order: null, filename: "photo.jpg", size_bytes: 100, status: "ready", revision: 2, generation: 1, claimed_by: null, claim_revision: 0, claim_expires_at: null, uploaded_by: { principal_id: "account:42", client_id: "shop" }, uploaded_by_me: true, claimed_by_me: false, created_at: "2026-01-01T00:00:00Z", purpose: "common", part_id: null, assignment_key: null, recognition: null };

it("识图仅由手动触发，读取详情使用当前图片版本，服务关闭时不可提交", async () => {
  const writes: { url: string; body: unknown }[] = [];
  api.defaults.adapter = async config => {
    let data: unknown = [];
    if (config.url?.endsWith("/capabilities")) data = { media: false, recognition: true, bundle: false, research: false };
    if (config.url?.endsWith("/inbox-items")) data = [initial];
    if (config.url?.endsWith("/inbox-items/item")) data = { ...initial, revision: 4 };
    if (config.method === "post") { writes.push({ url: config.url!, body: JSON.parse(config.data) }); data = { job_id: "job", kind: "enrichment.recognize", status: "pending" }; }
    return { data: { data, meta: {} }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<InboxWorkspace />); fireEvent.click(await screen.findByRole("button", { name: "查看、核对与归档" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "刷新图片状态" })).toBeEnabled()); expect(writes).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "手动识别图片" })); await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toEqual({ url: "/pn-catalog/v1/inbox/item/recognition", body: { expected_revision: 4 } });
  expect(screen.queryByRole("button", { name: /归档到/ })).not.toBeInTheDocument();
});

it("归档核对目标权限和身份基线；丢响应后冻结完整意图并复用同一请求", async () => {
  let item = initial;
  const writes: { body: Record<string, unknown>; key: string }[] = [];
  api.defaults.adapter = async config => {
    const response = (data: unknown, meta = {}) => ({ data: { data, meta }, config, headers: {}, status: 200, statusText: "OK" });
    if (config.url?.endsWith("/capabilities")) return response({ media: false, recognition: false, bundle: false, research: false });
    if (config.url === "/parts/search") return { ...response(null), data: { items: [{ part_id: 42, pn_std: "TARGET-PN" }] } };
    if (config.url?.endsWith("/inbox-items")) return response([item]);
    if (config.url?.endsWith("/inbox-items/item")) return response(item);
    if (config.url?.endsWith("/claim")) { item = { ...item, revision: 5, claim_revision: 2, claimed_by: "account:42", claimed_by_me: true, claim_expires_at: new Date(Date.now() + 300_000).toISOString() }; return response(item); }
    if (config.url?.endsWith("/access")) return response({ part_id: 42, allowed_actions: ["inbox.assign", "gallery.archive"], readable_fields: ["inbox", "gallery"], writable_fields: ["inbox", "gallery"] });
    if (config.url?.endsWith("/gallery")) return response({}, { identity_revision: 7 });
    if (config.url?.endsWith("/assignment")) {
      writes.push({ body: JSON.parse(config.data), key: String(config.headers["Idempotency-Key"]) });
      if (writes.length === 1) throw new Error("网络中断");
      item = { ...item, status: "assigned", part_id: 42, revision: 6 }; return response(item);
    }
    return response([]);
  };
  render(<InboxWorkspace />); fireEvent.click(await screen.findByRole("button", { name: "查看、核对与归档" }));
  fireEvent.click(await screen.findByRole("button", { name: "认领图片" }));
  const search = await screen.findByLabelText("搜索归档 PN"); fireEvent.change(search, { target: { value: "TARGET" } }); fireEvent.click(screen.getByRole("button", { name: "查找型号" }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "归档 PN" })).toBeEnabled());
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "归档 PN" })); fireEvent.click(await screen.findByText("TARGET-PN"));
  fireEvent.click(screen.getByRole("button", { name: "核对目标 PN 权限和版本" }));
  const archive = await screen.findByRole("button", { name: "归档到 TARGET-PN" }); expect(writes).toHaveLength(0);
  fireEvent.click(archive); fireEvent.click(await screen.findByRole("button", { name: "OK" }));
  await screen.findByText(/归档意图已锁定/); expect(screen.getByLabelText("搜索归档 PN")).toBeDisabled(); expect(screen.getByRole("combobox", { name: "归档 PN" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "归档到 TARGET-PN" })); fireEvent.click(await screen.findByRole("button", { name: "OK" }));
  await screen.findByText(/已归档到 PN #42/);
  expect(writes).toHaveLength(2); expect(writes[1]).toEqual(writes[0]);
  expect(writes[0].body).toMatchObject({ claim_revision: 2, part_id: 42, expected_identity_revision: 7, purpose: "common" });
});

it("刷新图片链接再次鉴权，撤权失败后移除先前签名预览", async () => {
  let linkReads = 0;
  api.defaults.adapter = async config => {
    let data: unknown = [];
    if (config.url?.endsWith("/capabilities")) data = { media: true, recognition: false, bundle: false, research: false };
    if (config.url?.endsWith("/inbox-items")) data = [initial];
    if (config.url?.endsWith("/inbox-items/item")) data = initial;
    if (config.url?.endsWith("/links")) {
      if (++linkReads > 1) throw { response: { data: { error: { code: "auth.forbidden", message: "图片权限已撤回" } } } };
      data = { url: "https://images.example.test/original", derivatives: [], expires_at: new Date(Date.now() + 300_000).toISOString() };
    }
    return { data: { data, meta: {} }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<InboxWorkspace />); fireEvent.click(await screen.findByRole("button", { name: "查看、核对与归档" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "查看／刷新图片链接" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "查看／刷新图片链接" }));
  await waitFor(() => expect(document.querySelector('img[src="https://images.example.test/original"]')).not.toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "查看／刷新图片链接" }));
  await screen.findByText("图片权限已撤回");
  expect(document.querySelector('img[src="https://images.example.test/original"]')).toBeNull();
});
