import { useCallback, useRef, useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PanelRefresh, RegisterPanelRefresh } from "../panelUtils";

const api = vi.hoisted(() => ({
  searchSiteIssues: vi.fn(), searchSiteIssueCandidates: vi.fn(),
  getReturnReceiptSummary: vi.fn(), searchReturnReceipts: vi.fn(),
  getReturnReceiptDemands: vi.fn(), createReturnReceipt: vi.fn(),
}));
vi.mock("../../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual<typeof import("../../../../api/maintenanceOperations")>("../../../../api/maintenanceOperations"),
  ...api,
}));
vi.mock("../../../../components/maintenance/WorkbookRoundTrip", () => ({
  default: ({ onAfterApply }: { onAfterApply: () => Promise<boolean> }) => {
    const [status, setStatus] = useState("未上传");
    return <><button onClick={async () => {
      setStatus("刷新中");
      setStatus(await onAfterApply() ? "刷新成功" : "刷新失败");
    }}>模拟工作簿上传完成</button><span>{status}</span></>;
  },
}));
vi.mock("../../../../components/PartPicker", () => ({
  default: ({ onChange }: { onChange: (id: number, part: unknown) => void }) =>
    <button onClick={() => onChange(1, { pn_std: "RETURN-PN" })}>选择测试返件</button>,
}));
vi.mock("../ReturnReceiptImport", () => ({ default: () => null }));
import SiteReturnTab from "../SiteReturnTab";

function Harness() {
  const readers = useRef(new Map<string, PanelRefresh>());
  const registerRefresh = useCallback<RegisterPanelRefresh>((key, refresh) => {
    if (refresh) readers.current.set(key, refresh);
    else readers.current.delete(key);
  }, []);
  const onChanged = useCallback(async () => {
    const results = await Promise.allSettled([...readers.current.values()].map((refresh) => refresh()));
    return results.every((result) => result.status === "fulfilled" && result.value);
  }, []);
  return <SiteReturnTab projectId="p1" exportBase="项目" canUpload onChanged={onChanged} registerRefresh={registerRefresh} />;
}

let revision = 0;
const issue = () => ({
  issue_id: "i1", issue_no: `ISSUE-${revision}`, project_id: "p1", issue_date: "2026-09-22",
  workflow_status: "confirmed", receiver: "接收人", issued_by: "登记人", site_location: "现场", version: 1,
  lines: [{ issue_line_id: "il1", pn: "PART-1", quantity: "2", no_return: false, cost_source: "manual" }],
});
const receipt = () => ({
  receipt_id: "r1", project_id: "p1", source: "manual", pn: `RETURN-${revision}`,
  qty: "1", line_status: "active", version: 1, created_by: "登记人",
});
const summary = () => ({ project_id: "p1", project_total_qty: String(revision + 1), unassigned_qty: "1", by_demand: [] });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; }

beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear(); revision = 0;
  localStorage.setItem("permissions", JSON.stringify({
    action_maintenance_site_issue_manage: true, data_purchase_cost: true,
    action_maintenance_bad_return_manage: true,
  }));
  api.searchSiteIssues.mockImplementation(async ({ page, page_size }) => ({ data: { rows: [issue()], total: 1, page, page_size } }));
  api.searchSiteIssueCandidates.mockResolvedValue({ data: { rows: [], total: 0, page: 1, adapter: { production_ready: true } } });
  api.getReturnReceiptSummary.mockImplementation(async () => ({ data: summary() }));
  api.searchReturnReceipts.mockImplementation(async () => ({ data: { items: [receipt()], total: 1 } }));
  api.getReturnReceiptDemands.mockResolvedValue({ data: { rows: [], total: 0 } });
  api.createReturnReceipt.mockImplementation(async () => { revision += 1; return { data: { ...receipt(), replayed: false } }; });
});
afterEach(cleanup);

