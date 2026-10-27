import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import type { Inquiry, Offer } from "../../../api/presales";
import InquiryDetailPage from "../InquiryDetailPage";

const mocks = vi.hoisted(() => ({
  getInquiry: vi.fn(), revisions: vi.fn(), withdrawInquiry: vi.fn(), copyInquiry: vi.fn(), closeInquiry: vi.fn(), library: vi.fn(),
}));
vi.mock("../../../api/presales", () => ({ presales: mocks }));
vi.mock("../../../components/gallery/PartThumb", () => ({ default: ({ pn }: { pn: string }) => <span>{pn} 缩略图</span> }));
vi.mock("../../../components/gallery/GalleryPicker", () => ({
  default: ({ items }: { items: { partId: number; pn: string }[] }) => <div role="dialog" aria-label="图库多选下载">{items.map((item) => item.pn).join("、")}</div>,
}));
vi.mock("../../../components/workflow/Timeline", () => ({
  default: ({ subjectType, subjectId }: { subjectType: string; subjectId: number }) => <div>时间线 {subjectType} {subjectId}</div>,
}));

function offer(id: number, patch: Partial<Offer> = {}): Offer {
  return {
    id, inquiry_id: 5, line_id: 11, part_id: 8, unit_price: "100.00", is_tax_inclusive: true,
    no_stock: false, qty_available: "2", stock_location: "北京", lead_time: "现货", version: 1,
    revision_no: 1, status: "published", valid_from: "2026-01-01T00:00:00Z", valid_until: "2099-12-31T15:59:59Z",
    reporter: { id: id + 20, name: `采购${id}` }, ...patch,
  };
}

function inquiry(patch: Partial<Inquiry> = {}): Inquiry {
  return {
    id: 5, inquiry_no: "XJ0005", kind: "sales_inquiry", status: "submitted", revision: 1, version: 4,
    created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-02T00:00:00Z", customer_id: 31,
    notes: "需要质保", line_count: 1, progress: { quoted_lines: 1, line_count: 1 },
    lines: [{ id: 11, inquiry_id: 5, seq: 1, part_id: 8, pn_raw: "PN-8", line_key: "line-a", qty: "2",
      require_exact_pn: true, status: "active", version: 1, field_values: [{ field_def_id: 1, name: "质保", value: "一年" }] }],
    work_items: [{ id: 1, assignee_id: 21, claimed_by: null, status: "done", paused: false, version: 1 }],
    field_values: [{ field_def_id: 2, name: "交货要求", value: "整批交付" }], offers: [offer(1), offer(2)], ...patch,
  };
}

function EditRoute() {
  const location = useLocation();
  return <><span>编辑路由</span><output aria-label="编辑地址">{location.pathname}{location.search}</output></>;
}

function openPage(data = inquiry()) {
  mocks.getInquiry.mockResolvedValue(data);
  render(<MemoryRouter initialEntries={["/presales/inquiries/5"]}><Routes>
    <Route path="/presales/inquiries/:id" element={<InquiryDetailPage />} />
    <Route path="/presales/inquiries/:id/edit" element={<EditRoute />} />
    <Route path="/presales/offers/new" element={<EditRoute />} />
    <Route path="/presales/tasks/:id" element={<EditRoute />} />
  </Routes></MemoryRouter>);
  return screen.findByText("询价单 XJ0005");
}

beforeEach(() => { vi.clearAllMocks(); mocks.library.mockResolvedValue({}); });
afterEach(() => { cleanup(); });

