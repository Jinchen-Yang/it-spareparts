import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const {
  applyMaintenanceProjectWorkbook,
  downloadMaintenanceProjectWorkbook,
  validateMaintenanceProjectWorkbook,
} = vi.hoisted(() => ({
  applyMaintenanceProjectWorkbook: vi.fn(),
  downloadMaintenanceProjectWorkbook: vi.fn(),
  validateMaintenanceProjectWorkbook: vi.fn(),
}));

vi.mock("../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceOperations")>(
    "../../../api/maintenanceOperations",
  );
  return {
    ...actual,
    applyMaintenanceProjectWorkbook,
    downloadMaintenanceProjectWorkbook,
    validateMaintenanceProjectWorkbook,
  };
});

import ProjectWorkbookActions from "../ProjectWorkbookActions";

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  downloadMaintenanceProjectWorkbook.mockResolvedValue({ data: new Blob(["xlsx"]) });
  validateMaintenanceProjectWorkbook.mockResolvedValue({
    data: {
      validation_token: "token-1",
      project_id: "project-1",
      data_version: "version-3",
      filename: "维保更新.xlsx",
      preview: {
        protocol_version: "2.0",
        sheets: [],
        latest_tracking_month: "2026-08",
        last_exported_at: null,
        data_version: "version-3",
      },
      changes: { collection_append: 2 },
      warnings: ["有 1 行备注为空"],
      errors: [],
      can_apply: true,
    },
  });
  applyMaintenanceProjectWorkbook.mockResolvedValue({
    data: { applied: true, changed_rows: 2, data_version: "version-4" },
  });
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: vi.fn(() => "blob:workbook"),
    revokeObjectURL: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ProjectWorkbookActions", () => {
  it("下载完整四表，并在上传后先预览差异再确认应用", async () => {
    const onApplied = vi.fn();
    render(
      <ProjectWorkbookActions
        projectId="project-1"
        projectCode="XM-001"
        onApplied={onApplied}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "下载完整四表" }));
    await waitFor(() => expect(downloadMaintenanceProjectWorkbook).toHaveBeenCalledWith("project-1"));

    const file = new File(["xlsx"], "维保更新.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    fireEvent.change(screen.getByLabelText("选择月度更新工作簿"), {
      target: { files: [file] },
    });

    expect(await screen.findByText("校验通过，尚未写入系统")).toBeInTheDocument();
    expect(screen.getByText("新增回款记录")).toBeInTheDocument();
    expect(screen.getByText("2 行")).toBeInTheDocument();
    expect(screen.getByText("有 1 行备注为空")).toBeInTheDocument();
    expect(applyMaintenanceProjectWorkbook).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "确认应用本次更新" }));
    await waitFor(() => expect(applyMaintenanceProjectWorkbook).toHaveBeenCalledWith(
      "project-1",
      { validation_token: "token-1", data_version: "version-3" },
    ));
    expect(await screen.findByText("已应用 2 行更新")).toBeInTheDocument();
    expect(onApplied).toHaveBeenCalledOnce();
  });

  it("校验失败时展示错误且不允许应用", async () => {
    validateMaintenanceProjectWorkbook.mockResolvedValueOnce({
      data: {
        validation_token: "token-bad",
        project_id: "project-1",
        data_version: "version-3",
        filename: "错误.xlsx",
        preview: {
          protocol_version: "2.0",
          sheets: [],
          latest_tracking_month: null,
          last_exported_at: null,
          data_version: "version-3",
        },
        changes: {},
        warnings: [],
        errors: ["02_备件消耗是系统生成表，禁止修改"],
        can_apply: false,
      },
    });
    render(<ProjectWorkbookActions projectId="project-1" projectCode="XM-001" />);

    fireEvent.change(screen.getByLabelText("选择月度更新工作簿"), {
      target: { files: [new File(["bad"], "错误.xlsx")] },
    });

    expect(await screen.findByText("校验未通过，未写入系统")).toBeInTheDocument();
    expect(screen.getByText("02_备件消耗是系统生成表，禁止修改")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认应用本次更新" })).toBeNull();
  });

  it("无回填应用权限时保留下载，但不暴露上传入口", () => {
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_customer: true,
      data_purchase_cost: true,
      data_profit: true,
      own_customers_only: false,
      action_maintenance_roundtrip_apply: false,
    }));

    render(<ProjectWorkbookActions projectId="project-1" projectCode="XM-001" />);

    expect(screen.getByRole("button", { name: "下载完整四表" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "上传月度更新" })).toBeNull();
    expect(screen.queryByLabelText("选择月度更新工作簿")).toBeNull();
  });

  it("缺少完整四表所需数据权限时同时隐藏下载和上传", () => {
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_customer: true,
      data_purchase_cost: false,
      data_profit: true,
      own_customers_only: false,
      action_maintenance_roundtrip_apply: true,
    }));

    render(<ProjectWorkbookActions projectId="project-1" projectCode="XM-001" />);

    expect(screen.queryByRole("button", { name: "下载完整四表" })).toBeNull();
    expect(screen.queryByRole("button", { name: "上传月度更新" })).toBeNull();
  });
});
