import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { presales, type Inquiry } from "../../../api/presales";
import { presalesPool, type PoolLine, type PoolPage as PoolPageData } from "../../../api/presalesPool";
import PoolPage from "../PoolPage";

vi.mock("../NewInquiryPanel", () => ({ default: () => <div>新建询价表单</div> }));
vi.mock("../../../components/gallery/PartThumb", () => ({ default: () => null }));
vi.mock("../../../components/workbook", () => ({ WorkbookExportButton: ({ label }: { label: string }) => <button>{label}</button> }));

function line(patch: Partial<PoolLine> = {}): PoolLine {
  return { line_id: 1, line_version: 3, inquiry_id: 9, inquiry_no: "XJ20261101001", inquiry_version: 1, inquiry_status: "submitted",
    closed_outcome: null, priority: "normal", submitted_at: "2026-11-01T00:00:00Z", requester: "张三", is_mine: false, inquiry_notes: null,
    part_id: 5, pn: "PN-1", description: "内存", qty: "2.000", require_exact_pn: false, return_defective: true, notes: null,
    expected_delivery: null, answer_state: "pending", answer_reason: null, answered_by: null, answered_at: null, price: null, latest_same_pn: null, ...patch };
}

const quoted = (value = "100.00", by = "李四", version = 4) => line({ line_version: version, answer_state: "quoted", answered_by: by, answered_at: "2026-11-01T01:00:00Z",
  price: { offer_id: 7, unit_price: value, is_tax_inclusive: true, price_basis: "sale", lead_time: null, stock_location: null, qty_available: null, notes: null, supplier_id: null, supplier_name: null } });

function page(items: PoolLine[]): PoolPageData {
  return { items, total: items.length, page: 1, page_size: 50, counts: { pending: 1, quoted: 2, skip: 0, mine: 0, all: 3, drafts: 0 } };
}

const show = () => render(<AntApp><MemoryRouter><PoolPage /></MemoryRouter></AntApp>);

