import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getMaintenanceAcceptance: vi.fn(),
  uploadMaintenanceAcceptanceAttachment: vi.fn(),
  submitMaintenanceAcceptance: vi.fn(),
  downloadMaintenanceAcceptanceAttachment: vi.fn(),
  deleteMaintenanceAcceptanceAttachment: vi.fn(),
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
    deleteMaintenanceAcceptanceAttachment: mocks.deleteMaintenanceAcceptanceAttachment,
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
  review_policy: "submit_takes_effect",
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
    // 2026-08-25 新口径：上传只传文件本身——无版本握手、无前端幂等键
    expect(payload).not.toHaveProperty("expected_version");
    expect(payload).not.toHaveProperty("idempotencyKey");
  });

  it("点击上传按钮会调用隐藏文件输入框，打开原生文件选择器", async () => {
    const { container } = render(<MaintenanceAcceptancePanel projectId="p1" />);
    const button = await screen.findByRole("button", { name: /上传验收附件/ });
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const click = vi.spyOn(input, "click").mockImplementation(() => undefined);

    fireEvent.click(button);

    expect(click).toHaveBeenCalledTimes(1);
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

  it("任意格式上传失败不再提示检查格式", async () => {
    mocks.uploadMaintenanceAcceptanceAttachment.mockRejectedValue(new Error("Network Error"));
    const { container } = render(<MaintenanceAcceptancePanel projectId="p1" />);
    await screen.findByText("上传验收附件");
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).not.toHaveAttribute("accept");
    fireEvent.change(input, {
      target: { files: [new File(["report"], "验收报告.签字扫描")] },
    });

    expect(await screen.findByText("附件上传失败，请刷新核对附件列表后重试。")).toBeInTheDocument();
    expect(screen.queryByText(/检查格式/)).not.toBeInTheDocument();
  });

  // b12c5fb「首传被 version 守卫静默吞掉」回归思想移植，适配 2ebbf90 新口径：
  // version=0 空载荷首传正常发请求、不再发 expected_version，成功后刷新。
  it("version=0 空载荷首次上传：不发 expected_version，上传后刷新", async () => {
    mocks.getMaintenanceAcceptance.mockResolvedValue({
      data: {
        ...record,
        deliverable_id: null,
        due_date: null,
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
    const [projectId, payload] = mocks.uploadMaintenanceAcceptanceAttachment.mock.calls[0];
    expect(projectId).toBe("p1");
    expect(payload.file).toBe(file);
    expect(payload).not.toHaveProperty("expected_version");
    // 初次 load + 上传成功后 refreshAfterMutation 各一次
    await waitFor(() => expect(mocks.getMaintenanceAcceptance).toHaveBeenCalledTimes(2));
    // 成功路径清空 input，否则重选同一文件不触发 onChange
    await waitFor(() => expect(input.value).toBe(""));
  });

  it("附件已写入但父级读回失败时传播失败并失效旧验收状态", async () => {
    mocks.uploadMaintenanceAcceptanceAttachment.mockResolvedValue({ data: {} });
    const onChanged = vi.fn().mockResolvedValue(false);
    const { container } = render(
      <MaintenanceAcceptancePanel projectId="p1" onChanged={onChanged} />,
    );
    await screen.findByText("上传验收附件");
    fireEvent.change(container.querySelector('input[type="file"]') as HTMLInputElement, {
      target: { files: [new File(["report"], "验收报告.pdf", { type: "application/pdf" })] },
    });

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/操作已写入，但验收页面刷新失败/)).toBeInTheDocument();
  });

  it("删除附件：Popconfirm 确认后调 DELETE 并刷新列表", async () => {
    const attachment = {
      file_id: "file-1",
      original_filename: "验收报告.pdf",
      mime_type: "application/pdf",
      size_bytes: 128,
      sha256: "sha-1",
      uploaded_by: "admin",
      uploaded_by_name: "李呈辉",
      uploaded_at: "2026-08-25T00:00:00Z",
    };
    mocks.getMaintenanceAcceptance.mockResolvedValue({
      data: { ...record, attachments: [attachment] },
    });
    mocks.deleteMaintenanceAcceptanceAttachment.mockResolvedValue({
      data: { file_id: "file-1", archived: true },
    });
    render(<MaintenanceAcceptancePanel projectId="p1" />);
    await screen.findByText("验收报告.pdf");
    // 2026-08-26 客户口径：附件旁显示上传人姓名
    expect(screen.getByText("李呈辉")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "删除 验收报告.pdf" }));
    fireEvent.click(await screen.findByRole("button", { name: /^删\s*除$/ }));

    await waitFor(() =>
      expect(mocks.deleteMaintenanceAcceptanceAttachment)
        .toHaveBeenCalledWith("p1", "file-1"));
    await waitFor(() => expect(mocks.getMaintenanceAcceptance).toHaveBeenCalledTimes(2));
  });

  it("无提交权限：隐藏上传/提交按钮并提示联系管理员开通", async () => {
    localStorage.setItem("role", "sales");
    localStorage.setItem("permissions", "{}");
    render(<MaintenanceAcceptancePanel projectId="p1" />);

    expect(await screen.findByText(/请联系管理员开通/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "上传验收附件" })).toBeNull();
    expect(screen.queryByRole("button", { name: "提交验收报告" })).toBeNull();
  });

  it("无附件时提交按钮禁用", async () => {
    render(<MaintenanceAcceptancePanel projectId="p1" />);
    const submitButton = await screen.findByRole("button", { name: "提交验收报告" });
    expect(submitButton).toBeDisabled();
    expect(screen.getByText("验收附件待上传")).toBeInTheDocument();
  });
});
