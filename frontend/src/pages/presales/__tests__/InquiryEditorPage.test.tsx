import { openLine, closeLine } from "./nativeUi";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { cleanupAntd, render } from "../../../test/antdRender";
import type { Inquiry, Offer } from "../../../api/presales";

const mocks = vi.hoisted(() => ({
  presales: { getInquiry: vi.fn(), createInquiry: vi.fn(), updateInquiry: vi.fn(), submitInquiry: vi.fn(),
    resubmitInquiry: vi.fn(), appendLines: vi.fn(), library: vi.fn(), customers: vi.fn() },
  suggestParts: vi.fn(), getFieldTemplate: vi.fn(), listFieldDefinitions: vi.fn(),
  listWorkCandidates: vi.fn(),
  masterCategories: vi.fn(), masterCheck: vi.fn(), masterCreate: vi.fn(),
}));
vi.mock("../../../api", async (importOriginal) => ({ ...await importOriginal<typeof import("../../../api")>(),
  masterCategories: mocks.masterCategories, masterCheck: mocks.masterCheck, masterCreate: mocks.masterCreate,
}));
vi.mock("../../../api/presales", () => ({ presales: mocks.presales }));
vi.mock("../../../api/search", () => ({ suggestParts: mocks.suggestParts }));
vi.mock("../../../api/workItems", () => ({ listWorkCandidates: mocks.listWorkCandidates }));
vi.mock("../../../api/fields", () => ({ getFieldTemplate: mocks.getFieldTemplate, listFieldDefinitions: mocks.listFieldDefinitions,
  suggestFieldNames: vi.fn(async () => []), defineField: vi.fn() }));
vi.mock("../../../api/audience", () => ({ listAudiencePeople: vi.fn(async () => []), listAudienceGroups: vi.fn(async () => []) }));
vi.mock("../../../components/gallery/PartThumb", () => ({ default: ({ pn }: { pn: string }) => <span>{pn} 图片</span> }));
vi.mock("../../../components/pricing/PriceRefBadge", () => ({ default: () => <span>参考价</span> }));
import InquiryEditorPage from "../InquiryEditorPage";
vi.setConfig({ testTimeout: 15000 });

const partA = { part_id: 10, pn_std: "PN-A", brand: "品牌甲", summary: "型号简介", match_type: "prefix", match_reason: "PN", matched_terms: [] };
const partB = { ...partA, part_id: 11, pn_std: "PN-B" };
const validOffer: Offer = { id: 70, inquiry_id: 5, line_id: 50, part_id: 10, version: 1, revision_no: 1,
  status: "published", unit_price: "12.50", is_tax_inclusive: true, no_stock: false,
  valid_from: "2020-01-01T00:00:00Z" };
const field = { id: 31, name: "质保要求", value_type: "text", subject_type: "presales_inquiry", level: "line",
  owner_scope: "personal", owner_id: "sales", choices: null, default_rule_id: null, default_rule: null,
  auto_include: true, sort: 0, archived: false };
