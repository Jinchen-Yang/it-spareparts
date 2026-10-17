// 采购分析页 URL 状态（D-39）：筛选、日期预设、口径多选、group_by、下钻层全部写进
// URL，可分享/可刷新/可后退。默认值不落 URL（零参数 = 今天 + 采购类型分组 + 三口径并排）。
// 多选值用逗号连接（取值来自 source_type_raw / 采购员 / 分类文本，均不含 ASCII 逗号）。
import type { SetURLSearchParams } from "react-router-dom";
import type { Dayjs } from "dayjs";
import {
  DEFAULT_DATE_PRESET,
  isDatePreset,
  resolveDateRange,
  type DatePreset,
  type DateRange,
} from "../../../components/DateRangePresets";
import type { PurchaseAnalyticsFilters } from "../../../api/purchaseAnalytics";
import { businessToday } from "../../../utils/date";

export type AmountBasis = "inc" | "ex" | "paid";

export const AMOUNT_BASIS_OPTIONS: { value: AmountBasis; label: string }[] = [
  { value: "inc", label: "含税" },
  { value: "ex", label: "不含税" },
  { value: "paid", label: "实付" },
];
export const ALL_BASES: AmountBasis[] = ["inc", "ex", "paid"];
/** Q7：三口径并存，默认三项并排显示。 */
export const DEFAULT_BASES: AmountBasis[] = [...ALL_BASES];

export const GROUP_BY_OPTIONS = [
  { value: "source_type", label: "采购类型" },
  { value: "purchaser", label: "采购员" },
  { value: "salesperson", label: "销售人员" },
  { value: "customer", label: "客户" },
  { value: "category", label: "分类" },
  { value: "month", label: "按月" },
  { value: "day", label: "按日" },
];
export const GROUP_BY_KEYS = GROUP_BY_OPTIONS.map((o) => o.value);
export const DEFAULT_GROUP_BY = "source_type";

export const SORT_KEYS = ["order_date", "amount_inc", "amount_ex", "amount_paid"] as const;
export type AnalyticsSort = (typeof SORT_KEYS)[number];
export const DEFAULT_SORT: AnalyticsSort = "order_date";

export interface AnalyticsUrlState {
  preset: DatePreset;
  from: string | null;
  to: string | null;
  sourceType: string[];
  purchaser: string[];
  salesperson: string[];
  customerIds: number[];
  category: string[];
  groupBy: string;
  /** 非空 = 分组下钻层；值是分组键（文本 / 客户 id 字符串 / 哨兵值）。 */
  groupKey: string | null;
  sort: AnalyticsSort;
  dir: "asc" | "desc";
  page: number;
  basis: AmountBasis[];
  /** 非空 = 订单明细抽屉打开。 */
  orderId: number | null;
}

// ---------------------------------------------------------------- 解析（URL → 状态）

function splitList(raw: string | null): string[] {
  if (!raw) return [];
  // 不 trim：筛选值要与 source_type_raw/采购员等原值精确匹配（D-39），
  // 库里首尾带空格的取值一旦被静默 trim 就会精确匹配失败（0 行）。
  // 只剔除空串（连续逗号/尾逗号）。
  return Array.from(new Set(raw.split(",").filter(Boolean)));
}

function parseBasis(raw: string | null): AmountBasis[] {
  const valid = new Set<string>(ALL_BASES);
  const parsed = splitList(raw).filter((v) => valid.has(v)) as AmountBasis[];
  return parsed.length ? parsed : [...DEFAULT_BASES];
}

export function parseAnalyticsUrl(sp: URLSearchParams): AnalyticsUrlState {
  const presetRaw = sp.get("preset");
  const from = sp.get("from");
  const to = sp.get("to");
  const groupByRaw = sp.get("group_by");
  const sortRaw = sp.get("sort");
  const pageRaw = Number.parseInt(sp.get("page") || "", 10);
  const orderIdRaw = Number.parseInt(sp.get("order_id") || "", 10);
  return {
    preset: isDatePreset(presetRaw) ? presetRaw : DEFAULT_DATE_PRESET,
    from: from && to ? from : null, // 只有两个都在时才有意义（单边视为残缺）
    to: from && to ? to : null,
    sourceType: splitList(sp.get("source_type")),
    purchaser: splitList(sp.get("purchaser")),
    salesperson: splitList(sp.get("salesperson")),
    // 解析成 id 后再去重（"12" 与 "12.5" 字符串不同、解析后同 id）
    customerIds: Array.from(new Set(
      splitList(sp.get("customer"))
        .map((v) => Number.parseInt(v, 10))
        .filter((n) => Number.isInteger(n) && n > 0),
    )),
    category: splitList(sp.get("category")),
    groupBy: GROUP_BY_KEYS.includes(groupByRaw ?? "") ? groupByRaw! : DEFAULT_GROUP_BY,
    groupKey: sp.get("group_key") || null,
    sort: (SORT_KEYS as readonly string[]).includes(sortRaw ?? "")
      ? (sortRaw as AnalyticsSort)
      : DEFAULT_SORT,
    dir: sp.get("dir") === "asc" ? "asc" : "desc",
    page: Number.isInteger(pageRaw) && pageRaw > 1 ? pageRaw : 1,
    basis: parseBasis(sp.get("basis")),
    orderId: Number.isInteger(orderIdRaw) && orderIdRaw > 0 ? orderIdRaw : null,
  };
}

