import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import type { Inquiry, Offer } from "../../../api/presales";
import type { WorkItem } from "../../../api/workItems";

const mocks = vi.hoisted(() => ({ getInquiry: vi.fn(), library: vi.fn(), reference: vi.fn(), listWorkItems: vi.fn(),
  saveOffer: vi.fn(), replyInquiry: vi.fn(), publishInquiry: vi.fn(), suppliers: vi.fn(),
  listWorkCandidates: vi.fn(), getFieldTemplate: vi.fn(), refreshWorkBadges: vi.fn() }));
vi.mock("../../../api/presales", () => ({ presales: mocks }));
vi.mock("../../../api/workItems", () => ({ listWorkItems: mocks.listWorkItems, listWorkCandidates: mocks.listWorkCandidates }));
vi.mock("../../../api/fields", () => ({ getFieldTemplate: mocks.getFieldTemplate }));
vi.mock("../../../components/workflow/useWorkBadge", () => ({ refreshWorkBadges: mocks.refreshWorkBadges }));
vi.mock("../../../components/workflow/Timeline", () => ({ default: () => null }));
vi.mock("../../../components/gallery/PartThumb", () => ({ default: () => null }));
vi.mock("../../../components/pricing/PriceRefBadge", () => ({ default: () => null }));
vi.mock("../../../components/fields/CustomFieldsEditor", () => ({ default: () => null }));
vi.mock("../../../components/audience/AudiencePicker", () => ({
  PUBLIC_AUDIENCE: { mode: "public", user_ids: [], group_ids: [] }, default: () => null,
}));

import ProcessPage from "../ProcessPage";
import OfferNewPage from "../OfferNewPage";
import InquiryDetailPage from "../InquiryDetailPage";

const task: WorkItem = { id: 71, subject_type: "presales_inquiry", subject_id: 1, pool_key: "presales_inquiry",
  assignee_id: 7, claimed_by: null, status: "in_progress", version: 3, paused: false,
  summary: {}, created_at: "2026-10-25T00:00:00Z", updated_at: "2026-10-25T00:00:00Z", allowed_actions: [] };
const quote = (patch: Partial<Offer> = {}): Offer => ({ id: 81, inquiry_id: 1, line_id: 11, part_id: 1,
  unit_price: "120.00", no_stock: false, validity_days: 3, notes: null, reporter: { id: 7, name: "采购甲" },
  version: 1, revision_no: 0, status: "draft", valid_from: null, valid_until: null, ...patch });
const activeQuote = () => quote({ id: 99, inquiry_id: 9, line_id: 90, status: "published", revision_no: 1,
  valid_from: "2026-10-25T00:00:00Z", valid_until: "2026-10-27T15:59:59.999999Z" });
let inquiry: Inquiry;

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  localStorage.setItem("role", "purchaser");
  localStorage.setItem("permissions", JSON.stringify({ action_presales_quote: true, action_presales_offer: true, data_supplier: true }));
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-10-25T04:00:00Z"));
  inquiry = { id: 1, kind: "sales_inquiry", inquiry_no: "XJ-1", status: "submitted", version: 4, revision: 1,
    created_by: 3, created_at: "2026-10-25T00:00:00Z", updated_at: "2026-10-25T00:00:00Z",
    line_count: 2, progress: { quoted_lines: 0, line_count: 2 },
    lines: [1, 2].map((id) => ({ id: id + 10, inquiry_id: 1, line_key: `line-${id}`, seq: id, part_id: id,
      pn_raw: `PN-${id}`, qty: "2", status: "active", version: 1 })), offers: [],
    work_items: [{ id: 71, assignee_id: 7, claimed_by: null, status: "in_progress", version: 3, paused: false }] };
  mocks.getInquiry.mockImplementation(async () => inquiry);
  mocks.reference.mockImplementation(async () => ({ offers: await mocks.library() }));
  mocks.library.mockResolvedValue({ 1: [activeQuote()] });
  mocks.suppliers.mockResolvedValue([]);
  mocks.getFieldTemplate.mockResolvedValue([]);
  mocks.listWorkCandidates.mockResolvedValue({ items: [] });
  mocks.replyInquiry.mockResolvedValue([]);
  mocks.publishInquiry.mockResolvedValue([]);
  mocks.saveOffer.mockImplementation(async () => inquiry.offers?.[0]);
});
afterEach(async () => { await cleanupAntd(); vi.restoreAllMocks(); });

