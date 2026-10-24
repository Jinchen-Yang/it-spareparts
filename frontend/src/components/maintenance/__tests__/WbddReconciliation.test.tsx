import { legacyAuthorizationState, prepareLegacyPageFixture } from "../../../test/legacyAuthorization";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WbddMissing } from "../../../api/maintenanceWbddImport";

const reconcile = vi.hoisted(() => vi.fn());
vi.mock("../../../api/maintenanceWbddImport", async () => ({
  ...await vi.importActual<typeof import("../../../api/maintenanceWbddImport")>("../../../api/maintenanceWbddImport"),
  reconcileWbdd: reconcile,
}));
import WbddReconciliation from "../WbddReconciliation";

function fixture(): WbddMissing {
  return { readiness: "ready", batch_id: 8, uploaded_at: null, missing_count: 1,
    missing_line_count: 1, total_candidate_count: 2, truncated: false, candidate_digest: "digest-8",
    coverage: { source_order_count: 4, blocked_order_count: 0,
      daily_windows: [{ date: "2026-06-09", from_sequence: 1, to_sequence: 3 }, { date: "2026-08-09", from_sequence: 2, to_sequence: 6 }] },
    missing_orders: [{ source_order_id: "private-source-id", order_no: "WBDD-20260609-0002", order_date: "2026-06-09", line_count: 3, assigned_project_id: "private-project-id", project_name: "项目甲", linked_site_line_count: 2 }],
    missing_lines: [{ line_id: 12, source_order_id: "private-other-id", order_no: "WBDD-20260809-0004", pn: "PN-X", qty: "1.5", project_name: "项目乙", linked_site_line_count: 1 }],
    warnings: [],
  };
}
const props = () => ({ data: fixture(), loading: false, canReconcile: true,
  onRefresh: vi.fn().mockResolvedValue(true), onCommitted: vi.fn().mockResolvedValue(true) });
