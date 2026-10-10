import { useCallback, useEffect, useRef, useState } from "react";
import { fetchMaintenanceExplorer, type ExplorerDimension, type ExplorerMetric, type MaintenanceExplorerParams, type MaintenanceExplorerResponse } from "../../api/maintenanceAnalytics";

export const EXPLORER_METRICS: Record<ExplorerMetric, { label: string; unit: string; note: string }> = {
  issued: { label: "实际领用", unit: "件", note: "按已确认、更正后的领用事实和领用日期统计；需求数量不替代实际领用。" },
  cost: { label: "已知需求成本", unit: "元", note: "按需求单日期汇总已知含税成本；缺价单列，未知不当作零元。这不是实际领用成本。" },
  effective: { label: "需求净量", unit: "件", note: "需求数量减退货数量，按需求单日期统计；保留负值，不等同实际领用。" },
  orders: { label: "需求单数", unit: "单", note: "各组内需求单去重；同一张单可能出现在多个 PN 中，组间单数不可直接相加。" },
  missing: { label: "缺价明细", unit: "行", note: "按需求单日期统计缺少成本的明细行数，用于定位待核对的资料。" },
};
export const EXPLORER_DIMENSIONS: Record<ExplorerDimension, string> = { pn: "PN 排名", project: "项目排名", customer: "客户分布", salesperson: "销售分布", business: "业务类型" };
const validDimension = (value: string | null): ExplorerDimension => value && Object.prototype.hasOwnProperty.call(EXPLORER_DIMENSIONS, value) ? value as ExplorerDimension : "pn";
const validMetric = (value: string | null): ExplorerMetric => value && Object.prototype.hasOwnProperty.call(EXPLORER_METRICS, value) ? value as ExplorerMetric : "issued";
const positiveInt = (value: string | null, fallback: number, max: number) => { const n = Number(value); return Number.isInteger(n) && n > 0 ? Math.min(n, max) : fallback; };

/** 只读取统计参数；布局与回跳地址绝不传入 API。 */
export function explorerParamsFromSearch(search: URLSearchParams): MaintenanceExplorerParams {
  const params: MaintenanceExplorerParams = {
    range: search.get("range") || "ytd",
    dimension: validDimension(search.get("dim") || search.get("dimension")),
    metric: validMetric(search.get("metric")),
    page: positiveInt(search.get("page"), 1, 1000000),
    page_size: positiveInt(search.get("ps"), 20, 100),
    top_n: positiveInt(search.get("top"), 20, 30),
    focus_page: positiveInt(search.get("focus_page"), 1, 1000000),
    focus_page_size: 8,
  };
  for (const key of ["q", "business_type", "project", "customer", "sp", "order_no", "demand_type", "warehouse", "cost_source", "focus"] as const) {
    const value = search.get(key);
    if (value) params[key] = value;
  }
  if (params.range === "custom") {
    params.date_from = search.get("from") || undefined;
    params.date_to = search.get("to") || undefined;
  }
  return params;
}

/** 展示格式化不把 Decimal 先转成浮点，避免大金额在浏览器中丢分。 */
export function formatExplorerValue(value: string | number | null | undefined, metric?: ExplorerMetric): string {
  if (value == null) return "未知";
  const raw = String(value);
  if (!/^-?\d+(\.\d+)?$/.test(raw)) return "—";
  const negative = raw.startsWith("-");
  const [whole, fraction = ""] = raw.replace(/^-/, "").split(".");
  const scale = metric === "cost" ? 2 : 3;
  const padded = fraction.padEnd(scale + 1, "0");
  let units = BigInt(whole + padded.slice(0, scale));
  if (Number(padded[scale]) >= 5) units += 1n;
  const digits = units.toString().padStart(scale + 1, "0");
  const grouped = digits.slice(0, -scale).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const decimal = metric === "cost" ? digits.slice(-scale) : digits.slice(-scale).replace(/0+$/, "");
  return `${metric === "cost" ? "¥" : ""}${negative && units !== 0n ? "-" : ""}${grouped}${decimal ? `.${decimal}` : ""}`;
}

export function explorerErrorMessage(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string") return detail.message;
  if (Array.isArray(detail)) return "筛选参数无效，请检查日期、项目与排名条件。";
  return "分析数据加载失败，请重试。";
}

/** 请求切换即隐藏上一范围的数据；abort + 代次守卫同时覆盖网络与解包竞态。 */
export function useMaintenanceExplorer(params: MaintenanceExplorerParams, refreshKey = 0) {
  const key = JSON.stringify(params);
  const [retry, setRetry] = useState(0);
  const sequence = useRef(0);
  const [state, setState] = useState<{ key: string; data: MaintenanceExplorerResponse | null; error: string | null; loading: boolean }>({ key: "", data: null, error: null, loading: true });
  useEffect(() => {
    const request = ++sequence.current;
    const controller = new AbortController();
    setState(previous => ({ key, data: previous.key === key ? previous.data : null, error: null, loading: true }));
    fetchMaintenanceExplorer(JSON.parse(key) as MaintenanceExplorerParams, controller.signal).then(data => {
      if (request === sequence.current && !controller.signal.aborted) setState({ key, data, error: null, loading: false });
    }).catch(error => {
      if (request === sequence.current && !controller.signal.aborted) setState({ key, data: null, error: explorerErrorMessage(error), loading: false });
    });
    return () => { controller.abort(); sequence.current += 1; };
  }, [key, refreshKey, retry]);
  const reload = useCallback(() => { setRetry(value => value + 1); return true; }, []);
  return { ...(state.key === key ? state : { key, data: null, error: null, loading: true }), reload };
}
