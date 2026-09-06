import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getOptions: vi.fn(),
  preview: vi.fn(),
  apply: vi.fn(),
  download: vi.fn(),
  saveBlob: vi.fn(),
}));

vi.mock("../../../api/maintenanceBatchTransfer", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceBatchTransfer",
  );
  return {
    ...actual,
    getMaintenanceBatchTransferOptions: (...args: unknown[]) => mocks.getOptions(...args),
    previewMaintenanceBatchTransfer: (...args: unknown[]) => mocks.preview(...args),
    applyMaintenanceBatchTransfer: (...args: unknown[]) => mocks.apply(...args),
    downloadMaintenanceBatchTransfer: (...args: unknown[]) => mocks.download(...args),
  };
});

vi.mock("../../../api/maintenanceWorkbooks", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceWorkbooks",
  );
  return { ...actual, saveBlob: (...args: unknown[]) => mocks.saveBlob(...args) };
});

import MaintenanceBatchTransferButton from "../MaintenanceBatchTransferButton";

const options = {
  can_import: true,
  can_download: true,
  max_files: 20,
  accepted_extensions: [".xlsx"],
  import_kinds: [
    {
      key: "sales_contract",
      label: "销售合同",
      required_fields: ["contract_no"],
      accepted_aliases: { contract_no: ["销售单号"] },
      metric_basis: { contract_amount: "含税口径由是否含税列决定" },
    },
  ],
  download_forms: [
    { key: "project", label: "项目清单", default_selected: true },
    { key: "collection", label: "回款明细", default_selected: true },
  ],
  download_fields: [
    {
      key: "project_name",
      label: "项目名称",
      group: "项目",
      form_keys: ["project"],
      default_selected: true,
    },
    {
      key: "collection_received_inc_tax",
      label: "累计已回款",
      group: "金额",
      form_keys: ["project", "collection"],
      default_selected: true,
    },
  ],
  default_forms: ["project", "collection"],
  default_fields: ["project_name", "collection_received_inc_tax"],
};

const preview = {
  schema_version: "maintenance-batch-preview.v1",
  preview_token: "signed-preview-token",
  payload_hash: "a".repeat(64),
  data_version: 17,
  expires_at: "2026-08-28T12:30:00+08:00",
  can_apply: true,
  files: [
    {
      file_id: "f-sales",
      filename: "销售订单.xlsx",
      import_kind: "sales_contract",
      source_sha256: "b".repeat(64),
      detected_sheet: "销售订单",
      header_rows: [1, 2],
      detected_fields: [
        {
          source_column: "DK订单金额",
          canonical_field: "contract_amount",
          canonical_label: "合同金额",
          confidence: "alias",
          required: true,
          metric_basis: "DL是否含税 + DM/DN/DO",
        },
      ],
      mapping_conflicts: [],
    },
  ],
  rows: [
    {
      row_key: "row-matched",
      file_id: "f-sales",
      filename: "销售订单.xlsx",
      detected_sheet: "销售订单",
      source_row: 3,
      canonical: { contract_no: "XSDD-001", contract_amount: "100.00" },
      normalized_key: "XSDD-001",
      idempotency_key: "row-key-1",
      matched_project_id: "p1",
      matched_project_name: "项目一",
      matched_contract_id: "c1",
      match_strategy: "exact_contract_no",
      candidate_count: 1,
      match_state: "matched",
      action: "update_contract",
      row_status: "ready",
      warnings: [],
      errors: [],
    },
    {
      row_key: "row-ambiguous",
      file_id: "f-sales",
      filename: "销售订单.xlsx",
      detected_sheet: "销售订单",
      source_row: 4,
      canonical: { contract_no: "XSDD-002" },
      normalized_key: "XSDD-002",
      idempotency_key: "row-key-2",
      matched_project_id: null,
      matched_project_name: null,
      matched_contract_id: null,
      match_strategy: "candidate",
      candidate_count: 2,
      candidates: [
        { project_id: "p1", project_name: "项目一" },
        { project_id: "p2", project_name: "项目二" },
      ],
      match_state: "ambiguous",
      action: "block",
      row_status: "needs_review",
      warnings: [{ code: "multiple_candidates", message: "命中两个候选项目" }],
      errors: [],
    },
    {
      row_key: "row-unmatched",
      file_id: "f-sales",
      filename: "销售订单.xlsx",
      source_row: 5,
      canonical: { contract_no: "XSDD-003" },
      normalized_key: "XSDD-003",
      idempotency_key: "row-key-3",
      matched_project_id: null,
      matched_project_name: null,
      matched_contract_id: null,
      match_strategy: "none",
      candidate_count: 0,
      match_state: "unmatched",
      action: "create_project",
      row_status: "ready",
      warnings: [],
      errors: [],
    },
    {
      row_key: "row-invalid",
      file_id: "f-sales",
      filename: "销售订单.xlsx",
      source_row: 6,
      canonical: { contract_no: null },
      normalized_key: null,
      idempotency_key: "row-key-4",
      matched_project_id: null,
      matched_project_name: null,
      matched_contract_id: null,
      match_strategy: "none",
      candidate_count: 0,
      match_state: "invalid",
      action: "block",
      row_status: "blocked",
      warnings: [],
      errors: [{ code: "missing_contract_no", message: "缺少销售单号" }],
    },
  ],
  summary: {
    total: 4,
    matched: 1,
    ambiguous: 1,
    unmatched: 1,
    invalid: 1,
    ready: 2,
  },
};

