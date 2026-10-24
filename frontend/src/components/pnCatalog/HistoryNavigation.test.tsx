import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "../../test/antdRender";
import api from "../../api";
import CatalogPartPanel from "./CatalogPartPanel";

const snapshot = { schema_version: 1, resources: { part_master: { migration_state: "implemented", runtime_state: "enabled", decisions: { read_audit: { effect: "allow" } }, field_permissions: [] } } };
vi.mock("../../authz/AuthzProvider", async original => ({ ...await original<object>(), useAuthz: () => ({ status: "ready", snapshot }) }));
const adapter = api.defaults.adapter;
afterEach(() => { cleanup(); api.defaults.adapter = adapter; });

it("仅有历史读取权时仍可到达迁入历史，不借用当前任务读取权", async () => {
  localStorage.setItem("token", "history-reader"); const calls: string[] = [];
  api.defaults.adapter = async config => {
    calls.push(config.url || "");
    const data = config.url?.endsWith("/access") ? { part_id: 42, allowed_actions: ["read_legacy_history", "read_audit"], readable_fields: [], writable_fields: [] }
      : config.url?.endsWith("/capabilities") ? { media: false, research: false, recognition: false, bundle: false } : [];
    return { data: { data, meta: {} }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<CatalogPartPanel partId={42} />);
  await screen.findByRole("tab", { name: "研究、任务与历史" });
  await screen.findByText("我的迁入历史");
  await waitFor(() => expect(calls.some(path => path.endsWith("/legacy-history"))).toBe(true));
  expect(calls.some(path => path.endsWith("/jobs"))).toBe(false);
});
