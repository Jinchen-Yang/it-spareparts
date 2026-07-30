import { useEffect, useRef, useState } from "react";
import {
  Card, DatePicker, Input, Button, Space, Tag, Tooltip, message, Statistic, Row, Col,
  Drawer, Progress, Alert, Empty, Segmented, Upload, Pagination,
} from "antd";
import { InfoCircleOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../components/ResizableTable";
import PageHeader from "../components/PageHeader";
import dayjs from "dayjs";
import type { Dayjs } from "dayjs";
import api from "../api";
import { TaxMoney, useTaxBasis } from "../context/TaxBasis";
import type { TaxBasis } from "../context/TaxBasis";
import { money, pct } from "../utils/format";

type PartsProfitStatus =
  | "complete_actual"
  | "complete_estimated"
  | "missing_revenue"
  | "missing_tax_rate"
  | "invalid_tax_rate"
  | "ambiguous_revenue"
  | "incomplete_cost"
  | "filtered_scope";

type ContributionStatus =
  | "complete"
  | "expense_data_unavailable"
  | "expense_tax_unknown"
  | PartsProfitStatus;

interface DualMarginFields {
  revenue_inc?: number | null;
  revenue_ex?: number | null;
  parts_cost_inc_tax?: number | null;
  parts_cost_ex_tax?: number | null;
  parts_gross_profit_inc?: number | null;
  parts_gross_profit_ex?: number | null;
  parts_gross_margin_inc?: number | null;
  parts_gross_margin_ex?: number | null;
  parts_profit_status_inc?: PartsProfitStatus | string | null;
  parts_profit_status_ex?: PartsProfitStatus | string | null;
  contribution_profit_inc?: number | null;
  contribution_profit_ex?: number | null;
  contribution_margin_inc?: number | null;
  contribution_margin_ex?: number | null;
  contribution_status_inc?: ContributionStatus | string | null;
  contribution_status_ex?: ContributionStatus | string | null;
  expense_inc?: number | null;
  expense_ex?: number | null;
}

interface ProjectRow {
  project: string;
  lines: number;
  order_count: number;
  missing_detail_orders: number;
  structure_complete: boolean;
  qty: number | null;
  cost_inc: number | null;
  cost_ex: number | null;
  cost_total: number | null;
  actual_cost_inc: number | null;
  actual_cost_ex: number | null;
  estimated_cost_inc: number | null;
  estimated_cost_ex: number | null;
  actual_lines: number | null;
  estimated_lines: number | null;
  missing_cost_lines: number | null;
  known_cost_total: number | null;
  cost_quality?: string | null;
  coverage_pct: number | null;
  by_source: Record<string, number> | null;
  months: number;
  sales_orders: string[];
  contract_amount: number | null;
  contract_shared: boolean;
  contract_incomplete: boolean;
  maint_end: string | null;
  lifecycle_status: LifecycleStatus;
}

interface LineRow {
  id: number;
  order_no: string; order_date: string | null; demand_type: string | null;
  business_type: string | null; warehouse: string | null;
  pn_std: string | null; description: string | null;
  qty: number | null; return_qty: number | null;
  unit_cost: number | null; cost_amount: number | null;
  unit_cost_inc_tax?: number | null; unit_cost_ex_tax?: number | null;
  cost_amount_inc_tax?: number | null; cost_amount_ex_tax?: number | null;
  cost_tier: "actual" | "estimated" | "missing" | null;
  cost_source: string | null; cost_tax_basis: string | null;
  price_month: string | null; trace_months: number | null;
  linked_purchase_order_no: string | null; anomaly_flags: string[];
  price_distance_days: number | null; confidence: string | null;
}

type CostQuality = "actual_only" | "contains_estimate" | "incomplete";
type BoardStatus =
  | "incomplete_cost"
  | "expense_data_unavailable"
  | "red"
  | "yellow"
  | "green"
  | "no_budget";
type LifecycleStatus = "ongoing" | "ended" | "missing";
type LifecycleFilter = LifecycleStatus | "all";
type LifecycleCounts = Record<LifecycleStatus, number>;
type ReminderStatusFilter = BoardStatus | "all";
type ExportDatePreset = "all" | "today" | "last7" | "last14" | "last21" | "last30" | "month" | "custom";
const DOWNLOAD_PROJECT_QUERY_MAX_LENGTH = 128;
const DOWNLOAD_PROJECT_NAME_MAX_LENGTH = 256;
const DOWNLOAD_CONTRACT_MAX_LENGTH = 64;

interface BoardRow extends DualMarginFields {
  contract: string | null;
  decision_status?: string | null;
  status?: string | null;
  projects: { project: string; lines: number; spent_parts: number | null }[];
  lines: number; coverage_pct: number | null;
  actual_cost_inc: number | null; actual_cost_ex: number | null;
  estimated_cost_inc: number | null; estimated_cost_ex: number | null;
  actual_lines: number | null; estimated_lines: number | null;
  missing_cost_lines: number | null; known_cost_total: number | null;
  cost_quality?: string | null;
  spent_parts: number | null; spent_expense: number | null; spent: number | null;
  expense_data_available?: boolean | null;
  budget: number | null; remaining: number | null; remaining_pct: number | null;
  low_conf_pct: number | null;
  maint_start: string | null; maint_end: string | null;
  lifecycle_status: LifecycleStatus;
  first_out: string | null; last_out: string | null;
}

const STATUS_META: Record<BoardStatus, { label: string; color: string; bg: string }> = {
  incomplete_cost: { label: "成本不完整，需补数据", color: "#8c6d31", bg: "rgba(140,109,49,0.08)" },
  expense_data_unavailable: { label: "费用数据未就绪", color: "#8c6d31", bg: "rgba(140,109,49,0.08)" },
  red: { label: "预算已用完或超预算", color: "#c0524a", bg: "rgba(192,82,74,0.08)" },
  yellow: { label: "预算余量 ≤ 20%", color: "#b8860b", bg: "rgba(212,160,23,0.10)" },
  green: { label: "预算余量 > 20%", color: "#3f7a45", bg: "rgba(63,122,69,0.07)" },
  no_budget: { label: "无预算(未关联合同额)", color: "#8c8c8c", bg: "rgba(0,0,0,0.03)" },
};
const LIFECYCLE_META: Record<LifecycleStatus, { label: string; color: string }> = {
  ongoing: { label: "进行中", color: "blue" },
  ended: { label: "已结束", color: "default" },
  missing: { label: "期限缺失", color: "orange" },
};
const EMPTY_LIFECYCLE_COUNTS: LifecycleCounts = { ongoing: 0, ended: 0, missing: 0 };
const CONF_META: Record<string, { label: string; color: string }> = {
  high: { label: "高", color: "green" }, medium: { label: "中", color: "blue" },
  low: { label: "低", color: "orange" },
};
const COST_TIER_META: Record<string, { label: string; color: string }> = {
  actual: { label: "实际采购参考", color: "green" },
  estimated: { label: "估算参考", color: "gold" },
  missing: { label: "成本缺失", color: "orange" },
};

// 既有来源 + 缺失成本历史参考；trace_avg 及历史层必须带真实追溯月数。
const SOURCE_META: Record<string, { label: string; color: string }> = {
  direct: { label: "实际·专属采购", color: "green" },
  window: { label: "实际·±7天最近价", color: "cyan" },
  month_avg: { label: "实际·当月均价", color: "blue" },
  trace_avg: { label: "预估·追溯均价", color: "orange" },
  sales_ref: { label: "预估·销售参考", color: "purple" },
  pool_purchase: { label: "预估·互通池采购均价", color: "gold" },
  pool_sales: { label: "预估·互通池销售均价", color: "magenta" },
  purchase_history: { label: "预估·本PN历史采购", color: "volcano" },
  sales_history: { label: "预估·本PN历史销售", color: "purple" },
  none: { label: "成本缺失", color: "red" },
};
const SOURCE_ORDER = [
  "direct", "window", "month_avg", "trace_avg", "sales_ref",
  "pool_purchase", "pool_sales", "purchase_history", "sales_history", "none",
];
const COVERAGE_WARN_PCT = 80;   // 覆盖率预警线（经验值，非验收线；<此值标红提示核对无成本行）
const BOARD_PAGE_SIZE = 12;

const PROFIT_BASIS_LABEL: Record<TaxBasis, string> = {
  inc: "含税",
  ex: "未税",
  both: "含税与未税",
};

type ProfitStatusMeta = {
  label: string;
  color: string;
  detail: string;
};

const PARTS_PROFIT_STATUS_META: Record<string, ProfitStatusMeta> = {
  complete_actual: {
    label: "完整 · 实际",
    color: "green",
    detail: "收入和成本证据完整，成本仅含实际参考。",
  },
  complete_estimated: {
    label: "含估算",
    color: "gold",
    detail: "收入和成本证据完整，但成本中含低置信估算。",
  },
  missing_revenue: {
    label: "收入缺失",
    color: "orange",
    detail: "合同收入缺失，毛利保持为空。",
  },
  missing_tax_rate: {
    label: "税率缺失",
    color: "orange",
    detail: "合同税率缺失，对应含税毛利保持为空。",
  },
  invalid_tax_rate: {
    label: "税率异常",
    color: "red",
    detail: "合同税率异常，对应含税毛利保持为空。",
  },
  ambiguous_revenue: {
    label: "合同收入冲突",
    color: "red",
    detail: "同一 XSDD 存在多个冲突金额，收入取值未确认，毛利保持为空。",
  },
  incomplete_cost: {
    label: "成本不完整",
    color: "orange",
    detail: "仍有成本缺失，毛利保持为空。",
  },
  filtered_scope: {
    label: "日期筛选下暂不计算",
    color: "default",
    detail: "当前是期间成本，不能与完整合同收入直接比较。",
  },
};

const CONTRIBUTION_STATUS_META: Record<string, ProfitStatusMeta> = {
  complete: {
    label: "完整",
    color: "green",
    detail: "备件毛利与费用证据均完整，可展示合同级贡献毛利。",
  },
  expense_tax_unknown: {
    label: "费用税务口径缺失",
    color: "orange",
    detail: "报销费用缺少税务口径，合同级贡献毛利保持为空。",
  },
  expense_data_unavailable: {
    label: "费用数据未就绪",
    color: "orange",
    detail: "尚无可证明完整的报销数据集，合同级贡献毛利保持为空。",
  },
  parts_profit_unavailable: {
    label: "备件毛利未就绪",
    color: "orange",
    detail: "备件毛利证据尚未完整，合同级贡献毛利保持为空。",
  },
};

function selectedProfitBases(
  basis: TaxBasis,
): Array<"inc" | "ex"> {
  return basis === "both" ? ["inc", "ex"] : [basis];
}

function marginValue(
  row: DualMarginFields,
  basis: "inc" | "ex",
  field: "revenue" | "parts_cost" | "parts_profit" | "parts_margin"
    | "parts_status" | "contribution_profit" | "contribution_margin"
    | "contribution_status",
) {
  const suffix = basis === "inc" ? "inc" : "ex";
  if (field === "parts_cost") {
    return row[`parts_cost_${suffix}_tax` as keyof DualMarginFields];
  }
  if (field === "parts_profit") {
    return row[`parts_gross_profit_${suffix}` as keyof DualMarginFields];
  }
  if (field === "parts_margin") {
    return row[`parts_gross_margin_${suffix}` as keyof DualMarginFields];
  }
  if (field === "parts_status") {
    return row[`parts_profit_status_${suffix}` as keyof DualMarginFields];
  }
  return row[`${field}_${suffix}` as keyof DualMarginFields];
}

function ProfitStatusTag({
  status,
  kind,
}: {
  status: string | null | undefined;
  kind: "parts" | "contribution";
}) {
  const meta = status
    ? (
      kind === "parts"
        ? PARTS_PROFIT_STATUS_META[status]
        : CONTRIBUTION_STATUS_META[status]
    )
    : undefined;
  if (!meta) {
    return (
      <Tooltip title="后端尚未提供可核实的毛利状态。">
        <span
          tabIndex={0}
          aria-label="结果未提供：后端尚未提供可核实的毛利状态。"
        >
          <Tag>结果未提供</Tag>
        </span>
      </Tooltip>
    );
  }
  return (
    <Tooltip title={meta.detail}>
      <span tabIndex={0} aria-label={`${meta.label}：${meta.detail}`}>
        <Tag color={meta.color}>{meta.label}</Tag>
      </span>
    </Tooltip>
  );
}

function MarginFacts({ row, basis }: {
  row: DualMarginFields;
  basis: "inc" | "ex";
}) {
  const partsStatus = marginValue(
    row,
    basis,
    "parts_status",
  ) as string | null | undefined;
  const rawContributionStatus = marginValue(
    row,
    basis,
    "contribution_status",
  ) as string | null | undefined;
  const partsComplete = (
    partsStatus === "complete_actual" || partsStatus === "complete_estimated"
  );
  const contributionStatus = partsComplete
    ? rawContributionStatus
    : "parts_profit_unavailable";
  const partsProfitAllowed = partsComplete;
  const contributionAllowed = partsComplete && contributionStatus === "complete";
  const revenueAllowed = partsComplete || partsStatus === "incomplete_cost";
  const partsCostAllowed = partsComplete || (
    partsStatus === "missing_revenue"
    || partsStatus === "missing_tax_rate"
    || partsStatus === "invalid_tax_rate"
    || partsStatus === "ambiguous_revenue"
    || partsStatus === "filtered_scope"
  );
  // UI 同样 fail-closed：阻断/未知状态即便夹带脏数字，也不能显示为财务结论。
  const revenue = revenueAllowed
    ? marginValue(row, basis, "revenue") as number | null | undefined
    : null;
  const partsCost = partsCostAllowed
    ? marginValue(row, basis, "parts_cost") as number | null | undefined
    : null;
  const partsProfit = partsProfitAllowed
    ? marginValue(row, basis, "parts_profit") as number | null | undefined
    : null;
  const partsMargin = partsProfitAllowed
    ? marginValue(row, basis, "parts_margin") as number | null | undefined
    : null;
  const contributionProfit = contributionAllowed
    ? marginValue(row, basis, "contribution_profit") as number | null | undefined
    : null;
  const contributionMargin = contributionAllowed
    ? marginValue(row, basis, "contribution_margin") as number | null | undefined
    : null;
  const hasEvidence = [
    revenue,
    partsCost,
    partsProfit,
    partsMargin,
    contributionProfit,
    contributionMargin,
    partsStatus,
    rawContributionStatus,
  ].some((value) => value != null);
  if (!hasEvidence) return <span style={{ color: "var(--mb-text-3)" }}>—</span>;

  return (
    <div
      data-testid={`maintenance-margin-card-${basis}`}
      style={{ fontSize: 12.5, lineHeight: 1.7 }}
    >
      <div>
        <strong>合同级备件毛利</strong>{" "}
        <ProfitStatusTag status={partsStatus} kind="parts" />
      </div>
      <div>合同收入 {money(revenue)}</div>
      <div>备件成本 {money(partsCost)}</div>
      <div>毛利 {money(partsProfit)} · {pct(partsMargin, 2)}</div>
      <div style={{ marginTop: 4 }}>
        <strong>合同级贡献毛利</strong>{" "}
        <ProfitStatusTag status={contributionStatus} kind="contribution" />
      </div>
      <div>
        贡献毛利 {money(contributionProfit)} · {pct(contributionMargin, 2)}
      </div>
    </div>
  );
}

const BOARD_STATUSES = new Set<BoardStatus>([
  "incomplete_cost", "expense_data_unavailable",
  "red", "yellow", "green", "no_budget",
]);

function normalizeDecisionStatus(
  decisionStatus: string | null | undefined,
): BoardStatus | null {
  return BOARD_STATUSES.has(decisionStatus as BoardStatus)
    ? decisionStatus as BoardStatus
    : null;
}

function normalizeCostQuality(value: string | null | undefined): CostQuality | null {
  if (
    value === "actual_only"
    || value === "contains_estimate"
    || value === "incomplete"
  ) return value;
  return null;
}

function effectiveBoardStatus(row: BoardRow): BoardStatus | null {
  const status = normalizeDecisionStatus(row.decision_status);
  return normalizeCostQuality(row.cost_quality) === "incomplete"
    || status === "incomplete_cost"
    ? "incomplete_cost"
    : status;
}

export function buildOrderExportParams(
  preset: ExportDatePreset,
  range: [Dayjs, Dayjs] | null,
) {
  if (preset !== "all" && !range) return null;
  return range ? {
    date_from: range[0].format("YYYY-MM-DD"),
    date_to: range[1].format("YYYY-MM-DD"),
  } : {};
}

export function formatRoundtripImportSummary(data: unknown): string {
  if (!data || typeof data !== "object") return "回填文件导入成功";
  const row = data as Record<string, unknown>;
  if (typeof row.message === "string" && row.message.trim()) return row.message.trim();
  if (row.no_op === true) return "该工作簿此前已成功导入，本次未重复更新";
  const counts = (
    row.counts && typeof row.counts === "object"
      ? row.counts
      : {}
  ) as Record<string, unknown>;
  const protocolParts: string[] = [];
  if (typeof row.changed_rows === "number") {
    protocolParts.push(`变更 ${row.changed_rows} 行`);
  }
  const protocolMetrics: Array<[string, string]> = [
    ["新增", "create"],
    ["更新", "update"],
    ["作废", "void"],
    ["保留", "keep"],
  ];
  for (const [label, key] of protocolMetrics) {
    if (typeof counts[key] === "number") {
      protocolParts.push(`${label} ${counts[key]} 行`);
    }
  }
  if (protocolParts.length) return `回填完成：${protocolParts.join(" · ")}`;
  const metrics: Array<[string, string[]]> = [
    ["处理", ["rows_total", "total_rows", "processed"]],
    ["新增", ["rows_inserted", "inserted", "created"]],
    ["更新", ["rows_updated", "updated"]],
    ["跳过", ["rows_skipped", "skipped"]],
    ["失败", ["rows_error", "errors", "rejected"]],
  ];
  const parts = metrics.flatMap(([label, keys]) => {
    const key = keys.find((candidate) => typeof row[candidate] === "number");
    return key ? [`${label} ${row[key]} 行`] : [];
  });
  return parts.length ? `回填完成：${parts.join(" · ")}` : "回填文件导入成功";
}

export function formatMaintenanceRecomputeSummary(
  data: Record<string, number | null | undefined>,
): string {
  return (
    `重算完成：${data.lines_in_scope ?? 0} 行 · 专属采购 ${data.direct ?? 0}` +
    ` · ±7天 ${data.window ?? 0} · 当月均价 ${data.month_avg ?? 0}` +
    ` · 池采购 ${data.pool_purchase ?? 0} · 池销售 ${data.pool_sales ?? 0}` +
    ` · 本PN采购 ${data.purchase_history ?? 0} · 本PN销售 ${data.sales_history ?? 0}` +
    ` · 人工回填 ${data.manual ?? 0} · 无成本 ${data.none ?? 0}`
  );
}

function presetRange(preset: ExportDatePreset, anchor: Dayjs): [Dayjs, Dayjs] {
  if (preset === "today") return [anchor, anchor];
  if (preset === "last7") return [anchor.subtract(6, "day"), anchor];
  if (preset === "last14") return [anchor.subtract(13, "day"), anchor];
  if (preset === "last21") return [anchor.subtract(20, "day"), anchor];
  if (preset === "last30") return [anchor.subtract(29, "day"), anchor];
  return [anchor.startOf("month"), anchor];
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    try {
      document.body.appendChild(anchor);
      anchor.click();
    } finally {
      anchor.remove();
    }
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(url), 100);
  }
}

