import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({
  plan: vi.fn(),
  workspace: vi.fn(),
  create: vi.fn(),
  patch: vi.fn(),
}));
vi.mock("../../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual<typeof import("../../../../api/maintenanceOperations")>("../../../../api/maintenanceOperations"),
  getMaintenanceProjectWorkspace: mocks.workspace,
  createProjectCollection: mocks.create,
  patchProjectCollection: mocks.patch,
}));
vi.mock("../../../../api/maintenanceWorkbooks", async () => ({
  ...await vi.importActual<typeof import("../../../../api/maintenanceWorkbooks")>("../../../../api/maintenanceWorkbooks"),
  // 组件用真实 getCollectionPlan（内部已解包 {total, rows}），必须 mock 该模块。
  getCollectionPlan: mocks.plan,
  downloadProjectMaster: vi.fn(),
  validateProjectMaster: vi.fn(),
  applyProjectMaster: vi.fn(),
}));
vi.mock("../../../../components/maintenance/WorkbookRoundTrip", () => ({ default: () => null }));
import CollectionTab from "../CollectionTab";
import type { MaintenanceCollectionSnapshotRow } from "../../../../api/maintenanceOperations";

const snapshot = (over: Partial<MaintenanceCollectionSnapshotRow> = {}): MaintenanceCollectionSnapshotRow => ({
  collection_id: "c1",
  project_contract_id: "pc-1",
  contract_no: "HT-2026-001",
  report_month: "2026-08-01",
  cumulative_amount: 1000,
  receipt_reference: "PZ-1",
  status: "confirmed",
  remark: null,
  version: 3,
  ...over,
});

// getMaintenanceProjectWorkspace 的 axios 形状：{ data: { project: { contracts } } }。
const contractsWorkspace = (rows: unknown[]) => ({
  data: {
    project: { contracts: rows },
    collection_snapshots: { rows: [], total: 0, page: 1, page_size: 1 },
    requisitions: { rows: [], total: 0, page: 1, page_size: 1 },
    approved_expenses: { rows: [], total: 0, page: 1, page_size: 1 },
    reminders: [],
    workbook_preview: { protocol_version: "2.0", sheets: [], latest_tracking_month: null, last_exported_at: null, data_version: "v0" },
    as_of: "2026-09-20",
    data_version: "v0",
  },
});

const contract = {
  project_contract_id: "pc-1",
  contract_id: "ct-1",
  contract_no: "HT-2026-001",
  contract_amount: 1000,
  contract_amount_basis: "inc_tax",
  contract_status: "已生效",
  status_mapping_state: "mapped",
  included_in_total: true,
  is_effective: true,
  amount_status: "available",
  received_amount: 0,
};

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function grantManage() {
  localStorage.setItem("permissions", JSON.stringify({
    action_maintenance_roundtrip_apply: true,
    data_profit: true,
  }));
}

const props = (over: { rows?: MaintenanceCollectionSnapshotRow[]; projectId?: string; onRefresh?: () => Promise<boolean> } = {}) => ({
  projectId: over.projectId ?? "p1",
  exportBase: "项目",
  canUpload: false,
  rows: over.rows ?? [],
  loading: false,
  onRefresh: over.onRefresh ?? vi.fn().mockResolvedValue(true),
  registerRefresh: vi.fn(),
});

// AntD 两个中文字按钮的 accessible name 常插入空格（作 废 / 恢 复），统一走正则。
const btn = (name: string) => ({ name: new RegExp(`^${name.split("").join("\\s*")}$`) });

/** 打开合同下拉并点选一个选项（未展开的 Select 选项不在 DOM 里，findByTitle 等 渲染）。 */
async function pickContract(dialog: HTMLElement, title: string) {
  fireEvent.mouseDown(within(dialog).getByLabelText("关联合同（当前项目内选择）"));
  fireEvent.click(await screen.findByTitle(title));
}

