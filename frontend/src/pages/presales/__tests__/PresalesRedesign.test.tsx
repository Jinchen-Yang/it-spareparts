import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import type { Inquiry, Offer } from "../../../api/presales";
import type { WorkItem } from "../../../api/workItems";

const mocks = vi.hoisted(() => ({ getInquiry: vi.fn(), library: vi.fn(), listWorkItems: vi.fn(), saveOffer: vi.fn(),
  updateOffer: vi.fn(), replyInquiry: vi.fn(), claimWorkItem: vi.fn(), listInquiries: vi.fn(), createInquiry: vi.fn(),
  submitInquiry: vi.fn(), directOffer: vi.fn(), urgeInquiry: vi.fn(), customers: vi.fn(), listWorkCandidates: vi.fn(), suggestParts: vi.fn(), refreshWorkBadges: vi.fn() }));
vi.mock("../../../api/presales", () => ({ presales: mocks }));
vi.mock("../../../api/workItems", () => ({ listWorkItems: mocks.listWorkItems, claimWorkItem: mocks.claimWorkItem, listWorkCandidates: mocks.listWorkCandidates }));
vi.mock("../../../api/search", () => ({ suggestParts: mocks.suggestParts }));
vi.mock("../../../components/workflow/useWorkBadge", () => ({ refreshWorkBadges: mocks.refreshWorkBadges }));
vi.mock("../../../components/parts/PartRegistrationForm", () => ({ default: () => null }));
vi.mock("../../../components/gallery/PartThumb", () => ({ default: ({ pn }: { pn: string }) => <button>{pn} 图片放大</button> }));
vi.mock("../../../components/parts/CreatePartModal", () => ({ default: ({ pn, onSelected, onClose }: {
  pn: string; onSelected: (part: { part_id: number; pn_std: string; description: string }) => void; onClose: () => void;
}) => <button onClick={() => { onSelected({ part_id: 42, pn_std: pn, description: "新建描述" }); onClose(); }}>确认建档</button> }));
vi.mock("../../../components/audience/AudiencePicker", () => ({
  PUBLIC_AUDIENCE: { mode: "public", user_ids: [], group_ids: [] }, default: () => null,
}));

