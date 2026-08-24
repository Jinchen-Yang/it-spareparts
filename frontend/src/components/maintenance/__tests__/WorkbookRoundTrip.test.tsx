import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
}

describe("WorkbookRoundTrip", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(cleanup);

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
    uploadFile(container, makeFile());
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/已覆盖：补价 2 行/)).toBeInTheDocument();
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
    uploadFile(container, makeFile());
    fireEvent.click(await screen.findByRole("button", { name: /取\s*消/ }));
    // 弹窗关闭有动画，等一拍再断言——本用例的要点是「取消绝不落库」
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(onApply).not.toHaveBeenCalled();
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
    uploadFile(container, makeFile());
    expect(await screen.findByText(/第 12 行：上传行数不足/)).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
  });
});
