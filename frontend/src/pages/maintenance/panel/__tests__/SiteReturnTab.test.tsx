import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ search: vi.fn() }));
vi.mock("../../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual<typeof import("../../../../api/maintenanceOperations")>("../../../../api/maintenanceOperations"),
  searchSiteIssues: mocks.search,
}));
vi.mock("../../../../components/maintenance/WorkbookRoundTrip", () => ({ default: () => null }));
vi.mock("../ReturnReceiptsSection", () => ({ default: () => null }));
import SiteReturnTab from "../SiteReturnTab";
beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); });
afterEach(cleanup);
describe("领用行要求以服务端快照为准", () => {
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
