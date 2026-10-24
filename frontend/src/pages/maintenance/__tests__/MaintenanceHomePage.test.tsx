import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getBoardProjects = vi.fn();
const searchBoardProjects = vi.fn();
const downloadSparePartLines = vi.fn();
const validateSparePartLines = vi.fn();
const applySparePartLines = vi.fn();

vi.mock("../../../api/maintenanceBossBoard", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceBossBoard",
  );
  return {
    ...actual,
    getBoardProjects: (...args: unknown[]) => getBoardProjects(...args),
    searchBoardProjects: (...args: unknown[]) => searchBoardProjects(...args),
  };
});

vi.mock("../../../api/maintenanceWorkbooks", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceWorkbooks",
  );
  return {
    ...actual,
    downloadSparePartLines: (...args: unknown[]) => downloadSparePartLines(...args),
    validateSparePartLines: (...args: unknown[]) => validateSparePartLines(...args),
    applySparePartLines: (...args: unknown[]) => applySparePartLines(...args),
    saveBlob: vi.fn(),
  };
});

import MaintenanceHomePage from "../MaintenanceHomePage";

const stat = <T,>(value: T) => ({ state: "ready" as const, value, as_of: null });

/** 取最后一次调用的首个入参（tsconfig 的 lib 早于 es2022，没有 Array.at）。 */
function lastArg(mock: { mock: { calls: unknown[][] } }) {
  const calls = mock.mock.calls;
  return calls[calls.length - 1]?.[0];
}

function row(id: string, name = id) {
  return {
    project_id: id, project_code: name, display_name: name,
    lifecycle: "ongoing", is_archived: false,
    contract_nos: [`XSDD-${id}`], project_manager: "李经理",
    salesperson: "王销售",
    contract_amount_inc_tax: stat("1000.00"),
    known_apply_cost_ex_tax: stat("500.00"),
    procured_qty: stat("1.000"),
    collection_preview_inc_tax: stat("100.00"),
    cost_ratio_pct: stat("50.0"),
    card_status: "normal" as const,
    has_activity_in_window: true, pre_delivery_order_count: 0,
    orders_ytd: stat(1), lines_ytd: stat(1),
    known_apply_cost_inc_tax: stat({
      actual_amount: "565.00", estimated_amount: "0", known_amount: "565.00",
      missing_lines: 0, coverage_pct: 100, quality: "actual_only" as const,
    }),
    shipped_qty: { state: "not_imported" as const, value: null, as_of: null },
    returned_good_qty: { state: "not_imported" as const, value: null, as_of: null },
    returned_bad_qty: { state: "not_imported" as const, value: null, as_of: null },
  };
}

const page = (rows: unknown[]) => ({
  data: { rows, total: rows.length, page: 1, page_size: 20, sort: "name",
          window: { from: "2026-01-01", to: "2026-08-16" } },
});

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("permissions", JSON.stringify({
    page_maintenance: true, data_purchase_cost: true, data_profit: true,
  }));
  // IntersectionObserver 在 jsdom 里不存在：无限加载靠它，测试里给个惰性桩
  (globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver =
    class {
      observe() {}
      disconnect() {}
    };
  getBoardProjects.mockResolvedValue(page([row("p1", "项目一"), row("p2", "项目二")]));
  searchBoardProjects.mockResolvedValue(page([row("p2", "项目二")]));
  validateSparePartLines.mockResolvedValue({ valid: true });
  applySparePartLines.mockResolvedValue({
    cost_refills: 1,
    site_return_flags: 0,
    expense_updates: 0,
    collection_creates: 0,
    collection_voids: 0,
  });
});

afterEach(cleanup);

function renderPage() {
  return render(
    <MemoryRouter>
      <MaintenanceHomePage />
    </MemoryRouter>,
  );
}

