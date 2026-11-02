import { cleanup, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const searchMovements = vi.fn();
const exportMovements = vi.fn();
vi.mock("../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual<Record<string, unknown>>("../../../api/maintenanceOperations"),
  searchMaintenanceMovements: (...args: unknown[]) => searchMovements(...args),
  exportMaintenanceMovements: (...args: unknown[]) => exportMovements(...args),
}));
import MaintenanceMovementSearch from "../MaintenanceMovementSearch";

function row(entity = "issue", id = "r1", name = "项目甲") {
  return {
    entity_type: entity, record_id: id, project: { id: "p/1", code: "P1", name },
    document: { id, number: "LY-AUTO-1", display_number: "LY-20261027-0001", date: "2026-10-27", status: "confirmed", source: "page_manual" },
    pn: "PN-123", serials: ["SN-123"], source_serial_number: "SOURCE-SN",
    quantity: "1.000", matched_fields: ["pn", "serial_numbers"], excerpt: "pn: PN-123", truncated: false,
    record_ref: { project_id: "p/1", tab: entity === "demand_order" ? "parts-orders" : "site" },
  };
}
function result(items = [row()], page = 1, hasMore = false, unavailable: string[] = []) {
  return { data: { items, page, page_size: 20, has_more: hasMore, unavailable_entities: unavailable } };
}
function mount() {
  render(<MemoryRouter><MaintenanceMovementSearch /></MemoryRouter>);
  fireEvent.change(screen.getByRole("textbox", { name: "流转搜索词" }), { target: { value: "PN-123" } });
  fireEvent.click(screen.getByRole("button", { name: "搜索流转" }));
}

beforeEach(() => {
  vi.clearAllMocks(); searchMovements.mockResolvedValue(result());
  exportMovements.mockResolvedValue({ data: new Blob(["csv"]) });
  Object.defineProperty(URL, "createObjectURL", { value: vi.fn(() => "blob:movements"), configurable: true });
  Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), configurable: true });
});
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); });

