import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";

const get = vi.fn();
const post = vi.fn();

vi.mock("../../api", () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}));

import ProjectCostPage from "../ProjectCostPage";

type Lifecycle = "ongoing" | "ended" | "missing" | "all";

const counts = { ongoing: 2, ended: 4, missing: 1 };

function projects(project = "进行中项目", lifecycle: Lifecycle = "ongoing") {
  return {
    data: {
      rows: [{
        project,
        lines: 1,
        qty: 2,
        cost_inc: 100,
        cost_ex: 88.5,
        cost_total: 100,
        coverage_pct: 100,
        by_source: { direct: 1 },
        months: 1,
        sales_orders: ["XSDD-1"],
        contract_amount: 1000,
        contract_shared: false,
        contract_incomplete: false,
        maint_end: lifecycle === "missing" ? null : lifecycle === "ended" ? "2026-07-15" : "2026-07-16",
        lifecycle_status: lifecycle === "all" ? "ongoing" : lifecycle,
      }],
      start_date: "2026-01-01",
      as_of: "2026-07-16",
      lifecycle_filter: lifecycle,
      lifecycle_counts: counts,
    },
  };
}

function board(contract = "XSDD-1", lifecycle: Lifecycle = "ongoing") {
  return {
    data: {
      rows: [{
        contract,
        status: "green",
        projects: [{ project: `${contract}项目`, lines: 1, spent_parts: 100 }],
        lines: 1,
        coverage_pct: 100,
        spent_parts: 100,
        spent_expense: 0,
        spent: 100,
        budget: 1000,
        remaining: 900,
        remaining_pct: 90,
        low_conf_pct: 0,
        maint_start: "2026-01-01",
        maint_end: lifecycle === "missing" ? null : lifecycle === "ended" ? "2026-07-15" : "2026-07-16",
        lifecycle_status: lifecycle === "all" ? "ongoing" : lifecycle,
        first_out: "2026-01-01",
        last_out: "2026-07-01",
      }],
      profit_restricted: false,
      as_of: "2026-07-16",
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
    if (path === "/maintenance/export") return Promise.resolve({ data: new Blob(["ok"]) });
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
  message.destroy();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:test") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
});

afterEach(() => {
  cleanup();
  message.destroy();
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
    expect(screen.getByText("健康")).toBeInTheDocument();
    expect(screen.getByText("2026-07-15")).toBeInTheDocument();
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

  it("导出使用当前生命周期筛选", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    fireEvent.click(screen.getByText("期限缺失 1"));
    await screen.findByText("期限缺失项目");
    fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));

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
