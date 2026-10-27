import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import DataGrid, { type DataGridColumn } from "../DataGrid";
import { compareGridValues, sortGridRows } from "../sort";
import { gridAccountId, gridStorageKey } from "../useGridView";

interface Row { id: number; pn: string; name: string; quantity: number | null }
const rows: Row[] = [
  { id: 1, pn: "X10", name: "上海", quantity: 10 },
  { id: 2, pn: "X2", name: "北京", quantity: 2 },
  { id: 3, pn: "X1", name: "广州", quantity: null },
];
const columns: DataGridColumn<Row>[] = [
  { key: "pn", title: "PN", type: "code", identity: true, width: 120 },
  { key: "name", title: "名称", type: "zh", width: 160 },
  { key: "quantity", title: "数量", type: "number", width: 80 },
];
const storageKey = gridStorageKey("parts", "account-a")!;
const pointerCapture = vi.fn();
const releaseCapture = vi.fn();
let animationFrames: Map<number, FrameRequestCallback>;
let nextFrame: number;

function grid(overrides: Partial<React.ComponentProps<typeof DataGrid<Row>>> = {}) {
  return <DataGrid id="parts" accountId="account-a" columns={columns} rows={rows} rowKey={(row) => row.id} {...overrides} />;
}
function values() {
  return Array.from(screen.getByRole("table").querySelectorAll("tbody .data-grid-row"))
    .map((row) => row.querySelector(".data-grid-cell-content")?.textContent);
}
function flushFrame() {
  act(() => {
    const pending = [...animationFrames.values()];
    animationFrames.clear();
    pending.forEach((callback) => callback(16));
  });
}

beforeEach(() => {
  localStorage.clear();
  animationFrames = new Map(); nextFrame = 0;
  vi.stubGlobal("PointerEvent", class extends MouseEvent {
    pointerId: number;
    constructor(type: string, options: PointerEventInit = {}) { super(type, options); this.pointerId = options.pointerId ?? 1; }
  });
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => { const id = ++nextFrame; animationFrames.set(id, callback); return id; });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation((id) => { animationFrames.delete(id); });
  Object.defineProperty(HTMLElement.prototype, "setPointerCapture", { configurable: true, value: pointerCapture });
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => true });
  Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", { configurable: true, value: releaseCapture });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); pointerCapture.mockClear(); releaseCapture.mockClear(); });

