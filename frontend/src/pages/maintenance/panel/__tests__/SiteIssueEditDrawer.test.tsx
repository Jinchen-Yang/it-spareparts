import { legacyAuthorizationState, prepareLegacyPageFixture } from "../../../../test/legacyAuthorization";
vi.mock("../../../../authz/AuthzProvider", async importOriginal => ({
  ...await importOriginal<typeof import("../../../../authz/AuthzProvider")>(),
  useAuthz: legacyAuthorizationState,
}));
import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../../test/antdRender";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SiteIssueDocument } from "../../../../api/maintenanceOperations";

const mocks = vi.hoisted(() => ({
  search: vi.fn(), categories: vi.fn(), correctLine: vi.fn(), correctHeader: vi.fn(), demands: vi.fn(), partSearch: vi.fn(),
}));
vi.mock("../../../../api/search", () => ({ unifiedSearch: mocks.partSearch }));
vi.mock("../../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual<typeof import("../../../../api/maintenanceOperations")>("../../../../api/maintenanceOperations"),
  searchSiteIssues: mocks.search,
  listMaintenancePartCategories: mocks.categories,
  correctSiteIssueLine: mocks.correctLine,
  correctSiteIssueHeader: mocks.correctHeader,
  getSiteIssueDemandCandidates: mocks.demands,
}));
vi.mock("../../../../components/maintenance/WorkbookRoundTrip", () => ({ default: () => null }));
vi.mock("../../../../components/maintenance/SiteIssueWorkflowPanel", () => ({ default: () => null }));
vi.mock("../ReturnReceiptsSection", () => ({ default: () => null }));
import SiteReturnTab from "../SiteReturnTab";

const issue = (overrides: Partial<SiteIssueDocument> = {}): SiteIssueDocument => ({
  issue_id: "i1", issue_no: "ISSUE-1", display_no: "LY-20261020-0001", project_id: "p1",
  issue_date: "2026-10-20", receiver: "现场甲", issued_by: "仓库乙", site_location: "旧地址",
  version: 7, source: "workbook", workflow_status: "confirmed", total_line_count: 1,
  lines: [{
    issue_line_id: "l1", line_no: 1, version: 2, part_id: 11, pn: "PN-1", quantity: "2.000",
    serial_number: "SN-1", remark: "旧备注", demand_order_no: "WBDD-1", no_return: null,
    delivery_line_id: null, source_order_id: null, source_line_id: null,
    cost_source: null, cost_source_label: null, cost_is_estimate: false,
    cost_amount_ex_tax: null, cost_amount_inc_tax: null,
  }], ...overrides,
});

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  localStorage.setItem("permissions", JSON.stringify({ action_maintenance_site_issue_manage: true, data_purchase_cost: true }));
  localStorage.setItem("name", "合成操作员");
  mocks.categories.mockResolvedValue({ data: { categories: [] } });
  mocks.search.mockResolvedValue({ data: { total: 1, rows: [issue()] } });
  mocks.demands.mockResolvedValue({ data: { rows: [], total: 0 } });
  mocks.partSearch.mockResolvedValue({ items: [{ part_id: 22, pn_std: "PN-2", description: "替换型号" }], similar_items: [], exact: true });
  mocks.correctLine.mockResolvedValue({ data: {
    issue_id: "i1", issue_line_id: "l1", issue_version: 8, line_version: 3,
    no_return: null, changed: true, idempotent_replay: false, workbook_revision: 9,
  } });
  mocks.correctHeader.mockResolvedValue({ data: {
    issue_id: "i1", issue_version: 8, line_versions: { l1: 2 }, affected_line_count: 1,
    scope: "whole_issue", message: "单头修改影响整张领用单全部行",
    changed: true, idempotent_replay: false, workbook_revision: 9,
  } });
});
afterEach(cleanup);

async function openEditor(onChanged = vi.fn().mockResolvedValue(true)) {
  render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false}
    onChanged={onChanged} registerRefresh={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "编辑领用行" }));
  const dialog = await screen.findByRole("dialog", { name: "编辑领用事实" });
  await waitFor(() => expect(within(dialog).getByRole("textbox", { name: "行更正原因" })).toBeEnabled());
  return dialog;
}