describe("维保主页（项目卡墙）", () => {
  it("默认只看进行中项目（#37）", async () => {
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalled());
    expect(getBoardProjects.mock.calls[0][0]).toMatchObject({ lifecycle: "ongoing" });
  });

  it("渲染项目卡", async () => {
    renderPage();
    expect(await screen.findByText("项目一")).toBeInTheDocument();
    expect(screen.getByText("项目二")).toBeInTheDocument();
  });

  it("首次加载失败显示重试，恢复后显示项目而非空态", async () => {
    getBoardProjects.mockRejectedValueOnce(new Error("network unavailable"));
    renderPage();

    expect(await screen.findByText("项目列表加载失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));

    expect(await screen.findByText("项目一")).toBeInTheDocument();
    expect(getBoardProjects).toHaveBeenCalledTimes(2);
    expect(lastArg(getBoardProjects)).toMatchObject({ page: 1, lifecycle: "ongoing" });
    expect(screen.queryByText("项目列表加载失败")).toBeNull();
  });

  it("失败时不把未取得的项目列表显示为没有项目", async () => {
    getBoardProjects.mockRejectedValueOnce(new Error("network unavailable"));
    renderPage();

    expect(await screen.findByText("项目列表加载失败")).toBeInTheDocument();
    expect(screen.queryByText(/没有符合条件的项目/)).toBeNull();
  });

  it("重试期间切换筛选后，旧请求失败不会覆盖新列表", async () => {
    let rejectRetry!: (reason: unknown) => void;
    getBoardProjects.mockRejectedValueOnce(new Error("network unavailable"));
    getBoardProjects.mockImplementationOnce(() => new Promise((_resolve, reject) => {
      rejectRetry = reject;
    }));
    getBoardProjects.mockResolvedValueOnce(page([row("ended", "已结束项目")]));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /重\s*试/ }));
    fireEvent.click(screen.getByText("已结束"));
    expect(await screen.findByText("已结束项目")).toBeInTheDocument();
    await act(async () => { rejectRetry(new Error("late failure")); });

    expect(screen.getByText("已结束项目")).toBeInTheDocument();
    expect(screen.queryByText("项目列表加载失败")).toBeNull();
    expect(screen.queryByRole("button", { name: /重\s*试/ })).toBeNull();
    expect(lastArg(getBoardProjects)).toMatchObject({ page: 1, lifecycle: "ended" });
  });

  it("续页失败保留已加载卡片，重试同页后继续加载且不重复卡片", async () => {
    type Callback = (entries: Array<{ isIntersecting: boolean }>) => void;
    const observers = new Set<Callback>();
    (globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver =
      class {
        constructor(private callback: Callback) {}
        observe() { observers.add(this.callback); }
        disconnect() { observers.delete(this.callback); }
      };
    // Typography 也使用 IntersectionObserver；向当前仍在观察的目标统一派发。
    const intersect = () => [...observers].forEach((callback) => callback([{ isIntersecting: true }]));
    let failed = false;
    getBoardProjects.mockImplementation((params: { page: number }) => {
      if (params.page === 2 && !failed) {
        failed = true;
        return Promise.reject(new Error("network unavailable"));
      }
      return Promise.resolve({
        data: { ...page([]).data, rows: [row(`p${params.page}`, `第${params.page}页项目`)], total: 60 },
      });
    });
    renderPage();
    expect(await screen.findByText("第1页项目")).toBeInTheDocument();
    await act(async () => { intersect(); });
    expect(getBoardProjects.mock.calls.map(([params]) => params.page)).toEqual([1, 2]);

    expect(await screen.findByText("项目列表加载失败")).toBeInTheDocument();
    expect(screen.getByText("第1页项目")).toBeInTheDocument();
    expect(screen.queryByText("已到底")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));

    expect(await screen.findByText("第2页项目")).toBeInTheDocument();
    expect(screen.getAllByText("第1页项目")).toHaveLength(1);
    expect(getBoardProjects.mock.calls.map(([params]) => params.page)).toEqual([1, 2, 2]);
    await act(async () => { intersect(); });
    expect(await screen.findByText("第3页项目")).toBeInTheDocument();
    expect(lastArg(getBoardProjects)).toMatchObject({ page: 3 });
  });

  it("页头有标题与副标（超预算变色、点卡进项目）", () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "维保项目" })).toBeInTheDocument();
    expect(screen.getByText(/超预算的项目会变黄、变红/)).toBeInTheDocument();
  });

  it("页头有「需求单与同步」入口，链到 /maintenance/demands（#267）", () => {
    renderPage();
    const link = screen.getByRole("link", { name: "需求单与同步" });
    expect(link).toHaveAttribute("href", "/maintenance/demands");
  });

  it("切到已结束会重新拉取", async () => {
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText("已结束"));
    await waitFor(() =>
      expect(lastArg(getBoardProjects)).toMatchObject({
        lifecycle: "ended",
      }));
  });

  it("期限缺失可筛出（R5：台账未导入时项目不得整面消失）", async () => {
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText("期限缺失"));
    await waitFor(() =>
      expect(lastArg(getBoardProjects)).toMatchObject({
        lifecycle: "missing",
      }));
  });

  it("有合同财务权限时「回款已完成」可筛出（2026-09-04 客户反馈）", async () => {
    localStorage.setItem("permissions", JSON.stringify({ data_profit: true }));
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText("回款已完成"));
    await waitFor(() =>
      expect(lastArg(getBoardProjects)).toMatchObject({
        lifecycle: "payment_complete",
      }));
  });

  it("无合同财务权限时不显示「回款已完成」页签", async () => {
    localStorage.setItem("permissions", JSON.stringify({ data_purchase_cost: true }));
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("回款已完成")).toBeNull();
  });

  it("有关键字时改走搜索端点（GET 不接自由文本）", async () => {
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalled());
    const box = screen.getByPlaceholderText("搜项目名 / XSDD 单号 / 销售姓名");
    fireEvent.change(box, { target: { value: "XSDD-p2" } });
    fireEvent.keyDown(box, { key: "Enter", code: "Enter", keyCode: 13 });
    await waitFor(() => expect(searchBoardProjects).toHaveBeenCalled());
    expect(lastArg(searchBoardProjects)).toMatchObject({ q: "XSDD-p2" });
  });

  it("状态筛选把 card_status 带给后端（#43）", async () => {
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalled());
    fireEvent.mouseDown(screen.getByText("全部状态"));
    fireEvent.click(await screen.findByTitle("报警"));
    await waitFor(() =>
      expect(lastArg(getBoardProjects)).toMatchObject({
        card_status: "alert",
      }));
  });

  it("状态筛选第一页零命中时仍继续拉取后续候选页", async () => {
    let intersect: ((entries: Array<{ isIntersecting: boolean }>) => void) | null = null;
    (globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver =
      class {
        constructor(callback: (entries: Array<{ isIntersecting: boolean }>) => void) {
          intersect = callback;
        }
        observe() {}
        disconnect() {}
      };
    const alertRow = { ...row("p-alert", "第二页报警项目"), card_status: "alert" as const };
    getBoardProjects.mockImplementation((params: { page: number; card_status?: string }) => {
      if (params.card_status !== "alert") return Promise.resolve(page([row("p1")]));
      return Promise.resolve({
        data: {
          rows: params.page === 2 ? [alertRow] : [],
          total: 40,
          page: params.page,
          page_size: 20,
          sort: "name",
          window: { from: "2026-01-01", to: "2026-08-16" },
        },
      });
    });
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalled());
    fireEvent.mouseDown(screen.getByText("全部状态"));
    fireEvent.click(await screen.findByTitle("报警"));
    await waitFor(() => expect(lastArg(getBoardProjects)).toMatchObject({
      page: 1,
      card_status: "alert",
    }));
    await waitFor(() => expect(intersect).not.toBeNull());
    (intersect as unknown as (entries: Array<{ isIntersecting: boolean }>) => void)([
      { isIntersecting: true },
    ]);

    expect(await screen.findByText("第二页报警项目")).toBeInTheDocument();
    expect(lastArg(getBoardProjects)).toMatchObject({ page: 2, card_status: "alert" });
  });

  it("有金额阅读权但无上传动作键时只给下载按钮", async () => {
    renderPage();
    expect(await screen.findByRole("button", { name: /下载全项目备件行级表/ }))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /上传覆盖/ })).toBeNull();
  });

  it("有上传动作键时出现上传入口", async () => {
    localStorage.setItem(
      "permissions",
      JSON.stringify({ page_maintenance: true, data_purchase_cost: true, data_profit: true,
        action_maintenance_expense_collection_upload: true }),
    );
    renderPage();
    expect(await screen.findByRole("button", { name: /上传覆盖/ })).toBeInTheDocument();
  });

  it.each([
    { page_maintenance: true, data_purchase_cost: true },
    { page_maintenance: true, action_maintenance_expense_collection_upload: true },
    { page_maintenance: true, data_profit: true, data_purchase_cost: false, action_maintenance_expense_collection_upload: true },
  ])("无有效利润资格时不提供全局下载或上传：%j", async (permissions) => {
    localStorage.setItem("permissions", JSON.stringify(permissions));
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: /下载全项目备件行级表|上传覆盖/ })).toBeNull();
    expect(downloadSparePartLines).not.toHaveBeenCalled();
    expect(validateSparePartLines).not.toHaveBeenCalled();
  });

  it("上传确认期间切换筛选，应用后只刷新最新筛选", async () => {
    localStorage.setItem(
      "permissions",
      JSON.stringify({ page_maintenance: true, data_purchase_cost: true, data_profit: true,
        action_maintenance_expense_collection_upload: true }),
    );
    const { container } = renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalledTimes(1));
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["xlsx"], "parts.xlsx")] },
    });
    const dialog = await screen.findByRole("dialog");

    fireEvent.click(screen.getByText("已结束"));
    await waitFor(() => expect(lastArg(getBoardProjects)).toMatchObject({ lifecycle: "ended" }));
    const callsBeforeApply = getBoardProjects.mock.calls.length;
    fireEvent.click(within(dialog).getByRole("button", { name: /确认回传/ }));

    await waitFor(() => expect(applySparePartLines).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(getBoardProjects.mock.calls.length).toBeGreaterThan(callsBeforeApply));
    expect(lastArg(getBoardProjects)).toMatchObject({ lifecycle: "ended" });
  });

  it("下载按当前时间预设取数（#38）", async () => {
    downloadSparePartLines.mockResolvedValue(new Blob(["x"]));
    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: /下载全项目备件行级表/ }));
    await waitFor(() => expect(downloadSparePartLines).toHaveBeenCalledWith({
      range: "this_month",
    }));
  });
});