describe("询价详情", () => {
  it("逐行横向比价，同口径找最低；旧版本及过期不参与，撤销报价不出现", async () => {
    await openPage(inquiry({ offers: [
      offer(1, { unit_price: "100.00" }), offer(2, { unit_price: "90.00" }),
      offer(3, { unit_price: "20.00", status: "stale" }), offer(4, { unit_price: "1.00", status: "withdrawn" }),
      offer(5, { unit_price: "80.00", is_tax_inclusive: false }),
      offer(6, { unit_price: "10.00", valid_until: "2020-01-01T00:00:00Z" }),
    ] }));
    const table = within(screen.getByRole("table", { name: "询价比价矩阵" })).getByRole("row", { name: /PN-8/ });
    const lowest = within(table).getAllByText(/最低价/);
    expect(lowest).toHaveLength(2);
    expect(lowest[0].closest("button")).toHaveAttribute("aria-label", "采纳 PN-8 报价 2");
    expect(lowest[1].closest("button")).toHaveAttribute("aria-label", "采纳 PN-8 报价 5");
    expect(within(table).getByText("基于旧版本")).toBeInTheDocument();
    expect(within(table).getByText("已失效")).toBeInTheDocument();
    expect(within(table).queryByText("采购4")).toBeNull();
    expect(within(table).queryByText(/¥1\.00/)).toBeNull();
    expect(screen.getByText("整批交付")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开 PN-8" }));
    expect(screen.getByText("一年")).toBeInTheDocument();
    expect(screen.getByText("时间线 presales_inquiry 5")).toBeInTheDocument();
  });

  it("后端省略的客户、备注、供应商、报价人不生成占位字段", async () => {
    const data = inquiry({ offers: [offer(1)] });
    delete data.customer_id; delete data.notes;
    delete data.offers![0].reporter;
    await openPage(data);
    expect(screen.queryByText("客户")).toBeNull();
    expect(screen.queryByText("客户要求")).toBeNull();
    expect(screen.queryByText("供应商")).toBeNull();
    expect(screen.queryByText("采购备注")).toBeNull();
    expect(screen.queryByText(/无权查看|已隐藏|私密/)).toBeNull();
    expect(screen.getByText("报价 #1")).toBeInTheDocument();
  });

  it("不同询价行各自找最低价，无货报价不标最低价", async () => {
    const data = inquiry();
    data.lines!.push({ ...data.lines![0], id: 12, seq: 2, line_key: "line-b", pn_raw: "PN-9" });
    data.offers = [offer(1, { unit_price: "500.00" }), offer(2, { line_id: 12, unit_price: "10.00" }),
      offer(3, { line_id: 12, unit_price: null, no_stock: true })];
    await openPage(data);
    expect(within(within(screen.getByRole("table", { name: "询价比价矩阵" })).getByRole("row", { name: /PN-8/ })).getByText("含税最低价").closest("button")).toHaveAttribute("aria-label", "采纳 PN-8 报价 1");
    const second = within(screen.getByRole("table", { name: "询价比价矩阵" })).getByRole("row", { name: /PN-9/ });
    expect(within(second).getByText("含税最低价").closest("button")).toHaveAttribute("aria-label", "采纳 PN-9 报价 2");
    expect(within(second).getByText("无货")).toBeInTheDocument();
    expect(within(second).queryByText("已失效")).toBeNull();
  });

  it("调用 revisions 展示真实可见历史，并复用图库下载组件", async () => {
    const user = userEvent.setup();
    mocks.revisions.mockResolvedValue([{ revision_no: 1, snapshot: offer(1, { unit_price: "75.00" }),
      changed_fields: ["unit_price"], created_at: "2026-10-01T00:00:00Z", reason: "调整有效期" }]);
    await openPage();
    await user.click(screen.getByRole("button", { name: "查看报价 1 历史" }));
    const modal = await screen.findByRole("dialog");
    expect(await within(modal).findByText("75.00")).toBeInTheDocument();
    expect(mocks.revisions).toHaveBeenCalledWith(1);
    expect(within(modal).getByText("原因：调整有效期")).toBeInTheDocument();
    expect(within(modal).queryByText("供应商")).toBeNull();
    await user.click(within(modal).getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: "图库多选下载" }));
    expect(screen.getByRole("dialog", { name: "图库多选下载" })).toHaveTextContent("PN-8");
  });

  it("关单成交可选择本单报价并携带询价版本", async () => {
    const user = userEvent.setup();
    mocks.closeInquiry.mockResolvedValue(inquiry({ status: "closed", closed_outcome: "won", won_offer_id: 2, version: 5 }));
    await openPage();
    await user.click(screen.getByRole("button", { name: "关单" }));
    await user.click(screen.getByRole("combobox", { name: "采纳报价" }));
    await user.click(await screen.findByText(/PN-8 · 报价 #2/));
    await user.click(screen.getByRole("button", { name: "确认关单" }));
    await waitFor(() => expect(mocks.closeInquiry).toHaveBeenCalledWith(5, { expected_version: 4, outcome: "won", won_offer_id: 2 }));
    expect(await screen.findByText("已结束")).toBeInTheDocument();
  });

  it("整单引用外单有效报价可只读比价，成交关单可不采纳本单报价", async () => {
    const user = userEvent.setup();
    const data = inquiry({ offers: [] });
    data.lines![0].ref_offer_id = 7;
    data.lines![0].ref_offer_revision_no = 1;
    data.lines![0].ref_offer = offer(7, { inquiry_id: 3, line_id: 99, unit_price: "120.00" });
    mocks.closeInquiry.mockResolvedValue({ ...data, status: "closed", closed_outcome: "won", version: 5 });
    await openPage(data);
    expect(await screen.findByText("采购7")).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: "询价比价矩阵" })).getByRole("row", { name: /PN-8/ })).toHaveTextContent("¥120.00");
    expect(mocks.library).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "关单" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("采纳本单报价（可选）")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("combobox", { name: "采纳报价" }));
    expect(screen.getByText(/PN-8 · 报价 #7/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "确认关单" }));
    await waitFor(() => expect(mocks.closeInquiry).toHaveBeenCalledWith(5, { expected_version: 4, outcome: "won" }));
  });

  it("外单引用未返回授权快照时不补查当前报价或生成字段占位", async () => {
    const data = inquiry({ offers: [] });
    data.lines![0].ref_offer_id = 7;
    mocks.library.mockResolvedValue({ 8: [offer(7, { inquiry_id: 3, line_id: 99,
      valid_until: "2020-01-01T00:00:00Z", notes: "过期报价备注" })] });
    await openPage(data);
    expect(mocks.library).not.toHaveBeenCalled();
    expect(screen.queryByText("采购7")).toBeNull();
    expect(screen.queryByText("采购备注")).toBeNull();
    expect(screen.queryByText("过期报价备注")).toBeNull();
    expect(screen.queryByRole("button", { name: "采纳 PN-8 报价 7" })).toBeNull();
  });

  it.each([["未成交", "lost"], ["取消", "cancelled"]])("关单%s不携带采纳报价", async (label, outcome) => {
    const user = userEvent.setup();
    mocks.closeInquiry.mockResolvedValue(inquiry({ status: "closed" }));
    await openPage();
    await user.click(screen.getByRole("button", { name: "关单" }));
    await user.click(screen.getByRole("radio", { name: label }));
    await user.click(screen.getByRole("button", { name: "确认关单" }));
    await waitFor(() => expect(mocks.closeInquiry).toHaveBeenCalledWith(5, { expected_version: 4, outcome }));
  });

  it("撤回成功后进入编辑；复制已关单后进入新单编辑", async () => {
    const user = userEvent.setup();
    mocks.withdrawInquiry.mockResolvedValue(inquiry({ status: "revising" }));
    await openPage();
    await user.click(screen.getByRole("button", { name: "撤回修改" }));
    expect(await screen.findByText("编辑路由")).toBeInTheDocument();
    expect(screen.getByLabelText("编辑地址")).toHaveTextContent("/presales/inquiries/5/edit");
    expect(mocks.withdrawInquiry).toHaveBeenCalledWith(5, { expected_version: 4 });
    cleanup();
    mocks.copyInquiry.mockResolvedValue(inquiry({ id: 6, status: "draft" }));
    await openPage(inquiry({ status: "closed" }));
    await user.click(screen.getByRole("button", { name: "复制为新单" }));
    expect(await screen.findByText("编辑路由")).toBeInTheDocument();
    expect(screen.getByLabelText("编辑地址")).toHaveTextContent("/presales/inquiries/6/edit");
    expect(mocks.copyInquiry).toHaveBeenCalledWith(5, { expected_version: 4 });
  });

  it("追加行进入追加模式，409 刷新最新版本", async () => {
    const user = userEvent.setup();
    await openPage();
    await user.click(screen.getByRole("button", { name: "追加行" }));
    expect(await screen.findByText("编辑路由")).toBeInTheDocument();
    expect(screen.getByLabelText("编辑地址")).toHaveTextContent("/presales/inquiries/5/edit?append=1");
    cleanup();
    mocks.withdrawInquiry.mockRejectedValue({ response: { status: 409, data: { detail: "版本冲突" } } });
    await openPage();
    mocks.getInquiry.mockResolvedValue(inquiry({ version: 7, status: "revising" }));
    await user.click(screen.getByRole("button", { name: "撤回修改" }));
    expect(await screen.findByText("撤回修改中")).toBeInTheDocument();
    expect(mocks.getInquiry).toHaveBeenCalledTimes(3);
  });

  it("主动报价关单后复制，进入主动报价草稿编辑入口", async () => {
    const user = userEvent.setup();
    mocks.copyInquiry.mockResolvedValue(inquiry({ id: 6, kind: "proactive_offer", status: "draft" }));
    await openPage(inquiry({ kind: "proactive_offer", status: "closed" }));
    await user.click(screen.getByRole("button", { name: "复制为新单" }));
    expect(await screen.findByText("编辑路由")).toBeInTheDocument();
    expect(screen.getByLabelText("编辑地址")).toHaveTextContent("/presales/offers/new?draft=6");
  });

  it.each([
    ["draft", "继续填写主动报价", "/presales/offers/new?draft=5"],
    ["submitted", "更新 / 撤销报价", "/presales/tasks/5"],
  ] as const)("主动报价 %s 提供可继续处理的入口", async (status, label, target) => {
    const user = userEvent.setup();
    await openPage(inquiry({ kind: "proactive_offer", status }));
    await user.click(screen.getByRole("button", { name: label }));
    expect(await screen.findByText("编辑路由")).toBeInTheDocument();
    expect(screen.getByLabelText("编辑地址")).toHaveTextContent(target);
  });
});

