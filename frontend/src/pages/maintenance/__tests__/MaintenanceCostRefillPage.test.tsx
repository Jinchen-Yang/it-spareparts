import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const {
  listMaintenanceCostGaps,
  listMaintenanceProjectOperations,
  recomputeMaintenanceCostGaps,
  updateMaintenanceCostGap,
} = vi.hoisted(() => ({
  listMaintenanceCostGaps: vi.fn(),
  listMaintenanceProjectOperations: vi.fn(),
  recomputeMaintenanceCostGaps: vi.fn(),
  updateMaintenanceCostGap: vi.fn(),
}));

vi.mock("../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceOperations")>(
    "../../../api/maintenanceOperations",
  );
  return {
    ...actual,
    listMaintenanceCostGaps,
    listMaintenanceProjectOperations,
    recomputeMaintenanceCostGaps,
    updateMaintenanceCostGap,
  };
});

import MaintenanceCostRefillPage from "../MaintenanceCostRefillPage";

const gap = {
  line_id: "line-1",
  version: 7,
  project_id: "project-1",
  project_code: "XM-001",
  order_no: "WBDD-001",
  order_date: "2026-08-01",
  contract_no: "HT-001",
  pn: "PN-MISSING",
  description: "待定价备件",
  quantity: 2,
  current_unit_cost: null,
  references: [
    {
      source: "direct_purchase",
      document_no: "PO-LINKED",
      document_date: "2026-07-31",
      distance_days: -1,
      weighted_unit_price: 88,
      sample_lines: 1,
      sample_quantity: 2,
    },
    {
      source: "purchase_window",
      document_no: "PO-7",
      document_date: "2026-08-04",
      distance_days: 3,
      weighted_unit_price: 92,
      sample_lines: 3,
      sample_quantity: 10,
    },
    {
      source: "sales_window",
      document_no: "SO-7",
      document_date: "2026-07-30",
      distance_days: -2,
      weighted_unit_price: 118,
      sample_lines: 2,
      sample_quantity: 5,
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  listMaintenanceCostGaps.mockResolvedValue({
    data: { rows: [gap], total: 1, page: 1, page_size: 20, data_version: "v7" },
  });
  listMaintenanceProjectOperations.mockResolvedValue({
    data: {
      rows: [
        { project_id: "project-1", project_code: "XM-001", display_name: "项目 A" },
        { project_id: "project-2", project_code: "XM-002", display_name: "项目 B" },
      ],
      total: 2,
      page: 1,
      page_size: 200,
      as_of: "2026-08-08",
      data_version: "v1",
    },
  });
  updateMaintenanceCostGap.mockResolvedValue({
    data: {
      issue_line_id: "line-1",
      version: 8,
      unit_cost: 103.96,
      cost_amount: 207.92,
      unit_cost_ex_tax: 92,
      unit_cost_inc_tax: 103.96,
      cost_amount_ex_tax: 184,
      cost_amount_inc_tax: 207.92,
      cost_source: "manual",
      manual_applied: true,
      resolution: "manual",
    },
  });
  recomputeMaintenanceCostGaps.mockResolvedValue({
    data: { resolved: 0, remaining: 1, data_version: "v7" },
  });
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("MaintenanceCostRefillPage", () => {
  it("重新匹配后到系统价格并刷新缺价清单", async () => {
    listMaintenanceCostGaps
      .mockResolvedValueOnce({
        data: { rows: [gap], total: 1, page: 1, page_size: 20, data_version: "v7" },
      })
      .mockResolvedValueOnce({
        data: { rows: [], total: 0, page: 1, page_size: 20, data_version: "v8" },
      });
    recomputeMaintenanceCostGaps.mockResolvedValueOnce({
      data: { resolved: 1, remaining: 0, data_version: "v8" },
    });
    render(
      <MemoryRouter>
        <MaintenanceCostRefillPage projectId="project-1" />
      </MemoryRouter>,
    );

    await screen.findByText("PN-MISSING");
    fireEvent.click(screen.getByRole("button", { name: "重新匹配系统价格" }));

    await waitFor(() => expect(recomputeMaintenanceCostGaps).toHaveBeenCalledWith(
      "project-1",
      { reason: "重新匹配后到采购或销售价格证据" },
    ));
    expect(await screen.findByText("已更新 1 行系统价格，仍有 0 行缺价。"))
      .toBeInTheDocument();
    await waitFor(() => expect(listMaintenanceCostGaps).toHaveBeenCalledTimes(2));
    expect(screen.getByText("当前项目没有待回填成本")).toBeInTheDocument();
  });

  it("系统价格更新成功但清单刷新失败时保留成功事实并单独提示刷新错误", async () => {
    listMaintenanceCostGaps
      .mockResolvedValueOnce({
        data: { rows: [gap], total: 1, page: 1, page_size: 20, data_version: "v7" },
      })
      .mockRejectedValueOnce(new Error("refresh failed"));
    recomputeMaintenanceCostGaps.mockResolvedValueOnce({
      data: { resolved: 1, remaining: 0, data_version: "v8" },
    });
    render(
      <MemoryRouter>
        <MaintenanceCostRefillPage projectId="project-1" />
      </MemoryRouter>,
    );

    await screen.findByText("PN-MISSING");
    fireEvent.click(screen.getByRole("button", { name: "重新匹配系统价格" }));

    expect(await screen.findByText("已更新 1 行系统价格，仍有 0 行缺价。"))
      .toBeInTheDocument();
    expect(await screen.findByText("系统价格已更新，但缺价清单刷新失败，请手动重试。"))
      .toBeInTheDocument();
    expect(screen.queryByText(/数据未改动/)).toBeNull();
  });

  it("高页码重算后显式回到第 1 页加载，避免清单缩水后停留空页", async () => {
    const pageTwoGap = { ...gap, line_id: "line-21", pn: "PN-PAGE-2" };
    listMaintenanceCostGaps
      .mockResolvedValueOnce({
        data: { rows: [gap], total: 21, page: 1, page_size: 20, data_version: "v7" },
      })
      .mockResolvedValueOnce({
        data: { rows: [pageTwoGap], total: 21, page: 2, page_size: 20, data_version: "v7" },
      })
      .mockResolvedValue({
        data: { rows: [], total: 0, page: 1, page_size: 20, data_version: "v8" },
      });
    recomputeMaintenanceCostGaps.mockResolvedValueOnce({
      data: { resolved: 1, remaining: 0, data_version: "v8" },
    });
    render(
      <MemoryRouter>
        <MaintenanceCostRefillPage projectId="project-1" />
      </MemoryRouter>,
    );

    await screen.findByText("PN-MISSING");
    fireEvent.click(screen.getByTitle("2"));
    await screen.findByText("PN-PAGE-2");
    fireEvent.click(screen.getByRole("button", { name: "重新匹配系统价格" }));

    await waitFor(() => expect(listMaintenanceCostGaps).toHaveBeenLastCalledWith(
      "project-1",
      { page: 1, page_size: 20 },
    ));
    expect(await screen.findByText("当前项目没有待回填成本")).toBeInTheDocument();
    expect(screen.queryByTitle("2")).toBeNull();
  });

  it("展示关联采购和前后 7 天加权证据，人工确认后按版本回填", async () => {
    render(
      <MemoryRouter>
        <MaintenanceCostRefillPage projectId="project-1" />
      </MemoryRouter>,
    );

    expect(await screen.findByText("PN-MISSING")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回项目" })).toHaveAttribute(
      "href",
      "/maintenance/beta/projects/project-1",
    );
    expect(screen.getByText("关联采购")).toBeInTheDocument();
    expect(screen.getByText("采购 ±7 天加权")).toBeInTheDocument();
    expect(screen.getByText("销售 ±7 天加权")).toBeInTheDocument();
    expect(screen.getByText("PO-7")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "回填 PN-MISSING" }));
    const dialog = await screen.findByRole("dialog", { name: "回填成本" });
    fireEvent.click(within(dialog).getByRole("button", { name: "采用采购 ±7 天加权参考" }));
    expect(within(dialog).getByTestId("inc-tax-unit-cost-preview")).toHaveTextContent("¥103.96");
    fireEvent.change(within(dialog).getByLabelText("回填原因"), {
      target: { value: "已核对采购发票" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存成本" }));

    await waitFor(() => expect(updateMaintenanceCostGap).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({
        line_id: "line-1",
        version: 7,
        unit_cost_ex_tax: 92,
        reason: "已核对采购发票",
        evidence: expect.stringContaining("PO-7"),
      }),
    ));
    expect(await screen.findByText("成本已回填")).toBeInTheDocument();
  });

  it("未税输入变化时按 13% 和 HALF_UP 实时预览含税单位成本", async () => {
    render(
      <MemoryRouter>
        <MaintenanceCostRefillPage projectId="project-1" />
      </MemoryRouter>,
    );

    await screen.findByText("PN-MISSING");
    fireEvent.click(screen.getByRole("button", { name: "回填 PN-MISSING" }));
    const dialog = await screen.findByRole("dialog", { name: "回填成本" });
    const input = within(dialog).getByLabelText("未税单位成本");
    fireEvent.change(input, { target: { value: "100.5" } });

    expect(within(dialog).getByTestId("inc-tax-unit-cost-preview"))
      .toHaveTextContent("¥113.57");
    expect(within(dialog).getByText(/13%/)).toBeInTheDocument();
    expect(within(dialog).getByText(/HALF_UP/)).toBeInTheDocument();
  });

  it("允许有证据的零元更换并保留 0.00 含税预览和保存值", async () => {
    render(
      <MemoryRouter>
        <MaintenanceCostRefillPage projectId="project-1" />
      </MemoryRouter>,
    );

    await screen.findByText("PN-MISSING");
    fireEvent.click(screen.getByRole("button", { name: "回填 PN-MISSING" }));
    const dialog = await screen.findByRole("dialog", { name: "回填成本" });
    fireEvent.change(within(dialog).getByLabelText("未税单位成本"), {
      target: { value: "0" },
    });
    fireEvent.change(within(dialog).getByLabelText("价格证据"), {
      target: { value: "厂家免费更换确认单 FREE-001" },
    });
    fireEvent.change(within(dialog).getByLabelText("回填原因"), {
      target: { value: "有证据的免费更换" },
    });

    expect(within(dialog).getByTestId("inc-tax-unit-cost-preview"))
      .toHaveTextContent("¥0.00");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存成本" }));

    await waitFor(() => expect(updateMaintenanceCostGap).toHaveBeenCalledWith(
      "project-1",
      {
        line_id: "line-1",
        version: 7,
        unit_cost_ex_tax: 0,
        evidence: "厂家免费更换确认单 FREE-001",
        reason: "有证据的免费更换",
      },
    ));
  });

  it("负数成本仍被拒绝且不会发出保存请求", async () => {
    render(
      <MemoryRouter>
        <MaintenanceCostRefillPage projectId="project-1" />
      </MemoryRouter>,
    );

    await screen.findByText("PN-MISSING");
    fireEvent.click(screen.getByRole("button", { name: "回填 PN-MISSING" }));
    const dialog = await screen.findByRole("dialog", { name: "回填成本" });
    fireEvent.change(within(dialog).getByLabelText("未税单位成本"), {
      target: { value: "-0.01" },
    });
    fireEvent.change(within(dialog).getByLabelText("价格证据"), {
      target: { value: "异常负价证据" },
    });
    fireEvent.change(within(dialog).getByLabelText("回填原因"), {
      target: { value: "验证负数拦截" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存成本" }));

    expect(await within(dialog).findByText(/请填写有效未税单位成本/)).toBeInTheDocument();
    expect(updateMaintenanceCostGap).not.toHaveBeenCalled();
  });

  it("人工保存前出现系统证据时明确提示采用系统价格", async () => {
    updateMaintenanceCostGap.mockResolvedValueOnce({
      data: {
        issue_line_id: "line-1",
        version: 8,
        unit_cost: "92.00",
        cost_amount: "184.00",
        unit_cost_ex_tax: "81.42",
        unit_cost_inc_tax: "92.00",
        cost_amount_ex_tax: "162.83",
        cost_amount_inc_tax: "184.00",
        cost_source: "purchase_window",
        manual_applied: false,
        resolution: "automatic_evidence",
      },
    });
    render(
      <MemoryRouter>
        <MaintenanceCostRefillPage projectId="project-1" />
      </MemoryRouter>,
    );

    await screen.findByText("PN-MISSING");
    fireEvent.click(screen.getByRole("button", { name: "回填 PN-MISSING" }));
    const dialog = await screen.findByRole("dialog", { name: "回填成本" });
    fireEvent.click(within(dialog).getByRole("button", { name: "采用采购 ±7 天加权参考" }));
    fireEvent.change(within(dialog).getByLabelText("回填原因"), {
      target: { value: "保存前再次核对" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存成本" }));

    expect(await screen.findByText("保存时发现新的系统价格，已采用系统证据并刷新清单。"))
      .toBeInTheDocument();
  });

  it("版本冲突时自动刷新到最新版本并保留草稿后可再次保存", async () => {
    const latestGap = { ...gap, version: 8 };
    listMaintenanceCostGaps
      .mockResolvedValueOnce({
        data: { rows: [gap], total: 1, page: 1, page_size: 20, data_version: "v7" },
      })
      .mockResolvedValue({
        data: { rows: [latestGap], total: 1, page: 1, page_size: 20, data_version: "v8" },
      });
    updateMaintenanceCostGap.mockRejectedValueOnce({ response: { status: 409 } });
    render(
      <MemoryRouter>
        <MaintenanceCostRefillPage projectId="project-1" />
      </MemoryRouter>,
    );

    await screen.findByText("PN-MISSING");
    fireEvent.click(screen.getByRole("button", { name: "回填 PN-MISSING" }));
    const dialog = await screen.findByRole("dialog", { name: "回填成本" });
    fireEvent.click(within(dialog).getByRole("button", { name: "采用采购 ±7 天加权参考" }));
    fireEvent.change(within(dialog).getByLabelText("回填原因"), {
      target: { value: "冲突时不能丢掉" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存成本" }));

    expect(await within(dialog).findByText("已刷新到最新版本；当前草稿已保留，请核对后重新保存。"))
      .toBeInTheDocument();
    await waitFor(() => expect(listMaintenanceCostGaps).toHaveBeenCalledTimes(2));
    expect(within(dialog).getByLabelText("回填原因")).toHaveValue("冲突时不能丢掉");

    fireEvent.click(within(dialog).getByRole("button", { name: "保存成本" }));
    await waitFor(() => expect(updateMaintenanceCostGap).toHaveBeenLastCalledWith(
      "project-1",
      expect.objectContaining({ version: 8, reason: "冲突时不能丢掉" }),
    ));
  });

  it("版本冲突刷新未返回时切换项目，不会重新打开旧项目草稿", async () => {
    let resolveConflict!: (value: { data: {
      rows: Array<typeof gap>;
      total: number;
      page: number;
      page_size: number;
      data_version: string;
    } }) => void;
    const pendingConflict = new Promise<Parameters<typeof resolveConflict>[0]>((resolve) => {
      resolveConflict = resolve;
    });
    listMaintenanceCostGaps
      .mockResolvedValueOnce({
        data: { rows: [gap], total: 1, page: 1, page_size: 20, data_version: "v7" },
      })
      .mockReturnValueOnce(pendingConflict)
      .mockResolvedValue({
        data: { rows: [], total: 0, page: 1, page_size: 20, data_version: "v1" },
      });
    updateMaintenanceCostGap.mockRejectedValueOnce({ response: { status: 409 } });

    render(
      <MemoryRouter initialEntries={["/maintenance/beta/cost-refill?project_id=project-1"]}>
        <MaintenanceCostRefillPage />
      </MemoryRouter>,
    );

    await screen.findByText("PN-MISSING");
    fireEvent.click(screen.getByRole("button", { name: "回填 PN-MISSING" }));
    const dialog = await screen.findByRole("dialog", { name: "回填成本" });
    fireEvent.click(within(dialog).getByRole("button", { name: "采用采购 ±7 天加权参考" }));
    fireEvent.change(within(dialog).getByLabelText("回填原因"), {
      target: { value: "旧项目草稿" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存成本" }));
    await waitFor(() => expect(listMaintenanceCostGaps).toHaveBeenCalledTimes(2));

    fireEvent.mouseDown(screen.getByRole("combobox"));
    fireEvent.click(await screen.findByText("XM-002 · 项目 B"));
    await waitFor(() => expect(listMaintenanceCostGaps).toHaveBeenCalledWith(
      "project-2",
      { page: 1, page_size: 20 },
    ));

    await act(async () => {
      resolveConflict({
        data: { rows: [{ ...gap, version: 8 }], total: 1, page: 1, page_size: 20, data_version: "v8" },
      });
      await pendingConflict;
    });
    expect(screen.queryByRole("dialog", { name: "回填成本" })).toBeNull();
    expect(updateMaintenanceCostGap).toHaveBeenCalledTimes(1);
  });

  it("无项目管理权限时拒绝直接进入回填页面且不读取缺价数据", async () => {
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({
      action_maintenance_project_manage: false,
    }));

    render(
      <MemoryRouter>
        <MaintenanceCostRefillPage projectId="project-1" />
      </MemoryRouter>,
    );

    expect(screen.getByText("无人工成本回填权限")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存成本" })).toBeNull();
    await waitFor(() => expect(listMaintenanceCostGaps).not.toHaveBeenCalled());
  });

  it("只有项目管理动作权限但无采购成本字段权限时不展示回填入口", async () => {
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({
      action_maintenance_project_manage: true,
      data_purchase_cost: false,
    }));

    render(
      <MemoryRouter>
        <MaintenanceCostRefillPage projectId="project-1" />
      </MemoryRouter>,
    );

    expect(screen.getByText("无人工成本回填权限")).toBeInTheDocument();
    await waitFor(() => expect(listMaintenanceCostGaps).not.toHaveBeenCalled());
  });
});
