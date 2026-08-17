import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getCapabilities = vi.fn();
const searchCatalog = vi.fn();
const listApplications = vi.fn();
const getApplication = vi.fn();
const getProjects = vi.fn();
const getCartDraft = vi.fn();
const replaceCartDraft = vi.fn();
const submitCartDraft = vi.fn();
const applyRevision = vi.fn();

vi.mock("../../api/replenishment", () => ({
  getReplenishmentCapabilities: (...args: unknown[]) => getCapabilities(...args),
  searchReplenishmentCatalog: (...args: unknown[]) => searchCatalog(...args),
  listReplenishmentApplications: (...args: unknown[]) => listApplications(...args),
  getReplenishmentApplication: (...args: unknown[]) => getApplication(...args),
  getReplenishmentProjects: (...args: unknown[]) => getProjects(...args),
  getReplenishmentCartDraft: (...args: unknown[]) => getCartDraft(...args),
  replaceReplenishmentCartDraft: (...args: unknown[]) => replaceCartDraft(...args),
  submitReplenishmentCartDraft: (...args: unknown[]) => submitCartDraft(...args),
  deleteReplenishmentCartDraft: vi.fn(),
  applyReplenishmentRevision: (...args: unknown[]) => applyRevision(...args),
  downloadSystemScreeningWorkbook: vi.fn(),
}));

import ReplenishmentBetaPage from "../ReplenishmentBetaPage";

const capabilities = {
  enabled: true,
  beta: true,
  can_view_price: true,
  can_create: true,
  can_review: false,
  workflow_mode: "system_screening",
  stage: "screening_complete",
  stable_path: "/inventory",
  data_contract: "仅记录维保项目补库申请与提交时冻结的三查事实；不修改库存、不自动审批、不自动定价。",
};

const project = {
  project_id: "proj-a",
  project_code: "WX-2026-001",
  display_name: "测试维保项目A",
};

const noPoolPart = {
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
};

const submittedApplication = {
  application_id: "app-submitted",
  application_no: "BLK-20260817-ABC1234567",
  owner_username: "admin",
  owner_display_name: "管理员",
  salesperson_name_snapshot: null,
  is_legacy_project_unbound: false,
  project,
  status: "submitted",
  workflow_mode: "system_screening",
  stage: "screening_complete",
  version: 1,
  latest_version_no: 1,
  created_at: "2026-08-17T00:00:00Z",
  updated_at: "2026-08-17T01:00:00Z",
  versions: [{
    version_id: "version-1",
    version_no: 1,
    parent_version_id: null,
    status: "submitted",
    warehouse: null,
    request_note: null,
    content_digest: "1".repeat(64),
    submitted_by: "admin",
    submitted_at: "2026-08-17T01:00:00Z",
    lines: [{
      line_id: "line-1",
      request_line_id: "intent-1",
      source_line_id: null,
      line_no: 1,
      part_id: 7,
      pn_std: "NO-POOL-001",
      description: "没有池和价格仍完整展示",
      brand: null,
      unit: "件",
      quantity: 2,
      special_note: null,
      pool: { group_id: null, name: null, version: null },
      price_window: noPoolPart.price_window,
      purchase: null,
      sales: null,
      screening: {
        schema_version: 1,
        as_of: "2026-08-17",
        lookback_days: 180,
        checks: [{ name: "pool", passed: true }],
        anomaly_count: 0,
      },
      latest_sales: null,
      // 后端 Decimal 序列化可能为字符串——渲染必须兼容（回归用例）
      pool_floor_ex_tax: "1758.60",
      review: null,
    }],
    review: null,
  }],
};

beforeEach(() => {
  vi.clearAllMocks();
  getCapabilities.mockResolvedValue({ data: capabilities });
  getProjects.mockResolvedValue({ data: { items: [project] } });
  listApplications.mockResolvedValue({
    data: { items: [], total: 0, page: 1, page_size: 20 },
  });
  getApplication.mockReset();
  getCartDraft.mockResolvedValue({ data: { draft: null } });
  replaceCartDraft.mockResolvedValue({ data: { draft: { version: 1 } } });
  submitCartDraft.mockReset();
  searchCatalog.mockResolvedValue({
    data: { total: 1, page: 1, page_size: 20, items: [noPoolPart] },
  });
});

afterEach(() => cleanup());

