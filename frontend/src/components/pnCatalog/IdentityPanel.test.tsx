import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import api from "../../api";
import IdentityPanel from "./IdentityPanel";

const actions = ["read", "rebind_facts", "merge", "unmerge", "review_alias"];
const snapshot = { schema_version: 1, resources: {
  part_identity: { runtime_state: "enabled", migration_state: "implemented", decisions: Object.fromEntries(actions.map(a => [a, { effect: "allow" }])), field_permissions: ["identity", "aliases"] },
  part_master: { runtime_state: "enabled", migration_state: "implemented", decisions: { read: { effect: "allow" } }, field_permissions: [] },
} };
vi.mock("../../authz/AuthzProvider", async original => ({ ...await original<object>(), useAuthz: () => ({ status: "ready", snapshot }) }));
const original = api.defaults.adapter;
beforeEach(() => { localStorage.setItem("token", "identity-editor"); });
afterEach(() => { cleanup(); api.defaults.adapter = original; });
const expected = { "1": { identity_revision: 2, profile_revision: 3 }, "2": { identity_revision: 4, profile_revision: 5 }, "3": { identity_revision: 6, profile_revision: 7 } };
async function choosePn(label: string, number: number) {
  await waitFor(() => expect(screen.getByLabelText(`搜索${label}`)).toBeEnabled());
  fireEvent.change(screen.getByLabelText(`搜索${label}`), { target: { value: String(number) } });
  fireEvent.click(screen.getByRole("button", { name: `查找${label}` }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: label })).toBeEnabled());
  fireEvent.mouseDown(screen.getByRole("combobox", { name: label }));
  fireEvent.click(await screen.findByText(`PN-${number}`));
}

it.each(["merge", "unmerge", "alias_reassign"] as const)("%s 使用真实选择和完整原版本，确认前不写入", async operation => {
  const writes: { body: unknown; key: string }[] = [];
  api.defaults.adapter = async config => {
    let data: unknown = [];
    if (config.url === "/parts/search") return { data: { items: [{ part_id: Number(config.params.q), pn_std: `PN-${config.params.q}` }] }, config, headers: {}, status: 200, statusText: "OK" };
    if (config.url?.endsWith("/merge-logs")) data = [{ merge_log_id: 8, source_pn: "S", target_pn: "T", source_part_id: 1, target_part_id: 2, relationship_active: true }];
    if (config.url?.endsWith("/aliases")) data = [{ alias_id: 9, pn_raw: "ALIAS", pn_std: "OLD", part_id: 1, status: "active" }];
    if (config.url?.endsWith("/preview")) data = { operation, target_part_id: 2, expected, parts: [1, 2, 3].map(part_id => ({ part_id, pn_std: `PN-${part_id}`, status: "active", merged_into_id: null })) };
    if (config.url?.endsWith("/commands")) {
      writes.push({ body: JSON.parse(config.data), key: String(config.headers["Idempotency-Key"]) });
      if (operation === "merge" && writes.length === 1) throw { response: { status: 503, data: { error: { code: "request.unavailable", message: "响应未知" } } } };
      data = { parts: [], outcome: {}, recalculation_job: { job_id: "job", part_id: 2, status: "pending" } };
    }
    return { data: { data, meta: {} }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<IdentityPanel />);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "身份操作" })).toBeEnabled());
  if (operation !== "merge") {
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "身份操作" }));
    fireEvent.click(await screen.findByText(operation === "unmerge" ? "回滚合并" : "调整别名归属"));
  }
  if (operation === "merge") await choosePn("来源 PN", 1);
  if (operation !== "unmerge") await choosePn("目标 PN", 2);
  if (operation === "unmerge") fireEvent.click(await screen.findByRole("button", { name: "选择此合并记录" }));
  if (operation === "alias_reassign") fireEvent.click(await screen.findByRole("button", { name: "选择此别名" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "读取身份变更预览" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "读取身份变更预览" }));
  await screen.findByText("确认涉及的 PN 与原版本"); expect(writes).toHaveLength(0);
  fireEvent.click(screen.getByRole("checkbox", { name: /我已核对这次/ }));
  fireEvent.click(screen.getByRole("button", { name: "确认身份变更" }));
  if (operation === "merge") { await screen.findByText(/原操作与版本已冻结/); expect(screen.getByRole("combobox", { name: "身份操作" })).toBeDisabled(); fireEvent.click(screen.getByRole("button", { name: "重试同一次确认" })); }
  await screen.findByText("身份变更已保存");
  const ids = operation === "merge" ? { source_part_id: 1, target_part_id: 2 } : operation === "unmerge" ? { merge_log_id: 8 } : { alias_id: 9, target_part_id: 2 };
  expect(writes[0].body).toEqual({ operation, ...ids, expected });
  if (operation === "merge") expect(writes[1]).toEqual(writes[0]);
});
