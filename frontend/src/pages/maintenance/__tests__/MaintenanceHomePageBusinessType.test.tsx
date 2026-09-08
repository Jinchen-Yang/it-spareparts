import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * 维保主页业务类型筛选（2026-09-08 客户需求）。
 *
 * 两条最要紧的断言：
 * 1. **默认不排除任何一档**——生产 648 个项目里 647 个未标注，默认排除等于把卡墙筛空（R5）。
 * 2. **导出/批量移交按钮必须收到同一个筛选**——这是全案唯一没有守卫的漏点：tsc 拦不住
 *    （两个按钮的 filters 里该字段是可选的，页面漏传照样编译通过）、后端也拦不住
 *    （前端根本没发），漏了就是「屏幕上 3 个项目、导出下来是全量」且 CI 全绿。
 */

const getBoardProjects = vi.fn();
const searchBoardProjects = vi.fn();

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
  return { ...actual, saveBlob: vi.fn() };
});

const exportFilters = vi.fn();
const transferFilters = vi.fn();

vi.mock("../../../components/maintenance/MaintenanceProjectExportButton", () => ({
  default: (props: { filters: unknown }) => {
    exportFilters(props.filters);
    return <button type="button">项目清单导出</button>;
  },
}));

vi.mock("../../../components/maintenance/MaintenanceBatchTransferButton", () => ({
  default: (props: { filters: unknown }) => {
    transferFilters(props.filters);
    return <button type="button">批量导入 / 下载</button>;
  },
}));

import MaintenanceHomePage from "../MaintenanceHomePage";

const stat = <T,>(value: T) => ({ state: "ready" as const, value, as_of: null });

function row(id: string, businessType: string | null, code: string) {
  return {
    project_id: id, project_code: id, display_name: id,
    lifecycle: "ongoing", is_archived: false,
    business_type: businessType, business_type_code: code,
    contract_nos: [`XSDD-${id}`], project_manager: "李经理", salesperson: "王销售",
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

const page = (rows: unknown[], hidden = 0) => ({
  data: {
    rows, total: rows.length, page: 1, page_size: 20, sort: "name",
    business_type_hidden: hidden,
    window: { from: "2026-01-01", to: "2026-08-16" },
  },
});

function lastArg(mock: { mock: { calls: unknown[][] } }) {
  const calls = mock.mock.calls;
  return calls[calls.length - 1]?.[0];
}

/** 打开业务类型多选并点掉某一档（antd 的选项在浮层里，直接按 option 节点取）。 */
async function toggleBusinessType(label: string) {
  const select = screen
    .getByTestId("business-type-filter")
    .querySelector(".ant-select-selector") as HTMLElement;
  fireEvent.mouseDown(select);
  await waitFor(() =>
    expect(document.querySelectorAll(".ant-select-item-option").length)
      .toBeGreaterThan(0));
  const option = [...document.querySelectorAll(".ant-select-item-option")]
    .find((node) => node.textContent === label);
  expect(option, `业务类型选项「${label}」没找到`).toBeTruthy();
  fireEvent.click(option as Element);
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  (globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver =
    class {
      observe() {}
      disconnect() {}
    };
  getBoardProjects.mockResolvedValue(page([
    row("整体维保项目", "整体维保", "overall"),
    row("未标注项目", null, "unlabeled"),
  ]));
  searchBoardProjects.mockResolvedValue(page([row("整体维保项目", "整体维保", "overall")]));
});

afterEach(cleanup);

function renderPage() {
  return render(
    <MemoryRouter>
      <MaintenanceHomePage />
    </MemoryRouter>,
  );
}

describe("维保主页 · 业务类型筛选", () => {
  it("默认不排除任何一档（R5：647/648 个项目未标注，默认排除会把卡墙筛空）", async () => {
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalled());
    expect(getBoardProjects.mock.calls[0][0]).toMatchObject({
      business_type: "all",
    });
  });

  it("勾掉一档就开始收窄，且与期限状态叠加而不是互斥", async () => {
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByText("已结束"));
    await waitFor(() =>
      expect(lastArg(getBoardProjects)).toMatchObject({ lifecycle: "ended" }));

    // 多选框里去掉「非维保」：期限筛选必须原样保留
    await toggleBusinessType("非维保");

    await waitFor(() => {
      const params = lastArg(getBoardProjects) as { lifecycle: string; business_type: string };
      expect(params.lifecycle).toBe("ended");
      expect(params.business_type).not.toBe("all");
      expect(params.business_type).not.toContain("other");
      expect(params.business_type).toContain("unlabeled");
    });
  });

  it("导出与批量移交拿到的筛选与卡墙完全一致（漏传＝导出全量且 CI 全绿）", async () => {
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalled());

    const wall = (lastArg(getBoardProjects) as { business_type: string }).business_type;
    expect((lastArg(exportFilters) as { business_type: string }).business_type).toBe(wall);
    expect((lastArg(transferFilters) as { business_type: string }).business_type).toBe(wall);

    await toggleBusinessType("非维保");

    await waitFor(() => {
      const narrowed = (lastArg(getBoardProjects) as { business_type: string }).business_type;
      expect(narrowed).not.toBe("all");
      expect((lastArg(exportFilters) as { business_type: string }).business_type)
        .toBe(narrowed);
      expect((lastArg(transferFilters) as { business_type: string }).business_type)
        .toBe(narrowed);
    });
  });

  it("被挡掉的项目数常驻可见，且一键可撤销（R5：隐藏必须可计数可撤销）", async () => {
    getBoardProjects.mockResolvedValue(page([row("整体维保项目", "整体维保", "overall")], 7));
    renderPage();

    expect(await screen.findByText(/已按业务类型隐藏 7 个项目/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看全部" }));
    await waitFor(() =>
      expect(lastArg(getBoardProjects)).toMatchObject({ business_type: "all" }));
  });

  it("卡片标出业务类型，未标注是明确提示而不是留白", async () => {
    renderPage();
    expect(await screen.findByText("整体维保")).toBeInTheDocument();
    expect(screen.getByText("未标注业务类型")).toBeInTheDocument();
  });

  it("筛空时的文案指向真正的原因（多数项目未标注），不让人以为没项目", async () => {
    getBoardProjects.mockResolvedValue(page([], 12));
    renderPage();
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalled());

    await toggleBusinessType("未标注");

    expect(await screen.findByText(/请勾上「未标注」或清空业务类型筛选/)).toBeInTheDocument();
  });
});
