/**
 * 老板经营看板 v2 共用层：全局筛选 URL 编解码、竞态守卫取数 hook、
 * 权限三态渲染（无权限 ≠ 暂无数据 ≠ 未设置）、价格参考状态标签。
 *
 * URL 是筛选的唯一真值源：刷新/复制链接/前进后退都从 URL 重放。
 * 筛选变化 push 进历史（后退可恢复），展示偏好（粒度/成本法）replace 不堆历史。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { Tag, Tooltip } from "antd";
import dayjs from "dayjs";
import type { ReferenceStatus } from "../../api";
import {
  ISO_DATE_FORMAT, isStrictIsoDate, strictIsoDateOrNull, strictIsoDateRange,
} from "../../utils/date";
import { EMPTY, moneyExact } from "../../utils/format";

// ---------------------------------------------------------------- 全局筛选

export type RangeKey = "today" | "7d" | "30d" | "month" | "custom";

export interface BoardFilters {
  rangeKey: RangeKey;
  /** 仅 custom 生效 */
  from: string | null;
  to: string | null;
  partId: number | null;
  /** part_id 的展示标签（PartPicker 回填用，纯展示不回传后端） */
  partPn: string | null;
  poolId: number | null;
  salesperson: string | null;
  purchaser: string | null;
  /** 趋势图点击下钻：订单块的日期覆盖窗口 */
  drillFrom: string | null;
  drillTo: string | null;
  granularity: "day" | "week" | "month";
  costMethod: "moving_avg" | "fifo";
}

export interface DateRange { date_from?: string; date_to?: string }

const D = ISO_DATE_FORMAT;

/** rangeKey → 实际统计窗口（custom 缺参时退回 30d，绝不产出半开窗口） */
export function rangeToDates(key: RangeKey, from: string | null, to: string | null): DateRange {
  const today = dayjs();
  if (key === "today") return { date_from: today.format(D), date_to: today.format(D) };
  if (key === "7d") return { date_from: today.subtract(6, "day").format(D), date_to: today.format(D) };
  if (key === "month") return { date_from: today.startOf("month").format(D), date_to: today.format(D) };
  const custom = strictIsoDateRange(from, to);
  if (key === "custom" && custom) {
    return { date_from: custom.from, date_to: custom.to };
  }
  return { date_from: today.subtract(29, "day").format(D), date_to: today.format(D) };
}

const RANGE_KEYS: RangeKey[] = ["today", "7d", "30d", "month", "custom"];

function readIntParam(sp: URLSearchParams, key: string): number | null {
  const raw = sp.get(key);
  if (raw == null) return null;
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 ? n : null;
}

function readDateParam(sp: URLSearchParams, key: string): string | null {
  return strictIsoDateOrNull(sp.get(key));
}

export function useBoardFilters() {
  const [sp, setSp] = useSearchParams();

  const filters: BoardFilters = useMemo(() => {
    const rawRange = sp.get("range") as RangeKey | null;
    const drillWindow = strictIsoDateRange(
      readDateParam(sp, "od_from"), readDateParam(sp, "od_to"),
    );
    return {
      rangeKey: rawRange && RANGE_KEYS.includes(rawRange) ? rawRange : "30d",
      from: readDateParam(sp, "from"),
      to: readDateParam(sp, "to"),
      partId: readIntParam(sp, "part_id"),
      partPn: sp.get("pn"),
      poolId: readIntParam(sp, "pool"),
      salesperson: sp.get("sp") || null,
      purchaser: sp.get("buyer") || null,
      // 下钻必须是完整正向闭区间；半开/逆序/坏日期整体失效，绝不下发。
      drillFrom: drillWindow?.from ?? null,
      drillTo: drillWindow?.to ?? null,
      granularity: (["day", "week", "month"].includes(sp.get("gran") || "") ? sp.get("gran") : "day") as BoardFilters["granularity"],
      costMethod: sp.get("cost") === "fifo" ? "fifo" : "moving_avg",
    };
  }, [sp]);

  /** 全局统计窗口（KPI/趋势/榜/池），订单块另叠 drill 覆盖 */
  const dateRange = useMemo(
    () => rangeToDates(filters.rangeKey, filters.from, filters.to),
    [filters.rangeKey, filters.from, filters.to]);

  /** 订单块窗口：趋势点击下钻优先于全局时间 */
  const ordersRange: DateRange = useMemo(
    () => (filters.drillFrom && filters.drillTo
      ? { date_from: filters.drillFrom, date_to: filters.drillTo }
      : dateRange),
    [filters.drillFrom, filters.drillTo, dateRange]);

  /** 增量改 URL。筛选变化 push 进历史（前进/后退可恢复）；replace=true 用于展示偏好。 */
  const patch = useCallback((next: Record<string, string | number | null | undefined>,
                             opts: { replace?: boolean } = {}) => {
    const merged = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(next)) {
      if (v === undefined || v === null || v === "") merged.delete(k);
      else merged.set(k, String(v));
    }
    setSp(merged, { replace: opts.replace ?? false });
  }, [sp, setSp]);

  const clearAll = useCallback(() => {
    // 清除筛选：回到默认 30 天全量视角（保留展示偏好 gran/cost）
    const merged = new URLSearchParams();
    const gran = sp.get("gran"); const cost = sp.get("cost");
    if (gran) merged.set("gran", gran);
    if (cost) merged.set("cost", cost);
    setSp(merged, { replace: false });
  }, [sp, setSp]);

  // granularity/costMethod 是展示偏好，clearAll 会保留；其余范围/业务条件都应可清除。
  const hasFilter = filters.rangeKey !== "30d" || !!(filters.from || filters.to
    || filters.partId || filters.poolId || filters.salesperson
    || filters.purchaser || filters.drillFrom || filters.drillTo
    || sp.has("od_from") || sp.has("od_to"));

  return { filters, dateRange, ordersRange, patch, clearAll, hasFilter };
}

