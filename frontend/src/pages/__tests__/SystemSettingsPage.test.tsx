import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
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
  maintenance_project_profit_default_basis: "both",
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

describe("维保合同级毛利默认展示口径", () => {
  it("明确说明只影响默认展示，并提供含税、未税、同时显示三项", async () => {
    render(<SystemSettingsPage />);
    await screen.findByText("维保合同级毛利默认展示口径");

    expect(screen.getByText(/只影响项目成本页的默认展示/)).toBeInTheDocument();
    expect(await screen.findByRole("radio", { name: /含税毛利/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /未税毛利/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /同时显示/ })).toBeChecked();
  });

  it("保存时携带当前版本，成功后采用服务端的新版本", async () => {
    updateSystemSettings.mockResolvedValue({
      data: {
        ...initial,
        maintenance_project_profit_default_basis: "inc",
        version: 4,
        updated_by: "admin",
        updated_at: "2026-07-28T20:00:00+08:00",
      },
    });
    render(<SystemSettingsPage />);
    fireEvent.click(await screen.findByRole("radio", { name: /含税毛利/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));

    await waitFor(() => expect(updateSystemSettings).toHaveBeenCalledWith({
      maintenance_project_profit_default_basis: "inc",
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
    fireEvent.click(await screen.findByRole("radio", { name: /未税毛利/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));

    expect(await screen.findByText(/已被其他管理员修改/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新加载" })).toBeInTheDocument();
    expect(screen.getByText(/当前版本：v3/)).toBeInTheDocument();

    const inc = screen.getByRole("radio", { name: /含税毛利/ });
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

    const inc = await screen.findByRole("radio", { name: /含税毛利/ });
    const ex = screen.getByRole("radio", { name: /未税毛利/ });
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
        maintenance_project_profit_default_basis: "inc",
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

    fireEvent.click(screen.getByRole("radio", { name: /未税毛利/ }));
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
    fireEvent.click(await screen.findByRole("radio", { name: /未税毛利/ }));
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
