import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { act, cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import type { Inquiry, Offer, OfferInput } from "../../../api/presales";
import type { WorkItem } from "../../../api/workItems";

const mocks = vi.hoisted(() => ({ getInquiry: vi.fn(), library: vi.fn(), listWorkItems: vi.fn(),
  saveOffer: vi.fn(), replyInquiry: vi.fn(), claimWorkItem: vi.fn(), refreshWorkBadges: vi.fn() }));
vi.mock("../../../api/presales", () => ({ presales: mocks }));
vi.mock("../../../api/workItems", () => ({ listWorkItems: mocks.listWorkItems, claimWorkItem: mocks.claimWorkItem }));
vi.mock("../../../components/workflow/useWorkBadge", () => ({ refreshWorkBadges: mocks.refreshWorkBadges }));
vi.mock("../../../components/gallery/PartThumb", () => ({ default: () => null }));
vi.mock("../../../components/parts/CreatePartModal", () => ({ default: () => null }));
vi.mock("../../../components/parts/PartRegistrationForm", () => ({ default: () => null }));

import QuotePage from "../QuotePage";

const task: WorkItem = { id: 71, subject_type: "presales_inquiry", subject_id: 1, pool_key: "presales_inquiry",
  assignee_id: 7, claimed_by: 7, status: "in_progress", version: 3, paused: false,
  summary: {}, created_at: "2026-10-25T00:00:00Z", updated_at: "2026-10-25T00:00:00Z", allowed_actions: [] };
let inquiries: Record<number, Inquiry>;
let tasks: WorkItem[];

function draft(inquiryId: number, lineId: number, price: number, version = 1): Offer {
  return { id: lineId + 70, inquiry_id: inquiryId, line_id: lineId, part_id: lineId - 10,
    unit_price: price, is_tax_inclusive: true, no_stock: false, reporter: { id: 7, name: "采购甲" },
    version, revision_no: 0, status: "draft", valid_from: null };
}

function persist(inquiryId: number, lineId: number, input: OfferInput) {
  const inquiry = inquiries[inquiryId];
  const previous = inquiry.offers?.find((offer) => offer.line_id === lineId);
  const saved = draft(inquiryId, lineId, Number(input.unit_price), (previous?.version ?? 0) + 1);
  inquiries[inquiryId] = { ...inquiry, offers: [...(inquiry.offers ?? []).filter((offer) => offer.line_id !== lineId), saved] };
  return saved;
}

const priceInput = (pn: string) => screen.getByRole("spinbutton", { name: `${pn} 单价` });
const saveButton = (pn: string) => within(priceInput(pn).closest("tr")!).getByRole("button", { name: /^保\s?存$/ });
const setPrice = (pn: string, price: number) => fireEvent.change(priceInput(pn), { target: { value: String(price) } });
const renderQuote = async () => { await act(async () => { render(<MemoryRouter><QuotePage /></MemoryRouter>); }); };

beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear(); localStorage.setItem("role", "purchaser");
  tasks = [task];
  inquiries = { 1: { id: 1, kind: "sales_inquiry", inquiry_no: "XJ-1", status: "submitted", version: 4, revision: 1,
    created_by: 3, created_by_name: "张三", created_at: "2026-10-25T00:00:00Z", updated_at: "2026-10-25T00:00:00Z",
    line_count: 2, progress: { quoted_lines: 0, line_count: 2 },
    lines: [1, 2].map((id) => ({ id: id + 10, inquiry_id: 1, line_key: `line-${id}`, seq: id, part_id: id,
      pn_raw: `PN-${id}`, qty: "2", status: "active", version: 1 })), offers: [],
    work_items: [{ id: 71, assignee_id: 7, claimed_by: 7, status: "in_progress", version: 3, paused: false }] } };
  mocks.getInquiry.mockImplementation(async (id: number) => inquiries[id]);
  mocks.library.mockResolvedValue({});
  mocks.listWorkItems.mockImplementation(async ({ tab }: { tab: string }) => ({ items: tab === "claimed" ? tasks : [],
    total: tab === "claimed" ? tasks.length : 0, page: 1, page_size: 100, can_supervise: false }));
  mocks.saveOffer.mockImplementation(async (id: number, lineId: number, input: OfferInput) => persist(id, lineId, input));
  mocks.replyInquiry.mockImplementation(async (id: number) => {
    const inquiry = inquiries[id];
    inquiries[id] = { ...inquiry, offers: inquiry.offers?.map((offer) => ({ ...offer, status: "published", valid_from: "2026-10-25T01:00:00Z" })),
      work_items: inquiry.work_items?.map((item) => ({ ...item, status: "done" })) };
    return [];
  });
});
afterEach(async () => { await cleanupAntd(); vi.restoreAllMocks(); });