function inquiry(changes: Partial<Inquiry> = {}): Inquiry {
  return { id: 1, kind: "sales_inquiry", inquiry_no: "XJ001", status: "draft", version: 1, revision: 1,
    customer_id: 5, notes: "客户原要求", assignee_ids: [2], created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
    line_count: 1, progress: { quoted_lines: 0, line_count: 1 }, field_values: [],
    lines: [{ id: 10, inquiry_id: 1, line_key: "stable-a", seq: 1, part_id: 10, pn_raw: "PN-A", qty: "2", require_exact_pn: true,
      ref_offer_id: null, notes: "原行备注", version: 1, status: "active", field_values: [] }], ...changes };
}
let pathname = "";
function Probe() { pathname = useLocation().pathname; return <div data-testid="route">{pathname}</div>; }
function mount(path = "/presales/inquiries/new", state?: unknown) {
  return render(<MemoryRouter initialEntries={[{ pathname: path.split("?")[0], search: path.includes("?") ? `?${path.split("?")[1]}` : "", state }]}>
    <Routes>
      <Route path="/presales/inquiries/new" element={<InquiryEditorPage />} />
      <Route path="/presales/inquiries/:id/edit" element={<InquiryEditorPage />} />
      <Route path="*" element={<Probe />} />
    </Routes><Probe />
  </MemoryRouter>);
}
async function addPart(pn: string) {
  await closeLine();
  fireEvent.change(screen.getByRole("textbox", { name: "搜索并批量加入 PN" }), { target: { value: pn } });
  fireEvent.click(await screen.findByRole("option", { name: new RegExp(pn) }));
  await openLine(pn);
  await screen.findByRole("spinbutton", { name: `${pn} 数量` });
}
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); pathname = "";
  mocks.suggestParts.mockImplementation(async (q: string) => q === "PN-A" ? [partA] : q === "PN-B" ? [partB] : []);
  mocks.presales.library.mockResolvedValue({ 10: [validOffer], 11: [] });
  mocks.listWorkCandidates.mockResolvedValue({ items: [{ id: 2, display_name: "采购甲" }, { id: 3, display_name: "采购乙" }] });
  mocks.masterCategories.mockResolvedValue({ data: { categories: [], spec_fields: [] } });
  mocks.masterCheck.mockResolvedValue({ data: { near_duplicates: [] } });
  mocks.masterCreate.mockResolvedValue({ data: { created: true, id: 71, pn_std: "NEW-CPU" } });
  mocks.presales.customers.mockResolvedValue([{ id: 5, name: "客户甲" }]);
  mocks.getFieldTemplate.mockImplementation(async (_subject, level) => level === "line" ? [field] : []);
  mocks.listFieldDefinitions.mockImplementation(async (_subject, level) => level === "line" ? [field] : []);
  mocks.presales.getInquiry.mockResolvedValue(inquiry());
  const savedLines = (body: { lines: Inquiry["lines"] }) => body.lines?.map((line, index) => ({ ...line, line_key: line.line_key || `saved-${index}` }));
  // 保存响应遵循回读契约；不能把写入的 user_ids/group_ids 原样当成读取规则。
  const savedRules = (body: { offer_audience?: { mode: string }; notes_audience?: { mode: string } }) => ({
    ...(body.offer_audience ? { offer_audience: { mode: body.offer_audience.mode, users: [], groups: [] } } : {}),
    ...(body.notes_audience ? { notes_audience: { mode: body.notes_audience.mode, users: [], groups: [] } } : {}),
  });
  mocks.presales.createInquiry.mockImplementation(async (body) => inquiry({ ...body, ...savedRules(body), lines: savedLines(body), id: 9, version: 1 }));
  mocks.presales.updateInquiry.mockImplementation(async (id, body) => inquiry({ ...body, ...savedRules(body), lines: savedLines(body), id, version: body.expected_version + 1 }));
  mocks.presales.submitInquiry.mockResolvedValue(inquiry({ id: 9, status: "submitted", version: 2 }));
  mocks.presales.resubmitInquiry.mockResolvedValue(inquiry({ status: "submitted", version: 7 }));
  mocks.presales.appendLines.mockResolvedValue(inquiry({ status: "submitted", version: 5 }));
});
afterEach(cleanupAntd);

