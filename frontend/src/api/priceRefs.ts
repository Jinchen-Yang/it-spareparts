/**
 * 售前询价 M3 价格参考客户端（`POST /parts/price-refs`）。
 *
 * 售前询价、报价页和型号联想会一次给一批 PN；同一事件循环 tick 内多个组件
 * 各自调用 `fetchPriceRefs` 时合并成一次请求（每个请求最多 200 个 id）。
 * 结果按账号 + 窗口做内存缓存，TTL 60 秒；退出登录经
 * `clearSessionScopedPreferences()` 统一清空（本模块加载时注册清理钩子）。
 */
import { api } from "../api";
import { registerSessionCacheClearer } from "../sessionPreferences";

export const PRICE_REF_CACHE_TTL_MS = 60_000;
export const PRICE_REF_MAX_BATCH = 200;

export interface PriceRefSide {
  avg_inc_tax: number | null;
  avg_ex_tax: number | null;
  sample_count: number;
  order_count: number;
  latest_date: string | null;
  oldest_date: string | null;
}

export interface InquiryRef {
  min_money: number | null;
  max_money: number | null;
  last_money: number | null;
  count: number;
}

export interface PriceRefWindow {
  start: string;
  end: string;
  days: number;
}

export interface PriceRef {
  sale: PriceRefSide | null;
  purchase: PriceRefSide | null;
  purchase_restricted: boolean;
  inquiry_ref: InquiryRef;
  window: PriceRefWindow;
  method: string;
}

type ItemMap = Record<number, PriceRef | null>;

interface PendingRequest {
  partIds: number[];
  windowDays: number;
  account: string;
  resolve: (values: ItemMap) => void;
  reject: (reason: unknown) => void;
}

interface CacheEntry {
  value: PriceRef | null;
  expiresAt: number;
}

const cache = new Map<string, CacheEntry>();
const pending: PendingRequest[] = [];
let flushScheduled = false;

const accountKey = () => localStorage.getItem("token") || "__anonymous__";

const cacheKey = (account: string, windowDays: number, partId: number) =>
  `${account}|${windowDays}|${partId}`;

