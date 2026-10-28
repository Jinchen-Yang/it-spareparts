import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import type { Inquiry } from "../../../api/presales";
import type { WorkItem } from "../../../api/workItems";
import type { WorkbookExportButtonProps } from "../../../components/workbook/WorkbookExportButton";
import type { WorkbookImportProps } from "../../../components/workbook/WorkbookImport";

const mocks = vi.hoisted(() => ({
  getInquiry: vi.fn(), library: vi.fn(), reference: vi.fn(), customers: vi.fn(), replyInquiry: vi.fn(),
  saveOffer: vi.fn(), publishInquiry: vi.fn(), listWorkItems: vi.fn(), listWorkCandidates: vi.fn(),
  getFieldTemplate: vi.fn(), listFieldDefinitions: vi.fn(), inbox: vi.fn(), importer: vi.fn(),
  getInquirySchema: vi.fn(), exportInquiryTemplate: vi.fn(), exportInquiry: vi.fn(),
  getQuoteSheetSchema: vi.fn(), exportQuoteSheet: vi.fn(), previewInquiry: vi.fn(), previewQuoteSheet: vi.fn(),
}));
vi.mock("../../../api/presales", () => ({ presales: mocks }));
vi.mock("../../../api/presalesWorkbooks", () => ({ presalesWorkbooks: mocks }));
vi.mock("../../../api/workItems", () => ({ listWorkItems: mocks.listWorkItems, listWorkCandidates: mocks.listWorkCandidates }));
vi.mock("../../../api/fields", () => ({ getFieldTemplate: mocks.getFieldTemplate, listFieldDefinitions: mocks.listFieldDefinitions }));
vi.mock("../../../components/workflow/useWorkBadge", () => ({ refreshWorkBadges: vi.fn() }));
vi.mock("../../../components/parts/LivePartSearch", () => ({ default: () => <span>型号检索</span> }));
vi.mock("../../../components/gallery/PartThumb", () => ({ default: () => <span>图片</span> }));
vi.mock("../../../components/gallery/GalleryPicker", () => ({ default: () => null }));
vi.mock("../../../components/pricing/PriceRefBadge", () => ({ default: () => <span>参考价</span> }));
vi.mock("../../../components/fields/CustomFieldsEditor", () => ({ default: () => <span>自定义字段</span> }));
vi.mock("../../../components/audience/AudiencePicker", () => ({
  PUBLIC_AUDIENCE: { mode: "public", user_ids: [], group_ids: [] }, default: () => <span>可见范围</span>,
}));
vi.mock("../../../components/audience/AudienceLock", () => ({ default: () => null }));
vi.mock("../QuoteGrid", () => ({ default: () => <span>采购行明细</span> }));
vi.mock("../../../components/workbook", () => ({
  WorkbookExportButton: (props: WorkbookExportButtonProps) => <button disabled={props.disabled} onClick={() => {
    void props.fetchSchema(props.schemaName); void props.onExport({ lines: ["mode", "unit_price"] });
  }}>{props.label || "导出工作簿"}</button>,
  WorkbookImport: (props: WorkbookImportProps) => {
    mocks.importer(props);
    return <div><span>{props.successMessage}</span>
      <button onClick={() => { void props.previewFile?.(new File(["test"], "往返.xlsx")); }}>预览测试文件</button>
      <button onClick={() => { void props.onApplied?.({ inquiry_id: 18 }); }}>确认工作簿草稿</button>
    </div>;
  },
}));
vi.mock("../../../components/workflow/WorkInbox", () => ({ default: (props: {
  refreshKey?: number; onSelectionChange?: (items: WorkItem[]) => void; isRowSelectable?: (item: WorkItem) => boolean;
}) => {
  mocks.inbox(props);
  const choices = [ownTask, { ...ownTask, id: 72, subject_id: 2 },
    { ...ownTask, id: 73, assignee_id: 8, allowed_actions: [] },
    { ...ownTask, id: 74, assignee_id: null, claimed_by: null, allowed_actions: ["claim"] as WorkItem["allowed_actions"] },
    { ...ownTask, id: 75, paused: true }];
  return <div><span>队列刷新 {props.refreshKey}</span>{choices.map((item, index) => <button key={item.id}
    disabled={!props.isRowSelectable?.(item)} onClick={() => props.onSelectionChange?.([choices[0], choices[1]])}>
    {["选择我的单1", "选择我的单2", "选择其他人待办", "选择未领公共池", "选择暂停待办"][index]}</button>)}</div>;
} }));