type BlobDownloadResponse = {
  data: Blob;
  headers?: unknown;
};

class InvalidDownloadResponseError extends Error {
  constructor(message = "服务器返回的不是可下载文件，请稍后重试或联系管理员") {
    super(message);
  }
}

class DownloadSessionChangedError extends Error {
  constructor() {
    super("登录账号已变更，旧会话下载已取消，请重新操作");
  }
}

const CSV_CONTENT_TYPES = ["text/csv", "application/csv"] as const;
const XLSX_CONTENT_TYPES = [
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
] as const;
const ZIP_CONTENT_TYPES = ["application/zip", "application/x-zip-compressed"] as const;

function boundAuthorizationHeaders(sessionToken: string | null): Record<string, string> | undefined {
  return sessionToken ? { Authorization: `Bearer ${sessionToken}` } : undefined;
}

function responseHeader(headers: unknown, name: string): string | undefined {
  if (!headers || typeof headers !== "object") return undefined;
  const getter = (headers as { get?: unknown }).get;
  if (typeof getter === "function") {
    const value = getter.call(headers, name);
    return typeof value === "string" ? value : undefined;
  }
  const record = headers as Record<string, unknown>;
  const value = record[name] ?? record[name.toLowerCase()] ?? record[name.toUpperCase()];
  return typeof value === "string" ? value : undefined;
}

function safeDownloadFilename(value: string | undefined, fallback: string): string {
  if (!value) return fallback;
  const basename = value
    .split(/[\\/]/)
    .pop()
    ?.replace(/\p{Cf}/gu, "")
    .replace(/[\u0000-\u001f\u007f:*?"<>|]/g, "_")
    .trim();
  return basename || fallback;
}

function responseFilename(headers: unknown, fallback: string): string {
  const disposition = responseHeader(headers, "content-disposition");
  if (!disposition) return fallback;
  const encoded = disposition.match(/filename\*\s*=\s*UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      return safeDownloadFilename(decodeURIComponent(encoded.trim().replace(/^"|"$/g, "")), fallback);
    } catch {
      return fallback;
    }
  }
  const quoted = disposition.match(/filename\s*=\s*"([^"]+)"/i)?.[1];
  const plain = disposition.match(/filename\s*=\s*([^;\s]+)/i)?.[1];
  return safeDownloadFilename(quoted || plain, fallback);
}

const ZIP_EOCD_MIN_BYTES = 22;
const ZIP_EOCD_MAX_COMMENT_BYTES = 0xffff;
const ZIP_EOCD_SEARCH_BYTES = ZIP_EOCD_MIN_BYTES + ZIP_EOCD_MAX_COMMENT_BYTES;
const ZIP_MAX_CENTRAL_DIRECTORY_BYTES = 32 * 1024 * 1024;
const ZIP_MAX_ENTRY_COUNT = 4096;
const ZIP_LOCAL_HEADER_BYTES = 30;
const ZIP_CENTRAL_HEADER_BYTES = 46;

function invalidArchiveError(isXlsx: boolean): InvalidDownloadResponseError {
  return new InvalidDownloadResponseError(
    isXlsx
      ? "服务器返回的 Excel 文件损坏或结构不完整，已取消下载"
      : "服务器返回的 ZIP 文件损坏或结构不完整，已取消下载",
  );
}

function unsupportedZip64Error(): InvalidDownloadResponseError {
  return new InvalidDownloadResponseError(
    "服务器返回的文件使用了暂不支持的 ZIP64 格式，已取消下载，请联系管理员",
  );
}

function throwIfDownloadAborted(signal?: AbortSignal): void {
  if (signal?.aborted) {
    throw new DOMException("The download was aborted.", "AbortError");
  }
}

async function blobSliceBytes(
  blob: Blob,
  start: number,
  end: number,
  signal?: AbortSignal,
): Promise<Uint8Array> {
  throwIfDownloadAborted(signal);
  if (
    !Number.isSafeInteger(start)
    || !Number.isSafeInteger(end)
    || start < 0
    || end < start
    || end > blob.size
  ) {
    throw new Error("invalid blob slice");
  }
  const slice = blob.slice(start, end);
  const arrayBuffer = (slice as Blob & {
    arrayBuffer?: () => Promise<ArrayBuffer>;
  }).arrayBuffer;
  if (typeof arrayBuffer === "function") {
    const bytes = new Uint8Array(await arrayBuffer.call(slice));
    throwIfDownloadAborted(signal);
    return bytes;
  }
  const bytes = await new Promise<Uint8Array>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (reader.result instanceof ArrayBuffer) {
        resolve(new Uint8Array(reader.result));
      } else {
        reject(new Error("download prefix is not binary"));
      }
    };
    reader.onerror = () => reject(reader.error || new Error("download prefix read failed"));
    reader.readAsArrayBuffer(slice);
  });
  throwIfDownloadAborted(signal);
  return bytes;
}

function zipView(bytes: Uint8Array): DataView {
  return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
}

function hasZip64Extra(extra: Uint8Array): boolean | null {
  const view = zipView(extra);
  let cursor = 0;
  while (cursor < extra.length) {
    if (cursor + 4 > extra.length) return null;
    const fieldId = view.getUint16(cursor, true);
    const fieldSize = view.getUint16(cursor + 2, true);
    cursor += 4;
    if (cursor + fieldSize > extra.length) return null;
    if (fieldId === 0x0001) return true;
    cursor += fieldSize;
  }
  return false;
}

function decodeZipEntryName(bytes: Uint8Array, utf8: boolean): string | null {
  try {
    return new TextDecoder(utf8 ? "utf-8" : "windows-1252", {
      fatal: utf8,
    }).decode(bytes);
  } catch {
    return null;
  }
}

type ParsedZipEntry = {
  compressedSize: number;
  crc32: number;
  flags: number;
  localHeaderOffset: number;
  method: number;
  name: string;
  nameBytes: Uint8Array;
  uncompressedSize: number;
};

