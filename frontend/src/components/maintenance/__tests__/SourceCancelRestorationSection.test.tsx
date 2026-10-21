import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  cancelList: vi.fn(), cancelDetail: vi.fn(), cancelConfirm: vi.fn(), cancelStatus: vi.fn(),
  list: vi.fn(), preview: vi.fn(), restore: vi.fn(),
}));
vi.mock("../../../api/maintenanceSourceCancelCascade", () => ({
  listSourceCancellations: api.cancelList,
  getSourceCancellation: api.cancelDetail,
  confirmSourceCancellation: api.cancelConfirm,
  getSourceCancellationStatus: api.cancelStatus,
  listSourceRestorations: api.list,
  getSourceRestoration: api.preview,
  restoreSourceCancellation: api.restore,
}));
import SourceCancelRestorationSection from "../SourceCancelRestorationSection";

const item = {
  batch_id: 8, source_order_id: "private-order-id", order_no: "WBDD-20261020-0001",
  confirmed_at: "2026-10-20T09:00:00Z", confirmed_by: "cancel-admin",
  confirm_reason: "已核对源单取消", voided_lines: 2, voided_site_lines: 1, voided_site_issues: 1,
  warnings: [], conflicts: [], restored: false, restore_receipt: null,
  state: "restorable", expected_digest: null,
};
const previewInfo = {
  ...item, expected_digest: "digest-8", context: { revision: 5 },
  restorable_demands: [{ line_id: 12, raw_line_id: "private-raw-line", pn: "PN-需求", qty: "3" }],
  restorable_site_lines: [{ issue_line_id: "sl-1", issue_id: "i-1", issue_no: "LY-精确-1", pn: "PN-领用", quantity: "2" }],
  restorable_site_issues: [{ issue_id: "i-1", issue_no: "LY-精确-1", voided_status: "void", original_status: "confirmed" }],
};
const restoreResult = {
  batch_id: 8, source_order_id: "private-order-id", status: "restored",
  restored_at: "2026-10-21T09:30:00Z", restored_lines: 1, restored_site_lines: 1,
  restored_site_issues: 1, tombstone_version: 3, replayed: false, warnings: [],
};
const props = () => ({ projectId: "project-1", canConfirm: true, sessionToken: "token-1",
  onChanged: vi.fn().mockResolvedValue(true) });

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  // 会话守卫按 localStorage token 比对 sessionToken prop：流程测试保持当前账号。
  localStorage.setItem("token", "token-1");
  api.list.mockResolvedValue({ data: { items: [item], total: 1 } });
  api.preview.mockResolvedValue({ data: previewInfo });
  api.restore.mockResolvedValue({ data: restoreResult });
});
afterEach(cleanup);

const openPreview = async () => {
  fireEvent.click(await screen.findByRole("button", { name: "查看恢复预览" }));
  return screen.findByRole("dialog");
};