import InquiryEditorPage from "../InquiryEditorPage";
import QuoteWorkbenchPage from "../QuoteWorkbenchPage";
import { ProcessContent } from "../ProcessPage";
import WorkbookDialog from "../WorkbookDialog";

const ownTask: WorkItem = { id: 71, subject_type: "presales_inquiry", subject_id: 1, pool_key: "presales_inquiry",
  assignee_id: 7, claimed_by: null, status: "in_progress", version: 1, paused: false,
  created_at: "2026-10-25T00:00:00Z", updated_at: "2026-10-25T00:00:00Z", summary: {}, allowed_actions: ["start", "complete"] };
function inquiry(status: Inquiry["status"] = "draft"): Inquiry {
  return { id: 1, kind: "sales_inquiry", inquiry_no: "XJ-1", status, version: 1, revision: 1, created_by: 3,
    created_at: "2026-10-25T00:00:00Z", updated_at: "2026-10-25T00:00:00Z", line_count: 1,
    progress: { quoted_lines: 0, line_count: 1 },
    lines: [{ id: 11, inquiry_id: 1, line_key: "line-1", seq: 1, part_id: 1, pn_raw: "PN-1", description: "型号一",
      qty: "2", status: "active", version: 1 }], offers: [],
    work_items: [{ id: 71, assignee_id: 7, claimed_by: null, status: "in_progress", version: 1, paused: false }] };
}
function RouteProbe() { return <span data-testid="route">{useLocation().pathname}</span>; }
function editor(path = "/presales/inquiries/1/edit") {
  return render(<MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/presales/inquiries/new" element={<InquiryEditorPage />} />
    <Route path="/presales/inquiries/:id/edit" element={<InquiryEditorPage />} />
  </Routes><RouteProbe /></MemoryRouter>);
}
function process() {
  return render(<MemoryRouter initialEntries={[{ pathname: "/presales/tasks/1", state: { workItem: ownTask } }]}>
    <ProcessContent inquiryId={1} />
  </MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  mocks.getInquiry.mockResolvedValue(inquiry());
  mocks.library.mockResolvedValue({}); mocks.reference.mockResolvedValue({ offers: {} }); mocks.customers.mockResolvedValue([]);
  mocks.getFieldTemplate.mockResolvedValue([]); mocks.listFieldDefinitions.mockResolvedValue([]);
  mocks.listWorkCandidates.mockResolvedValue({ items: [] });
  mocks.listWorkItems.mockResolvedValue({ items: [ownTask], total: 1, page: 1, page_size: 100, can_supervise: false });
  mocks.getInquirySchema.mockResolvedValue({}); mocks.getQuoteSheetSchema.mockResolvedValue({});
  mocks.exportInquiryTemplate.mockResolvedValue(undefined); mocks.exportInquiry.mockResolvedValue(undefined);
  mocks.exportQuoteSheet.mockResolvedValue(undefined); mocks.previewInquiry.mockResolvedValue({}); mocks.previewQuoteSheet.mockResolvedValue({});
});
afterEach(cleanupAntd);

describe("T18 销售 Excel 页面接线", () => {
  it.each(["draft", "revising"] as const)("%s 状态提供本单下载与导入，应用后重新读取草稿", async (status) => {
    mocks.getInquiry.mockResolvedValue(inquiry(status));
    editor();
    fireEvent.click(await screen.findByRole("button", { name: "下载本单 Excel" }));
    expect(mocks.exportInquiry).toHaveBeenCalledWith(1, { lines: ["mode", "unit_price"] });
    fireEvent.click(screen.getByRole("button", { name: "上传 Excel" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "预览测试文件" }));
    expect(mocks.previewInquiry).toHaveBeenCalledWith(expect.any(File), 1);
    fireEvent.click(within(dialog).getByRole("button", { name: "确认工作簿草稿" }));
    await waitFor(() => expect(mocks.getInquiry).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mocks.replyInquiry).not.toHaveBeenCalled();
  });

  it.each(["submitted", "closed"] as const)("%s 状态不提供编辑 Excel", async (status) => {
    mocks.getInquiry.mockResolvedValue(inquiry(status));
    editor();
    await screen.findByRole("table", { name: "询价行" });
    expect(screen.queryByRole("button", { name: "下载本单 Excel" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "上传 Excel" })).not.toBeInTheDocument();
  });

  it("空模板下载不带单据，上传新建预览后按草稿编号跳转编辑页", async () => {
    editor("/presales/inquiries/new");
    fireEvent.click(screen.getByRole("button", { name: "下载本单 Excel" }));
    expect(mocks.exportInquiryTemplate).toHaveBeenCalledWith({ lines: ["mode", "unit_price"] });
    fireEvent.click(screen.getByRole("button", { name: "上传 Excel" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "下载空模板" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "预览测试文件" }));
    expect(mocks.previewInquiry).toHaveBeenCalledWith(expect.any(File), undefined);
    fireEvent.click(within(dialog).getByRole("button", { name: "确认工作簿草稿" }));
    await waitFor(() => expect(screen.getByTestId("route")).toHaveTextContent("/presales/inquiries/18/edit"));
  });
});

