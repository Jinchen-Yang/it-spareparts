export interface PoolAnalysisNavigationContext {
  groupId: number;
  range: string;
  dateFrom?: string | null;
  dateTo?: string | null;
  side: "purchase" | "sales";
  purchaseType?: string | null;
  employee?: string | null;
  priceSort?: string | null;
  priceOrder?: string | null;
}

function setIfPresent(query: URLSearchParams, key: string, value?: string | null) {
  if (value != null && value !== "") query.set(key, value);
}

/** 型号全景深链：型号身份与池分析来源上下文同行，刷新和分享都不丢返回条件。 */
export function poolAnalysisPartPath(partId: number, context: PoolAnalysisNavigationContext): string {
  const query = new URLSearchParams({
    part_id: String(partId),
    group_id: String(context.groupId),
    range: context.range,
    side: context.side,
  });
  setIfPresent(query, "date_from", context.dateFrom);
  setIfPresent(query, "date_to", context.dateTo);
  setIfPresent(query, "purchase_type", context.purchaseType);
  setIfPresent(query, "employee", context.employee);
  setIfPresent(query, "price_sort", context.priceSort);
  setIfPresent(query, "price_order", context.priceOrder);
  return `/parts?${query.toString()}`;
}

/** 型号页识别池分析来源，恢复成池详情页自身使用的 from/to 查询格式。 */
export function poolAnalysisReturnPath(query: URLSearchParams): string | null {
  const groupId = Number(query.get("group_id"));
  if (!Number.isInteger(groupId) || groupId <= 0) return null;

  const result = new URLSearchParams();
  const range = query.get("range");
  const dateFrom = query.get("date_from");
  const dateTo = query.get("date_to");
  if (dateFrom && dateTo) {
    result.set("range", "custom");
    result.set("from", dateFrom);
    result.set("to", dateTo);
  } else if (range && range !== "custom") {
    result.set("range", range);
  }
  for (const key of ["side", "purchase_type", "employee", "price_sort", "price_order"]) {
    setIfPresent(result, key, query.get(key));
  }
  return `/pool-analysis/${groupId}${result.size ? `?${result.toString()}` : ""}`;
}
