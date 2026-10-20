import { App, ConfigProvider } from "antd";
import { cleanup, render, screen, waitFor, within } from "../../test/antdRender";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import GovernancePage from "../GovernancePage";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock("../../api", () => ({ default: api, api }));

const part = {
  pn_std: "KEYBOARD-PN", description: "合成治理型号", needs_review: true,
  is_excluded: false, exclude_reason: null, sales_lines: 0, revenue: null, gross_margin: null,
};

async function tabTo(user: ReturnType<typeof userEvent.setup>, target: HTMLElement) {
  for (let i = 0; i < 24 && document.activeElement !== target; i++) await user.tab();
  expect(target).toHaveFocus();
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  api.get.mockImplementation(async (url: string) => ({
    data: url === "/governance/parts" ? { items: [part], total: 1 } : {},
  }));
  api.put.mockResolvedValue({ data: {} });
});
afterEach(cleanup);

it("治理排除可用 Tab 和 Enter 打开原因确认，恢复可用空格触发原接口", async () => {
  const user = userEvent.setup();
  render(<ConfigProvider theme={{ token: { motion: false } }}><App><GovernancePage /></App></ConfigProvider>);
  await screen.findByText("KEYBOARD-PN");
  await tabTo(user, screen.getByRole("button", { name: "排除" }));
  await user.keyboard("{Enter}");
  const dialog = await screen.findByRole("dialog", { name: "排除型号：KEYBOARD-PN" });
  expect(api.put).not.toHaveBeenCalled();
  await user.type(within(dialog).getByPlaceholderText("排除原因（写入审计）"), "合成排除原因");
  api.get.mockImplementation(async (url: string) => ({
    data: url === "/governance/parts" ? { items: [{ ...part, is_excluded: true }], total: 1 } : {},
  }));
  await tabTo(user, within(dialog).getByRole("button", { name: "确认排除" }));
  await user.keyboard("{Enter}");
  await waitFor(() => expect(api.put).toHaveBeenCalledWith("/governance/exclude", {
    pn_std: "KEYBOARD-PN", excluded: true, reason: "合成排除原因",
  }));
  await tabTo(user, await screen.findByRole("button", { name: "恢复" }));
  await user.keyboard(" ");
  await waitFor(() => expect(api.put).toHaveBeenLastCalledWith("/governance/exclude", {
    pn_std: "KEYBOARD-PN", excluded: false,
  }));
});
