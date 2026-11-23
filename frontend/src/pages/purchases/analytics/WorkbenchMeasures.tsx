import type { ReactNode } from "react";
import {
  fetchCube,
  type Cube,
  type CubeQuery,
  type Measure,
  type Group,
  type WbDimension,
  type WbMetric,
} from "../../../api/purchaseWorkbench";
import { formatMoney, qty } from "../../../utils/format";
import { EmptyNote, Guide, Pick, QueryBlock, useSetting, useWorkbench, type DrillScope } from "./WorkbenchUI";
import DocumentPreview from "./DocumentPreview";
import WorkbenchChartPoint from "./WorkbenchChartPoint";

export const dimensionNames: Record<WbDimension, string> = {
  buyer: "采购员",
  type: "采购类型",
  supplier: "供应商",
  pn: "PN",
  pool: "互通池",
  category: "商品大类",
  unit: "单位",
  day: "采购日期",
  week: "采购周",
  month: "采购月",
  customer: "客户",
  salesperson: "销售人员",
  linkedSalesOrderNo: "关联销售单",
  orderId: "采购单",
  linkStatus: "销采关联状态",
};
export const metricNames: Record<WbMetric, string> = {
  amount: "采购金额",
  orders: "采购单数",
  lines: "明细行数",
  qty: "采购量",
  avgOrder: "单均金额",
  suppliers: "供应商数",
  pns: "PN 数",
};
export const basisNames: Record<string, string> = { paid: "实付", inc: "含税", ex: "不含税" };
const TOP_OPTIONS = ["3", "5", "10", "20"];
export const num = (n: number | null | undefined) =>
  n == null ? "—" : n.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
export const cash = (n: number | null | undefined) => formatMoney(n, { prefix: "¥" });
export const percent = (n: number | null | undefined) => (n == null ? "—" : (n * 100).toFixed(1) + "%");
/** 页面精确筛选可以指定未填单位。 */
export function quantityUnit(scope: DrillScope | undefined, fallback: string): string | null {
  return scope && Object.prototype.hasOwnProperty.call(scope, "unit") ? scope.unit : fallback;
}
export function display(n: number | null | undefined, metric: WbMetric, unit: string | null) {
  if (n == null || (metric === "qty" && unit === null)) return "—";
  if (metric === "amount" || metric === "avgOrder") return cash(n);
  const suffix = { orders: " 单", lines: " 行", qty: " " + unit, suppliers: " 家", pns: " 个" };
  return (metric === "qty" ? qty(n) : num(n)) + suffix[metric];
}
export function useMeasure(average = false) {
  const { costRestricted } = useWorkbench();
  const { get, set } = useSetting();
  const choices: WbMetric[] = costRestricted
    ? ["orders", "lines", "qty"]
    : ["amount", "orders", "lines", "qty", ...(average ? ["avgOrder" as const] : [])];
  const requested = get("metric", choices[0]);
  const metric = choices.find((v) => v === requested) || choices[0];
  const rawBasis = get("basis", "paid");
  const basis = Object.hasOwnProperty.call(basisNames, rawBasis) ? rawBasis : "paid";
  const unit = get("unit", "件");
  return { metric, basis, unit, choices, get, set };
}

export function CubeBlock({ query, children }: { query: CubeQuery; children: (result: Cube) => ReactNode }) {
  const { filters, scope } = useWorkbench();
  if (
    Object.entries(query.scope || {}).some(
      ([key, value]) => scope && Object.prototype.hasOwnProperty.call(scope, key) && scope[key] !== value,
    )
  ) {
    return <EmptyNote>当前精确筛选未包含该分组。</EmptyNote>;
  }
  const q = { ...query, scope: { ...scope, ...query.scope } };
  return (
    <QueryBlock id={JSON.stringify(q)} load={() => fetchCube(filters, q)}>
      {children}
    </QueryBlock>
  );
}
export function QuantityUnitControl({
  unit,
  label = "数量单位",
  onChange,
}: {
  unit: string | null;
  label?: string;
  onChange: (unit: string) => void;
}) {
  const { scope } = useWorkbench();
  if (scope && Object.prototype.hasOwnProperty.call(scope, "unit")) {
    return <span className="pw-pick" aria-label={label}>{unit === null
      ? "单位未填，采购量不可比较"
      : `${label} · ${unit}（页面筛选）`}</span>;
  }
  return (
    <CubeBlock query={{ row: "unit", metric: "orders", basis: "paid", page_size: 100 }}>
      {(data) => {
        const units = data.rows.flatMap((r) => r.key ? [r.key] : []);
        return <Pick label={label} value={unit || ""}
          options={[...new Set([...(unit ? [unit] : []), ...units])].map((u) => [u, u])}
          onChange={onChange} />;
      }}
    </CubeBlock>
  );
}
export function MeasureTools({ average = false, top = false }: { average?: boolean; top?: boolean }) {
  const { metric, basis, unit, choices, get, set } = useMeasure(average);
  const { scope } = useWorkbench();
  return (
    <div className="pw-controls">
      <Pick
        label="统计指标"
        value={metric}
        options={choices.map((m) => [m, metricNames[m]])}
        onChange={(v) => set({ metric: v, rpage: "", cpage: "" })}
      />
      {(metric === "amount" || metric === "avgOrder") && (
        <Pick
          label="金额口径"
          value={basis}
          options={Object.entries(basisNames)}
          onChange={(v) => set({ basis: v, rpage: "", cpage: "" })}
        />
      )}
      {metric === "qty" && (
        <QuantityUnitControl unit={quantityUnit(scope, unit)}
          onChange={(v) => set({ unit: v, rpage: "", cpage: "" })} />
      )}
      {top && (
        <Pick
          label="显示名次"
          value={String(readTop(get("top", "")))}
          options={TOP_OPTIONS.map((v) => [v, "前 " + v + " 项"])}
          onChange={(v) => set({ top: v })}
        />
      )}
    </div>
  );
}
export function readTop(raw: string) {
  return TOP_OPTIONS.includes(raw) ? Number(raw) : 20;
}
export function Segments({
  options,
  value,
  onChange,
}: {
  options: readonly (readonly [string, string])[];
  value: string;
  onChange: (key: string) => void;
}) {
  return (
    <div className="pw-segments">
      {options.map(([id, name]) => (
        <button key={id} type="button" aria-pressed={id === value} onClick={() => onChange(id)}>
          {name}
        </button>
      ))}
    </div>
  );
}
export function GroupName({ item, dim }: { item: Group; dim: WbDimension }) {
  const { filters, scope, ownerToken, onOwnerStale } = useWorkbench();
  if ((dim === "orderId" || dim === "linkedSalesOrderNo") && item.key)
    return (
      <DocumentPreview
        kind={dim === "orderId" ? "purchase" : "sale"}
        number={item.label}
        orderId={dim === "orderId" ? Number(item.key) : undefined}
        {...{ filters, scope: scope || {}, ownerToken, onOwnerStale }}
      >
        {item.label}
      </DocumentPreview>
    );
  return (
    <span className="pw-name">
      {item.label || "未填写"}
      {dim === "pn" && <small>{item.description ? `目录 · ${item.description}` : "目录描述未填"}</small>}
    </span>
  );
}