async function validateZipContainer(
  blob: Blob,
  requireXlsx: boolean,
  signal?: AbortSignal,
): Promise<void> {
  const invalid = (): never => {
    throw invalidArchiveError(requireXlsx);
  };
  throwIfDownloadAborted(signal);
  if (blob.size < ZIP_EOCD_MIN_BYTES) invalid();
  if (blob.size > 0xffffffff) throw unsupportedZip64Error();

  const tailStart = Math.max(0, blob.size - ZIP_EOCD_SEARCH_BYTES);
  const tail = await blobSliceBytes(blob, tailStart, blob.size, signal);
  const tailView = zipView(tail);
  let eocdInTail = -1;
  for (let cursor = tail.length - ZIP_EOCD_MIN_BYTES; cursor >= 0; cursor -= 1) {
    if (tailView.getUint32(cursor, true) !== 0x06054b50) continue;
    const commentLength = tailView.getUint16(cursor + 20, true);
    if (cursor + ZIP_EOCD_MIN_BYTES + commentLength === tail.length) {
      eocdInTail = cursor;
      break;
    }
  }
  if (eocdInTail < 0) invalid();

  if (
    eocdInTail >= 20
    && tailView.getUint32(eocdInTail - 20, true) === 0x07064b50
  ) {
    throw unsupportedZip64Error();
  }

  const diskNumber = tailView.getUint16(eocdInTail + 4, true);
  const centralDiskNumber = tailView.getUint16(eocdInTail + 6, true);
  const entriesOnDisk = tailView.getUint16(eocdInTail + 8, true);
  const entryCount = tailView.getUint16(eocdInTail + 10, true);
  const centralSize = tailView.getUint32(eocdInTail + 12, true);
  const centralOffset = tailView.getUint32(eocdInTail + 16, true);
  if (
    entriesOnDisk === 0xffff
    || entryCount === 0xffff
    || centralSize === 0xffffffff
    || centralOffset === 0xffffffff
  ) {
    throw unsupportedZip64Error();
  }
  if (
    diskNumber !== 0
    || centralDiskNumber !== 0
    || entriesOnDisk !== entryCount
    || entryCount > ZIP_MAX_ENTRY_COUNT
    || centralSize > ZIP_MAX_CENTRAL_DIRECTORY_BYTES
  ) {
    invalid();
  }
  const eocdOffset = tailStart + eocdInTail;
  if (
    !Number.isSafeInteger(centralOffset + centralSize)
    || centralOffset + centralSize !== eocdOffset
  ) {
    invalid();
  }
  if (entryCount === 0) {
    invalid();
  }
  if (
    centralSize < entryCount * ZIP_CENTRAL_HEADER_BYTES
    || centralOffset >= eocdOffset
  ) {
    invalid();
  }

  const central = await blobSliceBytes(
    blob,
    centralOffset,
    centralOffset + centralSize,
    signal,
  );
  const centralView = zipView(central);
  const entries: ParsedZipEntry[] = [];
  const names = new Set<string>();
  let cursor = 0;
  for (let index = 0; index < entryCount; index += 1) {
    throwIfDownloadAborted(signal);
    if (
      cursor + ZIP_CENTRAL_HEADER_BYTES > central.length
      || centralView.getUint32(cursor, true) !== 0x02014b50
    ) {
      invalid();
    }
    const flags = centralView.getUint16(cursor + 8, true);
    const method = centralView.getUint16(cursor + 10, true);
    const crc32 = centralView.getUint32(cursor + 16, true);
    const compressedSize = centralView.getUint32(cursor + 20, true);
    const uncompressedSize = centralView.getUint32(cursor + 24, true);
    const nameLength = centralView.getUint16(cursor + 28, true);
    const extraLength = centralView.getUint16(cursor + 30, true);
    const commentLength = centralView.getUint16(cursor + 32, true);
    const diskStart = centralView.getUint16(cursor + 34, true);
    const localHeaderOffset = centralView.getUint32(cursor + 42, true);
    if (
      compressedSize === 0xffffffff
      || uncompressedSize === 0xffffffff
      || localHeaderOffset === 0xffffffff
      || diskStart === 0xffff
    ) {
      throw unsupportedZip64Error();
    }
    if (diskStart !== 0 || nameLength === 0) invalid();
    const recordEnd = (
      cursor
      + ZIP_CENTRAL_HEADER_BYTES
      + nameLength
      + extraLength
      + commentLength
    );
    if (recordEnd > central.length) invalid();
    const nameBytes = central.slice(
      cursor + ZIP_CENTRAL_HEADER_BYTES,
      cursor + ZIP_CENTRAL_HEADER_BYTES + nameLength,
    );
    const extra = central.slice(
      cursor + ZIP_CENTRAL_HEADER_BYTES + nameLength,
      cursor + ZIP_CENTRAL_HEADER_BYTES + nameLength + extraLength,
    );
    const zip64Extra = hasZip64Extra(extra);
    if (zip64Extra == null) invalid();
    if (zip64Extra) throw unsupportedZip64Error();
    const name = (
      decodeZipEntryName(nameBytes, (flags & 0x0800) !== 0) ?? invalid()
    );
    if (name.length === 0) invalid();
    if (names.has(name)) invalid();
    names.add(name);
    entries.push({
      compressedSize,
      crc32,
      flags,
      localHeaderOffset,
      method,
      name,
      nameBytes,
      uncompressedSize,
    });
    cursor = recordEnd;
  }
  if (cursor !== central.length) invalid();

  const localOffsets = new Set<number>();
  const localRanges: Array<[number, number]> = [];
  for (const entry of entries) {
    throwIfDownloadAborted(signal);
    if (
      entry.localHeaderOffset >= centralOffset
      || localOffsets.has(entry.localHeaderOffset)
    ) {
      invalid();
    }
    localOffsets.add(entry.localHeaderOffset);
    const fixedEnd = entry.localHeaderOffset + ZIP_LOCAL_HEADER_BYTES;
    if (fixedEnd > centralOffset) invalid();
    const local = await blobSliceBytes(
      blob,
      entry.localHeaderOffset,
      fixedEnd,
      signal,
    );
    const localView = zipView(local);
    if (localView.getUint32(0, true) !== 0x04034b50) invalid();
    const localFlags = localView.getUint16(6, true);
    const localMethod = localView.getUint16(8, true);
    const localCrc32 = localView.getUint32(14, true);
    const localCompressedSize = localView.getUint32(18, true);
    const localUncompressedSize = localView.getUint32(22, true);
    const localNameLength = localView.getUint16(26, true);
    const localExtraLength = localView.getUint16(28, true);
    if (
      localFlags !== entry.flags
      || localMethod !== entry.method
      || localNameLength !== entry.nameBytes.length
    ) {
      invalid();
    }
    if (
      localCompressedSize === 0xffffffff
      || localUncompressedSize === 0xffffffff
    ) {
      throw unsupportedZip64Error();
    }
    if (
      (entry.flags & 0x0008) === 0
      && (
        localCrc32 !== entry.crc32
        || localCompressedSize !== entry.compressedSize
        || localUncompressedSize !== entry.uncompressedSize
      )
    ) {
      invalid();
    }
    const variableEnd = fixedEnd + localNameLength + localExtraLength;
    if (variableEnd > centralOffset) invalid();
    const variable = await blobSliceBytes(blob, fixedEnd, variableEnd, signal);
    const localName = variable.slice(0, localNameLength);
    if (
      localName.some((value, index) => value !== entry.nameBytes[index])
    ) {
      invalid();
    }
    const localZip64Extra = hasZip64Extra(variable.slice(localNameLength));
    if (localZip64Extra == null) invalid();
    if (localZip64Extra) throw unsupportedZip64Error();
    const dataEnd = variableEnd + entry.compressedSize;
    if (!Number.isSafeInteger(dataEnd) || dataEnd > centralOffset) invalid();
    localRanges.push([entry.localHeaderOffset, dataEnd]);
  }
  localRanges.sort(([left], [right]) => left - right);
  for (let index = 1; index < localRanges.length; index += 1) {
    if (localRanges[index][0] < localRanges[index - 1][1]) invalid();
  }

  if (
    requireXlsx
    && (!names.has("[Content_Types].xml") || !names.has("xl/workbook.xml"))
  ) {
    throw new InvalidDownloadResponseError(
      "服务器返回的 Excel 文件不是有效的 XLSX 工作簿，已取消下载",
    );
  }
}

async function saveDownloadResponse(
  response: BlobDownloadResponse,
  fallbackFilename: string,
  expectedTypes: readonly string[],
  beforeSave?: () => boolean,
  signal?: AbortSignal,
): Promise<void> {
  throwIfDownloadAborted(signal);
  if (!(response.data instanceof Blob)) throw new InvalidDownloadResponseError();
  if (response.data.size === 0) {
    throw new InvalidDownloadResponseError(
      "服务器返回了空文件，已取消下载，请重试或联系管理员",
    );
  }
  const contentType = (
    responseHeader(response.headers, "content-type")
    || response.data.type
  ).split(";")[0].trim().toLowerCase();
  if (!contentType) {
    throw new InvalidDownloadResponseError(
      "服务器未返回文件类型，已取消下载，请重试或联系管理员",
    );
  }
  if (!expectedTypes.includes(contentType)) {
    throw new InvalidDownloadResponseError();
  }
  if (expectedTypes.includes(XLSX_CONTENT_TYPES[0])) {
    await validateZipContainer(response.data, true, signal);
  } else if (expectedTypes.includes(ZIP_CONTENT_TYPES[0])) {
    await validateZipContainer(response.data, false, signal);
  }
  throwIfDownloadAborted(signal);
  if (beforeSave && !beforeSave()) return;
  saveBlob(response.data, responseFilename(response.headers, fallbackFilename));
}

const EXPORT_VALIDATION_PARAM_LABELS: Record<string, string> = {
  q: "项目搜索",
  project: "项目名称",
  contract: "合同编号",
  date_from: "开始日期",
  date_to: "结束日期",
  lifecycle: "期限状态",
  month: "月份",
  status: "提醒类型",
  file: "文件",
};

function formatExportValidationDetail(detail: unknown): string | undefined {
  if (typeof detail === "string") return detail.trim() || undefined;
  if (!Array.isArray(detail)) return undefined;
  const messages = detail.flatMap((item): string[] => {
    if (typeof item === "string") return item.trim() ? [item.trim()] : [];
    if (!item || typeof item !== "object") return [];
    const row = item as Record<string, unknown>;
    const location = Array.isArray(row.loc) ? row.loc : [];
    const parameter = [...location].reverse().find(
      (value): value is string => typeof value === "string"
        && value !== "query"
        && value !== "body",
    );
    const label = parameter
      ? EXPORT_VALIDATION_PARAM_LABELS[parameter] || `请求参数 ${parameter}`
      : "请求参数";
    const context = (
      row.ctx && typeof row.ctx === "object"
        ? row.ctx
        : {}
    ) as Record<string, unknown>;
    if (
      row.type === "string_too_long"
      && Number.isSafeInteger(context.max_length)
    ) {
      return [`${label}不能超过 ${context.max_length} 个字符`];
    }
    if (
      row.type === "string_too_short"
      && Number.isSafeInteger(context.min_length)
    ) {
      return [`${label}不能少于 ${context.min_length} 个字符`];
    }
    if (row.type === "missing") return [`${label}不能为空`];
    if (row.type === "string_pattern_mismatch") return [`${label}格式不正确`];
    const rawMessage = typeof row.msg === "string"
      ? row.msg.replace(/\p{Cf}/gu, "").trim()
      : "";
    return rawMessage ? [`${label}：${rawMessage}`] : [`${label}校验失败`];
  });
  const uniqueMessages = [...new Set(messages)].slice(0, 3);
  return uniqueMessages.length ? uniqueMessages.join("；") : undefined;
}

async function readExportError(error: unknown): Promise<{
  status?: number;
  detail?: string;
}> {
  if (
    error instanceof InvalidDownloadResponseError
    || error instanceof DownloadSessionChangedError
  ) {
    return { detail: error.message };
  }
  const response = (error as {
    response?: { status?: number; data?: unknown };
  })?.response;
  let detail: string | undefined;
  if (
    response?.status != null
    && [403, 404, 413, 422, 429].includes(response.status)
    && response.data instanceof Blob
  ) {
    try {
      const text = typeof response.data.text === "function"
        ? await response.data.text()
        : await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result || ""));
          reader.onerror = () => reject(reader.error);
          reader.readAsText(response.data as Blob);
        });
      const body = JSON.parse(text) as { detail?: unknown };
      detail = formatExportValidationDetail(body.detail);
    } catch {
      // 非 JSON 错误体只使用安全的状态提示。
    }
  }
  return { status: response?.status, detail };
}

function SourceTag({ source, trace, distance }: {
  source: string | null; trace?: number | null; distance?: number | null;
}) {
  if (!source) return <span style={{ color: "var(--mb-text-3)" }}>-</span>;
  const m = SOURCE_META[source] || { label: source, color: "default" };
  const suffix = source === "window" && distance != null ? `·距${distance}天`
    : trace && trace >= 1 ? `·追溯${trace}月` : "";
  return <Tag color={m.color}>{m.label}{suffix}</Tag>;
}

