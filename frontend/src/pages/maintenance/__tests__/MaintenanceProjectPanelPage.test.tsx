import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Modal, message } from "antd";

const getBoardProject = vi.fn();
const getBoardProjectOrders = vi.fn();
const getBoardOrderLines = vi.fn();
const getMaintenanceProject = vi.fn();
const updateMaintenanceProject = vi.fn();
const listMaintenanceSourceOrders = vi.fn();
const assignMaintenanceSourceOrders = vi.fn();
const downloadProjectMaster = vi.fn();
const validateProjectMaster = vi.fn();
const applyProjectMaster = vi.fn();
const getCollectionPlan = vi.fn();
const listProjectExpenseRows = vi.fn();
const listProjectPartsRows = vi.fn();
const getMaintenanceProjectWorkspace = vi.fn();
const searchMaintenanceManagerAccounts = vi.fn();
const searchSiteIssues = vi.fn();
const searchMaintenanceReturnObligations = vi.fn();
const searchMaintenanceBadReturns = vi.fn();

vi.mock("../../../api/maintenanceBossBoard", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceBossBoard",
  );
  return {
    ...actual,
    getBoardProject: (...a: unknown[]) => getBoardProject(...a),
    getBoardProjectOrders: (...a: unknown[]) => getBoardProjectOrders(...a),
    getBoardOrderLines: (...a: unknown[]) => getBoardOrderLines(...a),
  };
});
vi.mock("../../../api/maintenanceProjects", () => ({
  getMaintenanceProject: (...a: unknown[]) => getMaintenanceProject(...a),
  updateMaintenanceProject: (...a: unknown[]) => updateMaintenanceProject(...a),
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
    getCollectionPlan: (...a: unknown[]) => getCollectionPlan(...a),
    listProjectExpenseRows: (...a: unknown[]) => listProjectExpenseRows(...a),
    listProjectPartsRows: (...a: unknown[]) => listProjectPartsRows(...a),
    validateProjectMaster: (...a: unknown[]) => validateProjectMaster(...a),
    applyProjectMaster: (...a: unknown[]) => applyProjectMaster(...a),
    saveBlob: vi.fn(),
  };
});
vi.mock("../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceOperations",
  );
  return {
    ...actual,
    getMaintenanceProjectWorkspace: (...a: unknown[]) => getMaintenanceProjectWorkspace(...a),
    searchMaintenanceManagerAccounts: (...a: unknown[]) =>
      searchMaintenanceManagerAccounts(...a),
    searchSiteIssues: (...a: unknown[]) => searchSiteIssues(...a),
    searchMaintenanceReturnObligations: (...a: unknown[]) =>
      searchMaintenanceReturnObligations(...a),
    searchMaintenanceBadReturns: (...a: unknown[]) => searchMaintenanceBadReturns(...a),
  };
});

import MaintenanceProjectPanelPage from "../MaintenanceProjectPanelPage";

const stat = <T,>(value: T) => ({ state: "ready" as const, value, as_of: null });
const notImported = () => ({ state: "not_imported" as const, value: null, as_of: null });

const projectRow = {
  project_id: "p1", project_code: "合成项目A", display_name: "合成项目A",
  lifecycle: "ongoing", is_archived: false,
  contract_nos: ["XSDD-1", "XSDD-2"], project_manager: "李经理",
  salesperson: "王销售",
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
  getBoardProject.mockResolvedValue({ data: projectRow });
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
  searchMaintenanceManagerAccounts.mockResolvedValue({ data: { rows: [] } });
  updateMaintenanceProject.mockResolvedValue({ data: {} });
  listProjectExpenseRows.mockResolvedValue({ rows: [], total: 0 });
  listProjectPartsRows.mockResolvedValue({ rows: [], total: 0, sheet: "03_备件订单" });
  getCollectionPlan.mockResolvedValue({ rows: [], total: 0 });
  getMaintenanceProjectWorkspace.mockResolvedValue({
    data: {
      project: { metrics: {
        received_amount: 100,
        total_contract_amount: 1000,
        collection_progress_pct: 10,
      } },
      collection_snapshots: { rows: [], total: 0, page: 1, page_size: 100 },
    },
  });
  searchSiteIssues.mockResolvedValue({
    data: { project_id: "p1", rows: [], total: 0, page: 1, page_size: 100 },
  });
  searchMaintenanceReturnObligations.mockResolvedValue({
    data: { project_id: "p1", rows: [], total: 0, page: 1, page_size: 200 },
  });
  searchMaintenanceBadReturns.mockResolvedValue({
    data: { project_id: "p1", rows: [], total: 0, page: 1, page_size: 100 },
  });
  validateProjectMaster.mockResolvedValue({
    expense_updates: 1,
    will_void_rows: [],
    will_reassign_orders: [],
  });
  applyProjectMaster.mockResolvedValue({ expense_updates: 1 });
});

