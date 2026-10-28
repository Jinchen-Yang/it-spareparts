import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { Dayjs } from "dayjs";
import { cleanupAntd, render } from "../../../test/antdRender";
import type { Inquiry } from "../../../api/presales";
import type { WorkbookPreviewResult, WorkbookSchema } from "../../../api/workbooks";

const mocks = vi.hoisted(() => ({
  listInquiries: vi.fn(), getInquiry: vi.fn(), listWorkCandidates: vi.fn(), purchaseOptions: vi.fn(),
  getSalesSummarySchema: vi.fn(), exportSalesSummary: vi.fn(), getInquirySchema: vi.fn(),
  exportInquiryTemplate: vi.fn(), previewInquiry: vi.fn(), applyWorkbook: vi.fn(),
}));
vi.mock("../../../api/presales", () => ({ presales: { listInquiries: mocks.listInquiries, getInquiry: mocks.getInquiry } }));
vi.mock("../../../api/workItems", () => ({ listWorkCandidates: mocks.listWorkCandidates }));
vi.mock("../../../api/purchaseAnalytics", () => ({ fetchPurchaseAnalyticsOptions: mocks.purchaseOptions }));
vi.mock("../../../api/presalesWorkbooks", () => ({ presalesWorkbooks: mocks }));
vi.mock("../../../api/workbooks", async (original) => ({ ...await original<typeof import("../../../api/workbooks")>(), applyWorkbook: mocks.applyWorkbook }));
vi.mock("../../../nav", () => ({ readPermissionMap: () => ({ action_presales_inquire: true }) }));
vi.mock("antd", async (original) => {
  const antd = await original<typeof import("antd")>();
  const dayjs = (await import("dayjs")).default;
  // 只替换日期控件本身，验证页面如何把日期传给服务端。
  return { ...antd, DatePicker: { ...antd.DatePicker,
    RangePicker: ({ onChange }: { onChange?: (value: [Dayjs, Dayjs]) => void }) =>
      <button type="button" onClick={() => onChange?.([dayjs("2026-01-02"), dayjs("2026-01-04")])}>设置日期范围</button>,
  } };
});
import InquiryListPage from "../InquiryListPage";

const inquiry: Inquiry = { id: 1, inquiry_no: "XJ-T18", kind: "sales_inquiry", status: "draft", version: 1, revision: 1,
  is_owner: true, customer_id: 5, customer_name: "售前接口客户", line_count: 1,
  progress: { quoted_lines: 0, line_count: 1 },
  updated_at: "2026-01-03T10:00:00Z", created_at: "2026-01-01T00:00:00Z" };
const schema: WorkbookSchema = { name: "presales_sales_summary", version: 1, instructions: [], sheets: [{
  key: "summary", title: "销售汇总", allow_insert: false, allow_delete: false, custom_columns: false,
  columns: [{ key: "pn", header: "PN", type: "code", edit: "locked", hint: "", mandatory: true },
    { key: "qty", header: "数量", type: "int", edit: "reference", hint: "" },
    { key: "description", header: "描述", type: "text", edit: "reference", hint: "" }],
}] };
const preview: WorkbookPreviewResult = { operation_id: 18, plan_digest: "t18", replayed: false,
  plan: { rows: [{ key: "new:2", sheet: "lines", row_number: 2, entity_id: null, status: "added", changes: [], values: { pn: "0231" }, errors: [], warnings: [] }],
    counts: { added: 1, modified: 0, unchanged: 0, deleted: 0, conflict: 0 }, errors: [], warnings: [] } };

function mount() {
  return render(<MemoryRouter initialEntries={["/presales/inquiries"]}><Routes>
    <Route path="/presales/inquiries" element={<InquiryListPage />} />
    <Route path="/presales/inquiries/:id/edit" element={<p>Excel 草稿编辑页</p>} />
  </Routes></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  mocks.listInquiries.mockResolvedValue({ items: [inquiry], total: 1, page: 1, page_size: 20 });
  mocks.getInquiry.mockResolvedValue({ ...inquiry, lines: [], work_items: [] });
  mocks.listWorkCandidates.mockResolvedValue({ items: [] });
  mocks.getSalesSummarySchema.mockResolvedValue(schema);
  mocks.exportSalesSummary.mockResolvedValue(undefined);
  mocks.getInquirySchema.mockResolvedValue({ ...schema, name: "presales_inquiry_lines" });
  mocks.exportInquiryTemplate.mockResolvedValue(undefined);
  mocks.previewInquiry.mockResolvedValue(preview);
  mocks.applyWorkbook.mockResolvedValue({ operation_id: 18, replayed: false, response: { inquiry_id: 19 } });
});
afterEach(async () => { await cleanupAntd(); vi.useRealTimers(); });

