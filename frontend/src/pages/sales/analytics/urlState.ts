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
  SALES_GROUP_DIMS,
  SALES_GROUP_SORTS,
  type SalesAmountBasis,
  type SalesAnalyticsFilters,
  type SalesAnalyticsSortKey,
  type SalesGroupDim,
  type SalesGroupSort,
  type SalesTrendGranularity,
  type SortOrder,
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

/** 页签（D-44）：默认订单明细——沿用 D-41「打开即大单列表」与旧分享链。 */
export const SALES_TABS = ["overview", "compact", "groups", "trend", "orders"] as const;
export type SalesTab = (typeof SALES_TABS)[number];
export const DEFAULT_TAB: SalesTab = "orders";
export const DEFAULT_GROUP_BY: SalesGroupDim = "customer";
export const DEFAULT_GROUP_TOP = 20;
export const GROUP_TOP_OPTIONS = [10, 20, 50, 0] as const;
/** 多维紧凑图（2026-10-22）：每维 Top N 与共用指标。 */
export const COMPACT_TOP_OPTIONS = [10, 15, 20] as const;
export const DEFAULT_COMPACT_TOP = 10;
export const COMPACT_METRICS = [
  "amount_ex_tax", "amount_inc_tax", "revenue", "gross_profit", "orders",
] as const;
export type CompactMetric = (typeof COMPACT_METRICS)[number];
export const DEFAULT_COMPACT_METRIC: CompactMetric = "amount_ex_tax";
const AMOUNT_BASES: SalesAmountBasis[] = ["amount_ex_tax", "amount_inc_tax", "revenue"];
const GRANULARITIES: SalesTrendGranularity[] = ["day", "week", "month"];
/** 金额拖动条步长（D-44 §7：1k）；URL 中的金额按此取整。 */
export const AMOUNT_STEP = 1000;