describe("领用事实页内编辑", () => {
  it("只提交实际变化的行字段，不把单头或未触及行交给完整替换接口", async () => {
    const dialog = await openEditor();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "现场备注" }), { target: { value: "现场核实" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "行更正原因" }), { target: { value: "核对现场记录" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存行修改" }));
    await waitFor(() => expect(mocks.correctLine).toHaveBeenCalledWith("i1", "l1", {
      expected_issue_version: 7, expected_line_version: 2, changes: { remark: "现场核实" },
      reason: "核对现场记录", idempotency_key: expect.any(String),
    }, expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) })));
    expect(mocks.correctHeader).not.toHaveBeenCalled();
  });

  it.each(["line", "header"] as const)("%s 409 保留草稿，显式重读后才能重新确认，绝不使用冲突详情自动升版本", async (kind) => {
    const command = kind === "line" ? mocks.correctLine : mocks.correctHeader;
    const field = kind === "line" ? "现场备注" : "领用地址";
    const reason = kind === "line" ? "行更正原因" : "单头更正原因";
    const save = kind === "line" ? "保存行修改" : "保存单头修改";
    command.mockRejectedValueOnce({ response: { status: 409, data: { detail: {
      code: "site_issue_edit_version_conflict", message: "版本已变化", current_issue_version: 99,
    } } } });
    const dialog = await openEditor();
    fireEvent.change(within(dialog).getByRole("textbox", { name: field }), { target: { value: "我的草稿" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: reason }), { target: { value: "核对记录" } });
    fireEvent.click(within(dialog).getByRole("button", { name: save }));
    await within(dialog).findByText(/版本已变化/);
    expect(within(dialog).getByRole("textbox", { name: field })).toHaveValue("我的草稿");
    expect(within(dialog).getByRole("button", { name: save })).toBeDisabled();
    expect(command).toHaveBeenCalledTimes(1);
    const latest = issue({ version: 8, site_location: "别人已改" });
    latest.lines[0] = { ...latest.lines[0], version: 4, remark: "别人已改", serial_number: "新 SN" };
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [latest] } });
    fireEvent.click(within(dialog).getByRole("button", { name: "重新读取并保留草稿" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: save })).toBeEnabled());
    expect(mocks.search).toHaveBeenLastCalledWith(expect.objectContaining({ project_id: "p1", issue_id: "i1" }), expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) }));
    expect(within(dialog).getByRole("textbox", { name: field })).toHaveValue("我的草稿");
    fireEvent.click(within(dialog).getByRole("button", { name: save }));
    await waitFor(() => expect(command).toHaveBeenCalledTimes(2));
    const position = kind === "line" ? 2 : 1;
    expect(command.mock.calls[1][position]).toMatchObject({
      expected_issue_version: 8, ...(kind === "line" ? { expected_line_version: 4 } : {}),
      changes: kind === "line" ? { remark: "我的草稿" } : { site_location: "我的草稿" },
    });
    expect(command.mock.calls[1][position].idempotency_key).not.toBe(command.mock.calls[0][position].idempotency_key);
  });

  it("地址独立保存并明确整单影响，空字符串清空，另一区草稿在保存和重读后保留", async () => {
    const dialog = await openEditor();
    expect(within(dialog).getByText("影响整张领用单全部行")).toBeInTheDocument();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "现场备注" }), { target: { value: "尚未保存的行草稿" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "领用地址" }), { target: { value: "" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "单头更正原因" }), { target: { value: "核实地址留空" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存单头修改" }));
    await waitFor(() => expect(mocks.correctHeader).toHaveBeenCalledWith("i1", {
      expected_issue_version: 7, changes: { site_location: "" },
      reason: "核实地址留空", idempotency_key: expect.any(String),
    }, expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) })));
    expect(mocks.correctLine).not.toHaveBeenCalled();
    expect(await within(dialog).findByText("单头修改影响整张领用单全部行")).toBeInTheDocument();
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [issue({ version: 8, site_location: null })] } });
    fireEvent.click(within(dialog).getByRole("button", { name: "重新读取并保留草稿" }));
    await waitFor(() => expect(within(dialog).getByRole("textbox", { name: "现场备注" })).toBeEnabled());
    expect(within(dialog).getByRole("textbox", { name: "现场备注" })).toHaveValue("尚未保存的行草稿");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "行更正原因" }), { target: { value: "核对行" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存行修改" }));
    await waitFor(() => expect(mocks.correctLine).toHaveBeenCalledWith("i1", "l1", expect.objectContaining({
      expected_issue_version: 8, changes: { remark: "尚未保存的行草稿" },
    }), expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) })));
  });

  it.each(["line", "header"] as const)("%s 网络结果未知冻结原请求，关闭后仍可原键核对，后续拒绝不能抹掉首次未知", async (kind) => {
    const command = kind === "line" ? mocks.correctLine : mocks.correctHeader;
    command.mockRejectedValueOnce(new Error("响应丢失"))
      .mockRejectedValueOnce({ response: { status: 403, data: { detail: { message: "本次拒绝" } } } });
    const dialog = await openEditor();
    fireEvent.change(within(dialog).getByRole("textbox", { name: kind === "line" ? "现场备注" : "领用地址" }), { target: { value: "待核对请求" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: kind === "line" ? "行更正原因" : "单头更正原因" }), { target: { value: "核对事实" } });
    fireEvent.click(within(dialog).getByRole("button", { name: kind === "line" ? "保存行修改" : "保存单头修改" }));
    await within(dialog).findByText(/结果未知/);
    expect(within(dialog).getByRole("textbox", { name: "现场备注" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "重新读取并保留草稿" })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭编辑" }));
    fireEvent.click(screen.getByRole("button", { name: "继续编辑或核对领用" }));
    const restored = await screen.findByRole("dialog", { name: "编辑领用事实" });
    fireEvent.click(within(restored).getByRole("button", { name: "原样核对回执" }));
    await within(restored).findByText(/尚未核对首次结果/);
    fireEvent.click(within(restored).getByRole("button", { name: "原样核对回执" }));
    await within(restored).findByText(/保存成功/);
    expect(command).toHaveBeenCalledTimes(3);
    expect(command.mock.calls[1].slice(0, -1)).toEqual(command.mock.calls[0].slice(0, -1));
    expect(command.mock.calls[2].slice(0, -1)).toEqual(command.mock.calls[0].slice(0, -1));
  });

  it("数量按文本精度提交，SN 和需求单可显式清空，未知成本只读显示待补价格", async () => {
    const dialog = await openEditor();
    expect(within(dialog).getAllByText("待补价格").length).toBeGreaterThan(0);
    fireEvent.change(within(dialog).getByRole("textbox", { name: "领用数量" }), { target: { value: "3.125" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "SN" }), { target: { value: "" } });
    fireEvent.change(within(dialog).getByRole("combobox", { name: "关联需求单" }), { target: { value: "" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "行更正原因" }), { target: { value: "核对行字段" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存行修改" }));
    await waitFor(() => expect(mocks.correctLine).toHaveBeenCalledWith("i1", "l1", expect.objectContaining({
      changes: { quantity: "3.125", serial_number: "", demand_order_no: "" },
    }), expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) })));
  });

  it("无动作权限或非可编辑来源不显示通用编辑入口", async () => {
    const native = issue({ issue_id: "native", source: "site_issue_v2" });
    native.lines = [{ ...native.lines[0], issue_line_id: "native-line" }];
    const delivery = issue({ issue_id: "delivery" });
    delivery.lines = [{ ...delivery.lines[0], issue_line_id: "delivery-line", delivery_line_id: "d1" }];
    mocks.search.mockResolvedValue({ data: { total: 3, rows: [issue(), native, delivery] } });
    const props = { projectId: "p1", exportBase: "项目", canUpload: false,
      onChanged: vi.fn().mockResolvedValue(true), registerRefresh: vi.fn() };
    const { rerender } = render(<SiteReturnTab {...props} />);
    await screen.findByRole("button", { name: "编辑领用行" });
    expect(screen.getAllByRole("button", { name: "编辑领用行" })).toHaveLength(1);
    localStorage.setItem("permissions", JSON.stringify({ data_purchase_cost: true }));
    rerender(<SiteReturnTab {...props} />);
    expect(screen.queryByRole("button", { name: "编辑领用行" })).not.toBeInTheDocument();
  });

  it("StrictMode 的延迟整单读取可恢复，旧代次响应不覆盖最新版本", async () => {
    const pending: ((value: unknown) => void)[] = [];
    mocks.search.mockImplementation((input: { issue_id?: string }) => input.issue_id
      ? new Promise((resolve) => pending.push(resolve))
      : Promise.resolve({ data: { total: 1, rows: [issue()] } }));
    render(<StrictMode><SiteReturnTab projectId="p1" exportBase="项目" canUpload={false}
      onChanged={vi.fn().mockResolvedValue(true)} registerRefresh={vi.fn()} /></StrictMode>);
    fireEvent.click(await screen.findByRole("button", { name: "编辑领用行" }));
    const dialog = await screen.findByRole("dialog", { name: "编辑领用事实" });
    await waitFor(() => expect(pending).toHaveLength(2));
    await act(async () => pending[1]({ data: { total: 1, rows: [issue({ version: 8 })] } }));
    await act(async () => pending[0]({ data: { total: 1, rows: [issue({ version: 7 })] } }));
    expect(within(dialog).getByRole("textbox", { name: "行更正原因" })).toBeEnabled();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "现场备注" }), { target: { value: "严格模式更正" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "行更正原因" }), { target: { value: "核对事实" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存行修改" }));
    await waitFor(() => expect(mocks.correctLine).toHaveBeenCalledWith("i1", "l1", expect.objectContaining({
      expected_issue_version: 8, changes: { remark: "严格模式更正" },
    }), expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) })));
  });

  it.each(["line", "header"] as const)("%s 已成功字段不会在重读后当作草稿覆盖别人的后续更正", async (kind) => {
    const dialog = await openEditor();
    const field = kind === "line" ? "现场备注" : "领用地址";
    const reason = kind === "line" ? "行更正原因" : "单头更正原因";
    const save = kind === "line" ? "保存行修改" : "保存单头修改";
    const command = kind === "line" ? mocks.correctLine : mocks.correctHeader;
    fireEvent.change(within(dialog).getByRole("textbox", { name: field }), { target: { value: "我已保存的 B" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: reason }), { target: { value: "核对事实" } });
    fireEvent.click(within(dialog).getByRole("button", { name: save }));
    await within(dialog).findByText(/保存成功/);
    const latest = issue({ version: 9, site_location: kind === "header" ? "他人后改的 C" : "旧地址" });
    latest.lines[0] = { ...latest.lines[0], version: 4, remark: kind === "line" ? "他人后改的 C" : "旧备注" };
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [latest] } });
    fireEvent.click(within(dialog).getByRole("button", { name: "重新读取并保留草稿" }));
    await waitFor(() => expect(within(dialog).getByRole("textbox", { name: field })).toBeEnabled());
    expect(within(dialog).getByRole("textbox", { name: field })).toHaveValue("他人后改的 C");
    fireEvent.change(within(dialog).getByRole("textbox", { name: kind === "line" ? "SN" : "领用人" }), {
      target: { value: "这次的新更正" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: save }));
    await waitFor(() => expect(command).toHaveBeenCalledTimes(2));
    const payload = command.mock.calls[1][kind === "line" ? 2 : 1];
    expect(payload.changes).toEqual(kind === "line" ? { serial_number: "这次的新更正" } : { receiver: "这次的新更正" });
  });

  it("成功后刷新失败仍保留回执，重新打开不会再次发送成功请求", async () => {
    const dialog = await openEditor(vi.fn().mockRejectedValue(new Error("刷新失败")));
    fireEvent.change(within(dialog).getByRole("textbox", { name: "现场备注" }), { target: { value: "已成功" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "行更正原因" }), { target: { value: "核对记录" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存行修改" }));
    await within(dialog).findByText("修改回执已保留，但页面刷新失败；成功命令无需再次提交。");
    expect(within(dialog).getByRole("button", { name: "保存行修改" })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭编辑" }));
    fireEvent.click(screen.getByRole("button", { name: "继续编辑或核对领用" }));
    expect(await screen.findByText(/保存成功/)).toBeInTheDocument();
    expect(mocks.correctLine).toHaveBeenCalledTimes(1);
  });

  it("选 PN 成组提交稳定身份，不因唯一候选自动改需求单关联", async () => {
    mocks.demands.mockResolvedValue({ data: { rows: [{ order_no: "WBDD-OTHER" }], total: 1 } });
    const dialog = await openEditor();
    const picker = within(within(dialog).getByRole("group", { name: "PN / 备件" })).getByRole("combobox");
    fireEvent.change(picker, { target: { value: "PN-2" } });
    fireEvent.click(await screen.findByText("PN-2"));
    expect(within(dialog).getByRole("combobox", { name: "关联需求单" })).toHaveValue("WBDD-1");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "行更正原因" }), { target: { value: "更正现场备件" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存行修改" }));
    await waitFor(() => expect(mocks.correctLine).toHaveBeenCalledWith("i1", "l1", expect.objectContaining({
      changes: { part_id: 22, pn: "PN-2" },
    }), expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) })));
  });

  it("整单重读发现筛选外发货关联行时，保留当前行编辑但隐藏单头保存", async () => {
    const full = issue({ total_line_count: 2 });
    full.lines.push({ ...full.lines[0], issue_line_id: "delivery", line_no: 2, delivery_line_id: "d1" });
    mocks.search.mockImplementation(async (input: { issue_id?: string }) => ({
      data: { total: 1, rows: [input.issue_id ? full : issue()] },
    }));
    const dialog = await openEditor();
    expect(within(dialog).getByRole("textbox", { name: "现场备注" })).toBeEnabled();
    expect(within(dialog).queryByRole("button", { name: "保存单头修改" })).not.toBeInTheDocument();
    expect(within(dialog).getByText(/本单包含发货关联行/)).toBeInTheDocument();
  });

  it("登录会话变化后不以新账号重放未知命令", async () => {
    mocks.correctLine.mockRejectedValueOnce(new Error("响应丢失"));
    const dialog = await openEditor();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "现场备注" }), { target: { value: "待核对" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "行更正原因" }), { target: { value: "核对事实" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存行修改" }));
    await within(dialog).findByText(/结果未知/);
    localStorage.setItem("token", "other-account");
    fireEvent.click(within(dialog).getByRole("button", { name: "原样核对回执" }));
    await within(dialog).findByText("登录会话已变化，请重新进入项目后核对");
    expect(mocks.correctLine).toHaveBeenCalledTimes(1);
  });
});

// Explicit pre-cutover permission context for existing component behaviour tests.
beforeEach(() => prepareLegacyPageFixture({page_parts: true}));