it("引用只展示行内钉住版本，当前报价不覆盖旧价格，续期也不使旧报价复活", async () => {
  const data = inquiry({ offers: [offer(7, { unit_price: "999.00", revision_no: 3 })] });
  data.lines![0] = { ...data.lines![0], ref_offer_id: 7, ref_offer_revision_no: 1,
    ref_offer: offer(7, { unit_price: "120.00", valid_until: "2020-01-01T00:00:00Z",
      latest: { unit_price: "999.00", is_tax_inclusive: false, revision_no: 3 } }) };
  await openPage(data);
  const table = within(screen.getByRole("table", { name: "询价比价矩阵" })).getByRole("row", { name: /PN-8/ });
  expect(within(table).getByText(/¥120.00/)).toBeInTheDocument();
  expect(within(table).getByText("已失效")).toBeInTheDocument();
  expect(within(table).getByText("该报价已更新为 ¥999 · 不含税")).toBeInTheDocument();
  expect(within(table).queryByText("¥999.00 · 含税")).not.toBeInTheDocument();
  expect(mocks.library).not.toHaveBeenCalled();
});

it("成交采用独立钉住快照并显示原报价撤销，未授权 latest 不生成更新提示", async () => {
  await openPage(inquiry({ status: "closed", won_offer_id: 7, won_offer_revision_no: 1,
    won_offer: offer(7, { unit_price: "155.00", original_withdrawn: true }), offers: [] }));
  expect(screen.getByText("报价 #7 · 第 1 版")).toBeInTheDocument();
  expect(screen.getByText(/已失效 ¥155/)).toBeInTheDocument();
  expect(screen.getByText("原报价已撤销")).toBeInTheDocument();
  expect(screen.queryByText(/该报价已更新为/)).not.toBeInTheDocument();
});

