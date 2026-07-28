import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { message } from "antd";

const get = vi.fn();
const post = vi.fn();

vi.mock("../../api", () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}));

import ProjectCostPage, { buildOrderExportParams } from "../ProjectCostPage";

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
        budget: nullable(1000),
        remaining: nullable(900),
        remaining_pct: nullable(90),
        low_conf_pct: nullable(0),
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
  get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
    const lifecycle = config?.params?.lifecycle ?? "ongoing";
    const label = lifecycle === "ended" ? "已结束" : lifecycle === "missing" ? "期限缺失" : "进行中";
    if (path === "/maintenance/projects") return Promise.resolve(projects(`${label}项目`, lifecycle));
    if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
    if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-16" } });
    if (path === "/maintenance/orders/export") return Promise.resolve({ data: new Blob(["ok"]) });
    if (path === "/maintenance/export-workbooks") return Promise.resolve({ data: new Blob(["zip"]) });
    return Promise.resolve({ data: { rows: [], total: 0 } });
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "readonly");
  localStorage.setItem("permissions", JSON.stringify({
    page_maintenance: true,
    data_purchase_cost: true,
    data_profit: true,
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
  vi.restoreAllMocks();
});

describe("维保项目生命周期筛选", () => {
  it("默认只请求进行中，并常驻展示三类计数和日期边界口径", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);

    expect(await screen.findByText("进行中项目")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/maintenance/projects", expect.objectContaining({
      params: expect.objectContaining({ lifecycle: "ongoing" }),
    }));
    expect(get).toHaveBeenCalledWith("/maintenance/board", expect.objectContaining({
      params: expect.objectContaining({ lifecycle: "ongoing" }),
    }));
    expect(screen.getByText("进行中 2")).toBeInTheDocument();
    expect(screen.getByText("已结束 4")).toBeInTheDocument();
    expect(screen.getByText("期限缺失 1")).toBeInTheDocument();
    expect(screen.getByText(/日期范围筛选出库日期/)).toHaveTextContent("维保期限状态按 2026-07-16 判断");
    expect(screen.getByText(/终止日当天仍算进行中/)).toBeInTheDocument();
  });

  it("375px 窄屏用可收缩换行父容器且只让八档日期控件横向滚动", async () => {
    installSuccessResponses();
    render(<div style={{ width: 375 }}><ProjectCostPage /></div>);
    await screen.findByText("进行中项目");

    const segmented = screen.getByRole("radiogroup", { name: "维保订单导出日期" });
    const dateScroller = segmented.parentElement;
    const controls = dateScroller?.parentElement;
    expect(dateScroller).toHaveStyle({
      width: "100%", minWidth: "0", maxWidth: "100%", overflowX: "auto",
    });
    expect(controls).toHaveStyle({
      display: "flex", flexWrap: "wrap", width: "100%", minWidth: "0",
    });
    expect(controls).not.toHaveClass("ant-space-item");
    expect(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }).parentElement).toBe(controls);
    expect(screen.getByRole("button", { name: "导出订单汇总 Excel" }).parentElement).toBe(controls);
    expect(screen.getByLabelText("批量导出说明").parentElement).toBe(controls);
  });

  it("清楚区分批量完整工作簿、订单汇总、当前项目统计和单本工作簿", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

    const zipButton = screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" });
    const summaryButton = screen.getByRole("button", { name: "导出订单汇总 Excel" });
    expect(zipButton).toHaveClass("ant-btn-primary");
    expect(summaryButton).not.toHaveClass("ant-btn-primary");
    expect(screen.getByRole("button", { name: "导出当前项目统计 CSV" })).toBeInTheDocument();
    expect(screen.getByText("单本工作簿")).toBeInTheDocument();
    expect(screen.getByText(/时间范围只决定纳入哪些合同/))
      .toHaveTextContent("每本仍包含该合同完整数据");
    expect(screen.getByText(/批量导出不受项目搜索或维保期限筛选影响/)).toBeInTheDocument();
  });

  it.each([
    ["缺成本权限", { data_purchase_cost: false, data_profit: true, own_customers_only: false }],
    ["缺利润权限", { data_purchase_cost: true, data_profit: false, own_customers_only: false }],
    ["受限销售", { data_purchase_cost: true, data_profit: true, own_customers_only: true }],
  ])("%s 时隐藏批量和单本工作簿入口", async (_label, permissions) => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      ...permissions,
    }));
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

    expect(screen.queryByRole("button", { name: "批量导出项目工作簿 ZIP" })).toBeNull();
    expect(screen.queryByText("单本工作簿")).toBeNull();
    expect(screen.getByRole("button", { name: "导出订单汇总 Excel" })).toBeInTheDocument();
  });

  it.each(["今天", "近7天", "近14天", "近21天", "近30天", "本月"])(
    "首次加载尚未取得 as_of 时禁用%s且保持全部档",
    (label) => {
      get.mockReturnValue(new Promise(() => undefined));
      render(<ProjectCostPage />);

      expect(screen.getByRole("radio", { name: label })).toBeDisabled();
      expect(screen.getByRole("radio", { name: "全部" })).toHaveAttribute("checked");
    },
  );

  it("切换期限筛选同时更新项目和看板请求，并把期限状态与盈亏状态分开显示", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

    fireEvent.click(screen.getByText("已结束 4"));
    expect(await screen.findByText("已结束项目")).toBeInTheDocument();
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/maintenance/projects", expect.objectContaining({
        params: expect.objectContaining({ lifecycle: "ended" }),
      }));
      expect(get).toHaveBeenCalledWith("/maintenance/board", expect.objectContaining({
        params: expect.objectContaining({ lifecycle: "ended" }),
      }));
    });
    expect(screen.getAllByText("已结束").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("预算余量 > 20%")).toBeInTheDocument();
    expect(screen.getByText("2026-07-15")).toBeInTheDocument();
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
    expect(within(card).getAllByText("成本不完整，需补数据")).toHaveLength(2);
    expect(card).toHaveTextContent("实际参考：含税 ¥100 / 不含税 ¥0");
    expect(card).toHaveTextContent("估算参考：含税 ¥40 / 不含税 ¥0");
    expect(card).not.toHaveTextContent("实际参考 ¥100");
    expect(card).not.toHaveTextContent("估算参考 ¥40");
    expect(card).not.toHaveTextContent("¥140");
    expect(card).toHaveTextContent("缺失 2 行");
    expect(within(card).queryByText(/剩余/)).toBeNull();
    expect(within(card).queryByText(/预算消耗/)).toBeNull();
    expect(within(card).queryByRole("progressbar")).toBeNull();
    expect(within(card).queryByText(/健康|亏损|超支/)).toBeNull();
    expect(screen.getByRole("radiogroup", { name: "预算消耗参考状态筛选" }))
      .toBeInTheDocument();
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
    expect(within(card).getAllByText("成本不完整，需补数据")).toHaveLength(2);
    expect(card).not.toHaveTextContent("预算余量 > 20%");
    expect(within(card).queryByText(/剩余预算/)).toBeNull();
    expect(within(card).queryByRole("progressbar")).toBeNull();
    expect(screen.getByText("待补成本 1")).toBeInTheDocument();
    expect(screen.getByText("🟢 0")).toBeInTheDocument();
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
    expect(within(card).getAllByText("成本不完整，需补数据")).toHaveLength(2);
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
    expect(card).toHaveTextContent("无预算(未关联合同额)");
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
      "实际采购参考（不含税）",
      "估算参考（含税）",
      "估算参考（不含税）",
      "缺失成本行",
    ]) {
      expect(screen.getByText(title).closest(".ant-statistic")).toHaveTextContent("—");
    }
    expect(screen.queryByRole("radiogroup", { name: "预算消耗参考状态筛选" })).toBeNull();
    expect(screen.getByText("当前账号不展示预算消耗决策分类与状态筛选"))
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
    expect(card).toHaveTextContent("实际参考：含税 ¥100 / 不含税 ¥0");
    expect(card).toHaveTextContent("缺失 0 行");
    expect(card).not.toHaveTextContent(/合同额参考|剩余预算|预算消耗参考状态/);
    expect(within(card).queryByRole("progressbar")).toBeNull();
    expect(screen.queryByRole("radiogroup", { name: "预算消耗参考状态筛选" })).toBeNull();
    expect(screen.getByText("当前账号不展示预算消耗决策分类与状态筛选"))
      .toBeInTheDocument();
    expect(screen.queryByText("¥1,000")).toBeNull();
  });

  it.each([
    ["显式 incomplete", "incomplete"],
    ["质量字段异常为空但其他成本事实可见", null],
  ])("利润权限受限且%s时仍 fail-closed 展示补数事实，决策外观保持中性",
    async (_label, costQuality) => {
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
          cost_quality: costQuality,
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
    expect(within(card).getByText("成本不完整，需补数据")).toBeInTheDocument();
    expect(card).toHaveTextContent("缺失 1 行");
    expect(within(card).queryByText(/剩余预算|预算余量|预算已用完/)).toBeNull();
    expect(within(card).queryByRole("progressbar")).toBeNull();
    expect(card).toHaveStyle({ borderLeft: "4px solid #8c8c8c" });
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
    expect(card).toHaveStyle({ borderLeft: "4px solid #8c8c8c" });
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

  it("旧筛选最后返回也不能覆盖新筛选", async () => {
    const oldProjects = deferred<ReturnType<typeof projects>>();
    const oldBoard = deferred<ReturnType<typeof board>>();
    const newProjects = deferred<ReturnType<typeof projects>>();
    const newBoard = deferred<ReturnType<typeof board>>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const latest = config?.params?.lifecycle === "ended";
      if (path === "/maintenance/projects") return latest ? newProjects.promise : oldProjects.promise;
      if (path === "/maintenance/board") return latest ? newBoard.promise : oldBoard.promise;
      return Promise.reject(new Error("unexpected"));
    });

    render(<ProjectCostPage />);
    fireEvent.click(screen.getByText("已结束 0"));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(4));
    newProjects.resolve(projects("最新筛选项目", "ended"));
    newBoard.resolve(board("最新合同", "ended"));
    expect(await screen.findByText("最新筛选项目")).toBeInTheDocument();

    oldProjects.resolve(projects("迟到旧项目", "ongoing"));
    oldBoard.resolve(board("迟到旧合同", "ongoing"));
    await waitFor(() => expect(screen.queryByText("迟到旧项目")).toBeNull());
    expect(screen.getByText("最新筛选项目")).toBeInTheDocument();
  });

  it("当前筛选失败会清空旧项目和卡片，显示可持续重试的错误状态", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    expect(await screen.findByText("进行中项目")).toBeInTheDocument();
    expect(screen.getByText("XSDD-1")).toBeInTheDocument();

    get.mockRejectedValue(new Error("network"));
    fireEvent.click(screen.getByText("已结束 4"));
    expect(await screen.findByText("项目成本加载失败，旧结果已清空。"))
      .toBeInTheDocument();
    expect(screen.queryByText("进行中项目")).toBeNull();
    expect(screen.queryByText("XSDD-1")).toBeNull();
    expect(screen.getByText("进行中 0")).toBeInTheDocument();
    expect(screen.getByText("已结束 0")).toBeInTheDocument();
    expect(screen.getByText("期限缺失 0")).toBeInTheDocument();
    expect(screen.getByText(/日期范围筛选出库日期/)).toHaveTextContent("维保期限状态按 后端请求当天 判断");
    expect(screen.getByRole("button", { name: /重\s*试/ })).toBeInTheDocument();

    const callsBeforeRetry = get.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(callsBeforeRetry));
  });

  it("全部档通过新端点导出且不携带页面期限或搜索参数", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: {},
      responseType: "blob",
    }));
  });

  it("全部档一键下载所有项目工作簿 ZIP 且不携带页面筛选参数", async () => {
    installSuccessResponses();
    let downloadedName = "";
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      downloadedName = this.download;
    });
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/export-workbooks", {
      params: {},
      responseType: "blob",
    }));
    await waitFor(() => expect(downloadedName).toBe("maintenance_project_workbooks_all.zip"));
  });

  it("批量工作簿 ZIP 慢请求防重复且不与其他导出互锁", async () => {
    const zipPending = deferred<{ data: Blob }>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("当前项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/export-workbooks") return zipPending.promise;
      if (path === "/maintenance/orders/export") return Promise.resolve({ data: new Blob(["xlsx"]) });
      if (path === "/maintenance/export") return Promise.resolve({ data: new Blob(["csv"]) });
      if (path === "/maintenance/export-workbook") return Promise.resolve({ data: new Blob(["single"]) });
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage />);
    await screen.findByText("当前项目");
    const zipButton = screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" });
    const orderButton = screen.getByRole("button", { name: "导出订单汇总 Excel" });
    const csvButton = screen.getByRole("button", { name: "导出当前项目统计 CSV" });

    fireEvent.click(zipButton);
    fireEvent.click(zipButton);

    await waitFor(() => expect(zipButton).toBeDisabled());
    expect(orderButton).toBeEnabled();
    expect(csvButton).toBeEnabled();
    fireEvent.click(orderButton);
    fireEvent.click(csvButton);
    fireEvent.click(screen.getByText("单本工作簿"));
    await waitFor(() => {
      expect(get.mock.calls.filter(([path]) => path === "/maintenance/export-workbooks")).toHaveLength(1);
      expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.anything());
      expect(get).toHaveBeenCalledWith("/maintenance/export", expect.anything());
      expect(get).toHaveBeenCalledWith("/maintenance/export-workbook", expect.anything());
    });

    zipPending.resolve({ data: new Blob(["zip"]) });
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
      if (path === "/maintenance/orders/export") return Promise.resolve({ data: new Blob(["ok"]) });
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage />);
    await screen.findByText("暂无数据（导入维保出库后自动生成）");

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
        return Promise.resolve({ data: new Blob(["zip"]) });
      }
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage />);
    await screen.findByText("暂无数据（导入维保出库后自动生成）");

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
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    const button = screen.getByRole("button", { name: "导出订单汇总 Excel" });

    fireEvent.click(button);

    await waitFor(() => expect(button).toBeDisabled());
    pending.resolve({ data: new Blob(["ok"]) });
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("近7天同步页面日期筛选并按含首尾的七天范围导出", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    const expected = {
      date_from: "2026-07-10",
      date_to: "2026-07-16",
    };

    fireEvent.click(screen.getByText("近7天"));

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/maintenance/projects", expect.objectContaining({
        params: expect.objectContaining(expected),
      }));
      expect(get).toHaveBeenCalledWith("/maintenance/board", expect.objectContaining({
        params: expect.objectContaining(expected),
      }));
    });
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: expected,
      responseType: "blob",
    }));
  });

  it("今天档使用今天作为同一个闭区间首尾", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    fireEvent.click(screen.getByText("今天"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: { date_from: "2026-07-16", date_to: "2026-07-16" },
      responseType: "blob",
    }));
  });

  it("近14天档从今天向前包含十四个自然日", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    fireEvent.click(screen.getByText("近14天"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: {
        date_from: "2026-07-03",
        date_to: "2026-07-16",
      },
      responseType: "blob",
    }));
  });

  it("近21天档从今天向前包含二十一个自然日", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    fireEvent.click(screen.getByText("近21天"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: {
        date_from: "2026-06-26",
        date_to: "2026-07-16",
      },
      responseType: "blob",
    }));
  });

  it("近30天档从今天向前包含三十个自然日", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    fireEvent.click(screen.getByText("近30天"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: {
        date_from: "2026-06-17",
        date_to: "2026-07-16",
      },
      responseType: "blob",
    }));
  });

  it("本月档从当月一日到今天", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    fireEvent.click(screen.getByText("本月"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: {
        date_from: "2026-07-01",
        date_to: "2026-07-16",
      },
      responseType: "blob",
    }));
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
      if (path === "/maintenance/orders/export") return Promise.resolve({ data: new Blob(["ok"]) });
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage />);
    await screen.findByText("跨月项目");

    fireEvent.click(screen.getByText("本月"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: { date_from: "2026-06-01", date_to: "2026-06-30" },
      responseType: "blob",
    }));
  });

  it("自定义缺少日期范围时不发送导出请求", async () => {
    installSuccessResponses();
    const warningMessage = vi.spyOn(message, "warning");
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

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
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

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
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(remove).toHaveBeenCalled());
    expect(document.querySelector('a[download="maintenance_orders_all.xlsx"]')).toBeNull();
    expect(window.setTimeout).toHaveBeenCalledWith(expect.any(Function), 100);
    await new Promise((resolve) => setTimeout(resolve, 110));
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
  });

  it("下载锚点 append 抛错也执行移除并最终延迟释放 Object URL", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
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
    let attachedWhenClicked = false;
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      if (this.download) attachedWhenClicked = document.body.contains(this);
    });
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

    fireEvent.click(screen.getByText("单本工作簿"));

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
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

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
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith(
      "订单明细超过 Excel 单 Sheet 数据行上限 1048575",
    ));
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
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

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
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

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
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

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
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

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
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith("导出失败，请稍后重试"));
  });

  it("范围导出的下载文件名使用实际起止日期", async () => {
    installSuccessResponses();
    let downloadedName = "";
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      downloadedName = this.download;
    });
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    fireEvent.click(screen.getByText("近7天"));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    await waitFor(() => expect(downloadedName).toBe(
      "maintenance_orders_2026-07-10_2026-07-16.xlsx",
    ));
  });

  it("自定义 RangePicker 的完整范围同步页面并精确导出", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
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
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/projects", expect.objectContaining({
      params: expect.objectContaining(expected),
    })));
    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: expected,
      responseType: "blob",
    }));
  });

  it("保留项目聚合 CSV 并继续使用当前生命周期筛选", async () => {
    installSuccessResponses();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("当前项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/export") return Promise.resolve({ data: new Blob(["csv"]) });
      return Promise.resolve({ data: new Blob(["xlsx"]) });
    });
    render(<ProjectCostPage />);
    await screen.findByText("当前项目");
    fireEvent.click(screen.getByText("期限缺失 1"));
    await screen.findByText("当前项目");

    fireEvent.click(screen.getByRole("button", { name: "导出当前项目统计 CSV" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/export", expect.objectContaining({
      params: expect.objectContaining({ lifecycle: "missing" }),
      responseType: "blob",
    })));
  });

  it("项目搜索同时下发项目表和合同看板，避免上下作用域不一致", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

    const search = screen.getByPlaceholderText("搜索项目名");
    fireEvent.change(search, { target: { value: "联通项目" } });
    fireEvent.keyDown(search, { key: "Enter", code: "Enter" });

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/maintenance/projects", expect.objectContaining({
        params: expect.objectContaining({ q: "联通项目", lifecycle: "ongoing" }),
      }));
      expect(get).toHaveBeenCalledWith("/maintenance/board", expect.objectContaining({
        params: expect.objectContaining({ q: "联通项目", lifecycle: "ongoing" }),
      }));
    });

    const callsBeforeClear = get.mock.calls.length;
    fireEvent.change(search, { target: { value: "" } });
    await waitFor(() => {
      const newCalls = get.mock.calls.slice(callsBeforeClear);
      expect(newCalls).toEqual(expect.arrayContaining([
        ["/maintenance/projects", expect.objectContaining({
          params: expect.objectContaining({ q: undefined, lifecycle: "ongoing" }),
        })],
        ["/maintenance/board", expect.objectContaining({
          params: expect.objectContaining({ q: undefined, lifecycle: "ongoing" }),
        })],
      ]));
    });
  });

  it("项目 CSV 慢请求防重复且不与主 XLSX 导出互锁", async () => {
    const csvPending = deferred<{ data: Blob }>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("当前项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/export") return csvPending.promise;
      if (path === "/maintenance/orders/export") return Promise.resolve({ data: new Blob(["xlsx"]) });
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage />);
    await screen.findByText("当前项目");
    const csvButton = screen.getByRole("button", { name: "导出当前项目统计 CSV" });
    const xlsxButton = screen.getByRole("button", { name: "导出订单汇总 Excel" });

    fireEvent.click(csvButton);
    await waitFor(() => expect(csvButton).toBeDisabled());
    expect(xlsxButton).toBeEnabled();
    fireEvent.click(csvButton);
    fireEvent.click(xlsxButton);

    await waitFor(() => expect(get.mock.calls.filter(([path]) => path === "/maintenance/export"))
      .toHaveLength(1));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", expect.anything()));
    csvPending.resolve({ data: new Blob(["csv"]) });
    await waitFor(() => expect(csvButton).toBeEnabled());
  });

  it("预设导出跨业务日时刷新 as_of、重算范围并同步页面筛选", async () => {
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("跨夜项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-17" } });
      if (path === "/maintenance/orders/export") return Promise.resolve({ data: new Blob(["xlsx"]) });
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage />);
    await screen.findByText("跨夜项目");
    fireEvent.click(screen.getByText("近7天"));

    fireEvent.click(screen.getByRole("button", { name: "导出订单汇总 Excel" }));

    const refreshed = { date_from: "2026-07-11", date_to: "2026-07-17" };
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/as-of"));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: refreshed,
      responseType: "blob",
    }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/projects", expect.objectContaining({
      params: expect.objectContaining(refreshed),
    })));
  });

  it("批量工作簿相对日期档跨业务日时刷新 as_of 后再导出", async () => {
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("跨夜项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return Promise.resolve({ data: { as_of: "2026-07-17" } });
      if (path === "/maintenance/export-workbooks") return Promise.resolve({ data: new Blob(["zip"]) });
      return Promise.reject(new Error("unexpected"));
    });
    let downloadedName = "";
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      downloadedName = this.download;
    });
    render(<ProjectCostPage />);
    await screen.findByText("跨夜项目");
    fireEvent.click(screen.getByText("近7天"));

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    const refreshed = { date_from: "2026-07-11", date_to: "2026-07-17" };
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/as-of"));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/export-workbooks", {
      params: refreshed,
      responseType: "blob",
    }));
    await waitFor(() => expect(downloadedName).toBe(
      "maintenance_project_workbooks_2026-07-11_2026-07-17.zip",
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
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    fireEvent.click(screen.getByText(label));

    fireEvent.click(screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/export-workbooks", {
      params: { date_from: dateFrom, date_to: dateTo },
      responseType: "blob",
    }));
  });

  it("批量工作簿自定义范围精确导出且不额外请求 as_of", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
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

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/export-workbooks", {
      params: { date_from: "2026-07-03", date_to: "2026-07-19" },
      responseType: "blob",
    }));
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/as-of")).toHaveLength(0);
  });

  it("批量工作簿等待 as_of 时切换日期档会取消旧范围导出", async () => {
    const asOfPending = deferred<{ data: { as_of: string } }>();
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") return Promise.resolve(projects("当前项目", lifecycle));
      if (path === "/maintenance/board") return Promise.resolve(board(undefined, lifecycle));
      if (path === "/maintenance/as-of") return asOfPending.promise;
      if (path === "/maintenance/export-workbooks") return Promise.resolve({ data: new Blob(["zip"]) });
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage />);
    await screen.findByText("当前项目");
    fireEvent.click(screen.getByText("近7天"));
    const zipButton = screen.getByRole("button", { name: "批量导出项目工作簿 ZIP" });
    fireEvent.click(zipButton);
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/as-of"));

    fireEvent.click(screen.getByRole("radio", { name: "全部" }));
    asOfPending.resolve({ data: { as_of: "2026-07-17" } });

    await waitFor(() => expect(zipButton).toBeEnabled());
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/export-workbooks")).toHaveLength(0);
  });

  it("全部档和自定义档导出不额外请求 as_of", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

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
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: { date_from: "2026-07-03", date_to: "2026-07-19" },
      responseType: "blob",
    }));
    expect(get.mock.calls.filter(([path]) => path === "/maintenance/as-of")).toHaveLength(0);
  });

  it("非默认期限无结果时说明是当前筛选为空，不误报尚未导入", async () => {
    get.mockImplementation((path: string, config?: { params?: { lifecycle?: Lifecycle } }) => {
      const lifecycle = config?.params?.lifecycle ?? "ongoing";
      if (path === "/maintenance/projects") {
        return Promise.resolve({ data: { ...projects("", lifecycle).data, rows: [] } });
      }
      if (path === "/maintenance/board") {
        return Promise.resolve({ data: { ...board("", lifecycle).data, rows: [] } });
      }
      return Promise.reject(new Error("unexpected"));
    });
    render(<ProjectCostPage />);
    await screen.findByText("暂无数据（导入维保出库后自动生成）");

    fireEvent.click(screen.getByText("已结束 4"));
    expect(await screen.findByText("当前筛选暂无合同，请调整项目、日期或期限状态"))
      .toBeInTheDocument();
    expect(screen.getByText("当前筛选无结果，请调整搜索、日期或期限状态"))
      .toBeInTheDocument();
  });
});
