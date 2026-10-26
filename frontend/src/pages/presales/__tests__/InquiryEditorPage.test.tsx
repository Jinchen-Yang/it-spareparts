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
  valid_from: "2020-01-01T00:00:00Z", valid_until: "2099-01-01T00:00:00Z" };
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
  fireEvent.change(screen.getByRole("textbox", { name: "搜索并批量加入 PN" }), { target: { value: pn } });
  fireEvent.click(await screen.findByRole("option", { name: new RegExp(pn) }));
  await screen.findByRole("spinbutton", { name: `${pn} 数量` });
}
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); pathname = "";
  mocks.suggestParts.mockImplementation(async (q: string) => q === "PN-A" ? [partA] : q === "PN-B" ? [partB] : []);
  mocks.presales.library.mockResolvedValue({ 10: [validOffer], 11: [] });
  mocks.listWorkCandidates.mockResolvedValue({ items: [{ id: 2, display_name: "采购甲" }, { id: 3, display_name: "采购乙" }] });
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
  it("多选加入、直接引用、按原文待建档、M7 交期与多人指派组成正确提交体", async () => {
    mount();
    await addPart("PN-A");
    fireEvent.click(await screen.findByRole("button", { name: "直接引用" }));
    await addPart("PN-B");
    fireEvent.change(screen.getByRole("textbox", { name: "搜索并批量加入 PN" }), { target: { value: "RAW-NEW" } });
    fireEvent.click(await screen.findByRole("button", { name: /按原文添加「RAW-NEW」/ }));
    expect(screen.getByText("待建档", { exact: true })).toBeVisible();
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
        expect.objectContaining({ part_id: 10, qty: 4, require_exact_pn: true, ref_offer_id: 70, field_values: [
          { field_def_id: 31, value: "三年" }, { new_name: "期望交期", value: "2026-11-01" },
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
    data.lines![0].field_values = [{ field_def_id: 31, name: "质保要求", value: "一年", rule: privateRule },
      { field_def_id: 33, name: "期望交期", value: "2026-11-01", rule: includeRule }];
    mocks.getFieldTemplate.mockImplementation(async (_subject, level) => level === "line" ? [field] : [{ ...field, id: 32, name: "单头说明", level: "header" }]);
    mocks.listFieldDefinitions.mockImplementation(mocks.getFieldTemplate.getMockImplementation()!);
    mocks.presales.getInquiry.mockResolvedValue(data);
    mount("/presales/inquiries/1/edit");
    const warranty = await screen.findByRole("textbox", { name: "质保要求字段值" });
    expect(screen.getByRole("button", { name: "客户要求可见性：私密" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "PN-A 行备注可见性：不给谁看" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "PN-A 期望交期可见性：部分可见" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "质保要求可见性：私密" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "单头说明可见性：不给谁看" })).toBeInTheDocument();
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
    expect(body.lines[0].field_values).toEqual([{ field_def_id: 31, value: "三年" }, { field_def_id: 33, value: "2026-11-01" }]);
  });

  it("撤回后的单据保留 line_key，先保存最新版本再重新提交且不重置可见性", async () => {
    mocks.presales.getInquiry.mockResolvedValue(inquiry({ status: "revising", version: 5 }));
    mocks.presales.updateInquiry.mockResolvedValueOnce(inquiry({ status: "revising", version: 6 }));
    mount("/presales/inquiries/1/edit");
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

  it("409 提示后读取最新版本；服务端未返回的字段不显示也不覆盖", async () => {
    const hidden = inquiry({ version: 3 });
    delete hidden.customer_id; delete hidden.assignee_ids; delete hidden.notes;
    delete hidden.lines![0].notes;
    mocks.presales.getInquiry.mockResolvedValueOnce(hidden).mockResolvedValueOnce({ ...hidden, version: 4 });
    mocks.presales.updateInquiry.mockRejectedValueOnce({ response: { status: 409 } });
    mount("/presales/inquiries/1/edit");
    await screen.findByRole("spinbutton", { name: "PN-A 数量" });
    expect(screen.queryByRole("combobox", { name: "客户" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "客户要求" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
    await screen.findByText("已被他人修改/领取，已自动刷新");
    await waitFor(() => expect(mocks.presales.getInquiry).toHaveBeenCalledTimes(2));
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
    const input = await screen.findByRole("textbox", { name: "质保要求字段值" });
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
    await waitFor(() => expect(mocks.presales.updateInquiry).toHaveBeenCalled());
    expect(mocks.presales.updateInquiry.mock.calls[0][1].lines[0].field_values).toEqual([{ field_def_id: 31, value: null }]);
  });

  it("报价库带入引用后建单；提交失败保留草稿身份，重试不会重复创建", async () => {
    mocks.presales.submitInquiry.mockRejectedValueOnce(new Error("network"));
    mount("/presales/inquiries/new", { referenceOffer: validOffer, part: partA });
    await screen.findByText("已引用报价 #70 · 本行不再派单");
    await waitFor(() => expect(mocks.presales.library).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "提交" }));
    await screen.findByText("保存失败，请重试");
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
    await screen.findByRole("spinbutton", { name: "PN-B 数量" });
    await act(async () => { resolveA(inquiry()); });
    expect(screen.queryByRole("spinbutton", { name: "PN-A 数量" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "存草稿" }));
    await waitFor(() => expect(mocks.presales.updateInquiry).toHaveBeenCalledWith(2, expect.objectContaining({ expected_version: 8 })));
  });
});
