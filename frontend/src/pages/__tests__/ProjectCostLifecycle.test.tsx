import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";
import dayjs from "dayjs";

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
    if (path === "/maintenance/orders/export") return Promise.resolve({ data: new Blob(["ok"]) });
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

  it("全部档通过新端点导出且不携带页面期限或搜索参数", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    fireEvent.click(screen.getByRole("button", { name: "导出维保订单 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: {},
      responseType: "blob",
    }));
  });

  it("近7天同步页面日期筛选并按含首尾的七天范围导出", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    const today = dayjs();
    const expected = {
      date_from: today.subtract(6, "day").format("YYYY-MM-DD"),
      date_to: today.format("YYYY-MM-DD"),
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
    fireEvent.click(screen.getByRole("button", { name: "导出维保订单 Excel" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: expected,
      responseType: "blob",
    }));
  });

  it("今天档使用今天作为同一个闭区间首尾", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    const today = dayjs().format("YYYY-MM-DD");

    fireEvent.click(screen.getByText("今天"));
    fireEvent.click(screen.getByRole("button", { name: "导出维保订单 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: { date_from: today, date_to: today },
      responseType: "blob",
    }));
  });

  it("近14天档从今天向前包含十四个自然日", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    const today = dayjs();

    fireEvent.click(screen.getByText("近14天"));
    fireEvent.click(screen.getByRole("button", { name: "导出维保订单 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: {
        date_from: today.subtract(13, "day").format("YYYY-MM-DD"),
        date_to: today.format("YYYY-MM-DD"),
      },
      responseType: "blob",
    }));
  });

  it("近21天档从今天向前包含二十一个自然日", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    const today = dayjs();

    fireEvent.click(screen.getByText("近21天"));
    fireEvent.click(screen.getByRole("button", { name: "导出维保订单 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: {
        date_from: today.subtract(20, "day").format("YYYY-MM-DD"),
        date_to: today.format("YYYY-MM-DD"),
      },
      responseType: "blob",
    }));
  });

  it("近30天档从今天向前包含三十个自然日", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    const today = dayjs();

    fireEvent.click(screen.getByText("近30天"));
    fireEvent.click(screen.getByRole("button", { name: "导出维保订单 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: {
        date_from: today.subtract(29, "day").format("YYYY-MM-DD"),
        date_to: today.format("YYYY-MM-DD"),
      },
      responseType: "blob",
    }));
  });

  it("本月档从当月一日到今天", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    const today = dayjs();

    fireEvent.click(screen.getByText("本月"));
    fireEvent.click(screen.getByRole("button", { name: "导出维保订单 Excel" }));

    await waitFor(() => expect(get).toHaveBeenCalledWith("/maintenance/orders/export", {
      params: {
        date_from: today.startOf("month").format("YYYY-MM-DD"),
        date_to: today.format("YYYY-MM-DD"),
      },
      responseType: "blob",
    }));
  });

  it("自定义缺少日期范围时不发送导出请求", async () => {
    installSuccessResponses();
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

    fireEvent.click(screen.getByText("自定义"));
    const callsBeforeExport = get.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "导出维保订单 Excel" }));

    await Promise.resolve();
    expect(get.mock.calls.slice(callsBeforeExport).filter(([path]) => (
      path === "/maintenance/orders/export"
    ))).toHaveLength(0);
  });

  it("下载锚点加入 DOM 后点击移除并延迟释放 Object URL", async () => {
    installSuccessResponses();
    let attachedWhenClicked = false;
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      attachedWhenClicked = document.body.contains(this);
    });
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");

    fireEvent.click(screen.getByRole("button", { name: "导出维保订单 Excel" }));

    await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled());
    expect(attachedWhenClicked).toBe(true);
    expect(document.querySelector('a[download="maintenance_orders_all.xlsx"]')).toBeNull();
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
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

    fireEvent.click(screen.getByRole("button", { name: "导出维保订单 Excel" }));

    await waitFor(() => expect(errorMessage).toHaveBeenCalledWith("无维保页面权限"));
  });

  it("范围导出的下载文件名使用实际起止日期", async () => {
    installSuccessResponses();
    let downloadedName = "";
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (this: HTMLAnchorElement) {
      downloadedName = this.download;
    });
    render(<ProjectCostPage />);
    await screen.findByText("进行中项目");
    const today = dayjs();

    fireEvent.click(screen.getByText("近7天"));
    fireEvent.click(screen.getByRole("button", { name: "导出维保订单 Excel" }));

    await waitFor(() => expect(downloadedName).toBe(
      `maintenance_orders_${today.subtract(6, "day").format("YYYY-MM-DD")}_${today.format("YYYY-MM-DD")}.xlsx`,
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
    fireEvent.click(screen.getByRole("button", { name: "导出维保订单 Excel" }));
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

    fireEvent.click(screen.getByRole("button", { name: "导出项目 CSV" }));

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