const toNumber = (value: unknown): number | null => {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

const toCount = (value: unknown): number => {
  const parsed = toNumber(value);
  return parsed == null ? 0 : Math.max(0, Math.trunc(parsed));
};

const textOrNull = (value: unknown): string | null =>
  value == null ? null : String(value);

function normalizeSide(raw: unknown): PriceRefSide | null {
  if (!raw || typeof raw !== "object") return null;
  const side = raw as Record<string, unknown>;
  return {
    avg_inc_tax: toNumber(side.avg_inc_tax),
    avg_ex_tax: toNumber(side.avg_ex_tax),
    sample_count: toCount(side.sample_count),
    order_count: toCount(side.order_count),
    latest_date: textOrNull(side.latest_date),
    oldest_date: textOrNull(side.oldest_date),
  };
}

/** 后端 Decimal/日期统一收口为可展示的数字与 ISO 文本；缺项按 null，不猜 0 价。 */
export function normalizePriceRef(raw: unknown): PriceRef | null {
  if (!raw || typeof raw !== "object") return null;
  const item = raw as Record<string, unknown>;
  const inquiry = (item.inquiry_ref && typeof item.inquiry_ref === "object"
    ? item.inquiry_ref : {}) as Record<string, unknown>;
  const window = (item.window && typeof item.window === "object"
    ? item.window : {}) as Record<string, unknown>;
  return {
    sale: normalizeSide(item.sale),
    purchase: normalizeSide(item.purchase),
    purchase_restricted: item.purchase_restricted === true,
    inquiry_ref: {
      min_money: toNumber(inquiry.min_money),
      max_money: toNumber(inquiry.max_money),
      last_money: toNumber(inquiry.last_money),
      count: toCount(inquiry.count),
    },
    window: {
      start: textOrNull(window.start) || "",
      end: textOrNull(window.end) || "",
      days: toCount(window.days),
    },
    method: typeof item.method === "string" ? item.method : "",
  };
}

const chunk = <T,>(items: T[], size: number): T[][] => {
  const chunks: T[][] = [];
  for (let index = 0; index < items.length; index += size) {
    chunks.push(items.slice(index, index + size));
  }
  return chunks;
};

function scheduleFlush() {
  if (flushScheduled) return;
  flushScheduled = true;
  queueMicrotask(() => { void flush(); });
}

async function flush() {
  flushScheduled = false;
  if (pending.length === 0) return;
  const requests = pending.splice(0, pending.length);
  const groups = new Map<string, {
    account: string;
    windowDays: number;
    requests: PendingRequest[];
  }>();
  for (const request of requests) {
    const key = `${request.account}|${request.windowDays}`;
    const group = groups.get(key);
    if (group) group.requests.push(request);
    else groups.set(key, {
      account: request.account,
      windowDays: request.windowDays,
      requests: [request],
    });
  }
  try {
    await Promise.all([...groups.values()].map(flushGroup));
  } catch (error) {
    // 未预期的实现错误也必须落到调用方，避免 pending promise 永久悬挂。
    for (const request of requests) request.reject(error);
  }
}

async function flushGroup(group: {
  account: string;
  windowDays: number;
  requests: PendingRequest[];
}) {
  const partIds = [...new Set(group.requests.flatMap((request) => request.partIds))];
  const values: ItemMap = {};
  let failure: unknown = null;
  for (const batch of chunk(partIds, PRICE_REF_MAX_BATCH)) {
    try {
      const response = await api.post<{ items?: Record<string, unknown> }>(
        "/parts/price-refs",
        { part_ids: batch, window_days: group.windowDays },
      );
      const items = response?.data?.items ?? {};
      const expiresAt = Date.now() + PRICE_REF_CACHE_TTL_MS;
      for (const partId of batch) {
        const value = normalizePriceRef(items[String(partId)]);
        values[partId] = value;
        cache.set(cacheKey(group.account, group.windowDays, partId), {
          value,
          expiresAt,
        });
      }
    } catch (error) {
      failure = error;
      break;
    }
  }
  for (const request of group.requests) {
    if (failure) {
      request.reject(failure);
      continue;
    }
    const resolved: ItemMap = {};
    for (const partId of request.partIds) resolved[partId] = values[partId] ?? null;
    request.resolve(resolved);
  }
}

/** 清空价格参考内存缓存；退出登录与 401 踢线经 sessionPreferences 调用。 */
export function clearPriceRefCache() {
  cache.clear();
}

registerSessionCacheClearer(clearPriceRefCache);

/**
 * 批量取参考价。同 tick 的调用合并；命中缓存（账号 + 窗口）不再发请求。
 * 返回以 part_id 为键的映射；后端未返回的 id（不存在/已合并）为 null。
 */
export function fetchPriceRefs(
  partIds: number[],
  opts: { windowDays?: number } = {},
): Promise<ItemMap> {
  const windowDays = opts.windowDays ?? 30;
  const uniqueIds = [...new Set(partIds.filter((id) => Number.isInteger(id) && id > 0))];
  if (uniqueIds.length === 0) return Promise.resolve({});
  const account = accountKey();
  const now = Date.now();
  const result: ItemMap = {};
  const misses: number[] = [];
  for (const partId of uniqueIds) {
    const entry = cache.get(cacheKey(account, windowDays, partId));
    if (entry && entry.expiresAt > now) result[partId] = entry.value;
    else misses.push(partId);
  }
  if (misses.length === 0) return Promise.resolve(result);
  return new Promise<ItemMap>((resolve, reject) => {
    pending.push({
      partIds: misses,
      windowDays,
      account,
      resolve: (values) => resolve({ ...result, ...values }),
      reject,
    });
    scheduleFlush();
  });
}
