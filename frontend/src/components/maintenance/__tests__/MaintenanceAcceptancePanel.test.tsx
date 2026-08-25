import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getMaintenanceAcceptance: vi.fn(),
  uploadMaintenanceAcceptanceAttachment: vi.fn(),
  submitMaintenanceAcceptance: vi.fn(),
  downloadMaintenanceAcceptanceAttachment: vi.fn(),
  saveBlob: vi.fn(),
}));

vi.mock("../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceOperations")>(
    "../../../api/maintenanceOperations",
  );
  return {
    ...actual,
    getMaintenanceAcceptance: mocks.getMaintenanceAcceptance,
    uploadMaintenanceAcceptanceAttachment: mocks.uploadMaintenanceAcceptanceAttachment,
    submitMaintenanceAcceptance: mocks.submitMaintenanceAcceptance,
    downloadMaintenanceAcceptanceAttachment: mocks.downloadMaintenanceAcceptanceAttachment,
  };
});

vi.mock("../../../api/maintenanceWorkbooks", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceWorkbooks")>(
    "../../../api/maintenanceWorkbooks",
  );
  return { ...actual, saveBlob: mocks.saveBlob };
});

import MaintenanceAcceptancePanel from "../MaintenanceAcceptancePanel";

const record = {
  deliverable_id: "acceptance-1",
  project_id: "p1",
  deliverable_type: "acceptance_report" as const,
  due_date: "2026-08-31",
  submission_status: "not_submitted",
  submitted_at: null,
  submitted_by: null,
  approval_status: "not_reviewed",
  approved_at: null,
  approved_by: null,
  rejection_reason: null,
  configuration_state: "configured",
  version: 1,
  review_policy: "admin_only_pending_business_role_configuration",
  attachments: [],
};

describe("MaintenanceAcceptancePanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions", "{}");
    mocks.getMaintenanceAcceptance.mockResolvedValue({ data: record });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("缺少 randomUUID 时仍把附件发到上传接口", async () => {
    vi.stubGlobal("crypto", {});
    mocks.uploadMaintenanceAcceptanceAttachment.mockResolvedValue({ data: {} });
    const { container } = render(<MaintenanceAcceptancePanel projectId="p1" />);
    await screen.findByText("上传验收附件");
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["report"], "验收报告.pdf", { type: "application/pdf" });

    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() =>
      expect(mocks.uploadMaintenanceAcceptanceAttachment).toHaveBeenCalledTimes(1));
    const [projectId, payload] = mocks.uploadMaintenanceAcceptanceAttachment.mock.calls[0];
    expect(projectId).toBe("p1");
    expect(payload.file).toBe(file);
    expect(payload.expected_version).toBe(1);
    expect(payload.idempotencyKey).toMatch(/^acceptance-upload-/);
  });

  it("展示后端结构化 message，不再吞成笼统上传失败", async () => {
    mocks.uploadMaintenanceAcceptanceAttachment.mockRejectedValue({
      response: { data: { detail: { message: "附件类型与扩展名不一致" } } },
    });
    const { container } = render(<MaintenanceAcceptancePanel projectId="p1" />);
    await screen.findByText("上传验收附件");
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["bad"], "伪装.pdf", { type: "application/pdf" })] },
    });

    expect(await screen.findByText("附件类型与扩展名不一致")).toBeInTheDocument();
  });
});

describe("MaintenanceAcceptancePanel · version=0 首次上传", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("无交付行（version=0）首次上传正常发请求（2026-08-25 静默 return 修复）", async () => {
    mocks.getMaintenanceAcceptance.mockResolvedValue({
      data: {
        ...record,
        deliverable_id: null,
        due_date: null,
        configuration_state: "configured",
        version: 0,
      },
    });
    mocks.uploadMaintenanceAcceptanceAttachment.mockResolvedValue({ data: {} });
    const { container } = render(<MaintenanceAcceptancePanel projectId="p1" />);
    await screen.findByText("上传验收附件");
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["first"], "首次验收报告.pdf", { type: "application/pdf" });

    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() =>
      expect(mocks.uploadMaintenanceAcceptanceAttachment).toHaveBeenCalledTimes(1));
    const [, payload] = mocks.uploadMaintenanceAcceptanceAttachment.mock.calls[0];
    expect(payload.expected_version).toBe(0);
    // 早退/成功路径都必须清空 input，否则同文件重选不触发 onChange
    await waitFor(() => expect(input.value).toBe(""));
  });

});
