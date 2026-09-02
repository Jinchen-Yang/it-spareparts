import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Modal, message } from "antd";

const { saveBlob } = vi.hoisted(() => ({ saveBlob: vi.fn() }));

vi.mock("../../../api/maintenanceWorkbooks", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceWorkbooks")>(
    "../../../api/maintenanceWorkbooks",
  );
  return { ...actual, saveBlob };
});

import WorkbookRoundTrip from "../WorkbookRoundTrip";

const makeFile = () => new File(["xlsx"], "回填.xlsx", { type: "application/vnd.ms-excel" });

function uploadFile(container: HTMLElement, file: File) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
  return input;
}

describe("WorkbookRoundTrip", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => {
    cleanup();
    Modal.destroyAll();
    message.destroy();
  });

  it("hides the upload button without the upload permission", () => {
    render(
      <WorkbookRoundTrip
        title="总表"
        filename="a.xlsx"
        canUpload={false}
        onDownload={vi.fn()}
        onApply={vi.fn()}
      />,
    );
    expect(screen.getByText("下载总表")).toBeInTheDocument();
    expect(screen.queryByText("上传覆盖")).not.toBeInTheDocument();
  });

  it("applies directly when no onValidate is given (legacy single-phase)", async () => {
    const onApply = vi.fn().mockResolvedValue({ cost_refills: 2 });
    const { container } = render(
      <WorkbookRoundTrip
        title="备件表"
        filename="a.xlsx"
        canUpload
        onDownload={vi.fn()}
        onApply={onApply}
      />,
    );
    const file = makeFile();
    const firstInput = uploadFile(container, file);
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/已覆盖：补价 2 行/)).toBeInTheDocument();
    await waitFor(() =>
      expect(container.querySelector('input[type="file"]')).not.toBe(firstInput));
    uploadFile(container, file);
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(2));
  });

  it("does not claim the upload failed or the stale UI refreshed after commit succeeds", async () => {
    const onApply = vi.fn().mockResolvedValue({ contract_updates: 1 });
    const onAfterApply = vi.fn().mockResolvedValue(false);
    const { container } = render(
      <WorkbookRoundTrip
        title="总表"
        filename="a.xlsx"
        canUpload
        onDownload={vi.fn()}
        onApply={onApply}
        onAfterApply={onAfterApply}
      />,
    );

    uploadFile(container, makeFile());

    await waitFor(() => expect(onAfterApply).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/数据已写入，但页面刷新失败/)).toBeInTheDocument();
    expect(screen.queryByText(/已覆盖并刷新/)).toBeNull();
    expect(screen.queryByText("上传失败")).toBeNull();
  });

  it("two-phase: shows the void preview and applies only after confirm", async () => {
    const onValidate = vi.fn().mockResolvedValue({
      expense_updates: 1,
      will_void_rows: [{ sheet: "04_报销订单", order_no: "BXD-1", reason: "上传文件缺行" }],
      will_reassign_orders: [{
        source_order_id: "raw-wbdd-1",
        order_no: "WBDD-1",
        from_project_name: "旧项目",
        to_project_id: "new-project",
      }],
    });
    const onApply = vi.fn().mockResolvedValue({ expense_updates: 1 });
    const { container } = render(
      <WorkbookRoundTrip
        title="总表"
        filename="a.xlsx"
        canUpload
        onDownload={vi.fn()}
        onValidate={onValidate}
        onApply={onApply}
      />,
    );
    uploadFile(container, makeFile());

    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(1));
    expect(onApply).not.toHaveBeenCalled();
    expect(await screen.findByText(/以下 1 行将被作废/)).toBeInTheDocument();
    expect(screen.getByText(/BXD-1/)).toBeInTheDocument();
    expect(screen.getByText(/以下 1 张 WBDD 将按本次项目内人工认证更正归属/)).toBeInTheDocument();
    expect(screen.getByText(/WBDD-1 · 旧项目 → 当前项目/)).toBeInTheDocument();

    fireEvent.click(screen.getByText("确认回传"));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
  });

  it("two-phase: cancel leaves the file unapplied", async () => {
    const onValidate = vi.fn().mockResolvedValue({ will_void_rows: [] });
    const onApply = vi.fn();
    const { container } = render(
      <WorkbookRoundTrip
        title="总表"
        filename="a.xlsx"
        canUpload
        onDownload={vi.fn()}
        onValidate={onValidate}
        onApply={onApply}
      />,
    );
    const file = makeFile();
    const firstInput = uploadFile(container, file);
    const cancelButtons = await screen.findAllByRole("button", { name: /取\s*消/ });
    fireEvent.click(cancelButtons[cancelButtons.length - 1]);
    // 弹窗关闭有动画，等一拍再断言——本用例的要点是「取消绝不落库」
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(onApply).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(container.querySelector('input[type="file"]')).not.toBe(firstInput));
    uploadFile(container, file);
    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(2));
    const retryCancelButtons = await screen.findAllByRole("button", { name: /取\s*消/ });
    fireEvent.click(retryCancelButtons[retryCancelButtons.length - 1]);
  });

  it("two-phase: a rejected validation never reaches apply and shows the backend line error", async () => {
    const onValidate = vi.fn().mockRejectedValue({
      response: { data: { detail: "第 12 行：上传行数不足导出行数 50%，整本拒绝" } },
    });
    const onApply = vi.fn();
    const { container } = render(
      <WorkbookRoundTrip
        title="总表"
        filename="a.xlsx"
        canUpload
        onDownload={vi.fn()}
        onValidate={onValidate}
        onApply={onApply}
      />,
    );
    const file = makeFile();
    const firstInput = uploadFile(container, file);
    expect(await screen.findByText(/第 12 行：上传行数不足/)).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(container.querySelector('input[type="file"]')).not.toBe(firstInput));
    uploadFile(container, file);
    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(2));
  });
});

describe("WorkbookRoundTrip · 2.7.0 行级冲突与强制接管", () => {
  it("409 冲突展示三值对照，确认后带 force_takeover 重传", async () => {
    const conflicts = [
      {
        sheet: "03_备件明细", row: "WBDD-1", entity_id: "7",
        field: "需求数量", old: "4", new: "6", base: "2",
        reason: "server_changed_since_export",
      },
    ];
    const onApply = vi.fn()
      .mockRejectedValueOnce({
        response: {
          status: 409,
          data: {
            detail: {
              code: "row_conflicts",
              message: "部分行已被他人更新",
              conflicts,
            },
          },
        },
      })
      .mockResolvedValueOnce({
        cost_refills: 0, site_return_flags: 0, expense_updates: 0,
        collection_creates: 0, collection_voids: 0,
        changes: conflicts.map((c) => ({ ...c, overridden: true })),
        overridden: conflicts,
        force_takeover: true,
      });
    const { container } = render(
      <WorkbookRoundTrip
        title="测试表" filename="t.xlsx"
        onDownload={vi.fn().mockResolvedValue(new Blob())}
        onApply={onApply}
        canUpload
      />,
    );
    uploadFile(container, makeFile());
    // 冲突弹窗：三值都渲染
    expect(
      (await screen.findAllByText(/有 1 处改动与他人冲突/)).length,
    ).toBeGreaterThan(0);
    expect(screen.getAllByText(/你下载时的值：2/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/当前最新值：4/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/你上传的值：6/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /强制接管并上传/ }));
    await waitFor(() => {
      expect(onApply).toHaveBeenCalledTimes(2);
      expect(onApply).toHaveBeenLastCalledWith(expect.any(File), {
        forceTakeover: true,
      });
    });
    await screen.findByText(/已覆盖/);
  });
});