function selectBoth() {
  for (const label of ["WBDD-20260609-0002", "PN-X"]) {
    fireEvent.click(within(screen.getByText(label).closest("tr")!).getByRole("checkbox"));
  }
}
async function begin() {
  selectBoth();
  fireEvent.click(screen.getByRole("button", { name: /^确认同步/ }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByRole("button", { name: "确认同步" })).toBeDisabled();
  fireEvent.change(within(dialog).getByRole("textbox", { name: "同步原因" }), { target: { value: "核对原件已删除" } });
  return dialog;
}
beforeEach(() => { reconcile.mockReset(); reconcile.mockResolvedValue({ data: { voided_orders: 1, voided_lines: 4, voided_site_lines: 3, voided_site_issues: 1 } }); });
afterEach(cleanup);

describe("原件差集确认同步", () => {
  it("列出两类候选与比较范围，确认请求携带digest和选中身份，用户界面不展示内部ID", async () => {
    const p = props(); render(<WbddReconciliation {...p} />);
    expect(screen.getByText(/2026-06-09：1–3/)).toBeInTheDocument();
    expect(screen.queryByText("private-source-id")).toBeNull();
    expect(screen.getByText("1.5")).toBeInTheDocument();
    const dialog = await begin();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认同步" }));
    await waitFor(() => expect(reconcile).toHaveBeenCalledTimes(1));
    expect(reconcile.mock.calls[0]).toEqual([8, { expected_digest: "digest-8", source_order_ids: ["private-source-id"], line_ids: [12], reason: "核对原件已删除", idempotency_key: expect.stringMatching(/^wbdd-reconcile-/) }, expect.objectContaining({ request: expect.any(Function), isCurrent: expect.any(Function) })]);
    await waitFor(() => expect(p.onCommitted).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/同步已完成：作废 1 张需求单、4 条需求明细、3 条关联领用明细/)).toBeInTheDocument();
  });

  it("历史缺少快照不显示旧错误差集", () => {
    const p = props(); p.data.readiness = "snapshot_required";
    render(<WbddReconciliation {...p} />);
    expect(screen.getByText(/重新上传原件后核对/)).toBeInTheDocument();
    expect(screen.queryByText("PN-X")).toBeNull();
    expect(screen.queryByText("WBDD-20260609-0002")).toBeNull();
    expect(screen.getByRole("button", { name: "确认同步" })).toBeDisabled();
  });

  it("无权限不给勾选或写入口；无摘要不能提交", () => {
    const p = props(); const view = render(<WbddReconciliation {...p} canReconcile={false} />);
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "确认同步" })).toBeNull();
    view.rerender(<WbddReconciliation {...p} data={{ ...p.data, candidate_digest: null }} />);
    expect(screen.getByRole("button", { name: "确认同步" })).toBeDisabled();
  });

  it("重复点击只发一次请求", async () => {
    let resolve!: (value: unknown) => void;
    reconcile.mockReturnValue(new Promise(value => { resolve = value; }));
    render(<WbddReconciliation {...props()} />);
    const dialog = await begin(); const button = within(dialog).getByRole("button", { name: "确认同步" });
    fireEvent.click(button); fireEvent.click(button);
    expect(reconcile).toHaveBeenCalledTimes(1);
    await act(async () => resolve({ data: { voided_orders: 1, voided_lines: 4, voided_site_lines: 3, voided_site_issues: 1 } }));
  });

  it("网络结果未知时保留相同payload/key，即使父组件换了清单也不会生成新操作", async () => {
    reconcile.mockRejectedValueOnce(new Error("network lost"));
    const p = props(); const view = render(<WbddReconciliation {...p} />);
    const dialog = await begin(); fireEvent.click(within(dialog).getByRole("button", { name: "确认同步" }));
    await screen.findByText(/本次同步结果尚未确认/);
    const original = reconcile.mock.calls[0];
    view.rerender(<WbddReconciliation {...p} data={{ ...p.data, batch_id: 9, candidate_digest: "new-digest" }} />);
    expect(within(screen.getByRole("dialog")).getByRole("textbox")).toBeDisabled();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "重试同一次同步" }));
    await waitFor(() => expect(reconcile).toHaveBeenCalledTimes(2));
    expect(reconcile.mock.calls[1][0]).toBe(original[0]);
    expect(reconcile.mock.calls[1][1]).toBe(original[1]);
    await waitFor(() => expect(p.onCommitted).toHaveBeenCalledTimes(1));
  });

  it("409拒绝后清除待执行请求并要求刷新，不沿用旧摘要重试", async () => {
    reconcile.mockRejectedValueOnce({ response: { status: 409 } });
    const p = props(); render(<WbddReconciliation {...p} />);
    const dialog = await begin(); fireEvent.click(within(dialog).getByRole("button", { name: "确认同步" }));
    await screen.findByText(/请刷新差异清单后重新核对/);
    expect(screen.getByRole("button", { name: "确认同步" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "重试同一次同步" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "刷新差异清单" }));
    await waitFor(() => expect(p.onRefresh).toHaveBeenCalledTimes(1));
    expect(p.onCommitted).not.toHaveBeenCalled();
  });

  it("写入成功但刷新失败仍报告写入成功，不把它当网络未知重发", async () => {
    const p = props(); p.onCommitted.mockRejectedValueOnce(new Error("refresh failed"));
    render(<WbddReconciliation {...p} />);
    const dialog = await begin(); fireEvent.click(within(dialog).getByRole("button", { name: "确认同步" }));
    expect(await screen.findByText(/同步已完成.*页面刷新失败/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试同一次同步" })).toBeNull();
    expect(reconcile).toHaveBeenCalledTimes(1);
  });
});

// These existing business behavior fixtures intentionally exercise the legacy runtime.
vi.mock("../../../authz/AuthzProvider", async original => ({
  ...await original<typeof import("../../../authz/AuthzProvider")>(),
  useAuthz: () => legacyAuthorizationState(),
}));
beforeEach(() => prepareLegacyPageFixture());
