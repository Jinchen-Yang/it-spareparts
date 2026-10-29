import { openLine, closeLine } from "./nativeUi";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import type { Inquiry, Offer } from "../../../api/presales";
import type { PartSuggestion } from "../../../api/search";
import type { WorkItem } from "../../../api/workItems";

const mocks = vi.hoisted(() => ({
  getInquiry: vi.fn(), createInquiry: vi.fn(), saveOffer: vi.fn(), updateOffer: vi.fn(),
  withdrawOffer: vi.fn(), replyInquiry: vi.fn(), publishInquiry: vi.fn(), partCheck: vi.fn(),
  bindPart: vi.fn(), createPart: vi.fn(), library: vi.fn(), reference: vi.fn(), revisions: vi.fn(), listWorkItems: vi.fn(),
  listWorkCandidates: vi.fn(), getFieldTemplate: vi.fn(), refreshWorkBadges: vi.fn(), inbox: vi.fn(), suppliers: vi.fn(),
  startFailed: false,
}));
vi.mock("../../../api/presales", () => ({ presales: mocks }));
vi.mock("../../../api", async (importOriginal) => ({ ...await importOriginal<typeof import("../../../api")>(),
  masterCategories: vi.fn(async () => ({ data: { categories: [], spec_fields: [] } })),
}));
vi.mock("../../../api/workItems", () => ({ listWorkItems: mocks.listWorkItems, listWorkCandidates: mocks.listWorkCandidates }));
vi.mock("../../../api/fields", () => ({ getFieldTemplate: mocks.getFieldTemplate }));
vi.mock("../../../components/workflow/useWorkBadge", () => ({ refreshWorkBadges: mocks.refreshWorkBadges }));
vi.mock("../../../components/workflow/Timeline", () => ({ default: () => <div>时间线</div> }));
vi.mock("../../../components/gallery/PartThumb", () => ({ default: ({ pn }: { pn: string }) => <span>图片 {pn}</span> }));
vi.mock("../../../components/gallery/GalleryPicker", () => ({ default: () => <div>多选图片下载</div> }));
vi.mock("../../../components/pricing/PriceRefBadge", () => ({ default: () => <span>近 30 天采购均价</span> }));
vi.mock("../../../components/fields/CustomFieldsEditor", () => ({ default: () => <span>字段池</span> }));
vi.mock("../../../components/audience/AudiencePicker", () => ({
  PUBLIC_AUDIENCE: { mode: "public", user_ids: [], group_ids: [] }, default: () => <span>受众选择器</span>,
}));
vi.mock("../../../components/workflow/WorkInbox", () => ({ default: (props: {
  subjectType: string; renderRow?: (item: WorkItem) => import("react").ReactNode; onOpen: (item: WorkItem) => void; beforeOpen?: (item: WorkItem) => boolean | Promise<boolean>;
}) => { mocks.inbox(props); return <>{[1, 2].map((subjectId) => <button key={subjectId} onClick={async () => {
  const item = { ...task, subject_id: subjectId, subject_type: props.subjectType };
  if ((!props.beforeOpen || await props.beforeOpen(item)) && !mocks.startFailed) props.onOpen(item);
}}>{subjectId === 1 ? "打开采购待办" : "打开另一张采购待办"}</button>)}</>; } }));
vi.mock("../../../components/parts/LivePartSearch", () => ({ default: ({ mode, value = [], onChange }: {
  mode?: string; value?: PartSuggestion[]; onChange: (items: PartSuggestion[]) => void;
}) => <div>{mode === "multiple" ? [1, 2].map((id) => <button key={id} onClick={() => onChange([
  ...value.filter((part) => part.part_id !== id), part(id),
])}>加入 PN-{id}</button>) : <button onClick={() => onChange([part(42)])}>选定已有 PN</button>}</div> }));

import TasksPage from "../TasksPage";
import ProcessPage from "../ProcessPage";
import OfferNewPage from "../OfferNewPage";
import InquiryDetailPage from "../InquiryDetailPage";

const task: WorkItem = { id: 71, subject_type: "presales_inquiry", subject_id: 1, pool_key: "presales_inquiry",
  assignee_id: 7, claimed_by: null, status: "in_progress", version: 3, paused: false,
  summary: {}, created_at: "2026-10-25T00:00:00Z", updated_at: "2026-10-25T00:00:00Z", allowed_actions: [] };
const part = (id: number): PartSuggestion => ({ part_id: id, pn_std: `PN-${id}`, description: `型号 ${id}`,
  match_type: "exact_pn", match_reason: "PN 精确", matched_terms: [] });
const quote = (patch: Partial<Offer> = {}): Offer => ({ id: 81, inquiry_id: 1, line_id: 11, part_id: 1,
  unit_price: "120.00", is_tax_inclusive: true, no_stock: false, qty_available: "3", stock_location: "上海",
  lead_time: "现货", supplier_id: null, source_key: "", notes: null,
  reporter: { id: 7, name: "采购甲" }, version: 1, revision_no: 0, status: "draft", valid_from: null, ...patch });
const baseInquiry = (): Inquiry => ({ id: 1, kind: "sales_inquiry", inquiry_no: "XJ-1", status: "submitted",
  version: 4, revision: 1, created_by: 3, created_at: "2026-10-25T00:00:00Z", updated_at: "2026-10-25T00:00:00Z",
  line_count: 2, progress: { quoted_lines: 0, line_count: 2 }, notes: "客户要求",
  lines: [1, 2].map((id) => ({ id: id + 10, inquiry_id: 1, line_key: `line-${id}`, seq: id, part_id: id,
    pn_raw: `PN-${id}`, description: `型号 ${id}`, qty: "2", status: "active", version: 1 })),
  offers: [], work_items: [{ id: 71, assignee_id: 7, claimed_by: null, status: "in_progress", version: 3, paused: false }] });