function process() {
  return render(<MemoryRouter initialEntries={[{ pathname: "/presales/tasks/1", state: { workItem: task } }]}>
    <Routes><Route path="/presales/tasks/:id" element={<ProcessPage />} /></Routes>
  </MemoryRouter>);
}

function proactive() {
  inquiry = { ...inquiry, kind: "proactive_offer", status: "draft", created_by: 7, lines: [inquiry.lines![0]], offers: [quote()] };
  return render(<MemoryRouter initialEntries={["/presales/offers/new?draft=1"]}><OfferNewPage /></MemoryRouter>);
}

const conflict = () => ({ response: { status: 409, data: { detail: { code: "existing_offer", draft_offer_id: 81,
  line_id: 11, existing_offers: [activeQuote()] } } } });

describe("r6 采购逐行三选一", () => {
  it("显示可见有效报价，沿用不复制价格，不报原因填写完成后一次回复", async () => {
    process();
    fireEvent.click(await screen.findByRole("button", { name: "展开 PN-1" }));
    await screen.findByText(/有效报价 ¥120/);
    expect(screen.getByRole("columnheader", { name: /已有有效报价/ })).toBeInTheDocument();
    const mode = screen.getByRole("radiogroup", { name: "PN-1处理方式" });
    fireEvent.click(within(mode).getByRole("radio", { name: "沿用有效报价" }));
    expect(screen.queryByLabelText("PN-1单价")).not.toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "PN-1沿用报价" }));
    fireEvent.click(await screen.findByText(/报价 #99/, { selector: ".ant-select-item-option-content span" }));
    expect(screen.getByText("所沿用报价：")).toHaveTextContent("剩 3 天");
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "PN-2处理方式" })).getByRole("radio", { name: "无货 / 不报" }));
    expect(screen.getByRole("button", { name: "回复给销售" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("PN-2不报原因"), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "回复给销售" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("PN-2不报原因"), { target: { value: "  供应商确认无货  " } });
    fireEvent.click(screen.getByRole("button", { name: "回复给销售" }));
    await waitFor(() => expect(mocks.replyInquiry).toHaveBeenCalledWith(1, { expected_version: 4,
      work_item_id: 71, expected_work_item_version: 3, line_responses: [
        { line_id: 11, mode: "reuse", ref_offer_id: 99 }, { line_id: 12, mode: "skip", reason: "供应商确认无货" },
      ] }));
    expect(mocks.saveOffer).not.toHaveBeenCalled();
  });

  it("有有效报价也须明确处理，未保存的新报价不会算已回复", async () => {
    process();
    fireEvent.click(await screen.findByRole("button", { name: "展开 PN-1" }));
    await screen.findByText(/有效报价 ¥120/);
    expect(screen.getByRole("button", { name: "回复给销售" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("PN-1单价"), { target: { value: "160" } });
    expect(screen.getByRole("button", { name: "回复给销售" })).toBeDisabled();
    expect(mocks.replyInquiry).not.toHaveBeenCalled();
  });

  it("沿用报价在原有效期结束后显示已失效", async () => {
    inquiry.lines = [inquiry.lines![0]];
    inquiry.work_items![0].status = "done";
    inquiry.line_responses = [{ line_id: 11, mode: "reuse", ref_offer_id: 99, created_by: 7, work_item_id: 71,
      offer: activeQuote(), expired: false }];
    process();
    fireEvent.click(await screen.findByRole("button", { name: "展开 PN-1" }));
    const reused = await screen.findByText("所沿用报价：");
    expect(reused).toHaveTextContent("剩 3 天");
    vi.mocked(Date.now).mockReturnValue(Date.parse("2026-10-27T16:00:00Z"));
    await waitFor(() => expect(reused).toHaveTextContent("已失效"), { timeout: 2500 });
  });

  it("改选沿用报价更新原报价人，切换不报后不再显示沿用信息", async () => {
    inquiry.lines = [inquiry.lines![0]];
    inquiry.line_responses = [{ line_id: 11, mode: "reuse", ref_offer_id: 99, created_by: 7,
      work_item_id: 71, offer: activeQuote(), expired: false, reporter_label: "采购甲" }];
    mocks.library.mockResolvedValue({ 1: [activeQuote(), { ...activeQuote(), id: 10,
      reporter: { id: 8, name: "采购乙" } }] });
    process();
    fireEvent.click(await screen.findByRole("button", { name: "展开 PN-1" }));
    expect(await screen.findByText("原报价人：采购甲")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "PN-1沿用报价" }));
    fireEvent.click(await screen.findByText(/报价 #10/, { selector: ".ant-select-item-option-content span" }));
    expect(screen.getByText("原报价人：采购乙")).toBeInTheDocument();
    expect(screen.queryByText("原报价人：采购甲")).not.toBeInTheDocument();
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "PN-1处理方式" })).getByRole("radio", { name: "无货 / 不报" }));
    expect(screen.queryByText("所沿用报价：")).not.toBeInTheDocument();
    expect(screen.queryByText("沿用报价当前不可见或已失效")).not.toBeInTheDocument();
  });

  it("销售详情回读沿用的当前报价，过期保留失效提示及不报原因", async () => {
    vi.mocked(Date.now).mockReturnValue(Date.parse("2026-10-28T00:00:00Z"));
    inquiry.line_responses = [{ line_id: 11, mode: "reuse", ref_offer_id: 99, created_by: 7, work_item_id: 71,
      offer: activeQuote(), expired: true }, { line_id: 12, mode: "skip", reason: "暂不报价", created_by: 7, work_item_id: 71, expired: false }];
    render(<MemoryRouter initialEntries={["/presales/inquiries/1"]}><Routes>
      <Route path="/presales/inquiries/:id" element={<InquiryDetailPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("沿用报价 #99")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "询价比价矩阵" })).toHaveTextContent("¥120.00");
    expect(screen.getByRole("table", { name: "询价比价矩阵" })).toHaveTextContent("已失效");
    fireEvent.click(screen.getByRole("button", { name: "展开 PN-2" }));
    expect(screen.getByText("无货 / 不报：暂不报价")).toBeInTheDocument();
  });

  it("销售详情只显示服务端允许的沿用报价，M8 隐藏时不补出价格", async () => {
    inquiry.line_responses = [{ line_id: 11, mode: "reuse", ref_offer_id: 99, created_by: 7, work_item_id: 71, expired: false }];
    render(<MemoryRouter initialEntries={["/presales/inquiries/1"]}><Routes>
      <Route path="/presales/inquiries/:id" element={<InquiryDetailPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("沿用报价 #99")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开 PN-1" }));
    expect(screen.getByText("当前不可见")).toBeInTheDocument();
    expect(screen.queryByText(/¥120/)).not.toBeInTheDocument();
  });

  it("更新迁移归属后，旧主动单通过实时关联仍可定位当前报价", async () => {
    const current = activeQuote();
    inquiry = { ...inquiry, kind: "proactive_offer", created_by: 7, lines: [inquiry.lines![0]], offers: [current],
      work_items: [], line_responses: [{ line_id: 11, mode: "reuse", ref_offer_id: current.id,
        created_by: 7, work_item_id: null, offer: current, expired: false }] };
    process();
    expect(await screen.findByLabelText("PN-1单价")).toHaveValue("120.00");
    expect(screen.getByRole("button", { name: "更新报价" })).toBeEnabled();
  });

  it("同一旧行的多条报价迁移归属后，按服务端可见关联继续逐行比价", async () => {
    inquiry.lines = [{ ...inquiry.lines![0], linked_offer_ids: [99, 100] }];
    inquiry.offers = [activeQuote(), { ...activeQuote(), id: 100, line_id: 91, inquiry_id: 10, unit_price: "155.00" }];
    render(<MemoryRouter initialEntries={["/presales/inquiries/1"]}><Routes>
      <Route path="/presales/inquiries/:id" element={<InquiryDetailPage />} /></Routes></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "展开 PN-1" }));
    const comparison = await screen.findByRole("table", { name: "PN-1 报价详情" });
    expect(within(comparison).getByText(/¥120/)).toBeInTheDocument();
    expect(within(comparison).getByText(/¥155/)).toBeInTheDocument();
    expect(within(comparison).getAllByRole("button", { name: /查看报价 .* 历史/ })).toHaveLength(2);
  });
});

