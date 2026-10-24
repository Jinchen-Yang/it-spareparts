import { legacyAuthorizationState } from "../../../test/legacyAuthorization";
import { prepareLegacyPageFixture } from "../../../test/legacyAuthorization";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanupAntd, renderWithoutMotion } from "../../../test/antdRender";

const get = vi.hoisted(() => vi.fn());
vi.mock("../../../api", () => ({ api: { get, request: (config: { url: string; method: string; [key: string]: unknown }) => { const { url, method: _method, ...options } = config; return get(url, options); } } }));

import GlobalMaintenanceRowWorkbench from "../GlobalMaintenanceRowWorkbench";

const fields = [
  { field: "pn_std", label: "PN", type: "text", operators: ["contains", "eq", "prefix", "is_empty"] },
  { field: "qty", label: "需求数量", type: "number", operators: ["eq", "range", "is_empty"] },
];

beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:rows") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  get.mockImplementation(async (path: string, options?: { params?: Record<string, unknown> }) => {
    if (path.endsWith("/fields")) return { data: { fields, amount_filter_allowed: false } };
    if (path === "/maintenance/projects/stable") return { data: { rows: [], total: 0 } };
    if (path.endsWith("/export")) return { data: new Blob(["PN-A"], { type: "text/csv" }) };
    const filtered = Boolean(options?.params?.filters);
    return {
      data: {
        rows: [{
          line_id: options?.params?.page === 2 ? 21 : 1,
          project_name: "合成项目",
          pn_std: "PN-A",
          description: "合成备件",
          order_no: "WBDD-1",
          qty: "2",
        }],
        total: filtered ? 21 : 45,
        page: options?.params?.page ?? 1,
        page_size: 20,
      },
    };
  });
});

afterEach(async () => {
  await cleanupAntd();
  vi.restoreAllMocks();
});