let currentInquiry: Inquiry;

beforeEach(() => {
  vi.clearAllMocks();
  mocks.startFailed = false;
  localStorage.clear();
  localStorage.setItem("role", "purchaser");
  localStorage.setItem("permissions", JSON.stringify({ action_presales_quote: true, action_presales_offer: true, data_supplier: true }));
  mocks.suppliers.mockResolvedValue([]);
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-10-25T04:00:00Z"));
  currentInquiry = baseInquiry();
  mocks.getInquiry.mockImplementation(async () => currentInquiry);
  mocks.listWorkItems.mockResolvedValue({ items: [task], total: 1, page: 1, page_size: 100, can_supervise: false });
  mocks.listWorkCandidates.mockResolvedValue({ items: [{ id: 3, display_name: "销售甲" }] });
  mocks.getFieldTemplate.mockResolvedValue([]);
  mocks.reference.mockImplementation(async () => ({ offers: await mocks.library() }));
  mocks.library.mockResolvedValue({});
  mocks.revisions.mockResolvedValue([]);
  mocks.replyInquiry.mockImplementation(async () => {
    currentInquiry = { ...currentInquiry, offers: currentInquiry.offers?.map((offer) => ({ ...offer, status: "published" as const,
      version: offer.version + 1, valid_from: "2026-10-25T04:00:00Z" })),
      work_items: [{ ...currentInquiry.work_items![0], status: "done", version: 4 }] };
    return currentInquiry.offers;
  });
  mocks.saveOffer.mockImplementation(async (_inquiryId: number, lineId: number, body: Record<string, unknown>) => {
    const rules = Object.fromEntries(["supplier_audience", "reporter_audience", "notes_audience"].flatMap((field) =>
      body[field] ? [[field, { mode: (body[field] as { mode: string }).mode, users: [], groups: [] }]] : []));
    const offer = quote({ ...body, id: lineId + 70, line_id: lineId, part_id: lineId - 10,
      ...rules, version: Number(body.offer_id ? body.expected_version : 0) + 1 });
    currentInquiry = { ...currentInquiry, offers: [...(currentInquiry.offers ?? []).filter((item) => item.line_id !== lineId), offer] };
    return offer;
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function processPage(withNavigationTask = true) {
  return render(<MemoryRouter initialEntries={[{ pathname: "/presales/tasks/1", state: withNavigationTask ? { workItem: task } : undefined }]}>
    <Routes><Route path="/presales/tasks/:id" element={<ProcessPage />} /></Routes>
  </MemoryRouter>);
}
function offerPage() {
  currentInquiry = { ...baseInquiry(), kind: "proactive_offer", status: "draft", created_by: 7 };
  return render(<MemoryRouter initialEntries={["/presales/offers/new?draft=1"]}><OfferNewPage /></MemoryRouter>);
}
async function makeProactiveDraft() {
  await screen.findByRole("button", { name: "发布主动报价" });
}

describe("采购待办与询价处理", () => {
  it("报价通知进入只读询价详情并显示通知报价，不请求采购参考接口", async () => {
    currentInquiry = { ...baseInquiry(), kind: "proactive_offer", created_by: undefined,
      offers: [quote({ status: "published", unit_price: "567.89", valid_from: "2026-10-25T00:00:00Z" })] };
    render(<MemoryRouter initialEntries={["/presales/tasks"]}><Routes>
      <Route path="/presales/tasks" element={<TasksPage />} />
      <Route path="/presales/inquiries/:id" element={<InquiryDetailPage />} />
      <Route path="/presales/workbench/:id" element={<ProcessPage />} />
    </Routes></MemoryRouter>);
    fireEvent.click(screen.getByRole("tab", { name: "报价通知" }));
    fireEvent.click(screen.getByRole("button", { name: "打开采购待办" }));
    await screen.findByRole("heading", { name: "询价单 XJ-1" });
    expect(screen.getAllByText(/567.89/).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "退回公共池" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存草稿" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "回复给销售" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "更新 / 撤销报价" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "关单" })).not.toBeInTheDocument();
    expect(mocks.reference).not.toHaveBeenCalled();
    expect(mocks.saveOffer).not.toHaveBeenCalled();
  });

  it("旧处理页带通知身份打开时也重定向至详情", async () => {
    render(<MemoryRouter initialEntries={[{ pathname: "/presales/workbench/1", state: { workItem: { ...task, subject_type: "presales_offer_notice" } } }]}><Routes>
      <Route path="/presales/workbench/:id" element={<ProcessPage />} />
      <Route path="/presales/inquiries/:id" element={<div>只读通知详情</div>} />
    </Routes></MemoryRouter>);
    expect(await screen.findByText("只读通知详情")).toBeInTheDocument();
    expect(mocks.getInquiry).not.toHaveBeenCalled();
    expect(mocks.reference).not.toHaveBeenCalled();
  });

  it("保存其他行成功但刷新失败时保留建档抽屉全部输入并锁定提交", async () => {
    currentInquiry.lines![0].part_id = null;
    currentInquiry.offers = [quote({ id: 82, line_id: 12, part_id: 2 })];
    processPage();
    await openLine("PN-1", true);
    fireEvent.change(await screen.findByLabelText("PN-1建档描述"), { target: { value: "未保存的完整描述" } });
    fireEvent.change(screen.getByLabelText("PN-1建档简介"), { target: { value: "GPU 简介" } });
    fireEvent.change(screen.getByLabelText("PN-1建档品牌"), { target: { value: "品牌草稿" } });
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    fireEvent.change(screen.getByRole("combobox", { name: "参数 1 名称" }), { target: { value: "容量" } });
    fireEvent.change(screen.getByLabelText("参数 1 值"), { target: { value: "80GB" } });
    mocks.getInquiry.mockRejectedValueOnce({ response: { status: 503 } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await screen.findByText("保存报价已成功，但页面刷新失败，请勿重复提交。");
    expect(screen.getByLabelText("PN-1建档描述")).toHaveValue("未保存的完整描述");
    expect(screen.getByLabelText("PN-1建档简介")).toHaveValue("GPU 简介");
    expect(screen.getByLabelText("PN-1建档品牌")).toHaveValue("品牌草稿");
    expect(screen.getByLabelText("参数 1 值")).toHaveValue("80GB");
    expect(screen.getByRole("button", { name: /查\s*重/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "刷新结果" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /查\s*重/ })).toBeEnabled());
    expect(screen.getByLabelText("PN-1建档描述")).toHaveValue("未保存的完整描述");
    expect(screen.getByRole("combobox", { name: "参数 1 名称" })).toHaveValue("容量");
    expect(screen.getByLabelText("参数 1 值")).toHaveValue("80GB");
    expect(mocks.saveOffer).toHaveBeenCalledOnce();
    expect(mocks.createPart).not.toHaveBeenCalled();
  });
  it("复用售前待办组件并带待办身份打开处理页", async () => {
    function Opened() { const location = useLocation(); return <div>处理单 {(location.state as { workItem: WorkItem }).workItem.subject_id}</div>; }
    render(<MemoryRouter initialEntries={["/presales/tasks"]}><Routes>
      <Route path="/presales/tasks" element={<TasksPage />} /><Route path="/presales/workbench/:id" element={<Opened />} />
    </Routes></MemoryRouter>);
    expect(mocks.inbox.mock.calls[0][0].subjectType).toBe("presales_inquiry");
    fireEvent.click(screen.getByRole("button", { name: "打开采购待办" }));
    expect(await screen.findByText("处理单 1")).toBeInTheDocument();
  });

  it("查重命中后选定已有主数据并使用行版本绑定", async () => {
    currentInquiry.lines![0].part_id = null;
    mocks.partCheck.mockResolvedValue({ near_duplicates: [{ pn_std: "PN-42", reason: "近似" }] });
    mocks.bindPart.mockImplementation(async () => {
      currentInquiry = { ...currentInquiry, version: 5, lines: currentInquiry.lines!.map((line) => line.id === 11 ? { ...line, part_id: 42, version: 2 } : line) };
      return currentInquiry;
    });
    processPage();
    await openLine("PN-1", true);
    fireEvent.click(await screen.findByRole("button", { name: /查\s*重/ }));
    expect(mocks.partCheck).toHaveBeenCalledWith(11, "PN-1");
    fireEvent.click(await screen.findByRole("button", { name: "选定已有 PN" }));
    fireEvent.click(screen.getByRole("button", { name: "绑定已有 PN" }));
    await waitFor(() => expect(mocks.bindPart).toHaveBeenCalledWith(11, { expected_version: 1, part_id: 42 }));
    await waitFor(() => expect(screen.queryByRole("button", { name: /查\s*重/ })).not.toBeInTheDocument());
  });

  it("报价通知复用待办组件并打开对应询价单", async () => {
    render(<MemoryRouter initialEntries={["/presales/tasks"]}><Routes>
      <Route path="/presales/tasks" element={<TasksPage />} />
      <Route path="/presales/inquiries/:id" element={<div>报价通知单详情</div>} />
    </Routes></MemoryRouter>);
    fireEvent.click(screen.getByRole("tab", { name: "报价通知" }));
    expect(mocks.inbox.mock.lastCall?.[0].subjectType).toBe("presales_offer_notice");
    fireEvent.click(screen.getByRole("button", { name: "打开采购待办" }));
    expect(await screen.findByText("报价通知单详情")).toBeInTheDocument();
  });

  it("查重无命中才能新建并绑定主数据", async () => {
    currentInquiry.lines![0].part_id = null;
    mocks.partCheck.mockResolvedValue({ near_duplicates: [] });
    mocks.createPart.mockResolvedValue({ ...currentInquiry, version: 5,
      lines: currentInquiry.lines!.map((line) => ({ ...line, part_id: line.id })) });
    processPage();
    await openLine("PN-1", true);
    await screen.findByRole("button", { name: /查\s*重/ });
    expect(screen.queryByRole("button", { name: "新建并绑定 PN" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /查\s*重/ }));
    const createButton = await screen.findByRole("button", { name: "新建并绑定 PN" });
    await waitFor(() => expect(createButton).toBeEnabled());
    fireEvent.click(createButton);
    await waitFor(() => expect(mocks.createPart).toHaveBeenCalledWith(11, {
      expected_version: 1, pn_std: "PN-1", description: "型号 1", brand: null, machine_or_part: "备件",
    }));
  });

  it("逐行保存新报价或填写不报原因，不再填写有效期，全部完成才回复", async () => {
    processPage();
    await openLine("PN-1");
    await openLine("PN-1");
    await screen.findByLabelText("PN-1单价");
    expect(screen.queryByLabelText("统一有效期")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("PN-1有效天数")).not.toBeInTheDocument();
    expect(within(screen.getByRole("dialog")).getByText("报价不设有效期，一直有效，直到你更新或撤回。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "回复给销售" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("PN-1单价"), { target: { value: "120" } });
    expect(screen.getAllByLabelText("供应商可见性：私密")).toHaveLength(1);
    fireEvent.click(screen.getAllByRole("button", { name: "保存该行报价" })[0]);
    await waitFor(() => expect(mocks.saveOffer).toHaveBeenCalledWith(1, 11, expect.objectContaining({
      unit_price: 120, expected_version: 4, work_item_id: 71,
      supplier_audience: { mode: "private", user_ids: [], group_ids: [] },
    })));
    await waitFor(() => expect(screen.getByText("已保存草稿")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "回复给销售" })).toBeDisabled();
    await openLine("PN-2");
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "PN-2处理方式" })).getByRole("radio", { name: "无货 / 不报" }));
    expect(screen.getByRole("button", { name: "回复给销售" })).toBeDisabled();
    await openLine("PN-2");
    fireEvent.change(screen.getByLabelText("PN-2不报原因"), { target: { value: "供应商确认无货" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "回复给销售" })).toBeEnabled());
    await closeLine();
    fireEvent.click(screen.getByRole("button", { name: "回复给销售" }));
    await waitFor(() => expect(mocks.replyInquiry).toHaveBeenCalledWith(1, {
      expected_version: 4, work_item_id: 71, expected_work_item_version: 3,
      line_responses: [{ line_id: 11, mode: "quote" }, { line_id: 12, mode: "skip", reason: "供应商确认无货" }],
    }));
    await openLine("PN-1");
    await screen.findAllByRole("button", { name: "更新报价" });
    expect(screen.queryByRole("button", { name: "回复给销售" })).not.toBeInTheDocument();
  });

  it("已回复报价更新走报价版本，不覆盖未调整的可见性，撤销必填原因", async () => {
    currentInquiry.lines = [currentInquiry.lines![0]];
    currentInquiry.offers = [quote({ status: "published", version: 6, valid_from: "2026-10-25T00:00:00Z" })];
    currentInquiry.work_items![0].status = "done";
    mocks.updateOffer.mockImplementation(async () => {
      const updated = quote({ ...currentInquiry.offers![0], unit_price: 150, version: 7 });
      currentInquiry = { ...currentInquiry, offers: [updated] }; return updated;
    });
    mocks.withdrawOffer.mockImplementation(async () => {
      const withdrawn = quote({ ...currentInquiry.offers![0], status: "withdrawn", version: 8 });
      currentInquiry = { ...currentInquiry, offers: [withdrawn] }; return withdrawn;
    });
    processPage();
    await openLine("PN-1");
    fireEvent.change(await screen.findByLabelText("PN-1单价"), { target: { value: "150" } });
    fireEvent.click(screen.getByRole("button", { name: "更新报价" }));
    await waitFor(() => expect(mocks.updateOffer).toHaveBeenCalledWith(81, expect.objectContaining({ unit_price: 150, expected_version: 6 })));
    expect(mocks.updateOffer.mock.calls[0][1]).not.toHaveProperty("supplier_audience");
    await waitFor(() => expect(screen.getByRole("button", { name: "撤销报价" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "撤销报价" }));
    expect(await screen.findByRole("button", { name: "确认撤销" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("撤销原因"), { target: { value: "  报价填写错误  " } });
    fireEvent.click(screen.getByRole("button", { name: "确认撤销" }));
    await waitFor(() => expect(mocks.withdrawOffer).toHaveBeenCalledWith(81, { expected_version: 7, reason: "报价填写错误" }));
  });

  it("深链接从本人待办找身份；版本冲突后自动刷新且保留报价输入", async () => {
    currentInquiry.offers = [quote({ unit_price: "80", version: 1 })];
    mocks.saveOffer.mockImplementationOnce(async () => {
      currentInquiry = { ...currentInquiry, offers: [quote({ unit_price: "99", version: 2 })] };
      throw { response: { status: 409, data: { detail: { code: "version_conflict", message: "报价版本已更新" } } } };
    });
    processPage(false);
    await openLine("PN-1");
    fireEvent.change(await screen.findByLabelText("PN-1单价"), { target: { value: "120" } });
    fireEvent.click(screen.getAllByRole("button", { name: "保存该行报价" })[0]);
    expect(await screen.findByText("报价版本已更新")).toBeInTheDocument();
    expect(mocks.listWorkItems).toHaveBeenCalledWith({ tab: "assigned", subject_type: "presales_inquiry", page: 1, page_size: 100 });
    await waitFor(() => expect(mocks.getInquiry).toHaveBeenCalledTimes(2));
    expect(Number((screen.getByLabelText("PN-1单价") as HTMLInputElement).value)).toBe(120);
    expect(mocks.saveOffer).toHaveBeenCalledOnce();
  });

  it("报价草稿保存成功但刷新失败时明确告知，不重复提交", async () => {
    processPage();
    await openLine("PN-1");
    fireEvent.change(await screen.findByLabelText("PN-1单价"), { target: { value: "120" } });
    mocks.getInquiry.mockRejectedValueOnce({ response: { status: 503, headers: { "x-request-id": "refresh-failed" } } });
    fireEvent.click(screen.getAllByRole("button", { name: "保存该行报价" })[0]);
    expect(await screen.findByText("保存报价已成功，但页面刷新失败，请勿重复提交。")).toBeVisible();
    expect(mocks.saveOffer).toHaveBeenCalledOnce();
    expect(screen.getByRole("dialog")).toHaveTextContent("120");
    await closeLine();
    expect(screen.getByRole("button", { name: "保存草稿" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "刷新结果" }));
    await waitFor(() => expect(screen.queryByText("操作已成功，页面尚未更新，请刷新确认后继续。")).not.toBeInTheDocument());
    expect(mocks.saveOffer).toHaveBeenCalledOnce();
    await openLine("PN-1");
    expect(Number((screen.getByLabelText("PN-1单价") as HTMLInputElement).value)).toBe(120);
  });

  it("他人已绑定 PN 时，冲突不关闭建档抽屉，确认重载后才放弃资料", async () => {
    currentInquiry.lines![0].part_id = null;
    mocks.partCheck.mockResolvedValue({ near_duplicates: [] });
    mocks.createPart.mockImplementationOnce(async () => {
      currentInquiry = { ...currentInquiry, lines: currentInquiry.lines!.map((line) => line.id === 11
        ? { ...line, part_id: 42, version: line.version + 1 } : line) };
      throw { response: { status: 409, data: { detail: { code: "version_conflict", message: "记录版本已变化" } } } };
    });
    processPage();
    fireEvent.click(await screen.findByRole("button", { name: "建档 PN-1" }));
    fireEvent.change(await screen.findByLabelText("PN-1建档简介"), { target: { value: "尚未保存的 GPU 资料" } });
    fireEvent.click(screen.getByRole("button", { name: /查\s*重/ }));
    const create = await screen.findByRole("button", { name: "新建并绑定 PN" });
    await waitFor(() => expect(create).toBeEnabled());
    fireEvent.click(create);
    await screen.findByText("该行已变化，建档填写内容仍保留。");
    expect(mocks.getInquiry).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("PN-1建档简介")).toHaveValue("尚未保存的 GPU 资料");
    fireEvent.click(screen.getByRole("button", { name: "重新加载该行" }));
    fireEvent.click(await screen.findByRole("button", { name: "继续保留" }));
    expect(mocks.getInquiry).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "重新加载该行" }));
    fireEvent.click(await screen.findByRole("button", { name: "重新加载" }));
    await waitFor(() => expect(mocks.getInquiry).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByLabelText("PN-1建档简介")).not.toBeInTheDocument());
    expect(mocks.createPart).toHaveBeenCalledOnce();
  });

  it("销售重新提交后旧报价 stale，可重新填报价且不沿用旧报价 ID", async () => {
    currentInquiry.lines = [currentInquiry.lines![0]];
    currentInquiry.offers = [quote({ status: "stale", version: 6 })];
    currentInquiry.field_values = [{ field_def_id: 1, name: "质保要求", value: "一年" }];
    processPage();
    await openLine("PN-1");
    expect(await screen.findByText("质保要求: 一年")).toBeInTheDocument();
    expect(screen.getByText("基于旧版本")).toBeInTheDocument();
    await openLine("PN-1");
    expect(screen.getByLabelText("PN-1单价")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("PN-1单价"), { target: { value: "160" } });
    fireEvent.click(screen.getByRole("button", { name: "保存该行报价" }));
    await waitFor(() => expect(mocks.saveOffer).toHaveBeenCalledWith(1, 11, expect.objectContaining({ unit_price: 160, expected_version: 4 })));
    expect(mocks.saveOffer.mock.calls[0][2]).not.toHaveProperty("offer_id");
  });

  it("销售已引用的行仅展示钉住报价，不提供再次报价或撤销入口", async () => {
    const published = quote({ status: "published", revision_no: 1,
      valid_from: "2026-10-25T00:00:00Z" });
    currentInquiry.lines![0] = { ...currentInquiry.lines![0], ref_offer_id: published.id,
      ref_offer_revision_no: 1, ref_offer: published };
    currentInquiry.offers = [published];
    processPage();
    await openLine("PN-1");
    const referenced = (await screen.findByText("已引用报价，无需再次处理")).closest("tr")!;
    expect(within(referenced).queryByRole("button", { name: "更新报价" })).not.toBeInTheDocument();
    expect(within(referenced).queryByRole("button", { name: "撤销报价" })).not.toBeInTheDocument();
    expect(within(referenced).queryByLabelText("PN-1单价")).not.toBeInTheDocument();
    await openLine("PN-2");
    expect(screen.getAllByRole("button", { name: "保存该行报价" })).toHaveLength(1);
  });

  it("主动报价无需采购待办，可更新重复报价指向的原有效报价", async () => {
    const original = quote({ id: 99, line_id: 90, inquiry_id: 9, status: "published", version: 8,
      valid_from: "2026-10-25T00:00:00Z" });
    currentInquiry = { ...currentInquiry, kind: "proactive_offer", created_by: 7,
      lines: [{ ...currentInquiry.lines![0], ref_offer_id: 99 }], offers: [], work_items: [] };
    mocks.library.mockResolvedValue({ 1: [original] });
    mocks.updateOffer.mockResolvedValue({ ...original, unit_price: 170, version: 9 });
    processPage(false);
    await openLine("PN-1");
    fireEvent.change(await screen.findByLabelText("PN-1单价"), { target: { value: "170" } });
    fireEvent.click(screen.getByRole("button", { name: "更新报价" }));
    await waitFor(() => expect(mocks.updateOffer).toHaveBeenCalledWith(99, expect.objectContaining({ expected_version: 8, unit_price: 170 })));
    expect(mocks.listWorkItems).not.toHaveBeenCalled();
  });

  it("重复主动报价引用过期后，先通过历史定位原单，再用当前版本续期", async () => {
    const expired = quote({ id: 99, line_id: 90, inquiry_id: 9, status: "published", version: 12,
      valid_from: "2026-10-20T00:00:00Z" });
    currentInquiry = { ...currentInquiry, kind: "proactive_offer", created_by: 7,
      lines: [{ ...currentInquiry.lines![0], ref_offer_id: 99 }], offers: [], work_items: [] };
    mocks.library.mockResolvedValue({ 1: [] });
    mocks.revisions.mockResolvedValue([{ revision_no: 1, snapshot: { ...expired, version: 2 } }]);
    mocks.getInquiry.mockImplementation(async (id: number) => id === 9
      ? { ...currentInquiry, id: 9, offers: [expired] } : currentInquiry);
    mocks.updateOffer.mockResolvedValue({ ...expired, version: 13 });
    processPage(false);
    await openLine("PN-1");
    await openLine("PN-1");
    fireEvent.change(screen.getByLabelText("PN-1单价"), { target: { value: "130" } });
    fireEvent.click(screen.getByRole("button", { name: "更新报价" }));
    await waitFor(() => expect(mocks.updateOffer).toHaveBeenCalledWith(99, expect.objectContaining({ expected_version: 12 })));
    expect(mocks.revisions).toHaveBeenCalledWith(99);
    expect(mocks.getInquiry).toHaveBeenCalledWith(9);
  });
});