// ---------------------------------------------------------------- 序列化（状态 → URL）

function basisIsDefault(basis: AmountBasis[]): boolean {
  return (
    basis.length === DEFAULT_BASES.length && ALL_BASES.every((b) => basis.includes(b))
  );
}

/** 状态增量 → URL 增量；默认值转 undefined（useUrlPatch 语义：undefined = 删键）。 */
export function serializeAnalyticsState(
  next: Partial<AnalyticsUrlState>,
): Record<string, string | undefined> {
  const out: Record<string, string | undefined> = {};
  if (next.preset !== undefined) {
    out.preset = next.preset === DEFAULT_DATE_PRESET ? undefined : next.preset;
    if (next.preset === "custom") {
      out.from = next.from || undefined;
      out.to = next.to || undefined;
    } else {
      // 非自定义预设：残留的 from/to 一并清掉
      out.from = undefined;
      out.to = undefined;
    }
  }
  if (next.sourceType !== undefined) {
    out.source_type = next.sourceType.length ? next.sourceType.join(",") : undefined;
  }
  if (next.purchaser !== undefined) {
    out.purchaser = next.purchaser.length ? next.purchaser.join(",") : undefined;
  }
  if (next.salesperson !== undefined) {
    out.salesperson = next.salesperson.length ? next.salesperson.join(",") : undefined;
  }
  if (next.customerIds !== undefined) {
    out.customer = next.customerIds.length ? next.customerIds.join(",") : undefined;
  }
  if (next.category !== undefined) {
    out.category = next.category.length ? next.category.join(",") : undefined;
  }
  if (next.groupBy !== undefined) {
    out.group_by = next.groupBy === DEFAULT_GROUP_BY ? undefined : next.groupBy;
  }
  if (next.groupKey !== undefined) {
    out.group_key = next.groupKey || undefined;
  }
  if (next.sort !== undefined) {
    out.sort = next.sort === DEFAULT_SORT ? undefined : next.sort;
  }
  if (next.dir !== undefined) {
    out.dir = next.dir === "desc" ? undefined : next.dir;
  }
  if (next.page !== undefined) {
    out.page = next.page > 1 ? String(next.page) : undefined;
  }
  if (next.basis !== undefined) {
    out.basis = basisIsDefault(next.basis) ? undefined : next.basis.join(",");
  }
  if (next.orderId !== undefined) {
    out.order_id = next.orderId != null ? String(next.orderId) : undefined;
  }
  return out;
}

/** URL 增量更新（replace 不堆历史）；resetPage 供筛选变化时回到第 1 页。 */
export function useAnalyticsPatch(sp: URLSearchParams, setSp: SetURLSearchParams) {
  return (
    next: Partial<AnalyticsUrlState>,
    opts?: { resetPage?: boolean },
  ) => {
    const merged = new URLSearchParams(sp);
    const patch = serializeAnalyticsState(
      opts?.resetPage ? { ...next, page: 1 } : next,
    );
    for (const [k, v] of Object.entries(patch)) {
      if (v === undefined) merged.delete(k);
      else merged.set(k, v);
    }
    setSp(merged, { replace: true });
  };
}

// ---------------------------------------------------------------- 请求参数（状态 → API）

/** 日期部分：「全部」不传日期；自定义非法回退今天（与组件内展示口径一致）。 */
export function resolveAnalyticsRange(state: AnalyticsUrlState, today: Dayjs): DateRange | null {
  return resolveDateRange(state.preset, state.from, state.to, today);
}

export function toAnalyticsApiFilters(
  state: AnalyticsUrlState,
  today: Dayjs = businessToday(),
): PurchaseAnalyticsFilters {
  const range = resolveAnalyticsRange(state, today);
  return {
    ...(range ? { date_from: range.from, date_to: range.to } : {}),
    ...(state.sourceType.length ? { source_type: state.sourceType } : {}),
    ...(state.purchaser.length ? { purchaser: state.purchaser } : {}),
    ...(state.salesperson.length ? { salesperson: state.salesperson } : {}),
    ...(state.customerIds.length ? { customer_id: state.customerIds } : {}),
    ...(state.category.length ? { category: state.category } : {}),
  };
}