describe("销售询价编辑", () => {
  it("只读编辑页优先显示客户名称", async () => {
    mocks.presales.getInquiry.mockResolvedValue(inquiry({ status: "submitted", customer_name: "客户名称而非编号" }));
    mount("/presales/inquiries/1/edit");
    expect(await screen.findByText("客户名称而非编号")).toBeVisible();
    expect(screen.queryByText("5", { selector: ".ant-descriptions-item-content" })).not.toBeInTheDocument();
  });
  it("输入未建档PN可就地建档并加入询价，建档本身不提交询价", async () => {
    localStorage.setItem("permissions", JSON.stringify({ page_master_data: true }));
    mount();
    fireEvent.change(screen.getByRole("textbox", { name: "搜索并批量加入 PN" }), { target: { value: "NEW-CPU" } });
    fireEvent.click(await screen.findByRole("button", { name: "新建 PN「NEW-CPU」" }));
    fireEvent.change(screen.getByLabelText("NEW-CPU建档描述"), { target: { value: "处理器描述" } });
    fireEvent.click(screen.getByRole("button", { name: /查\s*重/ }));
    const createButton = await screen.findByRole("button", { name: "新建并加入当前单据" });
    await waitFor(() => expect(createButton).toBeEnabled());
    fireEvent.click(createButton);
    await screen.findByRole("button", { name: "编辑 NEW-CPU" });
    expect(mocks.presales.createInquiry).not.toHaveBeenCalled();
    expect(mocks.presales.submitInquiry).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
    await waitFor(() => expect(mocks.presales.createInquiry).toHaveBeenCalledWith(expect.objectContaining({ lines: [expect.objectContaining({ part_id: 71, pn_raw: "NEW-CPU", description: "处理器描述" })] })));
  });

  it("无主数据权限时保留原文待建档路径，不展示新建入口", async () => {
    mount();
    fireEvent.change(screen.getByRole("textbox", { name: "搜索并批量加入 PN" }), { target: { value: "NEW-CPU" } });
    await screen.findByText("暂无匹配型号");
    expect(screen.queryByRole("button", { name: "新建 PN「NEW-CPU」" })).toBeNull();
    expect(screen.getByRole("button", { name: /按原文添加「NEW-CPU」/ })).toBeEnabled();
    expect(mocks.masterCategories).not.toHaveBeenCalled();
  });

  it("多选加入、直接引用、按原文待建档、列存储交期与多人指派组成正确提交体", async () => {
    mount();
    await addPart("PN-A");
    fireEvent.click(await screen.findByRole("button", { name: "直接引用" }));
    await addPart("PN-B");
    fireEvent.change(screen.getByRole("textbox", { name: "搜索并批量加入 PN" }), { target: { value: "RAW-NEW" } });
    fireEvent.click(await screen.findByRole("button", { name: /按原文添加「RAW-NEW」/ }));
    expect(screen.getByText("待建档", { exact: true })).toBeVisible();
    await openLine("PN-A");
    fireEvent.click(screen.getByRole("button", { name: "客户要求 / 自定义字段" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN-A 数量" }), { target: { value: "4" } });
    fireEvent.click(screen.getAllByRole("checkbox", { name: "要求 PN 一致" })[0]);
    fireEvent.change(screen.getByLabelText("PN-A 期望交期"), { target: { value: "2026-11-01" } });
    const fieldInputs = await screen.findAllByRole("textbox", { name: "质保要求字段值" });
    fireEvent.change(fieldInputs[0], { target: { value: "三年" } });
    fireEvent.change(screen.getByRole("textbox", { name: "客户要求" }), { target: { value: "尽快报价" } });
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "客户" }));
    fireEvent.click(await screen.findByText("客户甲"));
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "处理人" }));
    fireEvent.click(await screen.findByText("采购甲"));
    fireEvent.click(await screen.findByText("采购乙"));
    fireEvent.click(screen.getByRole("button", { name: "提交" }));
    await waitFor(() => expect(mocks.presales.submitInquiry).toHaveBeenCalledWith(9, { expected_version: 1 }));
    expect(mocks.presales.createInquiry).toHaveBeenCalledWith(expect.objectContaining({ kind: "sales_inquiry", customer_id: 5,
      notes: "尽快报价", assignee_ids: [2, 3], offer_audience: { mode: "public", user_ids: [], group_ids: [] }, lines: [
        expect.objectContaining({ part_id: 10, qty: 4, require_exact_pn: true, ref_offer_id: 70, expected_delivery: "2026-11-01", field_values: [
          { field_def_id: 31, value: "三年" },
        ] }), expect.objectContaining({ part_id: 11, ref_offer_id: null }), expect.objectContaining({ part_id: null, pn_raw: "RAW-NEW" }),
      ] }));
    expect(mocks.getFieldTemplate).toHaveBeenCalledWith("presales_inquiry", "line", "part:10", expect.any(AbortSignal));
    await waitFor(() => expect(pathname).toBe("/presales/inquiries/9"));
  });

  it("处理人留空进公共池，保存草稿不提交", async () => {
    mount(); await addPart("PN-B");
    fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
    await waitFor(() => expect(mocks.presales.createInquiry).toHaveBeenCalledWith(expect.objectContaining({ assignee_ids: [] })));
    expect(mocks.presales.submitInquiry).not.toHaveBeenCalled();
  });

  it("客户候选实时调用售前专用接口", async () => {
    mount();
    await waitFor(() => expect(mocks.presales.customers).toHaveBeenCalledWith("", expect.any(AbortSignal)));
    fireEvent.change(screen.getByRole("combobox", { name: "客户" }), { target: { value: "客户甲" } });
    await waitFor(() => expect(mocks.presales.customers).toHaveBeenLastCalledWith("客户甲", expect.any(AbortSignal)));
  });

  it("客户字段权限关闭时不展示选择器、不查询或覆盖客户", async () => {
    localStorage.setItem("permissions", JSON.stringify({ data_customer: false }));
    mount("/presales/inquiries/1/edit");
    await openLine("PN-A");
    await screen.findByRole("spinbutton", { name: "PN-A 数量" });
    expect(screen.queryByRole("combobox", { name: "客户" })).not.toBeInTheDocument();
    expect(mocks.presales.customers).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
    await waitFor(() => expect(mocks.presales.updateInquiry).toHaveBeenCalled());
    expect(mocks.presales.updateInquiry.mock.calls[0][1]).not.toHaveProperty("customer_id");
  });

  it("整单、备注及 M7 回读真实可见性，修改内容时全部沿用原规则", async () => {
    const privateRule = { mode: "private" as const, users: [], groups: [] };
    const includeRule = { mode: "include" as const, users: [{ id: "sale-a", name: "销售甲" }], groups: [] };
    const excludeRule = { mode: "exclude" as const, users: [], groups: [{ id: 88, name: "旧分组" }] };
    const data = inquiry({ notes_audience: privateRule, offer_audience: includeRule,
      field_values: [{ field_def_id: 32, name: "单头说明", value: "原说明", rule: excludeRule }] });
    data.lines![0].notes_audience = excludeRule;
    data.lines![0].expected_delivery = "2026-11-01";
    data.lines![0].field_values = [{ field_def_id: 31, name: "质保要求", value: "一年", rule: privateRule },
      { field_def_id: 33, name: "期望交期", value: "2026-11-01", rule: includeRule }];
    mocks.getFieldTemplate.mockImplementation(async (_subject, level) => level === "line" ? [field] : [{ ...field, id: 32, name: "单头说明", level: "header" }]);
    mocks.listFieldDefinitions.mockImplementation(mocks.getFieldTemplate.getMockImplementation()!);
    mocks.presales.getInquiry.mockResolvedValue(data);
    mount("/presales/inquiries/1/edit");
    await openLine("PN-A");
    const warranty = await screen.findByRole("textbox", { name: "质保要求字段值" });
    fireEvent.click(screen.getByRole("button", { name: "客户要求 / 自定义字段" }));
    fireEvent.click(screen.getByRole("button", { name: "设置本单报价可见范围" }));
    expect(screen.getByRole("button", { name: "客户要求可见性：私密" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "PN-A 行备注可见性：不给谁看" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /PN-A 期望交期可见性/ })).not.toBeInTheDocument();
    expect(screen.getByLabelText("PN-A 期望交期")).toHaveValue("2026-11-01");
    expect(screen.getByRole("button", { name: "质保要求可见性：私密" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "单头说明可见性：不给谁看" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "谁可以看" }).closest(".ant-select")).toHaveTextContent("销售甲");
    fireEvent.change(warranty, { target: { value: "三年" } });
    fireEvent.change(screen.getByRole("textbox", { name: "单头说明字段值" }), { target: { value: "新说明" } });
    fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
    await waitFor(() => expect(mocks.presales.updateInquiry).toHaveBeenCalled());
    const body = mocks.presales.updateInquiry.mock.calls[0][1];
    expect(body).not.toHaveProperty("offer_audience");
    expect(body).not.toHaveProperty("notes_audience");
    expect(body.lines[0]).not.toHaveProperty("notes_audience");
    expect(body.field_values).toEqual([{ field_def_id: 32, value: "新说明" }]);
    expect(body.lines[0].field_values).toEqual([{ field_def_id: 31, value: "三年" }]);
    expect(body.lines[0].expected_delivery).toBe("2026-11-01");
  });

  it("撤回后的单据保留 line_key，先保存最新版本再重新提交且不重置可见性", async () => {
    mocks.presales.getInquiry.mockResolvedValue(inquiry({ status: "revising", version: 5 }));
    mocks.presales.updateInquiry.mockResolvedValueOnce(inquiry({ status: "revising", version: 6 }));
    mount("/presales/inquiries/1/edit");
    await openLine("PN-A");
    fireEvent.change(await screen.findByRole("spinbutton", { name: "PN-A 数量" }), { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "重新提交" }));
    await waitFor(() => expect(mocks.presales.resubmitInquiry).toHaveBeenCalledWith(1, { expected_version: 6 }));
    const body = mocks.presales.updateInquiry.mock.calls[0][1];
    expect(body.expected_version).toBe(5);
    expect(body.lines[0]).toMatchObject({ line_key: "stable-a", qty: 7 });
    expect(body).not.toHaveProperty("offer_audience");
    expect(body).not.toHaveProperty("notes_audience");
    expect(body.lines[0]).not.toHaveProperty("notes_audience");
  });

  it("追加只发送新行和当前版本，不改已有单头或原行", async () => {
    mocks.presales.getInquiry.mockResolvedValue(inquiry({ status: "submitted", version: 4 }));
    mount("/presales/inquiries/1/edit?append=1");
    await screen.findByText("新增行（原有行不变）");
    expect(screen.queryByRole("spinbutton", { name: "PN-A 数量" })).not.toBeInTheDocument();
    await addPart("PN-B");
    fireEvent.click(screen.getByRole("button", { name: "提交追加行" }));
    await waitFor(() => expect(mocks.presales.appendLines).toHaveBeenCalledWith(1, {
      expected_version: 4, lines: [expect.objectContaining({ part_id: 11, pn_raw: "PN-B" })],
    }));
    expect(mocks.presales.updateInquiry).not.toHaveBeenCalled();
    expect(mocks.presales.appendLines.mock.calls[0][1].lines[0]).not.toHaveProperty("line_key");
  });

  it("版本冲突保留未保存数量；服务端未返回的字段不显示也不覆盖", async () => {
    const hidden = inquiry({ version: 3 });
    delete hidden.customer_id; delete hidden.assignee_ids; delete hidden.notes;
    delete hidden.lines![0].notes;
    mocks.presales.getInquiry.mockResolvedValueOnce(hidden);
    mocks.presales.updateInquiry.mockRejectedValueOnce({ response: { status: 409, data: { detail: { code: "version_conflict", message: "记录版本已变化" } } } });
    mount("/presales/inquiries/1/edit");
    await openLine("PN-A");
    await screen.findByRole("spinbutton", { name: "PN-A 数量" });
    expect(screen.queryByRole("combobox", { name: "客户" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "客户要求" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN-A 数量" }), { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
    await screen.findByText("记录版本已变化");
    expect(mocks.presales.getInquiry).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("spinbutton", { name: "PN-A 数量" })).toHaveValue("7.000");
    const body = mocks.presales.updateInquiry.mock.calls[0][1];
    expect(body).not.toHaveProperty("customer_id");
    expect(body).not.toHaveProperty("assignee_ids");
    expect(body).not.toHaveProperty("notes");
    expect(body.lines[0]).not.toHaveProperty("notes");
  });

  it("清空已有自定义字段发送 null，未调整的字段规则保持省略", async () => {
    const data = inquiry();
    data.lines![0].field_values = [{ field_def_id: 31, name: "质保要求", value: "一年" }];
    mocks.presales.getInquiry.mockResolvedValue(data);
    mount("/presales/inquiries/1/edit");
    await openLine("PN-A");
    const input = await screen.findByRole("textbox", { name: "质保要求字段值" });
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
    await waitFor(() => expect(mocks.presales.updateInquiry).toHaveBeenCalled());
    expect(mocks.presales.updateInquiry.mock.calls[0][1].lines[0].field_values).toEqual([{ field_def_id: 31, value: null }]);
  });

  it("报价库带入引用后建单；提交失败保留草稿身份，重试不会重复创建", async () => {
    mocks.presales.submitInquiry.mockRejectedValueOnce({ code: "ERR_NETWORK", request: {} });
    mount("/presales/inquiries/new", { referenceOffer: validOffer, part: partA });
    await screen.findByText("已引用报价 #70 · 本行不再派单");
    await waitFor(() => expect(mocks.presales.library).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "提交" }));
    await screen.findByText("未收到服务器响应");
    fireEvent.click(screen.getByRole("button", { name: "提交" }));
    await waitFor(() => expect(mocks.presales.submitInquiry).toHaveBeenCalledTimes(2));
    expect(mocks.presales.createInquiry).toHaveBeenCalledTimes(1);
    expect(mocks.presales.updateInquiry).toHaveBeenCalledWith(9, expect.objectContaining({ expected_version: 1 }));
    expect(mocks.presales.updateInquiry.mock.calls[0][1].lines[0].line_key).toBe("saved-0");
  });

  it("快速从 A 切到 B 后忽略 A 的迟到响应，保存只能使用 B 的身份和版本", async () => {
    let resolveA!: (value: Inquiry) => void;
    const dataB = inquiry({ id: 2, version: 8, inquiry_no: "XJ002" });
    dataB.lines = [{ ...dataB.lines![0], id: 20, inquiry_id: 2, line_key: "stable-b", pn_raw: "PN-B", part_id: 11 }];
    mocks.presales.getInquiry.mockImplementation((id) => id === 1 ? new Promise<Inquiry>((resolve) => { resolveA = resolve; }) : Promise.resolve(dataB));
    function Switch() { const navigate = useNavigate(); return <button onClick={() => navigate("/presales/inquiries/2/edit")}>切到 B</button>; }
    render(<MemoryRouter initialEntries={["/presales/inquiries/1/edit"]}>
      <Switch /><Routes><Route path="/presales/inquiries/:id/edit" element={<InquiryEditorPage />} /></Routes>
    </MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "切到 B" }));
    await openLine("PN-B");
    await screen.findByRole("spinbutton", { name: "PN-B 数量" });
    await act(async () => { resolveA(inquiry()); });
    expect(screen.queryByRole("spinbutton", { name: "PN-A 数量" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
    await waitFor(() => expect(mocks.presales.updateInquiry).toHaveBeenCalledWith(2, expect.objectContaining({ expected_version: 8 })));
  });
});

