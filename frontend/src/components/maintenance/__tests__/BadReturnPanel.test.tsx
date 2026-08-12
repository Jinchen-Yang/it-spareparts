import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { message } from "antd";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  searchMaintenanceReturnObligations: vi.fn(),
  searchMaintenanceBadReturns: vi.fn(),
  createMaintenanceBadReturnDraft: vi.fn(),
  submitMaintenanceBadReturn: vi.fn(),
  markMaintenanceBadReturnInTransit: vi.fn(),
  confirmMaintenanceBadReturnWarehouse: vi.fn(),
  voidMaintenanceBadReturn: vi.fn(),
  listMaintenanceReturnCategories: vi.fn(),
  resolveMaintenanceReturnObligationCategory: vi.fn(),
}));

vi.mock("../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceOperations")>(
    "../../../api/maintenanceOperations",
  );
  return { ...actual, ...api };
});

import BadReturnPanel from "../BadReturnPanel";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

const returnRate = {
  project_id: "project-1",
  status: "basis_incomplete" as const,
  official_basis: null,
  official_rate_pct: null,
  registered_rate_pct: null,
  warehouse_confirmed_rate_pct: null,
  required_quantity: "5.000",
  registered_quantity: "2.000",
  warehouse_confirmed_quantity: "1.000",
  outstanding_quantity: "4.000",
  exempt_quantity: "1.000",
  pending_quantity: "2.000",
  required_count: 1,
  exempt_count: 1,
  pending_count: 1,
  business_assumption: "仓库确认量仅作试算；官方返还率分子待业务确认。",
};

const obligations = [{
  obligation_id: "obligation-required",
  project_id: "project-1",
  issue_id: "issue-1",
  issue_no: "LYD-001",
  issue_line_id: "line-1",
  delivery_line_id: "delivery-1",
  part_id: 1,
  pn: "PN-REQUIRED",
  source_quantity: "5.000",
  required_quantity: "5.000",
  classification: "required" as const,
  category_id_snapshot: 11,
  category_major_snapshot: "服务器配件",
  category_minor_snapshot: "内存",
  rule_version: "maintenance-bad-return-category-v1",
  registered_quantity: "2.000",
  warehouse_confirmed_quantity: "1.000",
  remaining_quantity: "4.000",
  is_active: true,
  version: 1,
}, {
  obligation_id: "obligation-exempt",
  project_id: "project-1",
  issue_id: "issue-1",
  issue_no: "LYD-001",
  issue_line_id: "line-2",
  delivery_line_id: "delivery-2",
  part_id: 2,
  pn: "PN-DISK",
  source_quantity: "1.000",
  required_quantity: "0.000",
  classification: "exempt" as const,
  category_id_snapshot: 12,
  category_major_snapshot: "硬盘",
  category_minor_snapshot: null,
  rule_version: "maintenance-bad-return-category-v1",
  registered_quantity: "0.000",
  warehouse_confirmed_quantity: "0.000",
  remaining_quantity: "0.000",
  is_active: true,
  version: 1,
}, {
  obligation_id: "obligation-pending",
  project_id: "project-1",
  issue_id: "issue-1",
  issue_no: "LYD-001",
  issue_line_id: "line-3",
  delivery_line_id: "delivery-3",
  part_id: 3,
  pn: "PN-UNKNOWN",
  source_quantity: "2.000",
  required_quantity: "0.000",
  classification: "pending_category" as const,
  category_id_snapshot: null,
  category_major_snapshot: null,
  category_minor_snapshot: null,
  rule_version: "maintenance-bad-return-category-v1",
  registered_quantity: "0.000",
  warehouse_confirmed_quantity: "0.000",
  remaining_quantity: "0.000",
  is_active: true,
  version: 1,
}];

