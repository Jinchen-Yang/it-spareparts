/** 页面可读不等于管理员写操作可用：前端不能向普通用户展示必然 403 的按钮。 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";
import type { ComponentType } from "react";

const get = vi.fn();
const post = vi.fn();
const put = vi.fn();

vi.mock("../../api", () => ({
  default: { get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
    put: (...args: unknown[]) => put(...args) },
  api: { get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
    put: (...args: unknown[]) => put(...args) },
}));

import GovernancePage from "../GovernancePage";
import InventoryPage from "../InventoryPage";
import ProfitPage from "../ProfitPage";
import ProjectCostPage from "../ProjectCostPage";

const never = new Promise<never>(() => {});

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "sales");
  get.mockReturnValue(never);
});

afterEach(() => {
  cleanup();
  message.destroy();
});

const adminActions: Array<[string, ComponentType, string | RegExp]> = [
  ["利润页", ProfitPage, /重\s*算$/],
  ["项目成本页", ProjectCostPage, "重算成本"],
  ["数据治理页", GovernancePage, "重算利润"],
];

describe("管理员专属重算动作", () => {
  it.each(adminActions)("%s 对普通用户隐藏管理员动作", (_label, Page, buttonName) => {
    render(<Page />);
    expect(screen.queryByRole("button", { name: buttonName })).toBeNull();
  });

  it.each(adminActions)("%s 对管理员保留重算动作", (_label, Page, buttonName) => {
    localStorage.setItem("role", "admin");
    render(<Page />);
    expect(screen.getByRole("button", { name: buttonName })).toBeInTheDocument();
  });

  it("治理专员仍能看到治理动作，只隐藏跨页面的管理员重算", () => {
    localStorage.setItem("role", "readonly");
    render(<GovernancePage />);
    expect(screen.getByRole("button", { name: "刷新主数据" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "自动分类" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "价格与数量疑点" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "规则校准预览" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重算利润" })).toBeNull();
  });
});

describe("利润维度与数据范围一致", () => {
  it("受限销售默认按型号加载，且不展示会被后端拒绝的销售员/客户维度", async () => {
    localStorage.setItem("role", "sales");
    localStorage.setItem("permissions", JSON.stringify({
      page_profit: true,
      own_customers_only: true,
      data_customer: true,
    }));
    render(<ProfitPage />);
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/profit", expect.objectContaining({
        params: expect.objectContaining({ dimension: "part" }),
      }),
    ));
    expect(screen.getByText("按型号")).toBeInTheDocument();
    expect(screen.queryByText("按销售员")).toBeNull();
    expect(screen.queryByText("按客户")).toBeNull();
  });

  it.each([
    ["缺成本权限", { data_purchase_cost: false, data_profit: true }],
    ["缺利润权限", { data_purchase_cost: true, data_profit: false }],
  ])("%s 时隐藏会泄露成本/盈亏归类的异常筛选", (_label, permissions) => {
    localStorage.setItem("permissions", JSON.stringify({
      page_profit: true,
      own_customers_only: false,
      data_customer: true,
      ...permissions,
    }));
    render(<ProfitPage />);
    expect(screen.queryByText("仅看异常")).toBeNull();
  });

  it("成本与利润权限齐全时保留异常筛选", () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_profit: true,
      own_customers_only: false,
      data_customer: true,
      data_purchase_cost: true,
      data_profit: true,
    }));
    render(<ProfitPage />);
    expect(screen.getByText("仅看异常")).toBeInTheDocument();
  });

  it("脱敏后的利润 KPI 保持为空，不能误显示为真实零元", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_profit: true,
      own_customers_only: false,
      data_customer: true,
      data_purchase_cost: false,
      data_profit: true,
    }));
    get.mockResolvedValue({ data: { rows: [{
      dimension: "SECURE-PN",
      revenue: 100,
      revenue_costed: null,
      cost_moving_avg: null,
      gross_profit_moving: null,
      gross_margin_moving: null,
      cost_fifo: null,
      gross_profit_fifo: null,
      gross_margin_fifo: null,
      lines: 1,
      no_cost: null,
      excluded_revenue: 0,
    }] } });
    render(<ProfitPage />);
    await screen.findByText("SECURE-PN");

    for (const title of ["移动加权 · 毛利", "先进先出 FIFO · 毛利"]) {
      const card = screen.getByText(title).closest(".ant-card");
      expect(card).not.toBeNull();
      expect(card?.querySelector(".ant-statistic-content")).toHaveTextContent("-");
      expect(card).toHaveTextContent("毛利率 -");
      expect(card).not.toHaveTextContent("¥0");
    }
  });

  it("利润表和 KPI 优先采用后端明确双税字段，旧单字段只作兼容", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_profit: true,
      own_customers_only: false,
      data_customer: true,
      data_purchase_cost: true,
      data_profit: true,
    }));
    get.mockResolvedValue({ data: { rows: [{
      dimension: "DUAL-PN",
      revenue: 100,
      revenue_inc: 120,
      revenue_ex: 91,
      revenue_costed: 80,
      revenue_costed_inc: 96,
      revenue_costed_ex: 73,
      cost_moving_avg: 50,
      cost_moving_avg_inc: 60,
      cost_moving_avg_ex: 45,
      gross_profit_moving: 30,
      gross_profit_moving_inc: 36,
      gross_profit_moving_ex: 28,
      gross_margin_moving: 0.3836,
      cost_fifo: 51,
      cost_fifo_inc: 61,
      cost_fifo_ex: 46,
      gross_profit_fifo: 29,
      gross_profit_fifo_inc: 35,
      gross_profit_fifo_ex: 27,
      gross_margin_fifo: 0.3699,
      lines: 1,
      no_cost: 0,
      excluded_revenue: 0,
    }] } });

    render(<ProfitPage />);
    await screen.findByText("DUAL-PN");

    expect(screen.getAllByText("¥91").length).toBeGreaterThan(0);
    expect(screen.getAllByText("¥28").length).toBeGreaterThan(0);
    expect(screen.getAllByText("¥27").length).toBeGreaterThan(0);
    expect(screen.queryByText("¥100")).toBeNull();
  });
});

const inventoryResponse = {
  data: {
    total: 1,
    match_terms: null,
    items: [{
      part_id: 1,
      pn_std: "PERM-PN-1",
      description: "权限测试库存",
      brand: null,
      dynamic_qty: 5,
      anchor_qty: 5,
      anchor_date: "2026-07-15",
      in_qty: 0,
      out_sales: 0,
      out_maint: 0,
      warehouses: [{
        id: 11,
        warehouse: "总仓",
        qty: 5,
        source_qty: 5,
        manual_qty: null,
        is_qty_overridden: false,
        safety_stock: null,
        unit_cost: null,
        inventory_value: null,
        snapshot_date: "2026-07-15",
      }],
    }],
  },
};

async function expandInventoryRow() {
  await screen.findByText("PERM-PN-1");
  const expand = document.querySelector<HTMLButtonElement>(".ant-table-row-expand-icon");
  expect(expand).not.toBeNull();
  fireEvent.click(expand!);
  await screen.findByText("总仓");
}

describe("库存人工修正", () => {
  it("普通用户能看分仓库存，但看不到修正入口", async () => {
    get.mockResolvedValue(inventoryResponse);
    render(<InventoryPage />);
    await expandInventoryRow();
    expect(screen.queryByText("修正")).toBeNull();
    expect(put).not.toHaveBeenCalled();
  });

  it("管理员仍可打开修正弹窗", async () => {
    localStorage.setItem("role", "admin");
    get.mockResolvedValue(inventoryResponse);
    render(<InventoryPage />);
    await expandInventoryRow();
    fireEvent.click(screen.getByText("修正"));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "保存修正" })).toBeInTheDocument();
  });
});
