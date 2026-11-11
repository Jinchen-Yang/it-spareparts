import { businessToday, formatBusinessDate } from "../../utils/date";

/**
 * 作废前「商城已手工减库存」要不要出现（契约第 5 节）。
 * 维修员响应没有推送状态，先按 omit；服务端若返回下面两句，再改成 required 或 forbid。
 */
export const SHOP_STOCK_REQUIRED_DETAIL =
  "修好数量可能已计入商城库存，请先在商城手工减库存并勾选确认";

export const SHOP_STOCK_FORBIDDEN_DETAIL =
  "这笔推送还没入账，作废时不要勾选。请等商城补上经办人并盘点减回、核对在手后，再登记";

/** 商城明确没加库存的 block_code。publish_disabled 不在这里，那种 blocked 已经加过库存。 */
const UNSTOCKED_BLOCK_CODES = new Set([
  "missing_catalog_identity",
  "missing_formal_image",
  "missing_category_mapping",
  "missing_list_price",
  "publish_api_missing_stock_update",
]);

export interface ShopStockIntent {
  status: string;
  attempts: number;
  block_code?: string | null;
  last_error_code?: string | null;
  receipt_status?: string | null;
}

/** omit：不显示勾选，请求里送 false。required：必须勾选。forbid：missing_operator，禁止勾选。 */
export type ShopStockCheck = "omit" | "required" | "forbid";

function provedAdded(intent: ShopStockIntent): boolean {
  return intent.status === "delivered"
    || intent.receipt_status === "applied"
    || intent.receipt_status === "unchanged"
    || intent.block_code === "publish_disabled";
}

function definitelyUnstocked(intent: ShopStockIntent): boolean {
  if (provedAdded(intent)) return false;
  const block = intent.block_code ?? null;
  if (intent.receipt_status === "blocked" && block !== null && UNSTOCKED_BLOCK_CODES.has(block)) return true;
  // HTTP 409 会回滚，不留下已入账的投递行。已入账的情况在 provedAdded 里先返回。
  if (intent.last_error_code === "remote_busy") return true;
  // conflict / 422 落成 failed，且回执不是 applied / unchanged。
  if (intent.status === "failed") return true;
  if (intent.attempts === 0 && intent.status === "pending") return true;
  return false;
}

export function shopStockCheck(intent: ShopStockIntent | null | undefined): ShopStockCheck {
  if (!intent) return "omit";
  // 延迟入库单独处理：即使 attempts > 0 也不能在作废时勾选。
  if (intent.block_code === "missing_operator") return "forbid";
  if (provedAdded(intent)) return "required";
  if (definitelyUnstocked(intent)) return "omit";
  // 应答可能丢了。delivering 的租约是否过期接口不返回，不在这里猜；页面提示稍后再试，
  // 真要勾选时由服务端那句 required detail 把勾选放出来。
  if (intent.attempts > 0 && intent.status === "pending") return "required";
  return "omit";
}

export function visibleShopStockCheck(
  intent: ShopStockIntent | null | undefined,
  serverDetail?: string | null,
): ShopStockCheck {
  if (serverDetail === SHOP_STOCK_REQUIRED_DETAIL) return "required";
  if (serverDetail === SHOP_STOCK_FORBIDDEN_DETAIL) return "forbid";
  return shopStockCheck(intent);
}

export function detailOf(error: unknown, fallback = "操作没有完成"): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" && detail.trim() ? detail : fallback;
}

/** 4xx 不写幂等行，可以换内容重试。没收到响应或 5xx 时同一内容必须复用原键。 */
export function commandOutcome(error: unknown): "rejected" | "unknown" {
  const status = (error as { response?: { status?: number } })?.response?.status;
  if (typeof status !== "number" || status >= 500) return "unknown";
  return "rejected";
}

export interface CommandAttempt {
  fingerprint: string;
  key: string;
  unknown: boolean;
}

/**
 * 同一次失败请求复用键。内容变了才换新键。
 * 结果未知时不许改内容再发，否则原请求若已成功，新键会再记一笔。
 */
export function nextCommandAttempt(
  current: CommandAttempt | null,
  fingerprint: string,
  mint: () => string,
): { attempt: CommandAttempt; blocked: boolean } {
  if (current?.unknown && current.fingerprint !== fingerprint) {
    return { attempt: current, blocked: true };
  }
  if (current && current.fingerprint === fingerprint) {
    return { attempt: current, blocked: false };
  }
  return { attempt: { fingerprint, key: mint(), unknown: false }, blocked: false };
}

export function settleCommandAttempt(
  attempt: CommandAttempt,
  outcome: "ok" | "rejected" | "unknown",
): CommandAttempt | null {
  if (outcome === "ok") return null;
  if (outcome === "unknown") return { ...attempt, unknown: true };
  return { ...attempt, unknown: false };
}

export const UNKNOWN_ATTEMPT_MESSAGE = "上次提交没有收到结果，请保持原来的内容再试一次";

export function newIdempotencyKey(prefix: string): string {
  const suffix = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`.slice(0, 128);
}

/** 填写人只能作废 Asia/Shanghai 当天的记录。主管不受这一条限制。 */
export function createdOnBusinessToday(createdAt: string, now: Date = new Date()): boolean {
  const day = formatBusinessDate(createdAt);
  if (day === "—") return false;
  return day === businessToday(now).format("YYYY-MM-DD");
}

export function repairQtyError(fixed: number, scrap: number, pending: number): string | null {
  if (!Number.isInteger(fixed) || !Number.isInteger(scrap) || fixed < 0 || scrap < 0 || fixed > 1000 || scrap > 1000) {
    return "修好件数和废品件数须为 0–1000 的整数";
  }
  if (fixed + scrap < 1) return "至少填写一件";
  if (fixed + scrap > pending) return "修好数与废品数之和超过当前待修件数";
  return null;
}

export function voidReasonError(reason: string): string | null {
  const text = reason.trim();
  if (!text) return "请填写作废原因";
  if ([...text].length > 256) return "作废原因最长 256 字";
  return null;
}
