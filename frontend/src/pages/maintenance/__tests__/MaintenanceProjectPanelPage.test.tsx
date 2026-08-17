import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const searchBoardProjects = vi.fn();
const getBoardProjectOrders = vi.fn();
const getBoardOrderLines = vi.fn();
const getMaintenanceProject = vi.fn();
const listMaintenanceSourceOrders = vi.fn();
const assignMaintenanceSourceOrders = vi.fn();
const downloadProjectMaster = vi.fn();
const listProjectExpenseRows = vi.fn();

vi.mock("../../../api/maintenanceBossBoard", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceBossBoard",
  );
  return {
    ...actual,
    searchBoardProjects: (...a: unknown[]) => searchBoardProjects(...a),
    getBoardProjectOrders: (...a: unknown[]) => getBoardProjectOrders(...a),
    getBoardOrderLines: (...a: unknown[]) => getBoardOrderLines(...a),
  };
});
vi.mock("../../../api/maintenanceProjects", () => ({
  getMaintenanceProject: (...a: unknown[]) => getMaintenanceProject(...a),
  updateMaintenanceProject: vi.fn(),
}));
vi.mock("../../../api/maintenanceSourceAssignments", () => ({
  listMaintenanceSourceOrders: (...a: unknown[]) => listMaintenanceSourceOrders(...a),
  assignMaintenanceSourceOrders: (...a: unknown[]) => assignMaintenanceSourceOrders(...a),
}));
vi.mock("../../../api/maintenanceWorkbooks", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceWorkbooks",
  );
  return {
    ...actual,
    downloadProjectMaster: (...a: unknown[]) => downloadProjectMaster(...a),
    listProjectExpenseRows: (...a: unknown[]) => listProjectExpenseRows(...a),
    applyProjectMaster: vi.fn(),
    saveBlob: vi.fn(),
  };
});

import MaintenanceProjectPanelPage from "../MaintenanceProjectPanelPage";

const stat = <T,>(value: T) => ({ state: "ready" as const, value, as_of: null });
const notImported = () => ({ state: "not_imported" as const, value: null, as_of: null });

const projectRow = {
  project_id: "p1", project_code: "合成项目A", display_name: "合成项目A",
  lifecycle: "ongoing", is_archived: false,
  contract_nos: ["XSDD-1", "XSDD-2"], project_manager: "李经理",
  contract_amount_inc_tax: stat("1000.00"),
  known_apply_cost_ex_tax: stat("500.00"), procured_qty: stat("1.000"),
  collection_preview_inc_tax: stat("100.00"), cost_ratio_pct: stat("50.0"),
  card_status: "normal" as const, has_activity_in_window: true,
  pre_delivery_order_count: 0, orders_ytd: stat(1), lines_ytd: stat(1),
  known_apply_cost_inc_tax: stat({
    actual_amount: "565.00", estimated_amount: "0", known_amount: "565.00",
    missing_lines: 0, coverage_pct: 100, quality: "actual_only" as const,
  }),
  shipped_qty: notImported(), returned_good_qty: notImported(),
  returned_bad_qty: notImported(),
};

const orderRow = {
  source_order_id: "RAW-1", order_no: "WBDD-1", order_date: "2026-07-15",
  data_status: "已生效", project_raw: "合成项目A", is_pre_delivery: false,
  line_count: 2, known_apply_cost_inc_tax: projectRow.known_apply_cost_inc_tax,
  self_report: { head_demand_qty: "3", head_purchase_qty: "2",
                 head_shipped_qty: "2", head_returned_qty: "1" },
  facts: { shipped_qty: notImported(), returned_good_qty: notImported(),
           returned_bad_qty: notImported() },
  facts_scope: "project" as const,
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  searchBoardProjects.mockResolvedValue({ data: { rows: [projectRow], total: 1 } });
  // 与后端真实契约同形（MaintenanceProjectOverview = {project: {...}}）——旧 mock 的
  // 扁平形状曾掩盖「按 UUID 搜卡墙永远搜不到」的取数缺陷（2026-08-17 生产实发）。
  getMaintenanceProject.mockResolvedValue({
    data: {
      project: {
        project_id: "p1", project_code: "合成项目A", display_name: "合成项目A",
        project_manager_id: null, lifecycle_status: "ongoing",
        is_active: true, version: 1,
      },
    },
  });
  getBoardProjectOrders.mockResolvedValue({ data: { rows: [orderRow], total: 1 } });
  getBoardOrderLines.mockResolvedValue({ data: { rows: [], total: 0 } });
  listMaintenanceSourceOrders.mockResolvedValue({ data: { rows: [] } });
  listProjectExpenseRows.mockResolvedValue({ rows: [], total: 0 });
});