it("已保存引用按钉住快照判断失效，被取代的引用不因当前报价有效而恢复", async () => {
  const data = inquiry();
  data.lines![0] = { ...data.lines![0], ref_offer_id: 70, ref_offer_revision_no: 1,
    ref_offer: { ...validOffer, expired: true } };
  mocks.presales.getInquiry.mockResolvedValue(data);
  mount("/presales/inquiries/1/edit");
    await openLine("PN-A");
  expect(await screen.findByText("引用已失效")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
  expect(await screen.findByText("引用报价已失效，请重新选择有效报价或取消引用")).toBeInTheDocument();
  expect(mocks.presales.updateInquiry).not.toHaveBeenCalled();
});

it("从报价库展开报价带入的新引用可在 more 中校验并建单", async () => {
  mocks.presales.library.mockResolvedValue({ 10: [{ ...validOffer, id: 71, more: [validOffer] }] });
  mount("/presales/inquiries/new", { referenceOffer: validOffer, part: partA });
  await screen.findByText("已引用报价 #70 · 本行不再派单");
  await waitFor(() => expect(screen.queryByText("引用已失效")).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "提交" }));
  await waitFor(() => expect(mocks.presales.createInquiry).toHaveBeenCalledWith(expect.objectContaining({
    lines: [expect.objectContaining({ ref_offer_id: 70 })],
  })));
});

