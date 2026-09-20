import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MaintenanceCollectionSnapshotRow,
  MaintenanceContractSummary,
} from "../../../../api/maintenanceOperations";

const mocks = vi.hoisted(() => ({
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

import CollectionBatchMaintenance, {
  diffPatchUpdates,
} from "../CollectionBatchMaintenance";

const contract = (over: Partial<MaintenanceContractSummary> = {}): MaintenanceContractSummary => ({
  project_contract_id: "pc-1",
  contract_id: "ct-1",
  contract_no: "HT-1",
  contract_amount: 1000,
  contract_amount_basis: "inc_tax",
  contract_status: "已生效",
  status_mapping_state: "mapped",
  included_in_total: true,
  is_effective: true,
  amount_status: "available",
  received_amount: 0,
  ...over,
});

const snapshot = (over: Partial<MaintenanceCollectionSnapshotRow> = {}): MaintenanceCollectionSnapshotRow => ({
  collection_id: "c-1",
  project_contract_id: "pc-1",
  contract_no: "HT-1",
  report_month: "2026-08-01",
  cumulative_amount: 100,
  receipt_reference: "R-1",
  status: "confirmed",
  remark: "原备注",
  version: 3,
  ...over,
});

function workspace(
  contracts: MaintenanceContractSummary[],
  rows: MaintenanceCollectionSnapshotRow[] = [],
  total = rows.length,
  page = 1,
) {
  return {
    data: {
      project: { contracts },
      collection_snapshots: { rows, total, page, page_size: 100 },
      requisitions: { rows: [], total: 0, page: 1, page_size: 1 },
      approved_expenses: { rows: [], total: 0, page: 1, page_size: 1 },
      reminders: [],
      workbook_preview: {},
      as_of: "2026-09-20",
      data_version: "v1",
    },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

async function selectContract(row: number, title: string) {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: `第${row}行合同` }));
  const options = await screen.findAllByTitle(title);
  fireEvent.click(options[options.length - 1]);
}

async function fillCreateRow(row: number, title: string, month: string, amount: string) {
  await selectContract(row, title);
  fireEvent.change(screen.getByLabelText(`第${row}行月份`), { target: { value: month } });
  fireEvent.change(screen.getByLabelText(`第${row}行累计金额`), { target: { value: amount } });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.create.mockResolvedValue({ data: snapshot() });
  mocks.patch.mockResolvedValue({ data: snapshot() });
  mocks.workspace.mockResolvedValue(workspace([contract()]));
});
afterEach(() => cleanup());

describe("回款批量 helper", () => {
  it("diff 只处理显式提供字段：undefined 保持原值，null/空串才清空", () => {
    const base = snapshot();
    expect(diffPatchUpdates(base, { cumulative_amount: 120 })).toEqual({ cumulative_amount: 120 });
    expect(diffPatchUpdates(base, { receipt_reference: undefined, remark: undefined })).toEqual({});
    expect(diffPatchUpdates(base, { receipt_reference: null, remark: "" })).toEqual({
      receipt_reference: null,
      remark: null,
    });
  });
});

describe("批量登记真实交互", () => {
  it("两行分别冻结合同、月份和累计金额，Create 不伪造 idempotency_key", async () => {
    const onRefresh = vi.fn().mockResolvedValue(true);
    render(<CollectionBatchMaintenance projectId="p-1" selectedRows={[]} onRefresh={onRefresh} />);
    fireEvent.click(screen.getByRole("button", { name: /批\s*量\s*登\s*记/ }));
    await fillCreateRow(1, "HT-1", "2026-08", "100");
    fireEvent.click(screen.getByRole("button", { name: /新\s*增\s*一\s*行/ }));
    mocks.workspace.mockResolvedValue(workspace([
      contract(),
      contract({ project_contract_id: "pc-2", contract_id: "ct-2", contract_no: "HT-2" }),
    ]));
    // 合同来自打开弹窗时的当前项目读回；重新打开不是创建新请求，因此直接补入第二合同不可取。
    // 先关闭无提交草稿，再重开，让当前项目候选重新读取。
    fireEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
    fireEvent.click(screen.getByRole("button", { name: /批\s*量\s*登\s*记/ }));
    await waitFor(() => expect(mocks.workspace).toHaveBeenCalledTimes(2));
    await fillCreateRow(1, "HT-1", "2026-08", "100");
    fireEvent.click(screen.getByRole("button", { name: /新\s*增\s*一\s*行/ }));
    await fillCreateRow(2, "HT-2", "2026-09", "275.5");
    fireEvent.change(screen.getByLabelText("共同操作原因"), { target: { value: "银行回单补录" } });
    fireEvent.click(screen.getByRole("button", { name: /登\s*记 2 条/ }));

    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    expect(mocks.create.mock.calls[0][1]).toEqual({
      project_contract_id: "pc-1",
      report_month: "2026-08-01",
      cumulative_amount: 100,
      status: "unconfirmed",
      receipt_reference: null,
      remark: null,
      reason: "银行回单补录",
    });
    expect(mocks.create.mock.calls[1][1]).toEqual(expect.objectContaining({
      project_contract_id: "pc-2",
      report_month: "2026-09-01",
      cumulative_amount: 275.5,
    }));
    expect(mocks.create.mock.calls.some((call) => "idempotency_key" in call[1])).toBe(false);
  });

  it.each([
    ["0000-01", "10", /月份须为真实/],
    ["2026-13", "10", /月份须为真实/],
    ["2026-08", "1000000000000", /金额须小于 1e12/],
  ])("非法月份/金额不发请求：%s / %s", async (month, amount, expected) => {
    render(<CollectionBatchMaintenance projectId="p-1" selectedRows={[]} onRefresh={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /批\s*量\s*登\s*记/ }));
    await fillCreateRow(1, "HT-1", month, amount);
    fireEvent.change(screen.getByLabelText("共同操作原因"), { target: { value: "补录" } });
    fireEvent.click(screen.getByRole("button", { name: /登\s*记 1 条/ }));
    expect(await screen.findByText(expected)).toBeInTheDocument();
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("network unknown 后只复用原 payload；409 通过完整分页核对存在，不自动 PATCH", async () => {
    const target = snapshot({
      collection_id: "existing",
      project_contract_id: "pc-1",
      report_month: "2026-10-01",
      cumulative_amount: 321,
      receipt_reference: null,
      remark: null,
      status: "unconfirmed",
    });
    mocks.create
      .mockRejectedValueOnce(new Error("timeout"))
      .mockRejectedValueOnce({ response: { status: 409, data: { detail: "duplicate" } } });
    mocks.workspace.mockImplementation((_projectId: string, params: { collection_page?: number }) => {
      if (!params.collection_page) return Promise.resolve(workspace([contract()]));
      if (params.collection_page === 1) {
        return Promise.resolve(workspace([], Array.from({ length: 100 }, (_, i) => snapshot({ collection_id: `other-${i}`, project_contract_id: "other" })), 101, 1));
      }
      return Promise.resolve(workspace([], [target], 101, 2));
    });
    render(<CollectionBatchMaintenance projectId="p-1" selectedRows={[]} onRefresh={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /批\s*量\s*登\s*记/ }));
    await fillCreateRow(1, "HT-1", "2026-10", "321");
    fireEvent.change(screen.getByLabelText("共同操作原因"), { target: { value: "补录" } });
    fireEvent.click(screen.getByRole("button", { name: /登\s*记 1 条/ }));
    expect(await screen.findByText("结果未知")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /新\s*批\s*次/ })).toBeDisabled();
    const firstPayload = mocks.create.mock.calls[0][1];

    fireEvent.click(screen.getByRole("button", { name: /重\s*试 1 条/ }));
    expect(await screen.findByText("已核对当前值")).toBeInTheDocument();
    expect(mocks.create.mock.calls[1][1]).toEqual(firstPayload);
    expect("idempotency_key" in firstPayload).toBe(false);
    expect(mocks.workspace).toHaveBeenCalledWith("p-1", expect.objectContaining({ collection_page: 2 }));
    expect(mocks.patch).not.toHaveBeenCalled();
  });

  it("HTTP 500 后为 unknown；随后 403 只拒绝重试，仍保留原 payload 与新批次锁", async () => {
    mocks.create
      .mockRejectedValueOnce({ response: { status: 500 } })
      .mockRejectedValueOnce({ response: { status: 403, data: { detail: "forbidden" } } });
    render(<CollectionBatchMaintenance projectId="p-1" selectedRows={[]} onRefresh={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /批\s*量\s*登\s*记/ }));
    await fillCreateRow(1, "HT-1", "2026-10", "88");
    fireEvent.change(screen.getByLabelText("共同操作原因"), { target: { value: "补录" } });
    fireEvent.click(screen.getByRole("button", { name: /登\s*记 1 条/ }));
    expect(await screen.findByText("结果未知")).toBeInTheDocument();
    const payload = mocks.create.mock.calls[0][1];
    fireEvent.click(screen.getByRole("button", { name: /重\s*试 1 条/ }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    expect(mocks.create.mock.calls[1][1]).toEqual(payload);
    expect(screen.getByText("结果未知")).toBeInTheDocument();
    expect(screen.getByText(/此前请求结果仍未知/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /新\s*批\s*次/ })).toBeDisabled();
  });
});