import QuotePage from "../QuotePage";
import AskPage from "../AskPage";
import OfferNewPage from "../OfferNewPage";
import { answerSentence, lineAnswer, ownedOfferForEdit } from "../answers";

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
  it("真实新草稿优先于当前已发布报价，历史快照不能回退成编辑源", () => {
    const historical = offer({ unit_price: "100", expired: true, latest: { unit_price: "200", revision_no: 2, is_tax_inclusive: true } });
    inquiry.offers = [historical];
    expect(ownedOfferForEdit(inquiry, inquiry.lines![0], 7)).toBeUndefined();
    const draft = offer({ id: 82, status: "draft", unit_price: "300" });
    inquiry.offers.push(draft);
    inquiry.line_answers = [{ line_id: 11, state: "quoted", responded: true, offer: offer({ version: 5, unit_price: "200" }) }];
    expect(ownedOfferForEdit(inquiry, inquiry.lines![0], 7)).toBe(draft);
  });
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
  it("更新后使用服务端可见新价，已回复但无有效价不再推为待报价", () => {
    const line = inquiry.lines![0];
    inquiry.line_responses = [{ line_id: line.id, mode: "reuse", ref_offer_id: 81, created_by: 7, work_item_id: 71, expired: true, offer: offer({ unit_price: "120", expired: true }) }];
    inquiry.line_answers = [{ line_id: line.id, state: "quoted", responded: true, offer: offer({ unit_price: "234.56", version: 2 }) }];
    expect(answerSentence(line, lineAnswer(inquiry, line))).toContain("¥234.56");
    inquiry.line_answers = [{ line_id: line.id, state: "unavailable", responded: true }];
    expect(lineAnswer(inquiry, line)).toEqual({ state: "unavailable", reason: "已回复，当前无有效报价" });
    expect(answerSentence(line, lineAnswer(inquiry, line))).not.toContain("120");
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

  it("读取待办后续页，第 101 条紧急催办询价仍显示在报价行", async () => {
    const tasks = Array.from({ length: 101 }, (_, index) => ({ ...task, id: index + 71, subject_id: index + 1 }));
    mocks.listWorkItems.mockImplementation(async ({ tab, page }: { tab: string; page: number }) => ({
      items: tab === "claimed" ? tasks.slice((page - 1) * 100, page * 100) : [],
      total: tab === "claimed" ? tasks.length : 0, page, page_size: 100, can_supervise: false,
    }));
    mocks.getInquiry.mockImplementation(async (id: number) => ({ ...inquiry, id,
      priority: id === 101 ? "urgent" : "normal", urge_count: id === 101 ? 2 : 0,
      lines: id === 101 ? [{ ...inquiry.lines![0], inquiry_id: id, pn_raw: "PN-101" }] : [],
    }));
    renderQuote();
    const row = (await screen.findByRole("spinbutton", { name: "PN-101 单价" })).closest("tr")!;
    expect(within(row).getByText("紧急")).toBeInTheDocument();
    expect(within(row).getByText("已催 2 次")).toBeInTheDocument();
    expect(mocks.listWorkItems).toHaveBeenCalledWith({ tab: "claimed", subject_type: "presales_inquiry", page: 2, page_size: 100 });
    expect(mocks.getInquiry).toHaveBeenCalledWith(101);
  });

  it("沿用成本价同时带入单价、含税口径和价格性质", async () => {
    mocks.library.mockResolvedValue({ 1: [offer({ id: 99, line_id: 99, price_basis: "cost", is_tax_inclusive: false })] });
    mocks.saveOffer.mockResolvedValue(offer({ status: "draft", price_basis: "cost", is_tax_inclusive: false }));
    renderQuote();
    const input = await screen.findByRole("spinbutton", { name: "PN-1 单价" });
    const row = input.closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: "Expand row" }));
    fireEvent.click(await screen.findByRole("button", { name: "沿用" }));
    expect(Number((input as HTMLInputElement).value)).toBe(120);
    expect(within(row).getByText("成本价")).toBeInTheDocument();
    expect(within(row).getByText("不含税")).toBeInTheDocument();
    fireEvent.click(within(row).getByRole("button", { name: /^保\s?存$/ }));
    await waitFor(() => expect(mocks.saveOffer).toHaveBeenCalledWith(1, 11,
      expect.objectContaining({ unit_price: "120.00", is_tax_inclusive: false, price_basis: "cost" })));
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

  it("紧急与被催办的单置顶，报价可选成本价，坏件不返回直接可见", async () => {
    const other = { ...inquiry, id: 2, inquiry_no: "XJ-2", created_by_name: "李四", priority: "urgent" as const, urge_count: 2,
      lines: [{ ...inquiry.lines![0], id: 21, inquiry_id: 2, pn_raw: "PN-U", return_defective: false }], work_items: [{ ...inquiry.work_items![0], id: 72 }] };
    mocks.getInquiry.mockImplementation(async (id: number) => id === 2 ? other : inquiry);
    mocks.listWorkItems.mockImplementation(async ({ tab }: { tab: string }) => ({ items: tab === "claimed" ? [task, { ...task, id: 72, subject_id: 2 }] : [],
      total: tab === "claimed" ? 2 : 0, page: 1, page_size: 100, can_supervise: false }));
    mocks.saveOffer.mockResolvedValue(offer({ status: "draft" }));
    renderQuote();
    const first = (await screen.findAllByRole("spinbutton", { name: /单价$/ }))[0];
    expect(first).toHaveAccessibleName("PN-U 单价");
    const row = first.closest("tr")!;
    expect(within(row).getByText("紧急")).toBeInTheDocument();
    expect(within(row).getByText("已催 2 次")).toBeInTheDocument();
    expect(within(row).getByText("不返回")).toBeInTheDocument();
    fireEvent.change(first, { target: { value: "50" } });
    fireEvent.mouseDown(within(row).getByRole("combobox", { name: "PN-U 价格性质" }));
    fireEvent.click(await screen.findByTitle("成本价"));
    fireEvent.click(within(row).getByRole("button", { name: /^保\s?存$/ }));
    await waitFor(() => expect(mocks.saveOffer).toHaveBeenCalledWith(2, 21, expect.objectContaining({ unit_price: 50, price_basis: "cost" })));
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
  it("询价B更新同一报价后回到询价A编辑，使用当前版本和字段并保留A的历史快照", async () => {
    const historical = offer({ version: 1, unit_price: "100", expired: true });
    let current = offer({ inquiry_id: 2, line_id: 21, version: 5, unit_price: "200", is_tax_inclusive: null, price_basis: "cost" });
    inquiry = { ...inquiry, lines: [inquiry.lines![0]], offers: [historical],
      line_answers: [{ line_id: 11, state: "quoted", responded: true, offer: current }] };
    mocks.updateOffer.mockImplementation(async (_id, input) => {
      if (input.expected_version !== current.version) throw { response: { status: 409 } };
      current = { ...current, ...input, version: current.version + 1 };
      inquiry = { ...inquiry, line_answers: [{ line_id: 11, state: "quoted", responded: true, offer: current }] };
      return current;
    });
    renderQuote();
    fireEvent.click(await screen.findByRole("button", { name: "已报价 1" }));
    fireEvent.click(await screen.findByRole("button", { name: "更新报价" }));
    const input = screen.getByRole("spinbutton", { name: "PN-1 单价" });
    expect(input).toHaveValue("200");
    expect(screen.getByRole("combobox", { name: "PN-1 价格性质" }).closest(".ant-select")).toHaveTextContent("成本价");
    fireEvent.change(input, { target: { value: "250" } });
    fireEvent.click(within(input.closest("tr")!).getByRole("button", { name: /^保\s?存$/ }));
    await waitFor(() => expect(mocks.updateOffer).toHaveBeenCalledWith(81, expect.objectContaining({ expected_version: 5,
      unit_price: 250, price_basis: "cost", is_tax_inclusive: null })));
    await screen.findByText("¥250");
    expect(historical.version).toBe(1);
    expect(historical.unit_price).toBe("100");
    expect(mocks.saveOffer).not.toHaveBeenCalled();
  });
  it("关单后的本人报价仍可更新，存量未声明税口径不被改为含税", async () => {
    inquiry.status = "closed";
    inquiry.work_items = [{ ...inquiry.work_items![0], status: "done" }];
    inquiry.offers = [offer({ is_tax_inclusive: null })];
    mocks.updateOffer.mockResolvedValue(offer({ unit_price: "110", is_tax_inclusive: null }));
    renderQuote();
    fireEvent.click(await screen.findByRole("button", { name: "已报价 1" }));
    fireEvent.click(await screen.findByRole("button", { name: "更新报价" }));
    expect(screen.getByText("未声明")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN-1 单价" }), { target: { value: "110" } });
    fireEvent.click(within(screen.getByRole("spinbutton", { name: "PN-1 单价" }).closest("tr")!).getByRole("button", { name: /^保\s?存$/ }));
    await waitFor(() => expect(mocks.updateOffer).toHaveBeenCalledWith(81, expect.objectContaining({ unit_price: 110, is_tax_inclusive: null })));
  });
  it("保存已成功但刷新失败后禁止重复写入，保留刷新入口", async () => {
    inquiry.offers = [offer()];
    mocks.updateOffer.mockImplementation(async () => { mocks.getInquiry.mockRejectedValue(new Error("刷新失败")); return offer(); });
    renderQuote();
    fireEvent.click(await screen.findByRole("button", { name: "已报价 1" }));
    fireEvent.click(await screen.findByRole("button", { name: "更新报价" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN-1 单价" }), { target: { value: "110" } });
    fireEvent.click(within(screen.getByRole("spinbutton", { name: "PN-1 单价" }).closest("tr")!).getByRole("button", { name: /^保\s?存$/ }));
    expect(await screen.findByRole("button", { name: "刷新最新状态" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "更新报价" })).toBeDisabled();
    expect(mocks.updateOffer).toHaveBeenCalledOnce();
  });
  it("无货报价重新填价时明确恢复有货，不沿用旧无货标记", async () => {
    inquiry.offers = [offer({ no_stock: true, unit_price: null })];
    mocks.updateOffer.mockResolvedValue(offer({ unit_price: "110" }));
    renderQuote();
    fireEvent.click(await screen.findByRole("button", { name: "不报 1" }));
    fireEvent.click(await screen.findByRole("button", { name: "更新报价" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN-1 单价" }), { target: { value: "110" } });
    fireEvent.click(within(screen.getByRole("spinbutton", { name: "PN-1 单价" }).closest("tr")!).getByRole("button", { name: /^保\s?存$/ }));
    await waitFor(() => expect(mocks.updateOffer).toHaveBeenCalledWith(81, expect.objectContaining({ unit_price: 110, no_stock: false })));
  });
  it("最后一行不报已回复但刷新失败时也阻止重复提交", async () => {
    inquiry.offers = [offer({ status: "draft", valid_from: null })];
    mocks.replyInquiry.mockImplementation(async () => { mocks.getInquiry.mockRejectedValue(new Error("刷新失败")); return []; });
    renderQuote();
    const row = (await screen.findByRole("spinbutton", { name: "PN-2 单价" })).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /^不\s?报$/ }));
    fireEvent.click(await screen.findByRole("button", { name: "确定不报" }));
    expect(await screen.findByRole("button", { name: "刷新最新状态" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^保\s?存$/ })).not.toBeInTheDocument();
    expect(mocks.replyInquiry).toHaveBeenCalledOnce();
  });
});

describe("无单据主动报价", () => {
  it("旧新建深链也只发布报价，既有报价按读到的版本更新，留在当前页", async () => {
    mocks.suggestParts.mockResolvedValue([{ part_id: 1, pn_std: "PN-1", description: "型号", match_type: "exact_pn", match_reason: "", matched_terms: [] }]);
    mocks.directOffer.mockRejectedValueOnce({ response: { status: 409, data: { detail: { code: "existing_offer", existing_offers: [offer({ version: 3, is_tax_inclusive: null, price_basis: "cost" })] } } } })
      .mockResolvedValueOnce(offer({ inquiry_id: null, line_id: null, unit_price: "50", version: 4, is_tax_inclusive: null, price_basis: "cost" }));
    render(<MemoryRouter><OfferNewPage /></MemoryRouter>);
    fireEvent.change(screen.getByRole("combobox", { name: "主动报价 PN" }), { target: { value: "PN" } });
    fireEvent.click(await screen.findByText(/^PN-1/, { selector: ".ant-select-item-option-content > span" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "主动报价单价" }), { target: { value: "50" } });
    fireEvent.click(screen.getByRole("button", { name: /^保\s?存$/ }));
    expect(await screen.findByText(/PN-1 已更新为 ¥50/)).toBeInTheDocument();
    expect(mocks.directOffer).toHaveBeenLastCalledWith(expect.objectContaining({ part_id: 1, existing_offer_id: 81, expected_version: 3, unit_price: 50, is_tax_inclusive: null, price_basis: "cost" }));
    expect(mocks.createInquiry).not.toHaveBeenCalled();
    expect(mocks.saveOffer).not.toHaveBeenCalled();
    expect(screen.getByRole("spinbutton", { name: "主动报价单价" })).toHaveValue("50.00");
  });
});

describe("询价页", () => {
  it("未建档 PN 可显式建档后回填稳定身份，并展示图片入口", async () => {
    localStorage.setItem("role", "admin");
    mocks.listInquiries.mockResolvedValue({ items: [inquiry], total: 1, page: 1, page_size: 10 });
    mocks.createInquiry.mockResolvedValue({ ...inquiry, id: 5, status: "draft" });
    render(<MemoryRouter><AskPage /></MemoryRouter>);
    fireEvent.change(await screen.findByRole("combobox", { name: "第 1 行 PN" }), { target: { value: "NEW-PN" } });
    fireEvent.click(await screen.findByRole("button", { name: "新建 PN 档案" }));
    fireEvent.click(screen.getByRole("button", { name: "确认建档" }));
    expect(screen.getByText("新建描述")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
    await waitFor(() => expect(mocks.createInquiry).toHaveBeenCalledWith(expect.objectContaining({ lines: [expect.objectContaining({ part_id: 42, pn_raw: "NEW-PN" })] })));
    expect(await screen.findByRole("button", { name: "PN-1 图片放大" })).toBeInTheDocument();
  });
  it("已回复旧价失效后没有催办按钮，保留准确处理状态", async () => {
    inquiry.lines = [inquiry.lines![0]];
    inquiry.work_items = [{ ...inquiry.work_items![0], status: "done" }];
    inquiry.can_urge = false;
    inquiry.line_answers = [{ line_id: 11, state: "unavailable", responded: true }];
    mocks.listInquiries.mockResolvedValue({ items: [inquiry], total: 1, page: 1, page_size: 10 });
    render(<MemoryRouter><AskPage /></MemoryRouter>);
    expect(await screen.findByText("已回复，当前无有效报价")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /催\s?办/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /复制.*报价给客户/ })).not.toBeInTheDocument();
  });
  it("行内填写 PN 与数量后直接提交，PN 一致记住上次选择", async () => {
    mocks.listInquiries.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 });
    mocks.createInquiry.mockResolvedValue({ ...inquiry, id: 5, inquiry_no: "XJ-5", status: "draft" });
    mocks.submitInquiry.mockResolvedValue({ ...inquiry, id: 5 });
    render(<MemoryRouter><AskPage /></MemoryRouter>);
    fireEvent.change(await screen.findByRole("combobox", { name: "第 1 行 PN" }), { target: { value: "ABC-1" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "第 1 行要求 PN 一致" }));
    expect(screen.getByRole("checkbox", { name: "第 1 行坏件返回" })).toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: "第 1 行坏件返回" }));
    fireEvent.click(screen.getByRole("radio", { name: "紧急" }));
    fireEvent.click(screen.getByRole("button", { name: "提交询价" }));
    await waitFor(() => expect(mocks.createInquiry).toHaveBeenCalledWith(expect.objectContaining({ kind: "sales_inquiry", priority: "urgent",
      lines: [expect.objectContaining({ pn_raw: "ABC-1", part_id: null, qty: 1, require_exact_pn: false, return_defective: false })] })));
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
    inquiry.lines = inquiry.lines!.map((line, index) => ({ ...line, return_defective: index === 0 }));
    inquiry.line_responses = [{ line_id: 12, mode: "skip", reason: "停产", created_by: 7, work_item_id: 71, expired: false }];
    mocks.listInquiries.mockResolvedValue({ items: [inquiry], total: 1, page: 1, page_size: 10 });
    render(<MemoryRouter><AskPage /></MemoryRouter>);
    expect(await screen.findByText("¥120 含税")).toBeInTheDocument();
    expect(screen.getByText("不报：停产")).toBeInTheDocument();
    expect(screen.getByText("全部已答复")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制 PN-1 报价给客户" })).toBeInTheDocument();
    expect(screen.getByText("售价")).toBeInTheDocument();
    expect(screen.getAllByText("坏件返回").length).toBeGreaterThan(0);
    expect(screen.getByText("坏件不返回")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /催\s?办/ })).not.toBeInTheDocument();
  });

  it("未回复完的询价可催办，显示紧急与已催次数", async () => {
    inquiry.priority = "urgent";
    inquiry.offers = [offer({ price_basis: "cost" })];
    mocks.listInquiries.mockResolvedValue({ items: [inquiry], total: 1, page: 1, page_size: 10 });
    mocks.urgeInquiry.mockResolvedValue({ ...inquiry, urge_count: 1, last_urged_at: "2026-10-29T02:00:00Z" });
    render(<MemoryRouter><AskPage /></MemoryRouter>);
    expect(await screen.findByText("紧急")).toBeInTheDocument();
    expect(screen.getByText("成本价")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /催\s?办/ }));
    await waitFor(() => expect(mocks.urgeInquiry).toHaveBeenCalledWith(1));
    expect(await screen.findByText(/已催 1 次/)).toBeInTheDocument();
  });
});
