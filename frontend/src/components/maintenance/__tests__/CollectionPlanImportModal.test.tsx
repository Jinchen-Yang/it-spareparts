import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const {
  applyCollectionPlan,
  previewCollectionPlan,
  searchCollectionPlanBindingOptions,
} = vi.hoisted(() => ({
  applyCollectionPlan: vi.fn(),
  previewCollectionPlan: vi.fn(),
  searchCollectionPlanBindingOptions: vi.fn(),
}));

vi.mock("../../../api/maintenanceCollectionReminders", async () => {
  const actual = await vi.importActual<
    typeof import("../../../api/maintenanceCollectionReminders")
  >("../../../api/maintenanceCollectionReminders");
  return {
    ...actual,
    applyCollectionPlan,
    previewCollectionPlan,
    searchCollectionPlanBindingOptions,
  };
});

import CollectionPlanImportModal from "../CollectionPlanImportModal";
import { COLLECTION_IMPORT } from "../maintenanceLanguage";
import type { CollectionPreviewResponse } from "../../../api/maintenanceCollectionReminders";

const previewResponse = (overrides: Partial<CollectionPreviewResponse> = {}): CollectionPreviewResponse => ({
  batch_id: "batch-1",
  batch_version: 1,
  data_version: "dv-1",
  status: "valid",
  contract_version: "project-manager-xls-v1",
  file_sha256: "a".repeat(64),
  counts: {
    projects: 3,
    milestones: 19,
    bound: 1,
    pending_binding: 2,
    blockers: 0,
    warnings: 1,
    create: 5,
    update: 2,
    unchanged: 10,
    source_missing: 2,
  },
  rows: [
    {
      row_key: "row-1",
      external_order_no: "ORDER-001",
      source_project_name: "合成项目一",
      binding: {
        status: "reviewed",
        project_id: "project-1",
        project_version: 3,
        project_contract_id: "pc-1",
        project_contract_version: 1,
        existing_binding_version: 2,
      },
      milestone_diffs: [],
      warning_codes: [],
      blocker_codes: [],
    },
    {
      row_key: "row-2",
      external_order_no: "ORDER-002",
      source_project_name: "合成项目二",
      binding: {
        status: "pending_review",
        project_id: null,
        project_version: null,
        project_contract_id: null,
        project_contract_version: null,
        existing_binding_version: null,
      },
      milestone_diffs: [],
      warning_codes: [],
      blocker_codes: [],
    },
    {
      row_key: "row-3",
      external_order_no: "ORDER-003",
      source_project_name: "合成项目三",
      binding: {
        status: "pending_review",
        project_id: null,
        project_version: null,
        project_contract_id: null,
        project_contract_version: null,
        existing_binding_version: 5,
      },
      milestone_diffs: [],
      warning_codes: [],
      blocker_codes: [],
    },
  ],
  issues: [
    {
      code: "plan_total_mismatch",
      severity: "warning",
      row_key: "row-1",
      sequence: null,
      message: "计划合计与订单金额不一致",
    },
  ],
  can_apply: true,
  expires_at: "2026-08-15T00:00:00Z",
  ...overrides,
});

const projectOptions = {
  data: {
    batch_id: "batch-1",
    rows: [
      {
        project_id: "project-2",
        project_code: "XM-002",
        display_name: "二号项目",
        version: 2,
        contracts: [
          {
            project_contract_id: "pc-2",
            contract_no: "HT-002",
            relation_status: "current",
            lifecycle_status: "effective",
            version: 1,
          },
        ],
      },
    ],
    total: 1,
    page: 1,
    page_size: 50,
    q: "XM-002",
  },
};

function pickXls(): File {
  return new File(["xls-bytes"], "回款计划.xls", { type: "application/vnd.ms-excel" });
}