describe("询价报价共享池", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
    vi.spyOn(presales, "suppliers").mockResolvedValue([]);
    vi.spyOn(presalesPool, "counts").mockResolvedValue({ pending: 1, quoted: 2, skip: 0, mine: 0, all: 3, drafts: 0 });
  });
  afterEach(cleanup);

  it("一次请求显示待报价行和各筛选数量，不逐单读详情", async () => {
    const lines = vi.spyOn(presalesPool, "lines").mockResolvedValue(page([line()]));
    show();
    expect(await screen.findByText("PN-1")).toBeInTheDocument();
    expect(lines).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "待报价 1" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "已报价 2" })).toBeInTheDocument();
    expect(screen.getByText("张三")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "关闭询价 XJ20261101001" })).toBeNull();
  });

  it("报价表按未关单的询价选择来源，已关单行不可勾选", async () => {
    const open = line({ inquiry_no: "XJ1" });
    const closed = line({ line_id: 2, inquiry_id: 2, inquiry_no: "XJ2", pn: "PN-2", inquiry_status: "closed", closed_outcome: "lost" });
    vi.spyOn(presalesPool, "lines").mockResolvedValue(page([open, closed]));
    show();
    await screen.findByText("未成交");
    expect(screen.getByLabelText(`选择报价表来源 XJ1 ${open.pn}`)).toBeEnabled();
    expect(screen.getByLabelText(`选择报价表来源 XJ2 ${closed.pn}`)).toBeDisabled();
    expect(screen.getByLabelText("PN-1 单价")).toBeEnabled();
    expect(screen.getByLabelText("PN-2 单价")).toBeDisabled();
    expect(within(screen.getByText("XJ2").closest("tr")!).queryByText("不报")).toBeNull();
    expect(screen.getByText(/旧版直接引用报价的行请在页面改价/)).toBeInTheDocument();
  });

  it.each([
    [true, false, false], [false, true, false], [true, true, true],
  ])("绑定入口校验动作权限 %s 和主数据权限 %s", async (action, master, allowed) => {
    localStorage.setItem("permissions", JSON.stringify({ page_presales: true,
      action_presales_bind_part: action, page_master_data: master }));
    vi.spyOn(presalesPool, "lines").mockResolvedValue(page([line({ part_id: null })]));
    show();
    expect(await screen.findByText("待绑定型号")).toBeInTheDocument();
    expect(!!screen.queryByRole("button", { name: "绑定 / 建档" })).toBe(allowed);
  });

  it("填价保存后只替换这一行，并显示更新人", async () => {
    const lines = vi.spyOn(presalesPool, "lines").mockResolvedValue(page([line()]));
    const setPrice = vi.spyOn(presalesPool, "setPrice").mockResolvedValue(quoted("88.50", "王五"));
    vi.spyOn(presalesPool, "counts").mockResolvedValue({ pending: 0, quoted: 3, skip: 0, mine: 0, all: 3, drafts: 0 });
    show();
    fireEvent.change(await screen.findByRole("spinbutton", { name: "PN-1 单价" }), { target: { value: "88.5" } });
    fireEvent.click(screen.getByRole("button", { name: "保 存" }));
    await waitFor(() => expect(setPrice).toHaveBeenCalledWith(1, { expected_version: 3, unit_price: 88.5, is_tax_inclusive: true, price_basis: "sale" }));
    expect(await screen.findByText(/王五/)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "待报价 0" })).toBeInTheDocument();
    expect(lines).toHaveBeenCalledTimes(1);
  });

  it("并发冲突时显示他人的最新价并保留本人输入", async () => {
    vi.spyOn(presalesPool, "lines").mockResolvedValue(page([line()]));
    vi.spyOn(presalesPool, "setPrice").mockRejectedValue({ response: { status: 409, data: { detail: { code: "version_conflict", latest: quoted("100.00", "李四") } } } });
    show();
    fireEvent.change(await screen.findByRole("spinbutton", { name: "PN-1 单价" }), { target: { value: "90" } });
    fireEvent.click(screen.getByRole("button", { name: "保 存" }));
    expect(await screen.findByText(/刚被 李四 更新/)).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "PN-1 单价" })).toHaveValue("90.00");
  });

  it("刷新和保存询价备注都不会替过期本地报价接受他人的新价格", async () => {
    const latest = quoted("118.00", "李四", 4);
    vi.spyOn(presalesPool, "lines").mockResolvedValueOnce(page([line()])).mockResolvedValue(page([latest]));
    const afterNote = { ...latest, line_version: 5, notes: "送到库房" };
    const edit = vi.spyOn(presalesPool, "edit").mockResolvedValue(afterNote);
    const save = vi.spyOn(presalesPool, "setPrice")
      .mockRejectedValueOnce({ response: { status: 409, data: { detail: { code: "version_conflict", latest: afterNote } } } })
      .mockResolvedValue(quoted("90.00", "张三", 6));
    show();
    fireEvent.change(await screen.findByRole("spinbutton", { name: "PN-1 单价" }), { target: { value: "90" } });
    fireEvent.click(screen.getByRole("button", { name: /刷\s*新/ }));
    expect(await screen.findByText(/行已更新，当前答复.*118/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /展开行|Expand row/ }));
    const notes = screen.getByRole("textbox", { name: "PN-1 询价备注" });
    fireEvent.change(notes, { target: { value: "送到库房" } });
    fireEvent.blur(notes);
    await waitFor(() => expect(edit).toHaveBeenCalledWith(1, { expected_version: 4, notes: "送到库房" }));
    const button = screen.getByRole("button", { name: "保 存" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(save).toHaveBeenNthCalledWith(1, 1, expect.objectContaining({ expected_version: 3, unit_price: 90 })));
    expect(await screen.findByText(/刚被 李四 更新/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "已核对，按此版本编辑" }));
    fireEvent.click(button);
    await waitFor(() => expect(save).toHaveBeenNthCalledWith(2, 1, expect.objectContaining({ expected_version: 5, unit_price: 90 })));
  });

  it("已报价行改坏件返回要先确认，确认后才提交并回到待报价", async () => {
    vi.spyOn(presalesPool, "lines").mockResolvedValue(page([quoted()]));
    const edit = vi.spyOn(presalesPool, "edit").mockResolvedValue(line({ line_version: 5, return_defective: false }));
    show();
    fireEvent.click(await screen.findByRole("checkbox", { name: "PN-1 坏件返回" }));
    const dialog = await screen.findByRole("dialog");
    expect(edit).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "确定修改" }));
    await waitFor(() => expect(edit).toHaveBeenCalledWith(1, { expected_version: 4, return_defective: false, confirm_reset: true }));
    expect(await screen.findByRole("checkbox", { name: "PN-1 坏件返回" })).not.toBeChecked();
  });

  it("待报价行改坏件返回直接生效，不打扰用户", async () => {
    vi.spyOn(presalesPool, "lines").mockResolvedValue(page([line()]));
    const edit = vi.spyOn(presalesPool, "edit").mockResolvedValue(line({ line_version: 4, return_defective: false }));
    show();
    fireEvent.click(await screen.findByRole("checkbox", { name: "PN-1 坏件返回" }));
    await waitFor(() => expect(edit).toHaveBeenCalledWith(1, { expected_version: 3, return_defective: false }));
  });

  it("同型号最新价可一键沿用，未建档行不能填价", async () => {
    const hint = { offer_id: 8, unit_price: "66.00", is_tax_inclusive: false, price_basis: "cost" as const, reporter: "赵六", at: "2026-10-30T00:00:00Z" };
    vi.spyOn(presalesPool, "lines").mockResolvedValue(page([line({ latest_same_pn: hint }), line({ line_id: 2, pn: "PN-2", part_id: null })]));
    const setPrice = vi.spyOn(presalesPool, "setPrice").mockResolvedValue(quoted("66.00"));
    show();
    fireEvent.click(await screen.findByRole("button", { name: /沿用/ }));
    fireEvent.click(screen.getAllByRole("button", { name: "保 存" })[0]);
    await waitFor(() => expect(setPrice).toHaveBeenCalledWith(1, { expected_version: 3, unit_price: 66, is_tax_inclusive: false, price_basis: "cost" }));
    expect(screen.getByRole("spinbutton", { name: "PN-2 单价" })).toBeDisabled();
    expect(screen.getByText("待绑定型号")).toBeInTheDocument();
  });

  it("已报价行改数量后取消，输入框恢复原值且不提交", async () => {
    vi.spyOn(presalesPool, "lines").mockResolvedValue(page([quoted()]));
    const edit = vi.spyOn(presalesPool, "edit");
    show();
    const input = await screen.findByRole("spinbutton", { name: "PN-1 数量" });
    fireEvent.change(input, { target: { value: "9" } });
    fireEvent.blur(input);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /取\s*消/ }));
    await waitFor(() => expect(screen.getByRole("spinbutton", { name: "PN-1 数量" })).toHaveValue("2"));
    expect(edit).not.toHaveBeenCalled();
  });

  it("展开行可改交期并随保存提交，未改的字段不传", async () => {
    vi.spyOn(presalesPool, "lines").mockResolvedValue(page([quoted()]));
    const setPrice = vi.spyOn(presalesPool, "setPrice").mockResolvedValue(quoted("100.00", "李四", 5));
    show();
    fireEvent.click(await screen.findByRole("button", { name: /展开行|Expand row/ }));
    fireEvent.change(await screen.findByRole("textbox", { name: "PN-1 交期" }), { target: { value: "3天" } });
    fireEvent.click(screen.getByRole("button", { name: "保 存" }));
    await waitFor(() => expect(setPrice).toHaveBeenCalledWith(1, expect.objectContaining({ expected_version: 4, unit_price: 100, lead_time: "3天" })));
  });

  it("失焦保存询价备注后仍可用新行版本提交原来填写的单价和交期", async () => {
    vi.spyOn(presalesPool, "lines").mockResolvedValue(page([line()]));
    const edit = vi.spyOn(presalesPool, "edit").mockResolvedValue(line({ line_version: 4, notes: "原厂包装" }));
    const save = vi.spyOn(presalesPool, "setPrice").mockResolvedValue(quoted("88.50", "李四", 5));
    show();
    fireEvent.change(await screen.findByRole("spinbutton", { name: "PN-1 单价" }), { target: { value: "88.50" } });
    fireEvent.click(screen.getByRole("button", { name: /展开行|Expand row/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "PN-1 交期" }), { target: { value: "3天" } });
    const notes = screen.getByRole("textbox", { name: "PN-1 询价备注" });
    fireEvent.change(notes, { target: { value: "原厂包装" } });
    fireEvent.blur(notes);
    await waitFor(() => expect(edit).toHaveBeenCalledWith(1, { expected_version: 3, notes: "原厂包装" }));
    const button = screen.getByRole("button", { name: "保 存" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(save).toHaveBeenCalledWith(1, expect.objectContaining({
      expected_version: 4, unit_price: 88.5, lead_time: "3天",
    })));
  });

  it("只改价时不传交期等字段，服务端保留原值", async () => {
    vi.spyOn(presalesPool, "lines").mockResolvedValue(page([quoted()]));
    const setPrice = vi.spyOn(presalesPool, "setPrice").mockResolvedValue(quoted("90.00", "李四", 5));
    show();
    fireEvent.change(await screen.findByRole("spinbutton", { name: "PN-1 单价" }), { target: { value: "90" } });
    fireEvent.click(screen.getByRole("button", { name: "保 存" }));
    await waitFor(() => expect(setPrice).toHaveBeenCalled());
    expect(setPrice.mock.calls[0][1]).not.toHaveProperty("lead_time");
    expect(setPrice.mock.calls[0][1]).not.toHaveProperty("supplier_id");
  });

  it("改询价备注不弹作废确认", async () => {
    vi.spyOn(presalesPool, "lines").mockResolvedValue(page([quoted()]));
    const edit = vi.spyOn(presalesPool, "edit").mockResolvedValue(quoted("100.00", "李四", 5));
    show();
    fireEvent.click(await screen.findByRole("button", { name: /展开行|Expand row/ }));
    const notes = await screen.findByRole("textbox", { name: "PN-1 询价备注" });
    fireEvent.change(notes, { target: { value: "加急" } });
    fireEvent.blur(notes);
    await waitFor(() => expect(edit).toHaveBeenCalledWith(1, { expected_version: 4, notes: "加急" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("行内关单默认未成交，选已成交时带这一行的报价", async () => {
    const lines = vi.spyOn(presalesPool, "lines").mockResolvedValue(page([{ ...quoted(), is_mine: true }]));
    const close = vi.spyOn(presales, "closeInquiry").mockResolvedValue({} as Inquiry);
    show();
    fireEvent.click(await screen.findByRole("button", { name: "关闭询价 XJ20261101001" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("radio", { name: /已成交/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "确定关单" }));
    await waitFor(() => expect(close).toHaveBeenCalledWith(9, { expected_version: 1, outcome: "won", won_offer_id: 7 }));
    await waitFor(() => expect(lines).toHaveBeenCalledTimes(2));
  });

  it("主管可关闭他人询价，未报价行没有已成交选项；草稿入口显示本人草稿数", async () => {
    localStorage.setItem("permissions", JSON.stringify({ action_presales_supervise: true }));
    vi.spyOn(presalesPool, "lines").mockResolvedValue({ ...page([line()]), counts: { pending: 1, quoted: 0, skip: 0, mine: 1, all: 1, drafts: 2 } });
    show();
    expect(await screen.findByRole("link", { name: "我的草稿 2" })).toHaveAttribute("href", "/presales/inquiries?status=draft");
    fireEvent.click(screen.getByRole("button", { name: "关闭询价 XJ20261101001" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("radio", { name: /已成交/ })).not.toBeInTheDocument();
  });

  it("切换筛选和搜索都走服务端", async () => {
    const lines = vi.spyOn(presalesPool, "lines").mockResolvedValue(page([line()]));
    show();
    await screen.findByText("PN-1");
    fireEvent.click(screen.getByRole("button", { name: "我发起的 0" }));
    await waitFor(() => expect(lines).toHaveBeenLastCalledWith({ tab: "mine", page: 1, page_size: 50 }));
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索询价报价" }), { target: { value: "PN-9" } });
    fireEvent.keyDown(screen.getByRole("searchbox", { name: "搜索询价报价" }), { key: "Enter", code: "Enter", charCode: 13 });
    await waitFor(() => expect(lines).toHaveBeenLastCalledWith({ tab: "mine", page: 1, page_size: 50, q: "PN-9" }));
  });
});
