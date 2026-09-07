import {
  CloudDownloadOutlined,
  CloudUploadOutlined,
  InboxOutlined,
  SwapOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Collapse,
  Descriptions,
  Divider,
  Input,
  Modal,
  Row,
  Segmented,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
  Upload,
  message,
} from "antd";
import type { UploadFile, UploadProps } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Key } from "react";

import {
  applyMaintenanceBatchTransfer,
  downloadMaintenanceBatchTransfer,
  getMaintenanceBatchTransferOptions,
  previewMaintenanceBatchTransfer,
  ruleMaintenanceReceiptConflict,
  type MaintenanceBatchApplyResponse,
  type MaintenanceBatchDownloadField,
  type MaintenanceBatchDownloadInput,
  type MaintenanceBatchMatchState,
  type MaintenanceBatchPreviewFile,
  type MaintenanceBatchPreviewResponse,
  type MaintenanceBatchPreviewRow,
  type MaintenanceBatchTransferOptions,
} from "../../api/maintenanceBatchTransfer";
import { saveBlob } from "../../api/maintenanceWorkbooks";

const { Text, Paragraph } = Typography;
const DEFAULT_MAX_FILES = 20;

const MATCH_LABELS: Record<MaintenanceBatchMatchState, string> = {
  matched: "已匹配",
  ambiguous: "有歧义",
  unmatched: "未匹配",
  invalid: "无效",
};

const MATCH_COLORS: Record<MaintenanceBatchMatchState, string> = {
  matched: "green",
  ambiguous: "orange",
  unmatched: "default",
  invalid: "red",
};

const ACTION_LABELS: Record<string, string> = {
  create_project: "新建项目",
  create_contract: "新建合同",
  update_contract: "更新合同",
  upsert_collection_snapshot: "更新回款快照",
  update_collection_snapshot: "覆盖已确认累计",
  record_receipts: "登记收款台账",
  skip: "跳过",
  block: "阻断",
};

/** D-16 收款单台账：行级提示标签（已入账 / 台账冲突 / 需确认覆盖 / 建账与依赖类阻断）。 */
const RECEIPT_HINTS: Record<string, { label: string; color: string }> = {
  receipt_known: { label: "已在台账", color: "default" },
  known: { label: "已在台账", color: "default" },
  receipt_conflict: { label: "台账冲突", color: "red" },
  snapshot_overwrite: { label: "需确认覆盖", color: "orange" },
  requires_earlier_months: { label: "需同勾更早月份", color: "gold" },
  depends_on_update: { label: "依赖覆盖行", color: "gold" },
  record_receipts: { label: "仅登记台账", color: "cyan" },
  seed_required: { label: "需先建账", color: "red" },
  snapshot_voided: { label: "快照已作废", color: "red" },
  cross_file_same_contract: { label: "跨文件同合同", color: "red" },
  cumulative_unverifiable: { label: "累计无法核验", color: "red" },
  ledger_backfill: { label: "按台账新建", color: "blue" },
  stale_preview: { label: "预览已失效", color: "red" },
};

/**
 * 这些码的说明必须可见（不能只放悬停）：后端 hint_messages 之外，也把 issue.message 直接展示。
 * 台账冲突 / 已在台账的「台账 vs 本文件」句子对不能裁决的角色同样要可见，否则冲突只剩一个标签。
 */
const VISIBLE_HINT_CODES = new Set([
  "requires_earlier_months",
  "depends_on_update",
  "seed_required",
  "cumulative_unverifiable",
  "cross_file_same_contract",
  "snapshot_voided",
  "ledger_backfill",
  "stale_preview",
  "receipt_conflict",
  "receipt_known",
  "known",
]);

/** 台账冲突人工裁决只开放给 admin / boss（后端再校验实名账号与利润权限）。 */
const RULING_ROLES = new Set(["admin", "boss"]);

type MatchFilter = "all" | MaintenanceBatchMatchState;

export interface MaintenanceBatchTransferButtonProps {
  filters: Omit<MaintenanceBatchDownloadInput, "forms" | "fields">;
  onApplied: () => void | Promise<unknown>;
}