/** D-16 收款单预览：新建 / 覆盖（默认不勾）/ 已在台账 / 台账冲突 四种行。 */
const receiptRow = (overrides: Record<string, unknown>) => ({
  file_id: "f-receipt",
  filename: "收款单.xlsx",
  detected_sheet: "Sheet1",
  normalized_key: "20240101-0001",
  matched_project_id: "p1",
  matched_project_name: "项目一",
  matched_contract_id: "c1",
  match_strategy: "exact_contract_no",
  candidate_count: 1,
  candidates: [],
  match_state: "matched",
  before: null,
  delta: null,
  warnings: [],
  errors: [],
  ...overrides,
});

const receiptPreview = {
  ...preview,
  files: [
    {
      file_id: "f-receipt",
      filename: "收款单.xlsx",
      import_kind: "receipt",
      source_sha256: "c".repeat(64),
      detected_sheet: "Sheet1",
      header_rows: [1],
      detected_fields: [],
      mapping_conflicts: [],
    },
  ],
  rows: [
    receiptRow({
      row_key: "row-create",
      source_row: 4,
      canonical: { sales_order_no: "20240101-0001", report_month: "2026-08-01", cumulative_received_inc_tax: "180.00" },
      idempotency_key: "k-create",
      action: "upsert_collection_snapshot",
      row_status: "ready",
      after: { cumulative_amount: "180.00", new_receipts: 1 },
    }),
    receiptRow({
      row_key: "row-update",
      source_row: 3,
      canonical: { sales_order_no: "20240101-0001", report_month: "2026-07-01", cumulative_received_inc_tax: "130.00" },
      idempotency_key: "k-update",
      action: "update_collection_snapshot",
      row_status: "ready",
      requires_confirmation: true,
      before: { cumulative_amount: "100.00", source: "workbook", import_batch_id: "wb-1", updated_at: "2026-08-02T09:30:00" },
      after: { cumulative_amount: "130.00", new_receipts: 1 },
      warnings: [
        {
          code: "snapshot_overwrite",
          message: "将覆盖 2026-07 已确认累计 100.00 → 130.00（原来源 workbook / 批次 wb-1 / 2026-08-02 09:30）",
          field: "cumulative_received_inc_tax",
        },
      ],
    }),
    receiptRow({
      row_key: "row-known",
      source_row: 2,
      canonical: { receipt_key: "SK-1|XSDD-20240101-0001", sales_order_no: "20240101-0001", receipt_no: "SK-1" },
      idempotency_key: "k-known",
      match_strategy: "none",
      candidate_count: 0,
      action: "skip",
      row_status: "unchanged",
      after: null,
      warnings: [{ code: "receipt_known", message: "收款单 SK-1 已在台账（批次 12，2026-08-01 12:00），本次跳过", field: null }],
    }),
    receiptRow({
      row_key: "row-conflict",
      source_row: 5,
      canonical: { receipt_key: "SK-9|XSDD-20240101-0001", sales_order_no: "20240101-0001" },
      idempotency_key: "k-conflict",
      match_strategy: "none",
      candidate_count: 0,
      match_state: "invalid",
      action: "block",
      row_status: "blocked",
      after: null,
      errors: [
        { code: "receipt_conflict", message: "收款单 SK-9 与台账不一致（台账 100.00 / 2026-01-10，本文件 120.00 / 2026-01-10），需人工裁决，不自动覆盖也不累加", field: null },
        { code: "order_level_fail_closed", message: "销售订单 20240101-0001 存在无效/风险/冲突收款行，禁止从其余行计算部分累计", field: null },
      ],
    }),
  ],
  summary: { total: 4, matched: 3, ambiguous: 0, unmatched: 0, invalid: 1, ready: 2, known: 1, receipt_conflicts: 1, updates: 1 },
};

