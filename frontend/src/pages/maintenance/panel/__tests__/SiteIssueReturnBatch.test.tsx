import { prepareLegacyPageFixture, legacyAuthorizationState } from "../../../../test/legacyAuthorization";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../../test/antdRender";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ search: vi.fn(), categories: vi.fn(), correct: vi.fn() }));
vi.mock("../../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual<typeof import("../../../../api/maintenanceOperations")>("../../../../api/maintenanceOperations"),
  searchSiteIssues: mocks.search,
  listMaintenancePartCategories: mocks.categories,
  correctSiteIssueLine: mocks.correct,
}));
vi.mock("../../../../components/maintenance/WorkbookRoundTrip", () => ({ default: () => <button>Excel 批量维护</button> }));
vi.mock("../../../../components/maintenance/SiteIssueWorkflowPanel", () => ({ default: () => <div>领用工作台内容</div> }));
vi.mock("../ReturnReceiptsSection", () => ({ default: () => <button>批量修改收货属性</button> }));
import SiteReturnTab from "../SiteReturnTab";

const issue = (id = "i1", values: (boolean | null)[] = [null, false, true]) => ({
  issue_id: id, issue_no: `ISSUE-${id}`, project_id: "p1", version: 7,
  source: "workbook", workflow_status: "confirmed",
  lines: values.map((value, index) => ({
    issue_line_id: `${id}-l${index}`, line_no: index + 1, version: index + 2,
    pn: `PN-${id}-${index}`, quantity: "1", no_return: value, delivery_line_id: null as string | null,
    return_requirement: { requirement_status: value === false ? "required" : "exempt" },
  })),
});

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  localStorage.setItem("permissions", JSON.stringify({ action_maintenance_site_issue_manage: true, data_purchase_cost: true }));
  localStorage.setItem("name", "合成操作员");
  mocks.categories.mockResolvedValue({ data: { categories: [] } });
  mocks.search.mockResolvedValue({ data: { total: 1, rows: [issue()] } });
  mocks.correct.mockImplementation(async (issueId, lineId, input) => ({ data: {
    issue_id: issueId, issue_line_id: lineId,
    issue_version: input.expected_issue_version + 1, line_version: input.expected_line_version + 1,
    no_return: input.changes.no_return, changed: true, idempotent_replay: false, workbook_revision: 9,
  } }));
});
afterEach(cleanup);

