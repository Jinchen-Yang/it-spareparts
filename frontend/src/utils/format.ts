// 显示格式化工具单一真值源（此前 money 在 4 个页面、pct 在 3 个页面各复制一份）。
// 架构体检 2026-06-29 F2。pct 精度用 digits 参数保留各调用点既有行为。

export const EMPTY = "-";

export const money = (v: number | null | undefined): string =>
  v == null ? EMPTY : `¥${v.toLocaleString()}`;

export const pct = (v: number | null | undefined, digits = 1): string =>
  v == null ? EMPTY : `${(v * 100).toFixed(digits)}%`;