function fillCreate(dialog: HTMLElement, over: { month?: string; amount?: string; reason?: string } = {}) {
  fireEvent.change(within(dialog).getByLabelText("报告月份（YYYY-MM）"), { target: { value: over.month ?? "2026-09" } });
  fireEvent.change(within(dialog).getByLabelText("累计实收金额（含税）"), { target: { value: over.amount ?? "500" } });
  fireEvent.change(within(dialog).getByLabelText(/登记原因/), { target: { value: over.reason ?? "补录 9 月回款" } });
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.plan.mockResolvedValue({ total: 0, rows: [] });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("回款页面登记的合同选择", () => {
  it("登记时合同从 workspace 下拉选择而非手输 UUID", async () => {
    grantManage();
    mocks.workspace.mockResolvedValue(contractsWorkspace([contract]));
    render(<CollectionTab {...props()} />);
    fireEvent.click(await screen.findByRole("button", btn("登记回款")));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(mocks.workspace).toHaveBeenCalledWith("p1", expect.anything()));
    await pickContract(dialog, "HT-2026-001");
    fillCreate(dialog);
    fireEvent.click(within(dialog).getByRole("button", btn("登记")));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledWith("p1", expect.objectContaining({
      project_contract_id: "pc-1",
      report_month: "2026-09-01",
      cumulative_amount: 500,
      reason: "补录 9 月回款",
    })));
  });

  it("当前项目无合同关系时给空状态说明，未选合同不给提交", async () => {
    grantManage();
    mocks.workspace.mockResolvedValue(contractsWorkspace([]));
    render(<CollectionTab {...props()} />);
    fireEvent.click(await screen.findByRole("button", btn("登记回款")));
    expect(await screen.findByText("当前项目暂无合同关系")).toBeInTheDocument();
    const dialog = screen.getByRole("dialog");
    fillCreate(dialog);
    fireEvent.click(within(dialog).getByRole("button", btn("登记")));
    await waitFor(() => expect(within(dialog).getByText("请选择合同")).toBeInTheDocument());
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("合同读取失败显示原因与重试入口，重试成功后可选合同", async () => {
    grantManage();
    mocks.workspace.mockRejectedValueOnce(new Error("down"));
    render(<CollectionTab {...props()} />);
    fireEvent.click(await screen.findByRole("button", btn("登记回款")));
    expect(await screen.findByText("合同列表加载失败")).toBeInTheDocument();
    mocks.workspace.mockResolvedValue(contractsWorkspace([contract]));
    fireEvent.click(await screen.findByRole("button", btn("重试")));
    const dialog = screen.getByRole("dialog");
    await pickContract(dialog, "HT-2026-001");
  });

  it("权限关闭时不渲染操作列也不触发合同读取——不多读敏感信息", async () => {
    localStorage.setItem("permissions", JSON.stringify({ data_profit: true }));
    render(<CollectionTab {...props({ rows: [snapshot()] })} />);
    await screen.findByText("HT-2026-001");
    expect(screen.queryByRole("button", btn("登记回款"))).not.toBeInTheDocument();
    expect(screen.queryByRole("button", btn("修改"))).not.toBeInTheDocument();
    expect(screen.queryByRole("button", btn("作废"))).not.toBeInTheDocument();
    expect(screen.queryByRole("button", btn("批量登记"))).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /批\s*量\s*修\s*改/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /批\s*量\s*作\s*废/ })).not.toBeInTheDocument();
    expect(mocks.workspace).not.toHaveBeenCalled();
  });

  it("非法月份（2026-13）不发给后端，create 上屏校验错误", async () => {
    grantManage();
    mocks.workspace.mockResolvedValue(contractsWorkspace([contract]));
    render(<CollectionTab {...props()} />);
    fireEvent.click(await screen.findByRole("button", btn("登记回款")));
    const dialog = await screen.findByRole("dialog");
    await pickContract(dialog, "HT-2026-001");
    fillCreate(dialog, { month: "2026-13" });
    fireEvent.click(within(dialog).getByRole("button", btn("登记")));
    await waitFor(() => expect(within(dialog).getByText("报告月份格式须为 YYYY-MM（且为真实月份）")).toBeInTheDocument());
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("edit 路径同样拦截非法月份", async () => {
    grantManage();
    render(<CollectionTab {...props({ rows: [snapshot()] })} />);
    fireEvent.click(await screen.findByRole("button", btn("修改")));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("报告月份（YYYY-MM）"), { target: { value: "2026-13" } });
    fireEvent.change(within(dialog).getByLabelText(/修改原因/), { target: { value: "核对修正" } });
    fireEvent.click(within(dialog).getByRole("button", btn("保存修改")));
    await waitFor(() => expect(within(dialog).getByText("报告月份格式须为 YYYY-MM（且为真实月份）")).toBeInTheDocument());
    expect(mocks.patch).not.toHaveBeenCalled();
  });
});

