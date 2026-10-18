// 销售分析页 URL 状态（D-41）：筛选、日期预设、三金额多选、排序键、含退货开关、
// 下钻抽屉全部写进 URL，可分享/可刷新/可后退。默认值不落 URL（零参数 = 今天 +
// 三金额并排 + 不含税金额倒序 + 不含退货）。
// 多值格式与旧链接兼容规则统一见 utils/analyticsUrlState。
import type { SetURLSearchParams } from "react-router-dom";
import type { Dayjs } from "dayjs";
import {
  resolveDateRange,
  type DatePreset,
  type DateRange,
} from "../../../components/DateRangePresets";
import { businessToday } from "../../../utils/date";
import {
  SALES_ANALYTICS_SORT_KEYS,
  type SalesAnalyticsFilters,
  type SalesAnalyticsSortKey,
} from "../../../api/salesAnalytics";

import {
  clampAnalyticsPage, parseAnalyticsPageSize,
  DEFAULT_ANALYTICS_PAGE_SIZE, parsePositiveId, parseUrlList, parseUrlSelection, serializeUrlList,
  parseAnalyticsDates, serializeAnalyticsDates, writeAnalyticsUrlPatch,
  type UrlPatch, type AnalyticsPatchOptions,
} from "../../../utils/analyticsUrlState";

/** 三金额口径键（D-41 §2）：与后端三金额/排序键一一对应。 */
export type AmountKey = "ex" | "inc" | "revenue";

export const AMOUNT_OPTIONS: { value: AmountKey; label: string }[] = [
  { value: "ex", label: "不含税金额" },
  { value: "inc", label: "含税订单金额" },
  { value: "revenue", label: "营收" },
];
export const ALL_AMOUNTS: AmountKey[] = ["ex", "inc", "revenue"];
/** D-41 §2「三项并存」：默认三项并排显示。 */
export const DEFAULT_AMOUNTS: AmountKey[] = [...ALL_AMOUNTS];

/** 口径键 → 后端排序键（列头点击按此排序，D-41：可按任一已显示金额倒序）。 */
export const AMOUNT_SORT_KEY: Record<AmountKey, SalesAnalyticsSortKey> = {
  ex: "amount_ex_tax",
  inc: "amount_inc_tax",
  revenue: "revenue",
};

/** 默认按不含税金额倒序（与后端 sort 缺省一致；amount_ex_tax 100% 有值）。 */
export const DEFAULT_SORT: SalesAnalyticsSortKey = "amount_ex_tax";

export interface SalesAnalyticsUrlState {
  preset: DatePreset;
  from: string | null;
  to: string | null;
  businessType: string[];
  customer: string | null;
  salesperson: string | null;
  amounts: AmountKey[];
  sort: SalesAnalyticsSortKey;
  page: number;
  pageSize: number;
  /** D-40 §5：含退货 = 已对账 + 退货已对账；默认不含。 */
  includeReturns: boolean;
  /** 非空 = 订单详情抽屉打开。 */
  orderId: number | null;
}

// ---------------------------------------------------------------- 解析（URL → 状态）

export { ANALYTICS_PAGE_MAX as SALES_ANALYTICS_PAGE_MAX } from "../../../utils/analyticsUrlState";

export function parseSalesAnalyticsUrl(sp: URLSearchParams): SalesAnalyticsUrlState {
  const sortRaw = sp.get("sort");
  const selected = parseUrlSelection(sp, "basis", ALL_AMOUNTS);
  return {
    ...parseAnalyticsDates(sp),
    businessType: parseUrlList(sp, "business_type"),
    customer: sp.get("customer") || null,
    salesperson: sp.get("salesperson") || null,
    amounts: selected,
    sort: (SALES_ANALYTICS_SORT_KEYS as readonly string[]).includes(sortRaw ?? "")
      ? (sortRaw as SalesAnalyticsSortKey)
      : DEFAULT_SORT,
    // 与后端 ANALYTICS_PAGE_MAX 同上界：分享链/手改 URL 的超大页码收敛到 10000，不发必 422 的请求
    page: clampAnalyticsPage(sp.get("page")),
    pageSize: parseAnalyticsPageSize(sp.get("page_size")),
    includeReturns: sp.get("include_returns") === "1",
    orderId: parsePositiveId(sp.get("order_id")),
  };
}

// ---------------------------------------------------------------- 序列化（状态 → URL）

function amountsIsDefault(amounts: AmountKey[]): boolean {
  return (
    amounts.length === DEFAULT_AMOUNTS.length &&
    ALL_AMOUNTS.every((a) => amounts.includes(a))
  );
}

/** 状态增量 → URL 增量；默认值转 undefined（useSalesAnalyticsPatch 语义：undefined = 删键）。 */
export function serializeSalesAnalyticsState(
  next: Partial<SalesAnalyticsUrlState>,
): UrlPatch {
  const out: UrlPatch = serializeAnalyticsDates(next);
  if (next.businessType !== undefined) {
    out.business_type = serializeUrlList(next.businessType);
  }
  if (next.customer !== undefined) {
    out.customer = next.customer || undefined;
  }
  if (next.salesperson !== undefined) {
    out.salesperson = next.salesperson || undefined;
  }
  if (next.amounts !== undefined) {
    out.basis = amountsIsDefault(next.amounts) ? undefined : serializeUrlList(next.amounts);
  }
  if (next.sort !== undefined) {
    out.sort = next.sort === DEFAULT_SORT ? undefined : next.sort;
  }
  if (next.page !== undefined) {
    const page = clampAnalyticsPage(next.page);
    out.page = page > 1 ? String(page) : undefined;
  }
  if (next.includeReturns !== undefined) {
    out.include_returns = next.includeReturns ? "1" : undefined;
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
export function useSalesAnalyticsPatch(sp: URLSearchParams, setSp: SetURLSearchParams) {
  return (
    next: Partial<SalesAnalyticsUrlState>,
    opts?: AnalyticsPatchOptions,
  ) => {
    const patch = serializeSalesAnalyticsState(opts?.resetPage ? { ...next, page: 1 } : next);
    writeAnalyticsUrlPatch(sp, setSp, patch, opts, ["business_type", "basis"]);
  };
}

// ---------------------------------------------------------------- 请求参数（状态 → API）

/** 日期部分：「全部」不传日期；自定义非法回退今天（与组件内展示口径一致）。 */
export function resolveSalesRange(
  state: SalesAnalyticsUrlState,
  today: Dayjs,
): DateRange | null {
  return resolveDateRange(state.preset, state.from, state.to, today);
}

export function toSalesApiFilters(
  state: SalesAnalyticsUrlState,
  today: Dayjs = businessToday(),
): SalesAnalyticsFilters {
  const range = resolveSalesRange(state, today);
  // 客户/销售人员是 ILIKE 子串匹配：首尾空白剔除后才发（纯空白视为未筛），
  // 避免 "?customer=%20" 这类脏值静默进请求。
  const customer = state.customer?.trim() || undefined;
  const salesperson = state.salesperson?.trim() || undefined;
  return {
    ...(range ? { date_from: range.from, date_to: range.to } : {}),
    ...(state.businessType.length ? { business_type: state.businessType } : {}),
    ...(customer ? { customer } : {}),
    ...(salesperson ? { salesperson } : {}),
  };
}
