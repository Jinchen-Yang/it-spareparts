import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen, waitFor } from "../../test/antdRender";
import { renderUrlPage } from "../../test/urlStateRender";
import api from "../../api";
import GovernancePage from "../GovernancePage";

const adapter = api.defaults.adapter;
beforeEach(() => {
  localStorage.clear();
  api.defaults.adapter = async (config) => ({
    status: 200, statusText: "OK", headers: {}, config,
    data: config.url === "/governance/summary" || config.url === "/governance/metrics"
      ? null : { items: [], total: 0 },
  });
});
afterEach(() => { cleanup(); api.defaults.adapter = adapter; });

it("复制链接恢复治理标签；切换 replace、保留无关键，默认标签不写 URL", async () => {
  const { router } = renderUrlPage(<GovernancePage />, "/governance?tab=merges&origin=review");
  expect(screen.getByRole("tab", { name: "合并历史" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("tab", { name: "别名审核" }));
  await waitFor(() => expect(router.state.location.search).toBe("?tab=aliases&origin=review"));
  expect(router.state.historyAction).toBe("REPLACE");
  fireEvent.click(screen.getByRole("tab", { name: "型号治理" }));
  await waitFor(() => expect(router.state.location.search).toBe("?origin=review"));
  await act(() => router.navigate(-1));
  expect(router.state.location.pathname).toBe("/before");
  await act(() => router.navigate(1));
  expect(screen.getByRole("tab", { name: "型号治理" })).toHaveAttribute("aria-selected", "true");
});

it("非法标签回退型号治理", async () => {
  renderUrlPage(<GovernancePage />, "/governance?tab=invalid");
  expect(screen.getByRole("tab", { name: "型号治理" })).toHaveAttribute("aria-selected", "true");
  await act(async () => {});
});

// These fixtures exercise the retained legacy governance implementation.
vi.mock("../../authz/AuthzProvider", async original => ({ ...await original<object>(), useAuthz: () => ({ status: "ready", snapshot: { schema_version: 1, resources: { part_identity: { runtime_state: "legacy", migration_state: "implemented" }, part_master: { runtime_state: "legacy", migration_state: "implemented" } } } }) }));
