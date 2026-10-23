import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import GovernancePage from "../GovernancePage";
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("../../api", () => ({ default: api, api }));
beforeEach(() => {
  localStorage.setItem("role", "admin");
  vi.clearAllMocks();
  api.get.mockReturnValue(new Promise(() => {}));
  api.post.mockResolvedValue({ data: { brands: { brands: 0 }, specs: { spec_rows: 0 }, neg_margin: 0 } });
});
afterEach(() => { cleanup(); localStorage.clear(); });
it.each([
  ["刷新主数据", "/governance/refresh?with_candidates=true"],
  ["自动分类", "/governance/classify-backfill"],
  ["重算利润", "/profit/recompute"],
])("%s 取消不发请求，确认一次调用原接口", async (label, endpoint) => {
  render(<MemoryRouter><GovernancePage /></MemoryRouter>);
  expect(screen.getByRole("heading", { level: 1, name: "数据治理" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: label }));
  expect(api.post).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole("button", { name: /取\s*消/ }));
  expect(api.post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: label }));
  fireEvent.click(await screen.findByRole("button", { name: /确\s*认/ }));
  await waitFor(() => expect(api.post).toHaveBeenCalledExactlyOnceWith(endpoint));
});

// These fixtures exercise the retained legacy governance implementation.
vi.mock("../../authz/AuthzProvider", async original => ({ ...await original<object>(), useAuthz: () => ({ status: "ready", snapshot: { schema_version: 1, resources: { part_identity: { runtime_state: "legacy", migration_state: "implemented" }, part_master: { runtime_state: "legacy", migration_state: "implemented" } } } }) }));