describe("批量修改与作废", () => {
  it("两条不同 version 中只发送真正变化行，并把空凭据/备注显式发 null", async () => {
    const rows = [
      snapshot({ collection_id: "c-1", version: 3, cumulative_amount: 100 }),
      snapshot({ collection_id: "c-2", version: 8, project_contract_id: "pc-2", contract_no: "HT-2", cumulative_amount: 200 }),
    ];
    render(<CollectionBatchMaintenance projectId="p-1" selectedRows={rows} onRefresh={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /批\s*量\s*修\s*改/ }));
    fireEvent.change(screen.getByLabelText("第1行修改金额"), { target: { value: "150" } });
    fireEvent.change(screen.getByLabelText("第1行修改凭据"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("第1行修改备注"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("共同操作原因"), { target: { value: "核对回单" } });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存\s*批\s*量\s*修\s*改/ }));

    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    expect(mocks.patch).toHaveBeenCalledWith("c-1", {
      version: 3,
      reason: "核对回单",
      cumulative_amount: 150,
      receipt_reference: null,
      remark: null,
    });
  });

  it("共同作废原因必填；两行分别携带原 version，部分成功与 409 不升级版本重放", async () => {
    const rows = [snapshot({ collection_id: "c-1", version: 2 }), snapshot({ collection_id: "c-2", version: 9 })];
    mocks.patch
      .mockResolvedValueOnce({ data: rows[0] })
      .mockRejectedValueOnce({ response: { status: 409, data: { detail: "version conflict" } } });
    mocks.workspace.mockResolvedValue(workspace([], [rows[1]], 1));
    render(<CollectionBatchMaintenance projectId="p-1" selectedRows={rows} onRefresh={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /批\s*量\s*作\s*废/ }));
    fireEvent.click(screen.getByRole("button", { name: /确\s*认\s*批\s*量\s*作\s*废/ }));
    expect(await screen.findByText("共同作废原因必填")).toBeInTheDocument();
    expect(mocks.patch).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("共同作废原因"), { target: { value: "银行冲正" } });
    fireEvent.click(screen.getByRole("button", { name: /确\s*认\s*批\s*量\s*作\s*废/ }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(2));
    expect(mocks.patch.mock.calls.map((call) => call[1].version)).toEqual([2, 9]);
    expect(mocks.patch.mock.calls.every((call) => call[1].status === "void")).toBe(true);
    expect(await screen.findByText("冲突待人工核对")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /重\s*试/ })).not.toBeInTheDocument();
  });
});