it("多行通过独立 Drawer 编辑，批量粘贴去重且不丢行", async () => {
  mount();
  expect(screen.queryByRole("textbox", { name: "客户要求" })).not.toBeInTheDocument();
  await addPart("PN-A"); await addPart("PN-B"); await closeLine();
  expect(screen.queryByLabelText("PN-A 期望交期")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "批量粘贴 PN" }));
  fireEvent.change(screen.getByRole("textbox", { name: "批量 PN" }), { target: { value: "RAW-A\nRAW-B;RAW-A;PN-A" } });
  fireEvent.click(screen.getByRole("button", { name: "按原文加入" }));
  await openLine("RAW-A");
  expect(screen.getByRole("textbox", { name: "RAW-A 描述" })).toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "RAW-A 描述" }), { target: { value: "新增规格" } });
  await openLine("RAW-B");
  expect(screen.getByRole("textbox", { name: "RAW-B 描述" })).toBeInTheDocument();
  await closeLine();
  fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
  await waitFor(() => expect(mocks.presales.createInquiry).toHaveBeenCalled());
  expect(mocks.presales.createInquiry.mock.calls[0][0].lines.map((line: { pn_raw: string }) => line.pn_raw)).toEqual(["PN-A", "PN-B", "RAW-A", "RAW-B"]);
  expect(mocks.presales.createInquiry.mock.calls[0][0].lines[2].description).toBe("新增规格");
});

it("待建档行的描述在关闭 Drawer 和继续加入 PN 后保留", async () => {
  mount();
  fireEvent.change(screen.getByRole("textbox", { name: "搜索并批量加入 PN" }), { target: { value: "RAW-A" } });
  fireEvent.click(await screen.findByRole("button", { name: /按原文添加「RAW-A」/ }));
  await openLine("RAW-A");
  fireEvent.change(screen.getByRole("textbox", { name: "RAW-A 描述" }), { target: { value: "已补齐规格说明" } });
  await addPart("PN-A"); await addPart("PN-B");
  await openLine("RAW-A");
  expect(screen.getByRole("textbox", { name: "RAW-A 描述" })).toHaveValue("已补齐规格说明");
});