describe("r6 重复报价必须明确选择", () => {
  it.each(["update", "new"] as const)("询价回复 409 选择 %s 后带上逐报价决定重试", async (decision) => {
    inquiry.lines = [inquiry.lines![0]];
    inquiry.offers = [quote()];
    mocks.replyInquiry.mockRejectedValueOnce(conflict());
    process();
    fireEvent.click(await screen.findByRole("button", { name: "回复给销售" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("你已有该 PN 的有效报价")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: decision === "update" ? "更新那条" : "另报一条" }));
    await waitFor(() => expect(mocks.replyInquiry).toHaveBeenCalledTimes(2));
    expect(mocks.replyInquiry).toHaveBeenLastCalledWith(1, expect.objectContaining({
      line_responses: [{ line_id: 11, mode: "quote" }], existing_choices: { 81: decision === "update"
        ? { on_existing: "update", existing_offer_id: 99 } : { on_existing: "new" } },
    }));
  });

  it("询价回复取消选择后保留表单且不重试", async () => {
    inquiry.lines = [inquiry.lines![0]]; inquiry.offers = [quote()];
    mocks.replyInquiry.mockRejectedValueOnce(conflict());
    process();
    fireEvent.click(await screen.findByRole("button", { name: "回复给销售" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mocks.replyInquiry).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("PN-1单价")).toHaveValue("120.00");
  });

  it.each(["update", "new"] as const)("主动报价 409 选择 %s 后只重试发布，不重复保存草稿", async (decision) => {
    mocks.publishInquiry.mockRejectedValueOnce(conflict());
    proactive();
    fireEvent.click(await screen.findByRole("button", { name: "发布主动报价" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: decision === "update" ? "更新那条" : "另报一条" }));
    await screen.findByText("主动报价已发布");
    expect(mocks.saveOffer).toHaveBeenCalledTimes(1);
    expect(mocks.publishInquiry).toHaveBeenCalledTimes(2);
    expect(mocks.publishInquiry).toHaveBeenLastCalledWith(1, { expected_version: 4, notice_user_ids: [],
      existing_choices: { 81: decision === "update" ? { on_existing: "update", existing_offer_id: 99 } : { on_existing: "new" } } });
  });

  it("主动报价取消选择后保留草稿，不宣告发布成功", async () => {
    mocks.publishInquiry.mockRejectedValueOnce(conflict());
    proactive();
    fireEvent.click(await screen.findByRole("button", { name: "发布主动报价" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "发布主动报价" })).toBeEnabled());
    expect(mocks.publishInquiry).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("主动报价已发布")).not.toBeInTheDocument();
  });

  it("主动报价无货也必须填写原因后才能发布", async () => {
    proactive();
    fireEvent.click(await screen.findByRole("button", { name: "展开 PN-1" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "无货" }));
    expect(screen.getByRole("button", { name: "发布主动报价" })).toBeDisabled();
    expect(screen.getByText("无货原因（必填）")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("PN-1备注"), { target: { value: "  " } });
    expect(screen.getByRole("button", { name: "发布主动报价" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("PN-1备注"), { target: { value: "供应商售罄" } });
    expect(screen.getByRole("button", { name: "发布主动报价" })).toBeEnabled();
  });
});

it("处理页通过询价参考接口读取所有人可见候选，同一报价人的其他价可展开", async () => {
  inquiry.lines = [inquiry.lines![0]];
  mocks.library.mockResolvedValue({ 1: [{ ...activeQuote(), more: [{ ...activeQuote(), id: 100, unit_price: "140.00" }] }] });
  process();
  fireEvent.click(await screen.findByRole("button", { name: "展开 PN-1" }));
  await screen.findByText(/有效报价 ¥120/);
  expect(mocks.reference).toHaveBeenCalledWith({ inquiry_id: 1 });
  expect(screen.queryByText(/有效报价 ¥140/)).not.toBeInTheDocument();
  fireEvent.click(within(screen.getByRole("table", { name: "PN-1有效报价" })).getByRole("button", { name: "展开 采购甲" }));
  expect(screen.getByText(/有效报价 ¥140/)).toBeInTheDocument();
});

it("沿用行保留钉住的价格与原报价时间，隐藏报价人显示其他采购", async () => {
  const pinned = activeQuote();
  delete pinned.reporter;
  inquiry.line_responses = [{ line_id: 11, mode: "reuse", ref_offer_id: 99, ref_offer_revision_no: 1,
    created_by: 7, work_item_id: 71, offer: pinned, expired: false, reporter_label: "其他采购" }];
  render(<MemoryRouter initialEntries={["/presales/inquiries/1"]}><Routes>
    <Route path="/presales/inquiries/:id" element={<InquiryDetailPage />} /></Routes></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: "展开 PN-1" }));
  expect(await screen.findByText("原报价人：其他采购")).toBeInTheDocument();
  expect(screen.getByText(/原报价时间：2026-10-25/)).toBeInTheDocument();
  expect(screen.getByRole("table", { name: "询价比价矩阵" })).toHaveTextContent("¥120.00");
  expect(screen.getByRole("table", { name: "询价比价矩阵" })).toHaveTextContent("有效");
  expect(screen.queryByText("采购甲")).not.toBeInTheDocument();
});