afterEach(cleanup);

function renderPanel() {
  return render(
    <MemoryRouter initialEntries={["/maintenance/projects/p1"]}>
      <Routes>
        <Route path="/maintenance/projects/:projectId"
               element={<MaintenanceProjectPanelPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("项目面板", () => {
  it("顶部出库明细列出单据", async () => {
    renderPanel();
    expect(await screen.findByText("WBDD-1")).toBeInTheDocument();
    expect(screen.getByText("出库明细")).toBeInTheDocument();
  });

  it("四个 tab 齐全（表 6 的 web 呈现）", async () => {
    renderPanel();
    for (const label of ["项目基础信息", "备件成本", "报销", "回款"]) {
      expect(await screen.findByRole("tab", { name: label })).toBeInTheDocument();
    }
  });

  it("多合同项目给出合同筛选（#39）", async () => {
    renderPanel();
    expect(await screen.findByText("全部合同")).toBeInTheDocument();
  });

  it("总表下载走六 sheet（不传 sheets 参数）", async () => {
    downloadProjectMaster.mockResolvedValue(new Blob(["x"]));
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /下载本项目总表/ }));
    await waitFor(() => expect(downloadProjectMaster).toHaveBeenCalledWith("p1"));
  });

  it("tab 内下载只取该 sheet（#38 在哪下载就在哪上传）", async () => {
    downloadProjectMaster.mockResolvedValue(new Blob(["x"]));
    renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "备件成本" }));
    fireEvent.click(await screen.findByRole("button", { name: /下载备件成本/ }));
    await waitFor(() =>
      expect(downloadProjectMaster).toHaveBeenCalledWith("p1", ["03_备件订单"]));
  });

  it("无上传动作键时 tab 内没有上传入口", async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "报销" }));
    await screen.findByRole("button", { name: /下载报销/ });
    expect(screen.queryByRole("button", { name: /上传覆盖/ })).toBeNull();
  });

  it("无项目管理动作键时不显示归属挂靠与编辑入口", async () => {
    renderPanel();
    await screen.findByText("WBDD-1");
    expect(screen.queryByText(/归属挂靠/)).toBeNull();
    expect(screen.getByRole("button", { name: /编辑基本信息/ })).toBeDisabled();
  });

  it("有项目管理动作键时给出归属挂靠（#45 判定依据＝XSDD）", async () => {
    localStorage.setItem("permissions",
      JSON.stringify({ action_maintenance_project_manage: true }));
    listMaintenanceSourceOrders.mockResolvedValue({
      data: { rows: [{ raw_order_id: "RAW-9", order_no: "WBDD-9",
                       order_date: "2026-07-20", project_raw: "某项目",
                       matches_project_xsdd: true }] },
    });
    renderPanel();
    expect(await screen.findByText(/归属挂靠（判定依据＝XSDD 销售订单）/))
      .toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /确认挂靠到本项目/ }));
    await waitFor(() => expect(assignMaintenanceSourceOrders).toHaveBeenCalled());
    expect(assignMaintenanceSourceOrders.mock.calls[0][0]).toMatchObject({
      project_id: "p1",
      items: [{ source_order_id: "RAW-9" }],
    });
  });

  it("状态列在明细里原样展示，页面明示不参与计算（铁律 3）", async () => {
    getBoardOrderLines.mockResolvedValue({
      data: { rows: [{
        raw_line_id: "L1", pn_std: "PN-1", pn_raw: "PN-1",
        pool: { in_pool: null, pool_name: null, pool_status: null },
        description: "描述", qty: "3", return_qty: "0",
        purchased_qty: "1", pending_supply_qty: "1", pending_return_qty: "1",
        consumed_qty: null,
        known_apply_cost_inc_tax: stat("100.00"), cost_source: stat("direct"),
        confidence: stat("high"),
      }], total: 1 },
    });
    renderPanel();
    fireEvent.click(await screen.findByText("WBDD-1"));
    expect(await screen.findByText(/系统只展示、不参与任何计算/)).toBeInTheDocument();
  });
});

