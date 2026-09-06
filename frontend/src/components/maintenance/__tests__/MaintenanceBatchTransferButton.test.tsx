import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getOptions: vi.fn(),
  preview: vi.fn(),
  apply: vi.fn(),
  download: vi.fn(),
  saveBlob: vi.fn(),
  ruling: vi.fn(),
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
    ruleMaintenanceReceiptConflict: (...args: unknown[]) => mocks.ruling(...args),
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

/**
 * D-16 依赖联动：06 月新建（无依赖）/ 07 月覆盖（默认不勾）/ 08 月新建依赖 07 覆盖行与 06 月 /
 * 09 月新建依赖 08 月；另一合同 10 月新建依赖一条被「需先建账」阻断的行。
 */
const dependencyPreview = {
  ...preview,
  files: receiptPreview.files,
  rows: [
    receiptRow({
      row_key: "row-jun",
      source_row: 2,
      canonical: { sales_order_no: "20240101-0001", report_month: "2026-06-01", cumulative_received_inc_tax: "80.00" },
      idempotency_key: "k-jun",
      action: "upsert_collection_snapshot",
      row_status: "ready",
      after: { cumulative_amount: "80.00", new_receipts: 1 },
      depends_on_row_keys: [],
      hint_messages: [],
    }),
    receiptRow({
      row_key: "row-jul",
      source_row: 3,
      canonical: { sales_order_no: "20240101-0001", report_month: "2026-07-01", cumulative_received_inc_tax: "130.00" },
      idempotency_key: "k-jul",
      action: "update_collection_snapshot",
      row_status: "ready",
      requires_confirmation: true,
      before: { cumulative_amount: "100.00", source: "workbook" },
      after: { cumulative_amount: "130.00", new_receipts: 1 },
      depends_on_row_keys: ["row-jun"],
      hint_messages: [],
      warnings: [{ code: "snapshot_overwrite", message: "将覆盖 2026-07 已确认累计 100.00 → 130.00（原来源 workbook）", field: null }],
    }),
    receiptRow({
      row_key: "row-aug",
      source_row: 4,
      canonical: { sales_order_no: "20240101-0001", report_month: "2026-08-01", cumulative_received_inc_tax: "180.00" },
      idempotency_key: "k-aug",
      action: "upsert_collection_snapshot",
      row_status: "ready",
      after: { cumulative_amount: "180.00", new_receipts: 1 },
      depends_on_row_keys: ["row-jul", "row-jun"],
      hint_messages: ["2026-08 累计依赖 2026-07 的覆盖行，需一起勾选"],
      warnings: [{ code: "depends_on_update", message: "2026-08 累计依赖 2026-07 的覆盖行，需一起勾选", field: null }],
    }),
    receiptRow({
      row_key: "row-sep",
      source_row: 5,
      canonical: { sales_order_no: "20240101-0001", report_month: "2026-09-01", cumulative_received_inc_tax: "200.00" },
      idempotency_key: "k-sep",
      action: "upsert_collection_snapshot",
      row_status: "ready",
      after: { cumulative_amount: "200.00", new_receipts: 1 },
      depends_on_row_keys: ["row-aug"],
      hint_messages: ["需同勾更早月份：2026-06、2026-07、2026-08"],
      warnings: [{ code: "requires_earlier_months", message: "需同勾更早月份：2026-06、2026-07、2026-08", field: null }],
    }),
    receiptRow({
      row_key: "row-orphan",
      source_row: 6,
      normalized_key: "20240101-0002",
      matched_project_id: "p2",
      matched_project_name: "项目二",
      matched_contract_id: "c2",
      canonical: { sales_order_no: "20240101-0002", report_month: "2026-10-01", cumulative_received_inc_tax: "50.00" },
      idempotency_key: "k-orphan",
      action: "upsert_collection_snapshot",
      row_status: "ready",
      after: { cumulative_amount: "50.00", new_receipts: 1 },
      depends_on_row_keys: ["row-blocked"],
      hint_messages: ["需同勾更早月份：2026-05"],
    }),
    receiptRow({
      row_key: "row-blocked",
      source_row: 7,
      normalized_key: "20240101-0002",
      matched_project_id: "p2",
      matched_project_name: "项目二",
      matched_contract_id: "c2",
      canonical: { sales_order_no: "20240101-0002", report_month: "2026-05-01", cumulative_received_inc_tax: "20.00" },
      idempotency_key: "k-blocked",
      match_state: "invalid",
      action: "block",
      row_status: "blocked",
      after: null,
      depends_on_row_keys: [],
      hint_messages: ["该合同已有确认快照但尚无收款单台账，请先上传该合同的全量历史收款单导出建账"],
      errors: [{ code: "seed_required", message: "该合同已有确认快照但尚无收款单台账，请先上传该合同的全量历史收款单导出建账", field: null }],
    }),
  ],
  summary: { total: 6, matched: 5, ambiguous: 0, unmatched: 0, invalid: 1, ready: 5, updates: 1 },
};

