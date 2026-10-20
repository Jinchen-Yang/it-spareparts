import { act, cleanup, fireEvent, render, screen, waitFor } from "../../../../test/antdRender";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ search: vi.fn(), categories: vi.fn() }));
vi.mock("../../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual<typeof import("../../../../api/maintenanceOperations")>("../../../../api/maintenanceOperations"),
  searchSiteIssues: mocks.search,
  listMaintenancePartCategories: mocks.categories,
}));
vi.mock("../../../../components/maintenance/WorkbookRoundTrip", () => ({ default: () => <button>Excel 批量维护</button> }));
vi.mock("../../../../components/maintenance/SiteIssueWorkflowPanel", () => ({ default: () => <div>领用工作台内容</div> }));
vi.mock("../ReturnReceiptsSection", () => ({ default: () => <div>返还台账内容</div> }));
import SiteReturnTab from "../SiteReturnTab";
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  mocks.categories.mockResolvedValue({ data: { categories: [{ major: "硬盘", minors: ["SAS"] }] } });
});
afterEach(cleanup);
describe("领用行要求以服务端快照为准", () => {
  it("整单作废时间按北京时间跨午夜换日", async () => {
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [{ issue_id: "voided", issue_no: "VOID-1", workflow_status: "void", voided_at: "2026-10-01T16:05:00Z", lines: [] }] } });
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn()} registerRefresh={vi.fn()} />);
    expect(await screen.findByText("作废于 2026-10-02 00:05")).toBeInTheDocument();
  });
  it("第3页切换品类后回到第1页，父级读回仍保留当前可见页", async () => {
    mocks.search.mockImplementation(async (input) => ({ data: { total: 1, rows: [{
      issue_id: "i1", issue_no: "ISSUE-1", workflow_status: "confirmed", lines: Array.from({ length: 40 }, (_, index) => ({
        issue_line_id: `l${index}`, pn: `${input.category_major ? "NEW" : "OLD"}-PN-${index}`, quantity: "1", no_return: null,
      })),
    }] } }));
    let refresh!: () => Promise<boolean>;
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn()}
      registerRefresh={(_key, reader) => { if (reader) refresh = reader; }} />);
    await screen.findByText("OLD-PN-0");
    fireEvent.click(screen.getByTitle("3"));
    expect(await screen.findByText("OLD-PN-20")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "领用一级品类" }));
    fireEvent.click(await screen.findByText("硬盘", { selector: ".ant-select-item-option-content" }));
    expect(await screen.findByText("NEW-PN-0")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("3"));
    await screen.findByText("NEW-PN-20");
    await act(async () => { await refresh(); });
    expect(screen.getByText("NEW-PN-20")).toBeInTheDocument();
    expect(screen.queryByText("NEW-PN-0")).not.toBeInTheDocument();
  });
  it("旧筛选请求迟到不覆盖新筛选，也不继续读取旧条件下一页", async () => {
    let resolveOld!: (value: unknown) => void;
    const old = new Promise((resolve) => { resolveOld = resolve; });
    mocks.search.mockImplementation((input) => input.category_major ? Promise.resolve({ data: { total: 1, rows: [{ issue_id: "new", issue_no: "NEW-FILTER", workflow_status: "confirmed", lines: [{ issue_line_id: "new-line", pn: "NEW-PN", quantity: "1", no_return: null }] }] } }) : old);
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn()} registerRefresh={vi.fn()} />);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "领用一级品类" }));
    fireEvent.click(await screen.findByText("硬盘", { selector: ".ant-select-item-option-content" }));
    await screen.findByText("NEW-PN");
    await act(async () => resolveOld({ data: { total: 2, rows: [{ issue_id: "old", issue_no: "OLD-FILTER", lines: [{ issue_line_id: "old-line", pn: "OLD-PN" }] }] } }));
    expect(screen.queryByText("OLD-PN")).not.toBeInTheDocument();
    expect(mocks.search).toHaveBeenCalledTimes(2);
  });
  it("按品类请求服务端并保留刷新条件，整单作废显示完整行数", async () => {
    localStorage.setItem("permissions", JSON.stringify({ action_maintenance_site_issue_manage: true, data_purchase_cost: true }));
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [{ issue_id: "i1", issue_no: "ISSUE-1", project_id: "p1", version: 1, total_line_count: 4, workflow_status: "confirmed", lines: [
      { issue_line_id: "l1", pn: "PN-1", quantity: "1", no_return: null },
    ] }] } });
    let refresh!: () => Promise<boolean>;
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn()}
      registerRefresh={(_key, reader) => { if (reader) refresh = reader; }} />);
    await screen.findByText("PN-1");
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "领用一级品类" }));
    fireEvent.click(await screen.findByText("硬盘", { exact: true, selector: ".ant-select-item-option-content" }));
    await waitFor(() => expect(mocks.search).toHaveBeenLastCalledWith(expect.objectContaining({ category_major: "硬盘", page: 1 })));
    await act(async () => { await refresh(); });
    expect(mocks.search).toHaveBeenLastCalledWith(expect.objectContaining({ category_major: "硬盘" }));
    fireEvent.click(screen.getByRole("button", { name: /^作\s*废$/ }));
    expect(await screen.findByText("作废领用单 ISSUE-1（共 4 行）")).toBeInTheDocument();
  });
  it("按返还操作、领用登记、领用明细和 Excel 批量维护的顺序展示", async () => {
    mocks.search.mockResolvedValue({ data: { total: 0, rows: [] } });
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn()} registerRefresh={vi.fn()} />);
    const returnSection = screen.getByRole("heading", { name: "返还登记与台账" });
    const issueSection = screen.getByRole("heading", { name: "领用登记与单据" });
    const detailsSection = screen.getByRole("heading", { name: "领用明细与批量维护" });
    const excel = screen.getByRole("button", { name: "Excel 批量维护" });
    expect(returnSection.compareDocumentPosition(issueSection) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(issueSection.compareDocumentPosition(detailsSection) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(detailsSection.compareDocumentPosition(excel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it.each([
    { no_return: true, status: "required", expected: "应返", absent: "免返" },
    { no_return: false, status: "exempt", expected: "免返", absent: "应返" },
    { no_return: false, status: "pending_category", expected: "待确认品类", absent: "应返" },
    { no_return: null, status: "required", expected: "按规则判断（应返）", absent: "按规则判断（待确认品类）" },
  ])("当前no_return=$no_return、快照$status时显示$expected", async ({ no_return, status, expected, absent }) => {
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [{ issue_id: "issue-1", issue_no: "ISSUE-1", issue_date: "2026-09-12", workflow_status: "confirmed", lines: [
      { issue_line_id: "line-1", pn: "PN-1", quantity: "1", no_return, delivery_line_id: "delivery-1", return_requirement: { requirement_status: status } },
    ] }] } });
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn()} registerRefresh={vi.fn()} />);
    expect(await screen.findByText(expected)).toBeInTheDocument();
    expect(screen.queryByText(absent)).not.toBeInTheDocument();
  });
});