export function RankCard({
  title,
  note,
  query,
  unit,
}: {
  title: string;
  note: string;
  query: CubeQuery;
  unit: string | null;
}) {
  return (
    <Guide title={title} note={note}>
      <CubeBlock query={query}>
        {(data) => {
          if (!data.rows.length) return <EmptyNote />;
          const known = data.rows.flatMap((r) => r.value == null ? [] : [r.value]);
          const min = Math.min(0, ...known), max = Math.max(0, ...known);
          const extent = max - min || 1;
          const money = query.metric === "amount" || query.metric === "avgOrder";
          const metricLabel = (money ? basisNames[query.basis] : "") + metricNames[query.metric];
          const isDocument = query.row === "orderId" || query.row === "linkedSalesOrderNo";
          const topSum = data.rows.reduce((sum, r) => sum + (r.value ?? 0), 0);
          const share =
            ["amount", "lines", "qty"].includes(query.metric) &&
            data.total.value != null &&
            data.total.value > 0 &&
            topSum >= 0 &&
            topSum <= data.total.value + 0.001
              ? topSum / data.total.value
              : null;
          return (
            <>
              <ol className="pw-list">
                {data.rows.map((row, i) => (
                  <li key={row.key ?? "∅"}>
                    {isDocument && <span className="pw-rank-document"><GroupName item={row} dim={query.row} /></span>}
                    <WorkbenchChartPoint
                      className="pw-chart-rank"
                      measure={row}
                      label={title + " · " + row.label + "，查看采购记录"}
                      x={[metricLabel, display(row.value, query.metric, unit)]}
                      y={[dimensionNames[query.row], row.label]}
                      detail={query.row === "pn" ? [["目录描述", row.description || "未填写"]] : []}
                    >
                      <span className="pw-rank">{row.value == null ? "—" : i + 1}</span>
                      {isDocument ? <span /> : <GroupName item={row} dim={query.row} />}
                      <strong>{display(row.value, query.metric, unit)}</strong>
                      <span className="pw-bar" aria-hidden="true">
                        {min < 0 && <b style={{ left: (-min / extent) * 100 + "%" }} />}
                        {row.value != null && <i className={row.value < 0 ? "negative" : undefined}
                          style={{
                            left: ((Math.min(0, row.value) - min) / extent) * 100 + "%",
                            width: (Math.abs(row.value) / extent) * 100 + "%",
                          }} />}
                      </span>
                    </WorkbenchChartPoint>
                  </li>
                ))}
              </ol>
              <div className="pw-foot">
                <span>
                  显示 {data.rows.length} / {data.total_rows} 项
                </span>
                {share != null && (
                  <span>
                    前 {data.rows.length} 项占 {percent(share)}
                  </span>
                )}
                <span>全集 {display(data.total.value, query.metric, unit)}</span>
              </div>
            </>
          );
        }}
      </CubeBlock>
    </Guide>
  );
}

export function positivePage(value: string) {
  const n = Number(value);
  return Number.isSafeInteger(n) && n > 0 ? n : 1;
}
export function basisValue(r: Measure, basis: string) {
  return basis === "inc" ? r.amount_inc : basis === "ex" ? r.amount_ex : r.amount_paid;
}
