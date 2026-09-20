/** 人工登记领用（page_manual）组件：入口可用、预览冻结、幂等键、epoch、锁。 */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  previewManualSiteIssue: vi.fn(),
  createManualSiteIssue: vi.fn(),
  patchManualSiteIssue: vi.fn(),
  voidSiteIssue: vi.fn(),
  searchSiteIssues: vi.fn(),
  searchSiteIssueCandidates: vi.fn(),
}));

vi.mock("../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceOperations")>(
    "../../../api/maintenanceOperations",
  );
  return { ...actual, ...api };
});

const PartPickerMock = vi.hoisted(() => ({
  __esModule: true,
  default: vi.fn(),
}));
vi.mock("../../PartPicker", () => PartPickerMock);

import ManualSiteIssueSection from "../ManualSiteIssueSection";

const manualIssue = {
  issue_id: "issue-manual-1",
  project_id: "project-1",
  issue_no: "LYR-20260920-ABC",
  issue_date: "2026-09-20",
  workflow_status: "confirmed" as const,
  raw_status: "已确认",
  status_mapping_state: "mapped",
  normalized_status: "confirmed",
  status_mapping_version: "page-manual-v1",
  source: "page_manual",
  import_batch_id: null,
  receiver: "张三",
  issued_by: "李四",
  site_location: "现场 A",
  created_by: "op",
  confirmed_at: null,
  corrected_at: null,
  voided_at: null,
  version: 1,
  lines: [{
    issue_line_id: "issue-line-m1",
    line_no: 1,
    part_id: 1,
    pn: "PN-MANUAL-1",
    quantity: "2.000",
    delivery_line_id: null,
    source_order_id: null,
    source_line_id: null,
    serial_number: null,
    no_return: null,
    remark: null,
    demand_order_no: null,
    cost_source: null,
    cost_source_label: "待补价格",
    cost_is_estimate: false,
    cost_amount_ex_tax: null,
    cost_amount_inc_tax: null,
    version: 1,
  }],
};

const previewPayload = {
  project_id: "project-1",
  issue_date: "2026-09-20",
  receiver: "张三",
  issued_by: "李四",
  site_location: "现场 A",
  inventory_effect: "none" as const,
  total_cost_ex_tax: null,
  total_cost_inc_tax: null,
  lines: [{
    part_id: 1,
    pn: "PN-MANUAL-1",
    quantity: "2.000",
    serial_number: null,
    no_return: null,
    demand_order_no: null,
    remark: null,
    cost_source: null,
    cost_source_label: "待补价格",
    cost_is_estimate: false,
    unit_cost_ex_tax: null,
    cost_amount_ex_tax: null,
    cost_amount_inc_tax: null,
    cost_gap: true,
  }],
};

beforeEach(() => {
  vi.clearAllMocks();
  PartPickerMock.default.mockImplementation(
    ({ value, onChange }: { value?: number | null; onChange?: (v: number | null) => void }) => (
      <input
        aria-label="型号选择"
        value={value ?? ""}
        onChange={(event) => onChange?.(Number(event.target.value) || null)}
      />
    ),
  );
  api.previewManualSiteIssue.mockResolvedValue({ data: previewPayload });
  api.createManualSiteIssue.mockResolvedValue({ data: manualIssue });
  api.patchManualSiteIssue.mockResolvedValue({
    data: { ...manualIssue, workflow_status: "corrected" as const, version: 2 },
  });
  api.voidSiteIssue.mockResolvedValue({
    data: { ...manualIssue, workflow_status: "void" as const, version: 2 },
  });
});

afterEach(cleanup);

const reloadIssues = vi.fn().mockResolvedValue(undefined);
const onChanged = vi.fn().mockResolvedValue(undefined);

function renderSection(issues = [manualIssue], projectId = "project-1") {
  return render(
    <ManualSiteIssueSection
      projectId={projectId}
      canManage
      issues={issues}
      onChanged={onChanged}
      reloadIssues={reloadIssues}
    />,
  );
}

