import { openLine, closeLine } from "./nativeUi";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { cleanupAntd, render } from "../../../test/antdRender";
import type { Inquiry, Offer } from "../../../api/presales";

const mocks = vi.hoisted(() => ({ listInquiries: vi.fn(), getInquiry: vi.fn(), listWorkCandidates: vi.fn(), fetchPurchaseAnalyticsOptions: vi.fn() }));
vi.mock("../../../api/presales", () => ({ presales: { listInquiries: mocks.listInquiries, getInquiry: mocks.getInquiry } }));
vi.mock("../../../api/workItems", () => ({ listWorkCandidates: mocks.listWorkCandidates }));
vi.mock("../../../api/purchaseAnalytics", () => ({ fetchPurchaseAnalyticsOptions: mocks.fetchPurchaseAnalyticsOptions }));
vi.mock("../../../nav", () => ({ readPermissionMap: () => JSON.parse(localStorage.getItem("permissions") || "{}") }));
import InquiryListPage from "../InquiryListPage";

const row: Inquiry = { id: 1, inquiry_no: "XJ001", kind: "sales_inquiry", status: "submitted", version: 1, revision: 1,
  customer_id: 5, customer_name: "客户甲", line_count: 2, progress: { quoted_lines: 2, line_count: 2 },
  updated_at: "2026-01-01T10:00:00Z", created_at: "2026-01-01T00:00:00Z" };
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  mocks.listInquiries.mockResolvedValue({ items: [row], total: 1, page: 1, page_size: 20 });
  mocks.getInquiry.mockResolvedValue({ ...row, work_items: [
    { id: 11, assignee_id: 2, claimed_by: null, status: "done", paused: false, version: 1 },
    { id: 12, assignee_id: 3, claimed_by: null, status: "pending", paused: false, version: 1 },
  ] });
  mocks.listWorkCandidates.mockResolvedValue({ items: [{ id: 2, display_name: "采购甲" }, { id: 3, display_name: "采购乙" }] });
  mocks.fetchPurchaseAnalyticsOptions.mockResolvedValue({ data: { customers: [{ customer_id: 5, customer: "客户甲" }], selected_customers: [] } });
});
afterEach(cleanupAntd);

it("列表按处理人回复显示 1/2 进度与标签，主管可切换全部", async () => {
  localStorage.setItem("permissions", JSON.stringify({ action_presales_supervise: true }));
  render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  expect(await screen.findByText("处理中「已回复 1/2」")).toBeVisible();
  expect(screen.getByText("采购甲 · 已回复")).toBeVisible();
  expect(screen.getByText("采购乙 · 待处理")).toBeVisible();
  // 客户名来自售前列表项 customer_name（T18），不再借用需采购分析权限的 options 接口。
  expect(await screen.findByText("客户甲")).toBeVisible();
  expect(mocks.fetchPurchaseAnalyticsOptions).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("tab", { name: "全部" }));
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ scope: "all" })));
});

it("普通用户无全部入口；类型/状态使用服务端支持的筛选键，隐藏字段不补占位", async () => {
  const hidden = { ...row }; delete hidden.customer_id;
  mocks.listInquiries.mockResolvedValue({ items: [hidden], total: 1 });
  mocks.getInquiry.mockResolvedValue({ ...hidden, work_items: [{ id: 11, status: "done", paused: false, version: 1 }] });
  render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  await screen.findAllByText("已回复", { selector: ".ant-tag" });
  expect(screen.queryByRole("tab", { name: "全部" })).not.toBeInTheDocument();
  expect(screen.queryByText(/账号 #/)).not.toBeInTheDocument();
  expect(screen.queryByText(/客户 #/)).not.toBeInTheDocument();
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "询价类型" }));
  fireEvent.click(screen.getAllByText("主动报价").pop()!);
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ kind: "proactive_offer" })));
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "询价状态" }));
  fireEvent.click(screen.getByText("草稿", { selector: ".ant-select-item-option-content" }));
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ status: "draft" })));
  // T18 起日期与搜索走服务端，不再有"仅作用于当前页"的提示。
  expect(screen.queryByText(/作用于当前页/)).not.toBeInTheDocument();
});

it("表头三态排序调用服务端并回到第一页，取消时恢复最近动态默认序", async () => {
  render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  await screen.findByText("XJ001");
  fireEvent.click(screen.getByRole("columnheader", { name: /行数/ }));
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ page: 1, sort: "line_count", dir: "asc" })));
  fireEvent.click(screen.getByRole("columnheader", { name: /行数/ }));
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ sort: "line_count", dir: "desc" })));
  fireEvent.click(screen.getByRole("columnheader", { name: /行数/ }));
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ sort: "updated_at", dir: "desc" })));
});