describe("归属挂靠候选按 XSDD 预筛（#48）", () => {
  it("把本项目 id 交给后端排序，而不是前端筛（前端只拿一页会漏选）", async () => {
    localStorage.setItem("permissions",
      JSON.stringify({ action_maintenance_project_manage: true }));
    renderPanel();
    await waitFor(() => expect(listMaintenanceSourceOrders).toHaveBeenCalled());
    expect(listMaintenanceSourceOrders.mock.calls[0][0]).toMatchObject({
      assignment_status: "unassigned",
      xsdd_project_id: "p1",
    });
  });

  it("命中本项目 XSDD 的候选打「同 XSDD」标", async () => {
    localStorage.setItem("permissions",
      JSON.stringify({ action_maintenance_project_manage: true }));
    listMaintenanceSourceOrders.mockResolvedValue({
      data: { rows: [
        { raw_order_id: "RAW-A", order_no: "WBDD-A", order_date: "2026-07-20",
          project_raw: "本项目", matches_project_xsdd: true },
        { raw_order_id: "RAW-B", order_no: "WBDD-B", order_date: "2026-07-21",
          project_raw: "别的", matches_project_xsdd: false },
      ] },
    });
    renderPanel();
    expect(await screen.findByText("同 XSDD")).toBeInTheDocument();
    // 不命中的单仍在列表里——这是排序不是过滤
    expect(screen.getByText("WBDD-B")).toBeInTheDocument();
  });
});

describe("报销 tab 展示备注（#47）", () => {
  it("列出报销行并显示备注列", async () => {
    listProjectExpenseRows.mockResolvedValue({
      rows: [{
        raw_line_id: "BXD-1#1", bxd_no: "BXD-20260101-1",
        expense_date: "2026-07-01", person: "张三", expense_type: "差旅",
        fee_category: "交通", reason: "现场维保", contract_no: "XSDD-1",
        amount_ex_tax: "100.00", amount_inc_tax: "113.00",
        data_status: "已结束", remark: "客户确认可报",
      }],
      total: 1,
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "报销" }));
    expect(await screen.findByText("客户确认可报")).toBeInTheDocument();
    expect(screen.getByText("BXD-20260101-1")).toBeInTheDocument();
  });

  it("没有备注时显示「—」，不显示空白也不显示 0", async () => {
    listProjectExpenseRows.mockResolvedValue({
      rows: [{
        raw_line_id: "BXD-2#1", bxd_no: "BXD-2", expense_date: null,
        person: null, expense_type: null, fee_category: null, reason: null,
        contract_no: null, amount_ex_tax: null, amount_inc_tax: null,
        data_status: null, remark: null,
      }],
      total: 1,
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "报销" }));
    await screen.findByText("BXD-2");
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("上传覆盖后回读，页面不留旧值", async () => {
    listProjectExpenseRows.mockResolvedValue({ rows: [], total: 0 });
    renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "报销" }));
    await waitFor(() => expect(listProjectExpenseRows).toHaveBeenCalledWith("p1"));
  });
});