async function selectAll() {
  const list = within(screen.getByRole("region", { name: "领用明细与批量维护" }));
  await list.findByText("PN-i1-0");
  fireEvent.click(list.getByRole("checkbox", { name: "Select all" }));
}
async function confirmBatch(action = "批量设为免返") {
  fireEvent.click(screen.getByRole("button", { name: action }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByRole("button", { name: "确认修改" })).toBeDisabled();
  fireEvent.change(within(dialog).getByRole("textbox", { name: "更正原因" }), { target: { value: "合同逐行核实" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "确认修改" }));
  return dialog;
}

describe("领用行返还要求批量", () => {
  it("免返发送 true，按规则的硬盘仍显式更正；同单接续新版本、跳过未变化并展示逐行结果", async () => {
    const onChanged = vi.fn().mockResolvedValue(true);
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={onChanged} registerRefresh={vi.fn()} />);
    await selectAll();
    expect(screen.getByRole("button", { name: "批量修改收货属性" })).toBeInTheDocument();
    const dialog = await confirmBatch();
    expect(within(dialog).getByText("是否应返还：否（免返）")).toBeInTheDocument();
    await waitFor(() => expect(mocks.correct).toHaveBeenCalledTimes(2));
    expect(mocks.correct).toHaveBeenNthCalledWith(1, "i1", "i1-l0", {
      expected_issue_version: 7, expected_line_version: 2,
      changes: { no_return: true }, reason: "合同逐行核实", idempotency_key: expect.any(String),
    }, expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) }));
    expect(mocks.correct).toHaveBeenNthCalledWith(2, "i1", "i1-l1", expect.objectContaining({
      expected_issue_version: 8, expected_line_version: 3, changes: { no_return: true },
    }), expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) }));
    expect(await within(dialog).findByText("成功 2，未变化 1，冲突 0，失败 0，结果未知 0，未提交 0")).toBeInTheDocument();
    const skipped = within(dialog).getByText("PN-i1-2").closest("tr")!;
    expect(within(skipped).getByText("未变化")).toBeInTheDocument();
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(within(dialog).getByText("操作人：合成操作员（系统按登录实名记录）")).toBeInTheDocument();
  });

  it.each([
    { action: "批量设为应返", value: false, wording: "是否应返还：是（应返）" },
    { action: "恢复按规则判断", value: null, wording: "是否应返还：按规则判断" },
  ])("$action 原样发送 $value，保留三态而非直接映射是/否", async ({ action, value, wording }) => {
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [issue("i1", [true])] } });
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn().mockResolvedValue(true)} registerRefresh={vi.fn()} />);
    await selectAll();
    const dialog = await confirmBatch(action);
    expect(within(dialog).getByText(wording)).toBeInTheDocument();
    await waitFor(() => expect(mocks.correct).toHaveBeenCalledWith("i1", "i1-l0", expect.objectContaining({ changes: { no_return: value } }), expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) })));
    expect(await within(dialog).findByText("成功 1，未变化 0，冲突 0，失败 0，结果未知 0，未提交 0")).toBeInTheDocument();
  });

  it("同单前行冲突后暂停该单，其他单成功/失败独立展示，绝不采用冲突详情的新版本", async () => {
    mocks.search.mockResolvedValue({ data: { total: 3, rows: [issue("i1", [false, false]), issue("i2", [false]), issue("i3", [false])] } });
    mocks.correct.mockRejectedValueOnce({ response: { status: 409, data: { detail: { code: "site_issue_edit_version_conflict", message: "版本已变化", current_issue_version: 99 } } } })
      .mockRejectedValueOnce({ response: { status: 422, data: { detail: { message: "更正被拒绝" } } } });
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn().mockResolvedValue(true)} registerRefresh={vi.fn()} />);
    await selectAll();
    const dialog = await confirmBatch();
    expect(await within(dialog).findByText("成功 1，未变化 0，冲突 1，失败 1，结果未知 0，未提交 1")).toBeInTheDocument();
    expect(mocks.correct.mock.calls.map((call) => call[1])).toEqual(["i1-l0", "i2-l0", "i3-l0"]);
    expect(mocks.correct.mock.calls.map((call) => call[2].expected_issue_version)).toEqual([7, 7, 7]);
    expect(within(dialog).getByText("同单前行冲突或结果未知，请刷新后重新核对")).toBeInTheDocument();
  });

  it("全部原始值相同时仅显示未变化，无 HTTP 写入", async () => {
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [issue("i1", [null, null])] } });
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn().mockResolvedValue(true)} registerRefresh={vi.fn()} />);
    await selectAll();
    const dialog = await confirmBatch("恢复按规则判断");
    expect(await within(dialog).findByText("成功 0，未变化 2，冲突 0，失败 0，结果未知 0，未提交 0")).toBeInTheDocument();
    expect(mocks.correct).not.toHaveBeenCalled();
  });

  it("成功后刷新失败保留回执，关闭结果不会重发", async () => {
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [issue("i1", [false])] } });
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn().mockRejectedValue(new Error("刷新断网"))} registerRefresh={vi.fn()} />);
    await selectAll();
    const dialog = await confirmBatch();
    expect(await within(dialog).findByText("修改回执已保留，但页面刷新失败；请重新加载，成功行无需再次提交。")).toBeInTheDocument();
    expect(within(dialog).getByText("成功")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: /^关\s*闭$/ }));
    expect(mocks.correct).toHaveBeenCalledTimes(1);
  });

  it("结果未知保留首次 key 和载荷核对回执，重放成功不重发其他行", async () => {
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [issue("i1", [false, false])] } });
    mocks.correct.mockRejectedValueOnce(new Error("响应丢失"));
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn().mockResolvedValue(true)} registerRefresh={vi.fn()} />);
    await selectAll();
    const dialog = await confirmBatch();
    expect(await within(dialog).findByText("成功 0，未变化 0，冲突 0，失败 0，结果未知 1，未提交 1")).toBeInTheDocument();
    const replay = within(dialog).getByRole("button", { name: "原样核对回执" });
    await waitFor(() => expect(replay).toBeEnabled());
    fireEvent.click(replay);
    expect(await within(dialog).findByText("成功 1，未变化 0，冲突 0，失败 0，结果未知 0，未提交 1")).toBeInTheDocument();
    expect(mocks.correct).toHaveBeenCalledTimes(2);
    expect(mocks.correct.mock.calls[1].slice(0, -1)).toEqual(mocks.correct.mock.calls[0].slice(0, -1));
  });

  it("关闭未知结果后仍可恢复同键核对入口，不能用新批次抹掉未知回执", async () => {
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [issue("i1", [false])] } });
    mocks.correct.mockRejectedValueOnce(new Error("响应丢失"));
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn().mockResolvedValue(false)} registerRefresh={vi.fn()} />);
    await selectAll();
    const dialog = await confirmBatch();
    const close = within(dialog).getByRole("button", { name: /^关\s*闭$/ });
    await waitFor(() => expect(close).toBeEnabled());
    fireEvent.click(close);
    await selectAll();
    expect(screen.getByRole("button", { name: "批量设为应返" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "批量设为免返" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "恢复按规则判断" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "查看上次结果" }));
    const restored = await screen.findByRole("dialog");
    fireEvent.click(within(restored).getByRole("button", { name: "原样核对回执" }));
    await waitFor(() => expect(mocks.correct).toHaveBeenCalledTimes(2));
    expect(mocks.correct.mock.calls[1].slice(0, -1)).toEqual(mocks.correct.mock.calls[0].slice(0, -1));
  });

  it.each([403, 422])("原样核对再次收到 %s 仍保留首次结果未知", async (status) => {
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [issue("i1", [false])] } });
    mocks.correct.mockRejectedValueOnce(new Error("响应丢失"))
      .mockRejectedValueOnce({ response: { status, data: { detail: { message: "本次请求被拒绝" } } } });
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn().mockResolvedValue(true)} registerRefresh={vi.fn()} />);
    await selectAll();
    const dialog = await confirmBatch();
    const replay = await within(dialog).findByRole("button", { name: "原样核对回执" });
    await waitFor(() => expect(replay).toBeEnabled());
    fireEvent.click(replay);
    expect(await within(dialog).findByText("尚未核对首次结果，请刷新核实：本次请求被拒绝")).toBeInTheDocument();
    expect(within(dialog).getByText("成功 0，未变化 0，冲突 0，失败 0，结果未知 1，未提交 0")).toBeInTheDocument();
    expect(mocks.correct.mock.calls[1].slice(0, -1)).toEqual(mocks.correct.mock.calls[0].slice(0, -1));
  });

  it("首行等待时登录会话变化，后续行不以新账号继续提交", async () => {
    mocks.search.mockResolvedValue({ data: { total: 1, rows: [issue("i1", [false, false])] } });
    let resolveFirst!: (value: unknown) => void;
    mocks.correct.mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }));
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn().mockResolvedValue(true)} registerRefresh={vi.fn()} />);
    await selectAll();
    const dialog = await confirmBatch();
    localStorage.setItem("token", "another-session");
    await act(async () => resolveFirst({ data: { issue_version: 8, changed: true } }));
    // A completion owned by the previous session cannot become a success receipt for the new session.
    expect(within(dialog).queryByText(/成功 1/)).not.toBeInTheDocument();
    expect(within(dialog).queryByText("返还要求已保存")).not.toBeInTheDocument();
    expect(mocks.correct).toHaveBeenCalledTimes(1);
  });

  it("超过原因上限明确报错并禁用提交，不静默截断", async () => {
    render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn()} registerRefresh={vi.fn()} />);
    await selectAll();
    fireEvent.click(screen.getByRole("button", { name: "批量设为应返" }));
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByRole("textbox", { name: "更正原因" });
    fireEvent.change(input, { target: { value: "核".repeat(1001) } });
    expect(input).toHaveValue("核".repeat(1001));
    expect(within(dialog).getByText("更正原因最多 1000 个字符，请删减后提交")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确认修改" })).toBeDisabled();
    expect(mocks.correct).not.toHaveBeenCalled();
  });

  it("无动作权限隐藏批量入口，原生/草稿/发货关联行禁选", async () => {
    const native = { ...issue("native", [false]), source: "site_issue_v2" };
    const draft = { ...issue("draft", [false]), workflow_status: "draft" };
    const delivery = issue("delivery", [false]);
    delivery.lines[0].delivery_line_id = "delivery-id";
    mocks.search.mockResolvedValue({ data: { total: 4, rows: [issue("i1", [false]), native, draft, delivery] } });
    const { rerender } = render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn()} registerRefresh={vi.fn()} />);
    await screen.findByText("PN-i1-0");
    for (const pn of ["PN-native-0", "PN-draft-0", "PN-delivery-0"]) {
      expect(within(screen.getByText(pn).closest("tr")!).getByRole("checkbox")).toBeDisabled();
    }
    expect(screen.getByRole("button", { name: "批量设为应返" })).toBeDisabled();
    localStorage.setItem("permissions", "{}");
    rerender(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={vi.fn()} registerRefresh={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "批量设为应返" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("第一行完成前不发第二行，切项目后不继续旧批次", async () => {
    let resolveFirst!: (value: unknown) => void;
    mocks.correct.mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }));
    const refresh = vi.fn().mockResolvedValue(true);
    const { rerender } = render(<SiteReturnTab projectId="p1" exportBase="项目" canUpload={false} onChanged={refresh} registerRefresh={vi.fn()} />);
    await selectAll();
    await confirmBatch();
    expect(mocks.correct).toHaveBeenCalledTimes(1);
    mocks.search.mockResolvedValue({ data: { total: 0, rows: [] } });
    rerender(<SiteReturnTab projectId="p2" exportBase="项目" canUpload={false} onChanged={refresh} registerRefresh={vi.fn()} />);
    await act(async () => resolveFirst({ data: { issue_version: 8, changed: true } }));
    expect(mocks.correct).toHaveBeenCalledTimes(1);
    expect(refresh).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

vi.mock("../../../../authz/AuthzProvider", async original => ({
  ...await original<typeof import("../../../../authz/AuthzProvider")>(), useAuthz: () => legacyAuthorizationState(),
}));
beforeEach(() => prepareLegacyPageFixture());
