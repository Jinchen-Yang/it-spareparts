// 采购分析页 URL 状态（D-39）：筛选、日期预设、口径多选、group_by、下钻层全部写进
// URL，可分享/可刷新/可后退。默认值不落 URL（零参数 = 今天 + 采购类型分组 + 三口径并排）。
// 多值格式与旧链接兼容规则统一见 utils/analyticsUrlState。
import type { SetURLSearchParams } from "react-router-dom";
import type { Dayjs } from "dayjs";
import {
  resolveDateRange,
  type DatePreset,
  type DateRange,
} from "../../../components/DateRangePresets";
import type { PurchaseAnalyticsFilters } from "../../../api/purchaseAnalytics";
import { businessToday } from "../../../utils/date";

import {
  clampAnalyticsPage, parseAnalyticsPageSize,
  DEFAULT_ANALYTICS_PAGE_SIZE, parsePositiveId, parseUrlList, parseUrlSelection, serializeUrlList,
  parseAnalyticsDates, serializeAnalyticsDates, writeAnalyticsUrlPatch,
  type UrlPatch, type AnalyticsPatchOptions,
} from "../../../utils/analyticsUrlState";

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
  pageSize: number;
  basis: AmountBasis[];
  /** 非空 = 订单明细抽屉打开。 */
  orderId: number | null;
}

// ---------------------------------------------------------------- 解析（URL → 状态）

export function parseAnalyticsUrl(sp: URLSearchParams): AnalyticsUrlState {
  const groupByRaw = sp.get("group_by");
  const sortRaw = sp.get("sort");
  const selected = parseUrlSelection(sp, "basis", ALL_BASES);
  return {
    ...parseAnalyticsDates(sp),
    sourceType: parseUrlList(sp, "source_type"),
    purchaser: parseUrlList(sp, "purchaser"),
    salesperson: parseUrlList(sp, "salesperson"),
    // 严格整数校验后去重，拒绝截断小数或混入文本的 ID。
    customerIds: Array.from(new Set(
      parseUrlList(sp, "customer")
        .map(parsePositiveId)
        .filter((n): n is number => n !== null),
    )),
    category: parseUrlList(sp, "category"),
    groupBy: GROUP_BY_KEYS.includes(groupByRaw ?? "") ? groupByRaw! : DEFAULT_GROUP_BY,
    groupKey: sp.get("group_key") || null,
    sort: (SORT_KEYS as readonly string[]).includes(sortRaw ?? "")
      ? (sortRaw as AnalyticsSort)
      : DEFAULT_SORT,
    dir: sp.get("dir") === "asc" ? "asc" : "desc",
    page: clampAnalyticsPage(sp.get("page")),
    pageSize: parseAnalyticsPageSize(sp.get("page_size")),
    basis: selected,
    orderId: parsePositiveId(sp.get("order_id")),
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
): UrlPatch {
  const out: UrlPatch = serializeAnalyticsDates(next);
  if (next.sourceType !== undefined) {
    out.source_type = serializeUrlList(next.sourceType);
  }
  if (next.purchaser !== undefined) {
    out.purchaser = serializeUrlList(next.purchaser);
  }
  if (next.salesperson !== undefined) {
    out.salesperson = serializeUrlList(next.salesperson);
  }
  if (next.customerIds !== undefined) {
    out.customer = serializeUrlList(next.customerIds);
  }
  if (next.category !== undefined) {
    out.category = serializeUrlList(next.category);
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
    const page = clampAnalyticsPage(next.page);
    out.page = page > 1 ? String(page) : undefined;
  }
  if (next.basis !== undefined) {
    out.basis = basisIsDefault(next.basis) ? undefined : serializeUrlList(next.basis);
  }
  if (next.orderId !== undefined) {
    out.order_id = next.orderId != null ? String(next.orderId) : undefined;
  }
  if (next.pageSize !== undefined) {
    const size = parseAnalyticsPageSize(next.pageSize);
    out.page_size = size === DEFAULT_ANALYTICS_PAGE_SIZE ? undefined : String(size);
  }
  if (Object.values(out).some(Array.isArray)) out.url_version = "2";
  return out;
}

/** 筛选/分页默认 replace；下钻导航显式 push；resetPage 回到第 1 页。 */
export function useAnalyticsPatch(sp: URLSearchParams, setSp: SetURLSearchParams) {
  return (
    next: Partial<AnalyticsUrlState>,
    opts?: AnalyticsPatchOptions,
  ) => {
    const patch = serializeAnalyticsState(opts?.resetPage ? { ...next, page: 1 } : next);
    writeAnalyticsUrlPatch(sp, setSp, patch, opts, ["source_type", "purchaser", "salesperson", "customer", "category", "basis"]);
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
