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
import { money } from "../utils/format";

interface ProjectRow {
  project: string;
  lines: number;
  qty: number | null;
  cost_inc: number | null;
  cost_ex: number | null;
  cost_total: number | null;
  coverage_pct: number | null;
  by_source: Record<string, number>;
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
  cost_source: string | null; cost_tax_basis: string | null;
  price_month: string | null; trace_months: number | null;
  linked_purchase_order_no: string | null; anomaly_flags: string[];
  price_distance_days: number | null; confidence: string | null;
}

type BoardStatus = "red" | "yellow" | "green" | "no_budget";
type LifecycleStatus = "ongoing" | "ended" | "missing";
type LifecycleFilter = LifecycleStatus | "all";
type LifecycleCounts = Record<LifecycleStatus, number>;
type ExportDatePreset = "all" | "today" | "last7" | "last14" | "last21" | "last30" | "month" | "custom";

interface BoardRow {
  contract: string | null;
  status?: BoardStatus;
  projects: { project: string; lines: number; spent_parts: number | null }[];
  lines: number; coverage_pct: number | null;
  spent_parts: number | null; spent_expense: number | null; spent: number | null;
  budget: number | null; remaining: number | null; remaining_pct: number | null;
  low_conf_pct: number | null;
  maint_start: string | null; maint_end: string | null;
  lifecycle_status: LifecycleStatus;
  first_out: string | null; last_out: string | null;
}

