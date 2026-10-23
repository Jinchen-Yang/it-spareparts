import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import api from "../../api";
import InboxGroupEditor from "./InboxGroupEditor";
vi.mock("../../authz/AuthzProvider", async importOriginal => ({ ...await importOriginal<object>(), useAuthz: () => ({ status: "ready", snapshot: { resources: {} } }) }));
const original = api.defaults.adapter;
beforeEach(() => { localStorage.setItem("token", "group-editor"); });
afterEach(() => { cleanup(); api.defaults.adapter = original; });

it("分组分页结束重查版本，变化时不显示可提交的新旧混合顺序", async () => {
  let reads = 0;
  api.defaults.adapter = async config => ({ data: { data: config.url?.endsWith("/inbox-items") ? [{ inbox_id: "first", filename: "first.jpg" }] : { group_id: "group", revision: ++reads, ordered_item_ids: ["first"] }, meta: {} }, config, headers: {}, status: 200, statusText: "OK" });
  render(<InboxGroupEditor groupId="group" own onChanged={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "读取同组图片顺序" }));
  await screen.findByText("读取期间分组发生变化，请重新读取");
  expect(screen.queryByRole("button", { name: "保存组顺序" })).not.toBeInTheDocument();
});

it("人工排序提交完整顺序及原组版本，冲突保留草稿且禁止继续覆盖", async () => {
  const writes: unknown[] = [];
  api.defaults.adapter = async config => {
    if (config.method === "patch") { writes.push(JSON.parse(config.data)); throw { response: { data: { error: { code: "catalog.revision_conflict", message: "组已更新" } } } }; }
    const data = config.url?.endsWith("/inbox-items") ? [{ inbox_id: "first", filename: "first.jpg", client_order: 0 }, { inbox_id: "second", filename: "second.jpg", client_order: 1 }] : { group_id: "group", revision: 5, ordered_item_ids: ["first", "second"] };
    return { data: { data, meta: {} }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<InboxGroupEditor groupId="group" own onChanged={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "读取同组图片顺序" }));
  await screen.findByText("1. first.jpg");
  fireEvent.click(screen.getAllByRole("button", { name: "下 移" })[0]);
  fireEvent.click(screen.getByRole("button", { name: "保存组顺序" }));
  await screen.findByText("组已更新。本次排序草稿已保留。");
  expect(writes).toEqual([{ expected_revision: 5, ordered_item_ids: ["second", "first"] }]);
  expect(screen.getByText("1. second.jpg")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("button", { name: "保存组顺序" })).toBeDisabled());
});