it("共享页复用可见范围并排除我的单据，不推断被过滤的客户和报价人", async () => {
  const shared = { ...row, id: 2, inquiry_no: "XJ002" }; delete shared.customer_id;
  mocks.listInquiries.mockImplementation(({ scope }: { scope: string }) => Promise.resolve({ items: scope === "shared" ? [shared] : [row], total: 1 }));
  mocks.getInquiry.mockImplementation((id: number) => Promise.resolve(id === 2 ? shared : row));
  render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  await screen.findByText("XJ001");
  fireEvent.click(screen.getByRole("tab", { name: "共享给我" }));
  expect(await screen.findByText("XJ002")).toBeVisible();
  await waitFor(() => expect(screen.queryByText("XJ001")).not.toBeInTheDocument());
});

it("共享页 shared 持续版本冲突时只自动重试一次，随后保留错误供手动重试", async () => {
  const pending: Array<(reason: unknown) => void> = [];
  mocks.listInquiries.mockImplementation(({ scope }: { scope: string }) => scope === "shared"
    ? new Promise((_, reject) => { pending.push(reject); })
    : Promise.resolve({ items: [row], total: 1 }));
  render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  await screen.findByText("XJ001");
  fireEvent.click(screen.getByRole("tab", { name: "共享给我" }));
  await waitFor(() => expect(pending).toHaveLength(1));
  const conflict = { response: { status: 409, data: { detail: { code: "version_conflict", message: "记录版本已更新" } } } };
  await act(async () => { pending[0](conflict); });
  await waitFor(() => expect(pending).toHaveLength(2));
  await act(async () => { pending[1](conflict); });
  expect(pending).toHaveLength(2);
  expect(screen.getByText("记录版本已更新")).toBeVisible();
  expect(screen.queryByText("加载中")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));
  await waitFor(() => expect(pending).toHaveLength(3));
});

it("PN 状态按报价和版本去重，采购明确不报的行显示原因而非待报价", async () => {
  const quoted: Offer = { id: 71, inquiry_id: 1, line_id: 11, part_id: 1, version: 1, revision_no: 2,
    status: "published", unit_price: "100", is_tax_inclusive: true,
    valid_from: "2020-01-01T00:00:00Z" };
  mocks.getInquiry.mockResolvedValue({ ...row, offers: [quoted], lines: [
    { id: 11, inquiry_id: 1, line_key: "quoted", seq: 1, pn_raw: "PN-QUOTED", qty: 1, status: "active", version: 1,
      ref_offer_id: quoted.id, ref_offer: quoted, linked_offers: [quoted] },
    { id: 12, inquiry_id: 1, line_key: "skipped", seq: 2, pn_raw: "PN-SKIPPED", qty: 1, status: "active", version: 1 },
  ], line_responses: [
    { line_id: 11, mode: "reuse", created_by: 2, offer: quoted, ref_offer_id: quoted.id },
    { line_id: 12, mode: "skip", created_by: 2, reason: "已停产，无法供货" },
  ], work_items: [{ id: 11, assignee_id: 2, status: "done", paused: false, version: 1 }] });
  render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  await screen.findByText("PN-QUOTED");
  expect(screen.getByText("PN-SKIPPED")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "报价概览" }));
  expect(screen.getByText("已引用 · 1 条有效报价")).toBeVisible();
  expect(screen.queryByText(/2 条有效报价|3 条有效报价|4 条有效报价/)).not.toBeInTheDocument();
  expect(screen.getByText(/不报.*已停产，无法供货/)).toBeVisible();
  expect(screen.queryByText("待报价", { exact: true })).not.toBeInTheDocument();
});

it("列表摘要使用本行钉住版本，被取代的引用不因当前报价有效而恢复", async () => {
  const current: Offer = { id: 71, inquiry_id: 1, line_id: 11, part_id: 1, version: 2, revision_no: 2,
    status: "published", unit_price: "50", is_tax_inclusive: true,
    valid_from: "2020-01-01T00:00:00Z" };
  mocks.getInquiry.mockResolvedValue({ ...row, offers: [current], lines: [
    { id: 11, inquiry_id: 1, line_key: "referenced", seq: 1, pn_raw: "PN-PINNED", qty: 1, status: "active", version: 1,
      ref_offer_id: current.id, ref_offer_revision_no: 1,
      ref_offer: { ...current, revision_no: 1, unit_price: "100", expired: true } },
  ] });
  render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  await screen.findByText("PN-PINNED");
  fireEvent.click(screen.getByRole("button", { name: "报价概览" }));
  expect(screen.queryByText(/条有效报价/)).not.toBeInTheDocument();
  expect(screen.getByText("待报价", { exact: true })).toBeVisible();
});

