import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getCapabilities = vi.fn();
const searchCatalog = vi.fn();
const listApplications = vi.fn();
const getApplication = vi.fn();

vi.mock("../../api/replenishment", () => ({
  getReplenishmentCapabilities: (...args: unknown[]) => getCapabilities(...args),
  searchReplenishmentCatalog: (...args: unknown[]) => searchCatalog(...args),
  listReplenishmentApplications: (...args: unknown[]) => listApplications(...args),
  getReplenishmentApplication: (...args: unknown[]) => getApplication(...args),
  createReplenishmentApplication: vi.fn(),
  updateReplenishmentDraft: vi.fn(),
  addReplenishmentLine: vi.fn(),
  updateReplenishmentLine: vi.fn(),
  removeReplenishmentLine: vi.fn(),
  submitReplenishmentApplication: vi.fn(),
  startReplenishmentRevision: vi.fn(),
  downloadManualReviewWorkbook: vi.fn(),
  downloadWbddSubsetWorkbook: vi.fn(),
}));

import ReplenishmentBetaPage from "../ReplenishmentBetaPage";

const capabilities = {
  enabled: true,
  beta: true,
  can_view_price: true,
  can_create: true,
  can_review: false,
  stable_path: "/inventory",
  data_contract: "仅记录补库申请，不修改库存；历史价格为未税聚合事实，不是自动定价。",
};

beforeEach(() => {
  vi.clearAllMocks();
  getCapabilities.mockResolvedValue({ data: capabilities });
  listApplications.mockResolvedValue({
    data: { items: [], total: 0, page: 1, page_size: 20 },
  });
  getApplication.mockReset();
  searchCatalog.mockResolvedValue({
    data: {
      total: 1,
      page: 1,
      page_size: 20,
      items: [{
        part_id: 7,
        pn_std: "NO-POOL-001",
        description: "没有池和价格仍完整展示",
        brand: null,
        unit: "件",
        needs_review: false,
        pool: { group_id: null, name: null, version: null },
        price_window: {
          date_from: "2026-02-12",
          date_to: "2026-08-10",
          days: 180,
          basis: "未税数量加权",
        },
        purchase: null,
        sales: null,
      }],
    },
  });
});

afterEach(() => cleanup());

describe("ReplenishmentBetaPage", () => {
  it("关闭服务端总闸时显示明确 Beta 状态和稳定版返回入口", async () => {
    getCapabilities.mockResolvedValueOnce({ data: { ...capabilities, enabled: false } });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    expect(await screen.findByText(/当前未开放/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /返回稳定版库存页/ })).toBeInTheDocument();
    expect(searchCatalog).not.toHaveBeenCalled();
    expect(listApplications).not.toHaveBeenCalled();
  });

  it("以购物卡片展示无池无价 PN，而不是隐藏商品", async () => {
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    expect(await screen.findByText("NO-POOL-001")).toBeInTheDocument();
    expect(screen.getByText("未加入互通池")).toBeInTheDocument();
    expect(screen.getAllByText("半年内无有效样本")).toHaveLength(2);
    expect(screen.getByText(/不会自动定价或改变库存/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /新建补库单/ })).toBeInTheDocument();
  });

  it("只有页面权限但没有价格数据权限时不请求目录和申请详情", async () => {
    getCapabilities.mockResolvedValueOnce({
      data: { ...capabilities, can_view_price: false, can_create: false },
    });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    expect(await screen.findByText(/没有半年采购\/销售价格事实的查看权限/)).toBeInTheDocument();
    expect(searchCatalog).not.toHaveBeenCalled();
    expect(listApplications).not.toHaveBeenCalled();
  });

  it("第二轮打回只展示当前版本结果，不累计上一轮打回数", async () => {
    const priceWindow = {
      date_from: "2026-02-12",
      date_to: "2026-08-10",
      days: 180,
      basis: "未税数量加权",
    };
    const line = (
      lineId: string,
      requestLineId: string,
      pn: string,
      reason: string,
      sourceLineId: string | null,
    ) => ({
      line_id: lineId,
      request_line_id: requestLineId,
      source_line_id: sourceLineId,
      line_no: 1,
      part_id: sourceLineId ? 2 : 1,
      pn_std: pn,
      description: null,
      brand: null,
      unit: "件",
      quantity: 1,
      special_note: null,
      pool: { group_id: null, name: null, version: null },
      price_window: priceWindow,
      purchase: null,
      sales: null,
      review: { decision: "rejected", reason },
    });
    const application = {
      application_id: "app-1",
      application_no: "BL202608100001",
      owner_username: "sales_manager",
      owner_display_name: "销售经理",
      salesperson_name_snapshot: "销售经理",
      status: "needs_revision",
      version: 8,
      latest_version_no: 2,
      created_at: "2026-08-10T00:00:00Z",
      updated_at: "2026-08-10T01:00:00Z",
      versions: [
        {
          version_id: "version-2",
          version_no: 2,
          parent_version_id: "version-1",
          status: "submitted",
          warehouse: "北京前置库",
          request_note: null,
          content_digest: "2".repeat(64),
          submitted_by: "sales_manager",
          submitted_at: "2026-08-10T01:00:00Z",
          lines: [line("line-2", "intent-1", "PN-NEW", "本轮原因", "line-1")],
          review: {
            review_id: "review-2",
            external_reference: null,
            summary_note: null,
            approved_count: 0,
            rejected_count: 1,
            reviewed_at: "2026-08-10T02:00:00Z",
          },
        },
        {
          version_id: "version-1",
          version_no: 1,
          parent_version_id: null,
          status: "submitted",
          warehouse: "北京前置库",
          request_note: null,
          content_digest: "1".repeat(64),
          submitted_by: "sales_manager",
          submitted_at: "2026-08-09T01:00:00Z",
          lines: [line("line-1", "intent-1", "PN-OLD", "上一轮原因", null)],
          review: {
            review_id: "review-1",
            external_reference: null,
            summary_note: null,
            approved_count: 0,
            rejected_count: 1,
            reviewed_at: "2026-08-09T02:00:00Z",
          },
        },
      ],
    };
    listApplications.mockResolvedValueOnce({
      data: {
        total: 1,
        page: 1,
        page_size: 20,
        items: [{
          application_id: application.application_id,
          application_no: application.application_no,
          owner_display_name: application.owner_display_name,
          status: application.status,
          version: application.version,
          latest_version_no: application.latest_version_no,
          updated_at: application.updated_at,
        }],
      },
    });
    getApplication.mockResolvedValue({ data: application });

    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    expect(await screen.findByText("审核打回 1 条")).toBeInTheDocument();
    expect(screen.getByText(/v2 · PN-NEW：本轮原因/)).toBeInTheDocument();
    expect(screen.queryByText(/v1 · PN-OLD：上一轮原因/)).not.toBeInTheDocument();
    expect(screen.getByText("版本留存（2）")).toBeInTheDocument();
  });
});
