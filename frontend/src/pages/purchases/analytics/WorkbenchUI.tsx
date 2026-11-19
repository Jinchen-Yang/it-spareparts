import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { Alert, Button, Skeleton } from "antd";
import { useLocation, useSearchParams } from "react-router-dom";
import { makeReadCache, useDashboardRead } from "./useDashboardRead";
import { sessionStale } from "../../../utils/session";
import type { PurchaseAnalyticsFilters } from "../../../api/purchaseAnalytics";

export type WorkbenchView = "boards" | "matrix" | "compare" | "work" | "structure" | "pool";
export type DrillScope = Record<string, string | null>;
export interface WorkbenchProps {
  view: WorkbenchView;
  filters: PurchaseAnalyticsFilters;
  ownerToken: string | null;
  onOwnerStale: () => void;
  costRestricted: boolean;
  customerRestricted: boolean;
  supplierRestricted: boolean;
  onDrill: (scope: DrillScope, label: string) => void;
  scope?: DrillScope;
  refreshKey?: number;
}

type WorkbenchCache = ReturnType<typeof makeReadCache<unknown>>;
export const WorkbenchContext = createContext<(WorkbenchProps & { cache: WorkbenchCache }) | null>(null);
export function useWorkbench() {
  const ctx = useContext(WorkbenchContext);
  if (!ctx) throw new Error("采购分析视图需要页面上下文");
  return ctx;
}

export function WorkbenchProvider({ children, ...props }: WorkbenchProps & { children: ReactNode }) {
  const parent = useContext(WorkbenchContext);
  const ownCache = useMemo(
    () => makeReadCache<unknown>(32),
    [props.ownerToken, props.refreshKey, props.costRestricted, props.customerRestricted, props.supplierRestricted],
  );
  const sameSession =
    parent &&
    parent.ownerToken === props.ownerToken &&
    parent.refreshKey === props.refreshKey &&
    parent.costRestricted === props.costRestricted &&
    parent.customerRestricted === props.customerRestricted &&
    parent.supplierRestricted === props.supplierRestricted;
  const cache = sameSession ? parent.cache : ownCache;
  return <WorkbenchContext.Provider value={{ ...props, cache }}>{children}</WorkbenchContext.Provider>;
}

export function useSetting() {
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const get = (key: string, value: string) => params.get("wb_" + key) || value;
  const set = (changes: Record<string, string>, push = false) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [key, value] of Object.entries(changes)) {
          if (value) next.set("wb_" + key, value);
          else next.delete("wb_" + key);
        }
        return next;
      },
      { replace: !push, state: push ? { procurement: true } : location.state },
    );
  return { get, set };
}

export function QueryBlock<T>({
  id,
  load,
  children,
}: {
  id: string;
  load: () => Promise<{ data: T }>;
  children: (result: T) => ReactNode;
}) {
  const { cache, ownerToken, onOwnerStale, filters, scope, refreshKey } = useWorkbench();
  const key = JSON.stringify([filters, scope, id, refreshKey]);
  return <BoundQuery key={key} {...{ cache, ownerToken, onOwnerStale, load, children }} cacheKey={key} />;
}
function BoundQuery<T>({
  cache,
  ownerToken,
  onOwnerStale,
  load,
  children,
  cacheKey,
}: {
  cache: WorkbenchCache;
  ownerToken: string | null;
  onOwnerStale: () => void;
  load: () => Promise<{ data: T }>;
  children: (result: T) => ReactNode;
  cacheKey: string;
}) {
  const [attempt, retry] = useState(0);
  const snapshot = useDashboardRead<T>(
    cacheKey + ":" + attempt,
    () => cache.get(cacheKey, async () => (await load()).data) as Promise<T>,
    ownerToken,
    onOwnerStale,
  );
  if (!snapshot) return <Skeleton active paragraph={{ rows: 2 }} title={false} />;
  if (snapshot.error)
    return (
      <Alert
        type="error"
        message={snapshot.error}
        action={
          <Button size="small" onClick={() => retry((n) => n + 1)}>
            重试
          </Button>
        }
      />
    );
  return snapshot.data == null ? <EmptyNote /> : <>{children(snapshot.data)}</>;
}
export function Drill({
  scope,
  label,
  children,
  title,
}: {
  scope: DrillScope;
  label: string;
  children: ReactNode;
  title?: string;
}) {
  const { onDrill, ownerToken, onOwnerStale } = useWorkbench();
  return (
    <button
      className="pw-link"
      type="button"
      title={title}
      aria-label={label}
      onClick={() => {
        if (sessionStale(ownerToken)) {
          onOwnerStale();
          return;
        }
        onDrill(scope, label);
      }}
    >
      {children}
    </button>
  );
}
export function Guide({
  title,
  note,
  extra,
  children,
}: {
  title: string;
  note: string;
  extra?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="pw-card">
      <header>
        <div>
          <h3>{title}</h3>
          <p>{note}</p>
        </div>
        {extra}
      </header>
      {children}
    </section>
  );
}
export function Pick({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: readonly (readonly [string, string])[];
  onChange: (v: string) => void;
}) {
  return (
    <label className="pw-pick">
      {label}
      <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, t]) => (
          <option key={v} value={v}>
            {t}
          </option>
        ))}
      </select>
    </label>
  );
}
export function EmptyNote({ children = "当前筛选范围内没有采购记录。" }: { children?: ReactNode }) {
  return <p className="pw-empty">{children}</p>;
}
