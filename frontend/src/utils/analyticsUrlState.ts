/** 采购/销售分析 URL 的公共契约。单值重复键采用首值；patch 保留未知参数。
 * 旧链接没有版本标记，逗号继续作为分隔符（历史上无法表达含逗号的单个值）。
 * 新链接 url_version=2 使用重复键，值中的逗号原样保留，避免单选时产生歧义。
 */
import type { SetURLSearchParams } from "react-router-dom";
import { DEFAULT_DATE_PRESET, isDatePreset } from "../components/DateRangePresets";

/** 与 backend/app/api/query_bounds.py 的分析分页上界一致。 */
export const ANALYTICS_PAGE_MAX = 10_000;
export const ANALYTICS_PAGE_SIZES = [20, 50, 100, 200] as const;
export const DEFAULT_ANALYTICS_PAGE_SIZE = 50;
export type UrlPatch = Record<string, string | string[] | undefined>;
export type AnalyticsPatchOptions = { resetPage?: boolean; history?: "push" | "replace" };

export function parsePositiveId(raw: string | null): number | null {
  if (!raw || !/^\d+$/.test(raw)) return null;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value > 0 && value <= 2_147_483_647 ? value : null;
}

export function clampAnalyticsPage(raw: string | number | null): number {
  const text = String(raw ?? "");
  if (!/^\d+$/.test(text)) return 1;
  const value = Number(text);
  return Math.max(1, Math.min(ANALYTICS_PAGE_MAX, value));
}

export function parseAnalyticsPageSize(raw: string | number | null): number {
  const text = String(raw ?? "");
  const value = /^\d+$/.test(text) ? Number(text) : NaN;
  return (ANALYTICS_PAGE_SIZES as readonly number[]).includes(value)
    ? value : DEFAULT_ANALYTICS_PAGE_SIZE;
}

export function parseUrlList(sp: URLSearchParams, key: string): string[] {
  // 精确匹配筛选不能 trim；首尾空格是源取值的一部分，仅去空串/重复值。
  const values = sp.getAll(key);
  return [...new Set((sp.get("url_version") === "2" ? values : values.flatMap(v => v.split(","))).filter(Boolean))];
}

/** 全选统一为默认顺序，避免全选不同顺序经省略默认键后失去 round-trip 恒等。 */
export function parseUrlSelection<T extends string>(sp: URLSearchParams, key: string, defaults: readonly T[]): T[] {
  const values = parseUrlList(sp, key).filter((value): value is T => defaults.includes(value as T));
  return !values.length || values.length === defaults.length ? [...defaults] : values;
}

export function serializeUrlList(values: (string | number)[]): string[] | undefined {
  const unique = [...new Set(values.map(String).filter(Boolean))];
  return unique.length ? unique : undefined;
}

export function parseAnalyticsDates(sp: URLSearchParams) {
  const preset = sp.get("preset");
  const from = sp.get("from");
  const to = sp.get("to");
  return {
    preset: isDatePreset(preset) ? preset : DEFAULT_DATE_PRESET,
    from: preset === "custom" && from && to ? from : null,
    to: preset === "custom" && from && to ? to : null,
  };
}

export function serializeAnalyticsDates(next: Partial<ReturnType<typeof parseAnalyticsDates>>): UrlPatch {
  if (next.preset === undefined) return {};
  return {
    preset: next.preset === DEFAULT_DATE_PRESET ? undefined : next.preset,
    from: next.preset === "custom" ? next.from || undefined : undefined,
    to: next.preset === "custom" ? next.to || undefined : undefined,
  };
}

/** 全量序列化只写已知键；应用增量时保留未知键，避免破坏其他导航参数。 */
export function applyAnalyticsUrlPatch(sp: URLSearchParams, patch: UrlPatch, listKeys: readonly string[]): URLSearchParams {
  const merged = new URLSearchParams(sp);
  // 升级旧链接时连同未修改的旧多选一起规范化，避免加版本后把旧逗号列表当单值。
  for (const key of listKeys) {
    if (!sp.has(key) || key in patch) continue;
    const values = parseUrlList(sp, key);
    merged.delete(key);
    values.forEach(value => merged.append(key, value));
  }
  for (const [key, value] of Object.entries(patch)) {
    merged.delete(key);
    if (Array.isArray(value)) value.forEach(item => merged.append(key, item));
    else if (value !== undefined) merged.set(key, value);
  }
  if (listKeys.some(key => merged.has(key))) merged.set("url_version", "2");
  else merged.delete("url_version");
  return merged;
}

export function writeAnalyticsUrlPatch(sp: URLSearchParams, setSp: SetURLSearchParams, patch: UrlPatch, opts: AnalyticsPatchOptions | undefined, listKeys: readonly string[]) {
  setSp(applyAnalyticsUrlPatch(sp, patch, listKeys), { replace: opts?.history !== "push" });
}