describe("采购主动报价", () => {
  it("主动报价版本冲突保留价格，确认重新加载后才采用服务端报价", async () => {
    currentInquiry = { ...currentInquiry, kind: "proactive_offer", status: "draft", created_by: 7,
      lines: [currentInquiry.lines![0]], offers: [quote({ version: 5 })] };
    mocks.saveOffer.mockRejectedValueOnce({ response: { status: 409, data: { detail: { code: "version_conflict" } } } });
    render(<MemoryRouter initialEntries={["/presales/offers/new?draft=1"]}><OfferNewPage /></MemoryRouter>);
    await openLine("PN-1");
    fireEvent.change(await screen.findByLabelText("PN-1单价"), { target: { value: "350" } });
    await closeLine();
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await screen.findByText("报价版本已变化，当前填写内容仍保留。");
    expect(mocks.getInquiry).toHaveBeenCalledTimes(1);
    await openLine("PN-1");
    expect(screen.getByLabelText("PN-1单价")).toHaveValue("350.00");
    await closeLine();
    fireEvent.click(screen.getByRole("button", { name: "重新加载最新报价" }));
    fireEvent.click(await screen.findByRole("button", { name: "继续保留" }));
    expect(mocks.getInquiry).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "重新加载最新报价" }));
    fireEvent.click(await screen.findByRole("button", { name: "重新加载" }));
    await waitFor(() => expect(mocks.getInquiry).toHaveBeenCalledTimes(2));
    await openLine("PN-1");
    expect(screen.getByLabelText("PN-1单价")).toHaveValue("120.00");
    expect(mocks.saveOffer).toHaveBeenCalledOnce();
  });

  it("主动报价遇到已有草稿409后接续草稿版本，保留输入与请求编号", async () => {
    offerPage();
    await makeProactiveDraft();
    await openLine("PN-1");
    fireEvent.change(screen.getByLabelText("PN-1单价"), { target: { value: "333" } });
    mocks.saveOffer.mockImplementationOnce(async () => {
      currentInquiry = { ...baseInquiry(), kind: "proactive_offer", status: "draft", created_by: 7,
        offers: [quote({ id: 181, unit_price: "222", version: 6 })] };
      throw { response: { status: 409, headers: { "x-request-id": "existing-draft-r2" },
        data: { detail: { code: "existing_draft", message: "该行货源已有草稿", offer_id: 181, version: 6 } } } };
    });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await screen.findByText("该行货源已有草稿");
    fireEvent.click(screen.getByText("查看诊断信息"));
    expect(screen.getByText(/existing-draft-r2/)).toBeVisible();
    await waitFor(() => expect(mocks.getInquiry).toHaveBeenCalledWith(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "保存草稿" })).toBeEnabled());
    expect(Number((screen.getByLabelText("PN-1单价") as HTMLInputElement).value)).toBe(333);
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(mocks.saveOffer).toHaveBeenLastCalledWith(1, 11,
      expect.objectContaining({ offer_id: 181, expected_version: 6, unit_price: 333 })));
    expect(mocks.saveOffer).toHaveBeenCalledTimes(2);
  });

  it("历史多行主动报价草稿仍可逐行保存后发布，不重复建单", async () => {
    mocks.publishInquiry.mockResolvedValue([]);
    offerPage();
    await makeProactiveDraft();
    expect(mocks.createInquiry).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "发布主动报价" })).toBeDisabled();
    await openLine("PN-1");
    fireEvent.change(screen.getByLabelText("PN-1单价"), { target: { value: "300" } });
    await openLine("PN-2");
    fireEvent.click(screen.getByRole("checkbox", { name: "无货" }));
    fireEvent.change(screen.getByLabelText("PN-2备注"), { target: { value: "供应商确认无货" } });
    await closeLine();
    fireEvent.click(screen.getByRole("button", { name: "发布主动报价" }));
    await screen.findByText("主动报价已发布");
    expect(mocks.saveOffer).toHaveBeenCalledTimes(2);
    expect(mocks.publishInquiry).toHaveBeenCalledWith(1, { expected_version: 4, notice_user_ids: [] });
    expect(mocks.refreshWorkBadges).toHaveBeenCalled();
  });

  it("本人同 PN 有效报价提示发布时明确选择更新或另报一条", async () => {
    mocks.library.mockResolvedValue({ 1: [quote({ status: "published", valid_from: "2026-10-25T00:00:00Z" })] });
    offerPage();
    await makeProactiveDraft();
    await openLine("PN-1");
    expect(await screen.findByText("你已有该 PN 的有效报价，发布时请选择更新或另报一条")).toBeInTheDocument();
  });

  it("复制出的主动报价草稿可通过 draft 参数继续填写，支持定向通知", async () => {
    currentInquiry = { ...currentInquiry, kind: "proactive_offer", status: "draft", created_by: 7,
      lines: [currentInquiry.lines![0]], offers: [quote({ version: 5 })] };
    mocks.publishInquiry.mockResolvedValue([]);
    render(<MemoryRouter initialEntries={["/presales/offers/new?draft=1"]}><OfferNewPage /></MemoryRouter>);
    await openLine("PN-1");
    expect(await screen.findByLabelText("PN-1单价")).toHaveValue("120.00");
    expect(mocks.createInquiry).not.toHaveBeenCalled();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "定向通知销售" }));
    fireEvent.click(await screen.findByText("销售甲"));
    await closeLine();
    fireEvent.click(screen.getByRole("button", { name: "发布主动报价" }));
    await waitFor(() => expect(mocks.publishInquiry).toHaveBeenCalledWith(1, { expected_version: 4, notice_user_ids: [3] }));
    expect(mocks.saveOffer).toHaveBeenCalledWith(1, 11, expect.objectContaining({ offer_id: 81, expected_version: 5 }));
  });

  it("从已有草稿切回新建入口时清空上一张单据的身份与报价", async () => {
    currentInquiry = { ...currentInquiry, kind: "proactive_offer", status: "draft", created_by: 7,
      lines: [currentInquiry.lines![0]], offers: [quote()] };
    function Switch() {
      const navigate = useNavigate();
      return <button onClick={() => navigate("/presales/offers/new")}>打开全新主动报价</button>;
    }
    render(<MemoryRouter initialEntries={["/presales/offers/new?draft=1"]}><Switch /><OfferNewPage /></MemoryRouter>);
    await openLine("PN-1");
    await screen.findByLabelText("PN-1单价");
    fireEvent.click(screen.getByRole("button", { name: "打开全新主动报价" }));
    expect(await screen.findByRole("combobox", { name: "主动报价 PN" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "生成报价草稿" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("PN-1单价")).not.toBeInTheDocument();
  });

  it("续填草稿尊重服务端省略字段，不显示供应商、货源、备注或报价人占位", async () => {
    const hidden = quote();
    delete hidden.supplier_id; delete hidden.source_key; delete hidden.notes; delete hidden.reporter;
    currentInquiry = { ...currentInquiry, kind: "proactive_offer", status: "draft", created_by: 7,
      lines: [currentInquiry.lines![0]], offers: [hidden] };
    render(<MemoryRouter initialEntries={["/presales/offers/new?draft=1"]}><OfferNewPage /></MemoryRouter>);
    await openLine("PN-1");
    await screen.findByLabelText("PN-1单价");
    await openLine("PN-1");
    for (const field of ["PN-1供应商", "PN-1货源标识", "PN-1备注"]) expect(screen.queryByLabelText(field)).not.toBeInTheDocument();
    expect(screen.queryByText("当前填写人")).not.toBeInTheDocument();
  });
});

