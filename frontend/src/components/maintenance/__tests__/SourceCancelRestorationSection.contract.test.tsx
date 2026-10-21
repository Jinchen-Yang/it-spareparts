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
const firstRestoreResult = {
  batch_id: 8, source_order_id: "private-order-id", status: "restored",
  restored_at: "2026-10-21T09:30:00Z", restored_lines: 1, restored_site_lines: 1,
  restored_site_issues: 1, tombstone_version: 3, replayed: false, warnings: [],
};
const props = () => ({ projectId: "project-1", canConfirm: true, sessionToken: "token-1",
  onChanged: vi.fn().mockResolvedValue(true) });

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  // 会话守卫按 localStorage token 比对 sessionToken prop：契约测试保持当前账号。
  localStorage.setItem("token", "token-1");
  api.list.mockResolvedValue({ data: { items: [item], total: 1 } });
  api.preview.mockResolvedValue({ data: previewInfo });
});
afterEach(cleanup);

const restoreAndWait = async () => {
  fireEvent.click(await screen.findByRole("button", { name: "查看恢复预览" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(await within(dialog).findByRole("textbox", { name: "恢复原因" }),
    { target: { value: "误确认，恢复" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "确认恢复" }));
  const confirmDialog = (await screen.findByText(/将恢复 1 条需求明细/)).closest("[role=dialog]") as HTMLElement;
  fireEvent.click(within(confirmDialog).getByRole("button", { name: "确认执行恢复" }));
  await waitFor(() => expect(api.restore).toHaveBeenCalledTimes(1));
};

describe("恢复回执 already_restored 契约（真实后端返回体）", () => {
  it("真实并发实测体（顶层无 warnings 字段）不崩溃并正常提示此前已恢复", async () => {
    // 与后端 _already_restored 实际返回一致：异键并发时另一请求拿到该体（验收 M5 亲测体）
    api.restore.mockResolvedValue({ data: {
      status: "already_restored", source_order_id: "private-order-id",
      restored_via: "cancel_restore", restored_by: "cancel-admin",
      restored_at: "2026-10-21T09:30:00Z", result: firstRestoreResult, replayed: false,
    } });
    render(<SourceCancelRestorationSection {...props()} />);
    await restoreAndWait();
    expect(await screen.findByText(
      /该确认此前已恢复（cancel-admin 于 2026-10-21T09:30:00Z），本次未重复写入。/)).toBeInTheDocument();
  });

  it("实名更正路径（restored_via=other、无 result、restored_at 为空）安全显示未知时间", async () => {
    api.restore.mockResolvedValue({ data: {
      status: "already_restored", source_order_id: "private-order-id",
      restored_via: "other", restored_by: "别的操作人", restored_at: null,
      result: null, replayed: false,
    } });
    render(<SourceCancelRestorationSection {...props()} />);
    await restoreAndWait();
    expect(await screen.findByText(
      /该确认此前已恢复（别的操作人 于 未知时间），本次未重复写入。/)).toBeInTheDocument();
  });

  it("新契约 already_restored 带 warnings 时逐条渲染警告", async () => {
    api.restore.mockResolvedValue({ data: {
      status: "already_restored", source_order_id: "private-order-id",
      restored_via: "cancel_restore", restored_by: "cancel-admin",
      restored_at: "2026-10-21T09:30:00Z", result: firstRestoreResult, replayed: false,
      warnings: ["此上传候选已经人工确认；再次处理须重新上传并核对。"],
    } });
    render(<SourceCancelRestorationSection {...props()} />);
    await restoreAndWait();
    expect(await screen.findByText(/该确认此前已恢复/)).toBeInTheDocument();
    expect(screen.getByText("此上传候选已经人工确认；再次处理须重新上传并核对。")).toBeInTheDocument();
  });
});
