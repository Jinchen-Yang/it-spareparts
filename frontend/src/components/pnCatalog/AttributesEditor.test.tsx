import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "../../test/antdRender";
import api from "../../api";
import AttributesEditor from "./AttributesEditor";

const snapshot = { schema_version: 1, resources: { part_master: { migration_state: "implemented", runtime_state: "enabled", decisions: { read: { effect: "allow" }, edit: { effect: "allow" } }, field_permissions: ["attributes"] } } };
vi.mock("../../authz/AuthzProvider", async importOriginal => ({ ...await importOriginal<object>(), useAuthz: () => ({ status: "ready", snapshot }) }));
const original = api.defaults.adapter;
beforeEach(() => { localStorage.setItem("token", "attributes-editor"); });
afterEach(() => { cleanup(); api.defaults.adapter = original; });

it("恢复 JSON null 与删除使用不同命令，并保留墓碑的单键版本", async () => {
  let state = { part_id: 42, identity_revision: 2, revision: 8, items: { custom: { value: "old" as unknown, deleted: true, version: 5, protected: true, provenance: {} } } };
  const writes: unknown[] = [];
  api.defaults.adapter = async config => {
    if (config.method === "patch") {
      const body = JSON.parse(config.data); writes.push(body);
      state = { ...state, revision: state.revision + 1, items: { custom: { ...state.items.custom, value: body.changes.custom.value ?? null, deleted: body.changes.custom.operation === "delete", version: state.items.custom.version + 1 } } };
    }
    return { data: { data: state, meta: {} }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<AttributesEditor partId={42} />);
  fireEvent.click(await screen.findByRole("button", { name: "恢复并编辑" }));
  expect(screen.getByLabelText("JSON 参数值")).toHaveValue("null");
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "保 存" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(writes[0]).toEqual({ expected_identity_revision: 2, field_versions: { custom: 5 }, changes: { custom: { operation: "set", value: null } } });
  fireEvent.click(screen.getByRole("button", { name: "删 除" }));
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "保 存" }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[1]).toEqual({ expected_identity_revision: 2, field_versions: { custom: 6 }, changes: { custom: { operation: "delete" } } });
});

it("同一参数冲突保留结构化草稿，明确更新基线后使用新版本", async () => {
  let gets = 0;
  const writes: { body: unknown; key: string }[] = [];
  api.defaults.adapter = async config => {
    const current = { part_id: 42, identity_revision: 2, revision: 8, items: { custom: { value: { server: ++gets }, deleted: false, version: gets > 1 ? 9 : 5, protected: true, provenance: {} } } };
    if (config.method === "patch") {
      writes.push({ body: JSON.parse(config.data), key: String(config.headers["Idempotency-Key"]) });
      if (writes.length === 1) throw Object.assign(new Error("conflict"), { response: { status: 409, data: { error: { code: "catalog.field_conflict", message: "参数已更新" } } } });
    }
    return { data: { data: current, meta: {} }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<AttributesEditor partId={42} />); fireEvent.click(await screen.findByRole("button", { name: "编 辑" }));
  fireEvent.change(screen.getByLabelText("JSON 参数值"), { target: { value: '[1,{"draft":true}]' } });
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "保 存" }));
  await screen.findByText(/参数已更新/); expect(screen.getByLabelText("JSON 参数值")).toHaveValue('[1,{"draft":true}]');
  fireEvent.click(screen.getByRole("button", { name: "更新基线并保留草稿" })); await screen.findByText("已更新当前值，请比较后保存草稿。");
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "保 存" })); await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[1].body).toEqual({ expected_identity_revision: 2, field_versions: { custom: 9 }, changes: { custom: { operation: "set", value: [1, { draft: true }] } } });
  expect(writes[1].key).not.toBe(writes[0].key);
});

it("自定义名称与 JavaScript 原型属性同名时仍作为普通 JSON 键创建", async () => {
  const writes: unknown[] = [];
  api.defaults.adapter = async config => {
    if (config.method === "patch") writes.push(JSON.parse(config.data));
    return { data: { data: { part_id: 42, identity_revision: 2, revision: 0, items: {} }, meta: {} }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<AttributesEditor partId={42} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "添加参数" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
  fireEvent.change(screen.getByLabelText("参数名"), { target: { value: "constructor" } });
  fireEvent.change(screen.getByLabelText("JSON 参数值"), { target: { value: '{"nested":true}' } });
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "保 存" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toEqual({ expected_identity_revision: 2, field_versions: { constructor: 0 }, changes: { constructor: { operation: "set", value: { nested: true } } } });
});