// ---------------------------------------------------------------- 竞态守卫取数

export interface BlockState<T> {
  data: T | null;
  loading: boolean;
  /** 接口失败信息；与"空数据"严格分离 */
  error: string | null;
}

/**
 * 带代次守卫的板块取数：deps 变化 → 旧请求即便最后返回也不得覆盖新数据；
 * 卸载后不再 setState。四态由调用方渲染：loading / error / 空 / 数据。
 */
export function useGuardedFetch<T>(fetcher: () => Promise<{ data: T }>, deps: unknown[]) {
  const [state, setState] = useState<BlockState<T>>({ data: null, loading: true, error: null });
  const gen = useRef(0);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const g = ++gen.current;
    setState((s) => ({ ...s, loading: true, error: null }));
    fetchRef.current()
      .then(({ data }) => { if (g === gen.current) setState({ data, loading: false, error: null }); })
      .catch((e) => {
        if (g === gen.current) {
          const msg = e?.response?.status ? `接口错误（${e.response.status}）` : "网络错误";
          setState({ data: null, loading: false, error: msg });
        }
      });
    return () => { gen.current += 1; };   // deps 变化/卸载即作废在途请求
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}

// ---------------------------------------------------------------- 权限三态渲染

export const MUTED: React.CSSProperties = { color: "var(--mb-text-3)", fontSize: 12.5 };

/** 金额三态：无权限（明确标注，绝不与"暂无数据"混淆）/ 无数据 / 有值 */
export function fmtMoneyR(v: number | null | undefined, restricted: boolean,
                          restrictedText = "无权限"): ReactNode {
  if (restricted) return <span style={MUTED} aria-label={restrictedText}>{restrictedText}</span>;
  if (v == null) return <span style={MUTED}>{EMPTY}</span>;
  return moneyExact(v);
}

/** 登录时落到 localStorage 的 data_* 权限（App.tsx 同源）。仅用于首渲染 UI 门控；
 * 数据面真值源永远是后端（响应旗标 + 字段脱敏 + 排序退回），本地值过期也不泄漏。 */
export function readLocalPerms(): Record<string, boolean> {
  try { return JSON.parse(localStorage.getItem("permissions") || "{}"); } catch { return {}; }
}

export function useLocalRestrictions() {
  const isAdmin = (localStorage.getItem("role") || "") === "admin";
  const perms = useMemo(readLocalPerms, []);
  return {
    isAdmin,
    poolManagement: isAdmin || !!perms.action_pool_manage || !!perms.action_pool_set_policy,
    profit: !isAdmin && perms.data_profit === false,
    cost: !isAdmin && perms.data_purchase_cost === false,
    governance: !isAdmin && perms.data_pool_price_governance === false,
    customer: !isAdmin && perms.data_customer === false,
    supplier: !isAdmin && perms.data_supplier === false,
  };
}

// ---------------------------------------------------------------- 价格参考状态