describe("报价页未保存行保护", () => {
  it("已存草稿再改价，保存末行不会发布旧价；逐行保存当前输入后仍自动回复", async () => {
    await renderQuote();
    setPrice("PN-1", 120); fireEvent.click(saveButton("PN-1"));
    await screen.findByText("已填，待回复");
    await waitFor(() => expect(saveButton("PN-1")).toBeEnabled());
    setPrice("PN-1", 180); setPrice("PN-2", 220);
    fireEvent.click(saveButton("PN-2"));
    await screen.findByText(/XJ-1 还有未保存的修改/);
    await waitFor(() => expect(saveButton("PN-1")).toBeEnabled());
    expect(mocks.replyInquiry).not.toHaveBeenCalled();
    expect(Number((priceInput("PN-1") as HTMLInputElement).value)).toBe(180);
    expect(inquiries[1].offers?.find((offer) => offer.line_id === 11)?.unit_price).toBe(120);
    fireEvent.click(saveButton("PN-1"));
    await waitFor(() => expect(mocks.replyInquiry).toHaveBeenCalledOnce());
    expect(mocks.saveOffer).toHaveBeenLastCalledWith(1, 11, expect.objectContaining({ unit_price: 180, offer_id: 81, expected_version: 1 }));
    expect(inquiries[1].offers?.map((offer) => [offer.line_id, offer.unit_price]).sort()).toEqual([[11, 180], [12, 220]]);
  });

  it("末行不报也不能发布另一行未保存的改价，保存后沿用原不报答复", async () => {
    inquiries[1].offers = [draft(1, 11, 120)];
    await renderQuote();
    setPrice("PN-1", 180);
    fireEvent.click(within(priceInput("PN-2").closest("tr")!).getByRole("button", { name: /^不\s?报$/ }));
    fireEvent.click(await screen.findByRole("button", { name: "确定不报" }));
    await screen.findByText(/XJ-1 还有未保存的修改/);
    await waitFor(() => expect(saveButton("PN-1")).toBeEnabled());
    expect(mocks.replyInquiry).not.toHaveBeenCalled();
    expect(Number((priceInput("PN-1") as HTMLInputElement).value)).toBe(180);
    fireEvent.click(saveButton("PN-1"));
    await waitFor(() => expect(mocks.replyInquiry).toHaveBeenCalledWith(1, expect.objectContaining({ line_responses: [
      { line_id: 11, mode: "quote" }, { line_id: 12, mode: "skip", reason: "无货源" },
    ] })));
  });

  it("其他询价的未保存输入不阻止当前单自动回复且保持原输入", async () => {
    inquiries[1].lines = [inquiries[1].lines![0]];
    inquiries[2] = { ...inquiries[1], id: 2, inquiry_no: "XJ-2", offers: [],
      lines: [{ ...inquiries[1].lines![0], id: 21, inquiry_id: 2, part_id: 3, pn_raw: "OTHER-PN" }],
      work_items: [{ ...inquiries[1].work_items![0], id: 72 }] };
    tasks.push({ ...task, id: 72, subject_id: 2 });
    await renderQuote();
    setPrice("OTHER-PN", 999); setPrice("PN-1", 180);
    fireEvent.click(saveButton("PN-1"));
    await waitFor(() => expect(mocks.replyInquiry).toHaveBeenCalledOnce());
    await waitFor(() => expect(priceInput("OTHER-PN")).toBeEnabled());
    expect(mocks.replyInquiry.mock.calls[0][0]).toBe(1);
    expect(Number((priceInput("OTHER-PN") as HTMLInputElement).value)).toBe(999);
    expect(mocks.saveOffer).toHaveBeenCalledOnce();
  });

  it("保存和自动回复在途时禁用行输入，防止确认过程中继续编辑价税字段", async () => {
    inquiries[1].lines = [inquiries[1].lines![0]];
    let releaseSave!: () => void;
    let releaseReply!: () => void;
    const savePending = new Promise<void>((resolve) => { releaseSave = resolve; });
    const replyPending = new Promise<void>((resolve) => { releaseReply = resolve; });
    mocks.saveOffer.mockImplementation(async (id: number, lineId: number, input: OfferInput) => { await savePending; return persist(id, lineId, input); });
    mocks.replyInquiry.mockImplementation(async () => { await replyPending; return []; });
    await renderQuote();
    setPrice("PN-1", 180); fireEvent.click(saveButton("PN-1"));
    await waitFor(() => expect(mocks.saveOffer).toHaveBeenCalledOnce());
    expect(priceInput("PN-1")).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "PN-1 含税" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "PN-1 价格性质" })).toBeDisabled();
    await act(async () => { releaseSave(); });
    await waitFor(() => expect(mocks.replyInquiry).toHaveBeenCalledOnce());
    expect(screen.queryByRole("spinbutton", { name: "PN-1 单价" })).not.toBeInTheDocument();
    await act(async () => { releaseReply(); });
    await waitFor(() => expect(priceInput("PN-1")).toBeEnabled());
    expect(Number((priceInput("PN-1") as HTMLInputElement).value)).toBe(180);
  });
});
