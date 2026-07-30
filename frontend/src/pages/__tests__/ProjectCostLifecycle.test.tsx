import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { message } from "antd";

const get = vi.fn();
const post = vi.fn();
const getSystemSettings = vi.fn();

vi.mock("../../api", () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}));

vi.mock("../../api/systemSettings", () => ({
  getSystemSettings: (...args: unknown[]) => getSystemSettings(...args),
}));

import ProjectCostPage, {
  buildOrderExportParams,
  formatMaintenanceRecomputeSummary,
  formatRoundtripImportSummary,
} from "../ProjectCostPage";
import { TaxBasisProvider } from "../../context/TaxBasis";

type Lifecycle = "ongoing" | "ended" | "missing" | "all";

const counts = { ongoing: 2, ended: 4, missing: 1 };
const nullable = <T,>(value: T): T | null => value;
const optional = <T,>(value: T): T | undefined => value;

function projects(project = "进行中项目", lifecycle: Lifecycle = "ongoing", asOf = "2026-07-16") {
  return {
    data: {
      rows: [{
        project,
        lines: 1,
        order_count: 1,
        missing_detail_orders: 0,
        structure_complete: true,
        qty: 2,
        cost_inc: nullable(100),
        cost_ex: nullable(88.5),
        cost_total: nullable(188.5),
        actual_cost_inc: nullable(100),
        actual_cost_ex: nullable(88.5),
        estimated_cost_inc: nullable(0),
        estimated_cost_ex: nullable(0),
        actual_lines: nullable(1),
        estimated_lines: nullable(0),
        missing_cost_lines: nullable(0),
        known_cost_total: nullable(188.5),
        cost_quality: nullable<string | undefined>("actual_only"),
        coverage_pct: nullable(100),
        by_source: nullable<Record<string, number>>({ direct: 1 }),
        months: 1,
        sales_orders: ["XSDD-1"],
        contract_amount: nullable(1000),
        contract_shared: false,
        contract_incomplete: false,
        maint_end: lifecycle === "missing" ? null : lifecycle === "ended" ? "2026-07-15" : "2026-07-16",
        lifecycle_status: lifecycle === "all" ? "ongoing" : lifecycle,
      }],
      start_date: "2026-01-01",
      as_of: asOf,
      lifecycle_filter: lifecycle,
      lifecycle_counts: counts,
      ranking_restricted: false,
    },
  };
}

function board(contract = "XSDD-1", lifecycle: Lifecycle = "ongoing", asOf = "2026-07-16") {
  return {
    data: {
      rows: [{
        contract,
        decision_status: optional<string | null>("green"),
        status: optional<string | null>("green"),
        projects: [{
          project: `${contract}项目`, lines: 1, spent_parts: 100,
          actual_cost_inc: 100, actual_cost_ex: 0,
          estimated_cost_inc: 0, estimated_cost_ex: 0,
          actual_lines: 1, estimated_lines: 0, missing_cost_lines: 0,
          known_cost_total: 100, cost_quality: "actual_only",
        }],
        lines: 1,
        actual_cost_inc: nullable(100),
        actual_cost_ex: nullable(0),
        estimated_cost_inc: nullable(0),
        estimated_cost_ex: nullable(0),
        actual_lines: nullable(1),
        estimated_lines: nullable(0),
        missing_cost_lines: nullable(0),
        known_cost_total: nullable(100),
        cost_quality: nullable<string | undefined>("actual_only"),
        coverage_pct: nullable(100),
        spent_parts: nullable(100),
        spent_expense: nullable(0),
        spent: nullable(100),
        expense_data_available: optional(true),
        expense_inc: nullable(0),
        expense_ex: nullable(0),
        budget: nullable(1000),
        remaining: nullable(900),
        remaining_pct: nullable(90),
        low_conf_pct: nullable(0),
        revenue_inc: nullable(1060),
        revenue_ex: nullable(1000),
        parts_cost_inc_tax: nullable(226),
        parts_cost_ex_tax: nullable(200),
        parts_gross_profit_inc: nullable(834),
        parts_gross_profit_ex: nullable(800),
        parts_gross_margin_inc: nullable(0.7868),
        parts_gross_margin_ex: nullable(0.8),
        parts_profit_status_inc: nullable("complete_estimated"),
        parts_profit_status_ex: nullable("complete_actual"),
        contribution_profit_inc: nullable(null),
        contribution_profit_ex: nullable(null),
        contribution_margin_inc: nullable(null),
        contribution_margin_ex: nullable(null),
        contribution_status_inc: nullable("expense_tax_unknown"),
        contribution_status_ex: nullable("expense_tax_unknown"),
        maint_start: "2026-01-01",
        maint_end: lifecycle === "missing" ? null : lifecycle === "ended" ? "2026-07-15" : "2026-07-16",
        lifecycle_status: lifecycle === "all" ? "ongoing" : lifecycle,
        first_out: "2026-01-01",
        last_out: "2026-07-01",
      }],
      profit_restricted: false,
      as_of: asOf,
      lifecycle_filter: lifecycle,
      lifecycle_counts: counts,
    },
  };
}

