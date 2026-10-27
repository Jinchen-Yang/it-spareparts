import { useEffect, useMemo, useState } from "react";
import type { DataGridColumn, DataGridSort, DataGridView } from "./types";
import { clampColumnWidth } from "./types";

/** JWT 的 sub 只用于偏好隔离，权限仍以服务端为准。不能用同名用户的 name 替代账号。 */
export function gridAccountId(explicit?: string | number): string | null {
  if (explicit != null) return String(explicit);
  try {
    const payload = localStorage.getItem("token")?.split(".")[1];
    if (!payload) return null;
    const encoded = payload.replace(/-/g, "+").replace(/_/g, "/");
    const bytes = Uint8Array.from(atob(encoded.padEnd(Math.ceil(encoded.length / 4) * 4, "=")), (char) => char.charCodeAt(0));
    const sub = JSON.parse(new TextDecoder().decode(bytes)).sub;
    return (typeof sub === "string" && sub.length > 0) || typeof sub === "number" ? String(sub) : null;
  } catch { return null; }
}

export function gridStorageKey(id: string, accountId: string | null) {
  return accountId == null ? null : `datagrid:v1:${encodeURIComponent(accountId)}:${encodeURIComponent(id)}`;
}

export function defaultGridView<T>(columns: DataGridColumn<T>[], defaultSort: DataGridSort | null = null): DataGridView {
  const identity = columns.find((column) => column.identity)?.key ?? columns[0]?.key;
  return {
    widths: Object.fromEntries(columns.map((column) => [column.key, clampColumnWidth(column, column.width ?? 160)])),
    hidden: columns.filter((column) => column.hidden && column.key !== identity).map((column) => column.key),
    sort: defaultSort,
  };
}

export function readGridView<T>(key: string | null, columns: DataGridColumn<T>[], defaultSort: DataGridSort | null = null): DataGridView {
  const defaults = defaultGridView(columns, defaultSort);
  if (!key) return defaults;
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return defaults;
    const saved: unknown = JSON.parse(raw);
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return defaults;
    const source = saved as Partial<DataGridView>;
    if (!source.widths || typeof source.widths !== "object" || !Array.isArray(source.hidden)) return defaults;
    const identity = columns.find((column) => column.identity)?.key ?? columns[0]?.key;
    const widths = { ...defaults.widths };
    for (const column of columns) {
      const width = source.widths[column.key];
      if (typeof width === "number" && Number.isFinite(width)) widths[column.key] = clampColumnWidth(column, width);
    }
    const sort = source.sort === null ? null : source.sort && columns.some((column) => column.key === source.sort?.key && (column.sortable ?? !!column.type))
      && ["asc", "desc"].includes(source.sort.dir) ? source.sort : defaults.sort;
    return { widths, hidden: source.hidden.filter((name) => typeof name === "string" && name !== identity && columns.some((column) => column.key === name)), sort };
  } catch { return defaults; }
}

export function useGridView<T>(id: string, accountId: string | number | undefined, columns: DataGridColumn<T>[], defaultSort: DataGridSort | null = null) {
  const key = gridStorageKey(id, gridAccountId(accountId));
  const schema = JSON.stringify(columns.map(({ key: columnKey, width, hidden, identity, type, minWidth, maxWidth }) => ({ key: columnKey, width, hidden, identity, type, minWidth, maxWidth })));
  const initial = useMemo(() => readGridView(key, columns, defaultSort), [key, schema]);
  const [state, setState] = useState({ key, view: initial });
  const view = state.key === key ? state.view : initial;
  useEffect(() => { setState({ key, view: initial }); }, [key, initial]);

  const update = (next: DataGridView) => {
    setState({ key, view: next });
    try { if (key) localStorage.setItem(key, JSON.stringify(next)); } catch { /* 禁用存储时保留当前页可用。 */ }
  };
  const reset = () => {
    const next = defaultGridView(columns, defaultSort);
    setState({ key, view: next });
    try { if (key) localStorage.removeItem(key); } catch { /* 默认视图不依赖持久化。 */ }
    return next;
  };
  return { view, update, reset, storageKey: key };
}