async function chooseBinding(orderNo: string, query: string) {
  const projectSelect = screen.getByLabelText(`${COLLECTION_IMPORT.bindingProjectLabel} ${orderNo}`);
  fireEvent.mouseDown(projectSelect);
  fireEvent.change(projectSelect, { target: { value: query } });
  await waitFor(() => expect(searchCollectionPlanBindingOptions).toHaveBeenCalled());
  const option = await screen.findByRole("option", { name: /二号项目/ });
  fireEvent.click(option);
  const contractSelect = screen.getByLabelText(`${COLLECTION_IMPORT.bindingContractLabel} ${orderNo}`);
  fireEvent.mouseDown(contractSelect);
  const contractOption = await screen.findByRole("option", { name: /HT-002/ });
  fireEvent.click(contractOption);
}

async function renderAndReachStep(stepIndex: 0 | 1 | 2 | 3, preview = previewResponse()) {
  const onApplied = vi.fn();
  const onClose = vi.fn();
  const utils = render(
    <CollectionPlanImportModal open onClose={onClose} onApplied={onApplied} />,
  );
  previewCollectionPlan.mockResolvedValue({ data: preview });
  const file = pickXls();
  fireEvent.change(screen.getByLabelText(COLLECTION_IMPORT.filePickLabel), {
    target: { files: [file] },
  });
  if (stepIndex >= 1) {
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.previewAction }));
    await screen.findByText(COLLECTION_IMPORT.previewZeroWriteHint);
  }
  if (stepIndex >= 2) {
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.nextStep }));
    await screen.findByText("ORDER-002");
  }
  if (stepIndex >= 3) {
    await chooseBinding("ORDER-002", "XM-002");
    await chooseBinding("ORDER-003", "XM-002");
    fireEvent.change(screen.getByLabelText(COLLECTION_IMPORT.bindingReasonLabel), {
      target: { value: "合同改派" },
    });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.nextStep }));
    await screen.findByRole("button", { name: COLLECTION_IMPORT.apply });
  }
  return { onApplied, onClose, ...utils };
}

beforeEach(() => {
  vi.clearAllMocks();
  previewCollectionPlan.mockResolvedValue({ data: previewResponse() });
  searchCollectionPlanBindingOptions.mockResolvedValue(projectOptions);
  applyCollectionPlan.mockResolvedValue({
    data: {
      batch_id: "batch-1",
      batch_version: 1,
      data_version: "dv-2",
      status: "applied",
      counts: { created: 5, updated: 2, unchanged: 10, source_missing: 2, needs_review: 1 },
      idempotent_replay: false,
      applied_at: "2026-08-14T10:00:00Z",
    },
  });
});

afterEach(() => {
  cleanup();
});

const stepsContainer = () =>
  within(document.querySelector(".ant-steps") as HTMLElement);

