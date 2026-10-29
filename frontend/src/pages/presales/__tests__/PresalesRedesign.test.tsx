import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import type { Inquiry, Offer } from "../../../api/presales";
import type { WorkItem } from "../../../api/workItems";

const mocks = vi.hoisted(() => ({ getInquiry: vi.fn(), library: vi.fn(), listWorkItems: vi.fn(), saveOffer: vi.fn(),
  updateOffer: vi.fn(), replyInquiry: vi.fn(), claimWorkItem: vi.fn(), listInquiries: vi.fn(), createInquiry: vi.fn(),
  submitInquiry: vi.fn(), customers: vi.fn(), listWorkCandidates: vi.fn(), suggestParts: vi.fn(), refreshWorkBadges: vi.fn() }));
vi.mock("../../../api/presales", () => ({ presales: mocks }));
vi.mock("../../../api/workItems", () => ({ listWorkItems: mocks.listWorkItems, claimWorkItem: mocks.claimWorkItem, listWorkCandidates: mocks.listWorkCandidates }));
vi.mock("../../../api/search", () => ({ suggestParts: mocks.suggestParts }));
vi.mock("../../../components/workflow/useWorkBadge", () => ({ refreshWorkBadges: mocks.refreshWorkBadges }));
vi.mock("../../../components/parts/PartRegistrationForm", () => ({ default: () => null }));
vi.mock("../../../components/audience/AudiencePicker", () => ({
  PUBLIC_AUDIENCE: { mode: "public", user_ids: [], group_ids: [] }, default: () => null,
}));

import QuotePage from "../QuotePage";
import AskPage from "../AskPage";
import { answerSentence, lineAnswer } from "../answers";

const task: WorkItem = { id: 71, subject_type: "presales_inquiry", subject_id: 1, pool_key: "presales_inquiry",
  assignee_id: 7, claimed_by: 7, status: "in_progress", version: 3, paused: false,
  summary: {}, created_at: "2026-10-25T00:00:00Z", updated_at: "2026-10-25T00:00:00Z", allowed_actions: [] };
const offer = (patch: Partial<Offer> = {}): Offer => ({ id: 81, inquiry_id: 1, line_id: 11, part_id: 1, unit_price: "120.00",
  is_tax_inclusive: true, no_stock: false, reporter: { id: 7, name: "采购甲" }, version: 1, revision_no: 1,
  status: "published", valid_from: "2026-10-26T03:20:00Z", ...patch });
let inquiry: Inquiry;

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  localStorage.setItem("role", "purchaser");
  inquiry = { id: 1, kind: "sales_inquiry", inquiry_no: "XJ-1", status: "submitted", version: 4, revision: 1,
    created_by: 3, created_by_name: "张三", customer_name: "示例客户", created_at: "2026-10-25T00:00:00Z", updated_at: "2026-10-25T00:00:00Z",
    line_count: 2, progress: { quoted_lines: 0, line_count: 2 },
    lines: [1, 2].map((id) => ({ id: id + 10, inquiry_id: 1, line_key: `line-${id}`, seq: id, part_id: id,
      pn_raw: `PN-${id}`, qty: "2", status: "active", version: 1 })), offers: [],
    work_items: [{ id: 71, assignee_id: 7, claimed_by: 7, status: "in_progress", version: 3, paused: false }] };
  mocks.getInquiry.mockImplementation(async () => inquiry);
  mocks.library.mockResolvedValue({});
  mocks.listWorkItems.mockImplementation(async ({ tab }: { tab: string }) => ({ items: tab === "claimed" ? [task] : [], total: tab === "claimed" ? 1 : 0, page: 1, page_size: 100, can_supervise: false }));
  mocks.replyInquiry.mockResolvedValue([]);
  mocks.listWorkCandidates.mockResolvedValue({ items: [] });
  mocks.customers.mockResolvedValue([]);
  mocks.suggestParts.mockResolvedValue([]);
});
afterEach(async () => { await cleanupAntd(); vi.restoreAllMocks(); });

describe("行答复推导", () => {
  it("有效价优先于不报，取最新报价并生成可转述的话", () => {
    const line = inquiry.lines![0];
    inquiry.offers = [offer({ id: 1, unit_price: "100", valid_from: "2026-10-25T01:00:00Z" }), offer({ id: 2, unit_price: "90", valid_from: "2026-10-26T01:00:00Z" })];
    inquiry.line_responses = [{ line_id: 11, mode: "skip", reason: "停产", created_by: 8, work_item_id: 1, expired: false }];
    const answer = lineAnswer(inquiry, line);
    expect(answer.state).toBe("quoted");
    expect(answerSentence(line, answer)).toMatch(/^PN-1 ¥90 含税，采购甲 .* 报价$/);
  });
  it("没有有效价时显示不报原因，否则待报价", () => {
    inquiry.line_responses = [{ line_id: 11, mode: "skip", reason: "停产", created_by: 8, work_item_id: 1, expired: false, reporter_label: "采购乙" }];
    expect(lineAnswer(inquiry, inquiry.lines![0])).toEqual({ state: "skip", reason: "停产", reporter: "采购乙" });
    expect(lineAnswer(inquiry, inquiry.lines![1])).toEqual({ state: "pending" });
  });
});