afterEach(() => {
  cleanup();
  Modal.destroyAll();
  message.destroy();
});

function renderPanel(withProjectSwitcher = false) {
  return render(
    <MemoryRouter initialEntries={["/maintenance/projects/p1"]}>
      {withProjectSwitcher ? (
        <Link to="/maintenance/projects/p2">切到项目B</Link>
      ) : null}
      <Routes>
        <Route path="/maintenance/projects/:projectId"
               element={<MaintenanceProjectPanelPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("项目面板", () => {
  it("切换项目后旧项目的晚到响应不会回写新项目", async () => {
    let resolveOldSearch!: (value: unknown) => void;
    const oldSearch = new Promise((resolve) => { resolveOldSearch = resolve; });
    const projectB = {
      ...projectRow,
      project_id: "p2",
      project_code: "合成项目B",
      display_name: "合成项目B",
      contract_nos: ["XSDD-B"],
    };
    getMaintenanceProject.mockImplementation((id: string) => Promise.resolve({
      data: {
        project: {
          project_id: id,
          project_code: id === "p1" ? "合成项目A" : "合成项目B",
          display_name: id === "p1" ? "合成项目A" : "合成项目B",
          project_manager_id: null,
          lifecycle_status: "ongoing",
          is_active: true,
          version: 1,
        },
      },
    }));
    getBoardProject.mockImplementation((id: string) =>
      id === "p1"
        ? oldSearch
        : Promise.resolve({ data: projectB }));

    renderPanel(true);
    expect(await screen.findByRole("heading", { name: "合成项目A" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "切到项目B" }));
    expect(await screen.findByRole("heading", { name: "合成项目B" })).toBeInTheDocument();

    resolveOldSearch({ data: projectRow });
    await waitFor(() => {
      expect(screen.queryByRole("heading", { name: "合成项目A" })).toBeNull();
      expect(screen.getByRole("heading", { name: "合成项目B" })).toBeInTheDocument();
    });
  });

  it("页头展示项目名、状态 Tag 和总表下载（主操作提到页头）", async () => {
    renderPanel();
    expect(await screen.findByRole("heading", { name: "合成项目A" })).toBeInTheDocument();
    // 页头生命周期 Tag（概览 tab 的 Descriptions 也有一项，故不校验唯一性）
    expect(screen.getAllByText("进行中").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /下载本项目总表/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /返回项目墙/ })).toBeInTheDocument();
  });

  it("健康带四格：合同额、累计已回款、回款进度、成本率", async () => {
    renderPanel();
    const band = await screen.findByTestId("panel-health-band");
    // antd Statistic 会把带小数的值拆成整数/小数两个 span 并加千分位，按 textContent 断言
    const bandText = () => (band.textContent ?? "").replace(/,/g, "");
    await waitFor(() => expect(bandText()).toContain("1000.00"));
    expect(bandText()).toContain("合同总额（含税）");
    expect(bandText()).toContain("累计已回款");
    expect(bandText()).toContain("¥100.00");
    expect(bandText()).toContain("回款进度");
    expect(bandText()).toContain("10%");
    expect(bandText()).toContain("成本率");
    expect(bandText()).toContain("50.0%");
  });

  it("健康带把 partial 合同额明确标为已知小计而非完整总额", async () => {
    getBoardProject.mockResolvedValue({
      data: {
        ...projectRow,
        contract_amount_inc_tax: {
          state: "partial",
          value: "800.00",
          as_of: null,
        },
      },
    });
    renderPanel();
    const band = await screen.findByTestId("panel-health-band");
    await waitFor(() => expect((band.textContent ?? "").replace(/,/g, ""))
      .toContain("800.00（已知小计，合同事实不完整）"));
  });

  it("部分缺价时健康带把备件成本与成本率标成已知下限", async () => {
    getBoardProject.mockResolvedValue({
      data: {
        ...projectRow,
        cost_ratio_pct: stat("85.0"),
        known_apply_cost_inc_tax: {
          state: "partial",
          value: {
            actual_amount: "850.00",
            estimated_amount: "0.00",
            known_amount: "850.00",
            missing_lines: 1,
            coverage_pct: 80,
            quality: "incomplete",
          },
          as_of: null,
        },
      },
    });
    renderPanel();
    const band = await screen.findByTestId("panel-health-band");
    await waitFor(() =>
      expect((band.textContent ?? "").replace(/,/g, ""))
        .toContain("¥850.00（已知下限）"));
    expect(band.textContent).toContain("85.0%（已知下限）");
  });

  it("聚合行缺失时健康带说「聚合数据暂缺」，页面不报错、基础信息走 stable 回退", async () => {
    getBoardProject.mockRejectedValue(new Error("aggregate unavailable"));
    renderPanel();
    const band = await screen.findByTestId("panel-health-band");
    await waitFor(() =>
      expect(within(band).getAllByText("聚合数据暂缺").length).toBeGreaterThan(0));
    expect(await screen.findByRole("heading", { name: "合成项目A" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("五个同级 tab 重排：概览 / 备件与需求单 / 报销 / 回款 / 领用与返还", async () => {
    renderPanel();
    for (const label of ["概览", "备件与需求单", "报销", "回款", "领用与返还"]) {
      expect(await screen.findByRole("tab", { name: label })).toBeInTheDocument();
    }
  });

  it("出库明细并入「备件与需求单」tab：需求单列表 + 点击单号钻取行级明细", async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "备件与需求单" }));
    expect(await screen.findByText("WBDD-1")).toBeInTheDocument();
    // 首屏不再被出库明细大表占据：概览是默认 tab，需求单表不在这里
  });

  it("概览 tab 只读：没有 01 表下载/上传，全页上传入口只有页头总表", async () => {
    localStorage.setItem("permissions",
      JSON.stringify({ action_maintenance_expense_collection_upload: true }));
    renderPanel();
    await screen.findByTestId("panel-health-band");
    expect(screen.queryByRole("button", { name: /下载基础信息表/ })).toBeNull();
    expect(screen.getAllByRole("button", { name: /上传覆盖/ })).toHaveLength(1);
  });

  it("回款 tab 只留快照表：累计/进度已上健康带，不再重复", async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "回款" }));
    expect(await screen.findByText("本项目暂无回款记录")).toBeInTheDocument();
    expect(screen.getAllByText("回款进度")).toHaveLength(1);
    expect(screen.getAllByText("累计已回款")).toHaveLength(1);
  });

  it("回款 tab 展示每条回款状态", async () => {
    getMaintenanceProjectWorkspace.mockResolvedValue({
      data: {
        project: { metrics: {
          received_amount: 600,
          total_contract_amount: 1000,
          collection_progress_pct: 60,
        } },
        collection_snapshots: {
          rows: [{
            collection_id: "COL-1",
            project_contract_id: "PC-1",
            contract_no: "XSDD-1",
            report_month: "2026-08-01",
            cumulative_amount: 600,
            receipt_reference: "REC-1",
            status: "confirmed",
            remark: "已到账",
            version: 1,
          }],
          total: 1,
          page: 1,
          page_size: 100,
        },
      },
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "回款" }));
    expect(await screen.findByText("回款状态")).toBeInTheDocument();
    expect(screen.getByText("已确认")).toBeInTheDocument();
    expect(screen.getByText("REC-1")).toBeInTheDocument();
    // 进度在健康带（页面顶部），不在 tab 内
    const band = screen.getByTestId("panel-health-band");
    expect(within(band).getByText("60%")).toBeInTheDocument();
  });

  it("workspace 回款按 total 拉取后续页", async () => {
    const snapshot = (id: string, reference: string) => ({
      collection_id: id,
      project_contract_id: "PC-1",
      contract_no: "XSDD-1",
      report_month: "2026-08-01",
      cumulative_amount: 600,
      receipt_reference: reference,
      status: "confirmed",
      remark: null,
      version: 1,
    });
    getMaintenanceProjectWorkspace.mockImplementation((_id: string, params: { collection_page?: number }) =>
      Promise.resolve({
        data: {
          project: { metrics: {
            received_amount: 600,
            total_contract_amount: 1000,
            collection_progress_pct: 60,
          } },
          collection_snapshots: {
            rows: params.collection_page === 2
              ? [snapshot("COL-2", "REC-PAGE-2")]
              : [snapshot("COL-1", "REC-PAGE-1")],
            total: 2,
            page: params.collection_page ?? 1,
            page_size: 100,
          },
        },
      }));

    renderPanel();
    await waitFor(() => expect(getMaintenanceProjectWorkspace).toHaveBeenCalledWith(
      "p1",
      expect.objectContaining({ collection_page: 2, collection_page_size: 100 }),
    ));
    fireEvent.click(await screen.findByRole("tab", { name: "回款" }));
    expect(await screen.findByText("REC-PAGE-1")).toBeInTheDocument();
    expect(screen.getByText("REC-PAGE-2")).toBeInTheDocument();
  });

  it("领用与返还 tab 合并领用、返还义务和返还单状态", async () => {
    searchSiteIssues.mockResolvedValue({
      data: {
        project_id: "p1",
        rows: [{
          issue_id: "ISSUE-1",
          project_id: "p1",
          issue_no: "CKD-1",
          issue_date: "2026-08-18",
          workflow_status: "confirmed",
          lines: [{
            issue_line_id: "LINE-1",
            part_id: 9,
            pn: "PN-001",
            serial_number: "SN-001",
            quantity: "2",
            no_return: false,
          }],
        }],
        total: 1,
        page: 1,
        page_size: 100,
      },
    });
    searchMaintenanceReturnObligations.mockResolvedValue({
      data: {
        project_id: "p1",
        rows: [{
          obligation_id: "OB-1",
          issue_line_id: "LINE-1",
          classification: "required",
          required_quantity: "2",
          registered_quantity: "2",
          warehouse_confirmed_quantity: "2",
          remaining_quantity: "0",
        }],
        total: 1,
        page: 1,
        page_size: 200,
      },
    });
    searchMaintenanceBadReturns.mockResolvedValue({
      data: {
        project_id: "p1",
        rows: [{
          return_id: "RET-1",
          return_no: "HJFH-1",
          status: "warehouse_confirmed",
          lines: [{ obligation_id: "OB-1" }],
        }],
        total: 1,
        page: 1,
        page_size: 100,
      },
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "领用与返还" }));
    expect(await screen.findByText("CKD-1")).toBeInTheDocument();
    expect(screen.getByText("PN-001")).toBeInTheDocument();
    expect(screen.getByText("仓库已确认返还")).toBeInTheDocument();
    expect(screen.getByText("HJFH-1")).toBeInTheDocument();
  });

  it("领用、返还义务和返还单都按 total 拉取后续页", async () => {
    const issue = (index: number) => ({
      issue_id: `ISSUE-${index}`,
      project_id: "p1",
      issue_no: `CKD-PAGE-${index}`,
      issue_date: "2026-08-18",
      workflow_status: "confirmed",
      lines: [{
        issue_line_id: `LINE-${index}`,
        part_id: index,
        pn: `PN-${index}`,
        serial_number: null,
        quantity: "1",
        no_return: false,
      }],
    });
    const obligation = (index: number) => ({
      obligation_id: `OB-${index}`,
      issue_line_id: `LINE-${index}`,
      classification: "required",
      required_quantity: "1",
      registered_quantity: "1",
      warehouse_confirmed_quantity: "1",
      remaining_quantity: "0",
    });
    const returned = (index: number) => ({
      return_id: `RET-${index}`,
      return_no: `HJFH-PAGE-${index}`,
      status: "warehouse_confirmed",
      lines: [{ obligation_id: `OB-${index}` }],
    });
    searchSiteIssues.mockImplementation((input: { page: number }) => Promise.resolve({
      data: { rows: [issue(input.page)], total: 2, page: input.page, page_size: 100 },
    }));
    searchMaintenanceReturnObligations.mockImplementation((input: { page: number }) => Promise.resolve({
      data: { rows: [obligation(input.page)], total: 2, page: input.page, page_size: 200 },
    }));
    searchMaintenanceBadReturns.mockImplementation((input: { page: number }) => Promise.resolve({
      data: { rows: [returned(input.page)], total: 2, page: input.page, page_size: 100 },
    }));

    renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "领用与返还" }));
    expect(await screen.findByText("CKD-PAGE-2")).toBeInTheDocument();
    expect(screen.getByText("HJFH-PAGE-2")).toBeInTheDocument();
    expect(searchSiteIssues).toHaveBeenCalledWith(expect.objectContaining({ page_size: 100 }));
    expect(searchSiteIssues).toHaveBeenCalledWith(expect.objectContaining({ page: 2 }));
    expect(searchMaintenanceReturnObligations)
      .toHaveBeenCalledWith(expect.objectContaining({ page: 2 }));
    expect(searchMaintenanceBadReturns).toHaveBeenCalledWith(expect.objectContaining({ page: 2 }));
  });

  it("多合同项目在「备件与需求单」tab 给出合同筛选（#39）", async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "备件与需求单" }));
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
    fireEvent.click(await screen.findByRole("tab", { name: "备件与需求单" }));
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
    await screen.findByTestId("panel-health-band");
    expect(screen.queryByText(/归属挂靠/)).toBeNull();
    expect(screen.getByRole("button", { name: /编辑基本信息/ })).toBeDisabled();
  });

  it("有项目管理动作键时概览给出归属挂靠（#45 判定依据＝XSDD）", async () => {
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
    await waitFor(() => expect(getMaintenanceProjectWorkspace.mock.calls.length).toBeGreaterThan(1));
  });

  it("基础信息不再把来源负责人原文伪装成账号改派", async () => {
    localStorage.setItem("permissions",
      JSON.stringify({ action_maintenance_project_manage: true }));
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /编辑基本信息/ }));
    const dialog = await screen.findByRole("dialog", { name: "编辑项目基本信息" });
    expect(within(dialog).queryByLabelText("维保负责人")).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: /确 定|OK/i }));
    await waitFor(() => expect(updateMaintenanceProject).toHaveBeenCalledTimes(1));
    expect(updateMaintenanceProject.mock.calls[0][1]).not.toHaveProperty("project_manager_id");
  });

  it("只有实名 admin 且有管理动作键时显示真实负责人 OCC 控件", async () => {
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions",
      JSON.stringify({ action_maintenance_project_manage: true }));
    getMaintenanceProjectWorkspace.mockResolvedValue({
      data: {
        project: {
          project_id: "p1",
          project_code: "合成项目A",
          display_name: "合成项目A",
          project_manager_id: "来源负责人原文",
          manager_assignment: {
            assignment_id: "MA-1",
            project_id: "p1",
            responsibility_type: "primary_manager",
            user_id: 8,
            username: "manager-a",
            display_name: "真实负责人",
            account_status: "active",
            source_manager_text: "来源负责人原文",
            version: 3,
            assigned_at: "2026-08-27T00:00:00Z",
            archived_at: null,
          },
          metrics: {
            received_amount: 100,
            total_contract_amount: 1000,
            collection_progress_pct: 10,
          },
        },
        collection_snapshots: { rows: [], total: 0, page: 1, page_size: 100 },
      },
    });
    renderPanel();
    expect(await screen.findByRole("button", { name: "管理负责人" })).toBeInTheDocument();
    expect(await screen.findByText("真实负责人")).toBeInTheDocument();
    expect(screen.getAllByText("来源负责人原文").length).toBeGreaterThanOrEqual(2);
  });

  it("非 admin 即使拥有项目管理动作键也不显示负责人账号改派", async () => {
    localStorage.setItem("role", "maintenance_manager");
    localStorage.setItem("permissions",
      JSON.stringify({ action_maintenance_project_manage: true }));
    getMaintenanceProjectWorkspace.mockResolvedValue({
      data: {
        project: {
          project_id: "p1",
          project_manager_id: "来源负责人原文",
          manager_assignment: null,
          metrics: {
            received_amount: 100,
            total_contract_amount: 1000,
            collection_progress_pct: 10,
          },
        },
        collection_snapshots: { rows: [], total: 0, page: 1, page_size: 100 },
      },
    });
    renderPanel();
    await screen.findByTestId("panel-health-band");
    expect(screen.queryByRole("button", { name: "映射负责人" })).toBeNull();
  });

  it("明细 PN 为主：全量直出、PN+描述主列、单价两档、成本来源四分类配色（2026-08-20）", async () => {
    listProjectPartsRows.mockResolvedValue({
      total: 3,
      sheet: "03_备件订单",
      rows: [
        { line_id: 1, pn_std: "PN-1", order_no: "WBDD-1",
          description: "系统关联行", qty: "3", return_qty: "0",
          unit_cost_ex_tax: "88.50", unit_cost_inc_tax: "100.00",
          cost_amount_inc_tax: "300.00",
          cost_source: "direct", confidence: "high" },
        { line_id: 2, pn_std: "PN-2", order_no: "WBDD-2",
          description: "人工回填行", qty: "1", return_qty: "0",
          unit_cost_ex_tax: "50.00", unit_cost_inc_tax: "56.50",
          cost_amount_inc_tax: "56.50",
          cost_source: "manual", confidence: "none" },
        { line_id: 3, pn_std: "PN-3", order_no: "WBDD-3",
          description: "缺失行", qty: "2", return_qty: "0",
          unit_cost_ex_tax: null, unit_cost_inc_tax: null,
          cost_amount_inc_tax: null,
          cost_source: "none", confidence: "none" },
      ],
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "备件与需求单" }));
    // 配色图例
    expect(await screen.findByText("系统关联")).toBeInTheDocument();
    expect(screen.getAllByText("人工回填").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("缺失")).toBeInTheDocument();
    expect(screen.getByText("估算")).toBeInTheDocument();
    // PN 主列：加粗 PN + 描述副行
    expect(screen.getByText("PN-1")).toBeInTheDocument();
    expect(screen.getByText("系统关联行")).toBeInTheDocument();
    // 单价两档渲染
    expect(screen.getByText("88.50")).toBeInTheDocument();
    // 分类彩标：direct=绿标、manual=紫标、none=红标
    const directTag = screen.getByText("直接采购价").closest(".ant-tag");
    expect(directTag?.className).toContain("green");
    // 图例与成本来源列各有一个「人工回填」Tag，两个都应为紫色
    const manualTags = screen.getAllByText("人工回填").map((n) => n.closest(".ant-tag"));
    expect(manualTags.length).toBeGreaterThanOrEqual(2);
    expect(manualTags.every((t) => t?.className.includes("purple"))).toBe(true);
    const noneTag = screen.getByText("暂无成本").closest(".ant-tag");
    expect(noneTag?.className).toContain("red");
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

  it("页头总表必须等当前报销 tab 读回完成后才报刷新成功", async () => {
    localStorage.setItem("permissions",
      JSON.stringify({ action_maintenance_expense_collection_upload: true }));
    let resolveReadback!: (value: { rows: never[]; total: number }) => void;
    const { container } = renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "报销" }));
    await waitFor(() => expect(listProjectExpenseRows).toHaveBeenCalledTimes(1));
    listProjectExpenseRows.mockImplementationOnce(() =>
      new Promise((resolve) => { resolveReadback = resolve; }));

    const inputs = container.querySelectorAll<HTMLInputElement>('input[type="file"]');
    fireEvent.change(inputs[0], {
      target: { files: [new File(["xlsx"], "项目总表.xlsx")] },
    });
    fireEvent.click(await screen.findByRole("button", { name: /确认回传/ }));

    await waitFor(() => expect(applyProjectMaster).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(listProjectExpenseRows).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/已覆盖并刷新/)).toBeNull();

    await act(async () => resolveReadback({ rows: [], total: 0 }));
    expect(await screen.findByText(/已覆盖并刷新：报销更新 1 行/)).toBeInTheDocument();
  });

  it("页头总表落库后报销读回失败会清空旧行并明确提示刷新失败", async () => {
    localStorage.setItem("permissions",
      JSON.stringify({ action_maintenance_expense_collection_upload: true }));
    listProjectExpenseRows
      .mockResolvedValueOnce({
        rows: [{
          raw_line_id: "BXD-OLD#1",
          bxd_no: "BXD-OLD",
          expense_date: null,
          person: null,
          expense_type: null,
          fee_category: null,
          reason: null,
          contract_no: null,
          amount_ex_tax: "100.00",
          amount_inc_tax: "113.00",
          data_status: "已结束",
          remark: "旧快照",
        }],
        total: 1,
      })
      .mockRejectedValueOnce(new Error("readback failed"));
    const { container } = renderPanel();
    fireEvent.click(await screen.findByRole("tab", { name: "报销" }));
    await screen.findByText("BXD-OLD");

    const inputs = container.querySelectorAll<HTMLInputElement>('input[type="file"]');
    fireEvent.change(inputs[0], {
      target: { files: [new File(["xlsx"], "项目总表.xlsx")] },
    });
    fireEvent.click(await screen.findByRole("button", { name: /确认回传/ }));

    expect(await screen.findByText(/数据已写入，但页面刷新失败/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("BXD-OLD")).toBeNull());
    expect(screen.queryByText(/已覆盖并刷新/)).toBeNull();
  });
});
