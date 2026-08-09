import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  searchSiteIssueCandidates: vi.fn(),
  searchSiteIssues: vi.fn(),
  createSiteIssueDraft: vi.fn(),
  patchSiteIssue: vi.fn(),
  previewSiteIssue: vi.fn(),
  confirmSiteIssue: vi.fn(),
  voidSiteIssue: vi.fn(),
}));

vi.mock("../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceOperations")>(
    "../../../api/maintenanceOperations",
  );
  return { ...actual, ...api };
});

import SiteIssueWorkflowPanel from "../SiteIssueWorkflowPanel";

const adapter = {
  key: "synthetic_delivery_v1",
  state: "synthetic_ready",
  production_ready: false,
  detail: "真实适配器接入前不得用于生产确认",
};

const candidate = {
  delivery_line_id: "delivery-1",
  source_order_id: "order-1",
  source_line_id: "line-1",
  delivery_no: "WBDD-001",
  delivery_date: "2026-08-08",
  part_id: 1,
  pn: "PN-001",
  serial_number: "SN-001",
  delivered_quantity: "5.000",
  confirmed_quantity: "0.000",
  available_quantity: "5.000",
  mapping_state: "ready",
  mapping_version: "v1",
};

const draft = {
  issue_id: "issue-1",
  project_id: "project-1",
  issue_no: "LYD-20260809-ABC",
  issue_date: "2026-08-09",
  workflow_status: "draft" as const,
  receiver: "张三",
  issued_by: "李四",
  site_location: "现场 A",
  version: 1,
  lines: [{
    issue_line_id: "issue-line-1",
    line_no: 1,
    part_id: 1,
    pn: "PN-001",
    quantity: "2.000",
    delivery_line_id: "delivery-1",
    source_order_id: "order-1",
    source_line_id: "line-1",
    serial_number: "SN-001",
    cost_source: null,
    cost_source_label: "待补价格",
    cost_is_estimate: false,
    cost_amount_ex_tax: null,
    cost_amount_inc_tax: null,
    version: 1,
  }],
};

beforeEach(() => {
  vi.clearAllMocks();
  api.searchSiteIssueCandidates.mockResolvedValue({
    data: { adapter, rows: [candidate], total: 1, page: 1, page_size: 50 },
  });
  api.searchSiteIssues.mockResolvedValue({
    data: {
      project_id: "project-1",
      adapter,
      rows: [draft],
      total: 1,
      page: 1,
      page_size: 20,
    },
  });
  api.previewSiteIssue.mockResolvedValue({
    data: {
      ...draft,
      can_confirm: true,
      blockers: [],
      inventory_effect: "none",
      lines: [{
        ...draft.lines[0],
        cost_source: "direct_purchase",
        cost_source_label: "关联采购单价",
        cost_amount_ex_tax: "40.00",
        cost_amount_inc_tax: "45.20",
        available_quantity: "5.000",
      }],
    },
  });
  api.confirmSiteIssue.mockResolvedValue({
    data: { ...draft, workflow_status: "confirmed", version: 2 },
  });
  api.createSiteIssueDraft.mockResolvedValue({ data: draft });
  api.patchSiteIssue.mockResolvedValue({ data: { ...draft, version: 2 } });
  api.voidSiteIssue.mockResolvedValue({
    data: { ...draft, workflow_status: "void", version: 2 },
  });
});

afterEach(cleanup);