const applyResult = {
  batch_id: "batch-1",
  status: "done",
  applied: 1,
  skipped: 0,
  blocked: 0,
  project_ids: ["p1"],
  invalidated_projects: ["p1"],
  audit_ref: "audit-1",
  rows: [
    {
      row_key: "row-matched",
      source_file: "销售订单.xlsx",
      source_sheet: "销售订单",
      source_row: 3,
      status: "applied",
      action: "update_contract",
      project_id: "p1",
      contract_id: "c1",
      message: "合同金额已更新",
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getOptions.mockResolvedValue({ data: options });
  mocks.preview.mockResolvedValue({ data: preview });
  mocks.apply.mockResolvedValue({ data: applyResult });
  mocks.download.mockResolvedValue({
    blob: new Blob(["xlsx"]),
    filename: "维保批量导出.xlsx",
  });
});

afterEach(cleanup);

function renderButton(onApplied = vi.fn()) {
  render(
    <MaintenanceBatchTransferButton
      filters={{ lifecycle: "ongoing", card_status: "warning", q: "项目", sort: "name" }}
      onApplied={onApplied}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "批量导入 / 下载" }));
  return onApplied;
}

describe("MaintenanceBatchTransferButton", () => {
  it("拖入多个 xlsx 后展示自动字段映射与四类行筛选", async () => {
    renderButton();
    await screen.findByText("先预览，再提交");

    const input = document.querySelector('input[type="file"]');
    expect(input).not.toBeNull();
    const files = [
      new File(["sales"], "销售订单.xlsx", { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }),
      new File(["receipt"], "回款明细.xlsx", { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }),
    ];
    fireEvent.change(input!, { target: { files } });
    fireEvent.click(await screen.findByRole("button", { name: "自动识别并预览" }));

    await waitFor(() => expect(mocks.preview).toHaveBeenCalledTimes(1));
    expect(mocks.preview.mock.calls[0][0]).toEqual(files);
    expect(await screen.findByText("已匹配 1")).toBeInTheDocument();
    expect(screen.getByText("有歧义 1")).toBeInTheDocument();
    expect(screen.getByText("未匹配 1")).toBeInTheDocument();
    expect(screen.getByText("无效 1")).toBeInTheDocument();

    fireEvent.click(screen.getByText("销售订单.xlsx · 字段映射"));
    expect(await screen.findByText("DK订单金额")).toBeInTheDocument();
    expect(screen.getByText("DL是否含税 + DM/DN/DO")).toBeInTheDocument();
  });

  it("只允许已匹配且 ready 的行提交，并用冻结 token/CAS 获取逐行回执", async () => {
    const onApplied = renderButton();
    await screen.findByText("先预览，再提交");
    const input = document.querySelector('input[type="file"]');
    const file = new File(["sales"], "销售订单.xlsx", { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
    fireEvent.change(input!, { target: { files: [file] } });
    fireEvent.click(await screen.findByRole("button", { name: "自动识别并预览" }));

    expect(await screen.findByText("可提交 1 行，已选 1 行；其余行需修正源文件或后端归属后重新预览。")).toBeInTheDocument();
    expect(screen.getByLabelText("选择 销售订单.xlsx 第 3 行")).toBeChecked();
    expect(screen.getByLabelText("有歧义行不可提交")).toBeDisabled();
    expect(screen.getByLabelText("未匹配行不可提交")).toBeDisabled();
    expect(screen.getByLabelText("无效行不可提交")).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "提交 1 行" }));
    await waitFor(() => expect(mocks.apply).toHaveBeenCalledWith({
      preview_token: "signed-preview-token",
      payload_hash: "a".repeat(64),
      data_version: 17,
      row_keys: ["row-matched"],
    }));
    expect(mocks.apply.mock.calls[0][0]).not.toHaveProperty("canonical");
    expect(mocks.apply.mock.calls[0][0]).not.toHaveProperty("mapping");
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("逐行提交结果")).toBeInTheDocument();
    expect(screen.getByText("合同金额已更新")).toBeInTheDocument();
  });

  it("收款单预览：覆盖行可勾选但默认不勾，已在台账与台账冲突有标签", async () => {
    mocks.preview.mockResolvedValue({ data: receiptPreview });
    mocks.apply.mockResolvedValue({
      data: {
        ...applyResult,
        applied: 2,
        rows: [
          {
            row_key: "row-update",
            source_file: "收款单.xlsx",
            source_sheet: "Sheet1",
            source_row: 3,
            status: "applied",
            action: "update_collection_snapshot",
            project_id: "p1",
            message: "XSDD-20240101-0001 2026-07 已覆盖：原值 100.00 → 新值 130.00（原来源 workbook/批次 wb-1/2026-08-02 09:30），登记 1 笔收款入台账",
            before_amount: "100.00",
            after_amount: "130.00",
            previous_source: "workbook",
          },
        ],
      },
    });
    renderButton();
    await screen.findByText("先预览，再提交");
    const input = document.querySelector('input[type="file"]');
    const file = new File(["receipt"], "收款单.xlsx", { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
    fireEvent.change(input!, { target: { files: [file] } });
    fireEvent.click(await screen.findByRole("button", { name: "自动识别并预览" }));

    // create 行默认勾选；update 行可勾选但默认不勾
    expect(await screen.findByText("可提交 2 行，已选 1 行；其余行需修正源文件或后端归属后重新预览。")).toBeInTheDocument();
    expect(screen.getByLabelText("选择 收款单.xlsx 第 4 行")).toBeChecked();
    const overwrite = screen.getByLabelText("确认覆盖 收款单.xlsx 第 3 行");
    expect(overwrite).not.toBeChecked();
    expect(overwrite).not.toBeDisabled();
    expect(screen.getByText("其中 1 行会覆盖既有已确认累计，默认未勾选，已确认覆盖 0 行。")).toBeInTheDocument();
    // 覆盖提示列：原值→新值 + 原来源
    expect(screen.getByText("将覆盖 2026-07 已确认累计 100.00 → 130.00（原来源 workbook / 批次 wb-1 / 2026-08-02 09:30）")).toBeInTheDocument();
    expect(screen.getByText("需确认覆盖")).toBeInTheDocument();
    expect(screen.getByText("覆盖已确认累计")).toBeInTheDocument();
    // 已在台账 / 台账冲突标签，且都不可提交
    expect(screen.getByText("已在台账")).toBeInTheDocument();
    expect(screen.getByText("台账冲突")).toBeInTheDocument();
    expect(screen.getByLabelText("无效行不可提交")).toBeDisabled();
    expect(screen.getAllByLabelText("已匹配行不可提交")).toHaveLength(1);

    // 显式勾选覆盖行后才随 apply 提交
    fireEvent.click(overwrite);
    expect(await screen.findByText("其中 1 行会覆盖既有已确认累计，默认未勾选，已确认覆盖 1 行。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "提交 2 行" }));
    await waitFor(() => expect(mocks.apply).toHaveBeenCalledTimes(1));
    expect([...mocks.apply.mock.calls[0][0].row_keys].sort()).toEqual(["row-create", "row-update"]);
    expect(await screen.findByText(/原值 100.00 → 新值 130.00/)).toBeInTheDocument();
  });

  it("批量下载按服务端表单/字段白名单并携带主页当前筛选", async () => {
    renderButton();
    await screen.findByText("先预览，再提交");
    fireEvent.click(screen.getByRole("tab", { name: "批量下载" }));
    expect(await screen.findByText("导出范围服从维保主页当前筛选")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "下载当前筛选全部项目" }));
    await waitFor(() => expect(mocks.download).toHaveBeenCalledWith({
      lifecycle: "ongoing",
      card_status: "warning",
      q: "项目",
      sort: "name",
      forms: ["project", "collection"],
      fields: ["project_name", "collection_received_inc_tax"],
    }));
    expect(mocks.saveBlob).toHaveBeenCalledWith(
      expect.any(Blob),
      "维保批量导出.xlsx",
    );
  });
});
