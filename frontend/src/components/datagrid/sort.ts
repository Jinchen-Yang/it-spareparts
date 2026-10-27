import type { DataGridColumn, DataGridSort, DataGridSortType } from "./types";
import { columnValue } from "./types";

const codeCollator = new Intl.Collator("en", { numeric: true });
const chineseCollator = new Intl.Collator("zh-Hans-CN-u-co-pinyin");

function normalized(value: unknown, type: DataGridSortType, rank?: DataGridColumn<unknown>["rank"]): string | number | null {
  if (value == null || typeof value === "string" && !value.trim()) return null;
  if (type === "number" || type === "money") {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }
  if (type === "date" || type === "time") {
    const time = value instanceof Date ? value.getTime() : typeof value === "number" ? value : Date.parse(String(value));
    return Number.isFinite(time) ? time : null;
  }
  if (type === "rank") {
    const number = Array.isArray(rank) ? rank.indexOf(String(value)) : rank ? rank[String(value)] : Number(value);
    return number != null && Number.isFinite(number) && number >= 0 ? number : null;
  }
  return String(value);
}

/** 空值先独立处理，保证降序时仍在末尾。 */
export function compareGridValues(a: unknown, b: unknown, type: DataGridSortType, dir: "asc" | "desc" = "asc", rank?: DataGridColumn<unknown>["rank"]) {
  const left = normalized(a, type, rank);
  const right = normalized(b, type, rank);
  if (left == null || right == null) return left == null ? right == null ? 0 : 1 : -1;
  const compared = type === "code" ? codeCollator.compare(String(left), String(right))
    : type === "zh" ? chineseCollator.compare(String(left), String(right)) : Number(left) - Number(right);
  return dir === "asc" ? compared : -compared;
}

export function sortGridRows<T>(rows: T[], columns: DataGridColumn<T>[], sort: DataGridSort | null) {
  const column = sort && columns.find((item) => item.key === sort.key);
  if (!sort || !column) return rows;
  return rows.map((row, index) => ({ row, index })).sort((a, b) =>
    compareGridValues(columnValue(column, a.row), columnValue(column, b.row), column.type ?? "zh", sort.dir, column.rank)
    || a.index - b.index).map(({ row }) => row);
}