async function fillEditor(dialog: HTMLElement) {
  fireEvent.change(within(dialog).getByLabelText("接收人"), { target: { value: "张三" } });
  fireEvent.change(within(dialog).getByLabelText("发出人"), { target: { value: "李四" } });
  fireEvent.change(within(dialog).getByLabelText("现场位置"), { target: { value: "现场 A" } });
  fireEvent.change(within(dialog).getByLabelText("第 1 行数量"), { target: { value: "2" } });
  fireEvent.change(within(dialog).getByLabelText("型号选择"), { target: { value: "1" } });
}

describe("ManualSiteIssueSection", () => {
  it("无权限时隐藏整块入口", () => {
    const { container } = render(
      <ManualSiteIssueSection
        projectId="project-1"
        canManage={false}
        issues={[manualIssue]}
        onChanged={onChanged}
        reloadIssues={reloadIssues}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("展示人工登记单并提供更正与作废入口", () => {
    renderSection();
    expect(screen.getByText("LYR-20260920-ABC")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "更正 LYR-20260920-ABC" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "作废 LYR-20260920-ABC" })).toBeInTheDocument();
  });

  it("预览实际消耗后才允许保存；提交消费冻结 payload 并带稳定幂等键", async () => {
    renderSection();
    fireEvent.click(screen.getByRole("button", { name: "人工登记领用" }));
    const dialog = await screen.findByRole("dialog", { name: "人工登记领用" });
    await fillEditor(dialog);

    fireEvent.click(within(dialog).getByRole("button", { name: "预览实际消耗" }));
    await waitFor(() => expect(api.previewManualSiteIssue).toHaveBeenCalledTimes(1));
    expect(await within(dialog).findByText("待补价格，金额留空")).toBeInTheDocument();

    fireEvent.click(await within(dialog).findByRole("button", { name: "确认登记" }));
    await waitFor(() => expect(api.createManualSiteIssue).toHaveBeenCalledTimes(1));
    const createArg = api.createManualSiteIssue.mock.calls[0][1];
    expect(createArg.lines).toEqual([
      expect.objectContaining({ part_id: 1, quantity: 2, no_return: null }),
    ]);
    expect(createArg.idempotency_key).toMatch(/^manual-site-create-/);
    expect(createArg.reason).toBe("人工登记现场领用");
    await waitFor(() => expect(reloadIssues).toHaveBeenCalled());
  });

  it("P1：改头字段（接收人）后重试不复用旧 key——完整指纹换新键", async () => {
    renderSection();
    fireEvent.click(screen.getByRole("button", { name: "人工登记领用" }));
    const dialog = await screen.findByRole("dialog", { name: "人工登记领用" });
    await fillEditor(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "预览实际消耗" }));
    await waitFor(() => expect(api.previewManualSiteIssue).toHaveBeenCalledTimes(1));
    api.createManualSiteIssue.mockRejectedValueOnce(new Error("network"));
    fireEvent.click(await within(dialog).findByRole("button", { name: "确认登记" }));
    await waitFor(() => expect(api.createManualSiteIssue).toHaveBeenCalledTimes(1));
    const firstKey = api.createManualSiteIssue.mock.calls[0][1].idempotency_key;

    // 同 payload 重试 → 同 key（防丢响应重复建单）
    fireEvent.click(await within(dialog).findByRole("button", { name: "确认登记" }));
    await waitFor(() => expect(api.createManualSiteIssue).toHaveBeenCalledTimes(2));
    expect(api.createManualSiteIssue.mock.calls[1][1].idempotency_key).toBe(firstKey);

    // 重试成功后弹窗会自动关闭；重开并改头字段（接收人换名）→ 必须是新 key
    await waitFor(() => expect(
      screen.queryByRole("dialog", { name: "人工登记领用" }),
    ).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "人工登记领用" }));
    const reopened = await screen.findByRole("dialog", { name: "人工登记领用" });
    await fillEditor(reopened);
    fireEvent.change(within(reopened).getByLabelText("接收人"), { target: { value: "王五" } });
    fireEvent.click(within(reopened).getByRole("button", { name: "预览实际消耗" }));
    await waitFor(() => expect(api.previewManualSiteIssue).toHaveBeenCalledTimes(2));
    fireEvent.click(within(reopened).getByRole("button", { name: "确认登记" }));
    await waitFor(() => expect(api.createManualSiteIssue).toHaveBeenCalledTimes(3));
    expect(api.createManualSiteIssue.mock.calls[2][1].idempotency_key).not.toBe(firstKey);
  });

  it("P1：预览后任何编辑使预览失效，必须重新预览；迟到旧预览不覆盖", async () => {
    let resolveFirst: (value: { data: typeof previewPayload }) => void = () => {};
    api.previewManualSiteIssue.mockImplementationOnce(
      () => new Promise((resolve) => { resolveFirst = resolve; }),
    );
    renderSection();
    fireEvent.click(screen.getByRole("button", { name: "人工登记领用" }));
    const dialog = await screen.findByRole("dialog", { name: "人工登记领用" });
    await fillEditor(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "预览实际消耗" }));

    // 预览在途时改数量（编辑使将到的预览失效）
    fireEvent.change(within(dialog).getByLabelText("第 1 行数量"), { target: { value: "6" } });
    resolveFirst({ data: previewPayload });
    await waitFor(() => expect(api.previewManualSiteIssue).toHaveBeenCalledTimes(1));
    // 迟到的预览不得成为可确认依据：okText 仍是「预览实际消耗」
    await waitFor(() => expect(
      within(dialog).getByRole("button", { name: "预览实际消耗" }),
    ).toBeInTheDocument());

    // 重新预览（新数量）后才可确认，且提交数量 = 6
    fireEvent.click(within(dialog).getByRole("button", { name: "预览实际消耗" }));
    await waitFor(() => expect(api.previewManualSiteIssue).toHaveBeenCalledTimes(2));
    fireEvent.click(await within(dialog).findByRole("button", { name: "确认登记" }));
    await waitFor(() => expect(api.createManualSiteIssue).toHaveBeenCalledTimes(1));
    expect(api.createManualSiteIssue.mock.calls[0][1].lines[0].quantity).toBe(6);
  });

  it("P1：预览生效后改头字段（现场位置）同样失效预览，确认按钮回到预览态", async () => {
    renderSection();
    fireEvent.click(screen.getByRole("button", { name: "人工登记领用" }));
    const dialog = await screen.findByRole("dialog", { name: "人工登记领用" });
    await fillEditor(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "预览实际消耗" }));
    await waitFor(() => expect(
      within(dialog).getByRole("button", { name: "确认登记" }),
    ).toBeInTheDocument());

    // 改头字段 → 预览失效，必须重新预览
    fireEvent.change(within(dialog).getByLabelText("现场位置"), { target: { value: "井场 C" } });
    expect(
      within(dialog).getByRole("button", { name: "预览实际消耗" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("待补价格，金额留空")).toBeNull();
  });

  it("P1：ref 同步锁防双击——预览在途时第二次点击不发第二个请求", async () => {
    api.previewManualSiteIssue.mockImplementationOnce(
      () => new Promise((resolve) => setTimeout(() => resolve({ data: previewPayload }), 50)),
    );
    renderSection();
    fireEvent.click(screen.getByRole("button", { name: "人工登记领用" }));
    const dialog = await screen.findByRole("dialog", { name: "人工登记领用" });
    await fillEditor(dialog);
    const previewButton = within(dialog).getByRole("button", { name: "预览实际消耗" });
    fireEvent.click(previewButton);
    fireEvent.click(previewButton); // 双击
    await waitFor(() => expect(api.previewManualSiteIssue).toHaveBeenCalledTimes(1));
    expect(api.previewManualSiteIssue).toHaveBeenCalledTimes(1);
  });

  it("P1：写入成功但刷新失败 → 警告刷新失败，不谎报登记失败、不重复提交", async () => {
    reloadIssues.mockRejectedValueOnce(new Error("refresh failed"));
    renderSection();
    fireEvent.click(screen.getByRole("button", { name: "人工登记领用" }));
    const dialog = await screen.findByRole("dialog", { name: "人工登记领用" });
    await fillEditor(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "预览实际消耗" }));
    await waitFor(() => expect(api.previewManualSiteIssue).toHaveBeenCalledTimes(1));
    fireEvent.click(await within(dialog).findByRole("button", { name: "确认登记" }));
    await waitFor(() => expect(api.createManualSiteIssue).toHaveBeenCalledTimes(1));
    // 弹窗已关（写成功路径），且 create 只发生一次
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "人工登记领用" })).toBeNull());
    expect(api.createManualSiteIssue).toHaveBeenCalledTimes(1);
  });

  it("更正已有单：行显式携带 issue_line_id 并带 version CAS", async () => {
    renderSection();
    fireEvent.click(screen.getByRole("button", { name: "更正 LYR-20260920-ABC" }));
    const dialog = await screen.findByRole("dialog", { name: "更正人工领用 LYR-20260920-ABC" });
    fireEvent.change(within(dialog).getByLabelText("第 1 行数量"), { target: { value: "3" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "预览实际消耗" }));
    await waitFor(() => expect(api.previewManualSiteIssue).toHaveBeenCalledTimes(1));
    fireEvent.click(within(dialog).getByRole("button", { name: "提交更正" }));
    await waitFor(() => expect(api.patchManualSiteIssue).toHaveBeenCalledTimes(1));
    const patchArg = api.patchManualSiteIssue.mock.calls[0][1];
    expect(patchArg.project_id).toBe("project-1");
    expect(patchArg.version).toBe(1);
    expect(patchArg.lines[0].issue_line_id).toBe("issue-line-m1");
    expect(patchArg.idempotency_key).toMatch(/^manual-site-patch-/);
  });

  it("作废需填原因，成功后刷新列表", async () => {
    renderSection();
    fireEvent.click(screen.getByRole("button", { name: "作废 LYR-20260920-ABC" }));
    const dialog = await screen.findByRole("dialog", { name: "作废 LYR-20260920-ABC" });
    const confirmButton = within(dialog).getByRole("button", { name: "确认作废" });
    expect(confirmButton).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText("作废原因"), { target: { value: "录错重录" } });
    expect(confirmButton).toBeEnabled();
    fireEvent.click(confirmButton);
    await waitFor(() => expect(api.voidSiteIssue).toHaveBeenCalledWith(
      "issue-manual-1",
      expect.objectContaining({ project_id: "project-1", version: 1 }),
    ));
    await waitFor(() => expect(reloadIssues).toHaveBeenCalled());
  });

  it("多行明细：添加/删除行，校验空型号拦截", async () => {
    renderSection();
    fireEvent.click(screen.getByRole("button", { name: "人工登记领用" }));
    const dialog = await screen.findByRole("dialog", { name: "人工登记领用" });
    await fillEditor(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "添加明细行" }));
    expect(within(dialog).getByLabelText("第 2 行数量")).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "预览实际消耗" }));
    await waitFor(() => expect(within(dialog).getByText("每一行都必须选择型号（PN）")).toBeInTheDocument());
    expect(api.previewManualSiteIssue).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "删除第 2 行" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "预览实际消耗" }));
    await waitFor(() => expect(api.previewManualSiteIssue).toHaveBeenCalledTimes(1));
  });

  it("P1：项目切换清空旧表单，旧项目填写不留到新项目", () => {
    const { rerender } = renderSection([manualIssue], "project-1");
    fireEvent.click(screen.getByRole("button", { name: "人工登记领用" }));
    expect(screen.getByRole("dialog", { name: "人工登记领用" })).toBeInTheDocument();

    rerender(
      <ManualSiteIssueSection
        projectId="project-2"
        canManage
        issues={[]}
        onChanged={onChanged}
        reloadIssues={reloadIssues}
      />,
    );
    // 弹窗已关、列表为新项目空态
    expect(screen.queryByRole("dialog", { name: "人工登记领用" })).toBeNull();
    expect(screen.getByText("本项目还没有页面人工登记的领用单")).toBeInTheDocument();
  });
});