const badReturnDraft = {
  return_id: "return-1",
  return_no: "HJFH-20260809-001",
  replaces_return_id: null,
  project_id: "project-1",
  status: "draft" as const,
  logistics_reference: null,
  warehouse_reference: null,
  inbound_reference: null,
  note: "现场集中返件",
  created_by: "synthetic-user",
  submitted_at: null,
  in_transit_at: null,
  warehouse_confirmed_at: null,
  voided_at: null,
  version: 1,
  lines: [{
    return_line_id: "return-line-1",
    line_no: 1,
    obligation_id: "obligation-required",
    part_id: 1,
    pn: "PN-REQUIRED",
    quantity: "2.000",
  }],
  inventory_effect: "none" as const,
  cost_effect: "none" as const,
  idempotent_replay: false,
};
const badReturnSubmitted = { ...badReturnDraft, status: "submitted" as const, version: 2 };
const badReturnInTransit = {
  ...badReturnSubmitted,
  status: "in_transit" as const,
  version: 3,
  logistics_reference: "LOG-REF-001",
};
const badReturnVoided = {
  ...badReturnSubmitted,
  status: "void" as const,
  version: 3,
  voided_at: "2026-08-09T10:00:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  api.searchMaintenanceReturnObligations.mockResolvedValue({
    data: { rows: obligations, total: 3, page: 1, page_size: 50, return_rate: returnRate },
  });
  api.searchMaintenanceBadReturns.mockResolvedValue({
    data: { project_id: "project-1", rows: [], total: 0, page: 1, page_size: 20 },
  });
  api.createMaintenanceBadReturnDraft.mockResolvedValue({ data: badReturnDraft });
  api.submitMaintenanceBadReturn.mockResolvedValue({
    data: badReturnSubmitted,
  });
  api.markMaintenanceBadReturnInTransit.mockResolvedValue({
    data: badReturnInTransit,
  });
  api.confirmMaintenanceBadReturnWarehouse.mockResolvedValue({
    data: {
      ...badReturnInTransit,
      status: "warehouse_confirmed",
      version: 4,
      warehouse_reference: "WH-CHECK-001",
      inbound_reference: "RKD-001",
    },
  });
  api.voidMaintenanceBadReturn.mockResolvedValue({ data: badReturnVoided });
  api.listMaintenanceReturnCategories.mockResolvedValue({
    data: {
      categories: [
        { category_id: 9, category_major: "硬盘", category_minor: "SAS" },
        { category_id: 11, category_major: "服务器配件", category_minor: "内存" },
      ],
    },
  });
  api.resolveMaintenanceReturnObligationCategory.mockResolvedValue({
    data: {
      ...obligations[2],
      classification: "exempt",
      category_id_snapshot: 9,
      category_major_snapshot: "硬盘",
      category_minor_snapshot: "SAS",
      version: 2,
    },
  });
});

afterEach(async () => {
  cleanup();
  message.destroy();
  await waitFor(() => {
    expect(document.querySelectorAll(".ant-message-notice")).toHaveLength(0);
  });
});