const STATUS_META: Record<BoardStatus, { label: string; color: string; bg: string }> = {
  red: { label: "亏损/超支", color: "#c0524a", bg: "rgba(192,82,74,0.08)" },
  yellow: { label: "预警 · 剩余≤20%", color: "#b8860b", bg: "rgba(212,160,23,0.10)" },
  green: { label: "健康", color: "#3f7a45", bg: "rgba(63,122,69,0.07)" },
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

// 成本来源五态（口径见开发方案 §4.2）；trace_avg 必须带追溯月数标注（客户要求）
const SOURCE_META: Record<string, { label: string; color: string }> = {
  direct: { label: "实际·专属采购", color: "green" },
  window: { label: "实际·±7天最近价", color: "cyan" },
  month_avg: { label: "实际·当月均价", color: "blue" },
  trace_avg: { label: "预估·追溯均价", color: "orange" },
  sales_ref: { label: "没有采购有销售", color: "purple" },
  none: { label: "无成本", color: "red" },
};
const SOURCE_ORDER = ["direct", "window", "month_avg", "trace_avg", "sales_ref", "none"];
const COVERAGE_WARN_PCT = 80;   // 覆盖率预警线（经验值，非验收线；<此值标红提示核对无成本行）

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
  const isAdmin = localStorage.getItem("role") === "admin";
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [exportDatePreset, setExportDatePreset] = useState<ExportDatePreset>("all");
  const exportDatePresetRef = useRef<ExportDatePreset>("all");
  const [q, setQ] = useState("");
  const [lifecycle, setLifecycle] = useState<LifecycleFilter>("ongoing");
  const [lifecycleCounts, setLifecycleCounts] = useState<LifecycleCounts>(EMPTY_LIFECYCLE_COUNTS);
  const [asOf, setAsOf] = useState("");
  const [rows, setRows] = useState<ProjectRow[]>([]);
  const [board, setBoard] = useState<BoardRow[]>([]);
  const [boardProfitRestricted, setBoardProfitRestricted] = useState(false);
  const [boardFilter, setBoardFilter] = useState<string>("all");
  const [startDate, setStartDate] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [exporting, setExporting] = useState(false);
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
      setStartDate(data.start_date);
      setBoard(bd.data.rows);
      setAsOf(data.as_of || bd.data.as_of || "");
      setLifecycleCounts(data.lifecycle_counts || bd.data.lifecycle_counts || EMPTY_LIFECYCLE_COUNTS);
      setBoardProfitRestricted(!!bd.data.profit_restricted);
      if (bd.data.profit_restricted) setBoardFilter("all");
    } catch {
      if (seq !== pageSeq.current) return;
      setRows([]);
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
      const response = (error as {
        response?: { status?: number; data?: unknown };
      })?.response;
      let detail: string | undefined;
      if ((response?.status === 403 || response?.status === 422) && response.data instanceof Blob) {
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
          // Non-JSON error bodies fall through to a safe status-specific message.
        }
      }
      message.error(detail || (response?.status === 403
        ? "无权限导出维保订单"
        : response?.status === 422
          ? "导出日期参数无效"
          : "导出失败，请稍后重试"));
    } finally {
      setExporting(false);
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
    { title: "备件成本(含税)", dataIndex: "cost_inc", width: 130, align: "right", render: money,
      sorter: (a, b) => (a.cost_inc ?? 0) - (b.cost_inc ?? 0) },
    { title: "备件成本(不含税)", dataIndex: "cost_ex", width: 130, align: "right", render: money,
      defaultSortOrder: "descend",
      sorter: (a, b) => (a.cost_ex ?? 0) - (b.cost_ex ?? 0) },
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
      render: (bs: Record<string, number>) => (
        <Space size={2} wrap>
          {SOURCE_ORDER.map((k) =>
            bs?.[k] ? <Tooltip key={k} title={SOURCE_META[k].label}>
              <Tag color={SOURCE_META[k].color}>{bs[k]}</Tag></Tooltip> : null)}
        </Space>
      ) },
    { title: "合同额(参考)", dataIndex: "contract_amount", width: 150, align: "right",
      render: (v: number | null, r) => (
        <span>
          {money(v)}
          {r.contract_shared && (
            <Tooltip title="该合同被多个项目共同引用，金额跨项目重复，仅作参考（本期不计项目毛利）">
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
    { title: "成本来源", width: 180,
      render: (_, r) => <SourceTag source={r.cost_source} trace={r.trace_months}
                                   distance={r.price_distance_days} /> },
    { title: "置信度", dataIndex: "confidence", width: 76,
      render: (v: string | null) => v
        ? <Tag color={CONF_META[v]?.color}>{CONF_META[v]?.label || v}</Tag>
        : <span style={{ color: "var(--mb-text-3)" }}>-</span> },
    { title: "口径", dataIndex: "cost_tax_basis", width: 70,
      render: (v: string | null) => v ? <Tag>{v === "inc" ? "含税" : "不含税"}</Tag> : "-" },
    { title: "取价月", dataIndex: "price_month", width: 90 },
    { title: "关联采购单", dataIndex: "linked_purchase_order_no", width: 160, ellipsis: true,
      render: (v: string | null) => v || <span style={{ color: "var(--mb-text-3)" }}>-</span> },
  ];

  // 成本被权限脱敏时（page_maintenance 开但无采购成本权限），合计显示为隐藏而非误导的 ¥0
  const costsMasked = rows.length > 0 && rows.every((r) => r.cost_inc == null && r.cost_ex == null);
  const totalInc = rows.reduce((s, r) => s + (r.cost_inc ?? 0), 0);
  const totalEx = rows.reduce((s, r) => s + (r.cost_ex ?? 0), 0);
  const totalNone = rows.reduce((s, r) => s + (r.by_source?.none ?? 0), 0);
  const costStat = (v: number) => costsMasked
    ? { value: "—" as const }
    : { value: v, precision: 2, prefix: "¥" };
  // 抽屉月份下拉：由当前项目行的 months 数不便直接取月份列表，简单用近 24 个月占位由后端过滤
  const monthOptions = detailProject
    ? (rows.find((r) => r.project === detailProject)?.months || 0)
    : 0;

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="项目成本"
        subtitle={`维保项目备件成本自动核算：已采购按真实采购价（专属采购/当月均价），未采购按追溯均价（≤3月，标注）或销售参考价，逐条标注来源与含税口径${startDate ? ` · 起算日 ${startDate}` : ""}`}
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
          <Space wrap size="large" style={{ width: "100%", minWidth: 0 }}>
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
            <Button type="primary" loading={exporting} disabled={exporting} onClick={exportOrders}>
              导出维保订单 Excel
            </Button>
            <Button
              loading={exportingProjects}
              onClick={exportProjectsCsv}
              disabled={!rows.length || exportingProjects}
            >导出项目 CSV</Button>
          </Space>
          <Alert
            type={lifecycleCounts.missing ? "warning" : "info"}
            showIcon
            message={`日期范围筛选出库日期；维保期限状态按 ${asOf || "后端请求当天"} 判断。`}
            description={`终止日当天仍算进行中；未填写终止日期的项目归入“期限缺失”。当前有 ${lifecycleCounts.missing} 个期限缺失项目。`}
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
        title={<Space>项目盈亏看板
          <Tooltip title="按合同（销售订单 XSDD）聚合：预算=合同金额（含税参考）；已花=备件成本(混合口径参考)+生效报销费用。共用合同自动合并为一张卡。剩余≤20% 黄灯预警、超支红灯。">
            <InfoCircleOutlined style={{ color: "var(--mb-text-3)" }} />
          </Tooltip></Space>}
      >
        {!boardProfitRestricted && (
          <div style={{ maxWidth: "100%", overflowX: "auto", marginBottom: 12, paddingBottom: 2 }}>
            <Segmented
              aria-label="盈亏状态筛选"
              value={boardFilter}
              onChange={(v) => setBoardFilter(v as string)}
              options={[
                { label: `全部 ${board.length}`, value: "all" },
                { label: `🔴 ${board.filter((b) => b.status === "red").length}`, value: "red" },
                { label: `🟡 ${board.filter((b) => b.status === "yellow").length}`, value: "yellow" },
                { label: `🟢 ${board.filter((b) => b.status === "green").length}`, value: "green" },
                { label: `无预算 ${board.filter((b) => b.status === "no_budget").length}`, value: "no_budget" },
              ]}
            />
          </div>
        )}
        {boardProfitRestricted && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="当前账号无利润查看权限，不展示红黄绿盈亏分类与状态筛选"
            description="合同按最近出库日期排列；成本字段仍按账号的数据权限单独显示或隐藏。"
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
            {(boardProfitRestricted || boardFilter === "all"
              ? board : board.filter((b) => b.status === boardFilter)).map((b) => {
              const meta = b.status ? STATUS_META[b.status] : NEUTRAL_META;
              const spentPct = !boardProfitRestricted && b.budget && b.spent != null
                ? Math.round((b.spent / b.budget) * 100) : null;
              let timePct: number | null = null;
              if (b.maint_start && b.maint_end) {
                const s0 = new Date(b.maint_start).getTime();
                const e0 = new Date(b.maint_end).getTime();
                if (e0 > s0) timePct = Math.min(Math.max(
                  Math.round(((Date.now() - s0) / (e0 - s0)) * 100), 0), 100);
              }
              return (
                <div key={b.contract ?? "(none)"} style={{
                  width: 370, maxWidth: "100%", boxSizing: "border-box",
                  borderRadius: 8, padding: "12px 14px",
                  border: "1px solid " + meta.color + "44",
                  borderLeft: "4px solid " + meta.color, background: meta.bg,
                }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <b style={{ fontFamily: "monospace", fontSize: 13 }}>{b.contract || "（未关联合同）"}</b>
                    <Space size={6}>
                      {b.status && (
                        <Tag color={b.status === "red" ? "red" : b.status === "yellow" ? "gold"
                          : b.status === "green" ? "green" : "default"}>{meta.label}</Tag>
                      )}
                      <LifecycleTag status={b.lifecycle_status} />
                      {b.contract && (
                        <a style={{ fontSize: 12 }} onClick={() =>
                          download("/maintenance/export-workbook", `项目工作簿_${b.contract}.xlsx`,
                                   { contract: b.contract })
                            .catch(() => message.error("工作簿导出失败，请稍后重试或检查权限"))
                        }>工作簿</a>
                      )}
                    </Space>
                  </div>
                  <div style={{ marginTop: 8, fontSize: 12.5 }}>
                    预算 {money(b.budget)} · 已花 {money(b.spent)} · 剩余{" "}
                    <span style={{ color: meta.color, fontWeight: 600 }}>
                      {money(b.remaining)}{b.remaining_pct != null ? `（${b.remaining_pct}%）` : ""}
                    </span>
                  </div>
                  {spentPct != null && (
                    <div style={{ marginTop: 4 }}>
                      <Progress percent={Math.min(spentPct, 100)} size="small"
                                strokeColor={meta.color} showInfo={false} />
                      <div style={{ fontSize: 11.5, color: "var(--mb-text-3)" }}>
                        预算消耗 {spentPct}%{spentPct > 100 ? "（超支）" : ""}
                        {timePct != null ? ` / 时间进度 ${timePct}%` : ""}
                        {timePct != null && spentPct > timePct + 15 ? " · 花钱快于时间 ⚠" : ""}
                      </div>
                    </div>
                  )}
                  <div style={{ marginTop: 6, fontSize: 12, color: "#6b665e" }}>
                    备件 {money(b.spent_parts)} + 费用 {money(b.spent_expense)}
                    {b.coverage_pct != null ? ` · 覆盖率 ${b.coverage_pct}%` : ""}
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
                        <span style={{ color: "var(--mb-text-3)" }}> · {money(pj.spent_parts)}</span>
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
        <Col xs={24} sm={12} lg={6}><Card size="small"><Statistic title="项目数" value={rows.length} /></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card size="small"><Statistic
          title={<Space size={4}>备件成本 · 含税小计
            <Tooltip title="含税/不含税为分列口径，请勿直接相加对账"><InfoCircleOutlined /></Tooltip></Space>}
          {...costStat(totalInc)} /></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card size="small"><Statistic
          title={<Space size={4}>备件成本 · 不含税小计
            <Tooltip title="含税/不含税为分列口径，请勿直接相加对账"><InfoCircleOutlined /></Tooltip></Space>}
          {...costStat(totalEx)} /></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card size="small"><Statistic title="无成本行(待处理)" value={totalNone}
          valueStyle={{ color: totalNone ? "var(--mb-danger)" : undefined }} /></Card></Col>
      </Row>

      <Card title="项目备件成本">
        <Alert
          type="info" showIcon style={{ marginBottom: 12 }}
          message="含税/不含税小计按采购原值口径分列（客户确认口径），请勿直接相加对账；「合同额」为关联销售订单金额，仅作参考，本期不计项目毛利。"
        />
        <ResizableTable
          storageKey="maint-projects"
          rowKey="project"
          size="small"
          loading={loading}
          columns={projectCols}
          dataSource={rows}
          scroll={{ x: 1520 }}
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