describe("CollectionPlanImportModal", () => {
  it("四步流程标题齐全，预览前不出现已写入口径", async () => {
    render(<CollectionPlanImportModal open onClose={vi.fn()} onApplied={vi.fn()} />);
    expect(stepsContainer().getByText(COLLECTION_IMPORT.stepSelectFile)).toBeInTheDocument();
    expect(stepsContainer().getByText(COLLECTION_IMPORT.stepPreview)).toBeInTheDocument();
    expect(stepsContainer().getByText(COLLECTION_IMPORT.stepReviewBindings)).toBeInTheDocument();
    expect(stepsContainer().getByText(COLLECTION_IMPORT.stepApply)).toBeInTheDocument();
    expect(screen.queryByText(/已写入/)).toBeNull();

    const file = pickXls();
    fireEvent.change(screen.getByLabelText(COLLECTION_IMPORT.filePickLabel), {
      target: { files: [file] },
    });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.previewAction }));
    expect(await screen.findByText(COLLECTION_IMPORT.previewZeroWriteHint)).toBeInTheDocument();
    expect(screen.queryByText(/已写入/)).toBeNull();
    expect(screen.getByText("ORDER-001")).toBeInTheDocument();
  });

  it("首次 preview 生成幂等键，重新预览生成新键", async () => {
    render(<CollectionPlanImportModal open onClose={vi.fn()} onApplied={vi.fn()} />);
    const file = pickXls();
    fireEvent.change(screen.getByLabelText(COLLECTION_IMPORT.filePickLabel), {
      target: { files: [file] },
    });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.previewAction }));
    await waitFor(() => expect(previewCollectionPlan).toHaveBeenCalledTimes(1));
    const [firstFile, firstKey] = previewCollectionPlan.mock.calls[0];
    expect(firstFile).toBe(file);
    expect(firstKey).toMatch(/^preview-/);

    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.repreview }));
    await waitFor(() => expect(previewCollectionPlan).toHaveBeenCalledTimes(2));
    const secondKey = previewCollectionPlan.mock.calls[1][1];
    expect(secondKey).toMatch(/^preview-/);
    expect(secondKey).not.toBe(firstKey);
  });

  it("阻断未清零时不能进入审核绑定与确认应用", async () => {
    const blocked = previewResponse({
      counts: {
        ...previewResponse().counts,
        blockers: 2,
        pending_binding: 0,
      },
      rows: previewResponse().rows.filter((row) => row.row_key === "row-1"),
      issues: [
        {
          code: "date_amount_orphan",
          severity: "blocker",
          row_key: "row-1",
          sequence: null,
          message: "日期金额不成对",
        },
      ],
      can_apply: false,
    });
    await renderAndReachStep(1, blocked);
    expect(screen.getByText(COLLECTION_IMPORT.blockerHint)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: COLLECTION_IMPORT.nextStep })).toBeDisabled();
  });

  it("待绑定未完成时下一步禁用，全部选择并补齐理由后启用", async () => {
    await renderAndReachStep(2);
    const next = screen.getByRole("button", { name: COLLECTION_IMPORT.nextStep });
    expect(next).toBeDisabled();

    await chooseBinding("ORDER-002", "XM-002");
    expect(screen.getByRole("button", { name: COLLECTION_IMPORT.nextStep })).toBeDisabled();

    await chooseBinding("ORDER-003", "XM-002");
    expect(screen.getByRole("button", { name: COLLECTION_IMPORT.nextStep })).toBeDisabled();

    fireEvent.change(screen.getByLabelText(COLLECTION_IMPORT.bindingReasonLabel), {
      target: { value: "合同改派" },
    });
    expect(screen.getByRole("button", { name: COLLECTION_IMPORT.nextStep })).toBeEnabled();
  });

  it("改派订单必须填写理由；新绑定 existing_binding_version 为 null", async () => {
    await renderAndReachStep(2);
    await chooseBinding("ORDER-002", "XM-002");
    await chooseBinding("ORDER-003", "XM-002");
    expect(screen.getByText(COLLECTION_IMPORT.bindingReasonRequired)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: COLLECTION_IMPORT.nextStep })).toBeDisabled();

    fireEvent.change(screen.getByLabelText(COLLECTION_IMPORT.bindingReasonLabel), {
      target: { value: "合同改派，原合同已终止" },
    });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.nextStep }));
    await screen.findByRole("button", { name: COLLECTION_IMPORT.apply });

    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.apply }));
    await waitFor(() => expect(applyCollectionPlan).toHaveBeenCalledTimes(1));
    const [batchId, body] = applyCollectionPlan.mock.calls[0];
    expect(batchId).toBe("batch-1");
    expect(body).toEqual({
      expected_batch_version: 1,
      expected_data_version: "dv-1",
      bindings: [
        {
          row_key: "row-2",
          external_order_no: "ORDER-002",
          project_id: "project-2",
          project_version: 2,
          project_contract_id: "pc-2",
          project_contract_version: 1,
          existing_binding_version: null,
          reason: null,
        },
        {
          row_key: "row-3",
          external_order_no: "ORDER-003",
          project_id: "project-2",
          project_version: 2,
          project_contract_id: "pc-2",
          project_contract_version: 1,
          existing_binding_version: 5,
          reason: "合同改派，原合同已终止",
        },
      ],
    });
  });

  it("绑定搜索：不足 2 字符不发请求，旧请求被取消，page_size 不超过 50", async () => {
    await renderAndReachStep(2);
    const projectSelect = screen.getByLabelText(`${COLLECTION_IMPORT.bindingProjectLabel} ORDER-002`);
    fireEvent.mouseDown(projectSelect);
    fireEvent.change(projectSelect, { target: { value: "X" } });
    await waitFor(() => expect(searchCollectionPlanBindingOptions).not.toHaveBeenCalled());

    fireEvent.change(projectSelect, { target: { value: "XM" } });
    await waitFor(() => expect(searchCollectionPlanBindingOptions).toHaveBeenCalledTimes(1));
    expect(searchCollectionPlanBindingOptions.mock.calls[0][0]).toBe("batch-1");
    expect(searchCollectionPlanBindingOptions.mock.calls[0][1]).toEqual({
      q: "XM",
      page: 1,
      page_size: 50,
    });
    expect(searchCollectionPlanBindingOptions.mock.calls[0][2].signal).toBeInstanceOf(AbortSignal);

    fireEvent.change(projectSelect, { target: { value: "XM-0" } });
    await waitFor(() => expect(searchCollectionPlanBindingOptions).toHaveBeenCalledTimes(2));
    expect(searchCollectionPlanBindingOptions.mock.calls[0][2].signal.aborted).toBe(true);
  });

  it("浏览器只保存当前 binding 选择，不携带整行原始数据", async () => {
    await renderAndReachStep(3);
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.apply }));
    await waitFor(() => expect(applyCollectionPlan).toHaveBeenCalledTimes(1));
    const bindings = applyCollectionPlan.mock.calls[0][1].bindings;
    expect(bindings).toHaveLength(2);
    for (const binding of bindings) {
      expect(Object.keys(binding).sort()).toEqual([
        "existing_binding_version",
        "external_order_no",
        "project_contract_id",
        "project_contract_version",
        "project_id",
        "project_version",
        "reason",
        "row_key",
      ]);
    }
  });

  it("409 保留当前步骤与选择并提示刷新", async () => {
    await renderAndReachStep(3);
    const conflict = Object.assign(new Error("conflict"), {
      response: {
        status: 409,
        data: { detail: { code: "version_conflict", message: "数据已变化，请刷新后重试" } },
      },
    });
    applyCollectionPlan.mockRejectedValueOnce(conflict);
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.apply }));
    expect(await screen.findByText(COLLECTION_IMPORT.versionConflict)).toBeInTheDocument();
    const activeStep = document.querySelector(".ant-steps-item-active");
    expect(activeStep?.textContent).toContain(COLLECTION_IMPORT.stepApply);
    // 选择仍保留在确认页摘要中
    expect(screen.getByText("ORDER-002")).toBeInTheDocument();
    expect(screen.getByText("ORDER-003")).toBeInTheDocument();
  });

  it("应用成功展示新增/更新/未变/来源缺失/计划变更待复核计数", async () => {
    const { onApplied, onClose } = await renderAndReachStep(3);
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.apply }));
    expect(await screen.findByText(COLLECTION_IMPORT.applyResultTitle)).toBeInTheDocument();
    expect(screen.getByText(`${COLLECTION_IMPORT.countCreated} 5`)).toBeInTheDocument();
    expect(screen.getByText(`${COLLECTION_IMPORT.countUpdated} 2`)).toBeInTheDocument();
    expect(screen.getByText(`${COLLECTION_IMPORT.countUnchanged} 10`)).toBeInTheDocument();
    expect(screen.getByText(`${COLLECTION_IMPORT.countSourceMissing} 2`)).toBeInTheDocument();
    expect(screen.getByText(`${COLLECTION_IMPORT.countNeedsReview} 1`)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.complete }));
    expect(onApplied).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("关闭后重新打开从新会话开始，不沿用旧批次与幂等键", async () => {
    const onClose = vi.fn();
    const onApplied = vi.fn();
    const { rerender } = render(
      <CollectionPlanImportModal open onClose={onClose} onApplied={onApplied} />,
    );
    fireEvent.change(screen.getByLabelText(COLLECTION_IMPORT.filePickLabel), {
      target: { files: [pickXls()] },
    });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.previewAction }));
    await screen.findByText(COLLECTION_IMPORT.previewZeroWriteHint);
    const firstKey = previewCollectionPlan.mock.calls[0][1];

    rerender(<CollectionPlanImportModal open={false} onClose={onClose} onApplied={onApplied} />);
    rerender(<CollectionPlanImportModal open onClose={onClose} onApplied={onApplied} />);

    expect(screen.getByLabelText(COLLECTION_IMPORT.filePickLabel)).toBeInTheDocument();
    expect(screen.queryByText("ORDER-001")).toBeNull();
    expect(screen.getByRole("button", { name: COLLECTION_IMPORT.previewAction })).toBeDisabled();

    fireEvent.change(screen.getByLabelText(COLLECTION_IMPORT.filePickLabel), {
      target: { files: [new File(["new-xls"], "新回款计划.xls", { type: "application/vnd.ms-excel" })] },
    });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.previewAction }));
    await waitFor(() => expect(previewCollectionPlan).toHaveBeenCalledTimes(2));
    expect(previewCollectionPlan.mock.calls[1][1]).not.toBe(firstKey);
  });

  it("关闭后丢弃旧会话晚到的预览结果", async () => {
    let resolvePreview!: (value: { data: CollectionPreviewResponse }) => void;
    previewCollectionPlan.mockImplementationOnce(() => new Promise((resolve) => {
      resolvePreview = resolve;
    }));
    const onClose = vi.fn();
    const onApplied = vi.fn();
    const { rerender } = render(
      <CollectionPlanImportModal open onClose={onClose} onApplied={onApplied} />,
    );
    fireEvent.change(screen.getByLabelText(COLLECTION_IMPORT.filePickLabel), {
      target: { files: [pickXls()] },
    });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.previewAction }));
    await waitFor(() => expect(previewCollectionPlan).toHaveBeenCalledTimes(1));

    rerender(<CollectionPlanImportModal open={false} onClose={onClose} onApplied={onApplied} />);
    rerender(<CollectionPlanImportModal open onClose={onClose} onApplied={onApplied} />);
    await act(async () => resolvePreview({ data: previewResponse() }));

    expect(screen.getByLabelText(COLLECTION_IMPORT.filePickLabel)).toBeInTheDocument();
    expect(screen.queryByText("ORDER-001")).toBeNull();
    expect(screen.getByRole("button", { name: COLLECTION_IMPORT.previewAction })).toBeDisabled();
  });

  it("关闭后丢弃旧会话晚到的应用结果", async () => {
    const { rerender, onClose, onApplied } = await renderAndReachStep(3);
    let resolveApply!: (value: {
      data: {
        batch_id: string;
        batch_version: number;
        data_version: string;
        status: "applied";
        counts: { created: number; updated: number; unchanged: number; source_missing: number; needs_review: number };
        idempotent_replay: boolean;
        applied_at: string;
      };
    }) => void;
    applyCollectionPlan.mockImplementationOnce(() => new Promise((resolve) => {
      resolveApply = resolve;
    }));
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_IMPORT.apply }));
    await waitFor(() => expect(applyCollectionPlan).toHaveBeenCalledTimes(1));

    rerender(<CollectionPlanImportModal open={false} onClose={onClose} onApplied={onApplied} />);
    rerender(<CollectionPlanImportModal open onClose={onClose} onApplied={onApplied} />);
    await act(async () => resolveApply({
      data: {
        batch_id: "batch-1",
        batch_version: 2,
        data_version: "dv-late",
        status: "applied",
        counts: { created: 99, updated: 0, unchanged: 0, source_missing: 0, needs_review: 0 },
        idempotent_replay: false,
        applied_at: "2026-08-14T10:00:00Z",
      },
    }));

    expect(screen.getByLabelText(COLLECTION_IMPORT.filePickLabel)).toBeInTheDocument();
    expect(screen.queryByText(COLLECTION_IMPORT.applyResultTitle)).toBeNull();
    expect(screen.queryByText(`${COLLECTION_IMPORT.countCreated} 99`)).toBeNull();
  });
});