/** D-16 新增码：快照作废（合同级）/ 跨文件同合同 / 累计无法核验 / 按台账新建 / 仅登记台账。 */
const codesPreview = {
  ...preview,
  files: receiptPreview.files,
  rows: [
    receiptRow({
      row_key: "row-voided",
      source_row: 2,
      canonical: { sales_order_no: "20240101-0001", report_month: "2026-03-01" },
      idempotency_key: "k-voided",
      match_state: "invalid",
      action: "block",
      row_status: "blocked",
      after: null,
      hint_messages: ["该合同存在已作废快照，整个合同各月份均需人工处理后再导入"],
      errors: [{ code: "snapshot_voided", message: "该合同存在已作废快照，整个合同各月份均需人工处理后再导入", field: null }],
    }),
    receiptRow({
      row_key: "row-cross",
      source_row: 3,
      canonical: { sales_order_no: "20240101-0003", report_month: "2026-03-01" },
      idempotency_key: "k-cross",
      match_state: "invalid",
      action: "block",
      row_status: "blocked",
      after: null,
      hint_messages: ["同一批次两个文件触及同一合同 20240101-0003，请只保留一个文件"],
      errors: [{ code: "cross_file_same_contract", message: "同一批次两个文件触及同一合同 20240101-0003，请只保留一个文件", field: null }],
    }),
    receiptRow({
      row_key: "row-unverifiable",
      source_row: 4,
      canonical: { sales_order_no: "20240101-0004", report_month: "2026-03-01" },
      idempotency_key: "k-unverifiable",
      match_state: "invalid",
      action: "block",
      row_status: "blocked",
      after: null,
      hint_messages: ["台账无法推导出 2026-03 已确认累计，请核对历史收款单"],
      errors: [{ code: "cumulative_unverifiable", message: "台账无法推导出 2026-03 已确认累计，请核对历史收款单", field: null }],
    }),
    receiptRow({
      row_key: "row-backfill",
      source_row: 5,
      canonical: { sales_order_no: "20240101-0005", report_month: "2026-03-01", cumulative_received_inc_tax: "60.00" },
      idempotency_key: "k-backfill",
      action: "upsert_collection_snapshot",
      row_status: "ready",
      after: { cumulative_amount: "60.00", new_receipts: 0 },
      hint_messages: ["该月只有台账收款、尚无快照，将按台账新建"],
      warnings: [{ code: "ledger_backfill", message: "该月只有台账收款、尚无快照，将按台账新建", field: null }],
    }),
    receiptRow({
      row_key: "row-record",
      source_row: 6,
      canonical: { sales_order_no: "20240101-0006", report_month: "2026-03-01", cumulative_received_inc_tax: "70.00" },
      idempotency_key: "k-record",
      action: "record_receipts",
      row_status: "ready",
      after: { cumulative_amount: "70.00", new_receipts: 2 },
      warnings: [{ code: "record_receipts", message: "累计不变，只把 2 笔新收款登记入台账", field: null }],
    }),
  ],
  summary: { total: 5, matched: 2, ambiguous: 0, unmatched: 0, invalid: 3, ready: 2 },
};