it("客户名直接显示售前列表字段，不请求采购分析选项", async () => {
  mount();
  expect(await screen.findByText("售前接口客户")).toBeVisible();
  expect(mocks.purchaseOptions).not.toHaveBeenCalled();
});

it("清除排序后仍可连续翻页，只有排序实际变化才回到首页", async () => {
  mocks.listInquiries.mockResolvedValue({ items: [inquiry], total: 61, page: 1, page_size: 20 });
  mount();
  await screen.findByText("XJ-T18");
  // 默认降序的下一次点击即清除排序。
  const header = screen.getByRole("columnheader", { name: /最近动态/ });
  fireEvent.click(header);
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ sort: "updated_at", dir: "desc", page: 1 })));
  fireEvent.click(screen.getByTitle("2"));
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 })));
  fireEvent.click(screen.getByTitle("3"));
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ page: 3 })));
  fireEvent.click(screen.getByRole("columnheader", { name: /行数/ }));
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ sort: "line_count", page: 1 })));
});

it("搜索等待最后一次输入300ms后传给服务端，返回行仍由服务端决定", async () => {
  vi.useFakeTimers();
  const view = mount();
  try {
    await act(async () => {});
    mocks.listInquiries.mockClear();
    const input = screen.getByRole("textbox", { name: "搜索单号、客户或 PN" });
    fireEvent.change(input, { target: { value: " P" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    fireEvent.change(input, { target: { value: " PN-SEARCH " } });
    await act(async () => { await vi.advanceTimersByTimeAsync(299); });
    expect(mocks.listInquiries).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ q: "PN-SEARCH", page: 1 }));
    expect(screen.getByText("XJ-T18")).toBeVisible();
  } finally { view.unmount(); vi.useRealTimers(); }
});

it("最近动态日期范围传完整自然日参数给服务端", async () => {
  mount(); await screen.findByText("XJ-T18");
  fireEvent.click(screen.getByRole("button", { name: "设置日期范围" }));
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({
    date_from: "2026-01-02", date_to: "2026-01-04", page: 1,
  })));
});

it("仅选择本人销售询价后才可按选列合并下载", async () => {
  mocks.listInquiries.mockResolvedValue({ items: [inquiry, { ...inquiry, id: 2, inquiry_no: "XJ-ASSIGNED", is_owner: false }], total: 2 });
  mount(); await screen.findByText("XJ-T18");
  const download = screen.getByRole("button", { name: "合并下载" });
  expect(download).toBeDisabled();
  expect(screen.getByRole("checkbox", { name: "选择 XJ-ASSIGNED" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "选择 XJ-T18" }));
  expect(download).toBeEnabled();
  fireEvent.click(download);
  expect(await screen.findByRole("checkbox", { name: "PN（必带）" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "描述" }));
  fireEvent.click(screen.getByRole("button", { name: "下载 Excel" }));
  await waitFor(() => expect(mocks.exportSalesSummary).toHaveBeenCalledWith([1], { summary: ["pn", "qty"] }));
});

it("从Excel新建在弹窗下载模板、预览后应用，并跳转生成的草稿编辑页", async () => {
  mount(); await screen.findByText("XJ-T18");
  fireEvent.click(screen.getByRole("button", { name: "从 Excel 新建" }));
  await waitFor(() => expect(screen.getByRole("dialog", { name: "从 Excel 新建询价" })).toBeVisible());
  fireEvent.click(screen.getByRole("button", { name: "下载空模板" }));
  await screen.findByRole("checkbox", { name: "PN（必带）" });
  fireEvent.click(screen.getByRole("button", { name: "下载 Excel" }));
  await waitFor(() => expect(mocks.exportInquiryTemplate).toHaveBeenCalled());
  const file = new File(["xlsx"], "inquiry.xlsx");
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  fireEvent.change(input, { target: { files: [file] } });
  expect(await screen.findByText("变更预览")).toBeVisible();
  expect(mocks.previewInquiry).toHaveBeenCalledWith(file, undefined);
  expect(mocks.applyWorkbook).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认应用" }));
  expect(await screen.findByText("Excel 草稿编辑页")).toBeVisible();
  expect(mocks.applyWorkbook).toHaveBeenCalledWith(18, "t18", []);
});