it("采纳已更新的沿用报价时，下拉明确告知当前价格税基及版本后按报价ID成交", async () => {
  const user = userEvent.setup();
  const data = inquiry({ offers: [] });
  data.line_responses = [{ line_id: 11, mode: "reuse", ref_offer_id: 7, ref_offer_revision_no: 1,
    created_by: 3, work_item_id: 1, expired: false, offer: offer(7, { line_id: 99, inquiry_id: 9, unit_price: "120.00",
      latest: { unit_price: "75.00", is_tax_inclusive: false, revision_no: 2 } }) }];
  mocks.closeInquiry.mockResolvedValue({ ...data, status: "closed", won_offer_id: 7, won_offer_revision_no: 2 });
  await openPage(data);
  await user.click(screen.getByRole("button", { name: "关单" }));
  await user.click(screen.getByRole("combobox", { name: "采纳报价" }));
  await user.click(await screen.findByText(/PN-8 · 报价 #7 · 采购7 · 当前¥75.00 · 不含税 · 第 2 版/));
  await user.click(screen.getByRole("button", { name: "确认关单" }));
  await waitFor(() => expect(mocks.closeInquiry).toHaveBeenCalledWith(5, { expected_version: 4, outcome: "won", won_offer_id: 7 }));
});

it("同一报价ID在不同询价行钉住不同版本时，逐行使用各自快照比价", async () => {
  const data = inquiry({ offers: [offer(7, { unit_price: "999.00", revision_no: 3 })] });
  data.lines = [
    { ...data.lines![0], linked_offer_ids: [7], linked_offers: [offer(7, { unit_price: "100.00", revision_no: 1 })] },
    { ...data.lines![0], id: 12, seq: 2, line_key: "line-b", pn_raw: "PN-9", linked_offer_ids: [7],
      linked_offers: [offer(7, { line_id: 12, unit_price: "200.00", revision_no: 2 })] },
  ];
  await openPage(data);
  expect(within(screen.getByRole("table", { name: "询价比价矩阵" })).getByRole("row", { name: /PN-8/ })).toHaveTextContent("¥100.00");
  expect(within(screen.getByRole("table", { name: "询价比价矩阵" })).getByRole("row", { name: /PN-9/ })).toHaveTextContent("¥200.00");
  expect(screen.queryByText(/¥999.00/)).not.toBeInTheDocument();
});

it("点矩阵报价选择成交采纳，过期与旧版本不可选择，时间线可以隐藏", async () => {
  const user = userEvent.setup();
  mocks.closeInquiry.mockResolvedValue(inquiry({ status: "closed", won_offer_id: 2 }));
  await openPage(inquiry({ offers: [offer(1, { status: "stale" }), offer(2), offer(3, { valid_until: "2020-01-01T00:00:00Z" })] }));
  expect(screen.getByRole("button", { name: "采纳 PN-8 报价 1" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "采纳 PN-8 报价 3" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "采纳 PN-8 报价 2" }));
  expect(screen.getByRole("button", { name: "采纳 PN-8 报价 2" })).toHaveAttribute("aria-pressed", "true");
  await user.click(screen.getByRole("button", { name: "隐藏时间线" }));
  expect(screen.queryByText("时间线 presales_inquiry 5")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "关单" }));
  await user.click(screen.getByRole("button", { name: "确认关单" }));
  await waitFor(() => expect(mocks.closeInquiry).toHaveBeenCalledWith(5, { expected_version: 4, outcome: "won", won_offer_id: 2 }));
});