it("处理页切换单据后，上一单保存触发的迟到刷新不会覆盖当前单据", async () => {
  const old = { ...baseInquiry(), kind: "proactive_offer" as const, created_by: 7,
    lines: [baseInquiry().lines![0]], offers: [quote({ status: "published" })], work_items: [] };
  const latest = { ...old, id: 2, inquiry_no: "XJ-2", created_by_name: "销售乙" };
  let resolve!: (value: Inquiry) => void;
  let reads = 0;
  mocks.getInquiry.mockImplementation((id: number) => {
    if (id === 2) return Promise.resolve(latest);
    return ++reads === 1 ? Promise.resolve(old) : new Promise<Inquiry>((done) => { resolve = done; });
  });
  mocks.updateOffer.mockResolvedValue(old.offers[0]);
  function Switch() {
    const navigate = useNavigate();
    return <button onClick={() => navigate("/presales/tasks/2")}>打开另一单</button>;
  }
  render(<MemoryRouter initialEntries={["/presales/tasks/1"]}><Switch /><Routes>
    <Route path="/presales/tasks/:id" element={<ProcessPage />} />
  </Routes></MemoryRouter>);
  await openLine("PN-1");
  fireEvent.click(await screen.findByRole("button", { name: "更新报价" }));
  await waitFor(() => expect(reads).toBe(2));
  fireEvent.click(screen.getByRole("button", { name: "打开另一单" }));
  expect(await screen.findByText("询价人：销售乙")).toBeInTheDocument();
  await act(async () => { resolve(old); });
  expect(screen.getByText("询价人：销售乙")).toBeInTheDocument();
  expect(screen.queryByText("XJ-1")).not.toBeInTheDocument();
});