function installSuccessResponses() {
  get.mockImplementation((
    path: string,
    config?: { params?: { lifecycle?: Lifecycle; status?: string } },
  ) => {
    const lifecycle = config?.params?.lifecycle ?? "ongoing";
    const label = lifecycle === "ended" ? "已结束" : lifecycle === "missing" ? "期限缺失" : "进行中";
    if (path === "/maintenance/projects") return Promise.resolve(projects(`${label}项目`, lifecycle));
    if (path === "/maintenance/board") {
      const response = board(undefined, lifecycle);
      return Promise.resolve({
        data: {
          ...response.data,
          status_filter_applied: config?.params?.status ? true : undefined,
        },
      });
    }
    if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-16" } });
    if ([
      "/maintenance/export",
      "/maintenance/board/export",
      "/maintenance/lines/export",
    ].includes(path)) return Promise.resolve(csvDownload());
    if ([
      "/maintenance/orders/export",
      "/maintenance/export-workbook",
      "/maintenance/roundtrip-template",
    ].includes(path)) return Promise.resolve(xlsxDownload());
    if ([
      "/maintenance/export-workbooks",
      "/maintenance/roundtrip-templates",
    ].includes(path)) return Promise.resolve(zipDownload());
    return Promise.resolve({ data: { rows: [], total: 0 } });
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function csvDownload(body = "csv") {
  return {
    data: new Blob([body], { type: "text/csv" }),
    headers: { "content-type": "text/csv" },
  };
}

const VALID_XLSX_BASE64 =
  "UEsDBBQAAAAIACIm/lzHHBc8CgAAAAgAAAATAAAAW0NvbnRlbnRfVHlwZXNdLnhtbLMJqSxILda3AwBQSwMEFAAAAAgAIib+XM6emBMNAAAACwAAAA8AAAB4bC93b3JrYm9vay54bWyzKc8vyk7Kz8/WtwMAUEsBAhQDFAAAAAgAIib+XMccFzwKAAAACAAAABMAAAAAAAAAAAAAAIABAAAAAFtDb250ZW50X1R5cGVzXS54bWxQSwECFAMUAAAACAAiJv5czp6YEw0AAAALAAAADwAAAAAAAAAAAAAAgAE7AAAAeGwvd29ya2Jvb2sueG1sUEsFBgAAAAACAAIAfgAAAHUAAAAAAA==";
const VALID_ZIP_BASE64 =
  "UEsDBBQAAAAIACIm/lxH3dx5BAAAAAIAAAAIAAAAZmlsZS50eHTLzwYAUEsBAhQDFAAAAAgAIib+XEfd3HkEAAAAAgAAAAgAAAAAAAAAAAAAAIABAAAAAGZpbGUudHh0UEsFBgAAAAABAAEANgAAACoAAAAAAA==";
const EMPTY_ZIP_BASE64 = "UEsFBgAAAAAAAAAAAAAAAAAAAAAAAA==";

function archiveBytes(base64: string) {
  return Uint8Array.from(atob(base64), (character) => character.charCodeAt(0));
}

function archiveBlob(base64: string, type: string) {
  return new Blob([archiveBytes(base64)], { type });
}

function storedZipBlob(
  entryName: string,
  payload: Uint8Array,
  type = "application/zip",
) {
  const name = new TextEncoder().encode(entryName);
  let crc = 0xffffffff;
  for (const byte of payload) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  crc = (crc ^ 0xffffffff) >>> 0;

  const local = new Uint8Array(30 + name.length);
  const localView = new DataView(local.buffer);
  localView.setUint32(0, 0x04034b50, true);
  localView.setUint16(4, 20, true);
  localView.setUint16(6, 0x0800, true);
  localView.setUint16(8, 0, true);
  localView.setUint32(14, crc, true);
  localView.setUint32(18, payload.length, true);
  localView.setUint32(22, payload.length, true);
  localView.setUint16(26, name.length, true);
  local.set(name, 30);

  const centralOffset = local.length + payload.length;
  const central = new Uint8Array(46 + name.length);
  const centralView = new DataView(central.buffer);
  centralView.setUint32(0, 0x02014b50, true);
  centralView.setUint16(4, 20, true);
  centralView.setUint16(6, 20, true);
  centralView.setUint16(8, 0x0800, true);
  centralView.setUint16(10, 0, true);
  centralView.setUint32(16, crc, true);
  centralView.setUint32(20, payload.length, true);
  centralView.setUint32(24, payload.length, true);
  centralView.setUint16(28, name.length, true);
  central.set(name, 46);

  const eocd = new Uint8Array(22);
  const eocdView = new DataView(eocd.buffer);
  eocdView.setUint32(0, 0x06054b50, true);
  eocdView.setUint16(8, 1, true);
  eocdView.setUint16(10, 1, true);
  eocdView.setUint32(12, central.length, true);
  eocdView.setUint32(16, centralOffset, true);
  return new Blob([local, new Uint8Array(payload), central, eocd], { type });
}

function xlsxDownload() {
  return {
    data: archiveBlob(
      VALID_XLSX_BASE64,
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    headers: {
      "content-type":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    },
  };
}

function zipDownload() {
  return {
    data: archiveBlob(VALID_ZIP_BASE64, "application/zip"),
    headers: { "content-type": "application/zip" },
  };
}

async function waitForDownloadsReady() {
  await waitFor(() => {
    expect(screen.getByRole("radio", { name: "今天" })).toBeEnabled();
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  getSystemSettings.mockResolvedValue({
    data: {
      purchase_display_basis: "both",
      sales_display_basis: "ex",
      maintenance_display_basis: "both",
      version: 1,
      updated_by: null,
      updated_at: null,
    },
  });
  localStorage.clear();
  localStorage.setItem("role", "readonly");
  localStorage.setItem("permissions", JSON.stringify({
    page_maintenance: true,
    data_customer: true,
    data_purchase_cost: true,
    data_profit: true,
    action_maintenance_roundtrip_apply: true,
    own_customers_only: false,
  }));
  message.destroy();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:test") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  vi.spyOn(window, "setTimeout");
});

afterEach(() => {
  cleanup();
  message.destroy();
  window.history.replaceState(null, "", "/");
  vi.restoreAllMocks();
});

describe("维保项目生命周期筛选", () => {
  it("项目数据首屏直接展示详细盈亏且不嵌下载入口", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);

    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/projects",
      expect.anything(),
    ));
    expect(screen.getByRole("heading", { name: "项目数据" })).toBeInTheDocument();
    expect(screen.getByText("详细盈亏")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "导出订单汇总 Excel" })).toBeNull();
    expect(screen.queryByRole("button", { name: "下载固定回填模板" })).toBeNull();
    expect(screen.queryByText("项目提醒")).toBeNull();
    expect(screen.queryByRole("radiogroup", { name: "预算消耗参考状态筛选" }))
      .toBeNull();
    expect(screen.queryByText(/当前有 \d+ 个期限缺失项目/)).toBeNull();
    const cardTitles = Array.from(
      document.querySelectorAll(".ant-card-head-title"),
      (node) => node.textContent,
    );
    expect(cardTitles[0]).toContain("详细盈亏");
    expect(cardTitles).not.toContain("数据筛选");
  });

  it("项目成本事实显式展示订单结构，存在无明细订单时不误报只有零行缺成本", async () => {
    const response = projects("结构不完整项目");
    response.data.rows[0] = {
      ...response.data.rows[0],
      order_count: 3,
      missing_detail_orders: 2,
      structure_complete: false,
      missing_cost_lines: 0,
      cost_quality: "incomplete",
    };
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") return Promise.resolve(response);
      if (path === "/maintenance/board") return Promise.resolve(board());
      return Promise.reject(new Error(`unexpected ${path}`));
    });

    render(<ProjectCostPage />);

    expect(await screen.findByText("结构不完整项目")).toBeInTheDocument();
    expect(screen.getAllByText("维保订单数").length).toBeGreaterThan(0);
    expect(screen.getAllByText("结构完整性").length).toBeGreaterThan(0);
    expect(screen.getByText("不完整 · 无明细 2 单")).toBeInTheDocument();
    expect(screen.getByText("需补数据 · 无明细 2 单 · 缺成本 0 行"))
      .toBeInTheDocument();
    expect(screen.queryByText("需补数据 · 0 行")).toBeNull();
  });

  it("详细盈亏合同卡分页，首屏不一次展开全部合同", async () => {
    const manyContracts = board();
    manyContracts.data.rows = Array.from({ length: 25 }, (_, index) => ({
      ...manyContracts.data.rows[0],
      contract: `XSDD-PAGE-${index + 1}`,
      projects: [{
        ...manyContracts.data.rows[0].projects[0],
        project: `分页项目-${index + 1}`,
      }],
    }));
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(manyContracts);
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    expect(await screen.findByTestId("maintenance-board-card-XSDD-PAGE-1"))
      .toBeInTheDocument();
    expect(screen.getAllByTestId(/maintenance-board-card-/)).toHaveLength(12);
    expect(screen.queryByTestId("maintenance-board-card-XSDD-PAGE-13")).toBeNull();

    fireEvent.click(screen.getByTitle("2"));

    expect(await screen.findByTestId("maintenance-board-card-XSDD-PAGE-13"))
      .toBeInTheDocument();
    expect(screen.queryByTestId("maintenance-board-card-XSDD-PAGE-1")).toBeNull();
    expect(screen.getAllByTestId(/maintenance-board-card-/)).toHaveLength(12);
  });

  it("下载中心独立集中六类下载、日期和单本导入", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);

    await waitForDownloadsReady();
    expect(screen.getByRole("heading", { name: "下载中心" })).toBeInTheDocument();
    expect(screen.getByLabelText("维保订单导出日期")).toBeInTheDocument();
    for (const name of [
      "导出项目成本 CSV",
      "导出合同详细盈亏 CSV",
      "导出订单汇总 Excel",
      "批量导出项目工作簿 ZIP",
      "导出单项目明细 CSV",
      "导出单合同工作簿 XLSX",
      "下载固定回填模板",
    ]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
    expect(screen.getByText(/项目成本 CSV 按项目汇总.*合同详细盈亏 CSV 按合同汇总/))
      .toBeInTheDocument();
    expect(screen.getByText("导入更新工作簿")).toBeInTheDocument();
    expect(screen.queryByText("详细盈亏")).toBeNull();
  });

  it("下载中心项目搜索、项目名称和合同编号遵守后端长度上限", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    expect(screen.getByLabelText("项目成本 CSV 项目搜索"))
      .toHaveAttribute("maxlength", "128");
    expect(screen.getByLabelText("单项目名称"))
      .toHaveAttribute("maxlength", "256");
    expect(screen.getByLabelText("单合同编号"))
      .toHaveAttribute("maxlength", "64");
  });

  it("下载中心全部 GET 文件请求都绑定点击时会话和可取消信号", async () => {
    localStorage.setItem("token", "admin-token");
    get.mockImplementation((path: string) => {
      if ([
        "/maintenance/export",
        "/maintenance/board/export",
        "/maintenance/lines/export",
      ].includes(path)) return Promise.resolve(csvDownload());
      if ([
        "/maintenance/orders/export",
        "/maintenance/export-workbook",
        "/maintenance/roundtrip-template",
      ].includes(path)) return Promise.resolve(xlsxDownload());
      if ([
        "/maintenance/export-workbooks",
        "/maintenance/roundtrip-templates",
      ].includes(path)) return Promise.resolve(zipDownload());
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.change(screen.getByLabelText("单项目名称"), {
      target: { value: "测试项目" },
    });
    fireEvent.change(screen.getByLabelText("单合同编号"), {
      target: { value: "XSDD-1" },
    });

    for (const name of [
      "导出项目成本 CSV",
      "导出合同详细盈亏 CSV",
      "导出订单汇总 Excel",
      "批量导出项目工作簿 ZIP",
      "导出单项目明细 CSV",
      "导出单合同工作簿 XLSX",
      "下载固定回填模板",
      "批量下载可回填工作簿 ZIP",
    ]) {
      fireEvent.click(screen.getByRole("button", { name }));
    }

    const paths = [
      "/maintenance/export",
      "/maintenance/board/export",
      "/maintenance/orders/export",
      "/maintenance/export-workbooks",
      "/maintenance/lines/export",
      "/maintenance/export-workbook",
      "/maintenance/roundtrip-template",
      "/maintenance/roundtrip-templates",
    ];
    await waitFor(() => {
      for (const path of paths) {
        const call = get.mock.calls.find(([calledPath]) => calledPath === path);
        expect(call?.[1]?.signal).toBeInstanceOf(AbortSignal);
        expect(call?.[1]?.headers).toEqual({
          Authorization: "Bearer admin-token",
        });
      }
    });
    await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(paths.length));
  });

  it("受限销售下载中心不展示后端必定 403 的订单和成本工作簿入口", async () => {
    localStorage.setItem("role", "sales");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: true,
      data_profit: true,
      own_customers_only: true,
    }));
    installSuccessResponses();

    render(<ProjectCostPage view="downloads" />);

    await waitForDownloadsReady();
    expect(screen.getByRole("button", { name: "导出项目成本 CSV" }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导出单项目明细 CSV" }))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "导出合同详细盈亏 CSV" })).toBeNull();
    expect(screen.queryByRole("button", { name: "导出订单汇总 Excel" })).toBeNull();
    expect(screen.queryByRole("button", { name: "批量导出项目工作簿 ZIP" })).toBeNull();
    expect(screen.queryByRole("button", { name: "导出单合同工作簿 XLSX" })).toBeNull();
    expect(screen.queryByRole("button", { name: "下载固定回填模板" })).toBeNull();
    expect(screen.queryByRole("button", { name: "批量下载可回填工作簿 ZIP" })).toBeNull();
  });

  it("受限销售项目数据只加载本人项目事实且不请求合同级详细盈亏", async () => {
    localStorage.setItem("role", "sales");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: true,
      data_profit: true,
      own_customers_only: true,
    }));
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") {
        return Promise.resolve(projects("销售A本人项目"));
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });

    render(<ProjectCostPage />);

    expect(await screen.findByText("销售A本人项目")).toBeInTheDocument();
    expect(screen.getByText("受限销售账号不提供合同级详细盈亏"))
      .toBeInTheDocument();
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/board"))
      .toHaveLength(0);
  });

  it("受限销售项目提醒不请求合同对象并禁用经营提醒筛选", async () => {
    localStorage.setItem("role", "sales");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: true,
      data_profit: true,
      own_customers_only: true,
    }));

    render(<ProjectCostPage view="reminders" />);

    expect(await screen.findAllByText("受限销售账号不提供合同级经营提醒"))
      .toHaveLength(2);
    expect(screen.getByRole("radio", { name: "全部提醒" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "红色预警" })).toBeDisabled();
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/board"))
      .toHaveLength(0);
  });

  it("项目提醒独立承载期限与异常，不重复展示下载中心", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="reminders" />);

    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/board",
      expect.anything(),
    ));
    expect(screen.getByRole("heading", { name: "项目提醒" })).toBeInTheDocument();
    expect(screen.getByText("维保期限")).toBeInTheDocument();
    expect(screen.queryByText("下载中心")).toBeNull();
    expect(screen.queryByRole("button", { name: "导出订单汇总 Excel" })).toBeNull();
    expect(get.mock.calls.some(([path]) => path === "/maintenance/projects")).toBe(false);
  });

  it("缺成本提醒仍在紧凑卡片中展示项目、期限和获准成本费用事实", async () => {
    get.mockImplementation((path: string) => {
      if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
      const response = board("XS-REMINDER-MISSING");
      response.data.rows[0] = {
        ...response.data.rows[0],
        decision_status: "incomplete_cost",
        status: "incomplete_cost",
        projects: [{
          ...response.data.rows[0].projects[0],
          project: "机房 UPS 维保项目",
          spent_parts: 140,
        }],
        actual_cost_inc: 100,
        actual_cost_ex: 88.5,
        estimated_cost_inc: 40,
        estimated_cost_ex: 35.4,
        actual_lines: 1,
        estimated_lines: 1,
        missing_cost_lines: 2,
        known_cost_total: 263.9,
        cost_quality: "incomplete",
        spent_parts: 263.9,
        spent_expense: 20,
        spent: null,
        expense_data_available: true,
        expense_inc: 20,
        expense_ex: 18,
        remaining: null,
        remaining_pct: null,
        maint_end: "2026-12-31",
      };
      return Promise.resolve(response);
    });

    render(<ProjectCostPage view="reminders" />);

    const grid = await screen.findByRole("list", { name: "项目提醒卡片" });
    const card = within(grid).getByRole("listitem");
    expect(card).toHaveTextContent("XS-REMINDER-MISSING");
    expect(card).toHaveTextContent("项目：机房 UPS 维保项目");
    expect(card).toHaveTextContent("进行中");
    expect(card).toHaveTextContent("2026-12-31");
    expect(card).toHaveTextContent("实际参考：含 ¥100 · 不含 ¥88.5");
    expect(card).toHaveTextContent("估算参考：含 ¥40 · 不含 ¥35.4");
    expect(card).toHaveTextContent("缺失成本行：2 行");
    expect(card).toHaveTextContent("成本：缺 2 行");
    expect(card).toHaveTextContent("费用：已就绪");
    expect(card).toHaveTextContent("含 ¥20 · 不含 ¥18");
    expect(card).toHaveTextContent("费用水位：暂不计算（成本证据不完整）");
    expect(within(card).queryByRole("alert")).toBeNull();
    expect(card).not.toHaveTextContent(
      /毛利|剩余预算|预算余量 > 20%|红色预警|绿色参考/,
    );
  });

  it("证据完整提醒展示后端给出的预算费用水位和剩余预算", async () => {
    installSuccessResponses();

    render(<ProjectCostPage view="reminders" />);

    const grid = await screen.findByRole("list", { name: "项目提醒卡片" });
    const card = within(grid).getByRole("listitem");
    expect(card).toHaveTextContent("成本：完整");
    expect(card).toHaveTextContent("缺失成本行：0 行");
    expect(card).toHaveTextContent("费用：已就绪");
    expect(card).toHaveTextContent(
      "费用水位：合同额参考 ¥1,000 · 已知支出 ¥100 · 剩余预算 ¥900（90%）",
    );
    expect(card).toHaveTextContent("预算余量 > 20%");
    expect(within(card).getByRole("progressbar")).toBeInTheDocument();
  });

  it("费用未就绪提醒保留成本事实但费用水位不把无记录伪装为零", async () => {
    get.mockImplementation((path: string) => {
      if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
      const response = board("XS-REMINDER-NO-EXPENSE");
      response.data.rows[0] = {
        ...response.data.rows[0],
        decision_status: "expense_data_unavailable",
        status: "expense_data_unavailable",
        expense_data_available: false,
        spent_expense: 0,
        expense_inc: 0,
        expense_ex: 0,
        spent: 100,
        remaining: 900,
        remaining_pct: 90,
      };
      return Promise.resolve(response);
    });

    render(<ProjectCostPage view="reminders" />);

    const grid = await screen.findByRole("list", { name: "项目提醒卡片" });
    const card = within(grid).getByRole("listitem");
    expect(card).toHaveTextContent("实际参考：含 ¥100 · 不含 ¥0");
    expect(card).toHaveTextContent("费用：未就绪（无记录不等于 0）");
    expect(card).toHaveTextContent("费用水位：暂不计算（费用未就绪）");
    expect(card).not.toHaveTextContent("费用：已就绪");
    expect(card).not.toHaveTextContent(/剩余预算|¥900/);
    expect(within(card).queryByRole("progressbar")).toBeNull();
  });

  it("费用未就绪却夹带 green 时降级为费用未就绪且不显示正式绿色判断", async () => {
    get.mockImplementation((path: string) => {
      if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
      const response = board("XS-REMINDER-DIRTY-GREEN");
      response.data.rows[0] = {
        ...response.data.rows[0],
        decision_status: "green",
        status: "green",
        expense_data_available: false,
      };
      return Promise.resolve(response);
    });

    render(<ProjectCostPage view="reminders" />);

    const card = within(
      await screen.findByRole("list", { name: "项目提醒卡片" }),
    ).getByRole("listitem");
    expect(card).toHaveTextContent("费用：未就绪（无记录不等于 0）");
    expect(card).toHaveTextContent("费用水位：暂不计算（费用未就绪）");
    expect(card).toHaveTextContent("费用数据未就绪");
    expect(card).not.toHaveTextContent("预算余量 > 20%");
    expect(within(card).queryByRole("progressbar")).toBeNull();
  });

  it("无预算提醒不把缺少合同额伪装成零余额", async () => {
    get.mockImplementation((path: string) => {
      if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
      const response = board("XS-REMINDER-NO-BUDGET");
      response.data.rows[0] = {
        ...response.data.rows[0],
        decision_status: "no_budget",
        status: "no_budget",
        budget: null,
        spent: 100,
        remaining: 0,
        remaining_pct: 0,
      };
      return Promise.resolve(response);
    });

    render(<ProjectCostPage view="reminders" />);

    const grid = await screen.findByRole("list", { name: "项目提醒卡片" });
    const card = within(grid).getByRole("listitem");
    expect(card).toHaveTextContent("费用水位：无预算（未关联合同额）");
    expect(card).not.toHaveTextContent(/剩余预算|余额 ¥0/);
    expect(within(card).queryByRole("progressbar")).toBeNull();
  });

  it.each([
    ["red", null],
    ["yellow", null],
    ["green", null],
    ["red", 0],
    ["yellow", 0],
    ["green", 0],
  ])(
    "夹带%s经营状态且预算为%s时统一降级无预算",
    async (dirtyStatus, budget) => {
      get.mockImplementation((path: string) => {
        if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
        const response = board(`XS-REMINDER-NO-BUDGET-${dirtyStatus}-${budget}`);
        response.data.rows[0] = {
          ...response.data.rows[0],
          decision_status: dirtyStatus,
          status: dirtyStatus,
          budget,
          spent: 100,
          remaining: 900,
          remaining_pct: 90,
        };
        return Promise.resolve(response);
      });

      render(<ProjectCostPage view="reminders" />);

      const card = within(
        await screen.findByRole("list", { name: "项目提醒卡片" }),
      ).getByRole("listitem");
      expect(card).toHaveTextContent("费用水位：无预算（未关联合同额）");
      expect(card).toHaveTextContent("无预算(未关联合同额)");
      expect(card).not.toHaveTextContent(
        /预算已用完或超预算|预算余量 ≤ 20%|预算余量 > 20%|剩余预算/,
      );
      expect(within(card).queryByRole("progressbar")).toBeNull();
    },
  );

  it.each([
    ["red", "spent"],
    ["red", "remaining"],
    ["red", "remaining_pct"],
    ["yellow", "spent"],
    ["yellow", "remaining"],
    ["yellow", "remaining_pct"],
    ["green", "spent"],
    ["green", "remaining"],
    ["green", "remaining_pct"],
  ] as const)(
    "夹带%s经营状态但%s无效时保持中性待核验",
    async (dirtyStatus, invalidField) => {
      get.mockImplementation((path: string) => {
        if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
        const response = board(`XS-REMINDER-INVALID-${dirtyStatus}-${invalidField}`);
        response.data.rows[0] = {
          ...response.data.rows[0],
          decision_status: dirtyStatus,
          status: dirtyStatus,
          [invalidField]: null,
        };
        return Promise.resolve(response);
      });

      render(<ProjectCostPage view="reminders" />);

      const card = within(
        await screen.findByRole("list", { name: "项目提醒卡片" }),
      ).getByRole("listitem");
      expect(card).toHaveTextContent("经营判断待核验");
      expect(card).not.toHaveTextContent(
        /预算已用完或超预算|预算余量 ≤ 20%|预算余量 > 20%|费用水位：合同额参考/,
      );
      expect(within(card).queryByRole("progressbar")).toBeNull();
    },
  );

  it("旧提醒响应缺少新证据字段时保持待核验且不沿用旧 green", async () => {
    get.mockImplementation((path: string) => {
      if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
      const response = board("XS-REMINDER-LEGACY");
      response.data.rows[0] = {
        ...response.data.rows[0],
        decision_status: undefined,
        status: "green",
        cost_quality: undefined,
        spent: 100,
        remaining: 900,
        remaining_pct: 90,
      };
      return Promise.resolve(response);
    });

    render(<ProjectCostPage view="reminders" />);

    const card = within(
      await screen.findByRole("list", { name: "项目提醒卡片" }),
    ).getByRole("listitem");
    expect(card).toHaveTextContent("成本：待核验");
    expect(card).toHaveTextContent("缺失成本行：0 行");
    expect(card).toHaveTextContent("费用水位：暂不计算（成本证据不完整）");
    expect(card).not.toHaveTextContent(/成本：完整|预算余量 > 20%|剩余预算/);
    expect(within(card).queryByRole("progressbar")).toBeNull();
  });

  it("成本质量未知时即使夹带 green 和余额也不展示正式经营判断", async () => {
    get.mockImplementation((path: string) => {
      if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
      const response = board("XS-REMINDER-QUALITY-UNKNOWN");
      response.data.rows[0] = {
        ...response.data.rows[0],
        decision_status: "green",
        status: "green",
        cost_quality: null,
        missing_cost_lines: null,
      };
      return Promise.resolve(response);
    });

    render(<ProjectCostPage view="reminders" />);

    const card = within(
      await screen.findByRole("list", { name: "项目提醒卡片" }),
    ).getByRole("listitem");
    expect(card).toHaveTextContent("成本：待核验");
    expect(card).toHaveTextContent("缺失成本行：待核验");
    expect(card).toHaveTextContent("费用水位：暂不计算（成本证据不完整）");
    expect(card).not.toHaveTextContent(/预算余量 > 20%|剩余预算|绿色参考/);
    expect(within(card).queryByRole("progressbar")).toBeNull();
  });

  it("无成本权限提醒只展示获准事实且不泄漏成本数量或回填入口", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_customer: true,
      data_purchase_cost: false,
      data_profit: true,
      action_maintenance_roundtrip_apply: true,
      own_customers_only: false,
    }));
    get.mockImplementation((path: string) => {
      if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
      const response = board("XS-REMINDER-COST-MASKED");
      response.data.rows[0] = {
        ...response.data.rows[0],
        decision_status: undefined,
        status: undefined,
        actual_cost_inc: null,
        actual_cost_ex: null,
        estimated_cost_inc: null,
        estimated_cost_ex: null,
        actual_lines: null,
        estimated_lines: null,
        missing_cost_lines: null,
        known_cost_total: null,
        cost_quality: null,
        spent_parts: null,
        spent: null,
        budget: null,
        remaining: null,
        remaining_pct: null,
      };
      return Promise.resolve({
        data: { ...response.data, decision_restricted: true, ranking_restricted: true },
      });
    });

    render(<ProjectCostPage view="reminders" />);

    const grid = await screen.findByRole("list", { name: "项目提醒卡片" });
    const card = within(grid).getByRole("listitem");
    expect(card).toHaveTextContent("XS-REMINDER-COST-MASKED项目");
    expect(card).toHaveTextContent("成本事实：无权限");
    expect(card).toHaveTextContent("成本：无权限");
    expect(card).toHaveTextContent("经营判断受限");
    expect(card).not.toHaveTextContent("费用水位");
    expect(card).not.toHaveTextContent(/实际参考：|估算参考：|成本：缺|预算余量|剩余预算/);
    expect(within(card).queryByRole("progressbar")).toBeNull();
    expect(within(card).queryByRole("link", { name: "去人工回填" })).toBeNull();
  });

  it("无成本权限时即使旧响应未带限制标志也不接受经营判断或成本事实", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_customer: true,
      data_purchase_cost: false,
      data_profit: true,
      action_maintenance_roundtrip_apply: true,
      own_customers_only: false,
    }));
    get.mockImplementation((path: string) => {
      if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
      return Promise.resolve(board("XS-REMINDER-STALE-RBAC"));
    });

    render(<ProjectCostPage view="reminders" />);

    const card = within(
      await screen.findByRole("list", { name: "项目提醒卡片" }),
    ).getByRole("listitem");
    expect(card).toHaveTextContent("成本事实：无权限");
    expect(card).toHaveTextContent("经营判断受限");
    expect(card).not.toHaveTextContent("费用水位");
    expect(card).not.toHaveTextContent(
      /实际参考：|估算参考：|缺失成本行：|预算余量 > 20%|剩余预算/,
    );
    expect(within(card).queryByRole("progressbar")).toBeNull();
  });

  it.each([false, true])(
    "无成本权限时旧响应夹带成本不完整且限制标志为%s仍只显示中性受限结论",
    async (restrictedFlag) => {
      localStorage.setItem("permissions", JSON.stringify({
        page_maintenance: true,
        data_customer: true,
        data_purchase_cost: false,
        data_profit: true,
        action_maintenance_roundtrip_apply: true,
        own_customers_only: false,
      }));
      get.mockImplementation((path: string) => {
        if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
        const response = board("XS-REMINDER-STALE-INCOMPLETE");
        response.data.rows[0] = {
          ...response.data.rows[0],
          decision_status: "incomplete_cost",
          status: "incomplete_cost",
          cost_quality: "incomplete",
          missing_cost_lines: 259,
        };
        return Promise.resolve({
          data: {
            ...response.data,
            decision_restricted: restrictedFlag,
            profit_restricted: restrictedFlag,
            ranking_restricted: restrictedFlag,
          },
        });
      });

      render(<ProjectCostPage view="reminders" />);

      const card = within(
        await screen.findByRole("list", { name: "项目提醒卡片" }),
      ).getByRole("listitem");
      expect(card).toHaveTextContent("成本事实：无权限");
      expect(card).toHaveTextContent("经营判断受限");
      expect(card).not.toHaveTextContent(
        /成本不完整|需补数据|缺 259|缺失成本行|费用水位|剩余预算/,
      );
      expect(within(card).queryByRole("progressbar")).toBeNull();
      expect(within(card).queryByRole("link", { name: "去人工回填" })).toBeNull();
    },
  );

  it("无客户信息权限时提醒不泄漏合同编号或合同预填入口", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_customer: false,
      data_purchase_cost: true,
      data_profit: true,
      action_maintenance_roundtrip_apply: true,
      own_customers_only: false,
    }));
    get.mockImplementation((path: string) => {
      if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
      const response = board("XS-SECRET-CONTRACT");
      response.data.rows[0] = {
        ...response.data.rows[0],
        projects: [{
          ...response.data.rows[0].projects[0],
          project: "获准展示的维保项目",
        }],
      };
      return Promise.resolve(response);
    });

    render(<ProjectCostPage view="reminders" />);

    const card = within(
      await screen.findByRole("list", { name: "项目提醒卡片" }),
    ).getByRole("listitem");
    expect(card).toHaveTextContent("合同信息受限");
    expect(card).toHaveTextContent("获准展示的维保项目");
    expect(card).not.toHaveTextContent("XS-SECRET-CONTRACT");
    expect(within(card).queryByRole("link", { name: "去人工回填" })).toBeNull();
  });

  it.each([
    ["明确关闭", false],
    ["权限键缺失", undefined],
  ])(
    "回填动作权限%s时即使可下载模板也隐藏提醒页动作入口",
    async (_permissionCase, actionPermission) => {
      const permissions: Record<string, boolean> = {
        page_maintenance: true,
        data_customer: true,
        data_purchase_cost: true,
        data_profit: true,
        own_customers_only: false,
      };
      if (actionPermission !== undefined) {
        permissions.action_maintenance_roundtrip_apply = actionPermission;
      }
      localStorage.setItem("permissions", JSON.stringify(permissions));
      installSuccessResponses();

      render(<ProjectCostPage view="reminders" />);

      const card = within(
        await screen.findByRole("list", { name: "项目提醒卡片" }),
      ).getByRole("listitem");
      expect(within(card).queryByRole("link", { name: "去人工回填" })).toBeNull();
    },
  );

  it("获准用户可从提醒携合同上下文去回填并返回提醒", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="reminders" />);

    const card = within(
      await screen.findByRole("list", { name: "项目提醒卡片" }),
    ).getByRole("listitem");
    const refillLink = within(card).getByRole("link", { name: "去人工回填" });
    expect(refillLink).toHaveAttribute(
      "href",
      "/maintenance/downloads?from=reminders&contract=XSDD-1",
    );

    cleanup();
    window.history.replaceState(null, "", refillLink.getAttribute("href")!);
    render(<ProjectCostPage view="downloads" />);

    expect(screen.getByRole("textbox", { name: "单合同编号" })).toHaveValue("XSDD-1");
    expect(screen.getByRole("link", { name: "返回项目提醒" }))
      .toHaveAttribute("href", "/maintenance/reminders");
  });

  it.each([
    ["超长合同", `?from=reminders&contract=${"X".repeat(65)}`, true],
    ["重复合同", "?from=reminders&contract=XS-1&contract=XS-2", true],
    ["控制字符", "?from=reminders&contract=XS%0A-1", true],
    ["残缺编码", "?from=reminders&contract=%E0%A4%A", true],
    ["重复来源", "?from=reminders&from=reminders&contract=XS-1", false],
    ["未知来源", "?from=other&contract=XS-1", false],
  ])("下载页对%s query fail closed", (_caseName, search, showReturn) => {
    window.history.replaceState(null, "", `/maintenance/downloads${search}`);
    render(<ProjectCostPage view="downloads" />);

    expect(screen.getByRole("textbox", { name: "单合同编号" })).toHaveValue("");
    if (showReturn) {
      expect(screen.getByRole("link", { name: "返回项目提醒" })).toBeInTheDocument();
    } else {
      expect(screen.queryByRole("link", { name: "返回项目提醒" })).toBeNull();
    }
  });

  it("下载页接受合法中文与斜杠合同号并只预填不自动提交", () => {
    const contract = "XSDD/中文\\维保-1";
    const params = new URLSearchParams({ from: "reminders", contract });
    window.history.replaceState(
      null,
      "",
      `/maintenance/downloads?${params.toString()}`,
    );

    render(<ProjectCostPage view="downloads" />);

    expect(screen.getByRole("textbox", { name: "单合同编号" })).toHaveValue(contract);
    expect(get).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
  });

  it("无客户信息权限时忽略 URL 中合同号且不展示回填模板", () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_customer: false,
      data_purchase_cost: true,
      data_profit: true,
      action_maintenance_roundtrip_apply: true,
      own_customers_only: false,
    }));
    window.history.replaceState(
      null,
      "",
      "/maintenance/downloads?from=reminders&contract=XS-SECRET",
    );

    render(<ProjectCostPage view="downloads" />);

    expect(screen.getByRole("textbox", { name: "单合同编号" })).toHaveValue("");
    expect(screen.queryByRole("button", { name: "下载固定回填模板" })).toBeNull();
    expect(screen.getByRole("link", { name: "返回项目提醒" })).toBeInTheDocument();
  });

  it.each([320, 375])("%dpx 提醒卡片保持单列可收缩且不引入固定宽度", async (width) => {
    installSuccessResponses();
    render(
      <div style={{ width }}>
        <ProjectCostPage view="reminders" />
      </div>,
    );

    const grid = await screen.findByRole("list", { name: "项目提醒卡片" });
    const card = within(grid).getByRole("listitem");
    expect(grid).toHaveStyle({
      display: "grid",
      width: "100%",
      minWidth: "0",
    });
    expect(grid.style.gridTemplateColumns)
      .toBe("repeat(auto-fill, minmax(min(100%, 340px), 1fr))");
    expect(card).toHaveStyle({
      width: "100%",
      maxWidth: "100%",
      minWidth: "0",
      boxSizing: "border-box",
    });
  });

  it("项目事实故障不拖死详细盈亏，且事实重试不重复加载 board", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/board") return Promise.resolve(board());
      if (path === "/maintenance/projects") return Promise.reject(new Error("projects down"));
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    expect(await screen.findByTestId("maintenance-board-card-XSDD-1"))
      .toBeInTheDocument();
    expect(await screen.findByText("项目成本事实加载失败")).toBeInTheDocument();
    const boardCallsBeforeRetry = get.mock.calls.filter(
      ([path]) => path === "/maintenance/board",
    ).length;
    fireEvent.click(screen.getByRole("button", { name: "重试项目事实" }));
    await waitFor(() => expect(
      get.mock.calls.filter(([path]) => path === "/maintenance/projects").length,
    ).toBe(2));
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/board"))
      .toHaveLength(boardCallsBeforeRetry);
  });

  it("board 故障不拖死项目事实，且盈亏重试不重复加载 projects", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") {
        return Promise.resolve(projects("事实仍可见"));
      }
      if (path === "/maintenance/board") return Promise.reject(new Error("board down"));
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    expect(await screen.findByText("事实仍可见")).toBeInTheDocument();
    expect(await screen.findByText("详细盈亏加载失败")).toBeInTheDocument();
    const projectCallsBeforeRetry = get.mock.calls.filter(
      ([path]) => path === "/maintenance/projects",
    ).length;
    fireEvent.click(screen.getByRole("button", { name: "重试详细盈亏" }));
    await waitFor(() => expect(
      get.mock.calls.filter(([path]) => path === "/maintenance/board").length,
    ).toBe(2));
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/projects"))
      .toHaveLength(projectCallsBeforeRetry);
  });

  it("下载中心提供按合同拆分的可回填 ZIP 且状态覆盖完整请求", async () => {
    installSuccessResponses();
    const pending = deferred<{
      data: Blob;
      headers: Record<string, string>;
    }>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-16" } });
      if (path === "/maintenance/roundtrip-templates") return pending.promise;
      return Promise.reject(new Error("unexpected"));
    });
    let downloadedName = "";
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloadedName = this.download;
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    const button = screen.getByRole("button", {
      name: "批量下载可回填工作簿 ZIP",
    });

    act(() => {
      button.click();
      button.click();
    });

    expect(await screen.findByRole("status")).toHaveTextContent(
      "正在按合同生成可回填工作簿 ZIP，请勿关闭页面或重复点击",
    );
    expect(button).toBeDisabled();
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/roundtrip-templates"))
      .toHaveLength(1);

    pending.resolve({
      data: zipDownload().data,
      headers: {
        "content-type": "application/zip",
        "content-disposition":
          "attachment; filename=\"maintenance_roundtrip_templates.zip\"; "
          + "filename*=UTF-8''%E7%BB%B4%E4%BF%9D%E9%A1%B9%E7%9B%AE%E6%89%B9%E9%87%8F"
          + "%E5%9B%9E%E5%A1%AB%E6%A8%A1%E6%9D%BF.zip",
      },
    });

    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
    expect(downloadedName).toBe("维保项目批量回填模板.zip");
    expect(button).toBeEnabled();
  });

  it("固定回填单模板同步双击时只启动一个昂贵生成请求", async () => {
    installSuccessResponses();
    const pending = deferred<{
      data: Blob;
      headers: Record<string, string>;
    }>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-16" } });
      if (path === "/maintenance/roundtrip-template") return pending.promise;
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.change(screen.getByLabelText("单合同编号"), {
      target: { value: "XSDD-1" },
    });
    const button = screen.getByRole("button", { name: "下载固定回填模板" });

    act(() => {
      button.click();
      button.click();
    });

    await waitFor(() => expect(
      get.mock.calls.filter(([path]) => path === "/maintenance/roundtrip-template"),
    ).toHaveLength(1));
    expect(button).toBeDisabled();

    pending.resolve({
      data: xlsxDownload().data,
      headers: {
        "content-type":
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      },
    });
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("单项目明细和单合同工作簿各自全程 loading 并用 ref 阻止重复请求", async () => {
    installSuccessResponses();
    const linesPending = deferred<{ data: Blob }>();
    const workbookPending = deferred<{ data: Blob }>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-16" } });
      if (path === "/maintenance/lines/export") return linesPending.promise;
      if (path === "/maintenance/export-workbook") return workbookPending.promise;
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.change(screen.getByLabelText("单项目名称"), {
      target: { value: "回填项目" },
    });
    const linesButton = screen.getByRole("button", { name: "导出单项目明细 CSV" });

    act(() => {
      linesButton.click();
      linesButton.click();
    });

    expect(await screen.findByText("正在生成单项目明细 CSV，请勿重复点击"))
      .toBeInTheDocument();
    expect(linesButton).toBeDisabled();
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/lines/export"))
      .toHaveLength(1);

    fireEvent.change(screen.getByLabelText("单合同编号"), {
      target: { value: "XSDD-1" },
    });
    const workbookButton = screen.getByRole("button", {
      name: "导出单合同工作簿 XLSX",
    });
    act(() => {
      workbookButton.click();
      workbookButton.click();
    });

    expect(await screen.findByText("正在生成单合同工作簿 XLSX，请勿重复点击"))
      .toBeInTheDocument();
    expect(workbookButton).toBeDisabled();
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/export-workbook"))
      .toHaveLength(1);

    linesPending.resolve(csvDownload());
    workbookPending.resolve(xlsxDownload());
    await waitFor(() => {
      expect(linesButton).toBeEnabled();
      expect(workbookButton).toBeEnabled();
    });
  });

  it("重算摘要只展示当前成本瀑布并包含人工回填", () => {
    const summary = formatMaintenanceRecomputeSummary({
      lines_in_scope: 9,
      direct: 1,
      window: 1,
      month_avg: 1,
      pool_purchase: 1,
      pool_sales: 1,
      purchase_history: 1,
      sales_history: 1,
      manual: 1,
      none: 1,
      trace_avg: 99,
      sales_ref: 99,
    });
    expect(summary).toContain("人工回填 1");
    expect(summary).not.toContain("追溯");
    expect(summary).not.toContain("销售参考");
  });

  it("项目提醒默认只请求进行中并常驻展示三类期限计数", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="reminders" />);

    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/board",
      expect.objectContaining({
        params: expect.objectContaining({ lifecycle: "ongoing" }),
      }),
    ));
    expect(screen.getByText("进行中 2")).toBeInTheDocument();
    expect(screen.getByText("已结束 4")).toBeInTheDocument();
    expect(screen.getByText("期限缺失 1")).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup", { name: "维保订单导出日期" })).toBeNull();
  });

  it("375px 窄屏下载中心让八档日期控件独立横向滚动", async () => {
    installSuccessResponses();
    render(<div style={{ width: 375 }}><ProjectCostPage view="downloads" /></div>);
    await waitForDownloadsReady();

    const segmented = screen.getByRole("radiogroup", { name: "维保订单导出日期" });
    const dateScroller = segmented.parentElement;
    expect(dateScroller).toHaveStyle({
      width: "100%", minWidth: "0", maxWidth: "100%", overflowX: "auto",
    });
    expect(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导出订单汇总 Excel" }))
      .toBeInTheDocument();
    expect(screen.getByLabelText("项目成本 CSV 项目搜索"))
      .toHaveStyle({ width: "100%" });
    const projectLifecycle = screen.getByRole("radiogroup", {
      name: "项目成本 CSV 期限状态筛选",
    });
    expect(projectLifecycle.parentElement).toHaveStyle({
      width: "100%", minWidth: "0", maxWidth: "100%", overflowX: "auto",
    });
  });

  it("375px 窄屏项目提醒让期限二级导航独立横向滚动", async () => {
    installSuccessResponses();
    render(<div style={{ width: 375 }}><ProjectCostPage view="reminders" /></div>);
    await screen.findByText("XSDD-1");

    const segmented = screen.getByRole("radiogroup", { name: "维保期限筛选" });
    const lifecycleScroller = segmented.parentElement;
    expect(lifecycleScroller).toHaveStyle({
      width: "100%", minWidth: "0", maxWidth: "100%", overflowX: "auto",
    });
    expect(screen.getByText("进行中 2")).toBeInTheDocument();
    expect(screen.getByText("已结束 4")).toBeInTheDocument();
    expect(screen.getByText("期限缺失 1")).toBeInTheDocument();
  });

  it("375px 窄屏项目提醒让提醒类型控件在自己的父层横向滚动", async () => {
    installSuccessResponses();
    render(<div style={{ width: 375 }}><ProjectCostPage view="reminders" /></div>);
    await screen.findByText("XSDD-1");

    const segmented = screen.getByRole("radiogroup", { name: "项目提醒类型筛选" });
    expect(segmented.parentElement).toHaveStyle({
      width: "100%", minWidth: "0", maxWidth: "100%", overflowX: "auto",
    });
  });

  it("清楚区分批量完整工作簿、订单汇总、当前项目统计和单本工作簿", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    const zipButton = screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" });
    const summaryButton = screen.getByRole("button", { name: "导出订单汇总 Excel" });
    expect(zipButton).toHaveClass("ant-btn-primary");
    expect(summaryButton).not.toHaveClass("ant-btn-primary");
    expect(screen.getByRole("button", { name: "导出项目成本 CSV" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导出单合同工作簿 XLSX" }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载固定回填模板" }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "批量下载可回填工作簿 ZIP" }))
      .toBeInTheDocument();
  });

  it.each([
    ["缺成本权限", { data_purchase_cost: false, data_profit: true, own_customers_only: false }],
    ["缺利润权限", { data_purchase_cost: true, data_profit: false, own_customers_only: false }],
    ["受限销售", { data_purchase_cost: true, data_profit: true, own_customers_only: true }],
  ])("%s 时隐藏批量、单本和回填工作簿入口", async (_label, permissions) => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      ...permissions,
    }));
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    expect(screen.queryByRole("button", { name: "批量导出项目工作簿 ZIP" })).toBeNull();
    expect(screen.queryByRole("button", { name: "导出单合同工作簿 XLSX" })).toBeNull();
    expect(screen.queryByRole("button", { name: "下载固定回填模板" })).toBeNull();
    expect(screen.queryByRole("button", { name: "导入更新工作簿" })).toBeNull();
    if (permissions.own_customers_only) {
      expect(screen.queryByRole("button", { name: "导出订单汇总 Excel" })).toBeNull();
    } else {
      expect(screen.getByRole("button", { name: "导出订单汇总 Excel" }))
        .toBeInTheDocument();
    }
  });

  it("有成本利润可见权限但无回填动作权限时只允许下载模板", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_customer: true,
      data_purchase_cost: true,
      data_profit: true,
      action_maintenance_roundtrip_apply: false,
      own_customers_only: false,
    }));
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    expect(screen.getByRole("button", { name: "下载固定回填模板" }))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "导入更新工作簿" }))
      .toBeNull();
  });

  it("无客户信息权限时保留普通工作簿并隐藏可回填工作簿与导入", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_customer: false,
      data_purchase_cost: true,
      data_profit: true,
      action_maintenance_roundtrip_apply: true,
      own_customers_only: false,
    }));
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    expect(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导出单合同工作簿 XLSX" }))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "下载固定回填模板" })).toBeNull();
    expect(screen.queryByRole("button", { name: "批量下载可回填工作簿 ZIP" })).toBeNull();
    expect(screen.queryByRole("button", { name: "导入更新工作簿" })).toBeNull();
  });

  it.each(["今天", "近7天", "近14天", "近21天", "近30天", "本月"])(
    "首次加载无需预取 as_of 也可选择%s并保持全部为默认档",
    (label) => {
      get.mockReturnValue(new Promise(() => undefined));
      render(<ProjectCostPage view="downloads" />);

      expect(screen.getByRole("radio", { name: label })).toBeEnabled();
      expect(screen.getByRole("radio", { name: "全部" })).toHaveAttribute("checked");
    },
  );

  it("点击相对日期档立即按需读取业务日并展示实际闭区间", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/as-of") {
        return Promise.resolve({ data: { as_of: "2026-07-17" } });
      }
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/as-of")).toHaveLength(0);

    fireEvent.click(screen.getByRole("radio", { name: "近7天" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/as-of",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
    expect(await screen.findByText(
      "实际闭区间：2026-07-11 至 2026-07-17（含首尾）",
    )).toBeInTheDocument();
    expect(get.mock.calls.filter(([path]) => (
      path === "/maintenance/projects" || path === "/maintenance/board"
    ))).toHaveLength(0);
  });

  it("项目数据切换期限时分别更新项目事实和详细盈亏", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("XSDD-1");

    fireEvent.click(screen.getByText("已结束 4"));
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/maintenance/projects", expect.objectContaining({
        params: expect.objectContaining({ lifecycle: "ended" }),
      }));
      expect(get).toHaveBeenCalledWith("/maintenance/board", expect.objectContaining({
        params: expect.objectContaining({ lifecycle: "ended" }),
      }));
    });
    expect(await screen.findByText("已结束项目")).toBeInTheDocument();
    expect(screen.getAllByText("已结束").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByTestId("maintenance-board-card-XSDD-1")).toBeInTheDocument();
  });

  it("成本不完整卡片只展示补数事实，不显示余额、进度条或红黄绿结论", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") return Promise.resolve(projects());
      if (path === "/maintenance/board") {
        const response = board("XS-MISSING");
        response.data.rows[0] = {
          ...response.data.rows[0],
          decision_status: "incomplete_cost",
          status: "incomplete_cost",
          actual_cost_inc: 100,
          actual_cost_ex: 0,
          estimated_cost_inc: 40,
          estimated_cost_ex: 0,
          actual_lines: 1,
          estimated_lines: 1,
          missing_cost_lines: 2,
          known_cost_total: 140,
          cost_quality: "incomplete",
          spent_parts: 140,
          spent: 140,
          remaining: null,
          remaining_pct: null,
        };
        return Promise.resolve(response);
      }
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    const card = await screen.findByTestId("maintenance-board-card-XS-MISSING");
    expect(within(card).getByText("成本证据不完整")).toBeInTheDocument();
    expect(card).toHaveTextContent("实际参考：含 ¥100 · 不含 ¥0");
    expect(card).toHaveTextContent("估算参考：含 ¥40 · 不含 ¥0");
    expect(card).not.toHaveTextContent("实际参考 ¥100");
    expect(card).not.toHaveTextContent("估算参考 ¥40");
    expect(card).not.toHaveTextContent("¥140");
    expect(card).toHaveTextContent("缺失 2 行");
    expect(within(card).queryByText(/剩余/)).toBeNull();
    expect(within(card).queryByText(/预算消耗/)).toBeNull();
    expect(within(card).queryByRole("progressbar")).toBeNull();
    expect(within(card).queryByText(/健康|亏损|超支/)).toBeNull();
    expect(screen.queryByRole("radiogroup", { name: "预算消耗参考状态筛选" }))
      .toBeNull();
  });

  it("费用全量水位未建立时不把无记录显示为0，也不展示预算余额或红黄绿", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") return Promise.resolve(projects());
      if (path === "/maintenance/board") {
        const response = board("XS-NO-EXPENSE-WATERMARK");
        response.data.rows[0] = {
          ...response.data.rows[0],
          decision_status: "expense_data_unavailable",
          status: "expense_data_unavailable",
          expense_data_available: false,
          spent_expense: null,
          spent: null,
          remaining: null,
          remaining_pct: null,
        };
        return Promise.resolve(response);
      }
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    const card = await screen.findByTestId(
      "maintenance-board-card-XS-NO-EXPENSE-WATERMARK",
    );
    expect(within(card).getByText("费用证据未就绪")).toBeInTheDocument();
    expect(card).toHaveTextContent("无报销记录不等于费用为 0");
    expect(card).toHaveTextContent("报销费用 数据未就绪（无记录不等于0）");
    expect(card).not.toHaveTextContent("报销费用 ¥0");
    expect(card).not.toHaveTextContent("剩余预算");
    expect(within(card).queryByRole("progressbar")).toBeNull();
    expect(screen.queryByText("待补费用数据 1")).toBeNull();
    expect(screen.queryByText("🟢 0")).toBeNull();
  });

  it("后端出现未知决策枚举时 fail-closed 为成本不完整，不能回退成旧 green", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") return Promise.resolve(projects());
      if (path === "/maintenance/board") {
        const response = board("XS-FUTURE");
        response.data.rows[0] = {
          ...response.data.rows[0],
          decision_status: "future_status",
          status: "green",
          cost_quality: "actual_only",
          remaining: 900,
          remaining_pct: 90,
        };
        return Promise.resolve(response);
      }
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    const card = await screen.findByTestId("maintenance-board-card-XS-FUTURE");
    expect(within(card).getByText("成本证据不完整")).toBeInTheDocument();
    expect(card).not.toHaveTextContent("预算余量 > 20%");
    expect(within(card).queryByText(/剩余预算/)).toBeNull();
    expect(within(card).queryByRole("progressbar")).toBeNull();
    expect(screen.queryByText("待补成本 1")).toBeNull();
    expect(screen.queryByText("🟢 0")).toBeNull();
  });

  it.each([
    ["decision_status 为 null", { decision_status: null, status: "green", cost_quality: "actual_only" }],
    ["decision_status 缺失", { decision_status: undefined, status: "green", cost_quality: "actual_only" }],
    ["cost_quality 为 null", { decision_status: "green", status: "green", cost_quality: null }],
    ["cost_quality 缺失", { decision_status: "green", status: "green", cost_quality: undefined }],
  ])("不受限响应%s时 fail-closed，不能展示旧状态的预算结论", async (_label, overrides) => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") return Promise.resolve(projects());
      if (path === "/maintenance/board") {
        const response = board("XS-NULL-GATE");
        response.data.rows[0] = {
          ...response.data.rows[0],
          ...overrides,
        };
        return Promise.resolve(response);
      }
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    const card = await screen.findByTestId("maintenance-board-card-XS-NULL-GATE");
    expect(within(card).getByText("成本证据不完整")).toBeInTheDocument();
    expect(card).not.toHaveTextContent("预算余量 > 20%");
    expect(within(card).queryByText(/剩余预算/)).toBeNull();
    expect(within(card).queryByRole("progressbar")).toBeNull();
  });

  it("no_budget 即使收到脏负预算也不在前端自行重算进度", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") return Promise.resolve(projects());
      if (path === "/maintenance/board") {
        const response = board("XS-NO-BUDGET");
        response.data.rows[0] = {
          ...response.data.rows[0],
          decision_status: "no_budget",
          status: "no_budget",
          budget: -1,
          spent: 100,
          remaining: null,
          remaining_pct: null,
        };
        return Promise.resolve(response);
      }
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    const card = await screen.findByTestId("maintenance-board-card-XS-NO-BUDGET");
    expect(card).toHaveTextContent("合同额参考 —");
    expect(card).not.toHaveTextContent("无预算(未关联合同额)");
    expect(within(card).queryByRole("progressbar")).toBeNull();
    expect(card).not.toHaveTextContent("预算消耗参考 -10000%");
  });

  it("成本权限脱敏时统计保持横杠，不把空值伪装成 0 或成本缺失", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: false,
      data_profit: true,
      own_customers_only: false,
    }));
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") {
        const response = projects();
        response.data.rows[0] = {
          ...response.data.rows[0],
          cost_inc: null,
          cost_ex: null,
          cost_total: null,
          actual_cost_inc: null,
          actual_cost_ex: null,
          estimated_cost_inc: null,
          estimated_cost_ex: null,
          actual_lines: null,
          estimated_lines: null,
          missing_cost_lines: null,
          known_cost_total: null,
          cost_quality: null,
          coverage_pct: null,
          by_source: null,
        };
        response.data.ranking_restricted = true;
        return Promise.resolve(response);
      }
      if (path === "/maintenance/board") {
        const response = board("XS-MASKED");
        response.data.rows[0] = {
          ...response.data.rows[0],
          decision_status: undefined,
          status: undefined,
          actual_cost_inc: null,
          actual_cost_ex: null,
          estimated_cost_inc: null,
          estimated_cost_ex: null,
          actual_lines: null,
          estimated_lines: null,
          missing_cost_lines: null,
          known_cost_total: null,
          cost_quality: null,
          coverage_pct: null,
          spent_parts: null,
          spent: null,
          remaining: null,
          remaining_pct: null,
        };
        return Promise.resolve({
          data: { ...response.data, decision_restricted: true },
        });
      }
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    await screen.findByTestId("maintenance-board-card-XS-MASKED");
    for (const title of [
      "实际采购参考（含税）",
      "实际采购参考（未税）",
      "估算参考（含税）",
      "估算参考（未税）",
      "缺失成本行",
    ]) {
      expect(screen.getByText(title).closest(".ant-statistic")).toHaveTextContent("—");
    }
    expect(screen.queryByRole("radiogroup", { name: "预算消耗参考状态筛选" })).toBeNull();
    expect(screen.getByText("当前账号不展示合同金额、毛利等受限字段"))
      .toBeInTheDocument();
    expect(screen.queryByText("缺失 0 行")).toBeNull();
  });

  it("利润权限脱敏时保留成本事实但不展示合同额、预算余量或决策分类", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: true,
      data_profit: false,
      own_customers_only: false,
    }));
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") {
        const response = projects();
        response.data.rows[0].contract_amount = null;
        return Promise.resolve(response);
      }
      if (path === "/maintenance/board") {
        const response = board("XS-PROFIT-MASKED");
        response.data.rows[0] = {
          ...response.data.rows[0],
          decision_status: undefined,
          status: undefined,
          budget: null,
          remaining: null,
          remaining_pct: null,
        };
        return Promise.resolve({
          data: { ...response.data, decision_restricted: true },
        });
      }
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    const card = await screen.findByTestId("maintenance-board-card-XS-PROFIT-MASKED");
    expect(card).toHaveTextContent("实际参考：含 ¥100 · 不含 ¥0");
    expect(card).toHaveTextContent("缺失 0 行");
    expect(card).not.toHaveTextContent(/合同额参考|剩余预算|预算消耗参考状态/);
    expect(within(card).queryByRole("progressbar")).toBeNull();
    expect(screen.queryByRole("radiogroup", { name: "预算消耗参考状态筛选" })).toBeNull();
    expect(screen.getByText("当前账号不展示合同金额、毛利等受限字段"))
      .toBeInTheDocument();
    expect(screen.queryByText("¥1,000")).toBeNull();
  });

  it("利润权限受限但成本质量显式 incomplete 时仍展示补数事实，决策外观保持中性",
    async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: true,
      data_profit: false,
      own_customers_only: false,
    }));
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") return Promise.resolve(projects());
      if (path === "/maintenance/board") {
        const response = board("XS-PROFIT-MISSING");
        response.data.rows[0] = {
          ...response.data.rows[0],
          decision_status: undefined,
          status: undefined,
          cost_quality: "incomplete",
          missing_cost_lines: 1,
          budget: null,
          remaining: null,
          remaining_pct: null,
        };
        return Promise.resolve({
          data: { ...response.data, decision_restricted: true },
        });
      }
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    const card = await screen.findByTestId("maintenance-board-card-XS-PROFIT-MISSING");
    expect(within(card).getByText("成本证据不完整")).toBeInTheDocument();
    expect(card).toHaveTextContent("缺失 1 行");
    expect(within(card).queryByText(/剩余预算|预算余量|预算已用完/)).toBeNull();
    expect(within(card).queryByRole("progressbar")).toBeNull();
    expect(card).not.toHaveStyle({ borderLeft: "4px solid #8c8c8c" });
  });

  it("利润权限受限且成本质量未知时保持中性，不把可见金额推断为成本不完整",
    async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: true,
      data_profit: false,
      own_customers_only: false,
    }));
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") return Promise.resolve(projects());
      if (path === "/maintenance/board") {
        const response = board("XS-PROFIT-UNKNOWN");
        response.data.rows[0] = {
          ...response.data.rows[0],
          decision_status: undefined,
          status: undefined,
          cost_quality: null,
          missing_cost_lines: null,
          budget: null,
          remaining: null,
          remaining_pct: null,
        };
        return Promise.resolve({
          data: { ...response.data, profit_restricted: true },
        });
      }
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    const card = await screen.findByTestId("maintenance-board-card-XS-PROFIT-UNKNOWN");
    expect(within(card).queryByText("成本证据不完整")).toBeNull();
    expect(card).not.toHaveTextContent(/预算余量|预算已用完|需补数据/);
    expect(within(card).queryByRole("progressbar")).toBeNull();
  });

  it("权限限制标志不一致时按任一 true 收口，不能泄漏决策状态", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") return Promise.resolve(projects());
      if (path === "/maintenance/board") {
        const response = board("XS-DRIFTED-MASK");
        return Promise.resolve({
          data: {
            ...response.data,
            decision_restricted: false,
            profit_restricted: true,
          },
        });
      }
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    const card = await screen.findByTestId("maintenance-board-card-XS-DRIFTED-MASK");
    expect(screen.queryByRole("radiogroup", { name: "预算消耗参考状态筛选" })).toBeNull();
    expect(card).not.toHaveTextContent(/预算余量 > 20%|剩余预算/);
    expect(card).not.toHaveStyle({ borderLeft: "4px solid #8c8c8c" });
  });

  it.each([
    ["decision_restricted", { decision_restricted: true }],
    ["profit_restricted", { decision_restricted: false, profit_restricted: true }],
    ["ranking_restricted", { decision_restricted: false, ranking_restricted: true }],
  ])("%s 且决策和成本状态未知时提醒保持中性，不误报待补成本",
    async (_label, restrictedFlags) => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/board") {
        const response = board("XS-RESTRICTED-UNKNOWN");
        response.data.rows[0] = {
          ...response.data.rows[0],
          decision_status: undefined,
          status: undefined,
          cost_quality: null,
          actual_cost_inc: null,
          actual_cost_ex: null,
          estimated_cost_inc: null,
          estimated_cost_ex: null,
          actual_lines: null,
          estimated_lines: null,
          missing_cost_lines: null,
          known_cost_total: null,
        };
        return Promise.resolve({
          data: { ...response.data, ...restrictedFlags },
        });
      }
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage view="reminders" />);

    expect(await screen.findByText("XS-RESTRICTED-UNKNOWN")).toBeInTheDocument();
    expect(screen.getByText("经营判断受限")).toBeInTheDocument();
    expect(screen.queryByText("成本不完整，需补数据")).toBeNull();
    expect(screen.queryByText(/成本缺失 .* 行/)).toBeNull();
  });

  it("项目成本字段不受限时 cost_quality null 按需补数据 fail-closed", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") {
        const response = projects("项目质量空值");
        response.data.rows[0].cost_quality = null;
        response.data.rows[0].missing_cost_lines = null;
        response.data.ranking_restricted = false;
        return Promise.resolve(response);
      }
      if (path === "/maintenance/board") return Promise.resolve(board());
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    await screen.findByText("项目质量空值");
    expect(screen.getByText("需补数据 · — 行")).toBeInTheDocument();
  });

  it("明细以 cost_tier 为权威，未知来源和税口径不显示成已知成本", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") return Promise.resolve(projects("严格分层项目"));
      if (path === "/maintenance/board") return Promise.resolve(board());
      if (path === "/maintenance/lines") {
        return Promise.resolve({
          data: {
            total: 1,
            page: 1,
            page_size: 50,
            rows: [{
              id: 1,
              order_no: "WBDD-INVALID",
              order_date: "2026-07-01",
              demand_type: "维保",
              business_type: null,
              warehouse: "北京仓",
              pn_std: "PN-INVALID",
              description: "历史脏行",
              qty: 1,
              return_qty: 0,
              unit_cost: null,
              cost_amount: null,
              cost_tier: "missing",
              cost_source: "future_source",
              cost_tax_basis: "gross",
              price_month: null,
              trace_months: null,
              linked_purchase_order_no: null,
              price_distance_days: null,
              confidence: "low",
              anomaly_flags: [],
            }],
          },
        });
      }
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);
    await screen.findByText("严格分层项目");
    fireEvent.click(screen.getByText("明细"));

    expect(await screen.findByText("PN-INVALID")).toBeInTheDocument();
    expect(screen.getAllByText("成本缺失")).toHaveLength(2);
    expect(screen.getByText("未知")).toBeInTheDocument();
    expect(screen.queryByText("future_source")).toBeNull();
    expect(screen.queryByText("¥999")).toBeNull();
  });

  it("项目出库明细按接口 id 稳定区分不同 PN 和数量", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const projectResponse = projects("多行明细项目");
    projectResponse.data.rows[0].months = 2;
    get.mockImplementation((
      path: string,
      config?: { params?: { month?: string } },
    ) => {
      if (path === "/maintenance/projects") return Promise.resolve(projectResponse);
      if (path === "/maintenance/board") return Promise.resolve(board());
      if (path === "/maintenance/lines") {
        const filteredRows = config?.params?.month
          ? [{
              id: "ML-MONTH",
              order_no: "WBDD-LINE-MONTH",
              order_date: "2026-06-30",
              demand_type: "维保",
              business_type: "备件维保",
              warehouse: "北京仓",
              pn_std: "PN-STABLE-MONTH",
              description: "月份筛选明细",
              qty: 7,
              return_qty: 0,
              unit_cost: null,
              cost_amount: null,
              cost_tier: "missing",
              cost_source: "none",
              cost_tax_basis: null,
              price_month: null,
              trace_months: null,
              linked_purchase_order_no: null,
              price_distance_days: null,
              confidence: null,
              anomaly_flags: [],
            }]
          : [
              {
                id: "ML-101",
                order_no: "WBDD-LINE-A",
                order_date: "2026-07-29",
                demand_type: "维保",
                business_type: "备件维保",
                warehouse: "北京仓",
                pn_std: "PN-STABLE-A",
                description: "第一条明细",
                qty: 1,
                return_qty: 0,
                unit_cost: null,
                cost_amount: null,
                cost_tier: "missing",
                cost_source: "none",
                cost_tax_basis: null,
                price_month: null,
                trace_months: null,
                linked_purchase_order_no: null,
                price_distance_days: null,
                confidence: null,
                anomaly_flags: [],
              },
              {
                id: "ML-202",
                order_no: "WBDD-LINE-B",
                order_date: "2026-07-30",
                demand_type: "维保",
                business_type: "备件维保",
                warehouse: "北京仓",
                pn_std: "PN-STABLE-B",
                description: "第二条明细",
                qty: 2,
                return_qty: 0,
                unit_cost: null,
                cost_amount: null,
                cost_tier: "missing",
                cost_source: "none",
                cost_tax_basis: null,
                price_month: null,
                trace_months: null,
                linked_purchase_order_no: null,
                price_distance_days: null,
                confidence: null,
                anomaly_flags: [],
              },
            ];
        return Promise.resolve({
          data: {
            total: filteredRows.length,
            page: 1,
            page_size: 50,
            rows: filteredRows,
          },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });

    render(<ProjectCostPage />);
    await screen.findByText("多行明细项目");
    fireEvent.click(screen.getByText("明细"));

    const firstRow = await waitFor(() => {
      const row = document.querySelector('tr[data-row-key="ML-101"]');
      expect(row).not.toBeNull();
      return row as HTMLTableRowElement;
    });
    const secondRow = document.querySelector('tr[data-row-key="ML-202"]') as HTMLTableRowElement;
    expect(secondRow).not.toBeNull();
    expect(within(firstRow).getByText("PN-STABLE-A")).toBeInTheDocument();
    expect(within(firstRow).getByText("1")).toBeInTheDocument();
    expect(within(secondRow).getByText("PN-STABLE-B")).toBeInTheDocument();
    expect(within(secondRow).getByText("2")).toBeInTheDocument();
    expect(consoleError.mock.calls.flat().join(" "))
      .not.toContain("Each child in a list should have a unique key prop");

    fireEvent.click(screen.getByPlaceholderText("按月筛选"));
    const monthCell = await waitFor(() => {
      const cell = document.querySelector(
        ".ant-picker-month-panel .ant-picker-cell-in-view",
      );
      expect(cell).not.toBeNull();
      return cell as HTMLElement;
    });
    fireEvent.click(monthCell);

    const filteredRow = await waitFor(() => {
      const row = document.querySelector('tr[data-row-key="ML-MONTH"]');
      expect(row).not.toBeNull();
      return row as HTMLTableRowElement;
    });
    expect(within(filteredRow).getByText("PN-STABLE-MONTH")).toBeInTheDocument();
    expect(within(filteredRow).getByText("7")).toBeInTheDocument();
    expect(document.querySelector('tr[data-row-key="ML-101"]')).toBeNull();
    expect(document.querySelector('tr[data-row-key="ML-202"]')).toBeNull();
    expect(get.mock.calls.some(([path, config]) => (
      path === "/maintenance/lines"
      && /^\d{4}-\d{2}$/.test(config?.params?.month ?? "")
    ))).toBe(true);
  });

  it("旧筛选最后返回也不能覆盖新筛选", async () => {
    const oldBoard = deferred<ReturnType<typeof board>>();
    const newBoard = deferred<ReturnType<typeof board>>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const latest = config?.params?.lifecycle === "ended";
      if (path === "/maintenance/board") return latest ? newBoard.promise : oldBoard.promise;
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage view="reminders" />);
    fireEvent.click(screen.getByText("已结束 0"));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    newBoard.resolve(board("最新合同", "ended"));
    expect(await screen.findByText("最新合同")).toBeInTheDocument();

    oldBoard.resolve(board("迟到旧合同", "ongoing"));
    await waitFor(() => expect(screen.queryByText("迟到旧合同")).toBeNull());
    expect(screen.getByText("最新合同")).toBeInTheDocument();
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/projects")).toHaveLength(0);
  });

  it("当前筛选失败会清空旧项目和卡片，显示可持续重试的错误状态", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="reminders" />);
    expect(await screen.findByText("XSDD-1")).toBeInTheDocument();

    get.mockRejectedValue(new Error("network"));
    fireEvent.click(screen.getByText("已结束 4"));
    expect(await screen.findByText("项目提醒加载失败，旧结果已清空。"))
      .toBeInTheDocument();
    expect(screen.queryByText("XSDD-1")).toBeNull();
    expect(screen.getByText("进行中 0")).toBeInTheDocument();
    expect(screen.getByText("已结束 0")).toBeInTheDocument();
    expect(screen.getByText("期限缺失 0")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重\s*试/ })).toBeInTheDocument();

    const callsBeforeRetry = get.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(callsBeforeRetry));
  });

  it("全部档通过新端点导出且不携带页面期限或搜索参数", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.objectContaining({
      params: {},
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
  });

  it("固定回填模板沿用导出日期口径，全部档下载完整模板", async () => {
    installSuccessResponses();
    let downloadedName = "";
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      downloadedName = this.download;
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "下载固定回填模板" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/roundtrip-template", expect.objectContaining({
      params: {},
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
    expect(downloadedName).toBe("维保项目回填模板_全部.xlsx");
  });

  it("下载中心可按合同下载带签名 scope 的单合同回填模板", async () => {
    installSuccessResponses();
    let downloadedName = "";
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      downloadedName = this.download;
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.change(screen.getByLabelText("单合同编号"), {
      target: { value: "XSDD-1" },
    });

    fireEvent.click(screen.getByRole("button", { name: "下载固定回填模板" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/roundtrip-template", expect.objectContaining({
      params: { contract: "XSDD-1" },
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
    expect(downloadedName).toBe("维保项目回填模板_XSDD-1_全部.xlsx");
  });

  it("导入固定工作簿使用 multipart 文件并常驻显示服务端处理摘要", async () => {
    installSuccessResponses();
    post.mockResolvedValue({
      data: {
        status: "success",
        no_op: false,
        changed_rows: 3,
        counts: { create: 1, update: 2, void: 0, keep: 4 },
      },
    });
    const { container } = render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    const file = new File(["workbook"], "维保回填.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });

    fireEvent.change(input!, { target: { files: [file] } });

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const [path, body] = post.mock.calls[0];
    expect(path).toBe("/maintenance/roundtrip-import");
    expect(body).toBeInstanceOf(FormData);
    expect((body as FormData).get("file")).toMatchObject({ name: "维保回填.xlsx" });
    expect(await screen.findAllByText(
      "回填完成：变更 3 行 · 新增 1 行 · 更新 2 行 · 作废 0 行 · 保留 4 行",
    )).not.toHaveLength(0);
    expect(screen.queryByText("回填工作簿导入失败，请检查模板内容后重试"))
      .toBeNull();
    expect(get.mock.calls.filter(([path]) => (
      path === "/maintenance/projects" || path === "/maintenance/board"
    ))).toHaveLength(0);
  });

  it("固定工作簿导入同步重复触发时只提交一次", async () => {
    installSuccessResponses();
    const importPending = deferred<{ data: { status: string; no_op: boolean } }>();
    post.mockReturnValue(importPending.promise);
    const { container } = render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    const first = new File(["first"], "第一次.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    const second = new File(["second"], "第二次.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });

    act(() => {
      fireEvent.change(input!, { target: { files: [first, second] } });
    });

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    importPending.resolve({ data: { status: "success", no_op: false } });
    await waitFor(() => expect(
      screen.getByRole("button", { name: "导入更新工作簿" }),
    ).toBeEnabled());
  });

  it("重复导入摘要明确说明未重复更新", () => {
    expect(formatRoundtripImportSummary({ status: "success", no_op: true }))
      .toBe("该工作簿此前已成功导入，本次未重复更新");
  });

  it("全部档一键下载所有项目工作簿 ZIP 且不携带页面筛选参数", async () => {
    installSuccessResponses();
    let downloadedName = "";
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      downloadedName = this.download;
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/export-workbooks", expect.objectContaining({
      params: {},
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
    await waitFor(() => expect(downloadedName).toBe("maintenance_project_workbooks_all.zip"));
  });

  it("批量工作簿 ZIP 慢请求防重复且不与其他导出互锁", async () => {
    const zipPending = deferred<{ data: Blob }>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("当前项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/export-workbooks") return zipPending.promise;
      if (path === "/maintenance/orders/export") return Promise.resolve(xlsxDownload());
      if (path === "/maintenance/export") return Promise.resolve(csvDownload());
      if (path === "/maintenance/export-workbook") return Promise.resolve(xlsxDownload());
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    const zipButton = screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" });
    const orderButton = screen.getByRole("button", { name: "导出订单汇总 Excel" });
    const csvButton = screen.getByRole("button", { name: "导出项目成本 CSV" });

    act(() => {
      zipButton.click();
      zipButton.click();
    });

    await waitFor(() => expect(zipButton).toBeDisabled());
    expect(orderButton).toBeEnabled();
    expect(csvButton).toBeEnabled();
    fireEvent.click(orderButton);
    fireEvent.click(csvButton);
    fireEvent.change(screen.getByLabelText("单合同编号"), {
      target: { value: "XSDD-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "导出单合同工作簿 XLSX" }));
    await waitFor(() => {
      expect(get.mock.calls.filter(([path]) => path === "/maintenance/export-workbooks")).toHaveLength(1);
      expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.anything());
      expect(get).toHaveBeenCalledWith("/maintenance/export", expect.anything());
      expect(get).toHaveBeenCalledWith("/maintenance/export-workbook", expect.anything());
    });

    zipPending.resolve(zipDownload());
    await waitFor(() => expect(zipButton).toBeEnabled());
  });

  it("项目 rows 为空时主 XLSX 导出仍可用", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") {
        return Promise.resolve({ data: { ...projects().data, rows: [] } });
      }
      if (path === "/maintenance/board") {
        return Promise.resolve({ data: { ...board().data, rows: [] } });
      }
      if (path === "/maintenance/orders/export") return Promise.resolve(xlsxDownload());
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    const button = screen.getByRole("button", { name: "导出订单汇总 Excel" });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.anything()));
  });

  it("项目 rows 为空时批量工作簿 ZIP 仍可用", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") {
        return Promise.resolve({ data: { ...projects().data, rows: [] } });
      }
      if (path === "/maintenance/board") {
        return Promise.resolve({ data: { ...board().data, rows: [] } });
      }
      if (path === "/maintenance/export-workbooks") {
        return Promise.resolve(zipDownload());
      }
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    const button = screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/export-workbooks", expect.anything()));
  });

  it("主 XLSX 请求进行中禁用按钮以避免重复导出", async () => {
    installSuccessResponses();
    const pending = deferred<{ data: Blob }>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/orders/export") return pending.promise;
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    const button = screen.getByRole("button", { name: "导出订单汇总 Excel" });

    fireEvent.click(button);

    await waitFor(() => expect(button).toBeDisabled());
    pending.resolve(xlsxDownload());
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("近7天只影响导出闭区间，不把日期注入页面项目和看板请求", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    const expected = {
      date_from: "2026-07-10",
      date_to: "2026-07-16",
    };

    fireEvent.click(screen.getByText("近7天"));

    for (const [path, config] of get.mock.calls.filter(([path]) => (
      path === "/maintenance/projects" || path === "/maintenance/board"
    ))) {
      expect(path).toMatch(/^\/maintenance\/(projects|board)$/);
      expect(config.params).not.toHaveProperty("date_from");
      expect(config.params).not.toHaveProperty("date_to");
    }
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.objectContaining({
      params: expected,
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
  });

  it("今天档使用今天作为同一个闭区间首尾", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("今天"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.objectContaining({
      params: { date_from: "2026-07-16", date_to: "2026-07-16" },
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
  });

  it("近14天档从今天向前包含十四个自然日", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("近14天"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.objectContaining({
      params: {
        date_from: "2026-07-03",
        date_to: "2026-07-16",
      },
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
  });

  it("近21天档从今天向前包含二十一个自然日", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("近21天"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.objectContaining({
      params: {
        date_from: "2026-06-26",
        date_to: "2026-07-16",
      },
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
  });

  it("近30天档从今天向前包含三十个自然日", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("近30天"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.objectContaining({
      params: {
        date_from: "2026-06-17",
        date_to: "2026-07-16",
      },
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
  });

  it("本月档从当月一日到今天", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("本月"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.objectContaining({
      params: {
        date_from: "2026-07-01",
        date_to: "2026-07-16",
      },
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
  });

  it("浏览器日期与后端业务日跨月时仍以 as_of 计算本月", async () => {
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") {
        return Promise.resolve(projects("跨月项目", lifecycle, "2026-06-30"));
      }
      if (path === "/maintenance/board") {
        return Promise.resolve(board(undefined, lifecycle, "2026-06-30"));
      }
      if (path === "/maintenance/as-of") {
        return Promise.resolve({ data: { as_of: "2026-06-30" } });
      }
      if (path === "/maintenance/orders/export") return Promise.resolve(xlsxDownload());
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByText("本月"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.objectContaining({
      params: { date_from: "2026-06-01", date_to: "2026-06-30" },
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
  });

  it("自定义缺少日期范围时不发送导出请求", async () => {
    installSuccessResponses();
    const warningMessage = vi.spyOn(message, "warning");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByText("自定义"));
    const callsBeforeExport = get.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await Promise.resolve();
    expect(get.mock.calls.slice(callsBeforeExport).filter(([path]) => (
      path === "/maintenance/orders/export"
    ))).toHaveLength(0);
    expect(warningMessage).toHaveBeenCalledWith("请选择自定义起止日期");
  });

  it.each(["today", "last7", "last14", "last21", "last30", "month"] as const)(
    "%s 档缺少日期范围时导出参数构造拒绝请求",
    (preset) => {
      expect(buildOrderExportParams(preset, null)).toBeNull();
    },
  );

  it("下载锚点加入 DOM 后点击移除并延迟释放 Object URL", async () => {
    installSuccessResponses();
    let attachedWhenClicked = false;
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      attachedWhenClicked = document.body.contains(this);
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled());
    expect(attachedWhenClicked).toBe(true);
    expect(document.querySelector('a[download="maintenance_orders_all.xlsx"]')).toBeNull();
    expect(window.setTimeout).toHaveBeenCalledWith(expect.any(Function), 100);
    await new Promise((resolve) => setTimeout(resolve, 110));
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
  });

  it("下载锚点 click 抛错也移除节点并最终延迟释放 Object URL", async () => {
    installSuccessResponses();
    const remove = vi.spyOn(HTMLAnchorElement.prototype, "remove");
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(() => {
      throw new Error("click failed");
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(remove).toHaveBeenCalled());
    expect(document.querySelector('a[download="maintenance_orders_all.xlsx"]')).toBeNull();
    expect(window.setTimeout).toHaveBeenCalledWith(expect.any(Function), 100);
    await new Promise((resolve) => setTimeout(resolve, 110));
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
  });

  it("下载锚点 append 抛错也执行移除并最终延迟释放 Object URL", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    const remove = vi.spyOn(HTMLAnchorElement.prototype, "remove");
    const originalAppend = document.body.appendChild.bind(document.body);
    vi.spyOn(document.body, "appendChild").mockImplementation((node) => {
      if (node instanceof HTMLAnchorElement) throw new Error("append failed");
      return originalAppend(node);
    });

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(remove).toHaveBeenCalled());
    expect(window.setTimeout).toHaveBeenCalledWith(expect.any(Function), 100);
    await new Promise((resolve) => setTimeout(resolve, 110));
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
  });

  it("旧合同工作簿也在锚点入 DOM 后点击移除并延迟释放 URL", async () => {
    installSuccessResponses();
    const originalImplementation = get.getMockImplementation();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      if (path === "/maintenance/export-workbook") {
        return Promise.resolve({
          data: xlsxDownload().data,
          headers: {
            "content-type":
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          },
        });
      }
      return originalImplementation?.(path, config);
    });
    let attachedWhenClicked = false;
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      if (this.download) attachedWhenClicked = document.body.contains(this);
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.change(screen.getByLabelText("单合同编号"), {
      target: { value: "XSDD-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "导出单合同工作簿 XLSX" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/export-workbook", expect.objectContaining({
      params: expect.objectContaining({ contract: "XSDD-1" }),
      responseType: "blob",
    })));
    await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled());
    expect(attachedWhenClicked).toBe(true);
    expect(document.querySelector('a[download="项目工作簿_XSDD-1.xlsx"]')).toBeNull();
    expect(window.setTimeout).toHaveBeenCalledWith(expect.any(Function), 100);
    await new Promise((resolve) => setTimeout(resolve, 110));
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
  });

  it("下载使用服务端 Content-Disposition 的 UTF-8 中文文件名", async () => {
    installSuccessResponses();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board("中文合同", lifecycle));
      if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-16" } });
      if (path === "/maintenance/export-workbook") return Promise.resolve({
        data: xlsxDownload().data,
        headers: {
          "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "content-disposition":
            "attachment; filename=\"project_workbook.xlsx\"; "
            + "filename*=UTF-8''project_workbook_%E4%B8%AD%E6%96%87%E5%90%88%E5%90%8C.xlsx",
        },
      });
      return Promise.reject(new Error("unexpected"));
    });
    let downloadedName = "";
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloadedName = this.download;
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.change(screen.getByLabelText("单合同编号"), {
      target: { value: "中文合同" },
    });
    fireEvent.click(screen.getByRole("button", { name: "导出单合同工作簿 XLSX" }));

    await waitFor(() => expect(downloadedName).toBe("project_workbook_中文合同.xlsx"));
  });

  it("下载文件名保留正常中文但剔除 Unicode 格式与双向控制字符", async () => {
    const unsafeFilename = "project_中文\u202E\u200B\u2066合同.xlsx";
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/export-workbook") return Promise.resolve({
        data: xlsxDownload().data,
        headers: {
          "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "content-disposition":
            `attachment; filename="project_workbook.xlsx"; filename*=UTF-8''${
              encodeURIComponent(unsafeFilename)
            }`,
        },
      });
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    let downloadedName = "";
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloadedName = this.download;
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.change(screen.getByLabelText("单合同编号"), {
      target: { value: "XSDD-1" },
    });

    fireEvent.click(screen.getByRole("button", { name: "导出单合同工作簿 XLSX" }));

    await waitFor(() => expect(downloadedName).toBe("project_中文合同.xlsx"));
  });

  it("HTTP 200 HTML 响应 fail-closed 且不触发文件保存", async () => {
    installSuccessResponses();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-16" } });
      if (path === "/maintenance/orders/export") return Promise.resolve({
        data: new Blob(["<html>login</html>"], { type: "text/html" }),
        headers: { "content-type": "text/html; charset=utf-8" },
      });
      return Promise.reject(new Error("unexpected"));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "服务器返回的不是可下载文件，请稍后重试或联系管理员",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("HTTP 200 下载响应缺失 MIME 时失败关闭并给出可理解提示", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/orders/export") {
        return Promise.resolve({
          data: new Blob([new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x01])]),
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "服务器未返回文件类型，已取消下载，请重试或联系管理员",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("HTTP 200 零字节文件失败关闭且不伪装成下载成功", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/orders/export") {
        return Promise.resolve({
          data: new Blob([], {
            type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          }),
          headers: {
            "content-type":
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "服务器返回了空文件，已取消下载，请重试或联系管理员",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("XLSX MIME 正确但文件签名损坏时失败关闭", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/orders/export") {
        return Promise.resolve({
          data: new Blob(["not-an-ooxml-workbook"], {
            type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          }),
          headers: {
            "content-type":
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "服务器返回的 Excel 文件损坏或结构不完整，已取消下载",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("只有五字节本地头的伪 XLSX 失败关闭", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/orders/export") {
        return Promise.resolve({
          data: new Blob(
            [new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x01])],
            { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
          ),
          headers: {
            "content-type":
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "服务器返回的 Excel 文件损坏或结构不完整，已取消下载",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("普通 ZIP 即使伪装成 XLSX MIME 也不能冒充工作簿", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/orders/export") {
        return Promise.resolve({
          data: archiveBlob(
            VALID_ZIP_BASE64,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          ),
          headers: {
            "content-type":
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "服务器返回的 Excel 文件不是有效的 XLSX 工作簿，已取消下载",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("ZIP MIME 正确但文件签名损坏时失败关闭", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/export-workbooks") {
        return Promise.resolve({
          data: new Blob(["not-a-zip-archive"], { type: "application/zip" }),
          headers: { "content-type": "application/zip" },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "服务器返回的 ZIP 文件损坏或结构不完整，已取消下载",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("只有五字节本地头的伪 ZIP 失败关闭", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/export-workbooks") {
        return Promise.resolve({
          data: new Blob(
            [new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x01])],
            { type: "application/zip" },
          ),
          headers: { "content-type": "application/zip" },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "服务器返回的 ZIP 文件损坏或结构不完整，已取消下载",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("中心目录偏移损坏的 ZIP 失败关闭", async () => {
    const corrupt = archiveBytes(VALID_ZIP_BASE64);
    new DataView(corrupt.buffer).setUint32(corrupt.length - 22 + 16, 1, true);
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/export-workbooks") {
        return Promise.resolve({
          data: new Blob([corrupt], { type: "application/zip" }),
          headers: { "content-type": "application/zip" },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "服务器返回的 ZIP 文件损坏或结构不完整，已取消下载",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it.each([
    ["/maintenance/export-workbooks", "批量导出项目工作簿 ZIP"],
    ["/maintenance/roundtrip-templates", "批量下载可回填工作簿 ZIP"],
  ])("%s 返回零成员 ZIP 时拒绝落盘", async (endpoint, buttonName) => {
    get.mockImplementation((path: string) => {
      if (path === endpoint) {
        return Promise.resolve({
          data: archiveBlob(EMPTY_ZIP_BASE64, "application/zip"),
          headers: { "content-type": "application/zip" },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: buttonName }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "服务器返回的 ZIP 文件损坏或结构不完整，已取消下载",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("ZIP64 响应给出明确的不支持提示", async () => {
    const eocd = archiveBytes(EMPTY_ZIP_BASE64);
    const eocdView = new DataView(eocd.buffer);
    eocdView.setUint16(8, 0xffff, true);
    eocdView.setUint16(10, 0xffff, true);
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/export-workbooks") {
        return Promise.resolve({
          data: new Blob([eocd], { type: "application/zip" }),
          headers: { "content-type": "application/zip" },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "服务器返回的文件使用了暂不支持的 ZIP64 格式，已取消下载，请联系管理员",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("大 ZIP 结构校验只读取尾部和目录切片，不把整个归档载入内存", async () => {
    const archive = storedZipBlob("file.txt", new Uint8Array(1024 * 1024));
    const originalSlice = archive.slice.bind(archive);
    const slice = vi.fn((
      start?: number,
      end?: number,
      contentType?: string,
    ) => originalSlice(start, end, contentType));
    Object.defineProperty(archive, "slice", { configurable: true, value: slice });
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/export-workbooks") {
        return Promise.resolve({
          data: archive,
          headers: { "content-type": "application/zip" },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(1));
    const readSizes = slice.mock.calls.map(([start = 0, end = archive.size]) => end - start);
    expect(Math.max(...readSizes)).toBeLessThan(70 * 1024);
    expect(readSizes.reduce((total, size) => total + size, 0)).toBeLessThan(70 * 1024);
    expect(slice).not.toHaveBeenCalledWith(0, archive.size);
  });

  it("长下载在会话切换并卸载页面后中止，迟到的管理员文件不得落盘", async () => {
    localStorage.setItem("token", "admin-token");
    const pending = deferred<{
      data: Blob;
      headers: Record<string, string>;
    }>();
    let requestSignal: AbortSignal | undefined;
    get.mockImplementation((
      path: string,
      config?: { signal?: AbortSignal },
    ) => {
      if (path === "/maintenance/export-workbooks") {
        requestSignal = config?.signal;
        return pending.promise;
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const { unmount } = render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));
    await waitFor(() => expect(requestSignal).toBeInstanceOf(AbortSignal));

    localStorage.setItem("token", "restricted-token");
    unmount();
    expect(requestSignal?.aborted).toBe(true);

    await act(async () => {
      pending.resolve({
        data: new Blob(
          [new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x01])],
          { type: "application/zip" },
        ),
        headers: { "content-type": "application/zip" },
      });
      await pending.promise;
    });

    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("长下载落盘前发现账号已切换时拒绝旧响应并提示重新操作", async () => {
    localStorage.setItem("token", "admin-token");
    const pending = deferred<{
      data: Blob;
      headers: Record<string, string>;
    }>();
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/orders/export") return pending.promise;
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/orders/export",
      expect.anything(),
    ));
    localStorage.setItem("token", "restricted-token");

    await act(async () => {
      pending.resolve({
        data: new Blob(
          [new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x01])],
          { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
        ),
        headers: {
          "content-type":
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
      });
      await pending.promise;
    });

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "登录账号已变更，旧会话下载已取消，请重新操作",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("相对日期等待业务日时切换账号，不得以新会话继续请求或落盘旧操作", async () => {
    localStorage.setItem("token", "admin-token");
    const asOfPending = deferred<{ data: { as_of: string } }>();
    let asOfSignal: AbortSignal | undefined;
    get.mockImplementation((
      path: string,
      config?: { signal?: AbortSignal },
    ) => {
      if (path === "/maintenance/as-of") {
        asOfSignal = config?.signal;
        return asOfPending.promise;
      }
      if (path === "/maintenance/export-workbooks") return Promise.resolve(zipDownload());
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("近7天"));
    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));
    await waitFor(() => expect(get.mock.calls.filter(([path]) => path === "/maintenance/as-of"))
      .toHaveLength(1));

    localStorage.setItem("token", "restricted-token");
    await act(async () => {
      asOfPending.resolve({ data: { as_of: "2026-07-17" } });
      await asOfPending.promise;
    });

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "登录账号已变更，旧会话下载已取消，请重新操作",
    ));
    expect(asOfSignal?.aborted).toBe(true);
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/export-workbooks"))
      .toHaveLength(0);
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("同一事件栈内切换账号时日期请求仍绑定点击时会话", async () => {
    localStorage.setItem("token", "admin-token");
    const asOfPending = deferred<{ data: { as_of: string } }>();
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/as-of") return asOfPending.promise;
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByText("近7天"));
    localStorage.setItem("token", "restricted-token");

    const asOfCall = get.mock.calls.find(([path]) => path === "/maintenance/as-of");
    expect(asOfCall?.[1]?.headers).toEqual({
      Authorization: "Bearer admin-token",
    });
    await act(async () => {
      asOfPending.resolve({ data: { as_of: "2026-07-17" } });
      await asOfPending.promise;
    });
    expect(asOfCall?.[1]?.signal?.aborted).toBe(true);
  });

  it("会话 token 缺失时下载请求不伪造 Bearer null", async () => {
    localStorage.removeItem("token");
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/orders/export") return Promise.resolve(xlsxDownload());
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => {
      const call = get.mock.calls.find(([path]) => path === "/maintenance/orders/export");
      expect(call?.[1]?.headers).toBeUndefined();
    });
  });

  it("相对日期等待业务日时卸载页面，会中止日期请求且不继续请求或落盘", async () => {
    const asOfPending = deferred<{ data: { as_of: string } }>();
    let asOfSignal: AbortSignal | undefined;
    get.mockImplementation((
      path: string,
      config?: { signal?: AbortSignal },
    ) => {
      if (path === "/maintenance/as-of") {
        asOfSignal = config?.signal;
        return asOfPending.promise;
      }
      if (path === "/maintenance/export-workbooks") return Promise.resolve(zipDownload());
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const { unmount } = render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("近7天"));
    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));
    await waitFor(() => expect(asOfSignal).toBeInstanceOf(AbortSignal));

    unmount();
    expect(asOfSignal?.aborted).toBe(true);
    await act(async () => {
      asOfPending.resolve({ data: { as_of: "2026-07-17" } });
      await asOfPending.promise;
    });

    expect(get.mock.calls.filter(([path]) => path === "/maintenance/export-workbooks"))
      .toHaveLength(0);
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("异步文件签名校验期间切换账号时仍拒绝旧会话文件落盘", async () => {
    localStorage.setItem("token", "admin-token");
    const validationPending = deferred<void>();
    const workbookBytes = archiveBytes(VALID_XLSX_BASE64);
    const workbook = xlsxDownload().data;
    const originalSlice = workbook.slice.bind(workbook);
    let delayNextRead = true;
    const slice = vi.fn((
      start = 0,
      end = workbook.size,
      contentType?: string,
    ) => {
      const actual = originalSlice(start, end, contentType);
      if (!delayNextRead) return actual;
      delayNextRead = false;
      Object.defineProperty(actual, "arrayBuffer", {
        configurable: true,
        value: async () => {
          await validationPending.promise;
          return workbookBytes.slice(start, end).buffer;
        },
      });
      return actual;
    });
    Object.defineProperty(workbook, "slice", { configurable: true, value: slice });
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/orders/export") {
        return Promise.resolve({
          data: workbook,
          headers: {
            "content-type":
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));
    await waitFor(() => expect(slice).toHaveBeenCalled());
    localStorage.setItem("token", "restricted-token");

    await act(async () => {
      validationPending.resolve(undefined);
      await validationPending.promise;
    });

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "登录账号已变更，旧会话下载已取消，请重新操作",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("异步文件结构校验期间卸载页面时立即停止后续成员扫描且不落盘", async () => {
    const validationPending = deferred<void>();
    const archiveBytesValue = archiveBytes(VALID_ZIP_BASE64);
    const archive = zipDownload().data;
    const originalSlice = archive.slice.bind(archive);
    let delayNextRead = true;
    const slice = vi.fn((
      start = 0,
      end = archive.size,
      contentType?: string,
    ) => {
      const actual = originalSlice(start, end, contentType);
      if (!delayNextRead) return actual;
      delayNextRead = false;
      Object.defineProperty(actual, "arrayBuffer", {
        configurable: true,
        value: async () => {
          await validationPending.promise;
          return archiveBytesValue.slice(start, end).buffer;
        },
      });
      return actual;
    });
    Object.defineProperty(archive, "slice", { configurable: true, value: slice });
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/export-workbooks") {
        return Promise.resolve({
          data: archive,
          headers: { "content-type": "application/zip" },
        });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const { unmount } = render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));
    await waitFor(() => expect(slice).toHaveBeenCalled());
    unmount();

    await act(async () => {
      validationPending.resolve(undefined);
      await validationPending.promise;
    });

    await waitFor(() => expect(slice).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled());
  });

  it("订单大文件生成状态覆盖完整 Promise 生命周期并阻止重复点击", async () => {
    installSuccessResponses();
    const pending = deferred<{ data: Blob }>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-16" } });
      if (path === "/maintenance/orders/export") return pending.promise;
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    const button = screen.getByRole("button", { name: "导出订单汇总 Excel" });

    act(() => {
      button.click();
      button.click();
    });

    expect(await screen.findByRole("status")).toHaveTextContent(
      "正在生成订单汇总 Excel，请勿关闭页面或重复点击",
    );
    expect(button).toBeDisabled();
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/orders/export")).toHaveLength(1);

    pending.resolve(xlsxDownload());
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
    expect(button).toBeEnabled();
  });

  it("403 JSON Blob 错误优先展示后端 detail", async () => {
    installSuccessResponses();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/orders/export") return Promise.reject({
        response: {
          status: 403,
          data: new Blob([JSON.stringify({ detail: "无维保页面权限" })], { type: "application/json" }),
        },
      });
      return Promise.reject(new Error("unexpected"));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith("无维保页面权限"));
  });

  it("422 JSON Blob 错误展示后端校验 detail", async () => {
    installSuccessResponses();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/orders/export") return Promise.reject({
        response: {
          status: 422,
          data: new Blob([JSON.stringify({ detail: "订单明细超过 Excel 单 Sheet 数据行上限 1048575" })],
            { type: "application/json" }),
        },
      });
      return Promise.reject(new Error("unexpected"));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "订单明细超过 Excel 单 Sheet 数据行上限 1048575",
    ));
  });

  it("422 JSON Blob 的 FastAPI 数组型 detail 转成准确的中文参数提示", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/export") return Promise.reject({
        response: {
          status: 422,
          data: new Blob([JSON.stringify({
            detail: [{
              type: "string_too_long",
              loc: ["query", "q"],
              msg: "String should have at most 128 characters",
              ctx: { max_length: 128 },
            }],
          })], { type: "application/json" }),
        },
      });
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出项目成本 CSV" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "项目搜索不能超过 128 个字符",
    ));
  });

  it("413 JSON Blob 显示资源上限原因并引导改用单合同回填模板", async () => {
    installSuccessResponses();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-16" } });
      if (path === "/maintenance/roundtrip-template") return Promise.reject({
        response: {
          status: 413,
          data: new Blob([
            JSON.stringify({ detail: "模板数据超过全局资源上限，请改用单合同模板" }),
          ], { type: "application/json" }),
        },
      });
      return Promise.reject(new Error("unexpected"));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "下载固定回填模板" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "模板数据超过全局资源上限，请改用单合同模板",
    ));
    expect(await screen.findAllByText(
      "模板数据超过全局资源上限，请改用单合同模板",
    )).not.toHaveLength(0);
  });

  it("404 JSON Blob 准确提示对象不存在且不保存空文件", async () => {
    installSuccessResponses();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-16" } });
      if (path === "/maintenance/lines/export") return Promise.reject({
        response: {
          status: 404,
          data: new Blob([
            JSON.stringify({ detail: "项目不存在：不存在项目" }),
          ], { type: "application/json" }),
        },
      });
      return Promise.reject(new Error("unexpected"));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.change(screen.getByLabelText("单项目名称"), {
      target: { value: "不存在项目" },
    });

    fireEvent.click(screen.getByRole("button", { name: "导出单项目明细 CSV" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "项目不存在：不存在项目",
    ));
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("批量工作簿 422 JSON Blob 错误优先展示后端 detail", async () => {
    installSuccessResponses();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/export-workbooks") return Promise.reject({
        response: {
          status: 422,
          data: new Blob([JSON.stringify({ detail: "范围内全部订单均未关联合同" })],
            { type: "application/json" }),
        },
      });
      return Promise.reject(new Error("unexpected"));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith("范围内全部订单均未关联合同"));
  });

  it("批量工作簿 403 JSON Blob 错误优先展示后端 detail", async () => {
    installSuccessResponses();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/export-workbooks") return Promise.reject({
        response: {
          status: 403,
          data: new Blob([JSON.stringify({ detail: "无成本查看权限，不能批量导出项目工作簿" })],
            { type: "application/json" }),
        },
      });
      return Promise.reject(new Error("unexpected"));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "无成本查看权限，不能批量导出项目工作簿",
    ));
  });

  it("批量工作簿 429 JSON Blob 展示并发占用提示", async () => {
    installSuccessResponses();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/export-workbooks") return Promise.reject({
        response: {
          status: 429,
          data: new Blob([JSON.stringify({ detail: "已有批量工作簿导出正在执行，请稍后重试" })],
            { type: "application/json" }),
        },
      });
      return Promise.reject(new Error("unexpected"));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "已有批量工作簿导出正在执行，请稍后重试",
    ));
  });

  it("批量工作簿普通 500 错误只展示安全通用提示", async () => {
    installSuccessResponses();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/export-workbooks") {
        return Promise.reject({ response: { status: 500, data: new Blob(["internal secret"]) } });
      }
      return Promise.reject(new Error("unexpected"));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "批量项目工作簿导出失败，请稍后重试",
    ));
  });

  it("普通 500 错误使用通用导出失败提示", async () => {
    installSuccessResponses();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("进行中项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/orders/export") return Promise.reject({ response: { status: 500 } });
      return Promise.reject(new Error("unexpected"));
    });
    const errorMessage = vi.spyOn(message, "error");
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith("导出失败，请稍后重试"));
  });

  it("范围导出的下载文件名使用实际起止日期", async () => {
    installSuccessResponses();
    let downloadedName = "";
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      downloadedName = this.download;
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("近7天"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(downloadedName).toBe(
      "maintenance_orders_2026-07-10_2026-07-16.xlsx",
    ));
  });

  it("自定义 RangePicker 只更新导出范围且下载中心不加载项目视图", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("自定义"));
    const start = screen.getByPlaceholderText("Start date");
    const end = screen.getByPlaceholderText("End date");

    fireEvent.focus(start);
    fireEvent.change(start, { target: { value: "2026-07-03" } });
    fireEvent.keyDown(start, { key: "Enter", code: "Enter" });
    fireEvent.focus(end);
    fireEvent.change(end, { target: { value: "2026-07-19" } });
    fireEvent.keyDown(end, { key: "Enter", code: "Enter" });

    const expected = { date_from: "2026-07-03", date_to: "2026-07-19" };
    expect(get.mock.calls.filter(([path]) => (
      path === "/maintenance/projects" || path === "/maintenance/board"
    ))).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.objectContaining({
      params: expected,
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
  });

  it("下载中心项目成本 CSV 使用独立项目搜索和期限状态筛选", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.change(screen.getByLabelText("项目成本 CSV 项目搜索"), {
      target: { value: "联通项目" },
    });
    fireEvent.click(screen.getByRole("radio", { name: "已结束" }));
    fireEvent.click(screen.getByRole("button", { name: "导出项目成本 CSV" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/export", expect.objectContaining({
      params: {
        q: "联通项目",
        lifecycle: "ended",
      },
      responseType: "blob",
    })));
    expect(get.mock.calls.some(([path]) => (
      path === "/maintenance/projects" || path === "/maintenance/board"
    ))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/orders/export",
      expect.objectContaining({ params: {} }),
    ));
  });

  it("项目提醒搜索只下发详细盈亏看板，不加载项目事实", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="reminders" />);
    await screen.findByText("XSDD-1");

    const search = screen.getByPlaceholderText("搜索项目名");
    fireEvent.change(search, { target: { value: "联通项目" } });
    fireEvent.keyDown(search, { key: "Enter", code: "Enter" });

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/maintenance/board", expect.objectContaining({
        params: expect.objectContaining({ q: "联通项目", lifecycle: "ongoing" }),
      }));
    });
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/projects")).toHaveLength(0);

    const callsBeforeClear = get.mock.calls.length;
    fireEvent.change(search, { target: { value: "" } });
    await waitFor(() => {
      const newCalls = get.mock.calls.slice(callsBeforeClear);
      expect(newCalls).toEqual(expect.arrayContaining([
        ["/maintenance/board", expect.objectContaining({
          params: expect.objectContaining({ q: undefined, lifecycle: "ongoing" }),
        })],
      ]));
    });
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/projects")).toHaveLength(0);
  });

  it("项目提醒保留待补成本、待补费用、红黄绿和无预算六类筛选", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="reminders" />);
    await screen.findByText("XSDD-1");

    for (const label of [
      "全部提醒",
      "待补成本",
      "待补费用",
      "红色预警",
      "黄色关注",
      "绿色参考",
      "无预算",
    ]) {
      expect(screen.getByRole("radio", { name: label })).toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole("radio", { name: "待补成本" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/board",
      expect.objectContaining({
        params: expect.objectContaining({
          lifecycle: "ongoing",
          status: "incomplete_cost",
        }),
      }),
    ));
    await waitFor(() => {
      expect(screen.getByRole("radio", { name: "待补成本" })).toBeChecked();
    });
    expect(screen.queryByText("提醒类型筛选未被后端确认应用")).toBeNull();
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/projects")).toHaveLength(0);
  });

  it.each([
    ["false", false],
    ["缺失", undefined],
  ])("提醒状态 applied=%s 时回退全部并给出可见解释", async (_label, applied) => {
    get.mockImplementation((
      path: string,
      config?: { params?: { status?: string } },
    ) => {
      if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
      const response = board();
      return Promise.resolve({
        data: {
          ...response.data,
          ...(config?.params?.status
            ? { status_filter_applied: applied }
            : {}),
        },
      });
    });
    render(<ProjectCostPage view="reminders" />);
    await screen.findByText("XSDD-1");

    fireEvent.click(screen.getByRole("radio", { name: "红色预警" }));

    expect(await screen.findByText("提醒类型筛选未被后端确认应用"))
      .toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "全部提醒" })).toBeChecked();
  });

  it("提醒状态请求异常时回退全部并给出可见解释", async () => {
    get.mockImplementation((
      path: string,
      config?: { params?: { status?: string } },
    ) => {
      if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
      if (config?.params?.status) return Promise.reject(new Error("status failed"));
      return Promise.resolve(board());
    });
    render(<ProjectCostPage view="reminders" />);
    await screen.findByText("XSDD-1");

    fireEvent.click(screen.getByRole("radio", { name: "待补费用" }));

    expect(await screen.findByText("提醒类型筛选未被后端确认应用"))
      .toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "全部提醒" })).toBeChecked();
  });

  it("迟到的旧提醒响应不能撤销后一次已确认筛选", async () => {
    const red = deferred<{ data: ReturnType<typeof board>["data"] & {
      status_filter_applied?: boolean;
    } }>();
    const green = deferred<{ data: ReturnType<typeof board>["data"] & {
      status_filter_applied?: boolean;
    } }>();
    get.mockImplementation((
      path: string,
      config?: { params?: { status?: string } },
    ) => {
      if (path !== "/maintenance/board") return Promise.reject(new Error("unexpected"));
      if (config?.params?.status === "red") return red.promise;
      if (config?.params?.status === "green") return green.promise;
      return Promise.resolve(board());
    });
    render(<ProjectCostPage view="reminders" />);
    await screen.findByText("XSDD-1");

    fireEvent.click(screen.getByRole("radio", { name: "红色预警" }));
    fireEvent.click(screen.getByRole("radio", { name: "绿色参考" }));
    await act(async () => {
      const response = board("GREEN-CONTRACT");
      green.resolve({ data: { ...response.data, status_filter_applied: true } });
    });
    expect(await screen.findByText("GREEN-CONTRACT")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "绿色参考" })).toBeChecked();

    await act(async () => {
      const response = board("RED-LATE");
      red.resolve({ data: { ...response.data, status_filter_applied: false } });
    });
    expect(screen.getByRole("radio", { name: "绿色参考" })).toBeChecked();
    expect(screen.queryByText("RED-LATE")).toBeNull();
    expect(screen.queryByText("提醒类型筛选未被后端确认应用")).toBeNull();
  });

  it("受限账号的提醒状态筛选未应用时重置为全部并禁用经营判断筛选", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/board") {
        return Promise.resolve({
          data: {
            ...board(undefined, "ongoing").data,
            decision_restricted: true,
            status_filter_applied: false,
          },
        });
      }
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="reminders" />);

    expect(await screen.findByText("当前账号无权判断经营提醒类型")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "全部提醒" })).toBeChecked();
    for (const label of [
      "待补成本",
      "待补费用",
      "红色预警",
      "黄色关注",
      "绿色参考",
      "无预算",
    ]) {
      expect(screen.getByRole("radio", { name: label })).toBeDisabled();
    }
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/projects")).toHaveLength(0);
  });

  it("项目提醒按合同分页且翻页后展示后续提醒", async () => {
    const response = board("XSDD-1", "ongoing");
    const seed = response.data.rows[0];
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/board") {
        return Promise.resolve({
          data: {
            ...response.data,
            rows: Array.from({ length: 13 }, (_value, index) => ({
              ...seed,
              contract: `XSDD-${index + 1}`,
            })),
          },
        });
      }
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="reminders" />);

    expect(await screen.findByText("XSDD-1")).toBeInTheDocument();
    expect(screen.queryByText("XSDD-13")).toBeNull();
    const pagination = screen.getByLabelText("项目提醒分页");
    fireEvent.click(within(pagination).getByTitle("2"));
    expect(await screen.findByText("XSDD-13")).toBeInTheDocument();
    expect(screen.queryByText("XSDD-1")).toBeNull();
  });

  it("项目 CSV 慢请求防重复且不与主 XLSX 导出互锁", async () => {
    const csvPending = deferred<{ data: Blob }>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("当前项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/export") return csvPending.promise;
      if (path === "/maintenance/orders/export") return Promise.resolve(xlsxDownload());
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    const csvButton = screen.getByRole("button", { name: "导出项目成本 CSV" });
    const xlsxButton = screen.getByRole("button", { name: "导出订单汇总 Excel" });

    act(() => {
      csvButton.click();
      csvButton.click();
    });
    await waitFor(() => expect(csvButton).toBeDisabled());
    expect(xlsxButton).toBeEnabled();
    fireEvent.click(xlsxButton);

    await waitFor(() => expect(get.mock.calls.filter(([path]) => path === "/maintenance/export"))
      .toHaveLength(1));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.anything()));
    csvPending.resolve(csvDownload());
    await waitFor(() => expect(csvButton).toBeEnabled());
  });

  it("合同详细盈亏 CSV 同步双击只启动一个请求", async () => {
    const profitPending = deferred<{ data: Blob }>();
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/board/export") return profitPending.promise;
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    const button = screen.getByRole("button", { name: "导出合同详细盈亏 CSV" });

    act(() => {
      button.click();
      button.click();
    });

    await waitFor(() => expect(button).toBeDisabled());
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/board/export"))
      .toHaveLength(1);

    profitPending.resolve(csvDownload());
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("预设导出跨业务日时刷新 as_of、重算导出范围但不筛页面", async () => {
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("跨夜项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-17" } });
      if (path === "/maintenance/orders/export") return Promise.resolve(xlsxDownload());
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("近7天"));

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    const refreshed = { date_from: "2026-07-11", date_to: "2026-07-17" };
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/as-of",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.objectContaining({
      params: refreshed,
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
    for (const [path, config] of get.mock.calls.filter(([path]) => (
      path === "/maintenance/projects" || path === "/maintenance/board"
    ))) {
      expect(config?.params).not.toHaveProperty("date_from");
      expect(config?.params).not.toHaveProperty("date_to");
    }
  });

  it("相对日期业务日首次失败后提供可见重试且第二次成功", async () => {
    let asOfAttempts = 0;
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/as-of") {
        asOfAttempts += 1;
        return asOfAttempts === 1
          ? Promise.reject(new Error("temporary"))
          : Promise.resolve({ data: { as_of: "2026-07-17" } });
      }
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByText("近7天"));

    expect(await screen.findByText("日期基准加载失败，尚未应用日期范围。"))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试日期基准" }));

    expect(await screen.findByText("实际闭区间：2026-07-11 至 2026-07-17（含首尾）"))
      .toBeInTheDocument();
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/as-of")).toHaveLength(2);
  });

  it("批量工作簿相对日期档跨业务日时刷新 as_of 后再导出", async () => {
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("跨夜项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-17" } });
      if (path === "/maintenance/export-workbooks") return Promise.resolve(zipDownload());
      return Promise.reject(new Error("unexpected"));
    });
    let downloadedName = "";
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      downloadedName = this.download;
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("近7天"));

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    const refreshed = { date_from: "2026-07-11", date_to: "2026-07-17" };
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/as-of",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/export-workbooks", expect.objectContaining({
      params: refreshed,
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
    await waitFor(() => expect(downloadedName).toBe(
      "maintenance_project_workbooks_2026-07-11_2026-07-17.zip",
    ));
  });

  it("相对日期首次导出完成后再次刷新业务日，不永久复用已完成的范围请求", async () => {
    const firstAsOf = deferred<{ data: { as_of: string } }>();
    let asOfCalls = 0;
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/as-of") {
        asOfCalls += 1;
        return asOfCalls === 1
          ? firstAsOf.promise
          : Promise.resolve({ data: { as_of: "2026-07-17" } });
      }
      if (path === "/maintenance/export-workbooks") return Promise.resolve(zipDownload());
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("近7天"));
    const button = screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" });
    fireEvent.click(button);

    await act(async () => {
      firstAsOf.resolve({ data: { as_of: "2026-07-16" } });
      await firstAsOf.promise;
    });
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/export-workbooks",
      expect.objectContaining({
        params: { date_from: "2026-07-10", date_to: "2026-07-16" },
      }),
    ));
    await waitFor(() => expect(button).toBeEnabled());

    fireEvent.click(button);

    await waitFor(() => expect(get.mock.calls.filter(([path]) => path === "/maintenance/as-of"))
      .toHaveLength(2));
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/export-workbooks",
      expect.objectContaining({
        params: { date_from: "2026-07-11", date_to: "2026-07-17" },
      }),
    ));
  });

  it.each([
    ["今天", "2026-07-16", "2026-07-16"],
    ["近7天", "2026-07-10", "2026-07-16"],
    ["近14天", "2026-07-03", "2026-07-16"],
    ["近21天", "2026-06-26", "2026-07-16"],
    ["近30天", "2026-06-17", "2026-07-16"],
    ["本月", "2026-07-01", "2026-07-16"],
  ])("批量工作簿%s档使用后端业务日闭区间 %s 至 %s", async (label, dateFrom, dateTo) => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText(label));

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/export-workbooks", expect.objectContaining({
      params: { date_from: dateFrom, date_to: dateTo },
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
  });

  it("批量工作簿自定义范围精确导出且不额外请求 as_of", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("自定义"));
    const start = screen.getByPlaceholderText("Start date");
    const end = screen.getByPlaceholderText("End date");
    fireEvent.focus(start);
    fireEvent.change(start, { target: { value: "2026-07-03" } });
    fireEvent.keyDown(start, { key: "Enter", code: "Enter" });
    fireEvent.focus(end);
    fireEvent.change(end, { target: { value: "2026-07-19" } });
    fireEvent.keyDown(end, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(start).toHaveValue("2026-07-03"));

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/export-workbooks", expect.objectContaining({
      params: { date_from: "2026-07-03", date_to: "2026-07-19" },
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/as-of")).toHaveLength(0);
  });

  it("批量工作簿等待 as_of 时切换日期档会取消旧范围导出", async () => {
    const asOfPending = deferred<{ data: { as_of: string } }>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("当前项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return asOfPending.promise;
      if (path === "/maintenance/export-workbooks") return Promise.resolve(zipDownload());
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();
    fireEvent.click(screen.getByText("近7天"));
    const zipButton = screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" });
    fireEvent.click(zipButton);
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/as-of",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));

    fireEvent.click(screen.getByRole("radio", { name: "全部" }));
    asOfPending.resolve({ data: { as_of: "2026-07-17" } });

    await waitFor(() => expect(zipButton).toBeEnabled());
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/export-workbooks")).toHaveLength(0);
  });

  it("全部档和自定义档导出不额外请求 as_of", async () => {
    installSuccessResponses();
    render(<ProjectCostPage view="downloads" />);
    await waitForDownloadsReady();

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.anything()));
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/as-of")).toHaveLength(0);

    fireEvent.click(screen.getByText("自定义"));
    fireEvent.focus(screen.getByPlaceholderText("Start date"));
    fireEvent.change(screen.getByPlaceholderText("Start date"), { target: { value: "2026-07-03" } });
    fireEvent.keyDown(screen.getByPlaceholderText("Start date"), { key: "Enter", code: "Enter" });
    fireEvent.focus(screen.getByPlaceholderText("End date"));
    fireEvent.change(screen.getByPlaceholderText("End date"), { target: { value: "2026-07-19" } });
    fireEvent.keyDown(screen.getByPlaceholderText("End date"), { key: "Enter", code: "Enter" });
    await waitFor(() => expect(screen.getByPlaceholderText("Start date")).toHaveValue("2026-07-03"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.objectContaining({
      params: { date_from: "2026-07-03", date_to: "2026-07-19" },
      responseType: "blob",
      signal: expect.any(AbortSignal),
    })));
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/as-of")).toHaveLength(0);
  });

  it("非默认期限无结果时说明是当前筛选为空，不误报尚未导入", async () => {
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/board") {
        return Promise.resolve({ data: { ...board("", lifecycle).data, rows: [] } });
      }
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage view="reminders" />);
    await screen.findByText("当前筛选暂无提醒");

    fireEvent.click(screen.getByText("已结束 4"));
    expect(await screen.findByText("当前筛选暂无提醒")).toBeInTheDocument();
    expect(screen.queryByText("暂无数据（导入维保出库后自动生成）")).toBeNull();
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/projects")).toHaveLength(0);
    });
  });

  it("项目事实迟到响应不能覆盖详细盈亏的期限计数和业务日", async () => {
    const pendingProjects = deferred<ReturnType<typeof projects>>();
    const pendingBoard = deferred<ReturnType<typeof board>>();
    get.mockImplementation((path: string) => {
      if (path === "/maintenance/projects") return pendingProjects.promise;
      if (path === "/maintenance/board") return pendingBoard.promise;
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);

    const boardResponse = board("XSDD-BOARD-ASOF", "ongoing", "2026-07-20");
    boardResponse.data.lifecycle_counts = { ongoing: 9, ended: 8, missing: 7 };
    await act(async () => {
      pendingBoard.resolve(boardResponse);
    });
    expect(await screen.findByText("详细盈亏截止 2026-07-20")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "进行中 9" })).toBeInTheDocument();

    const projectResponse = projects("迟到项目事实", "ongoing", "2026-07-01");
    projectResponse.data.lifecycle_counts = { ongoing: 1, ended: 2, missing: 3 };
    await act(async () => {
      pendingProjects.resolve(projectResponse);
    });

    expect(await screen.findByText("项目事实截止 2026-07-01")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "进行中 9" })).toBeInTheDocument();
    expect(screen.getByText("项目事实期限：进行中 1 · 已结束 2 · 缺失 3"))
      .toBeInTheDocument();
  });

