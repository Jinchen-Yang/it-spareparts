import type { Key, ReactNode } from "react";

export type DataGridSortType = "code" | "zh" | "number" | "money" | "date" | "time" | "rank";
export interface DataGridSort { key: string; dir: "asc" | "desc" }

export interface DataGridColumn<T> {
  key: string;
  title: string;
  width?: number;
  minWidth?: number;
  maxWidth?: number;
  type?: DataGridSortType;
  value?: (row: T) => unknown;
  render?: (row: T) => ReactNode;
  sortable?: boolean;
  identity?: boolean;
  hidden?: boolean;
  rank?: string[] | Record<string, number>;
  align?: "left" | "center" | "right";
}

export interface DataGridProps<T> {
  id: string;
  accountId?: string | number;
  columns: DataGridColumn<T>[];
  rows: T[];
  rowKey: (row: T) => Key;
  loading?: boolean;
  error?: ReactNode;
  onRetry?: () => void;
  emptyText?: ReactNode;
  renderExpanded?: (row: T) => ReactNode;
  expandedRowKeys?: Key[];
  defaultExpandedRowKeys?: Key[];
  onExpandedRowKeysChange?: (keys: Key[]) => void;
  defaultSort?: DataGridSort | null;
  sort?: DataGridSort | null;
  onSortChange?: (sort: DataGridSort | null) => void;
  serverSort?: boolean;
  toolbarExtra?: ReactNode;
  className?: string;
  ariaLabel?: string;
}

export interface DataGridView {
  widths: Record<string, number>;
  hidden: string[];
  sort: DataGridSort | null;
}

export function columnMinWidth<T>(column: DataGridColumn<T>) {
  // 缩略图、展开之外的工具列不参与业务字段类型约束。
  if (!column.type && column.minWidth != null) return Math.min(800, Math.max(24, column.minWidth));
  const minimum = column.type === "code" ? 80
    : column.type === "number" || column.type === "money" ? 56 : 96;
  return Math.min(800, Math.max(minimum, column.minWidth ?? minimum));
}

export function clampColumnWidth<T>(column: DataGridColumn<T>, width: number) {
  const minimum = columnMinWidth(column);
  return Math.round(Math.max(minimum, Math.min(Math.max(minimum, Math.min(800, column.maxWidth ?? 800)), width)));
}

export function columnValue<T>(column: DataGridColumn<T>, row: T): unknown {
  return column.value ? column.value(row) : (row as Record<string, unknown>)[column.key];
}