describe("误取消确认恢复入口", () => {
  it("没有连带作废权限时整个恢复入口隐藏，也不请求恢复清单", () => {
    render(<SourceCancelRestorationSection {...props()} canConfirm={false} />);
    expect(screen.queryByText("已确认作废恢复")).toBeNull();
    expect(api.list).not.toHaveBeenCalled();
  });

  it("清单为空显示空态；非取消确认产生的作废不进入该入口", async () => {
    api.list.mockResolvedValue({ data: { items: [], total: 0 } });
    render(<SourceCancelRestorationSection {...props()} />);
    expect(await screen.findByText("当前没有可恢复的取消确认")).toBeInTheDocument();
    expect(api.preview).not.toHaveBeenCalled();
  });

  it("预览可恢复范围后二次确认执行，展示恢复回执并刷新项目与清单", async () => {
    const p = props();
    render(<SourceCancelRestorationSection {...p} />);
    const dialog = await openPreview();
    await within(dialog).findByText("PN-需求");
    expect(within(dialog).getByText("PN-领用")).toBeInTheDocument();
    expect(within(dialog).getAllByText("LY-精确-1").length).toBeGreaterThanOrEqual(2);
    expect(within(dialog).getAllByText(/已作废 → 领用已确认/).length).toBeGreaterThan(0);
    expect(within(dialog).getByText(/cancel-admin 于 2026-10-20T09:00:00Z 确认作废/)).toBeInTheDocument();
    expect(within(dialog).queryByText("private-raw-line")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "确认恢复" })).toBeDisabled();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "恢复原因" }), { target: { value: "误确认，恢复" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认恢复" }));
    const confirmDialog = (await screen.findByText(/将恢复 1 条需求明细、1 条领用行、1 张领用单头/)).closest("[role=dialog]")! as HTMLElement;
    expect(within(confirmDialog).getByText(/该操作写入反向记录并升版项目工作簿/)).toBeInTheDocument();
    fireEvent.click(within(confirmDialog).getByRole("button", { name: "确认执行恢复" }));
    await waitFor(() => expect(api.restore).toHaveBeenCalledWith("project-1", "private-order-id", {
      expected_digest: "digest-8", reason: "误确认，恢复", idempotency_key: expect.stringMatching(/^cancel-restore-/),
    }, "token-1"));
    expect(await screen.findByText(/已恢复：1 条需求明细、1 条领用行、1 张领用单头/)).toBeInTheDocument();
    expect(p.onChanged).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  });

  it("预览存在冲突时逐项展示原因，不提供恢复动作", async () => {
    api.preview.mockResolvedValue({ data: {
      ...previewInfo, state: "conflict", expected_digest: null,
      conflicts: [
        { kind: "demand_line", id: "12", reason: "需求行已被他人改动，与作废时不一致" },
        { kind: "order", id: "private-order-id", reason: "来源单已重新挂靠到项目" },
      ] } });
    render(<SourceCancelRestorationSection {...props()} />);
    const dialog = await openPreview();
    expect(await within(dialog).findByText("需求行已被他人改动，与作废时不一致")).toBeInTheDocument();
    expect(within(dialog).getByText("来源单已重新挂靠到项目")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "确认恢复" })).toBeNull();
    expect(api.restore).not.toHaveBeenCalled();
  });

  it("历史回执缺快照时明确提示不可自动恢复并说明缺失", async () => {
    api.preview.mockResolvedValue({ data: {
      ...previewInfo, state: "unavailable", expected_digest: null,
      conflicts: [{ kind: "receipt", id: "private-order-id", reason: "回执缺少恢复前置快照（本功能上线前的历史确认），不可自动恢复" }] } });
    render(<SourceCancelRestorationSection {...props()} />);
    const dialog = await openPreview();
    expect((await within(dialog).findAllByText(/不可自动恢复/)).length).toBeGreaterThanOrEqual(2);
    expect(within(dialog).getAllByText(/回执缺少恢复前置快照/).length).toBeGreaterThanOrEqual(2);
    expect(within(dialog).queryByRole("button", { name: "确认恢复" })).toBeNull();
  });

  it("执行时 409 冲突零写，展示服务端冲突原因且不刷新项目", async () => {
    api.restore.mockRejectedValueOnce({ response: { status: 409, data: { detail: {
      code: "source_cancellation_restore_conflict", message: "相关数据已变化，本次未恢复，请刷新预览后重新核对",
      conflicts: [{ kind: "demand_line", id: "12", reason: "需求行已被他人改动，与作废时不一致" }] } } } });
    const p = props();
    render(<SourceCancelRestorationSection {...p} />);
    const dialog = await openPreview();
    fireEvent.change(await within(dialog).findByRole("textbox", { name: "恢复原因" }), { target: { value: "误确认，恢复" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认恢复" }));
    const confirmDialog = (await screen.findByText(/将恢复 1 条需求明细/)).closest("[role=dialog]") as HTMLElement;
    fireEvent.click(within(confirmDialog).getByRole("button", { name: "确认执行恢复" }));
    expect(await screen.findByText(/相关数据已变化，本次未恢复/)).toBeInTheDocument();
    expect(screen.getByText("需求行已被他人改动，与作废时不一致")).toBeInTheDocument();
    expect(p.onChanged).not.toHaveBeenCalled();
  });

  it("执行结果未知时可重试同一次恢复（沿用同一幂等键）", async () => {
    api.restore.mockRejectedValueOnce(new Error("connection lost"));
    render(<SourceCancelRestorationSection {...props()} />);
    const dialog = await openPreview();
    fireEvent.change(await within(dialog).findByRole("textbox", { name: "恢复原因" }), { target: { value: "误确认，恢复" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认恢复" }));
    const confirmDialog = (await screen.findByText(/将恢复 1 条需求明细/)).closest("[role=dialog]") as HTMLElement;
    fireEvent.click(within(confirmDialog).getByRole("button", { name: "确认执行恢复" }));
    expect(await screen.findByText(/本次恢复结果尚未确认/)).toBeInTheDocument();
    const original = api.restore.mock.calls[0];
    fireEvent.click(screen.getByRole("button", { name: "重试同一次恢复" }));
    await waitFor(() => expect(api.restore).toHaveBeenCalledTimes(2));
    expect(api.restore.mock.calls[1]).toEqual(original);
    expect(await screen.findByText(/已恢复：1 条需求明细、1 条领用行、1 张领用单头/)).toBeInTheDocument();
  });

  it("恢复清单加载失败可重试，恢复后的确认显示已恢复状态", async () => {
    api.list.mockRejectedValueOnce(new Error("offline"));
    render(<SourceCancelRestorationSection {...props()} />);
    expect(await screen.findByText(/恢复清单加载失败/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "刷新恢复清单" }));
    await screen.findByText(item.order_no);
    api.list.mockResolvedValue({ data: { items: [{ ...item, state: "restored", restored: true,
      restore_receipt: { batch_id: 8, operated_by: "cancel-admin", restored_at: "2026-10-21T09:30:00Z" } }], total: 1 } });
    api.preview.mockResolvedValue({ data: { ...previewInfo, state: "restored", restored: true,
      restore_receipt: { batch_id: 8, operated_by: "cancel-admin", restored_at: "2026-10-21T09:30:00Z" } } });
    fireEvent.click(screen.getByRole("button", { name: "刷新恢复清单" }));
    expect(await screen.findByText("已恢复")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看恢复预览" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/该确认已于 2026-10-21T09:30:00Z 由 cancel-admin 恢复/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "确认恢复" })).toBeNull();
  });
});