describe("领用返还统一读回", () => {
  it("工作簿完成后同时更新返还台账、上方领用工作台及领用明细，等待全部读回才成功", async () => {
    render(<Harness />);
    await screen.findByText("RETURN-0");
    await screen.findAllByText("ISSUE-0");
    revision = 1;
    const pending = deferred<{ data: ReturnType<typeof summary> }>();
    api.getReturnReceiptSummary.mockReturnValueOnce(pending.promise);
    fireEvent.click(screen.getByRole("button", { name: "模拟工作簿上传完成" }));
    await screen.findAllByText("ISSUE-1");
    expect(screen.getByText("刷新中")).toBeInTheDocument();
    expect(screen.queryByText("刷新成功")).not.toBeInTheDocument();
    await act(async () => pending.resolve({ data: summary() }));
    await screen.findByText("刷新成功");
    expect(screen.getByText("RETURN-1")).toBeInTheDocument();
    expect(api.searchSiteIssues).toHaveBeenCalledTimes(4); // 工作台与明细各初载及读回一次
    expect(api.searchSiteIssueCandidates).toHaveBeenCalledTimes(2);
    expect(api.searchReturnReceipts).toHaveBeenCalledTimes(2);
  });

  it.each(["receipts", "workflow"])("%s 读回失败会向上传回失败并清除该区域旧记录", async (failed) => {
    render(<Harness />);
    await screen.findByText("RETURN-0");
    await screen.findAllByText("ISSUE-0");
    revision = 1;
    if (failed === "receipts") api.searchReturnReceipts.mockRejectedValueOnce(new Error("offline"));
    else api.searchSiteIssues.mockImplementation(async ({ page, page_size }) => {
      if (page_size === 20) throw new Error("offline");
      return { data: { rows: [issue()], total: 1, page, page_size } };
    });
    fireEvent.click(screen.getByRole("button", { name: "模拟工作簿上传完成" }));
    await screen.findByText("刷新失败");
    expect(screen.queryByText("刷新成功")).not.toBeInTheDocument();
    expect(screen.queryByText(failed === "receipts" ? "RETURN-0" : "ISSUE-0")).not.toBeInTheDocument();
    expect(screen.getByText(failed === "receipts" ? "返还记录加载失败，不代表没有记录" : "现场领用单加载失败")).toBeInTheDocument();
  });

  it("登记返还后由同一屏障刷新自身与兄弟区域，每个列表仅重读一次", async () => {
    render(<Harness />);
    await screen.findByText("RETURN-0");
    fireEvent.click(screen.getByRole("button", { name: "登记返还" }));
    fireEvent.click(screen.getByRole("button", { name: "选择测试返件" }));
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: /^登\s*记$/ }));
    await screen.findByText("RETURN-1");
    await screen.findAllByText("ISSUE-1");
    await waitFor(() => expect(api.createReturnReceipt).toHaveBeenCalledTimes(1));
    expect(api.searchReturnReceipts).toHaveBeenCalledTimes(2);
    expect(api.getReturnReceiptSummary).toHaveBeenCalledTimes(2);
    expect(api.searchSiteIssues).toHaveBeenCalledTimes(4);
    expect(api.searchSiteIssueCandidates).toHaveBeenCalledTimes(2);
  });

  it("没有现场领用管理权限时不把隐藏的候选请求纳入刷新", async () => {
    localStorage.setItem("permissions", JSON.stringify({ action_maintenance_bad_return_manage: true }));
    render(<Harness />);
    await screen.findByText("RETURN-0");
    fireEvent.click(screen.getByRole("button", { name: "模拟工作簿上传完成" }));
    await screen.findByText("刷新成功");
    expect(api.searchSiteIssueCandidates).not.toHaveBeenCalled();
    expect(api.searchSiteIssues).toHaveBeenCalledTimes(2);
  });

  it("统一刷新保留领用列表已搜索条件和加载更多的范围，不提交搜索框未执行的输入", async () => {
    api.searchSiteIssues.mockImplementation(async ({ page, page_size }) => ({
      data: { rows: [{ ...issue(), issue_id: `i${page}`, issue_no: `ISSUE-PAGE-${page}-V${revision}` }], total: page_size === 20 ? 2 : 1, page, page_size },
    }));
    render(<Harness />);
    await screen.findByText("RETURN-0");
    const search = screen.getByLabelText("搜索现场领用单");
    fireEvent.change(search, { target: { value: "已执行筛选" } });
    fireEvent.keyDown(search, { key: "Enter", code: "Enter", charCode: 13 });
    await waitFor(() => expect(api.searchSiteIssues).toHaveBeenCalledWith(expect.objectContaining({ q: "已执行筛选" })));
    fireEvent.click(screen.getByRole("button", { name: "加载更多领用单" }));
    await screen.findByText("ISSUE-PAGE-2-V0");
    fireEvent.change(search, { target: { value: "未提交的新输入" } });
    revision = 1;
    api.searchSiteIssues.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "模拟工作簿上传完成" }));
    await screen.findByText("刷新成功");
    expect(screen.getByText("ISSUE-PAGE-2-V1")).toBeInTheDocument();
    expect(search).toHaveValue("未提交的新输入");
    const workflowCalls = api.searchSiteIssues.mock.calls.map(([params]) => params).filter((params) => params.page_size === 20);
    expect(workflowCalls).toEqual([
      expect.objectContaining({ q: "已执行筛选", page: 1 }),
      expect.objectContaining({ q: "已执行筛选", page: 2 }),
    ]);
  });

  it("卸载时移除所有子区读回函数，避免旧项目残留", async () => {
    const readers = new Map<string, PanelRefresh>();
    const registerRefresh: RegisterPanelRefresh = (key, refresh) => {
      if (refresh) readers.set(key, refresh);
      else readers.delete(key);
    };
    const { unmount } = render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload onChanged={vi.fn()} registerRefresh={registerRefresh} />);
    await screen.findByText("RETURN-0");
    expect([...readers.keys()].sort()).toEqual(["return-receipts", "site", "site-workflow"]);
    unmount();
    expect(readers.size).toBe(0);
  });
});