describe("列宽", () => {
  it("捕获指针并按帧调整一列，其他列不压缩，松开才持久化且不触发排序", () => {
    const onSortChange = vi.fn();
    const renderCell = vi.fn((row: Row) => row.pn);
    const { container } = render(grid({ columns: [{ ...columns[0], render: renderCell }, ...columns.slice(1)], onSortChange }));
    const separator = screen.getByRole("separator", { name: "调整 PN 列宽" });
    const canvas = container.querySelector<HTMLElement>(".data-grid-canvas")!;
    expect(canvas.style.getPropertyValue("--dg-total-width")).toBe("360px");
    const rendered = renderCell.mock.calls.length;
    fireEvent.pointerDown(separator, { button: 0, pointerId: 7, clientX: 120 });
    expect(pointerCapture).toHaveBeenCalledWith(7);
    fireEvent.pointerMove(separator, { pointerId: 7, clientX: 190 });
    fireEvent.pointerMove(separator, { pointerId: 7, clientX: 220 });
    expect(window.requestAnimationFrame).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem(storageKey)).toBeNull();
    flushFrame();
    expect(canvas.style.getPropertyValue("--dg-column-0")).toBe("220px");
    expect(canvas.style.getPropertyValue("--dg-column-1")).toBe("160px");
    expect(canvas.style.getPropertyValue("--dg-total-width")).toBe("460px");
    expect(separator).toHaveAttribute("aria-valuenow", "220");
    expect(canvas).toHaveAttribute("data-resizing", "true");
    expect(container.querySelector<HTMLElement>(".data-grid-guide")!.style.left).toBe("219px");
    expect(renderCell).toHaveBeenCalledTimes(rendered);
    fireEvent.pointerUp(separator, { pointerId: 7, clientX: 220 });
    fireEvent.click(separator);
    expect(onSortChange).not.toHaveBeenCalled();
    expect(releaseCapture).toHaveBeenCalledWith(7);
    expect(canvas).not.toHaveAttribute("data-resizing");
    expect(JSON.parse(localStorage.getItem(storageKey)!).widths.pn).toBe(220);
    expect(screen.getByRole("columnheader", { name: /PN/ })).toHaveAttribute("aria-sort", "none");
  });

  it("取消拖动恢复原列宽，不写存储；忽略其他指针", () => {
    render(grid());
    const handle = screen.getByRole("separator", { name: "调整 PN 列宽" });
    fireEvent.pointerDown(handle, { button: 0, pointerId: 2, clientX: 120 });
    fireEvent.pointerMove(handle, { pointerId: 99, clientX: 300 });
    expect(animationFrames.size).toBe(0);
    fireEvent.pointerMove(handle, { pointerId: 2, clientX: 300 });
    flushFrame();
    fireEvent.pointerCancel(handle, { pointerId: 2 });
    expect(handle).toHaveAttribute("aria-valuenow", "120");
    expect(localStorage.getItem(storageKey)).toBeNull();
  });

  it("横向滚动后，身份列的拖拽参考线仍跟随固定列边界", () => {
    const { container } = render(grid({ renderExpanded: (row) => row.name }));
    const viewport = container.querySelector<HTMLElement>(".data-grid-scroll")!;
    viewport.scrollLeft = 100;
    const handle = screen.getByRole("separator", { name: "调整 PN 列宽" });
    fireEvent.pointerDown(handle, { button: 0, pointerId: 1, clientX: 152 });
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 192 });
    flushFrame();
    expect(container.querySelector<HTMLElement>(".data-grid-guide")!.style.left).toBe("291px");
    expect(handle).toHaveAttribute("aria-valuenow", "160");
    fireEvent.pointerUp(handle, { pointerId: 1 });
  });

  it("双击测量表头和可见内容 scrollWidth，立即显示长字段且最大800", () => {
    const { container } = render(grid());
    const cells = container.querySelectorAll<HTMLElement>('[data-grid-measure="name"]');
    cells.forEach((cell, index) => Object.defineProperty(cell, "scrollWidth", { configurable: true, value: index === 0 ? 150 : index === 1 ? 520 : 90 }));
    const handle = screen.getByRole("separator", { name: "调整 名称 列宽" });
    fireEvent.doubleClick(handle);
    expect(handle).toHaveAttribute("aria-valuenow", "536");
    Object.defineProperty(cells[0], "scrollWidth", { configurable: true, value: 950 });
    fireEvent.doubleClick(handle);
    expect(handle).toHaveAttribute("aria-valuenow", "800");
    expect(screen.getByText("上海")).toHaveAttribute("title", "上海");
  });

  it("自动适配只测量当前表格在屏幕内的单元格，不包含展开区子表", () => {
    const { container } = render(grid({
      defaultExpandedRowKeys: [1],
      renderExpanded: () => <DataGrid id="nested" accountId="account-a" columns={columns} rows={[rows[0]]} rowKey={(row) => row.id} />,
    }));
    const outer = container.querySelector<HTMLElement>(".data-grid-canvas")!;
    const viewport = container.querySelector<HTMLElement>(".data-grid-scroll")!;
    vi.spyOn(viewport, "getBoundingClientRect").mockReturnValue({ top: 50, bottom: 2000, height: 1950 } as DOMRect);
    const cells = [...outer.querySelectorAll<HTMLElement>('[data-grid-measure="name"]')];
    let outerBodyCount = 0;
    for (const cell of cells) {
      const nested = cell.closest(".data-grid-canvas") !== outer;
      const offscreen = !nested && !cell.dataset.gridHeader && ++outerBodyCount === 3;
      Object.defineProperty(cell, "scrollWidth", { configurable: true, value: nested || offscreen ? 900 : cell.dataset.gridHeader ? 40 : 210 });
      vi.spyOn(cell, "getBoundingClientRect").mockReturnValue({ top: offscreen ? window.innerHeight + 50 : 100, bottom: offscreen ? window.innerHeight + 80 : 130, height: 30 } as DOMRect);
    }
    const handle = within(outer.querySelector("thead")!).getByRole("separator", { name: "调整 名称 列宽" });
    fireEvent.doubleClick(handle);
    expect(handle).toHaveAttribute("aria-valuenow", "226");
  });

  it("键盘每次8px，Shift32px，Enter自动适配，并执行各类型最小宽度", () => {
    const { container } = render(grid());
    const code = screen.getByRole("separator", { name: "调整 PN 列宽" });
    expect(code).toHaveAttribute("tabindex", "0");
    expect(code).toHaveAttribute("aria-orientation", "vertical");
    fireEvent.keyDown(code, { key: "ArrowRight" });
    expect(code).toHaveAttribute("aria-valuenow", "128");
    fireEvent.keyDown(code, { key: "ArrowLeft", shiftKey: true });
    expect(code).toHaveAttribute("aria-valuenow", "96");
    fireEvent.keyDown(code, { key: "ArrowLeft", shiftKey: true });
    expect(code).toHaveAttribute("aria-valuenow", "80");
    const number = screen.getByRole("separator", { name: "调整 数量 列宽" });
    fireEvent.keyDown(number, { key: "ArrowLeft", shiftKey: true });
    expect(number).toHaveAttribute("aria-valuenow", "56");
    const text = screen.getByRole("separator", { name: "调整 名称 列宽" });
    fireEvent.keyDown(text, { key: "ArrowLeft", shiftKey: true });
    fireEvent.keyDown(text, { key: "ArrowLeft", shiftKey: true });
    fireEvent.keyDown(text, { key: "ArrowLeft", shiftKey: true });
    expect(text).toHaveAttribute("aria-valuenow", "96");
    const measured = container.querySelector('[data-grid-measure="pn"]')!;
    Object.defineProperty(measured, "scrollWidth", { value: 240 });
    fireEvent.keyDown(code, { key: "Enter" });
    expect(code).toHaveAttribute("aria-valuenow", "276");
  });
});