function LifecycleTag({ status }: { status: LifecycleStatus }) {
  const meta = LIFECYCLE_META[status];
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

// 成本来源图例（列头 Tooltip 用，避免用户逐个 hover 才懂颜色）
const SourceLegend = (
  <div>
    {SOURCE_ORDER.map((k) => (
      <div key={k} style={{ whiteSpace: "nowrap" }}>
        <Tag color={SOURCE_META[k].color}>■</Tag>{SOURCE_META[k].label}
      </div>
    ))}
  </div>
);

export default function ProjectCostPage({
  view = "data",
}: {
  view?: "data" | "downloads" | "reminders";
}) {
  const role = localStorage.getItem("role") || "";
  const isAdmin = role === "admin";
  let localPermissions: Record<string, boolean> = {};
  try {
    localPermissions = JSON.parse(localStorage.getItem("permissions") || "{}");
  } catch {
    localPermissions = {};
  }
  const scopedSales = !isAdmin && (
    localPermissions.own_customers_only === true
    || (localPermissions.own_customers_only == null && role === "sales")
  );
  const canExportProjectWorkbooks = isAdmin || (
    !scopedSales
    && localPermissions.data_purchase_cost === true
    && localPermissions.data_profit === true
  );
  const canExportRoundtripWorkbooks = canExportProjectWorkbooks && (
    isAdmin || localPermissions.data_customer === true
  );
  const canApplyRoundtripWorkbook = canExportRoundtripWorkbooks && (
    isAdmin || localPermissions.action_maintenance_roundtrip_apply === true
  );
  const maintenanceBasis = useTaxBasis("maintenance");
  const [exportRange, setExportRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [exportDatePreset, setExportDatePreset] = useState<ExportDatePreset>("all");
  const exportDatePresetRef = useRef<ExportDatePreset>("all");
  const [exportAsOf, setExportAsOf] = useState("");
  const [exportScopeLoading, setExportScopeLoading] = useState(false);
  const [exportScopeError, setExportScopeError] = useState(false);
  const exportScopeSeq = useRef(0);
  const exportScopeRequest = useRef<{
    controller: AbortController;
    preset: ExportDatePreset;
    promise: Promise<[Dayjs, Dayjs] | null>;
    sessionToken: string | null;
  } | null>(null);
  const mountedRef = useRef(true);
  const [q, setQ] = useState("");
  const [downloadProjectQuery, setDownloadProjectQuery] = useState("");
  const [downloadProjectLifecycle, setDownloadProjectLifecycle] =
    useState<LifecycleFilter>("all");
  const [downloadProject, setDownloadProject] = useState("");
  const [downloadContract, setDownloadContract] = useState("");
  const [lifecycle, setLifecycle] = useState<LifecycleFilter>("ongoing");
  const [reminderStatus, setReminderStatus] = useState<ReminderStatusFilter>("all");
  const [projectLifecycleCounts, setProjectLifecycleCounts] =
    useState<LifecycleCounts>(EMPTY_LIFECYCLE_COUNTS);
  const [boardLifecycleCounts, setBoardLifecycleCounts] =
    useState<LifecycleCounts>(EMPTY_LIFECYCLE_COUNTS);
  const [projectAsOf, setProjectAsOf] = useState("");
  const [boardAsOf, setBoardAsOf] = useState("");
  const [rows, setRows] = useState<ProjectRow[]>([]);
  const [projectCostRestricted, setProjectCostRestricted] = useState(false);
  const [board, setBoard] = useState<BoardRow[]>([]);
  const [boardDecisionRestricted, setBoardDecisionRestricted] = useState(false);
  const [reminderFilterRestricted, setReminderFilterRestricted] = useState(false);
  const [reminderFilterRejected, setReminderFilterRejected] = useState(false);
  const [boardPage, setBoardPage] = useState(1);
  const [startDate, setStartDate] = useState("");
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [projectsLoadError, setProjectsLoadError] = useState(false);
  const [boardLoading, setBoardLoading] = useState(false);
  const [boardLoadError, setBoardLoadError] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportingWorkbooks, setExportingWorkbooks] = useState(false);
  const [exportingProjects, setExportingProjects] = useState(false);
  const [exportingProfit, setExportingProfit] = useState(false);
  const [exportingLines, setExportingLines] = useState(false);
  const [exportingSingleWorkbook, setExportingSingleWorkbook] = useState(false);
  const [downloadingTemplate, setDownloadingTemplate] = useState(false);
  const [downloadingTemplateBundle, setDownloadingTemplateBundle] = useState(false);
  const downloadControllersRef = useRef<Set<AbortController>>(new Set());
  const operationLocksRef = useRef<Set<string>>(new Set());
  const [importingRoundtrip, setImportingRoundtrip] = useState(false);
  const [activeDownloads, setActiveDownloads] = useState<Record<string, string>>({});
  const [roundtripResult, setRoundtripResult] = useState<string | null>(null);
  const [roundtripError, setRoundtripError] = useState<string | null>(null);
  // 明细抽屉
  const [detailProject, setDetailProject] = useState<string | null>(null);
  const [lines, setLines] = useState<LineRow[]>([]);
  const [linesTotal, setLinesTotal] = useState(0);
  const [linesPage, setLinesPage] = useState(1);
  const [linesMonth, setLinesMonth] = useState<string | undefined>(undefined);
  const [linesLoading, setLinesLoading] = useState(false);
  // 请求序号守卫：抽屉快速翻页/切项目时，迟到响应不得覆盖新结果
  const linesSeq = useRef(0);
  const detailRef = useRef<string | null>(null);
  // 两类业务对象独立加载、独立失败、独立重试；各自用请求代次阻止迟到响应覆盖新筛选。
  const projectsSeq = useRef(0);
  const boardSeq = useRef(0);
  const baseParams = () => ({
    q: q || undefined,
  });
  const beginDownload = (key: string, label: string) => {
    setActiveDownloads((current) => ({ ...current, [key]: label }));
  };
  const endDownload = (key: string) => {
    setActiveDownloads((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
  };
  const acquireOperation = (key: string): boolean => {
    if (operationLocksRef.current.has(key)) return false;
    operationLocksRef.current.add(key);
    return true;
  };
  const releaseOperation = (key: string) => {
    operationLocksRef.current.delete(key);
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      exportScopeSeq.current += 1;
      exportScopeRequest.current?.controller.abort();
      exportScopeRequest.current = null;
      for (const controller of downloadControllersRef.current) controller.abort();
      downloadControllersRef.current.clear();
      operationLocksRef.current.clear();
    };
  }, []);

  const lifecycleParams = () => ({ ...baseParams(), lifecycle });
  const boardParams = () => ({
    q: q || undefined,
    lifecycle,
    status: view === "reminders" && reminderStatus !== "all"
      ? reminderStatus
      : undefined,
  });

  const loadProjects = async () => {
    const seq = ++projectsSeq.current;
    setProjectsLoading(true);
    setProjectsLoadError(false);
    setRows([]);
    setProjectCostRestricted(false);
    setStartDate("");
    setProjectAsOf("");
    setProjectLifecycleCounts(EMPTY_LIFECYCLE_COUNTS);
    try {
      const { data } = await api.get("/maintenance/projects", {
        params: lifecycleParams(),
      });
      if (seq !== projectsSeq.current) return;
      setRows(data.rows);
      setProjectCostRestricted(!!data.ranking_restricted);
      setStartDate(data.start_date);
      setProjectAsOf(data.as_of || "");
      setProjectLifecycleCounts(data.lifecycle_counts || EMPTY_LIFECYCLE_COUNTS);
    } catch {
      if (seq !== projectsSeq.current) return;
      setRows([]);
      setProjectCostRestricted(false);
      setProjectsLoadError(true);
    } finally {
      if (seq === projectsSeq.current) setProjectsLoading(false);
    }
  };

  const loadBoard = async () => {
    if (scopedSales) {
      boardSeq.current += 1;
      setBoard([]);
      setBoardPage(1);
      setBoardLoading(false);
      setBoardLoadError(false);
      setBoardDecisionRestricted(true);
      setReminderFilterRestricted(true);
      setReminderFilterRejected(false);
      setBoardAsOf("");
      setBoardLifecycleCounts(EMPTY_LIFECYCLE_COUNTS);
      return;
    }
    const seq = ++boardSeq.current;
    setBoardLoading(true);
    setBoardLoadError(false);
    setBoard([]);
    setBoardPage(1);
    setBoardDecisionRestricted(false);
    setBoardAsOf("");
    setBoardLifecycleCounts(EMPTY_LIFECYCLE_COUNTS);
    try {
      const { data } = await api.get("/maintenance/board", {
        params: boardParams(),
      });
      if (seq !== boardSeq.current) return;
      setBoard(data.rows);
      setBoardAsOf(data.as_of || "");
      setBoardLifecycleCounts(data.lifecycle_counts || EMPTY_LIFECYCLE_COUNTS);
      setBoardDecisionRestricted(
        data.decision_restricted === true
        || data.profit_restricted === true
        || data.ranking_restricted === true,
      );
      const statusFilterRestricted = (
        data.decision_restricted === true
        || data.profit_restricted === true
        || data.ranking_restricted === true
      );
      setReminderFilterRestricted(statusFilterRestricted);
      const requestedReminderStatus = reminderStatus !== "all";
      const statusFilterRejected = requestedReminderStatus
        && data.status_filter_applied !== true;
      if (requestedReminderStatus && !statusFilterRestricted) {
        setReminderFilterRejected(statusFilterRejected);
      }
      if (statusFilterRestricted || statusFilterRejected) {
        setReminderStatus("all");
      }
    } catch {
      if (seq !== boardSeq.current) return;
      setBoard([]);
      setBoardDecisionRestricted(false);
      if (view === "reminders" && reminderStatus !== "all") {
        setReminderFilterRejected(true);
        setReminderStatus("all");
      }
      setBoardLoadError(true);
    } finally {
      if (seq === boardSeq.current) setBoardLoading(false);
    }
  };

  useEffect(() => {
    if (view === "downloads") {
      projectsSeq.current += 1;
      boardSeq.current += 1;
      exportScopeSeq.current += 1;
      exportScopeRequest.current?.controller.abort();
      exportScopeRequest.current = null;
      exportDatePresetRef.current = "all";
      setExportDatePreset("all");
      setExportRange(null);
      setExportAsOf("");
      setExportScopeLoading(false);
      setExportScopeError(false);
      setProjectsLoading(false);
      setBoardLoading(false);
      setProjectsLoadError(false);
      setBoardLoadError(false);
      setRows([]);
      setBoard([]);
      setProjectAsOf("");
      setBoardAsOf("");
      setProjectLifecycleCounts(EMPTY_LIFECYCLE_COUNTS);
      setBoardLifecycleCounts(EMPTY_LIFECYCLE_COUNTS);
      return;
    }
    if (view === "reminders") {
      projectsSeq.current += 1;
      setProjectsLoading(false);
      setProjectsLoadError(false);
      setRows([]);
      void loadBoard();
      return;
    }
    void loadProjects();
    void loadBoard();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, lifecycle, reminderStatus, view]);

  const loadLines = async (project: string, page: number, month?: string) => {
    const seq = ++linesSeq.current;
    setLinesLoading(true);
    try {
      const { data } = await api.get("/maintenance/lines", {
        params: { project, page, page_size: 50, month, ...baseParams() },
      });
      // 仅当仍是最新请求且抽屉未切项目时才落地（防竞态覆盖）
      if (seq !== linesSeq.current || detailRef.current !== project) return;
      setLines(data.rows);
      setLinesTotal(data.total);
      setLinesPage(page);
    } catch {
      if (seq === linesSeq.current) message.error("明细加载失败");
    } finally {
      if (seq === linesSeq.current) setLinesLoading(false);
    }
  };

  const openDetail = (project: string) => {
    detailRef.current = project;
    setDetailProject(project);
    setLines([]);
    setLinesTotal(0);
    setLinesPage(1);
    setLinesMonth(undefined);
    loadLines(project, 1);
  };

  const closeDetail = () => {
    detailRef.current = null;
    setDetailProject(null);
  };

  const recompute = async () => {
    setRecomputing(true);
    const hide = message.loading("正在重算项目成本，约需 1 分钟，请勿刷新页面…", 0);
    try {
      const { data } = await api.post("/maintenance/recompute");
      message.success(formatMaintenanceRecomputeSummary(data));
      void loadProjects();
      void loadBoard();
    } catch {
      message.error("重算失败（需要管理员权限）");
    } finally {
      hide();
      setRecomputing(false);
    }
  };

  const requestAndSaveDownload = async (
    path: string,
    params: Record<string, unknown>,
    filename: string,
    expectedTypes: readonly string[],
    sessionToken = localStorage.getItem("token"),
  ): Promise<void> => {
    if (!mountedRef.current) return;
    if (localStorage.getItem("token") !== sessionToken) {
      throw new DownloadSessionChangedError();
    }
    const controller = new AbortController();
    downloadControllersRef.current.add(controller);
    try {
      const response = await api.get(path, {
        params,
        responseType: "blob",
        signal: controller.signal,
        headers: boundAuthorizationHeaders(sessionToken),
      });
      if (controller.signal.aborted || !mountedRef.current) return;
      if (localStorage.getItem("token") !== sessionToken) {
        throw new DownloadSessionChangedError();
      }
      await saveDownloadResponse(response, filename, expectedTypes, () => {
        if (controller.signal.aborted || !mountedRef.current) return false;
        if (localStorage.getItem("token") !== sessionToken) {
          throw new DownloadSessionChangedError();
        }
        return true;
      }, controller.signal);
    } catch (error) {
      if (controller.signal.aborted) return;
      throw error;
    } finally {
      downloadControllersRef.current.delete(controller);
    }
  };

  const download = (
    path: string,
    filename: string,
    expectedTypes: readonly string[],
    extra?: Record<string, unknown>,
    sessionToken = localStorage.getItem("token"),
  ) => requestAndSaveDownload(
    path,
    { ...baseParams(), ...extra },
    filename,
    expectedTypes,
    sessionToken,
  );

  const requestRelativeExportScope = (
    requestedPreset: ExportDatePreset,
    sessionToken = localStorage.getItem("token"),
  ): Promise<[Dayjs, Dayjs] | null> => {
    const currentRequest = exportScopeRequest.current;
    if (
      currentRequest
      && !currentRequest.controller.signal.aborted
      && currentRequest.preset === requestedPreset
      && currentRequest.sessionToken === sessionToken
    ) {
      return currentRequest.promise;
    }
    currentRequest?.controller.abort();
    const seq = ++exportScopeSeq.current;
    const controller = new AbortController();
    setExportScopeLoading(true);
    setExportScopeError(false);
    setExportRange(null);
    setExportAsOf("");
    let request!: NonNullable<typeof exportScopeRequest.current>;
    const promise = api.get("/maintenance/as-of", {
      signal: controller.signal,
      headers: boundAuthorizationHeaders(sessionToken),
    })
      .then(({ data }) => {
        if (localStorage.getItem("token") !== sessionToken) {
          controller.abort();
        }
        if (
          controller.signal.aborted
          || !mountedRef.current
          || seq !== exportScopeSeq.current
          || exportDatePresetRef.current !== requestedPreset
        ) return null;
        const latestAsOf = typeof data?.as_of === "string" ? data.as_of : "";
        const anchor = latestAsOf ? dayjs(latestAsOf) : null;
        if (!anchor?.isValid()) throw new Error("invalid as_of");
        const resolvedRange = presetRange(requestedPreset, anchor);
        setExportAsOf(latestAsOf);
        setExportRange(resolvedRange);
        return resolvedRange;
      })
      .catch(() => {
        if (
          !controller.signal.aborted
          && mountedRef.current
          && seq === exportScopeSeq.current
          && exportDatePresetRef.current === requestedPreset
          && localStorage.getItem("token") === sessionToken
        ) {
          setExportScopeError(true);
          message.error("日期基准加载失败，请重试该日期档");
        }
        return null;
      })
      .finally(() => {
        if (mountedRef.current && seq === exportScopeSeq.current) {
          setExportScopeLoading(false);
        }
        if (exportScopeRequest.current === request) {
          exportScopeRequest.current = null;
        }
      });
    request = {
      controller,
      preset: requestedPreset,
      promise,
      sessionToken,
    };
    exportScopeRequest.current = request;
    return promise;
  };

  const resolveExportScope = async (
    requestedPreset: ExportDatePreset,
    sessionToken: string | null,
  ): Promise<{
    params: { date_from?: string; date_to?: string };
    range: [Dayjs, Dayjs] | null;
  } | null> => {
    if (!mountedRef.current) return null;
    if (localStorage.getItem("token") !== sessionToken) {
      throw new DownloadSessionChangedError();
    }
    let resolvedRange = exportRange;
    if (requestedPreset !== "all" && requestedPreset !== "custom") {
      const active = exportScopeRequest.current;
      resolvedRange = (
        active?.preset === requestedPreset
        && active.sessionToken === sessionToken
      )
        ? await active.promise
        : await requestRelativeExportScope(requestedPreset, sessionToken);
      if (!mountedRef.current) return null;
      if (localStorage.getItem("token") !== sessionToken) {
        throw new DownloadSessionChangedError();
      }
      if (!resolvedRange || exportDatePresetRef.current !== requestedPreset) return null;
    }
    const params = buildOrderExportParams(requestedPreset, resolvedRange);
    if (!params) {
      message.warning(requestedPreset === "custom"
        ? "请选择自定义起止日期"
        : "日期基准尚未加载，请稍后重试");
      return null;
    }
    return { params, range: resolvedRange };
  };

  const exportOrders = async () => {
    if (!acquireOperation("orders")) return;
    const sessionToken = localStorage.getItem("token");
    setExporting(true);
    beginDownload("orders", "正在生成订单汇总 Excel，请勿关闭页面或重复点击");
    try {
      const scope = await resolveExportScope(exportDatePreset, sessionToken);
      if (!scope) return;
      await requestAndSaveDownload(
        "/maintenance/orders/export",
        scope.params,
        scope.range
          ? `maintenance_orders_${scope.range[0].format("YYYY-MM-DD")}_${scope.range[1].format("YYYY-MM-DD")}.xlsx`
          : "maintenance_orders_all.xlsx",
        XLSX_CONTENT_TYPES,
        sessionToken,
      );
    } catch (error) {
      const { status, detail } = await readExportError(error);
      message.error(detail || (status === 403
        ? "无权限导出维保订单"
        : status === 422
          ? "导出日期参数无效"
          : "导出失败，请稍后重试"));
    } finally {
      endDownload("orders");
      setExporting(false);
      releaseOperation("orders");
    }
  };

  const exportWorkbooks = async () => {
    if (!acquireOperation("workbooks")) return;
    const sessionToken = localStorage.getItem("token");
    setExportingWorkbooks(true);
    const hide = message.loading("正在生成批量工作簿，数据较多时可能需要 1–2 分钟，请勿重复点击…", 0);
    try {
      const scope = await resolveExportScope(exportDatePreset, sessionToken);
      if (!scope) return;
      await requestAndSaveDownload(
        "/maintenance/export-workbooks",
        scope.params,
        scope.range
          ? `maintenance_project_workbooks_${scope.range[0].format("YYYY-MM-DD")}_${scope.range[1].format("YYYY-MM-DD")}.zip`
          : "maintenance_project_workbooks_all.zip",
        ZIP_CONTENT_TYPES,
        sessionToken,
      );
    } catch (error) {
      const { status, detail } = await readExportError(error);
      message.error(detail || (status === 403
        ? "无权限导出项目工作簿"
        : status === 422
          ? "批量项目工作簿导出范围无效"
          : status === 429
            ? "已有批量工作簿导出正在执行，请稍后重试"
            : "批量项目工作簿导出失败，请稍后重试"));
    } finally {
      hide();
      setExportingWorkbooks(false);
      releaseOperation("workbooks");
    }
  };

  const exportProjectsCsv = async () => {
    if (!acquireOperation("projects-csv")) return;
    const sessionToken = localStorage.getItem("token");
    setExportingProjects(true);
    try {
      const scope = await resolveExportScope(exportDatePreset, sessionToken);
      if (!scope) return;
      await requestAndSaveDownload(
        "/maintenance/export",
        {
          ...scope.params,
          q: downloadProjectQuery.trim() || undefined,
          lifecycle: downloadProjectLifecycle,
        },
        "maintenance_projects.csv",
        CSV_CONTENT_TYPES,
        sessionToken,
      );
    } catch (error) {
      const { detail } = await readExportError(error);
      message.error(detail || "项目 CSV 导出失败，请稍后重试或检查权限");
    } finally {
      setExportingProjects(false);
      releaseOperation("projects-csv");
    }
  };

  const exportContractProfitCsv = async () => {
    if (!acquireOperation("contract-profit")) return;
    const sessionToken = localStorage.getItem("token");
    setExportingProfit(true);
    beginDownload("contract-profit", "正在生成合同详细盈亏 CSV，请勿重复点击");
    try {
      const scope = await resolveExportScope(exportDatePreset, sessionToken);
      if (!scope) return;
      await download(
        "/maintenance/board/export",
        "maintenance_contract_profit.csv",
        CSV_CONTENT_TYPES,
        {
          ...scope.params,
          lifecycle: "all",
        },
        sessionToken,
      );
    } catch (error) {
      const { detail } = await readExportError(error);
      message.error(detail || "合同详细盈亏 CSV 导出失败，请稍后重试或检查权限");
    } finally {
      endDownload("contract-profit");
      setExportingProfit(false);
      releaseOperation("contract-profit");
    }
  };

  const exportLines = async (project = detailProject) => {
    if (!project) {
      message.warning("请先输入要导出的项目名称");
      return;
    }
    if (!acquireOperation("project-lines")) return;
    const sessionToken = localStorage.getItem("token");
    setExportingLines(true);
    beginDownload("project-lines", "正在生成单项目明细 CSV，请勿重复点击");
    try {
      const scope = await resolveExportScope(exportDatePreset, sessionToken);
      if (!scope) return;
      await download("/maintenance/lines/export", "项目备件明细.csv", CSV_CONTENT_TYPES, {
        ...scope.params,
        project,
        month: linesMonth,
      }, sessionToken);
    } catch (error) {
      const { detail } = await readExportError(error);
      message.error(detail || "明细导出失败，请稍后重试");
    } finally {
      endDownload("project-lines");
      setExportingLines(false);
      releaseOperation("project-lines");
    }
  };

  const exportSingleWorkbook = async (contract: string) => {
    if (!acquireOperation("single-workbook")) return;
    const sessionToken = localStorage.getItem("token");
    setExportingSingleWorkbook(true);
    beginDownload("single-workbook", "正在生成单合同工作簿 XLSX，请勿重复点击");
    try {
      const scope = await resolveExportScope(exportDatePreset, sessionToken);
      if (!scope) return;
      await download(
        "/maintenance/export-workbook",
        `项目工作簿_${contract}.xlsx`,
        XLSX_CONTENT_TYPES,
        {
        ...scope.params,
        contract,
        },
        sessionToken,
      );
    } catch (error) {
      const { detail } = await readExportError(error);
      message.error(detail || "工作簿导出失败，请稍后重试或检查权限");
    } finally {
      endDownload("single-workbook");
      setExportingSingleWorkbook(false);
      releaseOperation("single-workbook");
    }
  };

  const downloadRoundtripTemplate = async (contract?: string) => {
    if (!acquireOperation("roundtrip-template")) return;
    const sessionToken = localStorage.getItem("token");
    setDownloadingTemplate(true);
    setRoundtripError(null);
    const hide = message.loading("正在生成固定回填模板，请勿重复点击…", 0);
    try {
      const scope = await resolveExportScope(exportDatePreset, sessionToken);
      if (!scope) return;
      const safeContract = contract?.replace(/[\\/:*?"<>|]/g, "_");
      const scopeLabel = scope.range
        ? `${scope.range[0].format("YYYY-MM-DD")}_${scope.range[1].format("YYYY-MM-DD")}`
        : "全部";
      await requestAndSaveDownload(
        "/maintenance/roundtrip-template",
        {
          ...scope.params,
          ...(contract ? { contract } : {}),
        },
        `维保项目回填模板_${safeContract ? `${safeContract}_` : ""}${scopeLabel}.xlsx`,
        XLSX_CONTENT_TYPES,
        sessionToken,
      );
    } catch (error) {
      const { detail } = await readExportError(error);
      const text = detail || "固定回填模板下载失败，请稍后重试";
      setRoundtripError(text);
      message.error(text);
    } finally {
      hide();
      setDownloadingTemplate(false);
      releaseOperation("roundtrip-template");
    }
  };

  const downloadRoundtripTemplateBundle = async () => {
    if (!acquireOperation("roundtrip-bundle")) return;
    const sessionToken = localStorage.getItem("token");
    setDownloadingTemplateBundle(true);
    setRoundtripError(null);
    beginDownload(
      "roundtrip-bundle",
      "正在按合同生成可回填工作簿 ZIP，请勿关闭页面或重复点击",
    );
    try {
      const scope = await resolveExportScope(exportDatePreset, sessionToken);
      if (!scope) return;
      await requestAndSaveDownload(
        "/maintenance/roundtrip-templates",
        scope.params,
        scope.range
          ? `维保项目批量回填模板_${scope.range[0].format("YYYY-MM-DD")}_${scope.range[1].format("YYYY-MM-DD")}.zip`
          : "维保项目批量回填模板.zip",
        ZIP_CONTENT_TYPES,
        sessionToken,
      );
    } catch (error) {
      const { detail } = await readExportError(error);
      const text = detail || "批量可回填工作簿下载失败，请稍后重试";
      setRoundtripError(text);
      message.error(text);
    } finally {
      endDownload("roundtrip-bundle");
      setDownloadingTemplateBundle(false);
      releaseOperation("roundtrip-bundle");
    }
  };

  const importRoundtripWorkbook = async (file: File) => {
    if (!acquireOperation("roundtrip-import")) return;
    setImportingRoundtrip(true);
    setRoundtripResult(null);
    setRoundtripError(null);
    try {
      const body = new FormData();
      body.append("file", file);
      const { data } = await api.post("/maintenance/roundtrip-import", body);
      const summary = formatRoundtripImportSummary(data);
      setRoundtripResult(summary);
      message.success(summary);
    } catch (error) {
      const response = (
        typeof error === "object" && error !== null && "response" in error
      )
        ? (error as { response?: { data?: { detail?: unknown } } }).response
        : undefined;
      const detail = typeof response?.data?.detail === "string"
        ? response.data.detail
        : "回填工作簿导入失败，请检查模板内容后重试";
      setRoundtripError(detail);
      message.error(detail);
    } finally {
      setImportingRoundtrip(false);
      releaseOperation("roundtrip-import");
    }
  };

  const projectCols: ColumnsType<ProjectRow> = [
    { title: "项目", dataIndex: "project", width: 300, fixed: "left", ellipsis: true },
    { title: "期限状态", dataIndex: "lifecycle_status", width: 100,
      render: (v: LifecycleStatus) => <LifecycleTag status={v} /> },
    { title: "维保终止日期", dataIndex: "maint_end", width: 120,
      render: (v: string | null) => v || <span style={{ color: "var(--mb-warning)" }}>未填写</span> },
    { title: "出库行", dataIndex: "lines", width: 80, align: "right" },
    { title: "维保订单数", dataIndex: "order_count", width: 110, align: "right" },
    { title: "结构完整性", dataIndex: "structure_complete", width: 180,
      render: (_: boolean, row) => (
        row.structure_complete === true && (row.missing_detail_orders ?? 0) === 0
          ? <Tag color="green">完整</Tag>
          : <Tag color="orange">
              不完整 · 无明细 {row.missing_detail_orders ?? "—"} 单
            </Tag>
      ) },
    { title: "数量", dataIndex: "qty", width: 80, align: "right" },
    ...(maintenanceBasis !== "ex" ? [
      { title: "实际参考(含税)", dataIndex: "actual_cost_inc", width: 130, align: "right" as const, render: money },
      { title: "估算参考(含税)", dataIndex: "estimated_cost_inc", width: 130, align: "right" as const, render: money },
    ] : []),
    ...(maintenanceBasis !== "inc" ? [
      { title: "实际参考(未税)", dataIndex: "actual_cost_ex", width: 140, align: "right" as const, render: money },
      { title: "估算参考(未税)", dataIndex: "estimated_cost_ex", width: 140, align: "right" as const, render: money },
    ] : []),
    { title: "成本完整性", dataIndex: "cost_quality", width: 160,
      render: (rawQuality: string | null, row) => {
        // 成本字段确由 RBAC 隐藏时保持中性；不受限响应的 null/未知值仍 fail-closed。
        const quality = normalizeCostQuality(rawQuality);
        if ((row.missing_detail_orders ?? 0) > 0) {
          return (
            <Tag color="orange">
              需补数据 · 无明细 {row.missing_detail_orders} 单
              {row.missing_cost_lines == null
                ? ""
                : ` · 缺成本 ${row.missing_cost_lines} 行`}
            </Tag>
          );
        }
        if (quality == null && projectCostRestricted) return "—";
        if (quality == null || quality === "incomplete") {
          return <Tag color="orange">需补数据 · {row.missing_cost_lines ?? "—"} 行</Tag>;
        }
        if (quality === "contains_estimate") {
          return <Tag color="gold">完整 · 含估算 {row.estimated_lines ?? "—"} 行</Tag>;
        }
        return <Tag color="green">完整 · 仅实际参考</Tag>;
      } },
    { title: "已知成本兼容参考(混合原值)", dataIndex: "known_cost_total",
      width: 190, align: "right", render: money },
    { title: (
        <Tooltip title="有成本来源的出库行占比（按行数；销售参考价也计入已覆盖）。低于阈值提示核对无成本行。">
          覆盖率 <InfoCircleOutlined style={{ color: "var(--mb-text-3)" }} />
        </Tooltip>
      ), dataIndex: "coverage_pct", width: 120,
      render: (v: number | null) => v == null ? "-" :
        <Progress percent={v} size="small" status={v < COVERAGE_WARN_PCT ? "exception" : "normal"} /> },
    { title: (
        <Tooltip title={SourceLegend}>
          成本来源分布 <InfoCircleOutlined style={{ color: "var(--mb-text-3)" }} />
        </Tooltip>
      ), dataIndex: "by_source", width: 240,
      render: (bs: Record<string, number> | null) => bs == null ? "—" : (
          <Space size={2} wrap>
            {SOURCE_ORDER.map((k) =>
              bs[k] ? <Tooltip key={k} title={SOURCE_META[k].label}>
                <Tag color={SOURCE_META[k].color}>{bs[k]}</Tag></Tooltip> : null)}
          </Space>
        ) },
    { title: "合同额(参考)", dataIndex: "contract_amount", width: 150, align: "right",
      render: (v: number | null, r) => (
        <span>
          {money(v)}
          {r.contract_shared && (
            <Tooltip title="该合同被多个项目共同引用，金额跨项目重复，仅作参考（本期不计算单项目毛利）">
              <Tag color="warning" style={{ marginLeft: 4 }}>共用</Tag>
            </Tooltip>
          )}
          {r.contract_incomplete && (
            <Tooltip title="部分关联销售订单未在系统中（合同额被低估），请补导对应销售数据">
              <Tag color="default" style={{ marginLeft: 4 }}>不全</Tag>
            </Tooltip>
          )}
        </span>
      ) },
    { title: "月份数", dataIndex: "months", width: 80, align: "right" },
    { title: "", width: 70, fixed: "right", render: (_, r) => <a onClick={() => openDetail(r.project)}>明细</a> },
  ];

  const lineCols: ColumnsType<LineRow> = [
    { title: "日期", dataIndex: "order_date", width: 100 },
    { title: "维保单号", dataIndex: "order_no", width: 160, ellipsis: true },
    { title: "需求类型", dataIndex: "demand_type", width: 90 },
    { title: "PN", dataIndex: "pn_std", width: 160, ellipsis: true },
    { title: "描述", dataIndex: "description", ellipsis: true },
    { title: "数量", dataIndex: "qty", width: 70, align: "right" },
    { title: "退货", dataIndex: "return_qty", width: 70, align: "right",
      render: (v: number | null) => (v ? <Tag color="orange">{v}</Tag> : "-") },
    ...(maintenanceBasis !== "ex" ? [
      { title: "单位成本(含税)", dataIndex: "unit_cost_inc_tax", width: 120, align: "right" as const, render: money },
      { title: "成本金额(含税)", dataIndex: "cost_amount_inc_tax", width: 130, align: "right" as const, render: money },
    ] : []),
    ...(maintenanceBasis !== "inc" ? [
      { title: "单位成本(未税)", dataIndex: "unit_cost_ex_tax", width: 120, align: "right" as const, render: money },
      { title: "成本金额(未税)", dataIndex: "cost_amount_ex_tax", width: 130, align: "right" as const, render: money },
    ] : []),
    { title: "成本事实层级", dataIndex: "cost_tier", width: 120,
      render: (value: string | null) => value && COST_TIER_META[value]
        ? <Tag color={COST_TIER_META[value].color}>{COST_TIER_META[value].label}</Tag>
        : "—" },
    { title: "成本来源", width: 180,
      render: (_, r) => <SourceTag source={r.cost_tier === "missing" ? "none" : r.cost_source}
                                   trace={r.trace_months}
                                   distance={r.price_distance_days} /> },
    { title: "置信度", dataIndex: "confidence", width: 76,
      render: (v: string | null) => v
        ? <Tag color={CONF_META[v]?.color}>{CONF_META[v]?.label || v}</Tag>
        : <span style={{ color: "var(--mb-text-3)" }}>-</span> },
    { title: "口径", dataIndex: "cost_tax_basis", width: 70,
      render: (v: string | null) => v === "inc"
        ? <Tag>含税</Tag>
        : v === "ex"
          ? <Tag>不含税</Tag>
          : v
            ? <Tag color="orange">未知</Tag>
            : "-" },
    { title: "取价月", dataIndex: "price_month", width: 90 },
    { title: "关联采购单", dataIndex: "linked_purchase_order_no", width: 160, ellipsis: true,
      render: (v: string | null) => v || <span style={{ color: "var(--mb-text-3)" }}>-</span> },
  ];

  // 全部为 null 表示权限脱敏，不能把它折算成 0；空结果则按 0 展示。
  const sumRows = (values: (number | null)[]) => {
    if (rows.length === 0) return 0;
    if (values.every((value) => value == null)) return null;
    return values.reduce<number>((total, value) => total + (value ?? 0), 0);
  };
  const totalActualInc = sumRows(rows.map((row) => row.actual_cost_inc));
  const totalActualEx = sumRows(rows.map((row) => row.actual_cost_ex));
  const totalEstimatedInc = sumRows(rows.map((row) => row.estimated_cost_inc));
  const totalEstimatedEx = sumRows(rows.map((row) => row.estimated_cost_ex));
  const totalMissing = sumRows(rows.map((row) => row.missing_cost_lines));
  const costStat = (value: number | null) => value == null
    ? { value: "—" as const }
    : { value, precision: 2, prefix: "¥" };
  // 抽屉月份下拉：由当前项目行的 months 数不便直接取月份列表，简单用近 24 个月占位由后端过滤
  const monthOptions = detailProject
    ? (rows.find((r) => r.project === detailProject)?.months || 0)
    : 0;
  const pagedBoard = board.slice(
    (boardPage - 1) * BOARD_PAGE_SIZE,
    boardPage * BOARD_PAGE_SIZE,
  );

  if (view === "downloads") {
    return (
      <Space direction="vertical" size="large" style={{ width: "100%" }}>
        <PageHeader
          title="下载中心"
          subtitle="集中选择日期和业务对象后下载；大文件生成期间按钮保持锁定，失败时显示服务端的准确原因。"
        />
        <Card title="下载范围">
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <div style={{
              width: "100%",
              minWidth: 0,
              maxWidth: "100%",
              overflowX: "auto",
              paddingBottom: 2,
            }}>
              <Segmented
                aria-label="维保订单导出日期"
                value={exportDatePreset}
                onChange={(value) => {
                  const preset = value as ExportDatePreset;
                  exportDatePresetRef.current = preset;
                  setExportDatePreset(preset);
                  if (preset === "all" || preset === "custom") {
                    exportScopeSeq.current += 1;
                    exportScopeRequest.current?.controller.abort();
                    exportScopeRequest.current = null;
                    setExportScopeLoading(false);
                    setExportScopeError(false);
                    setExportRange(null);
                    setExportAsOf("");
                  } else {
                    void requestRelativeExportScope(preset);
                  }
                }}
                options={[
                  { label: "全部", value: "all" },
                  { label: "今天", value: "today" },
                  { label: "近7天", value: "last7" },
                  { label: "近14天", value: "last14" },
                  { label: "近21天", value: "last21" },
                  { label: "近30天", value: "last30" },
                  { label: "本月", value: "month" },
                  { label: "自定义", value: "custom" },
                ]}
              />
            </div>
            <DatePicker.RangePicker
              aria-label="导出自定义起止日期"
              value={exportRange}
              onChange={(value) => {
                exportScopeSeq.current += 1;
                exportScopeRequest.current?.controller.abort();
                exportScopeRequest.current = null;
                exportDatePresetRef.current = "custom";
                setExportDatePreset("custom");
                setExportScopeLoading(false);
                setExportScopeError(false);
                setExportAsOf("");
                setExportRange(value as [Dayjs, Dayjs] | null);
              }}
            />
            <Alert
              type={exportScopeError ? "error" : "info"}
              showIcon
              message={exportScopeError
                ? "日期基准加载失败，尚未应用日期范围。"
                : exportScopeLoading
                ? "正在读取后端业务日并计算实际闭区间…"
                : exportDatePreset !== "all"
                  && exportDatePreset !== "custom"
                  && exportRange
                  ? `实际闭区间：${exportRange[0].format("YYYY-MM-DD")} 至 ${
                    exportRange[1].format("YYYY-MM-DD")
                  }（含首尾）`
                  : exportDatePreset === "custom"
                    ? "自定义日期按所选首尾日期闭区间导出。"
                    : "相对日期按后端业务日计算；“全部”不附带日期范围。"}
              description={exportAsOf
                ? `后端业务日 ${exportAsOf}`
                : undefined}
              action={exportScopeError ? (
                <Button
                  size="small"
                  danger
                  loading={exportScopeLoading}
                  onClick={() => void requestRelativeExportScope(exportDatePresetRef.current)}
                >
                  重试日期基准
                </Button>
              ) : undefined}
            />
            {Object.entries(activeDownloads).map(([key, label]) => (
              <Alert
                key={key}
                type="info"
                showIcon
                role="status"
                message={label}
              />
            ))}
          </Space>
        </Card>

        <Card title="项目与合同下载">
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Card size="small" title="项目成本 CSV 筛选">
              <Space direction="vertical" size={10} style={{ width: "100%" }}>
                <div style={{ color: "var(--mb-text-3)", fontSize: 12.5 }}>
                  仅影响“导出项目成本 CSV”；日期仍使用上方统一下载范围。
                </div>
                <Row gutter={[12, 12]}>
                  <Col xs={24} md={10}>
                    <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 7 }}>
                      项目搜索
                    </div>
                    <Input
                      aria-label="项目成本 CSV 项目搜索"
                      placeholder="按项目名称关键词筛选（可选）"
                      allowClear
                      maxLength={DOWNLOAD_PROJECT_QUERY_MAX_LENGTH}
                      value={downloadProjectQuery}
                      onChange={(event) => setDownloadProjectQuery(event.target.value)}
                      style={{ width: "100%" }}
                    />
                  </Col>
                  <Col xs={24} md={14}>
                    <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 7 }}>
                      期限状态
                    </div>
                    <div style={{
                      width: "100%",
                      minWidth: 0,
                      maxWidth: "100%",
                      overflowX: "auto",
                      paddingBottom: 2,
                    }}>
                      <Segmented
                        aria-label="项目成本 CSV 期限状态筛选"
                        value={downloadProjectLifecycle}
                        onChange={(value) => {
                          setDownloadProjectLifecycle(value as LifecycleFilter);
                        }}
                        options={[
                          { label: "全部期限", value: "all" },
                          { label: "进行中", value: "ongoing" },
                          { label: "已结束", value: "ended" },
                          { label: "期限缺失", value: "missing" },
                        ]}
                      />
                    </div>
                  </Col>
                </Row>
                <Button
                  loading={exportingProjects}
                  disabled={exportingProjects}
                  onClick={exportProjectsCsv}
                >
                  导出项目成本 CSV
                </Button>
              </Space>
            </Card>
            <Space wrap>
              {!scopedSales && (
                <Button
                  loading={exportingProfit}
                  disabled={exportingProfit}
                  onClick={exportContractProfitCsv}
                >
                  导出合同详细盈亏 CSV
                </Button>
              )}
              {!scopedSales && (
                <Button loading={exporting} disabled={exporting} onClick={exportOrders}>
                  导出订单汇总 Excel
                </Button>
              )}
              {canExportProjectWorkbooks && (
                <Button
                  type="primary"
                  loading={exportingWorkbooks}
                  disabled={exportingWorkbooks}
                  onClick={exportWorkbooks}
                >
                  批量导出项目工作簿 ZIP
                </Button>
              )}
            </Space>
            <Alert
              type="info"
              showIcon
              message={scopedSales
                ? "项目成本 CSV 与单项目明细仅包含当前销售本人范围；合同级下载需要完整合同口径，受限销售账号不提供。"
                : "项目成本 CSV 按项目汇总备件成本；合同详细盈亏 CSV 按合同汇总收入、成本、费用、利润与证据状态。"}
            />
            <Space wrap style={{ width: "100%" }}>
              <Input
                aria-label="单项目名称"
                placeholder="输入完整项目名称"
                maxLength={DOWNLOAD_PROJECT_NAME_MAX_LENGTH}
                value={downloadProject}
                onChange={(event) => setDownloadProject(event.target.value)}
                style={{ width: "min(320px, 100%)" }}
              />
              <Button
                loading={exportingLines}
                disabled={exportingLines}
                onClick={() => void exportLines(downloadProject.trim())}
              >
                导出单项目明细 CSV
              </Button>
            </Space>
            {canExportProjectWorkbooks && (
              <Space wrap style={{ width: "100%" }}>
                <Input
                  aria-label="单合同编号"
                  placeholder="输入完整合同编号"
                  maxLength={DOWNLOAD_CONTRACT_MAX_LENGTH}
                  value={downloadContract}
                  onChange={(event) => setDownloadContract(event.target.value)}
                  style={{ width: "min(320px, 100%)" }}
                />
                <Button
                  loading={exportingSingleWorkbook}
                  disabled={exportingSingleWorkbook}
                  onClick={() => downloadContract.trim()
                    ? void exportSingleWorkbook(downloadContract.trim())
                    : message.warning("请先输入要导出的合同编号")}
                >
                  导出单合同工作簿 XLSX
                </Button>
                {canExportRoundtripWorkbooks && (
                  <Button
                    loading={downloadingTemplate}
                    disabled={downloadingTemplate}
                    onClick={() => void downloadRoundtripTemplate(
                      downloadContract.trim() || undefined,
                    )}
                  >
                    下载固定回填模板
                  </Button>
                )}
              </Space>
            )}
          </Space>
        </Card>

        {canExportRoundtripWorkbooks && (
          <Card title="固定回填工作簿">
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
              <Alert
                type="info"
                showIcon
                message="全量固定模板返回 413 时，请输入单合同、缩小日期范围，或改用按合同拆分的批量可回填 ZIP；每本仍需单独导入。"
              />
              <Button
                type="primary"
                loading={downloadingTemplateBundle}
                disabled={downloadingTemplateBundle}
                onClick={() => void downloadRoundtripTemplateBundle()}
              >
                批量下载可回填工作簿 ZIP
              </Button>
              {canApplyRoundtripWorkbook && (
                <Upload
                  accept=".xlsx"
                  maxCount={1}
                  showUploadList={false}
                  disabled={importingRoundtrip}
                  beforeUpload={(file) => {
                    void importRoundtripWorkbook(file);
                    return false;
                  }}
                >
                  <Button loading={importingRoundtrip} disabled={importingRoundtrip}>
                    导入更新工作簿
                  </Button>
                </Upload>
              )}
              {roundtripResult && <Alert type="success" showIcon message={roundtripResult} />}
              {roundtripError && <Alert type="error" showIcon message={roundtripError} />}
            </Space>
          </Card>
        )}
      </Space>
    );
  }

  if (view === "reminders") {
    return (
      <Space direction="vertical" size="large" style={{ width: "100%" }}>
        <PageHeader
          title="项目提醒"
          subtitle="集中查看期限、成本完整性、费用水位和预算参考；提醒不阻挡项目数据阅读。"
        />
        <Card title="提醒筛选">
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <div>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 7 }}>维保期限</div>
              <div style={{
                width: "100%",
                minWidth: 0,
                maxWidth: "100%",
                overflowX: "auto",
                paddingBottom: 2,
              }}>
                <Segmented
                  aria-label="维保期限筛选"
                  value={lifecycle}
                  onChange={(value) => setLifecycle(value as LifecycleFilter)}
                  options={[
                    { label: `进行中 ${boardLifecycleCounts.ongoing}`, value: "ongoing" },
                    { label: `已结束 ${boardLifecycleCounts.ended}`, value: "ended" },
                    { label: `期限缺失 ${boardLifecycleCounts.missing}`, value: "missing" },
                    {
                      label: `全部 ${
                        boardLifecycleCounts.ongoing
                        + boardLifecycleCounts.ended
                        + boardLifecycleCounts.missing
                      }`,
                      value: "all",
                    },
                  ]}
                />
              </div>
            </div>
            <div>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 7 }}>提醒类型</div>
              <div style={{
                width: "100%",
                minWidth: 0,
                maxWidth: "100%",
                overflowX: "auto",
                paddingBottom: 2,
              }}>
                <Segmented
                  aria-label="项目提醒类型筛选"
                  value={reminderStatus}
                  onChange={(value) => {
                    setReminderFilterRejected(false);
                    setReminderStatus(value as ReminderStatusFilter);
                  }}
                  options={[
                    { label: "全部提醒", value: "all" },
                    {
                      label: "待补成本",
                      value: "incomplete_cost",
                      disabled: reminderFilterRestricted || scopedSales,
                    },
                    {
                      label: "待补费用",
                      value: "expense_data_unavailable",
                      disabled: reminderFilterRestricted || scopedSales,
                    },
                    {
                      label: "红色预警",
                      value: "red",
                      disabled: reminderFilterRestricted || scopedSales,
                    },
                    {
                      label: "黄色关注",
                      value: "yellow",
                      disabled: reminderFilterRestricted || scopedSales,
                    },
                    {
                      label: "绿色参考",
                      value: "green",
                      disabled: reminderFilterRestricted || scopedSales,
                    },
                    {
                      label: "无预算",
                      value: "no_budget",
                      disabled: reminderFilterRestricted || scopedSales,
                    },
                  ]}
                />
              </div>
              {(reminderFilterRestricted || scopedSales) && (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginTop: 8 }}
                  message={scopedSales
                    ? "受限销售账号不提供合同级经营提醒"
                    : "当前账号无权判断经营提醒类型"}
                  description={scopedSales
                    ? "项目事实仍按当前销售本人范围提供；合同提醒需要完整合同口径。"
                    : "已重置为全部提醒；期限与获准事实仍可查看。"}
                />
              )}
              {reminderFilterRejected && !reminderFilterRestricted && !scopedSales && (
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginTop: 8 }}
                  message="提醒类型筛选未被后端确认应用"
                  description="已回退为全部提醒；只有后端明确返回 applied=true 才保留具体经营筛选。"
                />
              )}
            </div>
            <Input.Search
              placeholder="搜索项目名"
              allowClear
              style={{ width: "min(320px, 100%)" }}
              onChange={(event) => {
                if (!event.target.value) setQ("");
              }}
              onSearch={(value) => setQ(value.trim())}
            />
          </Space>
        </Card>
        <Card title="项目提醒" loading={boardLoading}>
          {scopedSales ? (
            <Alert
              type="info"
              showIcon
              message="受限销售账号不提供合同级经营提醒"
              description="请在项目数据中查看本人范围的项目事实。"
            />
          ) : boardLoadError ? (
            <Alert
              type="error"
              showIcon
              message="项目提醒加载失败，旧结果已清空。"
              action={<Button size="small" danger onClick={() => void loadBoard()}>重试</Button>}
            />
          ) : board.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选暂无提醒" />
          ) : (
            <Space direction="vertical" size={10} style={{ width: "100%" }}>
              {pagedBoard.map((item) => {
                const knownCostQuality = normalizeCostQuality(item.cost_quality);
                const status = boardDecisionRestricted
                  ? knownCostQuality === "incomplete" ? "incomplete_cost" : null
                  : effectiveBoardStatus(item) ?? "incomplete_cost";
                const meta = status == null ? null : STATUS_META[status];
                return (
                  <Alert
                    key={item.contract || "(none)"}
                    type={status === "red" || status === "incomplete_cost" ? "warning" : "info"}
                    showIcon
                    message={
                      <Space wrap>
                        <b>{item.contract || "（未关联合同）"}</b>
                        <Tag color={status === "red" ? "red" : status === "yellow" ? "gold" : "default"}>
                          {meta?.label || "经营判断受限"}
                        </Tag>
                        <LifecycleTag status={item.lifecycle_status} />
                      </Space>
                    }
                    description={status == null
                      ? "当前账号仅展示获准事实，不推断成本、费用或预算状态"
                      : `成本缺失 ${item.missing_cost_lines ?? "—"} 行 · 费用${
                        item.expense_data_available === true ? "已就绪" : "未就绪"
                      }`}
                  />
                );
              })}
              {board.length > BOARD_PAGE_SIZE && (
                <Pagination
                  aria-label="项目提醒分页"
                  current={boardPage}
                  pageSize={BOARD_PAGE_SIZE}
                  total={board.length}
                  showSizeChanger={false}
                  onChange={setBoardPage}
                  style={{ marginTop: 8, textAlign: "right" }}
                />
              )}
            </Space>
          )}
        </Card>
      </Space>
    );
  }

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="项目数据"
        subtitle={`维保备件成本按实际采购参考、估算参考、成本缺失分层；合同毛利同时保留含税与未税事实，证据不完整时保持空值${startDate ? ` · 起算日 ${startDate}` : ""}`}
      />
      <Card
        title={<Space>详细盈亏
          <Tooltip title="按合同聚合收入、备件成本与费用。含税、未税独立计算；任一口径证据不完整时，该口径毛利保持为空并显示原因。">
            <InfoCircleOutlined style={{ color: "var(--mb-text-3)" }} />
          </Tooltip>
          <Tag>{`详细盈亏截止 ${boardAsOf || "—"}`}</Tag>
        </Space>}
        loading={boardLoading}
      >
        <Space direction="vertical" size={12} style={{ width: "100%", marginBottom: 12 }}>
          <div>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 7 }}>维保期限</div>
            <div style={{
              width: "100%",
              minWidth: 0,
              maxWidth: "100%",
              overflowX: "auto",
              paddingBottom: 2,
            }}>
              <Segmented
                aria-label="维保期限筛选"
                value={lifecycle}
                onChange={(value) => setLifecycle(value as LifecycleFilter)}
                options={[
                  { label: `进行中 ${boardLifecycleCounts.ongoing}`, value: "ongoing" },
                  { label: `已结束 ${boardLifecycleCounts.ended}`, value: "ended" },
                  { label: <span style={{
                    color: boardLifecycleCounts.missing ? "#d46b08" : undefined,
                  }}>
                    期限缺失 {boardLifecycleCounts.missing}
                  </span>, value: "missing" },
                  { label: `全部 ${
                    boardLifecycleCounts.ongoing
                    + boardLifecycleCounts.ended
                    + boardLifecycleCounts.missing
                  }`,
                    value: "all" },
                ]}
              />
            </div>
          </div>
          <Input.Search
            placeholder="搜索项目名"
            allowClear
            style={{ width: "min(260px, 100%)" }}
            onChange={(event) => {
              if (!event.target.value) setQ("");
            }}
            onSearch={(value) => setQ(value.trim())}
          />
        </Space>
        <Space wrap style={{ marginBottom: 12 }}>
          <Tag color="blue">{PROFIT_BASIS_LABEL[maintenanceBasis]}</Tag>
          <span style={{ color: "var(--mb-text-3)", fontSize: 12.5 }}>
            由管理员在系统设置中统一配置，普通员工不能临时切换。
          </span>
        </Space>
        {boardDecisionRestricted && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="当前账号不展示合同金额、毛利等受限字段"
            description="实际、估算、缺失等成本事实仍按账号的数据权限显示。"
          />
        )}
        {scopedSales ? (
          <Alert
            type="info"
            showIcon
            message="受限销售账号不提供合同级详细盈亏"
            description="下方项目事实仍严格按当前销售本人范围加载。"
          />
        ) : boardLoadError ? (
          <Alert
            type="error"
            showIcon
            message="详细盈亏加载失败"
            description="项目成本事实仍可独立查看；可只重试详细盈亏。"
            action={(
              <Button size="small" danger onClick={() => void loadBoard()}>
                重试详细盈亏
              </Button>
            )}
          />
        ) : board.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={
            q || lifecycle !== "ongoing"
              ? "当前筛选暂无合同，请调整项目或期限状态"
              : "暂无数据（导入维保出库后自动生成）"
          } />
        ) : (
          <>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            {pagedBoard.map((b) => {
              // 决策字段被 RBAC 隐藏时保持中性；不受限响应则对缺失/null/未知值
              // 一律 fail-closed，禁止回退旧 status 伪造绿灯。
              const normalizedQuality = normalizeCostQuality(b.cost_quality);
              const normalizedDecision = effectiveBoardStatus(b);
              const costFactsMasked = boardDecisionRestricted && [
                b.cost_quality,
                b.actual_cost_inc, b.actual_cost_ex,
                b.estimated_cost_inc, b.estimated_cost_ex,
                b.actual_lines, b.estimated_lines,
                b.missing_cost_lines, b.known_cost_total,
              ].every((value) => value == null);
              const costIncomplete = !costFactsMasked
                && normalizedQuality === "incomplete";
              const decisionIncomplete = !boardDecisionRestricted && (
                normalizedQuality == null
                || normalizedDecision == null
                || normalizedDecision === "incomplete_cost"
              );
              const incomplete = costIncomplete || decisionIncomplete;
              const decisionStatus = boardDecisionRestricted
                ? undefined : incomplete ? "incomplete_cost" : normalizedDecision;
              const expenseUnavailable = !boardDecisionRestricted
                && decisionStatus === "expense_data_unavailable";
              const spentPct = !boardDecisionRestricted && !incomplete && !expenseUnavailable
                && b.budget != null && b.budget > 0 && b.spent != null
                && b.remaining != null && b.remaining_pct != null
                ? Math.round((b.spent / b.budget) * 100) : null;
              return (
                <div
                  key={b.contract ?? "(none)"}
                  data-testid={`maintenance-board-card-${b.contract || "unlinked"}`}
                  style={{
                  width: 370, maxWidth: "100%", boxSizing: "border-box",
                  borderRadius: 8, padding: "12px 14px",
                  border: "1px solid var(--mb-border)",
                  background: "var(--mb-surface)",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <b style={{ fontFamily: "monospace", fontSize: 13 }}>{b.contract || "（未关联合同）"}</b>
                    <Space size={6}>
                      <LifecycleTag status={b.lifecycle_status} />
                    </Space>
                  </div>
                  {!boardDecisionRestricted && (
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: maintenanceBasis === "both"
                          ? "repeat(2, minmax(0, 1fr))" : "1fr",
                        gap: 10,
                        marginTop: 10,
                      }}
                    >
                      {selectedProfitBases(maintenanceBasis).map((basis) => (
                        <div
                          key={basis}
                          style={{
                            minWidth: 0,
                            padding: "8px 9px",
                            borderRadius: 6,
                            background: "rgba(255,255,255,0.6)",
                          }}
                        >
                          <div style={{ fontWeight: 600, marginBottom: 3 }}>
                            {PROFIT_BASIS_LABEL[basis]}口径
                          </div>
                          <MarginFacts row={b} basis={basis} />
                        </div>
                      ))}
                    </div>
                  )}
                  {incomplete ? (
                    <Alert
                      type="info"
                      style={{ marginTop: 8 }}
                      message="成本证据不完整"
                      description="当前仅展示已知成本事实，不计算预算余额。"
                    />
                  ) : expenseUnavailable ? (
                    <Alert
                      type="info"
                      style={{ marginTop: 8 }}
                      message="费用证据未就绪"
                      description="当前只展示已知备件成本；无报销记录不等于费用为 0，不计算完整支出或预算余额。"
                    />
                  ) : !boardDecisionRestricted && (
                    <div style={{ marginTop: 8, fontSize: 12.5 }}>
                      合同额参考 {b.budget != null && b.budget > 0 ? money(b.budget) : "—"}
                      {" · "}已知支出兼容参考（混合原值） {money(b.spent)}
                      {" · "}剩余预算{" "}
                      <span style={{ fontWeight: 600 }}>
                        {money(b.remaining)}{b.remaining_pct != null ? `（${b.remaining_pct}%）` : ""}
                      </span>
                    </div>
                  )}
                  {spentPct != null && (
                    <div style={{ marginTop: 4 }}>
                      <Progress percent={Math.min(spentPct, 100)} size="small"
                                strokeColor="var(--mb-accent)" showInfo={false} />
                      <div style={{ fontSize: 11.5, color: "var(--mb-text-3)" }}>
                        已知支出占合同额参考 {spentPct}%
                      </div>
                    </div>
                  )}
                  <div style={{ marginTop: 6, fontSize: 12, color: "#6b665e" }}>
                    实际参考：<TaxMoney scope="maintenance"
                      inc={b.actual_cost_inc} ex={b.actual_cost_ex} />
                    <br />
                    估算参考：<TaxMoney scope="maintenance"
                      inc={b.estimated_cost_inc} ex={b.estimated_cost_ex} />
                    {" · "}
                    {b.missing_cost_lines == null ? "缺失 —" : `缺失 ${b.missing_cost_lines} 行`}
                    {" · "}报销费用{" "}
                    {b.expense_data_available === true
                      ? <TaxMoney scope="maintenance"
                          inc={b.expense_inc ?? null} ex={b.expense_ex ?? null} />
                      : "数据未就绪（无记录不等于0）"}
                    {(b.low_conf_pct ?? 0) >= 30 && (
                      <Tooltip title="低置信（追溯/销售参考）成本占比高，金额估算成分大，建议核对">
                        <Tag color="orange" style={{ marginLeft: 6 }}>估算成分高 {b.low_conf_pct}%</Tag>
                      </Tooltip>
                    )}
                  </div>
                  <div style={{ marginTop: 6, fontSize: 12 }}>
                    {b.projects.slice(0, 3).map((pj) => (
                      <div key={pj.project} style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                        <a onClick={() => openDetail(pj.project)}>{pj.project}</a>
                        <span style={{ color: "var(--mb-text-3)" }}>
                          {" · "}已知成本兼容参考（混合原值） {money(pj.spent_parts)}
                        </span>
                      </div>
                    ))}
                    {b.projects.length > 3 && (
                      <span style={{ color: "var(--mb-text-3)" }}>…等 {b.projects.length} 个项目</span>
                    )}
                  </div>
                </div>
              );
            })}
            </div>
            {board.length > BOARD_PAGE_SIZE && (
              <Pagination
                aria-label="详细盈亏合同分页"
                current={boardPage}
                pageSize={BOARD_PAGE_SIZE}
                total={board.length}
                showSizeChanger={false}
                onChange={setBoardPage}
                style={{ marginTop: 16, textAlign: "right" }}
              />
            )}
          </>
        )}
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={4}><Card size="small"><Statistic title="项目数" value={rows.length} /></Card></Col>
        {maintenanceBasis !== "ex" && (
          <>
            <Col xs={24} sm={12} lg={4}><Card size="small"><Statistic
              title="实际采购参考（含税）"
              {...costStat(totalActualInc)} /></Card></Col>
            <Col xs={24} sm={12} lg={4}><Card size="small"><Statistic
              title="估算参考（含税）"
              {...costStat(totalEstimatedInc)} /></Card></Col>
          </>
        )}
        {maintenanceBasis !== "inc" && (
          <>
            <Col xs={24} sm={12} lg={4}><Card size="small"><Statistic
              title="实际采购参考（未税）"
              {...costStat(totalActualEx)} /></Card></Col>
            <Col xs={24} sm={12} lg={4}><Card size="small"><Statistic
              title="估算参考（未税）"
              {...costStat(totalEstimatedEx)} /></Card></Col>
          </>
        )}
        <Col xs={24} sm={12} lg={4}><Card size="small"><Statistic
          title="缺失成本行"
          value={totalMissing == null ? "—" : totalMissing}
          valueStyle={{ color: totalMissing ? "var(--mb-danger)" : undefined }}
        /></Card></Col>
      </Row>

      <Card
        title="项目成本事实分层"
        extra={(
          <Space wrap>
            <Tag>{`项目事实截止 ${projectAsOf || "—"}`}</Tag>
            {isAdmin && (
              <Button type="primary" loading={recomputing} onClick={recompute}>
                重算成本
              </Button>
            )}
          </Space>
        )}
      >
        <div style={{ color: "var(--mb-text-3)", fontSize: 12.5, marginBottom: 10 }}>
          项目事实期限：进行中 {projectLifecycleCounts.ongoing}
          {" · "}已结束 {projectLifecycleCounts.ended}
          {" · "}缺失 {projectLifecycleCounts.missing}
        </div>
        {projectsLoadError && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message="项目成本事实加载失败"
            description="详细盈亏仍可独立查看；可只重试项目事实。"
            action={(
              <Button size="small" danger onClick={() => void loadProjects()}>
                重试项目事实
              </Button>
            )}
          />
        )}
        <Alert
          type="info" showIcon style={{ marginBottom: 12 }}
          message="含税与未税收入、成本、毛利独立展示；含估算会明确标识，成本、收入、税率或费用证据不完整时对应毛利保持为空，不以 0 补齐。"
        />
        <ResizableTable
          storageKey="maint-projects"
          rowKey="project"
          size="small"
          loading={projectsLoading}
          columns={projectCols}
          dataSource={rows}
          scroll={{ x: maintenanceBasis === "both" ? 2210 : 1890 }}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          locale={{ emptyText: (q || lifecycle !== "ongoing")
            ? "当前筛选无结果，请调整搜索或期限状态"
            : <Empty description="尚未导入维保出库数据，请到【数据导入】上传氚云维保订单 Excel" /> }}
        />
      </Card>

      <Drawer
        open={!!detailProject}
        width="min(1100px, 100vw)"
        onClose={closeDetail}
        title={detailProject ? `${detailProject} · 出库明细` : ""}
        extra={
          <Space>
            {monthOptions > 1 && (
              <DatePicker
                picker="month" placeholder="按月筛选" allowClear
                onChange={(d) => {
                  const m = d ? d.format("YYYY-MM") : undefined;
                  setLinesMonth(m);
                  if (detailProject) loadLines(detailProject, 1, m);
                }}
              />
            )}
          </Space>
        }
      >
        <ResizableTable
          storageKey="maint-lines"
          rowKey="id"
          size="small"
          loading={linesLoading}
          columns={lineCols}
          dataSource={lines}
          scroll={{ x: 1400 }}
          pagination={{
            current: linesPage, pageSize: 50, total: linesTotal,
            showSizeChanger: false, showTotal: (t) => `共 ${t} 行`,
            onChange: (p: number) => detailProject && loadLines(detailProject, p, linesMonth),
          }}
        />
      </Drawer>
    </Space>
  );
}
