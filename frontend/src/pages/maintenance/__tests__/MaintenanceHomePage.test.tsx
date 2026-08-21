import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getBoardProjects = vi.fn();
const searchBoardProjects = vi.fn();
const downloadSparePartLines = vi.fn();

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
    applySparePartLines: vi.fn(),
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
  // IntersectionObserver 在 jsdom 里不存在：无限加载靠它，测试里给个惰性桩
  (globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver =
    class {
      observe() {}
      disconnect() {}
    };
  getBoardProjects.mockResolvedValue(page([row("p1", "项目一"), row("p2", "项目二")]));
  searchBoardProjects.mockResolvedValue(page([row("p2", "项目二")]));
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

  it("有关键字时改走搜索端点（GET 不接自由文本）", async () => {
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalled());
    const box = screen.getByPlaceholderText("搜项目名 / XSDD 单号");
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

  it("无上传动作键时只给下载按钮（#38 下载谁都能，上传要键）", async () => {
    renderPage();
    expect(await screen.findByRole("button", { name: /下载全项目备件行级表/ }))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /上传覆盖/ })).toBeNull();
  });

  it("有上传动作键时出现上传入口", async () => {
    localStorage.setItem(
      "permissions",
      JSON.stringify({ action_maintenance_expense_collection_upload: true }),
    );
    renderPage();
    expect(await screen.findByRole("button", { name: /上传覆盖/ })).toBeInTheDocument();
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
