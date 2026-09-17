import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * 维保主页筛选 URL 化（v1.35 #N3）：期限/业务类型/状态/排序/关键词入 query。
 * URL 直开恢复筛选；改筛选写穿 URL；默认渲染零参数且请求默认值。
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

vi.mock("../../../components/maintenance/MaintenanceProjectExportButton", () => ({
  default: () => <button type="button">项目清单导出</button>,
}));

vi.mock("../../../components/maintenance/MaintenanceBatchTransferButton", () => ({
  default: () => <button type="button">批量导入 / 下载</button>,
}));

import MaintenanceHomePage from "../MaintenanceHomePage";

const stat = <T,>(value: T) => ({ state: "ready" as const, value, as_of: null });

function row(id: string) {
  return {
    project_id: id, project_code: id, display_name: id,
    lifecycle: "ongoing", is_archived: false,
    business_type: null, business_type_code: "unlabeled",
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

const page = (rows: unknown[]) => ({
  data: {
    rows, total: rows.length, page: 1, page_size: 20, sort: "name",
    window: { from: "2026-01-01", to: "2026-09-17" },
  },
});

function lastArg(mock: { mock: { calls: unknown[][] } }) {
  const calls = mock.mock.calls;
  return calls[calls.length - 1]?.[0];
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="wall-location">{location.pathname + location.search}</output>;
}

function query() {
  return new URLSearchParams(
    (screen.getByTestId("wall-location").textContent ?? "").split("?")[1] ?? "",
  );
}

/** 打开业务类型多选并点某一档（antd 选项在浮层里，按 option 节点取）。 */
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
  getBoardProjects.mockResolvedValue(page([row("p1"), row("p2")]));
  searchBoardProjects.mockResolvedValue(page([row("p1")]));
});

afterEach(cleanup);

function renderWall(initialPath = "/maintenance") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <MaintenanceHomePage />
      <LocationProbe />
    </MemoryRouter>,
  );
}

describe("维保主页 · 筛选 URL 化（v1.35）", () => {
  it("URL 直开恢复筛选：business_type/lifecycle=all 落到请求，q 走搜索端点并回填输入框", async () => {
    renderWall("/maintenance?business_type=computing&lifecycle=all&q=联想");
    await waitFor(() => expect(searchBoardProjects).toHaveBeenCalledTimes(1));
    expect(getBoardProjects).not.toHaveBeenCalled();
    expect(lastArg(searchBoardProjects)).toMatchObject({
      q: "联想", lifecycle: "all", business_type: "computing",
      page: 1, page_size: 20,
    });
    expect(screen.getByPlaceholderText("搜项目名 / XSDD 单号 / 销售姓名"))
      .toHaveValue("联想");
  });

  it("切换业务类型把 CSV 写进 URL 并按同一筛选请求", async () => {
    renderWall("/maintenance?business_type=computing");
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalledTimes(1));
    await toggleBusinessType("整体维保");
    await waitFor(() => expect(query().get("business_type")).toBe("computing,overall"));
    expect(lastArg(getBoardProjects)).toMatchObject({ business_type: "computing,overall" });
  });

  it("默认渲染：URL 零参数，请求默认值（ongoing / all / name）", async () => {
    renderWall("/maintenance");
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalledTimes(1));
    expect(query().toString()).toBe("");
    expect(getBoardProjects.mock.calls[0][0]).toMatchObject({
      lifecycle: "ongoing", business_type: "all", sort: "name", page: 1, page_size: 20,
    });
  });

  it("非法/未知参数回退默认值，不报错也不写回 URL", async () => {
    renderWall("/maintenance?lifecycle=bogus&sort=bogus&status=bogus&business_type=bogus");
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalledTimes(1));
    expect(getBoardProjects.mock.calls[0][0]).toMatchObject({
      lifecycle: "ongoing", business_type: "all", sort: "name",
    });
    expect(getBoardProjects.mock.calls[0][0].card_status).toBeUndefined();
    expect(query().get("lifecycle")).toBe("bogus");
  });

  it("关键词提交入 URL，删除条件胶囊清掉 q 并回到列表端点", async () => {
    renderWall("/maintenance");
    await waitFor(() => expect(getBoardProjects).toHaveBeenCalledTimes(1));
    const input = screen.getByPlaceholderText("搜项目名 / XSDD 单号 / 销售姓名");
    fireEvent.change(input, { target: { value: "联想" } });
    expect(query().has("q")).toBe(false);
    expect(getBoardProjects).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 13 });
    await waitFor(() => expect(searchBoardProjects).toHaveBeenCalledTimes(1));
    expect(query().get("q")).toBe("联想");
    fireEvent.click(screen.getByRole("button", { name: "移除关键词筛选" }));
    await waitFor(() => expect(query().has("q")).toBe(false));
    await waitFor(() => expect(lastArg(getBoardProjects)).toMatchObject({ lifecycle: "ongoing" }));
    expect(screen.getByPlaceholderText("搜项目名 / XSDD 单号 / 销售姓名")).toHaveValue("");
  });
});