describe("报价页", () => {
  const renderQuote = () => render(<MemoryRouter><QuotePage /></MemoryRouter>);

  it("询价人前置、行内填价；整单未填完只存草稿不回复", async () => {
    mocks.saveOffer.mockImplementation(async () => { inquiry = { ...inquiry, offers: [offer({ status: "draft", valid_from: null })] }; return inquiry.offers![0]; });
    renderQuote();
    expect(await screen.findAllByText("张三")).toHaveLength(2);
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN-1 单价" }), { target: { value: "120" } });
    const row = screen.getByRole("spinbutton", { name: "PN-1 单价" }).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /^保\s?存$/ }));
    await waitFor(() => expect(mocks.saveOffer).toHaveBeenCalledWith(1, 11, expect.objectContaining({ unit_price: 120, is_tax_inclusive: true, work_item_id: 71, expected_version: 4 })));
    expect(mocks.replyInquiry).not.toHaveBeenCalled();
  });

  it("最后一行标为不报后自动整单回复，不报原因随回复提交", async () => {
    inquiry.offers = [offer({ status: "draft", valid_from: null })];
    renderQuote();
    const row = (await screen.findByRole("spinbutton", { name: "PN-2 单价" })).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /^不\s?报$/ }));
    fireEvent.change(await screen.findByRole("textbox", { name: "不报原因" }), { target: { value: "停产" } });
    fireEvent.click(screen.getByRole("button", { name: "确定不报" }));
    await waitFor(() => expect(mocks.replyInquiry).toHaveBeenCalledWith(1, expect.objectContaining({ work_item_id: 71, expected_work_item_version: 3,
      line_responses: [{ line_id: 11, mode: "quote" }, { line_id: 12, mode: "skip", reason: "停产" }] })));
  });

  it("已报价行点「更新报价」原地改价，调用更新接口", async () => {
    inquiry.offers = [offer()];
    mocks.updateOffer.mockResolvedValue(offer({ unit_price: "110" }));
    renderQuote();
    fireEvent.click(await screen.findByRole("button", { name: "已报价 1" }));
    fireEvent.click(await screen.findByRole("button", { name: "更新报价" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN-1 单价" }), { target: { value: "110" } });
    fireEvent.click(within(screen.getByRole("spinbutton", { name: "PN-1 单价" }).closest("tr")!).getByRole("button", { name: /^保\s?存$/ }));
    await waitFor(() => expect(mocks.updateOffer).toHaveBeenCalledWith(81, expect.objectContaining({ unit_price: 110, expected_version: 1 })));
    expect(mocks.saveOffer).not.toHaveBeenCalled();
  });
});

describe("询价页", () => {
  it("行内填写 PN 与数量后直接提交，PN 一致记住上次选择", async () => {
    mocks.listInquiries.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 });
    mocks.createInquiry.mockResolvedValue({ ...inquiry, id: 5, inquiry_no: "XJ-5", status: "draft" });
    mocks.submitInquiry.mockResolvedValue({ ...inquiry, id: 5 });
    render(<MemoryRouter><AskPage /></MemoryRouter>);
    fireEvent.change(await screen.findByRole("combobox", { name: "第 1 行 PN" }), { target: { value: "ABC-1" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "第 1 行要求 PN 一致" }));
    fireEvent.click(screen.getByRole("button", { name: "提交询价" }));
    await waitFor(() => expect(mocks.createInquiry).toHaveBeenCalledWith(expect.objectContaining({ kind: "sales_inquiry",
      lines: [expect.objectContaining({ pn_raw: "ABC-1", part_id: null, qty: 1, require_exact_pn: false })] })));
    await waitFor(() => expect(mocks.submitInquiry).toHaveBeenCalledWith(5, { expected_version: 4 }));
    expect(localStorage.getItem("presales:exact-pn")).toBe("0");
  });

  it("PN 联想候选带最新有效报价小标签，精确匹配后描述旁也显示", async () => {
    mocks.listInquiries.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 });
    mocks.suggestParts.mockResolvedValue([
      { part_id: 1, pn_std: "ABC-1", summary: "示例硬盘", match_type: "exact_pn", match_reason: "", matched_terms: [] },
      { part_id: 2, pn_std: "ABC-10", summary: "无报价型号", match_type: "prefix", match_reason: "", matched_terms: [] },
    ]);
    mocks.library.mockResolvedValue({ 1: [offer({ id: 1, unit_price: "100", valid_from: "2026-10-20T01:00:00Z",
      more: [offer({ id: 2, unit_price: "95", is_tax_inclusive: false, valid_from: "2026-10-27T01:00:00Z" })] })] });
    render(<MemoryRouter><AskPage /></MemoryRouter>);
    fireEvent.change(await screen.findByRole("combobox", { name: "第 1 行 PN" }), { target: { value: "ABC-1" } });
    expect((await screen.findAllByText(/最新 ¥95 不含税 · 10-27/)).length).toBeGreaterThan(0);
    expect(mocks.library).toHaveBeenCalledWith([1, 2]);
    expect(screen.getByText("示例硬盘", { selector: "td *" })).toBeInTheDocument();
  });

  it("我的询价逐行显示报价和不报原因", async () => {
    inquiry.offers = [offer()];
    inquiry.line_responses = [{ line_id: 12, mode: "skip", reason: "停产", created_by: 7, work_item_id: 71, expired: false }];
    mocks.listInquiries.mockResolvedValue({ items: [inquiry], total: 1, page: 1, page_size: 10 });
    render(<MemoryRouter><AskPage /></MemoryRouter>);
    expect(await screen.findByText("¥120 含税")).toBeInTheDocument();
    expect(screen.getByText("不报：停产")).toBeInTheDocument();
    expect(screen.getByText("全部已答复")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制 PN-1 报价给客户" })).toBeInTheDocument();
  });
});