describe("全项目行级筛选工作台", () => {
  it("应用筛选后网页总数、翻页和完整 CSV 导出共用条件，导出不携带页码", async () => {
    renderWithoutMotion(<GlobalMaintenanceRowWorkbench rangeParams={{ range: "this_month" }} />);
    expect(await screen.findByText("共 45 行")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "添加筛选条件" }));
    fireEvent.change(screen.getByRole("textbox", { name: "条件 1 的值" }), { target: { value: "PN-A" } });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(await screen.findByText("共 21 行")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("2"));
    const filters = '[{"field":"pn_std","op":"contains","value":"PN-A"}]';
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/spare-part-lines/rows",
      expect.objectContaining({ params: expect.objectContaining({ page: 2, filters }) }),
    ));
    fireEvent.click(screen.getByRole("button", { name: "导出完整筛选集 CSV" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/spare-part-lines/export",
      expect.objectContaining({
        responseType: "blob",
        params: { range: "this_month", filters, format: "csv" },
      }),
    ));
    expect(screen.getByText(/核对导出不可回传/)).toBeInTheDocument();
  });

  it("显式选择项目后重新取得金额字段资格，列表和导出都保留项目限定", async () => {
    const original = get.getMockImplementation()!;
    get.mockImplementation((path: string, options?: { params?: Record<string, unknown> }) => {
      if (path === "/maintenance/projects/stable") {
        return Promise.resolve({ data: { rows: [{ project_id: "p-owned", display_name: "本人负责项目" }], total: 1 } });
      }
      if (path.endsWith("/fields") && options?.params?.project_id === "p-owned") {
        return Promise.resolve({ data: { fields: [...fields, {
          field: "effective_unit_cost_ex_tax", label: "有效未税单位成本", type: "number", operators: ["eq", "range", "is_empty"],
        }], amount_filter_allowed: true } });
      }
      return original(path, options);
    });
    renderWithoutMotion(<GlobalMaintenanceRowWorkbench rangeParams={{ range: "this_month" }} />);
    expect(await screen.findByText(/当前范围不可使用金额条件/)).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "行级表项目范围" }));
    fireEvent.click(await screen.findByText("本人负责项目", { selector: ".ant-select-item-option-content" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/spare-part-lines/fields", expect.objectContaining({ params: { project_id: "p-owned" }, headers: { Authorization: "Bearer legacy-page-fixture-owner" }, signal: expect.any(AbortSignal) }),
    ));
    await waitFor(() => expect(screen.queryByText(/当前范围不可使用金额条件/)).not.toBeInTheDocument());
    await screen.findByText("共 45 行");
    fireEvent.click(screen.getByRole("button", { name: "导出完整筛选集 XLSX" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/maintenance/spare-part-lines/export",
      expect.objectContaining({ params: { range: "this_month", project_id: "p-owned", format: "xlsx" } }),
    ));
  });

  it("筛选请求失败后隐藏旧行数并禁用导出，重试仍使用失败条件", async () => {
    const original = get.getMockImplementation()!;
    let rejectNextFilter = true;
    get.mockImplementation((path: string, options?: { params?: Record<string, unknown> }) => {
      if (path.endsWith("/rows") && options?.params?.filters && rejectNextFilter) {
        rejectNextFilter = false;
        return Promise.reject({ response: { data: { detail: { message: "合成查询暂不可用" } } } });
      }
      return original(path, options);
    });
    renderWithoutMotion(<GlobalMaintenanceRowWorkbench rangeParams={{ range: "this_month" }} />);
    await screen.findByText("共 45 行");
    fireEvent.click(screen.getByRole("button", { name: "添加筛选条件" }));
    fireEvent.change(screen.getByRole("textbox", { name: "条件 1 的值" }), { target: { value: "PN-A" } });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(await screen.findByText("合成查询暂不可用")).toBeInTheDocument();
    expect(screen.queryByText("共 45 行")).not.toBeInTheDocument();
    expect(screen.queryByText("共 0 行")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导出完整筛选集 CSV" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重试列表" }));
    expect(await screen.findByText("共 21 行")).toBeInTheDocument();
    expect(get).toHaveBeenLastCalledWith("/maintenance/spare-part-lines/rows", expect.objectContaining({
      params: expect.objectContaining({ filters: '[{"field":"pn_std","op":"contains","value":"PN-A"}]' }),
    }));
  });

  it("切换项目后迟到的旧范围列表和权限目录不能覆盖新范围", async () => {
    const original = get.getMockImplementation()!;
    let resolveFields!: (response: unknown) => void;
    let resolveRows!: (response: unknown) => void;
    get.mockImplementation((path: string, options?: { params?: Record<string, unknown> }) => {
      if (path === "/maintenance/projects/stable") {
        return Promise.resolve({ data: { rows: [{ project_id: "p-owned", display_name: "本人负责项目" }], total: 1 } });
      }
      if (path.endsWith("/fields")) {
        if (!options?.params?.project_id) return new Promise((resolve) => { resolveFields = resolve; });
        return Promise.resolve({ data: { fields, amount_filter_allowed: true } });
      }
      if (path.endsWith("/rows")) {
        if (!options?.params?.project_id) return new Promise((resolve) => { resolveRows = resolve; });
        return Promise.resolve({ data: { rows: [], total: 7, page: 1, page_size: 20 } });
      }
      return original(path, options);
    });
    renderWithoutMotion(<GlobalMaintenanceRowWorkbench rangeParams={{ range: "this_month" }} />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "行级表项目范围" })).not.toBeDisabled());
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "行级表项目范围" }));
    fireEvent.click(await screen.findByText("本人负责项目", { selector: ".ant-select-item-option-content" }));
    expect(await screen.findByText("共 7 行")).toBeInTheDocument();
    await act(async () => {
      resolveFields({ data: { fields, amount_filter_allowed: false } });
      resolveRows({ data: { rows: [], total: 99, page: 1, page_size: 20 } });
    });
    expect(screen.getByText("共 7 行")).toBeInTheDocument();
    expect(screen.queryByText("共 99 行")).not.toBeInTheDocument();
    expect(screen.queryByText(/当前范围不可使用金额条件/)).not.toBeInTheDocument();
  });
});

// Existing behavior fixtures explicitly run before the canonical module cutover.
vi.mock("../../../authz/AuthzProvider", async original => ({
  ...await original<typeof import("../../../authz/AuthzProvider")>(),
  useAuthz: () => legacyAuthorizationState(),
}));

beforeEach(() => prepareLegacyPageFixture());