it("主动报价编辑跨单旧行关联时先读当前报价，不以钉快照的旧价格版本编辑", async () => {
  const pinned = { ...activeQuote(), unit_price: "120.00", version: 2,
    latest: { unit_price: "180.00", is_tax_inclusive: true, revision_no: 3 } };
  inquiry = { ...inquiry, kind: "proactive_offer", created_by: 7, lines: [{ ...inquiry.lines![0], linked_offer_ids: [99] }],
    offers: [pinned], work_items: [] };
  mocks.library.mockResolvedValue({ 1: [{ ...activeQuote(), unit_price: "180.00", version: 8, revision_no: 3 }] });
  process();
  expect(await screen.findByLabelText("PN-1单价")).toHaveValue("180.00");
  expect(screen.getByRole("button", { name: "更新报价" })).toBeEnabled();
  expect(screen.queryByText(/该报价已更新为/)).not.toBeInTheDocument();
});

it("有效报价清单的更多报价也在原有效期到点后移除", async () => {
  inquiry.lines = [inquiry.lines![0]];
  mocks.library.mockResolvedValue({ 1: [{ ...activeQuote(), more: [{ ...activeQuote(), id: 100, unit_price: "140.00", valid_until: "2026-10-25T04:00:01Z" }] }] });
  process();
  fireEvent.click(await screen.findByRole("button", { name: "展开 PN-1" }));
  const offers = await screen.findByRole("table", { name: "PN-1有效报价" });
  fireEvent.click(within(offers).getByRole("button", { name: "展开 采购甲" }));
  expect(screen.getByText(/有效报价 ¥140/)).toBeInTheDocument();
  vi.mocked(Date.now).mockReturnValue(Date.parse("2026-10-25T04:00:02Z"));
  await waitFor(() => expect(screen.queryByText(/报价 ¥140/)).not.toBeInTheDocument(), { timeout: 2500 });
});