it("原生表格不内嵌表单，不再显示统一有效期", async () => {
  processPage();
  const grid = await screen.findByRole("table", { name: "采购报价明细" });
  expect(within(grid).getAllByRole("row")).toHaveLength(3);
  expect(screen.queryByLabelText("PN-1供应商")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("PN-2供应商")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("统一有效期")).not.toBeInTheDocument();
  expect(screen.getByText("报价不设有效期，一直有效，直到更新或撤回")).toBeInTheDocument();
  await openLine("PN-1");
  fireEvent.change(screen.getByLabelText("PN-1单价"), { target: { value: "100" } });
  await openLine("PN-2");
  fireEvent.change(screen.getByLabelText("PN-2单价"), { target: { value: "200" } });
  await closeLine();
  fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
  await waitFor(() => expect(mocks.saveOffer).toHaveBeenCalledTimes(2));
  expect(mocks.saveOffer).toHaveBeenCalledWith(1, 11, expect.objectContaining({ unit_price: 100 }));
  expect(mocks.saveOffer).toHaveBeenCalledWith(1, 12, expect.objectContaining({ unit_price: 200 }));
  await waitFor(() => expect(screen.getByRole("button", { name: "回复给销售" })).toBeEnabled());
});

it("保存一行报价不会丢失另一行未保存的编辑", async () => {
  processPage();
    await openLine("PN-1");
  fireEvent.change(await screen.findByLabelText("PN-1单价"), { target: { value: "100" } });
  await openLine("PN-2");
  fireEvent.change(screen.getByLabelText("PN-2单价"), { target: { value: "200" } });
  await openLine("PN-1");
  fireEvent.click(screen.getByRole("button", { name: "保存该行报价" }));
  await screen.findByText("已保存草稿");
  await openLine("PN-2");
  expect(screen.getByLabelText("PN-2单价")).toHaveValue("200.00");
  expect(screen.getByRole("button", { name: "回复给销售" })).toBeDisabled();
});

