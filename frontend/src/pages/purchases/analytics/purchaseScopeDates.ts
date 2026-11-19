import dayjs from "dayjs";
import type { PurchaseAnalyticsFilters, PurchaseScope } from "../../../api/purchaseAnalytics";

/** Convert drilled time buckets to a concrete intersected period before an adjacent-period comparison. */
export function resolveTimeScope(scope: PurchaseScope, filters: PurchaseAnalyticsFilters) {
  const rest = { ...scope };
  let from = filters.date_from,
    to = filters.date_to;
  let hasBucket = false;
  for (const dimension of ["day", "week", "month"] as const) {
    if (!(dimension in rest)) continue;
    hasBucket = true;
    const key = rest[dimension];
    if (!key) return null;
    const start = dayjs(dimension === "month" ? key + "-01" : key);
    if (!start.isValid() || start.format(dimension === "month" ? "YYYY-MM" : "YYYY-MM-DD") !== key)
      return null;
    const end =
      dimension === "week" ? start.add(6, "day") : dimension === "month" ? start.endOf("month") : start;
    const lo = start.format("YYYY-MM-DD"),
      hi = end.format("YYYY-MM-DD");
    from = from && from > lo ? from : lo;
    to = to && to < hi ? to : hi;
    delete rest[dimension];
  }
  return from && to && from > to ? null : { scope: rest, from, to, hasBucket };
}
