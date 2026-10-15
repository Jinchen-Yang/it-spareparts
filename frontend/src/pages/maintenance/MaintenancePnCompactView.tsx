import { Alert, Button, Select, Skeleton, Space } from "antd";
import { Link, useSearchParams } from "react-router-dom";
import type { ExplorerRow } from "../../api/maintenanceAnalytics";
import { BOARD_BUSINESS_TYPE_LABELS } from "../../api/maintenanceBossBoard";
import MaintenancePnCompactRanking, { type PnCompactRankingItem } from "./MaintenancePnCompactRanking";
import { readPermissionMap } from "../../nav";
import { useVisibleMaintenanceRefresh } from "../../utils/maintenanceRefresh";
import { formatExplorerValue as fmt, pnCompactParamsFromSearch, useMaintenanceExplorer } from "./analyticsExplorerModel";
import "./maintenancePnCompact.css";

const EMPTY_LABELS: Record<string, string> = {};
const DEMAND_LABELS: Record<string, string> = { repair: "报修供货", stock: "补库供货" };
const COST_LABELS: Record<string, string> = { linked: "系统关联", estimated: "估算", manual: "人工回填", missing: "缺失" };
const csvLabels = (value: string, labels: Record<string, string>) => value.split(",").map(code => labels[code] || code).join("、");

function chartItems(rows: ExplorerRow[], cost = false): PnCompactRankingItem[] {
  return rows.map(row => ({ key: row.key, pn: row.label, value: row.value, displayValue: fmt(row.value, cost ? "cost" : "effective"),
    note: cost && row.cost_state === "partial" ? "部分缺价" : undefined }));
}

export default function MaintenancePnCompactView({ refreshKey = 0, projectLabels = EMPTY_LABELS }: {
  refreshKey?: number;
  projectLabels?: Record<string, string>;
}) {
  const [search, setSearch] = useSearchParams();
  const params = pnCompactParamsFromSearch(search);
  const { data, loading, error, reload } = useMaintenanceExplorer(params, refreshKey);
  useVisibleMaintenanceRefresh(async () => { if (loading) return false; reload(); return true; });
  const permissions = readPermissionMap();
  const overview = data?.pn_overview;
  const canCost = !!permissions.data_purchase_cost && overview?.cost_visibility === "visible"
    && data?.meta.cost_visibility === "visible" && data.summary.cost_state !== "restricted" && overview.cost_rows !== null;
  const multiSearch = new URLSearchParams(search);
  multiSearch.delete("view");
  const scope: string[] = [];
  if (params.business_type && params.business_type !== "all") scope.push(`业务类型：${csvLabels(params.business_type, BOARD_BUSINESS_TYPE_LABELS)}`);
  if (params.project) scope.push(`项目：${csvLabels(params.project, projectLabels)}`);
  if (params.customer && permissions.data_customer && data?.meta.customer_visibility !== "restricted") scope.push(`客户：${params.customer}`);
  if (params.sp) scope.push(`销售：${params.sp}`);
  if (params.q) scope.push(`搜索：${params.q}`);
  if (params.order_no) scope.push(`需求单号：${params.order_no}`);
  if (params.demand_type) scope.push(`需求类型：${csvLabels(params.demand_type, DEMAND_LABELS)}`);
  if (params.warehouse) scope.push(`仓库：${params.warehouse.split(",").join("、")}`);
  if (params.cost_source) scope.push(`成本来源：${csvLabels(params.cost_source, COST_LABELS)}`);

  return <div className="maintenance-pn-compact-view">
    <div className="mpc-toolbar">
      <Space wrap>
        <Select aria-label="紧凑图展示数量" value={params.top_n} options={[10, 15].map(value => ({ value, label: `Top ${value}` }))}
          onChange={value => setSearch(previous => { const next = new URLSearchParams(previous); next.set("pn_top", String(value)); return next; }, { replace: true })} />
        <span>按当前筛选全集分别排名，可直接框选下方区域截图。</span>
      </Space>
      <Link to={`/maintenance/analytics${multiSearch.size ? `?${multiSearch}` : ""}`}>打开多维分析</Link>
    </div>
    {loading ? <div className="mpc-loading" role="status"><p>分析数据更新中，请稍候再截图。</p><Skeleton active paragraph={{ rows: 5 }} /></div>
      : error ? <Alert type="error" showIcon message={error} action={<Button aria-label="重试" onClick={reload}>重试</Button>} />
        : !data || !overview ? <Alert type="warning" showIcon message="PN 紧凑图暂不可用，请刷新重试或打开多维分析。" action={<Button aria-label="重试" onClick={reload}>重试</Button>} />
          : <section className="maintenance-pn-compact" data-testid="pn-compact-capture" aria-label="PN 紧凑图截图区域">
            <header className="mpc-heading"><h2>PN 紧凑图 <small>Top {params.top_n}</small></h2><span>需求日期：{data.window.date_from || "最早记录"} — {data.window.date_to || "最新记录"}</span></header>
            <p className="mpc-scope">当前授权范围{scope.length ? `内 · ${scope.join(" · ")}` : "（无附加筛选）"}</p>
            <div className="mpc-kpis">
              {canCost && <div><span>已知需求成本（含税）</span><strong>{fmt(data.summary.cost_inc, "cost")}</strong><small>{data.summary.cost_state === "partial" ? "部分缺价，未推算缺失金额" : data.summary.cost_state === "unknown" ? overview.cost_rows!.length ? "全部缺价，金额未知" : "当前筛选下无需求成本记录" : "按当前可用成本统计"}</small></div>}
              <div><span>需求净量 / 件</span><strong>{fmt(data.summary.effective_qty, "effective")}</strong><small>需求数量 − 退货数量</small></div>
              <div><span>需求涉及 PN / 项目</span><strong>{fmt(data.summary.pn_count)} / {fmt(data.summary.project_count)}</strong><small>{fmt(data.summary.order_count)} 张需求单，全集去重</small></div>
            </div>
            {!canCost && <p className="mpc-notice">当前账号未开放成本展示，仅展示需求净量。</p>}
            <div className={`mpc-charts${canCost ? "" : " mpc-charts-single"}`}>
              {canCost && <MaintenancePnCompactRanking title="已知需求成本（含税）" metricLabel="按需求日期 · 元 · 缺价不当作零元" items={chartItems(overview.cost_rows!, true)} visibleCount={params.top_n} />}
              <MaintenancePnCompactRanking title="需求净量" metricLabel="按需求日期 · 件 · 需求数量减退货数量" items={chartItems(overview.effective_rows)} visibleCount={params.top_n} />
            </div>
            <footer className="mpc-footer">
              <p>{canCost ? `${fmt(data.summary.missing_lines)} 行缺价；未知不等于零，部分缺价仅展示已知金额。` : ""}需求净量不等同实际领用，不按返还收货冲减。{overview.has_negative ? "包含负值，条形保留方向，不计算集中度。" : ""}</p>
              <p>两榜与 KPI 来自同一次统计 · 统计时点：{data.meta.as_of}</p>
            </footer>
          </section>}
  </div>;
}