describe("SiteIssueWorkflowPanel", () => {
  it("没有现场领用权限时完全隐藏入口且不发请求", () => {
    render(
      <SiteIssueWorkflowPanel
        projectId="project-1"
        canManage={false}
        onChanged={vi.fn()}
      />,
    );

    expect(screen.queryByTestId("site-issue-workflow")).toBeNull();
    expect(api.searchSiteIssues).not.toHaveBeenCalled();
    expect(api.searchSiteIssueCandidates).not.toHaveBeenCalled();
  });

  it("展示系统单号、成本状态、库存不影响提示和适配器生产闸门", async () => {
    render(
      <SiteIssueWorkflowPanel
        projectId="project-1"
        canManage
        onChanged={vi.fn()}
      />,
    );

    const panel = await screen.findByTestId("site-issue-workflow");
    expect(within(panel).getByText("LYD-20260809-ABC")).toBeInTheDocument();
    expect(within(panel).getByText("待确认草稿")).toBeInTheDocument();
    expect(within(panel).getByText(/全过程不修改公司库、地区库或前置库库存/))
      .toBeInTheDocument();
    expect(within(panel).getByText(/真实适配器接入前不得用于生产确认/))
      .toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "新建领用单" }));
    expect(await screen.findByText("PN-001")).toBeInTheDocument();
    expect(screen.getByText(/可领 5.000/)).toBeInTheDocument();
    expect(screen.getByText(/稳定来源（WBDD\/source_order_id）：order-1 · 行 line-1/))
      .toBeInTheDocument();
  });

  it("固定展示当前单据已选明细，禁用已领完候选并显式提供继续加载", async () => {
    const hiddenDraft = {
      ...draft,
      lines: [{
        ...draft.lines[0],
        issue_line_id: "hidden-issue-line",
        delivery_line_id: "hidden-delivery",
        source_order_id: "WBDD-HIDDEN",
        source_line_id: "hidden-line",
      }],
    };
    const consumedCandidate = {
      ...candidate,
      delivery_line_id: "delivery-full",
      source_order_id: "WBDD-FULL",
      source_line_id: "full-line",
      pn: "PN-FULL",
      confirmed_quantity: "5.000",
      available_quantity: "0.000",
    };
    api.searchSiteIssues.mockResolvedValue({
      data: {
        project_id: "project-1",
        adapter,
        rows: [hiddenDraft],
        total: 21,
        page: 1,
        page_size: 20,
      },
    });
    api.searchSiteIssueCandidates.mockResolvedValue({
      data: { adapter, rows: [consumedCandidate], total: 51, page: 1, page_size: 50 },
    });

    render(
      <SiteIssueWorkflowPanel
        projectId="project-1"
        canManage
        onChanged={vi.fn()}
      />,
    );

    const panel = await screen.findByTestId("site-issue-workflow");
    expect(within(panel).getByText("共 21 张，已显示 1 张")).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "加载更多领用单" }));
    await waitFor(() => expect(api.searchSiteIssues).toHaveBeenCalledWith(
      expect.objectContaining({ project_id: "project-1", page: 2, page_size: 20 }),
    ));

    fireEvent.click(within(panel).getByRole("button", { name: "编辑草稿" }));
    const editorDialog = await screen.findByRole("dialog", { name: "编辑现场领用草稿" });
    expect(within(editorDialog).getByText("本单已选明细（固定展示）")).toBeInTheDocument();
    expect(within(editorDialog).getByText(/WBDD-HIDDEN · 行 hidden-line/)).toBeInTheDocument();
    expect(within(editorDialog).getByText(/已领完/)).toBeInTheDocument();
    expect(within(editorDialog).getByRole("checkbox", { name: "PN-FULL" })).toBeDisabled();
    expect(within(editorDialog).getByText("共 51 条稳定发货明细，已显示 1 条"))
      .toBeInTheDocument();
    fireEvent.click(within(editorDialog).getByRole("button", { name: "加载更多发货明细" }));
    await waitFor(() => expect(api.searchSiteIssueCandidates).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ page: 2, page_size: 50 }),
    ));
  });

  it("从发货候选创建草稿，客户端只提交业务字段", async () => {
    const onChanged = vi.fn();
    render(
      <SiteIssueWorkflowPanel
        projectId="project-1"
        canManage
        onChanged={onChanged}
      />,
    );

    const panel = await screen.findByTestId("site-issue-workflow");
    fireEvent.click(within(panel).getByRole("button", { name: "新建领用单" }));
    const editorDialog = await screen.findByRole("dialog", { name: "新建现场领用草稿" });
    fireEvent.change(within(editorDialog).getByLabelText("接收人"), { target: { value: "王五" } });
    fireEvent.change(within(editorDialog).getByLabelText("发出人"), { target: { value: "赵六" } });
    fireEvent.change(within(editorDialog).getByLabelText("现场位置"), { target: { value: "井场 B" } });
    fireEvent.change(within(editorDialog).getByLabelText("操作原因"), { target: { value: "月度现场实际领用" } });
    fireEvent.click(within(editorDialog).getByRole("checkbox"));
    fireEvent.change(within(editorDialog).getByLabelText("PN-001 领用数量"), { target: { value: "2" } });
    fireEvent.click(within(editorDialog).getByRole("button", { name: "保存草稿" }));

    await waitFor(() => expect(api.createSiteIssueDraft).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({
        receiver: "王五",
        issued_by: "赵六",
        site_location: "井场 B",
        lines: [{ delivery_line_id: "delivery-1", quantity: 2 }],
        reason: "月度现场实际领用",
      }),
    ));
    const submitted = api.createSiteIssueDraft.mock.calls[0][1];
    expect(submitted).not.toHaveProperty("issue_id");
    expect(submitted).not.toHaveProperty("issue_no");
    expect(submitted.lines[0]).not.toHaveProperty("issue_line_id");
    expect(onChanged).toHaveBeenCalled();
  });

  it("已确认单可走更正与作废命令，并始终携带项目和版本", async () => {
    const confirmed = { ...draft, workflow_status: "confirmed" as const, version: 4 };
    api.searchSiteIssues.mockResolvedValue({
      data: {
        project_id: "project-1",
        adapter,
        rows: [confirmed],
        total: 1,
        page: 1,
        page_size: 20,
      },
    });
    const onChanged = vi.fn();
    render(
      <SiteIssueWorkflowPanel
        projectId="project-1"
        canManage
        onChanged={onChanged}
      />,
    );

    const panel = await screen.findByTestId("site-issue-workflow");
    expect(within(panel).getByText("已确认")).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "更正" }));
    const editorDialog = await screen.findByRole("dialog", { name: "更正现场领用单" });
    fireEvent.change(within(editorDialog).getByLabelText("现场位置"), { target: { value: "井场 C" } });
    fireEvent.click(within(editorDialog).getByRole("button", { name: "提交更正" }));
    await waitFor(() => expect(api.patchSiteIssue).toHaveBeenCalledWith(
      "issue-1",
      expect.objectContaining({ project_id: "project-1", version: 4, site_location: "井场 C" }),
    ));

    fireEvent.click(within(panel).getByRole("button", { name: "作废" }));
    const voidDialog = await screen.findByRole("dialog", { name: "作废 LYD-20260809-ABC" });
    expect(within(voidDialog).getByText(/不会冲减历史成本，也不会修改任何库存/)).toBeInTheDocument();
    expect(within(voidDialog).getByRole("button", { name: "确认作废" })).toBeDisabled();
    fireEvent.change(within(voidDialog).getByLabelText("作废原因"), { target: { value: "现场记录重复录入" } });
    fireEvent.click(within(voidDialog).getByRole("button", { name: "确认作废" }));
    await waitFor(() => expect(api.voidSiteIssue).toHaveBeenCalledWith(
      "issue-1",
      expect.objectContaining({ project_id: "project-1", version: 4, reason: "现场记录重复录入" }),
    ));
    expect(onChanged).toHaveBeenCalledTimes(2);
  });

  it("先完整预览成本与余额，再确认且不允许迟到响应覆盖新项目", async () => {
    const onChanged = vi.fn();
    const { rerender } = render(
      <SiteIssueWorkflowPanel
        projectId="project-1"
        canManage
        onChanged={onChanged}
      />,
    );
    const panel = await screen.findByTestId("site-issue-workflow");
    fireEvent.click(within(panel).getByRole("button", { name: "预览并确认" }));

    const preview = await screen.findByRole("dialog", { name: "确认影响预览" });
    expect(within(preview).getByText("¥40.00")).toBeInTheDocument();
    expect(within(preview).getByText("关联采购单价")).toBeInTheDocument();
    expect(within(preview).getByText(/库存影响：无/)).toBeInTheDocument();
    fireEvent.click(within(preview).getByRole("button", { name: "确认现场领用" }));
    await waitFor(() => expect(api.confirmSiteIssue).toHaveBeenCalledWith(
      "issue-1",
      expect.objectContaining({ project_id: "project-1", version: 1 }),
    ));
    expect(onChanged).toHaveBeenCalled();

    let resolveOld!: (value: unknown) => void;
    api.searchSiteIssues
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({
        data: {
          project_id: "project-2",
          adapter,
          rows: [{ ...draft, issue_id: "issue-2", project_id: "project-2", issue_no: "LYD-NEW" }],
          total: 1,
          page: 1,
          page_size: 20,
        },
      });
    rerender(
      <SiteIssueWorkflowPanel
        projectId="project-old"
        canManage
        onChanged={onChanged}
      />,
    );
    rerender(
      <SiteIssueWorkflowPanel
        projectId="project-2"
        canManage
        onChanged={onChanged}
      />,
    );
    expect(await screen.findByText("LYD-NEW")).toBeInTheDocument();
    resolveOld({ data: { project_id: "project-old", adapter, rows: [{ ...draft, issue_no: "LYD-STALE" }], total: 1, page: 1, page_size: 20 } });
    await waitFor(() => expect(screen.queryByText("LYD-STALE")).toBeNull());
  });

  it("失败和空结果都给出可见状态，不把页面留成黑盒", async () => {
    api.searchSiteIssues.mockRejectedValueOnce(new Error("synthetic failure"));
    api.searchSiteIssueCandidates.mockResolvedValueOnce({
      data: {
        adapter: { ...adapter, state: "unavailable", detail: "真实发货适配器尚未接入" },
        rows: [],
        total: 0,
        page: 1,
        page_size: 50,
      },
    });
    render(
      <SiteIssueWorkflowPanel
        projectId="project-1"
        canManage
        onChanged={vi.fn()}
      />,
    );

    expect(await screen.findByText("现场领用单加载失败")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
    expect(screen.getByText("真实发货适配器尚未接入")).toBeInTheDocument();
  });
});
