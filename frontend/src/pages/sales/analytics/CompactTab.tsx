// 多维紧凑图页签（2026-10-22，对标维保数据分析「PN 紧凑双榜 / 多维排名」）：
// 同一筛选全集下，按客户 / 销售人员 / 商品大类 / 品牌 / PN / 业务类型 / 仓库 /
// 客户类型 / 客户城市各自独立排名，一屏并列成紧凑条形榜，共用一个排名指标。
// - 每维一次 /groups 请求（sort = 指标，desc，top = N），与分组排行页同口径；
//   null ≠ 0：金额缺失组不画条、显示「—」；毛利含负值时条形保留方向（0 轴）。
// - 占比只在指标 = 当前金额区间口径（amount_basis）时显示——后端 share 按该
//   口径计算，换指标后占比不再对应，宁缺不错。
// - 点击可下钻维度的条目 = 该维度筛选收窄为此值并跳订单明细（drillPatch 同语义）；
//   PN / 仓库 / 城市无筛选参数，只展示。
// - 权限：无客户权限不出客户类维度（后端 403）；无成本权限不给不含税 / 含税指标；
//   无毛利权限不给毛利指标。
import { Alert, Button, Segmented, Select, Skeleton } from "antd";
import {
  fetchSalesAnalyticsGroups,
  SALES_CUSTOMER_DIMS,
  type SalesAnalyticsFilters,
  type SalesGroupDim,
  type SalesGroupRow,
  type SalesGroupsResp,
} from "../../../api/salesAnalytics";
import { pct } from "../../../utils/format";
import { fmtMoney } from "../shared";
import { drillPatch, groupRowLabel, GROUP_DIM_LABELS } from "./GroupsTab";
import { useAsyncData } from "./useAsyncData";
import {
  COMPACT_TOP_OPTIONS,
  type CompactMetric,
  type SalesAnalyticsUrlState,
} from "./urlState";
import "./salesCompact.css";

export const COMPACT_METRIC_LABELS: Record<CompactMetric, string> = {
  amount_ex_tax: "不含税金额",
  amount_inc_tax: "含税订单金额",
  revenue: "营收",
  gross_profit: "毛利",
  orders: "订单数",
};

/** 紧凑图维度顺序：主维度在前，客户类属维度随客户权限。 */
export const COMPACT_DIMS: readonly SalesGroupDim[] = [
  "customer", "salesperson", "category", "brand", "pn",
  "business_type", "warehouse", "customer_type", "customer_city",
];

export interface CompactTabProps {
  state: SalesAnalyticsUrlState;
  apiFilters: SalesAnalyticsFilters;
  filterKey: string;
  patch: (next: Partial<SalesAnalyticsUrlState>, opts?: { history?: "push" | "replace" }) => void;
  costRestricted: boolean;
  profitRestricted: boolean;
  customerRestricted: boolean;
  toMessage: (err: unknown) => string;
  ownerToken: string | null;
  onOwnerStale: () => void;
}

/** 当前账号可用的指标（受限指标不出现，URL 残值回退到首个可用）。 */
export function availableCompactMetrics(costRestricted: boolean, profitRestricted: boolean): CompactMetric[] {
  return (Object.keys(COMPACT_METRIC_LABELS) as CompactMetric[]).filter((metric) => {
    if (costRestricted && (metric === "amount_ex_tax" || metric === "amount_inc_tax")) return false;
    if (profitRestricted && metric === "gross_profit") return false;
    return true;
  });
}