const rulingResult = {
  ruling_id: "r-1",
  superseded_receipt_id: "9",
  new_receipt_id: "10",
  affected_months: [{ report_month: "2026-01-01", current_cumulative: "100.00", derived_cumulative: "120.00" }],
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
  localStorage.removeItem("role");
  mocks.getOptions.mockResolvedValue({ data: options });
  mocks.preview.mockResolvedValue({ data: preview });
  mocks.apply.mockResolvedValue({ data: applyResult });
  mocks.ruling.mockResolvedValue(rulingResult);
  mocks.download.mockResolvedValue({
    blob: new Blob(["xlsx"]),
    filename: "维保批量导出.xlsx",
  });
});

afterEach(() => {
  cleanup();
  message.destroy();
  localStorage.removeItem("role");
});

/** 打开弹窗、拖入一个收款单文件并触发预览。 */
async function previewReceiptFile(onApplied = vi.fn()) {
  renderButton(onApplied);
  await screen.findByText("先预览，再提交");
  const input = document.querySelector('input[type="file"]');
  const file = new File(["receipt"], "收款单.xlsx", { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
  fireEvent.change(input!, { target: { files: [file] } });
  fireEvent.click(await screen.findByRole("button", { name: "自动识别并预览" }));
  await waitFor(() => expect(mocks.preview).toHaveBeenCalledTimes(1));
  return onApplied;
}

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

  it("依赖行默认不勾；勾选依赖行自动带上非覆盖依赖并提示，覆盖行须手动确认；取消被依赖行时一并取消", async () => {
    mocks.preview.mockResolvedValue({ data: dependencyPreview });
    await previewReceiptFile();

    // 默认只勾 06 月：07 月是覆盖行，08/09 月依赖它，10 月依赖被阻断的行
    expect(await screen.findByText("可提交 5 行，已选 1 行；其余行需修正源文件或后端归属后重新预览。")).toBeInTheDocument();
    expect(screen.getByLabelText("选择 收款单.xlsx 第 2 行")).toBeChecked();
    expect(screen.getByLabelText("确认覆盖 收款单.xlsx 第 3 行")).not.toBeChecked();
    expect(screen.getByLabelText("选择 收款单.xlsx 第 4 行")).not.toBeChecked();
    expect(screen.getByLabelText("选择 收款单.xlsx 第 5 行")).not.toBeChecked();
    expect(screen.getByLabelText("选择 收款单.xlsx 第 6 行")).not.toBeChecked();
    // 依赖说明必须可见，不能只放悬停
    expect(screen.getByText("2026-08 累计依赖 2026-07 的覆盖行，需一起勾选")).toBeInTheDocument();
    expect(screen.getByText("需同勾更早月份：2026-06、2026-07、2026-08")).toBeInTheDocument();
    expect(screen.getByText("依赖覆盖行")).toBeInTheDocument();
    expect(screen.getByText("需同勾更早月份")).toBeInTheDocument();

    // 勾 09 月 → 自动带上 08 月；07 月是覆盖行，D-16 要求手动逐行确认，不自动带上，提交先禁用
    fireEvent.click(screen.getByLabelText("选择 收款单.xlsx 第 5 行"));
    expect(await screen.findByText(/已同时勾选其依赖的 1 行：收款单\.xlsx 第 4 行/)).toBeInTheDocument();
    expect(await screen.findByText(/覆盖既有已确认累计的行须手动逐行勾选确认：收款单\.xlsx 第 3 行/)).toBeInTheDocument();
    expect(screen.getByText("可提交 5 行，已选 3 行；其余行需修正源文件或后端归属后重新预览。")).toBeInTheDocument();
    expect(screen.getByLabelText("确认覆盖 收款单.xlsx 第 3 行")).not.toBeChecked();
    expect(screen.getByLabelText("选择 收款单.xlsx 第 4 行")).toBeChecked();
    expect(screen.getByText(/勾选不一致，无法提交：/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "提交 3 行" })).toBeDisabled();
    // 手动勾上 07 月覆盖行 → 勾选一致，可提交
    fireEvent.click(screen.getByLabelText("确认覆盖 收款单.xlsx 第 3 行"));
    expect(await screen.findByText("其中 1 行会覆盖既有已确认累计，默认未勾选，已确认覆盖 1 行。")).toBeInTheDocument();
    expect(screen.getByText("可提交 5 行，已选 4 行；其余行需修正源文件或后端归属后重新预览。")).toBeInTheDocument();
    expect(screen.queryByText(/勾选不一致/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "提交 4 行" })).not.toBeDisabled();

    // 取消 07 月覆盖行 → 依赖它的 08/09 月一并取消
    fireEvent.click(screen.getByLabelText("确认覆盖 收款单.xlsx 第 3 行"));
    expect(await screen.findByText(/已同时取消依赖它的 2 行/)).toBeInTheDocument();
    expect(screen.getByText("可提交 5 行，已选 1 行；其余行需修正源文件或后端归属后重新预览。")).toBeInTheDocument();
    expect(screen.getByLabelText("选择 收款单.xlsx 第 4 行")).not.toBeChecked();
    expect(screen.getByLabelText("选择 收款单.xlsx 第 5 行")).not.toBeChecked();

    // 再勾 09 月（自动带上 08 月）并手动确认 07 月覆盖行后提交：四行一起进 apply
    fireEvent.click(screen.getByLabelText("选择 收款单.xlsx 第 5 行"));
    await screen.findAllByText(/已同时勾选其依赖的 1 行/);
    fireEvent.click(screen.getByLabelText("确认覆盖 收款单.xlsx 第 3 行"));
    fireEvent.click(await screen.findByRole("button", { name: "提交 4 行" }));
    await waitFor(() => expect(mocks.apply).toHaveBeenCalledTimes(1));
    expect([...mocks.apply.mock.calls[0][0].row_keys].sort()).toEqual(["row-aug", "row-jul", "row-jun", "row-sep"]);
  });

  it("依赖行不可勾选时提交按钮禁用并写明原因；需先建账标签与说明可见", async () => {
    mocks.preview.mockResolvedValue({ data: dependencyPreview });
    await previewReceiptFile();
    await screen.findByText("可提交 5 行，已选 1 行；其余行需修正源文件或后端归属后重新预览。");
    expect(screen.getByText("需先建账")).toBeInTheDocument();
    expect(screen.getByText("该合同已有确认快照但尚无收款单台账，请先上传该合同的全量历史收款单导出建账")).toBeInTheDocument();
    expect(screen.getByText("需同勾更早月份：2026-05")).toBeInTheDocument();

    // 10 月依赖被阻断的 05 月行：勾上后无法自动补齐依赖 → 提交禁用并说明
    fireEvent.click(screen.getByLabelText("选择 收款单.xlsx 第 6 行"));
    expect(await screen.findByText("勾选不一致，无法提交：收款单.xlsx 第 6 行 依赖 收款单.xlsx 第 7 行")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "提交 2 行" })).toBeDisabled();

    fireEvent.click(screen.getByLabelText("选择 收款单.xlsx 第 6 行"));
    await waitFor(() => expect(screen.getByRole("button", { name: "提交 1 行" })).not.toBeDisabled());
    expect(screen.queryByText(/勾选不一致/)).not.toBeInTheDocument();
  });

  it("D-16 新增码有标签，说明文字可见", async () => {
    mocks.preview.mockResolvedValue({ data: codesPreview });
    await previewReceiptFile();
    await screen.findByText("可提交 2 行，已选 2 行；其余行需修正源文件或后端归属后重新预览。");
    expect(screen.getByText("快照已作废")).toBeInTheDocument();
    expect(screen.getByText("该合同存在已作废快照，整个合同各月份均需人工处理后再导入")).toBeInTheDocument();
    expect(screen.getByText("跨文件同合同")).toBeInTheDocument();
    expect(screen.getByText("同一批次两个文件触及同一合同 20240101-0003，请只保留一个文件")).toBeInTheDocument();
    expect(screen.getByText("累计无法核验")).toBeInTheDocument();
    expect(screen.getByText("台账无法推导出 2026-03 已确认累计，请核对历史收款单")).toBeInTheDocument();
    expect(screen.getByText("按台账新建")).toBeInTheDocument();
    expect(screen.getByText("该月只有台账收款、尚无快照，将按台账新建")).toBeInTheDocument();
    expect(screen.getByText("仅登记台账")).toBeInTheDocument();
    expect(screen.getByText("登记收款台账")).toBeInTheDocument();
    expect(screen.getAllByLabelText("无效行不可提交")).toHaveLength(3);
  });

  it("台账冲突：非管理员只见标签，不能裁决", async () => {
    localStorage.setItem("role", "sales");
    mocks.preview.mockResolvedValue({ data: receiptPreview });
    await previewReceiptFile();
    expect(await screen.findByText("台账冲突")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "以本文件为准（人工裁决）" })).not.toBeInTheDocument();
  });

  it("台账冲突：老板可人工裁决，须填原因，成功后自动重新预览", async () => {
    localStorage.setItem("role", "boss");
    mocks.preview.mockResolvedValue({ data: receiptPreview });
    await previewReceiptFile();
    fireEvent.click(await screen.findByRole("button", { name: "以本文件为准（人工裁决）" }));

    expect(await screen.findByText("人工裁决：以本文件为准")).toBeInTheDocument();
    expect(screen.getByText("100.00 / 2026-01-10")).toBeInTheDocument();
    expect(screen.getByText("120.00 / 2026-01-10")).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "确认裁决" });
    expect(confirm).toBeDisabled();

    fireEvent.change(screen.getByLabelText("裁决原因"), { target: { value: "  已核对银行回单，以本文件为准 " } });
    await waitFor(() => expect(confirm).not.toBeDisabled());
    fireEvent.click(confirm);

    await waitFor(() => expect(mocks.ruling).toHaveBeenCalledWith({
      contract_no: "20240101-0001",
      receipt_no: "SK-9",
      receipt_date: "2026-01-10",
      actual_amount: "120.00",
      reason: "已核对银行回单，以本文件为准",
    }));
    await waitFor(() => expect(mocks.preview).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/已裁决收款单 SK-9/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("人工裁决：以本文件为准")).not.toBeInTheDocument());
  });

  it("台账冲突：共享口令管理员裁决被 403 时在弹窗内提示实名要求，不重新预览", async () => {
    localStorage.setItem("role", "admin");
    mocks.preview.mockResolvedValue({ data: receiptPreview });
    mocks.ruling.mockRejectedValue({
      response: { status: 403, data: { detail: { code: "permission_denied", message: "经营事实写入必须使用实名系统账号" } } },
    });
    await previewReceiptFile();
    fireEvent.click(await screen.findByRole("button", { name: "以本文件为准（人工裁决）" }));
    fireEvent.change(await screen.findByLabelText("裁决原因"), { target: { value: "核对回单" } });
    const confirm = screen.getByRole("button", { name: "确认裁决" });
    await waitFor(() => expect(confirm).not.toBeDisabled());
    fireEvent.click(confirm);

    expect(await screen.findByText("经营事实写入必须使用实名系统账号")).toBeInTheDocument();
    expect(mocks.preview).toHaveBeenCalledTimes(1);
    expect(screen.getByText("人工裁决：以本文件为准")).toBeInTheDocument();
  });

  it("apply 返回 stale_preview 时提示重新预览并清空预览状态", async () => {
    mocks.apply.mockRejectedValue({
      response: { status: 409, data: { detail: { code: "stale_preview", message: "预览后台账/快照已变化，请重新预览" } } },
    });
    const onApplied = await previewReceiptFile();
    fireEvent.click(await screen.findByRole("button", { name: "提交 1 行" }));

    expect(await screen.findByText("预览后台账/快照已变化，请重新预览——预览已清除，请重新点击「自动识别并预览」")).toBeInTheDocument();
    expect(screen.queryByText("行匹配预览")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^提交 \d+ 行$/ })).not.toBeInTheDocument();
    expect(onApplied).not.toHaveBeenCalled();
    // 文件仍在，可直接重新预览
    const previewButton = screen.getByRole("button", { name: "自动识别并预览" });
    expect(previewButton).not.toBeDisabled();
    fireEvent.click(previewButton);
    await waitFor(() => expect(mocks.preview).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("行匹配预览")).toBeInTheDocument();
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