it("引用快照不随时间失效（D-0046）", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-25T15:59:59.500Z"));
  const pinned: Offer = { id: 71, inquiry_id: 1, line_id: 11, part_id: 1, version: 1, revision_no: 1,
    status: "published", unit_price: "100", is_tax_inclusive: true,
    valid_from: "2026-10-25T00:00:00Z" };
  mocks.getInquiry.mockResolvedValue({ ...row, offers: [{ ...pinned, revision_no: 2 }], lines: [
    { id: 11, inquiry_id: 1, line_key: "referenced", seq: 1, pn_raw: "PN-PINNED", qty: 1, status: "active", version: 1,
      ref_offer_id: pinned.id, ref_offer_revision_no: 1, ref_offer: pinned },
  ] });
  const view = render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  try {
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "报价概览" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(499); });
    expect(screen.getByText("已引用 · 1 条有效报价")).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(2 * 86_400_000); });
    expect(screen.getByText("已引用 · 1 条有效报价")).toBeVisible();
  } finally {
    view.unmount();
    vi.useRealTimers();
  }
});

it("共享分页直接请求 shared，服务端结果完整展示，不再拉取 mine 排除", async () => {
  render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  await screen.findByText("XJ001");
  mocks.listInquiries.mockClear();
  fireEvent.click(screen.getByRole("tab", { name: "共享给我" }));
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenCalledWith(expect.objectContaining({ scope: "shared", page: 1 })));
  expect(await screen.findByText("XJ001")).toBeVisible();
  expect(mocks.listInquiries.mock.calls.every(([params]) => params.scope === "shared")).toBe(true);
});

it("草稿沿用和不报标为未回复，不显示为有效报价或正式不报", async () => {
  const reused: Offer = { id: 90, inquiry_id: 9, line_id: 99, part_id: 1, status: "published", revision_no: 1, version: 1,
    unit_price: "123", valid_from: "2020-01-01T00:00:00Z" };
  mocks.getInquiry.mockResolvedValue({ ...row, offers: [], work_items: [{ id: 11, assignee_id: 2, status: "in_progress", paused: false, version: 1 }],
    lines: [11, 12].map((id) => ({ id, line_key: `line-${id}`, inquiry_id: 1, seq: id, pn_raw: `DRAFT-${id}`, qty: 1, status: "active", version: 1 })),
    line_responses: [{ line_id: 11, mode: "reuse", ref_offer_id: 90, offer: reused, is_draft: true, created_by: 2 },
      { line_id: 12, mode: "skip", reason: "草稿无货", is_draft: true, created_by: 2 }] });
  render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  await screen.findByText("DRAFT-11");
  expect(screen.getByText("DRAFT-12")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "报价概览" }));
  expect(screen.getAllByText("草稿（未回复）")).toHaveLength(2);
  expect(screen.queryByText(/条有效报价/)).not.toBeInTheDocument();
  expect(screen.queryByText(/不报 · 草稿无货/)).not.toBeInTheDocument();
  expect(screen.getByText("处理中「已回复 0/1」")).toBeVisible();
});

it("当前答复失效时列表和概览一致，不把历史引用价计入有效价格合计", async () => {
  const historical: Offer = { id: 71, inquiry_id: 1, line_id: 11, part_id: 1, version: 1, revision_no: 1,
    status: "published", unit_price: "100", is_tax_inclusive: true, valid_from: "2020-01-01T00:00:00Z" };
  mocks.getInquiry.mockResolvedValue({ ...row, lines: [
    { id: 11, inquiry_id: 1, line_key: "historical", seq: 1, pn_raw: "PN-HISTORY", qty: 1, status: "active", version: 1,
      ref_offer_id: historical.id, ref_offer: historical },
  ], line_answers: [{ line_id: 11, state: "unavailable", responded: true, reason: "已回复，当前无有效报价" }] });
  render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  await screen.findByText("PN-HISTORY 已回复，当前无有效报价");
  fireEvent.click(screen.getByRole("button", { name: "报价概览" }));
  expect(screen.getAllByText("PN-HISTORY 已回复，当前无有效报价")).toHaveLength(2);
  expect(screen.queryByText(/条有效报价/)).not.toBeInTheDocument();
  expect(screen.getByText("最低报价合计（含税）：—")).toBeVisible();
});