function metricValue(row: SalesGroupRow, metric: CompactMetric): number | null {
  const value = row[metric];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function metricText(value: number | null, metric: CompactMetric): string {
  if (metric === "orders") return value == null ? "—" : `${value.toLocaleString("zh-CN")} 单`;
  return fmtMoney(value);
}

function CompactPanel({
  dim, metric, top, showShare, state, apiFilters, filterKey, patch, toMessage, ownerToken, onOwnerStale,
}: {
  dim: SalesGroupDim; metric: CompactMetric; top: number; showShare: boolean;
} & Pick<CompactTabProps, "state" | "apiFilters" | "filterKey" | "patch" | "toMessage" | "ownerToken" | "onOwnerStale">) {
  const { data, loading, error, retry } = useAsyncData<SalesGroupsResp>(
    (context) => fetchSalesAnalyticsGroups({ ...apiFilters, group_by: dim, sort: metric, order: "desc", top }, context),
    [filterKey, dim, metric, top],
    toMessage,
    true,
    ownerToken,
    onOwnerStale,
    "read_groups",
  );
  const rows = data?.groups ?? [];
  const values = rows.map((row) => metricValue(row, metric));
  const minimum = Math.min(0, ...values.map((v) => v ?? 0));
  const maximum = Math.max(0, ...values.map((v) => v ?? 0));
  const span = maximum - minimum || 1;
  const zero = (-minimum / span) * 100;
  const topShare = data?.concentration?.[top >= 10 ? "top10_share" : "top5_share"] ?? null;
  const title = GROUP_DIM_LABELS[dim];

  return (
    <section className="sc-panel" role="group" aria-label={`${title}排名`} data-testid={`compact-${dim}`}>
      <header className="sc-panel-head">
        <h3>{title}</h3>
        <span>
          {data ? `共 ${data.total_groups} 组` : ""}
          {showShare && data && topShare != null ? ` · 前 10 占 ${pct(topShare)}` : ""}
        </span>
      </header>
      {error ? (
        <Alert type="error" showIcon message={error}
          action={<Button type="link" size="small" onClick={retry}>重试</Button>} />
      ) : loading && !data ? (
        <Skeleton active paragraph={{ rows: 5 }} title={false} />
      ) : !rows.length ? (
        <p className="sc-empty">当前筛选下没有记录</p>
      ) : (
        <ol className="sc-list">
          {rows.map((row, index) => {
            const value = values[index];
            const label = groupRowLabel(dim, row);
            const drill = drillPatch(state, dim, row);
            const name = drill ? (
              <button type="button" className="sc-name is-link" title={`${label}（点击筛选并查看订单）`}
                onClick={() => patch(drill, { history: "push" })}>{label}</button>
            ) : <span className="sc-name" title={label}>{label}</span>;
            return (
              <li key={`${row.key ?? "__empty__"}-${index}`} className="sc-row">
                <span className={`sc-index${index < 3 ? " is-top" : ""}`} aria-hidden>{index + 1}</span>
                {name}
                <span className="sc-value">
                  {metricText(value, metric)}
                  {showShare && row.share != null ? <small>{pct(row.share)}</small> : null}
                </span>
                <span className="sc-track" aria-hidden>
                  <i className="sc-zero" style={{ left: `${zero}%` }} />
                  {value != null && value !== 0 ? (
                    <i className={`sc-fill${value < 0 ? " is-negative" : ""}`} style={{
                      left: `${value < 0 ? zero + (value / span) * 100 : zero}%`,
                      width: `${(Math.abs(value) / span) * 100}%`,
                    }} />
                  ) : null}
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

export default function CompactTab(props: CompactTabProps) {
  const {
    state, apiFilters, filterKey, patch, costRestricted, profitRestricted, customerRestricted,
    toMessage, ownerToken, onOwnerStale,
  } = props;
  const metrics = availableCompactMetrics(costRestricted, profitRestricted);
  const metric = metrics.includes(state.compactMetric) ? state.compactMetric : metrics[0];
  const top = state.compactTop;
  const basis = apiFilters.amount_basis ?? "amount_ex_tax";
  const showShare = metric === basis;
  const dims = COMPACT_DIMS.filter((dim) => !(customerRestricted && SALES_CUSTOMER_DIMS.includes(dim)));
  const windowText = `${apiFilters.date_from ?? "最早记录"} — ${apiFilters.date_to ?? "最新记录"}`;

  return (
    <div className="sales-compact" data-testid="sales-compact">
      <div className="sc-toolbar">
        <Segmented<CompactMetric>
          aria-label="紧凑图排名指标"
          value={metric}
          options={metrics.map((value) => ({ value, label: COMPACT_METRIC_LABELS[value] }))}
          onChange={(value) => patch({ compactMetric: value })}
        />
        <Select<number>
          aria-label="紧凑图每维展示数量"
          value={top}
          style={{ width: 110 }}
          options={COMPACT_TOP_OPTIONS.map((value) => ({ value, label: `Top ${value}` }))}
          onChange={(value) => patch({ compactTop: value })}
        />
        <span className="sc-hint">各维度按当前筛选全集分别排名；点击条目即收窄筛选并查看订单。</span>
      </div>
      <section className="sc-board" aria-label="多维紧凑图截图区域">
        <header className="sc-heading">
          <h2>多维紧凑图 <small>按{COMPACT_METRIC_LABELS[metric]} · Top {top}</small></h2>
          <span>订单日期：{windowText}</span>
        </header>
        <div className="sc-grid">
          {dims.map((dim) => (
            <CompactPanel key={dim} dim={dim} metric={metric} top={top} showShare={showShare}
              state={state} apiFilters={apiFilters} filterKey={filterKey} patch={patch}
              toMessage={toMessage} ownerToken={ownerToken} onOwnerStale={onOwnerStale} />
          ))}
        </div>
        <footer className="sc-footer">
          {showShare
            ? `占比按${COMPACT_METRIC_LABELS[metric]}计，分母为筛选全集（非仅前 ${top} 名）。`
            : "当前指标与金额区间口径不同，不显示占比。"}
          {metric === "gross_profit" ? "毛利含负值时条形保留方向；无已配成本的组无毛利（—），不按 0 计。" : ""}
          商品大类 / 品牌 / PN 为行级维度：整单入选、匹配行计额。
        </footer>
      </section>
    </div>
  );
}
