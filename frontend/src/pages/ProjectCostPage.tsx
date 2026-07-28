import { useEffect, useRef, useState } from "react";
import {
  Card, DatePicker, Input, Button, Space, Tag, Tooltip, message, Statistic, Row, Col,
  Drawer, Progress, Alert, Empty, Segmented,
} from "antd";
import { InfoCircleOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../components/ResizableTable";
import PageHeader from "../components/PageHeader";
import dayjs from "dayjs";
import type { Dayjs } from "dayjs";
import api from "../api";
import { getSystemSettings } from "../api/systemSettings";
import type { MaintenanceProfitDefaultBasis } from "../api/systemSettings";
import { MAINTENANCE_PROFIT_BASIS_KEY } from "../sessionPreferences";
import { money, pct } from "../utils/format";

export { MAINTENANCE_PROFIT_BASIS_KEY } from "../sessionPreferences";

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
}

interface ProjectRow {
  project: string;
  lines: number;
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
type ExportDatePreset = "all" | "today" | "last7" | "last14" | "last21" | "last30" | "month" | "custom";

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
const NEUTRAL_META = { label: "", color: "#8c8c8c", bg: "rgba(0,0,0,0.03)" };
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

const PROFIT_BASIS_LABEL: Record<MaintenanceProfitDefaultBasis, string> = {
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
    label: "费用税务口径待确认",
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

function validProfitBasis(value: unknown): value is MaintenanceProfitDefaultBasis {
  return value === "inc" || value === "ex" || value === "both";
}

export function readMaintenanceProfitBasisOverride(): MaintenanceProfitDefaultBasis | null {
  try {
    const value = localStorage.getItem(MAINTENANCE_PROFIT_BASIS_KEY);
    return validProfitBasis(value) ? value : null;
  } catch {
    return null;
  }
}

function writeMaintenanceProfitBasisOverride(
  basis: MaintenanceProfitDefaultBasis | null,
) {
  try {
    if (basis == null) localStorage.removeItem(MAINTENANCE_PROFIT_BASIS_KEY);
    else localStorage.setItem(MAINTENANCE_PROFIT_BASIS_KEY, basis);
  } catch {
    // 隐私模式等场景仍更新当前页面状态；只是不跨刷新保存。
  }
}

function selectedProfitBases(
  basis: MaintenanceProfitDefaultBasis,
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
        <strong>合同级贡献毛利（费用口径待确认）</strong>{" "}
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
): BoardStatus {
  return BOARD_STATUSES.has(decisionStatus as BoardStatus)
    ? decisionStatus as BoardStatus
    : "incomplete_cost";
}

function normalizeCostQuality(value: string | null | undefined): CostQuality {
  if (value === "actual_only" || value === "contains_estimate") return value;
  return "incomplete";
}

function effectiveBoardStatus(row: BoardRow): BoardStatus {
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

async function readExportError(error: unknown): Promise<{
  status?: number;
  detail?: string;
}> {
  const response = (error as {
    response?: { status?: number; data?: unknown };
  })?.response;
  let detail: string | undefined;
  if (
    response?.status != null
    && [403, 422, 429].includes(response.status)
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
      if (typeof body.detail === "string") detail = body.detail;
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

export default function ProjectCostPage() {
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
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [profitBasisOverride, setProfitBasisOverride] = useState<
    MaintenanceProfitDefaultBasis | null
  >(readMaintenanceProfitBasisOverride);
  const [adminProfitBasis, setAdminProfitBasis] = useState<
    MaintenanceProfitDefaultBasis | null
  >(null);
  const [profitBasisFallback, setProfitBasisFallback] = useState(false);
  const [exportDatePreset, setExportDatePreset] = useState<ExportDatePreset>("all");
  const exportDatePresetRef = useRef<ExportDatePreset>("all");
  const [q, setQ] = useState("");
  const [lifecycle, setLifecycle] = useState<LifecycleFilter>("ongoing");
  const [lifecycleCounts, setLifecycleCounts] = useState<LifecycleCounts>(EMPTY_LIFECYCLE_COUNTS);
  const [asOf, setAsOf] = useState("");
  const [rows, setRows] = useState<ProjectRow[]>([]);
  const [projectCostRestricted, setProjectCostRestricted] = useState(false);
  const [board, setBoard] = useState<BoardRow[]>([]);
  const [boardDecisionRestricted, setBoardDecisionRestricted] = useState(false);
  const [boardFilter, setBoardFilter] = useState<string>("all");
  const [startDate, setStartDate] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportingWorkbooks, setExportingWorkbooks] = useState(false);
  const exportingWorkbooksRef = useRef(false);
  const [exportingProjects, setExportingProjects] = useState(false);
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
  // 页面两组聚合请求共享代次：快速切换期限/日期/搜索时，迟到响应不能覆盖最新筛选。
  const pageSeq = useRef(0);
  const effectiveProfitBasis = profitBasisOverride ?? adminProfitBasis ?? "both";

  useEffect(() => {
    let active = true;
    getSystemSettings()
      .then(({ data }) => {
        if (!active) return;
        if (validProfitBasis(data.maintenance_project_profit_default_basis)) {
          setAdminProfitBasis(data.maintenance_project_profit_default_basis);
          setProfitBasisFallback(false);
        } else {
          setAdminProfitBasis("both");
          setProfitBasisFallback(true);
        }
      })
      .catch(() => {
        if (!active) return;
        setAdminProfitBasis("both");
        setProfitBasisFallback(true);
      });
    return () => {
      active = false;
    };
  }, []);

  const chooseProfitBasis = (basis: MaintenanceProfitDefaultBasis) => {
    writeMaintenanceProfitBasisOverride(basis);
    setProfitBasisOverride(basis);
  };

  const restoreAdminProfitBasis = () => {
    writeMaintenanceProfitBasisOverride(null);
    setProfitBasisOverride(null);
  };

  const baseParams = () => ({
    q: q || undefined,
    date_from: range?.[0]?.format("YYYY-MM-DD"),
    date_to: range?.[1]?.format("YYYY-MM-DD"),
  });

  const lifecycleParams = () => ({ ...baseParams(), lifecycle });
  const boardParams = () => ({
    q: q || undefined,
    date_from: range?.[0]?.format("YYYY-MM-DD"),
    date_to: range?.[1]?.format("YYYY-MM-DD"),
    lifecycle,
  });

  const load = async () => {
    const seq = ++pageSeq.current;
    setLoading(true);
    setLoadError(false);
    // 筛选已经变化时不继续展示上一筛选的结果，避免用户把旧数据误认成新口径。
    setRows([]);
    setBoard([]);
    setLifecycleCounts(EMPTY_LIFECYCLE_COUNTS);
    setAsOf("");
    try {
      const [{ data }, bd] = await Promise.all([
        api.get("/maintenance/projects", { params: lifecycleParams() }),
        api.get("/maintenance/board", { params: boardParams() }),
      ]);
      if (seq !== pageSeq.current) return;
      setRows(data.rows);
      setProjectCostRestricted(!!data.ranking_restricted);
      setStartDate(data.start_date);
      setBoard(bd.data.rows);
      setAsOf(data.as_of || bd.data.as_of || "");
      setLifecycleCounts(data.lifecycle_counts || bd.data.lifecycle_counts || EMPTY_LIFECYCLE_COUNTS);
      const decisionRestricted = bd.data.decision_restricted === true
        || bd.data.profit_restricted === true
        || bd.data.ranking_restricted === true
        || data.ranking_restricted === true;
      setBoardDecisionRestricted(decisionRestricted);
      if (decisionRestricted) setBoardFilter("all");
    } catch {
      if (seq !== pageSeq.current) return;
      setRows([]);
      setProjectCostRestricted(false);
      setBoard([]);
      setLoadError(true);
    } finally {
      if (seq === pageSeq.current) setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, range, lifecycle]);

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
      message.success(
        `重算完成：${data.lines_in_scope} 行 · 专属采购 ${data.direct} · ±7天 ${data.window}` +
        ` · 当月均价 ${data.month_avg} · 追溯 ${data.trace_avg} · 销售参考 ${data.sales_ref}` +
        ` · 池采购 ${data.pool_purchase} · 池销售 ${data.pool_sales}` +
        ` · 本PN历史采购 ${data.purchase_history} · 本PN历史销售 ${data.sales_history}` +
        ` · 无成本 ${data.none}`);
      load();
    } catch {
      message.error("重算失败（需要管理员权限）");
    } finally {
      hide();
      setRecomputing(false);
    }
  };

  const download = (path: string, filename: string, extra?: Record<string, unknown>) =>
    api.get(path, { params: { ...baseParams(), ...extra }, responseType: "blob" })
      .then((res) => saveBlob(res.data, filename));

  const exportOrders = async () => {
    const requestedPreset = exportDatePreset;
    let exportRange = range;
    const initialParams = buildOrderExportParams(requestedPreset, exportRange);
    if (!initialParams) {
      message.warning(exportDatePreset === "custom"
        ? "请选择自定义起止日期"
        : "日期基准尚未加载，请稍后重试");
      return;
    }
    setExporting(true);
    try {
      if (requestedPreset !== "all" && requestedPreset !== "custom") {
        const { data } = await api.get("/maintenance/as-of");
        if (exportDatePresetRef.current !== requestedPreset) return;
        const latestAsOf = typeof data?.as_of === "string" ? data.as_of : "";
        const anchor = latestAsOf ? dayjs(latestAsOf) : null;
        if (!anchor?.isValid()) throw new Error("invalid as_of");
        exportRange = presetRange(requestedPreset, anchor);
        setAsOf(latestAsOf);
        setRange(exportRange);
      }
      const params = buildOrderExportParams(requestedPreset, exportRange);
      if (!params) {
        message.warning(exportDatePreset === "custom"
          ? "请选择自定义起止日期"
          : "日期基准尚未加载，请稍后重试");
        return;
      }
      const res = await api.get("/maintenance/orders/export", { params, responseType: "blob" });
      saveBlob(res.data, exportRange
        ? `maintenance_orders_${exportRange[0].format("YYYY-MM-DD")}_${exportRange[1].format("YYYY-MM-DD")}.xlsx`
        : "maintenance_orders_all.xlsx");
    } catch (error) {
      const { status, detail } = await readExportError(error);
      message.error(detail || (status === 403
        ? "无权限导出维保订单"
        : status === 422
          ? "导出日期参数无效"
          : "导出失败，请稍后重试"));
    } finally {
      setExporting(false);
    }
  };

  const exportWorkbooks = async () => {
    if (exportingWorkbooksRef.current) return;
    const requestedPreset = exportDatePreset;
    let exportRange = range;
    const initialParams = buildOrderExportParams(requestedPreset, exportRange);
    if (!initialParams) {
      message.warning(requestedPreset === "custom"
        ? "请选择自定义起止日期"
        : "日期基准尚未加载，请稍后重试");
      return;
    }
    exportingWorkbooksRef.current = true;
    setExportingWorkbooks(true);
    try {
      if (requestedPreset !== "all" && requestedPreset !== "custom") {
        const { data } = await api.get("/maintenance/as-of");
        if (exportDatePresetRef.current !== requestedPreset) return;
        const latestAsOf = typeof data?.as_of === "string" ? data.as_of : "";
        const anchor = latestAsOf ? dayjs(latestAsOf) : null;
        if (!anchor?.isValid()) throw new Error("invalid as_of");
        exportRange = presetRange(requestedPreset, anchor);
        setAsOf(latestAsOf);
        setRange(exportRange);
      }
      const params = buildOrderExportParams(requestedPreset, exportRange);
      if (!params) {
        message.warning(requestedPreset === "custom"
          ? "请选择自定义起止日期"
          : "日期基准尚未加载，请稍后重试");
        return;
      }
      const res = await api.get("/maintenance/export-workbooks", { params, responseType: "blob" });
      saveBlob(res.data, exportRange
        ? `maintenance_project_workbooks_${exportRange[0].format("YYYY-MM-DD")}_${exportRange[1].format("YYYY-MM-DD")}.zip`
        : "maintenance_project_workbooks_all.zip");
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
      exportingWorkbooksRef.current = false;
      setExportingWorkbooks(false);
    }
  };

  const exportProjectsCsv = async () => {
    if (exportingProjects) return;
    setExportingProjects(true);
    try {
      await download("/maintenance/export", "maintenance_projects.csv", { lifecycle });
    } catch {
      message.error("项目 CSV 导出失败，请稍后重试或检查权限");
    } finally {
      setExportingProjects(false);
    }
  };

  const exportLines = () => {
    if (!detailProject) return;
    download("/maintenance/lines/export", `项目备件明细.csv`,
      { project: detailProject, month: linesMonth })
      .catch(() => message.error("明细导出失败，请稍后重试"));
  };

  const projectCols: ColumnsType<ProjectRow> = [
    { title: "项目", dataIndex: "project", width: 300, fixed: "left", ellipsis: true },
    { title: "期限状态", dataIndex: "lifecycle_status", width: 100,
      render: (v: LifecycleStatus) => <LifecycleTag status={v} /> },
    { title: "维保终止日期", dataIndex: "maint_end", width: 120,
      render: (v: string | null) => v || <span style={{ color: "var(--mb-warning)" }}>未填写</span> },
    { title: "出库行", dataIndex: "lines", width: 80, align: "right" },
    { title: "数量", dataIndex: "qty", width: 80, align: "right" },
    { title: "实际参考(含税)", dataIndex: "actual_cost_inc", width: 130, align: "right", render: money },
    { title: "实际参考(不含税)", dataIndex: "actual_cost_ex", width: 140, align: "right", render: money },
    { title: "估算参考(含税)", dataIndex: "estimated_cost_inc", width: 130, align: "right", render: money },
    { title: "估算参考(不含税)", dataIndex: "estimated_cost_ex", width: 140, align: "right", render: money },
    { title: "成本完整性", dataIndex: "cost_quality", width: 160,
      render: (rawQuality: string | null, row) => {
        // 成本字段确由 RBAC 隐藏时保持中性；不受限响应的 null/未知值仍 fail-closed。
        if (rawQuality == null && projectCostRestricted) return "—";
        const quality = normalizeCostQuality(rawQuality);
        if (quality === "incomplete") {
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
    { title: "单价", dataIndex: "unit_cost", width: 100, align: "right", render: money },
    { title: "金额", dataIndex: "cost_amount", width: 110, align: "right", render: money },
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

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="项目成本"
        subtitle={`维保备件成本按实际采购参考、估算参考、成本缺失分层；合同毛利同时保留含税与未税事实，证据不完整时保持空值${startDate ? ` · 起算日 ${startDate}` : ""}`}
      />
      <Card>
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <div>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 7 }}>维保期限</div>
            <div style={{ maxWidth: "100%", overflowX: "auto", paddingBottom: 2 }}>
              <Segmented
                aria-label="维保期限筛选"
                value={lifecycle}
                onChange={(value) => setLifecycle(value as LifecycleFilter)}
                options={[
                  { label: `进行中 ${lifecycleCounts.ongoing}`, value: "ongoing" },
                  { label: `已结束 ${lifecycleCounts.ended}`, value: "ended" },
                  { label: <span style={{ color: lifecycleCounts.missing ? "#d46b08" : undefined }}>
                    期限缺失 {lifecycleCounts.missing}
                  </span>, value: "missing" },
                  { label: `全部 ${lifecycleCounts.ongoing + lifecycleCounts.ended + lifecycleCounts.missing}`,
                    value: "all" },
                ]}
              />
            </div>
          </div>
          <div>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 7 }}>
              合同级毛利展示口径
            </div>
            <Space wrap>
              <Segmented
                aria-label="合同级毛利展示口径"
                value={effectiveProfitBasis}
                onChange={(value) => {
                  chooseProfitBasis(value as MaintenanceProfitDefaultBasis);
                }}
                options={[
                  { label: "含税毛利", value: "inc" },
                  { label: "未税毛利", value: "ex" },
                  { label: "同时显示", value: "both" },
                ]}
              />
              <Button
                size="small"
                disabled={profitBasisOverride == null}
                onClick={restoreAdminProfitBasis}
              >
                恢复管理员默认
              </Button>
              <span style={{ color: "var(--mb-text-3)", fontSize: 12.5 }}>
                {adminProfitBasis == null
                  ? "正在读取管理员默认…"
                  : `管理员默认：${PROFIT_BASIS_LABEL[adminProfitBasis]}`}
                {profitBasisOverride != null ? " · 当前为个人临时选择" : " · 当前跟随默认"}
              </span>
            </Space>
            {profitBasisFallback && (
              <Alert
                type="warning"
                showIcon
                style={{ marginTop: 8 }}
                message={profitBasisOverride == null
                  ? "管理员默认读取失败，本次安全回退为含税与未税同时显示。"
                  : "管理员默认读取失败，当前继续使用已保存的个人临时选择。"}
              />
            )}
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 24, width: "100%", minWidth: 0 }}>
            <div style={{ width: "100%", minWidth: 0, maxWidth: "100%", overflowX: "auto", paddingBottom: 2 }}>
              <Segmented
                aria-label="维保订单导出日期"
                value={exportDatePreset}
                onChange={(value) => {
                  const preset = value as ExportDatePreset;
                  exportDatePresetRef.current = preset;
                  setExportDatePreset(preset);
                  if (preset === "all") setRange(null);
                  if (preset === "custom") setRange(null);
                   const anchor = asOf ? dayjs(asOf) : null;
                   if (anchor && preset !== "all" && preset !== "custom") {
                     setRange(presetRange(preset, anchor));
                   }
                }}
                options={[
                  { label: "全部", value: "all" },
                  { label: "今天", value: "today", disabled: !asOf },
                  { label: "近7天", value: "last7", disabled: !asOf },
                  { label: "近14天", value: "last14", disabled: !asOf },
                  { label: "近21天", value: "last21", disabled: !asOf },
                  { label: "近30天", value: "last30", disabled: !asOf },
                  { label: "本月", value: "month", disabled: !asOf },
                  { label: "自定义", value: "custom" },
                ]}
              />
            </div>
            <DatePicker.RangePicker
              value={range}
              onChange={(v) => {
                exportDatePresetRef.current = "custom";
                setExportDatePreset("custom");
                setRange(v as [Dayjs, Dayjs] | null);
              }}
            />
            <Input.Search
              placeholder="搜索项目名"
              allowClear
              style={{ width: "min(260px, 100%)" }}
              onChange={(event) => {
                if (!event.target.value) setQ("");
              }}
              onSearch={(v) => setQ(v.trim())}
            />
            {isAdmin && (
              <Button type="primary" loading={recomputing} onClick={recompute}>重算成本</Button>
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
            <Button loading={exporting} disabled={exporting} onClick={exportOrders}>
              导出订单汇总 Excel
            </Button>
            <Button
              loading={exportingProjects}
              onClick={exportProjectsCsv}
              disabled={!rows.length || exportingProjects}
            >导出当前项目统计 CSV</Button>
            <div
              aria-label="批量导出说明"
              style={{ width: "100%", color: "var(--mb-text-2)", fontSize: 12.5, lineHeight: 1.6 }}
            >
              <div>批量项目工作簿 ZIP：时间范围只决定纳入哪些合同，每本仍包含该合同完整数据。</div>
              <div>订单汇总 Excel：汇总范围内的订单及明细；两种批量导出不受项目搜索或维保期限筛选影响。</div>
            </div>
          </div>
          <Alert
            type={lifecycleCounts.missing ? "warning" : "info"}
            showIcon
            message={`日期范围筛选出库日期；批量导出按维保单制单日期；维保期限状态按 ${asOf || "后端请求当天"} 判断。`}
            description={`批量工作簿只按范围决定合同是否入包，每本保持完整；终止日当天仍算进行中；未填写终止日期的项目归入“期限缺失”。当前有 ${lifecycleCounts.missing} 个期限缺失项目。`}
          />
          {loadError && (
            <Alert
              type="error"
              showIcon
              message="项目成本加载失败，旧结果已清空。"
              description="请检查网络或账号权限后重试；错误期间不会继续展示上一筛选的数据。"
              action={<Button size="small" danger onClick={load}>重试</Button>}
            />
          )}
        </Space>
      </Card>

      <Card
        title={<Space>合同预算与双口径毛利
          <Tooltip title="按合同聚合收入、备件成本与费用。含税、未税独立计算；任一口径证据不完整时，该口径毛利保持为空并显示原因。">
            <InfoCircleOutlined style={{ color: "var(--mb-text-3)" }} />
          </Tooltip></Space>}
      >
        {!boardDecisionRestricted && (
          <div style={{ maxWidth: "100%", overflowX: "auto", marginBottom: 12, paddingBottom: 2 }}>
            <Segmented
              aria-label="预算消耗参考状态筛选"
              value={boardFilter}
              onChange={(v) => setBoardFilter(v as string)}
              options={[
                { label: `全部 ${board.length}`, value: "all" },
                { label: `待补成本 ${board.filter((b) =>
                  effectiveBoardStatus(b) === "incomplete_cost").length}`,
                  value: "incomplete_cost" },
                { label: `待补费用数据 ${board.filter((b) =>
                  effectiveBoardStatus(b) === "expense_data_unavailable").length}`,
                  value: "expense_data_unavailable" },
                { label: `🔴 ${board.filter((b) =>
                  effectiveBoardStatus(b) === "red").length}`, value: "red" },
                { label: `🟡 ${board.filter((b) =>
                  effectiveBoardStatus(b) === "yellow").length}`, value: "yellow" },
                { label: `🟢 ${board.filter((b) =>
                  effectiveBoardStatus(b) === "green").length}`, value: "green" },
                { label: `无预算 ${board.filter((b) =>
                  effectiveBoardStatus(b) === "no_budget").length}`,
                  value: "no_budget" },
              ]}
            />
          </div>
        )}
        {boardDecisionRestricted && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="当前账号不展示预算消耗决策分类与状态筛选"
            description="合同按最近出库日期排列；实际、估算、缺失等成本事实仍按账号的数据权限显示。"
          />
        )}
        {board.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={
            q || range || lifecycle !== "ongoing"
              ? "当前筛选暂无合同，请调整项目、日期或期限状态"
              : "暂无数据（导入维保出库后自动生成）"
          } />
        ) : (
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            {(boardDecisionRestricted || boardFilter === "all"
              ? board : board.filter((b) =>
                effectiveBoardStatus(b) === boardFilter)).map((b) => {
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
                b.cost_quality == null || normalizedDecision === "incomplete_cost"
              );
              const incomplete = costIncomplete || decisionIncomplete;
              const decisionStatus = boardDecisionRestricted
                ? undefined : incomplete ? "incomplete_cost" : normalizedDecision;
              const expenseUnavailable = !boardDecisionRestricted
                && decisionStatus === "expense_data_unavailable";
              const meta = decisionStatus ? STATUS_META[decisionStatus] : NEUTRAL_META;
              const hasBudgetDecision = decisionStatus === "red"
                || decisionStatus === "yellow"
                || decisionStatus === "green";
              const spentPct = !boardDecisionRestricted && !incomplete && hasBudgetDecision
                && b.budget != null && b.budget > 0 && b.spent != null
                && b.remaining != null && b.remaining_pct != null
                ? Math.round((b.spent / b.budget) * 100) : null;
              let timePct: number | null = null;
              if (!incomplete && !expenseUnavailable && b.maint_start && b.maint_end) {
                const s0 = new Date(b.maint_start).getTime();
                const e0 = new Date(b.maint_end).getTime();
                if (e0 > s0) timePct = Math.min(Math.max(
                  Math.round(((Date.now() - s0) / (e0 - s0)) * 100), 0), 100);
              }
              return (
                <div
                  key={b.contract ?? "(none)"}
                  data-testid={`maintenance-board-card-${b.contract || "unlinked"}`}
                  style={{
                  width: 370, maxWidth: "100%", boxSizing: "border-box",
                  borderRadius: 8, padding: "12px 14px",
                  border: "1px solid " + meta.color + "44",
                  borderLeft: "4px solid " + meta.color, background: meta.bg,
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <b style={{ fontFamily: "monospace", fontSize: 13 }}>{b.contract || "（未关联合同）"}</b>
                    <Space size={6}>
                      {decisionStatus && (
                        <Tag color={decisionStatus === "red" ? "red" : decisionStatus === "yellow" ? "gold"
                          : decisionStatus === "green" ? "green"
                            : decisionStatus === "incomplete_cost" ? "orange" : "default"}>
                          {meta.label}
                        </Tag>
                      )}
                      <LifecycleTag status={b.lifecycle_status} />
                      {b.contract && canExportProjectWorkbooks && (
                        <a style={{ fontSize: 12 }} onClick={() =>
                          download("/maintenance/export-workbook", `项目工作簿_${b.contract}.xlsx`,
                                   { contract: b.contract })
                            .catch(() => message.error("工作簿导出失败，请稍后重试或检查权限"))
                        }>单本工作簿</a>
                      )}
                    </Space>
                  </div>
                  {!boardDecisionRestricted && (
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: effectiveProfitBasis === "both"
                          ? "repeat(2, minmax(0, 1fr))" : "1fr",
                        gap: 10,
                        marginTop: 10,
                      }}
                    >
                      {selectedProfitBases(effectiveProfitBasis).map((basis) => (
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
                      type="warning"
                      showIcon
                      style={{ marginTop: 8 }}
                      message="成本不完整，需补数据"
                      description="当前仅展示已知成本事实，不计算预算余额或红黄绿参考。"
                    />
                  ) : expenseUnavailable ? (
                    <Alert
                      type="warning"
                      showIcon
                      style={{ marginTop: 8 }}
                      message="费用数据未就绪"
                      description="当前只展示已知备件成本；无报销记录不等于费用为 0，不计算完整支出、预算余额或红黄绿参考。"
                    />
                  ) : !boardDecisionRestricted && (
                    <div style={{ marginTop: 8, fontSize: 12.5 }}>
                      合同额参考 {money(b.budget)} · 已知支出兼容参考（混合原值） {money(b.spent)}
                      {" · "}剩余预算{" "}
                      <span style={{ color: meta.color, fontWeight: 600 }}>
                        {money(b.remaining)}{b.remaining_pct != null ? `（${b.remaining_pct}%）` : ""}
                      </span>
                    </div>
                  )}
                  {spentPct != null && (
                    <div style={{ marginTop: 4 }}>
                      <Progress percent={Math.min(spentPct, 100)} size="small"
                                strokeColor={meta.color} showInfo={false} />
                      <div style={{ fontSize: 11.5, color: "var(--mb-text-3)" }}>
                        预算消耗参考 {spentPct}%{spentPct > 100 ? "（超过合同额参考）" : ""}
                        {timePct != null ? ` / 时间进度 ${timePct}%` : ""}
                        {timePct != null && spentPct > timePct + 15 ? " · 支出进度快于时间 ⚠" : ""}
                      </div>
                    </div>
                  )}
                  <div style={{ marginTop: 6, fontSize: 12, color: "#6b665e" }}>
                    实际参考：含税 {money(b.actual_cost_inc)} / 不含税 {money(b.actual_cost_ex)}
                    <br />
                    估算参考：含税 {money(b.estimated_cost_inc)} / 不含税 {money(b.estimated_cost_ex)}
                    {" · "}
                    {b.missing_cost_lines == null ? "缺失 —" : `缺失 ${b.missing_cost_lines} 行`}
                    {" · "}报销费用{" "}
                    {b.expense_data_available === true
                      ? money(b.spent_expense)
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
        )}
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={4}><Card size="small"><Statistic title="项目数" value={rows.length} /></Card></Col>
        <Col xs={24} sm={12} lg={4}><Card size="small"><Statistic
          title="实际采购参考（含税）"
          {...costStat(totalActualInc)} /></Card></Col>
        <Col xs={24} sm={12} lg={4}><Card size="small"><Statistic
          title="实际采购参考（不含税）"
          {...costStat(totalActualEx)} /></Card></Col>
        <Col xs={24} sm={12} lg={4}><Card size="small"><Statistic
          title="估算参考（含税）"
          {...costStat(totalEstimatedInc)} /></Card></Col>
        <Col xs={24} sm={12} lg={4}><Card size="small"><Statistic
          title="估算参考（不含税）"
          {...costStat(totalEstimatedEx)} /></Card></Col>
        <Col xs={24} sm={12} lg={4}><Card size="small"><Statistic
          title="缺失成本行"
          value={totalMissing == null ? "—" : totalMissing}
          valueStyle={{ color: totalMissing ? "var(--mb-danger)" : undefined }}
        /></Card></Col>
      </Row>

      <Card title="项目成本事实分层">
        <Alert
          type="info" showIcon style={{ marginBottom: 12 }}
          message="含税与未税收入、成本、毛利独立展示；含估算会明确标识，成本、收入、税率或费用证据不完整时对应毛利保持为空，不以 0 补齐。"
        />
        <ResizableTable
          storageKey="maint-projects"
          rowKey="project"
          size="small"
          loading={loading}
          columns={projectCols}
          dataSource={rows}
          scroll={{ x: 1960 }}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          locale={{ emptyText: (q || range || lifecycle !== "ongoing")
            ? "当前筛选无结果，请调整搜索、日期或期限状态"
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
            <Button size="small" onClick={exportLines} disabled={!linesTotal}>导出明细 CSV</Button>
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