/** 状态用文字+颜色双编码（不依赖颜色单独表达异常） */
export const REFERENCE_STATUS_META: Record<ReferenceStatus, { text: string; color?: string; hint?: string }> = {
  above_manual_max: { text: "超采购上限", color: "red", hint: "高于人工最高采购价（历史分析，不拦截）" },
  below_manual_min: { text: "破销售下限", color: "red", hint: "低于人工最低销售价（历史分析，不拦截）" },
  above_pool_average: { text: "高于池均价", color: "orange" },
  below_pool_average: { text: "低于池均价", color: "orange" },
  within_limit: { text: "约束内", color: "green" },
  within_pool_average: { text: "不劣于池均价", color: "green" },
  no_manual_limit: { text: "无约束价", hint: "该池未设人工约束价，仅与池均价比较" },
  no_pool_average: { text: "无池均价" },
  no_price: { text: "无价格", hint: "¥0 赠送/换货行，不构成价格信号" },
  no_pool: { text: "未入池" },
};

export function ReferenceStatusTag({ status }: { status: ReferenceStatus | null | undefined }) {
  if (!status) return <span style={MUTED}>{EMPTY}</span>;
  const meta = REFERENCE_STATUS_META[status] ?? { text: status };
  const tag = <Tag color={meta.color}>{meta.text}</Tag>;
  return meta.hint ? <Tooltip title={meta.hint}>{tag}</Tooltip> : tag;
}

const VIOLATION_STATUSES: ReferenceStatus[] = ["above_manual_max", "below_manual_min"];
const WARN_STATUSES: ReferenceStatus[] = ["above_pool_average", "below_pool_average"];

/** 订单级历史分析概要：越线行数 > 劣于池均价行数 > 正常。
 * 对无成本权限的账号仍可见（状态是标签非金额，后端已保证不可反推金额）。 */
export function orderReferenceSummary(
  parts: Array<{ reference_status: ReferenceStatus; in_stats_scope: boolean }> | undefined,
  partsRestricted: boolean,
): ReactNode {
  if (partsRestricted) return <Tag>无明细权限</Tag>;
  if (!parts || parts.length === 0) return <span style={MUTED}>{EMPTY}</span>;
  const scoped = parts.filter((p) => p.in_stats_scope);
  const violations = scoped.filter((p) => VIOLATION_STATUSES.includes(p.reference_status)).length;
  const warns = scoped.filter((p) => WARN_STATUSES.includes(p.reference_status)).length;
  if (violations > 0) return <Tag color="red">越线 {violations} 行</Tag>;
  if (warns > 0) return <Tag color="orange">劣于池均价 {warns} 行</Tag>;
  if (scoped.some((p) => ["within_limit", "within_pool_average", "no_manual_limit"].includes(p.reference_status))) {
    return <Tag color="green">正常</Tag>;
  }
  return <span style={MUTED}>无池参考</span>;
}

// ---------------------------------------------------------------- 杂项

/** 趋势点击下钻：桶末不得越过全局 date_to 或今天。 */
export function drillRangeOf(
  period: string,
  granularity: "day" | "week" | "month",
  bounds: { dateFrom?: string; dateTo?: string; today?: string } = {},
): { from: string; to: string } {
  const bucketStart = dayjs(period);
  let start = bucketStart;
  let end = granularity === "week" ? bucketStart.add(6, "day")
    : granularity === "month" ? bucketStart.endOf("month") : bucketStart;
  if (isStrictIsoDate(bounds.dateFrom)) {
    const lower = dayjs(bounds.dateFrom);
    if (start.isBefore(lower, "day")) start = lower;
  }
  const today = isStrictIsoDate(bounds.today) ? dayjs(bounds.today) : dayjs().startOf("day");
  const caps = [today, ...(isStrictIsoDate(bounds.dateTo) ? [dayjs(bounds.dateTo)] : [])];
  for (const cap of caps) if (end.isAfter(cap, "day")) end = cap;
  if (end.isBefore(start, "day")) end = start;
  return { from: start.format(D), to: end.format(D) };
}

/** 互通池详情唯一深链构造器：所有入口一致保留当前统计窗口。 */
export function poolAnalysisPath(groupId: number, range?: DateRange): string {
  const query = new URLSearchParams();
  const window = strictIsoDateRange(range?.date_from, range?.date_to);
  if (window) {
    query.set("from", window.from);
    query.set("to", window.to);
  }
  const suffix = query.toString();
  return `/pool-analysis/${groupId}${suffix ? `?${suffix}` : ""}`;
}

export const RANGE_OPTIONS = [
  { label: "今天", value: "today" },
  { label: "近7天", value: "7d" },
  { label: "近30天", value: "30d" },
  { label: "本月", value: "month" },
  { label: "自定义", value: "custom" },
];
