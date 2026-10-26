import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { cleanupAntd, render } from "../../../test/antdRender";
import type { Inquiry } from "../../../api/presales";

const mocks = vi.hoisted(() => ({ listInquiries: vi.fn(), getInquiry: vi.fn(), listWorkCandidates: vi.fn(), fetchPurchaseAnalyticsOptions: vi.fn() }));
vi.mock("../../../api/presales", () => ({ presales: { listInquiries: mocks.listInquiries, getInquiry: mocks.getInquiry } }));
vi.mock("../../../api/workItems", () => ({ listWorkCandidates: mocks.listWorkCandidates }));
vi.mock("../../../api/purchaseAnalytics", () => ({ fetchPurchaseAnalyticsOptions: mocks.fetchPurchaseAnalyticsOptions }));
vi.mock("../../../nav", () => ({ readPermissionMap: () => JSON.parse(localStorage.getItem("permissions") || "{}") }));
import InquiryListPage from "../InquiryListPage";

const row: Inquiry = { id: 1, inquiry_no: "XJ001", kind: "sales_inquiry", status: "submitted", version: 1, revision: 1,
  customer_id: 5, line_count: 2, progress: { quoted_lines: 2, line_count: 2 },
  updated_at: "2026-01-01T10:00:00Z", created_at: "2026-01-01T00:00:00Z" };
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  mocks.listInquiries.mockResolvedValue({ items: [row], total: 1, page: 1, page_size: 20 });
  mocks.getInquiry.mockResolvedValue({ ...row, work_items: [
    { id: 11, assignee_id: 2, claimed_by: null, status: "done", paused: false, version: 1 },
    { id: 12, assignee_id: 3, claimed_by: null, status: "pending", paused: false, version: 1 },
  ] });
  mocks.listWorkCandidates.mockResolvedValue({ items: [{ id: 2, display_name: "采购甲" }, { id: 3, display_name: "采购乙" }] });
  mocks.fetchPurchaseAnalyticsOptions.mockResolvedValue({ data: { customers: [{ customer_id: 5, customer: "客户甲" }], selected_customers: [] } });
});
afterEach(cleanupAntd);

it("列表按处理人回复显示 1/2 进度与标签，主管可切换全部", async () => {
  localStorage.setItem("permissions", JSON.stringify({ action_presales_supervise: true }));
  render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  expect(await screen.findByText("处理中「已回复 1/2」")).toBeVisible();
  expect(screen.getByText("采购甲 · 已回复")).toBeVisible();
  expect(screen.getByText("采购乙 · 待处理")).toBeVisible();
  expect(await screen.findByText("客户甲")).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "全部" }));
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ scope: "all" })));
});

it("普通用户只有我的询价；类型/状态使用服务端支持的筛选键，隐藏字段不补占位", async () => {
  const hidden = { ...row }; delete hidden.customer_id;
  mocks.listInquiries.mockResolvedValue({ items: [hidden], total: 1 });
  mocks.getInquiry.mockResolvedValue({ ...hidden, work_items: [{ id: 11, status: "done", paused: false, version: 1 }] });
  render(<MemoryRouter><InquiryListPage /></MemoryRouter>);
  await screen.findAllByText("已回复", { selector: ".ant-tag" });
  expect(screen.queryByRole("tab", { name: "全部" })).not.toBeInTheDocument();
  expect(screen.queryByText(/账号 #/)).not.toBeInTheDocument();
  expect(screen.queryByText(/客户 #/)).not.toBeInTheDocument();
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "询价类型" }));
  fireEvent.click(screen.getAllByText("主动报价").pop()!);
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ kind: "proactive_offer" })));
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "询价状态" }));
  fireEvent.click(screen.getByText("草稿", { selector: ".ant-select-item-option-content" }));
  await waitFor(() => expect(mocks.listInquiries).toHaveBeenLastCalledWith(expect.objectContaining({ status: "draft" })));
  expect(screen.getByText("日期筛选作用于当前页最近动态时间。")).toBeVisible();
});