describe("T18 采购 Excel 页面接线", () => {
  it("单张报价表按当前单导出、按文件预览，应用只保存草稿并刷新", async () => {
    mocks.getInquiry.mockResolvedValue(inquiry("submitted"));
    process();
    fireEvent.click(await screen.findByRole("button", { name: "下载报价表" }));
    expect(mocks.exportQuoteSheet).toHaveBeenCalledWith([1], { lines: ["mode", "unit_price"] });
    fireEvent.click(screen.getByRole("button", { name: "上传报价表" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("已保存为草稿，请在页面确认后回复")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "预览测试文件" }));
    expect(mocks.previewQuoteSheet).toHaveBeenCalledWith(expect.any(File));
    expect(mocks.previewInquiry).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认工作簿草稿" }));
    await waitFor(() => expect(mocks.getInquiry).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("已保存为草稿，请在页面确认后回复")).toBeInTheDocument();
    expect(mocks.replyInquiry).not.toHaveBeenCalled();
    expect(mocks.publishInquiry).not.toHaveBeenCalled();
    expect(mocks.saveOffer).not.toHaveBeenCalled();
  });

  it("合并下载只接受可操作本人待办，合并导入应用刷新队列并保留草稿提示", async () => {
    mocks.getInquiry.mockResolvedValue(inquiry("submitted"));
    render(<MemoryRouter initialEntries={[{ pathname: "/presales/workbench", state: { workItem: ownTask } }]}>
      <QuoteWorkbenchPage />
    </MemoryRouter>);
    await screen.findByRole("button", { name: "合并下载" });
    expect(screen.getByRole("button", { name: "合并下载" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "选择其他人待办" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "选择未领公共池" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "选择暂停待办" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "选择我的单1" }));
    fireEvent.click(screen.getByRole("button", { name: "合并下载" }));
    expect(mocks.exportQuoteSheet).toHaveBeenCalledWith([1, 2], { lines: ["mode", "unit_price"] });
    fireEvent.click(screen.getByRole("button", { name: "上传合并表" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "预览测试文件" }));
    expect(mocks.previewQuoteSheet).toHaveBeenCalledWith(expect.any(File));
    fireEvent.click(within(dialog).getByRole("button", { name: "确认工作簿草稿" }));
    expect(await screen.findByText("已保存为草稿，请在页面确认后回复")).toBeInTheDocument();
    expect(screen.getByText("队列刷新 1")).toBeInTheDocument();
    expect(mocks.getInquiry).not.toHaveBeenCalled();
    expect(mocks.replyInquiry).not.toHaveBeenCalled();
    expect(mocks.publishInquiry).not.toHaveBeenCalled();
  });

  it("独立导入弹窗的采购模式不按页面单号改写文件来源", async () => {
    render(<WorkbookDialog mode="quote" inquiryId={9} onClose={vi.fn()} onApplied={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "预览测试文件" }));
    expect(mocks.previewQuoteSheet).toHaveBeenCalledWith(expect.any(File));
    expect(mocks.previewInquiry).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "下载空模板" })).not.toBeInTheDocument();
    expect(mocks.importer).toHaveBeenLastCalledWith(expect.objectContaining({ schemaName: "presales_quote_sheet",
      successMessage: "已保存为草稿，请在页面确认后回复" }));
  });
});
