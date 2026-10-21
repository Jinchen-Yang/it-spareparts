/** PR #440 第 2 轮返工（sol M3 / astra B3）：恢复弹窗的账号切换守卫。
 *
 * 弹窗打开时记下会话标识（sessionToken prop），首次提交、未知结果重试、
 * 异步响应落地前都比对当前 localStorage token；会话变了就关弹窗、清掉
 * 旧预览与 attempt，不向新账号提交。
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  list: vi.fn(), preview: vi.fn(), restore: vi.fn(),
}));
vi.mock("../../../api/maintenanceSourceCancelCascade", () => ({
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
  localStorage.setItem("token", "token-1");
  api.list.mockResolvedValue({ data: { items: [item], total: 1 } });
  api.preview.mockResolvedValue({ data: previewInfo });
  api.restore.mockResolvedValue({ data: restoreResult });
});
afterEach(cleanup);

const submitRestore = async () => {
  fireEvent.click(await screen.findByRole("button", { name: "查看恢复预览" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(await within(dialog).findByRole("textbox", { name: "恢复原因" }),
    { target: { value: "误确认，恢复" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "确认恢复" }));
  const confirmDialog = (await screen.findByText(/将恢复 1 条需求明细/)).closest("[role=dialog]") as HTMLElement;
  fireEvent.click(within(confirmDialog).getByRole("button", { name: "确认执行恢复" }));
};

describe("恢复弹窗账号切换守卫", () => {
  it("提交前 token 已换人：不提交、关弹窗并提示", async () => {
    render(<SourceCancelRestorationSection {...props()} />);
    await submitRestore();
    localStorage.setItem("token", "token-b");
    fireEvent.click(screen.getByRole("button", { name: "确认执行恢复" }));
    expect(api.restore).not.toHaveBeenCalled();
    expect(await screen.findByText(/账号已切换/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("PN-需求")).toBeNull());
  });

  it("结果未知后 token 已换人：重试同一次恢复被拒绝", async () => {
    api.restore.mockRejectedValueOnce(new Error("connection lost"));
    render(<SourceCancelRestorationSection {...props()} />);
    await submitRestore();
    expect(await screen.findByText(/本次恢复结果尚未确认/)).toBeInTheDocument();
    localStorage.setItem("token", "token-b");
    fireEvent.click(screen.getByRole("button", { name: "重试同一次恢复" }));
    expect(api.restore).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/账号已切换/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "重试同一次恢复" })).toBeNull());
  });

  it("恢复在途时 token 已换人：响应落地不写回执、不刷新", async () => {
    let finish!: (value: { data: typeof restoreResult }) => void;
    api.restore.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const p = props();
    render(<SourceCancelRestorationSection {...p} />);
    await submitRestore();
    expect(api.restore).toHaveBeenCalledTimes(1);
    localStorage.setItem("token", "token-b");
    finish({ data: restoreResult });
    await screen.findByText(/账号已切换/);
    expect(screen.queryByText(/已恢复：1 条需求明细/)).toBeNull();
    await waitFor(() => expect(p.onChanged).not.toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText("PN-需求")).toBeNull());
  });

  it("预览在途时 token 已换人：预览落地不开弹窗、不可提交", async () => {
    let finish!: (value: { data: typeof previewInfo }) => void;
    api.preview.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    render(<SourceCancelRestorationSection {...props()} />);
    fireEvent.click(await screen.findByRole("button", { name: "查看恢复预览" }));
    localStorage.setItem("token", "token-b");
    finish({ data: previewInfo });
    expect(await screen.findByText(/账号已切换/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("PN-需求")).toBeNull());
    expect(screen.queryByRole("button", { name: "确认恢复" })).toBeNull();
    expect(api.restore).not.toHaveBeenCalled();
  });
});
