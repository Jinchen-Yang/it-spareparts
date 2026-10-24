import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import api from "../../api";
import SuppressionsPanel from "./SuppressionsPanel";
import type { Gallery } from "../../api/pnCatalog";

const snapshot = { schema_version: 1, resources: { part_gallery: { migration_state: "implemented", runtime_state: "enabled", decisions: { "gallery.read": { effect: "allow" }, "gallery.suppress": { effect: "allow" }, "gallery.unsuppress": { effect: "allow" } }, field_permissions: ["gallery"] } } };
vi.mock("../../authz/AuthzProvider", async original => ({ ...await original<object>(), useAuthz: () => ({ status: "ready", snapshot }) }));
const adapter = api.defaults.adapter;
const hash = "b".repeat(64);
const gallery: Gallery = { group_id: "group", part_id: 1, revision: 7, primary_entry_id: "entry", ordered_entry_ids: ["entry"], entries: [{ entry_id: "entry", content_hash: hash, asset_id: "asset", purpose: "common", status: "gallery", revision: 1, generation: 1, width: 1, height: 1, size_bytes: 10, source: {}, derivatives: [] }] };
const row = { suppression_id: "rule", group_id: "group", content_hash: hash, reason: "重复", revision: 3, active: true };
beforeEach(() => { localStorage.setItem("token", "suppressions"); });
afterEach(() => { cleanup(); api.defaults.adapter = adapter; });
const view = () => render(<SuppressionsPanel gallery={gallery} onChanged={vi.fn().mockResolvedValue(undefined)} />);

it("未知隐藏结果保留原内容、版本、理由与请求号", async () => {
  const writes: { body: string; key: unknown }[] = [];
  api.defaults.adapter = async config => {
    if (config.method === "post") { writes.push({ body: config.data, key: config.headers["Idempotency-Key"] }); if (writes.length === 1) throw new Error("offline"); }
    return { config, headers: {}, status: 200, statusText: "OK", data: { data: config.method === "post" ? row : [], meta: {} } };
  };
  view(); fireEvent.click(screen.getByText("隐藏相同图片与恢复显示")); await screen.findByText("暂无隐藏规则");
  fireEvent.change(screen.getByLabelText("要隐藏的图片"), { target: { value: "entry" } });
  fireEvent.change(screen.getByLabelText("隐藏原因"), { target: { value: "不要重复" } });
  fireEvent.click(screen.getByRole("button", { name: "隐藏相同内容" }));
  await screen.findByText("请求结果未知，原规则和版本已保留，请重试同一次操作。");
  expect(screen.getByLabelText("隐藏原因")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "重试同一次操作" }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[0]).toEqual(writes[1]); expect(JSON.parse(writes[0].body)).toEqual({ content_hash: hash, expected_gallery_revision: 7, reason: "不要重复" });
});

it("恢复提交隐藏规则版本，撤权后清除隐藏详情", async () => {
  let deleted = "";
  api.defaults.adapter = async config => {
    if (config.method === "delete") deleted = config.url || "";
    if (deleted && config.method === "get") throw { response: { status: 403, data: { error: { message: "当前无读取权限", code: "auth.forbidden" } } } };
    return { config, headers: {}, status: 200, statusText: "OK", data: { data: config.method === "delete" ? row : [row], meta: {} } };
  };
  view(); fireEvent.click(screen.getByText("隐藏相同图片与恢复显示"));
  fireEvent.click(await screen.findByRole("button", { name: "恢复显示" }));
  await screen.findByText("当前无读取权限");
  expect(deleted).toContain("/suppressions/rule?expected_revision=3");
  expect(screen.queryByText(/… · 重复/)).toBeNull();
});