describe("ReplenishmentBetaPage（原子提交流程）", () => {
  it("关闭服务端总闸时显示明确正式功能状态和维保主页返回入口", async () => {
    getCapabilities.mockResolvedValueOnce({ data: { ...capabilities, enabled: false } });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    expect(await screen.findByText(/当前未开放/)).toBeInTheDocument();
    expect(screen.queryByText(/Beta/)).toBeNull();
    expect(screen.getByRole("button", { name: /返回维保主页/ })).toBeInTheDocument();
    expect(searchCatalog).not.toHaveBeenCalled();
    expect(listApplications).not.toHaveBeenCalled();
    expect(getProjects).not.toHaveBeenCalled();
  });

  it("只有页面权限但没有价格数据权限时不请求目录和申请详情", async () => {
    getCapabilities.mockResolvedValueOnce({
      data: { ...capabilities, can_view_price: false, can_create: false },
    });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    expect(await screen.findByText(/没有半年采购\/销售价格事实的查看权限/)).toBeInTheDocument();
    expect(searchCatalog).not.toHaveBeenCalled();
    expect(listApplications).not.toHaveBeenCalled();
    expect(getProjects).not.toHaveBeenCalled();
  });

  it("没有可选维保项目时给出明确指引", async () => {
    getProjects.mockResolvedValueOnce({ data: { items: [] } });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    expect(await screen.findByText("当前账号没有可选的维保项目")).toBeInTheDocument();
  });

  it("以购物卡片展示无池无价 PN，按钮为加入申请", async () => {
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    expect(await screen.findByText("NO-POOL-001")).toBeInTheDocument();
    expect(screen.queryByText(/Beta/)).toBeNull();
    expect(screen.getByText("未加入互通池")).toBeInTheDocument();
    expect(screen.getAllByText("半年内无有效样本")).toHaveLength(2);
    expect(screen.getByRole("button", { name: /加入申请/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /提交补库申请/ })).toBeDisabled();
  });

  it("选择项目并加入 PN 后一次性原子提交，展示冻结结果", async () => {
    submitCartDraft.mockResolvedValue({ data: submittedApplication });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    // 选择项目（antd Select）
    fireEvent.mouseDown(await screen.findByRole("combobox"));
    fireEvent.click(await screen.findByText(/WX-2026-001/));
    expect(screen.getAllByText(/WX-2026-001/).length).toBeGreaterThan(0);

    // 加入 PN
    fireEvent.click(screen.getByRole("button", { name: /加入申请/ }));

    // 提交
    fireEvent.click(screen.getByRole("button", { name: /提交补库申请/ }));
    fireEvent.click(await screen.findByRole("button", { name: /确认提交/ }));

    await waitFor(() => {
      expect(replaceCartDraft).toHaveBeenCalled();
      expect(submitCartDraft).toHaveBeenCalledTimes(1);
    });
    const calls = replaceCartDraft.mock.calls;
    const [projectId, payload] = calls[calls.length - 1];
    expect(projectId).toBe("proj-a");
    expect(payload).toMatchObject({
      lines: [{ part_id: 7, quantity: 1, special_note: null }],
    });
    expect(submitCartDraft.mock.calls[0]).toEqual(["proj-a", 1]);

    // 结果展示：单号 + 项目 + 已提交 + 冻结证据
    expect(await screen.findByText("BLK-20260817-ABC1234567")).toBeInTheDocument();
    expect(screen.getAllByText(/WX-2026-001/).length).toBeGreaterThan(0);
    expect(screen.getByText("已提交，系统三查与价格证据已冻结")).toBeInTheDocument();
    expect(screen.getByText(/三查通过/)).toBeInTheDocument();
    // 字符串金额必须归一为数字渲染，不能崩溃
    expect(screen.getByText(/池内最低价参考 ¥1758.60/)).toBeInTheDocument();
  });

  it("历史申请展示项目归属并可切换查看详情", async () => {
    listApplications.mockResolvedValueOnce({
      data: {
        total: 1,
        page: 1,
        page_size: 20,
        items: [{
          application_id: submittedApplication.application_id,
          application_no: submittedApplication.application_no,
          owner_display_name: "管理员",
          project,
          status: "submitted",
          workflow_mode: "system_screening",
          stage: "screening_complete",
          version: 1,
          latest_version_no: 1,
          updated_at: "2026-08-17T01:00:00Z",
        }],
      },
    });
    getApplication.mockResolvedValue({ data: submittedApplication });

    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    expect(await screen.findByText("BLK-20260817-ABC1234567")).toBeInTheDocument();
    expect(screen.getByText(/WX-2026-001 · 测试维保项目A/)).toBeInTheDocument();
  });

  it("打回后「退回编辑」：预填原行（标红+推荐），可删减后全量重新提交（#10）", async () => {
    const rejectedApp = {
      application_id: "app-rejected",
      application_no: "BLK-20260818-REJECT001",
      owner_username: "admin",
      owner_display_name: "管理员",
      salesperson_name_snapshot: null,
      is_legacy_project_unbound: false,
      project,
      status: "needs_revision",
      workflow_mode: "system_screening",
      stage: "needs_revision",
      version: 2,
      latest_version_no: 1,
      created_at: "2026-08-18T00:00:00Z",
      updated_at: "2026-08-18T01:00:00Z",
      versions: [{
        version_id: "version-1",
        version_no: 1,
        parent_version_id: null,
        status: "submitted",
        warehouse: null,
        request_note: "原备注",
        content_digest: "1".repeat(64),
        submitted_by: "admin",
        submitted_at: "2026-08-18T01:00:00Z",
        lines: [{
          line_id: "line-1",
          request_line_id: "req-1",
          source_line_id: null,
          line_no: 1,
          part_id: 10467,
          pn_std: "COLD-PN-001",
          description: "冷门备件",
          brand: null,
          unit: "件",
          quantity: 1,
          special_note: null,
          pool: { group_id: null, name: null, version: null },
          price_window: { date_from: "2026-02-12", date_to: "2026-08-10", days: 180, basis: "未税数量加权" },
          purchase: null,
          sales: null,
          screening: {
            schema_version: 2,
            as_of: "2026-08-18",
            lookback_days: 182,
            checks: [{ key: "pool_membership", passed: false, detail: { in_pool: false } }],
            anomaly_count: 1,
            auto_review: { decision: "rejected", reason_code: "no_purchase_or_sales_in_182_days" },
            recommendations: [{
              part_id: 7671, pn_std: "POOL-PN-001", pool_name: "测试池",
            }],
          },
          latest_sales: null,
          pool_floor_ex_tax: null,
          review: { decision: "rejected", reason: "no_purchase_or_sales_in_182_days" },
        }],
        review: null,
      }],
    };
    applyRevision.mockResolvedValue({ data: { ...submittedApplication, application_id: "app-rejected", application_no: "BLK-20260818-REJECT001" } });
    listApplications.mockResolvedValueOnce({
      data: {
        total: 1, page: 1, page_size: 20,
        items: [{
          application_id: "app-rejected",
          application_no: "BLK-20260818-REJECT001",
          owner_display_name: "管理员",
          project,
          status: "needs_revision",
          workflow_mode: "system_screening",
          stage: "needs_revision",
          version: 2,
          latest_version_no: 1,
          updated_at: "2026-08-18T01:00:00Z",
        }],
      },
    });
    getApplication.mockResolvedValue({ data: rejectedApp });
    getCartDraft.mockResolvedValue({ data: { draft: null } });

    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    // 打回申请详情出现「退回编辑」
    expect(await screen.findByText("BLK-20260818-REJECT001")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /退回编辑/ }));

    // 编辑态：标题变为编辑被打回申请，打回行标红 + 推荐替换按钮
    expect(await screen.findByText("编辑被打回申请：BLK-20260818-REJECT001")).toBeInTheDocument();
    expect(screen.getByText(/被打回：no_purchase_or_sales_in_182_days/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /替换为 POOL-PN-001/ })).toBeInTheDocument();

    // 用推荐替换打回行，然后重新提交 → applyRevision 带完整 lines
    fireEvent.click(screen.getByRole("button", { name: /替换为 POOL-PN-001/ }));
    fireEvent.click(screen.getByRole("button", { name: /提交补库申请/ }));
    fireEvent.click(await screen.findByRole("button", { name: /确认提交/ }));

    await waitFor(() => {
      expect(applyRevision).toHaveBeenCalledTimes(1);
    });
    const [applicationId, payload] = applyRevision.mock.calls[0];
    expect(applicationId).toBe("app-rejected");
    expect(payload).toMatchObject({
      expected_application_version: 2,
      lines: [{ part_id: 7671, quantity: 1, special_note: null }],
    });
    expect(payload.client_request_id).toMatch(/^.{8,128}$/);
  });
});
