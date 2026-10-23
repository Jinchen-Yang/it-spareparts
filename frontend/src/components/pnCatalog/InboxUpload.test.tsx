import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import api from "../../api";
import InboxUpload from "./InboxUpload";
const actions = ["inbox.group.create", "inbox.group.add", "uploads.cancel"];
const snapshot = { schema_version: 1, resources: { part_gallery: { migration_state: "implemented", runtime_state: "enabled", decisions: Object.fromEntries(actions.map(a => [a, { effect: "allow" }])), field_permissions: ["inbox"] } } };
vi.mock("../../authz/AuthzProvider", async importOriginal => ({ ...await importOriginal<object>(), useAuthz: () => ({ status: "ready", snapshot }) }));
const original = api.defaults.adapter;
const form = { method: "POST", url: "https://images.example.test", fields: {}, headers: {}, expires_at: "2099-01-01" };
beforeEach(() => { localStorage.setItem("token", "uploader"); vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true })); });
afterEach(() => { cleanup(); api.defaults.adapter = original; vi.unstubAllGlobals(); });

it("恢复已分组的上传不重放组操作；校验响应丢失后读取票据避免再次传图", async () => {
  let completed = false;
  const writes: string[] = [];
  api.defaults.adapter = async config => {
    const ticket = { upload_id: "ticket", status: completed ? "verifying" : "issued", revision: completed ? 2 : 1, upload_form: form };
    let data: unknown = ticket;
    if (config.method === "post") writes.push(config.url!);
    if (config.url?.endsWith("/inbox")) data = { inbox_id: "item", group_id: "existing-group", position: 7 };
    if (config.url?.endsWith("/completion")) { completed = true; throw new Error("响应丢失"); }
    return { data: { data, meta: {} }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<InboxUpload onChanged={() => {}} />);
  fireEvent.change(screen.getByLabelText("选择收件箱图片"), { target: { files: [new File(["image"], "photo.jpg", { type: "image/jpeg" })] } });
  fireEvent.click(await screen.findByRole("button", { name: "上传并校验" }));
  await screen.findByText("响应丢失");
  fireEvent.click(screen.getByRole("button", { name: "重试当前上传" }));
  await screen.findByText("verifying");
  expect(writes).toEqual(["/pn-catalog/v1/uploads", "/pn-catalog/v1/uploads/ticket/completion"]);
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("后选图片先上传仍携原始拍摄序号和当前组版本，取消后可开始下一批", async () => {
  const additions: Record<string, unknown>[] = []; const cancellations: unknown[] = [];
  api.defaults.adapter = async config => {
    let data: unknown = { upload_id: "ticket", status: "issued", revision: 3, upload_form: form };
    if (config.url?.endsWith("/inbox")) throw { response: { data: { error: { code: "request.not_found", message: "尚未接收" } } } };
    if (config.url?.endsWith("/inbox-groups")) data = { group_id: "group", revision: 4, ordered_item_ids: ["already"] };
    if (config.url?.endsWith("/items")) { additions.push(JSON.parse(config.data)); data = { group_id: "group", revision: 5, ordered_item_ids: ["already", "new"] }; }
    if (config.url?.endsWith("/completion")) data = { upload_id: "ticket", status: "verifying", revision: 4 };
    if (config.url?.endsWith("/cancellation")) { cancellations.push(JSON.parse(config.data)); data = { upload_id: "ticket", status: "cancelled", revision: 5 }; }
    return { data: { data, meta: {} }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<InboxUpload onChanged={() => {}} />);
  fireEvent.change(screen.getByLabelText("选择收件箱图片"), { target: { files: [new File(["one"], "first.jpg", { type: "image/jpeg" }), new File(["two"], "second.jpg", { type: "image/jpeg" })] } });
  fireEvent.click(screen.getAllByRole("button", { name: "上传并校验" })[1]);
  await screen.findByText("verifying");
  expect(additions).toEqual([{ upload_id: "ticket", position: 1, client_order: 1, expected_group_revision: 4 }]);
  fireEvent.click(screen.getAllByRole("button", { name: "取消上传" })[1]); fireEvent.click(await screen.findByRole("button", { name: "OK" }));
  await screen.findByText("cancelled"); expect(cancellations).toEqual([{ expected_revision: 4 }]);
  await waitFor(() => expect(screen.getAllByRole("button", { name: "取消上传" })).toHaveLength(1));
  fireEvent.click(screen.getByRole("button", { name: "取消上传" })); fireEvent.click(await screen.findByRole("button", { name: "OK" }));
  fireEvent.click(await screen.findByRole("button", { name: "添加下一批图片" }));
  expect(screen.queryByText("second.jpg")).not.toBeInTheDocument();
});