it("深链接首次加载失败后重试会恢复本人待办身份", async () => {
  mocks.getInquiry.mockRejectedValueOnce(new Error("offline"));
  processPage(false);
  fireEvent.click(await screen.findByRole("button", { name: /重\s*试/ }));
  await openLine("PN-1");
  expect(await screen.findByLabelText("PN-1单价")).toBeEnabled();
  expect(mocks.listWorkItems).toHaveBeenCalledWith(expect.objectContaining({ tab: "assigned" }));
  await openLine("PN-1");
  fireEvent.change(screen.getByLabelText("PN-1单价"), { target: { value: "100" } });
  fireEvent.click(screen.getAllByRole("button", { name: "保存该行报价" })[0]);
  await waitFor(() => expect(mocks.saveOffer).toHaveBeenCalledWith(1, 11, expect.objectContaining({ work_item_id: 71 })));
});

it("返回列表先确认放弃未保存修改，取消继续保留当前输入", async () => {
  render(<MemoryRouter initialEntries={[{ pathname: "/presales/workbench/1", state: { workItem: task } }]}><Routes>
    <Route path="/presales/workbench/:id" element={<ProcessPage />} />
    <Route path="/presales/workbench" element={<div>待办列表</div>} />
  </Routes></MemoryRouter>);
  await openLine("PN-1");
  fireEvent.change(screen.getByLabelText("PN-1单价"), { target: { value: "133" } });
  await closeLine();
  fireEvent.click(screen.getByRole("button", { name: "返回列表" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "继续填写" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await openLine("PN-1");
  expect(screen.getByLabelText("PN-1单价")).toHaveValue("133.00");
  expect(mocks.getInquiry).not.toHaveBeenCalledWith(2);
  await closeLine();
  fireEvent.click(screen.getByRole("button", { name: "返回列表" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "放弃修改并返回" }));
  expect(await screen.findByText("待办列表")).toBeInTheDocument();
  expect(mocks.saveOffer).not.toHaveBeenCalled();
});

it("保存失败仍保留未保存输入，离开页面和返回列表均保护草稿", async () => {
  mocks.saveOffer.mockRejectedValueOnce(new Error("offline"));
  processPage();
  await openLine("PN-1");
  fireEvent.change(screen.getByLabelText("PN-1单价"), { target: { value: "133" } });
  fireEvent.click(screen.getByRole("button", { name: "保存该行报价" }));
  await waitFor(() => expect(mocks.saveOffer).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByRole("button", { name: "保存该行报价" })).toBeEnabled());
  expect(screen.getByLabelText("PN-1单价")).toHaveValue("133.00");
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(true);
  await closeLine();
  fireEvent.click(screen.getByRole("button", { name: "返回列表" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "继续填写" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await openLine("PN-1");
  expect(screen.getByLabelText("PN-1单价")).toHaveValue("133.00");
});

it("隐藏客户要求不显示空值占位", async () => {
  currentInquiry.offers = [quote(), quote({ id: 82, line_id: 12, part_id: 2 })];
  delete currentInquiry.notes;
  processPage();
  await openLine("PN-1");
  await screen.findByLabelText("PN-1单价");
  expect(screen.queryByText("无客户补充要求")).not.toBeInTheDocument();
});


it("采购报价页显示销售姓名，单号和只读禁用输入不出现", async () => {
  currentInquiry.created_by_name = "销售张三";
  currentInquiry.work_items = [];
  mocks.listWorkItems.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
  processPage(false);
  expect(await screen.findByRole("heading", { name: "询价人：销售张三" })).toBeInTheDocument();
  expect(screen.queryByText("XJ-1")).not.toBeInTheDocument();
  await openLine("PN-1");
  expect(screen.queryByLabelText("PN-1单价")).not.toBeInTheDocument();
  expect(screen.queryByRole("spinbutton", { name: "统一有效期" })).not.toBeInTheDocument();
});

it.each(["采购询价", "报价通知"])("%s 主要标识为可见询价人，隐藏人员不回退到单号", async (tab) => {
  render(<MemoryRouter><TasksPage /></MemoryRouter>);
  if (tab === "报价通知") fireEvent.click(screen.getByRole("tab", { name: tab }));
  const renderRow = mocks.inbox.mock.lastCall![0].renderRow;
  const name = render(<>{renderRow({ ...task, summary: { inquiry_no: "XJ-SECRET", created_by_name: "销售李四" } })}</>);
  expect(name.getByText("销售李四")).toBeInTheDocument();
  expect(name.queryByText("XJ-SECRET")).not.toBeInTheDocument();
  name.unmount();
  const hidden = render(<>{renderRow({ ...task, summary: { inquiry_no: "XJ-SECRET" } })}</>);
  expect(hidden.getByText("业务人员")).toBeInTheDocument();
  expect(hidden.queryByText("XJ-SECRET")).not.toBeInTheDocument();
});