describe("作废与恢复动作", () => {
  it("作废要求原因并带版本走 PATCH status=void；缺原因不提交", async () => {
    grantManage();
    render(<CollectionTab {...props({ rows: [snapshot()] })} />);
    fireEvent.click(await screen.findByRole("button", btn("作废")));
    const dialog = await screen.findByRole("dialog");
    expect(await screen.findByText(/软作废/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", btn("确认作废")));
    await waitFor(() => expect(within(dialog).getByText("必须填写原因")).toBeInTheDocument());
    expect(mocks.patch).not.toHaveBeenCalled();
    fireEvent.change(within(dialog).getByLabelText(/作废原因/), { target: { value: "银行冲正" } });
    fireEvent.click(within(dialog).getByRole("button", btn("确认作废")));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledWith("c1", expect.objectContaining({
      version: 3,
      reason: "银行冲正",
      status: "void",
    })));
  });

  it("void 行有恢复入口，恢复默认回待确认并带版本与原因", async () => {
    grantManage();
    render(<CollectionTab {...props({ rows: [snapshot({ status: "void", cumulative_amount: 800 })] })} />);
    fireEvent.click(await screen.findByRole("button", btn("恢复")));
    const dialog = await screen.findByRole("dialog");
    expect(await screen.findByText(/待确认/)).toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText(/恢复原因/), { target: { value: "误作废" } });
    fireEvent.click(within(dialog).getByRole("button", btn("确认恢复")));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledWith("c1", expect.objectContaining({
      version: 3,
      reason: "误作废",
      status: "unconfirmed",
    })));
  });

  it("作废失败时错误上屏且表单值不丢，可直接重试", async () => {
    grantManage();
    // axios 形状错误：readError 透传 detail，页面展示服务端原因而非笼统 fallback
    mocks.patch.mockRejectedValueOnce({ response: { data: { detail: "回款快照已变化（当前版本 4），请刷新后重试" } } });
    render(<CollectionTab {...props({ rows: [snapshot()] })} />);
    fireEvent.click(await screen.findByRole("button", btn("作废")));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/作废原因/), { target: { value: "银行冲正" } });
    fireEvent.click(within(dialog).getByRole("button", btn("确认作废")));
    expect(await screen.findByText("回款快照已变化（当前版本 4），请刷新后重试")).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/作废原因/)).toHaveValue("银行冲正");
    mocks.patch.mockResolvedValueOnce({ data: snapshot({ status: "void" }) });
    fireEvent.click(within(dialog).getByRole("button", btn("确认作废")));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(2));
  });

  it("修改对话框预填行值，保存时带 OCC 版本", async () => {
    grantManage();
    render(<CollectionTab {...props({ rows: [snapshot()] })} />);
    fireEvent.click(await screen.findByRole("button", btn("修改")));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("报告月份（YYYY-MM）")).toHaveValue("2026-08");
    fireEvent.change(within(dialog).getByLabelText("累计实收金额（含税）"), { target: { value: "1200" } });
    fireEvent.change(within(dialog).getByLabelText(/修改原因/), { target: { value: "核对回单修正" } });
    fireEvent.click(within(dialog).getByRole("button", btn("保存修改")));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledWith("c1", expect.objectContaining({
      version: 3,
      reason: "核对回单修正",
      cumulative_amount: 1200,
    })));
  });
});

