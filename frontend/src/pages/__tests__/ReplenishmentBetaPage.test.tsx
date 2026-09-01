import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getCapabilities = vi.fn();
const searchCatalog = vi.fn();
const listApplications = vi.fn();
const getApplication = vi.fn();
const getProjects = vi.fn();
const getCartDraft = vi.fn();
const replaceCartDraft = vi.fn();
const deleteCartDraft = vi.fn();
const submitCartDraft = vi.fn();
const applyRevision = vi.fn();
const downloadWorkbook = vi.fn();

vi.mock("../../api/replenishment", () => ({
  getReplenishmentCapabilities: (...args: unknown[]) => getCapabilities(...args),
  searchReplenishmentCatalog: (...args: unknown[]) => searchCatalog(...args),
  listReplenishmentApplications: (...args: unknown[]) => listApplications(...args),
  getReplenishmentApplication: (...args: unknown[]) => getApplication(...args),
  getReplenishmentProjects: (...args: unknown[]) => getProjects(...args),
  getReplenishmentCartDraft: (...args: unknown[]) => getCartDraft(...args),
  replaceReplenishmentCartDraft: (...args: unknown[]) => replaceCartDraft(...args),
  submitReplenishmentCartDraft: (...args: unknown[]) => submitCartDraft(...args),
  deleteReplenishmentCartDraft: (...args: unknown[]) => deleteCartDraft(...args),
  applyReplenishmentRevision: (...args: unknown[]) => applyRevision(...args),
  downloadSystemScreeningWorkbook: (...args: unknown[]) => downloadWorkbook(...args),
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

const needsRevisionApplication = {
  ...submittedApplication,
  application_id: "app-needs-revision-readonly",
  application_no: "BLK-20260831-READONLY",
  status: "needs_revision",
  stage: "needs_revision",
  version: 2,
  versions: [{
    ...submittedApplication.versions[0],
    lines: [{
      ...submittedApplication.versions[0].lines[0],
      review: { decision: "rejected", reason: "no_purchase_or_sales_in_182_days" },
      screening: {
        ...submittedApplication.versions[0].lines[0].screening,
        anomaly_count: 1,
        recommendations: [{
          part_id: 8,
          pn_std: "POOL-PN-READONLY",
          description: "只读推荐 PN",
          pool_group_id: 1,
          pool_name: "测试池",
          score: 0.9,
          match_reason: "同池相似",
        }],
      },
    }],
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
  getCartDraft.mockReset();
  getCartDraft.mockResolvedValue({ data: { draft: null } });
  replaceCartDraft.mockResolvedValue({
    data: {
      draft: {
        version: 1,
        client_request_id: "cart-submit-request-001",
      },
    },
  });
  submitCartDraft.mockReset();
  downloadWorkbook.mockReset();
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

  it("没有创建权限时仍可按项目查看价格、云端草稿和本人历史，写入口保持只读", async () => {
    getCapabilities.mockResolvedValueOnce({
      data: { ...capabilities, can_create: false },
    });
    getCartDraft.mockResolvedValueOnce({
      data: {
        draft: {
          draft_id: "draft-readonly-navigation",
          project_id: project.project_id,
          request_note: "可查看的云端草稿",
          client_request_id: "draft-readonly-navigation-request",
          version: 3,
          created_at: "2026-08-30T00:00:00Z",
          updated_at: "2026-08-30T01:00:00Z",
          lines: [{
            draft_line_id: "draft-line-readonly-navigation",
            line_no: 1,
            part_id: noPoolPart.part_id,
            pn_std: noPoolPart.pn_std,
            description: noPoolPart.description,
            brand: null,
            unit: noPoolPart.unit,
            quantity: 2,
            special_note: "只读行",
          }],
        },
      },
    });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    expect(await screen.findByText("NO-POOL-001")).toBeInTheDocument();
    expect(screen.getAllByText("半年内无有效样本")).toHaveLength(2);
    expect(screen.getByRole("button", { name: /申请记录/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /加入申请/ })).toBeDisabled();
    const projectSelect = screen.getByRole("combobox");
    expect(projectSelect).toBeEnabled();

    fireEvent.mouseDown(projectSelect);
    fireEvent.click(await screen.findByText(/WX-2026-001/));

    const requestNote = await screen.findByDisplayValue("可查看的云端草稿");
    expect(getCartDraft).toHaveBeenCalledWith(project.project_id);
    const cart = screen.getByText("新建维保补库申请").closest(".ant-card");
    expect(cart).not.toBeNull();
    const cartUi = within(cart as HTMLElement);
    expect(requestNote).toBeDisabled();
    expect(cartUi.getByDisplayValue("只读行")).toBeDisabled();
    expect(cartUi.getByRole("spinbutton")).toBeDisabled();
    expect(cartUi.getByRole("button", { name: /提交补库申请/ })).toBeDisabled();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 600));
    });
    expect(replaceCartDraft).not.toHaveBeenCalled();
    expect(submitCartDraft).not.toHaveBeenCalled();
    expect(applyRevision).not.toHaveBeenCalled();
  });

  it("已有草稿在创建权限收回后保持只读，篡改控件也不会保存或提交", async () => {
    const mutableCapabilities = { ...capabilities };
    getCapabilities.mockResolvedValueOnce({ data: mutableCapabilities });
    getCartDraft.mockResolvedValueOnce({
      data: {
        draft: {
          draft_id: "draft-readonly",
          project_id: project.project_id,
          request_note: "只读草稿备注",
          client_request_id: "draft-readonly-request",
          version: 4,
          created_at: "2026-08-30T00:00:00Z",
          updated_at: "2026-08-30T01:00:00Z",
          lines: [{
            draft_line_id: "draft-line-readonly",
            line_no: 1,
            part_id: noPoolPart.part_id,
            pn_std: noPoolPart.pn_std,
            description: noPoolPart.description,
            brand: null,
            unit: noPoolPart.unit,
            quantity: 2,
            special_note: "只读行备注",
          }],
        },
      },
    });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    fireEvent.mouseDown(await screen.findByRole("combobox"));
    fireEvent.click(await screen.findByText(/WX-2026-001/));
    const requestNote = await screen.findByDisplayValue("只读草稿备注");
    await waitFor(() => expect(getCartDraft).toHaveBeenCalledWith(project.project_id));
    replaceCartDraft.mockClear();
    deleteCartDraft.mockClear();

    mutableCapabilities.can_create = false;
    fireEvent.click(screen.getByRole("button", { name: /申请记录/ }));

    const cart = screen.getByText("新建维保补库申请").closest(".ant-card");
    expect(cart).not.toBeNull();
    const cartUi = within(cart as HTMLElement);
    const line = cartUi.getByText(noPoolPart.pn_std).closest(".replenishment-cart-line");
    expect(line).not.toBeNull();
    const lineUi = within(line as HTMLElement);
    const quantity = lineUi.getByRole("spinbutton");
    const lineNote = lineUi.getByPlaceholderText("特殊情况说明（选填）");
    const remove = lineUi.getByRole("button", { name: /delete/i });
    const submit = cartUi.getByRole("button", { name: /提交补库申请/ });

    expect(cartUi.getByRole("combobox")).toBeEnabled();
    expect(requestNote).toBeDisabled();
    expect(quantity).toBeDisabled();
    expect(lineNote).toBeDisabled();
    expect(remove).toBeDisabled();
    expect(submit).toBeDisabled();

    // UI 属性被人为移除时，自动保存与提交函数仍须 fail closed。
    requestNote.removeAttribute("disabled");
    fireEvent.change(requestNote, { target: { value: "不得保存的新备注" } });
    quantity.removeAttribute("disabled");
    fireEvent.change(quantity, { target: { value: "3" } });
    lineNote.removeAttribute("disabled");
    fireEvent.change(lineNote, { target: { value: "不得保存的新行备注" } });
    submit.removeAttribute("disabled");
    fireEvent.click(submit);
    const confirm = screen.queryByRole("button", { name: /确认提交/ });
    if (confirm) fireEvent.click(confirm);
    remove.removeAttribute("disabled");
    fireEvent.click(remove);

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 600));
    });
    expect(replaceCartDraft).not.toHaveBeenCalled();
    expect(deleteCartDraft).not.toHaveBeenCalled();
    expect(submitCartDraft).not.toHaveBeenCalled();
    expect(applyRevision).not.toHaveBeenCalled();
  });

  it("needs_revision 在没有创建权限时只可查看，退回编辑与导出入口均禁用", async () => {
    getCapabilities.mockResolvedValueOnce({
      data: { ...capabilities, can_create: false },
    });
    listApplications.mockResolvedValueOnce({
      data: {
        total: 1,
        page: 1,
        page_size: 20,
        items: [{
          application_id: needsRevisionApplication.application_id,
          application_no: needsRevisionApplication.application_no,
          owner_display_name: needsRevisionApplication.owner_display_name,
          project,
          status: "needs_revision",
          workflow_mode: "system_screening",
          stage: "needs_revision",
          version: 2,
          latest_version_no: 1,
          updated_at: needsRevisionApplication.updated_at,
        }],
      },
    });
    getApplication.mockResolvedValue({ data: needsRevisionApplication });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    fireEvent.click(await screen.findByRole("button", { name: /申请记录/ }));
    fireEvent.click(await screen.findByRole("button", { name: /查看详情/ }));
    const revise = await screen.findByRole("button", { name: /退回编辑/ });
    const exportButton = screen.getByRole("button", { name: /导出复核包/ });
    expect(revise).toBeDisabled();
    expect(exportButton).toBeDisabled();

    revise.removeAttribute("disabled");
    fireEvent.click(revise);
    expect(screen.queryByText(/编辑被打回申请/)).toBeNull();
    expect(downloadWorkbook).not.toHaveBeenCalled();
    expect(replaceCartDraft).not.toHaveBeenCalled();
    expect(submitCartDraft).not.toHaveBeenCalled();
    expect(applyRevision).not.toHaveBeenCalled();
  });

  it("申请项目已退出当前范围时历史仍可查看和导出，但不能退回编辑", async () => {
    getProjects.mockResolvedValueOnce({ data: { items: [] } });
    listApplications.mockResolvedValueOnce({
      data: {
        total: 1,
        page: 1,
        page_size: 20,
        items: [{
          application_id: needsRevisionApplication.application_id,
          application_no: needsRevisionApplication.application_no,
          owner_display_name: needsRevisionApplication.owner_display_name,
          project,
          status: "needs_revision",
          workflow_mode: "system_screening",
          stage: "needs_revision",
          version: 2,
          latest_version_no: 1,
          updated_at: needsRevisionApplication.updated_at,
        }],
      },
    });
    getApplication.mockResolvedValue({ data: needsRevisionApplication });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    fireEvent.click(await screen.findByRole("button", { name: /申请记录/ }));
    fireEvent.click(await screen.findByRole("button", { name: /查看详情/ }));

    expect(await screen.findByRole("button", { name: /退回编辑/ })).toBeDisabled();
    expect(screen.getByText("申请被自动审核打回——当前仅可查看历史")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /导出复核包/ })).toBeEnabled();
  });

  it("刷新会同步最新项目范围，撤权后的历史复提入口立即变为只读", async () => {
    getProjects
      .mockResolvedValueOnce({ data: { items: [project] } })
      .mockResolvedValueOnce({ data: { items: [] } });
    listApplications.mockResolvedValue({
      data: {
        total: 1,
        page: 1,
        page_size: 20,
        items: [{
          application_id: needsRevisionApplication.application_id,
          application_no: needsRevisionApplication.application_no,
          owner_display_name: needsRevisionApplication.owner_display_name,
          project,
          status: "needs_revision",
          workflow_mode: "system_screening",
          stage: "needs_revision",
          version: 2,
          latest_version_no: 1,
          updated_at: needsRevisionApplication.updated_at,
        }],
      },
    });
    getApplication.mockResolvedValue({ data: needsRevisionApplication });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    await waitFor(() => expect(getProjects).toHaveBeenCalledTimes(1));
    fireEvent.click(await screen.findByRole("button", { name: /刷新/ }));
    await waitFor(() => expect(getProjects).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("button", { name: /申请记录/ }));
    fireEvent.click(await screen.findByRole("button", { name: /查看详情/ }));

    expect(await screen.findByRole("button", { name: /退回编辑/ })).toBeDisabled();
    expect(screen.getByText("申请被自动审核打回——当前仅可查看历史")).toBeInTheDocument();
    expect(applyRevision).not.toHaveBeenCalled();
  });

  it("并发刷新乱序返回时只接受最后一次项目范围结果", async () => {
    let resolveOlder!: (value: { data: { items: (typeof project)[] } }) => void;
    let resolveNewer!: (value: { data: { items: (typeof project)[] } }) => void;
    const older = new Promise<{ data: { items: (typeof project)[] } }>((resolve) => {
      resolveOlder = resolve;
    });
    const newer = new Promise<{ data: { items: (typeof project)[] } }>((resolve) => {
      resolveNewer = resolve;
    });
    getProjects
      .mockResolvedValueOnce({ data: { items: [project] } })
      .mockReturnValueOnce(older)
      .mockReturnValueOnce(newer);
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    await waitFor(() => expect(getProjects).toHaveBeenCalledTimes(1));
    const refresh = await screen.findByRole("button", { name: /刷新/ });
    fireEvent.click(refresh);
    fireEvent.click(refresh);
    await waitFor(() => expect(getProjects).toHaveBeenCalledTimes(3));

    await act(async () => {
      resolveNewer({ data: { items: [] } });
      await newer;
    });
    expect(await screen.findByText("当前账号没有可选的维保项目")).toBeInTheDocument();

    await act(async () => {
      resolveOlder({ data: { items: [project] } });
      await older;
    });
    expect(screen.getByText("当前账号没有可选的维保项目")).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("没有创建权限时已提交详情的导出复核包入口禁用", async () => {
    getCapabilities.mockResolvedValueOnce({
      data: { ...capabilities, can_create: false },
    });
    listApplications.mockResolvedValueOnce({
      data: {
        total: 1,
        page: 1,
        page_size: 20,
        items: [{
          application_id: submittedApplication.application_id,
          application_no: submittedApplication.application_no,
          owner_display_name: submittedApplication.owner_display_name,
          project,
          status: "submitted",
          workflow_mode: "system_screening",
          stage: "screening_complete",
          version: 1,
          latest_version_no: 1,
          updated_at: submittedApplication.updated_at,
        }],
      },
    });
    getApplication.mockResolvedValue({ data: submittedApplication });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    fireEvent.click(await screen.findByRole("button", { name: /申请记录/ }));
    fireEvent.click(await screen.findByRole("button", { name: /查看详情/ }));

    expect(await screen.findByRole("button", { name: /导出复核包/ })).toBeDisabled();
    expect(downloadWorkbook).not.toHaveBeenCalled();
  });

  it("needs_revision 导出点击前创建权限被收回时，exportCurrent 不得发起下载", async () => {
    const mutableCapabilities = { ...capabilities };
    getCapabilities.mockResolvedValueOnce({ data: mutableCapabilities });
    listApplications.mockResolvedValueOnce({
      data: {
        total: 1,
        page: 1,
        page_size: 20,
        items: [{
          application_id: needsRevisionApplication.application_id,
          application_no: needsRevisionApplication.application_no,
          owner_display_name: needsRevisionApplication.owner_display_name,
          project,
          status: "needs_revision",
          workflow_mode: "system_screening",
          stage: "needs_revision",
          version: 2,
          latest_version_no: 1,
          updated_at: needsRevisionApplication.updated_at,
        }],
      },
    });
    getApplication.mockResolvedValue({ data: needsRevisionApplication });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    fireEvent.click(await screen.findByRole("button", { name: /申请记录/ }));
    fireEvent.click(await screen.findByRole("button", { name: /查看详情/ }));
    const exportButton = await screen.findByRole("button", { name: /导出复核包/ });
    expect(exportButton).toBeEnabled();

    // 模拟按钮渲染后权限被服务端收回；函数级 guard 必须独立于 disabled 属性。
    mutableCapabilities.can_create = false;
    fireEvent.click(exportButton);

    expect(downloadWorkbook).not.toHaveBeenCalled();
  });

  it("复提编辑期间权限被收回时，推荐替换和全部草稿控件立即只读", async () => {
    const mutableCapabilities = { ...capabilities };
    getCapabilities.mockResolvedValueOnce({ data: mutableCapabilities });
    listApplications.mockResolvedValueOnce({
      data: {
        total: 1,
        page: 1,
        page_size: 20,
        items: [{
          application_id: needsRevisionApplication.application_id,
          application_no: needsRevisionApplication.application_no,
          owner_display_name: needsRevisionApplication.owner_display_name,
          project,
          status: "needs_revision",
          workflow_mode: "system_screening",
          stage: "needs_revision",
          version: 2,
          latest_version_no: 1,
          updated_at: needsRevisionApplication.updated_at,
        }],
      },
    });
    getApplication.mockResolvedValue({ data: needsRevisionApplication });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    fireEvent.click(await screen.findByRole("button", { name: /申请记录/ }));
    fireEvent.click(await screen.findByRole("button", { name: /查看详情/ }));
    fireEvent.click(await screen.findByRole("button", { name: /退回编辑/ }));
    const recommendation = await screen.findByRole("button", {
      name: /替换为 POOL-PN-READONLY/,
    });

    mutableCapabilities.can_create = false;
    fireEvent.click(screen.getByRole("button", { name: /申请记录/ }));

    const cart = screen.getByText(/编辑被打回申请/).closest(".ant-card");
    expect(cart).not.toBeNull();
    const cartUi = within(cart as HTMLElement);
    expect(cartUi.getByRole("combobox")).toBeEnabled();
    expect(cartUi.getByPlaceholderText("整单备注（选填）")).toBeDisabled();
    expect(cartUi.getByRole("spinbutton")).toBeDisabled();
    expect(cartUi.getByPlaceholderText("特殊情况说明（选填）")).toBeDisabled();
    expect(cartUi.getByRole("button", { name: /delete/i })).toBeDisabled();
    expect(cartUi.getByRole("button", { name: /提交补库申请/ })).toBeDisabled();
    expect(recommendation).toBeDisabled();
    expect(replaceCartDraft).not.toHaveBeenCalled();
    expect(submitCartDraft).not.toHaveBeenCalled();
    expect(applyRevision).not.toHaveBeenCalled();
  });

  it("复提确认框打开后权限被收回时，执行函数不得调用 applyRevision", async () => {
    const mutableCapabilities = { ...capabilities };
    getCapabilities.mockResolvedValueOnce({ data: mutableCapabilities });
    listApplications.mockResolvedValueOnce({
      data: {
        total: 1,
        page: 1,
        page_size: 20,
        items: [{
          application_id: needsRevisionApplication.application_id,
          application_no: needsRevisionApplication.application_no,
          owner_display_name: needsRevisionApplication.owner_display_name,
          project,
          status: "needs_revision",
          workflow_mode: "system_screening",
          stage: "needs_revision",
          version: 2,
          latest_version_no: 1,
          updated_at: needsRevisionApplication.updated_at,
        }],
      },
    });
    getApplication.mockResolvedValue({ data: needsRevisionApplication });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    fireEvent.click(await screen.findByRole("button", { name: /申请记录/ }));
    fireEvent.click(await screen.findByRole("button", { name: /查看详情/ }));
    fireEvent.click(await screen.findByRole("button", { name: /退回编辑/ }));
    fireEvent.click(await screen.findByRole("button", { name: /提交补库申请/ }));
    const confirm = await screen.findByRole("button", { name: /确认提交/ });

    mutableCapabilities.can_create = false;
    fireEvent.click(confirm);

    expect(replaceCartDraft).not.toHaveBeenCalled();
    expect(submitCartDraft).not.toHaveBeenCalled();
    expect(applyRevision).not.toHaveBeenCalled();
  });

  it("没有可选维保项目时给出明确指引", async () => {
    getProjects.mockResolvedValueOnce({ data: { items: [] } });
    render(<MemoryRouter><ReplenishmentBetaPage /></MemoryRouter>);

    expect(await screen.findByText("当前账号没有可选的维保项目")).toBeInTheDocument();
    expect(screen.getByText(/负责人\/viewer 挂靠/)).toBeInTheDocument();
    expect(screen.getByText(/只看自己维保项目.*销售映射/)).toBeInTheDocument();
    expect(screen.getByText(/老板和管理员.*全范围/)).toBeInTheDocument();
    expect(screen.queryByText(/销售经理需要先/)).toBeNull();
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

  it("选择项目后原子提交；响应丢失重试复用云端草稿 key 并展示冻结结果", async () => {
    submitCartDraft
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValueOnce({ data: submittedApplication });
    listApplications.mockResolvedValue({
      data: {
        total: 1, page: 1, page_size: 20,
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

    // 选择项目（antd Select）
    fireEvent.mouseDown(await screen.findByRole("combobox"));
    fireEvent.click(await screen.findByText(/WX-2026-001/));
    expect(screen.getAllByText(/WX-2026-001/).length).toBeGreaterThan(0);

    // 加入 PN
    const addButton = screen.getByRole("button", { name: /加入申请/ });
    await waitFor(() => expect(addButton).toBeEnabled());
    fireEvent.click(addButton);

    // 提交
    fireEvent.click(screen.getByRole("button", { name: /提交补库申请/ }));
    fireEvent.click(await screen.findByRole("button", { name: /确认提交/ }));

    await waitFor(() => {
      expect(replaceCartDraft).toHaveBeenCalled();
      expect(submitCartDraft).toHaveBeenCalledTimes(1);
    });
    const replaceCountAfterFirstAttempt = replaceCartDraft.mock.calls.length;
    const submitButton = screen.getByRole("button", { name: /提交补库申请/ });
    await waitFor(() => expect(submitButton).toBeEnabled());
    fireEvent.click(submitButton);
    fireEvent.click(await screen.findByRole("button", { name: /确认提交/ }));
    await waitFor(() => expect(submitCartDraft).toHaveBeenCalledTimes(2));
    expect(replaceCartDraft).toHaveBeenCalledTimes(replaceCountAfterFirstAttempt);
    const calls = replaceCartDraft.mock.calls;
    const [projectId, payload] = calls[calls.length - 1];
    expect(projectId).toBe("proj-a");
    expect(payload).toMatchObject({
      lines: [{ part_id: 7, quantity: 1, special_note: null }],
    });
    expect(submitCartDraft.mock.calls[0]).toEqual([
      "proj-a",
      1,
      "cart-submit-request-001",
    ]);
    expect(submitCartDraft.mock.calls[1]).toEqual(submitCartDraft.mock.calls[0]);

    // 提交成功：打开「申请记录」Drawer，详情展示单号 + 项目 + 已提交 + 冻结证据
    fireEvent.click(screen.getByRole("button", { name: /申请记录/ }));
    fireEvent.click(await screen.findByRole("button", { name: /查看详情/ }));
    expect((await screen.findAllByText(/BLK-20260817-ABC1234567/)).length).toBeGreaterThan(0);
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

    // 打开「申请记录」Drawer：列表展示单号 + 项目归属
    fireEvent.click(await screen.findByRole("button", { name: /申请记录/ }));
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
    applyRevision
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValueOnce({ data: { ...submittedApplication, application_id: "app-rejected", application_no: "BLK-20260818-REJECT001" } });
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

    // 打开「申请记录」Drawer → 查看打回申请详情 → 出现「退回编辑」
    fireEvent.click(await screen.findByRole("button", { name: /申请记录/ }));
    fireEvent.click(await screen.findByRole("button", { name: /查看详情/ }));
    expect((await screen.findAllByText(/BLK-20260818-REJECT001/)).length).toBeGreaterThan(0);
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
    // 首次请求若服务端已成功、但客户端丢失响应，原样重试必须复用同一 key。
    const revisionSubmit = screen.getByRole("button", { name: /提交补库申请/ });
    await waitFor(() => expect(revisionSubmit).toBeEnabled());
    fireEvent.click(revisionSubmit);
    fireEvent.click(await screen.findByRole("button", { name: /确认提交/ }));
    await waitFor(() => {
      expect(applyRevision).toHaveBeenCalledTimes(2);
    });
    const [applicationId, payload] = applyRevision.mock.calls[0];
    const [, retryPayload] = applyRevision.mock.calls[1];
    expect(applicationId).toBe("app-rejected");
    expect(payload).toMatchObject({
      expected_application_version: 2,
      lines: [{ part_id: 7671, quantity: 1, special_note: null }],
    });
    expect(payload.client_request_id).toMatch(/^.{8,128}$/);
    expect(retryPayload.client_request_id).toBe(payload.client_request_id);
  });
});