it("最新报价先到期时保留同组仍有效的旧报价供沿用", async () => {
  inquiry.lines = [inquiry.lines![0]];
  mocks.library.mockResolvedValue({ 1: [{ ...activeQuote(), valid_until: "2026-10-25T04:00:01Z",
    more: [{ ...activeQuote(), id: 10, unit_price: "140.00", valid_from: "2026-10-24T00:00:00Z" }] }] });
  process();
  fireEvent.click(await screen.findByRole("button", { name: "展开 PN-1" }));
  expect(await screen.findByText(/有效报价 ¥120/)).toBeInTheDocument();
  vi.mocked(Date.now).mockReturnValue(Date.parse("2026-10-25T04:00:02Z"));
  await waitFor(() => {
    expect(screen.queryByText(/报价 ¥120/)).not.toBeInTheDocument();
    expect(screen.getByText(/有效报价 ¥140/)).toBeInTheDocument();
  }, { timeout: 2500 });
  fireEvent.click(within(screen.getByRole("radiogroup", { name: "PN-1处理方式" })).getByRole("radio", { name: "沿用有效报价" }));
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "PN-1沿用报价" }));
  fireEvent.click(await screen.findByText(/报价 #10/, { selector: ".ant-select-item-option-content span" }));
  expect(screen.getByRole("button", { name: "回复给销售" })).toBeEnabled();
});
