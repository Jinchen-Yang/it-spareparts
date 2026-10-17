// 销售分析页 URL 状态（D-41）：筛选、日期预设、三金额多选、排序键、含退货开关、
// 下钻抽屉全部写进 URL，可分享/可刷新/可后退。默认值不落 URL（零参数 = 今天 +
// 三金额并排 + 不含税金额倒序 + 不含退货）。
// 多选值用逗号连接（取值来自 business_type 原值，不含 ASCII 逗号）。
import type { SetURLSearchParams } from "react-router-dom";
import type { Dayjs } from "dayjs";
import {
  DEFAULT_DATE_PRESET,
  isDatePreset,
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
  /** D-40 §5：含退货 = 已对账 + 退货已对账；默认不含。 */
  includeReturns: boolean;
  /** 非空 = 订单详情抽屉打开。 */
  orderId: number | null;
}

// ---------------------------------------------------------------- 解析（URL → 状态）

function splitList(raw: string | null): string[] {
  if (!raw) return [];
  // 不 trim：ETL 落库时已 clean_str（首尾空白不可能存在于 business_type 原值），
  // 后端对筛选值也统一 strip 后精确匹配——这里保持原样只为 URL/选项值往返保真
  // （选项值来自 /options 原值分组，静默 trim 会让 URL 与选项显示不一致）。只剔除空串。
  return Array.from(new Set(raw.split(",").filter(Boolean)));
}

function parseAmounts(raw: string | null): AmountKey[] {
  const valid = new Set<string>(ALL_AMOUNTS);
  const parsed = splitList(raw).filter((v) => valid.has(v)) as AmountKey[];
  return parsed.length ? parsed : [...DEFAULT_AMOUNTS];
}

export function parseSalesAnalyticsUrl(sp: URLSearchParams): SalesAnalyticsUrlState {
  const presetRaw = sp.get("preset");
  const from = sp.get("from");
  const to = sp.get("to");
  const sortRaw = sp.get("sort");
  const pageRaw = Number.parseInt(sp.get("page") || "", 10);
  const orderIdRaw = Number.parseInt(sp.get("order_id") || "", 10);
  return {
    preset: isDatePreset(presetRaw) ? presetRaw : DEFAULT_DATE_PRESET,
    from: from && to ? from : null, // 只有两个都在时才有意义（单边视为残缺）
    to: from && to ? to : null,
    businessType: splitList(sp.get("business_type")),
    customer: sp.get("customer") || null,
    salesperson: sp.get("salesperson") || null,
    amounts: parseAmounts(sp.get("basis")),
    sort: (SALES_ANALYTICS_SORT_KEYS as readonly string[]).includes(sortRaw ?? "")
      ? (sortRaw as SalesAnalyticsSortKey)
      : DEFAULT_SORT,
    page: Number.isInteger(pageRaw) && pageRaw > 1 ? pageRaw : 1,
    includeReturns: sp.get("include_returns") === "1",
    orderId: Number.isInteger(orderIdRaw) && orderIdRaw > 0 ? orderIdRaw : null,
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
  if (next.businessType !== undefined) {
    out.business_type = next.businessType.length
      ? next.businessType.join(",")
      : undefined;
  }
  if (next.customer !== undefined) {
    out.customer = next.customer || undefined;
  }
  if (next.salesperson !== undefined) {
    out.salesperson = next.salesperson || undefined;
  }
  if (next.amounts !== undefined) {
    out.basis = amountsIsDefault(next.amounts) ? undefined : next.amounts.join(",");
  }
  if (next.sort !== undefined) {
    out.sort = next.sort === DEFAULT_SORT ? undefined : next.sort;
  }
  if (next.page !== undefined) {
    out.page = next.page > 1 ? String(next.page) : undefined;
  }
  if (next.includeReturns !== undefined) {
    out.include_returns = next.includeReturns ? "1" : undefined;
  }
  if (next.orderId !== undefined) {
    out.order_id = next.orderId != null ? String(next.orderId) : undefined;
  }
  return out;
}

/** URL 增量更新（replace 不堆历史）；resetPage 供筛选变化时回到第 1 页。 */
export function useSalesAnalyticsPatch(sp: URLSearchParams, setSp: SetURLSearchParams) {
  return (
    next: Partial<SalesAnalyticsUrlState>,
    opts?: { resetPage?: boolean },
  ) => {
    const merged = new URLSearchParams(sp);
    const patch = serializeSalesAnalyticsState(
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