describe("BadReturnPanel", () => {
  it("只读账号看到完整返还事实，品类待判定时不显示虚假百分比", async () => {
    render(
      <BadReturnPanel
        projectId="project-1"
        returnRate={returnRate}
        canManage={false}
        onChanged={vi.fn()}
      />,
    );

    const panel = await screen.findByTestId("bad-return-panel");
    expect(within(panel).getByTestId("return-required")).toHaveTextContent("应返5.000");
    expect(within(panel).getByTestId("return-registered")).toHaveTextContent("已登记2.000");
    expect(within(panel).getByTestId("return-confirmed")).toHaveTextContent("仓库确认1.000");
    expect(within(panel).getByTestId("return-outstanding")).toHaveTextContent("待仓库确认4.000");
    expect(within(panel).getByTestId("return-exempt")).toHaveTextContent("硬盘免返1.000");
    expect(within(panel).getByTestId("return-pending")).toHaveTextContent("品类待判定2.000");
    expect(within(panel).getByText("返还率暂不可判定")).toBeInTheDocument();
    expect(panel).not.toHaveTextContent("%");
    expect(within(panel).getAllByText("硬盘免返")).toHaveLength(2);
    expect(within(panel).getAllByText("品类待判定")).toHaveLength(2);
    expect(panel).toHaveTextContent("管理员关联标准品类后才能判定是否应返");
    expect(panel).toHaveTextContent("未登记 4.000");
    expect(within(panel).queryByRole("button", { name: "新建坏件返还单" })).toBeNull();
    expect(within(panel).queryByRole("button", { name: "处理品类" })).toBeNull();
    expect(api.searchMaintenanceReturnObligations).toHaveBeenCalledWith({
      project_id: "project-1",
      page: 1,
      page_size: 50,
    });
    expect(api.searchMaintenanceBadReturns).toHaveBeenCalledWith({
      project_id: "project-1",
      page: 1,
      page_size: 20,
    });
  });

  it("管理员可在待判定义务上关联标准品类并填写判定原因", async () => {
    localStorage.setItem("role", "admin");
    const onChanged = vi.fn();
    render(
      <BadReturnPanel
        projectId="project-1"
        returnRate={returnRate}
        canManage={false}
        onChanged={onChanged}
      />,
    );

    const panel = await screen.findByTestId("bad-return-panel");
    fireEvent.click(within(panel).getByRole("button", { name: "处理品类" }));
    const dialog = await screen.findByRole("dialog", { name: "处理品类待判定" });
    await waitFor(() => expect(api.listMaintenanceReturnCategories).toHaveBeenCalledOnce());
    fireEvent.mouseDown(within(dialog).getByRole("combobox", { name: "标准品类" }));
    fireEvent.click(await screen.findByText("硬盘 / SAS"));
    fireEvent.change(within(dialog).getByLabelText("判定原因"), {
      target: { value: "核对标准品类主数据后确认" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认关联" }));

    await waitFor(() => expect(
      api.resolveMaintenanceReturnObligationCategory,
    ).toHaveBeenCalledWith(
      "obligation-pending",
      expect.objectContaining({
        project_id: "project-1",
        version: 1,
        category_id: 9,
        reason: "核对标准品类主数据后确认",
      }),
    ));
    expect(onChanged).toHaveBeenCalled();
  });

  it("没有明确应返项时显示业务状态而不是 100%", async () => {
    const noReturnRate = {
      ...returnRate,
      status: "no_return_required" as const,
      required_quantity: "0.000",
      registered_quantity: "0.000",
      warehouse_confirmed_quantity: "0.000",
      outstanding_quantity: "0.000",
      exempt_quantity: "3.000",
      pending_quantity: "0.000",
      required_count: 0,
      exempt_count: 2,
      pending_count: 0,
    };
    api.searchMaintenanceReturnObligations.mockResolvedValueOnce({
      data: { rows: [], total: 0, page: 1, page_size: 50, return_rate: noReturnRate },
    });

    render(
      <BadReturnPanel
        projectId="project-1"
        returnRate={noReturnRate}
        canManage={false}
        onChanged={vi.fn()}
      />,
    );

    const panel = await screen.findByTestId("bad-return-panel");
    expect(within(panel).getByText("无应返项")).toBeInTheDocument();
    expect(panel).not.toHaveTextContent("100%");
  });

  it("管理账号只能从明确应返且仍有余额的义务建立草稿", async () => {
    const availableRate = {
      ...returnRate,
      status: "available" as const,
      official_rate_pct: null,
      registered_rate_pct: "40.00",
      warehouse_confirmed_rate_pct: "20.00",
      pending_quantity: "0.000",
      pending_count: 0,
    };
    api.searchMaintenanceReturnObligations.mockResolvedValueOnce({
      data: { rows: obligations, total: 3, page: 1, page_size: 50, return_rate: availableRate },
    });
    const onChanged = vi.fn();

    render(
      <BadReturnPanel
        projectId="project-1"
        returnRate={availableRate}
        canManage
        onChanged={onChanged}
      />,
    );

    const panel = await screen.findByTestId("bad-return-panel");
    expect(within(panel).getByText("仓库确认返还率（试算）")).toBeInTheDocument();
    expect(panel).toHaveTextContent("20%");
    expect(panel).not.toHaveTextContent("官方返还率");
    const createButton = within(panel).getByRole("button", { name: "新建坏件返还单" });
    await waitFor(() => expect(createButton).toBeEnabled());
    fireEvent.click(createButton);
    const dialog = await screen.findByRole("dialog", { name: "新建坏件返还草稿" });
    expect(within(dialog).getByText("PN-REQUIRED")).toBeInTheDocument();
    expect(within(dialog).queryByText("PN-DISK")).toBeNull();
    expect(within(dialog).queryByText("PN-UNKNOWN")).toBeNull();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "PN-REQUIRED" }));
    fireEvent.change(within(dialog).getByLabelText("PN-REQUIRED 返还数量"), {
      target: { value: "2" },
    });
    fireEvent.change(within(dialog).getByLabelText("返还单备注"), {
      target: { value: "现场集中返件" },
    });
    fireEvent.change(within(dialog).getByLabelText("建立草稿原因"), {
      target: { value: "现场坏件已收集" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存草稿" }));

    await waitFor(() => expect(api.createMaintenanceBadReturnDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        project_id: "project-1",
        lines: [{ obligation_id: "obligation-required", quantity: 2 }],
        note: "现场集中返件",
        reason: "现场坏件已收集",
      }),
    ));
    const submitted = api.createMaintenanceBadReturnDraft.mock.calls[0][0];
    expect(submitted).not.toHaveProperty("return_id");
    expect(submitted).not.toHaveProperty("return_no");
    expect(submitted).not.toHaveProperty("status");
    expect(await screen.findByText("HJFH-20260809-001")).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();
  });

  it("刷新后加载已有草稿并携带当前版本提交登记", async () => {
    api.searchMaintenanceBadReturns.mockResolvedValueOnce({
      data: {
        project_id: "project-1",
        rows: [badReturnDraft],
        total: 1,
        page: 1,
        page_size: 20,
      },
    });

    render(
      <BadReturnPanel
        projectId="project-1"
        returnRate={returnRate}
        canManage
        onChanged={vi.fn()}
      />,
    );

    const card = await screen.findByTestId("bad-return-card-return-1");
    fireEvent.click(within(card).getByRole("button", { name: "提交返还单" }));
    const dialog = await screen.findByRole("dialog", { name: "提交坏件返还单" });
    fireEvent.change(within(dialog).getByLabelText("操作原因"), {
      target: { value: "已核对现场返件数量" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认提交" }));

    await waitFor(() => expect(api.submitMaintenanceBadReturn).toHaveBeenCalledWith(
      "return-1",
      expect.objectContaining({
        project_id: "project-1",
        version: 1,
        reason: "已核对现场返件数量",
      }),
    ));
    expect(await within(card).findByText("已登记")).toBeInTheDocument();
    await waitFor(() => {
      expect(api.searchMaintenanceReturnObligations).toHaveBeenCalledTimes(2);
    });
  });

  it("已登记返还单可记录人工物流参考并标记在途", async () => {
    api.searchMaintenanceBadReturns.mockResolvedValueOnce({
      data: {
        project_id: "project-1",
        rows: [badReturnSubmitted],
        total: 1,
        page: 1,
        page_size: 20,
      },
    });

    render(
      <BadReturnPanel
        projectId="project-1"
        returnRate={returnRate}
        canManage
        onChanged={vi.fn()}
      />,
    );

    const card = await screen.findByTestId("bad-return-card-return-1");
    expect(within(card).getByRole("button", { name: "仓库确认" })).toBeInTheDocument();
    fireEvent.click(within(card).getByRole("button", { name: "标记在途" }));
    const dialog = await screen.findByRole("dialog", { name: "标记坏件返还在途" });
    fireEvent.change(within(dialog).getByLabelText("物流参考"), {
      target: { value: "LOG-REF-001" },
    });
    fireEvent.change(within(dialog).getByLabelText("操作原因"), {
      target: { value: "现场已交寄" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认在途" }));

    await waitFor(() => expect(api.markMaintenanceBadReturnInTransit).toHaveBeenCalledWith(
      "return-1",
      expect.objectContaining({
        project_id: "project-1",
        version: 2,
        logistics_reference: "LOG-REF-001",
        reason: "现场已交寄",
      }),
    ));
    expect(await within(card).findByText("在途")).toBeInTheDocument();
    expect(within(card).getByText("物流参考：LOG-REF-001")).toBeInTheDocument();
  });

  it("在途返还单由仓库确认，并可关联外部稳定入库引用", async () => {
    api.searchMaintenanceBadReturns.mockResolvedValueOnce({
      data: {
        project_id: "project-1",
        rows: [badReturnInTransit],
        total: 1,
        page: 1,
        page_size: 20,
      },
    });
    const onChanged = vi.fn();

    render(
      <BadReturnPanel
        projectId="project-1"
        returnRate={returnRate}
        canManage
        onChanged={onChanged}
      />,
    );

    const card = await screen.findByTestId("bad-return-card-return-1");
    fireEvent.click(within(card).getByRole("button", { name: "仓库确认" }));
    const dialog = await screen.findByRole("dialog", { name: "仓库确认坏件返还" });
    expect(within(dialog).getByText("仓库确认只更新试算返还率")).toBeInTheDocument();
    expect(dialog).not.toHaveTextContent("官方返还率分子");
    fireEvent.change(within(dialog).getByLabelText("仓库确认参考"), {
      target: { value: "WH-CHECK-001" },
    });
    fireEvent.change(within(dialog).getByLabelText("外部入库稳定引用（可选）"), {
      target: { value: "RKD-001" },
    });
    fireEvent.change(within(dialog).getByLabelText("操作原因"), {
      target: { value: "仓库已验收坏件" },
    });
    const confirmButton = within(dialog).getByRole("button", { name: "确认仓库收件" });
    expect(confirmButton).not.toBeDisabled();
    fireEvent.click(confirmButton);

    await waitFor(() => expect(api.confirmMaintenanceBadReturnWarehouse).toHaveBeenCalledWith(
      "return-1",
      expect.objectContaining({
        project_id: "project-1",
        version: 3,
        warehouse_reference: "WH-CHECK-001",
        inbound_reference: "RKD-001",
        reason: "仓库已验收坏件",
      }),
    ));
    expect(await within(card).findByText("仓库已确认")).toBeInTheDocument();
    expect(within(card).getByText("仓库参考：WH-CHECK-001")).toBeInTheDocument();
    expect(within(card).getByText("正式入库引用：RKD-001")).toBeInTheDocument();
    expect(within(card).getByText("已关联正式入库，不可直接作废")).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "作废返还单" })).toBeNull();
    expect(onChanged).toHaveBeenCalled();
  });

  it("返还单追加式作废后可从原明细建立带关联的替代单", async () => {
    api.searchMaintenanceBadReturns.mockResolvedValueOnce({
      data: {
        project_id: "project-1",
        rows: [badReturnSubmitted],
        total: 1,
        page: 1,
        page_size: 20,
      },
    });
    api.createMaintenanceBadReturnDraft.mockResolvedValueOnce({
      data: {
        ...badReturnDraft,
        return_id: "return-2",
        return_no: "HJFH-20260809-002",
        replaces_return_id: "return-1",
      },
    });

    render(
      <BadReturnPanel
        projectId="project-1"
        returnRate={returnRate}
        canManage
        onChanged={vi.fn()}
      />,
    );

    const card = await screen.findByTestId("bad-return-card-return-1");
    fireEvent.click(within(card).getByRole("button", { name: "作废返还单" }));
    const voidDialog = await screen.findByRole("dialog", { name: "作废坏件返还单" });
    fireEvent.change(within(voidDialog).getByLabelText("作废原因"), {
      target: { value: "返还数量登记错误" },
    });
    fireEvent.click(within(voidDialog).getByRole("button", { name: "确认追加式作废" }));
    await waitFor(() => expect(api.voidMaintenanceBadReturn).toHaveBeenCalledWith(
      "return-1",
      expect.objectContaining({
        project_id: "project-1",
        version: 2,
        reason: "返还数量登记错误",
      }),
    ));
    expect(await within(card).findByText("已作废")).toBeInTheDocument();

    fireEvent.click(within(card).getByRole("button", { name: "建立替代单" }));
    const replacementDialog = await screen.findByRole("dialog", {
      name: "建立替代坏件返还草稿",
    });
    expect(within(replacementDialog).getByRole("checkbox", { name: "PN-REQUIRED" }))
      .toBeChecked();
    fireEvent.change(within(replacementDialog).getByLabelText("建立草稿原因"), {
      target: { value: "按正确业务事实重建" },
    });
    fireEvent.click(within(replacementDialog).getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(api.createMaintenanceBadReturnDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        project_id: "project-1",
        replaces_return_id: "return-1",
        lines: [{ obligation_id: "obligation-required", quantity: 2 }],
        reason: "按正确业务事实重建",
      }),
    ));
  });

  it("相邻用例不继承 AntD 全局消息挂载点", () => {
    expect(document.querySelectorAll(".ant-message-notice")).toHaveLength(0);
  });

  it("同一次提交失败后重试复用打开弹窗时生成的幂等键", async () => {
    api.searchMaintenanceBadReturns.mockResolvedValueOnce({
      data: {
        project_id: "project-1",
        rows: [badReturnDraft],
        total: 1,
        page: 1,
        page_size: 20,
      },
    });
    api.submitMaintenanceBadReturn
      .mockRejectedValueOnce(new Error("synthetic network timeout"))
      .mockResolvedValueOnce({ data: badReturnSubmitted });

    render(
      <BadReturnPanel
        projectId="project-1"
        returnRate={returnRate}
        canManage
        onChanged={vi.fn()}
      />,
    );

    const card = await screen.findByTestId("bad-return-card-return-1");
    fireEvent.click(within(card).getByRole("button", { name: "提交返还单" }));
    const dialog = await screen.findByRole("dialog", { name: "提交坏件返还单" });
    fireEvent.change(within(dialog).getByLabelText("操作原因"), {
      target: { value: "确认数量无误" },
    });
    const submitButton = within(dialog).getByRole("button", { name: "确认提交" });
    fireEvent.click(submitButton);
    expect(await within(dialog).findByText(/提交失败/)).toBeInTheDocument();
    const firstKey = api.submitMaintenanceBadReturn.mock.calls[0][1].idempotency_key;

    fireEvent.click(submitButton);
    await waitFor(() => expect(api.submitMaintenanceBadReturn).toHaveBeenCalledTimes(2));
    expect(api.submitMaintenanceBadReturn.mock.calls[1][1].idempotency_key).toBe(firstKey);
  });

  it("加载失败后给出通用错误和可用重试，不泄露服务端详情", async () => {
    api.searchMaintenanceReturnObligations.mockRejectedValueOnce({
      response: { data: { detail: "内部义务 obligation-secret-1" } },
    });

    render(
      <BadReturnPanel
        projectId="project-1"
        returnRate={returnRate}
        canManage={false}
        onChanged={vi.fn()}
      />,
    );

    const panel = await screen.findByTestId("bad-return-panel");
    expect(await within(panel).findByText("坏件返还信息加载失败")).toBeInTheDocument();
    expect(panel).not.toHaveTextContent("obligation-secret-1");
    fireEvent.click(within(panel).getByRole("button", { name: "重试" }));
    expect(await within(panel).findByText("PN-REQUIRED")).toBeInTheDocument();
    expect(api.searchMaintenanceReturnObligations).toHaveBeenCalledTimes(2);
  });

  it("切换项目后忽略旧项目迟到的义务与返还单响应", async () => {
    const oldObligations = deferred<{ data: {
      rows: typeof obligations;
      total: number;
      page: number;
      page_size: number;
      return_rate: typeof returnRate;
    } }>();
    const oldReturns = deferred<{ data: {
      project_id: string;
      rows: typeof badReturnDraft[];
      total: number;
      page: number;
      page_size: number;
    } }>();
    const nextObligation = {
      ...obligations[0],
      obligation_id: "obligation-next",
      project_id: "project-2",
      pn: "PN-NEXT",
    };
    const nextRate = { ...returnRate, project_id: "project-2" };
    api.searchMaintenanceReturnObligations
      .mockReturnValueOnce(oldObligations.promise)
      .mockResolvedValueOnce({
        data: { rows: [nextObligation], total: 1, page: 1, page_size: 50, return_rate: nextRate },
      });
    api.searchMaintenanceBadReturns
      .mockReturnValueOnce(oldReturns.promise)
      .mockResolvedValueOnce({
        data: { project_id: "project-2", rows: [], total: 0, page: 1, page_size: 20 },
      });

    const { rerender } = render(
      <BadReturnPanel
        projectId="project-1"
        returnRate={returnRate}
        canManage={false}
        onChanged={vi.fn()}
      />,
    );
    expect(api.searchMaintenanceReturnObligations).toHaveBeenCalledWith(expect.objectContaining({
      project_id: "project-1",
    }));
    rerender(
      <BadReturnPanel
        projectId="project-2"
        returnRate={nextRate}
        canManage={false}
        onChanged={vi.fn()}
      />,
    );
    expect(await screen.findByText("PN-NEXT")).toBeInTheDocument();

    await act(async () => {
      oldObligations.resolve({
        data: { rows: obligations, total: 3, page: 1, page_size: 50, return_rate: returnRate },
      });
      oldReturns.resolve({
        data: {
          project_id: "project-1",
          rows: [badReturnDraft],
          total: 1,
          page: 1,
          page_size: 20,
        },
      });
      await Promise.resolve();
    });

    expect(screen.queryByText("PN-REQUIRED")).toBeNull();
    expect(screen.queryByText("HJFH-20260809-001")).toBeNull();
    expect(screen.getByText("PN-NEXT")).toBeInTheDocument();
  });

  it("义务和返还单超过首屏时可继续加载，且第二页不会覆盖已展示事实", async () => {
    const nextObligation = {
      ...obligations[0],
      obligation_id: "obligation-page-2",
      pn: "PN-PAGE-2",
    };
    const nextReturn = {
      ...badReturnSubmitted,
      return_id: "return-page-2",
      return_no: "HJFH-20260809-021",
    };
    api.searchMaintenanceReturnObligations
      .mockResolvedValueOnce({
        data: { rows: obligations, total: 51, page: 1, page_size: 50, return_rate: returnRate },
      })
      .mockResolvedValueOnce({
        data: { rows: [nextObligation], total: 51, page: 2, page_size: 50, return_rate: returnRate },
      });
    api.searchMaintenanceBadReturns
      .mockResolvedValueOnce({
        data: {
          project_id: "project-1",
          rows: [badReturnDraft],
          total: 21,
          page: 1,
          page_size: 20,
        },
      })
      .mockResolvedValueOnce({
        data: {
          project_id: "project-1",
          rows: [nextReturn],
          total: 21,
          page: 2,
          page_size: 20,
        },
      });

    render(
      <BadReturnPanel
        projectId="project-1"
        returnRate={returnRate}
        canManage={false}
        onChanged={vi.fn()}
      />,
    );

    const panel = await screen.findByTestId("bad-return-panel");
    expect(await within(panel).findByText("PN-REQUIRED")).toBeInTheDocument();
    expect(within(panel).getByText("HJFH-20260809-001")).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "加载更多返还义务" }));
    await waitFor(() => expect(api.searchMaintenanceReturnObligations).toHaveBeenLastCalledWith({
      project_id: "project-1",
      page: 2,
      page_size: 50,
    }));
    expect(await within(panel).findByText("PN-PAGE-2")).toBeInTheDocument();
    expect(within(panel).getByText("PN-REQUIRED")).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "加载更多返还单" }));
    await waitFor(() => expect(api.searchMaintenanceBadReturns).toHaveBeenLastCalledWith({
      project_id: "project-1",
      page: 2,
      page_size: 20,
    }));
    expect(await within(panel).findByText("HJFH-20260809-021")).toBeInTheDocument();
    expect(within(panel).getByText("HJFH-20260809-001")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "加载更多返还义务" })).toBeNull();
    expect(within(panel).queryByRole("button", { name: "加载更多返还单" })).toBeNull();
  });
});