function readDetail(data: unknown): string | null {
  if (!data || typeof data !== "object" || !("detail" in data)) return null;
  const detail = (data as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  return null;
}

async function errorMessage(error: unknown, fallback: string): Promise<string> {
  const data = (error as { response?: { data?: unknown } })?.response?.data;
  if (data instanceof Blob) {
    try {
      return readDetail(JSON.parse(await data.text())) ?? fallback;
    } catch {
      return fallback;
    }
  }
  return readDetail(data) ?? fallback;
}

function readDetailCode(data: unknown): string | null {
  if (!data || typeof data !== "object" || !("detail" in data)) return null;
  const detail = (data as { detail: unknown }).detail;
  if (detail && typeof detail === "object" && "code" in detail) {
    return String((detail as { code: unknown }).code);
  }
  return null;
}

function errorStatus(error: unknown): number | null {
  return (error as { response?: { status?: number } })?.response?.status ?? null;
}

/** apply 时预览凭证已失效（预览后台账/快照已变化）：按码或后端固定文案识别，409/422 都可能。 */
function isStalePreviewError(error: unknown): boolean {
  const status = errorStatus(error);
  if (status !== 409 && status !== 422) return false;
  const data = (error as { response?: { data?: unknown } })?.response?.data;
  return readDetailCode(data) === "stale_preview" || (readDetail(data) ?? "").includes("请重新预览");
}

/**
 * apply 的终态失败：后端已把批次记为 failed（409 apply_conflict / stale_preview、
 * 422 business_rule_violation / invalid_selection、未预期异常 5xx），预览凭证随之作废，
 * 再点提交只会得到「该预览已失败或失效」。403（实名门禁 / 范围）不是终态：批次仍 processing，
 * 去掉覆盖行重提或换实名账号都行，预览必须保留。
 */
function isTerminalApplyFailure(error: unknown): boolean {
  const status = errorStatus(error);
  return status === 409 || status === 422 || (status !== null && status >= 500);
}

function terminalApplyFallback(error: unknown): string {
  if (isStalePreviewError(error)) return "预览后台账/快照已变化，请重新预览";
  const status = errorStatus(error);
  if (status === 409) return "预览已过期或数据版本已变化，请重新预览";
  if (status === 422) return "批量提交被拒绝，本次预览已作废，请重新预览";
  return "批量提交失败，本次预览已作废，请重新预览";
}

function canRuleReceiptConflict(): boolean {
  return RULING_ROLES.has(localStorage.getItem("role") ?? "");
}

function rawFile(file: UploadFile): File | null {
  return file.originFileObj ?? (file as unknown as File);
}

function rowCanApply(row: MaintenanceBatchPreviewRow): boolean {
  return row.match_state === "matched"
    && row.row_status === "ready"
    && row.action !== "skip"
    && row.action !== "block"
    && row.errors.length === 0;
}

/** 覆盖既有已确认累计的行：可勾选，但绝不默认勾选（D-16）。 */
function rowNeedsConfirmation(row: MaintenanceBatchPreviewRow): boolean {
  return row.requires_confirmation === true || row.action === "update_collection_snapshot";
}

/** 本行必须与之同勾的行键：更早月份累计行、本行新建所依赖的覆盖行（D-16）。 */
function rowDependencies(row: MaintenanceBatchPreviewRow): string[] {
  return row.depends_on_row_keys ?? [];
}

function rowLabel(row: MaintenanceBatchPreviewRow | undefined, key?: string): string {
  return row ? `${row.filename} 第 ${row.source_row} 行` : `行 ${key ?? "?"}（不在本次预览中）`;
}

/** 行键 → 直接依赖 / 直接被谁依赖，两个方向都建，勾选联动时各走一边。 */
function dependencyEdges(rows: MaintenanceBatchPreviewRow[]) {
  const dependencies = new Map<string, string[]>();
  const dependents = new Map<string, string[]>();
  rows.forEach((row) => {
    dependencies.set(row.row_key, rowDependencies(row));
    rowDependencies(row).forEach((dep) => {
      dependents.set(dep, [...(dependents.get(dep) ?? []), row.row_key]);
    });
  });
  return { dependencies, dependents };
}

/** 沿 edges 求传递闭包（不含起点本身）。 */
function closure(start: Iterable<string>, edges: Map<string, string[]>): Set<string> {
  const seen = new Set<string>();
  const stack = [...start];
  while (stack.length) {
    const key = stack.pop() as string;
    (edges.get(key) ?? []).forEach((next) => {
      if (!seen.has(next)) {
        seen.add(next);
        stack.push(next);
      }
    });
  }
  return seen;
}

/**
 * 默认勾选：可提交且不需确认覆盖的行，再迭代剔除依赖未全部默认勾选的行——
 * 新建行依赖默认不勾的覆盖行 / 更早月份行时，默认提交必被后端整批拒绝（D-16）。
 */
function defaultSelectedKeys(rows: MaintenanceBatchPreviewRow[]): string[] {
  const selected = new Set(
    rows.filter((row) => rowCanApply(row) && !rowNeedsConfirmation(row)).map((row) => row.row_key),
  );
  let changed = true;
  while (changed) {
    changed = false;
    rows.forEach((row) => {
      if (selected.has(row.row_key) && rowDependencies(row).some((dep) => !selected.has(dep))) {
        selected.delete(row.row_key);
        changed = true;
      }
    });
  }
  return rows.filter((row) => selected.has(row.row_key)).map((row) => row.row_key);
}

/** 行级提示标签：同一码在合同级 fail-closed 时可能重复出现（各月各一条），只渲染一个标签。 */
function receiptHints(row: MaintenanceBatchPreviewRow) {
  const seen = new Set<string>();
  const hints: { label: string; color: string; code: string; message: string }[] = [];
  [...row.errors, ...row.warnings].forEach((issue) => {
    if (!(issue.code in RECEIPT_HINTS) || seen.has(issue.code)) return;
    seen.add(issue.code);
    hints.push({ ...RECEIPT_HINTS[issue.code], code: issue.code, message: issue.message });
  });
  return hints;
}

/** 可见提示文本：后端 hint_messages 优先，再补上必须可见的码的 message（按文本去重）。 */
function hintTexts(row: MaintenanceBatchPreviewRow): string[] {
  const texts = new Set(row.hint_messages ?? []);
  [...row.errors, ...row.warnings].forEach((issue) => {
    if (VISIBLE_HINT_CODES.has(issue.code)) texts.add(issue.message);
  });
  return [...texts];
}

interface RulingSubject {
  contract_no: string;
  receipt_no: string;
  receipt_date: string | null;
  actual_amount: string | null;
  ledger_amount: string | null;
  ledger_date: string | null;
  message: string;
}

function valueText(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  return String(value);
}

function dateText(value: unknown): string | null {
  const raw = valueText(value);
  return raw && /^\d{4}-\d{2}-\d{2}/.test(raw) ? raw.slice(0, 10) : null;
}

/**
 * 台账冲突行 → 裁决所需的合同 / 收款单号 / 文件值 / 台账值。契约字段优先：
 * canonical.{receipt_no, receipt_date, actual_amount} 是本文件值，before.{…} 是台账值；
 * 老预览没有这些字段时才退回解析固定文案「台账 金额 / 日期，本文件 金额 / 日期」和
 * receipt_key 前缀；取不到就留空，由界面拒绝提交——绝不猜值（REQ #56 / #57）。
 */
function rulingSubject(row: MaintenanceBatchPreviewRow): RulingSubject | null {
  const conflict = [...row.errors, ...row.warnings].find((issue) => issue.code === "receipt_conflict");
  if (!conflict) return null;
  const receiptKey = valueText(row.canonical.receipt_key);
  const contractNo = valueText(row.canonical.contract_no)
    ?? valueText(row.canonical.sales_order_no)
    ?? row.normalized_key;
  // 旧形态退路：receipt_key 是「收款单号|订单号」，收款单号本身含 | 时会截断，故契约字段优先
  const receiptNo = valueText(row.canonical.receipt_no) ?? receiptKey?.split("|")[0] ?? null;
  if (!contractNo || !receiptNo) return null;
  const fileMatch = /本文件\s*(-?[\d.]+)\s*\/\s*(\d{4}-\d{2}-\d{2})/.exec(conflict.message);
  const ledgerMatch = /台账\s*(-?[\d.]+)\s*\/\s*(\d{4}-\d{2}-\d{2})/.exec(conflict.message);
  return {
    contract_no: contractNo,
    receipt_no: receiptNo,
    receipt_date: dateText(row.canonical.receipt_date) ?? fileMatch?.[2] ?? null,
    actual_amount: valueText(row.canonical.actual_amount) ?? fileMatch?.[1] ?? null,
    ledger_amount: valueText(row.before?.actual_amount) ?? ledgerMatch?.[1] ?? null,
    ledger_date: dateText(row.before?.receipt_date) ?? ledgerMatch?.[2] ?? null,
    message: conflict.message,
  };
}

function overwriteText(row: MaintenanceBatchPreviewRow): string | null {
  const overwrite = row.warnings.find((issue) => issue.code === "snapshot_overwrite");
  if (overwrite) return overwrite.message;
  if (!rowNeedsConfirmation(row)) return null;
  const before = row.before?.cumulative_amount;
  const after = row.after?.cumulative_amount;
  const month = String(row.canonical.report_month ?? "").slice(0, 7);
  return `将覆盖 ${month} 已确认累计 ${String(before ?? "—")} → ${String(after ?? "—")}`;
}

function countsFromRows(rows: MaintenanceBatchPreviewRow[]) {
  return rows.reduce(
    (counts, row) => ({ ...counts, [row.match_state]: counts[row.match_state] + 1 }),
    { matched: 0, ambiguous: 0, unmatched: 0, invalid: 0 },
  );
}

function issueText(row: MaintenanceBatchPreviewRow): string {
  // 台账/覆盖类提示单独成列渲染，这里不重复
  const issues = [...row.errors, ...row.warnings].filter((issue) => !(issue.code in RECEIPT_HINTS));
  if (issues.length) return issues.map((issue) => issue.message).join("；");
  if (row.match_state === "ambiguous" && row.candidates?.length) {
    return `候选：${row.candidates.map((item) => item.project_name).join("、")}`;
  }
  if (row.match_state === "unmatched") return "未找到唯一项目/合同";
  return "—";
}

function canonicalText(row: MaintenanceBatchPreviewRow): string {
  const pairs = Object.entries(row.canonical)
    .filter(([, value]) => value !== null && value !== "")
    .slice(0, 4)
    .map(([key, value]) => `${key}: ${String(value)}`);
  return pairs.length ? pairs.join("；") : "—";
}

function MappingPreview({ file }: { file: MaintenanceBatchPreviewFile }) {
  const columns: ColumnsType<MaintenanceBatchPreviewFile["detected_fields"][number]> = [
    { title: "源列", dataIndex: "source_column", width: 170 },
    {
      title: "识别为",
      key: "canonical",
      render: (_, field) => field.canonical_label || field.canonical_field || "未映射",
    },
    {
      title: "置信方式",
      dataIndex: "confidence",
      width: 100,
      render: (value: string) => <Tag>{value}</Tag>,
    },
    {
      title: "约束/口径",
      key: "basis",
      width: 220,
      render: (_, field) => (
        <Space size={4} wrap>
          {field.required ? <Tag color="red">必填</Tag> : null}
          {field.metric_basis ? <Text type="secondary">{field.metric_basis}</Text> : null}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={10} style={{ width: "100%" }}>
      <Descriptions size="small" column={{ xs: 1, sm: 3 }}>
        <Descriptions.Item label="识别类型">{file.import_kind}</Descriptions.Item>
        <Descriptions.Item label="工作表">{file.detected_sheet || "—"}</Descriptions.Item>
        <Descriptions.Item label="表头行">
          {file.header_rows.length ? file.header_rows.join("、") : "—"}
        </Descriptions.Item>
      </Descriptions>
      {file.mapping_conflicts.map((conflict, index) => (
        <Alert
          key={`${conflict.canonical_field ?? "conflict"}-${index}`}
          type="warning"
          showIcon
          message={conflict.message}
          description={conflict.source_columns.join("、")}
        />
      ))}
      <Table
        size="small"
        pagination={false}
        rowKey={(field) => `${field.source_column}-${field.canonical_field ?? "unmapped"}`}
        dataSource={file.detected_fields}
        columns={columns}
        scroll={{ x: 620 }}
      />
    </Space>
  );
}

interface ImportPanelProps {
  options: MaintenanceBatchTransferOptions;
  onApplied: () => void | Promise<unknown>;
}

function ImportPanel({ options, onApplied }: ImportPanelProps) {
  const [files, setFiles] = useState<UploadFile[]>([]);
  const [preview, setPreview] = useState<MaintenanceBatchPreviewResponse | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<MatchFilter>("all");
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([]);
  const [result, setResult] = useState<MaintenanceBatchApplyResponse | null>(null);
  // 台账冲突人工裁决弹窗（D-16 / REQ #56 #57）
  const [rulingRow, setRulingRow] = useState<MaintenanceBatchPreviewRow | null>(null);
  const [rulingReason, setRulingReason] = useState("");
  const [ruling, setRuling] = useState(false);
  const [rulingError, setRulingError] = useState<string | null>(null);
  const requestGeneration = useRef(0);
  const canRule = canRuleReceiptConflict();

  const maxFiles = options.max_files || DEFAULT_MAX_FILES;
  const accepted = options.accepted_extensions.length
    ? options.accepted_extensions.join(",")
    : ".xlsx";

  useEffect(() => () => {
    requestGeneration.current += 1;
  }, []);

  /** 清掉预览、勾选与回执（文件保留），用户须重新预览。 */
  const resetPreviewState = () => {
    setPreview(null);
    setResult(null);
    setSelectedRowKeys([]);
    setFilter("all");
  };

  const invalidatePreview = () => {
    requestGeneration.current += 1;
    resetPreviewState();
    setError(null);
  };

  const uploadProps: UploadProps = {
    accept: accepted,
    multiple: true,
    fileList: files,
    disabled: previewing || applying,
    beforeUpload(file) {
      const isXlsx = /\.xlsx$/i.test(file.name);
      if (!isXlsx) {
        message.error(`${file.name} 不是 .xlsx 文件`);
        return Upload.LIST_IGNORE;
      }
      return false;
    },
    onChange(info) {
      invalidatePreview();
      setFiles(info.fileList.slice(0, maxFiles));
      if (info.fileList.length > maxFiles) {
        message.warning(`一次最多选择 ${maxFiles} 个文件`);
      }
    },
    onRemove() {
      invalidatePreview();
      return true;
    },
  };

  const runPreview = async () => {
    if (!files.length || previewing || applying) return;
    const sourceFiles = files.map(rawFile).filter((file): file is File => Boolean(file));
    if (sourceFiles.length !== files.length) {
      setError("部分文件无法读取，请移除后重新选择");
      return;
    }
    const generation = ++requestGeneration.current;
    setPreviewing(true);
    setError(null);
    setResult(null);
    try {
      const { data } = await previewMaintenanceBatchTransfer(sourceFiles);
      if (generation !== requestGeneration.current) return;
      setPreview(data);
      // 覆盖既有累计的行默认不勾选，必须由用户逐行确认（D-16）
      setSelectedRowKeys(defaultSelectedKeys(data.rows));
      setFilter("all");
    } catch (reason) {
      if (generation !== requestGeneration.current) return;
      setError(await errorMessage(reason, "批量预览失败，请检查文件后重试"));
    } finally {
      if (generation === requestGeneration.current) setPreviewing(false);
    }
  };

  const counts = useMemo(
    () => countsFromRows(preview?.rows ?? []),
    [preview],
  );
  const visibleRows = useMemo(
    () => preview?.rows.filter((row) => filter === "all" || row.match_state === filter) ?? [],
    [filter, preview],
  );
  const selectableKeys = useMemo(
    () => new Set(preview?.rows.filter(rowCanApply).map((row) => row.row_key) ?? []),
    [preview],
  );
  const confirmationKeys = useMemo(
    () => new Set(
      preview?.rows
        .filter((row) => rowCanApply(row) && rowNeedsConfirmation(row))
        .map((row) => row.row_key) ?? [],
    ),
    [preview],
  );
  const edges = useMemo(() => dependencyEdges(preview?.rows ?? []), [preview]);
  const rowsByKey = useMemo(
    () => new Map((preview?.rows ?? []).map((row) => [row.row_key, row] as const)),
    [preview],
  );
  const dependentCount = useMemo(
    () => (preview?.rows ?? []).filter((row) => rowCanApply(row) && rowDependencies(row).length > 0).length,
    [preview],
  );
  const safeSelectedKeys = selectedRowKeys.filter((key) => selectableKeys.has(String(key)));
  const selectedOverwrites = safeSelectedKeys.filter((key) => confirmationKeys.has(String(key)));
  // 已勾选行的依赖是否都在勾选集合里；不一致时后端会整批拒绝，前端直接禁用提交并说明原因
  const selectedSet = new Set(safeSelectedKeys.map(String));
  const missingDependencies = (preview?.rows ?? [])
    .filter((row) => selectedSet.has(row.row_key))
    .flatMap((row) => {
      const missing = rowDependencies(row).filter((dep) => !selectedSet.has(dep));
      return missing.length
        ? [`${rowLabel(row)} 依赖 ${missing.map((dep) => rowLabel(rowsByKey.get(dep), dep)).join("、")}`]
        : [];
    });
  const selectionInconsistent = missingDependencies.length > 0;

  /** 勾选依赖行 → 自动带上其（传递）依赖；取消被依赖行 → 一并取消依赖它的行（D-16）。 */
  const changeSelection = (nextKeys: Key[]) => {
    const current = new Set(safeSelectedKeys.map(String));
    const next = new Set(nextKeys.map(String).filter((key) => selectableKeys.has(key)));
    const added = [...next].filter((key) => !current.has(key));
    const removed = [...current].filter((key) => !next.has(key));
    const wanted = [...closure(added, edges.dependencies)]
      .filter((key) => !next.has(key) && selectableKeys.has(key));
    // D-16 第 3 点：覆盖既有已确认累计的行必须由用户逐行手动勾选确认，不随依赖自动带上；
    // 缺了它勾选就不一致，提交按钮禁用并写明原因，直到用户自己勾上。
    const autoAdded = wanted.filter((key) => !confirmationKeys.has(key));
    const explicitNeeded = wanted.filter((key) => confirmationKeys.has(key));
    autoAdded.forEach((key) => next.add(key));
    const autoRemoved = [...closure(removed, edges.dependents)].filter((key) => next.has(key));
    autoRemoved.forEach((key) => next.delete(key));
    const describe = (keys: string[]) => keys.map((key) => rowLabel(rowsByKey.get(key), key)).join("、");
    if (autoAdded.length) {
      message.info(`已同时勾选其依赖的 ${autoAdded.length} 行：${describe(autoAdded)}`);
    }
    if (explicitNeeded.length) {
      message.warning(`覆盖既有已确认累计的行须手动逐行勾选确认：${describe(explicitNeeded)}`);
    }
    if (autoRemoved.length) {
      message.info(`已同时取消依赖它的 ${autoRemoved.length} 行：${describe(autoRemoved)}`);
    }
    setSelectedRowKeys((preview?.rows ?? []).filter((row) => next.has(row.row_key)).map((row) => row.row_key));
  };

  const rulingTarget = rulingRow ? rulingSubject(rulingRow) : null;
  const rulingReady = Boolean(rulingTarget?.receipt_date && rulingTarget?.actual_amount);
  const rulingReasonText = rulingReason.trim();
  const rulingReasonValid = rulingReasonText.length > 0 && rulingReasonText.length <= 1000;

  const openRuling = (row: MaintenanceBatchPreviewRow) => {
    setRulingRow(row);
    setRulingReason("");
    setRulingError(null);
  };

  const closeRuling = () => {
    if (ruling) return;
    setRulingRow(null);
    setRulingReason("");
    setRulingError(null);
  };

  const submitRuling = async () => {
    const subject = rulingTarget;
    if (!subject || !subject.receipt_date || !subject.actual_amount || !rulingReasonValid || ruling) return;
    setRuling(true);
    setRulingError(null);
    try {
      const outcome = await ruleMaintenanceReceiptConflict({
        contract_no: subject.contract_no,
        receipt_no: subject.receipt_no,
        receipt_date: subject.receipt_date,
        actual_amount: subject.actual_amount,
        reason: rulingReasonText,
      });
      message.success(
        `已裁决收款单 ${subject.receipt_no}：台账原行作废、以本文件值重建，涉及 ${outcome.affected_months.length} 个月份累计；正在重新预览`,
      );
      setRulingRow(null);
      setRulingReason("");
      // 裁决只改台账不改快照：必须重新预览，受影响月份才会以覆盖行呈现
      await runPreview();
    } catch (reason) {
      const status = errorStatus(reason);
      const detail = await errorMessage(reason, "");
      setRulingError(
        detail
        || (status === 403
          ? "经营事实写入必须使用实名系统账号（admin / boss）"
          : status === null && reason instanceof Error && reason.message
            ? reason.message
            : "裁决失败，请稍后重试"),
      );
    } finally {
      setRuling(false);
    }
  };

  const apply = async () => {
    if (!preview || !safeSelectedKeys.length || applying) return;
    const generation = requestGeneration.current;
    setApplying(true);
    setError(null);
    try {
      const { data } = await applyMaintenanceBatchTransfer({
        preview_token: preview.preview_token,
        payload_hash: preview.payload_hash,
        data_version: preview.data_version,
        row_keys: safeSelectedKeys.map(String),
      });
      // 用户可能在网络请求期间关闭弹窗；后端一旦成功，主页仍必须刷新，不能因
      // ImportPanel 已卸载而留下旧卡片。弹窗内状态只在本次会话仍有效时更新。
      if (generation === requestGeneration.current) {
        setResult(data);
        setSelectedRowKeys([]);
      }
      await onApplied();
      message.success(`批量提交完成：成功 ${data.applied} 行`);
    } catch (reason) {
      if (generation !== requestGeneration.current) return;
      if (isTerminalApplyFailure(reason)) {
        // 后端已把批次记为 failed（stale_preview / apply_conflict / 422 业务拒绝 / 5xx）：预览凭证作废，
        // 清掉预览与勾选（保留文件）让用户重新预览，而不是留着一个再点也只会 409 的死预览
        resetPreviewState();
        setError(
          `${await errorMessage(reason, terminalApplyFallback(reason))}——预览已清除，请重新点击「自动识别并预览」`,
        );
        return;
      }
      // 403 不是终态（实名门禁 / 项目范围）：批次仍在，预览与勾选保留，按后端原话提示
      // （如「经营事实写入必须使用实名系统账号」），用户可去掉覆盖行重提或换实名账号；与裁决弹窗同一处理。
      setError(
        await errorMessage(
          reason,
          errorStatus(reason) === 403 ? "当前账号没有批量导入权限" : "批量提交失败，请稍后重试",
        ),
      );
    } finally {
      if (generation === requestGeneration.current) setApplying(false);
    }
  };

  const rowColumns: ColumnsType<MaintenanceBatchPreviewRow> = [
    {
      title: "来源",
      key: "source",
      width: 190,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Text>{row.filename}</Text>
          <Text type="secondary">
            {row.detected_sheet ? `${row.detected_sheet} · ` : ""}第 {row.source_row} 行
          </Text>
        </Space>
      ),
    },
    {
      title: "匹配",
      key: "match",
      width: 190,
      render: (_, row) => (
        <Space direction="vertical" size={2}>
          <Tag color={MATCH_COLORS[row.match_state]}>{MATCH_LABELS[row.match_state]}</Tag>
          <Text>{row.matched_project_name || "—"}</Text>
        </Space>
      ),
    },
    {
      title: "动作",
      dataIndex: "action",
      width: 130,
      render: (value: string) => ACTION_LABELS[value] || value,
    },
    {
      title: "识别内容",
      key: "canonical",
      ellipsis: true,
      render: (_, row) => <Text title={canonicalText(row)}>{canonicalText(row)}</Text>,
    },
    {
      title: "台账/覆盖提示",
      key: "receipt_hints",
      width: 300,
      render: (_, row) => {
        const hints = receiptHints(row);
        const overwrite = overwriteText(row);
        const texts = hintTexts(row);
        const subject = canRule ? rulingSubject(row) : null;
        if (!hints.length && !overwrite && !texts.length) return "—";
        return (
          <Space direction="vertical" size={2}>
            <Space size={4} wrap>
              {hints.map((hint, index) => (
                <Tag key={`${hint.code}-${index}`} color={hint.color} title={hint.message}>{hint.label}</Tag>
              ))}
            </Space>
            {overwrite ? <Text type="warning" title={overwrite}>{overwrite}</Text> : null}
            {/* 依赖月份 / 建账 / 核验 / 台账冲突类说明必须可见，不能只靠悬停 */}
            {texts.map((hint, index) => (
              <Text key={`${index}-${hint}`} type="secondary" style={{ display: "block" }}>{hint}</Text>
            ))}
            {subject ? (
              <Button size="small" danger onClick={() => openRuling(row)}>
                以本文件为准（人工裁决）
              </Button>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: "问题/候选",
      key: "issues",
      width: 260,
      ellipsis: true,
      render: (_, row) => <Text title={issueText(row)}>{issueText(row)}</Text>,
    },
  ];

  const resultColumns: ColumnsType<MaintenanceBatchApplyResponse["rows"][number]> = [
    { title: "文件", dataIndex: "source_file", render: (value) => value || "—" },
    { title: "源行", dataIndex: "source_row", width: 80, render: (value) => value ?? "—" },
    {
      title: "结果",
      dataIndex: "status",
      width: 150,
      render: (value: string, row) => {
        const code = row.error_code && RECEIPT_HINTS[row.error_code];
        return (
          <Space size={4} wrap>
            <Tag color={value === "applied" ? "green" : value === "skipped" ? "default" : "red"}>
              {value}
            </Tag>
            {code ? <Tag color={code.color}>{code.label}</Tag> : null}
          </Space>
        );
      },
    },
    { title: "动作", dataIndex: "action", width: 130, render: (value) => ACTION_LABELS[value] || value || "—" },
    { title: "说明", dataIndex: "message", render: (value) => value || "—" },
  ];

  if (!options.can_import) {
    return <Alert type="info" showIcon message="当前账号没有批量导入权限" />;
  }

  return (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message="先预览，再提交"
        description="系统自动识别表单、字段和项目归属。字段映射只读展示；正式提交只消费冻结的预览凭证。歧义、未匹配或无效行不能勾选；收款单已在台账的行自动跳过，覆盖既有已确认累计的行默认不勾选，需逐行确认。"
      />

      <Upload.Dragger {...uploadProps}>
        <p className="ant-upload-drag-icon"><InboxOutlined /></p>
        <p className="ant-upload-text">拖入一个或多个 .xlsx，或点击选择文件</p>
        <p className="ant-upload-hint">最多 {maxFiles} 个；可混合销售合同与回款表，实际类型由服务端识别</p>
      </Upload.Dragger>

      <Space wrap>
        <Button
          aria-label="自动识别并预览"
          type="primary"
          icon={<CloudUploadOutlined />}
          loading={previewing}
          disabled={!files.length || applying}
          onClick={() => void runPreview()}
        >
          自动识别并预览
        </Button>
        {preview ? (
          <Text type="secondary">
            预览有效期至 {new Date(preview.expires_at).toLocaleString("zh-CN")}
          </Text>
        ) : null}
      </Space>

      {error ? <Alert type="error" showIcon message={error} /> : null}

      {preview ? (
        <>
          <Collapse
            size="small"
            items={preview.files.map((file) => ({
              key: file.file_id,
              label: `${file.filename} · 字段映射`,
              children: <MappingPreview file={file} />,
            }))}
          />

          <Card size="small" title="行匹配预览">
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
              <Segmented<MatchFilter>
                value={filter}
                onChange={setFilter}
                options={[
                  { value: "all", label: `全部 ${preview.rows.length}` },
                  { value: "matched", label: `已匹配 ${counts.matched}` },
                  { value: "ambiguous", label: `有歧义 ${counts.ambiguous}` },
                  { value: "unmatched", label: `未匹配 ${counts.unmatched}` },
                  { value: "invalid", label: `无效 ${counts.invalid}` },
                ]}
              />
              <Table
                size="small"
                rowKey="row_key"
                dataSource={visibleRows}
                columns={rowColumns}
                pagination={{ pageSize: 10, showSizeChanger: false }}
                scroll={{ x: 980 }}
                rowSelection={{
                  selectedRowKeys: safeSelectedKeys,
                  preserveSelectedRowKeys: true,
                  onChange: changeSelection,
                  getCheckboxProps: (row) => ({
                    disabled: !rowCanApply(row),
                    "aria-label": !rowCanApply(row)
                      ? `${MATCH_LABELS[row.match_state]}行不可提交`
                      : rowNeedsConfirmation(row)
                        ? `确认覆盖 ${row.filename} 第 ${row.source_row} 行`
                        : `选择 ${row.filename} 第 ${row.source_row} 行`,
                  }),
                }}
              />
              <Row justify="space-between" align="middle" gutter={[12, 8]}>
                <Col>
                  <Text type="secondary">
                    可提交 {selectableKeys.size} 行，已选 {safeSelectedKeys.length} 行；其余行需修正源文件或后端归属后重新预览。
                  </Text>
                  {confirmationKeys.size ? (
                    <Text type="warning" style={{ display: "block" }}>
                      其中 {confirmationKeys.size} 行会覆盖既有已确认累计，默认未勾选，已确认覆盖 {selectedOverwrites.length} 行。
                    </Text>
                  ) : null}
                  {dependentCount ? (
                    <Text type="secondary" style={{ display: "block" }}>
                      其中 {dependentCount} 行依赖更早月份 / 覆盖行：依赖未勾时默认不勾，勾选时自动带上依赖行，取消被依赖行时一并取消。
                    </Text>
                  ) : null}
                  {selectionInconsistent ? (
                    <Text type="danger" style={{ display: "block" }}>
                      勾选不一致，无法提交：{missingDependencies.join("；")}
                    </Text>
                  ) : null}
                </Col>
                <Col>
                  <Button
                    type="primary"
                    loading={applying}
                    disabled={!preview.can_apply || !safeSelectedKeys.length || previewing || selectionInconsistent}
                    title={selectionInconsistent ? `勾选不一致：${missingDependencies.join("；")}` : undefined}
                    onClick={() => void apply()}
                  >
                    提交 {safeSelectedKeys.length} 行
                  </Button>
                </Col>
              </Row>
            </Space>
          </Card>

          <Modal
            open={rulingRow !== null}
            title="人工裁决：以本文件为准"
            okText="确认裁决"
            cancelText="取消"
            confirmLoading={ruling}
            okButtonProps={{ danger: true, disabled: !rulingReady || !rulingReasonValid }}
            onOk={() => void submitRuling()}
            onCancel={closeRuling}
            destroyOnHidden
          >
            {rulingTarget ? (
              <Space direction="vertical" size={10} style={{ width: "100%" }}>
                <Alert
                  type="warning"
                  showIcon
                  message="台账原行将作废并以本文件值重建；快照不自动改写"
                  description="裁决只改台账，不求和、不猜重。受影响月份的累计会在重新预览时以「覆盖已确认累计」行呈现，仍需逐行确认。"
                />
                <Descriptions size="small" column={1} bordered>
                  <Descriptions.Item label="合同 / 销售订单">{rulingTarget.contract_no}</Descriptions.Item>
                  <Descriptions.Item label="收款单号">{rulingTarget.receipt_no}</Descriptions.Item>
                  <Descriptions.Item label="台账值（金额 / 日期）">
                    {`${rulingTarget.ledger_amount ?? "—"} / ${rulingTarget.ledger_date ?? "—"}`}
                  </Descriptions.Item>
                  <Descriptions.Item label="本文件值（裁决后生效）">
                    {`${rulingTarget.actual_amount ?? "—"} / ${rulingTarget.receipt_date ?? "—"}`}
                  </Descriptions.Item>
                </Descriptions>
                {!rulingReady ? (
                  <Alert type="error" showIcon message="本行缺少文件值（金额 / 日期），无法裁决；请重新预览后再试" />
                ) : null}
                <Input.TextArea
                  aria-label="裁决原因"
                  rows={3}
                  maxLength={1000}
                  showCount
                  value={rulingReason}
                  onChange={(event) => setRulingReason(event.target.value)}
                  placeholder="必填，1~1000 字：说明为何以本文件为准（如已核对银行回单）"
                />
                {rulingError ? <Alert type="error" showIcon message={rulingError} /> : null}
              </Space>
            ) : null}
          </Modal>
        </>
      ) : null}

      {result ? (
        <Card size="small" title="逐行提交结果">
          <Space direction="vertical" size={10} style={{ width: "100%" }}>
            <Descriptions size="small" column={{ xs: 1, sm: 4 }}>
              <Descriptions.Item label="成功">{result.applied}</Descriptions.Item>
              <Descriptions.Item label="跳过">{result.skipped}</Descriptions.Item>
              <Descriptions.Item label="阻断">{result.blocked}</Descriptions.Item>
              <Descriptions.Item label="审计号">{result.audit_ref}</Descriptions.Item>
            </Descriptions>
            <Table
              size="small"
              rowKey="row_key"
              pagination={false}
              dataSource={result.rows}
              columns={resultColumns}
              scroll={{ x: 720 }}
            />
          </Space>
        </Card>
      ) : null}
    </Space>
  );
}

interface DownloadPanelProps {
  options: MaintenanceBatchTransferOptions;
  filters: Omit<MaintenanceBatchDownloadInput, "forms" | "fields">;
}

function fieldAvailable(field: MaintenanceBatchDownloadField, forms: string[]): boolean {
  return !field.form_keys.length || field.form_keys.some((key) => forms.includes(key));
}

function DownloadPanel({ options, filters }: DownloadPanelProps) {
  const availableFormKeys = options.download_forms.map((form) => form.key);
  const initialForms = options.default_forms.filter((key) => availableFormKeys.includes(key));
  const [forms, setForms] = useState<string[]>(
    initialForms.length
      ? initialForms
      : options.download_forms.filter((form) => form.default_selected).map((form) => form.key),
  );
  const initialAvailableFields = options.download_fields.filter((field) => fieldAvailable(field, forms));
  const initialDefaultFields = options.default_fields.filter((key) =>
    initialAvailableFields.some((field) => field.key === key),
  );
  const [fields, setFields] = useState<string[]>(
    initialDefaultFields.length
      ? initialDefaultFields
      : initialAvailableFields.filter((field) => field.default_selected).map((field) => field.key),
  );
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const availableFields = useMemo(
    () => options.download_fields.filter((field) => fieldAvailable(field, forms)),
    [forms, options.download_fields],
  );
  const groupedFields = useMemo(() => {
    const groups = new Map<string, MaintenanceBatchDownloadField[]>();
    availableFields.forEach((field) => {
      groups.set(field.group || "其他", [...(groups.get(field.group || "其他") ?? []), field]);
    });
    return [...groups.entries()];
  }, [availableFields]);

  const changeForms = (next: string[]) => {
    setForms(next);
    const eligible = new Set(
      options.download_fields.filter((field) => fieldAvailable(field, next)).map((field) => field.key),
    );
    setFields((current) => current.filter((key) => eligible.has(key)));
  };

  const restoreDefaults = () => {
    const nextForms = options.default_forms.filter((key) => availableFormKeys.includes(key));
    const normalizedForms = nextForms.length
      ? nextForms
      : options.download_forms.filter((form) => form.default_selected).map((form) => form.key);
    const eligible = new Set(
      options.download_fields
        .filter((field) => fieldAvailable(field, normalizedForms))
        .map((field) => field.key),
    );
    const defaults = options.default_fields.filter((key) => eligible.has(key));
    setForms(normalizedForms);
    setFields(
      defaults.length
        ? defaults
        : options.download_fields
          .filter((field) => eligible.has(field.key) && field.default_selected)
          .map((field) => field.key),
    );
  };

  const download = async () => {
    if (!forms.length || !fields.length || downloading) return;
    setDownloading(true);
    setError(null);
    try {
      const result = await downloadMaintenanceBatchTransfer({
        ...filters,
        forms,
        fields,
      });
      saveBlob(result.blob, result.filename);
      message.success("批量文件已开始下载");
    } catch (reason) {
      setError(await errorMessage(reason, "批量下载失败，请稍后重试"));
    } finally {
      setDownloading(false);
    }
  };

  if (!options.can_download) {
    return <Alert type="info" showIcon message="当前账号没有批量下载权限" />;
  }

  return (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message="导出范围服从维保主页当前筛选"
        description="会覆盖当前筛选命中的全部项目，不限于已经滚动加载的卡片。表单与字段均来自服务端权限白名单。"
      />

      <div>
        <Text strong>选择表单</Text>
        <Checkbox.Group value={forms} onChange={(values) => changeForms(values.map(String))} style={{ width: "100%" }}>
          <Row gutter={[12, 10]} style={{ marginTop: 8 }}>
            {options.download_forms.map((form) => (
              <Col xs={24} sm={12} key={form.key}>
                <Checkbox value={form.key}>
                  {form.label}
                  {form.description ? <Text type="secondary"> · {form.description}</Text> : null}
                </Checkbox>
              </Col>
            ))}
          </Row>
        </Checkbox.Group>
      </div>

      <Divider style={{ margin: 0 }} />

      <Space wrap>
        <Button size="small" onClick={() => setFields(availableFields.map((field) => field.key))} disabled={!availableFields.length}>
          字段全选
        </Button>
        <Button size="small" onClick={() => setFields([])} disabled={!fields.length}>取消字段</Button>
        <Button size="small" onClick={restoreDefaults}>恢复默认</Button>
        <Text type="secondary">已选 {forms.length} 个表单、{fields.length} 个字段</Text>
      </Space>

      {groupedFields.map(([group, items]) => (
        <div key={group}>
          <Text strong>{group}</Text>
          <Row gutter={[12, 10]} style={{ marginTop: 8 }}>
            {items.map((field) => (
              <Col xs={24} sm={12} key={field.key}>
                <Checkbox
                  checked={fields.includes(field.key)}
                  onChange={(event) => setFields((current) => event.target.checked
                    ? [...current, field.key]
                    : current.filter((key) => key !== field.key))}
                >
                  {field.label}
                </Checkbox>
              </Col>
            ))}
          </Row>
        </div>
      ))}

      {error ? <Alert type="error" showIcon message={error} /> : null}

      <Button
        aria-label="下载当前筛选全部项目"
        type="primary"
        icon={<CloudDownloadOutlined />}
        loading={downloading}
        disabled={!forms.length || !fields.length}
        onClick={() => void download()}
      >
        下载当前筛选全部项目
      </Button>
    </Space>
  );
}

export default function MaintenanceBatchTransferButton({
  filters,
  onApplied,
}: MaintenanceBatchTransferButtonProps) {
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<MaintenanceBatchTransferOptions | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const openGeneration = useRef(0);

  const loadOptions = async () => {
    const generation = ++openGeneration.current;
    setLoading(true);
    setOptions(null);
    setError(null);
    try {
      const { data } = await getMaintenanceBatchTransferOptions();
      if (generation !== openGeneration.current) return;
      setOptions(data);
    } catch (reason) {
      if (generation !== openGeneration.current) return;
      setError(await errorMessage(reason, "无法读取批量导入/下载配置，请稍后重试"));
    } finally {
      if (generation === openGeneration.current) setLoading(false);
    }
  };

  useEffect(() => {
    if (open) void loadOptions();
    else openGeneration.current += 1;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <>
      <Button aria-label="批量导入 / 下载" icon={<SwapOutlined />} onClick={() => setOpen(true)}>
        批量导入 / 下载
      </Button>
      <Modal
        open={open}
        title="维保项目批量导入 / 下载"
        width={1180}
        footer={null}
        onCancel={() => setOpen(false)}
        destroyOnHidden
        styles={{ body: { maxHeight: "78vh", overflowY: "auto" } }}
      >
        {loading ? (
          <div style={{ padding: 48, textAlign: "center" }}><Spin /></div>
        ) : error ? (
          <Alert
            type="error"
            showIcon
            message={error}
            action={<Button size="small" onClick={() => void loadOptions()}>重试</Button>}
          />
        ) : options ? (
          <Tabs
            items={[
              {
                key: "import",
                label: "批量导入",
                children: <ImportPanel options={options} onApplied={onApplied} />,
              },
              {
                key: "download",
                label: "批量下载",
                children: <DownloadPanel options={options} filters={filters} />,
              },
            ]}
          />
        ) : (
          <Paragraph type="secondary">暂无可用配置</Paragraph>
        )}
      </Modal>
    </>
  );
}