describe("快速切项目与在途提交隔离", () => {
  it("A 项目 POST pending 时切 B 开新表单：A resolve 不关 B 的对话框、不触发刷新", async () => {
    grantManage();
    const slowCreate = deferred<{ data: MaintenanceCollectionSnapshotRow }>();
    mocks.workspace.mockResolvedValue(contractsWorkspace([contract]));
    const refreshA = vi.fn().mockResolvedValue(true);
    const refreshB = vi.fn().mockResolvedValue(true);
    mocks.create.mockImplementation((id: string) => id === "p1" ? slowCreate.promise : Promise.resolve({ data: snapshot() }));
    const base = { exportBase: "项目", canUpload: false, rows: [] as MaintenanceCollectionSnapshotRow[], loading: false, registerRefresh: vi.fn() };
    const { rerender } = render(
      <CollectionTab projectId="p1" {...base} onRefresh={refreshA} />,
    );
    fireEvent.click(await screen.findByRole("button", btn("登记回款")));
    const dialogA = await screen.findByRole("dialog");
    await pickContract(dialogA, "HT-2026-001");
    fillCreate(dialogA);
    fireEvent.click(within(dialogA).getByRole("button", btn("登记")));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledWith("p1", expect.objectContaining({ report_month: "2026-09-01" })));
    // A 写请求在途：切到 B 并打开新登记表单
    rerender(<CollectionTab projectId="p2" {...base} onRefresh={refreshB} />);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fireEvent.click(await screen.findByRole("button", btn("登记回款")));
    const dialogB = await screen.findByRole("dialog");
    await waitFor(() => expect(mocks.workspace).toHaveBeenCalledWith("p2", expect.anything()));
    await pickContract(dialogB, "HT-2026-001");
    // A 的写请求此刻才完成：不得关掉 B 的表单、不得触发任何项目的刷新链。
    // （成功提示不做 DOM 断言：AntD message 挂 body，跨测试 cleanup 会残留。）
    await act(async () => { await slowCreate.resolve({ data: snapshot() }); });
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(refreshA).not.toHaveBeenCalled();
    expect(refreshB).not.toHaveBeenCalled();
    // B 的表单仍可正常提交（A 的 finally 没有把 B 的 submitting 或表单搞坏）
    fillCreate(dialogB);
    fireEvent.click(within(dialogB).getByRole("button", btn("登记")));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledWith("p2", expect.anything()));
    // B 成功后走 B 的刷新闭包（不是 A 的）
    await waitFor(() => expect(refreshB).toHaveBeenCalled());
    expect(refreshA).not.toHaveBeenCalled();
  });

  it("旧项目的慢合同响应不落进新项目的下拉", async () => {
    grantManage();
    const slowWorkspace = deferred<ReturnType<typeof contractsWorkspace>>();
    mocks.workspace.mockImplementation((id: string) =>
      id === "p1" ? slowWorkspace.promise : Promise.resolve(contractsWorkspace([{ ...contract, contract_no: "HT-B-002" }])));
    const base = { exportBase: "项目", canUpload: false, rows: [], loading: false, onRefresh: vi.fn().mockResolvedValue(true), registerRefresh: vi.fn() };
    const { rerender } = render(<CollectionTab projectId="p1" {...base} />);
    fireEvent.click(await screen.findByRole("button", btn("登记回款")));
    await screen.findByRole("dialog");
    rerender(<CollectionTab projectId="p2" {...base} />);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fireEvent.click(await screen.findByRole("button", btn("登记回款")));
    const dialogB = await screen.findByRole("dialog");
    // B 的合同加载完成后展开下拉即见 B 的合同
    await pickContract(dialogB, "HT-B-002");
    await act(async () => {
      await slowWorkspace.resolve(contractsWorkspace([{ ...contract, contract_no: "HT-A-OLD" }]));
    });
    // A 的迟到合同不得覆盖 B 的选择器：重新展开验证仍是 B 的合同
    fireEvent.mouseDown(within(dialogB).getByLabelText("关联合同（当前项目内选择）"));
    await waitFor(() => expect(screen.getAllByTitle("HT-B-002").length).toBeGreaterThan(0));
    expect(screen.queryByTitle("HT-A-OLD")).not.toBeInTheDocument();
  });
});

describe("批量入口与列表勾选接线", () => {
  it("有效行可勾选并传给批量修改；已作废行选择框禁用", async () => {
    grantManage();
    const active = snapshot({ collection_id: "active-1", contract_no: "HT-ACTIVE" });
    const voided = snapshot({
      collection_id: "void-1",
      contract_no: "HT-VOID",
      report_month: "2026-07-01",
      status: "void",
    });
    render(<CollectionTab {...props({ rows: [active, voided] })} />);

    const modify = await screen.findByRole("button", { name: /批\s*量\s*修\s*改/ });
    expect(modify).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /已作废记录 HT-VOID 不可选择/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /选择回款记录 HT-ACTIVE 2026-08/ }));
    expect(await screen.findByRole("button", { name: /批\s*量\s*修\s*改.*1/ })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: /批\s*量\s*修\s*改.*1/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("HT-ACTIVE")).toBeInTheDocument();
    expect(within(dialog).getByText("v3")).toBeInTheDocument();
  });
});