describe("同步锁、项目隔离与父刷新", () => {
  it("双击只发一轮；切项目后旧请求不续发、不触发父刷新", async () => {
    const slow = deferred<{ data: MaintenanceCollectionSnapshotRow }>();
    mocks.create.mockReturnValueOnce(slow.promise);
    const refreshA = vi.fn().mockResolvedValue(true);
    const refreshB = vi.fn().mockResolvedValue(true);
    const { rerender } = render(<CollectionBatchMaintenance projectId="p-1" selectedRows={[]} onRefresh={refreshA} />);
    fireEvent.click(screen.getByRole("button", { name: /批\s*量\s*登\s*记/ }));
    await fillCreateRow(1, "HT-1", "2026-11", "10");
    fireEvent.change(screen.getByLabelText("共同操作原因"), { target: { value: "补录" } });
    const submit = screen.getByRole("button", { name: /登\s*记 1 条/ });
    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));

    rerender(<CollectionBatchMaintenance projectId="p-2" selectedRows={[]} onRefresh={refreshB} />);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await act(async () => { slow.resolve({ data: snapshot() }); });
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(mocks.create).toHaveBeenCalledTimes(1);
    expect(refreshA).not.toHaveBeenCalled();
    expect(refreshB).not.toHaveBeenCalled();
  });

  it("成功后关闭只刷新父列表，不重复写；刷新 false 仍不鼓励重新登记", async () => {
    const onRefresh = vi.fn().mockResolvedValue(false);
    render(<CollectionBatchMaintenance projectId="p-1" selectedRows={[]} onRefresh={onRefresh} />);
    fireEvent.click(screen.getByRole("button", { name: /批\s*量\s*登\s*记/ }));
    await fillCreateRow(1, "HT-1", "2026-12", "10");
    fireEvent.change(screen.getByLabelText("共同操作原因"), { target: { value: "补录" } });
    fireEvent.click(screen.getByRole("button", { name: /登\s*记 1 条/ }));
    expect((await screen.findAllByText("已写入")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /关\s*闭/ }));
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
    expect(mocks.create).toHaveBeenCalledTimes(1);
  });
});
