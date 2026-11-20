import { createContext, useContext, type ReactNode } from "react";
import { Alert, Pagination, Spin } from "antd";
import {
  fetchWarehouse,
  warehouseParams,
  type Coverage,
  type Drill,
  type WarehouseFilters,
  type WarehouseQuery,
  type WarehouseResults,
} from "../../api/warehouseAnalytics";
import { makeReadCache, useDashboardRead } from "../purchases/analytics/useDashboardRead";
import { pageSize } from "./warehouseState";

export const createWarehouseCache = () => makeReadCache<unknown>(64, 60_000);
export type Patch = (values: Record<string, string | null>, push?: boolean) => void;
export interface WarehouseContextValue {
  owner: string | null;
  stale: () => void;
  revision: number;
  cache: ReturnType<typeof createWarehouseCache>;
  params: URLSearchParams;
  filters: WarehouseFilters;
  coverage?: Coverage;
  patch: Patch;
  refresh: () => void;
  drill: (selection: Drill, title: string) => void;
}
export const WarehouseContext = createContext<WarehouseContextValue | null>(null);
export function useWarehouse() {
  const ctx = useContext(WarehouseContext);
  if (!ctx) throw new Error("Missing warehouse analysis context");
  return ctx;
}
export function useWarehouseRead<K extends keyof WarehouseResults>(
  endpoint: K,
  query: WarehouseQuery,
  enabled = true,
  docId?: string,
) {
  const { owner, stale, revision, cache } = useWarehouse();
  const params = warehouseParams(query);
  const key = enabled ? JSON.stringify([revision, endpoint, docId, params.toString()]) : null;
  return useDashboardRead<WarehouseResults[K]>(
    key,
    () => cache.get(key!, () => fetchWarehouse(endpoint, params, docId)) as Promise<WarehouseResults[K]>,
    owner,
    stale,
  );
}
export function Result<T>({
  read,
  children,
}: {
  read?: { data?: T; error?: string };
  children: (data: T) => ReactNode;
}) {
  const { refresh } = useWarehouse();
  if (read?.error)
    return (
      <Alert
        type="error"
        showIcon
        message={read.error}
        action={
          <button className="wa-link" onClick={refresh}>
            重试
          </button>
        }
      />
    );
  if (!read?.data)
    return (
      <div role="status" className="wa-empty">
        <Spin size="small" /> 正在读取…
      </div>
    );
  return <>{children(read.data)}</>;
}
export function Panel({
  title,
  why,
  actions,
  children,
}: {
  title: string;
  why: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="wa-panel">
      <header>
        <div>
          <h2>{title}</h2>
          <p>{why}</p>
        </div>
        {actions && <div className="wa-tools">{actions}</div>}
      </header>
      <div className="wa-panel-body">{children}</div>
    </section>
  );
}
export function Pick({
  label,
  value,
  options,
  change,
}: {
  label: string;
  value: string;
  options: Record<string, string>;
  change: (value: string) => void;
}) {
  return (
    <label className="wa-pick">
      {label}
      <select aria-label={label} value={value} onChange={(e) => change(e.target.value)}>
        {Object.entries(options).map(([key, name]) => (
          <option key={key} value={key}>
            {name}
          </option>
        ))}
      </select>
    </label>
  );
}
export function DataTable({
  labels,
  children,
  empty = false,
}: {
  labels: string[];
  children: ReactNode;
  empty?: boolean;
}) {
  if (empty) return <p className="wa-empty">当前筛选没有匹配记录。</p>;
  return (
    <div className="wa-scroll">
      <table>
        <thead>
          <tr>
            {labels.map((label, i) => (
              <th key={i} scope="col">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
export function Pager({
  page,
  total,
  change,
}: {
  page: number;
  total: number;
  change: (n: number) => void;
}) {
  return (
    <Pagination
      size="small"
      current={page}
      pageSize={pageSize}
      total={total}
      showSizeChanger={false}
      hideOnSinglePage
      showTotal={(n) => `共 ${n.toLocaleString()} 项`}
      onChange={change}
    />
  );
}
export const format = (value?: number | null) =>
  value == null ? "—" : value.toLocaleString("zh-CN", { maximumFractionDigits: 8 });
export const quantity = (value: number | null, unit?: string | null) =>
  value == null ? "数量未填" : `${format(value)} ${unit || "（单位未填）"}`;
export function DrillLink({ to, label, children }: { to: Drill; label: string; children: ReactNode }) {
  const { drill } = useWarehouse();
  return (
    <button type="button" className="wa-link" aria-label={label} onClick={() => drill(to, label)}>
      {children}
    </button>
  );
}
export function Tile({ label, value, detail }: { label: string; value: ReactNode; detail?: string }) {
  return (
    <div className="wa-tile">
      <small>{label}</small>
      <strong>{value}</strong>
      {detail && <span>{detail}</span>}
    </div>
  );
}