export interface SalesAnalyticsUrlState {
  preset: DatePreset;
  from: string | null;
  to: string | null;
  businessType: string[];
  /** 旧版子串模糊（兼容旧链接，界面不再提供输入）。 */
  customer: string | null;
  salesperson: string | null;
  /** D-44：客户规范名 / 客户类型 / 销售人员 / 商品大类 / 品牌多选精确匹配。 */
  customers: string[];
  customerTypes: string[];
  salespeople: string[];
  categories: string[];
  brands: string[];
  /** 金额区间（D-44 §7）：口径 + 闭区间两端（null = 不限）+ 负金额 / 无金额开关。 */
  amountBasis: SalesAmountBasis;
  amountMin: number | null;
  amountMax: number | null;
  includeNegative: boolean;
  includeEmpty: boolean;
  /** 订单列表排序方向（D-44 §4）。 */
  order: SortOrder;
  tab: SalesTab;
  groupBy: SalesGroupDim;
  groupSort: SalesGroupSort;
  groupOrder: SortOrder;
  /** 0 = 全部。 */
  groupTop: number;
  /** 多维紧凑图：每维 Top N 与排名指标。 */
  compactTop: number;
  compactMetric: CompactMetric;
  /** null = 按区间长度自动推荐。 */
  granularity: SalesTrendGranularity | null;
  /** 对比上月（D-44 §9），默认开。 */
  compare: boolean;
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
    customers: parseUrlList(sp, "customer_name"),
    customerTypes: parseUrlList(sp, "customer_type"),
    salespeople: parseUrlList(sp, "salesperson_name"),
    categories: parseUrlList(sp, "category"),
    brands: parseUrlList(sp, "brand"),
    amountBasis: oneOf(sp.get("amount_basis"), AMOUNT_BASES, "amount_ex_tax"),
    amountMin: parseAmount(sp.get("amin")),
    amountMax: parseAmount(sp.get("amax")),
    includeNegative: sp.get("neg") !== "0",
    includeEmpty: sp.get("empty") !== "0",
    order: sp.get("order") === "asc" ? "asc" : "desc",
    tab: oneOf(sp.get("tab"), SALES_TABS, DEFAULT_TAB),
    groupBy: oneOf(sp.get("group_by"), SALES_GROUP_DIMS, DEFAULT_GROUP_BY),
    groupSort: oneOf(sp.get("gsort"), SALES_GROUP_SORTS, "amount_ex_tax"),
    groupOrder: sp.get("gorder") === "asc" ? "asc" : "desc",
    groupTop: oneOf(Number(sp.get("gtop") ?? DEFAULT_GROUP_TOP), GROUP_TOP_OPTIONS, DEFAULT_GROUP_TOP),
    compactTop: oneOf(Number(sp.get("ctop") ?? DEFAULT_COMPACT_TOP), COMPACT_TOP_OPTIONS, DEFAULT_COMPACT_TOP),
    compactMetric: oneOf(sp.get("cmetric"), COMPACT_METRICS, DEFAULT_COMPACT_METRIC),
    granularity: (GRANULARITIES as string[]).includes(sp.get("gran") ?? "")
      ? (sp.get("gran") as SalesTrendGranularity) : null,
    compare: sp.get("compare") !== "0",
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

function oneOf<T extends string | number>(raw: unknown, allowed: readonly T[], fallback: T): T {
  return (allowed as readonly unknown[]).includes(raw) ? (raw as T) : fallback;
}

/** 金额 URL 值：有限数字，按 1k 步长取整（D-44 §7）；非法 → null（不限）。 */
export function parseAmount(raw: string | null): number | null {
  if (raw == null || raw.trim() === "") return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  return Math.round(n / AMOUNT_STEP) * AMOUNT_STEP;
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
  if (next.customers !== undefined) out.customer_name = serializeUrlList(next.customers);
  if (next.customerTypes !== undefined) out.customer_type = serializeUrlList(next.customerTypes);
  if (next.salespeople !== undefined) out.salesperson_name = serializeUrlList(next.salespeople);
  if (next.categories !== undefined) out.category = serializeUrlList(next.categories);
  if (next.brands !== undefined) out.brand = serializeUrlList(next.brands);
  if (next.amountBasis !== undefined) {
    out.amount_basis = next.amountBasis === "amount_ex_tax" ? undefined : next.amountBasis;
  }
  if (next.amountMin !== undefined) {
    out.amin = next.amountMin == null ? undefined : String(next.amountMin);
  }
  if (next.amountMax !== undefined) {
    out.amax = next.amountMax == null ? undefined : String(next.amountMax);
  }
  if (next.includeNegative !== undefined) out.neg = next.includeNegative ? undefined : "0";
  if (next.includeEmpty !== undefined) out.empty = next.includeEmpty ? undefined : "0";
  if (next.order !== undefined) out.order = next.order === "desc" ? undefined : next.order;
  if (next.tab !== undefined) out.tab = next.tab === DEFAULT_TAB ? undefined : next.tab;
  if (next.groupBy !== undefined) {
    out.group_by = next.groupBy === DEFAULT_GROUP_BY ? undefined : next.groupBy;
  }
  if (next.groupSort !== undefined) {
    out.gsort = next.groupSort === "amount_ex_tax" ? undefined : next.groupSort;
  }
  if (next.groupOrder !== undefined) {
    out.gorder = next.groupOrder === "desc" ? undefined : next.groupOrder;
  }
  if (next.groupTop !== undefined) {
    out.gtop = next.groupTop === DEFAULT_GROUP_TOP ? undefined : String(next.groupTop);
  }
  if (next.compactTop !== undefined) {
    out.ctop = next.compactTop === DEFAULT_COMPACT_TOP ? undefined : String(next.compactTop);
  }
  if (next.compactMetric !== undefined) {
    out.cmetric = next.compactMetric === DEFAULT_COMPACT_METRIC ? undefined : next.compactMetric;
  }
  if (next.granularity !== undefined) out.gran = next.granularity ?? undefined;
  if (next.compare !== undefined) out.compare = next.compare ? undefined : "0";
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

const SALES_LIST_KEYS = [
  "business_type", "basis", "customer_name", "customer_type", "salesperson_name",
  "category", "brand",
];

/** 筛选/分页默认 replace；下钻导航显式 push；resetPage 回到第 1 页。 */
export function useSalesAnalyticsPatch(sp: URLSearchParams, setSp: SetURLSearchParams) {
  return (
    next: Partial<SalesAnalyticsUrlState>,
    opts?: AnalyticsPatchOptions,
  ) => {
    const patch = serializeSalesAnalyticsState(opts?.resetPage ? { ...next, page: 1 } : next);
    writeAnalyticsUrlPatch(sp, setSp, patch, opts, SALES_LIST_KEYS);
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
    ...(state.customers.length ? { customer_name: state.customers } : {}),
    ...(state.customerTypes.length ? { customer_type: state.customerTypes } : {}),
    ...(state.salespeople.length ? { salesperson_name: state.salespeople } : {}),
    ...(state.categories.length ? { category: state.categories } : {}),
    ...(state.brands.length ? { brand: state.brands } : {}),
    ...(state.amountBasis !== "amount_ex_tax" ? { amount_basis: state.amountBasis } : {}),
    ...(state.amountMin != null ? { amount_min: state.amountMin } : {}),
    ...(state.amountMax != null ? { amount_max: state.amountMax } : {}),
    ...(!state.includeNegative ? { include_negative: false } : {}),
    ...(!state.includeEmpty ? { include_empty: false } : {}),
  };
}