describe("项目维保管理员统一口径", () => {
  it("采用服务端维保口径且普通页面没有个人切换入口", async () => {
    localStorage.setItem("maintenance_project_profit_basis", "ex");
    getSystemSettings.mockResolvedValue({
      data: {
        purchase_display_basis: "both",
        sales_display_basis: "ex",
        maintenance_display_basis: "inc",
        version: 2,
        updated_by: "admin",
        updated_at: "2026-07-28T20:00:00+08:00",
      },
    });
    installSuccessResponses();
    render(<TaxBasisProvider><ProjectCostPage /></TaxBasisProvider>);

    await screen.findByText("由管理员在系统设置中统一配置，普通员工不能临时切换。");
    expect(screen.getByTestId("maintenance-margin-card-inc")).toBeInTheDocument();
    expect(screen.queryByTestId("maintenance-margin-card-ex")).toBeNull();
    expect(screen.queryByRole("radiogroup", { name: "合同级毛利展示口径" })).toBeNull();
    expect(screen.queryByRole("button", { name: "恢复管理员默认" })).toBeNull();
  });

  it("统一策略读取失败时使用维保双列安全默认且不读取旧个人偏好", async () => {
    localStorage.setItem("maintenance_project_profit_basis", "ex");
    getSystemSettings.mockRejectedValue(new Error("network down"));
    installSuccessResponses();
    render(<TaxBasisProvider><ProjectCostPage /></TaxBasisProvider>);

    expect(await screen.findByTestId("maintenance-margin-card-inc")).toBeInTheDocument();
    expect(screen.getByTestId("maintenance-margin-card-ex")).toBeInTheDocument();
  });

  it("合同卡展示完整 flat 字段，估算及阻断状态清楚且空值不伪造为零", async () => {
    installSuccessResponses();
    const originalImplementation = get.getMockImplementation();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      if (path === "/maintenance/board") {
        const response = board();
        Object.assign(response.data.rows[0], {
          parts_profit_status_inc: "complete_estimated",
          parts_profit_status_ex: "complete_actual",
          contribution_status_inc: "expense_data_unavailable",
          contribution_status_ex: "expense_tax_unknown",
          contribution_profit_ex: null,
          contribution_margin_ex: null,
        });
        return Promise.resolve(response);
      }
      return originalImplementation?.(path, config);
    });
    render(<ProjectCostPage />);

    const inc = await screen.findByTestId("maintenance-margin-card-inc");
    const ex = screen.getByTestId("maintenance-margin-card-ex");
    expect(within(inc).getByText("含估算")).toBeInTheDocument();
    expect(within(inc).getByText("费用数据未就绪")).toBeInTheDocument();
    expect(inc).toHaveTextContent("合同收入 ¥1,060");
    expect(inc).toHaveTextContent("备件成本 ¥226");
    expect(inc).toHaveTextContent("合同级备件毛利");
    expect(inc).toHaveTextContent("¥834 · 78.68%");
    expect(within(ex).getByText("费用税务口径缺失")).toBeInTheDocument();
    expect(ex).toHaveTextContent("合同级贡献毛利");
    expect(ex).toHaveTextContent("贡献毛利 - · -");
    expect(ex).not.toHaveTextContent("贡献毛利 ¥0");
    expect(screen.queryByRole("columnheader", { name: /项目毛利/ })).toBeNull();
  });

  it("费用证据完整时只从 contribution 字段展示项目贡献毛利", async () => {
    installSuccessResponses();
    const originalImplementation = get.getMockImplementation();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      if (path === "/maintenance/board") {
        const response = board();
        Object.assign(response.data.rows[0], {
          parts_profit_status_inc: "complete_actual",
          contribution_profit_inc: 784,
          contribution_margin_inc: 0.7396,
          contribution_status_inc: "complete",
        });
        return Promise.resolve(response);
      }
      return originalImplementation?.(path, config);
    });
    render(<ProjectCostPage />);

    const inc = await screen.findByTestId("maintenance-margin-card-inc");
    expect(within(inc).getByText("完整")).toBeInTheDocument();
    expect(inc).toHaveTextContent("贡献毛利 ¥784 · 73.96%");
  });

  it("阻断或未知状态即使夹带脏数字也不展示毛利结论", async () => {
    installSuccessResponses();
    const originalImplementation = get.getMockImplementation();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      if (path === "/maintenance/board") {
        const response = board();
        Object.assign(response.data.rows[0], {
          parts_profit_status_inc: "ambiguous_revenue",
          contribution_status_inc: "complete",
          revenue_inc: 999,
          parts_gross_profit_inc: 998,
          parts_gross_margin_inc: 0.998,
          contribution_profit_inc: 997,
          contribution_margin_inc: 0.997,
          parts_profit_status_ex: "complete_actual",
          contribution_status_ex: "future_unknown_status",
          revenue_ex: 899,
          parts_cost_ex_tax: 898,
          parts_gross_profit_ex: 897,
          parts_gross_margin_ex: 0.897,
          contribution_profit_ex: 896,
          contribution_margin_ex: 0.896,
        });
        return Promise.resolve(response);
      }
      return originalImplementation?.(path, config);
    });
    render(<ProjectCostPage />);

    const inc = await screen.findByTestId("maintenance-margin-card-inc");
    const ex = screen.getByTestId("maintenance-margin-card-ex");
    expect(within(inc).getByText("合同收入冲突")).toBeInTheDocument();
    expect(inc).not.toHaveTextContent("¥999");
    expect(inc).not.toHaveTextContent("¥998");
    expect(inc).not.toHaveTextContent("¥997");
    expect(within(ex).getByText("结果未提供")).toBeInTheDocument();
    expect(ex).toHaveTextContent("合同收入 ¥899");
    expect(ex).toHaveTextContent("备件成本 ¥898");
    expect(ex).toHaveTextContent("¥897 · 89.70%");
    expect(ex).not.toHaveTextContent("¥896");
  });

  it("重复 XSDD 收入冲突时明确阻断毛利，不把任一历史金额当收入", async () => {
    installSuccessResponses();
    const originalImplementation = get.getMockImplementation();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      if (path === "/maintenance/board") {
        const response = board();
        Object.assign(response.data.rows[0], {
          revenue_inc: null,
          parts_gross_profit_inc: null,
          parts_gross_margin_inc: null,
          parts_profit_status_inc: "ambiguous_revenue",
          contribution_profit_inc: null,
          contribution_margin_inc: null,
          contribution_status_inc: "ambiguous_revenue",
        });
        return Promise.resolve(response);
      }
      return originalImplementation?.(path, config);
    });
    render(<ProjectCostPage />);

    const inc = await screen.findByTestId("maintenance-margin-card-inc");
    expect(within(inc).getByText("合同收入冲突")).toBeInTheDocument();
    expect(inc).toHaveTextContent("合同收入 -");
    expect(inc).not.toHaveTextContent("合同收入 ¥0");
  });
});
