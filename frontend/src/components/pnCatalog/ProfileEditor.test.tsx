import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import api from "../../api";
import ProfileEditor from "./ProfileEditor";
import { part } from "./fixtures";
const snapshot = { schema_version: 1, resources: { part_master: { migration_state: "implemented", runtime_state: "enabled", decisions: { read: { effect: "allow" }, edit: { effect: "allow" } }, field_permissions: ["brand", "description"] } } };
vi.mock("../../authz/AuthzProvider", async importOriginal => ({ ...await importOriginal<object>(), useAuthz: () => ({ status: "ready", snapshot }) }));
const original = api.defaults.adapter;
beforeEach(() => { localStorage.setItem("token", "editor"); });
afterEach(() => { cleanup(); api.defaults.adapter = original; });
it("字段冲突保留草稿，明确重基线后只提交草稿字段和最新版本", async () => {
  let gets = 0; const writes: { body: unknown; key: string }[] = [];
  const latest = { ...part, profile: { ...part.profile, brand: "别人品牌", description: "别人描述" }, field_versions: { ...part.field_versions, brand: 4 } };
  api.defaults.adapter = async config => {
    const response = (data: unknown) => ({ data, config, headers: {}, status: 200, statusText: "OK" });
    if (config.url === "/parts/master/categories") return response({ categories: [] });
    if (config.method === "get") return response({ data: ++gets === 1 ? part : latest, meta: {} });
    writes.push({ body: JSON.parse(config.data), key: String(config.headers["Idempotency-Key"]) });
    if (writes.length === 1) throw Object.assign(new Error("conflict"), { response: { status: 409, data: { error: { code: "catalog.field_conflict", message: "品牌已变化" } } } });
    return response({ data: { ...latest, profile: { ...latest.profile, brand: "我的草稿" } }, meta: {} });
  };
  render(<ProfileEditor partId={42} />); fireEvent.change(await screen.findByLabelText("品牌"), { target: { value: "我的草稿" } }); fireEvent.click(screen.getByRole("button", { name: "保存资料" }));
  expect(await screen.findByText(/品牌已变化/)).toBeInTheDocument(); expect(screen.getByLabelText("品牌")).toHaveValue("我的草稿");
  fireEvent.click(screen.getByRole("button", { name: "更新基线并保留草稿" })); await screen.findByDisplayValue("别人描述"); expect(screen.getByLabelText("品牌")).toHaveValue("我的草稿");
  fireEvent.click(screen.getByRole("button", { name: "保存资料" })); await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[1].body).toEqual({ expected_identity_revision: 2, changes: { brand: "我的草稿" }, field_versions: { brand: 4 }, spec_updates: {} }); expect(writes[1].key).not.toBe(writes[0].key);
});