describe("排序与展开", () => {
  it("点击表头循环升序、降序、取消，原始数据不变", () => {
    render(grid());
    const button = screen.getByRole("button", { name: "PN排序" });
    fireEvent.click(button);
    expect(values()).toEqual(["X1", "X2", "X10"]);
    expect(screen.getByText("排序：PN 升序")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /PN/ })).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(button);
    expect(values()).toEqual(["X10", "X2", "X1"]);
    expect(screen.getByRole("columnheader", { name: /PN/ })).toHaveAttribute("aria-sort", "descending");
    fireEvent.click(button);
    expect(values()).toEqual(["X10", "X2", "X1"]);
    expect(screen.getByText("排序：默认")).toBeInTheDocument();
    expect(rows.map((row) => row.pn)).toEqual(["X10", "X2", "X1"]);
  });

  it("服务端排序通知外层且绝不对当前页重新排列，恢复持久化排序也通知", () => {
    localStorage.setItem(storageKey, JSON.stringify({ widths: {}, hidden: [], sort: { key: "pn", dir: "asc" } }));
    const onSortChange = vi.fn();
    render(grid({ serverSort: true, onSortChange }));
    expect(onSortChange).toHaveBeenCalledWith({ key: "pn", dir: "asc" });
    expect(values()).toEqual(["X10", "X2", "X1"]);
    fireEvent.click(screen.getByRole("button", { name: "PN排序" }));
    expect(onSortChange).toHaveBeenLastCalledWith({ key: "pn", dir: "desc" });
    expect(values()).toEqual(["X10", "X2", "X1"]);
  });

  it("多行默认折叠，表头一键展开/折叠，编辑控件不触发展开", () => {
    render(grid({ columns: [...columns, { key: "edit", title: "编辑", render: (row) => <input aria-label={`数量 ${row.pn}`} defaultValue={row.quantity ?? ""} /> }],
      renderExpanded: (row) => <div>详情 {row.pn}</div> }));
    expect(screen.queryByText("详情 X10")).not.toBeInTheDocument();
    fireEvent.focus(screen.getByRole("textbox", { name: "数量 X10" }));
    fireEvent.click(screen.getByRole("textbox", { name: "数量 X10" }));
    fireEvent.change(screen.getByRole("textbox", { name: "数量 X10" }), { target: { value: "20" } });
    expect(screen.queryByText("详情 X10")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开 X10" }));
    expect(screen.getByText("详情 X10")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开全部行" }));
    expect(screen.getByText("详情 X2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "折叠全部行" }));
    expect(screen.queryByText("详情 X10")).not.toBeInTheDocument();
  });
});

describe("比较类型", () => {
  it.each([
    ["code", "X2", "X10"], ["zh", "北京", "上海"], ["number", "2", "10"],
    ["money", "2.05", "10.01"], ["date", "2026-01-02", "2026-10-01"],
    ["time", "2026-01-01T01:00:00+08:00", "2025-12-31T20:00:00Z"],
    ["rank", 2, 3],
  ] as const)("%s 按声明类型排序", (type, a, b) => {
    expect(compareGridValues(a, b, type)).toBeLessThan(0);
    expect(compareGridValues(a, b, type, "desc")).toBeGreaterThan(0);
  });
  it("阶段支持业务顺序数组和映射，不按显示名称字面序", () => {
    expect(compareGridValues("draft", "done", "rank", "asc", ["draft", "pending", "done"])).toBeLessThan(0);
    expect(compareGridValues("处理中", "已回复", "rank", "asc", { 处理中: 2, 已回复: 3 })).toBeLessThan(0);
  });
  it.each(["code", "zh", "number", "money", "date", "time", "rank"] as const)("%s 的空值在升降序中都排最后", (type) => {
    const value = type === "date" || type === "time" ? "2026-01-01" : 2;
    for (const dir of ["asc", "desc"] as const) {
      for (const blank of [null, undefined, "", "  "]) {
        expect(compareGridValues(blank, value, type, dir)).toBeGreaterThan(0);
        expect(compareGridValues(value, blank, type, dir)).toBeLessThan(0);
      }
    }
  });
  it("无效数字/日期按空值处理，同值保持原来顺序", () => {
    expect(compareGridValues("NaN", 2, "money", "desc")).toBe(1);
    expect(compareGridValues("not-date", new Date("2026-01-01"), "date")).toBe(1);
    const same = [{ ...rows[0], quantity: 1 }, { ...rows[1], quantity: 1 }];
    expect(sortGridRows(same, columns, { key: "quantity", dir: "desc" })).toEqual(same);
  });
});

describe("个人视图", () => {
  it("隐藏列并锁定身份列，宽度、显隐、排序重新挂载后恢复，一次重置清除三项", () => {
    const first = render(grid({ defaultSort: { key: "quantity", dir: "desc" } }));
    fireEvent.keyDown(screen.getByRole("separator", { name: "调整 PN 列宽" }), { key: "ArrowRight" });
    fireEvent.click(screen.getByRole("button", { name: "显示列" }));
    const menu = screen.getByRole("group", { name: "选择显示列" });
    expect(within(menu).getByRole("checkbox", { name: /PN/ })).toBeDisabled();
    fireEvent.click(within(menu).getByRole("checkbox", { name: "名称" }));
    fireEvent.click(screen.getByRole("button", { name: "PN排序" }));
    expect(screen.queryByRole("button", { name: "名称排序" })).not.toBeInTheDocument();
    first.unmount();
    render(grid({ defaultSort: { key: "quantity", dir: "desc" } }));
    expect(screen.getByRole("separator", { name: "调整 PN 列宽" })).toHaveAttribute("aria-valuenow", "128");
    expect(screen.queryByRole("button", { name: "名称排序" })).not.toBeInTheDocument();
    expect(screen.getByText("排序：PN 升序")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "恢复默认视图" }));
    expect(screen.getByRole("separator", { name: "调整 PN 列宽" })).toHaveAttribute("aria-valuenow", "120");
    expect(screen.getByRole("button", { name: "名称排序" })).toBeInTheDocument();
    expect(screen.getByText("排序：数量 降序")).toBeInTheDocument();
    expect(localStorage.getItem(storageKey)).toBeNull();
  });

  it("账号和表格 id 隔离偏好，换回账号恢复", () => {
    const { rerender } = render(grid());
    fireEvent.keyDown(screen.getByRole("separator", { name: "调整 PN 列宽" }), { key: "ArrowRight" });
    rerender(grid({ accountId: "account-b" }));
    expect(screen.getByRole("separator", { name: "调整 PN 列宽" })).toHaveAttribute("aria-valuenow", "120");
    rerender(grid({ id: "another-table" }));
    expect(screen.getByRole("separator", { name: "调整 PN 列宽" })).toHaveAttribute("aria-valuenow", "120");
    rerender(grid());
    expect(screen.getByRole("separator", { name: "调整 PN 列宽" })).toHaveAttribute("aria-valuenow", "128");
  });

  it("真实登录 JWT sub 是稳定账号键，刷新 token 不丢偏好；无身份不落盘", () => {
    const token = (expires: number) => `header.${btoa(JSON.stringify({ sub: "account-7", exp: expires }))}.signature`;
    localStorage.setItem("token", token(1));
    expect(gridAccountId()).toBe("account-7");
    const { unmount } = render(grid({ accountId: undefined }));
    fireEvent.keyDown(screen.getByRole("separator", { name: "调整 PN 列宽" }), { key: "ArrowRight" });
    unmount();
    localStorage.removeItem("token");
    expect(localStorage.getItem(gridStorageKey("parts", "account-7")!)).not.toBeNull();
    localStorage.setItem("token", token(2));
    const second = render(grid({ accountId: undefined }));
    expect(screen.getByRole("separator", { name: "调整 PN 列宽" })).toHaveAttribute("aria-valuenow", "128");
    second.unmount();
    localStorage.clear();
    render(grid({ accountId: undefined }));
    fireEvent.keyDown(screen.getByRole("separator", { name: "调整 PN 列宽" }), { key: "ArrowRight" });
    expect(localStorage.length).toBe(0);
  });

  it("损坏的存储和读写异常都不阻断表格，读取失败退回默认", () => {
    localStorage.setItem(storageKey, "invalid json");
    const first = render(grid());
    expect(screen.getByRole("separator", { name: "调整 PN 列宽" })).toHaveAttribute("aria-valuenow", "120");
    first.unmount();
    vi.spyOn(localStorage, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(localStorage, "setItem").mockImplementation(() => { throw new Error("full"); });
    vi.spyOn(localStorage, "removeItem").mockImplementation(() => { throw new Error("blocked"); });
    render(grid());
    expect(screen.getByRole("separator", { name: "调整 PN 列宽" })).toHaveAttribute("aria-valuenow", "120");
    fireEvent.keyDown(screen.getByRole("separator", { name: "调整 PN 列宽" }), { key: "ArrowRight" });
    expect(screen.getByRole("separator", { name: "调整 PN 列宽" })).toHaveAttribute("aria-valuenow", "128");
    fireEvent.click(screen.getByRole("button", { name: "恢复默认视图" }));
    expect(screen.getByRole("separator", { name: "调整 PN 列宽" })).toHaveAttribute("aria-valuenow", "120");
  });

  it("不恢复已禁用排序的旧列，服务端收到默认排序", () => {
    localStorage.setItem(storageKey, JSON.stringify({ widths: {}, hidden: [], sort: { key: "name", dir: "asc" } }));
    const onSortChange = vi.fn();
    render(grid({ columns: columns.map((column) => column.key === "name" ? { ...column, sortable: false } : column),
      defaultSort: { key: "pn", dir: "desc" }, serverSort: true, onSortChange }));
    expect(screen.getByText("排序：PN 降序")).toBeInTheDocument();
    expect(onSortChange).toHaveBeenCalledWith({ key: "pn", dir: "desc" });
  });
});

it("空、加载和错误状态分别展示，重试可调用", () => {
  const retry = vi.fn();
  const { rerender } = render(grid({ rows: [] }));
  expect(screen.getByText("暂无数据")).toBeInTheDocument();
  rerender(grid({ rows: [], loading: true }));
  expect(screen.getByRole("status")).toHaveTextContent("加载中");
  rerender(grid({ rows: [], error: "读取失败", onRetry: retry }));
  expect(screen.getByRole("alert")).toHaveTextContent("读取失败");
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(retry).toHaveBeenCalledOnce();
});