describe("跨项目流转搜索", () => {
  it("显示项目多个XSDD，下载完整筛选而不是当前页", async () => {
    const item = { ...row(), project: { ...row().project, sales_order_nos: ["XSDD-20261101-0001", "XSDD-20261101-0002"] } };
    searchMovements.mockResolvedValue(result([item], 2));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    mount();
    expect(await screen.findByText("XSDD-20261101-0001 / XSDD-20261101-0002")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下载全部筛选结果" }));
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect(exportMovements).toHaveBeenCalledWith({ q: "PN-123", field: "auto", entity: "all", match_mode: "literal" });
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:movements");
  });

  it("下载迟到时筛选改变或账号切换均不保存旧文件", async () => {
    let resolve!: (value: unknown) => void;
    exportMovements.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    mount();
    await screen.findByRole("link", { name: "项目甲" });
    fireEvent.click(screen.getByRole("button", { name: "下载全部筛选结果" }));
    fireEvent.change(screen.getByRole("textbox", { name: "流转搜索词" }), { target: { value: "NEW-PN" } });
    resolve({ data: new Blob(["late"]) });
    await waitFor(() => expect(screen.queryByRole("link", { name: "项目甲" })).toBeNull());
    expect(click).not.toHaveBeenCalled();
    localStorage.setItem("token", "different-session");
    fireEvent.click(screen.getByRole("button", { name: "搜索流转" }));
    expect(await screen.findByText("登录会话已变更（已切换账号或登出），请刷新页面后继续")).toBeInTheDocument();
    expect(searchMovements).toHaveBeenCalledTimes(1);
  });

  it("导出超限明确提示，不生成截断文件", async () => {
    exportMovements.mockRejectedValueOnce({ response: { status: 413 } });
    mount();
    await screen.findByRole("link", { name: "项目甲" });
    fireEvent.click(screen.getByRole("button", { name: "下载全部筛选结果" }));
    expect(await screen.findByText("导出超过上限，请收窄搜索条件（最多5000行）")).toBeInTheDocument();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });
  it("显示匹配证据及项目链接，需求单和领用分别进入对应页签", async () => {
    searchMovements.mockResolvedValue(result([row(), row("demand_order", "order1", "项目乙"), row("bad_return", "bad1", "项目丙")]));
    mount();
    expect(await screen.findByRole("link", { name: "项目甲" })).toHaveAttribute("href", "/maintenance/projects/p%2F1?tab=site");
    expect(screen.getByRole("link", { name: "项目乙" })).toHaveAttribute("href", "/maintenance/projects/p%2F1?tab=parts-orders");
    expect(screen.getByRole("link", { name: "项目丙" })).toHaveAttribute("href", "/maintenance/projects/p%2F1?tab=site");
    expect(screen.getAllByText("PN、逐件 SN")).toHaveLength(3);
    expect(screen.getAllByText("导入源：SOURCE-SN")).toHaveLength(3);
    expect(searchMovements).toHaveBeenCalledWith({ q: "PN-123", field: "auto", entity: "all", match_mode: "literal", page: 1, page_size: 20 });
  });

  it("has_more 提示与下一页沿用查询，改词后回第一页", async () => {
    searchMovements.mockResolvedValueOnce(result([row()], 1, true)).mockResolvedValueOnce(result([row("receipt", "r2", "下一页项目")], 2));
    mount();
    expect(await screen.findByText("第 1 页，还有更多结果")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("下一页项目")).toBeInTheDocument();
    expect(searchMovements.mock.calls[1][0]).toMatchObject({ q: "PN-123", page: 2 });
    fireEvent.change(screen.getByRole("textbox", { name: "流转搜索词" }), { target: { value: "NEW-PN" } });
    expect(screen.queryByText("下一页项目")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "搜索流转" }));
    await waitFor(() => expect(searchMovements.mock.calls[2][0]).toMatchObject({ q: "NEW-PN", page: 1 }));
  });

  it("无结果和领用不可用有明确文案", async () => {
    searchMovements.mockResolvedValue(result([], 1, false, ["issue"]));
    mount();
    expect(await screen.findByText("当前权限范围内未找到")).toBeInTheDocument();
    expect(screen.getByText("当前账号无法查询领用记录，其余类型仍可搜索")).toBeInTheDocument();
  });

  it("失败不显示未找到，输入变化后旧结果不覆盖", async () => {
    searchMovements.mockRejectedValueOnce(new Error("timeout"));
    mount();
    expect(await screen.findByText("流转搜索失败")).toBeInTheDocument();
    expect(screen.queryByText("当前权限范围内未找到")).toBeNull();
    let resolve!: (value: unknown) => void;
    searchMovements.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    fireEvent.click(screen.getByRole("button", { name: "搜索流转" }));
    fireEvent.change(screen.getByRole("textbox", { name: "流转搜索词" }), { target: { value: "OTHER" } });
    resolve(result([row("issue", "old", "旧项目")]));
    await waitFor(() => expect(screen.queryByText("旧项目")).toBeNull());
  });

  it("字段、类型、精确筛选传给接口，日期按上海业务日显示", async () => {
    const item = row();
    item.document.date = "2026-10-26T17:00:00-07:00";
    searchMovements.mockResolvedValue(result([item]));
    mount();
    expect(await screen.findByText("2026-10-27")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "搜索字段" }));
    fireEvent.click(await screen.findByText("快递或凭证号"));
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "记录类型" }));
    fireEvent.click(await screen.findByText("需求单"));
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "匹配方式" }));
    fireEvent.click(await screen.findByText("精确"));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "流转搜索词" }), { key: "Enter", code: "Enter" });
    await waitFor(() => expect(searchMovements).toHaveBeenLastCalledWith(expect.objectContaining({ field: "evidence", entity: "demand_order", match_mode: "exact", page: 1 })));
  });

});
