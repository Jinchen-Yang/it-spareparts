import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { message } from "antd";

const getSystemSettings = vi.fn();
const updateSystemSettings = vi.fn();

vi.mock("../../api/systemSettings", () => ({
  getSystemSettings: (...args: unknown[]) => getSystemSettings(...args),
  updateSystemSettings: (...args: unknown[]) => updateSystemSettings(...args),
}));

import SystemSettingsPage from "../SystemSettingsPage";
import { NAV_ITEMS } from "../../nav";
import type { SystemSettings } from "../../api/systemSettings";

const initial: SystemSettings = {
  purchase_display_basis: "both",
  sales_display_basis: "ex",
  maintenance_display_basis: "both",
  version: 3,
  updated_by: null,
  updated_at: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  getSystemSettings.mockResolvedValue({ data: initial });
});

afterEach(() => {
  cleanup();
  message.destroy();
});

describe("管理员统一税口径展示策略", () => {
  it("分采购、销售、项目维保三域配置，销售初始默认未税", async () => {
    render(<SystemSettingsPage />);
    await screen.findByText("税口径统一展示策略");

    expect(screen.getByText(/普通员工不能临时切换/)).toBeInTheDocument();
    const purchase = screen.getByRole("radiogroup", { name: "采购展示口径" });
    const sales = screen.getByRole("radiogroup", { name: "销售展示口径" });
    const maintenance = screen.getByRole("radiogroup", { name: "项目维保展示口径" });
    expect(within(purchase).getByRole("radio", { name: "同时显示" })).toBeChecked();
    expect(within(sales).getByRole("radio", { name: "不含税" })).toBeChecked();
    expect(within(maintenance).getByRole("radio", { name: "同时显示" })).toBeChecked();
  });

  it("保存时原子携带三域和当前版本，成功后采用服务端新版本", async () => {
    updateSystemSettings.mockResolvedValue({
      data: {
        ...initial,
        maintenance_display_basis: "inc",
        version: 4,
        updated_by: "admin",
        updated_at: "2026-07-28T20:00:00+08:00",
      },
    });
    render(<SystemSettingsPage />);
    const maintenance = await screen.findByRole("radiogroup", { name: "项目维保展示口径" });
    fireEvent.click(within(maintenance).getByRole("radio", { name: "含税" }));
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));

    await waitFor(() => expect(updateSystemSettings).toHaveBeenCalledWith({
      purchase_display_basis: "both",
      sales_display_basis: "ex",
      maintenance_display_basis: "inc",
      expected_version: 3,
    }));
    await waitFor(() => expect(
      screen.getByText(/当前版本：v4/),
    ).toBeInTheDocument());
  });

  it("409 时要求刷新，不把旧编辑结果伪装成已保存", async () => {
    updateSystemSettings.mockRejectedValue({
      response: { status: 409, data: { detail: "设置已被其他管理员修改，请刷新后重试" } },
    });
    render(<SystemSettingsPage />);
    const purchase = await screen.findByRole("radiogroup", { name: "采购展示口径" });
    fireEvent.click(within(purchase).getByRole("radio", { name: "含税" }));
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));

    expect(await screen.findByText(/已被其他管理员修改/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新加载" })).toBeInTheDocument();
    expect(screen.getByText(/当前版本：v3/)).toBeInTheDocument();

    const inc = within(purchase).getByRole("radio", { name: "含税" });
    expect(inc).toBeDisabled();
    fireEvent.click(inc);
    expect(screen.getByText(/已被其他管理员修改/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存设置" })).toBeDisabled();
    expect(updateSystemSettings).toHaveBeenCalledTimes(1);
  });

  it("保存请求未完成时锁定口径，迟到成功响应不会覆盖后续编辑", async () => {
    let resolveSave!: (value: {
      data: typeof initial;
    }) => void;
    updateSystemSettings.mockImplementation(() => new Promise((resolve) => {
      resolveSave = resolve;
    }));
    render(<SystemSettingsPage />);

    const purchase = await screen.findByRole("radiogroup", { name: "采购展示口径" });
    const inc = within(purchase).getByRole("radio", { name: "含税" });
    const ex = within(purchase).getByRole("radio", { name: "不含税" });
    fireEvent.click(inc);
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
    await waitFor(() => expect(updateSystemSettings).toHaveBeenCalledTimes(1));

    expect(inc).toBeDisabled();
    expect(ex).toBeDisabled();
    fireEvent.click(ex);
    expect(inc).toBeChecked();
    expect(ex).not.toBeChecked();

    resolveSave({
      data: {
        ...initial,
        purchase_display_basis: "inc",
        version: 4,
      },
    });
    await screen.findByText(/当前版本：v4/);
    expect(inc).toBeChecked();
  });

  it("重新加载失败时清除旧版本表单并始终保留重试入口", async () => {
    updateSystemSettings.mockRejectedValue({
      response: { status: 409, data: { detail: "设置已被其他管理员修改，请刷新后重试" } },
    });
    render(<SystemSettingsPage />);
    await screen.findByText(/当前版本：v3/);

    const sales = screen.getByRole("radiogroup", { name: "销售展示口径" });
    fireEvent.click(within(sales).getByRole("radio", { name: "含税" }));
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
    await screen.findByRole("button", { name: "重新加载" });

    getSystemSettings.mockRejectedValueOnce(new Error("network down"));
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));

    expect(await screen.findByText(/加载系统设置失败/)).toBeInTheDocument();
    expect(screen.queryByText(/当前版本：v3/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新加载" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存设置" })).not.toBeInTheDocument();
  });

  it("普通保存错误显示可恢复提示", async () => {
    updateSystemSettings.mockRejectedValue(new Error("network down"));
    render(<SystemSettingsPage />);
    const sales = await screen.findByRole("radiogroup", { name: "销售展示口径" });
    fireEvent.click(within(sales).getByRole("radio", { name: "含税" }));
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));

    expect(await screen.findByText(/保存失败/)).toBeInTheDocument();
  });
});

it("系统设置入口不绑定可委派权限键，因此只由 admin 注册菜单和路由", () => {
  const item = NAV_ITEMS.find((candidate) => candidate.key === "system-settings");
  expect(item).toBeDefined();
  expect(item?.path).toBe("/system-settings");
  expect(item?.perm).toBeUndefined();
  expect(item?.anyPerm).toBeUndefined();
});
