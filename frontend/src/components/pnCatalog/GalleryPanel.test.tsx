import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import api from "../../api";
import GalleryPanel from "./GalleryPanel";

const snapshot = { schema_version: 1, resources: { part_gallery: { migration_state: "implemented", runtime_state: "enabled", decisions: { "gallery.read": { effect: "allow" }, "gallery.arrange": { effect: "allow" } }, field_permissions: ["gallery"] } } };
vi.mock("../../authz/AuthzProvider", async importOriginal => ({ ...await importOriginal<object>(), useAuthz: () => ({ status: "ready", snapshot }) }));
const original = api.defaults.adapter;
beforeEach(() => { localStorage.setItem("token", "gallery-editor"); });
afterEach(() => { cleanup(); api.defaults.adapter = original; });
const entry = (id: string) => ({ entry_id: id, asset_id: `asset-${id}`, purpose: "common", status: "gallery", revision: 1, generation: 1, width: 10, height: 10, source: {}, derivatives: [] });
const caps = { media: false, bundle: false, research: false, recognition: false };

it("仅有正式图库读取权限时不展示候选或已删除入口", async () => {
  const reads: string[] = [];
  api.defaults.adapter = async config => {
    reads.push(config.params?.status);
    return { data: { data: { group_id: "group", part_id: 42, revision: 1, primary_entry_id: null, entries: [], ordered_entry_ids: [] }, meta: { identity_revision: 0 } }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<GalleryPanel partId={42} capabilities={caps} />);
  await screen.findByText("暂无图片");
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "图库状态" }));
  expect(screen.queryByText("候选图片")).not.toBeInTheDocument();
  expect(screen.queryByText("已删除")).not.toBeInTheDocument();
  expect(reads).toEqual(["gallery"]);
});

it("分页未读完不能重排；全部读取后提交完整顺序和服务器版本", async () => {
  const writes: unknown[] = [];
  api.defaults.adapter = async config => {
    if (config.method === "patch") writes.push(JSON.parse(config.data));
    const second = Boolean(config.params?.cursor);
    const id = second ? "second" : "first";
    return { data: { data: { group_id: "group", part_id: 42, revision: 7, primary_entry_id: second ? null : "first", entries: [entry(id)], ordered_entry_ids: [id] }, meta: { identity_revision: 3, next_cursor: second ? null : "next" } }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<GalleryPanel partId={42} capabilities={caps} />);
  expect(await screen.findByRole("button", { name: "设为主图" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "加载更多图片" }));
  await waitFor(() => expect(screen.getAllByRole("button", { name: "移到首位" })).toHaveLength(2));
  fireEvent.click(screen.getAllByRole("button", { name: "移到首位" })[1]);
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toEqual({ expected_gallery_revision: 7, primary_entry_id: "first", ordered_entry_ids: ["second", "first"] });
});

it("分页期间图库变化拒绝拼接新旧顺序", async () => {
  api.defaults.adapter = async config => {
    const second = Boolean(config.params?.cursor);
    return { data: { data: { group_id: "group", part_id: 42, revision: second ? 8 : 7, primary_entry_id: "first", entries: [entry(second ? "second" : "first")], ordered_entry_ids: [second ? "second" : "first"] }, meta: { identity_revision: 3, next_cursor: second ? null : "next" } }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<GalleryPanel partId={42} capabilities={caps} />);
  fireEvent.click(await screen.findByRole("button", { name: "加载更多图片" }));
  await screen.findByText("图库在分页期间已变化，请刷新后继续操作");
  expect(screen.getAllByRole("button", { name: "设为主图" })).toHaveLength(1);
  expect(screen.getByRole("button", { name: "设为主图" })).toBeDisabled();
});
